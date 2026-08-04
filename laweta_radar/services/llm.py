"""Cienka warstwa nad dostawcami LLM — JEDNA funkcja, trzy implementacje.

    zapytaj(system, user, max_tokens) -> str

Bierze prompt systemowy i treść użytkownika, oddaje SUROWY tekst odpowiedzi.
Parsowanie JSON-a, walidacja pól i decyzja, co zrobić z błędem, zostają
W KLASYFIKATORZE — wspólne dla wszystkich providerów. Gdyby każdy provider
parsował po swojemu, porównanie modeli mierzyłoby różnice w NASZYM kodzie
zamiast w modelach.

PO CO TA WARSTWA — powód jest POMIAROWY, nie estetyczny:

Różnica w cenie między najdroższym a najtańszym modelem przy naszym wolumenie
to około 25 zł miesięcznie, czyli mniej niż szum. Różnica w JAKOŚCI na polskich
postach pisanych bez ogonków, z literówkami i skrótami drogowymi może być duża
— i nie da się jej przewidzieć z benchmarków, bo żaden nie mierzy „wyciąganie
miejscowości z posta laweciarskiego". Dlatego mierzymy na SWOICH danych:
`scripts/porownaj_modele.py` puszcza ten sam zbiór postów przez wszystkie
skonfigurowane providery i wypisuje tabelę. Bez wymienialnego providera taki
pomiar wymagałby przepisania klasyfikatora trzy razy.

DWIE FUNKCJE, NIE JEDNA. `zapytaj` ma kształt z zadania i jest tym, czego używa
klasyfikator. `zapytaj_ze_zuzyciem` oddaje dodatkowo tokeny i czas — bez tego
porównywarka nie policzyłaby ani kosztu, ani mediany opóźnienia, a to są dwie
z pięciu liczb, dla których ona istnieje. Pierwsza jest cienkim opakowaniem
drugiej, więc obie ścieżki są dokładnie tym samym kodem.

ZALEŻNOŚCI PROVIDERÓW SĄ OPCJONALNE. W requirements.txt siedzi tylko `anthropic`.
Brak SDK innego providera ma dać CZYTELNY KOMUNIKAT PRZY STARCIE (`problemy()`,
`check_setup.sh`), a nie `ImportError` w środku runu o trzeciej w nocy — dlatego
import każdego SDK jest leniwy i zamknięty w funkcji.

CLI:
    python -m laweta_radar.services.llm            # co jest skonfigurowane
    python -m laweta_radar.services.llm "pytanie"  # jeden strzał (wymaga sieci)
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from laweta_radar.config import settings

ANTHROPIC = "anthropic"
OPENAI = "openai"
GEMINI = "gemini"
PROVIDERY = (ANTHROPIC, OPENAI, GEMINI)


class LLMNiedostepny(RuntimeError):
    """Model nie odpowiedział — awaria API, timeout, brak klucza albo brak SDK.

    JEDEN typ na wszystkie te przypadki, bo wołający robi z nimi to samo:
    zostawia post bez klasyfikacji i próbuje w kolejnym runie. Rozróżnianie
    „padło API" od „nie ma klucza" ma sens przy STARCIE (patrz `problemy()`),
    a nie w środku pętli po postach.
    """


@dataclass(frozen=True)
class Odpowiedz:
    """Surowa odpowiedź modelu wraz z tym, co potrzebne do rozliczenia runu.

    `tokeny_*` bywają zerami, gdy provider nie odda licznika — wtedy koszt
    wychodzi 0 i raport pokazuje to jako brak danych, a nie jako „za darmo".
    """

    tekst: str
    provider: str
    model: str
    ms: int
    tokeny_wejscie: int = 0
    tokeny_wyjscie: int = 0


# ---------------------------------------------------------------------------
# CENNIK — USD za milion tokenów, osobno wejście i wyjście.
#
# Wpisane są WYŁĄCZNIE modele Anthropic, bo tylko dla nich mamy pewne stawki
# w repo. Cennik OpenAI i Google świadomie NIE jest tu zgadywany: zła stawka
# nie wywala niczego, tylko po cichu przekłamuje jedyną liczbę, dla której
# porównywarka istnieje — i decyzja o modelu zapada na zmyślonych danych.
# Brakujące modele dopisujesz przez CENNIK_EXTRA w .env (JSON), np.:
#   CENNIK_EXTRA={"gpt-5-mini": [0.25, 2.00], "gemini-2.5-flash": [0.30, 2.50]}
# Klucz dopasowujemy PREFIKSEM, żeby wariant z datą ("...-20251001") trafiał
# na tę samą stawkę co alias.
# ---------------------------------------------------------------------------
CENNIK_USD_ZA_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
}


def _cennik() -> dict[str, tuple[float, float]]:
    """Cennik wbudowany + nadpisania z CENNIK_EXTRA. Śmieć w .env -> ignorujemy."""
    pelny = dict(CENNIK_USD_ZA_MTOK)
    import json  # noqa: PLC0415 — potrzebny tylko tutaj

    surowy = settings.CENNIK_EXTRA
    if not surowy:
        return pelny
    try:
        dodatkowe = json.loads(surowy)
        for model, para in dodatkowe.items():
            wej, wyj = para
            pelny[str(model)] = (float(wej), float(wyj))
    except (ValueError, TypeError, KeyError):
        # Zła składnia nie może zatrzymać runu — raport pokaże koszt jako brak
        # danych i to wystarczy, żeby autor zauważył literówkę.
        pass
    return pelny


def koszt_usd(model: str, tokeny_wejscie: int, tokeny_wyjscie: int) -> float | None:
    """Koszt jednego wywołania w USD. None = nie znamy stawki dla tego modelu.

    None, a nie 0.0 — zero czyta się jako „za darmo" i cicho zaniża sumę runu.
    """
    ceny = _cennik()
    for prefiks, (wej, wyj) in sorted(ceny.items(), key=lambda p: -len(p[0])):
        if model.startswith(prefiks):
            return tokeny_wejscie / 1e6 * wej + tokeny_wyjscie / 1e6 * wyj
    return None


# ---------------------------------------------------------------------------
# KONFIGURACJA PROVIDERA
#
# Model trzymamy per provider, bo przy porównaniu chcemy odpalić WSZYSTKIE
# skonfigurowane naraz — jedna zmienna CLASSIFIER_MODEL nie da się do tego użyć.
# `CLASSIFIER_MODEL` zostaje nazwą modelu Anthropic (tak jest w .env od początku
# repo i tak wołają go istniejące narzędzia).
# ---------------------------------------------------------------------------
_KONFIGURACJA: dict[str, dict[str, str]] = {
    ANTHROPIC: {"klucz": "ANTHROPIC_API_KEY", "sdk": "anthropic", "paczka": "anthropic"},
    OPENAI: {"klucz": "OPENAI_API_KEY", "sdk": "openai", "paczka": "openai"},
    GEMINI: {"klucz": "GEMINI_API_KEY", "sdk": "google.genai", "paczka": "google-genai"},
}


def normalizuj_provider(surowy: str | None) -> str:
    """Nazwa providera -> jedna z PROVIDERY. Nieznana wartość degraduje do anthropic.

    Degradacja, a nie wyjątek: literówka w .env nie może zatrzymać crona, a
    domyślny provider jest jedynym, którego zależność jest w requirements.txt.
    """
    s = (surowy or "").strip().lower()
    return s if s in PROVIDERY else ANTHROPIC


def model_domyslny(provider: str | None = None) -> str:
    provider = normalizuj_provider(provider or settings.LLM_PROVIDER)
    if provider == OPENAI:
        return settings.OPENAI_MODEL
    if provider == GEMINI:
        return settings.GEMINI_MODEL
    return settings.CLASSIFIER_MODEL


def klucz(provider: str) -> str:
    nazwa = _KONFIGURACJA[normalizuj_provider(provider)]["klucz"]
    return getattr(settings, nazwa, "")


def _sdk_obecny(provider: str) -> bool:
    """Czy SDK providera da się zaimportować. Bez importowania go na stałe.

    `find_spec` zamiast `import` w try/except, bo import ma efekty uboczne
    (czyta środowisko, otwiera pliki konfiguracyjne) i przy samym sprawdzaniu
    konfiguracji nie chcemy żadnych.
    """
    import importlib.util  # noqa: PLC0415

    modul = _KONFIGURACJA[normalizuj_provider(provider)]["sdk"]
    try:
        return importlib.util.find_spec(modul) is not None
    except (ImportError, ValueError):
        return False


def problemy(provider: str | None = None) -> list[str]:
    """Czego brakuje, żeby ten provider ruszył. Pusta lista = gotowy.

    Wołane PRZY STARCIE (check_setup.sh, CLI klasyfikatora, porównywarka), bo
    brak paczki ma się objawić jednym zdaniem przed pierwszym postem, a nie
    tracebackiem w połowie runu.
    """
    provider = normalizuj_provider(provider or settings.LLM_PROVIDER)
    cfg = _KONFIGURACJA[provider]
    braki: list[str] = []
    if not klucz(provider):
        braki.append(f"brak {cfg['klucz']} w .env")
    if not _sdk_obecny(provider):
        braki.append(f"brak pakietu `{cfg['paczka']}` — zainstaluj: pip install {cfg['paczka']}")
    return braki


def gotowe_providery() -> list[str]:
    """Providery, które realnie da się odpalić — dla porównywarki modeli."""
    return [p for p in PROVIDERY if not problemy(p)]


def opis() -> str:
    """Jedna linia do logu startowego: kto jest wybrany i co jeszcze umiemy."""
    wybrany = normalizuj_provider(settings.LLM_PROVIDER)
    braki = problemy(wybrany)
    stan = "OK" if not braki else "; ".join(braki)
    return (f"[llm] provider={wybrany} model={model_domyslny(wybrany)} ({stan}), "
            f"gotowe do porównania: {', '.join(gotowe_providery()) or 'brak'}")


# ---------------------------------------------------------------------------
# IMPLEMENTACJE
#
# Każda robi DOKŁADNIE to samo: system osobno, treść użytkownika osobno, zwraca
# surowy tekst i liczniki tokenów. Rozdział system/user nie jest kosmetyką —
# treść posta z grupy FB to NIEZAUFANY input i nie wolno jej sklejać
# z instrukcjami (patrz prompt w workers/classifier.py).
# ---------------------------------------------------------------------------
def _anthropic(system: str, user: str, max_tokens: int, model: str) -> tuple[str, int, int]:
    import anthropic  # noqa: PLC0415

    klient = anthropic.Anthropic(api_key=klucz(ANTHROPIC))
    odp = klient.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    # `content` to LISTA bloków, nie string. Bierzemy tylko bloki tekstowe i
    # sklejamy — model potrafi oddać ich kilka, a wzięcie content[0] po cichu
    # ucięłoby resztę JSON-a.
    tekst = "".join(b.text for b in odp.content if getattr(b, "type", "") == "text")
    uz = getattr(odp, "usage", None)
    return tekst, getattr(uz, "input_tokens", 0) or 0, getattr(uz, "output_tokens", 0) or 0


def _openai(system: str, user: str, max_tokens: int, model: str) -> tuple[str, int, int]:
    import openai  # noqa: PLC0415

    klient = openai.OpenAI(api_key=klucz(OPENAI))
    wspolne = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        odp = klient.chat.completions.create(max_tokens=max_tokens, **wspolne)
    except Exception as e:  # noqa: BLE001
        # Nowsze modele OpenAI odrzucają `max_tokens` i wymagają
        # `max_completion_tokens`. Rozpoznajemy to po treści błędu i ponawiamy
        # RAZ — inaczej podmiana modelu w .env wymagałaby zmiany w kodzie,
        # czyli dokładnie tego, czemu ta warstwa ma zapobiegać.
        if "max_completion_tokens" not in str(e):
            raise
        odp = klient.chat.completions.create(max_completion_tokens=max_tokens, **wspolne)
    tekst = (odp.choices[0].message.content or "") if odp.choices else ""
    uz = getattr(odp, "usage", None)
    return tekst, getattr(uz, "prompt_tokens", 0) or 0, getattr(uz, "completion_tokens", 0) or 0


def _gemini(system: str, user: str, max_tokens: int, model: str) -> tuple[str, int, int]:
    from google import genai  # noqa: PLC0415
    from google.genai import types  # noqa: PLC0415

    klient = genai.Client(api_key=klucz(GEMINI))
    odp = klient.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
        ),
    )
    uz = getattr(odp, "usage_metadata", None)
    return (
        odp.text or "",
        getattr(uz, "prompt_token_count", 0) or 0,
        getattr(uz, "candidates_token_count", 0) or 0,
    )


_IMPLEMENTACJE = {ANTHROPIC: _anthropic, OPENAI: _openai, GEMINI: _gemini}


def zapytaj_ze_zuzyciem(
    system: str,
    user: str,
    max_tokens: int,
    provider: str | None = None,
    model: str | None = None,
) -> Odpowiedz:
    """Jak `zapytaj`, ale oddaje też tokeny i czas — do rozliczenia runu.

    `provider`/`model` są nadpisywalne argumentem WYŁĄCZNIE po to, żeby
    porównywarka mogła puścić ten sam post przez kilka modeli w jednym procesie.
    Klasyfikator ich nie podaje i bierze to, co w .env.
    """
    provider = normalizuj_provider(provider or settings.LLM_PROVIDER)
    model = model or model_domyslny(provider)

    braki = problemy(provider)
    if braki:
        # Ten sam wyjątek co awaria API — wołający i tak zostawia post
        # do ponowienia. Treść mówi wprost, czego brakuje.
        raise LLMNiedostepny(f"provider {provider}: " + "; ".join(braki))

    start = time.monotonic()
    try:
        tekst, wej, wyj = _IMPLEMENTACJE[provider](system, user, max_tokens, model)
    except LLMNiedostepny:
        raise
    except Exception as e:  # noqa: BLE001
        # Każdy błąd SDK (sieć, 429, 500, timeout) ma tu jeden typ, bo wołający
        # robi z nimi to samo. Klasa oryginalnego wyjątku zostaje w komunikacie,
        # żeby dało się odróżnić limit od awarii w logu.
        raise LLMNiedostepny(f"{provider}/{model}: {type(e).__name__}: {e}") from e
    ms = int((time.monotonic() - start) * 1000)
    return Odpowiedz(
        tekst=tekst,
        provider=provider,
        model=model,
        ms=ms,
        tokeny_wejscie=wej,
        tokeny_wyjscie=wyj,
    )


def zapytaj(system: str, user: str, max_tokens: int) -> str:
    """Prompt systemowy + treść użytkownika -> SUROWY tekst odpowiedzi.

    Kontrakt jest celowo ubogi: nic tu nie parsuje, nic nie waliduje i nic nie
    ponawia. Wszystko, co zależy od kształtu odpowiedzi, siedzi w kliencie
    (workers/classifier.py) — dzięki temu podmiana providera nie dotyka logiki.
    """
    return zapytaj_ze_zuzyciem(system, user, max_tokens).tekst


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    import sys  # noqa: PLC0415

    print(opis())
    for p in PROVIDERY:
        braki = problemy(p)
        stan = "gotowy" if not braki else "; ".join(braki)
        print(f"  {p:<10} model={model_domyslny(p) or '(brak)':<32} {stan}")

    pytanie = " ".join(argv[1:]).strip()
    if not pytanie:
        print("\nPodaj tekst argumentem, żeby wykonać jedno realne wywołanie (wymaga sieci).")
        return 0

    try:
        odp = zapytaj_ze_zuzyciem("Odpowiadaj krótko, po polsku.", pytanie, 300)
    except LLMNiedostepny as e:
        print(f"\n[llm] {e}", file=sys.stderr)
        return 0

    koszt = koszt_usd(odp.model, odp.tokeny_wejscie, odp.tokeny_wyjscie)
    print(f"\n{odp.tekst.strip()}\n")
    print(f"[{odp.provider}/{odp.model}] {odp.ms} ms, "
          f"tokeny {odp.tokeny_wejscie}->{odp.tokeny_wyjscie}, "
          f"koszt {f'${koszt:.6f}' if koszt is not None else 'nieznany (dopisz CENNIK_EXTRA)'}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
