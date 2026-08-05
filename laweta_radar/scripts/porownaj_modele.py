#!/usr/bin/env python3
"""Porównanie modeli na NASZYCH danych — jedna komenda i decyzja na liczbach.

PO CO TO ISTNIEJE: różnica w cenie między najdroższym a najtańszym modelem
przy tym wolumenie to około 25 zł miesięcznie, czyli mniej niż szum. Różnica
w JAKOŚCI na polskich postach pisanych bez ogonków, z literówkami i skrótami
drogowymi może być duża — i nie da się jej przewidzieć z benchmarków, bo żaden
nie mierzy „wyciąganie miejscowości z posta laweciarskiego". Bez tego skryptu
wybór modelu jest zgadywaniem.

CO LICZYMY, per model:
  • trafność `czy_zlecenie` — najważniejsza, bo pomyłka w tę stronę kosztuje
    kurs (~300 zł) albo alert o kursie, którego nie ma;
  • trafność miasta odbioru i dostawy — to z nich powstaje trasa; model, który
    zgaduje miasto, jest GORSZY niż model, który zostawia null, więc null
    postawiony poprawnie liczy się tu jako trafienie;
  • trafność `pilnosc` — decyduje, czy budzimy człowieka w nocy;
  • ile razy wynik nie dał się sparsować — model oddający czasem prozę zamiast
    JSON-a jest bezużyteczny niezależnie od trafności na reszcie próbki;
  • mediana czasu odpowiedzi — mediana, nie średnia: jeden timeout nie ma
    przesuwać liczby, którą się porównuje;
  • realny koszt runu — z tokenów zwróconych przez API, nie z szacunku.

UŻYCIE:
    export PYTHONPATH=$PWD
    python laweta_radar/scripts/porownaj_modele.py --sucho     # plan, BEZ sieci
    python laweta_radar/scripts/porownaj_modele.py             # realny pomiar
    python laweta_radar/scripts/porownaj_modele.py --provider anthropic \\
        --model claude-opus-5                                  # jeden model
Wynik to tabela na stdout — zrzuć ją do docs/, jeśli decyzja ma zostać na papierze.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from laweta_radar.config import settings  # noqa: E402
from laweta_radar.services import llm  # noqa: E402
from laweta_radar.workers import classifier  # noqa: E402
from laweta_radar.workers.gate import normalizuj  # noqa: E402

KTO = "porownaj-modele"

ZBIOR_DOMYSLNY = (Path(__file__).resolve().parent.parent / "tests" / "dane"
                  / "posty_referencyjne.jsonl")

# Poniżej tylu postów wynik nie znaczy nic. Przy dziesięciu przypadkach różnica
# „92% vs 85%" to jeden post w tę lub w tamtą — czyli szum, na którym nie wolno
# oprzeć wyboru modelu. Ta sama zasada co MIN_PROBKA w raport_gate.py.
MIN_PROBKA = 40

# Pola oceniane. Nazwa w zbiorze -> jak wyjąć wartość z wyniku klasyfikatora.
OCENIANE: dict[str, Callable[[dict], object]] = {
    "czy_zlecenie": lambda w: w["czy_zlecenie"],
    "odbior_miasto": lambda w: w["odbior"]["miasto"],
    "dostawa_miasto": lambda w: w["dostawa"]["miasto"],
    "pilnosc": lambda w: w["pilnosc"],
}


def _wyjscie(komunikat: str) -> int:
    """Czyste zakończenie z komunikatem — ta sama zasada co w workerach."""
    print(f"[{KTO}] {komunikat}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# ZBIÓR REFERENCYJNY
# ---------------------------------------------------------------------------
def wczytaj(sciezka: Path) -> tuple[list[dict], list[str]]:
    """Wczytaj JSONL, pomijając komentarze. Zwraca (posty, ostrzeżenia).

    Zły wiersz POMIJAMY z ostrzeżeniem zamiast wywalać cały przebieg: literówka
    w jednym z czterdziestu wpisów nie może kasować pomiaru, za który zapłacono
    tokenami.
    """
    if not sciezka.exists():
        return [], [f"brak pliku {sciezka}"]

    posty: list[dict] = []
    ostrzezenia: list[str] = []
    for nr, linia in enumerate(sciezka.read_text(encoding="utf-8").splitlines(), start=1):
        surowa = linia.strip()
        if not surowa or surowa.startswith("#"):
            continue
        try:
            wpis = json.loads(surowa)
            wpis["tresc"], wpis["oczekiwane"]  # noqa: B018 — sprawdzenie obecności
        except (ValueError, KeyError, TypeError) as e:
            ostrzezenia.append(f"wiersz {nr} pominięty ({type(e).__name__})")
            continue
        posty.append(wpis)
    return posty, ostrzezenia


def _porownywalne(wartosc) -> object:
    """Forma do porównania etykiety z wynikiem modelu.

    Miasta przepuszczamy przez `gate.normalizuj` (małe litery, bez ogonków) —
    tę samą funkcję, której używa bramka, bo model równie dobrze napisze
    "Rzeszów" i "Rzeszow", a to jest ta sama odpowiedź. Drugiej implementacji
    normalizacji w repo nie chcemy: rozjechałaby się przy pierwszej poprawce.
    None zostaje None — brak miejsca to informacja, nie pusty napis.
    """
    if wartosc is None or isinstance(wartosc, bool):
        return wartosc
    return normalizuj(str(wartosc)) or None


# ---------------------------------------------------------------------------
# POMIAR
# ---------------------------------------------------------------------------
class Wynik:
    """Rozliczenie jednego modelu na całym zbiorze."""

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        self.trafienia: dict[str, int] = {p: 0 for p in OCENIANE}
        self.ocenione: dict[str, int] = {p: 0 for p in OCENIANE}
        self.czasy_ms: list[int] = []
        self.nieparsowalne = 0
        self.awarie = 0
        self.koszt_usd = 0.0
        self.koszt_nieznany = False
        self.pomylki: list[str] = []

    def dodaj(self, wpis: dict, wynik: dict) -> None:
        for pole, wyjmij in OCENIANE.items():
            if pole not in wpis["oczekiwane"]:
                continue  # post dwuznaczny — świadomie nieoceniany
            self.ocenione[pole] += 1
            oczekiwane = _porownywalne(wpis["oczekiwane"][pole])
            otrzymane = _porownywalne(wyjmij(wynik))
            if oczekiwane == otrzymane:
                self.trafienia[pole] += 1
            else:
                self.pomylki.append(
                    f"{wpis.get('id', '?')} {pole}: oczekiwano {oczekiwane!r}, "
                    f"model dał {otrzymane!r}")

    def procent(self, pole: str) -> float | None:
        ile = self.ocenione[pole]
        return None if not ile else 100.0 * self.trafienia[pole] / ile

    @property
    def mediana_ms(self) -> int | None:
        return int(statistics.median(self.czasy_ms)) if self.czasy_ms else None


def zmierz(model_wpis: tuple[str, str], posty: list[dict], gadaj: bool) -> Wynik:
    provider, model = model_wpis
    wynik = Wynik(provider, model)

    for i, wpis in enumerate(posty, start=1):
        if gadaj:
            print(f"\r[{KTO}] {provider}/{model}: {i}/{len(posty)}", end="", file=sys.stderr)
        try:
            odp = llm.zapytaj_ze_zuzyciem(
                classifier.zbuduj_system(wpis.get("grupa", "")),
                classifier.zbuduj_user(wpis["tresc"]),
                classifier.MAX_TOKENS,
                provider=provider,
                model=model,
            )
        except llm.LLMNiedostepny as e:
            # Awaria wołania to NIE jest wina modelu jako modelu — liczymy ją
            # osobno, żeby jeden timeout nie wyglądał w tabeli jak zła jakość.
            wynik.awarie += 1
            wynik.pomylki.append(f"{wpis.get('id', '?')} awaria: {e}")
            continue

        wynik.czasy_ms.append(odp.ms)
        koszt = llm.koszt_usd(odp.model, odp.tokeny_wejscie, odp.tokeny_wyjscie)
        if koszt is None:
            wynik.koszt_nieznany = True
        else:
            wynik.koszt_usd += koszt

        try:
            zwalidowany = classifier.rozbierz(odp.tekst)
        except classifier.OdpowiedzNieczytelna:
            wynik.nieparsowalne += 1
            wynik.pomylki.append(f"{wpis.get('id', '?')} nieparsowalne: {odp.tekst[:120]!r}")
            continue

        wynik.dodaj(wpis, zwalidowany)

    if gadaj:
        print("", file=sys.stderr)
    return wynik


# ---------------------------------------------------------------------------
# TABELA
# ---------------------------------------------------------------------------
def _pct(wartosc: float | None) -> str:
    return "  —  " if wartosc is None else f"{wartosc:5.1f}%"


def wypisz_tabele(wyniki: list[Wynik], ile_postow: int) -> None:
    print()
    print("=" * 100)
    print(f"PORÓWNANIE MODELI — {ile_postow} postów ze zbioru referencyjnego")
    print("=" * 100)
    naglowek = (f"{'model':<34} {'zlec.':>6} {'odbiór':>7} {'dostawa':>8} {'pilność':>8} "
                f"{'złe JSON':>9} {'awarie':>7} {'mediana':>8} {'koszt':>10}")
    print(naglowek)
    print("-" * len(naglowek))

    for w in wyniki:
        etykieta = f"{w.provider}/{w.model}"
        koszt = "  nieznany" if w.koszt_nieznany else f"${w.koszt_usd:8.4f}"
        mediana = "   —  " if w.mediana_ms is None else f"{w.mediana_ms:6d}ms"
        print(f"{etykieta[:34]:<34} "
              f"{_pct(w.procent('czy_zlecenie')):>6} "
              f"{_pct(w.procent('odbior_miasto')):>7} "
              f"{_pct(w.procent('dostawa_miasto')):>8} "
              f"{_pct(w.procent('pilnosc')):>8} "
              f"{w.nieparsowalne:9d} {w.awarie:7d} {mediana:>8} {koszt:>10}")

    print()
    kurs = settings.KURS_USD_PLN
    for w in wyniki:
        if w.koszt_nieznany:
            print(f"  {w.provider}/{w.model}: koszt nieznany — dopisz stawkę do CENNIK_EXTRA "
                  f"w .env, inaczej ta kolumna nic nie znaczy")
        else:
            print(f"  {w.provider}/{w.model}: run kosztował ${w.koszt_usd:.4f} "
                  f"(~{w.koszt_usd * kurs:.2f} zł po kursie {kurs:.2f})")

    print()
    print("JAK TO CZYTAĆ:")
    print("  • `zlec.` jest ważniejsze niż reszta razem wzięta — pomyłka w tę stronę")
    print("    kosztuje kurs albo alert o kursie, którego nie ma.")
    print("  • Poprawnie postawiony null liczy się jako trafienie: model zgadujący")
    print("    miasto jest GORSZY niż model, który zostawia puste pole.")
    print("  • Niezerowe `złe JSON` dyskwalifikuje model niezależnie od trafności.")
    print("  • Koszt liczony z tokenów zwróconych przez API — to realny rachunek")
    print("    za ten przebieg, nie szacunek.")


def wypisz_pomylki(wyniki: list[Wynik], limit: int) -> None:
    for w in wyniki:
        if not w.pomylki:
            continue
        print()
        print(f"--- {w.provider}/{w.model}: {len(w.pomylki)} rozbieżności "
              f"(pokazuję do {limit}) ---")
        for p in w.pomylki[:limit]:
            print(f"  {p}")


# ---------------------------------------------------------------------------
def _plan(modele: list[tuple[str, str]], posty: list[dict]) -> None:
    print(f"[{KTO}] Zbiór: {len(posty)} postów")
    seedy = sum(1 for p in posty if p.get("zrodlo") == "seed")
    print(f"[{KTO}] Z tego seedów (napisanych ręcznie przy budowie repo): {seedy}")
    print(f"[{KTO}] Do odpalenia: {len(modele)} modeli x {len(posty)} postów "
          f"= {len(modele) * len(posty)} wywołań")
    for provider, model in modele:
        print(f"[{KTO}]   {provider}/{model}")
    print(f"[{KTO}] Rząd wielkości kosztu: kilkadziesiąt groszy na model "
          f"(~{classifier.MAX_TOKENS} tokenów wyjścia na post, wejście ~1000).")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--plik", type=Path, default=ZBIOR_DOMYSLNY,
                    help=f"zbiór referencyjny (domyślnie {ZBIOR_DOMYSLNY.name})")
    ap.add_argument("--provider", action="append", choices=list(llm.PROVIDERY),
                    help="ogranicz do wskazanego providera (można podać kilka razy)")
    ap.add_argument("--model", action="append", default=[],
                    help="konkretny model zamiast domyślnego dla providera; "
                         "przy jednym providerze wystarczy sama nazwa modelu")
    ap.add_argument("--sucho", action="store_true",
                    help="pokaż plan i nie wołaj niczego (bez sieci i bez kosztu)")
    ap.add_argument("--pomylki", type=int, default=15,
                    help="ile rozbieżności wypisać per model (0 = żadnych)")
    args = ap.parse_args(argv[1:])

    posty, ostrzezenia = wczytaj(args.plik)
    for o in ostrzezenia:
        print(f"[{KTO}] UWAGA: {o}", file=sys.stderr)
    if not posty:
        return _wyjscie(f"Zbiór {args.plik} jest pusty — nie ma czego mierzyć.")

    providery = args.provider or llm.gotowe_providery()
    if not providery:
        # --sucho ma DZIAŁAĆ na świeżym klonie, bez kluczy i bez SDK: jego
        # zadaniem jest pokazać plan i koszt PRZED instalacją czegokolwiek.
        # Twarde wyjście tutaj kazałoby najpierw zapłacić za konfigurację,
        # żeby dowiedzieć się, ile kosztuje pomiar.
        if not args.sucho:
            return _wyjscie("Żaden provider nie jest gotowy. Sprawdź: "
                            "python -m laweta_radar.services.llm")
        providery = [llm.normalizuj_provider(settings.LLM_PROVIDER)]
        print(f"[{KTO}] Żaden provider nie jest gotowy — plan pokazuję dla "
              f"domyślnego ({providery[0]}).", file=sys.stderr)

    # --model bez providera dokładamy do pierwszego z listy; przy kilku
    # providerach kolejność --provider decyduje o przypisaniu.
    modele: list[tuple[str, str]] = []
    for i, p in enumerate(providery):
        nadpisany = args.model[i] if i < len(args.model) else None
        modele.append((p, nadpisany or llm.model_domyslny(p)))

    _plan(modele, posty)

    seedy = sum(1 for p in posty if p.get("zrodlo") == "seed")
    if len(posty) < MIN_PROBKA:
        print(f"[{KTO}] UWAGA: {len(posty)} postów to za mało na decyzję "
              f"(minimum {MIN_PROBKA}). Różnice kilku punktów procentowych na tej "
              f"próbce są szumem — dopisz realne posty z bazy (instrukcja "
              f"w nagłówku {args.plik.name}).", file=sys.stderr)
    if seedy == len(posty):
        print(f"[{KTO}] UWAGA: zbiór składa się WYŁĄCZNIE z seedów napisanych "
              f"razem z promptem. Model może na nich wyglądać lepiej, niż jest — "
              f"to sprawdzenie, że skrypt chodzi, a nie pomiar.", file=sys.stderr)

    if args.sucho:
        print(f"[{KTO}] --sucho: kończę bez wołania modeli.")
        return 0

    for provider, _ in modele:
        for brak in llm.problemy(provider):
            return _wyjscie(f"{provider}: {brak}")

    wyniki = [zmierz(m, posty, gadaj=sys.stderr.isatty()) for m in modele]
    wypisz_tabele(wyniki, len(posty))
    if args.pomylki:
        wypisz_pomylki(wyniki, args.pomylki)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
