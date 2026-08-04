"""
Offline testy laweta_radar/workers/apify_proxy.py — proxy per klucz Apify (żeby pula kont nie
wychodziła z jednego IP VPS-a).

Wszystko tutaj to CZYSTA logika (env -> przypisanie token->proxy), więc testy lecą
bez sieci: żadne z nich nie otwiera połączenia ani nie dotyka httpx (klient jest
budowany leniwie, w client_for_token). Konfigurację podajemy zawsze JAWNIE jako
słownik env — nic nie zależy od prawdziwego .env maszyny, na której leci pytest.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.workers import apify_proxy as ap  # noqa: E402

_GW = "http://user-session-{session}:haslo@gw.example.com:7000"


@pytest.fixture(autouse=True)
def _no_pool_file(monkeypatch, tmp_path):
    """Odcina darmową pulę z pliku od WSZYSTKICH testów w tym module.

    Bez tego wynik zależałby od tego, czy na maszynie leży .apify_proxy_pool.json
    (na VPS leży, w CI nie) — a testy konfiguracji mają sprawdzać konfigurację,
    nie zawartość dysku. Testy samej puli podmieniają tę ścieżkę u siebie.
    """
    monkeypatch.setattr(ap, "_pool_file_path", lambda env: tmp_path / "brak.json")


# ---------------------------------------------------------------------------
# Brak konfiguracji = zachowanie jak przed zmianą (zero regresji)
# ---------------------------------------------------------------------------
def test_no_config_means_no_proxy():
    cfg = ap.load_proxy_config({})
    assert cfg.enabled is False
    assert ap.proxy_for_token("tok_a", cfg) is None
    assert "BRAK proxy" in ap.describe(cfg)


def test_no_config_preflight_passes_with_warning_line():
    """Bez proxy run MA iść dalej (zgodność wstecz), ale z ostrzeżeniem w logu."""
    ok, lines = ap.preflight({})
    assert ok is True
    assert any("BRAK proxy" in ln for ln in lines)


def test_required_without_proxy_blocks_the_run():
    ok, lines = ap.preflight({"APIFY_PROXY_REQUIRED": "1"})
    assert ok is False
    assert any("APIFY_PROXY_REQUIRED" in ln for ln in lines)


def test_required_with_proxy_passes():
    ok, _ = ap.preflight({"APIFY_PROXY_REQUIRED": "1", "APIFY_PROXY_URL": _GW})
    assert ok is True


# ---------------------------------------------------------------------------
# CZĘŚCIOWE pokrycie — proxy jest, ale nie dla wszystkich kluczy
# ---------------------------------------------------------------------------
_PARTIAL = {"APIFY_API_TOKEN1": "tok_a", "APIFY_API_TOKEN2": "tok_b",
            "APIFY_PROXY1": "http://u:p@a.example:8000"}


def test_partial_coverage_is_reported_loudly():
    """Proxy tylko dla części kluczy: log MUSI to powiedzieć wprost, inaczej run
    wygląda na zabezpieczony, a większość kont i tak wychodzi z IP VPS-a."""
    ok, lines = ap.preflight(_PARTIAL, tokens=["tok_a", "tok_b"])
    assert ok is True                                    # bez REQUIRED jedziemy dalej
    assert any("1 z 2 kluczy NIE ma" in ln for ln in lines)


def test_partial_coverage_blocks_when_required():
    ok, lines = ap.preflight({**_PARTIAL, "APIFY_PROXY_REQUIRED": "1"},
                             tokens=["tok_a", "tok_b"])
    assert ok is False
    assert any("nie ma przypisanego proxy" in ln for ln in lines)


def test_full_coverage_reports_nothing_extra():
    ok, lines = ap.preflight({"APIFY_PROXY_URL": _GW}, tokens=["tok_a", "tok_b"])
    assert ok is True
    assert not any("NIE ma" in ln for ln in lines)


def test_preflight_without_tokens_keeps_working():
    """Wywołanie bez listy kluczy (stary kontrakt) nadal działa."""
    ok, lines = ap.preflight(_PARTIAL)
    assert ok is True and lines


# ---------------------------------------------------------------------------
# Brama z sesją lepką — każde konto ma SWOJĄ, STAŁĄ sesję
# ---------------------------------------------------------------------------
def test_gateway_session_is_sticky_per_token():
    cfg = ap.load_proxy_config({"APIFY_PROXY_URL": _GW})
    a1 = ap.proxy_for_token("tok_a", cfg)
    a2 = ap.proxy_for_token("tok_a", cfg)
    b1 = ap.proxy_for_token("tok_b", cfg)
    assert a1 == a2                      # to samo konto -> zawsze ta sama sesja
    assert a1 != b1                      # różne konta -> różne sesje
    assert "{session}" not in a1
    assert ap.session_id("tok_a") in a1


def test_session_id_is_stable_and_hides_the_token():
    sid = ap.session_id("tok_a")
    assert sid == ap.session_id("tok_a")
    assert len(sid) == 12
    assert "tok_a" not in sid


def test_gateway_without_session_placeholder_warns():
    """Brama bez {session} = jeden adres dla wszystkich kont, czyli problem zostaje."""
    cfg = ap.load_proxy_config({"APIFY_PROXY_URL": "http://u:p@gw.example.com:7000"})
    assert cfg.sticky_per_key is False
    assert any("{session}" in w for w in cfg.warnings)
    assert ap.proxy_for_token("tok_a", cfg) == ap.proxy_for_token("tok_b", cfg)


# ---------------------------------------------------------------------------
# Pula proxy — rozkład stabilny i NIEZALEŻNY od kolejności wpisów
# ---------------------------------------------------------------------------
def test_pool_assignment_is_stable_and_order_independent():
    urls = ["http://u:p@a.example:8000", "http://u:p@b.example:8000",
            "http://u:p@c.example:8000"]
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": ", ".join(urls)})
    cfg_rev = ap.load_proxy_config({"APIFY_PROXY_URLS": ", ".join(reversed(urls))})
    for tok in ("tok_a", "tok_b", "tok_c", "tok_d"):
        assert ap.proxy_for_token(tok, cfg) == ap.proxy_for_token(tok, cfg)
        # Odwrócona kolejność w .env NIE przenosi konta na inny adres — to jest
        # sedno lepkości: konto zmieniające co run kraj wyjścia jest podejrzane.
        assert ap.proxy_for_token(tok, cfg) == ap.proxy_for_token(tok, cfg_rev)


def test_pool_accepts_newlines_and_semicolons():
    cfg = ap.load_proxy_config(
        {"APIFY_PROXY_URLS": "http://u:p@a.example:8000\nhttp://u:p@b.example:8000; "
                             "http://u:p@c.example:8000"}
    )
    assert len(cfg.pool) == 3


def test_pool_spreads_tokens_over_all_proxies():
    """Rozkład ma realnie używać całej puli, nie wrzucać wszystkiego na jeden adres."""
    cfg = ap.load_proxy_config(
        {"APIFY_PROXY_URLS": ",".join(f"http://u:p@n{i}.example:8000" for i in range(5))}
    )
    used = {ap.proxy_for_token(f"apify_api_token_{i}", cfg) for i in range(100)}
    assert len(used) == 5


def test_adding_a_proxy_moves_only_a_small_share_of_accounts():
    """Dołożenie proxy do puli ma przenieść ~1/N kont, a nie przetasować wszystkich.

    Inaczej każde powiększenie puli zmieniałoby adres wyjściowy prawie każdego konta
    naraz — czyli dokładnie ten wzorzec, przed którym proxy ma chronić.
    """
    four = [f"http://u:p@n{i}.example:8000" for i in range(4)]
    cfg4 = ap.load_proxy_config({"APIFY_PROXY_URLS": ",".join(four)})
    cfg5 = ap.load_proxy_config({"APIFY_PROXY_URLS": ",".join(four + ["http://u:p@n4.example:8000"])})
    tokens = [f"apify_api_token_{i}" for i in range(200)]
    moved = sum(1 for t in tokens if ap.proxy_for_token(t, cfg4) != ap.proxy_for_token(t, cfg5))
    assert moved / len(tokens) < 0.35   # ~1/5 z zapasem na losowość hashu


def test_changing_the_proxy_password_does_not_move_accounts():
    """Rotacja hasła u dostawcy to ta sama maszyna i ten sam adres wyjściowy —
    nie ma prawa przerzucić kont na inne IP. Waga liczona z tożsamości proxy."""
    urls = "http://user:{p}@a.example:8000, http://user:{p}@b.example:8000, " \
           "http://user:{p}@c.example:8000"
    stare = ap.load_proxy_config({"APIFY_PROXY_URLS": urls.format(p="stare_haslo")})
    nowe = ap.load_proxy_config({"APIFY_PROXY_URLS": urls.format(p="nowe_haslo")})
    for i in range(50):
        tok = f"apify_api_token_{i}"
        assert (ap.proxy_label(ap.proxy_for_token(tok, stare))
                == ap.proxy_label(ap.proxy_for_token(tok, nowe)))


def test_single_pool_entry_warns_like_a_gateway_without_session():
    """Jedno proxy w puli to nadal JEDEN adres dla wszystkich kont — problem zostaje."""
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": "http://u:p@one.example:8000"})
    assert any("tylko JEDEN adres" in w for w in cfg.warnings)


def test_preflight_flags_when_every_key_shares_one_exit():
    cfg_env = {"APIFY_PROXY_URLS": "http://u:p@one.example:8000"}
    ok, lines = ap.preflight(cfg_env, tokens=["tok_a", "tok_b", "tok_c"])
    assert ok is True
    assert any("wychodzi przez JEDEN adres" in ln for ln in lines)


def test_preflight_quiet_when_exits_really_differ():
    ok, lines = ap.preflight({"APIFY_PROXY_URL": _GW}, tokens=["tok_a", "tok_b"])
    assert ok is True
    assert not any("JEDEN adres" in ln for ln in lines)


def test_duplicate_pool_entries_are_dropped_with_warning():
    cfg = ap.load_proxy_config(
        {"APIFY_PROXY_URLS": "http://u:p@a.example:8000, http://u:p@a.example:8000"}
    )
    assert len(cfg.pool) == 1
    assert any("duplikowanych" in w for w in cfg.warnings)


# ---------------------------------------------------------------------------
# Zakres portów — dostawcy z sesją lepką "port = sesja"
# ---------------------------------------------------------------------------
def test_port_range_expands():
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": "http://u:p@gw.example:10001-10010"})
    assert len(cfg.pool) == 10
    assert cfg.pool[0].endswith(":10001")
    assert cfg.pool[-1].endswith(":10010")


def test_port_range_reversed_is_an_error():
    with pytest.raises(ap.ApifyProxyError, match="odwrócony"):
        ap.load_proxy_config({"APIFY_PROXY_URLS": "http://u:p@gw.example:10010-10001"})


def test_absurd_port_range_is_an_error():
    with pytest.raises(ap.ApifyProxyError, match="limit"):
        ap.load_proxy_config({"APIFY_PROXY_URLS": "http://u:p@gw.example:1-65535"})


def test_plain_url_without_range_is_untouched():
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": "http://u:p@gw.example:8000/path"})
    assert cfg.pool == ("http://u:p@gw.example:8000/path",)


# ---------------------------------------------------------------------------
# Przypisanie per klucz (APIFY_PROXY{N}) i priorytety
# ---------------------------------------------------------------------------
def test_per_key_proxy_wins_over_pool_and_gateway():
    env = {
        "APIFY_API_TOKEN1": "tok_a",
        "APIFY_API_TOKEN2": "tok_b",
        "APIFY_PROXY1": "http://u:p@dedicated.example:8000",
        "APIFY_PROXY_URLS": "http://u:p@pool.example:8000",
        "APIFY_PROXY_URL": _GW,
    }
    cfg = ap.load_proxy_config(env)
    assert ap.proxy_for_token("tok_a", cfg) == "http://u:p@dedicated.example:8000"
    # tok_b nie ma swojego APIFY_PROXY2 -> spada na pulę.
    assert ap.proxy_for_token("tok_b", cfg) == "http://u:p@pool.example:8000"


def test_pool_wins_over_gateway_with_warning():
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": "http://u:p@pool.example:8000",
                                "APIFY_PROXY_URL": _GW})
    assert ap.proxy_for_token("tok_a", cfg) == "http://u:p@pool.example:8000"
    assert any("APIFY_PROXY_URLS i APIFY_PROXY_URL" in w for w in cfg.warnings)


def test_ignored_gateway_does_not_add_its_own_warning():
    """Brama przegrana przez pulę nie ma prawa dokładać ostrzeżenia o {session} —
    dwa sprzeczne komunikaty naraz tylko mylą przy czytaniu logu."""
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": "http://u:p@a.example:8000,"
                                                    "http://u:p@b.example:8000",
                                "APIFY_PROXY_URL": "http://u:p@gw.example:7000"})
    assert not any("APIFY_PROXY_URL nie zawiera" in w for w in cfg.warnings)
    assert len(cfg.warnings) == 1


def test_per_key_only_and_unmapped_token_falls_back_to_none():
    """Same APIFY_PROXY{N}, token bez swojego numeru -> brak proxy (i to widać w logu)."""
    cfg = ap.load_proxy_config({"APIFY_API_TOKEN1": "tok_a",
                                "APIFY_PROXY1": "http://u:p@a.example:8000"})
    assert ap.proxy_for_token("tok_a", cfg) == "http://u:p@a.example:8000"
    assert ap.proxy_for_token("tok_obcy", cfg) is None


def test_per_key_only_and_unmapped_token_raises_when_required():
    cfg = ap.load_proxy_config({"APIFY_API_TOKEN1": "tok_a",
                                "APIFY_PROXY1": "http://u:p@a.example:8000",
                                "APIFY_PROXY_REQUIRED": "1"})
    with pytest.raises(ap.ApifyProxyError, match="nie ma przypisanego proxy"):
        ap.proxy_for_token("tok_obcy", cfg)


def test_legacy_unnumbered_token_uses_pool():
    """Legacy APIFY_API_TOKEN (bez numeru) nie ma numeru, ale pulę dostaje normalnie."""
    cfg = ap.load_proxy_config({"APIFY_API_TOKEN": "tok_legacy",
                                "APIFY_PROXY_URLS": "http://u:p@a.example:8000"})
    assert ap.proxy_for_token("tok_legacy", cfg) == "http://u:p@a.example:8000"


# ---------------------------------------------------------------------------
# proxies_for_token — kolejność zapasowa dla martwych adresów z puli
# ---------------------------------------------------------------------------
def test_ranked_list_starts_with_the_sticky_choice_and_covers_the_whole_pool():
    pool = ",".join(f"http://u:p@n{i}.example:8000" for i in range(5))
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": pool})
    ranked = ap.proxies_for_token("tok_a", cfg)
    assert ranked[0] == ap.proxy_for_token("tok_a", cfg)   # pierwszy = ten sam wybór
    assert len(ranked) == 5 and len(set(ranked)) == 5      # bez powtórek, cała pula


def test_ranked_list_is_stable_and_respects_the_limit():
    pool = ",".join(f"http://u:p@n{i}.example:8000" for i in range(8))
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": pool})
    assert ap.proxies_for_token("tok_a", cfg) == ap.proxies_for_token("tok_a", cfg)
    assert ap.proxies_for_token("tok_a", cfg, limit=3) == ap.proxies_for_token("tok_a", cfg)[:3]


def test_ranked_list_does_not_second_guess_an_explicit_or_gateway_proxy():
    """Adres wskazany wprost (APIFY_PROXY{N}) i brama z {session} to WYBÓR operatora
    i nośnik lepkości — podmienianie ich za jego plecami psułoby jedno i drugie."""
    per_key = ap.load_proxy_config({"APIFY_API_TOKEN1": "tok_a",
                                    "APIFY_PROXY1": "http://u:p@a.example:8000"})
    assert ap.proxies_for_token("tok_a", per_key) == ["http://u:p@a.example:8000"]
    gw = ap.load_proxy_config({"APIFY_PROXY_URL": _GW})
    assert ap.proxies_for_token("tok_a", gw) == [ap.proxy_for_token("tok_a", gw)]


def test_ranked_list_is_empty_without_any_proxy():
    assert ap.proxies_for_token("tok_a", ap.load_proxy_config({})) == []


def test_empty_proxy_values_are_ignored():
    cfg = ap.load_proxy_config({"APIFY_PROXY1": "  ", "APIFY_PROXY_URLS": "  ",
                                "APIFY_PROXY_URL": ""})
    assert cfg.enabled is False


# ---------------------------------------------------------------------------
# Walidacja URL-i — zła konfiguracja ma paść GŁOŚNO, nie cicho wyjść z IP VPS-a
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "gw.example.com:7000",                 # brak schematu
    "ftp://gw.example.com:7000",           # schemat nie do proxy
    "http://",                             # brak hosta
    "http://u:p@gw.example.com:port",      # port nie-liczbowy
    "socks5h://u:p@gw.example:1080",       # httpx zna tylko socks5, nie socks5h
])
def test_malformed_proxy_url_raises(bad):
    with pytest.raises(ap.ApifyProxyError):
        ap.load_proxy_config({"APIFY_PROXY_URL": bad})


def test_allowed_schemes_match_what_httpx_really_accepts():
    """Nasza lista schematów MUSI być podzbiorem tego, co httpx przyjmuje — inaczej
    preflight świeci na zielono, a run wywala się przy pierwszym wołaniu Apify."""
    httpx = pytest.importorskip("httpx")
    for scheme in ap._ALLOWED_SCHEMES:
        try:
            httpx.Client(proxy=f"{scheme}://u:p@gw.example:1080").close()
        except ImportError:
            pass          # socks5 bez paczki socksio — schemat sam w sobie jest znany
        except ValueError as e:
            pytest.fail(f"httpx odrzuca schemat {scheme!r}, a my go dopuszczamy: {e}")


def test_broken_config_stops_the_run_in_preflight():
    ok, lines = ap.preflight({"APIFY_PROXY_URL": "ftp://gw.example.com:7000"})
    assert ok is False
    assert any("BŁĄD KONFIGURACJI" in ln for ln in lines)


def test_socks5_scheme_is_accepted():
    cfg = ap.load_proxy_config({"APIFY_PROXY_URL": "socks5://u:p@gw.example:1080"})
    assert cfg.enabled is True


# ---------------------------------------------------------------------------
# Logi — hasło do proxy NIGDY nie może trafić do logu
# ---------------------------------------------------------------------------
def test_mask_url_hides_password_and_keeps_login():
    masked = ap.mask_url("http://user-session-abc:supertajne@gw.example.com:7000")
    assert "supertajne" not in masked
    assert "user-session-abc" in masked
    assert masked == "http://user-session-abc:***@gw.example.com:7000"


def test_mask_url_without_credentials_is_unchanged():
    assert ap.mask_url("http://gw.example.com:7000") == "http://gw.example.com:7000"


def test_mask_url_hides_password_even_without_scheme():
    """Zapomniany `http://` to najczęstsza literówka w .env — a to WŁAŚNIE ten URL
    ląduje potem w komunikacie błędu i w logu. Hasło musi zniknąć i tutaj."""
    assert ap.mask_url("user:supertajne@gw.example.com:7000") == "user:***@gw.example.com:7000"


def test_validation_error_message_never_carries_the_password():
    with pytest.raises(ap.ApifyProxyError) as exc:
        ap.load_proxy_config({"APIFY_PROXY_URL": "user:supertajne@gw.example.com:7000"})
    assert "supertajne" not in str(exc.value)


def test_proxy_label_shows_only_host_and_port():
    label = ap.proxy_label("http://user:supertajne@gw.example.com:7000")
    assert label == "gw.example.com:7000"
    assert "supertajne" not in label and "user" not in label


def test_proxy_label_without_proxy_says_vps_ip():
    assert "VPS" in ap.proxy_label(None)


def test_describe_never_leaks_the_password():
    cfg = ap.load_proxy_config({"APIFY_PROXY_URL": _GW})
    assert "haslo" not in ap.describe(cfg)


def test_repr_of_config_leaks_neither_password_nor_apify_tokens():
    """Domyślny repr dataklasy wypisałby hasło proxy ORAZ wszystkie surowe tokeny
    Apify — jeden print(cfg) w logu crona wystawiłby całą pulę kont."""
    cfg = ap.load_proxy_config({"APIFY_PROXY_URL": _GW,
                                "APIFY_API_TOKEN1": "apify_api_SEKRETNY_TOKEN"})
    text = repr(cfg)
    assert "haslo" not in text
    assert "apify_api_SEKRETNY_TOKEN" not in text
    assert "ProxyConfig(" in text


# ---------------------------------------------------------------------------
# Klucze za dziurą w numeracji — monitor kredytów ich używa, więc weryfikacja też musi
# ---------------------------------------------------------------------------
def test_all_tokens_from_env_sees_keys_behind_a_gap():
    env = {"APIFY_API_TOKEN1": "tok_a", "APIFY_API_TOKEN2": "tok_b",
           "APIFY_API_TOKEN4": "tok_d",     # dziura na 3 — rotator tu urywa
           "APIFY_API_TOKEN": "tok_legacy", "APIFY_API_TOKEN3": "", "INNE": "x"}
    assert ap.all_tokens_from_env(env) == ["tok_a", "tok_b", "tok_d", "tok_legacy"]


def test_all_tokens_from_env_empty():
    assert ap.all_tokens_from_env({}) == []


def test_is_enabled_is_false_for_broken_config():
    assert ap.is_enabled({"APIFY_PROXY_URL": "ftp://gw.example:7000"}) is False
    assert ap.is_enabled({"APIFY_PROXY_URL": _GW}) is True
    assert ap.is_enabled({}) is False


# ---------------------------------------------------------------------------
# client_for_token — TU proxy albo trafia do httpx, albo cała reszta jest ozdobą
# ---------------------------------------------------------------------------
class _RecordingClient:
    last: dict = {}

    def __init__(self, **kw):
        type(self).last = dict(kw)

    def close(self):
        pass


def test_client_for_token_passes_the_proxy_to_httpx(monkeypatch):
    """Bez tego testu można usunąć przekazanie proxy do httpx i suite zostanie
    zielony — cały mechanizm byłby wtedy atrapą."""
    httpx = pytest.importorskip("httpx")
    monkeypatch.setattr(httpx, "Client", _RecordingClient)
    cfg = ap.load_proxy_config({"APIFY_PROXY_URL": _GW})

    ap.client_for_token("tok_a", timeout=12.5, cfg=cfg)
    assert _RecordingClient.last["proxy"] == ap.proxy_for_token("tok_a", cfg)
    assert _RecordingClient.last["timeout"] == 12.5

    # Różne konta -> RÓŻNE proxy trafiają do httpx (nie jedno dla wszystkich).
    ap.client_for_token("tok_b", cfg=cfg)
    assert _RecordingClient.last["proxy"] != ap.proxy_for_token("tok_a", cfg)


def test_client_for_token_without_config_passes_no_proxy(monkeypatch):
    """Brak konfiguracji = klient BEZ argumentu proxy (zachowanie jak przed zmianą)."""
    httpx = pytest.importorskip("httpx")
    monkeypatch.setattr(httpx, "Client", _RecordingClient)
    ap.client_for_token("tok_a", timeout=5, cfg=ap.load_proxy_config({}))
    assert "proxy" not in _RecordingClient.last


def test_real_httpx_client_is_actually_routed_through_the_proxy():
    """Kontrola na PRAWDZIWYM httpx: klient ma zamontowany transport przez proxy."""
    httpx = pytest.importorskip("httpx")
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": "http://u:p@gw.example:8000"})
    with ap.client_for_token("tok_a", timeout=5, cfg=cfg) as client:
        origins = [t._pool._proxy_url.origin for t in client._mounts.values()
                   if hasattr(t, "_pool") and getattr(t._pool, "_proxy_url", None)]
    assert origins, "klient nie ma transportu przez proxy"
    assert all(o.host == b"gw.example" and o.port == 8000 for o in origins)


def test_assignments_covers_every_token():
    cfg = ap.load_proxy_config({"APIFY_PROXY_URL": _GW})
    pairs = ap.assignments(["tok_a", "tok_b"], cfg)
    assert [t for t, _ in pairs] == ["tok_a", "tok_b"]
    assert all(url for _, url in pairs)


# ---------------------------------------------------------------------------
# Pula proxy z pliku (wejście ZEWNĘTRZNE) — wpięcie w konfigurację
# ---------------------------------------------------------------------------
def _write_pool(path, proxies, updated_at="2026-07-28T12:00:00+00:00"):
    path.write_text(json.dumps({"updated_at": updated_at, "proxies": proxies}),
                    encoding="utf-8")
    return path


def _pool_env(monkeypatch, path):
    monkeypatch.setattr(ap, "_pool_file_path", lambda env: path)


# Plik puli jest czytany TYLKO przy jawnym APIFY_PROXY_POOL=1 (patrz
# test_pool_file_is_ignored_by_default niżej). Testy, które sprawdzają samo
# wpięcie pliku w konfigurację, muszą więc podać tę zmienną.
_POOL_ON = {"APIFY_PROXY_POOL": "1"}


def test_pool_file_feeds_the_pool(monkeypatch, tmp_path):
    p = _write_pool(tmp_path / "pool.json", [
        {"url": "http://1.1.1.1:8000", "exit_ip": "1.1.1.1", "apify_ok": True},
        {"url": "http://2.2.2.2:8000", "exit_ip": "2.2.2.2", "apify_ok": True},
    ], updated_at=datetime.now(timezone.utc).isoformat())
    _pool_env(monkeypatch, p)
    cfg = ap.load_proxy_config(_POOL_ON)
    assert cfg.enabled is True
    assert cfg.pool_from_file == 2
    assert set(cfg.pool) == {"http://1.1.1.1:8000", "http://2.2.2.2:8000"}


def test_pool_file_skips_entries_that_cannot_reach_apify(monkeypatch, tmp_path):
    """Proxy żyje, ale nie dochodzi do Apify -> w puli zabierałoby konto i nie
    działało. Do konfiguracji NIE wchodzi."""
    p = _write_pool(tmp_path / "pool.json", [
        {"url": "http://1.1.1.1:8000", "apify_ok": True},
        {"url": "http://2.2.2.2:8000", "apify_ok": False},
    ], updated_at=datetime.now(timezone.utc).isoformat())
    _pool_env(monkeypatch, p)
    assert ap.load_proxy_config(_POOL_ON).pool == ("http://1.1.1.1:8000",)


def test_pool_file_merges_with_env_urls(monkeypatch, tmp_path):
    """Kilka stałych, płatnych adresów + dosypka z darmowej puli — obie naraz."""
    p = _write_pool(tmp_path / "pool.json",
                    [{"url": "http://9.9.9.9:8000", "apify_ok": True}],
                    updated_at=datetime.now(timezone.utc).isoformat())
    _pool_env(monkeypatch, p)
    cfg = ap.load_proxy_config({**_POOL_ON,
                                "APIFY_PROXY_URLS": "http://u:p@platne.example:8000"})
    assert len(cfg.pool) == 2
    assert cfg.pool_from_file == 1


def test_pool_file_is_ignored_by_default(monkeypatch, tmp_path):
    """Sam LEŻĄCY na dysku plik puli nie może nikogo nigdzie skierować.

    Regresja z 2026-07-31: plik czytał się bez pytania, a że odświeżanie zwracało
    0 zweryfikowanych adresów, w środku został JEDEN stary wpis odpowiadający w
    ~20% prób — i przez niego szedł komplet kont. Nieodświeżana pula jest gorsza
    niż jej brak, więc domyślnie jej nie ruszamy."""
    p = _write_pool(tmp_path / "pool.json",
                    [{"url": "http://1.1.1.1:8000", "apify_ok": True}],
                    updated_at=datetime.now(timezone.utc).isoformat())
    _pool_env(monkeypatch, p)
    cfg = ap.load_proxy_config({})
    assert cfg.enabled is False
    assert cfg.pool_from_file == 0


def test_pool_file_can_be_switched_off(monkeypatch, tmp_path):
    p = _write_pool(tmp_path / "pool.json",
                    [{"url": "http://1.1.1.1:8000", "apify_ok": True}])
    _pool_env(monkeypatch, p)
    assert ap.load_proxy_config({"APIFY_PROXY_POOL": "0"}).enabled is False


def test_stale_pool_file_warns(monkeypatch, tmp_path):
    """Tanie proxy gniją w godzinach — stara pula w logu wygląda jak świeża,
    a w praktyce to seria timeoutów.

    Ostrzeżenie ma nie tylko krzyknąć "STARY", ale powiedzieć CO Z TYM ZROBIĆ.
    W repo źródłowym wskazywało własny generator puli (`--refresh`); tutaj tego
    generatora NIE MA — plik jest wejściem zewnętrznym — więc komunikat kieruje
    na zmienną, która o nim decyduje. Test pilnuje właśnie tej użyteczności:
    samo "STARY" zostawia czytającego bez następnego kroku.
    """
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    p = _write_pool(tmp_path / "pool.json",
                    [{"url": "http://1.1.1.1:8000", "apify_ok": True},
                     {"url": "http://2.2.2.2:8000", "apify_ok": True}], updated_at=old)
    _pool_env(monkeypatch, p)
    cfg = ap.load_proxy_config(_POOL_ON)
    assert any("STARY" in w for w in cfg.warnings)
    assert "APIFY_PROXY_POOL_FILE" in " ".join(cfg.warnings)


def test_fresh_pool_file_does_not_warn(monkeypatch, tmp_path):
    p = _write_pool(tmp_path / "pool.json",
                    [{"url": "http://1.1.1.1:8000", "apify_ok": True},
                     {"url": "http://2.2.2.2:8000", "apify_ok": True}],
                    updated_at=datetime.now(timezone.utc).isoformat())
    _pool_env(monkeypatch, p)
    assert not any("STARY" in w for w in ap.load_proxy_config(_POOL_ON).warnings)


def test_broken_pool_file_is_not_an_error(monkeypatch, tmp_path):
    """Uszkodzony plik puli ma dać zachowanie 'brak proxy' (z ostrzeżeniem
    z describe), a nie wywalić run scrapera."""
    p = tmp_path / "pool.json"
    p.write_text("to nie jest JSON", encoding="utf-8")
    _pool_env(monkeypatch, p)
    cfg = ap.load_proxy_config(_POOL_ON)
    assert cfg.enabled is False
    assert "BRAK proxy" in ap.describe(cfg)


def test_malformed_entry_in_pool_file_is_skipped_not_fatal(monkeypatch, tmp_path):
    p = _write_pool(tmp_path / "pool.json", [
        {"url": "ftp://zly.example:1", "apify_ok": True},
        {"url": "http://1.1.1.1:8000", "apify_ok": True},
    ], updated_at=datetime.now(timezone.utc).isoformat())
    _pool_env(monkeypatch, p)
    cfg = ap.load_proxy_config(_POOL_ON)
    assert cfg.pool == ("http://1.1.1.1:8000",)
    assert any("pominięto wpis z pliku puli" in w for w in cfg.warnings)


def test_describe_mentions_the_free_pool_and_its_age(monkeypatch, tmp_path):
    p = _write_pool(tmp_path / "pool.json",
                    [{"url": "http://1.1.1.1:8000", "apify_ok": True},
                     {"url": "http://2.2.2.2:8000", "apify_ok": True}],
                    updated_at=datetime.now(timezone.utc).isoformat())
    _pool_env(monkeypatch, p)
    text = ap.describe(ap.load_proxy_config(_POOL_ON))
    assert "darmowej puli" in text
