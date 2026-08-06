"""Testy `services/mapa.py` — bez sieci i bez paczki `staticmap`.

DWA OBSZARY, oba krytyczne z innego powodu:

  1. KIEDY OBRAZKA NIE MA BYĆ. To jest ważniejsze niż sam obrazek: mapa z jednym
     punktem myli bardziej, niż pomaga, a wyjątek albo zwis w rysowaniu nie ma
     prawa opóźnić alertu. Wszystkie te ścieżki kończą się `None`, czyli
     „wyślij tekstem" — i żadna nie rzuca.
  2. CACHE. Kafelki utrzymuje projekt społeczny z darowizn. Ten sam post
     crossowany do pięciu grup ma dać JEDNO pobranie, nie pięć — inaczej
     blokada IP jest kwestią czasu, a objawia się brakiem obrazków bez
     żadnego błędu po naszej stronie.

Render podstawiamy atrapą (`atrapa_staticmap.py`), bo `staticmap` jest
zależnością opcjonalną i testy muszą przechodzić bez niej.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from laweta_radar.services import geo, mapa
from laweta_radar.tests import atrapa_staticmap

KROSNO = geo.Punkt(49.6886, 21.7706, "kod", "Krosno")
RZESZOW = geo.Punkt(50.0412, 21.9991, "kod", "Rzeszow")
NIEPEWNY = geo.Punkt(50.1000, 22.1000, "miasto_niepewne", "Nowa Wies")


@pytest.fixture(autouse=True)
def srodowisko(monkeypatch, tmp_path):
    """Atrapa renderu, cache w katalogu testu, obrazki włączone."""
    atrapa_staticmap.zainstaluj(monkeypatch)
    monkeypatch.setattr(mapa.settings, "MAPY_W_ALERTACH", 1)
    monkeypatch.setattr(mapa.settings, "MAPA_W", 700)
    monkeypatch.setattr(mapa.settings, "MAPA_H", 450)
    monkeypatch.setattr(mapa.settings, "BASE_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Obrazek powstaje i wygląda tak, jak uzgodniono
# ---------------------------------------------------------------------------
def test_dwa_punkty_daja_plik():
    sciezka = mapa.podglad_trasy(KROSNO, RZESZOW)
    assert sciezka and sciezka.endswith(".png")
    assert len(atrapa_staticmap.RENDERY) == 1


def test_kafelki_z_osm_i_wlasny_user_agent():
    """Kafelki utrzymuje projekt społeczny, a anonimowy ruch bywa blokowany
    hurtem — nagłówek jest wymogiem polityki użycia, nie ozdobą."""
    mapa.podglad_trasy(KROSNO, RZESZOW)
    opis = atrapa_staticmap.RENDERY[0]
    assert opis["url_template"] == "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"
    assert opis["headers"]["User-Agent"] == "laweta-radar/1.0"
    assert opis["rozmiar"] == (700, 450)


def test_trasa_i_markery_maja_uzgodnione_kolory():
    """Zielony = stąd, czerwony = dotąd. Te dwa kolory operator czyta bez
    legendy, więc zmiana któregokolwiek jest zmianą znaczenia obrazka."""
    mapa.podglad_trasy(KROSNO, RZESZOW)
    opis = atrapa_staticmap.RENDERY[0]

    (linia,) = opis["linie"]
    assert linia.color == "#0044ff"
    assert linia.width == 5
    # staticmap przyjmuje (lng, lat) — kolejność ODWROTNA niż w całym repo.
    assert linia.coords == [(21.7706, 49.6886), (21.9991, 50.0412)]

    odbior, dostawa = opis["markery"]
    assert (odbior.color, odbior.width) == ("#00aa00", 16)
    assert (dostawa.color, dostawa.width) == ("#dd0000", 16)


def test_punkt_niepewny_dostaje_pomaranczowy_marker():
    """Ta sama informacja, którą podpis podaje słowem („⚠ Nowa Wies? (niepewne)"),
    musi być widoczna w tym samym rzucie oka co kształt trasy — inaczej obrazek
    wygląda na pewniejszy niż tekst pod nim."""
    mapa.podglad_trasy(NIEPEWNY, RZESZOW)
    odbior, dostawa = atrapa_staticmap.RENDERY[0]["markery"]
    assert odbior.color == "#ff8800"
    assert dostawa.color == "#dd0000"       # drugi koniec bez zmian


def test_zapis_jest_atomowy_i_nie_zostawia_smieci():
    """Plik tymczasowy + podmiana nazwy: render przerwany po budżecie czasu nie
    ma prawa zostawić w cache'u obciętego PNG-a, który następny alert wyśle
    jako gotowy obrazek."""
    sciezka = mapa.podglad_trasy(KROSNO, RZESZOW)
    kat = mapa.katalog()
    assert list(kat.glob("*.tmp")) == []
    assert atrapa_staticmap.RENDERY[0]["zapis"]["format"] == "PNG"
    assert atrapa_staticmap.RENDERY[0]["zapis"]["sciezka"].endswith(".tmp")
    assert sciezka.endswith(".png")


# ---------------------------------------------------------------------------
# KIEDY OBRAZKA NIE MA BYĆ — ta połowa jest ważniejsza
# ---------------------------------------------------------------------------
def test_jeden_punkt_to_brak_obrazka():
    """Mapa z jednym punktem nie pokazuje kształtu trasy, tylko sugeruje, że
    trasa jest znana. Przy nierozpoznanym celu myli bardziej, niż pomaga."""
    assert mapa.podglad_trasy(KROSNO, None) is None
    assert mapa.podglad_trasy(None, RZESZOW) is None
    assert mapa.podglad_trasy(None, None) is None
    assert atrapa_staticmap.RENDERY == []


def test_wylaczone_w_env_nie_rysuje(monkeypatch):
    monkeypatch.setattr(mapa.settings, "MAPY_W_ALERTACH", 0)
    assert mapa.podglad_trasy(KROSNO, RZESZOW) is None
    assert atrapa_staticmap.RENDERY == []


def test_brak_paczki_mowi_raz_i_nie_wywala(monkeypatch, capsys):
    """Paczka jest OPCJONALNA. Jej brak to stan, nie zdarzenie — komunikat przy
    każdym alercie zamieniłby log fetchera w tę jedną linijkę powtórzoną
    trzysta razy, a realne błędy utonęłyby między nimi."""
    # `None` w sys.modules to udokumentowany sposób na „tej paczki nie ma":
    # import podnosi ImportError, dokładnie jak na VPS-ie bez `pip install`.
    monkeypatch.setitem(sys.modules, "staticmap", None)

    assert mapa.podglad_trasy(KROSNO, RZESZOW) is None
    assert mapa.podglad_trasy(KROSNO, RZESZOW) is None
    assert capsys.readouterr().err.count("brak staticmap") == 1


def test_wyjatek_w_renderze_konczy_sie_none_a_nie_wyjatkiem(capsys):
    """Zerwana sieć, kafelek 429, pełny dysk. Alert ma pójść tekstem, a nie
    zginąć razem z obrazkiem."""
    atrapa_staticmap.BLAD = RuntimeError("could not download tile")
    assert mapa.podglad_trasy(KROSNO, RZESZOW) is None
    assert "mapa pominięta" in capsys.readouterr().err
    assert list(mapa.katalog().glob("trasa_*.png")) == []


def test_przekroczony_budzet_nie_trzyma_alertu(monkeypatch, capsys):
    """Pięć sekund to najwięcej, co temu dodatkowi wolno zabrać z czasu dowozu
    zlecenia. Po budżecie wracamy z None, choćby render trwał dalej."""
    monkeypatch.setattr(mapa, "BUDZET_S", 0.2)
    atrapa_staticmap.OPOZNIENIE_S = 2.0

    start = time.monotonic()
    wynik = mapa.podglad_trasy(KROSNO, RZESZOW)
    czekalismy = time.monotonic() - start

    assert wynik is None
    assert czekalismy < 1.0
    assert "przekroczyło" in capsys.readouterr().err


def test_brak_miejsca_na_zapis_nie_wywala(monkeypatch, capsys):
    monkeypatch.setattr(mapa, "katalog", lambda: None)
    assert mapa.podglad_trasy(KROSNO, RZESZOW) is None


# ---------------------------------------------------------------------------
# CACHE — jedno pobranie na trasę
# ---------------------------------------------------------------------------
def test_druga_wysylka_tej_samej_trasy_nie_pobiera_kafelkow():
    """Ten sam post crossowany do pięciu grup to pięć alertów o JEDNEJ trasie.
    Bez cache'u to pięć kompletów pobrań tych samych kafelków z serwera
    utrzymywanego z darowizn."""
    pierwszy = mapa.podglad_trasy(KROSNO, RZESZOW)
    drugi = mapa.podglad_trasy(KROSNO, RZESZOW)
    assert pierwszy == drugi
    assert len(atrapa_staticmap.RENDERY) == 1


def test_klucz_zaokragla_do_trzech_miejsc():
    """Trzy miejsca po przecinku to ok. 100 m — poniżej tego progu obrazek jest
    piksel w piksel ten sam, więc dokładniejszy klucz znaczyłby wyłącznie
    ponowne pobranie tych samych kafelków."""
    prawie = geo.Punkt(49.68861, 21.77062, "kod", "Krosno (o metr obok)")
    mapa.podglad_trasy(KROSNO, RZESZOW)
    mapa.podglad_trasy(prawie, RZESZOW)
    assert len(atrapa_staticmap.RENDERY) == 1


def test_inna_trasa_to_inny_plik():
    a = mapa.podglad_trasy(KROSNO, RZESZOW)
    b = mapa.podglad_trasy(RZESZOW, KROSNO)
    assert a != b
    assert len(atrapa_staticmap.RENDERY) == 2


def test_pewnosc_punktu_zmienia_klucz():
    """Te same współrzędne z innym `zrodlo` dają inny obrazek (marker
    pomarańczowy zamiast zielonego) — cache nie ma prawa oddać poprzedniego,
    bo nie zgadzałby się z ostrzeżeniem w podpisie."""
    pewny = geo.Punkt(50.1, 22.1, "kod", "Nowa Wies")
    mapa.podglad_trasy(pewny, RZESZOW)
    mapa.podglad_trasy(NIEPEWNY, RZESZOW)
    assert len(atrapa_staticmap.RENDERY) == 2


def test_zmiana_rozmiaru_nie_oddaje_starego_obrazka(monkeypatch):
    mapa.podglad_trasy(KROSNO, RZESZOW)
    monkeypatch.setattr(mapa.settings, "MAPA_W", 900)
    mapa.podglad_trasy(KROSNO, RZESZOW)
    assert len(atrapa_staticmap.RENDERY) == 2


def test_sprzatanie_kasuje_stare_a_zostawia_swieze():
    """Trasa, która nie wróciła przez tydzień, nie wróci. Katalog cache rośnie
    inaczej w nieskończoność."""
    kat = mapa.katalog()
    stary = kat / "trasa_stara.png"
    stary.write_bytes(b"x")
    dawno = time.time() - (mapa.DNI_CACHE + 1) * 86400
    os.utime(stary, (dawno, dawno))
    swiezy = kat / "trasa_swieza.png"
    swiezy.write_bytes(b"x")

    # Ślad po procesie ubitym w środku zapisu — nikt go już nie podmieni
    # na gotowy obrazek, więc zostaje wyłącznie po to, żeby zajmować dysk.
    porzucony = kat / "trasa_przerwana.png.999.tmp"
    porzucony.write_bytes(b"x")
    os.utime(porzucony, (dawno, dawno))

    assert mapa.sprzataj(kat) == 2
    assert not stary.exists()
    assert not porzucony.exists()
    assert swiezy.exists()


def test_trafienie_w_cache_odswieza_wiek():
    """Wiek liczymy od OSTATNIEGO użycia — kurs krążący po grupach nie ma
    wypadać z cache'u w środku swojego życia."""
    sciezka = mapa.podglad_trasy(KROSNO, RZESZOW)
    dawno = time.time() - (mapa.DNI_CACHE - 1) * 86400
    os.utime(sciezka, (dawno, dawno))

    mapa.podglad_trasy(KROSNO, RZESZOW)
    assert os.stat(sciezka).st_mtime > dawno


def test_pusty_plik_w_cache_jest_generowany_od_nowa():
    """Przerwany zapis (dysk, ubity proces) zostawia zero bajtów. Telegram
    odrzuca puste zdjęcie, więc taki wpis ma być traktowany jak brak."""
    sciezka = mapa.podglad_trasy(KROSNO, RZESZOW)
    Path(sciezka).write_bytes(b"")
    mapa.podglad_trasy(KROSNO, RZESZOW)
    assert len(atrapa_staticmap.RENDERY) == 2


# ---------------------------------------------------------------------------
# Konfiguracja
# ---------------------------------------------------------------------------
def test_absurdalny_rozmiar_jest_przycinany(monkeypatch, capsys):
    """MAPA_W=7000 z literówki to kilkaset kafelków na jeden alert — czyli
    dokładnie to zachowanie, przez które OpenStreetMap blokuje IP."""
    monkeypatch.setattr(mapa.settings, "MAPA_W", 7000)
    monkeypatch.setattr(mapa.settings, "MAPA_H", 10)
    assert mapa.wymiary() == (mapa.MAX_BOK, mapa.MIN_BOK)
    assert "poza zakresem" in capsys.readouterr().err


def test_cache_schodzi_do_tmp_gdy_katalog_pakietu_niedostepny(monkeypatch, tmp_path):
    """Deploy z kodem na read-only nie ma wyłączać obrazków — cache w /tmp gubi
    się przy reboocie i to jest w porządku, bo to tylko kafelki."""
    # Plik zamiast katalogu: `mkdir` pada wtedy NotADirectoryError niezależnie
    # od uprawnień, czyli także na roocie (chmod 0500 roota nie zatrzymuje).
    zajete = tmp_path / "nie-katalog"
    zajete.write_text("", encoding="utf-8")
    monkeypatch.setattr(mapa.settings, "BASE_DIR", zajete)

    kat = mapa.katalog()
    assert kat is not None
    assert str(kat).startswith(str(mapa.tempfile.gettempdir()))
