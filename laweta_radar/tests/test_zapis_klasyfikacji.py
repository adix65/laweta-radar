"""Zapis wyniku klasyfikatora do bazy — jedyny test w repo, który dotyka Postgresa.

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
            "0004_klasyfikacja.sql", "0005_panel.sql", "0009_werdykt_modelu.sql")

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
    """Atrapa kursora zapamiętująca ostatnie zapytanie i jego parametry."""

    def __init__(self, sklad):
        self.sklad = sklad

    def execute(self, sql, params=None):
        self.sklad.append((" ".join(sql.split()), params))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Polaczenie:
    def __init__(self):
        self.zapytania: list = []
        self.commity = 0

    def cursor(self):
        return _Kursor(self.zapytania)

    def commit(self):
        self.commity += 1


def _zapisz(decyzja, identyfikator="fb1", log=lambda *_: None):
    conn = _Polaczenie()
    pusto = f._zapisz_post(conn, identyfikator, _post(), decyzja, log=log)
    sql, params = conn.zapytania[-1]
    return sql, params, pusto, conn


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
    sql, params, pusto, _ = _zapisz(decyzja)
    assert "pojazd_opis" in sql
    assert params[params.index(decyzja.zrodlo) + 1] is False   # czy_zlecenie
    # Bez werdyktu modelu NIE ostrzegamy — nie ma czego zgubić.
    assert pusto is False


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


# ---------------------------------------------------------------------------
# 2. OFFLINE — głośna reakcja na cichą utratę
# ---------------------------------------------------------------------------
def test_ostrzezenie_gdy_werdykt_jest_a_ekstrakcji_nie_ma():
    """DOKŁADNIE ten stan przeżył pierwszy przebieg bez jednej linijki w logu.

    Odtwarzamy go wprost: `Decyzja` z `zrodlo='ai'`, ale bez `wynik_ai` — czyli
    to, co produkowała poprzednia wersja `decyzja_o_poscie`.
    """
    kaleka = f.Decyzja(zrodlo="ai", czy_zlecenie=True, jezyk="pl", status="nowe",
                       stale=False, powod="zlecenie", pytano_model=True,
                       wynik_ai=None)
    linie: list[str] = []
    _, _, pusto, _ = _zapisz(kaleka, identyfikator="fb-zgubiony", log=linie.append)

    assert pusto is True
    assert len(linie) == 1
    assert "OSTRZEŻENIE" in linie[0]
    assert "fb-zgubiony" in linie[0], "bez fb_id nie da się dojść, który post przepadł"


def test_poprawny_zapis_nie_ostrzega():
    """Ostrzeżenie, które pada zawsze, jest tym samym co brak ostrzeżenia."""
    linie: list[str] = []
    _, _, pusto, _ = _zapisz(_decyzja_ai(), log=linie.append)
    assert pusto is False
    assert linie == []


def test_ubogi_wynik_modelu_to_nie_utrata():
    """Post, z którego model nic nie wyciągnął, NIE jest zgubionym wynikiem.

    `zwaliduj` wypełnia wtedy `typ`, `pilnosc`, `pojazd_kategoria`, boole stanu
    i `pewnosc` wartościami domyślnymi — i to jest poprawny, kompletny zapis.
    Ostrzeżenie w tym miejscu nauczyłoby operatora ignorować ostrzeżenia.
    """
    linie: list[str] = []
    decyzja = _decyzja_ai({"czy_zlecenie": False, "pewnosc": 0})
    _, _, pusto, _ = _zapisz(decyzja, log=linie.append)
    assert pusto is False
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
    kolumny = (*c.KOLUMNY_EKSTRAKCJI, "zrodlo_decyzji", "czy_zlecenie",
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
    pusto = f._zapisz_post(polaczenie, "fb-integracja", _post(), decyzja,
                           log=linie.append)
    assert pusto is False and linie == []

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
