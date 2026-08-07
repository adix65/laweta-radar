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


# =============================================================================
# WERYFIKACJA — cztery testy z docs/APIFY-PROXY.md (workers/apify_proxy.weryfikuj_proxy)
# =============================================================================
class _FakeResp:
    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeVerifyClient:
    def __init__(self, respond) -> None:
        self._respond = respond

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return self._respond(url)


def _client_maker(respond_factory):
    """respond_factory(proxy_kwarg) -> callable(url) -> _FakeResp albo rzuca."""
    def _Client(**kw):
        return _FakeVerifyClient(respond_factory(kw.get("proxy")))
    return _Client


def _respond_szczesliwie(_proxy):
    def _r(url):
        if "ipify" in url:
            return _FakeResp("5.6.7.8")
        if "apify.com" in url:
            return _FakeResp("", 401)     # 401 bez tokenu = SUKCES testu (d)
        raise AssertionError(f"nieoczekiwany url {url}")
    return _r


def test_weryfikuj_proxy_przechodzi_wszystkie_cztery_testy(monkeypatch):
    httpx = pytest.importorskip("httpx")
    monkeypatch.setattr(httpx, "Client", _client_maker(_respond_szczesliwie))
    w = ap.weryfikuj_proxy("http://u:p@proxy.example:8000", direct_ip="1.2.3.4")
    assert w.ok is True
    assert (w.odpowiada, w.nietransparentne, w.unikalne, w.dochodzi_do_apify) == (True,) * 4


def test_weryfikuj_proxy_nie_odpowiada(monkeypatch):
    httpx = pytest.importorskip("httpx")

    def respond_factory(_proxy):
        def _r(_url):
            raise RuntimeError("connection refused")
        return _r

    monkeypatch.setattr(httpx, "Client", _client_maker(respond_factory))
    w = ap.weryfikuj_proxy("http://u:p@martwe.example:8000", direct_ip="1.2.3.4")
    assert w.ok is False
    assert w.odpowiada is False
    assert w.blad     # powód zapisany, nie pusty


def test_weryfikuj_proxy_transparentne_oddaje_ip_vpsa(monkeypatch):
    """Test (b): proxy odpowiada, ale zwraca to samo IP, co bezpośrednie wyjście
    — czyli nie jest wcale proxy, tylko przezroczystym pass-through."""
    httpx = pytest.importorskip("httpx")

    def respond_factory(_proxy):
        def _r(url):
            if "ipify" in url:
                return _FakeResp("9.9.9.9")     # to samo co direct_ip niżej
            raise AssertionError("test (d) nie powinien być wołany po (b)")
        return _r

    monkeypatch.setattr(httpx, "Client", _client_maker(respond_factory))
    w = ap.weryfikuj_proxy("http://u:p@transparentne.example:8000", direct_ip="9.9.9.9")
    assert w.odpowiada is True
    assert w.nietransparentne is False
    assert w.ok is False
    assert "IP VPS" in w.blad


def test_weryfikuj_proxy_nie_dochodzi_do_apify(monkeypatch):
    """Test (d): proxy 'działa' (inne serwisy odpowiadają), ale do api.apify.com
    nie dochodzi — dokładnie ten przypadek, na którym kończy się większość
    darmowych list (docs/APIFY-PROXY.md)."""
    httpx = pytest.importorskip("httpx")

    def respond_factory(_proxy):
        def _r(url):
            if "ipify" in url:
                return _FakeResp("5.6.7.8")
            if "apify.com" in url:
                raise RuntimeError("connection timed out")
            raise AssertionError(url)
        return _r

    monkeypatch.setattr(httpx, "Client", _client_maker(respond_factory))
    w = ap.weryfikuj_proxy("http://u:p@nie-dochodzi.example:8000", direct_ip="1.2.3.4")
    assert w.odpowiada is True and w.nietransparentne is True
    assert w.dochodzi_do_apify is False
    assert w.ok is False


def test_weryfikuj_proxy_bez_znanego_direct_ip_nie_oznacza_transparentnosci(monkeypatch):
    httpx = pytest.importorskip("httpx")
    monkeypatch.setattr(httpx, "Client", _client_maker(_respond_szczesliwie))
    w = ap.weryfikuj_proxy("http://u:p@proxy.example:8000", direct_ip=None)
    assert w.nietransparentne is True


def test_zweryfikuj_pule_wykrywa_duplikat_tozsamosci(monkeypatch):
    """(c): dwa wpisy z tym samym host:port (różny tylko login) to JEDNO
    proxy — drugie wystąpienie ma wyjść jako NIE unikalne."""
    httpx = pytest.importorskip("httpx")
    monkeypatch.setattr(httpx, "Client", _client_maker(_respond_szczesliwie))
    wyniki = ap.zweryfikuj_pule([
        "http://usera:p@dup.example:8000",
        "http://userb:p@dup.example:8000",
        "http://inny.example:8000",
    ])
    assert [w.unikalne for w in wyniki] == [True, False, True]


def test_zweryfikuj_pule_pusta_lista(monkeypatch):
    assert ap.zweryfikuj_pule([]) == []


# =============================================================================
# KWARANTANNA — stan w bazie (workers/apify_proxy.oznacz_kwarantanna i spółka)
# =============================================================================
class _KursorProxyDB:
    def __init__(self, tabela: dict) -> None:
        self._tabela = tabela      # {proxy_hash: (etykieta, status, od_kiedy, wraca_o, ile_bledow)}
        self._wynik: list = []
        self.zapytania: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql: str, params=None) -> None:
        self.zapytania.append((sql, params))
        if sql.strip().startswith("SELECT proxy_hash"):
            (hashes,) = params
            self._wynik = [(h, *self._tabela[h]) for h in hashes if h in self._tabela]

    def fetchall(self):
        return self._wynik


class _PolaczenieProxyDB:
    def __init__(self, tabela: dict | None = None) -> None:
        self._tabela = tabela if tabela is not None else {}
        self.commity = 0

    def cursor(self):
        return _KursorProxyDB(self._tabela)

    def commit(self):
        self.commity += 1


def test_oznacz_kwarantanna_zapisuje_hash_i_etykiete_bez_hasla():
    tabela: dict = {}
    conn = _PolaczenieProxyDB(tabela)
    kursor = conn.cursor()
    kursor._tabela = tabela
    ap_conn = _JedenKursorConn(kursor)
    ap.oznacz_kwarantanna(ap_conn, "http://user:tajnehaslo@a.example:8000", "timeout")
    sql, params = kursor.zapytania[0]
    assert "zasoby_apify_proxy" in sql
    assert params[0] == ap._hash_proxy("http://user:tajnehaslo@a.example:8000")
    assert "tajnehaslo" not in " ".join(str(p) for p in params)
    assert "a.example:8000" in params[1]     # etykieta = host:port
    assert ap_conn.commity == 1


class _JedenKursorConn:
    """Połączenie oddające ZAWSZE ten sam kursor — do sprawdzenia, co realnie
    poszło do execute() bez polegania na tym, ile razy woła się `cursor()`."""

    def __init__(self, kursor) -> None:
        self._kursor = kursor
        self.commity = 0

    def cursor(self):
        return self._kursor

    def commit(self):
        self.commity += 1


def test_wczytaj_stan_proxy_mapuje_hash_z_powrotem_na_url():
    from datetime import datetime, timezone

    url = "http://u:p@a.example:8000"
    h = ap._hash_proxy(url)
    teraz = datetime.now(timezone.utc)
    conn = _PolaczenieProxyDB({h: ("a.example:8000", "kwarantanna", teraz, teraz, 2)})
    stan = ap.wczytaj_stan_proxy(conn, [url])
    assert stan[url]["status"] == "kwarantanna"
    assert stan[url]["ile_bledow"] == 2


def test_w_kwarantannie_prawda_gdy_nie_minelo(monkeypatch):
    from datetime import datetime, timedelta, timezone

    url = "http://u:p@a.example:8000"
    h = ap._hash_proxy(url)
    teraz = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    wraca_o = teraz + timedelta(minutes=10)
    conn = _PolaczenieProxyDB({h: ("a.example:8000", "kwarantanna", teraz, wraca_o, 1)})
    assert ap.w_kwarantannie(conn, url, teraz=teraz) is True


def test_w_kwarantannie_falsz_po_uplywie_czasu():
    from datetime import datetime, timedelta, timezone

    url = "http://u:p@a.example:8000"
    h = ap._hash_proxy(url)
    teraz = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    wraca_o = teraz - timedelta(minutes=1)      # kwarantanna już minęła
    conn = _PolaczenieProxyDB({h: ("a.example:8000", "kwarantanna", teraz, wraca_o, 1)})
    assert ap.w_kwarantannie(conn, url, teraz=teraz) is False


def test_w_kwarantannie_falsz_dla_nieznanego_proxy():
    assert ap.w_kwarantannie(_PolaczenieProxyDB(), "http://u:p@nieznane.example:8000") is False


def test_proxy_zywy_dla_tokenu_pomija_kwarantanne():
    pool = ",".join(f"http://u:p@n{i}.example:8000" for i in range(5))
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": pool})
    ranking = ap.proxies_for_token("tok_a", cfg)
    pierwszy, drugi = ranking[0], ranking[1]

    from datetime import datetime, timedelta, timezone
    teraz = datetime.now(timezone.utc)
    tabela = {ap._hash_proxy(pierwszy): ("x", "kwarantanna", teraz, teraz + timedelta(minutes=5), 1)}
    conn = _PolaczenieProxyDB(tabela)

    assert ap.proxy_zywy_dla_tokenu("tok_a", conn, cfg) == drugi


def test_proxy_zywy_dla_tokenu_none_gdy_cala_ranga_w_kwarantannie():
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": "http://u:p@n0.example:8000"})
    ranking = ap.proxies_for_token("tok_a", cfg)
    from datetime import datetime, timedelta, timezone
    teraz = datetime.now(timezone.utc)
    tabela = {ap._hash_proxy(u): ("x", "kwarantanna", teraz, teraz + timedelta(minutes=5), 1)
              for u in ranking}
    conn = _PolaczenieProxyDB(tabela)
    assert ap.proxy_zywy_dla_tokenu("tok_a", conn, cfg) is None


def test_proxy_zywy_dla_tokenu_bez_puli_zwraca_wybor_operatora():
    """Przy APIFY_PROXY{N} / bramie z {session} adres jest wyborem operatora —
    proxy_zywy_dla_tokenu nie ma go czym podmienić, nawet gdyby był w kwarantannie."""
    cfg = ap.load_proxy_config({"APIFY_API_TOKEN1": "tok_a",
                                "APIFY_PROXY1": "http://u:p@dedicated.example:8000"})
    conn = _PolaczenieProxyDB()
    assert ap.proxy_zywy_dla_tokenu("tok_a", conn, cfg) == "http://u:p@dedicated.example:8000"


def test_hash_proxy_stabilny_i_nie_niesie_haslo():
    h1 = ap._hash_proxy("http://user:haslo1@a.example:8000")
    h2 = ap._hash_proxy("http://user:haslo2@a.example:8000")
    assert h1 == h2                     # zmiana hasła nie rusza tożsamości
    assert "haslo1" not in h1 and "haslo2" not in h1
    assert len(h1) == 24


def test_oznacz_kwarantanna_niesie_eskalacje_do_doby_w_zapytaniu():
    """Trzecia awaria Z RZĘDU ma wydłużyć kwarantannę do doby (CASE na
    ile_bledow+1). Prawdziwe liczenie "z rzędu" robi Postgres (ON CONFLICT DO
    UPDATE) — offline sprawdzamy KONTRAKT zapytania: próg i wydłużony czas
    faktycznie trafiają do parametrów, nie tylko stałe pół godziny."""
    tabela: dict = {}
    kursor = _KursorProxyDB(tabela)
    conn = _JedenKursorConn(kursor)
    ap.oznacz_kwarantanna(conn, "http://u:p@a.example:8000", "timeout")
    sql, params = kursor.zapytania[0]
    assert "CASE" in sql and "ile_bledow + 1" in sql
    assert ap.KWARANTANNA_ESKALACJA_PROG in params
    assert ap.KWARANTANNA_ESKALACJA_MIN in params
    assert ap.KWARANTANNA_ESKALACJA_MIN == 24 * 60


# =============================================================================
# WYRÓWNANIE PO HASHU — sekcja 4 zadania „większa pula proxy"
# =============================================================================
def test_wyrownanie_daje_1_do_1_gdy_pula_wystarcza():
    """Pula >= liczba kluczy -> KAŻDY klucz dostaje WŁASNY adres, bez dzielenia."""
    pool = ",".join(f"http://u:p@n{i}.example:8000" for i in range(10))
    tokens = [f"apify_api_token_{i}" for i in range(10)]
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": pool}, tokens=tokens)
    assert len(cfg.balanced) == 10
    przydzielone = [ap.proxy_for_token(t, cfg) for t in tokens]
    assert len(set(przydzielone)) == 10          # zero powtórzeń


def test_wyrownanie_bez_tokens_nie_zmienia_zachowania():
    """Stary kontrakt: `load_proxy_config` bez `tokens` -> `balanced` puste,
    zachowanie identyczne jak przed wyrównaniem (sam rendezvous hashing)."""
    pool = ",".join(f"http://u:p@n{i}.example:8000" for i in range(3))
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": pool})
    assert cfg.balanced == {}


def test_wyrownanie_jest_stabilne_dla_tego_samego_zestawu():
    pool = ",".join(f"http://u:p@n{i}.example:8000" for i in range(5))
    tokens = [f"apify_api_token_{i}" for i in range(5)]
    cfg1 = ap.load_proxy_config({"APIFY_PROXY_URLS": pool}, tokens=tokens)
    cfg2 = ap.load_proxy_config({"APIFY_PROXY_URLS": pool}, tokens=tokens)
    assert cfg1.balanced == cfg2.balanced


def test_wyrownanie_nie_zalezy_od_kolejnosci_puli_w_env():
    urls = [f"http://u:p@n{i}.example:8000" for i in range(6)]
    tokens = [f"apify_api_token_{i}" for i in range(6)]
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": ",".join(urls)}, tokens=tokens)
    cfg_rev = ap.load_proxy_config({"APIFY_PROXY_URLS": ",".join(reversed(urls))}, tokens=tokens)
    assert cfg.balanced == cfg_rev.balanced


def test_wyrownanie_pomija_tokeny_z_wlasnym_proxy_per_klucz():
    """APIFY_PROXY{N} to wybór operatora — nie wchodzi do wyrównania puli."""
    env = {
        "APIFY_API_TOKEN1": "tok_a",
        "APIFY_PROXY1": "http://u:p@dedicated.example:8000",
        "APIFY_PROXY_URLS": "http://u:p@n0.example:8000,http://u:p@n1.example:8000",
    }
    cfg = ap.load_proxy_config(env, tokens=["tok_a", "tok_b"])
    assert "tok_a" not in cfg.balanced
    assert ap.proxy_for_token("tok_a", cfg) == "http://u:p@dedicated.example:8000"
    assert "tok_b" in cfg.balanced


def test_wyrownanie_przy_niedoborze_puli_niektorzy_dziela_adres():
    """Więcej kluczy niż proxy -> wyrównanie nie ma czarów, ktoś musi dzielić,
    ale KAŻDY token nadal dostaje jakiś adres (nikt nie zostaje bez proxy)."""
    pool = ",".join(f"http://u:p@n{i}.example:8000" for i in range(3))
    tokens = [f"apify_api_token_{i}" for i in range(7)]
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": pool}, tokens=tokens)
    assert len(cfg.balanced) == 7
    assert all(ap.proxy_for_token(t, cfg) for t in tokens)
    assert len({ap.proxy_for_token(t, cfg) for t in tokens}) == 3


def test_wyrownanie_dolozenie_klucza_rusza_najwyzej_garstke_przypisan():
    """Dołożenie JEDNEGO klucza MOŻE skonsumować adres, który wcześniej był
    zapasowym wyborem dla nadmiarowego tokenu (nowy klucz ma prawo do WŁASNEGO
    najlepszego wolnego adresu) — ale ma to poruszyć co najwyżej garstkę
    przypisań, a nie przetasować całą pulę. Każdy token, który miał WŁASNY
    (niedzielony) adres, zatrzymuje go zawsze — pass-1 to czysta funkcja
    (token, pool), niezależna od reszty zestawu kluczy."""
    pool = ",".join(f"http://u:p@n{i}.example:8000" for i in range(20))
    tokens = [f"apify_api_token_{i}" for i in range(15)]
    cfg_przed = ap.load_proxy_config({"APIFY_PROXY_URLS": pool}, tokens=tokens)
    cfg_po = ap.load_proxy_config({"APIFY_PROXY_URLS": pool},
                                  tokens=[*tokens, "apify_api_token_15"])
    zmienione = sum(1 for t in tokens if cfg_przed.balanced[t] != cfg_po.balanced[t])
    assert zmienione <= 2


def test_main_cli_uzywa_wyrownania_pula_wystarcza_na_16_kluczy(monkeypatch, capsys):
    """Regresja dokładnie zgłoszonego objawu: pula 48 proxy na 16 kluczy pokazywała
    w CLI (`python -m laweta_radar.workers.apify_proxy`) tylko 13 różnych adresów,
    z kolizjami (np. dwa klucze na tym samym IP), mimo że wolnych adresów było pod
    dostatkiem. Powód: `_main` budował `cfg` PRZED poznaniem listy tokenów, więc
    `load_proxy_config()` szedł bez `tokens=` i `_wyrownaj_przypisania` nigdy się
    nie odpalał — podgląd liczył goły rendezvous hashing zamiast wyrównanego
    przypisania. Po naprawie pula >= liczba kluczy ma dać KAŻDEMU kluczowi WŁASNY
    adres i NIE odpalać ostrzeżenia 'rozkład po hashu nie jest 1:1'."""
    for i in range(1, 17):
        monkeypatch.setenv(f"APIFY_API_TOKEN{i}", f"tok_{i}")
    pool = ",".join(f"http://u:p@n{i}.example:8000" for i in range(48))
    monkeypatch.setenv("APIFY_PROXY_URLS", pool)

    rc = ap._main(["apify_proxy"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Różnych proxy w użyciu: 16" in out
    assert "kont bez proxy: 0" in out
    assert "Najwięcej kont na jednym proxy: 1" in out
    assert "nie jest 1:1" not in out


# =============================================================================
# zywe_proxy_w_puli — bezpiecznik przy wyczerpaniu żywych adresów (sekcja 3)
# =============================================================================
def test_zywe_proxy_w_puli_bez_puli_jest_none():
    cfg = ap.load_proxy_config({"APIFY_PROXY_URL": _GW})
    assert ap.zywe_proxy_w_puli(_PolaczenieProxyDB(), cfg) is None


def test_zywe_proxy_w_puli_liczy_aktywne():
    pool = ["http://u:p@a.example:8000", "http://u:p@b.example:8000",
            "http://u:p@c.example:8000"]
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": ",".join(pool)})
    from datetime import datetime, timedelta, timezone
    teraz = datetime.now(timezone.utc)
    tabela = {ap._hash_proxy(pool[0]): ("x", "kwarantanna", teraz,
                                        teraz + timedelta(minutes=5), 1)}
    conn = _PolaczenieProxyDB(tabela)
    assert ap.zywe_proxy_w_puli(conn, cfg) == 2


def test_zywe_proxy_w_puli_zero_gdy_wszystko_w_kwarantannie():
    pool = ["http://u:p@a.example:8000", "http://u:p@b.example:8000"]
    cfg = ap.load_proxy_config({"APIFY_PROXY_URLS": ",".join(pool)})
    from datetime import datetime, timedelta, timezone
    teraz = datetime.now(timezone.utc)
    tabela = {ap._hash_proxy(u): ("x", "kwarantanna", teraz, teraz + timedelta(minutes=5), 1)
              for u in pool}
    conn = _PolaczenieProxyDB(tabela)
    assert ap.zywe_proxy_w_puli(conn, cfg) == 0
