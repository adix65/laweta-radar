"""Offline testy `apify_run` — bez sieci i bez klucza.

Testujemy tylko to, co da się sprawdzić bez Apify: rozpoznawanie kształtu
odpowiedzi i ostrzeganie o polach, których actor nie zna. Ta druga rzecz jest
jedyną obroną przed najdroższym cichym błędem w tym repo: literówka w nazwie pola
wejściowego nie zwraca błędu — zwraca run BEZ filtra, za pełną cenę.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.workers import apify_run  # noqa: E402


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
