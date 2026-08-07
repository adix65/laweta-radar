"""Zapis wyniku klasyfikatora do bazy — sam INSERT, na prawdziwym Postgresie.

CAŁY PRZEBIEG (`run()`: Apify -> bramka -> model -> zapis -> alert) sprawdza
`test_przebieg_do_bazy.py`. Ten podział nie jest kosmetyczny: testy z tego pliku
przechodziły na zielono przez cały czas, gdy produkcja zapisywała 27 postów
z werdyktem modelu i kompletem NULL-i — bo wołają `_zapisz_post` wprost, a bug
siedział w tym, co do tej funkcji dociera i co się dzieje z jej wynikiem dalej.


PO CO ISTNIEJE. Pierwszy realny przebieg fetchera zapisał 26 postów
z `zrodlo_decyzji='ai'` i KOMPLETEM pól ekstrakcji na NULL. Model odpowiedział,
walidacja czytała jego odpowiedź (w logu stoi „pole pilnosc: wartość 'do 08-110
siedlce' spoza zbioru"), fetcher wypisał 7 postów jako ZLECENIE — a mimo to
w bazie nie było ani typu, ani miejsca, ani telefonu. Wynik ginął MIĘDZY
zwrotem z klasyfikatora a INSERT-em: `Decyzja` niosła tylko `czy_zlecenie`
i `powod`, a INSERT nie wymieniał kolumn z 0004_klasyfikacja.sql.

Czego NIE złapałby test offline na atrapie kursora: tego, że nazwy kolumn
realnie istnieją w tabeli i że `ON CONFLICT` zachowuje się tak, jak myślimy.
Atrapa przyjmie każdy SQL, także taki z literówką w nazwie kolumny — a to jest
druga połowa tego buga. Dlatego tutaj jest prawdziwa baza.

URUCHOMIENIE. Bez bazy testy SIĘ POMIJAJĄ (reszta repo jest offline i ma taka
zostać), więc DSN podaje się jawnie:

    TEST_DATABASE_URL=postgresql://user:haslo@localhost/laweta_test \\
        python -m pytest laweta_radar/tests/test_zapis_klasyfikacji.py

OSOBNA BAZA, NIGDY PRODUKCYJNA. Fixture KASUJE `posty`, `harmonogram`
i `feedback`, a potem zakłada je od zera — wskazanie tu produkcji skasowałoby
wszystkie zebrane posty. Dlatego nazwa bazy MUSI zawierać "test" (`_sprawdz_dsn`);
DSN bez tego nie uruchamia testów, tylko je wywala z komunikatem. Zabezpieczenie
jest prymitywne i takie ma być: ma zadziałać przy `TEST_DATABASE_URL=$DATABASE_URL`
wklejonym w pośpiechu, a to jest jedyny realny sposób, w jaki ten test mógłby
zaszkodzić.

Schemat zakłada sam test, odpalając te same pliki z api/migrations/, które idą
na produkcję — kopia CREATE TABLE w teście rozjechałaby się z migracjami przy
pierwszej zmianie i test przestałby cokolwiek chronić.

Testy offline (parametry INSERT-a, ostrzeżenie o pustej ekstrakcji) chodzą
ZAWSZE — bez nich CI bez Postgresa nie pilnowałby niczego.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.workers import classifier as c  # noqa: E402
from laweta_radar.workers import fb_fetcher as f  # noqa: E402

TERAZ = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

MIGRACJE = ("0001_posty.sql", "0002_gate.sql", "0003_fetcher.sql",
            "0004_klasyfikacja.sql", "0005_panel.sql", "0009_werdykt_modelu.sql",
            "0010_kategoria_ladunku.sql", "0011_kierunek.sql",
            "0013_kierunek_geo.sql")

# Odpowiedź modelu z KOMPLETEM pól — świadomie taka, w której żadne pole nie
# schodzi na wartość domyślną. Post z samymi domyślnymi przeszedłby ten test
# nawet wtedy, gdyby zapis gubił połowę treści.
ODPOWIEDZ_MODELU = {
    "czy_zlecenie": True,
    "typ": "holowanie",
    "odbior": {"raw": "spod Biedronki na Podkarpackiej", "kod": "38-400",
               "miasto": "Krosno"},
    "dostawa": {"raw": "warsztat w Rzeszowie", "kod": "35-001", "miasto": "Rzeszow"},
    "pojazd": {"opis": "VW Golf IV", "kategoria": "osobowy"},
    "stan": {"toczy_sie": False, "ma_kola": False, "po_wypadku": True,
             "uwagi": "po stłuczce, koło urwane"},
    "pilnosc": "teraz",
    "kontakt": {"typ": "telefon", "wartosc": "600100200"},
    "cena_sugerowana": 350.0,
    "pewnosc": 88,
    "powod": "prośba o lawetę wprost, z miejscem i telefonem",
}


def _post(tresc: str = "Potrzebna laweta, auto po stłuczce", **nadpisz) -> dict:
    dane = {"tresc": tresc, "post_url": "https://fb.com/p/1", "group_url": "g",
            "group_name": "Grupa", "author_name": "Jan", "post_date": TERAZ}
    dane.update(nadpisz)
    return dane


def _klasyfikator(odpowiedz: dict | None = None):
    """Zamockowany klasyfikator: zwraca ZWALIDOWANY wynik, jak ten prawdziwy.

    Przepuszczamy przez `zwaliduj`, a nie podajemy gotowego słownika: to ta
    funkcja ustala kształt, który dostaje warstwa zapisu, i test ma sprawdzać
    ten kształt, a nie własne wyobrażenie o nim.
    """
    wynik = c.zwaliduj(odpowiedz if odpowiedz is not None else ODPOWIEDZ_MODELU)
    return lambda tresc, grupa, jezyk: wynik


# ---------------------------------------------------------------------------
# 1. OFFLINE — komplet pól dochodzi do parametrów INSERT-a
# ---------------------------------------------------------------------------
class _Kursor:
    """Atrapa kursora: zapamiętuje zapytania i ODDAJE zadany wiersz.

    `fetchone` jest tu nie dla wygody, tylko dlatego, że zapis PYTA TERAZ BAZĘ,
    co w wierszu naprawdę stoi. Atrapa, która na `RETURNING` oddaje zawsze
    komplet, potwierdzałaby każdą wersję kodu — łącznie z tą, która zgubiła 27
    klasyfikacji. Dlatego wiersz podaje test, a nie atrapa.
    """

    def __init__(self, sklad, wiersz):
        self.sklad = sklad
        self.wiersz = wiersz

    def execute(self, sql, params=None):
        self.sklad.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.wiersz

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Polaczenie:
    """`wiersz` = to, co baza oddaje na RETURNING/SELECT: (fb_id, *ekstrakcja)."""

    def __init__(self, wiersz=None):
        self.zapytania: list = []
        self.commity = 0
        self.wiersz = wiersz

    def cursor(self):
        return _Kursor(self.zapytania, self.wiersz)

    def commit(self):
        self.commity += 1


def _wiersz_zwrotny(decyzja) -> tuple | None:
    """Wiersz, jaki oddałaby poprawnie działająca baza po tym zapisie."""
    if decyzja.wynik_ai is None:
        return ("fb1", *[None] * len(c.KOLUMNY_EKSTRAKCJI))
    dane = c.wiersz_do_zapisu(decyzja.wynik_ai, "fb1")
    return ("fb1", *[dane[k] for k in c.KOLUMNY_EKSTRAKCJI])


_DOMYSLNY = object()   # „podaj wiersz, jaki oddałaby poprawna baza"


def _zapisz(decyzja, identyfikator="fb1", log=lambda *_: None, wiersz=_DOMYSLNY):
    conn = _Polaczenie(_wiersz_zwrotny(decyzja) if wiersz is _DOMYSLNY else wiersz)
    zapis = f._zapisz_post(conn, identyfikator, _post(), decyzja, log=log)
    sql, params = conn.zapytania[0]
    return sql, params, zapis, conn


def _decyzja_ai(odpowiedz: dict | None = None) -> f.Decyzja:
    return f.decyzja_o_poscie(_post(), TERAZ, grupa="Grupa",
                              klasyfikuj=_klasyfikator(odpowiedz))


def test_decyzja_niesie_caly_wynik_klasyfikatora():
    """`Decyzja` to JEDYNA rzecz, którą pętla przebiegu podaje do zapisu.

    Wynik odczytany w `decyzja_o_poscie` i nieprzepisany do `Decyzja` przestaje
    istnieć wraz z iteracją — i dokładnie tak zginęło 26 klasyfikacji.
    """
    decyzja = _decyzja_ai()
    assert decyzja.zrodlo == "ai"
    assert decyzja.wynik_ai is not None
    assert decyzja.wynik_ai["odbior"]["miasto"] == "Krosno"
    assert decyzja.wynik_ai["kontakt"]["wartosc"] == "600100200"


def test_bez_modelu_nie_ma_wyniku():
    """Brak klasyfikatora zostawia `wynik_ai=None` — post czeka, nie udaje."""
    decyzja = f.decyzja_o_poscie(_post(), TERAZ, klasyfikuj=lambda *_: None)
    assert decyzja.zrodlo == "gate"
    assert decyzja.wynik_ai is None


def test_insert_wymienia_wszystkie_kolumny_ekstrakcji():
    """Kolumna nieobecna w INSERT-cie nie zapisze się NIGDY i nie zgłosi błędu."""
    sql, _, _, _ = _zapisz(_decyzja_ai())
    brakujace = [k for k in c.KOLUMNY_EKSTRAKCJI if k not in sql]
    assert not brakujace, f"INSERT nie wymienia kolumn: {brakujace}"
    assert "ai_model" in sql and "ai_at" in sql


def test_parametry_insertu_niosa_realne_wartosci():
    """Nazwy kolumn w SQL-u i wartości pod nimi to dwie osobne rzeczy.

    INSERT z kompletem kolumn i kompletem NULL-i wygląda w kodzie poprawnie
    i zapisuje dokładnie to samo, co INSERT bez tych kolumn.
    """
    sql, params, _, _ = _zapisz(_decyzja_ai())
    kolumny = sql.split("INSERT INTO posty (", 1)[1].split(")", 1)[0]
    nazwy = [k.strip() for k in kolumny.split(",")]
    # `gate_at` i `ai_at` liczy baza (NOW()), więc nie mają swojego %s.
    wartosci = dict(zip([n for n in nazwy if n not in ("gate_at", "ai_at")], params))

    assert wartosci["typ"] == "holowanie"
    assert wartosci["odbior_miasto"] == "Krosno"
    assert wartosci["odbior_kod"] == "38-400"
    assert wartosci["dostawa_miasto"] == "Rzeszow"
    assert wartosci["pojazd_opis"] == "VW Golf IV"
    assert wartosci["pojazd_kategoria"] == "osobowy"
    assert wartosci["stan_toczy_sie"] is False
    assert wartosci["stan_po_wypadku"] is True
    assert wartosci["pilnosc"] == "teraz"
    assert wartosci["kontakt_typ"] == "telefon"
    assert wartosci["kontakt_wartosc"] == "600100200"
    assert wartosci["pewnosc"] == 88
    assert wartosci["cena_sugerowana"] == 350.0
    assert wartosci["powod"]
    assert wartosci["zrodlo_decyzji"] == "ai"
    assert wartosci["czy_zlecenie"] is True


def test_bez_klasyfikacji_kolumny_ekstrakcji_ida_puste_ale_ida():
    """Jedna ścieżka SQL dla obu przypadków — nie ma drugiej do zapomnienia."""
    decyzja = f.decyzja_o_poscie(_post(), TERAZ, klasyfikuj=lambda *_: None)
    sql, params, zapis, _ = _zapisz(decyzja)
    assert "pojazd_opis" in sql
    assert params[params.index(decyzja.zrodlo) + 1] is False   # czy_zlecenie
    # Bez werdyktu modelu NIE ostrzegamy — nie ma czego zgubić.
    assert zapis.bez_ekstrakcji is False


def test_on_conflict_dopisuje_klasyfikacje_do_istniejacego_wiersza():
    """`DO NOTHING` gubi werdykt dla posta, który trafił do bazy przed modelem.

    To druga połowa tej samej pułapki: wiersz istnieje, więc wszystko wygląda
    dobrze, a klasyfikacja, za którą zapłacono, nie ma jak wejść.
    """
    sql, _, _, _ = _zapisz(_decyzja_ai())
    assert "ON CONFLICT (fb_id) DO UPDATE SET" in sql
    for kolumna in c.KOLUMNY_EKSTRAKCJI:
        assert f"{kolumna} = EXCLUDED.{kolumna}" in sql, (
            f"ON CONFLICT nie aktualizuje `{kolumna}` — przy powtórce posta "
            f"ta kolumna zostanie pusta na zawsze")
    # ...ale tylko wtedy, gdy naprawdę mamy co dopisać, i nie kasując werdyktu,
    # który już jest.
    assert "WHERE EXCLUDED.zrodlo_decyzji = 'ai'" in sql
    assert "posty.zrodlo_decyzji IS DISTINCT FROM 'ai'" in sql


def test_on_conflict_naprawia_wiersz_z_werdyktem_i_pusta_ekstrakcja():
    """Wiersz z `zrodlo_decyzji='ai'` i kompletem NULL-i NIE MA CZEGO CHRONIĆ.

    Sam warunek „nie ruszaj wiersza z werdyktem" zamroził 27 postów z pierwszych
    przebiegów: werdykt mają, ekstrakcji nie mają i nigdy nie dostaną, bo każdy
    kolejny zapis odbija się od tego warunku. Naprawa musi być częścią zwykłego
    zapisu — skrypt migracyjny odpalany ręcznie nie zadziała w dniu, w którym
    problem wraca.
    """
    sql, _, _, _ = _zapisz(_decyzja_ai())
    warunek = sql.split("WHERE EXCLUDED.zrodlo_decyzji = 'ai'", 1)[1]
    for kolumna in c.KOLUMNY_EKSTRAKCJI:
        assert f"posty.{kolumna} IS NULL" in warunek, (
            f"warunek naprawy nie sprawdza `{kolumna}` — wiersz z pustą "
            f"ekstrakcją zostanie pusty na zawsze")
    assert "RETURNING" in sql, "bez RETURNING nie wiadomo, co się realnie zapisało"


def test_insert_wymienia_kolumny_geo():
    """`odbior_kraj`/`dostawa_kraj`/`kierunek_geo` jadą tym samym INSERT-em co
    ekstrakcja, osobną listą (`_kolumny_geo`) — patrz `KOLUMNY_GEO`."""
    sql, _, _, _ = _zapisz(_decyzja_ai())
    for kolumna in c.KOLUMNY_GEO:
        assert kolumna in sql, f"INSERT nie wymienia kolumny geo: {kolumna}"


def test_parametry_insertu_niosa_wartosci_geo():
    """Krosno i Rzeszów są oba PL w `data/kody_eu.csv` — kierunek krajowy."""
    sql, params, _, _ = _zapisz(_decyzja_ai())
    kolumny = sql.split("INSERT INTO posty (", 1)[1].split(")", 1)[0]
    nazwy = [k.strip() for k in kolumny.split(",")]
    wartosci = dict(zip([n for n in nazwy if n not in ("gate_at", "ai_at")], params))

    assert wartosci["odbior_kraj"] == "PL"
    assert wartosci["dostawa_kraj"] == "PL"
    assert wartosci["kierunek_geo"] == "krajowy"


def test_on_conflict_aktualizuje_kolumny_geo():
    """Bez tego powtórka posta z gotową klasyfikacją zostawia kraj i kierunek
    geograficzny puste na zawsze — ta sama pułapka co przy kolumnach ekstrakcji."""
    sql, _, _, _ = _zapisz(_decyzja_ai())
    for kolumna in c.KOLUMNY_GEO:
        assert f"{kolumna} = EXCLUDED.{kolumna}" in sql, (
            f"ON CONFLICT nie aktualizuje `{kolumna}`")


def test_warunek_naprawy_nie_sprawdza_kolumn_geo():
    """DOKŁADNIE odwrotność testu dla `KOLUMNY_EKSTRAKCJI`: warunek naprawy
    (`pusta_w_bazie`) MA zostać ślepy na kolumny geo, bo `kierunek_geo` nigdy
    nie jest NULL-em, gdy klasyfikacja w ogóle zaszła — a warunek, który
    czekałby na NULL tam, nigdy by się nie spełnił."""
    sql, _, _, _ = _zapisz(_decyzja_ai())
    warunek = sql.split("WHERE EXCLUDED.zrodlo_decyzji = 'ai'", 1)[1]
    for kolumna in c.KOLUMNY_GEO:
        assert f"posty.{kolumna} IS NULL" not in warunek, (
            f"warunek naprawy sprawdza `{kolumna}` — ten wiersz nigdy by go "
            f"nie spełnił, bo `kierunek_geo` nie jest NULL-em")


# ---------------------------------------------------------------------------
# 2. OFFLINE — głośna reakcja na cichą utratę
# ---------------------------------------------------------------------------
def test_ostrzezenie_gdy_werdykt_jest_a_ekstrakcji_nie_ma():
    """DOKŁADNIE ten stan przeżył dwa przebiegi bez jednej linijki w logu.

    Wiersz w bazie ma werdykt modelu i komplet NULL-i. Poprzednia wersja pytała
    o to słownik zbudowany w Pythonie — a ten był PEŁNY, bo wynik modelu istniał;
    ginął dopiero po drodze do tabeli. Ostrzeżenie stało więc na warunku, którego
    ta awaria nie potrafiła spełnić, i milczało przy 27 wierszach.
    """
    linie: list[str] = []
    pusty = ("fb-zgubiony", *[None] * len(c.KOLUMNY_EKSTRAKCJI))
    _, _, zapis, _ = _zapisz(_decyzja_ai(), identyfikator="fb-zgubiony",
                             log=linie.append, wiersz=pusty)

    assert zapis.bez_ekstrakcji is True
    assert len(linie) == 1
    assert "OSTRZEŻENIE" in linie[0]
    assert "fb-zgubiony" in linie[0], "bez fb_id nie da się dojść, który post przepadł"


def test_ostrzezenie_gdy_baza_nie_oddaje_wiersza():
    """Zapis, po którym w tabeli nie ma nic, jest utratą — nie ciszą.

    `ON CONFLICT ... WHERE` nie zwraca wiersza, gdy warunek go nie przepuścił.
    Tą właśnie drogą uciekał zablokowany zapis: baza milczy, kod czyta to jako
    sukces, wiersz zostaje taki, jaki był.
    """
    linie: list[str] = []
    _, _, zapis, _ = _zapisz(_decyzja_ai(), identyfikator="fb-brak",
                             log=linie.append, wiersz=None)
    assert zapis.bez_ekstrakcji is True
    assert "fb-brak" in linie[0]


def test_ostrzezenie_gdy_insert_nie_ma_kolumn_ekstrakcji(monkeypatch):
    """INSERT bez kolumn ekstrakcji zapisuje sam werdykt i wygląda poprawnie.

    To jest druga ścieżka, którą `czy_zlecenie` dojeżdża do bazy, a reszta
    wyniku nie: kolumny werdyktu są w kodzie na sztywno, kolumny ekstrakcji
    liczą się w czasie działania — i pusta lista nie jest błędem dla żadnej
    linijki SQL-a.
    """
    monkeypatch.setattr(f, "_kolumny_ekstrakcji", lambda: ())
    linie: list[str] = []
    _, _, zapis, _ = _zapisz(_decyzja_ai(), identyfikator="fb-bez-kolumn",
                             log=linie.append, wiersz=("fb-bez-kolumn",))
    assert zapis.bez_ekstrakcji is True
    assert "fb-bez-kolumn" in linie[0] and "ANI JEDNEJ kolumny" in linie[0]


def test_poprawny_zapis_nie_ostrzega():
    """Ostrzeżenie, które pada zawsze, jest tym samym co brak ostrzeżenia."""
    linie: list[str] = []
    _, _, zapis, _ = _zapisz(_decyzja_ai(), log=linie.append)
    assert zapis.bez_ekstrakcji is False
    assert linie == []


def test_ubogi_wynik_modelu_to_nie_utrata():
    """Post, z którego model nic nie wyciągnął, NIE jest zgubionym wynikiem.

    `zwaliduj` wypełnia wtedy `typ`, `pilnosc`, `pojazd_kategoria`, boole stanu
    i `pewnosc` wartościami domyślnymi — i to jest poprawny, kompletny zapis.
    Ostrzeżenie w tym miejscu nauczyłoby operatora ignorować ostrzeżenia.
    """
    linie: list[str] = []
    decyzja = _decyzja_ai({"czy_zlecenie": False, "pewnosc": 0})
    _, _, zapis, _ = _zapisz(decyzja, log=linie.append)
    assert zapis.bez_ekstrakcji is False
    assert linie == []


def test_bezpiecznik_nie_pozwala_odpalic_testow_na_produkcji():
    """Bez tego jedno wklejone `TEST_DATABASE_URL=$DATABASE_URL` kasuje bazę."""
    from laweta_radar.tests import test_zapis_klasyfikacji as t

    t._sprawdz_dsn("postgresql://u:h@localhost/laweta_test")
    t._sprawdz_dsn("postgresql://u:h@localhost/test_laweta?sslmode=require")
    with pytest.raises(AssertionError, match="KASUJE"):
        t._sprawdz_dsn("postgresql://u:h@db.prod:5432/laweta")


def test_ekstrakcja_pusta_nie_myli_falszu_z_brakiem():
    """`False`, `0` i `""` to WARTOŚCI. Uznanie ich za brak dałoby ostrzeżenie
    przy każdym poprawnie zapisanym poście po wypadku."""
    assert c.ekstrakcja_pusta({k: None for k in c.KOLUMNY_EKSTRAKCJI}) is True
    assert c.ekstrakcja_pusta({}) is True
    wiersz = {k: None for k in c.KOLUMNY_EKSTRAKCJI}
    wiersz["stan_po_wypadku"] = False
    assert c.ekstrakcja_pusta(wiersz) is False
    wiersz = {k: None for k in c.KOLUMNY_EKSTRAKCJI}
    wiersz["pewnosc"] = 0
    assert c.ekstrakcja_pusta(wiersz) is False


# ---------------------------------------------------------------------------
# 3. INTEGRACJA — realny Postgres, realne migracje, odczyt po zapisie
# ---------------------------------------------------------------------------
def _dsn() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or None


def _psycopg2():
    try:
        import psycopg2
    except ImportError:
        return None
    return psycopg2


baza = pytest.mark.skipif(
    _dsn() is None or _psycopg2() is None,
    reason="brak TEST_DATABASE_URL albo psycopg2 — testy integracyjne pomijamy")


def _sprawdz_dsn(dsn: str) -> None:
    """Bezpiecznik: fixture kasuje tabele, więc pracuje TYLKO na bazie testowej.

    Warunek stoi na nazwie bazy, bo to jedyna część DSN-a, którą człowiek
    świadomie wybiera przy zakładaniu bazy pod testy. `TEST_DATABASE_URL`
    wskazany na produkcję ma tu paść, a nie skasować tydzień zbierania.
    """
    nazwa = dsn.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in nazwa.lower():
        raise AssertionError(
            f"TEST_DATABASE_URL wskazuje bazę {nazwa!r} — ten test KASUJE tabele "
            f"`posty`, `harmonogram` i `feedback`. Załóż osobną bazę z 'test' "
            f"w nazwie (np. `createdb laweta_test`) i wskaż ją tutaj.")


@pytest.fixture
def polaczenie():
    """Świeża tabela `posty` na każdy test, założona MIGRACJAMI z repo."""
    psycopg2 = _psycopg2()
    _sprawdz_dsn(_dsn())
    conn = psycopg2.connect(_dsn())
    katalog = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "api", "migrations")
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS feedback, harmonogram, posty CASCADE")
        for plik in MIGRACJE:
            with open(os.path.join(katalog, plik), encoding="utf-8") as fh:
                cur.execute(fh.read())
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _wiersz_z_bazy(conn, fb_id: str) -> dict:
    kolumny = (*c.KOLUMNY_EKSTRAKCJI, *c.KOLUMNY_GEO, "zrodlo_decyzji", "czy_zlecenie",
               "status", "ai_model", "ai_at")
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(kolumny)} FROM posty WHERE fb_id = %s",  # noqa: S608
                    (fb_id,))
        wiersz = cur.fetchone()
    assert wiersz is not None, f"posta {fb_id} nie ma w bazie"
    return dict(zip(kolumny, wiersz))


@baza
def test_integracja_komplet_pol_jest_w_bazie_po_zapisie(polaczenie):
    """WYMAGANIE Z ZGŁOSZENIA: zamockowany klasyfikator zwraca komplet pól,
    a odczyt z bazy po zapisie pokazuje je WSZYSTKIE niepuste.

    To jedyny test, który sprawdza całą drogę: model -> `zwaliduj` ->
    `decyzja_o_poscie` -> `Decyzja` -> INSERT -> tabela. Każdy przystanek na tej
    trasie już raz zgubił wynik.
    """
    decyzja = _decyzja_ai()
    linie: list[str] = []
    zapis = f._zapisz_post(polaczenie, "fb-integracja", _post(), decyzja,
                           log=linie.append)
    assert zapis.bez_ekstrakcji is False and linie == []
    # `Zapis.wiersz` niesie to, co ODDAŁA BAZA — i to z niego powstaje alert.
    assert zapis.wiersz["odbior_miasto"] == "Krosno"

    w = _wiersz_z_bazy(polaczenie, "fb-integracja")
    puste = [k for k in c.KOLUMNY_EKSTRAKCJI if w[k] is None]
    assert not puste, f"po zapisie NULL-em wyszły: {puste}"

    # I te same wartości, nie „cokolwiek niepustego".
    assert w["typ"] == "holowanie"
    assert w["odbior_kod"] == "38-400" and w["odbior_miasto"] == "Krosno"
    assert w["dostawa_kod"] == "35-001"
    assert w["pojazd_opis"] == "VW Golf IV"
    assert w["stan_toczy_sie"] is False and w["stan_po_wypadku"] is True
    assert w["pilnosc"] == "teraz"
    assert w["kontakt_typ"] == "telefon" and w["kontakt_wartosc"] == "600100200"
    assert int(w["pewnosc"]) == 88
    assert float(w["cena_sugerowana"]) == 350.0
    # Krosno i Rzeszów są oba PL — kierunek krajowy, policzony przy TYM SAMYM
    # zapisie, z tych samych `odbior_kod`/`dostawa_kod` co wyżej.
    assert w["odbior_kraj"] == "PL" and w["dostawa_kraj"] == "PL"
    assert w["kierunek_geo"] == "krajowy"
    assert w["zrodlo_decyzji"] == "ai" and w["czy_zlecenie"] is True
    assert w["ai_model"] and w["ai_at"] is not None


@baza
def test_integracja_pytanie_z_zgloszenia_zwraca_wypelnione_kolumny(polaczenie):
    """Dokładnie ten SELECT, który w zgłoszeniu pokazał same zera.

        SELECT zrodlo_decyzji, count(*), count(typ), count(pewnosc), count(odbior_raw)
        FROM posty GROUP BY 1;
    """
    for i in range(3):
        f._zapisz_post(polaczenie, f"fb-{i}", _post(), _decyzja_ai(),
                       log=lambda *_: None)

    with polaczenie.cursor() as cur:
        cur.execute("SELECT zrodlo_decyzji, count(*), count(typ), count(pewnosc), "
                    "count(odbior_raw) FROM posty GROUP BY 1")
        wiersze = cur.fetchall()

    assert wiersze == [("ai", 3, 3, 3, 3)]


@baza
def test_integracja_klasyfikacja_dopisuje_sie_do_wiersza_bez_werdyktu(polaczenie):
    """Post zapisany PRZED klasyfikacją dostaje ją przy kolejnym podejściu.

    Bez tego kolejka `idx_posty_do_klasyfikacji` nie ma jak się opróżnić:
    ponowienie płaci za model, a `ON CONFLICT DO NOTHING` wyrzuca wynik.
    """
    przed = f.decyzja_o_poscie(_post(), TERAZ, klasyfikuj=lambda *_: None)
    f._zapisz_post(polaczenie, "fb-pozniej", _post(), przed, log=lambda *_: None)
    assert _wiersz_z_bazy(polaczenie, "fb-pozniej")["typ"] is None

    f._zapisz_post(polaczenie, "fb-pozniej", _post(), _decyzja_ai(),
                   log=lambda *_: None)
    w = _wiersz_z_bazy(polaczenie, "fb-pozniej")
    assert [k for k in c.KOLUMNY_EKSTRAKCJI if w[k] is None] == []
    assert w["zrodlo_decyzji"] == "ai"
    assert w["status"] == "nowe"


@baza
def test_integracja_powtorka_nie_cofa_statusu_operatora(polaczenie):
    """Operator wziął post na telefon — ponowny zapis nie wraca go do kolejki."""
    f._zapisz_post(polaczenie, "fb-dzwonie", _post(),
                   f.decyzja_o_poscie(_post(), TERAZ, klasyfikuj=lambda *_: None),
                   log=lambda *_: None)
    with polaczenie.cursor() as cur:
        cur.execute("UPDATE posty SET status = 'dzwonie' WHERE fb_id = 'fb-dzwonie'")
    polaczenie.commit()

    f._zapisz_post(polaczenie, "fb-dzwonie", _post(), _decyzja_ai(),
                   log=lambda *_: None)
    w = _wiersz_z_bazy(polaczenie, "fb-dzwonie")
    assert w["status"] == "dzwonie"
    assert w["typ"] == "holowanie"        # ekstrakcja mimo to się dopisała


@baza
def test_integracja_gotowego_werdyktu_nie_nadpisujemy(polaczenie):
    """Wiersz z werdyktem modelu jest już rozliczony — druga klasyfikacja go nie rusza."""
    f._zapisz_post(polaczenie, "fb-gotowy", _post(), _decyzja_ai(),
                   log=lambda *_: None)
    inny = _decyzja_ai({**ODPOWIEDZ_MODELU, "typ": "transport", "pewnosc": 10})
    f._zapisz_post(polaczenie, "fb-gotowy", _post(), inny, log=lambda *_: None)

    w = _wiersz_z_bazy(polaczenie, "fb-gotowy")
    assert w["typ"] == "holowanie" and int(w["pewnosc"]) == 88


@baza
def test_integracja_wiersz_z_werdyktem_bez_ekstrakcji_daje_sie_naprawic(polaczenie):
    """STAN Z PRODUKCJI: 27 wierszy z `zrodlo_decyzji='ai'` i kompletem NULL-i.

    Poprzednia wersja odbijała taki zapis (warunek `posty.zrodlo_decyzji IS
    DISTINCT FROM 'ai'` chronił wiersz, który nie miał czego chronić) i NIE
    zgłaszała tego — bo pytała słownik z pamięci, a ten był pełny. Wiersz
    zostawał pusty na zawsze, a log wyglądał na czysty.
    """
    with polaczenie.cursor() as cur:
        cur.execute(
            "INSERT INTO posty (fb_id, tresc, grupa_url, zrodlo_decyzji, "
            "czy_zlecenie, status) VALUES ('fb-kaleki', 'laweta', 'g', 'ai', "
            "true, 'nowe')")
    polaczenie.commit()

    linie: list[str] = []
    zapis = f._zapisz_post(polaczenie, "fb-kaleki", _post(), _decyzja_ai(),
                           log=linie.append)

    w = _wiersz_z_bazy(polaczenie, "fb-kaleki")
    assert [k for k in c.KOLUMNY_EKSTRAKCJI if w[k] is None] == []
    assert w["typ"] == "holowanie" and int(w["pewnosc"]) == 88
    assert zapis.bez_ekstrakcji is False and linie == []


@baza
def test_integracja_dedup_nie_zamraza_wierszy_do_naprawy(polaczenie):
    """Post uszkodzony MUSI wypaść z dedupu, inaczej naprawa jest nieosiągalna.

    `_istniejace_id` zwraca komplet fb_id — gdyby uszkodzony wiersz był w nim
    tylko jako „znany", pętla przebiegu pomijałaby go jako duplikat i ścieżka
    naprawcza nigdy by się nie wykonała.
    """
    with polaczenie.cursor() as cur:
        cur.execute(
            "INSERT INTO posty (fb_id, tresc, grupa_url, zrodlo_decyzji, "
            "czy_zlecenie, status) VALUES "
            "('fb-kaleki', 'a', 'g', 'ai', true, 'nowe'), "
            "('fb-zdrowy', 'b', 'g', 'ai', true, 'nowe'), "
            "('fb-bramka', 'c', 'g', 'gate', false, 'smiec')")
        cur.execute("UPDATE posty SET typ = 'holowanie', pewnosc = 70 "
                    " WHERE fb_id = 'fb-zdrowy'")
    polaczenie.commit()

    istniejace, do_naprawy = f._istniejace_id(polaczenie)
    assert istniejace == {"fb-kaleki", "fb-zdrowy", "fb-bramka"}
    assert do_naprawy == {"fb-kaleki"}


@baza
def test_integracja_preflight_wykrywa_brak_migracji_klasyfikatora(polaczenie):
    """Zapis dotyka kolumn z 0003 I 0004 — brak którejkolwiek ma paść PRZED Apify.

    Wcześniej preflight pytał tylko o `zrodlo_decyzji`, więc baza z 0003 bez 0004
    przechodziła kontrolę, a potem wywalała się na KAŻDYM poście — czyli już po
    zapłaceniu za pobranie.
    """
    assert f._brakujace_migracje(polaczenie) == []

    with polaczenie.cursor() as cur:
        cur.execute("ALTER TABLE posty DROP COLUMN pewnosc")
    polaczenie.commit()

    braki = f._brakujace_migracje(polaczenie)
    assert len(braki) == 1 and "0004_klasyfikacja.sql" in braki[0]


@baza
def test_integracja_raport_widzi_werdykt_modelu(polaczenie):
    """Macierz pomyłek stoi na parze (`zrodlo_decyzji`, `czy_zlecenie`).

    Ten SELECT jest kopią tego, co robi `scripts/raport_gate.py::_pobierz` —
    i to on wypisywał „BRAK DANYCH", dopóki werdykt czytano z `ai_zlecenie`.
    """
    f._zapisz_post(polaczenie, "fb-zlecenie", _post(), _decyzja_ai(),
                   log=lambda *_: None)
    f._zapisz_post(polaczenie, "fb-smiec", _post(),
                   _decyzja_ai({"czy_zlecenie": False, "pewnosc": 5}),
                   log=lambda *_: None)
    f._zapisz_post(polaczenie, "fb-bez-modelu", _post(),
                   f.decyzja_o_poscie(_post(), TERAZ, klasyfikuj=lambda *_: None),
                   log=lambda *_: None)

    with polaczenie.cursor() as cur:
        cur.execute("SELECT fb_id, CASE WHEN zrodlo_decyzji = 'ai' "
                    "THEN czy_zlecenie END FROM posty ORDER BY fb_id")
        werdykty = dict(cur.fetchall())

    assert werdykty["fb-zlecenie"] is True
    assert werdykty["fb-smiec"] is False
    # NULL, nie False — „modelu nie pytano" nie może uchodzić za „model odrzucił",
    # bo wtedy macierz pokazuje zero fałszywych odrzuceń dla każdej bramki.
    assert werdykty["fb-bez-modelu"] is None
