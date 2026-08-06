"""
Offline test laweta_radar/workers/apify_keys.py (REAKTYWNA rotacja kluczy Apify). Sprawdza:

  1. load_apify_tokens — APIFY_API_TOKEN1..N (pomija puste, kończy na pierwszej luce),
     fallback na legacy APIFY_API_TOKEN, brak kluczy -> pusta lista.
  2. classify_apify_error — PIĘĆ rozłącznych stanów: 401/403/"token-not-found" =
     klucz_martwy, 402/słowa w komunikacie = kredyt_wyczerpany, 429 = rate_limit,
     5xx/sieć = blad_sieci, inne 4xx = fatal.
  3. Rotacja: klucz martwy i klucz bez kredytu OBA przeskakują na następny, ale
     są zgłaszane inaczej (na stałe vs do końca miesiąca) — kolejne wywołanie
     startuje już od działającego (pominięty klucz NIE jest wołany).
  4. Błąd SIECI (timeout) -> ponowienia TYM SAMYM kluczem (bez przeskoku); RATE
     LIMIT (429) -> backoff 5/15/45s, TEN SAM klucz; gdy trwałe — wyjątek leci
     wyżej, a DOBRE klucze nie są marnowane.
  5. WSZYSTKIE klucze wyczerpane/martwe -> AllKeysExhausted (czysty błąd).
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
    STATUS_AKTYWNY,
    STATUS_BLAD_SIECI,
    STATUS_KLUCZ_MARTWY,
    STATUS_KREDYT_WYCZERPANY,
    STATUS_RATE_LIMIT,
    AllKeysExhausted,
    KeyRotator,
    classify_apify_error,
    klucze_zywe,
    load_apify_tokens,
    wczytaj_stany,
    zapisz_stan,
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
    for code in (401, 403):
        assert classify_apify_error(_HTTPError(code)) == STATUS_KLUCZ_MARTWY
    assert classify_apify_error(_HTTPError(402)) == STATUS_KREDYT_WYCZERPANY
    assert classify_apify_error(_HTTPError(429)) == STATUS_RATE_LIMIT
    for code in (500, 502, 503):
        assert classify_apify_error(_HTTPError(code)) == STATUS_BLAD_SIECI
    assert classify_apify_error(_HTTPError(400)) == "fatal"
    assert classify_apify_error(_HTTPError(404)) == "fatal"


def test_classify_message_keywords() -> None:
    # Bez kodu HTTP, ale komunikat jednoznaczny -> kredyt wyczerpany.
    for msg in ("Monthly usage hard limit exceeded",
                "insufficient credit on account", "Payment required", "quota reached"):
        assert classify_apify_error(RuntimeError(msg)) == STATUS_KREDYT_WYCZERPANY
    # 429 z "rate limit exceeded" ma zostać RATE LIMIT, nie wyczerpaniem (kod ma priorytet).
    assert classify_apify_error(_HTTPError(429, "rate limit exceeded")) == STATUS_RATE_LIMIT


def test_classify_dead_key_message_wins_over_status_code() -> None:
    """'user-or-token-not-found' — dokładnie ten błąd, który wywołał to zadanie:
    pięć kont padło naraz z tym komunikatem i zostało błędnie zaklasyfikowanych
    jako 'wyczerpany kredyt' zamiast 'martwy klucz'. Komunikat ma priorytet nad
    kodem HTTP, na wypadek gdyby Apify oddał go pod innym kodem niż 401."""
    assert classify_apify_error(_HTTPError(400, "user-or-token-not-found")) == STATUS_KLUCZ_MARTWY
    assert classify_apify_error(RuntimeError("token-not-found")) == STATUS_KLUCZ_MARTWY


def test_classify_network() -> None:
    assert classify_apify_error(ConnectTimeout("read timed out")) == STATUS_BLAD_SIECI


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
    """Wszystkie klucze MARTWE (403) -> AllKeysExhausted. call() traktuje martwy
    i wyczerpany kredytowo SYMETRYCZNIE na poziomie puli — różnica jest w tym,
    JAK są zgłaszane (patrz test_dead_and_over_credit_keys_are_logged_differently),
    nie w finalnym wyniku, gdy nic w puli nie zostało."""
    calls: list[str] = []

    def fn(token: str) -> str:
        calls.append(token)
        raise _HTTPError(403, "account banned")

    rot = _rotator(["k1", "k2"])
    try:
        rot.call(fn)
        raised = None
    except AllKeysExhausted as e:
        raised = e
    assert isinstance(raised, AllKeysExhausted)
    assert "2" in str(raised)               # komunikat podaje liczbę próbowanych kluczy
    assert calls == ["k1", "k2"]


def test_all_keys_over_credit_also_raises_all_keys_exhausted() -> None:
    """Ta sama pula wyczerpana kredytowo (402) -> ten sam finalny wyjątek."""
    rot = _rotator(["k1", "k2"])
    try:
        rot.call(lambda t: (_ for _ in ()).throw(_HTTPError(402, "usage limit exceeded")))
        raised = None
    except AllKeysExhausted as e:
        raised = e
    assert isinstance(raised, AllKeysExhausted)


def test_dead_and_over_credit_keys_are_logged_differently() -> None:
    """Rozróżnienie ma być WIDOCZNE w logu — inna reakcja operatora dla każdego."""
    logi: list[str] = []

    def fn(token: str) -> str:
        if token == "k1":
            raise _HTTPError(401, "user-or-token-not-found")
        if token == "k2":
            raise _HTTPError(402, "usage limit exceeded")
        return f"ok:{token}"

    rot = KeyRotator(["k1", "k2", "k3"], state_path=None, sleep=_noop_sleep,
                     log=logi.append)
    assert rot.call(fn) == "ok:k3"
    linia_k1 = next(l for l in logi if "#1" in l)
    linia_k2 = next(l for l in logi if "#2" in l)
    assert "MARTWY" in linia_k1
    assert "wraca 1. dnia miesiąca" in linia_k2


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
# 7) Rate limit (429) — backoff rosnący, TEN SAM klucz
# ---------------------------------------------------------------------------
def test_rate_limit_backs_off_same_key_then_succeeds() -> None:
    calls: list[str] = []
    delays: list[float] = []
    state = {"left": 2}

    def fn(token: str) -> str:
        calls.append(token)
        if state["left"] > 0:
            state["left"] -= 1
            raise _HTTPError(429, "rate limit exceeded")
        return "ok"

    rot = KeyRotator(["k1", "k2"], state_path=None, sleep=delays.append, log=_silent)
    assert rot.call(fn) == "ok"
    assert calls == ["k1", "k1", "k1"]        # ten sam klucz, BEZ przeskoku na k2
    assert delays == [5.0, 15.0]              # rosnący backoff z listy prób


def test_rate_limit_exhausted_raises_original_error_not_all_keys_exhausted() -> None:
    def fn(token: str) -> str:
        raise _HTTPError(429, "rate limit exceeded")

    rot = KeyRotator(["k1"], state_path=None, sleep=_noop_sleep, log=_silent)
    try:
        rot.call(fn)
        raised = None
    except _HTTPError as e:
        raised = e
    assert isinstance(raised, _HTTPError)     # NIE AllKeysExhausted — klucz nie jest pusty


# ---------------------------------------------------------------------------
# 8) on_wynik — obserwowalność stanu bez wiązania rotatora z bazą
# ---------------------------------------------------------------------------
def test_on_wynik_reports_every_outcome() -> None:
    zdarzenia: list[tuple[str, str]] = []

    def fn(token: str) -> str:
        if token == "k1":
            raise _HTTPError(401, "user-or-token-not-found")
        return "ok"

    rot = KeyRotator(["k1", "k2"], state_path=None, sleep=_noop_sleep, log=_silent,
                     on_wynik=lambda token, stan, powod: zdarzenia.append((token, stan)))
    assert rot.call(fn) == "ok"
    assert (("k1", STATUS_KLUCZ_MARTWY) in zdarzenia)
    assert (("k2", STATUS_AKTYWNY) in zdarzenia)


def test_on_wynik_not_called_when_omitted() -> None:
    """Domyślnie (bez on_wynik) rotator nie wymaga niczego — czyste API."""
    rot = _rotator(["k1"])
    assert rot.call(lambda t: "ok") == "ok"


# ---------------------------------------------------------------------------
# 9) Stan w bazie (tabela zasoby_apify) — upsert i filtr żywych kluczy
# ---------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, tabela: dict) -> None:
        self._tabela = tabela      # {klucz_hash: (status, powod, od_kiedy, ile_bledow)}
        self._wynik: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql: str, params=None) -> None:
        params = params or ()
        if sql.strip().startswith("INSERT INTO zasoby_apify"):
            klucz_hash, status, powod, ile_bledow_nowy, _status_aktywny = params
            poprzedni = self._tabela.get(klucz_hash)
            if poprzedni is None:
                od_kiedy = "NOW"
                ile = 0 if status == "aktywny" else 1
            else:
                stary_status, _p, stary_od, stary_ile = poprzedni
                od_kiedy = stary_od if stary_status == status else "NOW"
                ile = 0 if status == "aktywny" else (
                    stary_ile + 1 if stary_status == status else 1)
            self._tabela[klucz_hash] = (status, powod, od_kiedy, ile)
        elif sql.strip().startswith("SELECT klucz_hash"):
            (hashes,) = params
            self._wynik = [(h, *self._tabela[h]) for h in hashes if h in self._tabela]

    def fetchall(self):
        return self._wynik


class _FakeConn:
    def __init__(self, tabela: dict | None = None) -> None:
        self._tabela = tabela if tabela is not None else {}
        self.commity = 0

    def cursor(self):
        return _FakeCursor(self._tabela)

    def commit(self):
        self.commity += 1


def test_zapisz_stan_i_wczytaj_stany_roundtrip() -> None:
    conn = _FakeConn()
    zapisz_stan(conn, "tok_a", STATUS_KLUCZ_MARTWY, "401 — klucz nie działa")
    stany = wczytaj_stany(conn, ["tok_a", "tok_b"])
    assert "tok_a" in stany and "tok_b" not in stany     # tok_b nigdy nie zawiódł
    assert stany["tok_a"]["status"] == STATUS_KLUCZ_MARTWY
    assert conn.commity == 1


def test_zapisz_stan_nigdy_nie_trzyma_surowego_tokenu() -> None:
    conn = _FakeConn()
    zapisz_stan(conn, "apify_api_SEKRETNY_TOKEN", STATUS_KREDYT_WYCZERPANY, "402")
    (klucz_hash,) = conn._tabela.keys()
    assert "apify_api_SEKRETNY_TOKEN" not in klucz_hash
    assert len(klucz_hash) == 24


def test_klucze_zywe_odsiewa_martwe_i_tegomiesieczne_wyczerpane() -> None:
    from datetime import datetime, timezone

    conn = _FakeConn()
    zapisz_stan(conn, "martwy", STATUS_KLUCZ_MARTWY, "401")
    zapisz_stan(conn, "wyczerpany", STATUS_KREDYT_WYCZERPANY, "402")
    # Nadpisz od_kiedy na "prawdziwą" datę zamiast atrapy "NOW" — klucze_zywe
    # porównuje rok/miesiąc, więc atrapa cursora musi oddać realny obiekt.
    teraz = datetime(2026, 8, 15, tzinfo=timezone.utc)
    hash_wyczerpany = next(h for h, (s, *_r) in conn._tabela.items()
                          if s == STATUS_KREDYT_WYCZERPANY)
    stary = conn._tabela[hash_wyczerpany]
    conn._tabela[hash_wyczerpany] = (stary[0], stary[1], teraz, stary[3])

    zywe = klucze_zywe(conn, ["martwy", "wyczerpany", "swiezy"], teraz=teraz)
    assert zywe == ["swiezy"]                  # martwy i tegomiesięczny wyczerpany odpadają


def test_klucze_zywe_zwraca_wyczerpany_po_zmianie_miesiaca() -> None:
    from datetime import datetime, timezone

    conn = _FakeConn()
    zapisz_stan(conn, "wyczerpany", STATUS_KREDYT_WYCZERPANY, "402")
    hash_wyczerpany = next(iter(conn._tabela))
    lipiec = datetime(2026, 7, 31, tzinfo=timezone.utc)
    stary = conn._tabela[hash_wyczerpany]
    conn._tabela[hash_wyczerpany] = (stary[0], stary[1], lipiec, stary[3])

    sierpien = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert klucze_zywe(conn, ["wyczerpany"], teraz=sierpien) == ["wyczerpany"]


def test_klucze_zywe_bez_tokenow_bez_zapytania() -> None:
    assert klucze_zywe(_FakeConn(), []) == []


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
    assert classify_apify_error(ProxyError("tunnel failed")) == STATUS_BLAD_SIECI


def test_provider_quota_message_on_a_network_error_does_not_burn_the_key() -> None:
    """Dostawca proxy z wyczerpanym transferem pisze 'quota'/'limit' w komunikacie
    BŁĘDU SIECI. Wyczerpanie kredytu Apify przychodzi ZAWSZE jako odpowiedź HTTP,
    więc taki komunikat nie może oznaczać pustego klucza — inaczej jedna awaria
    proxy po kolei oznaczałaby wszystkie 120 kluczy jako puste."""
    assert classify_apify_error(ProxyError("bandwidth quota exceeded")) == STATUS_BLAD_SIECI
    assert classify_apify_error(ConnectTimeout("monthly limit reached")) == STATUS_BLAD_SIECI
    # Bez kodu HTTP i bez błędu sieci komunikat NADAL znaczy wyczerpanie kredytu.
    assert classify_apify_error(RuntimeError("monthly usage exceeded")) == STATUS_KREDYT_WYCZERPANY


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
