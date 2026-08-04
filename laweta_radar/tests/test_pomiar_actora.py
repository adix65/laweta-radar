"""Offline testy INTERPRETACJI pomiaru actora.

Cały pomiar sprowadza się do jednego zdania: „ŚCIEŻKA A czy B". Na tym zdaniu
stoi kształt fetchera i decyzja, ile kont Apify kupić, więc reguła, która je
wypisuje, musi być sprawdzalna BEZ wydawania pieniędzy — inaczej jedyną metodą
weryfikacji byłby kolejny płatny przebieg.

Testujemy dwie rzeczy, o które najłatwiej się potknąć:
  1. że PŁASKI wynik (grupa zbyt cicha) NIE jest mylony z działającym filtrem —
     to najgroźniejszy fałszywy pozytyw, bo brzmi jak sukces,
  2. że post starszy niż zadane okno ZAWSZE oznacza ścieżkę B, nawet gdy liczby
     poza tym wyglądają ładnie.

Bez sieci: dane wejściowe to zwykłe struktury, których nie odróżnisz od tych,
które przyszłyby z Apify.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.scripts import pomiar_actora as p  # noqa: E402

TERAZ = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _w(okno: str, ile: int, najstarszy_h: float | None, blad: str | None = None):
    return p.WynikOkna(okno=okno, godziny=p.okno_na_godziny(okno), ile=ile,
                       najstarszy_h=najstarszy_h, blad=blad)


# ---------------------------------------------------------------------------
# Przeliczanie okien i czytanie czasu z itemu
# ---------------------------------------------------------------------------
def test_okno_na_godziny():
    assert p.okno_na_godziny("7 days") == 168.0
    assert p.okno_na_godziny("1 day") == 24.0
    assert p.okno_na_godziny("12 hours") == 12.0
    assert p.okno_na_godziny("1 hour") == 1.0
    assert abs(p.okno_na_godziny("30 minutes") - 0.5) < 1e-9
    assert p.okno_na_godziny("wczoraj") is None
    assert p.okno_na_godziny("") is None


def test_czas_posta_rozne_formaty():
    """Actor zwraca czas raz jako ISO, raz jako unix — obie formy muszą działać.

    Nazwa pola wraca razem z czasem, bo trafia do raportu: prompt 2 ma ją dostać
    zmierzoną, a nie odkrywać ponownie na produkcji.
    """
    czas, pole = p.czas_posta({"time": "2026-08-04T10:00:00.000Z"})
    assert pole == "time" and czas == datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)

    czas, pole = p.czas_posta({"timestamp": 1754301600})          # sekundy
    assert pole == "timestamp" and czas.tzinfo is not None

    czas_ms, _ = p.czas_posta({"timestamp": 1754301600000})       # milisekundy
    assert czas_ms == czas                                        # ten sam moment

    assert p.czas_posta({"tresc": "brak daty"}) == (None, None)


def test_wieki_postow_liczy_tylko_te_z_data():
    itemy = [
        {"time": (TERAZ - timedelta(hours=2)).isoformat()},
        {"time": (TERAZ - timedelta(hours=10)).isoformat()},
        {"tresc": "post bez daty"},
    ]
    najstarszy, najnowszy, pole, ile = p.wieki_postow(itemy, teraz=TERAZ)
    assert round(najstarszy) == 10 and round(najnowszy) == 2
    assert pole == "time"
    assert ile == 2          # post bez daty NIE udaje, że ma wiek


# ---------------------------------------------------------------------------
# PYTANIE 1 — ścieżka A / B / „?"
# ---------------------------------------------------------------------------
def test_sciezka_A_gdy_liczba_postow_maleje_ze_zwezaniem_okna():
    wyniki = [_w("7 days", 30, 160.0), _w("1 day", 18, 22.0),
              _w("12 hours", 9, 11.0), _w("1 hour", 2, 0.8),
              _w("30 minutes", 1, 0.3)]
    r = p.rozstrzygnij_okno(wyniki)
    assert r.sciezka == "A" and r.jednoznaczne


def test_sciezka_A_gdy_limit_scina_liczbe_ale_ogon_mlodnieje():
    """Grupa ruchliwa: limit ścina liczbę do stałej, więc DZIAŁANIE filtra widać
    wyłącznie po wieku najstarszego posta. Bez tej reguły najlepszy możliwy wynik
    pomiaru (grupa z realnym ruchem) lądowałby jako „nierozstrzygnięty"."""
    wyniki = [_w("7 days", 30, 150.0), _w("1 day", 30, 23.0),
              _w("12 hours", 30, 11.5), _w("1 hour", 30, 0.9)]
    r = p.rozstrzygnij_okno(wyniki)
    assert r.sciezka == "A"


def test_sciezka_B_gdy_post_jest_starszy_niz_okno():
    """Najmocniejszy dowód: pole przyjęte i zignorowane. Reszta liczb wygląda
    poprawnie (maleją!), a mimo to musi wyjść B."""
    wyniki = [_w("7 days", 30, 160.0), _w("1 day", 25, 150.0),
              _w("1 hour", 20, 140.0)]
    r = p.rozstrzygnij_okno(wyniki)
    assert r.sciezka == "B"
    assert "poza oknem" in r.powod or "IGNOROWANE" in r.powod


def test_sciezka_B_gdy_actor_odrzuca_waskie_okna():
    wyniki = [_w("7 days", 30, 160.0), _w("1 day", 20, 22.0),
              _w("12 hours", 0, None, blad="Input is not valid"),
              _w("1 hour", 0, None, blad="Input is not valid"),
              _w("30 minutes", 0, None, blad="Input is not valid")]
    r = p.rozstrzygnij_okno(wyniki)
    assert r.sciezka == "B" and "poniżej 12 h" in r.powod


def test_plaski_wynik_to_NIEROZSTRZYGNIETE_a_nie_sciezka_A():
    """Grupa zbyt cicha: filtr działający i ignorowany dają IDENTYCZNY wynik.

    To jest fałszywy pozytyw, na którym najłatwiej się przejechać — wszystkie
    posty mieszczą się w oknie, więc nic nie wygląda podejrzanie.
    """
    wyniki = [_w("7 days", 3, 0.4), _w("1 day", 3, 0.4),
              _w("12 hours", 3, 0.4), _w("1 hour", 3, 0.4)]
    r = p.rozstrzygnij_okno(wyniki)
    assert r.sciezka == "?" and not r.jednoznaczne
    assert "PŁASKIE" in r.powod


def test_martwa_grupa_same_zera_to_tez_NIEROZSTRZYGNIETE():
    wyniki = [_w("7 days", 0, None), _w("1 day", 0, None), _w("1 hour", 0, None)]
    assert p.rozstrzygnij_okno(wyniki).sciezka == "?"


def test_same_bledy_to_awaria_dostepu_a_nie_werdykt():
    wyniki = [_w(o, 0, None, blad="401 Unauthorized") for o in p.OKNA]
    r = p.rozstrzygnij_okno(wyniki)
    assert r.sciezka == "?" and "awaria dostępu" in r.powod


def test_brak_wynikow():
    assert p.rozstrzygnij_okno([]).sciezka == "?"


def test_tolerancja_nie_uznaje_zaokraglenia_za_dziurawy_filtr():
    """Post 24,5 h przy oknie 24 h to zaokrąglenie znacznika czasu, nie awaria
    filtra. Bez marginesu KAŻDY działający filtr wychodziłby na ścieżkę B."""
    wyniki = [_w("7 days", 30, 160.0), _w("1 day", 12, 24.5), _w("1 hour", 2, 0.9)]
    assert p.rozstrzygnij_okno(wyniki).sciezka == "A"


# ---------------------------------------------------------------------------
# PYTANIE 2 — limit globalny czy per grupa
# ---------------------------------------------------------------------------
def _l(grup: int, ile: int, na_grupe: dict, limit: int = 30):
    return p.WynikLimitu(ile_grup_w_wejsciu=grup, limit=limit, ile=ile,
                         na_grupe=dict(na_grupe))


def test_limit_globalny_gdy_trzy_grupy_daja_tyle_co_jedna():
    jedna = _l(1, 30, {"A": 30})
    trzy = _l(3, 30, {"A": 22, "B": 8})
    r = p.rozstrzygnij_limit(jedna, trzy)
    assert r.sciezka == "GLOBALNY" and "NIEBEZPIECZNE" in r.powod


def test_limit_per_grupa_gdy_batch_skaluje_sie_z_liczba_grup():
    jedna = _l(1, 30, {"A": 30})
    trzy = _l(3, 90, {"A": 30, "B": 30, "C": 30})
    r = p.rozstrzygnij_limit(jedna, trzy)
    assert r.sciezka == "PER GRUPA"


def test_wynik_posredni_nie_udaje_rozstrzygniecia():
    jedna = _l(1, 30, {"A": 30})
    trzy = _l(3, 55, {"A": 30, "B": 25})
    r = p.rozstrzygnij_limit(jedna, trzy)
    assert r.sciezka == "?" and "pośredni" in r.powod


def test_brak_kompletu_wywolan():
    assert p.rozstrzygnij_limit(None, _l(3, 90, {})).sciezka == "?"
    assert p.rozstrzygnij_limit(_l(1, 30, {}), None).sciezka == "?"


def test_blad_wywolania_nie_daje_werdyktu():
    trzy = _l(3, 0, {})
    trzy.blad = "TIMED-OUT"
    assert p.rozstrzygnij_limit(_l(1, 30, {"A": 30}), trzy).sciezka == "?"


def test_klucz_grupy_radzi_sobie_z_zagniezdzonym_obiektem():
    assert p.klucz_grupy({"groupUrl": "https://fb.com/groups/a"}) == \
        "https://fb.com/groups/a"
    assert p.klucz_grupy({"group": {"name": "Pomoc drogowa"}}) == "Pomoc drogowa"
    assert p.klucz_grupy({"tresc": "x"}) == "(nieznana)"


# ---------------------------------------------------------------------------
# PYTANIE 3 — cena posta
# ---------------------------------------------------------------------------
def test_koszt_na_post():
    assert p.koszt_na_post(0.5, 100) == 0.005
    assert p.koszt_na_post(None, 100) is None
    assert p.koszt_na_post(0.5, 0) is None       # nie dzielimy przez zero itemów


# ---------------------------------------------------------------------------
# Raport — ma powstać także wtedy, gdy pomiar się nie udał
# ---------------------------------------------------------------------------
def _raport(**nadpisz):
    baza = dict(
        wyniki_okien=[_w("7 days", 30, 160.0), _w("1 day", 10, 22.0)],
        werdykt_okna=p.Rozstrzygniecie("A", "bo tak"),
        jedna=None, trzy=None,
        werdykt_limitu=p.Rozstrzygniecie("?", "nie mierzone"),
        saldo_przed=None, saldo_po=None,
        koszt_z_runow=0.20, itemow_razem=40, pole_czasu="time",
        build="1.2.3", grupa="https://fb.com/groups/test",
        data_pomiaru="2026-08-04 12:00 UTC",
    )
    baza.update(nadpisz)
    return p.raport_md(**baza)


def test_raport_niesie_werdykt_i_cene():
    md = _raport()
    assert "ŚCIEŻKA A" in md
    assert "0.00500" in md                       # 0.20 USD / 40 postów
    assert "2026-08-04 12:00 UTC" in md
    assert "`time`" in md                        # pole z czasem dla prompta 2


def test_raport_powstaje_takze_bez_kosztu_i_bez_werdyktu():
    """Nieudany pomiar MUSI zostawić plik. Brak pliku wygląda jak „jeszcze nie
    mierzyliśmy" i kusi, żeby pisać fetchera bez pomiaru — a to jest dokładnie
    ten błąd, przed którym ten krok ma bronić."""
    md = _raport(koszt_z_runow=None, itemow_razem=0,
                 werdykt_okna=p.Rozstrzygniecie("?", "grupa zbyt cicha"),
                 pole_czasu=None, build="")
    assert "ŚCIEŻKA ?" in md
    assert "Nie udało się policzyć ceny" in md
    assert "NIE ZNALEZIONO" in md
    assert "pomiar trzeba powtórzyć" in md


def test_raport_ostrzega_gdy_dwie_metody_liczenia_kosztu_sie_rozjezdzaja():
    saldo_przed = p.apify_credits.Zuzycie(1.00, 5.0, "", "")
    saldo_po = p.apify_credits.Zuzycie(1.40, 5.0, "", "")      # 0.40 z licznika
    md = _raport(saldo_przed=saldo_przed, saldo_po=saldo_po,
                 koszt_z_runow=0.20, itemow_razem=40)          # 0.20 z runów
    assert "rozjeżdżają" in md
    assert "WYŻSZĄ z dwóch liczb" in md
