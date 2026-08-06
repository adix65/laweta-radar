"""Źródła darmowej puli proxy — dane, nie kod.

Trzymane osobno z tego samego powodu co `groups.py`: repozytoria proxy zmieniają
nazwy plików i układ katalogów między wersjami (jeden traci plik `all/data.txt`,
inny przenosi listy krajów do innego folderu, jeszcze inny znika w całości — patrz
`mmpx12` niżej), więc zaszywanie ścieżek w `scripts/odswiez_proxy.py` gwarantuje,
że pierwsza taka zmiana wywala pobieranie w milczeniu. Tutaj literówkę albo martwy
URL widać jednym spojrzeniem, a naprawia się bez dotykania logiki weryfikacji.

STRUKTURA: {nazwa_zrodla: {"protokol": ..., "urls": [url1, url2, ...]}}.

  `urls`     — lista adresów tego źródła (część źródeł, np. kraje proxifly, to
               naturalnie WIELE plików pod jedną nazwą w logu).
  `protokol` — jak dopisać schemat do linii, które go NIE MAJĄ (patrz niżej).
               `None`, gdy plik źródłowy JUŻ niesie pełne URL-e (`http://ip:port`)
               — wtedy nic nie dopisujemy, linia bez schematu jest po prostu
               odrzucana jako śmieć.

DLACZEGO `protokol` W OGÓLE JEST POTRZEBNY. Część list (proxifly) publikuje pełne
URL-e (`http://1.2.3.4:8080`). Inne (TheSpeedX, monosans, jetkai, ShiftyTR,
roosterkid, zloi-user) publikują GOŁE `ip:port` — sam PLIK jest już przefiltrowany
pod jeden protokół (`http.txt` = wszystko w środku to proxy http), więc schemat
trzeba DOPISAĆ na podstawie tego, KTÓRY to plik, a nie treści linii. Kategoria
"https" w tych listach oznacza "obsługuje strony docelowe po HTTPS przez CONNECT",
NIE "proxy nasłuchuje po TLS" — dlatego mapuje się na schemat `http://`, tak samo
jak kategoria "http" (sprawdzone na proxifly: `protocols/https/data.txt` niesie
linie `http://...`, nie `https://...`).

nazwa_zrodla idzie do logu odświeżenia ("proxifly-http: 312 kandydatów, 4 przeszły")
— po tygodniu działania widać stąd, które źródło w ogóle ma sens trzymać.

KAŻDE ŹRÓDŁO JEST NIEZALEŻNE. `scripts/odswiez_proxy.py` pobiera każdy URL osobno
i błąd jednego (404, zmieniona struktura, timeout) NIE przerywa pobierania
pozostałych — pula i tak powstaje z tego, co się udało ściągnąć. Pusta lista
z jednego źródła to log ostrzegawczy, nie powód do przerwania całego odświeżenia.

SPRAWDZONO (data = kiedy ktoś ostatni raz potwierdził realnym zapytaniem, że URL
istnieje i zwraca listę w oczekiwanym formacie). Repo źródłowe bywa nieaktywne
miesiącami i pliki chwilowo znikają — `mmpx12/proxy-list` jest tu przykładem
CELOWO zostawionym w komentarzu: przy weryfikacji 2026-08-06 WSZYSTKIE jego pliki
(`http.txt`, `socks4.txt`, `socks5.txt`, obie gałęzie `main`/`master`) dawały 404
— repo albo zmieniło strukturę w całości, albo przestało istnieć w tej formie.
Wolimy brakujące źródło od źródła, które co odświeżenie krzyczy w logu 404 bez
szansy na naprawę. Gdy ktoś je kiedyś przywróci, dopisanie z powrotem to jedna
linia tutaj, nie grzebanie w skrypcie.
#   "mmpx12": {"protokol": "http", "urls": [
#       "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"]},
"""
from __future__ import annotations

# proxifly/free-proxy-list — odświeża się co ~kilka godzin, ma zarówno listy
# per-protokół, jak i per-kraj, WSZYSTKIE z pełnym schematem w linii (protokol=None).
# Kraje ograniczone do rynków, w których faktycznie działamy (PL i sąsiedzi + kilka
# dużych hostingowych) — adres wyjściowy nie musi wyglądać jak polski użytkownik,
# łączymy się z Apify, nie z Facebookiem, więc kraj serwera jest tu bez znaczenia
# poza tym, że ogranicza rozmiar pobieranej listy.
# Sprawdzono: 2026-08-06 (wszystkie pliki niżej zwracają 200 i pełne URL-e).
PROXIFLY = {
    "proxifly-http": {"protokol": None, "urls": [
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    ]},
    "proxifly-https": {"protokol": None, "urls": [
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt",
    ]},
    "proxifly-socks4": {"protokol": None, "urls": [
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt",
    ]},
    "proxifly-socks5": {"protokol": None, "urls": [
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
    ]},
    "proxifly-all": {"protokol": None, "urls": [
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    ]},
    "proxifly-kraje": {"protokol": None, "urls": [
        f"https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/{kod}/data.txt"
        for kod in ("PL", "DE", "NL", "CZ", "FR", "AT", "GB", "US")
    ]},
}

# TheSpeedX/PROXY-List — jedna lista na protokół, gałąź `master` (nie `main`),
# GOŁE `ip:port` (protokol dopisywany po nazwie pliku).
# Sprawdzono: 2026-08-06.
THESPEEDX = {
    "thespeedx-http": {"protokol": "http", "urls": [
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    ]},
    "thespeedx-socks4": {"protokol": "socks4", "urls": [
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
    ]},
    "thespeedx-socks5": {"protokol": "socks5", "urls": [
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    ]},
}

# monosans/proxy-list — GOŁE `ip:port` per protokół pod `proxies/`, gałąź `main`.
# `proxies/all.txt` (jak proxifly-all) niesie pełny schemat w linii — dosypka bez
# dodatkowego mapowania protokołu.
# UWAGA: repo miało kiedyś `proxies_anonymous/http.txt` (lista wstępnie
# przefiltrowana pod anonimowość) — przy weryfikacji 2026-08-06 ta ścieżka dawała
# 404, README repo mówi wprost, że format się zmienił (teraz `proxies/*.txt` +
# `proxies.json` z metadanymi per proxy, bez osobnej listy "anonimowych"). Wpis
# usunięty, nie zgadywany.
# Sprawdzono: 2026-08-06.
MONOSANS = {
    "monosans-http": {"protokol": "http", "urls": [
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    ]},
    "monosans-socks4": {"protokol": "socks4", "urls": [
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
    ]},
    "monosans-socks5": {"protokol": "socks5", "urls": [
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    ]},
    "monosans-all": {"protokol": None, "urls": [
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt",
    ]},
}

# jetkai/proxy-list — GOŁE `ip:port`, HTTP i "HTTPS" (patrz wyjaśnienie `protokol`
# w nagłówku modułu — obie kategorie dopisują schemat `http://`) pod tą samą ścieżką.
# Sprawdzono: 2026-08-06.
JETKAI = {
    "jetkai-http": {"protokol": "http", "urls": [
        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
    ]},
    "jetkai-https": {"protokol": "http", "urls": [
        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt",
    ]},
}

# Reszta — mniejsze listy, GOŁE `ip:port` (ShiftyTR, roosterkid) albo `ip:port:Kraj`
# (zloi-user — trzecie pole jest ignorowane przy parsowaniu, patrz WZORZEC_GOLY
# w scripts/odswiez_proxy.py). Trzymane osobno od dużej trójki wyżej, żeby log
# odświeżenia pokazywał je jako pojedyncze źródła, a nie zlewał w jedną pozycję.
# Sprawdzono: 2026-08-06.
INNE = {
    "shiftytr-http": {"protokol": "http", "urls": [
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    ]},
    "shiftytr-socks4": {"protokol": "socks4", "urls": [
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt",
    ]},
    "shiftytr-socks5": {"protokol": "socks5", "urls": [
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
    ]},
    "roosterkid-http": {"protokol": "http", "urls": [
        "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    ]},
    "roosterkid-socks4": {"protokol": "socks4", "urls": [
        "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt",
    ]},
    "roosterkid-socks5": {"protokol": "socks5", "urls": [
        "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
    ]},
    "zloi-user-http": {"protokol": "http", "urls": [
        "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
    ]},
    "zloi-user-socks4": {"protokol": "socks4", "urls": [
        "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks4.txt",
    ]},
    "zloi-user-socks5": {"protokol": "socks5", "urls": [
        "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt",
    ]},
}

# Wszystko razem — to jest to, co `scripts/odswiez_proxy.py` faktycznie pobiera
# domyślnie.
ZRODLA: dict[str, dict] = {**PROXIFLY, **THESPEEDX, **MONOSANS, **JETKAI, **INNE}
