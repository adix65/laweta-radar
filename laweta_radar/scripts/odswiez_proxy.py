#!/usr/bin/env python3
"""Odśwież darmową pulę proxy z WIELU publicznych list na GitHubie i ZWERYFIKUJ ją.

Pisze plik czytany przez workers/apify_proxy.py przy `APIFY_PROXY_POOL=1`:
    {"updated_at": "<ISO8601>", "proxies": [
        {"url": "...", "apify_ok": true, "zrodlo": "proxifly-http",
         "czas_ms": 842, "passy_pod_rzad": 3}, ...
    ]}
Tylko `url`/`apify_ok` są KONTRAKTEM z `workers/apify_proxy._read_pool_file` — reszta
pól to RANKING (patrz sekcja "RANKING" niżej), czytelny dla człowieka i przyszłych
narzędzi, ale nieużywany przez samo przypisanie token->proxy (to nadal czysty
rendezvous hashing, patrz `workers/apify_proxy._pick_from_pool`).

PRZECZYTAJ TO, ZANIM WŁĄCZYSZ PULĘ
----------------------------------
docs/APIFY-PROXY.md opisuje pomiar z repo źródłowego: odświeżenie z JEDNEGO źródła
zwracało ZERO zweryfikowanych adresów z 411 kandydatów, a jeden stary wpis, który
został w pliku, przejmował przez rendezvous hashing komplet kont i zamieniał runy
w timeouty — za które Apify i tak nalicza. Wniosek nie brzmiał „darmowe proxy nie
działa", tylko: **pula, której nikt nie odświeża, jest gorsza niż jej brak** — i
**jedno źródło daje za mało kandydatów, żeby ten pomiar w ogóle miał sens**.

Ten skrypt pobiera z DZIESIĘCIU źródeł naraz (`config/zrodla_proxy.py`), scala je,
odsiewa tanio i dopiero na tym, co zostało, robi drogą, pełną weryfikację. Więcej
źródeł nie znaczy „więcej adresów za wszelką cenę" — znaczy WIĘKSZY WYBÓR, z
którego RANKING (czas odpowiedzi, ile razy z rzędu przeszedł) wybiera te LEPSZE.

SKALA I DLACZEGO WERYFIKACJA JEST ETAPOWA
------------------------------------------
Dziesięć źródeł to rząd 20-50 tysięcy kandydatów po deduplikacji. Pełny test (cztery
zapytania HTTP/TLS na adres, patrz `workers/apify_proxy.weryfikuj_proxy`) na TAKIEJ
liczbie trwałby godziny przy rozsądnej równoległości. Dlatego weryfikacja idzie
w THREE ETAPACH, od najtańszego:

    1. FILTR FORMALNY (bez sieci) — poprawny host:port, odrzucenie adresów
       prywatnych (10.x/192.168.x/127.x i pokrewne), dedup PO HOST:PORT
       niezależnie od protokołu i źródła.
    2. TCP CONNECT (`--rownolegle-tcp`, timeout `--timeout-tcp`, domyślnie 2 s) —
       goły connect, bez TLS i bez HTTP — odsiewa 80-90% martwych adresów za
       ułamek kosztu pełnego testu.
    3. PEŁNE CZTERY TESTY (`--rownolegle-http`, timeout `--timeout`) na tym, co
       przeżyło etap 2 — I TYLKO TU liczy się PRZERWANIE: gdy zbierze się `--cel`
       zaakceptowanych adresów (domyślnie 3x liczba kluczy APIFY_API_TOKEN*), reszta
       kandydatów zostaje NIEPRZETESTOWANA. Sprawdzanie czterdziestu tysięcy
       adresów, żeby użyć czterdziestu, jest czystą stratą czasu.

Kolejność kandydatów jest LOSOWANA przy każdym odświeżeniu (po dedupie, przed
etapem 2) — inaczej zawsze przechodziłyby te same adresy z początku listy źródeł
i pula byłaby ciągle ta sama, mimo dziesięciu źródeł zamiast jednego.

RANKING ZAMIAST GOŁEJ LISTY
----------------------------
Do pliku, obok adresu, trafia: `zrodlo` (które źródło go dało — po tygodniu widać,
które źródła w ogóle mają sens trzymać), `czas_ms` (czas odpowiedzi ostatniego
sprawdzenia) i `passy_pod_rzad` (ile PEŁNYCH odświeżeń z rzędu przeszedł — licznik
dziedziczony z POPRZEDNIEGO pliku po adresie, zerowany, gdy adres wypadnie choć
raz). Plik jest zapisywany posortowany od najlepszego (najdłuższa passa, potem
najkrótszy czas) — sama `workers/apify_proxy.py` nadal liczy przypisanie
rendezvous hashingiem (kolejność w pliku go nie rusza), ale ranking pokazuje
człowiekowi, które adresy są warte zaufania, gdyby kiedyś trzeba było przycinać
pulę ręcznie.

DWA TRYBY — PEŁNE ODŚWIEŻENIE I SZYBKA KONTROLA
-------------------------------------------------
    python laweta_radar/scripts/odswiez_proxy.py               # PEŁNE: pobierz WSZYSTKIE
                                                                 # źródła, zweryfikuj etapowo
    python laweta_radar/scripts/odswiez_proxy.py --tylko-pula   # SZYBKA KONTROLA: sprawdź
                                                                 # TYLKO adresy już w puli,
                                                                 # wyrzuć martwe. Jeśli po
                                                                 # czyszczeniu zostało mniej
                                                                 # niż liczba kluczy, odpala
                                                                 # PEŁNE odświeżenie od razu.
    python laweta_radar/scripts/odswiez_proxy.py --sucho        # plan, bez sieci
    python laweta_radar/scripts/odswiez_proxy.py --limit 2000   # tylko pierwsze 2000 (po
                                                                 # scaleniu i losowaniu)

Włączenie puli po pierwszym udanym odświeżeniu (laweta_radar/.env):
    APIFY_PROXY_POOL=1
    APIFY_PROXY_REQUIRED=1      # bez działającego proxy NIE wychodź z IP VPS-a

CRON — instalowany automatycznie przez `scripts/setup_cron.sh` (dwupoziomowo:
PEŁNE co 2h, SZYBKA KONTROLA co 15 min), patrz ten skrypt po szczegóły wpisów.

Dwa zachowania, które są tu decyzją, a nie szczegółem (bez zmian względem wersji
jednożródłowej):
- **Nieudane pobranie WSZYSTKICH źródeł NIE czyści puli.** Stary plik zostaje
  nietknięty, kod wyjścia 1. Stara pula jest zła, ale pusta jest gorsza, gdy
  powodem jest chwilowy brak sieci, a nie martwe adresy.
- **Zero działających adresów zapisuje pustą pulę**, jawnie, kod wyjścia 1.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

try:                               # pakiet widoczny: -m, import pakietowy, testy
    from laweta_radar.scripts._sciezka import dodaj_repo_do_sciezki
except ImportError:                # uruchomienie po ścieżce do pliku
    from _sciezka import dodaj_repo_do_sciezki

dodaj_repo_do_sciezki()

from laweta_radar.config.zrodla_proxy import ZRODLA  # noqa: E402
from laweta_radar.workers import apify_proxy as ap  # noqa: E402
from laweta_radar.workers.apify_proxy import (  # noqa: E402
    _int_env, _pool_file_path, _TRUTHY, all_tokens_from_env, mask_url,
)

# Cel weryfikacji: ten sam host, z którym gada produkcja. Sprawdzanie proxy
# przez api.ipify.org mówiłoby tylko „proxy żyje" — a pytanie brzmi „czy tym
# proxy dojdę do Apify", i to jest inne pytanie, na które bywa inna odpowiedź.
# (Test „dochodzi do Apify" robi `apify_proxy.weryfikuj_proxy` — etap 3 niżej.)
CEL_WERYFIKACJI = "https://api.apify.com/v2/acts"

WZORZEC = re.compile(r"^(?P<proto>https?|socks[45])://(?P<host>[^\s:/@]+):(?P<port>\d{1,5})$")

# Domyślna równoległość etapów — nadpisywalna z .env, bo optymalna wartość
# zależy od łącza VPS-a (sekcja 1b zadania „większa pula proxy").
_DOMYSLNA_ROWNOLEGLE_TCP = 250
_DOMYSLNA_ROWNOLEGLE_HTTP = 32
_DOMYSLNY_TIMEOUT_TCP = 2.0
_DOMYSLNY_TIMEOUT_HTTP = 8.0
# Ile proxy zebrać, zanim etap 3 się przerwie — domyślnie 3x liczba kluczy.
_DOMYSLNY_MNOZNIK_CELU = 3

_LIST_SEP_RE = re.compile(r"[\s,;]+")


# ---------------------------------------------------------------------------
# Pobieranie — WIELE źródeł, każde osobno, awaria jednego nie przerywa reszty
# ---------------------------------------------------------------------------
def pobierz_liste(zrodlo: str, timeout: float) -> list[str]:
    """Surowe linie z publicznej listy. Wyjątek = to źródło nic nie dało."""
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        r = c.get(zrodlo)
        r.raise_for_status()
        return r.text.splitlines()


def _split_urls(raw: str) -> list[str]:
    """`APIFY_PROXY_POOL_URL` jako LISTA: przecinek / średnik / nowa linia."""
    return [p for p in _LIST_SEP_RE.split((raw or "").strip()) if p]


def pobierz_zrodla(zrodla: dict[str, dict], timeout: float, log=print) -> dict[str, dict]:
    """{nazwa: {"protokol": ..., "linie": [surowe linie]}} — KAŻDY url osobno;
    awaria jednego (404, timeout, zmieniona struktura repo) NIE przerywa
    pobierania pozostałych URL-i ani pozostałych źródeł."""
    wynik: dict[str, dict] = {}
    for nazwa, spec in zrodla.items():
        linie: list[str] = []
        for url in spec.get("urls", []):
            try:
                linie += pobierz_liste(url, timeout)
            except Exception as e:  # noqa: BLE001 — jedno padłe źródło nie ubija reszty
                log(f"[proxy] {nazwa}: nie pobrano {url} "
                    f"({type(e).__name__}: {str(e)[:120]})")
        wynik[nazwa] = {"protokol": spec.get("protokol"), "linie": linie}
    return wynik


# ---------------------------------------------------------------------------
# Etap 1 — filtr formalny (bez sieci): parsowanie, adresy prywatne, dedup host:port
# ---------------------------------------------------------------------------
def _prywatny_host(host: str) -> bool:
    """Czy `host` to prywatny/loopback/link-local adres IP (10.x, 192.168.x,
    127.x i pokrewne RFC 1918/3927/5735). Takiego adresu jako proxy z zewnątrz
    się nie użyje, więc odsiewamy go BEZ SIECI. Hostname (nie goły IP) PRZECHODZI
    — nie da się go ocenić bez DNS, a publiczne listy i tak niosą prawie
    wyłącznie gołe adresy IP."""
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


# Goły `ip:port`, ewentualnie z jednym lub więcej DODATKOWYCH pól po dwukropku
# (np. `ip:port:Country` u zloi-user) — trzecie pole i dalsze to metadana, nie
# część adresu, i jest ignorowane.
_WZORZEC_GOLY = re.compile(r"^(?P<host>[^\s:/@]+):(?P<port>\d{1,5})(?::.*)?$")


def _znormalizuj_linie(s: str, domyslny_protokol: str | None) -> str | None:
    """Linia z listy -> pełny URL proxy (`schemat://host:port`) albo `None` (śmieć).

    Publiczne listy mieszają DWA formaty: pełny URL (proxifly) i goły `ip:port`,
    gdzie sam PLIK ŹRÓDŁOWY jest już przefiltrowany pod jeden protokół (TheSpeedX,
    monosans, jetkai, ShiftyTR, roosterkid, zloi-user — patrz `config/zrodla_proxy.py`,
    sekcja "DLACZEGO protokol"). `domyslny_protokol` to protokół TEGO źródła —
    dopisywany do gołych linii; `None` = źródło ma zawsze nieść pełny schemat,
    goła linia jest wtedy śmieciem (zachowanie sprzed obsługi wielu źródeł).
    """
    m = WZORZEC.match(s)
    if m and 1 <= int(m.group("port")) <= 65535:
        return s
    if domyslny_protokol is None:
        return None
    m = _WZORZEC_GOLY.match(s)
    if not m or not 1 <= int(m.group("port")) <= 65535:
        return None
    return f"{domyslny_protokol}://{m.group('host')}:{m.group('port')}"


def kandydaci(linie: list[str], limit: int = 0, domyslny_protokol: str | None = None) -> list[str]:
    """Linie z JEDNEJ listy -> adresy proxy. Śmieci pomijamy cicho (plik/lista
    jest maszynowa), adresy prywatne odrzucamy, dedup PO HOST:PORT (nie po pełnym
    URL-u) — ten sam host:port podany raz jako `http://`, raz jako `https://`
    w dwóch listach to JEDEN kandydat, nie dwa.

    Zachowana dla zgodności z pojedynczą listą (i z testami sprzed wielu źródeł);
    `scal_kandydatow` niżej robi to samo, ale NA RAZ dla wszystkich źródeł, z
    zapamiętaniem, które źródło dało który adres.
    """
    widziane: set[str] = set()
    out: list[str] = []
    for linia in linie:
        s = linia.strip()
        if not s or s.startswith("#"):
            continue
        url = _znormalizuj_linie(s, domyslny_protokol)
        if url is None:
            continue
        m = WZORZEC.match(url)
        if _prywatny_host(m.group("host")):
            continue
        klucz = f"{m.group('host')}:{m.group('port')}"
        if klucz in widziane:
            continue
        widziane.add(klucz)
        out.append(url)
        if limit and len(out) >= limit:
            break
    return out


def scal_kandydatow(
    surowe_per_zrodlo: dict[str, dict],
) -> tuple[list[str], dict[str, str], dict[str, int]]:
    """Wszystkie źródła NARAZ (wyjście `pobierz_zrodla`) -> (kandydaci_po_dedupie,
    {url: zrodlo}, {zrodlo: ile_surowych_kandydatow_dalo}).

    Dedup PO HOST:PORT, niezależnie od protokołu I ŹRÓDŁA — pierwsze wystąpienie
    wygrywa. Kolejność źródeł (czyli kto wygrywa remis) jest nieistotna: i tak
    losujemy kolejność kandydatów PRZED weryfikacją (`main`), więc które źródło
    „dostało" dany host:port nie wpływa na to, co finalnie trafia do puli.
    """
    zrodlo_dla: dict[str, str] = {}
    widziane: set[str] = set()
    wynik: list[str] = []
    ile_surowych: dict[str, int] = {}
    for nazwa, dane in surowe_per_zrodlo.items():
        protokol = dane.get("protokol")
        tej_zrodlo = 0
        for linia in dane.get("linie", []):
            s = linia.strip()
            if not s or s.startswith("#"):
                continue
            url = _znormalizuj_linie(s, protokol)
            if url is None:
                continue
            m = WZORZEC.match(url)
            if _prywatny_host(m.group("host")):
                continue
            tej_zrodlo += 1
            klucz = f"{m.group('host')}:{m.group('port')}"
            if klucz in widziane:
                continue
            widziane.add(klucz)
            wynik.append(url)
            zrodlo_dla[url] = nazwa
        ile_surowych[nazwa] = tej_zrodlo
    return wynik, zrodlo_dla, ile_surowych


# ---------------------------------------------------------------------------
# Etap 2 — TCP connect, tani odsiew większości martwych adresów
# ---------------------------------------------------------------------------
def etap_tcp(kandydaci_lista: list[str], *, timeout: float, rownolegle: int,
            log=print) -> list[str]:
    """Goły `socket.create_connection`, bez TLS i bez HTTP — odsiewa 80-90%
    martwych adresów za ułamek kosztu etapu 3 (cztery pełne testy HTTP/TLS)."""
    import socket
    from urllib.parse import urlsplit

    if not kandydaci_lista:
        return []

    def _zyje(u: str) -> bool:
        p = urlsplit(u)
        if not p.hostname or not p.port:
            return False
        try:
            with socket.create_connection((p.hostname, p.port), timeout=timeout):
                return True
        except OSError:
            return False

    with ThreadPoolExecutor(max_workers=max(1, rownolegle)) as pool:
        zywe = list(pool.map(_zyje, kandydaci_lista))
    wynik = [u for u, ok in zip(kandydaci_lista, zywe) if ok]
    log(f"[proxy] etap 2 (TCP connect, timeout {timeout:g}s): {len(wynik)} żywych "
        f"z {len(kandydaci_lista)} kandydatów")
    return wynik


# ---------------------------------------------------------------------------
# Etap 3 — cztery pełne testy (apify_proxy.weryfikuj_proxy), z PRZERWANIEM
# ---------------------------------------------------------------------------
def etap_pelny(kandydaci_lista: list[str], *, cel: int, timeout: float, rownolegle: int,
               direct_ip: str | None, zrodlo_dla: dict[str, str],
               log=print) -> tuple[list[dict], int]:
    """Etap 3 w PARTIACH po `rownolegle` — PRZERYWA, gdy uzbiera `cel`
    zaakceptowanych adresów, zamiast sprawdzać resztę. `cel <= 0` = sprawdź
    WSZYSTKO bez limitu (tryb `--tylko-pula`, gdzie kandydatów jest garstka
    i chodzi o PRAWDZIWY stan całej puli, nie o zebranie N pierwszych).

    Oddaje ([{"url", "czas_ms", "zrodlo"}, ...], ile_faktycznie_sprawdzono) — druga
    wartość jest MNIEJSZA niż `len(kandydaci_lista)`, gdy przerwaliśmy wcześniej.
    """
    zaakceptowane: list[dict] = []
    tozsamosci: set[str] = set()
    sprawdzono = 0
    i = 0
    n = len(kandydaci_lista)
    while i < n and (cel <= 0 or len(zaakceptowane) < cel):
        partia = kandydaci_lista[i:i + max(1, rownolegle)]
        i += len(partia)
        sprawdzono += len(partia)

        def _sprawdz(u: str) -> tuple:
            start = monotonic()
            w = ap.weryfikuj_proxy(u, direct_ip=direct_ip, timeout=timeout)
            return w, int((monotonic() - start) * 1000)

        with ThreadPoolExecutor(max_workers=max(1, min(rownolegle, len(partia)))) as pool:
            wyniki = list(pool.map(_sprawdz, partia))

        for w, czas_ms in wyniki:
            if not w.ok:
                continue
            tozsamosc = ap._proxy_identity(w.url)
            if tozsamosc in tozsamosci:
                continue        # dubluje adres już zaakceptowany w tej samej serii
            tozsamosci.add(tozsamosc)
            zaakceptowane.append({"url": w.url, "czas_ms": czas_ms,
                                  "zrodlo": zrodlo_dla.get(w.url, "?")})
            if cel > 0 and len(zaakceptowane) >= cel:
                break

    if cel > 0 and sprawdzono < n:
        log(f"[proxy] PRZERWANO etap 3 po zebraniu {cel}: sprawdzono {sprawdzono} "
            f"z {n} kandydatów, reszta NIEPRZETESTOWANA — to jest funkcja, nie błąd "
            f"(nie ma sensu sprawdzać {n} adresów, żeby użyć {cel}).")
    return zaakceptowane, sprawdzono


# ---------------------------------------------------------------------------
# Ranking — passy_pod_rzad dziedziczone z poprzedniego pliku, zapis atomowy
# ---------------------------------------------------------------------------
def wczytaj_poprzednie_rekordy(plik: Path) -> dict[str, dict]:
    """{url: rekord} z pliku sprzed TEGO odświeżenia — do policzenia `passy_pod_rzad`.
    Brak/uszkodzony plik = pusty słownik (każdy adres startuje z passą 1), nigdy wyjątek."""
    try:
        dane = json.loads(plik.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — pierwszy przebieg albo uszkodzony plik
        return {}
    if not isinstance(dane, dict):
        return {}
    return {r["url"]: r for r in dane.get("proxies", [])
            if isinstance(r, dict) and r.get("url")}


def dopisz_passy(rekordy: list[dict], poprzednie: dict[str, dict]) -> list[dict]:
    """Dolicza `passy_pod_rzad`: +1 względem poprzedniego pliku, jeśli adres tam
    już był (i przeszedł), 1 dla adresu widzianego pierwszy raz. Adres, który
    wypadł choć RAZ, wraca do puli z passą 1 — licznik nie pamięta historii
    sprzed przerwy, bo przerwa już jest dowodem, że nie jest „sprawdzony wielokrotnie"."""
    out = []
    for r in rekordy:
        poprzedni = poprzednie.get(r["url"])
        passa = (int(poprzedni.get("passy_pod_rzad") or 0) + 1) if poprzedni else 1
        out.append({**r, "passy_pod_rzad": passa})
    return out


def zapisz(plik: Path, rekordy: list[dict]) -> None:
    """Zapis ATOMOWY: tmp obok pliku + podmiana nazwy.

    `rekordy` — [{"url": ..., "zrodlo": ..., "czas_ms": ..., "passy_pod_rzad": ...}]
    dla adresów, które PRZESZŁY weryfikację (`apify_ok=True` zawsze — tylko takie
    tu trafiają, patrz nagłówek modułu). Klucze poza `url` są OPCJONALNE — do pliku
    trafia to, co jest dostępne, żeby wywołanie z samą listą URL-i (testy, ręczne
    użycie) nadal działało.

    POSORTOWANE od najlepszego (najdłuższa passa, potem najkrótszy czas
    odpowiedzi) — `workers/apify_proxy.py` i tak liczy przypisanie rendezvous
    hashingiem (kolejność w pliku niczego nie rusza), ale sortowanie robi z tego
    pliku coś więcej niż nieuporządkowaną listę: pierwszy rzut oka pokazuje
    NAJLEPSZE adresy, nie przypadkowe.

    Worker czyta ten plik z crona, W ŚRODKU naszego przebiegu. Zapis wprost
    zostawiłby okno, w którym plik jest ucięty w połowie — `_read_pool_file`
    potraktowałby to jak pustą pulę i konta wyszłyby z gołego IP VPS-a.
    """
    posortowane = sorted(
        rekordy,
        key=lambda r: (-(int(r.get("passy_pod_rzad") or 0)),
                       r.get("czas_ms") if r.get("czas_ms") is not None else 10**9),
    )
    proxies = []
    for r in posortowane:
        wpis = {"url": r["url"], "apify_ok": True}
        for pole in ("zrodlo", "czas_ms", "passy_pod_rzad"):
            if r.get(pole) is not None:
                wpis[pole] = r[pole]
        proxies.append(wpis)

    dane = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "zrodlo": "laweta_radar/scripts/odswiez_proxy.py",
        "proxies": proxies,
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


def _liczba_kluczy() -> int:
    """Ile kluczy APIFY_API_TOKEN* widać w env — podstawa domyślnego celu
    (`DOCELOWA_LICZBA_PROXY = 3x liczba kluczy`). WSZYSTKIE klucze (także za
    dziurą w numeracji), tak samo jak `apify_proxy._main --check` — literówka
    w numeracji to dziura, którą ktoś kiedyś naprawi."""
    return max(1, len(all_tokens_from_env()))


def _zaladuj_dotenv() -> None:
    """CLI ma widzieć to samo .env, co cron — bez tego `APIFY_PROXY_POOL_URL`,
    `PROXY_CHECK_PARALLEL_*`, `DOCELOWA_LICZBA_PROXY` i klucze Apify (do liczenia
    domyślnego celu) działałyby tylko wtedy, gdy ktoś sam wyeksportował .env do
    powłoki. Best-effort: brak paczki `dotenv` nie ma prawa wywalić skryptu."""
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Tryb SZYBKA KONTROLA — sprawdź TYLKO adresy już w puli
# ---------------------------------------------------------------------------
def szybka_kontrola(plik: Path, *, timeout: float, rownolegle: int, log=print) -> tuple[list[dict], int]:
    """Sprawdza WYŁĄCZNIE adresy już zapisane w `plik` (etap 3, bez limitu) i
    zwraca ([rekordy_żywych], ile_było_w_puli_przed_kontrolą). Adresy, które padły,
    znikają — nie ma sensu trzymać w pliku coś, co właśnie nie przeszło testu."""
    poprzednie = wczytaj_poprzednie_rekordy(plik)
    urls = [r["url"] for r in poprzednie.values()]
    if not urls:
        return [], 0
    direct_ip = ap.wlasny_ip(timeout)
    zrodlo_dla = {u: (poprzednie[u].get("zrodlo") or "?") for u in urls}
    zaakceptowane, _ = etap_pelny(urls, cel=0, timeout=timeout, rownolegle=rownolegle,
                                  direct_ip=direct_ip, zrodlo_dla=zrodlo_dla, log=log)
    return dopisz_passy(zaakceptowane, poprzednie), len(urls)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap_ = argparse.ArgumentParser(
        description="Odśwież i zweryfikuj darmową pulę proxy dla Apify z WIELU źródeł "
                    "(config/zrodla_proxy.py), etapowo (format -> TCP -> pełne 4 testy).",
        epilog="Pula działa tylko przy APIFY_PROXY_POOL=1 — patrz docs/APIFY-PROXY.md.",
    )
    ap_.add_argument("--zrodlo", action="append", default=[],
                     help="dodatkowy URL listy (powtarzalny) — DOKŁADANY do domyślnych "
                          "źródeł, nie zamiast nich")
    ap_.add_argument("--plik", default="", help="gdzie zapisać (domyślnie jak APIFY_PROXY_POOL_FILE)")
    ap_.add_argument("--limit", type=int, default=0,
                     help="sprawdź najwyżej N kandydatów PO scaleniu źródeł i losowaniu "
                          "kolejności (0 = wszystkie)")
    ap_.add_argument("--cel", type=int, default=0,
                     help="ile proxy zebrać, zanim etap 3 się przerwie "
                          "(0 = z DOCELOWA_LICZBA_PROXY / .env / domyślnie 3x liczba kluczy)")
    ap_.add_argument("--rownolegle-tcp", type=int, default=0,
                     help=f"ile jednoczesnych sprawdzeń TCP, etap 2 "
                          f"(0 = z PROXY_CHECK_PARALLEL_TCP, domyślnie {_DOMYSLNA_ROWNOLEGLE_TCP})")
    ap_.add_argument("--rownolegle-http", type=int, default=0,
                     help=f"ile jednoczesnych pełnych sprawdzeń, etap 3 "
                          f"(0 = z PROXY_CHECK_PARALLEL_HTTP, domyślnie {_DOMYSLNA_ROWNOLEGLE_HTTP})")
    ap_.add_argument("--timeout-tcp", type=float, default=_DOMYSLNY_TIMEOUT_TCP,
                     help=f"timeout pojedynczego TCP connect w etapie 2 (s), domyślnie "
                          f"{_DOMYSLNY_TIMEOUT_TCP:g}")
    ap_.add_argument("--timeout", type=float, default=_DOMYSLNY_TIMEOUT_HTTP,
                     help=f"timeout pojedynczego pełnego sprawdzenia w etapie 3 (s), "
                          f"domyślnie {_DOMYSLNY_TIMEOUT_HTTP:g}")
    ap_.add_argument("--tylko-pula", action="store_true",
                     help="SZYBKA KONTROLA: sprawdź TYLKO adresy już w puli (bez "
                          "pobierania źródeł), wyrzuć martwe; jeśli po czyszczeniu "
                          "zostanie mniej niż liczba kluczy, odpal PEŁNE odświeżenie od razu")
    ap_.add_argument("--sucho", action="store_true", help="pokaż plan, nie ruszaj sieci ani pliku")
    a = ap_.parse_args(argv)

    _zaladuj_dotenv()

    plik = Path(a.plik) if a.plik else _pool_file_path(os.environ)
    timeout_tcp = a.timeout_tcp
    timeout_http = a.timeout
    rownolegle_tcp = a.rownolegle_tcp or _int_env(os.environ, "PROXY_CHECK_PARALLEL_TCP",
                                                  _DOMYSLNA_ROWNOLEGLE_TCP)
    rownolegle_http = a.rownolegle_http or _int_env(os.environ, "PROXY_CHECK_PARALLEL_HTTP",
                                                     _DOMYSLNA_ROWNOLEGLE_HTTP)
    liczba_kluczy = _liczba_kluczy()
    cel = a.cel or _int_env(os.environ, "DOCELOWA_LICZBA_PROXY",
                            _DOMYSLNY_MNOZNIK_CELU * liczba_kluczy)

    if a.tylko_pula:
        print(f"[proxy] SZYBKA KONTROLA: {plik}")
        if a.sucho:
            print("[proxy] SUCHO — nic nie sprawdzam ani nie zapisuję.")
            return 0
        zywe, bylo = szybka_kontrola(plik, timeout=timeout_http, rownolegle=rownolegle_http)
        print(f"[proxy] Żywych po kontroli: {len(zywe)} z {bylo}")
        if len(zywe) >= liczba_kluczy:
            zapisz(plik, zywe)
            print(f"[proxy] Zapisane: {plik}")
            return 0
        print(f"[proxy] UWAGA: {len(zywe)} < {liczba_kluczy} kluczy — pula za mała, "
              f"odpalam PEŁNE odświeżenie od razu (nie czekam na następny cykl).")
        # Nie nadpisujemy pliku samą oczyszczoną (za małą) pulą — pełne odświeżenie
        # niżej i tak go nadpisze wynikiem, a częściowa pula bez tego zostałaby
        # zapisana na chwilę i zaraz przykryta, bez żadnej korzyści.
        a.tylko_pula = False
        # spada do pełnego odświeżenia niżej

    zrodla = {nazwa: dict(spec) for nazwa, spec in ZRODLA.items()}
    dodatkowe: list[str] = list(a.zrodlo)
    dodatkowe += _split_urls(os.environ.get("APIFY_PROXY_POOL_URL", ""))
    if dodatkowe:
        # Ad-hoc źródła (CLI / .env) niosą zawsze PEŁNY URL, tak jak działało to
        # przed obsługą wielu źródeł — protokol=None, goła linia jest tu śmieciem.
        zrodla["dodatkowe (.env / --zrodlo)"] = {"protokol": None, "urls": dodatkowe}

    print(f"[proxy] Źródeł: {len(zrodla)} "
          f"({sum(len(spec['urls']) for spec in zrodla.values())} URL-i)")
    print(f"[proxy] Plik:   {plik}")
    print(f"[proxy] Cel weryfikacji (etap 3): {CEL_WERYFIKACJI}")
    print(f"[proxy] Cel liczby proxy: {cel} (liczba kluczy: {liczba_kluczy})")
    if a.sucho:
        print(f"[proxy] SUCHO — nic nie pobieram i nic nie zapisuję.")
        print(f"[proxy] Etap 2 (TCP): {rownolegle_tcp} naraz, timeout {timeout_tcp:g}s")
        print(f"[proxy] Etap 3 (pełne 4 testy): {rownolegle_http} naraz, timeout {timeout_http:g}s")
        print(f"[proxy] APIFY_PROXY_POOL="
              f"{'1 (pula WŁĄCZONA)' if pula_wlaczona() else '0 — worker i tak NIE użyje tego pliku'}")
        return 0

    surowe = pobierz_zrodla(zrodla, timeout_http)
    if not any(d["linie"] for d in surowe.values()):
        print("[proxy] ŻADNE źródło nic nie dało (sieć padła albo wszystkie zmieniły "
              "strukturę) — plik zostaje bez zmian.", file=sys.stderr)
        return 1

    kandydaci_final, zrodlo_dla, ile_surowych = scal_kandydatow(surowe)
    for nazwa in zrodla:
        print(f"[proxy]   {nazwa}: {ile_surowych.get(nazwa, 0)} kandydatów")
    print(f"[proxy] Po scaleniu i dedupie (host:port, bez adresów prywatnych): "
          f"{len(kandydaci_final)}")
    if not kandydaci_final:
        print("[proxy] Zero poprawnych kandydatów po filtrze formalnym — plik zostaje "
              "bez zmian.", file=sys.stderr)
        return 1

    if any(u.startswith("socks") for u in kandydaci_final):
        try:
            import socksio  # noqa: F401
        except ImportError:
            print('[proxy] UWAGA: w liście są adresy socks, a brakuje pakietu — '
                  'pip install "httpx[socks]". Bez niego wszystkie polecą jako niedziałające.')

    # Losowanie PRZED etapami sieciowymi: inaczej zawsze przechodziłyby te same
    # adresy z początku listy źródeł, a pula byłaby ciągle ta sama mimo wielu źródeł.
    random.shuffle(kandydaci_final)
    if a.limit:
        kandydaci_final = kandydaci_final[:max(0, a.limit)]
        print(f"[proxy] --limit: ograniczam do {len(kandydaci_final)} kandydatów")

    zywe_tcp = etap_tcp(kandydaci_final, timeout=timeout_tcp, rownolegle=rownolegle_tcp)
    if not zywe_tcp:
        print("[proxy] Zero adresów przeżyło etap TCP — plik zostaje bez zmian.",
              file=sys.stderr)
        return 1

    direct_ip = ap.wlasny_ip(timeout_http)
    zaakceptowane, sprawdzono = etap_pelny(
        zywe_tcp, cel=cel, timeout=timeout_http, rownolegle=rownolegle_http,
        direct_ip=direct_ip, zrodlo_dla=zrodlo_dla,
    )

    poprzednie = wczytaj_poprzednie_rekordy(plik)
    rekordy = dopisz_passy(zaakceptowane, poprzednie)
    zapisz(plik, rekordy)

    print(f"[proxy] Etap 3 (cztery pełne testy): sprawdzono {sprawdzono} z "
          f"{len(zywe_tcp)} po etapie TCP, dochodzi do Apify: {len(rekordy)}")
    per_zrodlo: dict[str, int] = {}
    for r in rekordy:
        per_zrodlo[r.get("zrodlo") or "?"] = per_zrodlo.get(r.get("zrodlo") or "?", 0) + 1
    for nazwa, ile in sorted(per_zrodlo.items(), key=lambda kv: -kv[1]):
        print(f"[proxy]   {nazwa}: {ile} zaakceptowanych")
    for r in sorted(rekordy, key=lambda r: r.get("czas_ms") or 0)[:10]:
        print(f"[proxy]   {mask_url(r['url'])}  {r.get('czas_ms', '?')} ms  "
              f"passa {r.get('passy_pod_rzad', 1)}  ({r.get('zrodlo', '?')})")
    if len(rekordy) > 10:
        print(f"[proxy]   … i {len(rekordy) - 10} więcej")

    if not rekordy:
        # Dokładnie ten wynik dał pomiar z repo źródłowego (0 z 411, jednym źródłem).
        # Plik zapisany jako pusty jest tu FUNKCJĄ, nie porażką: worker zobaczy pustą
        # pulę, a przy APIFY_PROXY_REQUIRED=1 zakończy czysto zamiast wyjść z IP VPS-a.
        print("[proxy] ZERO działających adresów — pula zapisana jako pusta.", file=sys.stderr)
        print("[proxy] Przy APIFY_PROXY_REQUIRED=1 workery zakończą czysto zamiast "
              "wyjść z IP VPS-a. Rozważ płatne proxy (APIFY_PROXY_URLS/APIFY_PROXY_URL).",
              file=sys.stderr)
        return 1

    if len(rekordy) < liczba_kluczy:
        print(f"[proxy] UWAGA: {len(rekordy)} działających adresów przy {liczba_kluczy} "
              f"kluczach — część kont wyjdzie z tego samego adresu (patrz "
              f"`python -m laweta_radar.workers.apify_proxy`).", file=sys.stderr)

    print(f"[proxy] Zapisane: {plik}")
    if not pula_wlaczona():
        print("[proxy] UWAGA: APIFY_PROXY_POOL nie jest ustawione na 1 — worker NIE użyje")
        print("[proxy]        tego pliku i dalej pokaże 'BRAK proxy'. Żeby włączyć, dopisz")
        print("[proxy]        do laweta_radar/.env:  APIFY_PROXY_POOL=1")
        print("[proxy]        oraz (bezpiecznik):    APIFY_PROXY_REQUIRED=1")
        print("[proxy]        Cron dwupoziomowy instaluje: scripts/setup_cron.sh")

    print("[proxy] Sprawdź przypisanie:  python -m laweta_radar.workers.apify_proxy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
