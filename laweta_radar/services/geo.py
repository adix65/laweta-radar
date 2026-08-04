"""Geo — z tego, co model wyciągnął z posta, robi współrzędne, kilometry
i JEDEN KLIKALNY LINK, który na telefonie otwiera Google Maps z gotową trasą.

DECYZJA ARCHITEKTONICZNA, KTÓREJ SIĘ TRZYMAMY: nie używamy Google Geocoding API
ani żadnego płatnego geokodera. 90% przypadków to kod pocztowy albo nazwa
miasta, a to załatwia lokalna baza — za darmo, offline, w mikrosekundy, bez
limitu zapytań i bez klucza, który może wygasnąć w środku nocy. Google Maps
używamy WYŁĄCZNIE jako deep link: darmowy, bez API key, otwiera natywną
aplikację na Androidzie i iOS.

TRASA, NIE PROMIEŃ. Przy transporcie międzynarodowym „ile km od bazy" nie
znaczy nic — liczy się DŁUGOŚĆ KURSU odbiór->dostawa. Ta liczba jest pierwsza
na ekranie; dystans od bazy do odbioru idzie obok, jako druga, pomocnicza.
ŻADNA Z NICH NIE SŁUŻY DO FILTROWANIA. Zasada naczelna repo: system pokazuje
zlecenia, decyduje kierowca — o tym, czy kurs pod Kolonię się opłaca, decyduje
człowiek patrzący na trasę, a nie kod porównujący kilometry z progiem.

`zrodlo` W KAŻDYM PUNKCIE JEST CZĘŚCIĄ PRODUKTU, nie diagnostyką. Wartość
"miasto_niepewne" znaczy „w Polsce jest kilkanaście miejscowości o tej nazwie
i wybraliśmy największą". To MUSI trafić do interfejsu: operator ma zobaczyć,
że lokalizacja jest zgadywana, ZANIM pojedzie 60 km.

ZERO WYWOŁAŃ SIECIOWYCH. Moduł czyta jeden plik CSV przy pierwszym użyciu
i dalej działa z pamięci. Bazę pobiera osobno `scripts/pobierz_geo.py`.

CLI:
    python -m laweta_radar.services.geo "Krosno" "Rzeszow"
    python -m laweta_radar.services.geo --kody "auto stoi w 50667 Koln, cena 2500 zl"
"""
from __future__ import annotations

import csv
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from laweta_radar.config import settings

KTO = "geo"

# Współczynnik korekty linii prostej na realną drogę. W Polsce, poza
# autostradami, trasa jest średnio o jakieś 20-30% dłuższa od odległości
# w linii prostej — stąd 1.25.
KOREKTA_TRASY = 1.25

PROMIEN_ZIEMI_KM = 6371.0

# Współrzędne Krosna — baza domyślna. Używane, gdy BAZA_LAT/BAZA_LNG nie są
# ustawione w .env. Świadomie NIE zerujemy do (0,0): punkt na Zatoce Gwinejskiej
# daje dystanse rzędu 5000 km, które wyglądają jak awaria geokodera, a nie jak
# brak konfiguracji.
KROSNO = (49.6886, 21.7706)


@dataclass(frozen=True)
class Punkt:
    """Miejsce na mapie wraz z informacją, SKĄD je znamy.

    `zrodlo`:
      "kod"             — dokładne dopasowanie kodu pocztowego, najpewniejsze;
      "miasto"          — nazwa miasta jednoznaczna w bazie;
      "miasto_niepewne" — nazwa niejednoznaczna, wzięliśmy największą
                          miejscowość o tej nazwie. POKAŻ TO OPERATOROWI;
      "baza"            — punkt operatora z .env.
    """

    lat: float
    lng: float
    zrodlo: str
    nazwa: str

    @property
    def niepewny(self) -> bool:
        """Skrót dla warstwy wyżej: czy przy tym punkcie postawić ostrzeżenie."""
        return self.zrodlo.endswith("_niepewne")

    def wspolrzedne(self) -> str:
        """Format `lat,lng` do URL-a Map. Sześć miejsc = ok. 10 cm, aż nadto."""
        return f"{self.lat:.6f},{self.lng:.6f}"


# ---------------------------------------------------------------------------
# NORMALIZACJA NAZW
#
# Post pisze "Rzeszow", baza ma "Rzeszów"; post pisze "KROSNO", baza "Krosno".
# Bez wspólnej formy żadne z tych dopasowań nie zadziała. `ł` nie rozkłada się
# przez NFKD (to osobna litera, nie l z akcentem), więc polskie znaki mapujemy
# jawnie; NFKD dobiera resztę alfabetów, które realnie tu wpadają — niemiecki,
# czeski, francuski.
# ---------------------------------------------------------------------------
_POLSKIE = str.maketrans({
    "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n",
    "ó": "o", "ś": "s", "ż": "z", "ź": "z",
    "Ą": "a", "Ć": "c", "Ę": "e", "Ł": "l", "Ń": "n",
    "Ó": "o", "Ś": "s", "Ż": "z", "Ź": "z",
})


def normalizuj_nazwe(nazwa: str) -> str:
    s = (nazwa or "").strip().lower().translate(_POLSKIE)
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    # Myślnik i kropka jako spacja: "Bielsko-Biala" i "Bielsko Biala" to jedno
    # miasto, a "sw. Anny" i "swietej Anny" pisze się w postach obu sposobami.
    s = re.sub(r"[-–—./]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# EGZONIMY — polskie nazwy miast zagranicznych.
#
# GeoNames zna "Köln", a post mówi "kupiłem auto w Kolonii". Bez tej tabeli
# najlepszy typ zlecenia, jaki ten system ma znajdować (transport z zagranicy
# zestawem B+E), nie geokoduje się w ogóle. Tabela jest krótka celowo: tylko
# kierunki, które realnie pojawiają się w tych grupach.
# ---------------------------------------------------------------------------
EGZONIMY: dict[str, str] = {
    "kolonia": "koln",
    "monachium": "munchen",
    "moguncja": "mainz",
    "norymberga": "nurnberg",
    "drezno": "dresden",
    "lipsk": "leipzig",
    "hanower": "hannover",
    "brema": "bremen",
    "akwizgran": "aachen",
    "wieden": "wien",
    "praga": "praha",
    "brno": "brno",
    "bratyslawa": "bratislava",
    "koszyce": "kosice",
    "bruksela": "bruxelles",
    "antwerpia": "antwerpen",
    "haga": "den haag",
    "paryz": "paris",
    "marsylia": "marseille",
    "lyon": "lyon",
    "mediolan": "milano",
    "rzym": "roma",
    "turyn": "torino",
    "wenecja": "venezia",
    "florencja": "firenze",
    "neapol": "napoli",
}


# ---------------------------------------------------------------------------
# BAZA KODÓW
#
# Plik CSV: kraj,kod,miejscowosc,wojewodztwo,lat,lng — jeden format dla
# wszystkich krajów. Wczytywany LENIWIE, przy pierwszym pytaniu: import modułu
# ma być darmowy, żeby dało się go zaimportować w teście, który geokodowania
# w ogóle nie dotyka.
# ---------------------------------------------------------------------------
_KATALOG_REPO = Path(__file__).resolve().parent.parent.parent
SCIEZKI_BAZY = (
    _KATALOG_REPO / "data" / "kody_eu.csv",
    _KATALOG_REPO / "data" / "kody_pl.csv",
)

_indeks_kodow: dict[str, list[dict]] | None = None
_indeks_miast: dict[str, list[dict]] | None = None
_zaladowana_sciezka: Path | None = None


def _normalizuj_kod(kod: str) -> str:
    """Kod do postaci indeksowanej: wielkie litery, bez spacji i myślników.

    "110 00", "11000" i "110-00" to ten sam czeski kod; "38-400" i "38400" to
    ten sam polski. Indeksujemy formę bez separatorów, a wyświetlamy oryginalną.
    """
    return re.sub(r"[\s.-]", "", (kod or "")).upper()


def _wczytaj(sciezka: Path | None = None) -> tuple[dict, dict]:
    """Zbuduj indeksy z pliku CSV. Brak pliku = puste indeksy, NIE wyjątek.

    Brak bazy geo to system, którego jeszcze nie włączono (plik pobiera się
    osobnym skryptem), a nie awaria. `geokoduj` odda wtedy None i warstwa wyżej
    po prostu nie pokaże trasy — dokładnie to samo, co przy poście bez miejsca.
    """
    global _zaladowana_sciezka
    kandydaci = [sciezka] if sciezka else list(SCIEZKI_BAZY)
    plik = next((p for p in kandydaci if p and p.exists()), None)
    _zaladowana_sciezka = plik
    if plik is None:
        return {}, {}

    po_kodzie: dict[str, list[dict]] = {}
    po_nazwie: dict[str, list[dict]] = {}
    with plik.open(encoding="utf-8", newline="") as f:
        for wiersz in csv.DictReader(f):
            try:
                lat = float(wiersz["lat"])
                lng = float(wiersz["lng"])
            except (TypeError, ValueError, KeyError):
                continue  # wiersz bez współrzędnych jest bezużyteczny
            rekord = {
                "kraj": (wiersz.get("kraj") or "PL").strip().upper(),
                "kod": (wiersz.get("kod") or "").strip(),
                "miejscowosc": (wiersz.get("miejscowosc") or "").strip(),
                "wojewodztwo": (wiersz.get("wojewodztwo") or "").strip(),
                "lat": lat,
                "lng": lng,
            }
            if rekord["kod"]:
                po_kodzie.setdefault(_normalizuj_kod(rekord["kod"]), []).append(rekord)
            if rekord["miejscowosc"]:
                po_nazwie.setdefault(normalizuj_nazwe(rekord["miejscowosc"]), []).append(rekord)
    return po_kodzie, po_nazwie


def zaladuj(sciezka: Path | None = None) -> None:
    """Wczytaj bazę (ponownie). Argument służy TYLKO testom i CLI."""
    global _indeks_kodow, _indeks_miast
    _indeks_kodow, _indeks_miast = _wczytaj(sciezka)


def _indeksy() -> tuple[dict, dict]:
    if _indeks_kodow is None or _indeks_miast is None:
        zaladuj()
    return _indeks_kodow or {}, _indeks_miast or {}


def stan_bazy() -> str:
    """Jedna linia do logu startowego i check_setup.sh."""
    kody, miasta = _indeksy()
    if not kody and not miasta:
        return (f"[{KTO}] BRAK bazy kodów — szukałem w: "
                + ", ".join(str(p) for p in SCIEZKI_BAZY)
                + ". Pobierz: python laweta_radar/scripts/pobierz_geo.py")
    return (f"[{KTO}] baza: {_zaladowana_sciezka}, {len(kody)} kodów, "
            f"{len(miasta)} nazw miejscowości")


# ---------------------------------------------------------------------------
# GEOKODOWANIE
# ---------------------------------------------------------------------------
def _srodek(rekordy: list[dict]) -> tuple[float, float]:
    """Środek zbioru punktów o tej samej nazwie.

    Duże miasto ma w bazie kilkadziesiąt kodów pocztowych rozrzuconych po
    dzielnicach; średnia z nich trafia mniej więcej w centrum. Pojedynczy
    wylosowany kod trafiłby w przypadkowe osiedle, czasem 8 km od ratusza.
    """
    return (sum(r["lat"] for r in rekordy) / len(rekordy),
            sum(r["lng"] for r in rekordy) / len(rekordy))


def geokoduj(kod: str | None, miasto: str | None) -> Punkt | None:
    """Kod pocztowy i/lub nazwa miasta -> Punkt. Brak dopasowania -> None.

    Kolejność prób jest kolejnością PEWNOŚCI:
      1. kod pocztowy — dokładne dopasowanie, `zrodlo="kod"`;
      2. nazwa miasta — po normalizacji; gdy nazwa jest niejednoznaczna
         (kilkanaście „Nowych Wsi" w Polsce), bierzemy największą miejscowość
         o tej nazwie i ustawiamy `zrodlo="miasto_niepewne"`;
      3. brak -> None. BEZ ZGADYWANIA: null jest lepszy niż zła współrzędna,
         bo zła współrzędna wyśle człowieka 80 km w złą stronę.
    """
    po_kodzie, po_nazwie = _indeksy()

    # --- 1. kod pocztowy ---
    klucz = _normalizuj_kod(kod or "")
    if klucz and klucz in po_kodzie:
        trafienia = po_kodzie[klucz]
        # Ten sam ciąg cyfr bywa kodem w kilku krajach (pięciocyfrowe DE
        # i czterocyfrowe AT/BE nie są rozłączne). Przy kolizji wygrywa PL —
        # tu jesteśmy — a kraj i tak wchodzi do nazwy, więc widać, co wybrano.
        rekord = next((r for r in trafienia if r["kraj"] == "PL"), trafienia[0])
        return Punkt(
            lat=rekord["lat"],
            lng=rekord["lng"],
            zrodlo="kod",
            nazwa=f"{rekord['miejscowosc']} {rekord['kod']} ({rekord['kraj']})".strip(),
        )

    # --- 2. nazwa miasta ---
    nazwa = normalizuj_nazwe(miasto or "")
    nazwa = EGZONIMY.get(nazwa, nazwa)
    if not nazwa or nazwa not in po_nazwie:
        return None

    rekordy = po_nazwie[nazwa]
    # Grupujemy po regionie: „Nowa Wieś" w podkarpackim i w mazowieckim to dwie
    # różne miejscowości, ale trzydzieści kodów Warszawy to jedno miasto.
    grupy: dict[tuple[str, str], list[dict]] = {}
    for r in rekordy:
        grupy.setdefault((r["kraj"], r["wojewodztwo"]), []).append(r)

    # „Największa" mierzona LICZBĄ KODÓW POCZTOWYCH. To proxy, nie ludność:
    # plik GeoNames z kodami nie niesie populacji, a dociąganie drugiego
    # zbioru tylko po to byłoby droższe niż zysk. Proxy jest dobre tam, gdzie
    # ma znaczenie (Warszawa ma ich setki, wieś jeden) i słabe tam, gdzie i tak
    # zgadujemy — dlatego przy niejednoznaczności zawsze wychodzi
    # `miasto_niepewne`, niezależnie od tego, jak pewnie wygląda zwycięzca.
    najwieksza = max(grupy.values(), key=len)
    lat, lng = _srodek(najwieksza)
    wzor = najwieksza[0]
    jednoznaczne = len(grupy) == 1
    opis_regionu = f", {wzor['wojewodztwo']}" if wzor["wojewodztwo"] else ""
    return Punkt(
        lat=lat,
        lng=lng,
        zrodlo="miasto" if jednoznaczne else "miasto_niepewne",
        nazwa=(f"{wzor['miejscowosc']}{opis_regionu} ({wzor['kraj']})"
               + ("" if jednoznaczne else f" — {len(grupy)} miejscowości o tej nazwie")),
    )


def baza() -> Punkt:
    """Punkt operatora z .env. Bez konfiguracji — Krosno, i mówimy o tym w nazwie."""
    if settings.BAZA_LAT and settings.BAZA_LNG:
        return Punkt(settings.BAZA_LAT, settings.BAZA_LNG, "baza", settings.BAZA_NAZWA)
    return Punkt(KROSNO[0], KROSNO[1], "baza", f"{settings.BAZA_NAZWA} (domyślne — ustaw BAZA_LAT/BAZA_LNG)")


# ---------------------------------------------------------------------------
# DYSTANS
# ---------------------------------------------------------------------------
def dystans_km(a: Punkt, b: Punkt) -> float:
    """Szacowana długość drogi między punktami, w kilometrach.

    Haversine (linia prosta po kuli) razy 1.25, bo w Polsce poza autostradami
    trasa jest średnio o 20-30% dłuższa od odległości w linii prostej.

    TA LICZBA SŁUŻY DO PRZESIEWU, NIE DO WYCENY. Jest szacunkiem z dwóch
    punktów na mapie i nie wie nic o tym, że między nimi leży rzeka bez mostu
    albo droga zamknięta na remont — na trasie Krosno-Rzeszów potrafi zaniżyć
    o kilkanaście procent. Nie wstawiaj jej na fakturę; do rozliczenia użyj
    kilometrów z licznika albo z nawigacji.
    """
    fi1, fi2 = math.radians(a.lat), math.radians(b.lat)
    dfi = math.radians(b.lat - a.lat)
    dlam = math.radians(b.lng - a.lng)
    h = (math.sin(dfi / 2) ** 2
         + math.cos(fi1) * math.cos(fi2) * math.sin(dlam / 2) ** 2)
    prosta = 2 * PROMIEN_ZIEMI_KM * math.asin(min(1.0, math.sqrt(h)))
    return round(prosta * KOREKTA_TRASY, 1)


# ---------------------------------------------------------------------------
# LINKI
#
# Format `maps/dir/?api=1` otwiera natywną aplikację Google Maps na Androidzie
# i iOS, nie wymaga klucza API i nic nie kosztuje. To jest cały powód, dla
# którego nie ma tu żadnego płatnego routingu: operator i tak patrzy na trasę
# w mapach, więc liczenie jej po naszej stronie byłoby płaceniem za drugą kopię
# tej samej informacji.
# ---------------------------------------------------------------------------
_MAPY = "https://www.google.com/maps/dir/?api=1"


def link_do_map(baza_pkt: Punkt, odbior: Punkt | None, dostawa: Punkt | None) -> str:
    """URL trasy baza -> (odbiór) -> dostawa. Bez żadnego punktu: pusty string.

    Pusty string, a nie link donikąd: warstwa wyżej po prostu nie pokazuje
    przycisku. Przycisk otwierający mapę bez celu jest gorszy niż jego brak,
    bo operator klika go w biegu i traci sekundy na nic.
    """
    cel = dostawa or odbior
    if cel is None:
        return ""
    czesci = [
        _MAPY,
        f"origin={quote(baza_pkt.wspolrzedne())}",
        f"destination={quote(cel.wspolrzedne())}",
    ]
    # Waypoint tylko wtedy, gdy są OBA punkty — inaczej odbiór byłby
    # jednocześnie przystankiem i celem, a mapy pokazałyby trasę do siebie.
    if odbior is not None and dostawa is not None:
        czesci.append(f"waypoints={quote(odbior.wspolrzedne())}")
    czesci.append("travelmode=driving")
    return "&".join(czesci)


def link_do_nawigacji(cel: Punkt) -> str:
    """Wariant „jedź tam teraz" — od razu odpala nawigację turn-by-turn."""
    return (f"{_MAPY}&destination={quote(cel.wspolrzedne())}"
            f"&travelmode=driving&dir_action=navigate")


# ---------------------------------------------------------------------------
# KALKULACJA
# ---------------------------------------------------------------------------
def kalkulacja(km: float) -> dict:
    """Prosty szacunek ze stawek z .env.

    TO JEST ETYKIETA NA EKRANIE, NIE BRAMKA. Nic się na jej podstawie nie
    ukrywa ani nie odrzuca — kierowca patrzy na liczbę i sam decyduje, czy
    kurs mu się opłaca. Zna swój sprzęt, swój kalendarz i swoją cenę lepiej
    niż ten wzór.
    """
    km = max(0.0, float(km or 0.0))
    return {
        "km_trasy": round(km, 1),
        "szacunek_pln": round(max(settings.STAWKA_MINIMALNA, km * settings.STAWKA_ZA_KM), 2),
    }


# ---------------------------------------------------------------------------
# WYCIĄGANIE KODÓW Z SUROWEGO TEKSTU — WIELOKRAJOWO
#
# Samo „pięć cyfr" złapie też fragment numeru telefonu i cenę, a „cztery cyfry"
# złapie rok i kwotę. Dlatego formaty dwuznaczne wymagają KONTEKSTU, a przy
# niepewności co do kraju zwracamy "?" i zostawiamy rozstrzygnięcie
# geokoderowi — zamiast pomijać kod, którego mogliśmy użyć.
# ---------------------------------------------------------------------------
_WZORCE = [
    # (nazwa, regex, kraj-domyślny, czy wymaga kontekstu)
    # PL: dwie cyfry, myślnik, trzy cyfry. Format unikalny — w numerze telefonu
    # "555-111-222" grupy mają po trzy cyfry, więc lookaroundy na cyfrę
    # I MYŚLNIK wystarczą, żeby go nie tknąć.
    ("PL", re.compile(r"(?<![0-9-])[0-9]{2}-[0-9]{3}(?![0-9-])"), "PL", False),
    # NL: cztery cyfry + dwie litery. Też unikalny.
    ("NL", re.compile(r"(?<![0-9A-Za-z])[0-9]{4} ?[A-Za-z]{2}(?![0-9A-Za-z])"), "NL", False),
    # CZ/SK: trzy cyfry, spacja, dwie cyfry. Format wspólny dla obu krajów,
    # więc kraj rozstrzyga wyłącznie kontekst.
    ("CZ_SK", re.compile(r"(?<![0-9])[0-9]{3} [0-9]{2}(?![0-9])"), "?", False),
    # DE: pięć cyfr. Wymaga kontekstu — inaczej złapie kawałek telefonu.
    ("DE", re.compile(r"(?<![0-9])[0-9]{5}(?![0-9])"), "DE", True),
    # AT/BE: cztery cyfry. Najgroźniejszy wzorzec — rok, cena, moc silnika.
    ("AT_BE", re.compile(r"(?<![0-9])[0-9]{4}(?![0-9])"), "?", True),
]

# Słowa, które przesądzają kraj. Szukane w oknie wokół kodu, po normalizacji.
# Zapisane jako RDZENIE, nie pełne formy: post odmienia ("w Wiedniu", "z Niemiec",
# "pod Kolonią"), więc "wieden" nie trafiłoby w "wiedniu", a "praga" w "pradze".
_SYGNALY_KRAJU: dict[str, tuple[str, ...]] = {
    "DE": ("niemiec", "niemczech", "niemieck", "deutschland", "aus ", " de ", "germany",
           "koln", "kolon", "hamburg", "berlin", "monachium", "munchen", "drezn", "dresden"),
    "CZ": ("czech", "czesk", "praha", "prag", "brno", "brnie", "ostrav", " cz "),
    "SK": ("slowac", "slovensk", "bratislav", "koszyc", "kosice", "zilin", " sk "),
    "NL": ("holand", "niderland", "nederland", "amsterdam", "rotterdam", "utrecht", "hadze"),
    "AT": ("austri", "osterreich", "wiedn", "wien", "graz", "salzburg", "linz"),
    "BE": ("belgi", "bruksel", "brussel", "antwerp", "gandaw", "gent", "liege", "luik"),
    "FR": ("francj", "france", "paryz", "paris", "lyon", "marsyl"),
    "IT": ("wloch", "italia", "mediolan", "milano", "rzym", "roma", "turyn", "torino"),
    "PL": ("polsk", "kraju"),
}

# Jednostki i waluty tuż po liczbie — jednoznaczny sygnał, że to NIE kod.
_PO_LICZBIE_NIE_KOD = re.compile(
    r"^\s*(zl|zlotych|pln|eur|euro|usd|km|kg|t\b|ton|kw|km/h|%|szt|cm|mm|godz|min|rok|roku)",
    re.IGNORECASE)

# To samo przed liczbą.
_PRZED_LICZBA_NIE_KOD = re.compile(
    r"(za|cena|kosztuje|placi|placę|place|oplata|rok|rocznik|przebieg|poj|pojemnosc|tel|telefon|nr)"
    r"[\s:.]*$", re.IGNORECASE)

# Przyimki i skróty, po których liczba jest miejscem, a nie kwotą.
_PRZED_LICZBA_TO_KOD = re.compile(
    r"(z|ze|do|w|we|na|pod|spod|od|aus|from|nach|kod|kodu|zip|plz)[\s:.]*$", re.IGNORECASE)

# Nazwa miejscowości obok kodu: słowo z wielkiej litery, min. 3 znaki.
_NAZWA_OBOK = re.compile(r"(?:^|\s)([A-ZÄÖÜŚŻŹĆŃŁÓĄĘ][\wäöüßśżźćńłóąę-]{2,})")


def _dlugosc_ciagu_cyfr(tekst: str, start: int, koniec: int) -> int:
    """Ile cyfr ma CAŁY ciąg, którego częścią jest dopasowanie.

    Rozszerzamy zakres w obie strony po cyfrach i pojedynczych separatorach
    (spacja, myślnik, kropka), bo tak ludzie zapisują numery telefonów:
    "502 33 44 55", "+48 505-606-707". Dziewięć cyfr albo więcej w jednym
    ciągu to numer, nie kod — i to jest jedyny sposób, żeby odróżnić czeskie
    "110 00" od kawałka polskiej komórki, bo OBA mają kształt trzy-spacja-dwie.
    """
    lewy = start
    while lewy > 0 and (tekst[lewy - 1].isdigit()
                        or (tekst[lewy - 1] in " -." and lewy >= 2 and tekst[lewy - 2].isdigit())):
        lewy -= 1
    prawy = koniec
    while prawy < len(tekst) and (tekst[prawy].isdigit()
                                  or (tekst[prawy] in " -."
                                      and prawy + 1 < len(tekst) and tekst[prawy + 1].isdigit())):
        prawy += 1
    return sum(1 for c in tekst[lewy:prawy] if c.isdigit())


def _wykluczone(tekst: str, start: int, koniec: int, koniec_cyfr: int) -> bool:
    """Twarde wykluczenia — sprawdzane dla KAŻDEGO wzorca, także bezkontekstowego.

    Trzy sytuacje, w których ciąg cyfr na pewno nie jest kodem:
      • jednostka albo waluta zaraz po cyfrach ("2500 zł", "180 km");
      • słowo ceny/telefonu przed nimi ("cena 2500", "tel 502");
      • cyfry są fragmentem dłuższego numeru (dziewięć cyfr i więcej).

    `koniec_cyfr` to koniec SAMYCH CYFR, nie całego dopasowania — inaczej
    holenderskie "cztery cyfry + dwie litery" zjadłoby "2500 zl" i sprawdzało
    walutę dopiero za nią.
    """
    przed = tekst[max(0, start - 30):start]
    if _PO_LICZBIE_NIE_KOD.match(tekst[koniec_cyfr:koniec_cyfr + 30]):
        return True
    if _PRZED_LICZBA_NIE_KOD.search(przed):
        return True
    return _dlugosc_ciagu_cyfr(tekst, start, koniec) >= 9


def _kontekst_wskazuje_kod(tekst: str, start: int, koniec: int) -> bool:
    """Czy ciąg cyfr w [start, koniec) da się uznać za kod, a nie za liczbę.

    Trzy niezależne przesłanki, wystarczy jedna:
      • przyimek albo skrót kodu bezpośrednio przed ("z 50667", "PLZ 50667");
      • nazwa miejscowości z wielkiej litery tuż obok ("50667 Köln", "Köln 50667");
      • słowo wskazujące kraj w oknie.
    Wykluczenia (`_wykluczone`) są sprawdzane WCZEŚNIEJ i osobno, bo "2500 zł"
    ma obok siebie i przyimek, i nazwę własną, a kodem nie jest.
    """
    przed = tekst[max(0, start - 30):start]
    po = tekst[koniec:koniec + 30]

    if _PRZED_LICZBA_TO_KOD.search(przed):
        return True
    if _NAZWA_OBOK.search(po[:20]) or _NAZWA_OBOK.search(" " + przed[-20:]):
        return True

    okno = normalizuj_nazwe(przed + " " + po)
    return any(s.strip() in okno for sygnaly in _SYGNALY_KRAJU.values() for s in sygnaly)


def _kraj_z_kontekstu(tekst: str, start: int, koniec: int,
                      dozwolone: tuple[str, ...]) -> str | None:
    """Który z `dozwolone` krajów wskazuje otoczenie kodu. None = żaden."""
    okno = normalizuj_nazwe(tekst[max(0, start - 40):koniec + 40])
    for kraj in dozwolone:
        if any(s.strip() in okno for s in _SYGNALY_KRAJU.get(kraj, ())):
            return kraj
    return None


def znajdz_kody(tekst: str) -> list[tuple[str, str]]:
    """Wyłuskaj kody pocztowe z surowej treści posta. Zwraca [(kod, kraj)].

    `kraj` to dwuliterowy kod albo "?", gdy formatu nie da się przypisać
    jednoznacznie (czeski i słowacki wyglądają tak samo, austriacki i belgijski
    też). "?" jest wartością POPRAWNĄ i celową: lepiej oddać kod do
    rozstrzygnięcia geokoderowi, niż pominąć go, bo nie wiadomo, z którego
    kraju pochodzi.

    Kolejność wzorców ma znaczenie i jest od NAJBARDZIEJ do NAJMNIEJ
    charakterystycznego. Pięciocyfrowy niemiecki nie może zjeść fragmentu
    czterocyfrowego z literami (NL), a czterocyfrowy nie może zjeść kawałka
    pięciocyfrowego — dlatego zajęte zakresy znaków są skreślane z dalszych prób.
    """
    tekst = tekst or ""
    zajete: list[tuple[int, int]] = []
    wynik: list[tuple[str, str]] = []
    widziane: set[tuple[str, str]] = set()

    for nazwa, wzorzec, domyslny_kraj, wymaga_kontekstu in _WZORCE:
        for m in wzorzec.finditer(tekst):
            start, koniec = m.span()
            if any(s < koniec and start < k for s, k in zajete):
                continue
            # Koniec samych cyfr — dla NL dopasowanie sięga za litery, a walutę
            # trzeba sprawdzić TUŻ ZA LICZBĄ, inaczej "2500 zl" przechodzi jako
            # holenderski kod "2500 ZL".
            koniec_cyfr = start + len(re.match(r"[0-9 .-]*", m.group(0)).group(0).rstrip(" .-"))
            if _wykluczone(tekst, start, koniec, koniec_cyfr):
                continue
            if wymaga_kontekstu and not _kontekst_wskazuje_kod(tekst, start, koniec):
                continue

            if nazwa == "CZ_SK":
                kraj = _kraj_z_kontekstu(tekst, start, koniec, ("CZ", "SK")) or "?"
            elif nazwa == "AT_BE":
                kraj = _kraj_z_kontekstu(tekst, start, koniec, ("AT", "BE")) or "?"
            elif nazwa == "DE":
                # Pięciocyfrowy jest niemiecki, chyba że kontekst mówi inaczej
                # (np. francuski i włoski też mają pięć cyfr).
                kraj = _kraj_z_kontekstu(tekst, start, koniec, ("FR", "IT", "DE")) or domyslny_kraj
            else:
                kraj = domyslny_kraj

            kod = re.sub(r"\s+", " ", m.group(0)).strip().upper()
            zajete.append((start, koniec))
            if (kod, kraj) not in widziane:
                widziane.add((kod, kraj))
                wynik.append((kod, kraj))
    return wynik


# ---------------------------------------------------------------------------
# ZŁOŻENIE DLA WARSTWY WYŻEJ
# ---------------------------------------------------------------------------
def podsumowanie(odbior: Punkt | None, dostawa: Punkt | None) -> dict:
    """Komplet liczb i linków dla jednego zlecenia — to, co idzie na ekran.

    `km_trasy` (odbiór->dostawa) jest liczbą PIERWSZĄ, bo to ona mówi, ile
    realnie trzeba przejechać z autem na lawecie. `km_od_bazy` idzie obok, jako
    druga i pomocnicza — przy transporcie międzynarodowym sama nic nie znaczy.
    Żadna z nich niczego nie filtruje.
    """
    b = baza()
    km_od_bazy = dystans_km(b, odbior) if odbior else None
    km_trasy = dystans_km(odbior, dostawa) if (odbior and dostawa) else None
    # Szacunek liczymy z DŁUGOŚCI KURSU, a gdy znamy tylko jeden punkt — z drogi
    # od bazy. `km_trasy` w wyniku zostaje wtedy None, bo jego brak jest
    # informacją: nie wiemy, dokąd auto ma jechać.
    podstawa = km_trasy if km_trasy is not None else (km_od_bazy or 0.0)
    return {
        "km_trasy": km_trasy,
        "km_od_bazy": km_od_bazy,
        "szacunek_pln": kalkulacja(podstawa)["szacunek_pln"],
        "niepewne": [p.nazwa for p in (odbior, dostawa) if p and p.niepewny],
        "link_trasa": link_do_map(b, odbior, dostawa),
        "link_nawigacja": link_do_nawigacji(odbior) if odbior else "",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    import argparse  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    import sys  # noqa: PLC0415

    ap = argparse.ArgumentParser(
        description="Geokodowanie, dystans i linki do map — bez sieci.")
    ap.add_argument("miejsca", nargs="*", help="odbiór [dostawa] — nazwa miasta albo kod")
    ap.add_argument("--kody", metavar="TEKST",
                    help="wyłuskaj kody pocztowe z surowego tekstu i zakończ")
    args = ap.parse_args(argv[1:])

    print(stan_bazy(), file=sys.stderr)

    if args.kody:
        znalezione = znajdz_kody(args.kody)
        print(f"KODY: {znalezione or '(brak)'}")
        for kod, kraj in znalezione:
            p = geokoduj(kod, None)
            print(f"  {kod} [{kraj}] -> {p.nazwa if p else 'brak w bazie'}")
        return 0

    if not args.miejsca:
        ap.print_help()
        return 0

    def jako_punkt(s: str) -> Punkt | None:
        # Wygląda jak kod? Spróbuj kodem, inaczej nazwą.
        return geokoduj(s, None) if re.search(r"[0-9]", s) else geokoduj(None, s)

    odbior = jako_punkt(args.miejsca[0])
    dostawa = jako_punkt(args.miejsca[1]) if len(args.miejsca) > 1 else None
    for etykieta, p in (("ODBIÓR", odbior), ("DOSTAWA", dostawa)):
        if p is None:
            print(f"{etykieta}:  brak dopasowania (null — świadomie nie zgadujemy)")
        else:
            print(f"{etykieta}:  {p.nazwa}  [{p.zrodlo}]  {p.wspolrzedne()}"
                  + ("   <-- LOKALIZACJA ZGADYWANA" if p.niepewny else ""))
    print()
    print(_json.dumps(podsumowanie(odbior, dostawa), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
