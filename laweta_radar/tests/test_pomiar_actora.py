"""
Offline testy laweta_radar/scripts/pomiar_actora.py — jednorazowego pomiaru actora.

Skrypt jest jednorazowy, ale jego WNIOSKI nie: `docs/POMIAR-ACTORA.md` decyduje
o kształcie `_build_actor_input`, o tym, czy wolno batchować grupy, i o budżecie
w `POSTY_NA_DOBE`. Błąd w regule rozstrzygającej nie objawia się wywrotką — objawia
się pewnym siebie zdaniem „ŚCIEŻKA A" w dokumencie, na którym ktoś potem zbuduje
fetcher pobierający w kółko te same posty. Dlatego regułę testujemy na danych
podstawionych, zamiast ufać jednemu przebiegowi na żywym actorze.

Wszystko tutaj jest BEZ SIECI: podstawiamy gotowe `Wynik`-i (to, co skrypt wyciąga
z Apify) i sprawdzamy, co z nich wyczytał.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

# Skrypt świadomie NIE jest modułem pakietu (to narzędzie jednorazowe, nie część
# systemu — patrz jego docstring), więc ładujemy go ze ścieżki.
_SCIEZKA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "pomiar_actora.py")
_spec = importlib.util.spec_from_file_location("pomiar_actora", _SCIEZKA)
pa = importlib.util.module_from_spec(_spec)
# Rejestracja PRZED wykonaniem modułu: @dataclass rozwiązuje adnotacje przez
# sys.modules[cls.__module__], więc bez tego dekorator wywala się na AttributeError.
sys.modules["pomiar_actora"] = pa
_spec.loader.exec_module(pa)

from laweta_radar.workers import apify_credits as ac  # noqa: E402


def _teraz() -> datetime:
    return datetime.now(timezone.utc)


def _wynik(etykieta, *, okno=None, itemow=0, najstarszy_h=None, najnowszy_h=None,
           ids=None, status="SUCCEEDED", blad="", limit=20, grupy=None,
           koszt=None, trwanie=None, per_grupa=None):
    """Skrót do budowania `Wynik`-a takiego, jaki zwróciłby realny przebieg."""
    return pa.Wynik(
        etykieta=etykieta,
        grupy=grupy or ["https://www.facebook.com/groups/111"],
        limit=limit,
        okno=okno,
        okno_s=pa.sekundy_okna(okno) if okno else None,
        itemow=itemow,
        najstarszy_h=najstarszy_h,
        najnowszy_h=najnowszy_h,
        id_postow=list(ids or []),
        per_grupa=dict(per_grupa or {}),
        trwanie_s=trwanie,
        status=status,
        blad=blad,
        koszt_saldo_usd=koszt,
    )


# ---------------------------------------------------------------------------
# Czytanie okna i czasu postów
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tekst, sekundy", [
    ("30 minutes", 1800), ("1 hour", 3600), ("12 hours", 43200),
    ("1 day", 86400), ("7 days", 604800), ("2 weeks", 1209600),
    ("  3   days  ", 259200), ("1 Day", 86400),
])
def test_sekundy_okna(tekst, sekundy):
    assert pa.sekundy_okna(tekst) == sekundy


@pytest.mark.parametrize("tekst", ["", "wczoraj", "kilka dni", "1 fortnight", None])
def test_sekundy_okna_nieczytelne(tekst):
    assert pa.sekundy_okna(tekst) is None


def test_czas_z_iso_i_epoki():
    oczekiwany = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    assert pa._na_czas("2026-05-10T12:00:00.000Z") == oczekiwany
    assert pa._na_czas("2026-05-10T12:00:00+00:00") == oczekiwany
    assert pa._na_czas(int(oczekiwany.timestamp())) == oczekiwany
    assert pa._na_czas(int(oczekiwany.timestamp() * 1000)) == oczekiwany
    assert pa._na_czas(str(int(oczekiwany.timestamp()))) == oczekiwany


def test_czas_odrzuca_liczby_ktore_nie_sa_data():
    """`likes: 42` nie może zostać wzięte za znacznik czasu (42 -> rok 1970)."""
    assert pa._na_czas(42) is None
    assert pa._na_czas(0) is None
    assert pa._na_czas(True) is None
    assert pa._na_czas("bardzo dawno") is None
    # Data z przyszłości też jest podejrzana — post nie może być jeszcze nieopublikowany.
    assert pa._na_czas((_teraz() + timedelta(days=3)).isoformat()) is None


def test_pole_czasu_wybierane_po_liczbie_trafien():
    """Wybieramy pole z NAJWIĘKSZĄ liczbą poprawnych dat, nie pierwsze pasujące."""
    baza = _teraz() - timedelta(hours=3)
    itemy = [
        {"timestamp": 7, "time": (baza - timedelta(hours=i)).isoformat()}
        for i in range(5)
    ]
    # `timestamp` wygląda jak pole z czasem, ale niesie liczbę komentarzy.
    czasy, pole = pa._czasy_postow(itemy)
    assert pole == "time"
    assert len(czasy) == 5


def test_pole_czasu_znajduje_nazwe_spoza_listy():
    """Nazwa pola z czasem to niewiadoma tego actora — musimy ją ODKRYĆ, nie zgadnąć."""
    baza = _teraz() - timedelta(hours=2)
    itemy = [{"kiedyToPoszlo": (baza - timedelta(minutes=i)).isoformat()} for i in range(4)]
    czasy, pole = pa._czasy_postow(itemy)
    assert pole == "kiedyToPoszlo"
    assert len(czasy) == 4


def test_klucz_grupy_i_przypisanie_itemu():
    assert pa.klucz_grupy("https://www.facebook.com/groups/123456/") == "123456"
    assert pa.klucz_grupy("https://facebook.com/groups/pomoc.drogowa.pdk?ref=x") \
        == "pomoc.drogowa.pdk"
    item = {"url": "https://www.facebook.com/groups/123456/posts/999/"}
    assert pa._grupa_itema(item, ["123456", "777"]) == "123456"
    assert pa._grupa_itema({"url": "https://example.com"}, ["123456"]) == ""


def test_wejscie_actora_jest_minimalne():
    """Wejście niesie WYŁĄCZNIE to, o co pytamy — każde pole więcej to kolejna niewiadoma."""
    bez = pa.wejscie_actora(["https://fb.com/groups/1"], 20, None)
    assert bez == {"startUrls": [{"url": "https://fb.com/groups/1"}], "resultsLimit": 20}
    z_oknem = pa.wejscie_actora(["https://fb.com/groups/1"], 20, "1 hour")
    assert z_oknem["onlyPostsNewerThan"] == "1 hour"
    inne_pole = pa.wejscie_actora(["https://fb.com/groups/1"], 20, "1 hour",
                                  pole_okna="postsNewerThan")
    assert "onlyPostsNewerThan" not in inne_pole
    assert inne_pole["postsNewerThan"] == "1 hour"


def test_w_oknie_ma_luz_na_rozjazd_zegarow():
    """Post na granicy okna nie może uchodzić za dowód, że okno nie działa."""
    tuz_za = _wynik("1 hour", okno="1 hour", itemow=1, najstarszy_h=1.02)
    assert tuz_za.w_oknie is True
    wyraznie_poza = _wynik("1 hour", okno="1 hour", itemow=1, najstarszy_h=9.0)
    assert wyraznie_poza.w_oknie is False
    bez_danych = _wynik("1 hour", okno="1 hour", itemow=0)
    assert bez_danych.w_oknie is None


def test_zaczete_minuty_zaokraglaja_w_gore():
    """Hipoteza „płacimy za ZACZĘTĄ minutę" wymaga zaokrąglenia w górę, nie do najbliższej."""
    assert _wynik("x", trwanie=1.0).zaczete_minuty == 1
    assert _wynik("x", trwanie=61.0).zaczete_minuty == 2
    assert _wynik("x", trwanie=120.0).zaczete_minuty == 2
    assert _wynik("x", trwanie=None).zaczete_minuty is None


# ---------------------------------------------------------------------------
# PYTANIE 1 — ścieżka A / B
# ---------------------------------------------------------------------------
def test_sciezka_a_gdy_okno_realnie_tnie():
    wyniki = [
        _wynik("kontrola (bez pola)", itemow=20, najstarszy_h=100.0, najnowszy_h=0.4,
               ids=[f"p{i}" for i in range(20)]),
        _wynik("7 days", okno="7 days", itemow=20, najstarszy_h=100.0, najnowszy_h=0.4,
               ids=[f"p{i}" for i in range(20)]),
        _wynik("1 day", okno="1 day", itemow=12, najstarszy_h=20.0, najnowszy_h=0.4,
               ids=[f"p{i}" for i in range(12)]),
        _wynik("12 hours", okno="12 hours", itemow=6, najstarszy_h=10.0, najnowszy_h=0.4,
               ids=[f"p{i}" for i in range(6)]),
        _wynik("1 hour", okno="1 hour", itemow=2, najstarszy_h=0.8, najnowszy_h=0.4,
               ids=["p0", "p1"]),
        _wynik("30 minutes", okno="30 minutes", itemow=0),
    ]
    sciezka, jednostka, linie = pa._rozstrzygnij_okno(wyniki)
    assert sciezka == "ŚCIEŻKA A"
    assert jednostka == "1 hour"
    assert any("MIEŚCI SIĘ" in l for l in linie)


def test_sciezka_b_gdy_godzina_i_doba_daja_te_same_posty():
    """Kryterium z zadania wprost: identyczny wynik dla „1 hour" i „1 day"."""
    ids = [f"p{i}" for i in range(20)]
    wyniki = [
        _wynik("kontrola (bez pola)", itemow=20, najstarszy_h=100.0, ids=ids),
        _wynik("1 day", okno="1 day", itemow=20, najstarszy_h=100.0, ids=ids),
        _wynik("1 hour", okno="1 hour", itemow=20, najstarszy_h=100.0, ids=ids),
    ]
    sciezka, jednostka, linie = pa._rozstrzygnij_okno(wyniki)
    assert sciezka == "ŚCIEŻKA B"
    assert "ignorowana" in jednostka
    assert any("DOKŁADNIE te same posty" in l for l in linie)


def test_rowna_liczba_bez_rownego_zestawu_nie_przesadza_o_b():
    """Dwa okna mogą dać po 20 postów przez nasycenie resultsLimit — to nie dowód."""
    wyniki = [
        _wynik("kontrola (bez pola)", itemow=20, najstarszy_h=100.0,
               ids=[f"p{i}" for i in range(20)]),
        _wynik("1 day", okno="1 day", itemow=20, najstarszy_h=23.0,
               najnowszy_h=0.2, ids=[f"d{i}" for i in range(20)]),
        _wynik("1 hour", okno="1 hour", itemow=20, najstarszy_h=0.9,
               najnowszy_h=0.1, ids=[f"h{i}" for i in range(20)]),
    ]
    sciezka, jednostka, _ = pa._rozstrzygnij_okno(wyniki)
    assert sciezka == "ŚCIEŻKA A"
    assert jednostka == "1 hour"


def test_sciezka_b_gdy_wask_okno_zwraca_zestaw_kontrolny():
    ids = [f"p{i}" for i in range(20)]
    wyniki = [
        _wynik("kontrola (bez pola)", itemow=20, najstarszy_h=90.0, ids=ids),
        _wynik("7 days", okno="7 days", itemow=20, najstarszy_h=90.0, ids=ids),
        _wynik("30 minutes", okno="30 minutes", itemow=20, najstarszy_h=90.0, ids=ids),
    ]
    sciezka, _, linie = pa._rozstrzygnij_okno(wyniki)
    assert sciezka == "ŚCIEŻKA B"
    assert any("ten sam zestaw" in l for l in linie)


def test_sciezka_b_gdy_actor_odrzuca_pole():
    wyniki = [
        _wynik("kontrola (bez pola)", itemow=20, najstarszy_h=50.0,
               ids=[f"p{i}" for i in range(20)]),
        _wynik("7 days", okno="7 days", status="", blad="HTTPStatusError: 400"),
        _wynik("1 day", okno="1 day", status="", blad="HTTPStatusError: 400"),
    ]
    sciezka, jednostka, linie = pa._rozstrzygnij_okno(wyniki)
    assert sciezka == "ŚCIEŻKA B"
    assert "odrzucone" in jednostka
    assert any("nie przyjmuje" in l for l in linie)


def test_sciezka_b_gdy_najwezsze_okno_oddaje_stare_posty():
    """Bez kontroli nadal rozstrzygamy: post starszy niż okno = okno nie tnie."""
    wyniki = [
        _wynik("1 day", okno="1 day", itemow=20, najstarszy_h=20.0,
               ids=[f"d{i}" for i in range(20)]),
        _wynik("1 hour", okno="1 hour", itemow=15, najstarszy_h=48.0,
               ids=[f"h{i}" for i in range(15)]),
    ]
    sciezka, _, linie = pa._rozstrzygnij_okno(wyniki)
    assert sciezka == "ŚCIEŻKA B"
    assert any("NIE tnie" in l for l in linie)


def test_nierozstrzygniete_gdy_wszystko_padlo():
    """Cisza w danych NIE może zostać ogłoszona jako ścieżka A."""
    wyniki = [
        _wynik("kontrola (bez pola)", status="", blad="ConnectTimeout"),
        _wynik("1 day", okno="1 day", status="", blad="ConnectTimeout"),
    ]
    sciezka, jednostka, _ = pa._rozstrzygnij_okno(wyniki)
    assert sciezka == "NIEROZSTRZYGNIĘTE"
    assert jednostka == "nie ustalono"


def test_nierozstrzygniete_gdy_grupa_jest_pusta():
    wyniki = [
        _wynik("kontrola (bez pola)", itemow=0),
        _wynik("1 day", okno="1 day", itemow=0),
        _wynik("1 hour", okno="1 hour", itemow=0),
    ]
    sciezka, _, linie = pa._rozstrzygnij_okno(wyniki)
    assert sciezka == "NIEROZSTRZYGNIĘTE"
    assert any("za cicha" in l for l in linie)


# ---------------------------------------------------------------------------
# PYTANIE 2 — resultsLimit przy wielu grupach
# ---------------------------------------------------------------------------
def test_limit_per_grupa():
    jedna = _wynik("1 grupa", itemow=30, limit=30)
    trzy = _wynik("3 grupy", itemow=88, limit=30, grupy=["a", "b", "c"],
                  per_grupa={"a": 30, "b": 30, "c": 28})
    werdykt, linie = pa._rozstrzygnij_limit(jedna, trzy)
    assert werdykt == "PER GRUPA"
    assert any("bezpieczne" in l for l in linie)


def test_limit_globalny_dzielony():
    jedna = _wynik("1 grupa", itemow=30, limit=30)
    trzy = _wynik("3 grupy", itemow=30, limit=30, grupy=["a", "b", "c"],
                  per_grupa={"a": 10, "b": 10, "c": 10})
    werdykt, linie = pa._rozstrzygnij_limit(jedna, trzy)
    assert werdykt == "GLOBALNY"
    assert any("dzielony" in l for l in linie)
    assert any("NIEBEZPIECZNE" in l for l in linie)


def test_limit_globalny_zjadany_przez_pierwsza_grupe():
    jedna = _wynik("1 grupa", itemow=30, limit=30)
    trzy = _wynik("3 grupy", itemow=30, limit=30, grupy=["a", "b", "c"],
                  per_grupa={"a": 30})
    werdykt, linie = pa._rozstrzygnij_limit(jedna, trzy)
    assert werdykt == "GLOBALNY"
    assert any("nie dostają NIC" in l for l in linie)


def test_limit_nierozstrzygniety_gdy_grupa_nie_nasycila_limitu():
    """Bez nasycenia limitu przez jedną grupę test nie odróżnia hipotez."""
    jedna = _wynik("1 grupa", itemow=12, limit=30)
    trzy = _wynik("3 grupy", itemow=36, limit=30, grupy=["a", "b", "c"],
                  per_grupa={"a": 12, "b": 12, "c": 12})
    werdykt, linie = pa._rozstrzygnij_limit(jedna, trzy)
    assert werdykt == "NIEROZSTRZYGNIĘTE"
    assert any("NIE nasyciła limitu" in l for l in linie)


def test_limit_nierozstrzygniety_bez_pomiaru():
    werdykt, _ = pa._rozstrzygnij_limit(None, None)
    assert werdykt == "NIEROZSTRZYGNIĘTE"


# ---------------------------------------------------------------------------
# PYTANIE 3 — koszt
# ---------------------------------------------------------------------------
def test_koszt_czysto_za_post():
    wyniki = [
        _wynik("a", itemow=10, koszt=0.05, trwanie=30.0),
        _wynik("b", itemow=20, koszt=0.10, trwanie=35.0),
        _wynik("c", itemow=30, koszt=0.15, trwanie=40.0),
    ]
    k = pa._policz_koszt(wyniki, cena_z_cennika=0.005)
    assert k.itemow == 60
    assert k.za_post_usd == pytest.approx(0.005)
    assert k.za_post_krancowy_usd == pytest.approx(0.005, abs=1e-9)
    assert k.stale_za_run_usd == pytest.approx(0.0, abs=1e-9)
    assert k.r2_itemy == pytest.approx(1.0)


def test_koszt_ze_skladnikiem_stalym_jest_widoczny():
    """Gdy run ma cenę wejścia, „suma/itemy" zawyża koszt krańcowy — musi to być widać."""
    wyniki = [
        _wynik("a", itemow=10, koszt=0.03, trwanie=30.0),
        _wynik("b", itemow=20, koszt=0.04, trwanie=30.0),
        _wynik("c", itemow=30, koszt=0.05, trwanie=30.0),
    ]
    k = pa._policz_koszt(wyniki, cena_z_cennika=None)
    assert k.za_post_usd == pytest.approx(0.12 / 60)
    assert k.za_post_krancowy_usd == pytest.approx(0.001, abs=1e-9)
    assert k.stale_za_run_usd == pytest.approx(0.02, abs=1e-9)
    assert any("Składnik STAŁY" in l for l in k.linie)


def test_koszt_rozpoznaje_rozliczanie_za_zaczeta_minute():
    """Koszt zależny od czasu, nie od liczby postów — wtedy częste płytkie runy nie mają sensu."""
    wyniki = [
        _wynik("a", itemow=3, koszt=0.02, trwanie=50.0),     # 1 zaczęta minuta
        _wynik("b", itemow=30, koszt=0.02, trwanie=59.0),    # 1
        _wynik("c", itemow=7, koszt=0.04, trwanie=61.0),     # 2
        _wynik("d", itemow=40, koszt=0.06, trwanie=125.0),   # 3
    ]
    k = pa._policz_koszt(wyniki, cena_z_cennika=None)
    assert k.r2_minuty > k.r2_itemy
    assert k.za_zaczeta_minute_usd == pytest.approx(0.02, abs=2e-3)
    assert any("RZADZIEJ i grubiej" in l for l in k.linie)


def test_model_z_ujemna_stala_jest_odrzucany():
    """Run o zerowym czasie nie oddaje pieniędzy — takie dopasowanie to artefakt.

    Przy krótkiej serii, w której czas runu rośnie razem z liczbą postów, model
    minutowy potrafi mieć wysokie R² i UJEMNY wyraz wolny. Bez odrzucenia wyglądałby
    na dowód rozliczania czasu i skłoniłby do przestawienia crona na rzadszy.
    """
    wyniki = [
        _wynik("a", itemow=10, koszt=0.05, trwanie=30.0),    # 1 zaczęta minuta
        _wynik("b", itemow=70, koszt=0.30, trwanie=61.0),    # 2
        _wynik("c", itemow=195, koszt=0.80, trwanie=121.0),  # 3
    ]
    k = pa._policz_koszt(wyniki, cena_z_cennika=None)
    assert k.stale_minutowe_usd is not None and k.stale_minutowe_usd < 0
    assert any("UJEMNY składnik stały" in l and "za zaczętą minutę" in l for l in k.linie)
    assert not any("RZADZIEJ i grubiej" in l for l in k.linie)
    assert any("LICZBA POSTÓW" in l for l in k.linie)


def test_koszt_niezmierzony_gdy_brak_danych_o_saldzie():
    """Zero zamiast niewiadomej byłoby błędem wyglądającym jak świetna wiadomość."""
    wyniki = [_wynik("a", itemow=10)]
    k = pa._policz_koszt(wyniki, cena_z_cennika=None)
    assert k.za_post_usd is None
    assert any("niezmierzony" in l for l in k.linie)


def test_koszt_pomija_wywolania_ktore_padly():
    wyniki = [
        _wynik("a", itemow=10, koszt=0.05, trwanie=30.0),
        _wynik("b", itemow=999, koszt=9.99, status="", blad="ConnectTimeout"),
    ]
    k = pa._policz_koszt(wyniki, cena_z_cennika=None)
    assert k.itemow == 10
    assert k.z_salda_usd == pytest.approx(0.05)


def test_uwaga_nie_uniewaznia_wywolania():
    """Padnięty odczyt salda PO runie nie może wykluczyć danych o postach."""
    w = _wynik("a", itemow=10, najstarszy_h=1.0, okno="1 hour")
    w.uwaga = "nie odczytano salda PO runie (ReadTimeout)"
    assert w.ok is True
    sciezka, jednostka, _ = pa._rozstrzygnij_okno([w])
    assert sciezka == "ŚCIEŻKA A"
    assert jednostka == "1 hour"


# ---------------------------------------------------------------------------
# Plan, budżet i raport
# ---------------------------------------------------------------------------
class _Opcje:
    """Minimalna atrapa argparse.Namespace dla `_plan`."""

    def __init__(self, **kw):
        self.limit_q1 = 20
        self.limit_q2 = 30
        self.bez_kontroli = False
        self.bez_limitu = False
        self.pole_okna = pa.POLE_OKNA
        self.aktor = "apify~facebook-groups-scraper"
        self.__dict__.update(kw)


def test_plan_domyslny_miesci_sie_w_budzecie_500():
    plan = pa._plan(["g1", "g2", "g3"], _Opcje())
    assert len(plan) == 8            # kontrola + 5 okien + 2 wywołania limitu
    assert pa._prognoza(plan) == 240
    assert pa._prognoza(plan) < 500


def test_prognoza_jest_pesymistyczna():
    """Prognoza NIE może zakładać odpowiedzi na pytanie, które dopiero mierzymy."""
    plan = pa._plan(["g1", "g2", "g3"], _Opcje(bez_kontroli=True))
    wywolanie_trzech = [p for p in plan if len(p[1]) == 3][0]
    # 3 grupy × limit 30 = 90, czyli wariant „limit per grupa" (droższy z dwóch).
    assert wywolanie_trzech[2] * len(wywolanie_trzech[1]) == 90


def test_plan_pomija_pytanie_2_gdy_grup_za_malo():
    plan = pa._plan(["g1"], _Opcje())
    assert not [p for p in plan if p[0] in ("1 grupa", "3 grupy")]


def test_raport_zawiera_werdykt_i_wersje_actora(tmp_path):
    wyniki_okna = [
        _wynik("kontrola (bez pola)", itemow=20, najstarszy_h=100.0,
               ids=[f"p{i}" for i in range(20)], koszt=0.08, trwanie=45.0),
        _wynik("1 hour", okno="1 hour", itemow=2, najstarszy_h=0.8, najnowszy_h=0.1,
               ids=["p0", "p1"], koszt=0.01, trwanie=30.0),
    ]
    sciezka, jednostka, linie = pa._rozstrzygnij_okno(wyniki_okna)
    koszt = pa._policz_koszt(wyniki_okna, cena_z_cennika=0.004)
    tresc = pa.zbuduj_raport(
        wyniki_okna=wyniki_okna, wyniki_limitu=[], sciezka_okna=sciezka,
        najmniejsza_jednostka=jednostka, linie_okna=linie,
        werdykt_limitu="NIEROZSTRZYGNIĘTE", linie_limitu=["brak trzech grup"],
        koszt=koszt, info_actora={"_wersja": "1.2", "_build": "0.1.77",
                                  "username": "apify", "name": "facebook-groups-scraper"},
        grupy=["https://www.facebook.com/groups/111"], opcje=_Opcje(),
    )
    assert "ŚCIEŻKA A" in tresc
    assert "wersja **1.2**" in tresc and "build **0.1.77**" in tresc
    assert "## PYTANIE 1" in tresc and "## PYTANIE 2" in tresc and "## PYTANIE 3" in tresc
    assert "_build_actor_input" in tresc
    # Raport ma dać się zapisać tam, gdzie wskaże `--raport`.
    plik = tmp_path / "POMIAR-ACTORA.md"
    plik.write_text(tresc, encoding="utf-8")
    assert plik.read_text(encoding="utf-8").startswith("# Pomiar actora")


def test_raport_nie_udaje_wyniku_gdy_nic_nie_zmierzono():
    """Brak pomiaru ma być w dokumencie WIDOCZNY — prompt 2 czyta ten plik dosłownie."""
    tresc = pa.zbuduj_raport(
        wyniki_okna=[], wyniki_limitu=[], sciezka_okna="NIEROZSTRZYGNIĘTE",
        najmniejsza_jednostka="nie ustalono", linie_okna=[],
        werdykt_limitu="NIEROZSTRZYGNIĘTE", linie_limitu=[], koszt=pa.Koszt(),
        info_actora={}, grupy=[], opcje=_Opcje(),
    )
    assert "Pomiar nie rozstrzygnął tego pytania" in tresc
    assert "Kosztu nie zmierzono" in tresc
    assert "jedną grupę na" in tresc      # wariant bezpieczny przy obu hipotezach


# ---------------------------------------------------------------------------
# Saldo konta (workers/apify_credits.py)
# ---------------------------------------------------------------------------
def test_saldo_z_odpowiedzi_apify():
    dane = {"data": {
        "monthlyUsageCycle": {"startAt": "2026-08-01T00:00:00.000Z",
                              "endAt": "2026-09-01T00:00:00.000Z"},
        "limits": {"maxMonthlyUsageUsd": 5},
        "current": {"monthlyUsageUsd": 1.25},
    }}
    s = ac.z_odpowiedzi(dane)
    assert s.uzyte_usd == 1.25
    assert s.limit_usd == 5
    assert s.zostalo_usd == 3.75
    assert s.cykl_do.startswith("2026-09-01")
    assert "1.2500" in s.opis()


def test_saldo_przezywa_przestawienie_pola_o_poziom():
    """Sztywna ścieżka data.current.* dałaby po cichu koszt 0 USD za post."""
    s = ac.z_odpowiedzi({"data": {"cokolwiek": {"glebiej": {"monthlyUsageUsd": 0.5}}}})
    assert s.uzyte_usd == 0.5
    assert s.limit_usd is None
    assert s.zostalo_usd is None


def test_saldo_mowi_wprost_gdy_licznika_nie_ma():
    with pytest.raises(ac.SaldoNieznane):
        ac.z_odpowiedzi({"data": {"limits": {"maxMonthlyUsageUsd": 5}}})


def test_saldo_nie_bierze_wartosci_logicznej_za_licznik():
    with pytest.raises(ac.SaldoNieznane):
        ac.z_odpowiedzi({"data": {"current": {"monthlyUsageUsd": True}}})
