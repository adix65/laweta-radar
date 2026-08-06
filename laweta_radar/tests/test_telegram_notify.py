"""Testy transportu — ŚCIEŻKA ZDJĘCIA (`sendPhoto`), bez sieci.

PO CO OSOBNY PLIK NA JEDNĄ FUNKCJĘ. `wywolaj_z_plikiem` to jedyne miejsce
w repo, w którym ręcznie składamy ciało żądania HTTP: Bot API przyjmuje plik
wyłącznie multipartem, a `urllib` ze stdlib nie ma tego gotowego. Brakujący
CRLF, zła granica albo pomylona kolejność nagłówków kończą się tym samym —
Telegram odrzuca żądanie, alert idzie tekstem i nikt nie wie dlaczego, bo
w logu stoi wtedy tylko „HTTP 400".

Ciało rozbieramy PARSEREM MIME ze stdlib, a nie szukaniem podciągów: test
sprawdzający `b'caption' in cialo` przechodzi także dla żądania, którego żaden
serwer nie zrozumie.

Reszta modułu (sendMessage, escape, truncate) chodzi w produkcji od początku
i jest sprawdzana przez `test_powiadomienia.py` oraz `test_bot.py`.
"""
from __future__ import annotations

import email
import json
import urllib.error

import pytest

from laweta_radar.services import telegram_notify as tn

PNG = b"\x89PNG\r\n\x1a\n-udawane-bajty-obrazka-"
PRZYCISKI = [[{"text": "🗺 Trasa w mapach", "url": "https://maps.example/1"},
              {"text": "🗑 Śmieć", "callback_data": "smiec:abc"}]]


@pytest.fixture(autouse=True)
def bot_skonfigurowany(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")


@pytest.fixture
def obrazek(tmp_path):
    plik = tmp_path / "trasa.png"
    plik.write_bytes(PNG)
    return plik


class _Odpowiedz:
    def __init__(self, tresc: bytes):
        self.tresc = tresc

    def read(self):
        return self.tresc

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def zadania(monkeypatch):
    """Przechwycone `urllib.request.Request` zamiast prawdziwego wywołania."""
    zebrane = []

    def _urlopen(req, timeout=None):
        zebrane.append(req)
        return _Odpowiedz(json.dumps({"ok": True, "result": {"message_id": 7}})
                          .encode("utf-8"))

    monkeypatch.setattr(tn.urllib.request, "urlopen", _urlopen)
    return zebrane


def _czesci(req) -> dict:
    """Ciało żądania rozebrane parserem MIME — {nazwa pola: bajty}."""
    naglowek = req.get_header("Content-type")
    assert naglowek.startswith("multipart/form-data; boundary=")
    surowe = (f"Content-Type: {naglowek}\r\nMIME-Version: 1.0\r\n\r\n"
              .encode("utf-8") + req.data)
    wiadomosc = email.message_from_bytes(surowe)
    assert wiadomosc.is_multipart(), "serwer nie rozpozna tego jako multipart"
    return {czesc.get_param("name", header="content-disposition"):
            czesc.get_payload(decode=True)
            for czesc in wiadomosc.get_payload()}


# ---------------------------------------------------------------------------
# Ciało żądania
# ---------------------------------------------------------------------------
def test_zdjecie_idzie_z_podpisem_przyciskami_i_bajtami_pliku(zadania, obrazek):
    assert tn.wyslij_zdjecie(obrazek, "🚨 TERAZ · 53 km", PRZYCISKI) == 7

    (req,) = zadania
    assert req.full_url.endswith("/sendPhoto")
    pola = _czesci(req)
    assert pola["chat_id"] == b"-100999"
    assert pola["caption"].decode("utf-8") == "🚨 TERAZ · 53 km"
    assert pola["parse_mode"] == b"Markdown"
    # Przyciski w multiparcie idą jako pole tekstowe z JSON-em — komplet, bo
    # Telegram odrzuca CAŁĄ klawiaturę przy jednym niepoprawnym przycisku.
    assert json.loads(pola["reply_markup"]) == {"inline_keyboard": PRZYCISKI}
    assert pola["photo"] == PNG


def test_naglowki_zgadzaja_sie_z_cialem(zadania, obrazek):
    """Zła długość albo granica spoza ciała = HTTP 400 bez żadnej wskazówki."""
    tn.wyslij_zdjecie(obrazek, "podpis")
    (req,) = zadania
    granica = req.get_header("Content-type").split("boundary=")[1]
    assert req.data.startswith(f"--{granica}\r\n".encode("utf-8"))
    assert req.data.endswith(f"\r\n--{granica}--\r\n".encode("utf-8"))
    assert int(req.get_header("Content-length")) == len(req.data)


def test_podpis_przyciety_do_limitu_telegrama(zadania, obrazek):
    """Podpis dłuższy niż 1024 znaki = odrzucone CAŁE wywołanie, czyli alert,
    który nie dotarł. Wołający przycina świadomie, a to jest ostatnia siatka."""
    tn.wyslij_zdjecie(obrazek, "x" * 2000)
    assert len(_czesci(zadania[0])["caption"]) == tn.MAX_CAPTION


def test_bez_przyciskow_nie_ma_pustego_pola(zadania, obrazek):
    tn.wyslij_zdjecie(obrazek, "podpis")
    assert "reply_markup" not in _czesci(zadania[0])


# ---------------------------------------------------------------------------
# Ścieżki awaryjne — wszystkie kończą się None, żeby wołający wysłał tekst
# ---------------------------------------------------------------------------
def test_http_400_ponawia_bez_parse_mode(monkeypatch, obrazek):
    """Ta sama zasada co przy `wyslij`: niesparowany znak Markdowna w cudzym
    poście nie może skasować alertu. Lepiej bez pogrubień niż wcale."""
    proby = []

    def _urlopen(req, timeout=None):
        proby.append(_czesci(req))
        if len(proby) == 1:
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)
        return _Odpowiedz(b'{"ok": true, "result": {"message_id": 9}}')

    monkeypatch.setattr(tn.urllib.request, "urlopen", _urlopen)
    assert tn.wyslij_zdjecie(obrazek, "podpis z * gwiazdką", PRZYCISKI) == 9
    assert "parse_mode" in proby[0]
    assert "parse_mode" not in proby[1]      # druga próba bez formatowania
    assert proby[1]["photo"] == PNG          # i wciąż ze zdjęciem


def test_inny_blad_http_to_none_bez_ponowienia(monkeypatch, obrazek):
    ile = []

    def _urlopen(req, timeout=None):
        ile.append(1)
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(tn.urllib.request, "urlopen", _urlopen)
    assert tn.wyslij_zdjecie(obrazek, "podpis") is None
    assert len(ile) == 1


def test_zerwana_siec_to_none_a_nie_wyjatek(monkeypatch, obrazek):
    def _urlopen(req, timeout=None):
        raise OSError("connection reset")

    monkeypatch.setattr(tn.urllib.request, "urlopen", _urlopen)
    assert tn.wyslij_zdjecie(obrazek, "podpis") is None


def test_znikniety_plik_to_none(zadania, tmp_path, capsys):
    """Plik mógł zniknąć między wygenerowaniem a wysyłką (sprzątanie cache'u)."""
    assert tn.wyslij_zdjecie(tmp_path / "nie-ma.png", "podpis") is None
    assert zadania == []
    assert "nie mogę odczytać" in capsys.readouterr().err


def test_pusty_plik_to_none(zadania, tmp_path):
    pusty = tmp_path / "pusty.png"
    pusty.write_bytes(b"")
    assert tn.wyslij_zdjecie(pusty, "podpis") is None
    assert zadania == []


def test_brak_chat_id_to_none(monkeypatch, zadania, obrazek):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    assert tn.wyslij_zdjecie(obrazek, "podpis") is None
    assert zadania == []
