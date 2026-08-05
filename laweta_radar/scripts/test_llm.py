#!/usr/bin/env python3
"""Jedno wywołanie na każdego providera — sprawdzenie, czy w ogóle jest z czym
rozmawiać, BEZ ruszania pipeline'u.

PO CO OSOBNY SKRYPT, skoro jest porównywarka modeli: porównywarka puszcza
czterdzieści postów przez każdy model i kosztuje kilkadziesiąt groszy oraz
kilka minut. Kiedy nie wiadomo, czy klucz jest dobry, czy nazwa modelu istnieje
i czy tryb JSON nie wywala błędu 400, to jest zła cena za odpowiedź „nie".
Tutaj jedno krótkie pytanie, jedna tabelka i wiadomo.

CO DOKŁADNIE SPRAWDZA — po jednym pytaniu na providera, tym samym dla wszystkich:
  • czy klucz działa i czy nazwa modelu istnieje (błąd 401/404 z komunikatem
    mówiącym, co poprawić, zamiast tracebacku);
  • czy odpowiedź da się sparsować jako JSON — tą samą ścieżką, której używa
    klasyfikator, nie własną;
  • ile tokenów poszło, z tokenami ROZUMOWANIA osobno (model, który dużo myśli
    i mało pisze, wygląda w tabeli na tańszy, niż jest);
  • ile to kosztowało i ile trwało.

Trwały błąd (zły klucz, nieznany model, odrzucony parametr) jest wypisany jako
TRWAŁY i nie jest ponawiany — ponawianie w tej sytuacji tylko pali czas.

UŻYCIE:
    export PYTHONPATH=$PWD
    python laweta_radar/scripts/test_llm.py              # wszystkie gotowe providery
    python laweta_radar/scripts/test_llm.py --provider openai
    python laweta_radar/scripts/test_llm.py --model gpt-5-mini --provider openai
    python laweta_radar/scripts/test_llm.py --sucho      # co by poszło, bez sieci
"""
from __future__ import annotations

import argparse
import json
import sys

try:                               # pakiet widoczny: -m, import pakietowy, testy
    from laweta_radar.scripts._sciezka import dodaj_repo_do_sciezki
except ImportError:                # uruchomienie po ścieżce do pliku
    from _sciezka import dodaj_repo_do_sciezki

dodaj_repo_do_sciezki()

from laweta_radar.services import llm  # noqa: E402

KTO = "test-llm"

# Krótkie i jednoznaczne, bo mierzymy DZIAŁANIE ŚCIEŻKI, nie inteligencję.
# Prompt systemowy musi zażądać JSON-a wprost: tryb OPENAI_JSON_MODE="object"
# odrzuca wywołanie, w którym słowo "JSON" nie pada w wiadomościach.
SYSTEM = 'Odpowiadasz wyłącznie obiektem JSON, bez komentarza przed ani po.'
USER = 'Zwróć dokładnie taki JSON: {"ok": true}'
MAX_TOKENS = 100


def _wyjscie(komunikat: str) -> int:
    print(f"[{KTO}] {komunikat}", file=sys.stderr)
    return 0


def _skrot(tekst: str, ile: int = 160) -> str:
    jedna_linia = " ".join((tekst or "").split())
    return jedna_linia if len(jedna_linia) <= ile else jedna_linia[:ile] + "…"


def sprobuj(provider: str, model: str) -> dict:
    """Jedno wywołanie. Zwraca słownik do wypisania — NIGDY nie rzuca.

    Skrypt diagnostyczny, który sam się wywala, jest bezużyteczny: przy dwóch
    providerach chcemy zobaczyć WYNIK OBU, także wtedy (a właściwie zwłaszcza
    wtedy), gdy pierwszy nie działa.
    """
    wynik: dict = {"provider": provider, "model": model}
    try:
        tekst, zuzycie = llm.zapytaj_ze_zuzyciem(SYSTEM, USER, MAX_TOKENS,
                                                 provider=provider, model=model)
    except llm.LLMBladTrwaly as e:
        return {**wynik, "blad": str(e), "trwaly": True}
    except llm.LLMNiedostepny as e:
        return {**wynik, "blad": str(e), "trwaly": False}

    # Parsowanie TĄ SAMĄ ścieżką co w produkcji. Własne `json.loads` tutaj
    # odpowiadałoby na inne pytanie niż to, które zadajemy: nie „czy to JSON",
    # tylko „czy KLASYFIKATOR to przyjmie".
    from laweta_radar.workers import classifier  # noqa: PLC0415 — tylko do parsowania

    try:
        dane = classifier._parse_json(tekst)
        parsuje, uwaga = True, ""
    except classifier.OdpowiedzNieczytelna as e:
        dane, parsuje, uwaga = {}, False, str(e)

    return {**wynik, "zuzycie": zuzycie, "tekst": tekst, "parsuje": parsuje,
            "uwaga": uwaga, "dane": dane}


def wypisz(wynik: dict) -> None:
    etykieta = wynik.get("zuzycie").etykieta() if wynik.get("zuzycie") else \
        f"{wynik['provider']}/{wynik['model'] or '(brak modelu)'}"
    print()
    print("=" * 78)
    print(etykieta)
    print("=" * 78)

    if "blad" in wynik:
        rodzaj = "TRWAŁY (ponawianie nic nie da)" if wynik["trwaly"] else "PRZEJŚCIOWY (ponów)"
        print(f"  BŁĄD {rodzaj}")
        for linia in str(wynik["blad"]).splitlines():
            print(f"    {linia}")
        return

    z = wynik["zuzycie"]
    koszt = z.koszt_usd()
    print(f"  surowa odpowiedź : {_skrot(wynik['tekst'])}")
    print(f"  parsuje się      : {'TAK' if wynik['parsuje'] else 'NIE — ' + wynik['uwaga']}")
    if wynik["parsuje"]:
        print(f"  po sparsowaniu   : {json.dumps(wynik['dane'], ensure_ascii=False)}")
    print(f"  tokeny wejścia   : {z.tokeny_wejscia}"
          f"{f' (+{z.tokeny_cache} z cache)' if z.tokeny_cache else ''}")
    print(f"  tokeny wyjścia   : {z.tokeny_wyjscia}")
    # OSOBNA linia, nie doliczona do wyjścia — to ta sama liczba widziana z dwóch
    # stron: do kosztu wchodzi (siedzi w wyjściu), do porównania idzie osobno.
    print(f"  w tym rozumowanie: {z.tokeny_rozumowania}"
          f"{'  <- płacisz za nie jak za wyjście' if z.tokeny_rozumowania else ''}")
    print(f"  czas             : {z.ms} ms")
    print(f"  koszt            : "
          f"{f'${koszt:.6f}' if koszt is not None else 'NIEZNANY — dopisz model do config/cennik.py'}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--provider", action="append", choices=list(llm.PROVIDERY),
                    help="ogranicz do wskazanego providera (można podać kilka razy)")
    ap.add_argument("--model", action="append", default=[],
                    help="konkretny model zamiast domyślnego dla providera")
    ap.add_argument("--sucho", action="store_true",
                    help="pokaż, co poszłoby do modeli, i zakończ (bez sieci i bez kosztu)")
    args = ap.parse_args(argv[1:])

    print(llm.opis(), file=sys.stderr)

    zadane = args.provider or list(llm.PROVIDERY)
    # Provider bez klucza/SDK/modelu NIE jest błędem tego skryptu — brak
    # OPENAI_API_KEY przy LLM_PROVIDER=anthropic to normalny, poprawny stan.
    # Mówimy o nim jedną linią i idziemy dalej.
    gotowe: list[tuple[str, str]] = []
    for i, p in enumerate(zadane):
        braki = llm.problemy(p)
        if braki:
            print(f"[{KTO}] pomijam {p}: {'; '.join(braki)}", file=sys.stderr)
            continue
        gotowe.append((p, args.model[i] if i < len(args.model) else llm.model_domyslny(p)))

    if not gotowe:
        return _wyjscie("Żaden provider nie jest gotowy — nie ma czego testować. "
                        "Szczegóły wyżej; wzór konfiguracji w .env.example.")

    print(f"[{KTO}] prompt systemowy: {SYSTEM}")
    print(f"[{KTO}] pytanie: {USER}")
    print(f"[{KTO}] do odpalenia: " + ", ".join(f"{p}/{m}" for p, m in gotowe))
    if args.sucho:
        return _wyjscie("--sucho: kończę bez wołania modeli.")

    wyniki = [sprobuj(p, m) for p, m in gotowe]
    for w in wyniki:
        wypisz(w)

    print()
    trwale = [w for w in wyniki if w.get("trwaly")]
    if trwale:
        print("TRWAŁE BŁĘDY — popraw konfigurację, zanim odpalisz porównanie modeli:")
        for w in trwale:
            print(f"  {w['provider']}/{w['model']}")
    dzialajace = [w for w in wyniki if "blad" not in w and w["parsuje"]]
    print(f"Providery gotowe do porównania na realnych danych: {len(dzialajace)}/{len(wyniki)}")
    if dzialajace:
        print("Dalej:  python laweta_radar/scripts/porownaj_modele.py --sucho")
    # Kod 0 także przy błędach — ta sama zasada co w workerach: to jest
    # diagnostyka, a nie awaria, i nie ma budzić crona.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
