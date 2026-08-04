"""Testy `services/geo.py` — bez sieci i bez bazy, jak wszystko w tym katalogu.

Sprawdzamy przede wszystkim `zrodlo`, a nie same kilometry. Powód: liczba
kilometrów, która jest o 15% za duża, kosztuje operatora trochę zdziwienia,
a liczba podana jako PEWNA, gdy jest zgadnięta, wysyła lawetę 600 km nie tam.
`zrodlo` jest jedynym mechanizmem, który to rozróżnia.
"""
from __future__ import annotations

import pytest

from laweta_radar.services import geo


@pytest.fixture(autouse=True)
def baza_pod_krosnem(monkeypatch):
    """Baza w okolicy Krosna — wszystkie dystanse liczone od tego punktu."""
    monkeypatch.setattr(geo.settings, "BAZA_LAT", 49.65)
    monkeypatch.setattr(geo.settings, "BAZA_LON", 21.60)


# ---------------------------------------------------------------------------
# Rozpoznawanie miejsca
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "nazwa,oczekiwana",
    [
        ("Rzeszów", "Rzeszów"),
        ("rzeszow", "Rzeszów"),          # bez ogonków — połowa postów tak wygląda
        ("  KRAKÓW  ", "Kraków"),
        ("Bielsko-Biała", "Bielsko-Biała"),
        ("bielsko biala", "Bielsko-Biała"),
        ("Gorzów Wlkp", "Gorzów Wielkopolski"),
        ("Monachium", "München"),        # egzonim wpisany ręcznie przez człowieka
        ("Kolonia", "Köln"),
        ("Praga", "Praha"),
    ],
)
def test_nazwa_rozpoznana_dokladnie(nazwa, oczekiwana):
    punkt = geo.wspolrzedne(nazwa)
    assert punkt.nazwa == oczekiwana
    assert punkt.zrodlo in ("miasto", "miasto_niepewne")


def test_nieznane_miejsce_nie_jest_zgadywane():
    """Wynik 'brak' jest POPRAWNY. Dopasowanie na siłę do najbliższej nazwy
    dałoby pinezkę i kilometry wyglądające na pewne — czyli najgorszy możliwy
    stan przy zleceniu, którego lokalizacji nie znamy."""
    punkt = geo.wspolrzedne("Zmyslone Miasto Ktorego Nie Ma")
    assert punkt.zrodlo == "brak"
    assert geo.droga_km(geo.punkt_bazy(), punkt) is None


def test_kod_pocztowy_gdy_nazwy_nie_znamy():
    punkt = geo.wspolrzedne("jakas wioska pod Krosnem", "38-400")
    assert punkt.zrodlo == "kod_pocztowy"
    assert geo.droga_km(geo.punkt_bazy(), punkt) < 30


def test_kod_pocztowy_zdejmuje_ostrzezenie_z_nazwy_wieloznacznej():
    """„Krosno" samo w sobie może być tym pod Rzeszowem albo Odrzańskim.
    „38-400 Krosno" nie może być niczym innym — i wtedy ostrzeżenie jest
    fałszywym alarmem, a operator, który raz je zignoruje, będzie je ignorował
    także wtedy, gdy będzie prawdziwe."""
    assert geo.wspolrzedne("Krosno").zrodlo == "miasto_niepewne"
    assert geo.wspolrzedne("Krosno", "38-400").zrodlo == "miasto"


def test_kod_sprzeczny_z_nazwa_degraduje_do_niepewnego():
    """Nazwa i kod pokazują na różne miejsca — jedno z nich jest błędne i nie
    wiemy które. Pewna liczba zbudowana z dwóch niezgodnych przesłanek jest
    gorsza niż liczba oznaczona jako niepewna."""
    punkt = geo.wspolrzedne("Gdańsk", "38-400")
    assert punkt.zrodlo == "miasto_niepewne"


def test_dopasowanie_rozmyte_jest_zawsze_niepewne():
    punkt = geo.wspolrzedne("Rzeszuw")     # literówka
    assert punkt.nazwa == "Rzeszów"
    assert punkt.zrodlo == "miasto_niepewne"


# ---------------------------------------------------------------------------
# Dystans i wycena
# ---------------------------------------------------------------------------
def test_dystans_jest_wiekszy_niz_linia_prosta():
    """Laweta jedzie drogami. Pokazanie odległości w linii prostej jako „km"
    to liczba, na podstawie której ktoś podaje cenę przez telefon."""
    a = geo.wspolrzedne("Kraków")
    b = geo.wspolrzedne("Rzeszów")
    prosta = geo.dystans_km(a.lat, a.lon, b.lat, b.lon)
    assert geo.droga_km(a, b) > prosta


def test_brak_progu_na_kilometry():
    """ZASADA NACZELNA REPO. Trasa Kolonia-Kraków to 1100 km i normalny dzień
    pracy tego operatora — `geo` ma ją POLICZYĆ, nigdy odrzucić."""
    wynik = geo.opisz({"miasto_od": "Köln", "miasto_do": "Kraków"})
    assert wynik.km_od_bazy > 1000
    assert wynik.szacunek_pln > 0
    assert wynik.link_mapy.startswith("https://")


def test_szacunek_rosnie_z_dystansem():
    assert geo.szacunek_pln(200) > geo.szacunek_pln(20)
    assert geo.szacunek_pln(None) is None


def test_baza_nieustawiona_daje_none_a_nie_zero(monkeypatch):
    """(0, 0) to punkt w Zatoce Gwinejskiej. Bez tego sprawdzenia każde zlecenie
    w Polsce pokazywałoby ~5000 km i wyglądało na pomyłkę systemu."""
    monkeypatch.setattr(geo.settings, "BAZA_LAT", 0.0)
    monkeypatch.setattr(geo.settings, "BAZA_LON", 0.0)
    assert geo.km_od_bazy("Rzeszów") is None


# ---------------------------------------------------------------------------
# Linki
# ---------------------------------------------------------------------------
def test_link_do_map_prowadzi_przez_punkt_odbioru():
    """Trasa liczona od miejsca odbioru pokazuje kurs tańszym, niż jest —
    dojazd z bazy to zwykle połowa kosztu."""
    link = geo.link_do_map("Krosno", "Rzeszów", "38-400")
    assert "origin=49.65" in link
    assert "waypoints=Krosno" in link
    assert "destination=Rzesz" in link


def test_link_nawigacji_startuje_prowadzenie():
    link = geo.link_nawigacji("Rzeszów")
    assert link.startswith("https://")        # nie intent google.navigation:
    assert "dir_action=navigate" in link


def test_link_dla_nierozpoznanego_miejsca_to_wyszukiwanie():
    """Link do nawigacji prowadzący w losowy punkt jest gorszy niż wyszukiwanie:
    pierwszy wysyła kierowcę w trasę, drugi każe mu spojrzeć."""
    link = geo.link_nawigacji("Zmyslone Miasto Ktorego Nie Ma")
    assert "maps/search" in link


def test_wspolrzedne_dla_mapy_tylko_gdy_wiemy_gdzie():
    assert geo.opisz({"miasto_od": "Rzeszów"}).lat is not None
    assert geo.opisz({"miasto_od": "Zmyslone Miasto"}).lat is None
