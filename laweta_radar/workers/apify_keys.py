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
  - BŁĄD SIECI (timeout / sieć / proxy / HTTP 5xx) -> kilka ponowień TYM SAMYM
    kluczem z krótkim odstępem (nie marnujemy dobrych kluczy na chwilowej awarii
    sieci ani na zadławionym proxy — patrz laweta_radar/workers/apify_proxy.py);
  - RATE LIMIT (HTTP 429) -> odstęp rosnący 5/15/45 s, TEN SAM klucz — to nie jest
    błąd konta, tylko "za szybko", więc zmiana klucza niczego by nie przyspieszyła;
  - KLUCZ MARTWY (HTTP 401/403 albo komunikat "token/user-not-found") -> klucz
    WYPADA NA STAŁE z rotacji (zwykle ban/odwołanie tokenu — nie wróci sam);
  - KREDYT WYCZERPANY (HTTP 402 albo komunikat usage/limit/exceeded/credit/quota/
    payment) -> klucz pomijamy W TYM przebiegu, wróci sam 1. dnia miesiąca;
  - oba stany wyczerpania -> log, przeskok na następny klucz, ponowienie;
  - gdy WSZYSTKIE klucze wyczerpane/martwe -> AllKeysExhausted (czysty, czytelny błąd).

Rozróżnienie martwy/wyczerpany jest CELOWE, nie kosmetyczne: to dwie różne
reakcje operatora. "Wyczerpany kredyt" znaczy "poczekaj do przyszłego miesiąca" —
system sam sobie z tym poradzi. "Martwy klucz" znaczy "konta już nie ma" —
zwykle ban, i BEZ CZŁOWIEKA klucz nigdy nie wróci do puli. Zlepienie tych dwóch
w jeden worek "wyczerpany" (jak było wcześniej) chowa alarm w szumie normalnego
miesięcznego resetu.

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
# UWAGA: to jest tylko PODPOWIEDŹ startowa dla jednego procesu — prawda o tym,
# który klucz jest martwy/wyczerpany, żyje w tabeli `zasoby_apify` (patrz niżej),
# bo to jedyne miejsce widoczne dla RÓWNOLEGŁYCH przebiegów naraz.
_STATE_PATH = Path(__file__).resolve().parent.parent / ".apify_key_state"

# Cztery stany, na jakie rozbijamy dawny worek "wyczerpany" (patrz docstring modułu).
STATUS_AKTYWNY = "aktywny"
STATUS_KREDYT_WYCZERPANY = "kredyt_wyczerpany"    # 402 / limit -> wraca 1. dnia miesiąca
STATUS_KLUCZ_MARTWY = "klucz_martwy"              # 401/403 / token-not-found -> na stałe
STATUS_BLAD_SIECI = "blad_sieci"                  # timeout/sieć/proxy/5xx -> ponów tym kluczem
STATUS_RATE_LIMIT = "rate_limit"                  # 429 -> odczekaj, ten sam klucz
_STATUS_FATAL = "fatal"                           # inny 4xx — nie dotyczy klucza, oddaj wyżej

# Komunikat jednoznacznie mówiący "tokenu/konta nie ma" — SILNIEJSZY sygnał niż
# sam kod HTTP, bo Apify bywa niekonsekwentne w kodach akurat dla tego przypadku
# (patrz historia tego pliku: pięć kont padło naraz z "user-or-token-not-found").
# Świadomie WĄSKIE dopasowania — samo "not found" złapałoby też np. "actor not
# found", co nie ma nic wspólnego ze stanem klucza.
_DEAD_KEY_HINTS = ("user-or-token-not-found", "token-not-found", "user-not-found")

# Słowa w komunikacie błędu jednoznacznie znaczące "ten klucz nie zapłaci".
_EXHAUSTION_HINTS = (
    "usage", "limit", "exceeded", "credit", "quota", "payment", "insufficient",
)

# Odstępy backoffu przy 429 — rosnące, TEN SAM klucz (patrz docstring modułu).
_RATE_LIMIT_BACKOFF_S = (5.0, 15.0, 45.0)

# Ile RAZEM prób tym samym kluczem przy błędzie SIECI i odstęp między nimi.
_TRANSIENT_ATTEMPTS = 3        # 1 pierwsza + 2 ponowienia
_TRANSIENT_DELAY_S = 3.0


class AllKeysExhausted(RuntimeError):
    """Wszystkie klucze Apify wyczerpane albo martwe (żaden nie odpowie teraz)."""


class _KluczMartwy(Exception):
    """Sygnał wewnętrzny: bieżący klucz martwy na stałe — przeskocz, nie wróci sam."""


class _KredytWyczerpany(Exception):
    """Sygnał wewnętrzny: bieżący klucz bez kredytu W TYM miesiącu — przeskocz."""


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
    """Zaklasyfikuj wyjątek z wołania Apify na jeden z pięciu stanów:

        STATUS_KLUCZ_MARTWY        401/403 albo "token/user-not-found" -> na stałe
        STATUS_KREDYT_WYCZERPANY   402 albo usage/limit/exceeded/credit/quota/payment
        STATUS_RATE_LIMIT          429 -> odczekaj, ten sam klucz
        STATUS_BLAD_SIECI          timeout/sieć/proxy/5xx -> ponów tym samym kluczem
        _STATUS_FATAL              inny błąd klienta (4xx), nie dotyczy klucza -> oddaj wyżej

    Kolejność sprawdzeń jest tu tak samo istotna jak w oryginalnym 3-stanowym
    podziale i z tych samych powodów — patrz komentarze przy każdym kroku.
    """
    msg = str(exc).lower()
    # 0) Komunikat JEDNOZNACZNIE mówiący "tokenu/konta nie ma" — sprawdzamy
    #    PRZED kodem HTTP, bo Apify bywa niekonsekwentne w kodzie dla akurat
    #    tego przypadku (patrz nagłówek modułu: 5 kont padło naraz właśnie tak).
    if any(w in msg for w in _DEAD_KEY_HINTS):
        return STATUS_KLUCZ_MARTWY

    status = _http_status(exc)
    # 1) 401 = token unieważniony/nieznany. 403 traktujemy tak samo — to samo
    #    odrzucenie tożsamości, nie przejściowe ograniczenie (to jest 429).
    #    Żaden z nich nie wróci sam — stąd "martwy", nie "wyczerpany".
    if status in (401, 403):
        return STATUS_KLUCZ_MARTWY
    # 2) 402 = wymagana płatność, czyli wyczerpany darmowy kredyt -> wraca
    #    samoistnie 1. dnia kolejnego miesiąca, bez udziału człowieka.
    if status == 402:
        return STATUS_KREDYT_WYCZERPANY
    # 3) Rate limit — SPRAWDZAMY PRZED analizą komunikatu i przed 5xx, bo 429
    #    często niesie "rate limit exceeded" (fałszywe dopasowanie do wyczerpania).
    if status == 429:
        return STATUS_RATE_LIMIT
    # 4) Błędy serwera Apify i sieć/timeout/proxy — SPRAWDZAMY PRZED słowami w
    #    komunikacie. Wyczerpanie kredytu Apify przychodzi ZAWSZE jako odpowiedź
    #    HTTP, nigdy jako błąd transportu; za to komunikat od dostawcy proxy
    #    potrafi nieść "limit" albo "quota" (wyczerpany transfer proxy) i bez tej
    #    kolejności zdrowe klucze byłyby po kolei oznaczane jako martwe/wyczerpane.
    if (status is not None and 500 <= status < 600) or _is_network_error(exc):
        return STATUS_BLAD_SIECI
    # 5) Komunikat o wyczerpaniu/limicie/płatności — łapie też przypadki bez kodu HTTP.
    if any(w in msg for w in _EXHAUSTION_HINTS):
        return STATUS_KREDYT_WYCZERPANY
    # 6) Pozostałe 4xx — błąd trwały, ale NIE dotyczy stanu klucza (np. zły actor).
    if status is not None and 400 <= status < 500:
        return _STATUS_FATAL
    # 7) Nieznane — ostrożnie jako błąd sieci (lepiej ponowić niż spalić dobry klucz).
    return STATUS_BLAD_SIECI


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
        rate_limit_backoff: tuple[float, ...] = _RATE_LIMIT_BACKOFF_S,
        on_wynik: Callable[[str, str, str], None] | None = None,
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
        self._rate_limit_backoff = tuple(rate_limit_backoff)
        # Wołany po KAŻDEJ próbie (token, stan, powód) — stan to jeden z
        # STATUS_* albo STATUS_AKTYWNY przy sukcesie. Rotator sam nie dotyka
        # bazy ani Telegrama (ma zostać czysty i testowalny bez sieci/DB) —
        # to wołający (fb_fetcher) persystuje stan i wysyła alerty.
        self._on_wynik = on_wynik
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

    def _zglos(self, token: str, stan: str, exc: BaseException | None = None) -> None:
        if self._on_wynik is None:
            return
        powod = "" if exc is None else f"{type(exc).__name__}: {str(exc)[:200]}"
        self._on_wynik(token, stan, powod)

    def _attempt_one_key(self, fn: Callable[[str], T], token: str, human_idx: int) -> T:
        """Jedno użycie klucza z ponowieniami na błędach SIECI i RATE LIMIT.

        Zwraca wynik fn. Rzuca `_KluczMartwy`/`_KredytWyczerpany` (rotacja wyżej
        przeskoczy na następny klucz) albo oryginalny wyjątek — błąd sieci po
        wyczerpaniu ponowień, błąd trwały (fatal) od razu.
        """
        siec_prob = 0
        limit_prob = 0
        while True:
            try:
                wynik = fn(token)
            except (_KluczMartwy, _KredytWyczerpany):
                raise
            except BaseException as exc:  # noqa: BLE001 — klasyfikujemy poniżej
                kind = classify_apify_error(exc)
                if kind == STATUS_KLUCZ_MARTWY:
                    self._zglos(token, kind, exc)
                    raise _KluczMartwy(str(exc)) from exc
                if kind == STATUS_KREDYT_WYCZERPANY:
                    self._zglos(token, kind, exc)
                    raise _KredytWyczerpany(str(exc)) from exc
                if kind == _STATUS_FATAL:
                    raise
                if kind == STATUS_RATE_LIMIT:
                    self._zglos(token, kind, exc)
                    if limit_prob >= len(self._rate_limit_backoff):
                        raise
                    delay = self._rate_limit_backoff[limit_prob]
                    limit_prob += 1
                    self._log(
                        f"[apify-keys] klucz #{human_idx} ({_mask(token)}): 429 rate limit "
                        f"— czekam {delay:g}s (próba {limit_prob}/{len(self._rate_limit_backoff)})"
                    )
                    self._sleep(delay)
                    continue
                # kind == STATUS_BLAD_SIECI — ponawiamy TYM SAMYM kluczem.
                self._zglos(token, kind, exc)
                siec_prob += 1
                if siec_prob >= self._transient_attempts:
                    raise
                self._log(
                    f"[apify-keys] klucz #{human_idx} ({_mask(token)}): błąd "
                    f"sieci {type(exc).__name__}: {exc} "
                    f"(próba {siec_prob}/{self._transient_attempts}) — ponawiam za "
                    f"{self._transient_delay:g}s"
                )
                self._sleep(self._transient_delay)
                continue
            else:
                self._zglos(token, STATUS_AKTYWNY)
                return wynik

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
            except _KluczMartwy:
                self._exhausted.add(idx)
                self._log(
                    f"[apify-keys] klucz #{idx + 1} ({_mask(token)}) MARTWY (401/403 "
                    f"— zwykle ban albo odwołany token) — wypada na stałe, "
                    f"przełączam na następny"
                )
                self._advance()
                continue
            except _KredytWyczerpany:
                self._exhausted.add(idx)
                self._log(
                    f"[apify-keys] klucz #{idx + 1} ({_mask(token)}) wyczerpany "
                    f"(brak kredytu / limit) — wraca 1. dnia miesiąca, "
                    f"przełączam na następny"
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
                if switches_left <= 0 or classify_apify_error(exc) != STATUS_BLAD_SIECI:
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
            f"Wszystkie {n} kluczy Apify wyczerpane albo martwe — żaden nie odpowie "
            f"teraz. Dodaj kolejne APIFY_API_TOKEN{{N}} do .env, poczekaj na miesięczny "
            f"reset (kredyt) albo sprawdź konta w panelu Apify (martwe klucze)."
        )


# ---------------------------------------------------------------------------
# Stan w BAZIE (tabela `zasoby_apify`, migracja 0012) — PRZEŻYWA równoległe
# przebiegi, w odróżnieniu od pliku stanu wyżej. Rotator sam tego nie woła
# (ma zostać czysty i testowalny bez DB) — to fb_fetcher.py podpina `on_wynik`
# i persystuje przez funkcje niżej.
# ---------------------------------------------------------------------------
def _hash_klucza(token: str) -> str:
    """Odcisk tokenu do bazy — NIGDY surowa wartość (patrz migracja 0012)."""
    import hashlib  # noqa: PLC0415 — moduł ma zostać lekki przy imporcie z workera

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def zapisz_stan(conn, token: str, status: str, powod: str = "") -> None:
    """Upsert stanu jednego klucza. `status='aktywny'` zeruje licznik błędów —
    to jest sygnał "klucz znowu żyje", a nie kolejne zdarzenie do zliczenia.

    UPSERT, nie INSERT: jeden wiersz na klucz. Gdy status SIĘ NIE ZMIENIA
    względem ostatniego zapisu, `ile_bledow` rośnie (kolejne wystąpienie tego
    samego problemu z rzędu) — inaczej licznik zawsze wynosiłby 1 i nie dałoby
    się odróżnić klucza, który padł raz, od takiego, który pada bez przerwy.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO zasoby_apify (klucz_hash, status, powod, od_kiedy, ile_bledow)
            VALUES (%s, %s, %s, NOW(), %s)
            ON CONFLICT (klucz_hash) DO UPDATE SET
                status       = EXCLUDED.status,
                powod        = EXCLUDED.powod,
                od_kiedy     = CASE WHEN zasoby_apify.status = EXCLUDED.status
                                    THEN zasoby_apify.od_kiedy ELSE NOW() END,
                ile_bledow   = CASE WHEN EXCLUDED.status = %s THEN 0
                                    WHEN zasoby_apify.status = EXCLUDED.status
                                    THEN zasoby_apify.ile_bledow + 1
                                    ELSE 1 END,
                zmieniono_at = NOW()
            """,
            (_hash_klucza(token), status, powod[:500], 0 if status == STATUS_AKTYWNY else 1,
             STATUS_AKTYWNY),
        )
    conn.commit()


def wczytaj_stany(conn, tokens) -> dict[str, dict]:
    """{token: {status, powod, od_kiedy, ile_bledow}} dla tokenów, które MAJĄ wpis.

    Token bez wpisu w tabeli (jeszcze nigdy nie zawiódł) po prostu nie ma tu
    klucza — wołający ma to czytać jako 'aktywny', nie jako brak danych.
    """
    if not tokens:
        return {}
    hash_to_token = {_hash_klucza(t): t for t in tokens}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT klucz_hash, status, powod, od_kiedy, ile_bledow FROM zasoby_apify "
            " WHERE klucz_hash = ANY(%s)",
            (list(hash_to_token),),
        )
        wiersze = cur.fetchall()
    out: dict[str, dict] = {}
    for klucz_hash, status, powod, od_kiedy, ile_bledow in wiersze:
        token = hash_to_token.get(klucz_hash)
        if token is not None:
            out[token] = {"status": status, "powod": powod,
                         "od_kiedy": od_kiedy, "ile_bledow": ile_bledow}
    return out


def klucze_zywe(conn, tokens, *, teraz=None) -> list[str]:
    """Tokeny NADAJĄCE SIĘ do rotacji teraz — bez martwych i bez tegomiesięcznie
    wyczerpanych.

    "Wraca 1. dnia miesiąca" liczymy PORÓWNANIEM miesiąca/roku z `od_kiedy` do
    bieżącego — bez osobnego zadania czyszczącego. Klucz oznaczony wyczerpanym
    31 lipca jest znowu żywy 1 sierpnia, bo to już inny (rok, miesiąc), nawet
    gdy nikt nie zdążył go jeszcze użyć i nadpisać statusu na 'aktywny'.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    teraz = teraz or datetime.now(timezone.utc)
    stany = wczytaj_stany(conn, tokens)
    zywe = []
    for token in tokens:
        wpis = stany.get(token)
        if wpis is None:
            zywe.append(token)
            continue
        if wpis["status"] == STATUS_KLUCZ_MARTWY:
            continue
        if wpis["status"] == STATUS_KREDYT_WYCZERPANY:
            od = wpis["od_kiedy"]
            if od is not None and (od.year, od.month) == (teraz.year, teraz.month):
                continue    # wyczerpany W TYM miesiącu — jeszcze nie wraca
        zywe.append(token)
    return zywe


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
