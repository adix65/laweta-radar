"""
Offline test laweta_radar/workers/apify_keys.py (REAKTYWNA rotacja kluczy Apify). Sprawdza:

  1. load_apify_tokens — APIFY_API_TOKEN1..N (pomija puste, kończy na pierwszej luce),
     fallback na legacy APIFY_API_TOKEN, brak kluczy -> pusta lista.
  2. classify_apify_error — 401/402/403 = exhausted, 429/5xx = transient, słowa w
     komunikacie (limit/credit/...) = exhausted, inne 4xx = fatal.
  3. Rotacja: błąd WYCZERPANIA przeskakuje na następny klucz i zwraca jego wynik;
     kolejne wywołanie startuje już od działającego (wyczerpany NIE jest wołany).
  4. Błąd PRZEJŚCIOWY (timeout) -> ponowienia TYM SAMYM kluczem (bez przeskoku);
     gdy trwały — wyjątek leci wyżej, a DOBRE klucze nie są marnowane.
  5. WSZYSTKIE klucze wyczerpane -> AllKeysExhausted (czysty błąd).
  6. Plik stanu — po przeskoku zapisuje indeks działającego klucza; nowy rotator
     startuje od niego.

BEZ sieci/DB/Apify: fn(token) to atrapa, która rzuca skonfigurowane wyjątki. sleep
podmieniamy na no-op (zero realnych odstępów). Uruchamialne pod pytest ORAZ wprost:
`python3 laweta_radar/tests/test_apify_keys.py`.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.workers.apify_keys import (  # noqa: E402
    AllKeysExhausted,
    KeyRotator,
    classify_apify_error,
    load_apify_tokens,
)


# --- Atrapy błędów (kształt jak z httpx / apify-client) ----------------------
class _FakeResp:
    def __init__(self, code: int) -> None:
        self.status_code = code


class _HTTPError(Exception):
    """Jak httpx.HTTPStatusError: niesie .response.status_code."""
    def __init__(self, code: int, msg: str = "") -> None:
        super().__init__(msg or f"HTTP {code}")
        self.response = _FakeResp(code)


class ConnectTimeout(Exception):
    """Nazwa zawiera 'timeout'/'connect' -> _is_network_error rozpozna jako sieć."""


def _noop_sleep(_seconds: float) -> None:
    return None


def _silent(_msg: str) -> None:
    return None


def _rotator(tokens, **kw) -> KeyRotator:
    kw.setdefault("state_path", None)      # domyślnie BEZ pliku stanu (czysto)
    kw.setdefault("sleep", _noop_sleep)
    kw.setdefault("log", _silent)
    return KeyRotator(tokens, **kw)


# ---------------------------------------------------------------------------
# 1) Wczytywanie listy kluczy
# ---------------------------------------------------------------------------
def test_load_numbered_skips_empty_stops_on_gap() -> None:
    env = {"APIFY_API_TOKEN1": "a", "APIFY_API_TOKEN2": "  ",
           "APIFY_API_TOKEN3": "c", "APIFY_API_TOKEN5": "e"}  # 4 brakuje -> stop na 4
    assert load_apify_tokens(env) == ["a", "c"]


def test_load_legacy_fallback() -> None:
    assert load_apify_tokens({"APIFY_API_TOKEN": "legacy"}) == ["legacy"]
    # numerowane mają priorytet — legacy ignorowane, gdy jest choć jeden numerowany
    assert load_apify_tokens({"APIFY_API_TOKEN1": "n1",
                              "APIFY_API_TOKEN": "legacy"}) == ["n1"]


def test_load_none() -> None:
    assert load_apify_tokens({}) == []


# ---------------------------------------------------------------------------
# 2) Klasyfikacja błędu
# ---------------------------------------------------------------------------
def test_classify_status_codes() -> None:
    for code in (401, 402, 403):
        assert classify_apify_error(_HTTPError(code)) == "exhausted"
    for code in (429, 500, 502, 503):
        assert classify_apify_error(_HTTPError(code)) == "transient"
    assert classify_apify_error(_HTTPError(400)) == "fatal"
    assert classify_apify_error(_HTTPError(404)) == "fatal"


def test_classify_message_keywords() -> None:
    # Bez kodu HTTP, ale komunikat jednoznaczny -> exhausted.
    for msg in ("Monthly usage hard limit exceeded",
                "insufficient credit on account", "Payment required", "quota reached"):
        assert classify_apify_error(RuntimeError(msg)) == "exhausted"
    # 429 z "rate limit exceeded" NIE może być uznane za wyczerpanie (kod ma priorytet).
    assert classify_apify_error(_HTTPError(429, "rate limit exceeded")) == "transient"


def test_classify_network() -> None:
    assert classify_apify_error(ConnectTimeout("read timed out")) == "transient"


# ---------------------------------------------------------------------------
# 3) Rotacja reaktywna na wyczerpaniu
# ---------------------------------------------------------------------------
def test_exhausted_key_switches_to_next() -> None:
    calls: list[str] = []

    def fn(token: str) -> str:
        calls.append(token)
        if token == "k1":
            raise _HTTPError(402, "usage limit exceeded")  # k1 wyczerpany
        return f"ok:{token}"

    rot = _rotator(["k1", "k2", "k3"])
    assert rot.call(fn) == "ok:k2"          # przeskok k1 -> k2
    assert calls == ["k1", "k2"]

    # Drugie wywołanie startuje JUŻ od k2 (k1 znany jako wyczerpany — nie wołamy go).
    calls.clear()
    assert rot.call(fn) == "ok:k2"
    assert calls == ["k2"]


# ---------------------------------------------------------------------------
# 4) Błąd przejściowy — ponowienia tym samym kluczem, bez przeskoku
# ---------------------------------------------------------------------------
def test_transient_retries_same_key_then_succeeds() -> None:
    calls: list[str] = []
    state = {"fails": 2}

    def fn(token: str) -> str:
        calls.append(token)
        if state["fails"] > 0:
            state["fails"] -= 1
            raise ConnectTimeout("temporary network blip")
        return "ok"

    rot = _rotator(["k1", "k2"], transient_attempts=3)
    assert rot.call(fn) == "ok"
    assert calls == ["k1", "k1", "k1"]      # ten sam klucz 3x, BEZ skoku na k2


def test_transient_persistent_raises_without_burning_keys() -> None:
    calls: list[str] = []

    def fn(token: str) -> str:
        calls.append(token)
        raise ConnectTimeout("network down")

    rot = _rotator(["k1", "k2"], transient_attempts=3)
    try:
        rot.call(fn)
        raised = None
    except ConnectTimeout as e:             # leci oryginalny błąd, NIE AllKeysExhausted
        raised = e
    assert isinstance(raised, ConnectTimeout)
    assert calls == ["k1", "k1", "k1"]      # k2 NIE tknięty (zwykła awaria sieci)


# ---------------------------------------------------------------------------
# 5) Wszystkie klucze wyczerpane
# ---------------------------------------------------------------------------
def test_all_keys_exhausted() -> None:
    calls: list[str] = []

    def fn(token: str) -> str:
        calls.append(token)
        raise _HTTPError(403, "monthly usage exceeded")

    rot = _rotator(["k1", "k2"])
    try:
        rot.call(fn)
        raised = None
    except AllKeysExhausted as e:
        raised = e
    assert isinstance(raised, AllKeysExhausted)
    assert "2" in str(raised)               # komunikat podaje liczbę próbowanych kluczy
    assert calls == ["k1", "k2"]


def test_empty_pool_raises() -> None:
    try:
        _rotator([]).call(lambda t: "x")
        raised = None
    except AllKeysExhausted as e:
        raised = e
    assert isinstance(raised, AllKeysExhausted)


# ---------------------------------------------------------------------------
# 6) Plik stanu — start od ostatnio działającego klucza
# ---------------------------------------------------------------------------
def test_state_file_remembers_working_key() -> None:
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / ".apify_key_state"

        def fn(token: str) -> str:
            if token in ("k1", "k2"):
                raise _HTTPError(402, "credit exhausted")
            return f"ok:{token}"

        rot = KeyRotator.for_tokens(["k1", "k2", "k3"], state_path=state,
                                    sleep=_noop_sleep, log=_silent)
        assert rot.call(fn) == "ok:k3"
        assert state.read_text().strip() == "2"   # zapisany indeks działającego (k3)

        # Nowy rotator (kolejny przebieg) startuje od k3 — NIE odbija o k1/k2.
        calls: list[str] = []

        def fn2(token: str) -> str:
            calls.append(token)
            return f"ok:{token}"

        rot2 = KeyRotator.for_tokens(["k1", "k2", "k3"], state_path=state,
                                     sleep=_noop_sleep, log=_silent)
        assert rot2.call(fn2) == "ok:k3"
        assert calls == ["k3"]                     # od razu działający klucz


# ---------------------------------------------------------------------------
# Runner bez pytesta: `python3 laweta_radar/tests/test_apify_keys.py`
# ---------------------------------------------------------------------------
def _run_tests() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} testów przeszło")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_tests())


# ---------------------------------------------------------------------------
# 6) Proxy per klucz (laweta_radar/workers/apify_proxy.py) — skutki dla klasyfikacji i rotacji
# ---------------------------------------------------------------------------
class ProxyError(Exception):
    """Jak httpx.ProxyError: nazwa klasy niesie 'proxy' -> błąd TRANSPORTU."""


def test_proxy_error_is_transient_not_a_dead_key() -> None:
    """Padnięte proxy to problem transportu — klucz jest zdrowy i nie wolno go spalić."""
    assert classify_apify_error(ProxyError("tunnel failed")) == "transient"


def test_provider_quota_message_on_a_network_error_does_not_burn_the_key() -> None:
    """Dostawca proxy z wyczerpanym transferem pisze 'quota'/'limit' w komunikacie
    BŁĘDU SIECI. Wyczerpanie kredytu Apify przychodzi ZAWSZE jako odpowiedź HTTP,
    więc taki komunikat nie może oznaczać pustego klucza — inaczej jedna awaria
    proxy po kolei oznaczałaby wszystkie 120 kluczy jako puste."""
    assert classify_apify_error(ProxyError("bandwidth quota exceeded")) == "transient"
    assert classify_apify_error(ConnectTimeout("monthly limit reached")) == "transient"
    # Bez kodu HTTP i bez błędu sieci komunikat NADAL znaczy wyczerpanie klucza.
    assert classify_apify_error(RuntimeError("monthly usage exceeded")) == "exhausted"


def test_dead_proxy_moves_to_the_next_key_when_switches_allowed() -> None:
    """Z proxy per klucz padnięte wyjście psuje JEDEN klucz — następny ma inne."""
    calls: list[str] = []

    def fn(token: str) -> str:
        calls.append(token)
        if token == "k1":
            raise ProxyError("proxy k1 nie odpowiada")
        return f"ok:{token}"

    rot = _rotator(["k1", "k2"], transient_attempts=2, transient_key_switches=2)
    assert rot.call(fn) == "ok:k2"
    assert calls == ["k1", "k1", "k2"]     # 2 próby na k1, potem przeskok


def test_transient_switches_are_capped_and_reraise_the_real_error() -> None:
    """Globalna awaria nie ma przemielać całej puli, a błąd końcowy ma być PRAWDZIWY
    (transport), nie mylącym 'skończył się kredyt'."""
    calls: list[str] = []

    def fn(token: str) -> str:
        calls.append(token)
        raise ProxyError("wszystko leży")

    rot = _rotator(["k1", "k2", "k3", "k4"], transient_attempts=1,
                   transient_key_switches=2)
    try:
        rot.call(fn)
        raised = None
    except ProxyError as e:
        raised = e
    except AllKeysExhausted as e:          # NIE tędy — to byłby fałszywy komunikat
        raised = e
    assert isinstance(raised, ProxyError)
    assert calls == ["k1", "k2", "k3"]     # 1 klucz + 2 dozwolone przeskoki, koniec


def test_default_behaviour_without_proxy_is_unchanged() -> None:
    """Domyślnie (bez proxy) trwały błąd sieci NIE rusza kolejnych kluczy — inaczej
    zwykła awaria łącza kosztowałaby 120 kluczy x ponowienia."""
    calls: list[str] = []

    def fn(token: str) -> str:
        calls.append(token)
        raise ConnectTimeout("network down")

    rot = _rotator(["k1", "k2"], transient_attempts=2)   # transient_key_switches=0
    try:
        rot.call(fn)
        raised = None
    except ConnectTimeout as e:
        raised = e
    assert isinstance(raised, ConnectTimeout)
    assert calls == ["k1", "k1"]
