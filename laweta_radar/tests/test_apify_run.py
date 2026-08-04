"""Offline testy `apify_run` i `apify_credits` — bez sieci i bez klucza.

Testujemy tylko to, co da się sprawdzić bez Apify: rozpoznawanie kształtu
odpowiedzi i ostrzeganie o polach, których actor nie zna. Ta druga rzecz jest
jedyną obroną przed najdroższym cichym błędem w tym repo: literówka w nazwie pola
wejściowego nie zwraca błędu — zwraca run BEZ filtra, za pełną cenę.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.workers import apify_credits, apify_run  # noqa: E402


# ---------------------------------------------------------------------------
# apify_run — identyfikator actora i czas trwania
# ---------------------------------------------------------------------------
def test_normalizuj_actor_przyjmuje_obie_notacje():
    """API chce tyldy, ludzie (i strona Apify) piszą ukośnik."""
    assert apify_run.normalizuj_actor("apify/facebook-groups-scraper") == \
        "apify~facebook-groups-scraper"
    assert apify_run.normalizuj_actor("apify~facebook-groups-scraper") == \
        "apify~facebook-groups-scraper"
    assert apify_run.normalizuj_actor("  memo23/x  ") == "memo23~x"


def test_trwanie_ze_znacznikow_apify():
    run = {"startedAt": "2026-08-04T12:00:00.000Z",
           "finishedAt": "2026-08-04T12:02:30.000Z"}
    assert apify_run._trwanie(run) == 150.0


def test_trwanie_niekompletnego_runu_to_None():
    """Run przerwany po naszej stronie nie ma `finishedAt` — to normalne."""
    assert apify_run._trwanie({"startedAt": "2026-08-04T12:00:00.000Z"}) is None
    assert apify_run._trwanie({}) is None
    assert apify_run._trwanie({"startedAt": "wczoraj", "finishedAt": "dziś"}) is None


# ---------------------------------------------------------------------------
# apify_run — ostrzeżenie o nieznanych polach wejściowych
# ---------------------------------------------------------------------------
SCHEMAT = {"properties": {"startUrls": {"type": "array"},
                          "resultsLimit": {"type": "integer"}}}


def test_nieznane_pola_wskazuje_literowke():
    braki = apify_run.nieznane_pola(
        {"startUrls": [], "resultsLimit": 30, "onlyPostsNewerThen": "1 day"},
        SCHEMAT)
    assert braki == ["onlyPostsNewerThen"]


def test_komplet_znanych_pol_nie_alarmuje():
    assert apify_run.nieznane_pola({"startUrls": [], "resultsLimit": 30},
                                   SCHEMAT) == []


def test_brak_schematu_to_brak_wiedzy_a_nie_brak_problemu():
    """Actor bez opublikowanego schematu nie pozwala niczego stwierdzić. Zwracamy
    pustą listę, ale NIE dlatego, że jest dobrze — dlatego, że nie wiadomo."""
    assert apify_run.nieznane_pola({"cokolwiek": 1}, {}) == []
    assert apify_run.nieznane_pola({"cokolwiek": 1}, {"properties": {}}) == []
    assert apify_run.nieznane_pola({"cokolwiek": 1}, {"properties": "śmieć"}) == []


# ---------------------------------------------------------------------------
# apify_credits — kształt odpowiedzi /users/me/limits
# ---------------------------------------------------------------------------
ODPOWIEDZ = json.loads("""
{"data": {
  "monthlyUsageCycle": {"startAt": "2026-08-01T00:00:00.000Z",
                        "endAt": "2026-08-31T23:59:59.999Z"},
  "limits": {"maxMonthlyUsageUsd": 5},
  "current": {"monthlyUsageUsd": 1.25}
}}
""")


def test_odczyt_zuzycia_konta():
    z = apify_credits.z_odpowiedzi(ODPOWIEDZ)
    assert z.zuzyte_usd == 1.25
    assert z.limit_usd == 5.0
    assert z.zostalo_usd == 3.75
    assert "1.2500" in z.opis() and "5.00" in z.opis()


def test_nieznany_ksztalt_odpowiedzi_to_None_a_nie_wyjatek():
    """Apify może przemianować pole. Pomiar ma wtedy powiedzieć „nie umiem
    odczytać salda" i policzyć koszt z runów — a nie wywalić się w połowie serii,
    za którą już zapłaciliśmy."""
    assert apify_credits.z_odpowiedzi({}) is None
    assert apify_credits.z_odpowiedzi({"data": {}}) is None
    assert apify_credits.z_odpowiedzi({"data": {"current": {"cos": 1}}}) is None


def test_brak_limitu_nie_psuje_odczytu_zuzycia():
    """Na płatnym planie limitu może nie być. Zużycie nadal jest tą liczbą,
    o którą chodzi."""
    z = apify_credits.z_odpowiedzi(
        {"data": {"current": {"monthlyUsageUsd": 2.0}}})
    assert z.zuzyte_usd == 2.0 and z.limit_usd is None and z.zostalo_usd is None
    assert "limit nieznany" in z.opis()


def test_koszt_runu_z_usageTotalUsd():
    assert apify_credits.koszt_runu({"usageTotalUsd": 0.0123}) == 0.0123
    assert apify_credits.koszt_runu({}) is None
    assert apify_credits.koszt_runu({"usageTotalUsd": "brak"}) is None
