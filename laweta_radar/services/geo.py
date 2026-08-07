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

DYSTANS I WYCENA TYLKO Z DWÓCH ZNANYCH PUNKTÓW. Gdy którykolwiek koniec trasy
jest nierozpoznany, `km_trasy` i `szacunek_pln` są NULL-em — i tak zostaje na
ekranie („trasa nieustalona"), zamiast liczby wziętej z czegokolwiek innego.
BAZA OPERATORA NIE ZASTĘPUJE BRAKUJĄCEGO PUNKTU: post „transport z Dębicy do
Turku, trasa ma około 490 km" z nierozpoznanym Turkiem pokazywał „60 km,
~250 zł" — czyli dojazd Krosno->Dębica podstawiony pod długość kursu. Kierowca
odrzuca wtedy kurs na 490 km, patrząc na wycenę lokalnego skoku. Zła liczba
jest gorsza niż jej brak: brak widać, złej liczby nie.

`zrodlo` W KAŻDYM PUNKCIE JEST CZĘŚCIĄ PRODUKTU, nie diagnostyką. Wartość
"miasto_niepewne" znaczy „w Polsce jest kilkanaście miejscowości o tej nazwie
i wybraliśmy największą". To MUSI trafić do interfejsu: operator ma zobaczyć,
że lokalizacja jest zgadywana, ZANIM pojedzie 60 km.

ZERO WYWOŁAŃ SIECIOWYCH. Moduł czyta jeden plik CSV przy pierwszym użyciu
i dalej działa z pamięci. Bazę pobiera osobno `scripts/pobierz_geo.py`.

CLI:
    python -m laweta_radar.services.geo "Krosno" "Rzeszow"
    python -m laweta_radar.services.geo --kody "auto stoi w 50667 Koln, cena 2500 zl"
    python -m laweta_radar.services.geo "Debica" "Turek" --tresc "trasa ma okolo 490 km"
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

# Kraj operatora. Stałą, nie polem w .env: cały ten system (stawki w PLN,
# domyślna baza w Krośnie, treść promptu) zakłada polskiego przewoźnika — gdyby
# to się zmieniło, zmieniłoby się dużo więcej niż jedna literówka w kodzie kraju.
KRAJ_BAZY = "PL"


@dataclass(frozen=True)
class Punkt:
    """Miejsce na mapie wraz z informacją, SKĄD je znamy.

    `zrodlo`:
      "kod"               — dokładne dopasowanie kodu pocztowego, najpewniejsze;
      "miasto"            — nazwa miasta jednoznaczna w bazie (także wtedy, gdy
                            jednoznaczność dał kraj wyczytany z treści posta);
      "miasto_niepewne"   — nazwa niejednoznaczna, wzięliśmy największą
                            miejscowość o tej nazwie. POKAŻ TO OPERATOROWI;
      "miasto_odmienione" — nazwa z posta była formą odmienioną („Kielc",
                            „Katowic") i dopasowała się dopiero prefiksem.
                            Traktowana jak "miasto_niepewne";
      "baza"              — punkt operatora z .env.

    `kraj`: dwuliterowy kod kraju z bazy `kody_eu.csv` (kolumna już tam jest —
    to odczyt, nie nowa logika). `None` TYLKO gdy punkt powstał bez tej kolumny
    (stare fixtury testów bez pola `kraj`); przy realnym dopasowaniu z bazy
    kraj jest ZAWSZE znany, bo to on rozstrzyga między kolizjami kodów i nazw
    (patrz `geokoduj`). Warstwa wyżej (`kierunek_geo`) czyta to pole wprost,
    zamiast zgadywać kraj po nazwie miasta.
    """

    lat: float
    lng: float
    zrodlo: str
    nazwa: str
    kraj: str | None = None

    @property
    def niepewny(self) -> bool:
        """Skrót dla warstwy wyżej: czy przy tym punkcie postawić ostrzeżenie."""
        return self.zrodlo.endswith("_niepewne") or self.zrodlo == "miasto_odmienione"

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
    # Nawiasy tak samo: GeoNames pisze "Frankfurt (Oder)", post — "Frankfurt Oder".
    s = re.sub(r"[-–—./()]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# EGZONIMY — polskie nazwy miast zagranicznych.
#
# GeoNames zna "Köln", a post mówi "kupiłem auto w Kolonii". Bez tej tabeli
# najlepszy typ zlecenia, jaki ten system ma znajdować (transport z zagranicy
# zestawem B+E), nie geokoduje się w ogóle. Tabela jest krótka celowo: tylko
# kierunki, które realnie pojawiają się w tych grupach.
#
# TO NIE JEST DUBLOWANIE INSTRUKCJI DLA MODELU, tylko siatka pod nią. Prompt
# klasyfikatora (przez `gate.INSTRUKCJA_JEZYKOWA_DLA_KLASYFIKATORA`) każe
# zostawiać nazwy miejscowości W ORYGINALE właśnie dlatego, że idą wprost tutaj.
# Ale post polski pisze „Kolonia" i model, który wiernie przepisze to z treści,
# ma rację — a wtedy dopasowanie zależy już tylko od tej tabeli.
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
# BAZA KODÓW I MIEJSCOWOŚCI
#
# Plik CSV: kraj,kod,miejscowosc,wojewodztwo,lat,lng[,populacja] — jeden format
# dla wszystkich krajów, ale DWA rodzaje wierszy:
#
#   • wiersz Z KODEM — z eksportu kodów pocztowych GeoNames. Obsługuje
#     wyszukiwanie PO KODZIE. W niektórych krajach (DE) pole `miejscowosc`
#     bywa nazwą instytucji („Agentur fuer Arbeit Dortmund" — Grosskunden-PLZ),
#     dlatego wyszukiwaniu po nazwie ten wiersz służy tylko AWARYJNIE;
#   • wiersz BEZ KODU, z populacją — z dumpu miejscowości GeoNames
#     (feature_class='P'). Obsługuje wyszukiwanie PO NAZWIE; populacja
#     rozstrzyga wybór między miastami o tej samej nazwie.
#
# Starsze bazy (zalążek w repo, fixtury testów) nie mają kolumny `populacja`
# ani wierszy miejscowości — wtedy po nazwie szukamy jak dawniej, po wierszach
# kodowych. Wczytywany LENIWIE, przy pierwszym pytaniu: import modułu ma być
# darmowy, żeby dało się go zaimportować w teście, który geokodowania w ogóle
# nie dotyka.
# ---------------------------------------------------------------------------
_KATALOG_REPO = Path(__file__).resolve().parent.parent.parent
SCIEZKI_BAZY = (
    _KATALOG_REPO / "data" / "kody_eu.csv",
    _KATALOG_REPO / "data" / "kody_pl.csv",
)

_indeks_kodow: dict[str, list[dict]] | None = None
_indeks_miast: dict[str, list[dict]] | None = None
_zaladowana_sciezka: Path | None = None


# Formaty kodu w postaci SAMODZIELNEJ — do sprawdzenia wartości, którą ktoś już
# wyodrębnił (np. model w klasyfikatorze). To NIE są wzorce do skanowania tekstu
# — tamte (`_WZORCE`) mają lookaroundy i wymagają kontekstu, bo w zdaniu „moge
# dac 2500 zl" cztery cyfry kodem nie są. Tutaj kontekstu nie ma i nie może być.
#
# Lista pokrywa dokładnie te kraje, które umie rozwiązać ta baza — bo jedynym
# sensownym kryterium „czy to jest kod" jest „czy da się z tego zrobić punkt".
FORMATY_KODU: dict[str, re.Pattern[str]] = {
    "PL": re.compile(r"^[0-9]{2}-[0-9]{3}$"),
    "DE_FR_IT": re.compile(r"^[0-9]{5}$"),
    "CZ_SK": re.compile(r"^[0-9]{3} ?[0-9]{2}$"),
    "NL": re.compile(r"^[0-9]{4} ?[A-Z]{2}$"),
    "AT_BE": re.compile(r"^[0-9]{4}$"),
}


def czy_kod_pocztowy(kod: str | None) -> bool:
    """Czy ten ciąg ma kształt kodu pocztowego w którymś z obsługiwanych krajów.

    Używa tego klasyfikator do walidacji pola, które oddał model. Format
    mieszka TUTAJ, bo to ten moduł musi z kodu zrobić współrzędne — druga lista
    wzorców w klasyfikatorze rozjechałaby się przy dołożeniu kolejnego kraju
    i objawiła jako cicho wyrzucane kody, których geokoder by nie zobaczył.
    """
    s = (kod or "").strip().upper()
    if not s:
        return False
    return any(w.match(s) for w in FORMATY_KODU.values())


def normalizuj_kod(kod: str | None) -> str:
    """Kod do postaci indeksowanej: wielkie litery, bez spacji i myślników.

    "110 00", "11000" i "110-00" to ten sam czeski kod; "38-400" i "38400" to
    ten sam polski. Indeksujemy formę bez separatorów, a wyświetlamy oryginalną.

    PUBLICZNE, bo pyta o to także klasyfikator: fallback regexowy musi wiedzieć,
    czy kod znaleziony w treści to TEN SAM kod, który oddał model — inaczej
    "38-400" od nas i "38400" od modelu wyglądają jak dwa różne miejsca i drugie
    z nich ląduje w polu dostawy. Druga implementacja tej normalizacji po stronie
    klasyfikatora rozjechałaby się przy pierwszym dołożonym formacie.
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
            # Populacja jest opcjonalna (starsze bazy jej nie mają), a śmieć
            # w kolumnie znaczy „nie wiemy", nie „zero mieszkańców".
            populacja_surowa = (wiersz.get("populacja") or "").strip()
            try:
                populacja = int(populacja_surowa) if populacja_surowa else None
            except ValueError:
                populacja = None
            rekord = {
                "kraj": (wiersz.get("kraj") or "PL").strip().upper(),
                "kod": (wiersz.get("kod") or "").strip(),
                "miejscowosc": (wiersz.get("miejscowosc") or "").strip(),
                "wojewodztwo": (wiersz.get("wojewodztwo") or "").strip(),
                "lat": lat,
                "lng": lng,
                "populacja": populacja,
            }
            if rekord["kod"]:
                po_kodzie.setdefault(normalizuj_kod(rekord["kod"]), []).append(rekord)
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


# Dopasowanie prefiksowe form odmienionych: nazwa z posta jest POCZĄTKIEM nazwy
# z bazy („Kielc" -> „Kielce"). Minimum 5 znaków, żeby „Nowa" nie łapało
# „Nowaczyzny"; najwyżej 3 znaki różnicy, bo dopełniacz obcina końcówkę,
# a nie pół słowa.
PREFIKS_MIN_ZNAKOW = 5
PREFIKS_MAX_OBCIECIE = 3

# Przewaga populacji, od której „największe miasto o tej nazwie" przestaje być
# zgadywaniem: zwycięzca musi być co najmniej TYLE razy większy od drugiego.
# Frankfurt am Main vs Frankfurt (Oder) to 13x — przechodzi. Dwie porównywalne
# wsie -> None, bo wybór między nimi byłby rzutem monetą z współrzędnymi.
PRZEWAGA_POPULACJI = 3.0

# Nazwy KRAJÓW w treści posta — rdzenie, dopasowywane od początku słowa, jak
# w `_SYGNALY_KRAJU` (odmienia się końcówka: „Niemcy", „w Niemczech",
# „z Niemiec"). CELOWO OSOBNA TABELA, nie `_SYGNALY_KRAJU`: tamta zawiera też
# nazwy MIAST („koln", „berlin"), które działają w wąskim oknie wokół kodu,
# ale na całej treści robiłyby szkody — „z Berlina do Nowej Wsi" wskazywałoby
# Niemcy przy rozstrzyganiu polskiej wsi, bo miasto z JEDNEGO końca trasy
# mówiłoby o kraju DRUGIEGO.
_NAZWY_KRAJOW: dict[str, tuple[str, ...]] = {
    "DE": ("niemc", "niemiec", "deutschland", "germany"),
    "CZ": ("czech", "czesk", "tschechien"),
    "SK": ("slowac", "slovensk", "slowakei"),
    "NL": ("holand", "holender", "niderland", "nederland"),
    "AT": ("austri", "osterreich"),
    "BE": ("belgi", "belgien"),
    "FR": ("francj", "francus", "france", "frankreich"),
    "IT": ("wloch", "wlosz", "italia", "italien"),
    "PL": ("polsk", "polsc", "polen"),
}


def _dopasuj_prefiksem(nazwa: str, po_nazwie: dict) -> str | None:
    """Nazwa z bazy, której `nazwa` jest początkiem. Wieloznaczność -> None.

    „Wieloznaczność" znaczy: prefiks pasuje do WIELU RÓŻNYCH nazw z bazy
    („kozie" -> „Kozienice" i „Koziegłowy"). Wtedy None, jak przy każdym innym
    zgadywaniu. To, że JEDNA dopasowana nazwa oznacza kilka miejscowości,
    rozstrzyga się dalej, tą samą drogą co przy dopasowaniu dosłownym.
    """
    if len(nazwa) < PREFIKS_MIN_ZNAKOW:
        return None
    trafienia = [n for n in po_nazwie
                 if n.startswith(nazwa)
                 and 0 < len(n) - len(nazwa) <= PREFIKS_MAX_OBCIECIE]
    return trafienia[0] if len(trafienia) == 1 else None


def _warianty(rekordy: list[dict]) -> list[dict]:
    """Wiersze o jednej nazwie -> lista kandydatów „ta nazwa, różne miejsca".

    Gdy nazwa ma wiersze z dumpu miejscowości (bez kodu, z populacją), to ONE
    są kandydatami — wiersz kodowy bywa nazwą instytucji, nie miasta (patrz
    komentarz przy `SCIEZKI_BAZY`). Wiersze kodowe dokładają wtedy tylko
    liczbę kodów regionu, jako zapasowe proxy wielkości. Bez wierszy
    miejscowości (zalążek, stare bazy) kandydatów budujemy jak dawniej:
    grupując wiersze kodowe po regionie, ze środkiem grupy jako współrzędną.
    """
    liczba_kodow: dict[tuple[str, str], int] = {}
    for r in rekordy:
        if r["kod"]:
            klucz = (r["kraj"], r["wojewodztwo"])
            liczba_kodow[klucz] = liczba_kodow.get(klucz, 0) + 1

    miejsca = [r for r in rekordy if not r["kod"]]
    if miejsca:
        return [{
            "kraj": r["kraj"],
            "wojewodztwo": r["wojewodztwo"],
            "miejscowosc": r["miejscowosc"],
            "lat": r["lat"],
            "lng": r["lng"],
            "populacja": r["populacja"] or 0,
            "liczba_kodow": liczba_kodow.get((r["kraj"], r["wojewodztwo"]), 0),
        } for r in miejsca]

    grupy: dict[tuple[str, str], list[dict]] = {}
    for r in rekordy:
        grupy.setdefault((r["kraj"], r["wojewodztwo"]), []).append(r)
    wynik = []
    for (kraj, wojewodztwo), grupa in grupy.items():
        lat, lng = _srodek(grupa)
        wynik.append({
            "kraj": kraj,
            "wojewodztwo": wojewodztwo,
            "miejscowosc": grupa[0]["miejscowosc"],
            "lat": lat,
            "lng": lng,
            "populacja": 0,
            "liczba_kodow": len(grupa),
        })
    return wynik


def _kraje_z_kodow(tresc: str | None, po_kodzie: dict) -> set[str]:
    """Kraje wskazane przez kody pocztowe stojące w treści posta.

    Kod znany bazie mówi o kraju WIĘCEJ niż jego kształt — „39400" wygląda
    na niemiecki, a jest też polskim 39-400 zapisanym bez myślnika. Dlatego
    dla kodu obecnego w bazie bierzemy wszystkie kraje jego wystąpień,
    a kształtem posiłkujemy się tylko przy kodzie, którego baza nie zna.
    """
    kraje: set[str] = set()
    for kod, kraj in znajdz_kody(tresc or ""):
        w_bazie = {r["kraj"] for r in po_kodzie.get(normalizuj_kod(kod), ())}
        if w_bazie:
            kraje.update(w_bazie)
        elif kraj != "?":
            kraje.add(kraj)
    return kraje


def _kraje_z_nazw(tresc: str | None) -> set[str]:
    """Kraje wymienione w treści posta z nazwy („Niemcy", „Deutschland")."""
    okno = normalizuj_nazwe(tresc or "")
    if not okno:
        return set()
    return {kraj for kraj, rdzenie in _NAZWY_KRAJOW.items()
            if any(_sygnal_w_oknie(rdzen, okno) for rdzen in rdzenie)}


def _przefiltruj_krajami(warianty: list[dict], kraje: set[str]) -> list[dict]:
    """Zostaw warianty ze wskazanych krajów. Filtr kasujący wszystko — ignoruj:
    kod czy kraj z treści może dotyczyć DRUGIEGO końca trasy i wtedy o tym
    mieście nie mówi nic, a nie „nie istnieje"."""
    if not kraje:
        return warianty
    pasujace = [w for w in warianty if w["kraj"] in kraje]
    return pasujace or warianty


def _wybierz_wariant(warianty: list[dict]) -> dict | None:
    """Który z wariantów o tej samej nazwie bierzemy. None = nie rozstrzygamy.

    Populacja rozstrzyga tylko przy WYRAŹNEJ przewadze (`PRZEWAGA_POPULACJI`):
    Frankfurt am Main wygrywa z Frankfurtem nad Odrą, ale dwie porównywalne
    miejscowości to None — zła współrzędna wysyła człowieka setki kilometrów
    w złą stronę i wygląda przy tym dokładnie tak samo jak trafiona.

    Bez danych o populacji (zalążek bazy, stare fixtury) zostaje dawne proxy:
    liczba kodów pocztowych regionu. Ono nie zgłasza remisów — wybiera zawsze,
    a niepewność niesie `zrodlo="miasto_niepewne"` ustawiane wyżej. Zmiana
    tego na None wyłączałaby geokodowanie wsi do czasu odświeżenia bazy.
    """
    if len(warianty) == 1:
        return warianty[0]
    po_populacji = sorted(warianty, key=lambda w: w["populacja"], reverse=True)
    najlepszy, drugi = po_populacji[0], po_populacji[1]
    if najlepszy["populacja"] > 0:
        if najlepszy["populacja"] >= PRZEWAGA_POPULACJI * max(drugi["populacja"], 1):
            return najlepszy
        return None
    return max(warianty, key=lambda w: w["liczba_kodow"])


def geokoduj(kod: str | None, miasto: str | None,
             tresc: str | None = None) -> Punkt | None:
    """Kod pocztowy i/lub nazwa miasta -> Punkt. Brak dopasowania -> None.

    `tresc` to PEŁNA treść posta — opcjonalna, ale bez niej nie da się
    rozstrzygnąć, o który kraj chodzi przy nazwie występującej w kilku krajach.
    Produkcyjny przypadek: „Miejscowosc Lahnstein 56112 Niemcy do 39-400
    Tarnobrzeg" dostawał Lahnstein w Austrii, 782 km od właściwego — kod 56112
    STAŁ w treści i wskazywał Niemcy jednoznacznie, tylko nikt go nie zapytał.

    Kolejność prób jest kolejnością PEWNOŚCI:
      1. kod pocztowy — dokładne dopasowanie, `zrodlo="kod"`;
      2. nazwa miasta — po normalizacji; forma odmieniona („Kielc", „Katowic")
         łapie się prefiksem i wychodzi jako `zrodlo="miasto_odmienione"`.
         Przy nazwie z wielu krajów kolejność rozstrzygania:
           a) kraj z kodu pocztowego stojącego w treści posta,
           b) kraj z nazwy kraju w treści („Niemcy", „Deutschland", „Czechy"),
           c) największa populacja — wynik oznaczony `zrodlo="miasto_niepewne"`,
           d) populacje porównywalne -> None;
      3. brak -> None. BEZ ZGADYWANIA: null jest lepszy niż zła współrzędna,
         bo zła współrzędna wyśle człowieka 80 km w złą stronę.
    """
    po_kodzie, po_nazwie = _indeksy()

    # --- 1. kod pocztowy ---
    klucz = normalizuj_kod(kod)
    if klucz and klucz in po_kodzie:
        trafienia = po_kodzie[klucz]
        # KOLIZJE MIĘDZY KRAJAMI SĄ REALNE, nie teoretyczne: "39200" to polska
        # Dębica zapisana bez myślnika ORAZ niemiecki kod spod Magdeburga,
        # a różnica to 700 km. Rozstrzygamy w trzech krokach, od najpewniejszego:
        #
        #   1. nazwą miasta, jeśli przyszła razem z kodem — to jedyna informacja,
        #      która naprawdę wie, o który kraj chodzi;
        #   2. PL, bo tu jesteśmy i taka jest większość postów;
        #   3. pierwszym trafieniem.
        #
        # Krok 2 bywa zły i nie da się tego uniknąć bez wiedzy, której nie mamy
        # — dlatego kraj ZAWSZE wchodzi do `nazwa` i widzi go operator, zanim
        # wsiądzie do auta.
        szukana = normalizuj_nazwe(miasto or "")
        rekord = (next((r for r in trafienia
                        if szukana and normalizuj_nazwe(r["miejscowosc"]) == szukana), None)
                  or next((r for r in trafienia if r["kraj"] == "PL"), None)
                  or trafienia[0])
        return Punkt(
            lat=rekord["lat"],
            lng=rekord["lng"],
            zrodlo="kod",
            nazwa=f"{rekord['miejscowosc']} {rekord['kod']} ({rekord['kraj']})".strip(),
            kraj=rekord["kraj"],
        )

    # --- 2. nazwa miasta ---
    nazwa = normalizuj_nazwe(miasto or "")
    nazwa = EGZONIMY.get(nazwa, nazwa)
    if not nazwa:
        return None
    odmienione = False
    if nazwa not in po_nazwie:
        # Polski dopełniacz OBCINA końcówkę („do Kielc", „z Katowic"), a model
        # przepisuje to, co stoi w tekście — więc nazwa z posta bywa POCZĄTKIEM
        # nazwy z bazy. Prompt każe pisać w mianowniku, ale instrukcja nie jest
        # kontrolą: to dopasowanie działa także wtedy, gdy model jej nie posłucha.
        nazwa = _dopasuj_prefiksem(nazwa, po_nazwie)
        if nazwa is None:
            return None
        odmienione = True

    warianty = _warianty(po_nazwie[nazwa])
    if len(warianty) > 1:
        # Treść posta zawęża warianty krajem — najpierw kodem pocztowym (a),
        # bo to najtwardszy sygnał, potem nazwą kraju (b). Filtr, który
        # skasowałby WSZYSTKIE warianty, jest ignorowany: kod z drugiego końca
        # trasy nie unieważnia miasta, tylko o nim nic nie mówi.
        for kraje in (_kraje_z_kodow(tresc, po_kodzie), _kraje_z_nazw(tresc)):
            if len(warianty) == 1:
                break
            warianty = _przefiltruj_krajami(warianty, kraje)

    wybrany = _wybierz_wariant(warianty)
    if wybrany is None:
        return None
    jednoznaczne = len(warianty) == 1
    if odmienione:
        zrodlo = "miasto_odmienione"
    else:
        zrodlo = "miasto" if jednoznaczne else "miasto_niepewne"
    opis_regionu = f", {wybrany['wojewodztwo']}" if wybrany["wojewodztwo"] else ""
    return Punkt(
        lat=wybrany["lat"],
        lng=wybrany["lng"],
        zrodlo=zrodlo,
        nazwa=(f"{wybrany['miejscowosc']}{opis_regionu} ({wybrany['kraj']})"
               + ("" if jednoznaczne
                  else f" — {len(warianty)} miejscowości o tej nazwie")),
        kraj=wybrany["kraj"],
    )


def baza() -> Punkt:
    """Punkt operatora z .env. Bez konfiguracji — Krosno, i mówimy o tym w nazwie."""
    if settings.BAZA_LAT and settings.BAZA_LNG:
        return Punkt(settings.BAZA_LAT, settings.BAZA_LNG, "baza", settings.BAZA_NAZWA,
                     kraj=KRAJ_BAZY)
    return Punkt(KROSNO[0], KROSNO[1], "baza",
                f"{settings.BAZA_NAZWA} (domyślne — ustaw BAZA_LAT/BAZA_LNG)",
                kraj=KRAJ_BAZY)


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
def kalkulacja(km: float | None) -> dict:
    """Prosty szacunek ze stawek z .env. `km=None` -> obie wartości NULL.

    TO JEST ETYKIETA NA EKRANIE, NIE BRAMKA. Nic się na jej podstawie nie
    ukrywa ani nie odrzuca — kierowca patrzy na liczbę i sam decyduje, czy
    kurs mu się opłaca. Zna swój sprzęt, swój kalendarz i swoją cenę lepiej
    niż ten wzór.

    NIEZNANY DYSTANS NIE MA CENY i ta reguła mieszka TUTAJ, a nie tylko
    u wołającego. Wcześniej `None` wchodziło przez `km or 0.0` w stawkę
    minimalną, więc zlecenie bez trasy dostawało „~250 zł" — liczbę, która
    wygląda jak wyliczenie, a jest wartością domyślną. Warstwa wyżej ma
    prawo nie mieć dystansu; nie ma prawa dostać za niego ceny.
    """
    if km is None:
        return {"km_trasy": None, "szacunek_pln": None}
    km = max(0.0, float(km))
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
# Rdzeń dopasowujemy od POCZĄTKU SŁOWA (`_sygnal_w_oknie`), bo odmienia się
# końcówka, nie początek.
#
# SYGNAŁY ZE SPACJAMI (" de ", " cz ", " sk ") to skróty krajów i znaczą coś
# WYŁĄCZNIE jako całe słowo. To nie jest kosmetyka zapisu: "sk" wyszukiwane jako
# fragment siedzi w „Skodzie", a "cz" w „częściach" — czyli w dwóch
# najczęstszych słowach w tych grupach.
_SYGNALY_KRAJU: dict[str, tuple[str, ...]] = {
    "DE": ("niemc", "niemiec", "niemczech", "niemieck", "deutschland", "aus ", " de ", "germany",
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

# Przedział, w którym czterocyfrowa liczba jest w tych postach rocznikiem auta
# częściej niż kodem. Co z tego wynika i dlaczego nie wykluczamy jej twardo —
# patrz `_wykluczone`. Słowo „rocznik" obok liczby łapie osobno
# `_PRZED_LICZBA_NIE_KOD`, ale w realnym poście prawie nigdy nie pada.
_ROCZNIKI = range(1950, 2036)

# Granica słowa w oknie wokół kodu. Okno jest już po `normalizuj_nazwe`, więc
# zostają w nim tylko małe litery bez ogonków, cyfry i interpunkcja.
_NIE_ALFANUM = re.compile(r"[^0-9a-z]+")


def _czy_rocznik(dopasowanie: str) -> bool:
    return dopasowanie.isdigit() and len(dopasowanie) == 4 and int(dopasowanie) in _ROCZNIKI


def _sygnal_w_oknie(sygnal: str, okno: str) -> bool:
    """Czy sygnał kraju pada w oknie (już znormalizowanym) wokół kodu.

    Dwie klasy sygnałów, dwie różne reguły — patrz komentarz przy
    `_SYGNALY_KRAJU`:
      • otoczony spacjami (" sk ") to skrót kraju i liczy się TYLKO jako całe
        słowo, inaczej „Skoda" robi ze Słowacji sygnał kraju;
      • bez spacji ("wiedn", "bratislav") to rdzeń, który ma trafiać w odmienione
        formy — dopasowujemy go od POCZĄTKU słowa, bo odmienia się końcówka.
    """
    rdzen = sygnal.strip()
    # Dzielimy po ZNAKACH NIEALFANUMERYCZNYCH, nie po spacjach: „SK," na końcu
    # zdania to nadal skrót kraju, a przy podziale po spacjach zostaje z przecinkiem
    # i nie pasuje do niczego.
    slowa = [s for s in _NIE_ALFANUM.split(okno) if s]
    if sygnal != rdzen:
        return rdzen in slowa
    return any(slowo.startswith(rdzen) for slowo in slowa)


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


def _mocny_sygnal_kodu(tekst: str, start: int, koniec: int) -> bool:
    """Przesłanki NIEZALEŻNE od nazwy własnej stojącej obok liczby.

    Osobne od reszty kontekstu, bo w tych postach obok liczby równie często stoi
    marka auta („Skoda Octavia 2012"), co miejscowość („Köln 50667") — a jedno
    i drugie jest słowem z wielkiej litery. Tam, gdzie pomyłka jest realna
    (rocznik), żądamy właśnie tych mocniejszych przesłanek:
      • przyimek albo skrót kodu bezpośrednio przed liczbą („z 50667", „PLZ 50667");
      • słowo wskazujące kraj w oknie wokół niej.
    """
    przed = tekst[max(0, start - 30):start]
    po = tekst[koniec:koniec + 30]
    if _PRZED_LICZBA_TO_KOD.search(przed):
        return True
    okno = normalizuj_nazwe(przed + " " + po)
    return any(_sygnal_w_oknie(s, okno) for sygnaly in _SYGNALY_KRAJU.values() for s in sygnaly)


def _wykluczone(tekst: str, start: int, koniec: int, koniec_cyfr: int) -> bool:
    """Twarde wykluczenia — sprawdzane dla KAŻDEGO wzorca, także bezkontekstowego.

    Cztery sytuacje, w których ciąg cyfr na pewno nie jest kodem:
      • jednostka albo waluta zaraz po cyfrach ("2500 zł", "180 km");
      • słowo ceny/telefonu przed nimi ("cena 2500", "tel 502");
      • cyfry są fragmentem dłuższego numeru (dziewięć cyfr i więcej);
      • wyglądają na ROCZNIK POJAZDU i nic mocniejszego za nimi nie stoi.

    Rocznik jest tu osobnym przypadkiem, bo trafia w DWA wzorce naraz: „2012"
    wygląda jak kod austriacki albo belgijski, a „2015 po stluczce" — jak
    holenderski (cztery cyfry i dwie litery, gdzie literami jest polski
    przyimek). Kolizja jest realna w obie strony (2000 to Antwerpia), więc nie
    wykluczamy tych liczb bezwarunkowo: przepuszczamy je, gdy obok pada słowo
    wskazujące kraj albo skrót kodu. Bez tego rocznik z opisu auta wchodzi do
    pola kodu — a zła współrzędna wysyła człowieka 80 km w złą stronę i wygląda
    przy tym dokładnie tak samo jak trafiona.

    `koniec_cyfr` to koniec SAMYCH CYFR, nie całego dopasowania — inaczej
    holenderskie "cztery cyfry + dwie litery" zjadłoby "2500 zl" i sprawdzało
    walutę dopiero za nią.
    """
    przed = tekst[max(0, start - 30):start]
    if _PO_LICZBIE_NIE_KOD.match(tekst[koniec_cyfr:koniec_cyfr + 30]):
        return True
    if _PRZED_LICZBA_NIE_KOD.search(przed):
        return True
    if _dlugosc_ciagu_cyfr(tekst, start, koniec) >= 9:
        return True
    return (_czy_rocznik(tekst[start:koniec_cyfr])
            and not _mocny_sygnal_kodu(tekst, start, koniec))


def _kontekst_wskazuje_kod(tekst: str, start: int, koniec: int) -> bool:
    """Czy ciąg cyfr w [start, koniec) da się uznać za kod, a nie za liczbę.

    Trzy niezależne przesłanki, wystarczy jedna:
      • przyimek albo skrót kodu bezpośrednio przed ("z 50667", "PLZ 50667");
      • słowo wskazujące kraj w oknie;
      • nazwa miejscowości z wielkiej litery tuż obok ("50667 Köln", "Köln 50667").
    Wykluczenia (`_wykluczone`) są sprawdzane WCZEŚNIEJ i osobno, bo "2500 zł"
    ma obok siebie i przyimek, i nazwę własną, a kodem nie jest.
    """
    if _mocny_sygnal_kodu(tekst, start, koniec):
        return True
    przed = tekst[max(0, start - 30):start]
    po = tekst[koniec:koniec + 30]
    return bool(_NAZWA_OBOK.search(po[:20]) or _NAZWA_OBOK.search(" " + przed[-20:]))


def _kraj_z_kontekstu(tekst: str, start: int, koniec: int,
                      dozwolone: tuple[str, ...]) -> str | None:
    """Który z `dozwolone` krajów wskazuje otoczenie kodu. None = żaden."""
    okno = normalizuj_nazwe(tekst[max(0, start - 40):koniec + 40])
    for kraj in dozwolone:
        if any(_sygnal_w_oknie(s, okno) for s in _SYGNALY_KRAJU.get(kraj, ())):
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
# ODLEGŁOŚĆ PODANA WPROST PRZEZ AUTORA POSTA
#
# „Trasa ma około 490 km" to liczba od człowieka, który tę trasę zna — i bywa
# JEDYNĄ, jaką mamy, bo geokoder nie rozpoznał celu. Pokazujemy ją zawsze jako
# CYTAT („wg autora: 490 km"), nigdy jako nasz wynik i NIGDY nie liczymy z niej
# wyceny: to treść cudzego posta, a nie pomiar.
#
# Zwracamy LICZBĘ, nie fragment tekstu, i to jest decyzja o bezpieczeństwie,
# nie o formacie. Post jest wejściem od nieznanej osoby; przez tę funkcję na
# ekran operatora i do wiadomości na Telegramie idzie wyłącznie `int` z zakresu
# tras — nie ma czym wstrzyknąć ani znaczników Markdowna, ani instrukcji.
#
# NAJWIĘKSZE RYZYKO TO PRZEBIEG AUTA. W tych grupach każdy post ma kilometry:
# „przebieg 190 tys km", „przejechane 245 000 km". Wzięcie ich za długość trasy
# byłoby dokładnie tym błędem, który ten moduł ma przestać popełniać — dlatego
# liczba musi mieć OBOK słowo o trasie i NIE MIEĆ słowa o przebiegu, a wynik
# poza zakresem realnego kursu odrzucamy.
# ---------------------------------------------------------------------------
# Realny kurs lawetą: od kilku kilometrów po transport z Hiszpanii. Powyżej
# 5000 km to już nie trasa, tylko licznik albo literówka.
ZAKRES_KM_TRASY = (1, 5000)

# Liczba tuż przed „km"/„kilometrami". `(?![\w/])` po jednostce wycina „km/h"
# i „kmh", a lookbehind — „8l/100km" i ogon dłuższej liczby („45000 km" nie ma
# dać 5000). Grupa tysięcy jest zapisywana i spacją, i kropką, i przecinkiem
# („1 200", „2.500", „2,500") — bez niej „2.500 km" dałoby „2 km", czyli
# dokładnie ten rodzaj cichej pomyłki, którą ten moduł ma eliminować.
# Ułamek („490,5 km") ucinamy: pełne kilometry wystarczą.
_LICZBA_PRZED_KM = re.compile(
    r"(?<![\w,./-])(\d{1,4}(?:[ \u00a0.,]\d{3})?)(?:[.,]\d{1,2})?"
    r"\s*(?:km|kilometr\w*)(?![\w/])",
    re.IGNORECASE)

# Słowa, po których liczba przy „km" jest DŁUGOŚCIĄ TRASY. Bez któregoś z nich
# w oknie liczby nie bierzemy — „jakieś 500 km" bez kontekstu równie dobrze
# opisuje zasięg auta, a milczenie jest tu tańsze niż zła liczba.
# Formy zapisane PO NORMALIZACJI (`normalizuj_nazwe`): bez ogonków, małymi.
_SYGNALY_TRASY = (
    "trasa", "trase", "trasy", "trasie",
    "dystans", "odleglosc", "odleglosci",
    "przejechac", "przejazd", "do przejechania",
    "w jedna strone", "w obie strony", "tam i z powrotem",
    "kurs", "kursu",
)

# Słowa, które przesądzają, że kilometry dotyczą PRZEBIEGU, a nie trasy.
# Sprawdzane w wąskim oknie przy samej liczbie, bo „przebieg" z drugiego końca
# zdania nie unieważnia poprawnie podanej trasy.
_SYGNALY_PRZEBIEGU = (
    "przebieg", "licznik", "tys", "mln", "spalanie", "zasieg", "gwarancj",
)


def km_wg_autora(tresc: str | None) -> int | None:
    """Odległość, którą autor posta podał wprost. Brak takiej liczby -> None.

    Bierzemy PIERWSZE trafienie, bo tak pisze się posty: najpierw trasa, potem
    ewentualnie wariant („490 km w jedną stronę, 980 tam i z powrotem") —
    a liczba w jedną stronę jest tą, o którą operator pyta.

    Ta wartość NIE WCHODZI DO ŻADNEGO RACHUNKU. Jest cytatem obok naszych
    kilometrów i tylko tak wolno ją pokazać: autor zna trasę lepiej niż nasz
    geokoder, ale to nadal jest liczba z cudzego posta i nikt jej nie sprawdził.
    """
    tekst = tresc or ""
    for m in _LICZBA_PRZED_KM.finditer(tekst):
        start, koniec = m.span()
        wartosc = int(re.sub(r"[^\d]", "", m.group(1)))
        if not (ZAKRES_KM_TRASY[0] <= wartosc <= ZAKRES_KM_TRASY[1]):
            continue
        # Okno przebiegu jest wąskie i przesunięte w lewo: „przebieg 190 tys km"
        # i „245 000 km przebiegu" — kwalifikator stoi tuż przy liczbie.
        przy_liczbie = normalizuj_nazwe(tekst[max(0, start - 25):koniec + 15])
        if any(s in przy_liczbie for s in _SYGNALY_PRZEBIEGU):
            continue
        # Okno trasy jest szersze i sięga do poprzedniego zdania, bo „Trasa
        # Dębica-Turek. Około 490 km." to jedna myśl zapisana dwoma zdaniami.
        okno = normalizuj_nazwe(tekst[max(0, start - 80):koniec + 40])
        if any(s in okno for s in _SYGNALY_TRASY):
            return wartosc
    return None


# ---------------------------------------------------------------------------
# KIERUNEK GEOGRAFICZNY — PL względem obu końców trasy.
#
# To jest WYMIAR DO FILTROWANIA ("pokaż wyjazdy z Polski"), nigdy powód
# odrzucenia zlecenia — zasada naczelna repo obowiązuje tu tak samo, jak przy
# kilometrach: kierunek nieznany nie chowa posta, tylko nie trafia do żadnej
# pigułki filtra poza "wszystkie".
#
# CZTERY WARTOŚCI ZAMIAST BOOLA "za granicą tak/nie", bo wyjazd i przywóz to
# dla przewoźnika DWA RÓŻNE PRODUKTY: wyjazd trzeba połączyć z ładunkiem
# powrotnym (pusty powrót zjada marżę), przywóz zwykle JEST już główną nogą
# kursu. Tranzyt (oba końce poza PL) to najrzadszy, ale realny przypadek —
# post widziany w polskiej grupie o trasie Kolonia->Amsterdam.
# ---------------------------------------------------------------------------
KIERUNEK_PRZYWOZ = "przywoz"
KIERUNEK_WYJAZD = "wyjazd"
KIERUNEK_KRAJOWY = "krajowy"
KIERUNEK_TRANZYT = "tranzyt"
KIERUNEK_NIEZNANY = "nieznany"


def kierunek_geo(odbior_kraj: str | None, dostawa_kraj: str | None) -> str:
    """Kraje obu końców trasy -> kierunek względem Polski.

    BEZ ZGADYWANIA, jak w `geokoduj`: brakujący kraj po którejkolwiek stronie
    (punkt nierozpoznany przez geokoder) daje `KIERUNEK_NIEZNANY`, nie próbę
    odgadnięcia z tego, co jednak wiadomo o drugim końcu — "trasa do Niemiec"
    bez rozpoznanego punktu odbioru równie dobrze może zaczynać się w Polsce,
    co być tranzytem przez nią.
    """
    if not odbior_kraj or not dostawa_kraj:
        return KIERUNEK_NIEZNANY
    odbior_pl = odbior_kraj == KRAJ_BAZY
    dostawa_pl = dostawa_kraj == KRAJ_BAZY
    if odbior_pl and dostawa_pl:
        return KIERUNEK_KRAJOWY
    if odbior_pl:
        return KIERUNEK_WYJAZD
    if dostawa_pl:
        return KIERUNEK_PRZYWOZ
    return KIERUNEK_TRANZYT


# ---------------------------------------------------------------------------
# ZŁOŻENIE DLA WARSTWY WYŻEJ
# ---------------------------------------------------------------------------
def podsumowanie(odbior: Punkt | None, dostawa: Punkt | None,
                 tresc: str | None = None) -> dict:
    """Komplet liczb i linków dla jednego zlecenia — to, co idzie na ekran.

    `km_trasy` (odbiór->dostawa) jest liczbą PIERWSZĄ, bo to ona mówi, ile
    realnie trzeba przejechać z autem na lawecie. `km_od_bazy` idzie obok, jako
    druga i pomocnicza — przy transporcie międzynarodowym sama nic nie znaczy.
    Żadna z nich niczego nie filtruje.

    OBA PUNKTY ALBO NIC. Brakujący koniec trasy zeruje `km_trasy` I
    `szacunek_pln` — nie ma podstawiania `km_od_bazy` ani stawki minimalnej pod
    nieznany dystans. `km_od_bazy` zostaje policzony, bo to osobna, prawdziwa
    liczba (baza->odbiór) i wyłącznie pod taką etykietą wolno ją pokazać; do
    pierwszej linii alertu ani do wyceny nie wchodzi NIGDY.

    `km_wg_autora` to odległość z treści posta — cytat obok naszych liczb,
    liczony osobno i osobno oznaczany na ekranie.
    """
    b = baza()
    km_od_bazy = dystans_km(b, odbior) if odbior else None
    km_trasy = dystans_km(odbior, dostawa) if (odbior and dostawa) else None
    odbior_kraj = odbior.kraj if odbior else None
    dostawa_kraj = dostawa.kraj if dostawa else None
    return {
        "km_trasy": km_trasy,
        "km_od_bazy": km_od_bazy,
        # `kalkulacja(None)` oddaje None — brak trasy nie ma ceny.
        "szacunek_pln": kalkulacja(km_trasy)["szacunek_pln"],
        "km_wg_autora": km_wg_autora(tresc),
        "niepewne": [p.nazwa for p in (odbior, dostawa) if p and p.niepewny],
        "link_trasa": link_do_map(b, odbior, dostawa),
        "link_nawigacja": link_do_nawigacji(odbior) if odbior else "",
        "odbior_kraj": odbior_kraj,
        "dostawa_kraj": dostawa_kraj,
        "kierunek_geo": kierunek_geo(odbior_kraj, dostawa_kraj),
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
    ap.add_argument("--tresc", metavar="TEKST",
                    help="treść posta — do odległości podanej przez autora wprost")
    args = ap.parse_args(argv[1:])

    print(stan_bazy(), file=sys.stderr)

    if args.kody:
        znalezione = znajdz_kody(args.kody)
        print(f"KODY: {znalezione or '(brak)'}")
        for kod, kraj in znalezione:
            p = geokoduj(kod, None)
            print(f"  {kod} [{kraj}] -> {p.nazwa if p else 'brak w bazie'}")
        wg_autora = km_wg_autora(args.kody)
        print(f"WG AUTORA: {f'{wg_autora} km' if wg_autora else '(nie podał)'}")
        return 0

    if not args.miejsca:
        ap.print_help()
        return 0

    def jako_punkt(s: str) -> Punkt | None:
        # Wygląda jak kod? Spróbuj kodem, inaczej nazwą. Treść posta (o ile
        # podana) idzie do rozstrzygania kraju — jak w produkcyjnych wywołaniach.
        return (geokoduj(s, None, tresc=args.tresc) if re.search(r"[0-9]", s)
                else geokoduj(None, s, tresc=args.tresc))

    odbior = jako_punkt(args.miejsca[0])
    dostawa = jako_punkt(args.miejsca[1]) if len(args.miejsca) > 1 else None
    for etykieta, p in (("ODBIÓR", odbior), ("DOSTAWA", dostawa)):
        if p is None:
            print(f"{etykieta}:  brak dopasowania (null — świadomie nie zgadujemy)")
        else:
            print(f"{etykieta}:  {p.nazwa}  [{p.zrodlo}]  {p.wspolrzedne()}"
                  + ("   <-- LOKALIZACJA ZGADYWANA" if p.niepewny else ""))
    print()
    print(_json.dumps(podsumowanie(odbior, dostawa, args.tresc),
                      ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
