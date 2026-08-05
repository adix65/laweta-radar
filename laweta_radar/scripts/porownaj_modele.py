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
  • HALUCYNACJE GEO — ile razy model wpisał miasto, którego w poście NIE BYŁO.
    Liczone na WSZYSTKICH postach, nie tylko na oznaczonych, bo nie wymaga
    etykiety: wystarczy porównać odpowiedź z treścią. To jedyna metryka, której
    tryb JSON nie poprawi ani o punkt — gwarantuje kształt odpowiedzi, nie jej
    prawdziwość;
  • ZGODNOŚĆ ZE SCHEMATEM — jak często surowa odpowiedź mieści się w kontrakcie
    BEZ napraw walidatora. Walidator i tak podstawi wartość domyślną, więc bez
    tej kolumny model zjeżdżający z kontraktu wygląda na poprawny, a po cichu
    oddaje "inne" i "elastycznie";
  • ile razy wynik nie dał się sparsować — model oddający czasem prozę zamiast
    JSON-a jest bezużyteczny niezależnie od trafności na reszcie próbki;
  • mediana czasu odpowiedzi — mediana, nie średnia: jeden timeout nie ma
    przesuwać liczby, którą się porównuje;
  • realny koszt runu — z tokenów zwróconych przez API, nie z szacunku, oraz
    tokeny ROZUMOWANIA osobno: model, który dużo myśli i mało pisze, wygląda
    inaczej w kolumnie „wyjście" niż na rachunku.

TRYB JSON JEDZIE W ETYKIECIE MODELU i to nie jest ozdoba. Przy
OPENAI_JSON_MODE=schema nie porównujesz dwóch modeli, tylko dwa STACKI: jeden
z gwarancją schematu i jeden bez. To legalne pytanie produkcyjne, ale inne niż
„który model lepiej rozumie polskie posty" — a po miesiącu nikt nie pamięta,
w którym ustawieniu powstała która tabela.

UŻYCIE:
    export PYTHONPATH=$PWD
    python laweta_radar/scripts/porownaj_modele.py --sucho     # plan, BEZ sieci
    python laweta_radar/scripts/porownaj_modele.py             # realny pomiar
    python laweta_radar/scripts/porownaj_modele.py --provider anthropic \\
        --model claude-opus-5                                  # jeden model
Wynik to tabela na stdout — zrzuć ją do docs/, jeśli decyzja ma zostać na papierze.
Zanim tu zapłacisz tokenami, sprawdź samą konfigurację za ułamek tej ceny:
    python laweta_radar/scripts/test_llm.py
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections.abc import Callable
from pathlib import Path

try:                               # pakiet widoczny: -m, import pakietowy, testy
    from laweta_radar.scripts._sciezka import dodaj_repo_do_sciezki
except ImportError:                # uruchomienie po ścieżce do pliku
    from _sciezka import dodaj_repo_do_sciezki

dodaj_repo_do_sciezki()

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
# HALUCYNACJA GEO
#
# Najdroższy błąd, jaki ten system potrafi popełnić, i jedyny, którego tryb JSON
# nie tyka: zgadnięte miasto wysyła człowieka 80 km w złą stronę, a puste pole
# tylko każe mu przeczytać post. Prompt tego zabrania wprost — ta kolumna
# sprawdza, czy model słucha.
#
# Metryka NIE POTRZEBUJE ETYKIET: liczy się na wszystkich postach w zbiorze,
# bo pytanie brzmi „czy ta nazwa w ogóle padła w poście", a nie „czy jest
# poprawna". Dzięki temu działa też na postach świadomie nieocenianych.
#
# LICZY DOLNĄ GRANICĘ, nie prawdę absolutną. Polska odmiana rozjeżdża formy
# ("Krosno" w poście stoi jako "w Krośnie"), więc porównujemy RDZEŃ nazwy, a nie
# całość. Model, który zgadnie miejscowość o wspólnym rdzeniu z czymś w treści,
# przejdzie tu niezauważony — ale to zdarza się rzadziej niż odmiana, a metryka
# zawyżająca halucynacje byłaby bezużyteczna: przestano by na nią patrzeć po
# drugim fałszywym alarmie.
# ---------------------------------------------------------------------------
_MIN_RDZEN = 4
_NIE_LITERA = re.compile(r"[^a-z0-9]+")


def rdzenie(nazwa: str) -> list[str]:
    """Nazwa miejscowości -> rdzenie jej członów, po normalizacji bramki.

    CZŁON PO CZŁONIE, bo w polskim odmieniają się OBA: "Miejsce Piastowe" stoi
    w poście jako "pod Miejscem Piastowym" i całość nie pasuje do niczego.
    Rozbicie na "miejs" + "piasto" pasuje do obu form.

    Rdzeń to nazwa bez dwóch ostatnich liter (minimum `_MIN_RDZEN` znaków) —
    tyle wystarcza na końcówkę przypadka i nie sięga w głąb nazwy.
    """
    czysta = normalizuj(str(nazwa))
    return [czlon[:max(_MIN_RDZEN, len(czlon) - 2)]
            for czlon in _NIE_LITERA.split(czysta) if czlon]


def czy_halucynacja(miasto, tresc: str) -> bool:
    """Czy ta nazwa miejscowości NIE pada w treści posta.

    None i pustka nie są halucynacją — brak miejsca to poprawna odpowiedź
    i cała reszta tego repo tak właśnie woli.

    Wymagamy WSZYSTKICH członów, a nie któregokolwiek: przy nazwie
    dwuczłonowej pierwszy człon bywa pospolity ("Nowy", "Wola", "Miejsce")
    i sam z siebie trafiłby w pół postów. Kierunek pomyłki jest wybrany
    świadomie — metryka ma raczej przeoczyć halucynację niż zgłosić fałszywą,
    bo po drugim fałszywym alarmie przestaje się na nią patrzeć.
    """
    if not miasto:
        return False
    czesci = rdzenie(miasto)
    if not czesci:
        return False
    w_tresci = normalizuj(tresc)
    return not all(czlon in w_tresci for czlon in czesci)


# ---------------------------------------------------------------------------
# ZGODNOŚĆ ZE SCHEMATEM
#
# Walidator klasyfikatora jest wyrozumiały z rozmysłem: pojedyncze pole spoza
# zbioru nie może skasować całego posta. Skutek uboczny jest taki, że model
# systematycznie zjeżdżający z kontraktu WYGLĄDA na poprawny — dostajemy "inne"
# i "elastycznie" zamiast błędu. Ta funkcja pokazuje, ile razy walidator musiał
# coś naprawić, i porównuje modele PRZED naprawą.
#
# Zbiory dopuszczalnych wartości bierzemy z klasyfikatora, a nie z własnej listy
# — druga kopia rozjechałaby się przy pierwszej dopisanej kategorii i mierzyłaby
# zgodność z kontraktem, którego nikt nie egzekwuje.
# ---------------------------------------------------------------------------
_ZBIORY_PROSTE = (
    ("typ", classifier._POPRAWNE_TYP),
    ("pilnosc", classifier._POPRAWNE_PILNOSC),
)
_ZBIORY_ZAGNIEZDZONE = (
    ("pojazd", "kategoria", classifier._POPRAWNE_KATEGORIE),
    ("kontakt", "typ", classifier._POPRAWNE_KONTAKT),
)
_OBIEKTY = {
    "odbior": ("raw", "kod", "miasto"),
    "dostawa": ("raw", "kod", "miasto"),
    "pojazd": ("opis", "kategoria"),
    "stan": ("toczy_sie", "ma_kola", "po_wypadku", "uwagi"),
    "kontakt": ("typ", "wartosc"),
}


def odchylenia_od_schematu(dane: dict) -> list[str]:
    """Czym surowa odpowiedź modelu wykracza poza kontrakt. Pusta lista = zgodna."""
    bledy: list[str] = []

    if not isinstance(dane.get("czy_zlecenie"), bool):
        bledy.append("czy_zlecenie nie jest wartością logiczną")

    for pole, zbior in _ZBIORY_PROSTE:
        if dane.get(pole) not in zbior:
            bledy.append(f"{pole}={dane.get(pole)!r} spoza zbioru")

    for obiekt, podpola in _OBIEKTY.items():
        wartosc = dane.get(obiekt)
        if not isinstance(wartosc, dict):
            bledy.append(f"{obiekt} nie jest obiektem")
            continue
        for podpole in podpola:
            if podpole not in wartosc:
                bledy.append(f"brak {obiekt}.{podpole}")

    for obiekt, podpole, zbior in _ZBIORY_ZAGNIEZDZONE:
        wartosc = dane.get(obiekt)
        if isinstance(wartosc, dict) and wartosc.get(podpole) not in zbior:
            bledy.append(f"{obiekt}.{podpole}={wartosc.get(podpole)!r} spoza zbioru")

    stan = dane.get("stan")
    if isinstance(stan, dict):
        bledy += [f"stan.{p} nie jest wartością logiczną"
                  for p in ("toczy_sie", "ma_kola", "po_wypadku")
                  if p in stan and not isinstance(stan[p], bool)]

    pewnosc = dane.get("pewnosc")
    if not isinstance(pewnosc, int) or isinstance(pewnosc, bool) or not 0 <= pewnosc <= 100:
        bledy.append(f"pewnosc={pewnosc!r} nie jest liczbą całkowitą 0-100")

    cena = dane.get("cena_sugerowana")
    if cena is not None and (isinstance(cena, bool) or not isinstance(cena, (int, float))):
        bledy.append(f"cena_sugerowana={cena!r} nie jest liczbą ani nullem")

    return bledy


# ---------------------------------------------------------------------------
# POMIAR
# ---------------------------------------------------------------------------
class Wynik:
    """Rozliczenie jednego modelu na całym zbiorze."""

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        self.tryb = ""            # tryb JSON — dopisywany z pierwszej odpowiedzi
        self.trafienia: dict[str, int] = {p: 0 for p in OCENIANE}
        self.ocenione: dict[str, int] = {p: 0 for p in OCENIANE}
        self.czasy_ms: list[int] = []
        self.nieparsowalne = 0
        self.awarie = 0
        self.halucynacje = 0      # miasta, których w poście nie było
        self.miasta_podane = 0    # ile razy model w ogóle wpisał miasto
        self.zgodne_ze_schematem = 0
        self.odpowiedzi = 0       # ile razy model w ogóle coś sparsowalnego oddał
        self.koszt_usd = 0.0
        self.koszt_nieznany = False
        self.tokeny_wejscia = 0
        self.tokeny_wyjscia = 0
        self.tokeny_rozumowania = 0
        self.pomylki: list[str] = []

    @property
    def etykieta(self) -> str:
        """Model RAZEM z trybem JSON. Nigdy sam model — patrz nagłówek pliku."""
        podstawa = f"{self.provider}/{self.model}"
        return f"{podstawa} [json={self.tryb}]" if self.tryb and self.tryb != "off" else podstawa

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

    def dodaj_halucynacje(self, wpis: dict, wynik: dict) -> None:
        """Miasta wpisane przez model, których w treści posta nie ma.

        Bez etykiet — liczone na każdym poście, patrz komentarz przy
        `czy_halucynacja`.
        """
        tresc = wpis["tresc"]
        for pole in ("odbior", "dostawa"):
            miasto = wynik[pole]["miasto"]
            if not miasto:
                continue
            self.miasta_podane += 1
            if czy_halucynacja(miasto, tresc):
                self.halucynacje += 1
                self.pomylki.append(
                    f"{wpis.get('id', '?')} HALUCYNACJA {pole}.miasto: {miasto!r} "
                    f"nie pada w poście")

    def procent(self, pole: str) -> float | None:
        ile = self.ocenione[pole]
        return None if not ile else 100.0 * self.trafienia[pole] / ile

    @property
    def procent_halucynacji(self) -> float | None:
        """Odsetek WPISANYCH miast, których w poście nie było. Mniej = lepiej.

        Mianownikiem są podane miasta, nie wszystkie posty: model zostawiający
        wszędzie null ma zero halucynacji i tak ma być — to zachowanie, którego
        prompt wymaga. Jego cena jest widoczna w kolumnach trafności.
        """
        return None if not self.miasta_podane else 100.0 * self.halucynacje / self.miasta_podane

    @property
    def procent_schematu(self) -> float | None:
        return None if not self.odpowiedzi else 100.0 * self.zgodne_ze_schematem / self.odpowiedzi

    @property
    def mediana_ms(self) -> int | None:
        return int(statistics.median(self.czasy_ms)) if self.czasy_ms else None


def zmierz(model_wpis: tuple[str, str], posty: list[dict], gadaj: bool) -> Wynik:
    provider, model = model_wpis
    wynik = Wynik(provider, model)
    wynik.tryb = llm.tryb_json(provider)

    for i, wpis in enumerate(posty, start=1):
        if gadaj:
            print(f"\r[{KTO}] {provider}/{model}: {i}/{len(posty)}", end="", file=sys.stderr)
        try:
            tekst, zuzycie = llm.zapytaj_ze_zuzyciem(
                classifier.zbuduj_system(wpis.get("grupa", "")),
                classifier.zbuduj_user(wpis["tresc"]),
                classifier.MAX_TOKENS,
                provider=provider,
                model=model,
            )
        except llm.LLMBladTrwaly:
            # Zły klucz, nieznany model, odrzucony parametr — ponawianie na
            # kolejnych czterdziestu postach tylko pali czas i nic nie zmierzy.
            # Przerywamy CAŁY pomiar, żeby operator zobaczył przyczynę teraz,
            # a nie po czterech minutach identycznych linijek.
            if gadaj:
                print("", file=sys.stderr)
            raise
        except llm.LLMNiedostepny as e:
            # Awaria wołania to NIE jest wina modelu jako modelu — liczymy ją
            # osobno, żeby jeden timeout nie wyglądał w tabeli jak zła jakość.
            wynik.awarie += 1
            wynik.pomylki.append(f"{wpis.get('id', '?')} awaria: {e}")
            continue

        wynik.czasy_ms.append(zuzycie.ms)
        wynik.tokeny_wejscia += zuzycie.tokeny_wejscia
        wynik.tokeny_wyjscia += zuzycie.tokeny_wyjscia
        wynik.tokeny_rozumowania += zuzycie.tokeny_rozumowania
        koszt = zuzycie.koszt_usd()
        if koszt is None:
            wynik.koszt_nieznany = True
        else:
            wynik.koszt_usd += koszt

        # Rozbiór DOKŁADNIE tą samą ścieżką co w produkcji, tylko rozłożony na
        # dwa kroki — surowy słownik jest potrzebny do kolumny „schemat", bo po
        # walidacji każda odpowiedź z definicji mieści się w kontrakcie.
        # (`classifier.rozbierz` to złożenie tych dwóch i nic więcej.)
        try:
            surowe = classifier._parse_json(tekst)
        except classifier.OdpowiedzNieczytelna:
            wynik.nieparsowalne += 1
            wynik.pomylki.append(f"{wpis.get('id', '?')} nieparsowalne: {tekst[:120]!r}")
            continue

        wynik.odpowiedzi += 1
        odchylenia = odchylenia_od_schematu(surowe)
        if not odchylenia:
            wynik.zgodne_ze_schematem += 1
        else:
            wynik.pomylki.append(
                f"{wpis.get('id', '?')} poza schematem: {'; '.join(odchylenia[:3])}")

        zwalidowany = classifier.zwaliduj(surowe)
        wynik.dodaj(wpis, zwalidowany)
        wynik.dodaj_halucynacje(wpis, zwalidowany)

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
    print("=" * 118)
    print(f"PORÓWNANIE MODELI — {ile_postow} postów ze zbioru referencyjnego")
    print("=" * 118)
    naglowek = (f"{'model [tryb json]':<40} {'zlec.':>6} {'odbiór':>7} {'dostawa':>8} "
                f"{'pilność':>8} {'halucyn.':>9} {'schemat':>8} {'złe JSON':>9} "
                f"{'awarie':>7} {'mediana':>8} {'koszt':>10}")
    print(naglowek)
    print("-" * len(naglowek))

    for w in wyniki:
        koszt = "  nieznany" if w.koszt_nieznany else f"${w.koszt_usd:8.4f}"
        mediana = "   —  " if w.mediana_ms is None else f"{w.mediana_ms:6d}ms"
        print(f"{w.etykieta[:40]:<40} "
              f"{_pct(w.procent('czy_zlecenie')):>6} "
              f"{_pct(w.procent('odbior_miasto')):>7} "
              f"{_pct(w.procent('dostawa_miasto')):>8} "
              f"{_pct(w.procent('pilnosc')):>8} "
              f"{_pct(w.procent_halucynacji):>9} "
              f"{_pct(w.procent_schematu):>8} "
              f"{w.nieparsowalne:9d} {w.awarie:7d} {mediana:>8} {koszt:>10}")

    print()
    kurs = settings.KURS_USD_PLN
    for w in wyniki:
        rozumowanie = (f", w tym {w.tokeny_rozumowania} na rozumowanie"
                       if w.tokeny_rozumowania else "")
        print(f"  {w.etykieta}: tokeny {w.tokeny_wejscia}->{w.tokeny_wyjscia}{rozumowanie}")
        if w.koszt_nieznany:
            print(f"      koszt NIEZNANY — dopisz stawkę do config/cennik.py albo do "
                  f"CENNIK_EXTRA w .env, inaczej ta kolumna nic nie znaczy")
        else:
            print(f"      run kosztował ${w.koszt_usd:.4f} "
                  f"(~{w.koszt_usd * kurs:.2f} zł po kursie {kurs:.2f})")
        if w.halucynacje:
            print(f"      UWAGA: {w.halucynacje} z {w.miasta_podane} podanych miast "
                  f"nie pada w treści posta")

    print()
    print("JAK TO CZYTAĆ:")
    print("  • `zlec.` jest ważniejsze niż reszta razem wzięta — pomyłka w tę stronę")
    print("    kosztuje kurs albo alert o kursie, którego nie ma.")
    print("  • Poprawnie postawiony null liczy się jako trafienie: model zgadujący")
    print("    miasto jest GORSZY niż model, który zostawia puste pole.")
    print("  • `halucyn.` MNIEJ = LEPIEJ: odsetek wpisanych miast, których w poście")
    print("    nie było. Zgadnięte miasto wysyła człowieka 80 km w złą stronę.")
    print("    Tryb JSON tej kolumny NIE poprawia — gwarantuje kształt odpowiedzi,")
    print("    nie jej prawdziwość. To jest ta różnica.")
    print("  • `schemat` = ile odpowiedzi mieści się w kontrakcie BEZ napraw")
    print("    walidatora. Niskie przy wysokiej trafności znaczy, że wynik ratuje")
    print("    nasz kod, a nie model — i przestanie ratować przy pierwszej zmianie")
    print("    kontraktu.")
    print("  • Niezerowe `złe JSON` dyskwalifikuje model niezależnie od trafności.")
    print("  • Koszt liczony z tokenów zwróconych przez API — to realny rachunek")
    print("    za ten przebieg, nie szacunek. Tokeny rozumowania SĄ w nim wliczone")
    print("    (dostawca liczy je jak wyjściowe) i wypisane osobno wyżej.")
    print("  • `[json=...]` przy nazwie modelu to CZĘŚĆ WYNIKU. Przy json=schema")
    print("    porównujesz dwa stacki, nie dwa modele — nie zestawiaj takiej tabeli")
    print("    z tabelą zrobioną przy innym ustawieniu.")


def wypisz_pomylki(wyniki: list[Wynik], limit: int) -> None:
    for w in wyniki:
        if not w.pomylki:
            continue
        print()
        print(f"--- {w.etykieta}: {len(w.pomylki)} rozbieżności "
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
        # Tryb JSON JEST częścią tego, co mierzymy — ma być widoczny już w planie,
        # zanim ktokolwiek zapłaci tokenami za tabelę, której potem nie da się
        # zestawić z żadną inną.
        tryb = llm.tryb_json(provider)
        znacznik = f" [json={tryb}]" if tryb != "off" else ""
        print(f"[{KTO}]   {provider}/{model or '(brak modelu)'}{znacznik}")
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

    # Wyniki wypisujemy TAKŻE po przerwaniu na błędzie trwałym: pomiar modeli,
    # które zdążyły przejść, jest już opłacony tokenami i nie ma powodu go tracić
    # dlatego, że w trzecim wygasł klucz.
    wyniki: list[Wynik] = []
    przerwane = ""
    try:
        for m in modele:
            wyniki.append(zmierz(m, posty, gadaj=sys.stderr.isatty()))
    except llm.LLMBladTrwaly as e:
        przerwane = str(e)

    if wyniki:
        wypisz_tabele(wyniki, len(posty))
        if args.pomylki:
            wypisz_pomylki(wyniki, args.pomylki)

    if przerwane:
        print()
        return _wyjscie("BŁĄD TRWAŁY — przerywam, bo ponawianie tylko pali czas:\n"
                        f"  {przerwane}\n"
                        f"  Sprawdź konfigurację jednym wywołaniem na providera:\n"
                        f"  python laweta_radar/scripts/test_llm.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
