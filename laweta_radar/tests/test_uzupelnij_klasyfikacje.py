"""Naprawa starych wierszy — na prawdziwej bazie, bo tylko tam widać, czy weszła.

Skrypt istnieje dla 27 wierszy, które w produkcji mają werdykt modelu i komplet
NULL-i. Test sprawdza dokładnie to, czego nie widać z kodu: że zapytanie
wybierające trafia w te wiersze i tylko w nie, oraz że po naprawie kolumny są
NAPRAWDĘ wypełnione — czytane z tabeli, nie ze słownika, który poszedł do UPDATE-a.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.scripts import uzupelnij_klasyfikacje as u  # noqa: E402
from laweta_radar.services import llm  # noqa: E402
from laweta_radar.tests.test_przebieg_do_bazy import (  # noqa: E402
    MIGRACJE, ODPOWIEDZ_MODELU)
from laweta_radar.tests.test_zapis_klasyfikacji import (  # noqa: E402
    _dsn, _psycopg2, _sprawdz_dsn, baza)
from laweta_radar.workers import classifier as c  # noqa: E402


@pytest.fixture
def baza_z_uszkodzonymi(monkeypatch):
    """Trzy wiersze: uszkodzony, zdrowy i taki, którego model nigdy nie widział."""
    psycopg2 = _psycopg2()
    _sprawdz_dsn(_dsn())
    conn = psycopg2.connect(_dsn())
    katalog = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "api", "migrations")
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS feedback, harmonogram, powiadomienia, "
                    "posty CASCADE")
        for plik in MIGRACJE:
            with open(os.path.join(katalog, plik), encoding="utf-8") as fh:
                cur.execute(fh.read())
        cur.execute(
            "INSERT INTO posty (fb_id, tresc, grupa_url, grupa_nazwa, "
            "zrodlo_decyzji, czy_zlecenie, status) VALUES "
            "('kaleki', 'Potrzebna laweta Krosno', 'g', 'Grupa', 'ai', true, 'nowe'),"
            "('zdrowy', 'Potrzebna laweta Rzeszow', 'g', 'Grupa', 'ai', true, 'nowe'),"
            "('bramka', 'Sprzedam felgi', 'g', 'Grupa', 'gate', false, 'smiec')")
        cur.execute("UPDATE posty SET typ = 'holowanie', pewnosc = 70 "
                    " WHERE fb_id = 'zdrowy'")
    conn.commit()

    monkeypatch.setattr(u.settings, "DATABASE_URL", _dsn())
    monkeypatch.setattr(llm, "model_domyslny", lambda: "model-testowy")
    monkeypatch.setattr(llm, "zapytaj", lambda *_a, **_k: ODPOWIEDZ_MODELU)
    try:
        yield conn
    finally:
        conn.close()


@baza
def test_wybiera_tylko_wiersze_z_werdyktem_i_pusta_ekstrakcja(baza_z_uszkodzonymi):
    """Zdrowy wiersz naprawiany drugi raz to zapłacone tokeny za nic, a wiersz
    bez werdyktu modelu to inna sprawa (kolejka do klasyfikacji, nie naprawa)."""
    wiersze = u.wiersze_do_naprawy(baza_z_uszkodzonymi, limit=100,
                                   tylko_zlecenia=False)
    assert [w[0] for w in wiersze] == ["kaleki"]


@baza
def test_naprawa_wypelnia_kolumny_w_tabeli(baza_z_uszkodzonymi):
    linie: list[str] = []
    assert u.run(limit=100, sucho=False, tylko_zlecenia=False,
                 log=linie.append) == 0

    with baza_z_uszkodzonymi.cursor() as cur:
        cur.execute(f"SELECT {', '.join(c.KOLUMNY_EKSTRAKCJI)} FROM posty "  # noqa: S608
                    f"WHERE fb_id = 'kaleki'")
        w = dict(zip(c.KOLUMNY_EKSTRAKCJI, cur.fetchone()))

    assert [k for k, v in w.items() if v is None] == []
    assert w["odbior_miasto"] == "Krosno" and int(w["pewnosc"]) == 88
    assert any("1 uzupełnionych" in linia for linia in linie)


@baza
def test_sucho_nie_wola_modelu(baza_z_uszkodzonymi, monkeypatch):
    """Pierwszy krok operatora nie ma prawa nic kosztować."""
    def _nie_wolno(*_a, **_k):
        raise AssertionError("--sucho zapytało model")

    monkeypatch.setattr(llm, "zapytaj", _nie_wolno)
    linie: list[str] = []
    assert u.run(limit=100, sucho=True, tylko_zlecenia=False, log=linie.append) == 0
    assert any("nie wołam modelu" in linia for linia in linie)

    with baza_z_uszkodzonymi.cursor() as cur:
        cur.execute("SELECT typ FROM posty WHERE fb_id = 'kaleki'")
        assert cur.fetchone()[0] is None
