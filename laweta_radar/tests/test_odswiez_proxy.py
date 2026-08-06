"""Testy generatora puli proxy — OFFLINE, bez sieci.

Sieć jest tu podmieniana świadomie: test, który realnie pobiera listy z GitHuba
i puka do api.apify.com, mierzy stan internetu, a nie kod — i czerwienieje
w pociągu. Sprawdzamy to, co należy do nas: parsowanie i scalanie wielu źródeł,
etapową weryfikację (format -> TCP -> pełne 4 testy, z przerwaniem po osiągnięciu
celu) i format pliku, który czyta workers/apify_proxy.py.
"""
from __future__ import annotations

import json

import pytest

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


# ---------------------------------------------------------------------------
# Etap 1 — filtr formalny: kandydaci() (jedna lista) i scal_kandydatow() (wiele)
# ---------------------------------------------------------------------------
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


def test_adresy_prywatne_wypadaja_bez_sieci():
    """10.x/192.168.x/127.x (i pokrewne RFC 1918/3927/5735) nie nadają się jako
    proxy z zewnątrz — odsiewamy je na etapie formalnym, bez pukania do sieci."""
    out = op.kandydaci([
        "http://10.0.0.5:8080",
        "http://192.168.1.1:3128",
        "http://127.0.0.1:8080",
        "http://169.254.1.1:8080",
        "http://8.8.8.8:8080",           # publiczny — zostaje
    ])
    assert out == ["http://8.8.8.8:8080"]


def test_dedup_po_host_port_niezaleznie_od_protokolu():
    """Ten sam host:port jako http:// i https:// to JEDEN kandydat, nie dwa —
    dwa 'różne' wpisy prowadzące do jednego adresu to jedno wyjście, nie dwa."""
    out = op.kandydaci(["http://1.2.3.4:8080", "https://1.2.3.4:8080"])
    assert out == ["http://1.2.3.4:8080"]


def test_scal_kandydatow_z_wielu_zrodel_dedupuje_i_pamieta_pochodzenie():
    surowe = {
        "zrodloA": {"protokol": None, "linie": ["http://1.1.1.1:8080", "http://2.2.2.2:8080"]},
        "zrodloB": {"protokol": None,
                   "linie": ["http://2.2.2.2:8080", "http://3.3.3.3:8080"]},   # 2.2.2.2 duplikat
    }
    kandydaci_, zrodlo_dla, ile_surowych = op.scal_kandydatow(surowe)
    assert set(kandydaci_) == {"http://1.1.1.1:8080", "http://2.2.2.2:8080",
                               "http://3.3.3.3:8080"}
    assert len(kandydaci_) == 3                    # duplikat policzony raz
    assert zrodlo_dla["http://1.1.1.1:8080"] == "zrodloA"
    assert zrodlo_dla["http://2.2.2.2:8080"] == "zrodloA"   # pierwsze wystąpienie wygrywa
    assert ile_surowych == {"zrodloA": 2, "zrodloB": 2}     # RAW, przed dedupem


def test_scal_kandydatow_dopisuje_schemat_z_protokolu_zrodla():
    """Źródło z gołym `ip:port` (TheSpeedX, monosans, jetkai, ShiftyTR, ...) ma
    dostać schemat z WŁASNEGO `protokol`, nie z sąsiedniego źródła."""
    surowe = {
        "thespeedx-http": {"protokol": "http", "linie": ["1.1.1.1:8080"]},
        "thespeedx-socks5": {"protokol": "socks5", "linie": ["2.2.2.2:1080"]},
    }
    kandydaci_, zrodlo_dla, _ = op.scal_kandydatow(surowe)
    assert set(kandydaci_) == {"http://1.1.1.1:8080", "socks5://2.2.2.2:1080"}
    assert zrodlo_dla["http://1.1.1.1:8080"] == "thespeedx-http"


def test_scal_kandydatow_ignoruje_trzecie_pole_ip_port_kraj():
    """zloi-user niesie `ip:port:Kraj` — trzecie pole to metadana, nie adres."""
    surowe = {"zloi-user-http": {"protokol": "http",
                                 "linie": ["1.1.1.1:8080:Russia"]}}
    kandydaci_, _, _ = op.scal_kandydatow(surowe)
    assert kandydaci_ == ["http://1.1.1.1:8080"]


def test_scal_kandydatow_bez_protokolu_gola_linia_jest_smieciem():
    """Źródło z `protokol=None` (proxifly) ma nieść PEŁNY schemat — goła linia
    (np. literówka w źródle) jest tu śmieciem, nie kandydatem."""
    surowe = {"proxifly-http": {"protokol": None, "linie": ["1.1.1.1:8080"]}}
    kandydaci_, _, ile_surowych = op.scal_kandydatow(surowe)
    assert kandydaci_ == []
    assert ile_surowych == {"proxifly-http": 0}


def test_scal_kandydatow_pusta_lista_zrodla_jest_nieszkodliwa():
    kandydaci_, zrodlo_dla, ile_surowych = op.scal_kandydatow(
        {"puste": {"protokol": None, "linie": []}})
    assert kandydaci_ == [] and zrodlo_dla == {} and ile_surowych == {"puste": 0}


# ---------------------------------------------------------------------------
# Pobieranie wielu źródeł — awaria JEDNEGO nie przerywa reszty
# ---------------------------------------------------------------------------
def test_pobierz_zrodla_jedno_zrodlo_niedostepne_reszta_dziala(monkeypatch):
    def _pobierz(url, _timeout):
        if "padniete" in url:
            raise OSError("connection refused")
        return [f"http://{url.split('/')[-1]}:8080"]

    monkeypatch.setattr(op, "pobierz_liste", _pobierz)
    wynik = op.pobierz_zrodla(
        {"a": {"protokol": None, "urls": ["https://x/padniete"]},
         "b": {"protokol": None, "urls": ["https://x/1.2.3.4", "https://x/5.6.7.8"]}},
        timeout=1.0,
    )
    assert wynik["a"]["linie"] == []                 # padło, ale NIE wywaliło reszty
    assert wynik["b"]["linie"] == ["http://1.2.3.4:8080", "http://5.6.7.8:8080"]


def test_pobierz_zrodla_jeden_url_w_srodku_listy_pada_reszta_zrodla_zostaje(monkeypatch):
    """Źródło z dwoma URL-ami (np. proxifly-kraje): jeden URL pada, drugi wciąż
    dokłada kandydatów — awaria jest per-URL, nie per-źródło."""
    def _pobierz(url, _timeout):
        if "zly" in url:
            raise OSError("404")
        return ["http://9.9.9.9:8080"]

    monkeypatch.setattr(op, "pobierz_liste", _pobierz)
    wynik = op.pobierz_zrodla(
        {"kraje": {"protokol": None, "urls": ["https://x/zly", "https://x/dobry"]}},
        timeout=1.0,
    )
    assert wynik["kraje"]["linie"] == ["http://9.9.9.9:8080"]


def test_pobierz_zrodla_niesie_protokol_do_dalszego_uzycia(monkeypatch):
    monkeypatch.setattr(op, "pobierz_liste", lambda *_, **__: ["1.1.1.1:8080"])
    wynik = op.pobierz_zrodla(
        {"thespeedx-http": {"protokol": "http", "urls": ["https://x/a"]}}, timeout=1.0)
    assert wynik["thespeedx-http"]["protokol"] == "http"


# ---------------------------------------------------------------------------
# Etap 2 — TCP connect
# ---------------------------------------------------------------------------
def test_etap_tcp_odsiewa_martwe(monkeypatch):
    class _FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _connect(addr, timeout):
        host, _port = addr
        if host == "martwy.example":
            raise OSError("connection refused")
        return _FakeSocket()

    import socket as socket_modul
    monkeypatch.setattr(socket_modul, "create_connection", _connect)

    zywe = op.etap_tcp(
        ["http://zywy.example:8080", "http://martwy.example:8080"],
        timeout=1.0, rownolegle=4,
    )
    assert zywe == ["http://zywy.example:8080"]


def test_etap_tcp_pusta_lista():
    assert op.etap_tcp([], timeout=1.0, rownolegle=4) == []


# ---------------------------------------------------------------------------
# Etap 3 — cztery pełne testy, z PRZERWANIEM po osiągnięciu celu
# ---------------------------------------------------------------------------
def _mock_weryfikuj_zawsze_ok(monkeypatch):
    """Każdy kandydat 'przechodzi' — do testów PRZERWANIA, gdzie treść wyniku
    nie ma znaczenia, tylko to, ILE razy funkcję w ogóle wywołano."""
    wolania = []

    def _fake(url, *, direct_ip=None, znane_tozsamosci=None, timeout=10.0):
        wolania.append(url)
        return apify_proxy.WynikWeryfikacji(url, True, True, True, True, "")

    monkeypatch.setattr(op.ap, "weryfikuj_proxy", _fake)
    return wolania


def test_etap_pelny_przerywa_po_osiagnieciu_celu(monkeypatch):
    """30 tysięcy kandydatów (tu: 300, żeby test leciał w milisekundach) -> etap 3
    kończy się po zebraniu `cel`, a NIE po sprawdzeniu wszystkich."""
    wolania = _mock_weryfikuj_zawsze_ok(monkeypatch)
    kandydaci_ = [f"http://n{i}.example:8080" for i in range(300)]
    zaakceptowane, sprawdzono = op.etap_pelny(
        kandydaci_, cel=40, timeout=1.0, rownolegle=32, direct_ip="1.2.3.4", zrodlo_dla={},
    )
    assert len(zaakceptowane) == 40
    assert sprawdzono < len(kandydaci_)
    assert len(wolania) == sprawdzono                # nic nie sprawdzone "po cichu" ponad to


def test_etap_pelny_bez_celu_sprawdza_wszystko(monkeypatch):
    """`cel<=0` (tryb --tylko-pula) = sprawdź WSZYSTKO, bez przerywania."""
    wolania = _mock_weryfikuj_zawsze_ok(monkeypatch)
    kandydaci_ = [f"http://n{i}.example:8080" for i in range(25)]
    zaakceptowane, sprawdzono = op.etap_pelny(
        kandydaci_, cel=0, timeout=1.0, rownolegle=8, direct_ip=None, zrodlo_dla={},
    )
    assert sprawdzono == 25 and len(zaakceptowane) == 25
    assert len(wolania) == 25


def test_etap_pelny_odrzuca_nieudane(monkeypatch):
    def _fake(url, *, direct_ip=None, znane_tozsamosci=None, timeout=10.0):
        ok = url.endswith(":1")
        return apify_proxy.WynikWeryfikacji(url, ok, ok, True, ok, "" if ok else "padło")

    monkeypatch.setattr(op.ap, "weryfikuj_proxy", _fake)
    zaakceptowane, sprawdzono = op.etap_pelny(
        ["http://a.example:1", "http://b.example:2"],
        cel=0, timeout=1.0, rownolegle=8, direct_ip=None, zrodlo_dla={},
    )
    assert sprawdzono == 2
    assert [r["url"] for r in zaakceptowane] == ["http://a.example:1"]


def test_etap_pelny_zapisuje_zrodlo_i_czas(monkeypatch):
    monkeypatch.setattr(op.ap, "weryfikuj_proxy",
                        lambda url, **k: apify_proxy.WynikWeryfikacji(url, True, True, True, True, ""))
    zaakceptowane, _ = op.etap_pelny(
        ["http://a.example:1"], cel=0, timeout=1.0, rownolegle=8,
        direct_ip=None, zrodlo_dla={"http://a.example:1": "zrodloX"},
    )
    assert zaakceptowane[0]["zrodlo"] == "zrodloX"
    assert isinstance(zaakceptowane[0]["czas_ms"], int)


# ---------------------------------------------------------------------------
# Ranking — passy_pod_rzad dziedziczone z poprzedniego pliku
# ---------------------------------------------------------------------------
def test_dopisz_passy_nowy_adres_startuje_od_jednego():
    out = op.dopisz_passy([{"url": "http://a.example:1"}], {})
    assert out[0]["passy_pod_rzad"] == 1


def test_dopisz_passy_zna_ny_adres_rosnie():
    poprzednie = {"http://a.example:1": {"url": "http://a.example:1", "passy_pod_rzad": 3}}
    out = op.dopisz_passy([{"url": "http://a.example:1"}], poprzednie)
    assert out[0]["passy_pod_rzad"] == 4


def test_wczytaj_poprzednie_rekordy_brak_pliku(tmp_path):
    assert op.wczytaj_poprzednie_rekordy(tmp_path / "brak.json") == {}


def test_wczytaj_poprzednie_rekordy_uszkodzony_plik_nie_wybucha(tmp_path):
    p = tmp_path / "zle.json"
    p.write_text("to nie jest JSON", encoding="utf-8")
    assert op.wczytaj_poprzednie_rekordy(p) == {}


# ---------------------------------------------------------------------------
# Zapis — format pliku, sortowanie, atomowość
# ---------------------------------------------------------------------------
def test_plik_czytelny_dla_workera(tmp_path, monkeypatch):
    """Format zapisu MUSI zgadzać się z `_read_pool_file` — to jest cały kontrakt."""
    plik = tmp_path / ".apify_proxy_pool.json"
    op.zapisz(plik, [{"url": "http://1.2.3.4:8080"}, {"url": "http://5.6.7.8:3128"}])

    monkeypatch.setenv("APIFY_PROXY_POOL_FILE", str(plik))
    urls, wiek_h = apify_proxy._read_pool_file({"APIFY_PROXY_POOL_FILE": str(plik)})

    assert set(urls) == {"http://1.2.3.4:8080", "http://5.6.7.8:3128"}
    assert wiek_h is not None and wiek_h < 1.0    # świeżo zapisany


def test_pusta_pula_jest_pusta_a_nie_uszkodzona(tmp_path):
    plik = tmp_path / "pula.json"
    op.zapisz(plik, [])
    assert apify_proxy._read_pool_file({"APIFY_PROXY_POOL_FILE": str(plik)})[0] == []
    assert json.loads(plik.read_text())["proxies"] == []


def test_zapis_jest_atomowy_bez_smieci(tmp_path):
    plik = tmp_path / "pula.json"
    op.zapisz(plik, [{"url": "http://1.2.3.4:8080"}])
    op.zapisz(plik, [{"url": "http://5.6.7.8:3128"}])
    assert [p.name for p in tmp_path.iterdir()] == ["pula.json"]
    assert json.loads(plik.read_text())["proxies"][0]["url"] == "http://5.6.7.8:3128"


def test_zapis_sortuje_od_najlepszego():
    """Najdłuższa passa najpierw, remis rozstrzyga krótszy czas odpowiedzi —
    plik ma pokazywać NAJLEPSZE adresy na górze, nie przypadkowe."""
    rekordy = [
        {"url": "http://wolny.example:1", "passy_pod_rzad": 5, "czas_ms": 900},
        {"url": "http://szybki.example:1", "passy_pod_rzad": 5, "czas_ms": 100},
        {"url": "http://nowy.example:1", "passy_pod_rzad": 1, "czas_ms": 50},
    ]
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        plik = Path(d) / "p.json"
        op.zapisz(plik, rekordy)
        urls = [r["url"] for r in json.loads(plik.read_text())["proxies"]]
    assert urls == ["http://szybki.example:1", "http://wolny.example:1",
                    "http://nowy.example:1"]


def test_zapis_niesie_pola_rankingu(tmp_path):
    plik = tmp_path / "p.json"
    op.zapisz(plik, [{"url": "http://a.example:1", "zrodlo": "proxifly-http",
                      "czas_ms": 123, "passy_pod_rzad": 2}])
    zapisane = json.loads(plik.read_text())["proxies"][0]
    assert zapisane["zrodlo"] == "proxifly-http"
    assert zapisane["czas_ms"] == 123
    assert zapisane["passy_pod_rzad"] == 2
    assert zapisane["apify_ok"] is True


# ---------------------------------------------------------------------------
# main() — orkiestracja end-to-end, offline (wszystko zamockowane)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _bez_env_kluczy(monkeypatch):
    """Testy main() mają być deterministyczne — bez przypadkowych
    APIFY_API_TOKEN*/APIFY_PROXY_POOL_URL z env maszyny, na której leci pytest."""
    for nazwa in list(__import__("os").environ):
        if nazwa.startswith("APIFY_API_TOKEN") or nazwa in (
            "APIFY_PROXY_POOL_URL", "APIFY_PROXY_POOL", "PROXY_CHECK_PARALLEL_TCP",
            "PROXY_CHECK_PARALLEL_HTTP", "DOCELOWA_LICZBA_PROXY",
        ):
            monkeypatch.delenv(nazwa, raising=False)
    monkeypatch.setattr(op, "_zaladuj_dotenv", lambda: None)


def _zawsze_zywy_tcp(monkeypatch):
    monkeypatch.setattr(op, "etap_tcp", lambda kandydaci_, **k: list(kandydaci_))


def _wszystko_ok_etap3(monkeypatch):
    monkeypatch.setattr(op.ap, "weryfikuj_proxy",
                        lambda url, **k: apify_proxy.WynikWeryfikacji(url, True, True, True, True, ""))
    monkeypatch.setattr(op.ap, "wlasny_ip", lambda *a, **k: "9.9.9.9")


def test_main_dwa_zrodla_jedno_niedostepne_pula_powstaje_z_drugiego(tmp_path, monkeypatch):
    def _pobierz(url, _timeout):
        if "padniete" in url:
            raise OSError("timeout")
        return ["http://1.1.1.1:8080", "http://2.2.2.2:8080"]

    monkeypatch.setattr(op, "ZRODLA", {
        "padniete": {"protokol": None, "urls": ["https://x/padniete"]},
        "zywe": {"protokol": None, "urls": ["https://x/zywe"]},
    })
    monkeypatch.setattr(op, "pobierz_liste", _pobierz)
    _zawsze_zywy_tcp(monkeypatch)
    _wszystko_ok_etap3(monkeypatch)

    plik = tmp_path / "pula.json"
    kod = op.main(["--plik", str(plik)])
    assert kod == 0
    zapisane = {r["url"] for r in json.loads(plik.read_text())["proxies"]}
    assert zapisane == {"http://1.1.1.1:8080", "http://2.2.2.2:8080"}


def test_main_sucho_nie_rusza_sieci_ani_pliku(tmp_path, monkeypatch):
    plik = tmp_path / "pula.json"

    def _wybuch(*_, **__):
        raise AssertionError("--sucho nie ma prawa pobierać listy")

    monkeypatch.setattr(op, "ZRODLA", {"a": {"protokol": None, "urls": ["https://x/a"]}})
    monkeypatch.setattr(op, "pobierz_liste", _wybuch)
    assert op.main(["--sucho", "--plik", str(plik)]) == 0
    assert not plik.exists()


def test_main_wszystkie_zrodla_padaja_zostawia_stary_plik(tmp_path, monkeypatch):
    plik = tmp_path / "pula.json"
    op.zapisz(plik, [{"url": "http://stary.example:8080"}])
    przed = plik.read_text()

    monkeypatch.setattr(op, "ZRODLA", {
        "a": {"protokol": None, "urls": ["https://x/a"]},
        "b": {"protokol": None, "urls": ["https://x/b"]},
    })
    monkeypatch.setattr(op, "pobierz_liste",
                        lambda *_, **__: (_ for _ in ()).throw(OSError("brak sieci")))
    assert op.main(["--plik", str(plik)]) == 1
    assert plik.read_text() == przed


def test_main_zero_dzialajacych_zapisuje_pusta_pule_kod_1(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "ZRODLA", {"a": {"protokol": None, "urls": ["https://x/a"]}})
    monkeypatch.setattr(op, "pobierz_liste", lambda *_, **__: ["http://1.2.3.4:8080"])
    _zawsze_zywy_tcp(monkeypatch)
    monkeypatch.setattr(op.ap, "weryfikuj_proxy",
                        lambda url, **k: apify_proxy.WynikWeryfikacji(url, False, False, True, False, "padło"))
    monkeypatch.setattr(op.ap, "wlasny_ip", lambda *a, **k: "9.9.9.9")

    plik = tmp_path / "pula.json"
    assert op.main(["--plik", str(plik)]) == 1
    assert json.loads(plik.read_text())["proxies"] == []


def test_main_limit_ogranicza_kandydatow_po_scaleniu(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "ZRODLA", {"a": {"protokol": None, "urls": ["https://x/a"]}})
    monkeypatch.setattr(op, "pobierz_liste",
                        lambda *_, **__: [f"http://n{i}.example:8080" for i in range(50)])
    widziane_w_tcp = []

    def _tcp(kandydaci_, **k):
        widziane_w_tcp.extend(kandydaci_)
        return list(kandydaci_)

    monkeypatch.setattr(op, "etap_tcp", _tcp)
    _wszystko_ok_etap3(monkeypatch)

    plik = tmp_path / "pula.json"
    assert op.main(["--plik", str(plik), "--limit", "5"]) == 0
    assert len(widziane_w_tcp) == 5


def test_main_przerywa_etap3_po_celu_przy_duzej_puli_kandydatow(tmp_path, monkeypatch):
    """Odpowiednik testu z zadania: 'X tysięcy kandydatów -> weryfikacja kończy
    się po osiągnięciu DOCELOWA_LICZBA_PROXY, a nie po sprawdzeniu wszystkich'."""
    monkeypatch.setattr(op, "ZRODLA", {"a": {"protokol": None, "urls": ["https://x/a"]}})
    monkeypatch.setattr(op, "pobierz_liste",
                        lambda *_, **__: [f"http://n{i}.example:8080" for i in range(3000)])
    _zawsze_zywy_tcp(monkeypatch)
    wolania = _mock_weryfikuj_zawsze_ok(monkeypatch)
    monkeypatch.setattr(op.ap, "wlasny_ip", lambda *a, **k: "9.9.9.9")

    plik = tmp_path / "pula.json"
    kod = op.main(["--plik", str(plik), "--cel", "10", "--rownolegle-http", "32"])
    assert kod == 0
    zapisane = json.loads(plik.read_text())["proxies"]
    assert len(zapisane) == 10
    assert len(wolania) < 3000            # NIE sprawdzono wszystkich 3000


def test_main_apify_proxy_pool_url_jako_lista_dokladana_do_zrodel(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "ZRODLA", {})     # zero domyślnych źródeł — WYŁĄCZNIE .env
    monkeypatch.setenv("APIFY_PROXY_POOL_URL", "https://x/a, https://x/b\nhttps://x/c")
    widziane = []

    def _pobierz(url, _timeout):
        widziane.append(url)
        return ["http://1.1.1.1:8080"]

    monkeypatch.setattr(op, "pobierz_liste", _pobierz)
    _zawsze_zywy_tcp(monkeypatch)
    _wszystko_ok_etap3(monkeypatch)

    plik = tmp_path / "pula.json"
    assert op.main(["--plik", str(plik)]) == 0
    assert set(widziane) == {"https://x/a", "https://x/b", "https://x/c"}


def test_main_tylko_pula_sprawdza_wylacznie_adresy_z_pliku(tmp_path, monkeypatch):
    plik = tmp_path / "pula.json"
    op.zapisz(plik, [{"url": "http://a.example:1"}, {"url": "http://b.example:1"}])
    monkeypatch.setenv("APIFY_API_TOKEN1", "tok_a")   # liczba_kluczy=1, 2 żywe wystarczą

    def _wybuch_pobieranie(*_, **__):
        raise AssertionError("--tylko-pula nie ma prawa pobierać źródeł")

    monkeypatch.setattr(op, "pobierz_liste", _wybuch_pobieranie)
    monkeypatch.setattr(op.ap, "weryfikuj_proxy",
                        lambda url, **k: apify_proxy.WynikWeryfikacji(url, True, True, True, True, ""))
    monkeypatch.setattr(op.ap, "wlasny_ip", lambda *a, **k: "9.9.9.9")

    assert op.main(["--tylko-pula", "--plik", str(plik)]) == 0
    zapisane = {r["url"] for r in json.loads(plik.read_text())["proxies"]}
    assert zapisane == {"http://a.example:1", "http://b.example:1"}


def test_main_tylko_pula_za_mala_po_czyszczeniu_odpala_pelne_odswiezenie(tmp_path, monkeypatch):
    """SZYBKA KONTROLA: jeśli po wyrzuceniu martwych zostało mniej niż liczba
    kluczy, PEŁNE odświeżenie ma odpalić się OD RAZU, bez czekania na cron."""
    plik = tmp_path / "pula.json"
    op.zapisz(plik, [{"url": "http://a.example:1"}, {"url": "http://martwy.example:1"}])
    monkeypatch.setenv("APIFY_API_TOKEN1", "tok_a")
    monkeypatch.setenv("APIFY_API_TOKEN2", "tok_b")   # liczba_kluczy=2, ale przeżyje tylko 1

    def _weryfikuj(url, **k):
        ok = "martwy" not in url
        return apify_proxy.WynikWeryfikacji(url, ok, ok, True, ok, "" if ok else "padł")

    monkeypatch.setattr(op.ap, "weryfikuj_proxy", _weryfikuj)
    monkeypatch.setattr(op.ap, "wlasny_ip", lambda *a, **k: "9.9.9.9")

    pelne_odswiezenie_wywolane = []
    monkeypatch.setattr(op, "ZRODLA", {"a": {"protokol": None, "urls": ["https://x/a"]}})
    monkeypatch.setattr(op, "pobierz_liste", lambda *_, **__: (
        pelne_odswiezenie_wywolane.append(1) or ["http://nowy.example:1", "http://nowy2.example:1"]
    ))
    _zawsze_zywy_tcp(monkeypatch)

    kod = op.main(["--tylko-pula", "--plik", str(plik)])
    assert kod == 0
    assert pelne_odswiezenie_wywolane      # PEŁNE odświeżenie faktycznie odpalone
    zapisane = {r["url"] for r in json.loads(plik.read_text())["proxies"]}
    assert zapisane == {"http://nowy.example:1", "http://nowy2.example:1"}


def test_main_docelowa_liczba_domyslnie_trzy_razy_liczba_kluczy(tmp_path, monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN1", "a")
    monkeypatch.setenv("APIFY_API_TOKEN2", "b")
    monkeypatch.setenv("APIFY_API_TOKEN3", "c")
    monkeypatch.setattr(op, "ZRODLA", {"a": {"protokol": None, "urls": ["https://x/a"]}})
    monkeypatch.setattr(op, "pobierz_liste",
                        lambda *_, **__: [f"http://n{i}.example:8080" for i in range(100)])
    _zawsze_zywy_tcp(monkeypatch)
    _mock_weryfikuj_zawsze_ok(monkeypatch)
    monkeypatch.setattr(op.ap, "wlasny_ip", lambda *a, **k: "9.9.9.9")

    plik = tmp_path / "pula.json"
    op.main(["--plik", str(plik)])
    assert len(json.loads(plik.read_text())["proxies"]) == 9   # 3 klucze x 3


def test_main_ostrzega_gdy_pula_zapisana_ale_wylaczona(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("APIFY_PROXY_POOL", raising=False)
    monkeypatch.setattr(op, "ZRODLA", {"a": {"protokol": None, "urls": ["https://x/a"]}})
    monkeypatch.setattr(op, "pobierz_liste", lambda *_, **__: ["http://1.2.3.4:8080"])
    _zawsze_zywy_tcp(monkeypatch)
    _wszystko_ok_etap3(monkeypatch)

    plik = tmp_path / "pula.json"
    assert op.main(["--plik", str(plik)]) == 0
    assert "APIFY_PROXY_POOL nie jest ustawione na 1" in capsys.readouterr().out


def test_main_bez_ostrzezenia_gdy_pula_wlaczona(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APIFY_PROXY_POOL", "1")
    monkeypatch.setattr(op, "ZRODLA", {"a": {"protokol": None, "urls": ["https://x/a"]}})
    monkeypatch.setattr(op, "pobierz_liste", lambda *_, **__: ["http://1.2.3.4:8080"])
    _zawsze_zywy_tcp(monkeypatch)
    _wszystko_ok_etap3(monkeypatch)

    plik = tmp_path / "pula.json"
    assert op.main(["--plik", str(plik)]) == 0
    assert "APIFY_PROXY_POOL nie jest ustawione" not in capsys.readouterr().out
