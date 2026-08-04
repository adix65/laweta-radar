"""Testy `services/powiadomienia.py` — bez sieci i bez bazy.

DWA OBSZARY, oba krytyczne z innego powodu:

  1. UKŁAD WIADOMOŚCI. Alert jest produktem tego systemu. Zmiana, która przesuwa
     kilometry z pierwszej linii albo gubi cytat, nie wywala żadnego testu
     „technicznego" i objawia się dopiero tym, że operator przestaje czytać.
  2. ANTYSPAM. Ścieżki, których nie widać w produkcji, DOPÓKI coś się nie zepsuje
     — przekroczony limit godzinowy, crosspost, cisza nocna. Zepsują się właśnie
     wtedy, gdy nikt nie patrzy, więc muszą być sprawdzone tutaj.

Fakty z bazy wchodzą do `ocen()` jako argumenty, więc cała logika progów jest
testowalna bez Postgresa.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from laweta_radar.services import geo, powiadomienia as pw

TERAZ = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)

ZLECENIE = {
    "fb_id": "abc123",
    "pilnosc": "pilne",
    "miasto_od": "Krosno",
    "kod_pocztowy": "38-400",
    "miasto_do": "Rzeszów",
    "pojazd": "VW Golf IV",
    "stan": "nie odpala",
    "toczenie": "toczy się",
    "tresc": "potrzebuje lawety z Krosna do Rzeszowa, golf stanal i nie odpala",
    "telefon": "+48 555 111 222",
    "grupa_nazwa": "Pomoc drogowa Podkarpacie",
    "pewnosc": 88,
    "jezyk": "pl",
    "post_url": "https://www.facebook.com/groups/1/posts/2/",
    "opublikowany_at": TERAZ - timedelta(minutes=4),
}


@pytest.fixture(autouse=True)
def baza_pod_krosnem(monkeypatch):
    monkeypatch.setattr(geo.settings, "BAZA_LAT", 49.65)
    monkeypatch.setattr(geo.settings, "BAZA_LON", 21.60)


# ---------------------------------------------------------------------------
# Układ wiadomości
# ---------------------------------------------------------------------------
def test_pierwsza_linia_to_trzy_liczby_i_nic_wiecej():
    """Pilność, dystans, szacunek. Wszystko inne w tej linii konkuruje
    o dwie sekundy, których nie ma."""
    pierwsza = pw.zbuduj_tresc(ZLECENIE, teraz=TERAZ).splitlines()[0]
    assert "PILNE" in pierwsza
    assert "km" in pierwsza
    assert "zł" in pierwsza
    # Miasto, pojazd i grupa NIE mają prawa się tu znaleźć.
    assert "Krosno" not in pierwsza
    assert "Golf" not in pierwsza
    assert "Podkarpacie" not in pierwsza


def test_cytat_jest_obowiazkowy():
    """Model może się pomylić. Człowiek musi mieć dostęp do oryginału bez
    klikania, bo decyzja zapada na TYM ekranie."""
    tresc = pw.zbuduj_tresc(ZLECENIE, teraz=TERAZ)
    assert "golf stanal i nie odpala" in tresc


def test_cytat_przyciety_na_granicy_slowa():
    dlugi = dict(ZLECENIE, tresc="wyraz " * 200)
    tresc = pw.zbuduj_tresc(dlugi, teraz=TERAZ)
    cytat = [w for w in tresc.splitlines() if w.startswith('"')][0]
    assert len(cytat) < pw.MAX_CYTAT + 40
    assert cytat.endswith('…"')


def test_wiek_posta_zawsze():
    """„4 min temu" znaczy, że warto dzwonić. „2 h temu" znaczy, że pewnie już
    ktoś pojechał. Bez tej liczby alert nie mówi, czy jest po co sięgać."""
    assert "4 min temu" in pw.zbuduj_tresc(ZLECENIE, teraz=TERAZ)
    bez_daty = dict(ZLECENIE, opublikowany_at=None)
    assert "wiek nieznany" in pw.zbuduj_tresc(bez_daty, teraz=TERAZ)


@pytest.mark.parametrize(
    "delta,oczekiwane",
    [
        (timedelta(minutes=4), "4 min temu"),
        (timedelta(hours=2), "2 h temu"),
        (timedelta(days=1), "wczoraj"),
        (timedelta(days=3), "3 dni temu"),
        (timedelta(seconds=-30), "przed chwilą"),   # zegar FB bywa przed naszym
    ],
)
def test_formaty_wieku(delta, oczekiwane):
    assert pw.wiek_posta(TERAZ - delta, TERAZ) == oczekiwane


def test_niepewna_lokalizacja_dostaje_znak_zapytania_i_ostrzezenie():
    niepewne = dict(ZLECENIE, kod_pocztowy="", miasto_od="Krosno")
    tresc = pw.zbuduj_tresc(niepewne, teraz=TERAZ)
    assert "Krosno?" in tresc
    assert "niepewne" in tresc


def test_pewna_lokalizacja_bez_ostrzezenia():
    """Kod pocztowy potwierdza nazwę — ostrzeganie mimo to jest fałszywym
    alarmem, a operator, który raz je zignoruje, zignoruje też prawdziwe."""
    tresc = pw.zbuduj_tresc(ZLECENIE, teraz=TERAZ)
    assert "?" not in tresc.split("\n")[2]
    assert "niepewne" not in tresc


def test_znacznik_jezyka_tylko_dla_obcych():
    """docs/WIELOJEZYCZNOSC.md: alert niesie znacznik, bo od niego zależy,
    w jakim języku operator ma oddzwonić. „pl" na polskim alercie to szum."""
    assert "de" in pw.zbuduj_tresc(dict(ZLECENIE, jezyk="de"), teraz=TERAZ)
    assert "🇩🇪" in pw.zbuduj_tresc(dict(ZLECENIE, jezyk="de"), teraz=TERAZ)
    assert "🇩🇪" not in pw.zbuduj_tresc(ZLECENIE, teraz=TERAZ)


def test_brak_numeru_jest_informacja_a_nie_pustka():
    """Brak numeru zmienia to, CO operator zrobi: pisze wiadomość zamiast
    dzwonić."""
    tresc = pw.zbuduj_tresc(dict(ZLECENIE, telefon=""), teraz=TERAZ)
    assert "brak numeru" in tresc


@pytest.mark.parametrize(
    "surowy,czytelny,klucz",
    [
        ("+48 555 111 222", "555 111 222", "555111222"),
        ("555111222", "555 111 222", "555111222"),
        ("555-111-222", "555 111 222", "555111222"),
        ("tel. 48555111222", "555 111 222", "555111222"),
        ("", "", ""),
    ],
)
def test_normalizacja_telefonu(surowy, czytelny, klucz):
    """Ten sam człowiek wkleja swój numer na pięć grup w pięciu formatach.
    Bez wspólnej postaci dedup po numerze nie łapie niczego."""
    assert pw.telefon_czytelnie(surowy) == czytelny
    assert pw.normalizuj_telefon(surowy) == klucz


# ---------------------------------------------------------------------------
# Przyciski
# ---------------------------------------------------------------------------
def test_przyciski_maja_trase_post_i_smiec():
    wiersze = pw.zbuduj_przyciski(ZLECENIE)
    etykiety = [p["text"] for w in wiersze for p in w]
    assert any("Trasa" in e for e in etykiety)
    assert any("post" in e for e in etykiety)
    assert any("Śmieć" in e for e in etykiety)
    dane = [p.get("callback_data") for w in wiersze for p in w]
    assert "smiec:abc123" in dane
    assert "biore:abc123" in dane


def test_brak_post_url_loguje_blad_i_pomija_przycisk(capsys):
    """Telegram odrzuca CAŁĄ klawiaturę, gdy `url` jest pusty — więc przycisk
    jest pomijany, nie wysyłany z pustką. Ale to jest BŁĄD: operator nie
    odpisuje z systemu, tylko wchodzi na Facebooka."""
    wiersze = pw.zbuduj_przyciski(dict(ZLECENIE, post_url=""))
    etykiety = [p["text"] for w in wiersze for p in w]
    assert not any("post" in e for e in etykiety)
    assert "BŁĄD" in capsys.readouterr().err


def test_kazdy_przycisk_ma_url_albo_callback():
    """Klawiatura z przyciskiem bez jednego i bez drugiego jest odrzucana
    w całości — czyli alert dociera BEZ ŻADNYCH przycisków."""
    for wiersz in pw.zbuduj_przyciski(ZLECENIE):
        for p in wiersz:
            assert p.get("url") or p.get("callback_data")


# ---------------------------------------------------------------------------
# Progi i antyspam
# ---------------------------------------------------------------------------
def _ocen(zlecenie=None, **kw):
    kw.setdefault("juz_wyslane", False)
    kw.setdefault("crosspost_id", None)
    kw.setdefault("w_ostatniej_godzinie", 0)
    kw.setdefault("teraz", TERAZ.replace(hour=12))
    return pw.ocen(zlecenie or ZLECENIE, **kw)


def test_normalne_zlecenie_idzie():
    assert _ocen().wysylac is True


def test_dedup_po_fb_id():
    d = _ocen(juz_wyslane=True)
    assert d.wysylac is False
    assert d.kod == "duplikat"


def test_crosspost_nie_wysyla_drugiej_wiadomosci():
    d = _ocen(crosspost_id=7)
    assert d.wysylac is False
    assert d.kod == "crosspost"


def test_dedup_wygrywa_z_progami():
    """Post już wysłany nie ma być ponownie oceniany progiem pewności —
    prompt klasyfikatora mógł się zmienić i ta sama treść dostałaby drugi alert."""
    d = _ocen(dict(ZLECENIE, pewnosc=5), juz_wyslane=True)
    assert d.kod == "duplikat"


def test_niska_pewnosc_nie_brzeczy_ale_nie_kasuje():
    """ZASADA NACZELNA: próg steruje WYŁĄCZNIE brzęczeniem. Zlecenie zostaje
    w bazie i w panelu."""
    d = _ocen(dict(ZLECENIE, pewnosc=10))
    assert d.wysylac is False
    assert d.kod == "pewnosc"
    assert "panelu" in d.powod


def test_brak_pewnosci_nie_blokuje():
    """Klasyfikator, który nie podał pewności, nie może wyciszyć systemu."""
    zl = dict(ZLECENIE)
    zl.pop("pewnosc")
    assert _ocen(zl).wysylac is True


def test_limit_godzinowy():
    d = _ocen(w_ostatniej_godzinie=pw.settings.MAX_POWIADOMIEN_H)
    assert d.wysylac is False
    assert d.kod == "limit"


def test_pauza_wycisza_ale_nie_kasuje():
    d = _ocen(pauza=True)
    assert d.wysylac is False
    assert d.kod == "pauza"
    assert "panelu" in d.powod


@pytest.mark.parametrize("godzina,cisza", [(23, True), (2, True), (5, True),
                                           (6, False), (12, False), (21, False),
                                           (22, True)])
def test_cisza_nocna_przez_polnoc(godzina, cisza, monkeypatch):
    monkeypatch.setattr(pw.settings, "CISZA_NOCNA_OD", 22)
    monkeypatch.setattr(pw.settings, "CISZA_NOCNA_DO", 6)
    lokalny = datetime(2026, 8, 4, godzina, 0).astimezone()
    assert pw.cisza_nocna(lokalny) is cisza


def test_cisza_wylaczona_gdy_od_rowna_sie_do(monkeypatch):
    """OD == DO to „bez ciszy nocnej", a nie „cisza przez całą dobę"."""
    monkeypatch.setattr(pw.settings, "CISZA_NOCNA_OD", 0)
    monkeypatch.setattr(pw.settings, "CISZA_NOCNA_DO", 0)
    assert pw.cisza_nocna(datetime(2026, 8, 4, 3, 0).astimezone()) is False


def test_brak_progu_na_kilometry():
    """Trasa Kolonia-Kraków to 1100 km i normalny dzień pracy tego operatora."""
    daleko = dict(ZLECENIE, miasto_od="Köln", miasto_do="Kraków", kod_pocztowy="")
    assert _ocen(daleko).wysylac is True


# ---------------------------------------------------------------------------
# Klucz dedupu treściowego
# ---------------------------------------------------------------------------
def test_klucz_tresci_ignoruje_roznice_w_tresci_posta():
    """Crosspost różni się WŁAŚNIE treścią („PILNE!!!" dopisane w jednej
    grupie) — hash z treści dałby pięć różnych kluczy, czyli dokładnie ten
    problem, który ten klucz ma rozwiązywać."""
    a = pw.klucz_tresci(ZLECENIE)
    b = pw.klucz_tresci(dict(ZLECENIE, tresc="PILNE!!! " + ZLECENIE["tresc"],
                             fb_id="inne", grupa_nazwa="Laweta Podkarpacie"))
    assert a == b


def test_klucz_tresci_rozny_dla_roznych_tras():
    a = pw.klucz_tresci(ZLECENIE)
    b = pw.klucz_tresci(dict(ZLECENIE, miasto_do="Gdańsk"))
    assert a != b


def test_klucz_tresci_pusty_gdy_nie_ma_z_czego():
    """Klucz zbudowany z pustek skleiłby ze sobą WSZYSTKIE nierozpoznane posty
    i wyciszył je po pierwszym."""
    assert pw.klucz_tresci({"fb_id": "x"}) == ""


# ---------------------------------------------------------------------------
# Odporność
# ---------------------------------------------------------------------------
def test_bez_fb_id_zwraca_false_bez_wyjatku(capsys):
    assert pw.powiadom_o_zleceniu({"tresc": "cokolwiek"}) is False
    assert "fb_id" in capsys.readouterr().err


def test_wyjatek_w_srodku_nie_wychodzi_na_zewnatrz(monkeypatch, capsys):
    """TO JEST GWARANCJA Z DOCSTRINGU MODUŁU: fetcher w środku przebiegu ma
    dwieście postów do zapisania i nie może zginąć przez błąd w budowaniu
    jednej wiadomości."""
    monkeypatch.setattr(pw.telegram_notify, "skonfigurowany",
                        lambda: (_ for _ in ()).throw(RuntimeError("bum")))
    assert pw.powiadom_o_zleceniu(ZLECENIE) is False
    assert "bum" in capsys.readouterr().err


def test_bez_bazy_nie_wysylamy(monkeypatch, capsys):
    """Kanał bez dedupu zajeżdża się w jeden dzień: fetcher nie zapisał posta,
    więc następny przebieg pobierze go znowu i wyśle znowu."""
    monkeypatch.setattr(pw.telegram_notify, "skonfigurowany", lambda: True)
    monkeypatch.setattr(pw.settings, "DATABASE_URL", "")
    assert pw.powiadom_o_zleceniu(ZLECENIE) is False
    assert "dedupu" in capsys.readouterr().err


def test_zbuduj_tresc_znosi_puste_zlecenie():
    """Klasyfikator zwracający same puste pola nie może wywalić alertu —
    degradacja tak, wyjątek nie."""
    tresc = pw.zbuduj_tresc({"fb_id": "x"}, teraz=TERAZ)
    assert "ZLECENIE" in tresc
    assert "? km" in tresc
