"""Testy generatora puli proxy — OFFLINE, bez sieci.

Sieć jest tu podmieniana świadomie: test, który realnie pobiera listę z GitHuba
i puka do api.apify.com, mierzy stan internetu, a nie kod — i czerwienieje
w pociągu. Sprawdzamy to, co należy do nas: parsowanie listy, dobór wpisów
i format pliku, który czyta workers/apify_proxy.py.
"""
from __future__ import annotations

import json

from laweta_radar.scripts import odswiez_proxy as op
from laweta_radar.workers import apify_proxy


LISTA = """
# komentarz, ma zniknąć
http://1.2.3.4:8080
http://5.6.7.8:3128
socks5://9.10.11.12:1080
http://1.2.3.4:8080

nie-proxy
http://brak-portu
http://13.14.15.16:99999
"""


def test_parsowanie_odsiewa_smieci_i_duplikaty():
    out = op.kandydaci(LISTA.splitlines())
    assert out == [
        "http://1.2.3.4:8080",
        "http://5.6.7.8:3128",
        "socks5://9.10.11.12:1080",
    ]


def test_port_poza_zakresem_wypada():
    """65535 to maksimum. Port 99999 pasuje do wzorca, ale nie istnieje."""
    assert op.kandydaci(["http://1.1.1.1:99999"]) == []
    assert op.kandydaci(["http://1.1.1.1:65535"]) == ["http://1.1.1.1:65535"]


def test_limit_ucina_liste():
    assert len(op.kandydaci(LISTA.splitlines(), limit=2)) == 2


def test_plik_czytelny_dla_workera(tmp_path, monkeypatch):
    """Format zapisu MUSI zgadzać się z `_read_pool_file` — to jest cały kontrakt.

    Gdyby te dwie strony się rozjechały, objawem byłaby pusta pula tuż obok
    świeżo zapisanego pliku: worker milczy, plik wygląda dobrze, a konta wychodzą
    z gołego IP VPS-a.
    """
    plik = tmp_path / ".apify_proxy_pool.json"
    op.zapisz(plik, ["http://1.2.3.4:8080", "http://5.6.7.8:3128"])

    monkeypatch.setenv("APIFY_PROXY_POOL_FILE", str(plik))
    urls, wiek_h = apify_proxy._read_pool_file({"APIFY_PROXY_POOL_FILE": str(plik)})

    assert urls == ["http://1.2.3.4:8080", "http://5.6.7.8:3128"]
    assert wiek_h is not None and wiek_h < 1.0    # świeżo zapisany


def test_pusta_pula_jest_pusta_a_nie_uszkodzona(tmp_path):
    """Zero działających adresów zapisujemy JAWNIE, zamiast zostawiać stary plik.

    To jest ta sama sytuacja, którą opisuje docs/APIFY-PROXY.md: jeden przeżyty
    wpis w nieodświeżanym pliku przejmował przez rendezvous hashing komplet kont.
    Pusta pula + APIFY_PROXY_REQUIRED=1 znaczy "zakończ czysto", i o to chodzi.
    """
    plik = tmp_path / "pula.json"
    op.zapisz(plik, [])
    assert apify_proxy._read_pool_file({"APIFY_PROXY_POOL_FILE": str(plik)})[0] == []
    assert json.loads(plik.read_text())["proxies"] == []


def test_zapis_jest_atomowy_bez_smieci(tmp_path):
    """Po zapisie w katalogu ma zostać JEDEN plik — żadnych .tmp."""
    plik = tmp_path / "pula.json"
    op.zapisz(plik, ["http://1.2.3.4:8080"])
    op.zapisz(plik, ["http://5.6.7.8:3128"])
    assert [p.name for p in tmp_path.iterdir()] == ["pula.json"]
    assert json.loads(plik.read_text())["proxies"][0]["url"] == "http://5.6.7.8:3128"


def test_sprawdz_kazdy_blad_to_nie_dochodzi(monkeypatch):
    """Wyjątek z httpx ma dać (url, False, powód), nigdy nie wywalić przebiegu."""
    class _Klient:
        def __init__(self, **_): pass
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def get(self, _url): raise TimeoutError("za wolno")

    monkeypatch.setattr("httpx.Client", _Klient)
    url, ok, powod = op.sprawdz("http://1.2.3.4:8080", 1.0)
    assert (url, ok) == ("http://1.2.3.4:8080", False)
    assert "TimeoutError" in powod


def test_sprawdz_401_to_sukces(monkeypatch):
    """401 znaczy 'doszliśmy do Apify, tylko bez klucza' — czyli proxy działa.

    Pytamy o OSIĄGALNOŚĆ, nie o autoryzację, i celowo nie wysyłamy przez cudze
    proxy żadnego klucza.
    """
    class _Odp:
        status_code = 401

    class _Klient:
        def __init__(self, **_): pass
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def get(self, _url): return _Odp()

    monkeypatch.setattr("httpx.Client", _Klient)
    _, ok, powod = op.sprawdz("http://1.2.3.4:8080", 1.0)
    assert ok and "401" in powod


def test_sucho_nie_rusza_sieci_ani_pliku(tmp_path, monkeypatch, capsys):
    plik = tmp_path / "pula.json"

    def _wybuch(*_, **__):
        raise AssertionError("--sucho nie ma prawa pobierać listy")

    monkeypatch.setattr(op, "pobierz_liste", _wybuch)
    assert op.main(["--sucho", "--plik", str(plik)]) == 0
    assert not plik.exists()
    assert "SUCHO" in capsys.readouterr().out


def test_brak_sieci_zostawia_stary_plik(tmp_path, monkeypatch, capsys):
    """Chwilowy brak sieci NIE MOŻE wyczyścić działającej puli.

    Stara pula jest zła, ale pusta jest gorsza, gdy powodem jest zerwane łącze,
    a nie martwe adresy: przy APIFY_PROXY_REQUIRED=1 pusta pula zatrzymuje
    zbieranie do czasu, aż ktoś to zauważy.
    """
    plik = tmp_path / "pula.json"
    op.zapisz(plik, ["http://1.2.3.4:8080"])
    przed = plik.read_text()

    monkeypatch.setattr(op, "pobierz_liste", lambda *_, **__: (_ for _ in ()).throw(OSError("brak sieci")))
    assert op.main(["--plik", str(plik)]) == 1
    assert plik.read_text() == przed


def test_zero_dzialajacych_konczy_kodem_1(tmp_path, monkeypatch):
    """Zapisujemy pustą pulę, ale kod wyjścia ma krzyczeć — cron ma to zauważyć."""
    plik = tmp_path / "pula.json"
    monkeypatch.setattr(op, "pobierz_liste", lambda *_, **__: ["http://1.2.3.4:8080"])
    monkeypatch.setattr(op, "sprawdz", lambda u, _t: (u, False, "TimeoutError"))

    assert op.main(["--plik", str(plik), "--rownolegle", "1"]) == 1
    assert json.loads(plik.read_text())["proxies"] == []


def test_dzialajace_trafiaja_do_pliku(tmp_path, monkeypatch):
    plik = tmp_path / "pula.json"
    monkeypatch.setattr(op, "pobierz_liste", lambda *_, **__: [
        "http://1.2.3.4:8080", "http://5.6.7.8:3128",
    ])
    monkeypatch.setattr(op, "sprawdz",
                        lambda u, _t: (u, u.endswith("8080"), "HTTP 401"))

    assert op.main(["--plik", str(plik), "--rownolegle", "1"]) == 0
    zapisane = json.loads(plik.read_text())["proxies"]
    assert [r["url"] for r in zapisane] == ["http://1.2.3.4:8080"]
    assert all(r["apify_ok"] is True for r in zapisane)


def test_ostrzega_gdy_pula_zapisana_ale_wylaczona(tmp_path, monkeypatch, capsys):
    """Świeży plik obok "BRAK proxy" z workera wygląda na sprzeczność — nie jest.

    Worker czyta plik WYŁĄCZNIE przy APIFY_PROXY_POOL=1, a odświeżenie samo
    niczego nie włącza (włączenie jest decyzją podejmowaną PO zobaczeniu, ile
    adresów przeżyło). Bez tego ostrzeżenia operator ma dwa narzędzia mówiące
    coś przeciwnego i żadnej podpowiedzi, które kłamie.
    """
    plik = tmp_path / "pula.json"
    monkeypatch.delenv("APIFY_PROXY_POOL", raising=False)
    monkeypatch.setattr(op, "pobierz_liste", lambda *_, **__: ["http://1.2.3.4:8080"])
    monkeypatch.setattr(op, "sprawdz", lambda u, _t: (u, True, "HTTP 401"))

    assert op.main(["--plik", str(plik), "--rownolegle", "1"]) == 0
    assert "APIFY_PROXY_POOL nie jest ustawione na 1" in capsys.readouterr().out


def test_bez_ostrzezenia_gdy_pula_wlaczona(tmp_path, monkeypatch, capsys):
    plik = tmp_path / "pula.json"
    monkeypatch.setenv("APIFY_PROXY_POOL", "1")
    monkeypatch.setattr(op, "pobierz_liste", lambda *_, **__: ["http://1.2.3.4:8080"])
    monkeypatch.setattr(op, "sprawdz", lambda u, _t: (u, True, "HTTP 401"))

    assert op.main(["--plik", str(plik), "--rownolegle", "1"]) == 0
    assert "APIFY_PROXY_POOL nie jest ustawione" not in capsys.readouterr().out
