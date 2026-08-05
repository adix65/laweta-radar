"""Offline testy services/geo.py — bez sieci, na własnej mikro-bazie.

DLACZEGO WŁASNA BAZA, A NIE data/kody_eu.csv: plik w repo jest zalążkiem
i będzie podmieniony pełnym eksportem z GeoNames przy pierwszym uruchomieniu
`scripts/pobierz_geo.py`. Test opierający się na jego zawartości zacząłby
wtedy padać z powodu, który nie ma nic wspólnego z kodem. Fixture niżej jest
mały, jawny i zawiera dokładnie te przypadki, o które chodzi — w tym dwie
„Nowe Wsie", których w zalążku świadomie nie ma.

CZEGO TE TESTY PILNUJĄ: geokoder ma prawo powiedzieć „nie wiem". Nie ma prawa
ZGADNĄĆ. Zgadnięta współrzędna wysyła człowieka 60 km w złą stronę i wygląda
przy tym dokładnie tak samo jak trafiona — dlatego `zrodlo="miasto_niepewne"`
jest tu testowane równie mocno jak same współrzędne.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.services import geo  # noqa: E402

# kraj,kod,miejscowosc,wojewodztwo,lat,lng
FIXTURE = """kraj,kod,miejscowosc,wojewodztwo,lat,lng
PL,38-400,Krosno,podkarpackie,49.6886,21.7706
PL,38-401,Krosno,podkarpackie,49.6950,21.7800
PL,35-001,Rzeszow,podkarpackie,50.0412,21.9991
PL,38-500,Sanok,podkarpackie,49.5558,22.2060
PL,36-001,Nowa Wies,podkarpackie,50.1000,22.1000
PL,05-870,Nowa Wies,mazowieckie,52.2000,20.6000
PL,62-001,Nowa Wies,wielkopolskie,52.4500,17.0000
DE,50667,Koln,Nordrhein-Westfalen,50.9375,6.9603
CZ,110 00,Praha,Praha,50.0755,14.4378
"""


def _z_fixture(tmp_path):
    plik = tmp_path / "kody_test.csv"
    plik.write_text(FIXTURE, encoding="utf-8")
    geo.zaladuj(plik)
    return plik


def setup_function() -> None:
    # Każdy test startuje z czystym stanem — indeksy są globalne (leniwy cache),
    # więc test, który wczytał fixture, nie może zatruć następnego.
    geo._indeks_kodow = None
    geo._indeks_miast = None


# ===========================================================================
# GEOKODOWANIE
# ===========================================================================
def test_dopasowanie_kodu(tmp_path):
    _z_fixture(tmp_path)
    p = geo.geokoduj("38-400", None)
    assert p is not None
    assert p.zrodlo == "kod"
    assert (round(p.lat, 4), round(p.lng, 4)) == (49.6886, 21.7706)
    assert "Krosno" in p.nazwa


def test_kod_bez_separatorow_tez_trafia(tmp_path):
    """"38400" i "38-400" to ten sam kod; "110 00" i "11000" też."""
    _z_fixture(tmp_path)
    assert geo.geokoduj("38400", None) is not None
    assert geo.geokoduj("11000", None) is not None
    assert geo.geokoduj("110 00", None) is not None


def test_dopasowanie_miasta(tmp_path):
    _z_fixture(tmp_path)
    p = geo.geokoduj(None, "Rzeszów")
    assert p is not None and p.zrodlo == "miasto"
    assert p.niepewny is False
    assert round(p.lat, 4) == 50.0412


def test_miasto_bez_ogonkow_i_wersalikami(tmp_path):
    """Post pisze "RZESZOW", baza ma "Rzeszow" — to jedno miasto."""
    _z_fixture(tmp_path)
    for zapis in ["Rzeszow", "RZESZÓW", "  rzeszow  ", "Rzeszów"]:
        p = geo.geokoduj(None, zapis)
        assert p is not None and round(p.lat, 4) == 50.0412, zapis


def test_miasto_z_wieloma_kodami_daje_jeden_punkt(tmp_path):
    """Krosno ma w fixture dwa kody — to nadal jedno, JEDNOZNACZNE miasto."""
    _z_fixture(tmp_path)
    p = geo.geokoduj(None, "Krosno")
    assert p is not None
    assert p.zrodlo == "miasto"          # nie "miasto_niepewne"
    assert 49.68 < p.lat < 49.70          # środek obu kodów


def test_miasto_niejednoznaczne_jest_oznaczone(tmp_path):
    """Trzy „Nowe Wsie" w trzech województwach — operator MUSI to zobaczyć.

    To jest sedno pola `zrodlo`: wynik nadal powstaje (bierzemy największą),
    ale niesie ostrzeżenie. Cichy wybór jednej z trzech wsi to 60 km w złą
    stronę bez żadnego sygnału, że coś było zgadywane.
    """
    _z_fixture(tmp_path)
    p = geo.geokoduj(None, "Nowa Wies")
    assert p is not None
    assert p.zrodlo == "miasto_niepewne"
    assert p.niepewny is True
    assert "3 miejscowości" in p.nazwa


def test_egzonim_kolonia_to_koln(tmp_path):
    """Post mówi "kupilem auto w Kolonii" — GeoNames zna tylko "Köln"."""
    _z_fixture(tmp_path)
    p = geo.geokoduj(None, "Kolonia")
    assert p is not None
    assert round(p.lat, 3) == 50.938


def test_brak_danych_daje_none(tmp_path):
    """Bez zgadywania. None jest poprawną odpowiedzią."""
    _z_fixture(tmp_path)
    assert geo.geokoduj(None, None) is None
    assert geo.geokoduj("", "") is None
    assert geo.geokoduj(None, "Pcim Dolny") is None
    assert geo.geokoduj("99-999", None) is None


def test_brak_pliku_bazy_nie_wywala(tmp_path):
    """Brak bazy to system, którego nie włączono — nie awaria."""
    geo.zaladuj(tmp_path / "nie-ma-mnie.csv")
    assert geo.geokoduj("38-400", "Krosno") is None
    assert "BRAK bazy" in geo.stan_bazy()


def test_kod_ma_pierwszenstwo_przed_miastem(tmp_path):
    """Kod jest pewniejszy niż nazwa, więc próbujemy go pierwszego."""
    _z_fixture(tmp_path)
    p = geo.geokoduj("38-500", "Nowa Wies")
    assert p is not None and p.zrodlo == "kod" and "Sanok" in p.nazwa


def test_kolizja_kodow_miedzy_krajami_rozstrzyga_miasto(tmp_path):
    """„50667" bez kontekstu to Köln; ten sam ciąg z polskim miastem — nie.

    Kolizje są realne, bo indeks zna kod bez separatorów: polska „39-200"
    zapisana jako „39200" ma kształt niemieckiego kodu, a różnica to 700 km.
    Nazwa miasta jest jedyną informacją, która naprawdę wie, o który kraj chodzi.
    """
    plik = tmp_path / "kolizja.csv"
    plik.write_text(
        "kraj,kod,miejscowosc,wojewodztwo,lat,lng\n"
        "PL,39-200,Debica,podkarpackie,50.0516,21.4111\n"
        "DE,39200,Magdeburg,Sachsen-Anhalt,52.1205,11.6276\n",
        encoding="utf-8")
    geo.zaladuj(plik)

    assert "Debica" in geo.geokoduj("39200", "Debica").nazwa
    assert "Magdeburg" in geo.geokoduj("39200", "Magdeburg").nazwa
    # Bez miasta wygrywa PL — tu jesteśmy i taka jest większość postów. Wybór
    # bywa zły i dlatego kraj ZAWSZE stoi w nazwie, którą widzi operator.
    bez_kontekstu = geo.geokoduj("39200", None)
    assert "Debica" in bez_kontekstu.nazwa and "(PL)" in bez_kontekstu.nazwa


def test_format_kodu_pokrywa_obslugiwane_kraje():
    """Kontrakt z klasyfikatorem: on pyta stąd, czy przyjąć kod od modelu."""
    for dobry in ["38-400", "50667", "110 00", "11000", "1012 AB", "1010"]:
        assert geo.czy_kod_pocztowy(dobry), dobry
    for zly in ["", None, "abc", "38-40", "1", "1234567", "38-400a"]:
        assert not geo.czy_kod_pocztowy(zly), zly


# ===========================================================================
# DYSTANS
# ===========================================================================
def test_dystans_krosno_rzeszow(tmp_path):
    """Krosno-Rzeszów: ~42 km w linii prostej, ~53 km po korekcie 1.25.

    Realna droga (DK19 przez Domaradz) to około 60 km, więc korekta ZANIŻA na
    tej trasie o kilkanaście procent. To jest dokładnie powód, dla którego
    docstring `dystans_km` mówi „do przesiewu, nie do wyceny": różnica rzędu
    kilku kilometrów nie zmienia decyzji „jechać czy nie", ale na fakturze
    byłaby błędem.
    """
    _z_fixture(tmp_path)
    krosno = geo.geokoduj("38-400", None)
    rzeszow = geo.geokoduj("35-001", None)
    km = geo.dystans_km(krosno, rzeszow)
    assert 45 <= km <= 70, km
    # Korekta faktycznie działa: wynik jest o 25% większy od linii prostej.
    assert abs(km / geo.KOREKTA_TRASY - 42.5) < 2.0


def test_dystans_jest_symetryczny_i_zerowy_dla_tego_samego_punktu(tmp_path):
    _z_fixture(tmp_path)
    a = geo.geokoduj("38-400", None)
    b = geo.geokoduj("35-001", None)
    assert geo.dystans_km(a, b) == geo.dystans_km(b, a)
    assert geo.dystans_km(a, a) == 0.0


def test_dystans_miedzynarodowy_ma_sens(tmp_path):
    """Krosno-Kolonia: w linii prostej ~1050 km, po korekcie ~1300."""
    _z_fixture(tmp_path)
    km = geo.dystans_km(geo.geokoduj("38-400", None), geo.geokoduj("50667", None))
    assert 1100 <= km <= 1500, km


# ===========================================================================
# LINKI
# ===========================================================================
def _param(url: str, nazwa: str) -> str | None:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query).get(nazwa, [None])[0]


def test_link_z_trzema_punktami(tmp_path):
    """baza -> odbiór (waypoint) -> dostawa (cel)."""
    _z_fixture(tmp_path)
    baza = geo.Punkt(49.6886, 21.7706, "baza", "Krosno")
    odbior = geo.geokoduj("38-500", None)      # Sanok
    dostawa = geo.geokoduj("35-001", None)     # Rzeszów
    url = geo.link_do_map(baza, odbior, dostawa)

    assert url.startswith("https://www.google.com/maps/dir/?api=1")
    assert _param(url, "origin") == baza.wspolrzedne()
    assert _param(url, "destination") == dostawa.wspolrzedne()
    assert _param(url, "waypoints") == odbior.wspolrzedne()
    assert _param(url, "travelmode") == "driving"


def test_link_z_jednym_punktem_nie_ma_waypointu(tmp_path):
    """Bez dostawy celem jest odbiór — i NIE ma go być jednocześnie w waypoints.

    Waypoint równy celowi każe mapom wyznaczyć trasę do samego siebie.
    """
    _z_fixture(tmp_path)
    baza = geo.Punkt(49.6886, 21.7706, "baza", "Krosno")
    odbior = geo.geokoduj("38-500", None)
    url = geo.link_do_map(baza, odbior, None)

    assert _param(url, "destination") == odbior.wspolrzedne()
    assert _param(url, "waypoints") is None


def test_link_bez_zadnego_punktu_jest_pusty(tmp_path):
    """Pusty string — warstwa wyżej po prostu nie pokazuje przycisku."""
    baza = geo.Punkt(49.6886, 21.7706, "baza", "Krosno")
    assert geo.link_do_map(baza, None, None) == ""


def test_link_do_nawigacji_odpala_nawigacje(tmp_path):
    _z_fixture(tmp_path)
    cel = geo.geokoduj("35-001", None)
    url = geo.link_do_nawigacji(cel)
    assert _param(url, "dir_action") == "navigate"
    assert _param(url, "destination") == cel.wspolrzedne()
    assert _param(url, "travelmode") == "driving"


def test_wspolrzedne_maja_kropke_dziesietna():
    """Przecinek rozdziela lat od lng — w liczbie musi być kropka."""
    p = geo.Punkt(49.6886, 21.7706, "kod", "Krosno")
    assert p.wspolrzedne() == "49.688600,21.770600"


# ===========================================================================
# KALKULACJA — etykieta, nie bramka
# ===========================================================================
def test_kalkulacja_ma_stawke_minimalna():
    from laweta_radar.config import settings

    maly = geo.kalkulacja(5)
    assert maly["km_trasy"] == 5.0
    assert maly["szacunek_pln"] == settings.STAWKA_MINIMALNA

    duzy = geo.kalkulacja(300)
    assert duzy["szacunek_pln"] == round(300 * settings.STAWKA_ZA_KM, 2)


def test_kalkulacja_znosi_smieci():
    assert geo.kalkulacja(0)["km_trasy"] == 0.0
    assert geo.kalkulacja(-10)["km_trasy"] == 0.0


def test_podsumowanie_pokazuje_trase_i_dystans_od_bazy(tmp_path):
    """Długość kursu jest liczbą pierwszą, dystans od bazy — pomocniczą."""
    _z_fixture(tmp_path)
    odbior = geo.geokoduj("38-500", None)      # Sanok
    dostawa = geo.geokoduj("35-001", None)     # Rzeszów
    p = geo.podsumowanie(odbior, dostawa)
    assert p["km_trasy"] and p["km_od_bazy"]
    assert p["link_trasa"] and p["link_nawigacja"]
    assert p["niepewne"] == []


def test_podsumowanie_bez_dostawy_nie_udaje_ze_zna_trase(tmp_path):
    """Brak `km_trasy` jest informacją: nie wiemy, dokąd auto ma jechać."""
    _z_fixture(tmp_path)
    p = geo.podsumowanie(geo.geokoduj("38-500", None), None)
    assert p["km_trasy"] is None
    assert p["km_od_bazy"] is not None
    assert p["szacunek_pln"] > 0


def test_podsumowanie_niesie_ostrzezenie_o_niepewnosci(tmp_path):
    """Niepewna lokalizacja MUSI dojść do interfejsu, nie zostać w module."""
    _z_fixture(tmp_path)
    p = geo.podsumowanie(geo.geokoduj(None, "Nowa Wies"), geo.geokoduj("35-001", None))
    assert len(p["niepewne"]) == 1
    assert "Nowa Wies" in p["niepewne"][0]


# ===========================================================================
# WYCIĄGANIE KODÓW Z SUROWEGO TEKSTU
# ===========================================================================
def test_kody_formaty_krajowe():
    assert ("38-400", "PL") in geo.znajdz_kody("odbior 38-400 Krosno")
    assert ("50667", "DE") in geo.znajdz_kody("auto stoi w 50667 Koln")
    assert ("110 00", "CZ") in geo.znajdz_kody("warsztat 110 00 Praha")
    assert ("811 01", "SK") in geo.znajdz_kody("odbior Bratislava 811 01")
    assert ("1012 AB", "NL") in geo.znajdz_kody("adres 1012 AB Amsterdam")
    assert ("1010", "AT") in geo.znajdz_kody("kupilem auto w Wiedniu, kod 1010")
    assert ("1000", "BE") in geo.znajdz_kody("odbior Bruksela 1000")


def test_kod_polski_nie_wymaga_kontekstu():
    """Format NN-NNN jest unikalny — nie da się go pomylić z niczym."""
    assert geo.znajdz_kody("38-400") == [("38-400", "PL")]


def test_cena_nie_jest_kodem():
    """„moge dac 2500 zl" — cztery cyfry i dwie litery, a kodem nie jest."""
    assert geo.znajdz_kody("moge dac 2500 zl za kurs") == []
    assert geo.znajdz_kody("cena 12000, rocznik 2015") == []
    assert geo.znajdz_kody("przebieg 180000 km, poj 1900") == []


def test_telefon_nie_jest_kodem():
    """Numer w formacie "502 33 44 55" ma kształt czeskiego kodu."""
    assert geo.znajdz_kody("dzwonic 502 33 44 55") == []
    assert geo.znajdz_kody("tel +48 505 606 707") == []
    assert geo.znajdz_kody("kontakt 601234567") == []


def test_rocznik_pojazdu_nie_jest_kodem():
    """Rocznik trafia w DWA wzorce naraz i w obu wygląda jak adres.

    „Skoda Octavia 2012" ma kształt kodu austriackiego, a nazwa własna obok jest
    marką, nie miastem. „Golfa 2015 po stluczce" ma kształt holenderskiego
    (cztery cyfry i dwie litery), gdzie literami jest polski przyimek. Oba
    wpadały do pola kodu, a od czasu fallbacku w klasyfikatorze wpadałyby wprost
    do bazy — bez modelu, który mógłby to wyprostować.
    """
    assert geo.znajdz_kody("Skoda Octavia 2012, nie odpala") == []
    assert geo.znajdz_kody("sprzedam Golfa 2015 po stluczce") == []
    assert geo.znajdz_kody("auto 1998 na chodzie, pilne") == []
    # Ten sam rocznik ZE wskazaniem kraju albo skrótem kodu nadal jest kodem —
    # 2000 to Antwerpia, a wykluczenie ma odsiewać rocznik, nie kasować Belgię.
    assert ("2000", "BE") in geo.znajdz_kody("odbior Antwerpia 2000, auto na kolach")
    assert ("2015", "?") in geo.znajdz_kody("odbior spod kodu 2015, auto gotowe")


def test_skoda_to_nie_slowacja():
    """Sygnały krajów zapisane ze spacjami (" sk ", " cz ") to CAŁE SŁOWA.

    Szukane jako fragment siedzą w „Skodzie" i w „częściach" — czyli w dwóch
    najczęstszych słowach w tych grupach. Skutek był podwójny: liczba obok marki
    dostawała kontekst „kraj", a czeskie i słowackie kody przypisywały się do
    kraju na podstawie nazwy auta.
    """
    assert geo.znajdz_kody("Skoda Fabia 2011, czesci nowe") == []
    assert geo._kraj_z_kontekstu("Skoda 110 00 czesci", 6, 12, ("CZ", "SK")) is None
    # Skrót jako osobne słowo nadal działa.
    assert geo._kraj_z_kontekstu("odbior 811 01, SK, auto stoi", 7, 13, ("CZ", "SK")) == "SK"


def test_niejednoznaczny_kraj_daje_pytajnik():
    """Lepiej oddać kod z krajem "?" niż pominąć go, bo nie wiadomo skąd.

    Rozstrzygnięcie zostawiamy geokoderowi — on ma bazę i sam zobaczy, że
    "602 00" istnieje tylko w jednym kraju.
    """
    kody = geo.znajdz_kody("odbior spod adresu 602 00, auto gotowe")
    assert kody == [("602 00", "?")]


def test_kilka_kodow_w_jednym_poscie():
    tekst = "z 38-400 Krosno do 50667 Koln, potem 110 00 Praha"
    assert geo.znajdz_kody(tekst) == [("38-400", "PL"), ("110 00", "CZ"), ("50667", "DE")]


def test_ten_sam_kod_nie_powtarza_sie():
    assert geo.znajdz_kody("38-400 Krosno, jeszcze raz 38-400") == [("38-400", "PL")]


def test_pusty_tekst_nie_wywala():
    assert geo.znajdz_kody("") == []
    assert geo.znajdz_kody(None) == []


# ===========================================================================
# BAZA OPERATORA
# ===========================================================================
def test_baza_domyslnie_krosno():
    """Bez BAZA_LAT/BAZA_LNG w .env bierzemy Krosno i MÓWIMY o tym w nazwie.

    Zero-zero (Zatoka Gwinejska) dawałoby dystanse rzędu 5000 km, które
    wyglądają jak awaria geokodera, a nie jak brak konfiguracji.
    """
    from laweta_radar.config import settings

    if settings.BAZA_LAT and settings.BAZA_LNG:
        return  # środowisko ma własną bazę — nie ma czego testować
    b = geo.baza()
    assert round(b.lat, 3) == 49.689
    assert b.zrodlo == "baza"
    assert "domyślne" in b.nazwa
