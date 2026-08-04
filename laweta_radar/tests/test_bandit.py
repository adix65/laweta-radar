"""Offline testy services/bandit.py — Thompson Sampling.

Najważniejszy test w tym pliku to `test_posterior_zgodny_z_oryginalem`: liczy
posterior WPROST ze wzorów przepisanych z repo źródłowego i porównuje z naszą
funkcją. Reszta modułu została przy przenoszeniu przebudowana (źródło danych
z SQL na argument — patrz docstring modułu), więc bez tego testu nikt nie ma jak
sprawdzić, czy przy okazji nie zmieniła się matematyka. A zmieniłaby się cicho:
bandyta z popsutym posteriorem nadal zwraca sensownie wyglądające klucze.

Bez sieci i bez bazy — moduł celowo nie dotyka psycopg2.
"""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.services import bandit  # noqa: E402


# ---------------------------------------------------------------------------
# Wzory PRZEPISANE Z ORYGINAŁU (services/bandit.py -> pick_technique).
# Celowo w postaci dosłownej, z magicznymi liczbami zamiast stałych: gdyby ktoś
# podmienił stałą w module, ten test ma tego NIE zauważyć po swojej stronie
# i pokazać rozjazd.
# ---------------------------------------------------------------------------
def _posterior_oryginalny(local_sent, local_success, g_sent, g_success, boost):
    if local_sent >= 15:
        alpha = 1 + local_success
        beta = 1 + (local_sent - local_success)
    else:
        alpha = 1 + local_success + 0.3 * g_success
        beta = 1 + (local_sent - local_success) + 0.3 * max(g_sent - g_success, 0)
    if boost:
        alpha += 0.5
    return alpha, beta


PRZYPADKI = [
    # (proby_lok, sukcesy_lok, proby_glob, sukcesy_glob, preferowany)
    (0, 0, 0, 0, False),          # czysty prior Beta(1,1)
    (0, 0, 0, 0, True),           # sam bonus
    (14, 7, 100, 40, False),      # tuż PONIŻEJ progu — globalne się liczą
    (15, 7, 100, 40, False),      # próg — globalne przestają się liczyć
    (16, 16, 100, 40, True),      # same sukcesy + bonus
    (3, 0, 50, 0, False),         # globalne bez sukcesów
    (2, 1, 10, 30, False),        # sukcesów globalnych WIĘCEJ niż prób (max(...,0))
    (200, 3, 0, 0, False),        # dużo prób, mało sukcesów
]


def test_posterior_zgodny_z_oryginalem():
    """Nasz posterior == wzór z oryginału, dla każdego przypadku brzegowego."""
    for pl, sl, pg, sg, pref in PRZYPADKI:
        assert bandit.posterior(pl, sl, pg, sg, pref) == \
               _posterior_oryginalny(pl, sl, pg, sg, pref), f"rozjazd dla {(pl, sl, pg, sg, pref)}"


def test_stale_nie_zmienily_sie():
    """Stałe są częścią zachowania, nie szczegółem — pilnujemy ich wprost."""
    assert bandit.MIN_LOKALNYCH_PROB == 15
    assert bandit.WAGA_GLOBALNA == 0.3
    assert bandit.BONUS_PRIOR == 0.5
    assert bandit._MIN_PARAM == 0.01


def test_prog_zaufania_odcina_globalne():
    """Od MIN_LOKALNYCH_PROB w górę dane globalne nie mają już wpływu."""
    bez = bandit.posterior(15, 5, proby_globalne=0, sukcesy_globalne=0)
    z_globalnymi = bandit.posterior(15, 5, proby_globalne=999, sukcesy_globalne=999)
    assert bez == z_globalnymi


def test_ponizej_progu_globalne_maja_wplyw():
    bez = bandit.posterior(5, 2, proby_globalne=0, sukcesy_globalne=0)
    z_globalnymi = bandit.posterior(5, 2, proby_globalne=100, sukcesy_globalne=60)
    assert z_globalnymi[0] > bez[0]


def test_beta_nie_schodzi_ponizej_podlogi():
    """betavariate rozkłada się przy zerze — podłoga 0.01 chroni przed ValueError."""
    for _ in range(200):
        assert 0.0 <= bandit._losuj_beta(0.0, 0.0) <= 1.0
        assert 0.0 <= bandit._losuj_beta(-5.0, -5.0) <= 1.0


# ---------------------------------------------------------------------------
# wybierz — zachowanie
# ---------------------------------------------------------------------------
def test_pusta_lista_zwraca_none_zamiast_wyjatku():
    """JEDYNE świadome odstępstwo od oryginału — patrz docstring `wybierz`.

    Oryginał wchodził tu w `random.choice([])` i rzucał IndexError. U nas pusta
    lista grup to stan normalny (świeży klon: wszystko `unverified`), a wyjątek
    z crona łamałby zasadę obowiązującą w całym repo.
    """
    assert bandit.wybierz([]) is None
    assert bandit.wybierz([], {"a": {"proby": 1, "sukcesy": 1}}) is None


def test_jedyny_kandydat_wygrywa_zawsze():
    assert bandit.wybierz(["jedyna"]) == "jedyna"


def test_kandydat_bez_statystyk_nie_wywala():
    """Nowa grupa nie ma jeszcze historii — ma dostać czysty prior, nie KeyError."""
    wynik = bandit.wybierz(["znana", "nowa"], {"znana": {"proby": 10, "sukcesy": 5}})
    assert wynik in ("znana", "nowa")


def test_wyrazna_przewaga_wygrywa_w_wiekszosci():
    """Bandyta ma EKSPLOATOWAĆ, gdy dane są jednoznaczne.

    Nie żądamy 100%: Thompson z założenia czasem sprawdza słabszego i to jest
    jego zaleta, nie usterka. Sprawdzamy więc kierunek, nie determinizm.
    """
    random.seed(1)
    stat = {
        "dobra": {"proby": 100, "sukcesy": 80},
        "slaba": {"proby": 100, "sukcesy": 2},
    }
    trafienia = [bandit.wybierz(["dobra", "slaba"], stat) for _ in range(200)]
    assert trafienia.count("dobra") > 190


def test_mniej_danych_znaczy_wiecej_eksploracji():
    """Przy tym samym STOSUNKU sukcesów mała próba ma być eksplorowana częściej.

    To jest cały powód, dla którego bierzemy Thompsona zamiast „wybierz
    najlepszego": grupa, która trzy razy nic nie dowiozła, nie może zostać
    skreślona na zawsze, bo trzy runy to nie jest dowód. Ta sama grupa po
    trzydziestu pustych runach — już tak.

    Sprawdzamy WŁASNOŚĆ (mało danych => więcej prób dla słabszego), a nie
    konkretną liczbę trafień: ta zależy od ziarna i przypięcie się do niej
    dawałoby test, który pilnuje generatora losowego zamiast bandyty.
    """
    def ile_eksploracji(proby_l, sukcesy_l, proby_g, sukcesy_g, seed):
        random.seed(seed)
        stat = {"lepsza": {"proby": proby_l, "sukcesy": sukcesy_l},
                "gorsza": {"proby": proby_g, "sukcesy": sukcesy_g}}
        wyniki = [bandit.wybierz(["lepsza", "gorsza"], stat) for _ in range(2000)]
        return wyniki.count("gorsza")

    malo = ile_eksploracji(4, 3, 3, 0, seed=7)      # ten sam stosunek 75% / 0%...
    duzo = ile_eksploracji(40, 30, 30, 0, seed=7)   # ...ale dziesięć razy więcej danych

    assert malo > duzo, "więcej danych powinno ZMNIEJSZAĆ eksplorację, nie zwiększać"
    assert malo > 0, "przy trzech pustych runach bandyta nie może skreślać grupy na zawsze"


def test_bonus_prior_pomaga_na_starcie():
    random.seed(3)
    trafienia = [bandit.wybierz(["a", "b"], preferowani=["a"]) for _ in range(400)]
    assert trafienia.count("a") > trafienia.count("b")


# ---------------------------------------------------------------------------
# rozdziel_budzet — nadbudowa
# ---------------------------------------------------------------------------
def test_rozdzial_budzetu_sumuje_sie_do_budzetu():
    """Nie wolno zgubić ani wymyślić runu — budżet Apify jest wspólny z drugim
    systemem, więc każdy run policzony podwójnie to run zabrany komuś innemu."""
    random.seed(11)
    podzial = bandit.rozdziel_budzet(["a", "b", "c"], 50)
    assert sum(podzial.values()) == 50


def test_rozdzial_budzetu_faworyzuje_skuteczna_grupe():
    random.seed(13)
    stat = {
        "dowozi": {"proby": 60, "sukcesy": 40},
        "martwa": {"proby": 60, "sukcesy": 1},
    }
    podzial = bandit.rozdziel_budzet(["dowozi", "martwa"], 100, stat)
    assert podzial.get("dowozi", 0) > podzial.get("martwa", 0)


def test_rozdzial_bez_kandydatow_i_bez_budzetu():
    """Oba stany są normalne (brak grup / brak kredytu) — mają dać pusto, nie wyjątek."""
    assert bandit.rozdziel_budzet([], 10) == {}
    assert bandit.rozdziel_budzet(["a"], 0) == {}
    assert bandit.rozdziel_budzet(["a"], -5) == {}


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
