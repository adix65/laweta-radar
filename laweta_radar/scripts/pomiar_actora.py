#!/usr/bin/env python3
"""
JEDNORAZOWY POMIAR actora apify/facebook-groups-scraper. NIE jest częścią pipeline'u.

Odpowiada na trzy pytania, na których stoi cała późniejsza architektura fetchera.
Zadaje je EMPIRYCZNIE, bo dokumentacja actora na nie nie odpowiada, a każda z tych
liczb wchodzi wprost do decyzji projektowej:

  1. Jaką najmniejszą jednostkę czasu przyjmuje `onlyPostsNewerThan`?
     Bez działającego okna czasowego każdy przebieg pobiera (i płaci za) te same
     posty co poprzedni. Przy fetcherze chodzącym co 5 minut to nie jest drobiazg,
     tylko różnica między systemem opłacalnym a nieopłacalnym.
       ŚCIEŻKA A — okno działa: liczba itemów maleje wraz ze zwężaniem, a wiek
                   najstarszego posta mieści się w oknie.
       ŚCIEŻKA B — jednostka ignorowana: wynik dla wąskiego okna jest taki sam jak
                   dla szerokiego (albo actor odrzuca pole).
  2. Jak działa `resultsLimit` przy WIELU grupach w `startUrls` — per grupa czy
     globalnie? Przy limicie globalnym batch po dziesięć grup zgubiłby posty
     z ośmiu z nich, czyli batchowanie byłoby wprost szkodliwe.
  3. Ile realnie kosztuje jeden pobrany post? Liczba wchodzi do `POSTY_NA_DOBE`
     i do decyzji „ile kont / czy płatny plan".

MIERZYMY TĄ SAMĄ ŚCIEŻKĄ CO PRODUKCJA: rotacja kluczy (workers/apify_keys.py) i proxy
per konto (workers/apify_proxy.py). Pomiar wykonany „na skróty", z gołego IP i jednego
klucza, zmierzyłby inny system niż ten, który potem stanie na cronie.

KOSZT. Skrypt liczy przewidywany koszt PRZED odpaleniem i pyta o potwierdzenie.
Domyślny plan to ~240 pobranych postów, twardy sufit `--budzet-postow` (500).
Każde wywołanie odejmuje kredyt puli WSPÓLNEJ z sales-core-engine — patrz README.

UŻYCIE:
    export PYTHONPATH=$PWD
    python laweta_radar/scripts/pomiar_actora.py --sucho      # plan i koszt, BEZ sieci
    python laweta_radar/scripts/pomiar_actora.py              # realny pomiar (pyta o TAK)

Grupy testowe bierzemy domyślnie z config/groups.py (status "ok" = człowiek
POTWIERDZIŁ, że grupa jest publiczna i żywa). Grupę spoza tej listy podajesz przez
`--grupa URL` i wtedy musisz dołożyć `--potwierdzam-publiczne`: na grupie prywatnej
zmierzylibyśmy komunikat błędu, a nie zachowanie actora — a run i tak zostanie
policzony.

WYNIK: docs/POMIAR-ACTORA.md (tabele, rozstrzygnięcie A/B, koszt posta, data, wersja
actora). Ten plik czyta prompt 2 przed napisaniem `_build_actor_input`.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Skrypt leży w laweta_radar/scripts/, a nie jest modułem pakietu — celowo, bo to
# narzędzie jednorazowe, nie część systemu. Dorzucamy katalog repo do ścieżki, żeby
# `python laweta_radar/scripts/pomiar_actora.py` działało bez ustawiania PYTHONPATH.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from laweta_radar.config import groups as konfig_grup  # noqa: E402
from laweta_radar.workers import apify_credits, apify_proxy  # noqa: E402
from laweta_radar.workers.apify_keys import (  # noqa: E402
    AllKeysExhausted,
    KeyRotator,
    load_apify_tokens,
)

API = "https://api.apify.com/v2"

# Okna do zbadania, od najszerszego do najwęższego. Kolejność ma znaczenie: pytanie
# brzmi „gdzie actor przestaje rozumieć jednostkę", a to widać dopiero w SERII.
OKNA = ("7 days", "1 day", "12 hours", "1 hour", "30 minutes")

# Pole wejściowe actora, którego dotyczy PYTANIE 1. Wystawione jako stała i jako
# `--pole-okna`, bo jeśli actor odrzuci tę nazwę, operator ma móc powtórzyć pomiar
# z inną (np. `onlyPostsNewerThan` vs `postsNewerThan`) bez ruszania kodu.
POLE_OKNA = "onlyPostsNewerThan"

# Ile itemów bierzemy w pomiarze okna i w pomiarze limitu. Q1 ma być TANIE (pięć
# wywołań + kontrola), Q2 potrzebuje limitu na tyle dużego, żeby JEDNA grupa go
# nasyciła — inaczej „30 dla jednej, 36 dla trzech" nie odróżnia limitu globalnego
# od per grupa (patrz `_rozstrzygnij_limit`).
LIMIT_Q1 = 20
LIMIT_Q2 = 30

# Tolerancja przy sprawdzaniu „czy najstarszy post mieści się w oknie". Zegary
# (nasz vs Apify vs FB) rozjeżdżają się o sekundy, a FB podaje czas posta
# z dokładnością do minuty — bez luzu post opublikowany dokładnie na granicy
# okna wyglądałby na dowód, że okno nie działa.
TOLERANCJA_OKNA = 0.10          # 10% szerokości okna
LUZ_OKNA_S = 300.0              # + 5 minut na rozjazd zegarów

# Nazwy pól z czasem publikacji, od najbardziej prawdopodobnej. Lista jest tylko
# podpowiedzią — `_czasy_postow` i tak sprawdza WSZYSTKIE pola itemu, bo nazwa
# pola w tym actorze to kolejna rzecz, której nie znamy przed pomiarem (i którą
# prompt 2 musi znać, żeby umieć odfiltrować stare posty po stronie fetchera).
POLA_CZASU = ("time", "timestamp", "date", "publishedAt", "publishTime",
              "createdAt", "postedAt", "creationTime")

# Pola z identyfikatorem posta — do porównywania ZESTAWÓW postów między oknami.
# Porównanie zestawów jest mocniejsze od porównania liczb: dwa okna mogą zwrócić
# po 20 postów i albo są to te same 20 (okno ignorowane), albo inne (okno działa).
POLA_ID = ("postId", "id", "postUrl", "topLevelUrl", "url", "facebookUrl")

# Statusy końcowe runu Apify — po nich nie ma na co czekać.
STATUSY_KONCOWE = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT")

# Najstarszy post, jaki uznajemy za sensowną datę. Chroni przed wzięciem `likes: 42`
# za znacznik czasu w epoce uniksowej (42 -> 1970 rok).
_NAJSTARSZY_SENSOWNY = datetime(2010, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Drobiazgi
# ---------------------------------------------------------------------------
def _kom(wartosc, format_="{:.2f}", pusto: str = "—") -> str:
    """Liczba do wypisania albo myślnik. Brak pomiaru ma wyglądać na brak pomiaru —
    nie na zero, bo zero w kolumnie kosztu czyta się jak dobra wiadomość."""
    return pusto if wartosc is None else format_.format(wartosc)


def _skrot_tokenu(token: str) -> str:
    """Klucz w postaci bezpiecznej do logów — zasada 4 z README (zero sekretów)."""
    return f"...{token[-4:]}" if len(token) >= 4 else "????"


def _teraz() -> datetime:
    return datetime.now(timezone.utc)


def sekundy_okna(okno: str) -> float | None:
    """"12 hours" -> 43200.0. None, gdy nie umiemy tego przeczytać.

    Potrzebne po NASZEJ stronie, żeby sprawdzić, czy actor rzeczywiście uciął po
    tym oknie — bez własnej interpretacji tekstu nie ma jak zweryfikować cudzej.
    """
    m = re.match(r"^\s*(\d+)\s*(minute|hour|day|week|month)s?\s*$", okno or "", re.I)
    if not m:
        return None
    ile, jednostka = int(m.group(1)), m.group(2).lower()
    mnoznik = {"minute": 60, "hour": 3600, "day": 86400,
               "week": 604800, "month": 2592000}[jednostka]
    return float(ile * mnoznik)


def _na_czas(wartosc) -> datetime | None:
    """Znacznik czasu z pola itemu -> datetime UTC. None, gdy to nie jest data.

    Przyjmujemy ISO 8601 (z „Z" włącznie) i epokę uniksową w sekundach albo
    milisekundach, bo nie wiemy z góry, w czym actor oddaje czas — a od tego zależy
    cała odpowiedź na PYTANIE 1.
    """
    if isinstance(wartosc, bool):
        return None
    if isinstance(wartosc, (int, float)):
        sekundy = float(wartosc)
        if sekundy > 1e11:               # milisekundy (po ~1973 r. w sekundach)
            sekundy /= 1000.0
        try:
            czas = datetime.fromtimestamp(sekundy, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(wartosc, str):
        tekst = wartosc.strip()
        if not tekst:
            return None
        if re.fullmatch(r"\d{9,14}", tekst):          # epoka podana jako napis
            return _na_czas(int(tekst))
        try:
            czas = datetime.fromisoformat(tekst.replace("Z", "+00:00"))
        except ValueError:
            return None
        if czas.tzinfo is None:
            czas = czas.replace(tzinfo=timezone.utc)
    else:
        return None
    # Odsiew liczb, które przypadkiem dają się przeczytać jako data (polubienia,
    # komentarze). Post z przyszłości albo sprzed 2010 r. to nie jest data posta.
    if czas < _NAJSTARSZY_SENSOWNY or czas > _teraz().replace(microsecond=0):
        return None
    return czas


def _czasy_postow(itemy: list[dict]) -> tuple[list[datetime], str]:
    """(czasy publikacji, nazwa pola, z którego je wzięliśmy).

    Wybieramy pole, które daje NAJWIĘCEJ poprawnych dat — nie pierwsze pasujące.
    Przy pierwszym pasującym wystarczyłoby, żeby jeden item miał `timestamp: 3`
    (liczba komentarzy), i cała analiza wieku poleciałaby na złym polu.
    """
    if not itemy:
        return [], ""
    nazwy: list[str] = list(POLA_CZASU)
    for item in itemy:
        if isinstance(item, dict):
            nazwy.extend(k for k in item if k not in nazwy)
    najlepsze, najlepsze_czasy = "", []
    for nazwa in nazwy:
        czasy = [c for item in itemy
                 if isinstance(item, dict) and (c := _na_czas(item.get(nazwa)))]
        if len(czasy) > len(najlepsze_czasy):
            najlepsze, najlepsze_czasy = nazwa, czasy
    return najlepsze_czasy, najlepsze


def _id_posta(item: dict, nr: int) -> str:
    """Identyfikator posta do porównywania zestawów między wywołaniami."""
    for nazwa in POLA_ID:
        wartosc = item.get(nazwa)
        if isinstance(wartosc, (str, int)) and str(wartosc).strip():
            return f"{nazwa}={wartosc}"
    return f"poz={nr}:{hash(json.dumps(item, sort_keys=True, default=str)) & 0xffffffff:x}"


def klucz_grupy(url: str) -> str:
    """`https://facebook.com/groups/123456/` -> `123456`.

    Po tym fragmencie rozpoznajemy, z której grupy przyszedł item — bez tego nie da
    się odpowiedzieć na PYTANIE 2 (czy limit rozdzielił się między grupy, czy pierwsza
    grupa zjadła całość).
    """
    m = re.search(r"/groups/([^/?#]+)", url or "", re.I)
    return (m.group(1) if m else (url or "")).strip().lower()


def _grupa_itema(item: dict, klucze: list[str]) -> str:
    """Do której z żądanych grup należy item ("" gdy nie da się rozstrzygnąć).

    Szukamy klucza grupy w CAŁYM itemie zrzuconym do tekstu, zamiast w konkretnym
    polu: nazwa pola z grupą jest kolejną niewiadomą tego actora, a identyfikator
    grupy i tak siedzi w linkach do posta.
    """
    tekst = json.dumps(item, default=str).lower()
    for klucz in klucze:
        if klucz and klucz in tekst:
            return klucz
    return ""


# ---------------------------------------------------------------------------
# Wynik jednego wywołania
# ---------------------------------------------------------------------------
@dataclass
class Wynik:
    """Jedno wywołanie actora — wszystko, co z niego wyciągnęliśmy."""

    etykieta: str
    grupy: list[str]
    limit: int
    okno: str | None = None                 # wartość pola okna albo None (kontrola)
    okno_s: float | None = None
    itemow: int = 0                         # ile pozycji trafiło do datasetu (= ile płacimy)
    itemow_pobranych: int = 0               # ile realnie ściągnęliśmy do analizy
    najstarszy_h: float | None = None
    najnowszy_h: float | None = None
    pole_czasu: str = ""
    id_postow: list[str] = field(default_factory=list)
    per_grupa: dict[str, int] = field(default_factory=dict)
    trwanie_s: float | None = None          # z run.stats.runTimeSecs (to, za co płacimy)
    trwanie_scienne_s: float = 0.0          # nasz zegar, razem z pollingiem
    status: str = ""
    run_id: str = ""
    klucz: str = ""
    koszt_saldo_usd: float | None = None    # różnica salda konta (PRZED vs PO)
    koszt_run_usd: float | None = None      # run.usageTotalUsd — kontrolnie
    blad: str = ""
    # Uwaga NIE unieważnia wywołania. Rozdział jest istotny: gdy padnie sam odczyt
    # salda PO runie, dane o postach są w porządku i mają wejść do rozstrzygnięcia
    # pytań 1 i 2 — brakuje wyłącznie jednej z dwóch miar kosztu.
    uwaga: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "SUCCEEDED" and not self.blad

    @property
    def zaczete_minuty(self) -> int | None:
        """Czas runu zaokrąglony w GÓRĘ do pełnej minuty.

        Rozliczanie „za zaczętą minutę" jest hipotezą, nie założeniem — jeśli tak
        jest, to seria krótkich wywołań kosztuje tyle samo co seria długich, a
        wtedy fetcher powinien wołać rzadziej i grubiej, nie częściej i cieniej.
        Kolumna jest po to, żeby `_koszt` mógł tę hipotezę sprawdzić na danych.
        """
        if self.trwanie_s is None:
            return None
        return max(1, math.ceil(self.trwanie_s / 60.0))

    @property
    def w_oknie(self) -> bool | None:
        """Czy najstarszy zwrócony post mieści się w zadanym oknie (None = nie wiadomo)."""
        if self.okno_s is None or self.najstarszy_h is None:
            return None
        return self.najstarszy_h * 3600.0 <= self.okno_s * (1 + TOLERANCJA_OKNA) + LUZ_OKNA_S


# ---------------------------------------------------------------------------
# Wołania Apify
# ---------------------------------------------------------------------------
def _naglowki(token: str) -> dict[str, str]:
    """Token w nagłówku, NIE w query stringu — inaczej wyciekałby do logów proxy."""
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _info_actora(klient, token: str, aktor: str) -> dict:
    """Metadane actora: wersja, numer builda, deklarowany cennik. Odczyt jest DARMOWY."""
    odp = klient.get(f"{API}/acts/{aktor}", headers=_naglowki(token))
    odp.raise_for_status()
    return odp.json().get("data", {})


def wejscie_actora(grupy: list[str], limit: int, okno: str | None,
                   pole_okna: str = POLE_OKNA) -> dict:
    """Wejście actora. ŚWIADOMIE minimalne — tylko to, o co pytamy.

    Każde dodatkowe pole (sortowanie, konfiguracja proxy Apify) to kolejna zmienna,
    której wpływu na wynik nie znamy, a przy niepoprawnej nazwie — odrzucone wejście
    i zmarnowane wywołanie. Pipeline dołoży resztę PO tym pomiarze, wiedząc już, co
    actor rozumie.
    """
    wejscie: dict = {"startUrls": [{"url": u} for u in grupy], "resultsLimit": limit}
    if okno:
        wejscie[pole_okna] = okno
    return wejscie


def _start_runu(klient, token: str, aktor: str, wejscie: dict,
                timeout_s: int, pamiec_mb: int) -> dict:
    odp = klient.post(
        f"{API}/acts/{aktor}/runs",
        params={"timeout": timeout_s, "memory": pamiec_mb},
        headers=_naglowki(token),
        json=wejscie,
    )
    odp.raise_for_status()
    return odp.json().get("data", {})


def _stan_runu(klient, token: str, run_id: str) -> dict:
    odp = klient.get(f"{API}/actor-runs/{run_id}", headers=_naglowki(token))
    odp.raise_for_status()
    return odp.json().get("data", {})


def _przerwij_run(klient, token: str, run_id: str) -> None:
    """Przerwij run, na który przestaliśmy czekać — inaczej mieli i nalicza dalej."""
    try:
        klient.post(f"{API}/actor-runs/{run_id}/abort", headers=_naglowki(token))
    except Exception:  # noqa: BLE001 — nieudane przerwanie nie może zepsuć pomiaru
        pass


def _dataset(klient, token: str, dataset_id: str) -> dict:
    odp = klient.get(f"{API}/datasets/{dataset_id}", headers=_naglowki(token))
    odp.raise_for_status()
    return odp.json().get("data", {})


def _itemy(klient, token: str, dataset_id: str, limit: int) -> list[dict]:
    """Pozycje z datasetu runu.

    BEZ `clean=true`: ten parametr ukrywa pozycje puste i pola techniczne, a my
    liczymy dokładnie to, za co Apify nalicza — czyli WSZYSTKIE pozycje datasetu.
    Czyszczenie zaniżyłoby mianownik w koszcie za post.
    """
    odp = klient.get(
        f"{API}/datasets/{dataset_id}/items",
        params={"format": "json", "limit": limit},
        headers=_naglowki(token),
    )
    odp.raise_for_status()
    dane = odp.json()
    return [i for i in dane if isinstance(i, dict)] if isinstance(dane, list) else []


# ---------------------------------------------------------------------------
# Pojedynczy pomiar
# ---------------------------------------------------------------------------
def _dokoncz_run(klient, token: str, run: dict, wynik: Wynik, uzyte_przed: float,
                 opcje: argparse.Namespace) -> None:
    """Doczekaj runu, zbierz itemy, policz wiek postów i różnicę salda."""
    wynik.run_id = run.get("id", "")
    koniec_czekania = time.monotonic() + opcje.limit_czekania
    stan = run
    while stan.get("status") not in STATUSY_KONCOWE:
        if time.monotonic() > koniec_czekania:
            _przerwij_run(klient, token, wynik.run_id)
            wynik.status = stan.get("status", "?")
            wynik.blad = (f"run nie skończył się w {opcje.limit_czekania:g}s — przerwany "
                          f"(status {wynik.status})")
            return
        time.sleep(opcje.polling)
        stan = _stan_runu(klient, token, wynik.run_id)

    wynik.status = stan.get("status", "?")
    statystyki = stan.get("stats") or {}
    trwanie = statystyki.get("runTimeSecs")
    wynik.trwanie_s = float(trwanie) if isinstance(trwanie, (int, float)) else None
    koszt_runu = stan.get("usageTotalUsd")
    wynik.koszt_run_usd = float(koszt_runu) if isinstance(koszt_runu, (int, float)) else None

    dataset_id = stan.get("defaultDatasetId") or ""
    if dataset_id:
        # itemCount z metadanych datasetu jest liczbą ROZLICZANĄ; len(itemy) to tylko
        # tyle, ile ściągnęliśmy do analizy. Przy rozjeździe wierzymy metadanym.
        try:
            wynik.itemow = int((_dataset(klient, token, dataset_id) or {}).get("itemCount") or 0)
        except Exception:  # noqa: BLE001 — brak metadanych nadrobi len(itemy) niżej
            pass
        itemy = _itemy(klient, token, dataset_id, opcje.limit_pobierania)
        wynik.itemow_pobranych = len(itemy)
        wynik.itemow = wynik.itemow or len(itemy)
        czasy, pole = _czasy_postow(itemy)
        wynik.pole_czasu = pole
        if czasy:
            teraz = _teraz()
            wynik.najstarszy_h = (teraz - min(czasy)).total_seconds() / 3600.0
            wynik.najnowszy_h = (teraz - max(czasy)).total_seconds() / 3600.0
        wynik.id_postow = [_id_posta(it, n) for n, it in enumerate(itemy)]
        if len(wynik.grupy) > 1:
            klucze = [klucz_grupy(u) for u in wynik.grupy]
            for it in itemy:
                nalezy = _grupa_itema(it, klucze) or "(nierozpoznana)"
                wynik.per_grupa[nalezy] = wynik.per_grupa.get(nalezy, 0) + 1

    if wynik.status == "SUCCEEDED" and not wynik.itemow:
        # Zero itemów przy statusie SUCCEEDED to informacja, nie awaria: okno mogło
        # być na tyle wąskie, że nic w nim nie ma. Zostawiamy bez błędu i pozwalamy
        # rozstrzygnąć `_rozstrzygnij_okno`.
        pass

    # Licznik zużycia po stronie Apify aktualizuje się z opóźnieniem — bez tej
    # przerwy różnica salda wychodziłaby zerowa i koszt posta wyszedłby 0 USD,
    # czyli błąd wyglądający jak świetna wiadomość.
    time.sleep(opcje.rozliczenie)
    try:
        wynik.koszt_saldo_usd = apify_credits.saldo(
            token, timeout=opcje.timeout_http).uzyte_usd - uzyte_przed
    except Exception as e:  # noqa: BLE001 — koszt z salda jest jedną z DWÓCH miar
        wynik.uwaga = f"nie odczytano salda PO runie ({type(e).__name__}); " \
                      f"koszt z usageTotalUsd"


def zmierz(rotator: KeyRotator, etykieta: str, grupy: list[str], limit: int,
           okno: str | None, opcje: argparse.Namespace) -> Wynik:
    """Jedno wywołanie actora, tą samą ścieżką co produkcja. Nigdy nie rzuca."""
    wynik = Wynik(etykieta=etykieta, grupy=list(grupy), limit=limit,
                  okno=okno, okno_s=sekundy_okna(okno) if okno else None)
    wejscie = wejscie_actora(grupy, limit, okno, opcje.pole_okna)
    start = time.monotonic()

    def _wywolanie(token: str) -> Wynik:
        wynik.klucz = _skrot_tokenu(token)
        # Saldo PRZED czytamy jeszcze przed uruchomieniem runu: martwy klucz odpada
        # tutaj (401/402 -> rotacja), zanim wydamy choćby grosz.
        uzyte_przed = apify_credits.saldo(token, timeout=opcje.timeout_http).uzyte_usd
        with apify_proxy.client_for_token(token, timeout=opcje.timeout_http) as klient:
            run = _start_runu(klient, token, opcje.aktor, wejscie,
                              opcje.timeout_runu, opcje.pamiec)
            # OD TEJ LINII nic nie może polecieć wyżej. Run JEST uruchomiony i JEST
            # płatny, a KeyRotator ponawia CAŁE `fn` po błędzie przejściowym — wyjątek
            # stąd oznaczałby drugi płatny run za ten sam pomiar (i zafałszowany koszt).
            # Wszystko, co pójdzie nie tak po starcie, jest po prostu wynikiem.
            try:
                _dokoncz_run(klient, token, run, wynik, uzyte_przed, opcje)
            except BaseException as e:  # noqa: BLE001 — patrz komentarz wyżej
                wynik.blad = f"{type(e).__name__}: {e}"
        return wynik

    try:
        rotator.call(_wywolanie)
    except AllKeysExhausted as e:
        wynik.blad = f"AllKeysExhausted: {e}"
    except Exception as e:  # noqa: BLE001 — np. HTTP 400 = actor odrzucił wejście
        # To NIE jest awaria pomiaru, tylko jego wynik: odrzucone wejście przy węższym
        # oknie jest odpowiedzią na PYTANIE 1 (ścieżka B).
        wynik.blad = f"{type(e).__name__}: {e}"
    wynik.trwanie_scienne_s = time.monotonic() - start
    return wynik


# ---------------------------------------------------------------------------
# PYTANIE 1 — rozstrzygnięcie
# ---------------------------------------------------------------------------
def _rozstrzygnij_okno(wyniki: list[Wynik]) -> tuple[str, str, list[str]]:
    """(ŚCIEŻKA A/B/NIEROZSTRZYGNIĘTE, najmniejsza działająca jednostka, uzasadnienie)."""
    linie: list[str] = []
    kontrola = next((w for w in wyniki if w.okno is None), None)
    z_oknem = [w for w in wyniki if w.okno]
    udane = [w for w in z_oknem if w.ok]
    z_itemami = [w for w in udane if w.itemow > 0 and w.okno_s is not None]

    bledne = [w for w in z_oknem if not w.ok]
    if bledne:
        linie.append("Okna zakończone błędem: "
                     + ", ".join(f"{w.okno} ({w.blad or w.status})" for w in bledne))

    if not udane:
        if kontrola and kontrola.ok:
            linie.append(f"Kontrola BEZ pola `{POLE_OKNA}` zwróciła {kontrola.itemow} "
                         f"itemów, a KAŻDE wywołanie z tym polem padło — actor tego "
                         f"pola nie przyjmuje.")
            return "ŚCIEŻKA B", "brak (pole odrzucone)", linie
        linie.append("Żadne wywołanie się nie udało — pomiar nie odpowiada na to pytanie. "
                     "Sprawdź klucze, proxy i czy grupa jest publiczna.")
        return "NIEROZSTRZYGNIĘTE", "nie ustalono", linie

    # Kryterium z zadania wprost: „1 hour" i „1 day" dają to samo -> jednostka ignorowana.
    godzina = next((w for w in udane if w.okno == "1 hour"), None)
    doba = next((w for w in udane if w.okno == "1 day"), None)
    if godzina and doba and godzina.itemow and godzina.itemow == doba.itemow:
        te_same = set(godzina.id_postow) == set(doba.id_postow) and bool(godzina.id_postow)
        linie.append(f"„1 hour” i „1 day” zwróciły po {godzina.itemow} itemów"
                     + (" i są to DOKŁADNIE te same posty." if te_same
                        else " (zestawy postów się różnią — sama równość liczb może "
                             "wynikać z nasycenia resultsLimit)."))
        if te_same:
            return "ŚCIEŻKA B", "brak (jednostka ignorowana)", linie

    # Zestaw postów identyczny z kontrolą przy WĄSKIM oknie = pole nic nie zmienia.
    # Sprawdzamy tylko NAJWĘŻSZE okno: przy szerokim identyczny zestaw jest normalny
    # (grupa po prostu nie ma starszych postów) i niczego by nie dowodził.
    if kontrola and kontrola.ok and kontrola.id_postow and z_itemami:
        naj = min(z_itemami, key=lambda x: x.okno_s or 0)
        if (set(naj.id_postow) == set(kontrola.id_postow)
                and (naj.okno_s or 0) <= 86400 and naj.w_oknie is False):
            linie.append(f"Okno „{naj.okno}” zwróciło DOKŁADNIE ten sam zestaw postów co "
                         f"wywołanie bez pola, a najstarszy z nich ma "
                         f"{naj.najstarszy_h:.1f} h — pole jest ignorowane.")
            return "ŚCIEŻKA B", "brak (jednostka ignorowana)", linie

    # Główne kryterium: czy najwęższe okno, które coś zwróciło, faktycznie ucięło.
    if not z_itemami:
        linie.append("Wszystkie okna zwróciły 0 itemów — grupa jest za cicha na ten "
                     "pomiar albo nasze wejście jest niepoprawne. Powtórz na grupie "
                     "z ruchem (kilka postów dziennie).")
        return "NIEROZSTRZYGNIĘTE", "nie ustalono", linie

    najwezsze = min(z_itemami, key=lambda w: w.okno_s or 0)
    if najwezsze.w_oknie is False:
        linie.append(f"Najwęższe okno, które coś zwróciło („{najwezsze.okno}”), oddało "
                     f"post starszy niż samo okno (najstarszy: {najwezsze.najstarszy_h:.1f} h "
                     f"przy oknie {najwezsze.okno_s / 3600:.1f} h) — actor NIE tnie po tym polu.")
        return "ŚCIEŻKA B", "brak (jednostka ignorowana)", linie

    # Monotoniczność: zwężanie okna nie może zwiększać liczby itemów.
    posortowane = sorted(udane, key=lambda w: w.okno_s or 0, reverse=True)
    ciag = " -> ".join(f"{w.okno}: {w.itemow}" for w in posortowane)
    rosnie = [(a.okno, b.okno) for a, b in zip(posortowane, posortowane[1:])
              if b.itemow > a.itemow]
    linie.append(f"Liczba itemów przy zwężaniu okna: {ciag}.")
    if rosnie:
        linie.append("UWAGA: zwężenie okna ZWIĘKSZYŁO liczbę itemów przy: "
                     + ", ".join(f"{a} -> {b}" for a, b in rosnie)
                     + " — to nie powinno się zdarzyć przy działającym oknie.")

    potwierdzone = [w for w in z_itemami if w.w_oknie is True]
    najmniejsza = min(potwierdzone, key=lambda w: w.okno_s or 0).okno if potwierdzone else ""
    for w in potwierdzone:
        linie.append(f"Okno „{w.okno}”: najstarszy post {_kom(w.najstarszy_h, '{:.2f}')} h, "
                     f"najnowszy {_kom(w.najnowszy_h, '{:.2f}')} h — MIEŚCI SIĘ w oknie.")
    puste = [w for w in udane if w.itemow == 0]
    if puste:
        linie.append("Okna bez itemów (spójne z działającym oknem, ale samodzielnie "
                     "niczego nie dowodzą): " + ", ".join(w.okno for w in puste) + ".")
    if bledne and potwierdzone:
        linie.append(f"Najmniejsza jednostka POTWIERDZONA danymi: „{najmniejsza}”. Węższe "
                     f"okna albo padły, albo nic nie zwróciły — traktuj to jako granicę, "
                     f"poniżej której nie ma dowodu.")
    return "ŚCIEŻKA A", najmniejsza or "nie ustalono", linie


# ---------------------------------------------------------------------------
# PYTANIE 2 — rozstrzygnięcie
# ---------------------------------------------------------------------------
def _rozstrzygnij_limit(jedna: Wynik | None, trzy: Wynik | None) -> tuple[str, list[str]]:
    """(PER GRUPA / GLOBALNY / NIEROZSTRZYGNIĘTE, uzasadnienie)."""
    linie: list[str] = []
    if jedna is None or trzy is None:
        return "NIEROZSTRZYGNIĘTE", ["Pomiar nie został wykonany (za mało grup "
                                     "zweryfikowanych — potrzebne trzy)."]
    if not (jedna.ok and trzy.ok):
        for w in (jedna, trzy):
            if not w.ok:
                linie.append(f"Wywołanie „{w.etykieta}” nie powiodło się: "
                             f"{w.blad or w.status}.")
        return "NIEROZSTRZYGNIĘTE", linie

    limit = jedna.limit
    linie.append(f"resultsLimit={limit}: jedna grupa -> {jedna.itemow} itemów, "
                 f"trzy grupy -> {trzy.itemow} itemów.")
    if trzy.per_grupa:
        linie.append("Rozkład przy trzech grupach: "
                     + ", ".join(f"{k}: {n}" for k, n in sorted(
                         trzy.per_grupa.items(), key=lambda kv: -kv[1])) + ".")

    if jedna.itemow < limit:
        # Bez nasycenia limitu przez JEDNĄ grupę test nie rozróżnia hipotez: przy
        # 12 postach na grupę „36 dla trzech" pasuje i do limitu globalnego 30
        # (prawie nasyconego), i do limitu per grupa (dalekiego od nasycenia).
        linie.append(f"UWAGA: pojedyncza grupa zwróciła {jedna.itemow} < {limit} itemów, "
                     f"czyli NIE nasyciła limitu. Przy nienasyconym limicie ten test "
                     f"niczego nie rozstrzyga — powtórz z --limit-q2 mniejszym niż "
                     f"{max(1, jedna.itemow)}.")
        return "NIEROZSTRZYGNIĘTE", linie

    if trzy.itemow >= 2 * limit:
        linie.append(f"{trzy.itemow} ≈ 3 × {limit} — limit działa PER GRUPA. Batchowanie "
                     f"grup w jednym wywołaniu jest bezpieczne.")
        return "PER GRUPA", linie
    if trzy.itemow <= math.ceil(1.34 * limit):
        najwiecej = max(trzy.per_grupa.values(), default=0)
        if najwiecej >= 0.9 * trzy.itemow and trzy.itemow:
            linie.append("Limit jest GLOBALNY i zjadany przez pierwszą grupę — pozostałe "
                         "grupy w tym samym wywołaniu nie dostają NIC.")
        else:
            linie.append("Limit jest GLOBALNY i dzielony między grupy — każda dostaje "
                         "ułamek tego, co dostałaby sama.")
        linie.append("Batchowanie grup w jednym wywołaniu jest NIEBEZPIECZNE: batch po "
                     "dziesięć grup zgubiłby posty z ośmiu z nich. Wołaj jedną grupę "
                     "na wywołanie.")
        return "GLOBALNY", linie

    linie.append(f"Wynik pośredni ({trzy.itemow} przy limicie {limit} i trzech grupach) — "
                 f"nie pasuje ani do limitu globalnego, ani do per grupa. Powtórz pomiar "
                 f"na grupach o podobnym ruchu.")
    return "NIEROZSTRZYGNIĘTE", linie


# ---------------------------------------------------------------------------
# PYTANIE 3 — koszt
# ---------------------------------------------------------------------------
def _dopasuj(punkty: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """Regresja liniowa y = a*x + b -> (a, b, R²). None, gdy nie ma czego dopasować."""
    n = len(punkty)
    if n < 3:
        return None
    sx = sum(x for x, _ in punkty)
    sy = sum(y for _, y in punkty)
    sxx = sum(x * x for x, _ in punkty)
    sxy = sum(x * y for x, y in punkty)
    mianownik = n * sxx - sx * sx
    if abs(mianownik) < 1e-12:            # wszystkie x identyczne — brak zmienności
        return None
    a = (n * sxy - sx * sy) / mianownik
    b = (sy - a * sx) / n
    srednia = sy / n
    calkowita = sum((y - srednia) ** 2 for _, y in punkty)
    resztowa = sum((y - (a * x + b)) ** 2 for x, y in punkty)
    r2 = 1.0 if calkowita < 1e-18 else 1.0 - resztowa / calkowita
    return a, b, r2


@dataclass
class Koszt:
    """Odpowiedź na PYTANIE 3 razem z tym, co ją podważa."""

    itemow: int = 0
    z_salda_usd: float | None = None
    z_runow_usd: float | None = None
    za_post_usd: float | None = None
    za_post_krancowy_usd: float | None = None   # nachylenie: koszt KOLEJNEGO posta
    stale_za_run_usd: float | None = None       # wyraz wolny: co płacimy za sam run
    r2_itemy: float | None = None
    r2_minuty: float | None = None
    za_zaczeta_minute_usd: float | None = None
    stale_minutowe_usd: float | None = None     # wyraz wolny modelu minutowego
    cena_z_cennika_usd: float | None = None
    linie: list[str] = field(default_factory=list)


def _policz_koszt(wyniki: list[Wynik], cena_z_cennika: float | None) -> Koszt:
    """Koszt jednego posta — z salda konta, z kontrolą modelu rozliczania.

    Samo „różnica salda / suma itemów" odpowiada na pytanie tylko wtedy, gdy cena
    jest wprost proporcjonalna do liczby postów. Jeśli w rozliczeniu siedzi składnik
    STAŁY (za uruchomienie albo za ZACZĘTĄ minutę), ta sama liczba wyjdzie inna przy
    innej wielkości wywołania — a pomiar zrobiony na krótkich runach zawyżyłby koszt
    produkcyjny albo odwrotnie. Dlatego liczymy oba modele i mówimy, który pasuje
    do danych.
    """
    k = Koszt()
    udane = [w for w in wyniki if w.ok]
    k.itemow = sum(w.itemow for w in udane)
    k.cena_z_cennika_usd = cena_z_cennika

    z_salda = [w.koszt_saldo_usd for w in udane if w.koszt_saldo_usd is not None]
    z_runow = [w.koszt_run_usd for w in udane if w.koszt_run_usd is not None]
    k.z_salda_usd = sum(z_salda) if z_salda else None
    k.z_runow_usd = sum(z_runow) if z_runow else None

    podstawa = k.z_salda_usd if k.z_salda_usd is not None else k.z_runow_usd
    if podstawa is None:
        k.linie.append("Nie udało się odczytać ANI salda konta, ANI usageTotalUsd — "
                       "koszt posta pozostaje niezmierzony.")
        return k
    zrodlo = "salda konta" if k.z_salda_usd is not None else "usageTotalUsd runów"
    if not k.itemow:
        k.linie.append(f"Seria nie pobrała żadnego posta — nie ma przez co dzielić "
                       f"{podstawa:.4f} USD z {zrodlo}.")
        return k

    k.za_post_usd = podstawa / k.itemow
    k.linie.append(f"{podstawa:.4f} USD z {zrodlo} / {k.itemow} postów = "
                   f"{k.za_post_usd:.6f} USD za post.")
    if k.z_salda_usd is not None and k.z_runow_usd is not None:
        roznica = abs(k.z_salda_usd - k.z_runow_usd)
        k.linie.append(f"Kontrolnie: saldo konta {k.z_salda_usd:.4f} USD vs suma "
                       f"usageTotalUsd {k.z_runow_usd:.4f} USD (rozjazd "
                       f"{roznica:.4f} USD)."
                       + (" Rozjazd większy niż 20% — licznik konta mógł nie zdążyć "
                          "się zaktualizować; powtórz z większym --rozliczenie."
                          if k.z_salda_usd and roznica > 0.2 * abs(k.z_salda_usd) else ""))

    # Model 1: koszt = stała_za_run + koszt_krańcowy × liczba postów.
    punkty = [(float(w.itemow), float(w.koszt_saldo_usd if w.koszt_saldo_usd is not None
                                      else w.koszt_run_usd))
              for w in udane
              if w.koszt_saldo_usd is not None or w.koszt_run_usd is not None]
    dopasowanie = _dopasuj(punkty)
    if dopasowanie:
        a, b, r2 = dopasowanie
        k.za_post_krancowy_usd, k.stale_za_run_usd, k.r2_itemy = a, b, r2
        k.linie.append(f"Model „stała + za post”: {b:.6f} USD za sam run + {a:.6f} USD "
                       f"za każdy post (R² = {r2:.3f}).")

    # Model 2: koszt = stała + cena × ZACZĘTA minuta (hipoteza rozliczania czasu).
    punkty_min = [(float(w.zaczete_minuty), float(w.koszt_saldo_usd
                                                  if w.koszt_saldo_usd is not None
                                                  else w.koszt_run_usd))
                  for w in udane
                  if w.zaczete_minuty is not None
                  and (w.koszt_saldo_usd is not None or w.koszt_run_usd is not None)]
    dopasowanie_min = _dopasuj(punkty_min)
    if dopasowanie_min:
        a, b, r2 = dopasowanie_min
        k.za_zaczeta_minute_usd, k.stale_minutowe_usd, k.r2_minuty = a, b, r2
        k.linie.append(f"Model „za zaczętą minutę”: {b:.6f} USD + {a:.6f} USD za każdą "
                       f"rozpoczętą minutę runu (R² = {r2:.3f}).")

    # Wyraz wolny wyraźnie UJEMNY znaczy, że model nie opisuje rozliczenia, tylko
    # przeszedł przez punkty — run o zerowym czasie nie może oddawać pieniędzy.
    # Bez tego sprawdzenia wysokie R² takiego dopasowania (a przy krótkiej serii
    # bywa wysokie) wyglądałoby na dowód rozliczania czasu.
    minutowy_sensowny = (k.stale_minutowe_usd is not None
                         and k.stale_minutowe_usd > -0.01)
    itemowy_sensowny = k.stale_za_run_usd is not None and k.stale_za_run_usd > -0.01
    for nazwa, sensowny, stala in (("za zaczętą minutę", minutowy_sensowny,
                                    k.stale_minutowe_usd),
                                   ("stała + za post", itemowy_sensowny,
                                    k.stale_za_run_usd)):
        if stala is not None and not sensowny:
            k.linie.append(f"Model „{nazwa}” ma UJEMNY składnik stały ({stala:.6f} USD) — "
                           f"nie opisuje rozliczenia, tylko przechodzi przez punkty. "
                           f"Odrzucony.")

    if k.r2_itemy is not None and k.r2_minuty is not None:
        if k.r2_minuty > k.r2_itemy + 0.15 and minutowy_sensowny:
            k.linie.append("Dane lepiej tłumaczy CZAS RUNU niż liczba postów — "
                           "rozliczenie idzie za zaczętą minutę. Wniosek dla fetchera: "
                           "wołaj RZADZIEJ i grubiej; częstsze, płytkie runy płacą "
                           "za to samo.")
        elif k.r2_itemy > k.r2_minuty + 0.15 or (itemowy_sensowny and not minutowy_sensowny):
            k.linie.append("Dane lepiej tłumaczy LICZBA POSTÓW niż czas runu — "
                           "rozliczenie jest za pozycję w datasecie. Częste, płytkie "
                           "runy nie są karane.")
        else:
            k.linie.append("Oba modele tłumaczą dane podobnie — seria jest za mało "
                           "zróżnicowana, żeby je rozdzielić. Nie opieraj na tym decyzji "
                           "o częstotliwości crona bez powtórzenia pomiaru.")
    if (k.stale_za_run_usd is not None and k.za_post_usd
            and k.stale_za_run_usd > 0.25 * podstawa / max(1, len(udane))):
        k.linie.append(f"Składnik STAŁY runu ({k.stale_za_run_usd:.6f} USD) jest istotny "
                       f"wobec kosztu pojedynczego wywołania — koszt za post zmierzony "
                       f"na małych runach NIE przenosi się wprost na produkcję.")
    if cena_z_cennika is not None and k.za_post_usd:
        iloraz = k.za_post_usd / cena_z_cennika if cena_z_cennika else 0.0
        k.linie.append(f"Cennik actora: {cena_z_cennika:.6f} USD za jednostkę — zmierzony "
                       f"koszt to {iloraz:.2f}× tej ceny."
                       + (" Rozjazd powyżej 1.5× oznacza, że płacimy też za coś poza "
                          "pozycjami datasetu (czas, proxy Apify)." if iloraz > 1.5 else ""))
    return k


# ---------------------------------------------------------------------------
# Raport
# ---------------------------------------------------------------------------
def _uwagi(w: Wynik) -> str:
    """Zawartość kolumny „błąd” — błąd unieważniający albo uwaga do wywołania."""
    czesci = [c for c in ((w.blad or w.status) if not w.ok else "", w.uwaga) if c]
    return (" / ".join(czesci) or "—").replace("|", "/")[:110]


def _tabela_okien(wyniki: list[Wynik]) -> list[str]:
    linie = ["| okno | itemów | najstarszy post | najnowszy post | w oknie? | czas runu | "
             "zaczęte min | koszt USD | błąd |",
             "|---|---|---|---|---|---|---|---|---|"]
    for w in wyniki:
        w_oknie = {True: "tak", False: "**NIE**", None: "—"}[w.w_oknie]
        koszt = w.koszt_saldo_usd if w.koszt_saldo_usd is not None else w.koszt_run_usd
        linie.append(
            f"| {w.okno or '_(bez pola — kontrola)_'} | {w.itemow} | "
            f"{_kom(w.najstarszy_h, '{:.2f} h')} | {_kom(w.najnowszy_h, '{:.2f} h')} | "
            f"{w_oknie} | {_kom(w.trwanie_s, '{:.1f} s')} | "
            f"{_kom(w.zaczete_minuty, '{:d}')} | {_kom(koszt, '{:.5f}')} | "
            f"{_uwagi(w)} |")
    return linie


def _tabela_limitu(wyniki: list[Wynik]) -> list[str]:
    linie = ["| wywołanie | grup | resultsLimit | itemów | itemów/grupę | czas runu | "
             "koszt USD | błąd |",
             "|---|---|---|---|---|---|---|---|"]
    for w in wyniki:
        rozklad = (", ".join(f"{k}: {n}" for k, n in sorted(
            w.per_grupa.items(), key=lambda kv: -kv[1])) or "—")
        koszt = w.koszt_saldo_usd if w.koszt_saldo_usd is not None else w.koszt_run_usd
        linie.append(
            f"| {w.etykieta} | {len(w.grupy)} | {w.limit} | {w.itemow} | {rozklad} | "
            f"{_kom(w.trwanie_s, '{:.1f} s')} | {_kom(koszt, '{:.5f}')} | "
            f"{_uwagi(w)} |")
    return linie


def zbuduj_raport(*, wyniki_okna: list[Wynik], wyniki_limitu: list[Wynik],
                  sciezka_okna: str, najmniejsza_jednostka: str, linie_okna: list[str],
                  werdykt_limitu: str, linie_limitu: list[str], koszt: Koszt,
                  info_actora: dict, grupy: list[str], opcje: argparse.Namespace) -> str:
    """Treść docs/POMIAR-ACTORA.md — plik, który prompt 2 czyta przed `_build_actor_input`."""
    wersja = info_actora.get("_wersja") or "nieustalona"
    build = info_actora.get("_build") or "nieustalony"
    pole_czasu = next((w.pole_czasu for w in wyniki_okna + wyniki_limitu if w.pole_czasu), "")
    uzyte_klucze = sorted({w.klucz for w in wyniki_okna + wyniki_limitu if w.klucz})
    wszystkie = wyniki_okna + wyniki_limitu

    L: list[str] = []
    L.append("# Pomiar actora `apify/facebook-groups-scraper`")
    L.append("")
    L.append("Wynik JEDNORAZOWEGO pomiaru (`laweta_radar/scripts/pomiar_actora.py`). "
             "Nie jest to dokumentacja actora, tylko protokół z tego, co actor zrobił "
             "na tej grupie, tego dnia, na tej wersji. Powtórz pomiar, gdy zmieni się "
             "wersja actora albo gdy liczby przestaną się zgadzać z rachunkiem.")
    L.append("")
    L.append(f"- **Data pomiaru:** {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}")
    L.append(f"- **Actor:** `{opcje.aktor}`, wersja **{wersja}**, build **{build}**")
    L.append(f"- **Grupy testowe:** " + ", ".join(f"`{u}`" for u in grupy))
    L.append(f"- **Klucze użyte (rotacja):** {', '.join(uzyte_klucze) or '—'}")
    L.append(f"- **Wywołań łącznie:** {len(wszystkie)}, "
             f"**pobranych postów:** {sum(w.itemow for w in wszystkie)}")
    if pole_czasu:
        L.append(f"- **Pole z czasem publikacji w itemie:** `{pole_czasu}` "
                 f"(tego używa fetcher do odrzucania starych postów)")
    L.append("")
    L.append("## Rozstrzygnięcie")
    L.append("")
    L.append("| Pytanie | Odpowiedź |")
    L.append("|---|---|")
    L.append(f"| 1. Najmniejsza jednostka `{opcje.pole_okna}` | **{sciezka_okna}** — "
             f"potwierdzona jednostka: **{najmniejsza_jednostka}** |")
    L.append(f"| 2. `resultsLimit` przy wielu grupach | **{werdykt_limitu}** |")
    L.append(f"| 3. Koszt jednego pobranego posta | "
             f"**{_kom(koszt.za_post_usd, '{:.6f} USD')}** |")
    L.append("")

    L.append(f"## PYTANIE 1 — najmniejsza jednostka `{opcje.pole_okna}`")
    L.append("")
    L.extend(_tabela_okien(wyniki_okna))
    L.append("")
    L.append(f"**{sciezka_okna}.**")
    L.append("")
    for linia in linie_okna:
        L.append(f"- {linia}")
    L.append("")
    if sciezka_okna == "ŚCIEŻKA A":
        L.append(f"> Okno czasowe DZIAŁA do `{najmniejsza_jednostka}`. Fetcher ma je "
                 f"ustawiać na długość swojego interwału (z zapasem na opóźnienie "
                 f"publikacji) i NIE pobierać po raz drugi tego, co już ma.")
    elif sciezka_okna == "ŚCIEŻKA B":
        L.append("> Okno czasowe NIE DZIAŁA. Każdy przebieg pobiera te same najnowsze "
                 "posty i płacimy za nie ponownie. Odsiew wieku musi się dziać PO "
                 "naszej stronie (po polu z czasem, patrz wyżej), a jedynym pokrętłem "
                 "kosztu zostaje `resultsLimit` i częstotliwość crona.")
    else:
        L.append("> **Pomiar nie rozstrzygnął tego pytania.** Nie buduj `_build_actor_input` "
                 "na założeniu, że okno działa — powtórz pomiar zgodnie z uwagami wyżej.")
    L.append("")

    L.append("## PYTANIE 2 — `resultsLimit` przy wielu grupach")
    L.append("")
    if wyniki_limitu:
        L.extend(_tabela_limitu(wyniki_limitu))
    else:
        L.append("_Pomiar nie został wykonany._")
    L.append("")
    L.append(f"**{werdykt_limitu}.**")
    L.append("")
    for linia in linie_limitu:
        L.append(f"- {linia}")
    L.append("")

    L.append("## PYTANIE 3 — koszt jednego pobranego posta")
    L.append("")
    L.append("| Miara | Wartość |")
    L.append("|---|---|")
    L.append(f"| Pobranych postów w serii | {koszt.itemow} |")
    L.append(f"| Koszt z salda konta (suma różnic PRZED/PO) | "
             f"{_kom(koszt.z_salda_usd, '{:.4f} USD')} |")
    L.append(f"| Koszt z `usageTotalUsd` runów (kontrolnie) | "
             f"{_kom(koszt.z_runow_usd, '{:.4f} USD')} |")
    L.append(f"| **Koszt za post (prosty iloraz)** | "
             f"**{_kom(koszt.za_post_usd, '{:.6f} USD')}** |")
    L.append(f"| Koszt krańcowy kolejnego posta (regresja) | "
             f"{_kom(koszt.za_post_krancowy_usd, '{:.6f} USD')} |")
    L.append(f"| Składnik stały jednego runu (regresja) | "
             f"{_kom(koszt.stale_za_run_usd, '{:.6f} USD')} |")
    L.append(f"| Koszt zaczętej minuty runu (regresja) | "
             f"{_kom(koszt.za_zaczeta_minute_usd, '{:.6f} USD')} "
             f"(składnik stały {_kom(koszt.stale_minutowe_usd, '{:.6f} USD')}) |")
    L.append(f"| Dopasowanie modeli (R²: posty / minuty) | "
             f"{_kom(koszt.r2_itemy, '{:.3f}')} / {_kom(koszt.r2_minuty, '{:.3f}')} |")
    L.append(f"| Cena z cennika actora | "
             f"{_kom(koszt.cena_z_cennika_usd, '{:.6f} USD')} |")
    L.append("")
    for linia in koszt.linie:
        L.append(f"- {linia}")
    L.append("")

    L.append("## Co z tego wynika dla `_build_actor_input`")
    L.append("")
    if sciezka_okna == "ŚCIEŻKA A":
        L.append(f"1. Ustawiaj `{opcje.pole_okna}` na okno nie węższe niż "
                 f"`{najmniejsza_jednostka}` — poniżej tej granicy pomiar nie ma dowodu.")
    elif sciezka_okna == "ŚCIEŻKA B":
        L.append(f"1. **NIE licz na `{opcje.pole_okna}`** — pole nic nie zmienia. "
                 f"Filtruj wiek po swojej stronie"
                 + (f", po polu `{pole_czasu}`." if pole_czasu else "."))
    else:
        L.append(f"1. Rozstrzygnij najpierw pytanie 1 — bez tego nie da się napisać "
                 f"`{opcje.pole_okna}` w wejściu actora w sposób, na którym można polegać.")
    if werdykt_limitu == "PER GRUPA":
        L.append("2. Wolno pakować wiele grup do jednego `startUrls` — `resultsLimit` "
                 "liczy się osobno dla każdej.")
    elif werdykt_limitu == "GLOBALNY":
        L.append("2. **Jedna grupa na wywołanie.** `resultsLimit` jest globalny, więc "
                 "batch grup gubi posty z większości z nich.")
    else:
        L.append("2. Do czasu rozstrzygnięcia pytania 2 wołaj **jedną grupę na "
                 "wywołanie** — to wariant, który przy obu hipotezach jest poprawny, "
                 "tylko przy jednej droższy.")
    if koszt.za_post_usd:
        L.append(f"3. Do `POSTY_NA_DOBE` wstaw budżet policzony z "
                 f"**{koszt.za_post_usd:.6f} USD za post**"
                 + (f" plus {koszt.stale_za_run_usd:.6f} USD za każde wywołanie"
                    if koszt.stale_za_run_usd and koszt.stale_za_run_usd > 0 else "") + ".")
    else:
        L.append("3. Kosztu nie zmierzono — `POSTY_NA_DOBE` nie ma się na czym oprzeć.")
    L.append("")
    L.append("---")
    L.append("")
    L.append("Pomiar powtórzysz: `python laweta_radar/scripts/pomiar_actora.py` "
             "(najpierw `--sucho`, żeby zobaczyć plan i koszt).")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# Plan i budżet
# ---------------------------------------------------------------------------
def _plan(grupy: list[str], opcje: argparse.Namespace) -> list[tuple[str, list[str], int, str | None]]:
    """Lista wywołań do wykonania: (etykieta, grupy, limit, okno)."""
    plan: list[tuple[str, list[str], int, str | None]] = []
    jedna = grupy[:1]
    if not opcje.bez_kontroli:
        # Kontrola BEZ pola okna. Jedno wywołanie więcej, ale to jedyny sposób, żeby
        # odróżnić „okno zwróciło wszystko, bo grupa jest mała" od „pole jest
        # ignorowane": porównujemy ZESTAWY postów, nie tylko ich liczbę.
        plan.append(("kontrola (bez pola)", jedna, opcje.limit_q1, None))
    plan.extend((okno, jedna, opcje.limit_q1, okno) for okno in OKNA)
    if len(grupy) >= 3 and not opcje.bez_limitu:
        plan.append(("1 grupa", jedna, opcje.limit_q2, None))
        plan.append(("3 grupy", grupy[:3], opcje.limit_q2, None))
    return plan


def _prognoza(plan) -> int:
    """Najgorszy przypadek liczby pobranych postów — do decyzji „odpalać czy nie".

    Liczymy PESYMISTYCZNIE (limit razy liczba grup), bo przy limicie per grupa
    wywołanie z trzema grupami odda trzykrotność — a to, czy tak jest, dopiero
    mierzymy. Prognoza, która zakłada odpowiedź na własne pytanie, jest bez wartości.
    """
    return sum(limit * max(1, len(grupy)) for _, grupy, limit, _ in plan)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _argumenty(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="pomiar_actora.py",
        description="Jednorazowy pomiar actora apify/facebook-groups-scraper: okno "
                    "czasowe, resultsLimit przy wielu grupach, realny koszt posta.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Najpierw ZAWSZE: --sucho (pokazuje plan i koszt, nie dotyka sieci).",
    )
    ap.add_argument("--grupa", action="append", default=[], metavar="URL",
                    help="grupa testowa (można podać wielokrotnie; do PYTANIA 2 potrzebne "
                         "trzy). Domyślnie brane są grupy ze statusem 'ok' z config/groups.py")
    ap.add_argument("--potwierdzam-publiczne", action="store_true",
                    help="wymagane przy --grupa: potwierdzasz, że grupa jest PUBLICZNA "
                         "i sprawdzona ręcznie (na prywatnej zmierzysz błąd, a run i tak "
                         "zostanie policzony)")
    ap.add_argument("--sucho", action="store_true",
                    help="pokaż plan i prognozę kosztu, NIE wołaj Apify")
    ap.add_argument("--tak", action="store_true",
                    help="nie pytaj o potwierdzenie (do uruchomień nieinteraktywnych)")
    ap.add_argument("--budzet-postow", type=int, default=500, metavar="N",
                    help="twardy sufit pobranych postów w całej serii (domyślnie 500)")
    ap.add_argument("--limit-q1", type=int, default=LIMIT_Q1, metavar="N",
                    help=f"resultsLimit w pomiarze okna (domyślnie {LIMIT_Q1})")
    ap.add_argument("--limit-q2", type=int, default=LIMIT_Q2, metavar="N",
                    help=f"resultsLimit w pomiarze limitu (domyślnie {LIMIT_Q2})")
    ap.add_argument("--odstep", type=float, default=5.0, metavar="S",
                    help="przerwa między wywołaniami actora w sekundach (domyślnie 5); "
                         "wywołania idą PO KOLEI, nigdy równolegle")
    ap.add_argument("--rozliczenie", type=float, default=10.0, metavar="S",
                    help="ile czekać po runie na aktualizację licznika zużycia "
                         "(domyślnie 10)")
    ap.add_argument("--polling", type=float, default=5.0, metavar="S",
                    help="co ile sprawdzać status runu (domyślnie 5)")
    ap.add_argument("--limit-czekania", type=float, default=420.0, metavar="S",
                    help="po ilu sekundach przerwać run, który nie kończy (domyślnie 420)")
    ap.add_argument("--timeout-runu", type=int, default=konfig_grup.APIFY_TIMEOUT,
                    metavar="S", help="timeout runu po stronie Apify "
                                      f"(domyślnie {konfig_grup.APIFY_TIMEOUT})")
    ap.add_argument("--timeout-http", type=float, default=60.0, metavar="S",
                    help="timeout pojedynczego zapytania HTTP (domyślnie 60)")
    ap.add_argument("--pamiec", type=int, default=1024, metavar="MB",
                    help="pamięć runu w MB (domyślnie 1024)")
    ap.add_argument("--limit-pobierania", type=int, default=1000, metavar="N",
                    help="ile pozycji datasetu ściągać do analizy (domyślnie 1000)")
    ap.add_argument("--pole-okna", default=POLE_OKNA, metavar="NAZWA",
                    help=f"nazwa pola z oknem czasowym (domyślnie {POLE_OKNA}) — zmień, "
                         f"jeśli actor odrzuci tę nazwę")
    ap.add_argument("--aktor", default=konfig_grup.APIFY_ACTOR, metavar="ID",
                    help=f"identyfikator actora (domyślnie {konfig_grup.APIFY_ACTOR})")
    ap.add_argument("--bez-kontroli", action="store_true",
                    help="pomiń wywołanie kontrolne bez pola okna (oszczędza jedno "
                         "wywołanie, ale osłabia rozstrzygnięcie ścieżki A/B)")
    ap.add_argument("--bez-limitu", action="store_true",
                    help="pomiń PYTANIE 2 (pomiar resultsLimit przy wielu grupach)")
    ap.add_argument("--raport", default=str(_ROOT / "docs" / "POMIAR-ACTORA.md"),
                    metavar="PLIK", help="gdzie zapisać wynik")
    return ap.parse_args(argv[1:])


def _wybierz_grupy(opcje: argparse.Namespace) -> tuple[list[str], list[str]]:
    """(grupy do pomiaru, komunikaty). Pusta lista = nie ma czego mierzyć.

    Domyślne źródło to config/groups.py ze statusem "ok" — a ten status oznacza
    dokładnie to, czego wymaga zasada pomiaru: człowiek WESZEDŁ w grupę i potwierdził,
    że jest publiczna i żywa. Z zewnątrz nie da się tego sprawdzić (FB pokazuje
    niezalogowanym ścianę logowania), więc grupę spoza tej listy przyjmujemy tylko
    z jawnym oświadczeniem operatora.
    """
    komunikaty: list[str] = []
    if opcje.grupa:
        if not opcje.potwierdzam_publiczne:
            komunikaty.append(
                "Podałeś --grupa, ale bez --potwierdzam-publiczne. Na grupie prywatnej "
                "albo martwej zmierzysz komunikat błędu, a nie zachowanie actora — i tak "
                "czy siak zapłacisz za run. Wejdź w grupę zalogowany, sprawdź, że jest "
                "publiczna, i powtórz z tą flagą.")
            return [], komunikaty
        komunikaty.append(f"Grupy z wiersza poleceń ({len(opcje.grupa)}), publiczność "
                          f"potwierdzona przez operatora.")
        return list(opcje.grupa), komunikaty

    grupy = [g["url"] for g in konfig_grup.grupy_do_pobrania()]
    if not grupy:
        komunikaty.append(
            "Brak zweryfikowanych grup w config/groups.py (wszystkie są 'unverified' "
            "albo bez adresu), a pomiar na niesprawdzonej grupie mierzy błąd, nie "
            "zachowanie actora. Ustaw status 'ok' po ręcznej weryfikacji albo podaj "
            "--grupa URL --potwierdzam-publiczne.")
        return [], komunikaty
    komunikaty.append(f"Grupy z config/groups.py ze statusem 'ok': {len(grupy)}.")
    if len(grupy) < 3:
        komunikaty.append("Mniej niż trzy zweryfikowane grupy — PYTANIE 2 (resultsLimit "
                          "przy wielu grupach) zostanie pominięte.")
    return grupy, komunikaty


def _cena_z_cennika(info: dict) -> float | None:
    """Deklarowana cena jednostkowa actora z jego metadanych (albo None).

    Bierzemy OSTATNI wpis z `pricingInfos` — cennik bywa historią zmian, a nas
    interesuje ten obowiązujący dziś.
    """
    wpisy = info.get("pricingInfos")
    if not isinstance(wpisy, list) or not wpisy:
        return None
    for wpis in reversed(wpisy):
        if not isinstance(wpis, dict):
            continue
        for nazwa in ("pricePerUnitUsd", "pricePerUnitUsdWithMargin", "unitPriceUsd"):
            wartosc = wpis.get(nazwa)
            if isinstance(wartosc, (int, float)) and not isinstance(wartosc, bool):
                return float(wartosc)
    return None


def _main(argv: list[str]) -> int:
    opcje = _argumenty(argv)

    tokeny = load_apify_tokens()
    grupy, komunikaty = _wybierz_grupy(opcje)
    for k in komunikaty:
        print(f"[pomiar] {k}")

    plan = _plan(grupy or ["(grupa nieustalona)"], opcje)
    prognoza = _prognoza(plan)

    print(f"\n[pomiar] Plan: {len(plan)} wywołań actora {opcje.aktor}, po kolei, "
          f"z przerwą {opcje.odstep:g}s.")
    for etykieta, gr, limit, okno in plan:
        pole = f"{opcje.pole_okna}={okno!r}" if okno else f"BEZ {opcje.pole_okna}"
        print(f"[pomiar]   - {etykieta:<22} grup: {len(gr)}, resultsLimit={limit}, {pole}")
    print(f"[pomiar] Prognoza PESYMISTYCZNA: do {prognoza} pobranych postów "
          f"(sufit --budzet-postow: {opcje.budzet_postow}).")

    if prognoza > opcje.budzet_postow:
        print(f"[pomiar] STOP: plan przekracza sufit o {prognoza - opcje.budzet_postow} "
              f"postów. Zmniejsz --limit-q1/--limit-q2 albo świadomie podnieś "
              f"--budzet-postow.")
        return 1

    if opcje.sucho:
        print("[pomiar] --sucho: nic nie wywołano, nic nie zapłacono.")
        return 0

    if not tokeny:
        print("[pomiar] Brak kluczy APIFY_API_TOKEN* — kończę bez działania. "
              "Sprawdź: python -m laweta_radar.config.settings")
        return 0
    if not grupy:
        print("[pomiar] Brak grupy testowej — kończę bez działania.")
        return 0

    wolno, linie = apify_proxy.preflight(tokens=tokeny)
    for linia in linie:
        print(linia)
    if not wolno:
        print("[pomiar] Kończę bez działania (patrz wyżej).")
        return 0

    rotator = KeyRotator.for_tokens(
        tokeny,
        # Padnięte proxy psuje JEDEN klucz, a następny ma inne wyjście — przy pomiarze
        # wolimy przeskoczyć niż stracić całą serię. Tak samo robi produkcja, gdy proxy
        # jest skonfigurowane (patrz docstring KeyRotator).
        transient_key_switches=2 if apify_proxy.is_enabled() else 0,
    )
    print(f"[pomiar] Kluczy w puli: {rotator.key_count}")

    # Odczyt metadanych actora jest DARMOWY, a daje wersję do raportu i cenę
    # katalogową do porównania — dlatego robimy go przed pytaniem o zgodę.
    info: dict = {}
    try:
        def _pobierz(token: str) -> dict:
            with apify_proxy.client_for_token(token, timeout=opcje.timeout_http) as klient:
                return _info_actora(klient, token, opcje.aktor)
        info = rotator.call(_pobierz)
        oznaczone = (info.get("taggedBuilds") or {}).get("latest") or {}
        info["_wersja"] = oznaczone.get("versionNumber") or ""
        info["_build"] = str(oznaczone.get("buildNumber") or "")
        cena = _cena_z_cennika(info)
        print(f"[pomiar] Actor: {info.get('username')}/{info.get('name')}, wersja "
              f"{info['_wersja'] or '?'}, build {info['_build'] or '?'}, cennik: "
              + (f"{cena:.6f} USD/jednostkę" if cena is not None else "nieznany"))
        if cena is not None:
            print(f"[pomiar] Prognoza kosztu wg cennika: do {prognoza * cena:.2f} USD "
                  f"(bez ewentualnego składnika stałego runu).")
    except Exception as e:  # noqa: BLE001 — brak metadanych nie blokuje pomiaru
        cena = None
        print(f"[pomiar] Nie udało się odczytać metadanych actora ({type(e).__name__}: {e}) "
              f"— lecę dalej, raport będzie bez wersji i bez ceny katalogowej.")

    if not opcje.tak:
        if not sys.stdin.isatty():
            print("[pomiar] Wejście nie jest terminalem, a pomiar kosztuje — przerywam. "
                  "Świadome uruchomienie nieinteraktywne: --tak")
            return 1
        odpowiedz = input(f"\nOdpalić {len(plan)} wywołań (do {prognoza} postów)? "
                          f"wpisz TAK: ").strip()
        if odpowiedz != "TAK":
            print("[pomiar] Przerwane — nic nie wywołano.")
            return 0

    wyniki: list[Wynik] = []
    for nr, (etykieta, gr, limit, okno) in enumerate(plan, 1):
        if nr > 1:
            time.sleep(opcje.odstep)     # po kolei, z odstępem — nigdy równolegle
        print(f"\n[pomiar] ({nr}/{len(plan)}) {etykieta} — grup: {len(gr)}, "
              f"limit {limit}, okno {okno or 'BRAK'} ...")
        w = zmierz(rotator, etykieta, gr, limit, okno, opcje)
        wyniki.append(w)
        koszt = w.koszt_saldo_usd if w.koszt_saldo_usd is not None else w.koszt_run_usd
        print(f"[pomiar]     -> {w.status or 'BŁĄD'}: {w.itemow} itemów, "
              f"najstarszy {_kom(w.najstarszy_h, '{:.2f} h')}, "
              f"czas {_kom(w.trwanie_s, '{:.1f} s')}, koszt {_kom(koszt, '{:.5f} USD')}"
              + (f", błąd: {w.blad}" if w.blad else ""))
        zebrane = sum(x.itemow for x in wyniki)
        if zebrane > opcje.budzet_postow:
            print(f"[pomiar] STOP: pobrano już {zebrane} postów, sufit to "
                  f"{opcje.budzet_postow}. Przerywam serię — raport powstanie z tego, "
                  f"co zdążyliśmy zmierzyć.")
            break

    etykiety_limitu = {"1 grupa", "3 grupy"}
    wyniki_okna = [w for w in wyniki if w.etykieta not in etykiety_limitu]
    wyniki_limitu = [w for w in wyniki if w.etykieta in etykiety_limitu]

    sciezka, jednostka, linie_okna = _rozstrzygnij_okno(wyniki_okna)
    werdykt_limitu, linie_limitu = _rozstrzygnij_limit(
        next((w for w in wyniki_limitu if w.etykieta == "1 grupa"), None),
        next((w for w in wyniki_limitu if w.etykieta == "3 grupy"), None),
    )
    koszt = _policz_koszt(wyniki, cena)

    print("\n" + "=" * 72)
    print(f"PYTANIE 1: {sciezka} — najmniejsza potwierdzona jednostka: {jednostka}")
    for linia in linie_okna:
        print(f"  · {linia}")
    print(f"\nPYTANIE 2: {werdykt_limitu}")
    for linia in linie_limitu:
        print(f"  · {linia}")
    print(f"\nPYTANIE 3: {_kom(koszt.za_post_usd, '{:.6f} USD')} za post")
    for linia in koszt.linie:
        print(f"  · {linia}")
    print("=" * 72)

    raport = zbuduj_raport(
        wyniki_okna=wyniki_okna, wyniki_limitu=wyniki_limitu, sciezka_okna=sciezka,
        najmniejsza_jednostka=jednostka, linie_okna=linie_okna,
        werdykt_limitu=werdykt_limitu, linie_limitu=linie_limitu, koszt=koszt,
        info_actora=info, grupy=grupy, opcje=opcje,
    )
    sciezka_pliku = Path(opcje.raport)
    sciezka_pliku.parent.mkdir(parents=True, exist_ok=True)
    sciezka_pliku.write_text(raport, encoding="utf-8")
    print(f"\n[pomiar] Raport: {sciezka_pliku}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
