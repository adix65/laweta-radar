"""Offline testy dwóch metryk z scripts/porownaj_modele.py — bez sieci i bez modeli.

Reszta tego skryptu to wołanie API i formatowanie tabeli; te dwie funkcje są
inne, bo LICZĄ. Metryka, która myli się po cichu, jest gorsza niż jej brak:
wygląda jak liczba, wchodzi do decyzji o wyborze modelu i nikt jej nie
kwestionuje.

HALUCYNACJA GEO — najdroższy błąd, jaki ten system potrafi popełnić (zgadnięte
miasto wysyła człowieka 80 km w złą stronę) i jedyna kolumna, której tryb JSON
nie poprawia. Testujemy głównie FAŁSZYWE ALARMY, bo polska odmiana rozjeżdża
formy ("Krosno" stoi w poście jako "w Krośnie"), a metryka krzycząca na
poprawne odpowiedzi zostaje zignorowana po drugim razie.

ZGODNOŚĆ ZE SCHEMATEM — walidator klasyfikatora jest wyrozumiały z rozmysłu,
więc model systematycznie zjeżdżający z kontraktu wygląda na poprawny. Ta
metryka pokazuje go PRZED naprawą.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.scripts import porownaj_modele as pm  # noqa: E402

POPRAWNA_ODPOWIEDZ = {
    "czy_zlecenie": True,
    "typ": "holowanie",
    "odbior": {"raw": "Krosno", "kod": "38-400", "miasto": "Krosno"},
    "dostawa": {"raw": None, "kod": None, "miasto": None},
    "pojazd": {"opis": "VW Golf", "kategoria": "osobowy"},
    "stan": {"toczy_sie": True, "ma_kola": True, "po_wypadku": False, "uwagi": None},
    "pilnosc": "teraz",
    "kontakt": {"typ": "telefon", "wartosc": "555111222"},
    "cena_sugerowana": None,
    "pewnosc": 90,
    "powod": "autor szuka lawety",
}


def z_podmiana(**zmiany) -> dict:
    dane = json.loads(json.dumps(POPRAWNA_ODPOWIEDZ))
    dane.update(zmiany)
    return dane


# ---------------------------------------------------------------------------
# HALUCYNACJA GEO
# ---------------------------------------------------------------------------
def test_zmyslone_miasto_jest_halucynacja():
    assert pm.czy_halucynacja("Zakopane", "zdechl mi golf w Krosnie, kto wezmie")


def test_odmiana_nie_jest_halucynacja():
    """Posty pisze się na telefonie i w przypadkach zależnych, nie w mianowniku."""
    for miasto, tresc in [
        ("Krosno", "stoje w Krosnie na dk28"),
        ("Rzeszów", "wioze do Rzeszowa, kto pomoze"),
        ("Sanok", "spod Sanoka do Krosna"),
        ("Jasło", "auto stoi w Jasle"),
    ]:
        assert not pm.czy_halucynacja(miasto, tresc), (miasto, tresc)


def test_nazwa_dwuczlonowa_odmieniona_w_obu_czlonach():
    """"Miejsce Piastowe" -> "pod Miejscem Piastowym". Oba człony się odmieniają."""
    assert not pm.czy_halucynacja(
        "Miejsce Piastowe", "Wjechalem w row pod Miejscem Piastowym, kto ma wyciagarke")


def test_nazwa_z_lacznikiem():
    assert not pm.czy_halucynacja("Bielsko-Biała", "auto stoi w Bielsku-Bialej")


def test_wymagane_sa_wszystkie_czlony_nazwy():
    """Sam pospolity pierwszy człon nie wystarcza — inaczej "Nowy" trafiałby wszędzie."""
    assert pm.czy_halucynacja("Nowy Sącz", "stoje pod Nowym Targiem")


def test_brak_miasta_nie_jest_halucynacja():
    """Null to POPRAWNA odpowiedź, wręcz preferowana — nie może psuć metryki."""
    for pusto in (None, "", "   "):
        assert not pm.czy_halucynacja(pusto, "post bez miejscowosci")


def test_metryka_nie_krzyczy_na_zbior_referencyjny():
    """Na etykietach ze zbioru fałszywych alarmów ma być ZERO.

    Etykiety opisują miasta, które w poście NAPRAWDĘ są — każdy alarm tutaj
    to błąd metryki, a nie modelu. Gdyby ten test zaczął padać po dopisaniu
    postów, to metryka wymaga poprawki, nie zbiór.
    """
    posty, _ = pm.wczytaj(pm.ZBIOR_DOMYSLNY)
    assert posty, "zbiór referencyjny zniknął albo przestał się wczytywać"
    falszywe = [
        f"{p.get('id')}: {p['oczekiwane'][pole]!r}"
        for p in posty
        for pole in ("odbior_miasto", "dostawa_miasto")
        if p["oczekiwane"].get(pole)
        and pm.czy_halucynacja(p["oczekiwane"][pole], p["tresc"])
    ]
    assert not falszywe, f"fałszywe alarmy metryki halucynacji: {falszywe}"


# ---------------------------------------------------------------------------
# ZGODNOŚĆ ZE SCHEMATEM
# ---------------------------------------------------------------------------
def test_poprawna_odpowiedz_nie_ma_odchylen():
    assert pm.odchylenia_od_schematu(POPRAWNA_ODPOWIEDZ) == []


def test_wartosc_spoza_zbioru_jest_odchyleniem():
    """Walidator podstawi wartość domyślną — ta metryka ma to zobaczyć PRZED nim."""
    assert pm.odchylenia_od_schematu(z_podmiana(pilnosc="natychmiast"))
    assert pm.odchylenia_od_schematu(z_podmiana(typ="laweta_ciezka"))


def test_brakujace_pole_jest_odchyleniem():
    bez_dostawy = json.loads(json.dumps(POPRAWNA_ODPOWIEDZ))
    del bez_dostawy["dostawa"]
    assert pm.odchylenia_od_schematu(bez_dostawy)

    bez_miasta = json.loads(json.dumps(POPRAWNA_ODPOWIEDZ))
    del bez_miasta["odbior"]["miasto"]
    assert pm.odchylenia_od_schematu(bez_miasta)


def test_zly_typ_jest_odchyleniem():
    """"true" (napis) to nie to samo co true — walidator to naprawi, my liczymy."""
    assert pm.odchylenia_od_schematu(z_podmiana(czy_zlecenie="true"))
    assert pm.odchylenia_od_schematu(z_podmiana(pewnosc="90"))
    assert pm.odchylenia_od_schematu(z_podmiana(pewnosc=150))
    assert pm.odchylenia_od_schematu(z_podmiana(cena_sugerowana="200 zl"))


def test_null_tam_gdzie_kontrakt_go_dopuszcza_nie_jest_odchyleniem():
    assert pm.odchylenia_od_schematu(z_podmiana(cena_sugerowana=None, powod=None)) == []


def test_zagniezdzony_zbior_tez_sprawdzany():
    dane = z_podmiana()
    dane["pojazd"]["kategoria"] = "kabriolet"
    assert pm.odchylenia_od_schematu(dane)


def test_zbiory_pochodza_z_klasyfikatora():
    """Jedno źródło prawdy: druga kopia list rozjechałaby się przy pierwszej zmianie."""
    from laweta_radar.workers import classifier

    assert dict(pm._ZBIORY_PROSTE)["typ"] is classifier._POPRAWNE_TYP
    assert dict(pm._ZBIORY_PROSTE)["pilnosc"] is classifier._POPRAWNE_PILNOSC


# ---------------------------------------------------------------------------
# ETYKIETA WYNIKU
# ---------------------------------------------------------------------------
def test_etykieta_niesie_tryb_json():
    """Tryb JEST częścią wyniku — bez niego dwie tabele są nieporównywalne."""
    w = pm.Wynik("openai", "gpt-5-mini")
    w.tryb = "schema"
    assert "gpt-5-mini" in w.etykieta and "schema" in w.etykieta


def test_etykieta_bez_trybu_przy_anthropic():
    """Haiku nie ma trybu JSON — dopisek "[json=off]" byłby szumem w każdej linii."""
    w = pm.Wynik("anthropic", "claude-haiku-4-5")
    w.tryb = "off"
    assert w.etykieta == "anthropic/claude-haiku-4-5"


def test_halucynacje_liczone_wzgledem_podanych_miast():
    """Mianownik to PODANE miasta, nie wszystkie posty.

    Model zostawiający wszędzie null ma zero halucynacji i tak ma być — to
    zachowanie, którego prompt wymaga. Jego cenę widać w kolumnach trafności.
    """
    w = pm.Wynik("openai", "gpt-5-mini")
    wpis = {"id": "x", "tresc": "zdechl mi golf w Krosnie", "oczekiwane": {}}
    wynik = z_podmiana()
    wynik["dostawa"] = {"raw": None, "kod": None, "miasto": "Zakopane"}
    w.dodaj_halucynacje(wpis, wynik)
    assert (w.miasta_podane, w.halucynacje) == (2, 1)
    assert w.procent_halucynacji == 50.0


def test_model_zostawiajacy_nulle_nie_ma_halucynacji():
    w = pm.Wynik("openai", "gpt-5-mini")
    wpis = {"id": "x", "tresc": "post bez miejscowosci", "oczekiwane": {}}
    puste = z_podmiana(odbior={"raw": None, "kod": None, "miasto": None})
    w.dodaj_halucynacje(wpis, puste)
    assert (w.miasta_podane, w.halucynacje) == (0, 0)
    assert w.procent_halucynacji is None
