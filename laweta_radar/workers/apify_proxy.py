"""
Proxy dla ruchu do Apify — żeby ~120 darmowych kont NIE wychodziło z JEDNEGO IP VPS-a.

PROBLEM: pula kont Apify (APIFY_API_TOKEN1..N, rotacja w
laweta_radar/workers/apify_keys.py) to nasze źródło darmowego kredytu na scrapery FB.
Cały ruch do api.apify.com leci jednak z jednego adresu VPS-a — a każde narzędzie,
które dotyka WSZYSTKICH kont naraz (fetcher grup, jakikolwiek przyszły monitor salda),
robi to w kilka sekund. Z punktu widzenia Apify wygląda to dokładnie jak
multi-accounting: dziesiątki kont, jeden adres, skorelowane w czasie. To prosta droga
do ubicia całej puli naraz.

ROZWIĄZANIE: każde konto dostaje SWOJE, STAŁE wyjście do internetu. Ten moduł liczy
"które proxy dla którego tokenu" i oddaje gotowego klienta HTTP. Trzy rzeczy są tu
kluczowe:

  1. LEPKOŚĆ (sticky). Przypisanie token -> proxy jest deterministyczne (hash tokenu),
     więc to samo konto ZAWSZE wychodzi z tego samego IP — także po restarcie, deployu
     i zmianie kolejności kluczy w .env. Konto, które co run loguje się z innego kraju,
     jest podejrzane bardziej niż konto siedzące na jednym adresie.
  2. NIEZALEŻNOŚĆ OD DOSTAWCY. Konfiguracja to zwykłe URL-e proxy, więc działa z czym
     kolwiek (IPRoyal, Decodo/Smartproxy, Oxylabs, Bright Data, własne VPS-y z Squid).
  3. ZERO ZMIANY ZACHOWANIA, GDY NIC NIE SKONFIGUROWANO. Bez zmiennych proxy w .env
     wszystko działa jak dotąd (wyjście wprost z VPS-a), tylko z ostrzeżeniem w logu.

KONFIGURACJA (.env) — trzy sposoby, priorytet od najwyższego:

  1. PER KLUCZ (najbardziej jawne, sensowne przy kilku kontach):
       APIFY_PROXY1=http://user:pass@1.2.3.4:8000     # proxy dla APIFY_API_TOKEN1
       APIFY_PROXY2=http://user:pass@5.6.7.8:8000     # proxy dla APIFY_API_TOKEN2
     Numer odpowiada numerowi tokenu. Klucz bez własnego wpisu spada niżej.

  2. PULA PROXY (typowe przy kilkudziesięciu kontach):
       APIFY_PROXY_URLS=http://u:p@a.example:8000, http://u:p@b.example:8000
     Można podać zakres portów, jeśli dostawca daje sesje lepkie "port = sesja":
       APIFY_PROXY_URLS=http://u:p@gw.example:10001-10100
     (rozwija się do 100 osobnych proxy). Tokeny rozkładają się po puli równomiernie
     i STABILNIE: kolejność wpisów w .env nic nie zmienia, a dołożenie kolejnego
     proxy przenosi na nie tylko ~1/N kont, reszta zostaje na swoich adresach.

  3. BRAMA Z SESJĄ LEPKĄ (najlepsze przy dużej puli kont, jeden wpis na wszystko):
       APIFY_PROXY_URL=http://user-session-{session}:pass@gw.example.com:7000
     Placeholder {session} podmieniamy na STABILNY identyfikator wyliczony z tokenu
     (sha256(token)[:12] — sam token nigdzie nie wycieka). Każde konto = inna sesja
     = inny, ale STAŁY adres wyjściowy. Dokładny format nazwy użytkownika bierzesz
     z panelu swojego dostawcy — my podmieniamy tylko {session}.

  Dodatkowo:
       APIFY_PROXY_REQUIRED=1
     Twardy bezpiecznik: gdy proxy NIE jest skonfigurowane, workery Apify kończą
     czysto zamiast wychodzić z gołego IP VPS-a. Domyślnie 0 (zgodność wstecz).

WERYFIKACJA (to jest ta część, którą naprawdę warto odpalić po konfiguracji):

    python -m laweta_radar.workers.apify_proxy          # co widzi konfiguracja, bez sieci
    python -m laweta_radar.workers.apify_proxy --check  # REALNY adres wyjściowy per konto

`--check` puszcza po jednym zapytaniu na konto przez jego proxy i pokazuje, z jakiego
IP faktycznie wychodzi — plus ostrzega, gdy któreś konto wychodzi z gołego IP VPS-a
albo gdy zbyt wiele kont dzieli jeden adres.

UWAGA: proxy socks5:// wymaga dodatkowej paczki (`pip install "httpx[socks]"`).
Dla http:// / https:// nie trzeba nic instalować.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

# Schematy proxy, które httpx faktycznie przyjmuje (httpx._config.Proxy dopuszcza
# DOKŁADNIE te trzy). Świadomie NIE ma tu socks5h — httpx odrzuca go z
# "Unknown scheme for proxy URL", więc przepuszczenie go tutaj dawałoby zielony
# preflight i wywrotkę dopiero przy pierwszym wołaniu Apify. socks5 wymaga extra:
# pip install "httpx[socks]".
_ALLOWED_SCHEMES = ("http", "https", "socks5")

# APIFY_API_TOKEN{N} (i legacy bez numeru) — potrzebne, żeby zmapować token na numer
# i znaleźć dla niego APIFY_PROXY{N}. Ten sam wzorzec czytania env co w
# laweta_radar/workers/apify_keys.py.
_TOKEN_ENV_RE = re.compile(r"^APIFY_API_TOKEN(\d*)$")
# APIFY_PROXY{N} — proxy przypisane WPROST do klucza o tym numerze.
_PROXY_ENV_RE = re.compile(r"^APIFY_PROXY(\d+)$")

# Zakres portów w URL-u puli: http://u:p@host:10001-10100 -> 100 osobnych proxy.
_PORT_RANGE_RE = re.compile(
    r"^(?P<head>[A-Za-z][A-Za-z0-9+.\-]*://(?:[^/@]*@)?[^/:@\[\]]+)"
    r":(?P<lo>\d{1,5})-(?P<hi>\d{1,5})(?P<tail>/.*)?$"
)
# Sufit rozwijania zakresu — chroni przed literówką w stylu :1-65535.
_MAX_PORT_RANGE = 1000

# Placeholder sesji lepkiej w APIFY_PROXY_URL.
_SESSION_PLACEHOLDER = "{session}"
_SESSION_ID_LEN = 12

# Separatory listy w APIFY_PROXY_URLS: przecinek, średnik, biała spacja, nowa linia.
_LIST_SEP_RE = re.compile(r"[\s,;]+")

_TRUTHY = ("1", "true", "yes", "on", "tak")


class ApifyProxyError(RuntimeError):
    """Błąd konfiguracji proxy Apify (zły URL albo brak proxy przy APIFY_PROXY_REQUIRED)."""


# ---------------------------------------------------------------------------
# Walidacja i normalizacja pojedynczego URL-a
# ---------------------------------------------------------------------------
def _validate_url(url: str, where: str) -> str:
    """Sprawdź URL proxy i oddaj go bez zmian; rzuć ApifyProxyError z czytelnym gdzie/co.

    Świadomie NIE poprawiamy literówek za użytkownika (np. brak schematu) — cicha
    naprawa złego URL-a kończy się ruchem, który leci nie tam, gdzie miał lecieć.
    """
    u = (url or "").strip()
    if not u:
        raise ApifyProxyError(f"{where}: pusty URL proxy")
    parts = urlsplit(u)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ApifyProxyError(
            f"{where}: nieobsługiwany schemat {parts.scheme or '(brak)'!r} w {mask_url(u)} — "
            f"dozwolone: {', '.join(_ALLOWED_SCHEMES)} (np. http://user:haslo@host:port)"
        )
    if not parts.hostname:
        raise ApifyProxyError(f"{where}: brak hosta w {mask_url(u)}")
    try:
        parts.port  # noqa: B018 — samo odczytanie waliduje port (rzuca przy nie-liczbie)
    except ValueError as e:
        raise ApifyProxyError(f"{where}: zły port w {mask_url(u)} ({e})") from e
    return u


def _expand_port_range(url: str) -> list[str]:
    """`http://u:p@host:10001-10100` -> 100 osobnych URL-i; zwykły URL -> [url].

    Dostawcy sesji lepkich często wystawiają jedną bramę i pulę portów, gdzie PORT
    JEST SESJĄ (port 10001 = zawsze ten sam adres wyjściowy). Rozwijanie zakresu
    oszczędza wklejania stu linijek do .env.
    """
    m = _PORT_RANGE_RE.match(url.strip())
    if not m:
        return [url.strip()]
    lo, hi = int(m.group("lo")), int(m.group("hi"))
    if lo > hi:
        raise ApifyProxyError(
            f"APIFY_PROXY_URLS: zakres portów {lo}-{hi} jest odwrócony w {mask_url(url)}"
        )
    if hi - lo + 1 > _MAX_PORT_RANGE:
        raise ApifyProxyError(
            f"APIFY_PROXY_URLS: zakres portów {lo}-{hi} to {hi - lo + 1} proxy — "
            f"limit to {_MAX_PORT_RANGE} (literówka w porcie?)"
        )
    head, tail = m.group("head"), m.group("tail") or ""
    return [f"{head}:{port}{tail}" for port in range(lo, hi + 1)]


def _split_list(raw: str) -> list[str]:
    """Surowa wartość APIFY_PROXY_URLS -> lista URL-i (przecinek/średnik/nowa linia)."""
    return [p for p in _LIST_SEP_RE.split((raw or "").strip()) if p]


# ---------------------------------------------------------------------------
# Maskowanie do logów — URL proxy niesie hasło, nigdy nie logujemy go wprost
# ---------------------------------------------------------------------------
def mask_url(url: str) -> str:
    """URL proxy bezpieczny do wypisania: hasło zastąpione gwiazdkami.

    Login ZOSTAJE — przy sesjach lepkich to w nim siedzi identyfikator sesji i bez
    niego nie da się sprawdzić, czy każde konto dostało inną. Hasło znika zawsze,
    TAKŻE gdy URL jest zepsuty (np. ktoś zapomniał `http://`) — a to najczęstsza
    literówka w .env i akurat jej komunikat błędu wypisuje ten URL do logu.
    """
    u = (url or "").strip()
    if "@" not in u:
        return u
    head, _, rest = u.rpartition("@")
    scheme, sep, userinfo = head.partition("://")
    if not sep:                       # brak schematu — i tak maskujemy, nie oddajemy surowca
        scheme, userinfo = "", head
    if ":" in userinfo:
        user, _, _pwd = userinfo.partition(":")
        userinfo = f"{user}:***"
    return f"{scheme}://{userinfo}@{rest}" if scheme else f"{userinfo}@{rest}"


def proxy_label(url: str | None) -> str:
    """Krótka etykieta proxy do logów workerów: host:port (bez loginu i hasła)."""
    if not url:
        return "bez proxy (IP VPS-a)"
    parts = urlsplit(url)
    host = parts.hostname or "?"
    return f"{host}:{parts.port}" if parts.port else host


# ---------------------------------------------------------------------------
# Konfiguracja
# ---------------------------------------------------------------------------
@dataclass(frozen=True, repr=False)
class ProxyConfig:
    """Odczytana konfiguracja proxy Apify (czysta struktura, zero I/O).

    repr=False jest tu ŚWIADOME i ważne: domyślny repr dataklasy wypisałby hasła do
    proxy ORAZ całą mapę token_index, czyli surowe tokeny Apify wszystkich kont.
    Wystarczyłby jeden print(cfg) albo wyjątek niosący ten obiekt w logu crona, żeby
    wystawić pulę. Własny __repr__ niżej pokazuje tylko liczby."""

    per_key: dict[int, str] = field(default_factory=dict)   # numer klucza -> URL
    pool: tuple[str, ...] = ()                              # APIFY_PROXY_URLS (rozwinięte)
    gateway: str = ""                                       # APIFY_PROXY_URL (z {session})
    required: bool = False                                  # APIFY_PROXY_REQUIRED
    token_index: dict[str, int] = field(default_factory=dict)  # token -> numer klucza
    pool_from_file: int = 0        # ile wpisów puli przyszło z darmowej puli (pliku)
    pool_age_h: float | None = None                         # wiek pliku puli w godzinach
    warnings: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        """Czy JAKIEKOLWIEK proxy jest skonfigurowane."""
        return bool(self.per_key or self.pool or self.gateway)

    @property
    def sticky_per_key(self) -> bool:
        """Czy każde konto może dostać WŁASNY adres wyjściowy (a nie kilka kont na jeden)."""
        return bool(self.gateway and _SESSION_PLACEHOLDER in self.gateway)

    def __repr__(self) -> str:          # bez sekretów — patrz docstring klasy
        return (f"ProxyConfig(per_key={len(self.per_key)}, pool={len(self.pool)}, "
                f"pool_from_file={self.pool_from_file}, "
                f"gateway={'tak' if self.gateway else 'nie'}, "
                f"sticky_per_key={self.sticky_per_key}, required={self.required}, "
                f"tokens_znanych={len(self.token_index)}, "
                f"warnings={len(self.warnings)})")


def _int_env(env, name: str, default: int) -> int:
    """Liczba ze zmiennej środowiskowej; śmieć albo brak -> wartość domyślna."""
    try:
        return int((env.get(name) or "").strip() or default)
    except ValueError:
        return default


def _pool_file_path(env) -> "Path":
    """Ścieżka pliku darmowej puli (APIFY_PROXY_POOL_FILE albo domyślna w projekcie)."""
    from pathlib import Path  # noqa: PLC0415 — tylko tutaj, moduł ma zostać lekki

    raw = (env.get("APIFY_PROXY_POOL_FILE") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent / ".apify_proxy_pool.json"


def _read_pool_file(env) -> tuple[list[str], float | None]:
    """(adresy proxy, wiek pliku w godzinach) z pliku puli; ([], None) gdy brak.

    Plik czytamy WPROST, bez importowania czegokolwiek: w tym repo NIE MA generatora
    darmowej puli (nie przenieśliśmy go — patrz docs/APIFY-PROXY.md), więc plik jest
    wejściem ZEWNĘTRZNYM, wskazywanym przez APIFY_PROXY_POOL_FILE. Brak/uszkodzony plik
    to pusta pula, nigdy wyjątek: workery mają wtedy zachować się jak przy braku proxy.

    Bierzemy tylko wpisy z apify_ok — proxy, które żyje, ale nie dochodzi do Apify,
    zajmowałoby w puli miejsce i przydzielone konto po prostu by nie działało.
    """
    import json  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    try:
        data = json.loads(_pool_file_path(env).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — brak/uszkodzony plik = pusta pula
        return [], None
    if not isinstance(data, dict):
        return [], None
    urls = [r["url"] for r in data.get("proxies", [])
            if isinstance(r, dict) and r.get("url") and r.get("apify_ok")]
    age_h: float | None = None
    raw_when = data.get("updated_at")
    if isinstance(raw_when, str) and raw_when.strip():
        try:
            when = datetime.fromisoformat(raw_when.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - when).total_seconds() / 3600.0
        except ValueError:
            age_h = None
    return urls, age_h


def _pool_file_urls(env) -> list[str]:
    return _read_pool_file(env)[0]


def all_tokens_from_env(env=None) -> list[str]:
    """WSZYSTKIE klucze APIFY_API_TOKEN* ze środowiska, po numerze (legacy na końcu).

    Celowo NIE używamy tu load_apify_tokens() z laweta_radar/workers/apify_keys.py:
    tamten czyta numerację CIĄGLE i urywa na pierwszej dziurze. Weryfikacja proxy ma
    natomiast pokazać KAŻDY klucz leżący w .env, także ten za dziurą — bo dziura w
    numeracji to literówka, którą ktoś kiedyś naprawi, a wtedy klucz nagle zacznie być
    używany. Sprawdzanie tylko kluczy widocznych dla rotatora dawałoby zielone światło
    kontom, które po takiej naprawie wyjdą z gołego IP VPS-a.
    """
    env = os.environ if env is None else env
    found: list[tuple[int, str]] = []
    legacy = ""
    for name, val in env.items():
        m = _TOKEN_ENV_RE.match(name)
        if not m:
            continue
        tok = (val or "").strip()
        if not tok:
            continue
        if m.group(1):
            found.append((int(m.group(1)), tok))
        else:
            legacy = tok
    found.sort(key=lambda p: p[0])
    out = [t for _, t in found]
    if legacy:
        out.append(legacy)
    return out


def _token_index_map(env) -> dict[str, int]:
    """{token: numer z APIFY_API_TOKEN{N}} — do dopasowania APIFY_PROXY{N} po numerze.

    Legacy APIFY_API_TOKEN (bez numeru) nie ma numeru, więc nie trafia do mapy —
    dla niego zadziała pula albo brama, nie przypisanie per klucz.
    """
    out: dict[str, int] = {}
    for name, val in env.items():
        m = _TOKEN_ENV_RE.match(name)
        if not m or not m.group(1):
            continue
        tok = (val or "").strip()
        if tok:
            out[tok] = int(m.group(1))
    return out


def load_proxy_config(env=None) -> ProxyConfig:
    """Zbierz konfigurację proxy ze środowiska. Rzuca ApifyProxyError przy złym URL-u."""
    env = os.environ if env is None else env
    warnings: list[str] = []

    per_key: dict[int, str] = {}
    for name, val in env.items():
        m = _PROXY_ENV_RE.match(name)
        if not m:
            continue
        raw = (val or "").strip()
        if raw:
            per_key[int(m.group(1))] = _validate_url(raw, name)

    pool: list[str] = []
    for raw in _split_list(env.get("APIFY_PROXY_URLS", "")):
        for url in _expand_port_range(raw):
            pool.append(_validate_url(url, "APIFY_PROXY_URLS"))
    # Duplikaty w puli psują rozkład kont po adresach (dwa "różne" wpisy = jedno IP).
    seen: set[str] = set()
    deduped = [u for u in pool if not (u in seen or seen.add(u))]
    if len(deduped) != len(pool):
        warnings.append(
            f"APIFY_PROXY_URLS: {len(pool) - len(deduped)} zduplikowanych wpisów pominięto"
        )
    pool = deduped

    # Pula z pliku dokłada się do APIFY_PROXY_URLS, a nie zastępuje ich: dzięki
    # temu da się trzymać kilka stałych, płatnych adresów i DOSYPYWAĆ tańszymi.
    # Wpisy z pliku walidujemy ŁAGODNIE — zepsuty wpis pomijamy zamiast wywalać
    # cały run, bo plik jest generowany maszynowo, a nie pisany ręcznie.
    #
    # DOMYŚLNIE WYŁĄCZONE (trzeba jawnie ustawić APIFY_PROXY_POOL=1). To wniosek
    # z produkcji repo źródłowego, przeniesiony tu świadomie: gdy plik czytał się
    # sam z dysku, jedno odświeżenie zwracające 0 żywych adresów zostawiało w nim
    # JEDEN stary wpis odpowiadający w ~20% prób — a rendezvous hashing kierował
    # przez ten jeden wpis KOMPLET kont. Scrapery zbierały timeouty na wywołaniach,
    # za które Apify i tak nalicza. Pula, której nikt nie odświeża, jest GORSZA
    # niż jej brak, a plik leżący na dysku wygląda identycznie w obu przypadkach.
    pool_from_file = 0
    pool_age_h: float | None = None
    if (env.get("APIFY_PROXY_POOL") or "").strip().lower() in _TRUTHY:
        file_urls, pool_age_h = _read_pool_file(env)
        for url in file_urls:
            try:
                url = _validate_url(url, "plik puli proxy")
            except ApifyProxyError as e:
                warnings.append(f"pominięto wpis z pliku puli: {e}")
                continue
            if url not in seen:
                seen.add(url)
                pool.append(url)
                pool_from_file += 1

    gateway = (env.get("APIFY_PROXY_URL") or "").strip()
    if gateway:
        gateway = _validate_url(gateway, "APIFY_PROXY_URL")
        # Ostrzegamy tylko, gdy brama FAKTYCZNIE zostanie użyta — przy ustawionej
        # puli i tak jej nie ruszymy, a dwa sprzeczne ostrzeżenia naraz mylą.
        if _SESSION_PLACEHOLDER not in gateway and not pool:
            warnings.append(
                "APIFY_PROXY_URL nie zawiera {session} — WSZYSTKIE konta pójdą przez ten "
                "sam adres wyjściowy, czyli problem 'jednego IP' zostaje (zmienił się "
                "tylko adres). Wstaw {session} w login wg formatu swojego dostawcy."
            )
    if pool and gateway:
        warnings.append(
            "ustawione są naraz APIFY_PROXY_URLS i APIFY_PROXY_URL — używam PULI "
            "(APIFY_PROXY_URLS); bramę zignorowano"
        )
    if len(pool) == 1 and not per_key:
        # Lustro ostrzeżenia o bramie bez {session}: jedno proxy w puli to nadal
        # JEDEN adres dla wszystkich kont, czyli problem zostaje — zmienił się
        # tylko adres. Bez tego jedyna różnica między tymi dwiema (identycznymi
        # w skutkach) pomyłkami była taka, że o jednej mówimy, a o drugiej nie.
        warnings.append(
            "APIFY_PROXY_URLS ma tylko JEDEN adres — wszystkie konta i tak wyjdą "
            "z jednego IP (zmienił się tylko adres). Dołóż więcej proxy albo użyj "
            "bramy APIFY_PROXY_URL z {session}."
        )

    if pool_from_file:
        max_age = _int_env(env, "APIFY_PROXY_POOL_MAX_AGE_H", 6)
        if pool_age_h is None or pool_age_h > max_age:
            # Darmowe proxy gniją w godzinach. Stara pula wygląda w logu tak samo
            # jak świeża, a w praktyce to seria timeoutów — musi być widać.
            warnings.append(
                f"plik puli proxy jest STARY ("
                f"{'nieznany wiek' if pool_age_h is None else f'{pool_age_h:.1f} h'}"
                f", limit {max_age} h) — odśwież go po swojej stronie albo wyłącz "
                f"pulę (APIFY_PROXY_POOL=0); ścieżka: APIFY_PROXY_POOL_FILE"
            )

    return ProxyConfig(
        per_key=per_key,
        pool=tuple(pool),
        gateway=gateway,
        required=(env.get("APIFY_PROXY_REQUIRED") or "").strip().lower() in _TRUTHY,
        token_index=_token_index_map(env),
        pool_from_file=pool_from_file,
        pool_age_h=pool_age_h,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Przypisanie token -> proxy (deterministyczne, czyli LEPKIE)
# ---------------------------------------------------------------------------
def session_id(token: str) -> str:
    """Stabilny identyfikator sesji dla tokenu — sha256(token)[:12].

    Sam token NIE wycieka (hash jednokierunkowy), a identyfikator jest ten sam po
    każdym restarcie, więc dostawca proxy trzyma dla tego konta ten sam adres.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:_SESSION_ID_LEN]


def _proxy_identity(url: str) -> str:
    """TOŻSAMOŚĆ proxy do liczenia przypisania: schemat + host + port + ścieżka.

    Świadomie BEZ loginu i hasła. Gdyby wagę liczyć z całego URL-a, zwykła rotacja
    hasła u dostawcy (operacja rutynowa, ten sam adres wyjściowy!) zmieniałaby wagi
    i przerzucała większość kont na inne IP — czyli tracilibyśmy lepkość przy
    czynności, która z adresami nie ma nic wspólnego.
    """
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    port = f":{p.port}" if p.port else ""
    return f"{p.scheme.lower()}://{host}{port}{p.path}"


def _pool_ranking(token: str, pool) -> list[str]:
    """Cała pula posortowana od NAJLEPSZEGO proxy dla tego tokenu (rendezvous hashing).

    Pierwszy element to dokładnie to, co odda `_pick_from_pool` — reszta to kolejność
    zapasowa, gdy pierwszy wybór nie odpowiada (tanie proxy umierają w godzinach).
    Kolejność jest deterministyczna, więc konto,
    które spadło na proxy zapasowe, będzie tam wracać dopóki pula się nie zmieni —
    lepkość zostaje zachowana także w scenariuszu awaryjnym.
    """
    return sorted(
        pool,
        key=lambda url: hashlib.sha256(
            f"{token}|{_proxy_identity(url)}".encode("utf-8")
        ).hexdigest(),
        reverse=True,
    )


def _pick_from_pool(token: str, pool) -> str:
    """Proxy z puli dla danego tokenu — rendezvous hashing (highest random weight).

    Wybieramy to proxy, dla którego hash(token | tożsamość proxy) jest największy.
    Trzy własności, których NIE daje zwykłe "hash tokenu modulo długość puli":

      • kolejność wpisów w APIFY_PROXY_URLS niczego nie zmienia (przy modulo
        przestawienie linijek w .env przenosiło konta na inne adresy — czyli
        gubiło lepkość dokładnie wtedy, gdy ktoś porządkował plik);
      • dołożenie/usunięcie JEDNEGO proxy przenosi ~1/N kont, a nie prawie
        wszystkie (przy modulo zmiana długości puli przetasowuje cały rozkład);
      • zmiana hasła u dostawcy nie rusza NICZEGO (waga liczona z tożsamości
        proxy, patrz _proxy_identity).

    UWAGA (świadomy kompromis): rozkład jest równomierny STATYSTYCZNIE, nie 1:1.
    Przy puli wielkości równej liczbie kont część adresów obsłuży dwa i więcej kont,
    a część zostanie nieużyta — tak działa rozrzut losowy. Wybieramy to zamiast
    przydziału idealnie równego, bo ten musiałby zależeć od CAŁEJ listy kluczy:
    dołożenie jednego konta przesuwałoby wtedy adresy innym, a stabilność adresu
    per konto jest tu ważniejsza niż wykorzystanie co do jednego IP. Realny rozkład
    pokazuje `python -m laweta_radar.workers.apify_proxy`.
    """
    return max(
        pool,
        key=lambda url: hashlib.sha256(
            f"{token}|{_proxy_identity(url)}".encode("utf-8")
        ).hexdigest(),
    )


def proxy_for_token(token: str, cfg: ProxyConfig | None = None, env=None) -> str | None:
    """URL proxy dla danego tokenu Apify albo None, gdy proxy nie skonfigurowano.

    Priorytet: APIFY_PROXY{N} (jawnie pod numer klucza) -> pula APIFY_PROXY_URLS ->
    brama APIFY_PROXY_URL z podmienionym {session}.
    """
    cfg = load_proxy_config(env) if cfg is None else cfg
    token = (token or "").strip()
    if not token or not cfg.enabled:
        if cfg.required and token:
            raise ApifyProxyError(
                "APIFY_PROXY_REQUIRED=1, ale nie ustawiono ŻADNEGO proxy "
                "(APIFY_PROXY{N} / APIFY_PROXY_URLS / APIFY_PROXY_URL)."
            )
        return None

    idx = cfg.token_index.get(token)
    if idx is not None and idx in cfg.per_key:
        return cfg.per_key[idx]
    if cfg.pool:
        return _pick_from_pool(token, cfg.pool)
    if cfg.gateway:
        return cfg.gateway.replace(_SESSION_PLACEHOLDER, session_id(token))
    # Jedyne dojście tutaj: same APIFY_PROXY{N}, a ten token nie ma swojego numeru.
    if cfg.required:
        raise ApifyProxyError(
            f"APIFY_PROXY_REQUIRED=1, a klucz {_mask_token(token)} nie ma przypisanego "
            f"proxy (brak APIFY_PROXY{{N}} dla jego numeru; dodaj go albo ustaw "
            f"APIFY_PROXY_URLS/APIFY_PROXY_URL jako wspólny fallback)."
        )
    return None


def proxies_for_token(
    token: str, cfg: ProxyConfig | None = None, env=None, limit: int | None = None
) -> list[str]:
    """Proxy dla tokenu w kolejności prób: [pierwszy wybór, zapasowe...].

    Pierwszy element jest ZAWSZE tym samym, co odda `proxy_for_token` — ta funkcja
    tylko dokłada, co spróbować, gdy wybrane proxy nie odpowiada. Ma to sens wyłącznie
    przy PULI: tanie adresy gniją między odświeżeniami puli, więc pojedyncza
    nieudana próba nie znaczy, że konto jest zepsute — znaczy,
    że zepsuty jest jeden adres. Przy proxy przypisanym wprost (APIFY_PROXY{N}) i przy
    bramie z {session} kolejność ma jeden element: tam adres jest wyborem operatora,
    a nie losem z puli, i podmienianie go za jego plecami psułoby lepkość.

    Pusta lista = brak proxy (wyjście wprost). Rzuca ApifyProxyError na tych samych
    zasadach co proxy_for_token (APIFY_PROXY_REQUIRED=1 bez przypisania).
    """
    cfg = load_proxy_config(env) if cfg is None else cfg
    token = (token or "").strip()
    first = proxy_for_token(token, cfg, env)
    if first is None:
        return []
    idx = cfg.token_index.get(token)
    if (idx is not None and idx in cfg.per_key) or not cfg.pool:
        return [first]              # wybór operatora / brama — bez podmieniania
    ranked = _pool_ranking(token, cfg.pool)
    return ranked[:limit] if limit is not None and limit > 0 else ranked


def _mask_token(token: str) -> str:
    """Skrót tokenu do logów — jak _mask w laweta_radar/workers/apify_keys.py."""
    return f"...{token[-4:]}" if len(token) >= 4 else "????"


def assignments(tokens, cfg: ProxyConfig | None = None, env=None) -> list[tuple[str, str | None]]:
    """[(token, url proxy albo None)] dla listy tokenów — podgląd całego przypisania."""
    cfg = load_proxy_config(env) if cfg is None else cfg
    return [(t, proxy_for_token(t, cfg)) for t in tokens]


# ---------------------------------------------------------------------------
# Wejście dla workerów
# ---------------------------------------------------------------------------
def describe(cfg: ProxyConfig | None = None, env=None) -> str:
    """Jedna linia do logu runu: przez co wychodzimy (albo że przez nic)."""
    cfg = load_proxy_config(env) if cfg is None else cfg
    if not cfg.enabled:
        return ("[apify-proxy] BRAK proxy — wszystkie konta Apify wychodzą z jednego IP "
                "VPS-a (ryzyko wspólnego bana puli). Konfiguracja: docs/APIFY-PROXY.md")
    bits: list[str] = []
    if cfg.per_key:
        bits.append(f"{len(cfg.per_key)} przypisanych wprost (APIFY_PROXY{{N}})")
    if cfg.pool:
        skad = ""
        if cfg.pool_from_file:
            wiek = ("wiek nieznany" if cfg.pool_age_h is None
                    else f"odświeżona {cfg.pool_age_h:.1f} h temu")
            z_env = len(cfg.pool) - cfg.pool_from_file
            skad = (f", w tym {cfg.pool_from_file} z darmowej puli ({wiek})"
                    + (f" i {z_env} z .env" if z_env else ""))
        bits.append(f"pula {len(cfg.pool)} proxy (lepkie po hashu klucza){skad}")
    elif cfg.gateway:
        bits.append(
            f"brama {proxy_label(cfg.gateway)}"
            + (" z sesją lepką per klucz" if cfg.sticky_per_key
               else " BEZ {session} — jeden adres dla wszystkich kont")
        )
    return "[apify-proxy] " + ", ".join(bits)


def preflight(env=None, tokens=None) -> tuple[bool, list[str]]:
    """Sprawdzenie na starcie runu: (czy wolno jechać, linie do wypisania).

    `tokens` (lista kluczy tego runu) jest opcjonalna, ale WARTO ją podać: tylko
    mając listę widać CZĘŚCIOWE pokrycie — sytuację, w której proxy jest
    skonfigurowane, ale część kluczy i tak nie dostaje przypisania (np. same
    APIFY_PROXY{N} dla trzech kont przy stu w puli). Bez tego sprawdzenia taki run
    wygląda w logu na zabezpieczony, a większość kont wychodzi z IP VPS-a.

    False oddajemy, gdy APIFY_PROXY_REQUIRED=1 i coś nie gra (brak proxy albo klucze
    bez przypisania) — wtedy worker ma zakończyć czysto zamiast wychodzić z gołego
    adresu. Zły URL proxy zatrzymuje run ZAWSZE, niezależnie od tej flagi: cichy
    fallback na bezpośrednie wyjście byłby dokładnie tym, czego chcemy uniknąć.
    """
    try:
        cfg = load_proxy_config(env)
    except ApifyProxyError as e:
        return False, [f"[apify-proxy] BŁĄD KONFIGURACJI: {e}"]
    lines = [describe(cfg)] + [f"[apify-proxy] UWAGA: {w}" for w in cfg.warnings]
    if cfg.required and not cfg.enabled:
        lines.append("[apify-proxy] APIFY_PROXY_REQUIRED=1 — przerywam, żeby nie wyjść "
                     "z gołego IP VPS-a.")
        return False, lines
    if cfg.enabled and tokens:
        try:
            assigned = [proxy_for_token(t, cfg) for t in tokens]
        except ApifyProxyError as e:      # REQUIRED=1 i klucz bez przypisania
            return False, [*lines, f"[apify-proxy] {e}"]
        without = sum(1 for p in assigned if not p)
        if without:
            lines.append(
                f"[apify-proxy] UWAGA: {without} z {len(tokens)} kluczy NIE ma "
                f"przypisanego proxy — te konta wyjdą z IP VPS-a. Dodaj wspólny "
                f"APIFY_PROXY_URLS/APIFY_PROXY_URL albo APIFY_PROXY_REQUIRED=1."
            )
        # Ile RÓŻNYCH wyjść realnie dostaliśmy. Samo "proxy jest skonfigurowane" nic
        # nie znaczy, jeśli wszystkie konta i tak lądują na jednym adresie — a tak
        # kończy się i pula z jednym wpisem, i brama bez {session}, i komplet
        # APIFY_PROXY{N} wskazujących ten sam host.
        # Rozróżniamy po mask_url (login ZOSTAJE, hasło znika), a NIE po
        # _proxy_identity: przy bramie z sesją lepką różnica siedzi właśnie w
        # loginie ({session}), więc liczenie po samym host:port uznałoby
        # NAJLEPSZĄ konfigurację za "wszyscy na jednym adresie".
        distinct = {mask_url(p) for p in assigned if p}
        if len(distinct) == 1 and len(tokens) > 1:
            lines.append(
                f"[apify-proxy] UWAGA: wszystkie {len(tokens)} kluczy wychodzi przez "
                f"JEDEN adres ({proxy_label(next(p for p in assigned if p))}) — to "
                f"nadal 'jedno IP', tylko inne niż VPS-a."
            )
    return True, lines


def is_enabled(env=None) -> bool:
    """Czy JAKIEKOLWIEK proxy jest skonfigurowane. Zła konfiguracja -> False.

    Do decyzji pobocznych (np. czy rotator kluczy ma przeskakiwać na błędzie
    transportu). Sam błąd konfiguracji zgłasza preflight — tu nie rzucamy.
    """
    try:
        return load_proxy_config(env).enabled
    except ApifyProxyError:
        return False


def client_for_token(token: str, *, timeout=None, env=None, cfg: ProxyConfig | None = None):
    """`httpx.Client` skierowany na proxy właściwe dla tego tokenu (albo bez proxy).

    Import httpx jest LENIWY, żeby sam moduł dał się zaimportować bez tej paczki —
    testy offline podmieniają httpx na atrapę, a czysta logika wyżej ma działać zawsze.
    """
    import httpx  # noqa: PLC0415 — świadomie leniwy import, patrz docstring

    proxy = proxy_for_token(token, cfg, env)
    kwargs = {"timeout": timeout} if timeout is not None else {}
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def client_for_proxy(proxy: str | None, *, timeout=None):
    """`httpx.Client` na KONKRETNYM proxy (albo bez proxy, gdy None).

    Dla wołających, którzy sami sterują kolejnością prób — patrz `proxies_for_token`:
    po nieudanej próbie sięga się po kolejny adres z rankingu tego samego tokenu.
    """
    import httpx  # noqa: PLC0415 — jak w client_for_token

    kwargs = {"timeout": timeout} if timeout is not None else {}
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


# ---------------------------------------------------------------------------
# CLI — podgląd przypisania i REALNA weryfikacja adresów wyjściowych
# ---------------------------------------------------------------------------
_CHECK_URL_DEFAULT = "https://api.ipify.org"
_CHECK_TIMEOUT = 20.0


# =============================================================================
# WERYFIKACJA PROXY — CZTERY TESTY przed dopuszczeniem do puli (docs/APIFY-PROXY.md)
#
# Adres, który "działa" (odpowiada na ping), potrafi być bezużyteczny na trzy
# różne sposoby: jest transparentne (oddaje IP VPS-a — dokładnie problem, po
# który sięgamy po proxy), dubluje inny wpis w puli (dwa "różne" adresy = jedno
# IP wyjściowe, gubi się cel puli) albo nie dochodzi do api.apify.com mimo że
# odpowiada na inne serwisy. TEN OSTATNI test jest sednem: większość darmowych
# list proxy kończy się właśnie tutaj — i to jest powód, dla którego darmowa
# pula (`APIFY_PROXY_POOL`) została w tym repo wyłączona domyślnie.
# =============================================================================
@dataclass(frozen=True)
class WynikWeryfikacji:
    """Wynik czterech testów jednego adresu proxy. `ok` = przeszedł WSZYSTKIE."""

    url: str
    odpowiada: bool            # (a) w ogóle odpowiada
    nietransparentne: bool     # (b) NIE oddaje gołego IP VPS-a
    unikalne: bool             # (c) nie dubluje adresu innego proxy z puli
    dochodzi_do_apify: bool    # (d) CONNECT 443 do api.apify.com się udaje
    blad: str = ""             # pierwszy napotkany powód niepowodzenia

    @property
    def ok(self) -> bool:
        return self.odpowiada and self.nietransparentne and self.unikalne and self.dochodzi_do_apify


# Endpoint do testu (d). BEZ tokenu — pytanie brzmi "czy dojdę", nie "czy mnie
# wpuszczą": 401 od Apify JEST sukcesem (dowodzi poprawnego uścisku TLS z ich
# certyfikatem), błąd połączenia/timeout NIE jest. Ta sama zasada co w
# `scripts/odswiez_proxy.py`, tu zastosowana do KAŻDEGO skonfigurowanego proxy,
# nie tylko do darmowej puli.
_APIFY_PROBE_URL = "https://api.apify.com/v2/users/me"


def _test_odpowiada_i_transparentne(url: str, direct_ip: str | None,
                                    timeout: float) -> tuple[bool, bool, str]:
    """(a) odpowiada, (b) NIE oddaje IP VPS-a — jednym zapytaniem do serwisu IP."""
    import httpx  # noqa: PLC0415

    try:
        with httpx.Client(proxy=url, timeout=timeout) as c:
            r = c.get(_CHECK_URL_DEFAULT)
            r.raise_for_status()
            ip = r.text.strip()
    except Exception as e:  # noqa: BLE001 — każdy błąd = proxy nie odpowiada
        return False, False, f"{type(e).__name__}: {e}"
    if not ip:
        return False, False, "pusta odpowiedź serwisu IP"
    if direct_ip and ip == direct_ip:
        return True, False, f"oddaje IP VPS-a ({ip}) — transparentne, bezużyteczne"
    return True, True, ""


def _test_dochodzi_do_apify(url: str, timeout: float) -> tuple[bool, str]:
    """(d) CONNECT 443 do api.apify.com. KAŻDA odpowiedź HTTP jest sukcesem —
    401 bez tokenu dowodzi poprawnego TLS-handshake'u z prawdziwym Apify."""
    import httpx  # noqa: PLC0415

    try:
        with httpx.Client(proxy=url, timeout=timeout) as c:
            c.get(_APIFY_PROBE_URL)
        return True, ""
    except Exception as e:  # noqa: BLE001 — błąd transportu = nie dochodzi
        return False, f"{type(e).__name__}: {e}"


def weryfikuj_proxy(url: str, *, direct_ip: str | None = None,
                    znane_tozsamosci: set | None = None, timeout: float = 10.0) -> WynikWeryfikacji:
    """Cztery testy dla JEDNEGO adresu. `znane_tozsamosci` — zbiór tożsamości
    (patrz `_proxy_identity`) już zaakceptowanych do puli, do testu (c); brak
    (None) = test (c) zawsze przechodzi (wywołanie ad hoc, poza kontekstem puli).

    Test (d) jest POMIJANY, gdy (a) albo (b) już padły — brak sensu pytać o
    Apify przez adres, który w ogóle nie odpowiada albo jest transparentny
    (oddaje IP VPS-a): taki adres jest bezużyteczny niezależnie od wyniku (d).
    """
    tozsamosc = _proxy_identity(url)
    unikalne = znane_tozsamosci is None or tozsamosc not in znane_tozsamosci
    odpowiada, nietransparentne, blad = _test_odpowiada_i_transparentne(url, direct_ip, timeout)
    if not (odpowiada and nietransparentne):
        return WynikWeryfikacji(url, odpowiada, nietransparentne, unikalne, False, blad)
    dochodzi, blad_d = _test_dochodzi_do_apify(url, timeout)
    return WynikWeryfikacji(url, odpowiada, nietransparentne, unikalne, dochodzi, blad_d)


def zweryfikuj_pule(urls, *, timeout: float = 10.0) -> list[WynikWeryfikacji]:
    """Wszystkie kandydaty naraz — testy (a)/(b)/(d) RÓWNOLEGLE (są niezależne per
    adres), test (c) doliczony PO fakcie w kolejności wejściowej listy (pierwsze
    wystąpienie tożsamości wygrywa jako „unikalne" — bez tego zrównoleglenie
    zrobiłoby z testu (c) loterię zależną od kolejności odpowiedzi sieci).

    `direct_ip` liczymy RAZ dla całej serii, nie per kandydat — jedno zapytanie
    bez proxy, niepotrzebne powtarzać setki razy.
    """
    import httpx  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    try:
        with httpx.Client(timeout=timeout) as c:
            direct_ip = c.get(_CHECK_URL_DEFAULT).text.strip() or None
    except Exception:  # noqa: BLE001 — bez adresu bezpośredniego test (b) jest łagodniejszy
        direct_ip = None

    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(urls)))) as pool:
        czesciowe = list(pool.map(
            lambda u: weryfikuj_proxy(u, direct_ip=direct_ip, timeout=timeout), urls))

    widziane: set[str] = set()
    wyniki: list[WynikWeryfikacji] = []
    for w in czesciowe:
        tozsamosc = _proxy_identity(w.url)
        unikalne = tozsamosc not in widziane
        widziane.add(tozsamosc)
        wyniki.append(w if unikalne == w.unikalne else replace(w, unikalne=unikalne))
    return wyniki


# =============================================================================
# KWARANTANNA — stan proxy w bazie (tabela `zasoby_apify_proxy`, migracja 0012)
#
# Przepinanie TYLKO gdy proxy padnie: klucz dostaje kolejne wolne proxy z
# rankingu (`proxies_for_token`), a stare ląduje tu na 30 minut i po niej
# wraca do WERYFIKACJI (test d), nie prosto do puli — padnięty adres ma
# UDOWODNIĆ, że znowu dochodzi do Apify, zanim ktoś znowu na niego trafi.
# =============================================================================
KWARANTANNA_MIN = 30

_STAN_AKTYWNE = "aktywne"
_STAN_KWARANTANNA = "kwarantanna"


def _hash_proxy(url: str) -> str:
    """Odcisk TOŻSAMOŚCI proxy (bez loginu/hasła — patrz `_proxy_identity`) do
    bazy. NIGDY hasło, NIGDY cały URL: to jest dokładnie ta sama zasada, dla
    której `mask_url`/`proxy_label` istnieją do logów."""
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(_proxy_identity(url).encode("utf-8")).hexdigest()[:24]


def oznacz_kwarantanna(conn, url: str, powod: str, *, klucz_hash: str | None = None,
                       minuty: int = KWARANTANNA_MIN) -> None:
    """Wyślij proxy do kwarantanny na `minuty` — upsert, licznik błędów rośnie."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO zasoby_apify_proxy
                (proxy_hash, etykieta, status, od_kiedy, wraca_o, ile_bledow,
                 przypisany_klucz_hash)
            VALUES (%s, %s, %s, NOW(), NOW() + make_interval(mins => %s), 1, %s)
            ON CONFLICT (proxy_hash) DO UPDATE SET
                etykieta               = EXCLUDED.etykieta,
                status                 = %s,
                od_kiedy               = NOW(),
                wraca_o                = NOW() + make_interval(mins => %s),
                ile_bledow             = zasoby_apify_proxy.ile_bledow + 1,
                przypisany_klucz_hash  = EXCLUDED.przypisany_klucz_hash,
                zmieniono_at           = NOW()
            """,
            (_hash_proxy(url), proxy_label(url), _STAN_KWARANTANNA, minuty, klucz_hash,
             _STAN_KWARANTANNA, minuty),
        )
    conn.commit()
    _ = powod  # powód idzie do logu wołającego (fb_fetcher) — tabela trzyma tylko stan


def oznacz_aktywne(conn, url: str, *, klucz_hash: str | None = None) -> None:
    """Proxy wraca do puli (przeszło ponowną weryfikację po kwarantannie albo
    właśnie zostało przydzielone kluczowi po raz pierwszy)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO zasoby_apify_proxy
                (proxy_hash, etykieta, status, od_kiedy, wraca_o, ile_bledow,
                 przypisany_klucz_hash)
            VALUES (%s, %s, %s, NOW(), NULL, 0, %s)
            ON CONFLICT (proxy_hash) DO UPDATE SET
                etykieta               = EXCLUDED.etykieta,
                status                 = %s,
                od_kiedy               = CASE WHEN zasoby_apify_proxy.status = %s
                                              THEN zasoby_apify_proxy.od_kiedy ELSE NOW() END,
                wraca_o                = NULL,
                ile_bledow             = 0,
                przypisany_klucz_hash  = EXCLUDED.przypisany_klucz_hash,
                zmieniono_at           = NOW()
            """,
            (_hash_proxy(url), proxy_label(url), _STAN_AKTYWNE, klucz_hash,
             _STAN_AKTYWNE, _STAN_AKTYWNE),
        )
    conn.commit()


def wczytaj_stan_proxy(conn, urls) -> dict[str, dict]:
    """{url: {etykieta, status, od_kiedy, wraca_o, ile_bledow}} dla ZNANYCH adresów.

    Adres bez wpisu (jeszcze nigdy nie zawiódł ani nie był przydzielony) po
    prostu nie ma tu klucza — wołający ma to czytać jako 'aktywne'."""
    urls = list(urls)
    if not urls:
        return {}
    hash_to_url = {_hash_proxy(u): u for u in urls}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT proxy_hash, etykieta, status, od_kiedy, wraca_o, ile_bledow "
            " FROM zasoby_apify_proxy WHERE proxy_hash = ANY(%s)",
            (list(hash_to_url),),
        )
        wiersze = cur.fetchall()
    out: dict[str, dict] = {}
    for proxy_hash, etykieta, status, od_kiedy, wraca_o, ile_bledow in wiersze:
        url = hash_to_url.get(proxy_hash)
        if url is not None:
            out[url] = {"etykieta": etykieta, "status": status, "od_kiedy": od_kiedy,
                       "wraca_o": wraca_o, "ile_bledow": ile_bledow}
    return out


def w_kwarantannie(conn, url: str, *, teraz=None) -> bool:
    """Czy `url` jest TERAZ w kwarantannie (a jej koniec jeszcze nie minął).

    Przeterminowana kwarantanna oddaje False CELOWO — worker ma wtedy sięgnąć
    po ten adres i (przez normalny użytek albo `--zweryfikuj-kwarantanne`)
    wrócić do testu (d), a nie ufać mu automatycznie tylko dlatego, że zegar minął.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    teraz = teraz or datetime.now(timezone.utc)
    stan = wczytaj_stan_proxy(conn, [url]).get(url)
    if stan is None or stan["status"] != _STAN_KWARANTANNA:
        return False
    wraca_o = stan["wraca_o"]
    return wraca_o is None or teraz < wraca_o


def proxy_zywy_dla_tokenu(token: str, conn, cfg: ProxyConfig | None = None,
                          env=None) -> str | None:
    """Jak `proxy_for_token`, ale POMIJA adresy aktualnie w kwarantannie — klucz
    dostaje kolejne wolne proxy z rankingu `proxies_for_token`.

    Sensowne WYŁĄCZNIE przy puli: przy proxy przypisanym wprost (APIFY_PROXY{N})
    i przy bramie z {session} adres jest wyborem operatora / nośnikiem lepkości,
    więc `proxies_for_token` i tak oddaje listę jednoelementową — podmienianie
    go za plecami operatora psułoby dokładnie to, po co tam jest.
    """
    cfg = load_proxy_config(env) if cfg is None else cfg
    for kandydat in proxies_for_token(token, cfg, env):
        if not w_kwarantannie(conn, kandydat):
            return kandydat
    return None     # cała ranga w kwarantannie — wołający decyduje (APIFY_PROXY_REQUIRED)


def _exit_ip(token: str | None, cfg: ProxyConfig, url: str, timeout: float) -> tuple[str, str]:
    """(adres wyjściowy, błąd) dla jednego tokenu; token=None -> wyjście bezpośrednie."""
    import httpx  # noqa: PLC0415

    proxy = proxy_for_token(token, cfg) if token else None
    kwargs = {"timeout": timeout}
    if proxy:
        kwargs["proxy"] = proxy
    try:
        with httpx.Client(**kwargs) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.text.strip(), ""
    except Exception as e:  # noqa: BLE001 — każdy błąd to po prostu "nie wiadomo, jaki IP"
        return "", f"{type(e).__name__}: {e}"


def _check(cfg: ProxyConfig, tokens: list[str], url: str, timeout: float) -> int:
    """Odpytaj `url` przez proxy KAŻDEGO tokenu i pokaż realne adresy wyjściowe."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    direct_ip, direct_err = _exit_ip(None, cfg, url, timeout)
    print(f"IP bezpośrednie (bez proxy): {direct_ip or f'(nie ustalono — {direct_err})'}")
    if not direct_ip:
        # Bez adresu bezpośredniego NIE MA jak stwierdzić, że któreś konto wychodzi
        # z gołego IP VPS-a — a to główne pytanie tego narzędzia. Mówimy o tym
        # wprost, żeby "brak ostrzeżeń" nie został wzięty za "wszystko gra".
        print("UWAGA: bez znanego IP bezpośredniego NIE wykryję konta, które omija "
              "proxy. Poniższa lista pokazuje adresy, ale wyciek na IP VPS-a "
              "zweryfikujesz dopiero, gdy uda się ustalić adres bezpośredni "
              "(np. --url na serwis osiągalny bez proxy).")
    print(f"Sprawdzam {len(tokens)} kluczy przez {url} ...\n")

    with ThreadPoolExecutor(max_workers=min(16, max(1, len(tokens)))) as pool:
        results = list(pool.map(lambda t: _exit_ip(t, cfg, url, timeout), tokens))

    by_ip: dict[str, int] = {}
    leaked = failed = 0
    for i, (token, (ip, err)) in enumerate(zip(tokens, results), 1):
        label = proxy_label(proxy_for_token(token, cfg))
        if err:
            failed += 1
            print(f"  #{i:<3} {_mask_token(token)}  {label:<28} BŁĄD  {err}")
            continue
        by_ip[ip] = by_ip.get(ip, 0) + 1
        flag = ""
        if direct_ip and ip == direct_ip:
            leaked += 1
            flag = "  <-- WYCHODZI Z IP VPS-A"
        print(f"  #{i:<3} {_mask_token(token)}  {label:<28} {ip}{flag}")

    print(f"\n=== Podsumowanie: {len(tokens)} kluczy, {len(by_ip)} różnych adresów "
          f"wyjściowych, {failed} błędów ===")
    if by_ip:
        worst_ip, worst_n = max(by_ip.items(), key=lambda kv: kv[1])
        print(f"Najgęstszy adres: {worst_ip} — {worst_n} kont")
    if leaked:
        print(f"UWAGA: {leaked} kont wychodzi z gołego IP VPS-a — proxy ich NIE obsłużyło.")
    if failed:
        print(f"UWAGA: {failed} kluczy nie dało się sprawdzić (złe dane proxy? limit u dostawcy?).")
    if not direct_ip:
        print("WERYFIKACJA NIEPEŁNA: nie ustalono IP bezpośredniego, więc wyciek na "
              "IP VPS-a NIE był sprawdzany.")
    return 1 if (leaked or failed or not direct_ip) else 0


def _main(argv: list[str]) -> int:
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser(
        description="Proxy dla ruchu do Apify: podgląd przypisania kluczy do proxy "
                    "(domyślnie) albo REALNA weryfikacja adresów wyjściowych (--check)."
    )
    ap.add_argument("--check", action="store_true",
                    help="odpytaj serwis IP przez proxy KAŻDEGO klucza i pokaż realne "
                         "adresy wyjściowe (wymaga sieci)")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="sprawdź tylko N pierwszych kluczy (szybki test przy dużej puli)")
    ap.add_argument("--url", default=os.getenv("APIFY_PROXY_CHECK_URL", _CHECK_URL_DEFAULT),
                    help=f"serwis zwracający IP wywołującego (domyślnie {_CHECK_URL_DEFAULT})")
    ap.add_argument("--timeout", type=float, default=_CHECK_TIMEOUT,
                    help=f"timeout pojedynczego sprawdzenia w sekundach (domyślnie {_CHECK_TIMEOUT:g})")
    args = ap.parse_args(argv[1:])

    # .env wczytujemy jak workery (CLI ma widzieć to samo, co cron).
    try:
        from pathlib import Path  # noqa: PLC0415

        from dotenv import load_dotenv  # noqa: PLC0415

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:  # noqa: BLE001 — brak dotenv nie może wywalić podglądu
        pass

    from laweta_radar.workers.apify_keys import load_apify_tokens  # noqa: PLC0415

    try:
        cfg = load_proxy_config()
    except ApifyProxyError as e:
        print(f"BŁĄD KONFIGURACJI: {e}")
        return 2

    # Sprawdzamy WSZYSTKIE klucze z env, nie tylko te widoczne dla rotatora: przy
    # dziurze w numeracji (jest ...TOKEN40 i ...TOKEN42, brakuje 41) rotator urywa
    # na 40 — a dziura to literówka, którą ktoś kiedyś naprawi. Weryfikacja tylko
    # widocznych dawałaby wtedy zielone światło kontom, które po naprawie wyjdą
    # z gołego IP VPS-a.
    tokens_all = all_tokens_from_env()
    hidden = len(tokens_all) - len(load_apify_tokens())
    if hidden > 0:
        print(f"UWAGA: {hidden} kluczy jest niewidocznych dla rotatora (dziura w "
              f"numeracji APIFY_API_TOKEN{{N}}) — sprawdzam je razem z resztą, bo po "
              f"naprawie numeracji zaczną być używane.")

    print(describe(cfg))
    for w in cfg.warnings:
        print(f"UWAGA: {w}")
    # Do WYPISYWANIA wyłączamy APIFY_PROXY_REQUIRED: przy włączonej fladze klucz bez
    # przypisania rzuca wyjątkiem, a to właśnie takie klucze mamy tu pokazać (CLI ma
    # diagnozować konfigurację, nie wywalać się na niej). Sam run workera nadal je
    # zablokuje — tam flaga działa normalnie.
    if cfg.required:
        print("UWAGA: APIFY_PROXY_REQUIRED=1 — klucze bez proxy zatrzymają run workera "
              "(poniżej pokazuję je jako 'BEZ PROXY').")
        cfg = replace(cfg, required=False)

    tokens = tokens_all
    if not tokens:
        print("Brak kluczy APIFY_API_TOKEN* w środowisku — nie ma czego przypisywać.")
        return 0
    if args.limit is not None:
        tokens = tokens[:max(0, args.limit)]

    if not args.check:
        print(f"\nPrzypisanie {len(tokens)} kluczy do proxy (bez sieci):")
        counts: dict[str, int] = {}
        for i, (token, proxy) in enumerate(assignments(tokens, cfg), 1):
            counts[proxy or ""] = counts.get(proxy or "", 0) + 1
            print(f"  #{i:<3} {_mask_token(token)} -> {mask_url(proxy) if proxy else 'BEZ PROXY'}")
        # Kubełek "" to konta BEZ proxy — liczymy go osobno, żeby nie udawał
        # "najgęstszego proxy" (to nie proxy, tylko wyciek na IP VPS-a).
        used = [n for k, n in counts.items() if k]
        print(f"\nRóżnych proxy w użyciu: {len(used)} | kont bez proxy: {counts.get('', 0)}")
        if used:
            print(f"Najwięcej kont na jednym proxy: {max(used)}")
        # Pula rozdziela konta po hashu, więc NIE jest to przydział 1:1: przy puli
        # wielkości równej liczbie kont część adresów obsłuży dwa i więcej kont, a
        # część zostanie nietknięta (zwykłe balls-in-bins). Dla anty-banu to bez
        # znaczenia (2-3 konta na adres to nie sygnał), ale kto płaci za dokładnie
        # tyle adresów, ile ma kont, powinien wiedzieć, czego się spodziewać.
        if cfg.pool and used and max(used) > 1 and len(cfg.pool) >= len(tokens):
            print(f"UWAGA: pula ma {len(cfg.pool)} proxy na {len(tokens)} kont, ale "
                  "rozkład po hashu nie jest 1:1 — dla ŚCISŁEGO jednego adresu na "
                  "konto użyj bramy z {session} albo APIFY_PROXY{N}.")
        print("\nRealne adresy wyjściowe sprawdzisz: "
              "python -m laweta_radar.workers.apify_proxy --check")
        return 0

    return _check(cfg, tokens, args.url, args.timeout)


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
