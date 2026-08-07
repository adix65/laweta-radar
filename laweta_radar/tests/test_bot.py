"""Testy `workers/bot.py` — bez sieci i bez bazy.

NAJWAŻNIEJSZY TEST W TYM PLIKU to `test_ignoruje_obcy_czat`. Adres bota jest
z natury publiczny (wystarczy znać jego nazwę, żeby do niego napisać), a komendy
tego bota zmieniają statusy zleceń i wyciszają powiadomienia. Regresja w tym
jednym miejscu zamienia bota w publiczny pilot do cudzej pracy — i nie objawia
się niczym, dopóki ktoś tego nie znajdzie.

Reszta pilnuje kolejności działań przy callbacku: `answerCallbackQuery` ZAWSZE
i najpierw, potem zmiana statusu, na końcu edycja wiadomości ze zdjęciem
przycisków. Każdy z tych trzech kroków pominięty osobno daje inny, cichy błąd.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from laweta_radar.workers import bot


class _Kursor:
    def __init__(self, wyniki):
        self.wyniki = list(wyniki)
        self.zapytania: list[tuple] = []

    def execute(self, sql, parametry=None):
        self.zapytania.append((sql, parametry))

    def fetchone(self):
        return self.wyniki.pop(0) if self.wyniki else None

    def fetchall(self):
        return self.wyniki

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Polaczenie:
    def __init__(self, wyniki=()):
        self.kursor = _Kursor(wyniki)
        self.commity = 0

    def cursor(self):
        return self.kursor

    def commit(self):
        self.commity += 1

    def rollback(self):
        pass


@pytest.fixture
def telegram(monkeypatch):
    """Przechwycone wywołania Bot API zamiast prawdziwej sieci."""
    wywolania: list[tuple[str, dict]] = []
    monkeypatch.setattr(bot.telegram_notify, "wywolaj",
                        lambda metoda, payload, timeout=None:
                        (wywolania.append((metoda, payload)), {})[1])
    monkeypatch.setattr(bot.telegram_notify, "wyslij",
                        lambda tekst, przyciski=None, parse_mode="Markdown":
                        (wywolania.append(("sendMessage", {"text": tekst})), 1)[1])
    return wywolania


@pytest.fixture(autouse=True)
def czat(monkeypatch):
    monkeypatch.setattr(bot.settings, "TELEGRAM_CHAT_ID", "12345")


def _callback(dane: str, czat_id: str = "12345") -> dict:
    return {
        "id": "cb1",
        "data": dane,
        "message": {"message_id": 99, "text": "stara treść",
                    "chat": {"id": int(czat_id)}},
    }


# ---------------------------------------------------------------------------
# Bezpieczeństwo
# ---------------------------------------------------------------------------
def test_ignoruje_obcy_czat(telegram, capsys):
    """Bez tego filtru bot jest publicznym pilotem do cudzych zleceń.
    Odpowiedź potwierdziłaby obcemu, że bot istnieje i reaguje — więc cisza."""
    conn = _Polaczenie()
    bot.obsluz_aktualizacje(conn, {"update_id": 1, "message": {
        "text": "/stop", "chat": {"id": 999}}})
    assert telegram == []
    assert conn.kursor.zapytania == []
    assert "spoza czatu" in capsys.readouterr().err


def test_ignoruje_obcy_czat_takze_w_callbacku(telegram):
    conn = _Polaczenie()
    bot.obsluz_aktualizacje(conn, {"update_id": 2,
                                   "callback_query": _callback("smiec:x", "999")})
    assert telegram == []


# ---------------------------------------------------------------------------
# Callbacki z przycisków
# ---------------------------------------------------------------------------
def test_smiec_odpowiada_zmienia_status_i_edytuje(telegram, monkeypatch):
    monkeypatch.setattr(bot.feedback, "zapisz", lambda *a, **k: True)
    conn = _Polaczenie([("abc",)])
    bot.obsluz_callback(conn, _callback("smiec:abc"))

    metody = [m for m, _ in telegram]
    # answerCallbackQuery PIERWSZE: bez tego Telegram trzyma kręcące się kółko
    # kilkanaście sekund, a operator klika drugi raz.
    assert metody[0] == "answerCallbackQuery"
    assert "editMessageText" in metody

    sql = conn.kursor.zapytania[0][0]
    assert "UPDATE posty" in sql
    assert conn.kursor.zapytania[0][1] == ("smiec", "abc")


def test_edycja_zdejmuje_przyciski(telegram, monkeypatch):
    """Alert, który po kliknięciu wygląda identycznie, uczy klikać jeszcze raz
    — a przy „Śmieć" drugie kliknięcie to zlecenie wyrzucone przez pomyłkę."""
    monkeypatch.setattr(bot.feedback, "zapisz", lambda *a, **k: True)
    bot.obsluz_callback(_Polaczenie([("abc",)]), _callback("smiec:abc"))
    edycja = [p for m, p in telegram if m == "editMessageText"][0]
    assert "reply_markup" not in edycja
    assert "śmieć" in edycja["text"]
    assert "stara treść" in edycja["text"]      # oryginał zostaje widoczny


def _callback_ze_zdjeciem(dane: str, podpis: str = "stary podpis") -> dict:
    """Callback spod alertu wysłanego jako ZDJĘCIE z mapą trasy.

    Telegram oddaje wtedy `caption`, a pola `text` NIE MA W OGÓLE — i to jest
    cała różnica, przez którą `editMessageText` odpowiada „there is no text in
    the message to edit".
    """
    return {"id": "cb1", "data": dane,
            "message": {"message_id": 99, "caption": podpis,
                        "photo": [{"file_id": "AgAC"}], "chat": {"id": 12345}}}


def test_alert_ze_zdjeciem_edytuje_podpis_a_nie_tekst(telegram, monkeypatch):
    """Alert z mapą trasy nie ma pola `text`. Bez tej gałęzi edycja pada,
    przyciski zostają na ekranie — a alert, który po kliknięciu wygląda
    identycznie, uczy klikać jeszcze raz. Przy „Śmieć" drugie kliknięcie to
    zlecenie wyrzucone przez pomyłkę."""
    monkeypatch.setattr(bot.feedback, "zapisz", lambda *a, **k: True)
    bot.obsluz_callback(_Polaczenie([("abc",)]), _callback_ze_zdjeciem("smiec:abc"))

    metody = [m for m, _ in telegram]
    assert "editMessageCaption" in metody
    assert "editMessageText" not in metody

    edycja = [p for m, p in telegram if m == "editMessageCaption"][0]
    assert "reply_markup" not in edycja        # przyciski znikają, jak przy tekście
    assert "śmieć" in edycja["caption"]
    assert "stary podpis" in edycja["caption"]  # oryginał zostaje widoczny


def test_dopisek_pod_zdjeciem_miesci_sie_w_limicie_podpisu(telegram, monkeypatch):
    """Podpis ma limit 1024 znaków i alert potrafi go dotykać. Doklejony dopisek
    przekroczyłby limit, Telegram odrzuciłby edycję i przyciski zostałyby na
    ekranie — czyli dokładnie ten skutek, przed którym ta gałąź broni."""
    monkeypatch.setattr(bot.feedback, "zapisz", lambda *a, **k: True)
    pod_korek = "x" * bot.telegram_notify.MAX_CAPTION
    bot.obsluz_callback(_Polaczenie([("abc",)]),
                        _callback_ze_zdjeciem("smiec:abc", pod_korek))

    edycja = [p for m, p in telegram if m == "editMessageCaption"][0]
    assert len(edycja["caption"]) <= bot.telegram_notify.MAX_CAPTION
    assert "śmieć" in edycja["caption"]


def test_biore_ustawia_dzwonie(telegram, monkeypatch):
    monkeypatch.setattr(bot.feedback, "zapisz", lambda *a, **k: True)
    conn = _Polaczenie([("abc",)])
    bot.obsluz_callback(conn, _callback("biore:abc"))
    assert conn.kursor.zapytania[0][1] == ("dzwonie", "abc")


def test_smiec_zapisuje_feedback(monkeypatch, telegram):
    """Każde kliknięcie „Śmieć" to materiał do poprawiania promptu. To jest
    jedyna pętla zwrotna w tym systemie."""
    zapisane = []
    monkeypatch.setattr(bot.feedback, "zapisz",
                        lambda conn, fb_id, ocena: zapisane.append((fb_id, ocena)))
    bot.obsluz_callback(_Polaczenie([("abc",)]), _callback("smiec:abc"))
    assert zapisane == [("abc", "smiec")]


def test_biore_nie_zapisuje_feedbacku(monkeypatch, telegram):
    """„Biorę" nie jest oceną klasyfikatora — model miał rację, a to jest stan
    domyślny, nie przykład treningowy."""
    zapisane = []
    monkeypatch.setattr(bot.feedback, "zapisz",
                        lambda conn, fb_id, ocena: zapisane.append((fb_id, ocena)))
    bot.obsluz_callback(_Polaczenie([("abc",)]), _callback("biore:abc"))
    assert zapisane == []


def test_nieznany_callback_nie_wywala(telegram, capsys):
    bot.obsluz_callback(_Polaczenie(), _callback("cokolwiek:x"))
    assert "nieznany callback" in capsys.readouterr().err


def test_callback_dla_nieznanego_fb_id(telegram, capsys):
    """Post skasowany z bazy (retencja), a wiadomość została na telefonie."""
    bot.obsluz_callback(_Polaczenie([None]), _callback("smiec:zniknelo"))
    assert "nieznanego fb_id" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Komendy
# ---------------------------------------------------------------------------
def test_komenda_z_nazwa_bota(monkeypatch):
    """W grupie Telegram dokleja `@nazwa_bota` do komendy."""
    monkeypatch.setattr(bot, "_dzis", lambda conn: "PODSUMOWANIE")
    assert bot.obsluz_komende(_Polaczenie(), "/dzis@laweta_bot") == "PODSUMOWANIE"


def test_ostatnie_domyslna_liczba(monkeypatch):
    zapamietane = []
    monkeypatch.setattr(bot, "_ostatnie",
                        lambda conn, ile: zapamietane.append(ile) or "ok")
    bot.obsluz_komende(_Polaczenie(), "/ostatnie")
    bot.obsluz_komende(_Polaczenie(), "/ostatnie 5")
    bot.obsluz_komende(_Polaczenie(), "/ostatnie dużo")   # śmieć -> domyślna
    assert zapamietane == [bot.DOMYSLNIE_OSTATNICH, 5, bot.DOMYSLNIE_OSTATNICH]


@pytest.fixture
def geo_pod_krosnem(tmp_path):
    """Mikro-baza geo: Krosno jest, Turku nie ma. Dokładnie ten układ, przy
    którym `/ostatnie` pokazywało dojazd z bazy zamiast długości kursu."""
    plik = tmp_path / "kody.csv"
    plik.write_text("kraj,kod,miejscowosc,wojewodztwo,lat,lng\n"
                    "PL,38-400,Krosno,podkarpackie,49.6886,21.7706\n",
                    encoding="utf-8")
    bot.geo.zaladuj(plik)
    yield
    bot.geo.zaladuj()


def _wiersz_ostatnich(tresc: str) -> tuple:
    return ("fb1", "Podkarpacie", None, "nowe", "38-400", "Krosno",
            "62-700", "Turek", tresc)


def test_ostatnie_nie_podstawia_dojazdu_pod_nieznana_trase(geo_pod_krosnem):
    """Ta sama zasada co w alercie i w panelu: bez obu rozpoznanych końców
    trasy nie ma kilometrów. Lista, w której „60 km" znaczy raz długość kursu,
    a raz drogę z bazy do odbioru, uczy operatora nie ufać żadnej z nich."""
    tekst = bot._ostatnie(_Polaczenie([_wiersz_ostatnich("laweta z Krosna")]), 5)
    assert "trasa nieustalona" in tekst
    assert "km" not in tekst.split("trasa nieustalona")[0]


def test_ostatnie_pokazuje_odleglosc_autora_gdy_trasy_nie_znamy(geo_pod_krosnem):
    """Autor podał 490 km wprost — to jedyna liczba, jaką tu mamy, i jedyna,
    którą wolno pokazać: podpisaną jako cudza."""
    tekst = bot._ostatnie(
        _Polaczenie([_wiersz_ostatnich("z Krosna do Turku, trasa ma okolo 490 km")]), 5)
    assert "wg autora: 490 km" in tekst


# ---------------------------------------------------------------------------
# /wyjazdy /przywozy /krajowe /tranzyt
# ---------------------------------------------------------------------------
def test_komendy_kierunku_routing(monkeypatch):
    """Cztery komendy, jedna funkcja — routing przekazuje właściwy kierunek
    i domyślną albo podaną liczbę godzin, tak jak `/ostatnie` przekazuje `ile`."""
    zapamietane = []
    monkeypatch.setattr(
        bot, "_lista_kierunku",
        lambda conn, kierunek, tytul, godziny: zapamietane.append((kierunek, godziny)) or "ok")
    bot.obsluz_komende(_Polaczenie(), "/wyjazdy")
    bot.obsluz_komende(_Polaczenie(), "/przywozy 48")
    bot.obsluz_komende(_Polaczenie(), "/krajowe dużo")   # śmieć -> domyślna
    bot.obsluz_komende(_Polaczenie(), "/tranzyt@laweta_bot 6")
    assert zapamietane == [
        (bot.geo.KIERUNEK_WYJAZD, bot.DOMYSLNIE_GODZIN_KIERUNKU),
        (bot.geo.KIERUNEK_PRZYWOZ, 48),
        (bot.geo.KIERUNEK_KRAJOWY, bot.DOMYSLNIE_GODZIN_KIERUNKU),
        (bot.geo.KIERUNEK_TRANZYT, 6),
    ]


@pytest.fixture
def geo_pl_de(tmp_path):
    """Krosno (PL) i Köln (DE) — starcza do policzenia wyjazdu i przywozu."""
    plik = tmp_path / "kody.csv"
    plik.write_text(
        "kraj,kod,miejscowosc,wojewodztwo,lat,lng\n"
        "PL,38-400,Krosno,podkarpackie,49.6886,21.7706\n"
        "DE,50667,Koln,Nordrhein-Westfalen,50.9375,6.9603\n",
        encoding="utf-8")
    bot.geo.zaladuj(plik)
    yield
    bot.geo.zaladuj()


def _wiersz_kierunku(fb_id="fb1", post_url="https://fb.com/p/1", status="nowe",
                     o_kod="38-400", o_miasto="Krosno",
                     d_kod="50667", d_miasto="Koln", tresc="laweta z Krosna do Kolonii") -> tuple:
    return (fb_id, post_url, status, o_kod, o_miasto, d_kod, d_miasto, tresc)


def test_lista_kierunku_pokazuje_trase_km_cene_i_link(geo_pl_de):
    tekst = bot._lista_kierunku(
        _Polaczenie([(1,), _wiersz_kierunku()]), bot.geo.KIERUNEK_WYJAZD,
        "🧭 Wyjazdy z Polski", 24)
    assert "Krosno" in tekst and "Koln" in tekst
    assert "km" in tekst
    assert "zł" in tekst
    assert "https://fb.com/p/1" in tekst


def test_lista_kierunku_pusta(geo_pl_de):
    tekst = bot._lista_kierunku(
        _Polaczenie([(0,)]), bot.geo.KIERUNEK_WYJAZD, "🧭 Wyjazdy z Polski", 24)
    assert "brak" in tekst.lower()


def test_lista_kierunku_liczy_pominiete_dokladnie(geo_pl_de):
    """`razem` z osobnego COUNT, nie `len(wiersze)` po limicie — inaczej liczba
    pominiętych zgadywałaby się z tego, co akurat zmieściło się w limicie."""
    tekst = bot._lista_kierunku(
        _Polaczenie([(3,), _wiersz_kierunku(fb_id="fb1"), _wiersz_kierunku(fb_id="fb2")]),
        bot.geo.KIERUNEK_WYJAZD, "🧭 Wyjazdy z Polski", 24)
    assert "1 pominiętych" in tekst


def test_stop_wlacza_pauze():
    conn = _Polaczenie([None])           # brak wpisu = pauza nieaktywna
    odp = bot.obsluz_komende(conn, "/stop")
    assert "wyciszone" in odp
    assert conn.kursor.zapytania[-1][1] == ("pauza", "/stop od operatora")


def test_stop_mowi_ze_nic_nie_ginie():
    """Operator ma wiedzieć, że wycisza TELEFON, a nie system. Bez tego zdania
    `/stop` wygląda jak wyłączenie zbierania zleceń."""
    odp = bot.obsluz_komende(_Polaczenie([None]), "/stop")
    assert "panelu" in odp


def test_stop_dwa_razy_nie_dubluje_wpisu():
    conn = _Polaczenie([("pauza",)])
    odp = bot.obsluz_komende(conn, "/stop")
    assert "już wyciszone" in odp
    assert not any("INSERT" in sql for sql, _ in conn.kursor.zapytania)


def test_start_wznawia():
    conn = _Polaczenie([("pauza",)])
    odp = bot.obsluz_komende(conn, "/start")
    assert "wznowione" in odp
    assert conn.kursor.zapytania[-1][1][0] == "wznowienie"


def test_nieznana_komenda_daje_pomoc():
    assert "/dzis" in bot.obsluz_komende(_Polaczenie(), "/cokolwiek")


def test_pauza_aktywna_czyta_ostatni_wpis():
    assert bot.pauza_aktywna(_Polaczenie([("pauza",)])) is True
    assert bot.pauza_aktywna(_Polaczenie([("wznowienie",)])) is False
    assert bot.pauza_aktywna(_Polaczenie([None])) is False


# ---------------------------------------------------------------------------
# Pętla
# ---------------------------------------------------------------------------
def test_zly_update_nie_ubija_petli(monkeypatch, capsys):
    """Proces w PM2 ma żyć mimo błędu przy JEDNYM zdarzeniu."""
    monkeypatch.setattr(bot.telegram_notify, "wywolaj",
                        lambda *a, **k: [{"update_id": 5, "message": {
                            "text": "/dzis", "chat": {"id": 12345}}}])
    monkeypatch.setattr(bot, "_polacz", lambda: _Polaczenie())
    monkeypatch.setattr(bot, "obsluz_aktualizacje",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("bum")))
    assert bot.przebieg(None) == 6           # offset mimo błędu idzie do przodu
    assert "bum" in capsys.readouterr().err


def test_bez_bazy_nie_potwierdzamy_offsetu(monkeypatch, capsys):
    """Kliknięcia mają POCZEKAĆ na powrót bazy, a nie zginąć. Telegram trzyma
    updaty 24 h, co jest z zapasem."""
    monkeypatch.setattr(bot.telegram_notify, "wywolaj",
                        lambda *a, **k: [{"update_id": 5, "message": {
                            "text": "/dzis", "chat": {"id": 12345}}}])
    monkeypatch.setattr(bot, "_polacz", lambda: None)
    assert bot.przebieg(7) == 7
    assert "nie potwierdzam" in capsys.readouterr().err


def test_brak_aktualizacji_zostawia_offset(monkeypatch):
    monkeypatch.setattr(bot.telegram_notify, "wywolaj", lambda *a, **k: [])
    assert bot.przebieg(42) == 42


# ---------------------------------------------------------------------------
# /limity — stan puli kont Apify
#
# SEDNO TYCH TESTÓW: trzy rozłączne stany konta (saldo znane / saldo nieznane /
# klucz martwy) muszą wyjść w wiadomości POPRAWNIE rozróżnione, a nie zlepione
# w jedno "błąd" — dokładnie ta pomyłka wywołała to zadanie. Token nigdy nie
# ma prawa trafić do wyniku.
# ---------------------------------------------------------------------------
TOKEN_TAJNY = "apify_api_SEKRETNY_TOKEN_Z_TESTU"

Saldo = bot.apify_credits.Saldo
StanKonta = bot.apify_credits.StanKonta


def _konto_ok(nazwa="poignant_kefir", uzyte=3.10, limit=5.0):
    return StanKonta(nazwa, bot.apify_credits.STAN_OK_ZNANE, Saldo(uzyte, limit))


def _konto_saldo_nieznane(nazwa="cichy_kefir"):
    return StanKonta(nazwa, bot.apify_credits.STAN_OK_NIEZNANE)


def _konto_martwe():
    return StanKonta("", bot.apify_credits.STAN_MARTWY, powod="401 — klucz nie działa")


def _konto_brak_odpowiedzi():
    return StanKonta("", bot.apify_credits.STAN_BRAK_ODPOWIEDZI, powod="Timeout: read timed out")


def test_limity_bez_skonfigurowanych_kluczy(monkeypatch):
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: [])
    tekst = bot._limity(_Polaczenie())
    assert "Brak skonfigurowanych kluczy" in tekst


def test_limity_rozroznia_trzy_stany_konta(monkeypatch):
    """Test zgodny z zadaniem: zamockowane odpowiedzi dla trzech stanów konta ->
    wiadomość zawiera wszystkie trzy poprawnie rozróżnione."""
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: [TOKEN_TAJNY, "t2", "t3"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu",
                        lambda tokens, **k: [_konto_ok(), _konto_saldo_nieznane(), _konto_martwe()])
    # Zapytania w kolejności: tempo zużycia (count, min), pobrane dziś (count).
    conn = _Polaczenie([(0, None), (0,)])
    tekst = bot._limity(conn)

    # Markdown escapuje `_` w nazwach kont (poignant_kefir -> poignant\_kefir),
    # dlatego sprawdzamy fragmenty bez podkreślnika zamiast całej nazwy.
    assert "poignant" in tekst and "kefir" in tekst
    assert "$3.10 / $5.00" in tekst
    assert "saldo nieznane" in tekst
    assert "BŁĄD 401" in tekst
    # Konto ze saldem nieznanym NIE ma prawa wyglądać jak błąd — to jest cały
    # sens tego zadania (darmowe konto bez /limits jest sprawne).
    wiersz_saldo_nieznane = next(w for w in tekst.splitlines() if "saldo nieznane" in w)
    assert "BŁĄD" not in wiersz_saldo_nieznane
    assert TOKEN_TAJNY not in tekst


def test_limity_token_nigdy_nie_wycieka(monkeypatch):
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: [TOKEN_TAJNY])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu", lambda tokens, **k: [_konto_ok()])
    conn = _Polaczenie([(0, None), (0,)])
    tekst = bot._limity(conn)
    assert TOKEN_TAJNY not in tekst


def test_limity_ostrzega_konto_prawie_wyczerpane(monkeypatch):
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["t1"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu",
                        lambda tokens, **k: [_konto_ok(uzyte=4.8, limit=5.0)])
    conn = _Polaczenie([(0, None), (0,)])
    tekst = bot._limity(conn)
    assert "⚠ prawie wyczerpane" in tekst


def test_limity_bez_ostrzezenia_ponizej_progu(monkeypatch):
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["t1"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu",
                        lambda tokens, **k: [_konto_ok(uzyte=3.1, limit=5.0)])
    conn = _Polaczenie([(0, None), (0,)])
    tekst = bot._limity(conn)
    assert "⚠ prawie wyczerpane" not in tekst


def test_limity_ostrzega_cala_pule_powyzej_progu(monkeypatch):
    """85% zużycia CAŁEJ puli — poniżej progu 90% pojedynczego konta, ale
    powyżej progu 80% dla całej puli: obie linie mają swój własny sygnał."""
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["t1"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu",
                        lambda tokens, **k: [_konto_ok(uzyte=4.25, limit=5.0)])
    conn = _Polaczenie([(0, None), (0,)])
    tekst = bot._limity(conn)
    assert "⚠ prawie wyczerpane" not in tekst   # próg konta (90%) nie przekroczony
    assert "pula na wyczerpaniu" in tekst        # próg puli (80%) przekroczony


def test_limity_liczy_zywe_klucze_gdy_ktorys_martwy(monkeypatch):
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["t1", "t2", "t3"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu",
                        lambda tokens, **k: [_konto_ok(), _konto_ok(), _konto_martwe()])
    conn = _Polaczenie([(0, None), (0,)])
    tekst = bot._limity(conn)
    assert "Żywe klucze: 2 z 3" in tekst


def test_limity_brak_odpowiedzi_nie_jest_martwym_kluczem(monkeypatch):
    """Timeout ma pokazać się jako 'brak odpowiedzi', nie jako martwy klucz —
    inaczej awaria sieci wygląda jak trwale ubite konto."""
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["t1", "t2"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu",
                        lambda tokens, **k: [_konto_ok(), _konto_brak_odpowiedzi()])
    conn = _Polaczenie([(0, None), (0,)])
    tekst = bot._limity(conn)
    assert "brak odpowiedzi" in tekst
    assert "Żywe klucze" not in tekst    # żaden klucz nie jest MARTWY, tylko jeden nie odpowiedział
    assert "BŁĄD 401" not in tekst


def test_limity_za_wczesnie_na_prognoze_przy_krotkiej_historii(monkeypatch):
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["t1"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu", lambda tokens, **k: [_konto_ok()])
    swiezy = datetime.now(timezone.utc) - timedelta(hours=2)
    conn = _Polaczenie([(50, swiezy), (10,)])
    tekst = bot._limity(conn)
    assert "za wcześnie na prognozę" in tekst
    assert "starczy na" not in tekst


def test_limity_liczy_tempo_i_prognoze_dni(monkeypatch):
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["t1"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu",
                        lambda tokens, **k: [_konto_ok(uzyte=2.0, limit=5.0)])
    stary = datetime.now(timezone.utc) - timedelta(hours=20)
    # 100 postów w oknie 24h * CENA_USD_ZA_POST -> tempo dzienne
    conn = _Polaczenie([(100, stary), (30,)])
    tekst = bot._limity(conn)
    oczekiwane_tempo = 100 * bot.cfg_groups.CENA_USD_ZA_POST
    assert f"{oczekiwane_tempo:.2f}" in tekst
    assert "starczy na" in tekst


def test_limity_pokazuje_pobrane_dzisiaj_i_budzet(monkeypatch):
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["t1"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu", lambda tokens, **k: [_konto_ok()])
    conn = _Polaczenie([(0, None), (340,)])
    tekst = bot._limity(conn)
    assert f"Pobrane dziś: 340 postów · budżet {bot.settings.POSTY_NA_DOBE}/dobę" in tekst


# ---------------------------------------------------------------------------
# /limity — sekcja proxy (widoczność puli: ile w puli, ile w kwarantannie,
# które konto z którego wychodzi — host:port, NIGDY hasło)
# ---------------------------------------------------------------------------
def test_limity_sekcja_proxy_niekonfigurowane(monkeypatch):
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["t1"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu", lambda tokens, **k: [_konto_ok()])
    pusta_cfg = bot.apify_proxy.load_proxy_config({})
    monkeypatch.setattr(bot.apify_proxy, "load_proxy_config", lambda **k: pusta_cfg)
    conn = _Polaczenie([(0, None), (0,)])
    tekst = bot._limity(conn)
    assert "nieskonfigurowane" in tekst


def test_limity_sekcja_proxy_pokazuje_pule_i_przypisanie_bez_hasla(monkeypatch):
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["tok_a"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu",
                        lambda tokens, **k: [_konto_ok(nazwa="konto1")])
    cfg = bot.apify_proxy.load_proxy_config(
        {"APIFY_PROXY_URLS": "http://user:tajnehaslo@a.example:8000,"
                             "http://user:tajnehaslo@b.example:8000"})
    monkeypatch.setattr(bot.apify_proxy, "load_proxy_config", lambda **k: cfg)
    monkeypatch.setattr(bot.apify_proxy, "wczytaj_stan_proxy", lambda conn, urls: {})
    conn = _Polaczenie([(0, None), (0,)])
    tekst = bot._limity(conn)

    assert "Pula: 2 adresów" in tekst
    assert "aktywnych 2" in tekst and "w kwarantannie 0" in tekst
    przypisany = bot.apify_proxy.proxy_for_token("tok_a", cfg)
    assert bot.apify_proxy.proxy_label(przypisany) in tekst
    assert "tajnehaslo" not in tekst and "user:" not in tekst


def test_limity_sekcja_proxy_oznacza_kwarantanne(monkeypatch):
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["tok_a"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu", lambda tokens, **k: [_konto_ok()])
    cfg = bot.apify_proxy.load_proxy_config({"APIFY_PROXY_URLS": "http://u:p@a.example:8000"})
    przypisany = bot.apify_proxy.proxy_for_token("tok_a", cfg)
    monkeypatch.setattr(bot.apify_proxy, "load_proxy_config", lambda **k: cfg)
    monkeypatch.setattr(bot.apify_proxy, "wczytaj_stan_proxy",
                        lambda conn, urls: {przypisany: {"status": "kwarantanna"}})
    conn = _Polaczenie([(0, None), (0,)])
    tekst = bot._limity(conn)
    assert "w kwarantannie 1" in tekst
    assert "⚠ w kwarantannie" in tekst


def test_limity_sekcja_proxy_pokazuje_wiek_darmowej_puli(monkeypatch):
    """Darmowa pula (APIFY_PROXY_POOL=1) gnije w godzinach — operator sprawdzający
    z Telegrama musi widzieć jej wiek, nie tylko liczbę adresów i kwarantannę."""
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["tok_a"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu", lambda tokens, **k: [_konto_ok()])
    cfg = bot.apify_proxy.load_proxy_config(
        {"APIFY_PROXY_URLS": "http://u:p@a.example:8000,http://u:p@b.example:8000"})
    cfg = dataclasses.replace(cfg, pool_from_file=2, pool_age_h=1.5)
    monkeypatch.setattr(bot.apify_proxy, "load_proxy_config", lambda **k: cfg)
    monkeypatch.setattr(bot.apify_proxy, "wczytaj_stan_proxy", lambda conn, urls: {})
    conn = _Polaczenie([(0, None), (0,)])
    tekst = bot._limity(conn)
    assert "Darmowa pula: 2 z 2 adresów (odświeżona 1.5 h temu)" in tekst


def test_limity_sekcja_proxy_pokazuje_ostrzezenia_konfiguracji(monkeypatch):
    """Ostrzeżenia z `load_proxy_config` (np. stara pula, brak {session}) dotąd
    lądowały tylko w logu workera — operator na Telegramie ich nie widział."""
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["tok_a"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu", lambda tokens, **k: [_konto_ok()])
    cfg = bot.apify_proxy.load_proxy_config(
        {"APIFY_PROXY_URLS": "http://u:p@a.example:8000,http://u:p@b.example:8000"})
    cfg = dataclasses.replace(
        cfg, warnings=("plik puli proxy jest STARY (12.0 h, limit 6 h) — odśwież go "
                       "po swojej stronie albo wyłącz pulę (APIFY_PROXY_POOL=0)",))
    monkeypatch.setattr(bot.apify_proxy, "load_proxy_config", lambda **k: cfg)
    monkeypatch.setattr(bot.apify_proxy, "wczytaj_stan_proxy", lambda conn, urls: {})
    conn = _Polaczenie([(0, None), (0,)])
    tekst = bot._limity(conn)
    assert "⚠ plik puli proxy jest STARY" in tekst
    assert "APIFY\\_PROXY\\_POOL" in tekst    # underscore uciekniety (legacy Markdown)


def test_limity_sekcja_proxy_bez_przypisania_pokazuje_ip_vpsa(monkeypatch):
    monkeypatch.setattr(bot, "load_apify_tokens", lambda: ["tok_a", "tok_b"])
    monkeypatch.setattr(bot.apify_credits, "pula_stanu",
                        lambda tokens, **k: [_konto_ok(), _konto_ok(nazwa="k2")])
    cfg = bot.apify_proxy.load_proxy_config(
        {"APIFY_API_TOKEN1": "tok_a", "APIFY_PROXY1": "http://u:p@dedicated.example:8000"})
    monkeypatch.setattr(bot.apify_proxy, "load_proxy_config", lambda **k: cfg)
    conn = _Polaczenie([(0, None), (0,)])
    tekst = bot._limity(conn)
    assert "dedicated.example:8000" in tekst
    assert "IP VPS-a (bez proxy)" in tekst
    assert "pojedyncze przypisania" in tekst


def test_limity_komenda_i_alias(monkeypatch):
    monkeypatch.setattr(bot, "_limity", lambda conn: "STAN PULI")
    assert bot.obsluz_komende(_Polaczenie(), "/limity") == "STAN PULI"
    assert bot.obsluz_komende(_Polaczenie(), "/limityapi") == "STAN PULI"


def test_pomoc_wspomina_limity():
    assert "/limity" in bot.POMOC


def test_setmycommands_wola_sie_przy_starcie(monkeypatch):
    wywolania = []
    monkeypatch.setattr(bot.telegram_notify, "wywolaj",
                        lambda metoda, payload, timeout=None: wywolania.append((metoda, payload)) or {})
    bot._zarejestruj_komendy()
    assert wywolania[0][0] == "setMyCommands"
    komendy = [c["command"] for c in wywolania[0][1]["commands"]]
    assert "limity" in komendy
    assert "dzis" in komendy and "ostatnie" in komendy and "stop" in komendy and "start" in komendy


def test_setmycommands_nie_wywala_bota_przy_bledzie(monkeypatch, capsys):
    def _wybuchnij(*a, **k):
        raise RuntimeError("Telegram niedostępny")
    monkeypatch.setattr(bot.telegram_notify, "wywolaj", _wybuchnij)
    bot._zarejestruj_komendy()          # nie ma wyjątku -> test przechodzi
    assert "setMyCommands" in capsys.readouterr().err
