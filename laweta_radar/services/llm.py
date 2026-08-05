"""Cienka warstwa nad dostawcami LLM — JEDNA funkcja, kilka implementacji.

    zapytaj(system, user, max_tokens) -> str

Bierze prompt systemowy i treść użytkownika, oddaje SUROWY tekst odpowiedzi.
Parsowanie JSON-a, walidacja pól i decyzja, co zrobić z błędem, zostają
W KLASYFIKATORZE — wspólne dla wszystkich providerów. Gdyby każdy provider
parsował po swojemu, porównanie modeli mierzyłoby różnice w NASZYM kodzie
zamiast w modelach. To nie jest estetyka: to jedyny powód, dla którego liczby
z `scripts/porownaj_modele.py` cokolwiek znaczą.

PO CO TA WARSTWA — powód jest POMIAROWY:

Różnica w cenie między najdroższym a najtańszym modelem przy naszym wolumenie
to około 25 zł miesięcznie, czyli mniej niż szum. Różnica w JAKOŚCI na polskich
postach pisanych bez ogonków, z literówkami i skrótami drogowymi może być duża
— i nie da się jej przewidzieć z benchmarków, bo żaden nie mierzy „wyciąganie
miejscowości z posta laweciarskiego". Dlatego mierzymy na SWOICH danych.

DWIE FUNKCJE, NIE JEDNA. `zapytaj` ma kształt z zadania i jest tym, czego używa
klasyfikator. `zapytaj_ze_zuzyciem` oddaje dodatkowo `Zuzycie` (tokeny + czas)
— bez tego porównywarka nie policzyłaby ani kosztu, ani mediany opóźnienia,
a to dwie z pięciu liczb, dla których ona istnieje. Pierwsza jest cienkim
opakowaniem drugiej, więc obie ścieżki to dokładnie ten sam kod.

ZALEŻNOŚCI PROVIDERÓW SĄ OPCJONALNE. W requirements.txt siedzi tylko `anthropic`.
Brak SDK innego providera ma dać CZYTELNY KOMUNIKAT PRZY STARCIE (`problemy()`,
`check_setup.sh`, `scripts/test_llm.py`), a nie `ImportError` w środku runu
o trzeciej w nocy — dlatego import każdego SDK jest leniwy i zamknięty
w funkcji.

BŁĘDY PRZEJŚCIOWE I TRWAŁE. `LLMNiedostepny` znaczy „spróbuj później" (limit
zapytań, timeout, 5xx). `LLMBladTrwaly` — jego podtyp — znaczy „ponawianie
tylko pali czas": zły klucz, nieznany model, odrzucony parametr. Wołający,
który łapie sam `LLMNiedostepny`, działa dalej bez zmian; ten, kto chce
zatrzymać run z konkretnym komunikatem, rozróżnia oba (patrz
`scripts/porownaj_modele.py`).

CLI:
    python -m laweta_radar.services.llm            # co jest skonfigurowane
    python -m laweta_radar.services.llm "pytanie"  # jeden strzał (wymaga sieci)
Sprawdzenie klucza, nazwy modelu i trybu JSON bez ruszania pipeline'u:
    python laweta_radar/scripts/test_llm.py
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, replace
from typing import Any, NamedTuple

from laweta_radar.config import cennik as _cennik_modul
from laweta_radar.config import settings

ANTHROPIC = "anthropic"
OPENAI = "openai"
GEMINI = "gemini"
PROVIDERY = (ANTHROPIC, OPENAI, GEMINI)

# Tryby JSON-a dla OpenAI (OPENAI_JSON_MODE). Opis i pułapka — przy `_response_format`.
JSON_OFF, JSON_OBJECT, JSON_SCHEMA = "off", "object", "schema"
TRYBY_JSON = (JSON_OFF, JSON_OBJECT, JSON_SCHEMA)


class LLMNiedostepny(RuntimeError):
    """Model nie odpowiedział — awaria API, timeout, brak klucza albo brak SDK.

    JEDEN typ na wszystkie te przypadki, bo wołający robi z nimi to samo:
    zostawia post bez klasyfikacji i próbuje w kolejnym runie. Rozróżnianie
    „padło API" od „nie ma klucza" ma sens przy STARCIE (patrz `problemy()`),
    a nie w środku pętli po postach.
    """


class LLMBladTrwaly(LLMNiedostepny):
    """Ponawianie nic nie da: zły klucz, nieznany model, odrzucony parametr.

    PODTYP, a nie osobna gałąź — kto łapie `LLMNiedostepny`, łapie i to, więc
    fetcher nie wymaga żadnej zmiany. Osobna klasa istnieje dla narzędzi, które
    mają PRZERWAĆ przebieg z jasnym komunikatem zamiast wisieć w ponawianiu:
    czterdzieści postów razy trzy próby na zły klucz to dwie minuty czekania na
    komunikat, który dało się wypisać po pierwszej odpowiedzi serwera.
    """


# ---------------------------------------------------------------------------
# ZUŻYCIE — bez tego porównanie modeli jest ślepe
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Zuzycie:
    """Rozliczenie jednego wywołania: tokeny, model, czas.

    KTÓRE TOKENY GDZIE (te same definicje co w config/cennik.py — jedno
    niedopatrzenie tutaj podwaja rachunek w tabeli porównawczej):
      • `tokeny_wejscia`     — wejście płatne pełną stawką, BEZ tego z cache;
      • `tokeny_cache`       — wejście przeczytane z cache (u obu dostawców 10%
                               stawki wejściowej);
      • `tokeny_wyjscia`     — CAŁE wyjście, RAZEM z tokenami rozumowania;
      • `tokeny_rozumowania` — podzbiór wyjścia, wystawiony OSOBNO.

    ROZUMOWANIE OSOBNO, BO INACZEJ PORÓWNANIE KŁAMIE. Model, który dużo myśli
    i mało pisze, ma niskie „wyjście" i wygląda w tabeli na tańszy i szybszy,
    niż jest — a płacimy za jego myślenie jak za wyjście i czekamy na nie tak
    samo. Do KOSZTU tokeny rozumowania wchodzą (siedzą w `tokeny_wyjscia`),
    do RAPORTU idą własną kolumną.

    Zera znaczą „provider nie oddał licznika", a nie „za darmo" — dlatego
    `koszt_usd()` przy nieznanym modelu zwraca None, nie 0.0.
    """

    tokeny_wejscia: int = 0
    tokeny_wyjscia: int = 0
    tokeny_cache: int = 0
    tokeny_rozumowania: int = 0
    model: str = ""
    ms: int = 0
    # Poza kontraktem z zadania, oba do RAPORTU: `tryb` niesie użyty tryb JSON,
    # bo przy "schema" nie porównujesz dwóch modeli, tylko dwa stacki — jeden
    # z gwarancją schematu i jeden bez. Wynik bez tej etykiety jest po miesiącu
    # nie do zestawienia z żadnym innym.
    provider: str = ""
    tryb: str = ""

    def koszt_usd(self) -> float | None:
        """Koszt tego wywołania w USD albo None, gdy nie znamy stawki modelu."""
        return _cennik_modul.koszt_usd(self.model, self.tokeny_wejscia,
                                       self.tokeny_wyjscia, self.tokeny_cache)

    def etykieta(self) -> str:
        """Nazwa do raportu — model RAZEM z trybem, nigdy sam.

        Punkt osobny w zadaniu i słusznie: dwa przebiegi tego samego modelu
        w różnych trybach JSON to dwa różne pomiary, a po miesiącu nikt nie
        pamięta, w którym co było ustawione.
        """
        podstawa = f"{self.provider}/{self.model}" if self.provider else self.model
        return f"{podstawa} [json={self.tryb}]" if self.tryb else podstawa


class Odpowiedz(NamedTuple):
    """Wynik `zapytaj_ze_zuzyciem`: (tekst, Zuzycie).

    KROTKA, ŻEBY DAŁO SIĘ ROZPAKOWAĆ — `tekst, zuzycie = zapytaj_ze_zuzyciem(...)`
    to kształt z zadania. Właściwości niżej istnieją, żeby `workers/classifier.py`
    i starsze wołania (`odp.tekst`, `odp.model`, `odp.tokeny_wejscie`) działały
    BEZ JEDNEJ ZMIANY. Warstwa, która przy dokładaniu providera każe poprawiać
    klasyfikator, nie robi tego, po co powstała.
    """

    tekst: str
    zuzycie: Zuzycie

    @property
    def provider(self) -> str:
        return self.zuzycie.provider

    @property
    def model(self) -> str:
        return self.zuzycie.model

    @property
    def ms(self) -> int:
        return self.zuzycie.ms

    @property
    def tokeny_wejscie(self) -> int:
        return self.zuzycie.tokeny_wejscia

    @property
    def tokeny_wyjscie(self) -> int:
        return self.zuzycie.tokeny_wyjscia


def koszt_usd(model: str, tokeny_wejscie: int, tokeny_wyjscie: int,
              tokeny_cache: int = 0) -> float | None:
    """Koszt wywołania w USD. None = nie znamy stawki (nigdy 0.0 po cichu).

    Przekierowanie do `config/cennik.py` — ceny nie mają mieszkać w kodzie,
    który woła sieć. Podpis został trzyargumentowy, bo tak wołają go istniejące
    narzędzia.
    """
    return _cennik_modul.koszt_usd(model, tokeny_wejscie, tokeny_wyjscie, tokeny_cache)


# ---------------------------------------------------------------------------
# KONFIGURACJA PROVIDERA
#
# Model trzymamy per provider, bo przy porównaniu chcemy odpalić WSZYSTKIE
# skonfigurowane naraz — jedna wspólna zmienna nie da się do tego użyć.
# `CLASSIFIER_MODEL` zostaje nazwą modelu Anthropic (tak jest w .env od początku
# repo i tak wołają go istniejące narzędzia).
# ---------------------------------------------------------------------------
_KONFIGURACJA: dict[str, dict[str, str]] = {
    ANTHROPIC: {"klucz": "ANTHROPIC_API_KEY", "sdk": "anthropic", "paczka": "anthropic",
                "model": "CLASSIFIER_MODEL"},
    OPENAI: {"klucz": "OPENAI_API_KEY", "sdk": "openai", "paczka": "openai",
             "model": "OPENAI_MODEL"},
    GEMINI: {"klucz": "GEMINI_API_KEY", "sdk": "google.genai", "paczka": "google-genai",
             "model": "GEMINI_MODEL"},
}


def _log(msg: str) -> None:
    print(f"[llm] {msg}", file=sys.stderr)


def normalizuj_provider(surowy: str | None) -> str:
    """Nazwa providera -> jedna z PROVIDERY. Nieznana wartość degraduje do anthropic.

    Degradacja, a nie wyjątek: literówka w .env nie może zatrzymać crona, a
    domyślny provider jest jedynym, którego zależność jest w requirements.txt.
    """
    s = (surowy or "").strip().lower()
    return s if s in PROVIDERY else ANTHROPIC


def model_domyslny(provider: str | None = None) -> str:
    """Nazwa modelu dla providera. Pusta = nie podano (patrz `problemy`).

    OPENAI_MODEL świadomie NIE MA wartości domyślnej. Nazwy modeli tej rodziny
    zmieniają się co kilka miesięcy, a domyślna w kodzie znaczyłaby, że po
    podmianie klucza system odpala model, którego nikt nie wybrał — i płaci za
    niego stawkę, której nikt nie sprawdzał.
    """
    provider = normalizuj_provider(provider or settings.LLM_PROVIDER)
    return getattr(settings, _KONFIGURACJA[provider]["model"], "")


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

    Wołane PRZY STARCIE (check_setup.sh, CLI klasyfikatora, porównywarka,
    scripts/test_llm.py), bo brak paczki albo pusta nazwa modelu ma się
    objawić jednym zdaniem przed pierwszym postem, a nie tracebackiem
    w połowie runu.
    """
    provider = normalizuj_provider(provider or settings.LLM_PROVIDER)
    cfg = _KONFIGURACJA[provider]
    braki: list[str] = []
    if not klucz(provider):
        braki.append(f"brak {cfg['klucz']} w .env")
    if not model_domyslny(provider):
        braki.append(f"brak {cfg['model']} w .env — nazwę modelu podaj świadomie, "
                     f"nie ma wartości domyślnej")
    if not _sdk_obecny(provider):
        braki.append(f"brak pakietu `{cfg['paczka']}` — zainstaluj: pip install {cfg['paczka']}")
    return braki


def gotowe_providery() -> list[str]:
    """Providery, które realnie da się odpalić — dla porównywarki modeli."""
    return [p for p in PROVIDERY if not problemy(p)]


def tryb_json(provider: str | None = None) -> str:
    """Tryb JSON dla tego providera. Poza OpenAI zawsze "off".

    Nieznana wartość degraduje do domyślnej z jednym ostrzeżeniem — ta sama
    zasada co przy providerze: literówka w .env nie zatrzymuje crona. Ale ma
    być WIDOCZNA, bo tryb wchodzi do raportu i cicha podmiana zafałszowałaby
    porównanie.
    """
    if normalizuj_provider(provider or settings.LLM_PROVIDER) != OPENAI:
        return JSON_OFF
    surowy = (settings.OPENAI_JSON_MODE or "").strip().lower()
    if not surowy:
        return JSON_OBJECT
    if surowy in TRYBY_JSON:
        return surowy
    _ostrzez_raz(f"OPENAI_JSON_MODE={surowy!r} spoza {TRYBY_JSON} -> {JSON_OBJECT!r}")
    return JSON_OBJECT


_ostrzezenia: set[str] = set()


def _ostrzez_raz(tresc: str) -> None:
    """Ostrzeżenie wypisywane RAZ na proces — inaczej 50 postów daje 50 kopii."""
    if tresc not in _ostrzezenia:
        _ostrzezenia.add(tresc)
        _log(tresc)


def opis() -> str:
    """Jedna linia do logu startowego: kto jest wybrany i co jeszcze umiemy."""
    wybrany = normalizuj_provider(settings.LLM_PROVIDER)
    braki = problemy(wybrany)
    stan = "OK" if not braki else "; ".join(braki)
    tryb = tryb_json(wybrany)
    return (f"[llm] provider={wybrany} model={model_domyslny(wybrany) or '(brak)'}"
            f"{f' json={tryb}' if wybrany == OPENAI else ''} ({stan}), "
            f"gotowe do porównania: {', '.join(gotowe_providery()) or 'brak'}")


# ---------------------------------------------------------------------------
# KLIENCI — jeden na proces, na (provider, klucz, timeout)
#
# Nie z oszczędności, tylko dla UCZCIWOŚCI POMIARU: klient tworzony przy każdym
# wywołaniu otwiera nowe połączenie TCP i nowy uścisk TLS, co dokłada
# kilkadziesiąt milisekund do każdego pomiaru. Mediana opóźnienia jest jedną
# z liczb, dla których porównywarka istnieje, i nie ma mierzyć naszego
# nawiązywania połączeń.
# ---------------------------------------------------------------------------
_klienci: dict[tuple, Any] = {}


def _klient(provider: str, buduj) -> Any:
    kluczyk = (provider, klucz(provider), settings.OPENAI_TIMEOUT_S)
    if kluczyk not in _klienci:
        _klienci[kluczyk] = buduj()
    return _klienci[kluczyk]


def zapomnij_klientow() -> None:
    """Wyrzuć zbuforowanych klientów (testy, podmiana klucza w tym samym procesie)."""
    _klienci.clear()


# ---------------------------------------------------------------------------
# IMPLEMENTACJE
#
# Każda robi DOKŁADNIE to samo: system osobno, treść użytkownika osobno, zwraca
# surowy tekst i liczniki. Rozdział system/user nie jest kosmetyką — treść posta
# z grupy FB to NIEZAUFANY input i nie wolno jej sklejać z instrukcjami
# (patrz prompt w workers/classifier.py).
# ---------------------------------------------------------------------------
def _anthropic(system: str, user: str, max_tokens: int, model: str) -> tuple[Zuzycie, str]:
    import anthropic  # noqa: PLC0415

    # Bez własnego timeoutu — zostaje domyślny z SDK. `OPENAI_TIMEOUT_S` nie
    # jest tu użyte świadomie: to zmienna z nazwą jednego providera i podpięcie
    # jej pod drugiego zmieniłoby po cichu zachowanie działającej produkcji,
    # a operator czytający .env nie miałby jak się o tym dowiedzieć.
    klient = _klient(ANTHROPIC, lambda: anthropic.Anthropic(api_key=klucz(ANTHROPIC)))
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

    # Obcięcie na limicie musi być widać JAKO OBCIĘCIE. Bez tego urwany
    # w połowie JSON dochodzi do klasyfikatora i ląduje w raporcie jako
    # „model nie umie w JSON" — czyli diagnoza modelu za błąd konfiguracji.
    if getattr(odp, "stop_reason", "") == "max_tokens":
        raise LLMNiedostepny(
            f"{ANTHROPIC}/{model}: odpowiedź obcięta na limicie {max_tokens} tokenów "
            f"— podnieś MAX_TOKENS, to NIE jest wina modelu")

    uz = getattr(odp, "usage", None)
    # Anthropic liczy inaczej niż OpenAI: `input_tokens` NIE zawiera tego, co
    # przyszło z cache — cache jest osobną pozycją. Dlatego tu dodajemy, a przy
    # OpenAI odejmujemy. Pomyłka w którąkolwiek stronę przesuwa koszt o rząd
    # wielkości na dokładnie tym samym ruchu.
    zapis_cache = getattr(uz, "cache_creation_input_tokens", 0) or 0
    return Zuzycie(
        # Zapis do cache kosztuje 1.25x wejścia; liczymy go jak zwykłe wejście,
        # bo ten system nie cache'uje promptu (pozycja zawsze wynosi 0). Gdyby
        # kiedyś zaczął, ta linia zaniża rachunek o 25% od zapisów.
        tokeny_wejscia=(getattr(uz, "input_tokens", 0) or 0) + zapis_cache,
        tokeny_wyjscia=getattr(uz, "output_tokens", 0) or 0,
        tokeny_cache=getattr(uz, "cache_read_input_tokens", 0) or 0,
        tokeny_rozumowania=0,  # rozszerzone myślenie nie jest tu włączone
        model=model,
        provider=ANTHROPIC,
    ), tekst


# --- OpenAI ----------------------------------------------------------------
#
# CO PROCES ZAPAMIĘTUJE. Ta rodzina modeli zmieniała nazwy parametrów między
# wersjami i nie da się tego wyczytać z nazwy modelu — „gpt-5" i „gpt-4o"
# wyglądają podobnie, a przyjmują co innego. Dlatego pytamy SERWER: próbujemy
# nowszej nazwy, a jeśli ją odrzuci, powtarzamy ze starszą i ZAPAMIĘTUJEMY
# odpowiedź na czas procesu. Zgadywanie z nazwy modelu zepsułoby się przy
# pierwszym modelu, o którym ten kod nie wie.
_NAZWA_LIMITU: dict[str, str] = {}          # model -> "max_completion_tokens" | "max_tokens"
_BEZ_ROZUMOWANIA: set[str] = set()          # modele, które odrzuciły reasoning_effort

# Parametry, które umiemy odstawić po odmowie serwera. Lista jest ZAMKNIĘTA:
# ponawianie po dowolnym błędzie 400 zamieniłoby literówkę w promptcie
# w nieskończoną pętlę płatnych wywołań.
_PARAMETRY_ADAPTOWANE = ("max_completion_tokens", "max_tokens", "reasoning_effort")

# Kody i zwroty, po których poznajemy „tego parametru nie znam / nie przyjmuję".
_KODY_ODRZUCENIA = ("unsupported_parameter", "unknown_parameter", "unsupported_value",
                    "invalid_value")
_ZWROTY_ODRZUCENIA = ("unsupported parameter", "unrecognized request argument",
                      "is not supported with this model", "unknown parameter",
                      "unsupported value", "invalid value")

# Zapas tokenów doliczany, gdy NIE sterujemy nakładem rozumowania.
#
# Te modele generują wewnętrzne tokeny rozumowania: rozliczane jak wyjściowe
# i wliczane do limitu. Przy zadaniu czysto ekstrakcyjnym to strata, ale bez
# zapasu limit wyczerpuje się PRZED napisaniem JSON-a i odpowiedź urywa się
# w połowie — w raporcie widać wtedy „model nie umie w JSON" zamiast „za mały
# limit". Zapas to SUFIT, nie wydatek: płacimy za tokeny wygenerowane, nie za
# dopuszczone, więc model nierozumujący nie traci na tym ani grosza.
ZAPAS_NA_ROZUMOWANIE = 2000


def _rola_systemowa() -> str:
    """Rola pierwszej wiadomości: "system" (domyślnie) albo "developer".

    Anthropic bierze prompt systemowy osobnym parametrem; OpenAI wkłada go do
    `messages` jako pierwszą wiadomość. W nowszych modelach ta rola nazywa się
    "developer", ale "system" jest nadal przyjmowane — dlatego domyślną
    zostawiamy zgodną wstecz, a przełącznik jest w .env, żeby dzień, w którym
    przestanie być, kosztował jedną linijkę konfiguracji, a nie deploy.
    """
    return (settings.OPENAI_ROLA_SYSTEMOWA or "").strip() or "system"


def _response_format(tryb: str) -> dict[str, Any] | None:
    """`response_format` dla wybranego trybu JSON albo None.

    ================================================================
    CZEGO TRYB JSON **NIE** GWARANTUJE — przeczytaj przed wyciąganiem wniosków
    z porównania modeli:

    Tryb JSON gwarantuje, że odpowiedź BĘDZIE poprawnym JSON-em o właściwych
    typach pól. NIE gwarantuje, że wartości są PRAWDZIWE. Model nadal może
    wpisać do `odbior.miasto` nazwę, której w poście nie było — dostaniesz ją
    tylko ładnie sformatowaną, przez co wygląda na pewniejszą niż była.
    Tryb JSON NIE zastępuje metryki halucynacji geo
    (`scripts/porownaj_modele.py`, kolumna „halucyn.").

    A zgadnięte miasto jest w tym systemie najdroższym możliwym błędem: wysyła
    człowieka 80 km w złą stronę, podczas gdy puste pole tylko każe mu
    przeczytać post.
    ================================================================

    UCZCIWOŚĆ PORÓWNANIA. Przy "schema" nie porównujesz dwóch modeli, tylko dwa
    stacki: jeden z gwarancją schematu i jeden bez. To legalne pytanie
    produkcyjne („czy warto to włączyć"), ale INNE niż „który model lepiej
    rozumie polskie posty". Dlatego tryb jedzie w `Zuzycie.tryb` i wychodzi
    w raporcie obok nazwy modelu — żeby po miesiącu nikt nie zestawił wyników
    z dwóch różnych ustawień.
    """
    if tryb == JSON_OBJECT:
        return {"type": "json_object"}
    if tryb == JSON_SCHEMA:
        from laweta_radar.services import schemat  # noqa: PLC0415 — patrz services/schemat.py

        return schemat.response_format()
    return None


def _odrzucony_parametr(blad: Exception) -> str:
    """Nazwa parametru, którego serwer nie przyjął. Pusty string = inny błąd.

    Najpierw `param` z odpowiedzi API (jest w niej wprost), potem nazwa
    wyłuskana z treści. Obie ścieżki wymagają dodatkowo, żeby błąd MÓWIŁ
    o nieznanym/nieprzyjętym parametrze — samo `param="max_completion_tokens"`
    bywa też przy „wartość za duża", a na to ponawianie z inną nazwą nie pomoże.
    """
    tresc = str(blad).lower()
    kod = str(getattr(blad, "code", "") or "").lower()
    if kod not in _KODY_ODRZUCENIA and not any(z in tresc for z in _ZWROTY_ODRZUCENIA):
        return ""
    param = str(getattr(blad, "param", "") or "").strip("'\" ")
    if param in _PARAMETRY_ADAPTOWANE:
        return param
    return next((n for n in _PARAMETRY_ADAPTOWANE if f"'{n}'" in tresc), "")


def _openai(system: str, user: str, max_tokens: int, model: str) -> tuple[Zuzycie, str]:
    import openai  # noqa: PLC0415

    klient = _klient(OPENAI, lambda: openai.OpenAI(
        api_key=klucz(OPENAI),
        timeout=float(settings.OPENAI_TIMEOUT_S),
        # Domyślne ponawianie SDK (429, 5xx, zerwane połączenie) zostawiamy —
        # to są dokładnie błędy PRZEJŚCIOWE. Błędów 4xx SDK nie ponawia, więc
        # zły klucz ani nieznany model nie kręcą się w pętli.
        max_retries=2,
    ))

    tryb = tryb_json(OPENAI)
    wspolne: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": _rola_systemowa(), "content": system},
            {"role": "user", "content": user},
        ],
    }
    format_odpowiedzi = _response_format(tryb)
    if format_odpowiedzi:
        wspolne["response_format"] = format_odpowiedzi

    odp, limit = _wolaj_openai(klient, wspolne, max_tokens, model)
    return _rozlicz_openai(odp, model, tryb, limit)


def _wolaj_openai(klient, wspolne: dict[str, Any], max_tokens: int, model: str):
    """Wywołanie z dopasowaniem parametrów do tego, co model realnie przyjmuje.

    Zwraca `(odpowiedź, użyty limit tokenów)` — limit wraca, bo komunikat
    o obcięciu ma podawać liczbę, która NAPRAWDĘ poszła do API, a nie tę
    z argumentu.

    Najwyżej trzy podejścia: nazwa limitu i sterowanie rozumowaniem to dwie
    niezależne rzeczy, z których każda może zostać odrzucona raz. Wynik każdej
    odmowy zapamiętujemy na czas procesu, więc drugi post płaci już tylko za
    jedno wywołanie.

    KAŻDEJ NAZWY LIMITU PRÓBUJEMY RAZ. Bez tego licznika 400 o innej przyczynie
    (np. „wartość za duża") wyglądałby jak spór o nazwę parametru i odbijał
    żądanie tam i z powrotem, płatnie, aż do wyczerpania pętli — a operator
    dostałby na końcu komunikat o parametrach zamiast o realnym błędzie.
    """
    probowane_nazwy: set[str] = set()
    for _ in range(len(_PARAMETRY_ADAPTOWANE)):
        parametry = dict(wspolne)
        wysilek = "" if model in _BEZ_ROZUMOWANIA else (settings.OPENAI_REASONING or "").strip()
        if wysilek:
            parametry["reasoning_effort"] = wysilek
        # Bez sterowania nakładem rozumowania podnosimy SUFIT, nie wydatek
        # — patrz ZAPAS_NA_ROZUMOWANIE. Liczone od `max_tokens` przy KAŻDYM
        # podejściu od nowa, nigdy narastająco.
        limit = max_tokens if wysilek else max_tokens + ZAPAS_NA_ROZUMOWANIE
        nazwa_limitu = _NAZWA_LIMITU.get(model, "max_completion_tokens")
        parametry[nazwa_limitu] = limit
        probowane_nazwy.add(nazwa_limitu)

        try:
            return klient.chat.completions.create(**parametry), limit
        except Exception as e:  # noqa: BLE001 — rozbieramy niżej, reszta leci dalej
            odrzucony = _odrzucony_parametr(e)
            if odrzucony in ("max_completion_tokens", "max_tokens"):
                nowa = ("max_tokens" if odrzucony == "max_completion_tokens"
                        else "max_completion_tokens")
                if nowa in probowane_nazwy:
                    raise  # obie nazwy już poszły — to nie jest spór o nazwę
                _NAZWA_LIMITU[model] = nowa
                _ostrzez_raz(f"{model}: limit tokenów przez {nowa!r} "
                             f"(serwer odrzucił {odrzucony!r})")
                continue
            if odrzucony == "reasoning_effort":
                _BEZ_ROZUMOWANIA.add(model)
                _ostrzez_raz(f"{model}: nie przyjmuje OPENAI_REASONING={settings.OPENAI_REASONING!r} "
                             f"— odstawiam ten parametr i podnoszę limit tokenów "
                             f"o {ZAPAS_NA_ROZUMOWANIE} na wypadek rozumowania")
                continue
            raise
    # Nie powinno się zdarzyć: każda ścieżka wyżej albo zwraca, albo rzuca.
    raise LLMBladTrwaly(f"{OPENAI}/{model}: nie udało się dobrać parametrów wywołania")


def _rozlicz_openai(odp, model: str, tryb: str, max_tokens: int) -> tuple[Zuzycie, str]:
    """Odpowiedź OpenAI -> (Zuzycie, tekst). Wyciąganie WSZYSTKIEGO defensywne.

    Pusta lista `choices` i `content=None` po zadziałaniu filtra treści to
    realne przypadki, nie teoria. Każdy z nich ma dać wyjątek złapany przez
    klasyfikator (post wraca do kolejki), a nie AttributeError w środku runu.
    """
    wybory = getattr(odp, "choices", None) or []
    if not wybory:
        raise LLMNiedostepny(f"{OPENAI}/{model}: odpowiedź bez `choices` — nie ma czego czytać")

    wybor = wybory[0]
    wiadomosc = getattr(wybor, "message", None)
    powod = str(getattr(wybor, "finish_reason", "") or "")

    odmowa = getattr(wiadomosc, "refusal", None)
    if odmowa:
        raise LLMNiedostepny(f"{OPENAI}/{model}: model odmówił odpowiedzi ({odmowa})")

    if powod == "content_filter":
        raise LLMNiedostepny(f"{OPENAI}/{model}: odpowiedź zablokowana przez filtr treści")
    if powod == "length":
        # Patrz komentarz przy obcięciu w `_anthropic`: obcięty JSON zdiagnozowany
        # jako „model nie umie w JSON" to godzina szukania nie tam, gdzie trzeba.
        raise LLMNiedostepny(
            f"{OPENAI}/{model}: odpowiedź obcięta na limicie {max_tokens} tokenów "
            f"(prawdopodobnie zjadły go tokeny rozumowania) — podnieś limit albo "
            f"ustaw niższy OPENAI_REASONING; to NIE jest wina modelu")

    tekst = getattr(wiadomosc, "content", None)
    if not tekst:
        raise LLMNiedostepny(f"{OPENAI}/{model}: pusta treść odpowiedzi "
                             f"(finish_reason={powod or 'brak'})")

    uz = getattr(odp, "usage", None)
    szczegoly_wej = getattr(uz, "prompt_tokens_details", None)
    szczegoly_wyj = getattr(uz, "completion_tokens_details", None)
    wejscie = getattr(uz, "prompt_tokens", 0) or 0
    z_cache = getattr(szczegoly_wej, "cached_tokens", 0) or 0
    return Zuzycie(
        # OpenAI liczy odwrotnie niż Anthropic: `prompt_tokens` ZAWIERA tokeny
        # z cache. Bez tego odjęcia płacilibyśmy za nie pełną stawkę w raporcie,
        # choć dostawca liczy dziesiątą część.
        tokeny_wejscia=max(0, wejscie - z_cache),
        tokeny_wyjscia=getattr(uz, "completion_tokens", 0) or 0,
        tokeny_cache=z_cache,
        # Tokeny rozumowania SIEDZĄ JUŻ w `completion_tokens` — tu tylko je
        # nazywamy, żeby raport pokazał je osobną kolumną. Dodanie ich do
        # wyjścia policzyłoby je drugi raz.
        tokeny_rozumowania=getattr(szczegoly_wyj, "reasoning_tokens", 0) or 0,
        model=model,
        provider=OPENAI,
        tryb=tryb,
    ), tekst


def _gemini(system: str, user: str, max_tokens: int, model: str) -> tuple[Zuzycie, str]:
    from google import genai  # noqa: PLC0415
    from google.genai import types  # noqa: PLC0415

    klient = _klient(GEMINI, lambda: genai.Client(api_key=klucz(GEMINI)))
    odp = klient.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
        ),
    )
    uz = getattr(odp, "usage_metadata", None)
    return Zuzycie(
        tokeny_wejscia=getattr(uz, "prompt_token_count", 0) or 0,
        tokeny_wyjscia=getattr(uz, "candidates_token_count", 0) or 0,
        tokeny_cache=getattr(uz, "cached_content_token_count", 0) or 0,
        tokeny_rozumowania=getattr(uz, "thoughts_token_count", 0) or 0,
        model=model,
        provider=GEMINI,
    ), (odp.text or "")


_IMPLEMENTACJE = {ANTHROPIC: _anthropic, OPENAI: _openai, GEMINI: _gemini}


# ---------------------------------------------------------------------------
# MAPOWANIE BŁĘDÓW
#
# PRZEJŚCIOWY (limit zapytań, timeout, 5xx) -> ponawiać ma sens: post wraca do
# kolejki i w kolejnym runie zwykle przechodzi.
# TRWAŁY (zły klucz, nieznany model, odrzucony parametr) -> ponawianie tylko
# pali czas. Ma zatrzymać przebieg z komunikatem mówiącym, co poprawić.
#
# Rozstrzygamy po KODZIE HTTP, nie po nazwie klasy wyjątku — SDK obu dostawców
# mają tę samą siatkę statusów, a nazwa klasy potrafi się zmienić między
# wersjami paczki. Dzięki temu ta jedna funkcja obsługuje wszystkich.
# ---------------------------------------------------------------------------
# Kształty kluczy API u dostawców, których obsługujemy. Regexp jest DRUGĄ linią
# obrony — pierwszą jest wycięcie realnie skonfigurowanych wartości — bo komunikat
# potrafi nieść klucz w formie, której nie mamy w .env (np. z innego środowiska).
_WZORZEC_SEKRETU = re.compile(r"sk-[A-Za-z0-9_\-]{6,}|AIza[A-Za-z0-9_\-]{10,}")


def zamaskuj(tekst: str) -> str:
    """Wytnij z tekstu wszystko, co wygląda na klucz API.

    POWÓD JEST KONKRETNY, NIE TEORETYCZNY: przy błędzie 401 OpenAI odsyła
    w treści CAŁY przekazany klucz ("Incorrect API key provided: sk-..."),
    a my wklejamy treść błędu do własnego komunikatu. Bez tej funkcji klucz
    ląduje w logu workera, w mailu od crona i w pliku, którego nikt nie rotuje
    — czyli jest do wyrzucenia, a nikt się o tym nie dowie.

    Wywalony klucz kosztuje mniej niż jego rotacja, więc maskujemy AGRESYWNIE:
    wszystko, co pasuje do kształtu, nawet gdyby przy okazji zniknął fragment
    komunikatu.
    """
    wynik = tekst or ""
    for provider in PROVIDERY:
        # Krótkiej wartości nie podstawiamy: `"".replace("", ...)` wstawiłoby
        # maskę między każdą parę znaków i zniszczyło komunikat.
        wartosc = klucz(provider)
        if len(wartosc) >= 8:
            wynik = wynik.replace(wartosc, "***")
    return _WZORZEC_SEKRETU.sub("***", wynik)


_WYJASNIENIA_HTTP: dict[int, str] = {
    400: "wywołanie odrzucone — sprawdź nazwę modelu i parametry",
    401: "klucz API odrzucony — sprawdź {klucz} w .env",
    403: "klucz nie ma dostępu do tego modelu — sprawdź uprawnienia projektu",
    404: "nieznany model {model!r} — sprawdź nazwę w {zmienna_modelu}",
    422: "wywołanie odrzucone jako niepoprawne",
}


def _zmapuj_blad(blad: Exception, provider: str, model: str) -> LLMNiedostepny:
    """Wyjątek SDK -> nasz wyjątek, z rozróżnieniem trwały/przejściowy."""
    cfg = _KONFIGURACJA[provider]
    kod = getattr(blad, "status_code", None)
    # `zamaskuj` NIE jest tu ozdobą — patrz jej docstring: przy 401 dostawca
    # odsyła w treści błędu przekazany klucz.
    opis_bledu = f"{provider}/{model}: {type(blad).__name__}: {zamaskuj(str(blad))}"

    # Wyczerpany limit konta przychodzi jako 429 — czyli tym samym kodem co
    # zwykłe „za dużo zapytań", które JEST przejściowe. Różnica jest w treści
    # i jest zasadnicza: doładowania konta nie załatwi żadna liczba ponowień.
    if kod == 429 and "insufficient_quota" in f"{getattr(blad, 'code', '')} {blad}".lower():
        return LLMBladTrwaly(f"{opis_bledu}\n  -> wyczerpany limit konta u dostawcy "
                             f"— ponawianie nic nie da")

    if kod in _WYJASNIENIA_HTTP:
        wskazowka = _WYJASNIENIA_HTTP[kod].format(
            klucz=cfg["klucz"], model=model, zmienna_modelu=cfg["model"])
        return LLMBladTrwaly(f"{opis_bledu}\n  -> {wskazowka}")

    # Brak klucza wykryty przez samo SDK (nie doszło do żadnego zapytania).
    if kod is None and "api_key" in str(blad).lower():
        return LLMBladTrwaly(f"{opis_bledu}\n  -> ustaw {cfg['klucz']} w .env")

    return LLMNiedostepny(opis_bledu)


# ---------------------------------------------------------------------------
# WEJŚCIE GŁÓWNE
# ---------------------------------------------------------------------------
def zapytaj_ze_zuzyciem(
    system: str,
    user: str,
    max_tokens: int,
    provider: str | None = None,
    model: str | None = None,
) -> Odpowiedz:
    """Jak `zapytaj`, ale oddaje też `Zuzycie` — do rozliczenia runu.

    Zwraca krotkę `(tekst, Zuzycie)`; da się ją i rozpakować, i odpytać
    o pola po nazwie (patrz `Odpowiedz`).

    `provider`/`model` są nadpisywalne argumentem WYŁĄCZNIE po to, żeby
    porównywarka mogła puścić ten sam post przez kilka modeli w jednym procesie.
    Klasyfikator ich nie podaje i bierze to, co w .env.
    """
    provider = normalizuj_provider(provider or settings.LLM_PROVIDER)
    model = model or model_domyslny(provider)

    braki = problemy(provider)
    if braki:
        # Brak konfiguracji jest z definicji TRWAŁY — dopóki nikt nie dopisze
        # klucza, żadna liczba ponowień nic nie zmieni.
        raise LLMBladTrwaly(f"provider {provider}: " + "; ".join(braki))

    start = time.monotonic()
    try:
        zuzycie, tekst = _IMPLEMENTACJE[provider](system, user, max_tokens, model)
    except LLMNiedostepny:
        raise
    except Exception as e:  # noqa: BLE001 — patrz `_zmapuj_blad`
        raise _zmapuj_blad(e, provider, model) from e

    return Odpowiedz(tekst, replace(zuzycie, ms=int((time.monotonic() - start) * 1000)))


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
    print(opis())
    for p in PROVIDERY:
        braki = problemy(p)
        stan = "gotowy" if not braki else "; ".join(braki)
        print(f"  {p:<10} model={model_domyslny(p) or '(brak)':<32} {stan}")

    pytanie = " ".join(argv[1:]).strip()
    if not pytanie:
        print("\nPodaj tekst argumentem, żeby wykonać jedno realne wywołanie (wymaga sieci).")
        print("Pełny test obu providerów naraz: python laweta_radar/scripts/test_llm.py")
        return 0

    try:
        tekst, zuzycie = zapytaj_ze_zuzyciem("Odpowiadaj krótko, po polsku.", pytanie, 300)
    except LLMNiedostepny as e:
        print(f"\n[llm] {e}", file=sys.stderr)
        return 0

    koszt = zuzycie.koszt_usd()
    print(f"\n{tekst.strip()}\n")
    print(f"[{zuzycie.etykieta()}] {zuzycie.ms} ms, "
          f"tokeny {zuzycie.tokeny_wejscia}->{zuzycie.tokeny_wyjscia}"
          f"{f' (w tym {zuzycie.tokeny_rozumowania} na rozumowanie)' if zuzycie.tokeny_rozumowania else ''}, "
          f"koszt {f'${koszt:.6f}' if koszt is not None else 'nieznany (dopisz do config/cennik.py)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
