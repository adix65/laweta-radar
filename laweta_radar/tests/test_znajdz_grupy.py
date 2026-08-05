"""Offline testy wyszukiwarki grup — bez sieci i bez wydawania kredytu.

Pięć rzeczy musi być pewnych, zanim ktokolwiek odpali płatną serię:

  1. NAZWY PÓL WEJŚCIA. Actor przyjmuje `startUrls`/`maxGroups`, a nie
     `search`/`maxItems` — i to jest różnica, której NIE WIDAĆ po runie: pole,
     którego actor nie zna, zostaje zignorowane bez błędu i płaci się pełną cenę
     za run bez filtra.
  2. CIASTECZKA. Bez sesji wyszukiwarka FB oddaje ścianę logowania, więc run
     kosztuje tyle samo i nie zwraca nic. Ich WARTOŚCI nie mają prawa wyjść
     do logu ani do podglądu `--sucho` — to żywa sesja Facebooka.
  3. LICZBA CZŁONKÓW czytana z podpisu FB. Pomyłka o rząd wielkości w jedną stronę
     wpuszcza do listy szum, w drugą — wycina grupę, która była tu najlepsza.
  4. DEDUP PO URL-u. Ta sama grupa wraca z każdej frazy w innej postaci; bez
     normalizacji CSV puchnie o duplikaty, których człowiek nie ma jak odsiać.
  5. SCALANIE Z PRACĄ RĘCZNĄ. To jedyny nieodwracalny błąd tego skryptu:
     nadpisanie kolumny `publiczna` kasuje godziny klikania po Facebooku i po
     drugim razie nikt narzędzia nie odpali.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.config import frazy_grup as cfg  # noqa: E402
from laweta_radar.config import settings  # noqa: E402
from laweta_radar.scripts import znajdz_grupy as z  # noqa: E402
from laweta_radar.workers import apify_run  # noqa: E402


# ---------------------------------------------------------------------------
# WEJŚCIE ACTORA — jedyny błąd tego skryptu, który kosztuje pieniądze po cichu
# ---------------------------------------------------------------------------
# Schemat WZIĘTY Z PRODUKCJI (`--schema`, memo23~facebook-search-groups-scraper,
# sprawdzony 2026-08-05): dokładnie sześć pól, ani `search`, ani `maxItems`.
# Stoi tutaj, a nie w kodzie, ŚWIADOMIE — rozjazd nazw ma być czerwonym testem
# na sucho, a nie rachunkiem za serię runów bez filtra.
SCHEMAT_ACTORA = {"properties": {
    "cookies":   {"type": "array"},
    "maxDelay":  {"type": "integer"},
    "maxGroups": {"type": "integer"},
    "minDelay":  {"type": "integer"},
    "proxy":     {"type": "object"},
    "startUrls": {"type": "array"},
}}


def test_wejscie_uzywa_wylacznie_pol_ktore_actor_deklaruje():
    wejscie = z._wejscie("giełda lawet", 30, [{"name": "c_user", "value": "1"}])
    assert apify_run.nieznane_pola(wejscie, SCHEMAT_ACTORA) == []


def test_wejscie_nie_ma_pol_ktorych_ten_actor_nigdy_nie_mial():
    """`search` i `maxItems` nie istnieją w tym actorze. Zła nazwa pola nie
    zwraca błędu — zwraca run BEZ FILTRA, za pełną cenę."""
    wejscie = z._wejscie("giełda lawet", 30)
    assert "search" not in wejscie
    assert "maxItems" not in wejscie
    assert wejscie[cfg.POLE_LIMITU] == 30


def test_fraza_jedzie_jako_adres_wyszukiwarki_w_startUrls():
    """Actor nie ma pola na frazę — szukanie wyraża się adresem."""
    wejscie = z._wejscie("giełda lawet", 30)
    assert wejscie[cfg.POLE_STARTOWE] == [
        {"url": "https://www.facebook.com/search/groups/?q=gie%C5%82da%20lawet"}]


def test_url_frazy_koduje_spacje_i_znaki_narodowe():
    """Frazy mają spacje i ogonki w czterech językach. Niezakodowane rozwalają
    adres, a actor dostaje URL, który nie jest wyszukiwaniem."""
    for fraza in ("giełda lawet", "odtahová služba", "Autotransport Börse",
                  "odťah vozidla", "přeprava vozidel Německo"):
        url = z.url_frazy(fraza)
        assert url.startswith("https://www.facebook.com/search/groups/?q=")
        assert " " not in url
        assert url.isascii()        # nic niezakodowanego nie wyszło na zewnątrz


def test_url_frazy_koduje_znaki_ktore_sa_skladnia_adresu():
    """`&`, `?` i `/` w treści frazy to znaki, nie separatory — inaczej reszta
    frazy zostaje przez FB przeczytana jako kolejny parametr."""
    ogon = z.url_frazy("laweta & pomoc/drogowa?").split("q=", 1)[1]
    assert "&" not in ogon and "?" not in ogon and "/" not in ogon


def test_odstepy_przewijania_ida_z_konfiguracji():
    """minDelay/maxDelay to nie pokrętło wydajności: zbyt agresywne przewijanie
    wygląda dla FB jak bot i psuje sesję, czyli wszystkie NASTĘPNE runy."""
    wejscie = z._wejscie("giełda lawet", 30)
    assert wejscie[cfg.POLE_MIN_ODSTEPU] == settings.FB_SEARCH_MIN_DELAY_S
    assert wejscie[cfg.POLE_MAX_ODSTEPU] == settings.FB_SEARCH_MAX_DELAY_S
    assert wejscie[cfg.POLE_MIN_ODSTEPU] <= wejscie[cfg.POLE_MAX_ODSTEPU]


def test_odwrocona_para_odstepow_nie_zatrzymuje_przebiegu(monkeypatch):
    """Zasada tego repo: zła wartość w .env degraduje, nie wysadza crona."""
    monkeypatch.setenv("FB_SEARCH_MIN_DELAY_S", "9")
    monkeypatch.setenv("FB_SEARCH_MAX_DELAY_S", "2")
    try:
        swieze = importlib.reload(settings)
        assert swieze.FB_SEARCH_MIN_DELAY_S == 9
        assert swieze.FB_SEARCH_MAX_DELAY_S == 9      # sufit dociągnięty do dołu
    finally:
        monkeypatch.undo()
        importlib.reload(settings)


def test_nazwy_pol_niosa_date_sprawdzenia_schematu():
    """Actory zmieniają wejście między wersjami. Bez daty nie da się odróżnić
    nazw, które ktoś sprawdził, od nazw, które ktoś zgadł."""
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", cfg.SCHEMAT_SPRAWDZONY)


def test_jezyk_frazy_rozpoznaje_blok_jezykowy():
    """Kolumna `jezyk` bierze się z tego, która fraza znalazła grupę — przy
    `--fraza` wpisanej z ręki nie ma innego źródła."""
    assert z.jezyk_frazy("giełda lawet") == "pl"
    assert z.jezyk_frazy("odtahová služba") == "cs"
    assert z.jezyk_frazy("coś wpisanego z ręki") == "?"


# ---------------------------------------------------------------------------
# CIASTECZKA — bez nich actor dostaje ścianę logowania, a rachunek jest ten sam
# ---------------------------------------------------------------------------
CIASTECZKA_FB = [
    {"name": "c_user", "value": "100000000000001", "domain": ".facebook.com"},
    {"name": "xs", "value": "TAJNA-SESJA", "domain": ".facebook.com"},
]


def _plik_ciastek(tmp_path, dane, nazwa="ciasteczka.json"):
    plik = tmp_path / nazwa
    plik.write_text(json.dumps(dane), encoding="utf-8")
    return plik


def test_wczytuje_oba_formaty_eksportu(tmp_path):
    """Rozszerzenia przeglądarki zapisują raz gołą listę, raz `{"cookies": [...]}`."""
    lista, _ = z.wczytaj_ciasteczka(_plik_ciastek(tmp_path, CIASTECZKA_FB))
    obiekt, _ = z.wczytaj_ciasteczka(
        _plik_ciastek(tmp_path, {"cookies": CIASTECZKA_FB}, "obiekt.json"))
    assert lista == obiekt == CIASTECZKA_FB


def test_brak_pliku_to_ostrzezenie_a_nie_wyjatek(tmp_path):
    """Brak sesji ma zatrzymać człowieka PYTANIEM przed serią, a nie tracebackiem
    w środku opłaconego przebiegu."""
    for sciezka in ("", None, tmp_path / "nie-ma-mnie.json"):
        ciasteczka, komunikat = z.wczytaj_ciasteczka(sciezka)
        assert ciasteczka == []
        assert komunikat.startswith("BRAK")


def test_zepsuty_plik_nie_wysadza_skryptu_i_nie_cytuje_tresci(tmp_path):
    """Treść wyjątku z JSON-a bywa fragmentem pliku, a plik jest sesją."""
    plik = tmp_path / "polamany.json"
    plik.write_text('[{"name": "xs", "value": "TAJNA-SESJA"', encoding="utf-8")
    ciasteczka, komunikat = z.wczytaj_ciasteczka(plik)
    assert ciasteczka == []
    assert "TAJNA-SESJA" not in komunikat


def test_ciasteczka_spoza_facebooka_nie_jada_do_cudzego_actora(tmp_path):
    """Eksport bywa eksportem CAŁEJ przeglądarki. Plik idzie w całości do obcego
    actora, więc jedzie wyłącznie to, co dotyczy Facebooka — i widać, ile zostało
    w domu."""
    plik = _plik_ciastek(tmp_path, [
        *CIASTECZKA_FB,
        {"name": "sid", "value": "SESJA-BANKU", "domain": ".bank.example"},
    ])
    ciasteczka, komunikat = z.wczytaj_ciasteczka(plik)
    assert ciasteczka == CIASTECZKA_FB
    assert "SESJA-BANKU" not in komunikat
    assert "1 spoza Facebooka POMINIĘTO" in komunikat


def test_komunikat_niesie_liczbe_a_nigdy_wartosci(tmp_path):
    ciasteczka, komunikat = z.wczytaj_ciasteczka(
        _plik_ciastek(tmp_path, CIASTECZKA_FB))
    assert len(ciasteczka) == 2
    assert komunikat.startswith("2 z ")
    assert "TAJNA-SESJA" not in komunikat
    assert "100000000000001" not in komunikat


def test_plik_bez_ciasteczek_sesji_jest_oznaczony(tmp_path):
    """Eksport z niezalogowanej karty wygląda jak poprawny plik i przechodzi
    wszystko poza tym jednym sprawdzeniem."""
    _, komunikat = z.wczytaj_ciasteczka(_plik_ciastek(
        tmp_path, [{"name": "datr", "value": "x", "domain": ".facebook.com"}]))
    assert "UWAGA" in komunikat and "c_user" in komunikat


def test_ciasteczka_doklejane_tylko_gdy_sa():
    """Pole wysłane pusto znaczy dla actora to samo co brak pola, ale w podglądzie
    `--sucho` wyglądałoby jak sesja, której nie ma."""
    assert cfg.POLE_CIASTECZEK not in z._wejscie("fraza", 5)
    assert cfg.POLE_CIASTECZEK not in z._wejscie("fraza", 5, [])
    assert z._wejscie("fraza", 5, CIASTECZKA_FB)[cfg.POLE_CIASTECZEK] == CIASTECZKA_FB


def test_podglad_wejscia_nie_pokazuje_wartosci_ciasteczek():
    """`--sucho` ma pozwolić porównać wejście ze schematem gołym okiem — i ani
    razu nie wypisać sesji, bo log bywa wklejany do zgłoszeń."""
    wejscie = z._wejscie("giełda lawet", 30, CIASTECZKA_FB)
    podglad = z.podglad_wejscia(wejscie)
    assert "TAJNA-SESJA" not in podglad
    assert "2 ciasteczek" in podglad
    assert cfg.POLE_STARTOWE in podglad          # reszta wejścia widoczna...
    assert cfg.POLE_CIASTECZEK in podglad        # ...razem z SAMYM polem cookies
    # Maskowanie jest kopią: wejście, które faktycznie leci do actora, nietknięte.
    assert wejscie[cfg.POLE_CIASTECZEK] == CIASTECZKA_FB


# ---------------------------------------------------------------------------
# Seria i próba — pętla fraz bez sieci
# ---------------------------------------------------------------------------
class _AtrapaRotatora:
    """Rotator sprowadzony do jednego klucza — tu testujemy pętlę fraz, nie rotację."""

    def call(self, fn):
        return fn("token-testowy")


def _atrapa_runu(itemy):
    return type("RunAtrapa", (), {
        "itemy": itemy, "ile_itemow": len(itemy), "koszt_usd": 0.01, "blad": None,
    })()


def _przechwyc_wywolania(monkeypatch, itemy):
    """Podmienia actora na atrapę; oddaje listę, do której wpadają WEJŚCIA."""
    wywolania: list[dict] = []

    def atrapa(token, actor, wejscie, **kw):  # noqa: ARG001
        wywolania.append(wejscie)
        return _atrapa_runu(itemy)

    monkeypatch.setattr(z.apify_run, "uruchom", atrapa)
    monkeypatch.setattr(z.time, "sleep", lambda _s: None)
    return wywolania


def test_ciasteczka_dojezdzaja_do_wejscia_actora(monkeypatch):
    wywolania = _przechwyc_wywolania(
        monkeypatch, [{"url": "https://facebook.com/groups/1", "name": "G"}])
    z._szukaj(_AtrapaRotatora(), [("pl", "giełda lawet")], 5, lambda *_: None,
              CIASTECZKA_FB)
    assert wywolania[0][cfg.POLE_CIASTECZEK] == CIASTECZKA_FB


def test_seria_przerywa_sie_gdy_pierwsza_fraza_zwraca_zero(monkeypatch):
    """Bez sesji KAŻDE wywołanie zwróci to samo zero i zostanie policzone tak
    samo jak udane. Jedno stracone wywołanie zamiast dwudziestu ośmiu."""
    wywolania = _przechwyc_wywolania(monkeypatch, [])
    seria = z._szukaj(_AtrapaRotatora(),
                      [("pl", "a"), ("pl", "b"), ("pl", "c")], 5, lambda *_: None)
    assert len(wywolania) == 1
    assert any("sesję" in b for b in seria.bledy)


def test_werdykt_proby_wskazuje_sesje_gdy_nie_ma_itemow(capsys):
    z._wynik_proby(z._Seria(), byly_ciasteczka=False)
    wyjscie = capsys.readouterr().out
    assert "ZERO itemów" in wyjscie and "FB_COOKIES_PATH" in wyjscie


def test_werdykt_proby_odroznia_brak_sesji_od_zmiany_pol_wyjscia(capsys):
    """Itemy bez adresów grup to inna awaria niż brak itemów — i co innego się
    po niej robi (POLA_URL, nie ciasteczka)."""
    z._wynik_proby(z._Seria(surowe=[{"zupelnie": "inny kształt"}]),
                   byly_ciasteczka=True)
    wyjscie = capsys.readouterr().out
    assert "POLA_URL" in wyjscie


# ---------------------------------------------------------------------------
# Liczba członków — cztery języki, kilka formatów zapisu
# ---------------------------------------------------------------------------
def test_parsuj_czlonkow_formaty_liczbowe():
    assert z.parsuj_czlonkow(3400) == 3400
    assert z.parsuj_czlonkow("3400") == 3400
    assert z.parsuj_czlonkow("12,5 tys. członków") == 12_500
    assert z.parsuj_czlonkow("1.2K members") == 1_200
    assert z.parsuj_czlonkow("12,500 members") == 12_500      # separator tysięcy
    assert z.parsuj_czlonkow("3,4 tis. členů") == 3_400
    assert z.parsuj_czlonkow("2,5 Mio. Mitglieder") == 2_500_000


def test_slowo_members_nie_jest_mnoznikiem_milionow():
    """„members" zaczyna się od „m" — przy dopasowaniu po prefiksie wyszłoby
    1,2 miliarda członków i grupa awansowałaby na szczyt listy."""
    assert z.parsuj_czlonkow("1200 members") == 1_200


def test_bierze_liczbe_przy_slowie_czlonkowie_a_nie_pierwsza_z_brzegu():
    """Podpis FB niesie też inne liczby. Bez kotwicy trafiłaby liczba postów."""
    assert z.parsuj_czlonkow(
        "Grupa publiczna · 3 posty dziennie · 12 tys. członków") == 12_000


def test_nieodczytana_liczba_to_zero_a_nie_wyjatek():
    assert z.parsuj_czlonkow("brak danych") == 0
    assert z.parsuj_czlonkow("") == 0
    assert z.parsuj_czlonkow(None) == 0
    assert z.parsuj_czlonkow(True) == 0        # bool to nie liczba członków


# ---------------------------------------------------------------------------
# Normalizacja URL — bez niej dedup nie działa
# ---------------------------------------------------------------------------
def test_normalizuj_url_sprowadza_warianty_do_jednej_postaci():
    warianty = [
        "https://www.facebook.com/groups/123456/",
        "http://m.facebook.com/groups/123456",
        "https://web.facebook.com/groups/123456/?ref=share",
        "facebook.com/groups/123456#post",
    ]
    assert len({z.normalizuj_url(u) for u in warianty}) == 1
    assert z.normalizuj_url(warianty[0]) == "https://www.facebook.com/groups/123456"


def test_normalizuj_url_pusty():
    assert z.normalizuj_url("") == ""
    assert z.normalizuj_url("   ") == ""


# ---------------------------------------------------------------------------
# Item z actora -> kandydat
# ---------------------------------------------------------------------------
def test_na_kandydata_wyciaga_komplet_danych():
    k = z.na_kandydata(
        {"url": "https://m.facebook.com/groups/999/?ref=x",
         "name": "Pomoc drogowa Podkarpacie",
         "membersCount": "4,2 tys. członków"},
        "pl", "pomoc drogowa Podkarpacie")
    assert k.url == "https://www.facebook.com/groups/999"
    assert k.nazwa == "Pomoc drogowa Podkarpacie"
    assert k.czlonkowie == 4_200
    assert k.jezyk == "pl"
    assert k.publiczna == ""            # ZAWSZE puste — to krok człowieka
    assert k.status == "kandydat"


def test_item_bez_adresu_grupy_odpada():
    assert z.na_kandydata({"name": "Coś bez linku"}, "pl", "fraza") is None
    assert z.na_kandydata({"url": "https://facebook.com/marketplace/x"},
                          "pl", "fraza") is None


def test_podpowiedz_prywatnosci_trafia_do_notatki_a_nie_do_kolumny():
    """Wyszukiwarka bywa źródłem tej informacji, ale pokazuje stan sprzed
    nieznanego czasu. Podpowiedź ma pomóc człowiekowi, nie zastąpić go."""
    k = z.na_kandydata({"url": "https://facebook.com/groups/1", "name": "X",
                        "privacy": "Private group"}, "pl", "fraza")
    assert k.publiczna == ""
    assert "wyszukiwarka: prywatna" in k.notatka

    k2 = z.na_kandydata({"url": "https://facebook.com/groups/2", "name": "Y",
                         "isPublic": True}, "pl", "fraza")
    assert k2.publiczna == ""
    assert "wyszukiwarka: publiczna" in k2.notatka


def test_notatka_ostrzega_przed_grupa_sprzedazowa():
    n = z.notatki("Sprzedam części do TIRów", "przewóz aut", "")
    assert "SPRZEDAŻ" in n


def test_notatka_ostrzega_przy_frazie_niepewnej():
    n = z.notatki("Autoüberführung Deutschland", "Autoüberführung", "")
    assert "nie na temat" in n


# ---------------------------------------------------------------------------
# Odsiew: dedup, próg, sortowanie
# ---------------------------------------------------------------------------
def _k(url: str, ilu: int, fraza: str = "f1", nazwa: str = "n"):
    return z.Kandydat(url=url, nazwa=nazwa, czlonkowie=ilu, jezyk="pl",
                      fraza_zrodlowa=fraza)


def test_odsiew_dedupuje_laczy_frazy_i_sortuje():
    lista, stat = z.odsiej([
        _k("https://www.facebook.com/groups/1", 1000, "giełda lawet", "A"),
        _k("https://www.facebook.com/groups/1", 1000, "pomoc drogowa", "A"),
        _k("https://www.facebook.com/groups/2", 9000, "przewóz aut", "B"),
    ], min_czlonkow=500)
    assert [k.nazwa for k in lista] == ["B", "A"]          # malejąco po członkach
    assert stat["duplikaty"] == 1
    assert "giełda lawet" in lista[1].fraza_zrodlowa
    assert "pomoc drogowa" in lista[1].fraza_zrodlowa      # obie frazy zachowane


def test_odsiew_wycina_ponizej_progu():
    lista, stat = z.odsiej([_k("https://www.facebook.com/groups/1", 120)],
                           min_czlonkow=500)
    assert lista == [] and stat["za_male"] == 1


def test_nieznana_liczba_czlonkow_NIE_wycina_grupy():
    """Zero znaczy „nie odczytaliśmy", nie „grupa jest pusta". Karanie grupy za
    nasz nieudany parsing wyrzuciłoby z listy każdy nowy format podpisu FB."""
    lista, stat = z.odsiej([_k("https://www.facebook.com/groups/1", 0)],
                           min_czlonkow=500)
    assert len(lista) == 1 and stat["za_male"] == 0


# ---------------------------------------------------------------------------
# Scalanie z CSV — praca ręczna jest nienaruszalna
# ---------------------------------------------------------------------------
def test_scalanie_nie_kasuje_kolumn_wypelnionych_przez_czlowieka():
    istniejace = [{
        "url": "https://www.facebook.com/groups/1", "nazwa": "Stara nazwa",
        "czlonkowie": "900", "jezyk": "pl", "fraza_zrodlowa": "giełda lawet",
        "publiczna": "TAK", "status": "ok", "notatka": "sprawdzone 2026-07-01",
    }]
    nowe = [_k("https://www.facebook.com/groups/1", 1500, "pomoc drogowa",
               "Nowa nazwa")]
    wynik = z.scal(nowe, istniejace)
    assert len(wynik) == 1
    w = wynik[0]
    assert w["publiczna"] == "TAK"                     # NIE nadpisane
    assert w["status"] == "ok"                         # NIE nadpisane
    assert w["notatka"] == "sprawdzone 2026-07-01"     # NIE nadpisane
    assert w["nazwa"] == "Nowa nazwa"                  # świeższe dane wygrywają
    assert w["czlonkowie"] == "1500"
    assert "giełda lawet" in w["fraza_zrodlowa"]       # frazy się sumują
    assert "pomoc drogowa" in w["fraza_zrodlowa"]


def test_scalanie_dokłada_nowe_grupy():
    istniejace = [{"url": "https://www.facebook.com/groups/1", "nazwa": "Stara",
                   "czlonkowie": "900", "publiczna": "TAK", "status": "ok"}]
    wynik = z.scal([_k("https://www.facebook.com/groups/2", 5000)], istniejace)
    assert {w["url"] for w in wynik} == {
        "https://www.facebook.com/groups/1", "https://www.facebook.com/groups/2"}


def test_scalanie_na_pustym_pliku_to_zwykly_pierwszy_przebieg():
    wynik = z.scal([_k("https://www.facebook.com/groups/1", 900)], [])
    assert len(wynik) == 1 and wynik[0]["publiczna"] == ""


def test_zapis_i_odczyt_csv_zachowuje_prace_reczna(tmp_path):
    """Pełna pętla: zapis -> odczyt -> ponowne scalenie. To ona chroni jedyną
    rzecz w repo, której nie da się odtworzyć ponownym uruchomieniem czegokolwiek."""
    plik = tmp_path / "kandydaci.csv"
    k = _k("https://www.facebook.com/groups/1", 900, "giełda lawet", "Grupa")
    z.zapisz_csv(plik, [k.wiersz()])

    # człowiek wypełnia kolumnę
    wiersze = z.czytaj_csv(plik)
    wiersze[0]["publiczna"] = "TAK"
    z.zapisz_csv(plik, wiersze)

    # miesiąc później: to samo wyszukiwanie znajduje grupę ponownie
    znowu = z.scal([_k("https://www.facebook.com/groups/1", 1100, "pomoc drogowa")],
                   z.czytaj_csv(plik))
    z.zapisz_csv(plik, znowu)

    koncowe = z.czytaj_csv(plik)
    assert len(koncowe) == 1
    assert koncowe[0]["publiczna"] == "TAK"        # praca ręczna PRZEŻYŁA
    assert koncowe[0]["czlonkowie"] == "1100"      # dane się odświeżyły
    assert not list(tmp_path.glob("*.tmp"))        # po zapisie nie zostaje śmieć


# ---------------------------------------------------------------------------
# --raport: blok do groups.py
# ---------------------------------------------------------------------------
def test_raport_bierze_wylacznie_grupy_oznaczone_TAK():
    wiersze = [
        {"url": "https://fb.com/groups/1", "nazwa": "Publiczna", "czlonkowie": "5000",
         "jezyk": "pl", "publiczna": "TAK", "status": "kandydat"},
        {"url": "https://fb.com/groups/2", "nazwa": "Prywatna", "czlonkowie": "9000",
         "jezyk": "pl", "publiczna": "NIE", "status": "kandydat"},
        {"url": "https://fb.com/groups/3", "nazwa": "Nieoznaczona",
         "czlonkowie": "9000", "jezyk": "pl", "publiczna": "", "status": "kandydat"},
    ]
    blok, stat = z.blok_do_groups_py(wiersze)
    assert "Publiczna" in blok
    assert "Prywatna" not in blok           # Apify i tak jej nie przeczyta
    assert "Nieoznaczona" not in blok       # brak decyzji ≠ zgoda
    assert stat == {"wszystkie": 3, "tak": 1, "nie": 1, "puste": 1}


def test_raport_pomija_grupy_odrzucone_recznie():
    wiersze = [{"url": "https://fb.com/groups/1", "nazwa": "Sama reklama",
                "czlonkowie": "9000", "jezyk": "pl", "publiczna": "TAK",
                "status": "odrzucona"}]
    blok, stat = z.blok_do_groups_py(wiersze)
    assert "Sama reklama" not in blok and stat["tak"] == 1


def test_raport_zawsze_daje_status_unverified():
    """`publiczna=TAK` odpowiada na JEDNO z trzech pytań z groups.py. Zostają dwa:
    czy grupa żyje i czy jest zgłoszeniowa, a nie ogłoszeniowa."""
    wiersze = [{"url": "https://fb.com/groups/1", "nazwa": "X", "czlonkowie": "5000",
                "jezyk": "pl", "publiczna": "TAK", "status": "ok"}]
    blok, _ = z.blok_do_groups_py(wiersze)
    assert '"status": "unverified"' in blok
    assert '"status": "ok"' not in blok


def test_blok_jest_poprawnym_pythonem_i_pasuje_do_groups_py():
    """Blok ma się WKLEIĆ do config/groups.py — więc musi się dać wykonać
    i przejść przez filtr `grupy_do_pobrania`.

    Nazwa grupy pochodzi z Facebooka, czyli spoza naszej kontroli: cudzysłów,
    odwrotny ukośnik i nowa linia muszą wyjść z bloku nieszkodliwe, bo człowiek
    wkleja go bez czytania linijka po linijce.
    """
    from laweta_radar.config import groups

    wiersze = [{"url": "https://www.facebook.com/groups/1",
                "nazwa": 'Grupa "X" \\ Laweta\nPodkarpacie',
                "czlonkowie": "5000", "jezyk": "pl", "publiczna": "TAK",
                "status": "kandydat"}]
    blok, _ = z.blok_do_groups_py(wiersze)
    przestrzen: dict = {}
    exec(blok, przestrzen)                                  # noqa: S102 — to jest test
    wpisy = przestrzen["FB_GRUPY"]
    assert len(wpisy) == 1 and wpisy[0]["url"].endswith("/groups/1")
    assert wpisy[0]["name"] == "Grupa 'X' / Laweta Podkarpacie"
    # Świeżo wklejone wpisy są `unverified`, więc fetcher ich NIE weźmie.
    assert groups.grupy_do_pobrania(wpisy) == []


# ---------------------------------------------------------------------------
# Konfiguracja fraz
# ---------------------------------------------------------------------------
def test_frazy_maja_cztery_bloki_jezykowe():
    assert set(cfg.FRAZY) == {"pl", "de", "cs", "sk"}
    assert all(cfg.FRAZY.values())


def test_frazy_filtruja_sie_po_jezyku():
    tylko_pl = cfg.frazy(["pl"])
    assert tylko_pl and all(j == "pl" for j, _ in tylko_pl)
    assert cfg.frazy(["xx"]) == []          # nieznany język = pusto, nie wyjątek


def test_region_wchodzi_do_fraz_polskich():
    """Frazy regionalne mają najlepszy stosunek zleceń do szumu — muszą jechać
    z jednej stałej, żeby zmiana regionu była zmianą JEDNEGO słowa."""
    polskie = [f for j, f in cfg.frazy(["pl"])]
    assert sum(1 for f in polskie if cfg.REGION in f) == 2
