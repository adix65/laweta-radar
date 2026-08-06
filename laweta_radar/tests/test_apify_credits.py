"""
Offline testy laweta_radar/workers/apify_credits.py — stan konta dla `/limity`.

SEDNO: trzy ROZŁĄCZNE stany konta (żywe+saldo, żywe+bez salda, martwe) plus
timeout jako czwarty, osobny wynik. Bez sieci: `client_for_token` jest
podmieniony na atrapę, która oddaje zaprogramowane odpowiedzi/wyjątki per URL.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.workers import apify_credits as ac  # noqa: E402


class _FakeResp:
    def __init__(self, status_code: int, dane: dict | None = None) -> None:
        self.status_code = status_code
        self._dane = dane or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise _HTTPError(self.status_code)

    def json(self):
        return self._dane


class _HTTPError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.response = _FakeResp(code)


class _Timeout(Exception):
    """Nazwa niesie 'timeout' — to jedyne, po czym ten test go rozpoznaje."""


class _FakeClient:
    def __init__(self, mapowanie: dict) -> None:
        self.mapowanie = mapowanie

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        for fragment, wynik in self.mapowanie.items():
            if fragment in url:
                if isinstance(wynik, BaseException):
                    raise wynik
                return wynik
        raise AssertionError(f"nieoczekiwany URL w teście: {url}")


def _client_factory(mapowanie):
    return lambda token, timeout=None, env=None, cfg=None: _FakeClient(mapowanie)


TOKEN_TAJNY = "apify_api_SEKRETNY_TOKEN_NIE_MA_PRAWA_WYCIEKNAC"


# ---------------------------------------------------------------------------
# stan_konta — trzy stany + timeout
# ---------------------------------------------------------------------------
def test_stan_konta_ok_saldo_znane(monkeypatch):
    mapowanie = {
        "/users/me/limits": _FakeResp(200, {"data": {
            "current": {"monthlyUsageUsd": 3.10},
            "limits": {"maxMonthlyUsageUsd": 5.0},
        }}),
        "/users/me": _FakeResp(200, {"data": {"username": "poignant_kefir"}}),
    }
    monkeypatch.setattr(ac, "client_for_token", _client_factory(mapowanie))
    s = ac.stan_konta(TOKEN_TAJNY)
    assert s.stan == ac.STAN_OK_ZNANE
    assert s.nazwa == "poignant_kefir"
    assert s.saldo.uzyte_usd == 3.10
    assert s.saldo.limit_usd == 5.0
    assert TOKEN_TAJNY not in repr(s)


def test_stan_konta_ok_saldo_nieznane_konto_zywe(monkeypatch):
    """Darmowe konto: /users/me odpowiada, /users/me/limits nie — to NIE jest błąd."""
    mapowanie = {
        "/users/me/limits": _HTTPError(403),
        "/users/me": _FakeResp(200, {"data": {"username": "cichy_kefir"}}),
    }
    monkeypatch.setattr(ac, "client_for_token", _client_factory(mapowanie))
    s = ac.stan_konta(TOKEN_TAJNY)
    assert s.stan == ac.STAN_OK_NIEZNANE
    assert s.nazwa == "cichy_kefir"
    assert s.saldo is None


def test_stan_konta_martwy(monkeypatch):
    mapowanie = {"/users/me": _FakeResp(401, {})}
    monkeypatch.setattr(ac, "client_for_token", _client_factory(mapowanie))
    s = ac.stan_konta(TOKEN_TAJNY)
    assert s.stan == ac.STAN_MARTWY
    assert s.nazwa == ""
    assert "401" in s.powod


def test_stan_konta_brak_odpowiedzi_na_timeout(monkeypatch):
    mapowanie = {"/users/me": _Timeout("read timed out")}
    monkeypatch.setattr(ac, "client_for_token", _client_factory(mapowanie))
    s = ac.stan_konta(TOKEN_TAJNY)
    assert s.stan == ac.STAN_BRAK_ODPOWIEDZI
    # Timeout NIE jest martwym kluczem — inna reakcja operatora (poczekać, nie alarmować).
    assert s.stan != ac.STAN_MARTWY


def test_stan_konta_nigdy_nie_wycieka_tokenu(monkeypatch):
    """Token pojawia się w treści wyjątku (najgorszy możliwy przypadek) — mimo to
    NIE ma prawa trafić do żadnego pola StanKonta."""
    mapowanie = {"/users/me": RuntimeError(f"connection to proxy failed, token={TOKEN_TAJNY}")}
    monkeypatch.setattr(ac, "client_for_token", _client_factory(mapowanie))
    s = ac.stan_konta(TOKEN_TAJNY)
    assert TOKEN_TAJNY not in s.nazwa
    assert TOKEN_TAJNY not in s.powod
    assert TOKEN_TAJNY not in repr(s)


# ---------------------------------------------------------------------------
# pula_stanu — równoległość + cache
# ---------------------------------------------------------------------------
def test_pula_stanu_odpytuje_wszystkie_tokeny(monkeypatch):
    mapowanie = {
        "/users/me/limits": _FakeResp(200, {"data": {
            "current": {"monthlyUsageUsd": 1.0}, "limits": {"maxMonthlyUsageUsd": 5.0}}}),
        "/users/me": _FakeResp(200, {"data": {"username": "u"}}),
    }
    monkeypatch.setattr(ac, "client_for_token", _client_factory(mapowanie))
    wyniki = ac.pula_stanu(["t1", "t2", "t3"], cache_ttl=0)
    assert len(wyniki) == 3
    assert all(w.stan == ac.STAN_OK_ZNANE for w in wyniki)


def test_pula_stanu_cache_nie_odpytuje_ponownie_w_oknie(monkeypatch):
    wolania = {"n": 0}

    def _client(token, timeout=None, env=None, cfg=None):
        wolania["n"] += 1
        return _FakeClient({"/users/me": _FakeResp(401, {})})

    monkeypatch.setattr(ac, "client_for_token", _client)
    czas = {"t": 100.0}
    ac.pula_stanu(["a", "b"], cache_ttl=300, _teraz=lambda: czas["t"])
    wolania_po_pierwszym = wolania["n"]
    assert wolania_po_pierwszym == 2          # jedno wywołanie /users/me na token

    czas["t"] += 60      # w oknie 300s
    ac.pula_stanu(["a", "b"], cache_ttl=300, _teraz=lambda: czas["t"])
    assert wolania["n"] == wolania_po_pierwszym       # cache trafiony, zero nowych wywołań

    czas["t"] += 600     # poza oknem
    ac.pula_stanu(["a", "b"], cache_ttl=300, _teraz=lambda: czas["t"])
    assert wolania["n"] == wolania_po_pierwszym * 2   # cache wygasł, odpytano znowu


def test_pula_stanu_pusta_lista():
    assert ac.pula_stanu([], cache_ttl=0) == []


# ---------------------------------------------------------------------------
# Runner bez pytesta
# ---------------------------------------------------------------------------
def _run_tests() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            import inspect
            if "monkeypatch" in inspect.signature(t).parameters:
                continue  # wymaga pytest — pomijamy w trybie bez frameworka
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} testów przeszło (bez tych wymagających pytest)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_tests())
