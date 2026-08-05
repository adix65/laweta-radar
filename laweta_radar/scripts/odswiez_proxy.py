#!/usr/bin/env python3
"""Odśwież darmową pulę proxy z publicznej listy na GitHubie i ZWERYFIKUJ ją.

Pisze plik czytany przez workers/apify_proxy.py przy `APIFY_PROXY_POOL=1`:
    {"updated_at": "<ISO8601>", "proxies": [{"url": "...", "apify_ok": true}, ...]}

PRZECZYTAJ TO, ZANIM WŁĄCZYSZ PULĘ
----------------------------------
docs/APIFY-PROXY.md opisuje pomiar z repo źródłowego: odświeżenie zwracało ZERO
zweryfikowanych adresów z 411 kandydatów, a jeden stary wpis, który został
w pliku, przejmował przez rendezvous hashing komplet kont i zamieniał runy
w timeouty — za które Apify i tak nalicza. Wniosek nie brzmiał „darmowe proxy nie
działa", tylko: **pula, której nikt nie odświeża, jest gorsza niż jej brak**.
Generatora nie przeniesiono właśnie dlatego, że bez niego pula gnije w ciszy.

Ten skrypt jest tą brakującą połową. Nie zmienia oceny darmowych adresów — ma
sprawić, że plik na dysku mówi prawdę o TERAZ, a nie o zeszłym tygodniu.

WERYFIKACJA JEST SEDNEM, NIE DODATKIEM
--------------------------------------
Do pliku trafiają wyłącznie adresy, przez które REALNIE udało się dojść do
api.apify.com. „Proxy żyje" i „proxy dochodzi do Apify" to dwie różne rzeczy:
adres odpowiadający na ping bywa blokowany przez Apify albo wisi na handshake'u,
a w puli zajmowałby miejsce i przydzielone mu konto po prostu by nie działało.

Sprawdzamy TLS-em do api.apify.com i bez żadnego klucza. Poprawny handshake
z certyfikatem Apify dowodzi, że doszliśmy tam naprawdę, a nie do podstawionej
strony — a brak klucza znaczy, że przez cudze proxy nie leci nic wrażliwego.
Odpowiedź 401 jest sukcesem: pytamy o osiągalność, nie o autoryzację.

UŻYCIE
------
    python laweta_radar/scripts/odswiez_proxy.py --sucho     # plan, bez sieci
    python laweta_radar/scripts/odswiez_proxy.py             # pobierz i sprawdź
    python laweta_radar/scripts/odswiez_proxy.py --limit 200 # tylko 200 kandydatów

Włączenie puli po odświeżeniu (laweta_radar/.env):
    APIFY_PROXY_POOL=1
    APIFY_PROXY_REQUIRED=1      # bez działającego proxy NIE wychodź z IP VPS-a

Cron ma sens WYŁĄCZNIE razem z APIFY_PROXY_POOL=1 — i wtedy ma sens duży, bo
plik starszy niż APIFY_PROXY_POOL_MAX_AGE_H (domyślnie 6 h) jest przez workera
zgłaszany jako stary:
    17 */3 * * * cd /home/ubuntu/laweta-radar && ./venv/bin/python \
        laweta_radar/scripts/odswiez_proxy.py >> /var/log/laweta/proxy.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:                               # pakiet widoczny: -m, import pakietowy, testy
    from laweta_radar.scripts._sciezka import dodaj_repo_do_sciezki
except ImportError:                # uruchomienie po ścieżce do pliku
    from _sciezka import dodaj_repo_do_sciezki

dodaj_repo_do_sciezki()

from laweta_radar.workers.apify_proxy import _TRUTHY, _pool_file_path, mask_url  # noqa: E402

# Ścieżkę pliku bierzemy z workera (funkcja prywatna, import świadomy): gdyby
# generator i czytelnik liczyły ją osobno, rozjechałyby się przy pierwszej
# zmianie APIFY_PROXY_POOL_FILE — a objawem byłaby pusta pula obok świeżo
# zapisanego pliku, czyli najgorszy możliwy rodzaj błędu.

# Lista z GitHuba. proxifly/free-proxy-list publikuje `protokół://ip:port`, po
# jednym na linię, i odświeża ją co kilka godzin. Domyślnie bierzemy listę HTTP:
# socks wymaga dodatkowo `pip install "httpx[socks]"`, więc wciągnięcie go tutaj
# dawałoby kandydatów, których i tak nie da się sprawdzić.
ZRODLA = {
    "http": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "socks4": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt",
    "socks5": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
    "all": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
}

# Cel weryfikacji: ten sam host, z którym gada produkcja. Sprawdzanie proxy
# przez api.ipify.org mówiłoby tylko „proxy żyje" — a pytanie brzmi „czy tym
# proxy dojdę do Apify", i to jest inne pytanie, na które bywa inna odpowiedź.
CEL = "https://api.apify.com/v2/acts"

WZORZEC = re.compile(r"^(?P<proto>https?|socks[45])://(?P<host>[^\s:/@]+):(?P<port>\d{1,5})$")


def pobierz_liste(zrodlo: str, timeout: float) -> list[str]:
    """Surowe linie z publicznej listy. Wyjątek = nie ma czego odświeżać."""
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        r = c.get(zrodlo)
        r.raise_for_status()
        return r.text.splitlines()


def kandydaci(linie: list[str], limit: int = 0) -> list[str]:
    """Linie -> lista URL-i proxy. Śmieci pomijamy cicho, plik jest maszynowy.

    Deduplikacja po pełnym URL-u, bo ta sama para host:port potrafi wystąpić
    w liście kilka razy — a sprawdzanie jej wielokrotnie to tylko dłuższy przebieg.
    """
    widziane: set[str] = set()
    out: list[str] = []
    for linia in linie:
        s = linia.strip()
        if not s or s.startswith("#"):
            continue
        m = WZORZEC.match(s)
        if not m or not 1 <= int(m.group("port")) <= 65535:
            continue
        if s in widziane:
            continue
        widziane.add(s)
        out.append(s)
        if limit and len(out) >= limit:
            break
    return out


def sprawdz(url: str, timeout: float) -> tuple[str, bool, str]:
    """(url, czy dochodzi do Apify, powód niepowodzenia)."""
    import httpx

    try:
        with httpx.Client(proxy=url, timeout=timeout) as c:
            r = c.get(CEL)
        # KAŻDA odpowiedź HTTP to sukces: doszła po TLS-ie z ważnym certyfikatem
        # api.apify.com, czyli proxy naprawdę tam sięga. 401 znaczy tylko tyle, że
        # nie podaliśmy klucza — i bardzo dobrze, przez cudze proxy nie ma po co.
        return url, True, f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001 — każdy błąd to po prostu "nie dochodzi"
        return url, False, f"{type(e).__name__}: {str(e)[:80]}"


def zapisz(plik: Path, dzialajace: list[str]) -> None:
    """Zapis ATOMOWY: tmp obok pliku + podmiana nazwy.

    Worker czyta ten plik z crona, w środku naszego przebiegu. Zapis wprost
    zostawiłby okno, w którym plik jest ucięty w połowie — a `_read_pool_file`
    potraktowałby to jak pustą pulę i konta wyszłyby z gołego IP VPS-a.
    """
    dane = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "zrodlo": "laweta_radar/scripts/odswiez_proxy.py",
        "proxies": [{"url": u, "apify_ok": True} for u in dzialajace],
    }
    plik.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(plik.parent), prefix=plik.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dane, f, ensure_ascii=False, indent=2)
        os.replace(tmp, plik)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def pula_wlaczona() -> bool:
    """Czy worker w ogóle sięgnie po ten plik (APIFY_PROXY_POOL)."""
    return (os.environ.get("APIFY_PROXY_POOL") or "").strip().lower() in _TRUTHY


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Odśwież i zweryfikuj darmową pulę proxy dla Apify.",
        epilog="Pula działa tylko przy APIFY_PROXY_POOL=1 — patrz docs/APIFY-PROXY.md.",
    )
    ap.add_argument("--zrodlo", default="", help=f"URL listy (domyślnie: {ZRODLA['http']})")
    ap.add_argument("--protokol", choices=sorted(ZRODLA), default="http",
                    help="która lista proxifly (domyślnie http; socks wymaga httpx[socks])")
    ap.add_argument("--plik", default="", help="gdzie zapisać (domyślnie jak APIFY_PROXY_POOL_FILE)")
    ap.add_argument("--limit", type=int, default=0, help="sprawdź najwyżej N kandydatów (0 = wszystkie)")
    ap.add_argument("--rownolegle", type=int, default=32, help="ile sprawdzeń naraz (domyślnie 32)")
    ap.add_argument("--timeout", type=float, default=8.0, help="limit na jedno sprawdzenie (s)")
    ap.add_argument("--sucho", action="store_true", help="pokaż plan, nie ruszaj sieci ani pliku")
    a = ap.parse_args(argv)

    zrodlo = a.zrodlo or os.environ.get("APIFY_PROXY_POOL_URL", "") or ZRODLA[a.protokol]
    plik = Path(a.plik) if a.plik else _pool_file_path(os.environ)

    print(f"[proxy] Źródło:  {zrodlo}")
    print(f"[proxy] Plik:    {plik}")
    print(f"[proxy] Cel weryfikacji: {CEL}")
    if a.sucho:
        print(f"[proxy] SUCHO — nic nie pobieram i nic nie zapisuję.")
        print(f"[proxy] Sprawdziłbym: {'wszystkie' if not a.limit else a.limit} kandydatów, "
              f"po {a.rownolegle} naraz, timeout {a.timeout}s")
        print(f"[proxy] APIFY_PROXY_POOL="
              f"{'1 (pula WŁĄCZONA)' if pula_wlaczona() else '0 — worker i tak NIE użyje tego pliku'}")
        return 0

    try:
        linie = pobierz_liste(zrodlo, a.timeout)
    except Exception as e:  # noqa: BLE001
        # Nie ruszamy istniejącego pliku: stara pula jest zła, ale pusta jest
        # gorsza, gdy powodem jest chwilowy brak sieci, a nie martwe adresy.
        print(f"[proxy] Nie pobrałem listy ({type(e).__name__}: {e}) — plik zostaje bez zmian.",
              file=sys.stderr)
        return 1

    lista = kandydaci(linie, a.limit)
    print(f"[proxy] Kandydatów: {len(lista)} (z {len(linie)} linii)")
    if not lista:
        print("[proxy] Lista pusta albo w nieznanym formacie — plik zostaje bez zmian.",
              file=sys.stderr)
        return 1

    if any(u.startswith("socks") for u in lista):
        try:
            import socksio  # noqa: F401
        except ImportError:
            print("[proxy] UWAGA: w liście są adresy socks, a brakuje pakietu — "
                  'pip install "httpx[socks]". Bez niego wszystkie polecą jako niedziałające.')

    print(f"[proxy] Sprawdzam po {a.rownolegle} naraz, timeout {a.timeout}s…")
    with ThreadPoolExecutor(max_workers=max(1, a.rownolegle)) as pool:
        wyniki = list(pool.map(lambda u: sprawdz(u, a.timeout), lista))

    dzialajace = [u for u, ok, _ in wyniki if ok]
    zapisz(plik, dzialajace)

    print(f"[proxy] Dochodzi do Apify: {len(dzialajace)} z {len(lista)}")
    for u in dzialajace[:10]:
        print(f"[proxy]   {mask_url(u)}")
    if len(dzialajace) > 10:
        print(f"[proxy]   … i {len(dzialajace) - 10} więcej")

    if not dzialajace:
        # Dokładnie ten wynik dał pomiar z repo źródłowego (0 z 411). Plik zapisany
        # jako pusty jest tu FUNKCJĄ, nie porażką: worker zobaczy pustą pulę, a przy
        # APIFY_PROXY_REQUIRED=1 zakończy czysto zamiast wyjść z gołego IP VPS-a.
        print("[proxy] ZERO działających adresów — pula zapisana jako pusta.", file=sys.stderr)
        print("[proxy] To ten sam wynik, co pomiar opisany w docs/APIFY-PROXY.md.", file=sys.stderr)
        print("[proxy] Przy APIFY_PROXY_REQUIRED=1 workery zakończą czysto zamiast "
              "wyjść z IP VPS-a. Rozważ płatne proxy (APIFY_PROXY_URLS/APIFY_PROXY_URL).",
              file=sys.stderr)
        return 1

    print(f"[proxy] Zapisane: {plik}")

    # Bez tego świeżo zapisana pula i „BRAK proxy" z workera wyglądają na
    # sprzeczność. Nie są nią: worker czyta ten plik WYŁĄCZNIE przy
    # APIFY_PROXY_POOL=1, a odświeżenie samo niczego nie włącza — bo włączenie
    # puli jest decyzją, którą podejmuje się PO zobaczeniu, ile adresów przeżyło.
    if not pula_wlaczona():
        print("[proxy] UWAGA: APIFY_PROXY_POOL nie jest ustawione na 1 — worker NIE użyje")
        print("[proxy]        tego pliku i dalej pokaże 'BRAK proxy'. Żeby włączyć, dopisz")
        print("[proxy]        do laweta_radar/.env:  APIFY_PROXY_POOL=1")
        print("[proxy]        oraz (bezpiecznik):    APIFY_PROXY_REQUIRED=1")
        print("[proxy]        i dodaj odświeżanie do crona — pula bez odświeżania jest")
        print("[proxy]        gorsza niż jej brak (docs/APIFY-PROXY.md).")

    print("[proxy] Sprawdź przypisanie:  python -m laweta_radar.workers.apify_proxy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
