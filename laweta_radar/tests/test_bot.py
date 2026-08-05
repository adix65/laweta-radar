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
