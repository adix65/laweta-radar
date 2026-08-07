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

import pytest

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

    Zapytanie w tym teście jest CELOWO bez separatora ("39200" — tak zapisałby
    to ktoś, kto zgubił polski myślnik) — więc kształt (`_kraje_z_ksztaltu`)
    niczego tu nie rozstrzyga i całe rozstrzygnięcie spada na miasto/kontekst;
    zapis Z separatorem ("39-200") ma osobny test niżej.
    """
    plik = tmp_path / "kolizja.csv"
    plik.write_text(
        "kraj,kod,miejscowosc,wojewodztwo,lat,lng\n"
        "PL,39-200,Debica,podkarpackie,50.0516,21.4111\n"
        "DE,39200,Magdeburg,Sachsen-Anhalt,52.1205,11.6276\n",
        encoding="utf-8")
    geo.zaladuj(plik)

    z_debica = geo.geokoduj("39200", "Debica")
    assert "Debica" in z_debica.nazwa and z_debica.zrodlo == "kod"
    z_magdeburg = geo.geokoduj("39200", "Magdeburg")
    assert "Magdeburg" in z_magdeburg.nazwa and z_magdeburg.zrodlo == "kod"
    # Bez miasta i bez treści posta NIC nie rozstrzyga kraj — wynik nadal
    # powstaje (PL, bo tu jesteśmy i taka jest większość postów), ale TERAZ
    # oznaczony jako niepewny: to zgadywanie, nie rozstrzygnięcie, i operator
    # ma to zobaczyć, zanim pojedzie 700 km w złą stronę.
    bez_kontekstu = geo.geokoduj("39200", None)
    assert "Debica" in bez_kontekstu.nazwa and "(PL)" in bez_kontekstu.nazwa
    assert bez_kontekstu.zrodlo == "miasto_niepewne"
    assert bez_kontekstu.niepewny is True
    assert "DE" in bez_kontekstu.nazwa   # kraj alternatywny widoczny w opisie


# ===========================================================================
# KOLIZJA PL/CZ — separator JEST nośnikiem informacji o kraju
#
# Realny przypadek produkcyjny: "Transport eines Jaguar XF von Bochum nach
# 66-449 Skwierzyna/ Polen" pokazywał marker dostawy pod Brnem (Czechy) zamiast
# w Skwierzynie (lubuskie), bo "66-449" (PL) i "664 49" (CZ) po zdjęciu
# separatorów dają identyczne "66449", a dopasowanie brało kolizję na ślepo.
#
# Fixture niżej jest CELOWO SYMETRYCZNA (oba kraje mają wpis pod dokładnie tym
# samym kolidującym kluczem) — testuje sam MECHANIZM rozstrzygania kształtem,
# niezależnie od tego, czy taka symetria akurat zachodzi w realnym eksporcie.
# W prawdziwej bazie NIE zachodzi: `grep '^PL,66-449,' data/kody_eu.csv` nie
# daje nic, Skwierzyna siedzi w pliku pod innym kodem. Ten ASYMETRYCZNY,
# faktyczny kształt (kod nie ma dopasowania w kraju, na który wskazuje —
# fallback nazwą albo `None`, NIGDY cudzy kraj) mają osobne testy niżej,
# `test_realny_przypadek_skwierzyna_daje_kierunek_przywoz_nie_tranzyt` i
# `test_realny_kod_bez_dopasowania_w_kraju_nigdy_nie_daje_cudzego_kraju`.
# ===========================================================================
FIXTURE_PL_CZ = """kraj,kod,miejscowosc,wojewodztwo,lat,lng
PL,66-449,Skwierzyna,lubuskie,52.5964,15.5088
CZ,664 49,Ostopovice,Jihomoravsky,49.1611,16.4967
"""


def _z_fixture_pl_cz(tmp_path):
    plik = tmp_path / "pl_cz.csv"
    plik.write_text(FIXTURE_PL_CZ, encoding="utf-8")
    geo.zaladuj(plik)
    return plik


def test_kod_z_myslnikiem_to_zawsze_polska(tmp_path):
    """"66-449" — myślnik po dwóch cyfrach jest UNIKALNIE polski. Rozstrzyga
    sam, bez miasta i bez treści, i to PEWNIE (`zrodlo="kod"`, nie niepewne)."""
    _z_fixture_pl_cz(tmp_path)
    p = geo.geokoduj("66-449", None)
    assert p is not None
    assert p.kraj == "PL" and "Skwierzyna" in p.nazwa
    assert p.zrodlo == "kod"
    assert p.niepewny is False


def test_kod_ze_spacja_po_trzech_cyfrach_to_czechy(tmp_path):
    """"664 49" — spacja po TRZECH cyfrach jest wzorem czesko-słowackim, nie
    polskim (polski to dwie-myślnik-trzy). Rozstrzyga pewnie na CZ."""
    _z_fixture_pl_cz(tmp_path)
    p = geo.geokoduj("664 49", None)
    assert p is not None
    assert p.kraj == "CZ" and "Ostopovice" in p.nazwa
    assert p.zrodlo == "kod"
    assert p.niepewny is False


def test_kod_bez_separatora_bez_kontekstu_jest_niepewny(tmp_path):
    """"66449" (bez separatora wcale) nie dowodzi niczego — może być polskim
    kodem bez myślnika tak samo jak czeskim/słowackim bez spacji. Bez miasta
    i bez treści posta MUSI wyjść jako niepewne, nie jako pewny wybór."""
    _z_fixture_pl_cz(tmp_path)
    p = geo.geokoduj("66449", None)
    assert p is not None
    assert p.zrodlo == "miasto_niepewne"
    assert p.niepewny is True
    # Oba kandydujące kraje mają być widoczne operatorowi w nazwie.
    assert "PL" in p.nazwa and "CZ" in p.nazwa


def test_kod_bez_separatora_z_krajem_w_tresci_wybiera_polske(tmp_path):
    """Ten sam niejednoznaczny "66449" — ale post mówi wprost "Polen". Kontekst
    z treści rozstrzyga tam, gdzie kształt kodu milczy."""
    _z_fixture_pl_cz(tmp_path)
    p = geo.geokoduj("66449", None, tresc="Transport nach 66449 Skwierzyna/ Polen")
    assert p is not None
    assert p.kraj == "PL"
    assert p.zrodlo == "kod"
    assert p.niepewny is False


def test_realny_przypadek_skwierzyna_daje_kierunek_przywoz_nie_tranzyt(tmp_path):
    """REALNY PRZYPADEK Z PRODUKCJI: "Transport eines Jaguar XF von Bochum nach
    66-449 Skwierzyna/ Polen" pokazywał Brno zamiast Skwierzyny, 887 km zamiast
    ~700 i kierunek "tranzyt" zamiast "przywoz".

    Fixture NIE jest symetryczna jak `FIXTURE_PL_CZ` wyżej — i to jest clou tego
    testu. `grep '^PL,66-449,' data/kody_eu.csv` w prawdziwym eksporcie GeoNames
    nie daje NIC: Skwierzyna w pliku siedzi pod innym kodem, więc pod kluczem
    "66449" baza zna WYŁĄCZNIE czeskie Ostopovice. Myślnik w "66-449" mówi
    "szukaj w Polsce", ale sam kod w Polsce nie istnieje — więc krok 1 kończy
    się bez punktu (nie czeskim), a rozstrzyga dopiero nazwa "Skwierzyna" z
    treści posta, ograniczona do kraju, o którym już wiadomo z kształtu (PL).
    """
    plik = tmp_path / "pl_cz_realny_eksport.csv"
    plik.write_text(
        "kraj,kod,miejscowosc,wojewodztwo,lat,lng\n"
        "PL,66-440,Skwierzyna,lubuskie,52.5964,15.5088\n"
        "CZ,664 49,Ostopovice,Jihomoravsky,49.1611,16.4967\n",
        encoding="utf-8")
    geo.zaladuj(plik)

    tresc = "Transport eines Jaguar XF von Bochum nach 66-449 Skwierzyna/ Polen"
    odbior = geo.Punkt(51.4818, 7.2162, "miasto", "Bochum (DE)", kraj="DE")
    dostawa = geo.geokoduj("66-449", "Skwierzyna", tresc=tresc)

    assert dostawa is not None
    assert dostawa.kraj == "PL"
    # "kod" nie pasował w PL (baza go tam nie ma) -> wynik przyszedł fallbackiem
    # nazwą miasta, nie dopasowaniem kodu.
    assert dostawa.zrodlo == "miasto"
    assert dostawa.niepewny is False
    assert round(dostawa.lat, 4) == 52.5964    # Skwierzyna, NIE Brno

    p = geo.podsumowanie(odbior, dostawa, tresc)
    assert p["dostawa_kraj"] == "PL"
    assert p["kierunek_geo"] == geo.KIERUNEK_PRZYWOZ
    assert p["niepewne"] == []   # rozstrzygnięte pewnie — nic tu nie ostrzega
    # Dystans musi wyjść z realnej Skwierzyny, nie z Brna (887 km bez sensu
    # dla trasy Bochum-lubuskie): Haversine*1.25 w tej relacji to rząd 650-780 km.
    assert 550 <= p["km_trasy"] <= 850, p["km_trasy"]


def test_realny_kod_bez_dopasowania_w_kraju_nigdy_nie_daje_cudzego_kraju(tmp_path):
    """Ten sam "66-449" jak wyżej, ale BEZ nazwy miasta ani w treści, ani jako
    argument: kod nie ma dopasowania w Polsce (baza pod tym kluczem zna
    wyłącznie Czechy), więc jedynym poprawnym wynikiem jest `None`. Kod
    z cudzego kraju pod tym samym, zdartym z separatorów kluczem jest ZAWSZE
    błędną odpowiedzią, nawet gdy cyfry pasują — pokazanie Ostopovic (Brno,
    887 km) jako pewnika jest dokładnie tym błędem, który ten test pilnuje.
    """
    plik = tmp_path / "pl_cz_realny_eksport_bez_miasta.csv"
    plik.write_text(
        "kraj,kod,miejscowosc,wojewodztwo,lat,lng\n"
        "PL,66-440,Skwierzyna,lubuskie,52.5964,15.5088\n"
        "CZ,664 49,Ostopovice,Jihomoravsky,49.1611,16.4967\n",
        encoding="utf-8")
    geo.zaladuj(plik)

    wynik = geo.geokoduj("66-449", None)
    assert wynik is None

    # Konsekwencja w warstwie wyżej: brak punktu -> brak trasy i brak ceny,
    # zamiast pewnie wyglądającej trasy do złego kraju.
    odbior = geo.Punkt(51.4818, 7.2162, "miasto", "Bochum (DE)", kraj="DE")
    p = geo.podsumowanie(odbior, wynik)
    assert p["km_trasy"] is None
    assert p["szacunek_pln"] is None
    assert p["dostawa_kraj"] is None


def test_de_piec_cyfr_kontra_cz_sk_bez_spacji_ta_sama_pulapka(tmp_path):
    """Ta sama pułapka po drugiej stronie: niemiecki kod (pięć cyfr, NIGDY
    separatora) i czesko-słowacki zapisany bez spacji dają identyczny klucz.
    Bez separatora ANI kontekstu nie zgadujemy; kontekst rozstrzyga tak samo
    jak przy PL/CZ."""
    plik = tmp_path / "de_cz.csv"
    plik.write_text(
        "kraj,kod,miejscowosc,wojewodztwo,lat,lng\n"
        "DE,10115,Berlin,Berlin,52.5300,13.3800\n"
        "CZ,101 15,Testovice,Praha,50.1000,14.3000\n",
        encoding="utf-8")
    geo.zaladuj(plik)

    # "10115" bez separatora: DE_FR_IT i CZ_SK dopasowują się oba -> niepewne.
    bez_kontekstu = geo.geokoduj("10115", None)
    assert bez_kontekstu.zrodlo == "miasto_niepewne"
    assert bez_kontekstu.niepewny is True

    # Spacja po TRZECH cyfrach jest jednoznacznie czesko-słowacka.
    z_myslnikiem = geo.geokoduj("101 15", None)
    assert z_myslnikiem.kraj == "CZ" and z_myslnikiem.zrodlo == "kod"

    # Kontekst "Niemcy" rozstrzyga gołe cyfry na DE.
    z_kontekstem = geo.geokoduj("10115", None, tresc="Berlin, Niemcy")
    assert z_kontekstem.kraj == "DE" and z_kontekstem.zrodlo == "kod"


# ===========================================================================
# NAZWY Z WIELU KRAJÓW, FORMY ODMIENIONE I POPULACJA
#
# Fixture w nowym formacie: wiersze KODOWE (jak dotąd) plus wiersze
# MIEJSCOWOŚCI — bez kodu, z populacją, prosto z dumpu GeoNames. To na nich
# opiera się wyszukiwanie po nazwie; wiersz kodowy z nazwą urzędu zamiast
# miasta („Agentur fuer Arbeit Dortmund") jest tu CELOWO, bo dokładnie takim
# wpisem niemiecki eksport kodów zatruwał wyszukiwanie po nazwie.
# ===========================================================================
FIXTURE_Z_MIEJSCOWOSCIAMI = """kraj,kod,miejscowosc,wojewodztwo,lat,lng,populacja
PL,25-001,Kielce,swietokrzyskie,50.8661,20.6286,
PL,,Kielce,swietokrzyskie,50.8661,20.6286,194852
PL,,Katowice,slaskie,50.2649,19.0238,294510
PL,39-400,Tarnobrzeg,podkarpackie,50.5730,21.6790,
PL,,Tarnobrzeg,podkarpackie,50.5730,21.6790,47816
DE,60311,Frankfurt am Main,Hessen,50.1106,8.6820,
DE,,Frankfurt am Main,Hessen,50.1106,8.6820,764104
DE,,Frankfurt (Oder),Brandenburg,52.3471,14.5506,57015
DE,56112,Lahnstein,Rheinland-Pfalz,50.3049,7.6060,
DE,,Lahnstein,Rheinland-Pfalz,50.3049,7.6060,18067
AT,,Lahnstein,Oberoesterreich,47.5833,13.5333,0
DE,44135,Agentur fuer Arbeit Dortmund,Nordrhein-Westfalen,51.5142,7.4700,
DE,,Dortmund,Nordrhein-Westfalen,51.5142,7.4652,588462
DE,,Neustadt,Hessen,50.8500,9.1167,20000
AT,,Neustadt,Tirol,47.2000,11.4000,15000
PL,,Strzelce,lubuskie,52.8770,15.5310,9700
PL,,Strzelin,dolnoslaskie,50.7833,17.0650,12500
"""


def _z_miejscowosciami(tmp_path):
    plik = tmp_path / "kody_miejscowosci_test.csv"
    plik.write_text(FIXTURE_Z_MIEJSCOWOSCIAMI, encoding="utf-8")
    geo.zaladuj(plik)
    return plik


def test_frankfurt_z_kodem_to_frankfurt_am_main(tmp_path):
    """Test z diagnozy produkcyjnej: "Frankfurt" + kod 60311 -> Frankfurt am
    Main (DE). Kod jest pewniejszy niż nazwa i rozstrzyga sam."""
    _z_miejscowosciami(tmp_path)
    p = geo.geokoduj("60311", "Frankfurt")
    assert p is not None
    assert p.zrodlo == "kod"
    assert "Frankfurt am Main" in p.nazwa and "(DE)" in p.nazwa


def test_frankfurt_bez_nawiasow_geonames(tmp_path):
    """GeoNames pisze "Frankfurt (Oder)", post — "Frankfurt Oder"."""
    _z_miejscowosciami(tmp_path)
    p = geo.geokoduj(None, "Frankfurt Oder")
    assert p is not None
    assert round(p.lat, 4) == 52.3471


def test_lahnstein_kod_w_tresci_wybiera_niemcy(tmp_path):
    """PRODUKCYJNY BŁĄD, dla którego powstała ta zmiana: "Miejscowosc
    Lahnstein 56112 Niemcy do 39-400 Tarnobrzeg" dostawał Lahnstein
    w Austrii, 782 km od właściwego. Kod 56112 STAŁ w treści i wskazywał
    Niemcy jednoznacznie — teraz jest pytany."""
    _z_miejscowosciami(tmp_path)
    p = geo.geokoduj(None, "Lahnstein",
                     tresc="Miejscowosc Lahnstein 56112 Niemcy do 39-400 Tarnobrzeg")
    assert p is not None
    assert "(DE)" in p.nazwa
    assert round(p.lat, 4) == 50.3049
    # Kraj z kodu rozstrzygnął jednoznacznie — to nie jest zgadywanie.
    assert p.zrodlo == "miasto"


def test_lahnstein_slowo_niemcy_wybiera_niemcy(tmp_path):
    """Bez kodu w treści rozstrzyga nazwa kraju."""
    _z_miejscowosciami(tmp_path)
    p = geo.geokoduj(None, "Lahnstein", tresc="Lahnstein, Niemcy, auto na kolach")
    assert p is not None and "(DE)" in p.nazwa


def test_lahnstein_bez_kontekstu_wybiera_populacje(tmp_path):
    """Bez kodu i bez nazwy kraju zostaje populacja: 18 tys. kontra wioska.
    Wybór jest oznaczony jako niepewny, bo to nadal wybór za autora posta."""
    _z_miejscowosciami(tmp_path)
    p = geo.geokoduj(None, "Lahnstein")
    assert p is not None and "(DE)" in p.nazwa
    assert p.zrodlo == "miasto_niepewne"
    assert p.niepewny is True


def test_kraj_z_drugiego_konca_trasy_nie_kasuje_miasta(tmp_path):
    """"transport z Czech" mówi o DRUGIM końcu trasy — filtr, który skasowałby
    wszystkie warianty Lahnstein, ma być zignorowany, nie posłuchany."""
    _z_miejscowosciami(tmp_path)
    p = geo.geokoduj(None, "Lahnstein", tresc="odbior Lahnstein, transport z Czech")
    assert p is not None and "(DE)" in p.nazwa


def test_kraje_z_kodow_ufa_rozstrzygnieciu_kodu_a_nie_calej_bazie(tmp_path):
    """`_kraje_z_kodow` (rozstrzyganie kolizji NAZWY miasta kodem znalezionym
    w treści) MA ufać temu, co `znajdz_kody` już rozstrzygnęła z kontekstu —
    nie wolno jej dorzucać WSZYSTKICH krajów, jakie baza zna pod tym samym,
    zdartym z separatorów kluczem. Baza ma tu (celowo, jak "66-449"/"664 49"
    w produkcji) DRUGI wpis pod tym samym "56112" — austriacki, czyli TEN SAM
    kraj co drugi wariant miasta "Lahnstein" w fixture.

    "Koln" (nie "Niemcy") jest tu użyte specjalnie: to sygnał KONTEKSTU kodu
    (`_SYGNALY_KRAJU`), nie nazwa kraju (`_NAZWY_KRAJOW`) — więc DRUGI filtr
    w `geokoduj` (`_kraje_z_nazw`) nie pomoże, i rozstrzygnięcie zależy
    WYŁĄCZNIE od `_kraje_z_kodow`. Stare zachowanie (bierz WSZYSTKIE kraje
    bazy pod tym kluczem) dorzucałoby AT, filtr nie zawężałby wariantów do
    jednego i wynik trafiałby do `zrodlo="miasto_niepewne"` — mimo że „56112
    Koln" w treści jest jednoznaczne.
    """
    plik = tmp_path / "kolizja_kodu_w_tresci.csv"
    plik.write_text(
        FIXTURE_Z_MIEJSCOWOSCIAMI
        + "AT,56112,Irrelevant,Sonstige,48.0000,14.0000,\n",
        encoding="utf-8")
    geo.zaladuj(plik)

    p = geo.geokoduj(None, "Lahnstein",
                     tresc="Miejscowosc Lahnstein 56112 Koln do 39-400 Tarnobrzeg")
    assert p is not None
    assert "(DE)" in p.nazwa
    assert round(p.lat, 4) == 50.3049
    assert p.zrodlo == "miasto"   # jednoznaczne — kod w treści rozstrzygnął sam
    assert p.niepewny is False


def test_populacje_porownywalne_daja_none(tmp_path):
    """20 tys. kontra 15 tys. to rzut monetą, nie rozstrzygnięcie. None —
    jak przy każdym innym zgadywaniu."""
    _z_miejscowosciami(tmp_path)
    assert geo.geokoduj(None, "Neustadt") is None


def test_forma_odmieniona_kielc_to_kielce(tmp_path):
    """Test z diagnozy: "do Kielc" -> model zapisuje "Kielc" -> baza ma
    "Kielce". Dopasowanie prefiksem, oznaczone własnym źródłem."""
    _z_miejscowosciami(tmp_path)
    p = geo.geokoduj(None, "Kielc")
    assert p is not None
    assert "Kielce" in p.nazwa
    assert p.zrodlo == "miasto_odmienione"
    assert p.niepewny is True     # traktowane jak "miasto_niepewne"


def test_forma_odmieniona_katowic_to_katowice(tmp_path):
    _z_miejscowosciami(tmp_path)
    p = geo.geokoduj(None, "Katowic")
    assert p is not None and "Katowice" in p.nazwa
    assert p.zrodlo == "miasto_odmienione"


def test_prefiks_za_krotki_nie_zgaduje(tmp_path):
    """Minimum 5 znaków — żeby "Nowa" nie łapało "Nowaczyzny"."""
    _z_miejscowosciami(tmp_path)
    assert geo.geokoduj(None, "Kiel") is None


def test_prefiks_wieloznaczny_daje_none(tmp_path):
    """"Strzel" pasuje do Strzelec i Strzelina — dwóch RÓŻNYCH miast. None."""
    _z_miejscowosciami(tmp_path)
    assert geo.geokoduj(None, "Strzel") is None


def test_prefiks_z_za_duza_roznica_nie_zgaduje(tmp_path):
    """Najwyżej 3 znaki różnicy: "Frank" nie ma prawa stać się Frankfurtem."""
    _z_miejscowosciami(tmp_path)
    assert geo.geokoduj(None, "Frank") is None


def test_dortmund_trafia_w_miasto_a_nie_w_urzad(tmp_path):
    """Test z diagnozy: "Dortmund" działał PRZYPADKIEM, przez nazwę urzędu
    ("Agentur fuer Arbeit Dortmund") z niemieckiego eksportu kodów. Po nazwie
    szukamy w miejscowościach — urząd zostaje przy swoim kodzie."""
    _z_miejscowosciami(tmp_path)
    p = geo.geokoduj(None, "Dortmund")
    assert p is not None
    assert p.zrodlo == "miasto"
    assert "Agentur" not in p.nazwa
    # Współrzędne wiersza miejscowości, nie urzędu.
    assert round(p.lng, 4) == 7.4652


def test_stara_baza_bez_populacji_dziala_jak_dotad(tmp_path):
    """Zalążek bazy i stare fixtury nie mają kolumny `populacja` ani wierszy
    miejscowości — wyszukiwanie po nazwie ma wtedy działać jak zawsze."""
    _z_fixture(tmp_path)
    p = geo.geokoduj(None, "Rzeszów", tresc="laweta z Rzeszowa do Krosna")
    assert p is not None and p.zrodlo == "miasto"


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


def test_kalkulacja_bez_dystansu_nie_ma_ceny():
    """`None` to nie jest „zero kilometrów", tylko „nie wiadomo ile".

    Wcześniej wchodziło przez `km or 0.0` w stawkę minimalną i zlecenie bez
    trasy dostawało „~250 zł" — liczbę, która wygląda jak wyliczenie, a jest
    wartością domyślną."""
    pusta = geo.kalkulacja(None)
    assert pusta["km_trasy"] is None
    assert pusta["szacunek_pln"] is None


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
    """Brak `km_trasy` jest informacją: nie wiemy, dokąd auto ma jechać.

    I nie ma ceny: szacunek liczony z dojazdu do bazy albo ze stawki minimalnej
    byłby liczbą, której nikt nie policzył z trasy."""
    _z_fixture(tmp_path)
    p = geo.podsumowanie(geo.geokoduj("38-500", None), None)
    assert p["km_trasy"] is None
    assert p["szacunek_pln"] is None
    assert p["km_od_bazy"] is not None


def test_nierozpoznany_drugi_punkt_zeruje_km_i_szacunek(tmp_path):
    """REALNY PRZYPADEK Z PRODUKCJI, ten, dla którego ten test istnieje.

    Post: „transport mikrosamochodu Aixam z Dębicy do Turku, 62-700. Trasa ma
    około 490 km". Dębica rozpoznana, Turek NIE — a panel pokazywał „60 km,
    ~250 zł", bo pod brakujący koniec trasy podstawiał się dojazd z bazy
    operatora (Krosno->Dębica to właśnie 60 km). Kierowca odrzuca wtedy kurs
    na 490 km, patrząc na wycenę lokalnego skoku.

    Zła liczba jest gorsza niż jej brak: brak widać, złej liczby nie."""
    plik = tmp_path / "kody.csv"
    plik.write_text("kraj,kod,miejscowosc,wojewodztwo,lat,lng\n"
                    "PL,39-200,Debica,podkarpackie,50.0517,21.4111\n"
                    "PL,38-400,Krosno,podkarpackie,49.6886,21.7706\n",
                    encoding="utf-8")
    geo.zaladuj(plik)

    odbior = geo.geokoduj("39-200", "Debica")
    dostawa = geo.geokoduj("62-700", "Turek")      # nie ma go w bazie
    assert odbior is not None and dostawa is None

    p = geo.podsumowanie(odbior, dostawa)
    assert p["km_trasy"] is None
    assert p["szacunek_pln"] is None
    # Dojazd z bazy nadal się liczy — to osobna, prawdziwa liczba. Test pilnuje,
    # żeby NIE PRZECIEKŁA na miejsce długości kursu ani do wyceny: gdyby
    # przeciekła, oba pola wyżej byłyby liczbami wyliczonymi właśnie z niej.
    assert p["km_od_bazy"] == pytest.approx(60, abs=5)


# ===========================================================================
# KIERUNEK GEOGRAFICZNY — kraj obu końców trasy względem Polski
#
# TESTY Z ZADANIA: "z Warszawy do Berlina" -> wyjazd; "z 64354 Reinheim do
# Belchatowa" -> przywoz; "Legnica -> Bogumin" -> wyjazd (PL->CZ); "Kolonia ->
# Amsterdam" -> tranzyt; punkt nierozpoznany -> "nieznany", zlecenie nadal
# widoczne (kierunek nigdy nie wycina — to sprawdzają testy API/panelu/bota,
# nie ten moduł: `geo.py` tylko liczy wartość, nie decyduje o widoczności).
# ===========================================================================
def test_punkt_niesie_kraj_z_dopasowania_kodem(tmp_path):
    _z_fixture(tmp_path)
    p = geo.geokoduj("38-400", None)          # Krosno, PL
    assert p.kraj == "PL"


def test_punkt_niesie_kraj_z_dopasowania_nazwa(tmp_path):
    _z_fixture(tmp_path)
    p = geo.geokoduj(None, "Koln")            # DE, z FIXTURE
    assert p.kraj == "DE"


def test_baza_jest_w_polsce():
    assert geo.baza().kraj == geo.KRAJ_BAZY == "PL"


@pytest.mark.parametrize("odbior_kraj,dostawa_kraj,oczekiwany", [
    ("PL", "DE", geo.KIERUNEK_WYJAZD),      # z Warszawy do Berlina
    ("DE", "PL", geo.KIERUNEK_PRZYWOZ),     # z Reinheim do Bełchatowa
    ("PL", "CZ", geo.KIERUNEK_WYJAZD),      # Legnica -> Bogumin
    ("PL", "PL", geo.KIERUNEK_KRAJOWY),
    ("DE", "NL", geo.KIERUNEK_TRANZYT),     # Kolonia -> Amsterdam
    ("DE", "DE", geo.KIERUNEK_TRANZYT),     # oba poza PL, nawet ten sam kraj
    (None, "PL", geo.KIERUNEK_NIEZNANY),
    ("PL", None, geo.KIERUNEK_NIEZNANY),
    (None, None, geo.KIERUNEK_NIEZNANY),
])
def test_kierunek_geo(odbior_kraj, dostawa_kraj, oczekiwany):
    assert geo.kierunek_geo(odbior_kraj, dostawa_kraj) == oczekiwany


def test_podsumowanie_niesie_kierunek_geo_wyjazd(tmp_path):
    _z_fixture(tmp_path)
    odbior = geo.geokoduj("38-400", None)     # Krosno, PL
    dostawa = geo.geokoduj("50667", None)     # Koln, DE
    p = geo.podsumowanie(odbior, dostawa)
    assert p["odbior_kraj"] == "PL"
    assert p["dostawa_kraj"] == "DE"
    assert p["kierunek_geo"] == geo.KIERUNEK_WYJAZD


def test_podsumowanie_niesie_kierunek_geo_krajowy(tmp_path):
    _z_fixture(tmp_path)
    odbior = geo.geokoduj("38-500", None)     # Sanok, PL
    dostawa = geo.geokoduj("35-001", None)    # Rzeszów, PL
    p = geo.podsumowanie(odbior, dostawa)
    assert p["kierunek_geo"] == geo.KIERUNEK_KRAJOWY


def test_podsumowanie_nierozpoznany_punkt_daje_kierunek_nieznany(tmp_path):
    """Punkt nierozpoznany -> "nieznany", ale `podsumowanie` samo w sobie
    niczego nie ukrywa — to warstwa wyżej (API/bot/panel) decyduje, co zrobić
    z tą wartością. Tu sprawdzamy tylko, że liczy się poprawnie."""
    _z_fixture(tmp_path)
    odbior = geo.geokoduj("38-400", None)     # Krosno, PL
    p = geo.podsumowanie(odbior, None)        # dostawa nierozpoznana
    assert p["dostawa_kraj"] is None
    assert p["kierunek_geo"] == geo.KIERUNEK_NIEZNANY


# ===========================================================================
# ODLEGŁOŚĆ PODANA PRZEZ AUTORA POSTA
#
# Autor zna trasę lepiej niż nasz geokoder — ale w tych grupach KAŻDY post ma
# kilometry, bo każdy ma przebieg auta. Wzięcie przebiegu za długość trasy
# byłoby dokładnie tym błędem, który ten moduł ma przestać popełniać, więc
# testy fałszywych trafień są tu równie ważne jak testy trafień.
# ===========================================================================
@pytest.mark.parametrize("tresc,oczekiwane", [
    # Zdanie wprost z produkcyjnego posta.
    ("transport mikrosamochodu Aixam z Debicy do Turku, 62-700. "
     "Trasa ma okolo 490 km.", 490),
    ("Trasa ma około 490 km", 490),
    # Sygnał w poprzednim zdaniu — jedna myśl zapisana dwoma zdaniami.
    ("Trasa Dębica - Turek. Około 490 km.", 490),
    ("Odległość: 1 200 km, płacę 4000 zł", 1200),
    # Tysiące z kropką: „2.500 km" to 2500, a nie 2.
    ("dystans 2.500 km, transport z Hiszpanii", 2500),
    ("490 km do przejechania", 490),
    ("trasa 490km w jedną stronę", 490),
    ("Trasa ok. 490 kilometrów", 490),
    ("kurs 120 Km, auto po wypadku", 120),
    # Pierwsze trafienie: „w jedną stronę" jest tą liczbą, o którą pyta operator.
    ("trasa 490 km w jedną stronę, 980 km tam i z powrotem", 490),
])
def test_km_wg_autora_czyta_odleglosc_z_tresci(tresc, oczekiwane):
    assert geo.km_wg_autora(tresc) == oczekiwane


@pytest.mark.parametrize("tresc", [
    "Golf 1.9 TDI, przebieg 190 tys km, nie odpala",
    "przejechane 245 000 km, silnik do remontu",
    "auto ma przebieg 3500 km, jak nowe",          # w zakresie tras, ale to licznik
    "spalanie 8l/100 km",
    "jedzie 140 km/h bez problemu",
    "potrzebna laweta, jakies 500 km stad",        # liczba bez słowa o trasie
    "cena 2500 zl",
    "",
    None,
])
def test_km_wg_autora_nie_bierze_przebiegu_ani_przypadkowych_liczb(tresc):
    """Milczenie jest tańsze niż zła liczba: „wg autora: 190000 km" przy
    przebiegu byłoby tym samym błędem, tylko z drugiej strony."""
    assert geo.km_wg_autora(tresc) is None


def test_km_wg_autora_jest_liczba_a_nie_fragmentem_posta():
    """Wprost o bezpieczeństwie: post jest wejściem od nieznanej osoby, a ta
    wartość idzie na ekran operatora i do wiadomości na Telegramie. Przez `int`
    nie przejdzie ani znacznik Markdowna, ani instrukcja dla modelu."""
    wynik = geo.km_wg_autora("trasa *490* km _pilne_ [klik](http://zle) 490 km")
    assert isinstance(wynik, int)


def test_podsumowanie_niesie_odleglosc_autora_obok_wlasnej(tmp_path):
    """Dwie liczby, dwa źródła, dwa pola. Rozjazd między nimi jest sygnałem,
    że któryś punkt złapaliśmy źle — sklejenie ich w jedno ten sygnał kasuje."""
    _z_fixture(tmp_path)
    p = geo.podsumowanie(geo.geokoduj("38-500", None), geo.geokoduj("35-001", None),
                         "Sanok -> Rzeszow, trasa ma jakies 80 km")
    assert p["km_wg_autora"] == 80
    assert p["km_trasy"] is not None and p["km_trasy"] != 80


def test_podsumowanie_bez_tresci_nie_wymysla_odleglosci(tmp_path):
    _z_fixture(tmp_path)
    assert geo.podsumowanie(geo.geokoduj("38-500", None), None)["km_wg_autora"] is None


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
