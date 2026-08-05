"""Offline testy scripts/raport_gate.py — bez bazy, na podstawionych wierszach.

Raport jest narzędziem DECYZYJNYM: to na jego podstawie ktoś przestawi
GATE_TRYB na "aktywny" i od tej chwili bramka zacznie kasować posty na dobre.
Raport, który myli się na korzyść bramki, jest gorszy niż jego brak — daje
zielone światło i wygląda przy tym wiarygodnie.

Dlatego testujemy tu przede wszystkim to, czego raport NIE MA prawa zrobić:
policzyć fałszywe odrzucenia za nisko i wystawić zgodę na przełączenie.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

_SCIEZKA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "raport_gate.py")
_spec = importlib.util.spec_from_file_location("raport_gate", _SCIEZKA)
raport = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(raport)


def wiersz(*, werdykt=True, punkty=0, powod="prosba wprost", ai=None, tresc="post testowy"):
    return {"fb_id": "x", "tresc": tresc, "post_url": None, "grupa": "g",
            "werdykt": werdykt, "punkty": punkty, "powod": powod,
            "tryb": "cien", "ai": ai}


def _wypisz(wiersze, prog=5, ma_werdykt=True, limit=50) -> str:
    """Uruchom raport i przechwyć to, co zobaczy człowiek."""
    buf, stary = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        raport._raport(wiersze, prog, ma_werdykt, limit)
    finally:
        sys.stdout = stary
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Przeliczanie progu bez dotykania słownika
# ---------------------------------------------------------------------------
def test_prog_rusza_tylko_decyzje_z_punktacji():
    """Warstwy 1-3 są od progu NIEZALEŻNE — zmiana progu nie może ich odwrócić."""
    wygaszony = wiersz(werdykt=False, powod="wygaszone")
    assert raport._werdykt_przy_progu(wygaszony, 0) is False
    assert raport._werdykt_przy_progu(wygaszony, 99) is False

    przepuszczony = wiersz(werdykt=True, powod="prosba wprost")
    assert raport._werdykt_przy_progu(przepuszczony, 99) is True


def test_prog_przelicza_decyzje_punktowe():
    w = wiersz(werdykt=False, punkty=4, powod="punktacja 4 < prog 5")
    assert raport._werdykt_przy_progu(w, 5) is False
    assert raport._werdykt_przy_progu(w, 4) is True
    assert raport._werdykt_przy_progu(w, 3) is True


def test_brak_decyzji_bramki_nie_udaje_werdyktu():
    """Post, którego bramka nie widziała, nie może wpaść do macierzy jako "zgoda"."""
    assert raport._werdykt_przy_progu(wiersz(werdykt=None), 5) is None


# ---------------------------------------------------------------------------
# Macierz pomyłek
# ---------------------------------------------------------------------------
def test_macierz_liczy_falszywe_odrzucenia_w_wlasciwej_komorce():
    m = raport._macierz([
        wiersz(werdykt=True, ai=True),                      # tp
        wiersz(werdykt=True, ai=False),                     # fp — grosz
        wiersz(werdykt=False, powod="wygaszone", ai=True),  # fn — KURS
        wiersz(werdykt=False, powod="wygaszone", ai=False),  # tn
    ], prog=5)
    assert m == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}


def test_posty_bez_werdyktu_ai_nie_licza_sie_do_macierzy():
    """Brak werdyktu modelu to NIE jest zgoda — inaczej macierz byłaby zawsze czysta."""
    m = raport._macierz([wiersz(werdykt=False, powod="wygaszone", ai=None)] * 10, prog=5)
    assert m == {"tp": 0, "fp": 0, "fn": 0, "tn": 0}


# ---------------------------------------------------------------------------
# Werdykt raportu — tu podejmuje się decyzję o przełączeniu
# ---------------------------------------------------------------------------
def test_jedno_falszywe_odrzucenie_blokuje_przelaczenie():
    wiersze = [wiersz(werdykt=True, ai=True) for _ in range(500)]
    wiersze.append(wiersz(werdykt=False, powod="wygaszone", ai=True,
                          tresc="Kupiłem auto w Kolonii, kto przywiezie?"))
    out = _wypisz(wiersze)
    assert "NIE PRZEŁĄCZAJ" in out
    assert "MOŻNA PRZEŁĄCZYĆ" not in out
    assert "Kupiłem auto w Kolonii" in out, "treść zabitego zlecenia musi być widoczna"


def test_zero_pomylek_na_malej_probce_to_ZA_MALO():
    """Zero na pięćdziesięciu postach to najbardziej prawdopodobny wynik także dla
    bramki, która kasuje co dwudzieste zlecenie. Raport nie ma prawa tego przemilczeć."""
    out = _wypisz([wiersz(werdykt=True, ai=True) for _ in range(50)])
    assert "ZA WCZEŚNIE" in out
    assert "MOŻNA PRZEŁĄCZYĆ" not in out


def test_zgoda_dopiero_przy_zerze_i_duzej_probce():
    wiersze = [wiersz(werdykt=True, ai=True) for _ in range(raport.MIN_PROBKA)]
    wiersze += [wiersz(werdykt=False, powod="wygaszone", ai=False) for _ in range(50)]
    out = _wypisz(wiersze)
    assert "MOŻNA PRZEŁĄCZYĆ" in out


def test_brak_kolumny_ai_nie_udaje_zera_pomylek():
    """Najgroźniejszy możliwy fałsz tego raportu: "0 fałszywych odrzuceń", bo
    nie było z czym porównywać. Ma powiedzieć wprost, że nie wie."""
    out = _wypisz([wiersz(werdykt=False, powod="wygaszone")] * 300, ma_werdykt=False)
    assert "NIEDOSTĘPNA" in out and "NIEPEŁNY" in out
    assert "MOŻNA PRZEŁĄCZYĆ" not in out


def test_kolumny_sa_ale_zaden_post_nie_ma_werdyktu_modelu():
    """Kolumny są, macierz pusta — to NIE to samo co brak migracji.

    Ta gałąź istnieje po to, żeby wskazać drugą możliwą przyczynę: klasyfikator
    przeszedł, a jego wynik nie dojechał do bazy. Dokładnie tak wyglądał raport,
    zanim fetcher zaczął zapisywać komplet pól — i właśnie dlatego bug przeżył
    cały przebieg niezauważony.
    """
    out = _wypisz([wiersz(werdykt=True, ai=None)] * 30, ma_werdykt=True)
    assert "BRAK DANYCH" in out
    assert "zrodlo_decyzji='ai'" in out
    assert "OSTRZEŻENIE" in out          # gdzie szukać, gdy to jednak utrata wyniku
    assert "MOŻNA PRZEŁĄCZYĆ" not in out


def test_decyzje_spoza_trybu_cienia_sa_oflagowane():
    """W trybie aktywnym odrzucony post NIE trafia do AI, więc jego brak
    w macierzy nie znaczy "zgoda" — to musi być widać."""
    w = wiersz(werdykt=False, powod="wygaszone", ai=None)
    w["tryb"] = "aktywny"
    assert "POZA trybem cienia" in _wypisz([w])


def test_odmiana_liczebnika():
    """Raport czyta człowiek — "1 zleceń" podkopuje zaufanie do reszty liczb."""
    assert raport._zlecen(1) == "1 zlecenie"
    assert raport._zlecen(3) == "3 zlecenia"
    assert raport._zlecen(5) == "5 zleceń"
    assert raport._zlecen(12) == "12 zleceń"
    assert raport._zlecen(22) == "22 zlecenia"


def test_powody_punktowe_grupuja_sie_w_jeden_wiersz():
    """Bez tego zestawienie miałoby osobny wiersz na każdą możliwą sumę punktów."""
    assert raport._kategoria_powodu("punktacja 3 < prog 5") == "punktacja"
    assert raport._kategoria_powodu("ogloszenie o pracy") == "ogloszenie o pracy"


def test_przelicz_uzywa_aktualnego_slownika():
    """--przelicz ma nadpisać STARĄ decyzję wynikiem dzisiejszych wzorców."""
    w = wiersz(werdykt=False, punkty=0, powod="wygaszone",
               tresc="Potrzebuję lawety z Krosna do Rzeszowa, golf nie odpala")
    raport._przelicz([w], prog=5)
    assert w["werdykt"] is True and w["powod"] == "prosba wprost"


def test_raport_bez_zadnej_decyzji_bramki_mowi_co_zrobic():
    out = _wypisz([wiersz(werdykt=None)] * 5)
    assert "Brak postów z decyzją bramki" in out and "fetcher" in out


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
