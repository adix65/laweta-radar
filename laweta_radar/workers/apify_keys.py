"""
Reaktywna ROTACJA kluczy Apify — pula wielu darmowych kont jako jeden, praktycznie
nieskończony zapas kredytu.

PROBLEM: jedno konto Apify = ~5 USD darmowego kredytu na miesiąc. Mając wiele kont
chcemy używać ich PO KOLEI: pierwszy klucz aż wyczerpie kredyt, potem automatycznie
następny — bez sprawdzania salda z góry. Podejście REAKTYWNE: próbujemy uruchomienia
i dopiero gdy Apify zwróci błąd wyczerpania / limitu / płatności, uznajemy klucz za
"pusty" i przeskakujemy na kolejny.

KLUCZE Z ENV: APIFY_API_TOKEN1, APIFY_API_TOKEN2, ... (numeracja od 1, DOWOLNIE wiele —
nic nie jest hardcodowane). Czytamy aż do pierwszej BRAKUJĄCEJ zmiennej (numeracja musi
być ciągła); puste (ustawione na '') pomijamy. Dla kompatybilności wstecznej, gdy nie ma
ŻADNEGO numerowanego, bierzemy stary APIFY_API_TOKEN (bez numeru).

NIEZALEŻNE OD METODY WOŁANIA: rotacja owija dowolne `fn(token) -> wynik` — czy w środku
jest httpx, czy apify-client. Błąd klasyfikujemy po kodzie HTTP (z .response.status_code
lub .status_code) i po treści komunikatu, więc działa dla obu sposobów wołania Apify.

UŻYCIE:
    from laweta_radar.workers.apify_keys import KeyRotator, AllKeysExhausted, load_apify_tokens
    tokens  = load_apify_tokens()                 # lista kluczy z env
    rotator = KeyRotator.for_tokens(tokens)       # + plik stanu (start od działającego)
    print(f"Wykryto {rotator.key_count} kluczy Apify")
    items = rotator.call(lambda token: _apify_run_group(url, limit, token))

`call`:
  - aktualnym kluczem próbuje fn(token);
  - błąd PRZEJŚCIOWY (timeout / sieć / proxy / HTTP 5xx / 429) -> kilka ponowień TYM
    SAMYM kluczem z krótkim odstępem (nie marnujemy dobrych kluczy na chwilowej awarii
    sieci ani na zadławionym proxy — patrz laweta_radar/workers/apify_proxy.py);
  - błąd WYCZERPANIA (HTTP 401/402/403 albo komunikat z usage/limit/exceeded/credit/
    quota/payment) -> log, przeskok na następny klucz, ponowienie;
  - gdy WSZYSTKIE klucze wyczerpane -> AllKeysExhausted (czysty, czytelny błąd).

`transient_key_switches` (domyślnie 0 = zachowanie jak dotąd): ile razy TRWAŁY błąd
transportu może przerzucić na następny klucz. Sens ma wyłącznie przy proxy per klucz
(laweta_radar/workers/apify_proxy.py) — tam padnięte proxy psuje JEDEN klucz, a następny ma inne
wyjście, więc warto spróbować. Bez proxy awaria sieci jest globalna i przechodzenie
po 120 kluczach to tylko godzina ponowień, dlatego domyślnie zostaje 0.

PLIK STANU (.apify_key_state w katalogu projektu): zapisujemy indeks ostatnio
DZIAŁAJĄCEGO klucza, żeby następny przebieg zaczynał od niego (a nie odbijał za każdym
razem o wyczerpane klucze z początku listy). To tylko podpowiedź — rotacja sama się
koryguje. Plik jest w .gitignore.

Szybki podgląd, ile kluczy widać ze środowiska (BEZ uruchamiania scrapera):
    python -m laweta_radar.workers.apify_keys
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable, TypeVar

try:
    import httpx  # tylko do rozpoznania błędów sieci/timeout — opcjonalne
except Exception:  # noqa: BLE001 — brak httpx nie może wywalić importu modułu
    httpx = None  # type: ignore[assignment]

T = TypeVar("T")

# Plik stanu obok .env (katalog projektu). Trzyma indeks ostatnio działającego klucza.
_STATE_PATH = Path(__file__).resolve().parent.parent / ".apify_key_state"

# Słowa w komunikacie błędu jednoznacznie znaczące "ten klucz nie zapłaci".
_EXHAUSTION_HINTS = (
    "usage", "limit", "exceeded", "credit", "quota", "payment", "insufficient",
)

# Ile RAZEM prób tym samym kluczem przy błędzie przejściowym i odstęp między nimi.
_TRANSIENT_ATTEMPTS = 3        # 1 pierwsza + 2 ponowienia
_TRANSIENT_DELAY_S = 3.0


class AllKeysExhausted(RuntimeError):
    """Wszystkie klucze Apify wyczerpane (żaden nie ma już darmowego kredytu)."""


class _KeyExhausted(Exception):
    """Sygnał wewnętrzny: bieżący klucz wyczerpany — przeskocz na następny."""


# ---------------------------------------------------------------------------
# Wczytanie listy kluczy ze środowiska
# ---------------------------------------------------------------------------
def load_apify_tokens(env=None) -> list[str]:
    """Zbierz klucze Apify ze środowiska -> lista tokenów (kolejność = priorytet).

    Czyta APIFY_API_TOKEN1, APIFY_API_TOKEN2, ... aż do pierwszej BRAKUJĄCEJ zmiennej
    (numeracja od 1, ciągła). Wartości puste ('') POMIJA, ale nie przerywa serii.
    Gdy nie ma żadnego numerowanego, dla kompatybilności bierze stary APIFY_API_TOKEN.
    Liczba kluczy jest DOWOLNA — nic nie jest hardcodowane.
    """
    env = os.environ if env is None else env
    tokens: list[str] = []
    i = 1
    while True:
        name = f"APIFY_API_TOKEN{i}"
        if name not in env:
            break                       # pierwsza brakująca -> koniec numerowanej serii
        val = (env.get(name) or "").strip()
        if val:
            tokens.append(val)          # puste pomijamy, ale lecimy dalej
        i += 1
    if not tokens:
        legacy = (env.get("APIFY_API_TOKEN") or "").strip()
        if legacy:
            tokens.append(legacy)
    return tokens


# ---------------------------------------------------------------------------
# Klasyfikacja błędu z wołania Apify (niezależnie od biblioteki)
# ---------------------------------------------------------------------------
def _http_status(exc: BaseException) -> int | None:
    """Kod HTTP z wyjątku, niezależnie od biblioteki (httpx / apify-client / inne)."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    if isinstance(code, int):
        return code
    for attr in ("status_code", "status", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and 100 <= v < 600:
            return v
    return None


def _is_network_error(exc: BaseException) -> bool:
    """Czy to błąd sieci/timeout (przejściowy), niezależnie od biblioteki."""
    if httpx is not None:
        # getattr + filtr na typy: odporne na atrapę httpx (testy podmieniają moduł
        # na pusty, bez TimeoutException/TransportError) — wtedy spadamy na nazwę klasy.
        net_types = tuple(
            t for t in (getattr(httpx, "TimeoutException", None),
                        getattr(httpx, "TransportError", None))
            if isinstance(t, type)
        )
        if net_types and isinstance(exc, net_types):
            return True
    name = type(exc).__name__.lower()
    # "proxy": ruch do Apify idzie przez proxy per klucz (laweta_radar/workers/apify_proxy.py) —
    # awaria proxy to problem TRANSPORTU, nie pustego konta.
    return any(w in name for w in ("timeout", "connect", "network", "unavailable", "proxy"))


def classify_apify_error(exc: BaseException) -> str:
    """Zaklasyfikuj wyjątek z wołania Apify: 'exhausted' | 'transient' | 'fatal'.

    - 'exhausted': klucz bez kredytu / limit / wymagana płatność / martwy token
                   -> przeskocz na następny klucz.
    - 'transient': timeout / sieć / HTTP 5xx / 429 -> ponów TYM SAMYM kluczem.
    - 'fatal':     inny błąd klienta (4xx) -> nie ponawiaj i nie pal kluczy; oddaj wyżej.
    """
    status = _http_status(exc)
    # 1) Jednoznaczne kody "ten klucz nie zapłaci".
    if status in (401, 402, 403):
        return "exhausted"
    # 2) Rate limit i błędy serwera Apify — przejściowe. SPRAWDZAMY PRZED słowami w
    #    komunikacie, bo 429 często niesie "rate limit exceeded" (fałszywe 'exhausted').
    if status == 429 or (status is not None and 500 <= status < 600):
        return "transient"
    # 3) Sieć/timeout/proxy — SPRAWDZAMY PRZED słowami w komunikacie. Wyczerpanie
    #    kredytu Apify przychodzi ZAWSZE jako odpowiedź HTTP, nigdy jako błąd
    #    transportu; za to komunikat od dostawcy proxy potrafi nieść "limit"
    #    albo "quota" (wyczerpany transfer proxy) i bez tej kolejności zdrowe
    #    klucze byłyby po kolei oznaczane jako puste — aż do AllKeysExhausted.
    if _is_network_error(exc):
        return "transient"
    # 4) Komunikat o wyczerpaniu/limicie/płatności — łapie też przypadki bez kodu HTTP.
    msg = str(exc).lower()
    if any(w in msg for w in _EXHAUSTION_HINTS):
        return "exhausted"
    # 5) Pozostałe 4xx — błąd trwały, ale NIE wyczerpanie klucza.
    if status is not None and 400 <= status < 500:
        return "fatal"
    # 6) Nieznane — ostrożnie jako przejściowe (lepiej ponowić niż spalić dobry klucz).
    return "transient"


# ---------------------------------------------------------------------------
# Plik stanu — indeks ostatnio działającego klucza
# ---------------------------------------------------------------------------
def _read_state_index(path: Path, key_count: int) -> int:
    """Indeks startowy z pliku stanu (ostatnio działający klucz), zaciśnięty do zakresu."""
    if key_count <= 0:
        return 0
    try:
        idx = int(path.read_text(encoding="utf-8").strip())
    except Exception:  # noqa: BLE001 — brak / uszkodzony plik = zaczynamy od początku
        return 0
    return idx if 0 <= idx < key_count else 0


def _write_state_index(path: Path, idx: int) -> None:
    """Zapisz indeks działającego klucza (best-effort — błąd zapisu nie wywala runu)."""
    try:
        path.write_text(str(idx), encoding="utf-8")
    except Exception:  # noqa: BLE001 — plik stanu to tylko optymalizacja
        pass


def _mask(token: str) -> str:
    """Skrót klucza bezpieczny do logów (nie logujemy pełnego sekretu)."""
    return f"...{token[-4:]}" if len(token) >= 4 else "????"


# ---------------------------------------------------------------------------
# Rotator
# ---------------------------------------------------------------------------
class KeyRotator:
    """Reaktywna rotacja po liście kluczy Apify (patrz docstring modułu)."""

    def __init__(
        self,
        tokens,
        *,
        state_path: Path | None = _STATE_PATH,
        start_index: int = 0,
        transient_attempts: int = _TRANSIENT_ATTEMPTS,
        transient_delay: float = _TRANSIENT_DELAY_S,
        transient_key_switches: int = 0,
        log: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._tokens = list(tokens)
        self._state_path = Path(state_path) if state_path else None
        n = len(self._tokens)
        self._idx = start_index if (n and 0 <= start_index < n) else 0
        self._persisted_idx = self._idx        # nie zapisuj, dopóki klucz się nie zmieni
        self._exhausted: set[int] = set()
        self._transient_attempts = max(1, int(transient_attempts))
        self._transient_delay = float(transient_delay)
        self._transient_key_switches = max(0, int(transient_key_switches))
        self._log = log
        self._sleep = sleep

    @property
    def key_count(self) -> int:
        return len(self._tokens)

    @classmethod
    def for_tokens(cls, tokens, *, state_path: Path | None = _STATE_PATH, **kw) -> "KeyRotator":
        """Rotator dla gotowej listy tokenów; start od indeksu z pliku stanu."""
        tokens = list(tokens)
        start = _read_state_index(Path(state_path), len(tokens)) if state_path else 0
        return cls(tokens, state_path=state_path, start_index=start, **kw)

    @classmethod
    def from_env(cls, *, env=None, state_path: Path | None = _STATE_PATH, **kw) -> "KeyRotator":
        """Rotator zbudowany wprost ze środowiska (APIFY_API_TOKEN1..N / legacy)."""
        return cls.for_tokens(load_apify_tokens(env), state_path=state_path, **kw)

    def _advance(self) -> None:
        if self._tokens:
            self._idx = (self._idx + 1) % len(self._tokens)

    def _persist(self, idx: int) -> None:
        # Zapis tylko, gdy działający klucz ZMIENIŁ się względem ostatnio utrwalonego —
        # mniej operacji na dysku i brak skutków ubocznych, gdy nic nie rotowaliśmy.
        if self._state_path is not None and idx != self._persisted_idx:
            _write_state_index(self._state_path, idx)
            self._persisted_idx = idx

    def _attempt_one_key(self, fn: Callable[[str], T], token: str, human_idx: int) -> T:
        """Jedno użycie klucza: fn(token) z ponowieniami na błędach PRZEJŚCIOWYCH.

        Zwraca wynik fn; rzuca _KeyExhausted (klucz pusty -> rotacja wyżej) albo
        oryginalny wyjątek (trwały błąd nie związany z kredytem -> oddaj do wołającego).
        """
        last_exc: BaseException | None = None
        for attempt in range(1, self._transient_attempts + 1):
            try:
                return fn(token)
            except _KeyExhausted:
                raise
            except BaseException as exc:  # noqa: BLE001 — klasyfikujemy poniżej
                kind = classify_apify_error(exc)
                if kind == "exhausted":
                    raise _KeyExhausted() from exc
                if kind == "fatal":
                    raise
                last_exc = exc           # transient — ponawiamy tym samym kluczem
                if attempt < self._transient_attempts:
                    self._log(
                        f"[apify-keys] klucz #{human_idx} ({_mask(token)}): błąd "
                        f"przejściowy {type(exc).__name__}: {exc} "
                        f"(próba {attempt}/{self._transient_attempts}) — ponawiam za "
                        f"{self._transient_delay:g}s"
                    )
                    self._sleep(self._transient_delay)
        assert last_exc is not None      # pętla zawsze ustawia last_exc przed wyjściem
        raise last_exc

    def call(self, fn: Callable[[str], T]) -> T:
        """Uruchom fn(token) z reaktywną rotacją kluczy; zwróć wynik fn.

        Rzuca AllKeysExhausted, gdy KAŻDY klucz okazał się pusty.
        """
        if not self._tokens:
            raise AllKeysExhausted("Brak skonfigurowanych kluczy Apify (APIFY_API_TOKEN*).")
        n = len(self._tokens)
        last_transient: BaseException | None = None
        switches_left = self._transient_key_switches
        # Co najwyżej n iteracji: odwiedzamy każdy klucz dokładnie raz (sukces wychodzi
        # wcześniej, każda inna ścieżka przesuwa indeks o 1).
        for _ in range(n):
            idx = self._idx
            if idx in self._exhausted:
                self._advance()
                continue
            token = self._tokens[idx]
            try:
                result = self._attempt_one_key(fn, token, idx + 1)
            except _KeyExhausted:
                self._exhausted.add(idx)
                self._log(
                    f"[apify-keys] klucz #{idx + 1} ({_mask(token)}) wyczerpany "
                    f"(brak kredytu / limit) — przełączam na następny"
                )
                self._advance()
                continue
            except BaseException as exc:  # noqa: BLE001 — klasyfikujemy poniżej
                # Domyślnie (transient_key_switches=0) trwały błąd transportu leci
                # wyżej i NIE rusza kolejnych kluczy — przy zwykłej awarii sieci
                # przechodzenie po 120 kluczach to godzina ponowień bez sensu.
                # Z proxy per klucz jest inaczej: awaria transportu bywa ZWIĄZANA
                # Z KLUCZEM (padło JEGO proxy), a następny klucz ma inne wyjście,
                # więc opłaca się spróbować — ale tylko kilka razy, żeby globalna
                # awaria nadal kończyła się szybko i czytelnym błędem.
                if switches_left <= 0 or classify_apify_error(exc) != "transient":
                    raise
                switches_left -= 1
                last_transient = exc
                self._log(
                    f"[apify-keys] klucz #{idx + 1} ({_mask(token)}): trwały błąd "
                    f"transportu {type(exc).__name__}: {exc} — próbuję następnego "
                    f"klucza (inne proxy); pozostało prób: {switches_left}"
                )
                self._advance()
                continue
            self._persist(idx)           # zapamiętaj działający klucz na następny przebieg
            return result
        if last_transient is not None:
            # Nie kłamiemy AllKeysExhausted, gdy realną przyczyną był transport —
            # operator ma zobaczyć błąd proxy/sieci, nie "skończył się kredyt".
            raise last_transient
        raise AllKeysExhausted(
            f"Wszystkie {n} kluczy Apify wyczerpane — żaden nie ma darmowego kredytu. "
            f"Dodaj kolejne APIFY_API_TOKEN{{N}} do .env albo poczekaj na miesięczny reset."
        )


# ---------------------------------------------------------------------------
# Szybki podgląd liczby kluczy ze środowiska (NIE uruchamia scrapera)
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    tokens = load_apify_tokens()
    print(f"Wykryto {len(tokens)} kluczy Apify")
    for i, t in enumerate(tokens, 1):
        print(f"  #{i}: {_mask(t)}")
    if tokens:
        start = _read_state_index(_STATE_PATH, len(tokens))
        print(f"(start od klucza #{start + 1} wg pliku stanu {_STATE_PATH.name})")
    else:
        print("Ustaw APIFY_API_TOKEN1, APIFY_API_TOKEN2, ... w .env "
              "(lub legacy APIFY_API_TOKEN).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
