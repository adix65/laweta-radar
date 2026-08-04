"""Uruchomienie actora Apify i odebranie wyników — jedno miejsce dla całego repo.

DLACZEGO OSOBNY MODUŁ, a nie kawałek każdego skryptu: „odpal actora i poczekaj"
wygląda na trzy linijki, a ma cztery pułapki, które kosztują pieniądze albo
diagnozę. Napisane raz, wołane przez `scripts/pomiar_actora.py`,
`scripts/znajdz_grupy.py` i (docelowo) fetcher — zamiast trzech kopii, z których
każda ma inną pułapkę załataną:

  1. RUN TRZEBA POLLOWAĆ, nie czekać jednym zapytaniem. `run-sync-get-dataset-items`
     oddaje same itemy — a bez obiektu runu nie ma `usageTotalUsd`, czyli nie ma
     odpowiedzi na pytanie „ile kosztował ten run". Startujemy więc run i odpytujemy
     o jego stan, aż dojdzie do stanu końcowego.
  2. RUN, KTÓRY SIĘ NIE UDAŁ, TEŻ KOSZTUJE. `FAILED`, `TIMED-OUT` i `ABORTED`
     zostają policzone, więc muszą trafić do raportu z kosztem, a nie zniknąć jako
     „wyjątek". Dlatego nieudany run jest normalnym wynikiem (`Run.udany == False`),
     a nie wyjątkiem.
  3. BŁĘDY HTTP MUSZĄ LECIEĆ WYŻEJ NIETKNIĘTE. To po nich `apify_keys` poznaje
     wyczerpany klucz (401/402/403) i odróżnia go od chwilowej awarii proxy.
     Opakowanie ich we własny wyjątek zamieniłoby rotację kluczy w zgadywankę.
  4. WYJŚCIE ZAWSZE PRZEZ PROXY TEGO TOKENU. Klienta HTTP bierzemy z
     `apify_proxy.client_for_token`, nigdy z gołego `httpx.Client()`.

`schemat_wejscia()` jest tu z piątego powodu: actory ze Store zmieniają nazwy pól
między wersjami, a literówka w nazwie pola nie zwraca błędu — zwraca run bez
filtra, za pełną cenę. Zanim wydamy pieniądze na serię, pytamy actora, jakie pola
naprawdę przyjmuje.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from laweta_radar.workers.apify_proxy import client_for_token

API = "https://api.apify.com/v2"

# Stany, po których run już się nie zmieni. "TIMING-OUT"/"ABORTING" są przejściowe
# i przechodzą w swoje wersje dokonane, więc czekamy na nie.
STANY_KONCOWE = ("SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED")

# Co ile pytamy o stan runu. 5 s to kompromis: scrapowanie grupy FB trwa
# kilkadziesiąt sekund do kilku minut, więc częstsze pytanie nic nie przyspiesza,
# a rzadsze dokłada do zmierzonego czasu trwania błąd większy niż on sam.
ODSTEP_POLLU_S = 5.0

# Sufit czekania po naszej stronie, niezależny od `timeout` przekazanego Apify.
# Gdyby Apify nie zamknął runu (albo zgubiła się odpowiedź), skrypt ma się poddać
# sam, zamiast wisieć do końca świata w tle crona.
MAX_CZEKANIA_S = 900.0

# Ile itemów bierzemy jednym zapytaniem do datasetu.
STRONA_DATASETU = 1000


class ApifyRunError(RuntimeError):
    """Nasz własny błąd przebiegu (np. run bez datasetu) — NIE błąd HTTP z Apify."""


@dataclass
class Run:
    """Wynik jednego uruchomienia actora — razem z kosztem i czasem.

    `koszt_usd` bierzemy z `usageTotalUsd` obiektu runu. Jest natychmiastowy
    i przypisany dokładnie do tego runu — inaczej niż licznik konta, który
    agreguje z opóźnieniem (patrz `apify_credits`).
    """

    id: str
    status: str
    itemy: list[dict] = field(default_factory=list)
    koszt_usd: float | None = None
    trwanie_s: float | None = None
    blad: str | None = None
    surowy: dict = field(default_factory=dict)

    @property
    def udany(self) -> bool:
        return self.status == "SUCCEEDED"

    @property
    def ile_itemow(self) -> int:
        return len(self.itemy)


def normalizuj_actor(actor: str) -> str:
    """`apify/nazwa` -> `apify~nazwa`. API przyjmuje tyldę, ludzie piszą ukośnik."""
    return (actor or "").strip().replace("/", "~")


def _naglowki(token: str) -> dict[str, str]:
    """Token w NAGŁÓWKU, nigdy w URL-u — URL-e trafiają do logów i śladów błędów."""
    return {"Authorization": f"Bearer {token}"}


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trwanie(run: dict) -> float | None:
    """Czas trwania runu w sekundach, ze znaczników Apify (ISO 8601 z Z)."""
    from datetime import datetime  # noqa: PLC0415 — potrzebny tylko tutaj

    start, koniec = run.get("startedAt"), run.get("finishedAt")
    if not start or not koniec:
        return None
    try:
        t0 = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(koniec).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t1 - t0).total_seconds()


def schemat_wejscia(token: str, actor: str, *, timeout: float = 30.0, env=None) -> dict:
    """Input schema NAJNOWSZEGO builda actora. `{}` gdy actor go nie publikuje.

    Droga jest dwuetapowa, bo schemat nie leży w obiekcie actora: actor wskazuje
    `taggedBuilds.latest.buildId`, a schemat siedzi dopiero w buildzie (jako łańcuch
    z JSON-em). Wołający porównuje z tym nazwy pól, które zamierza wysłać —
    literówka w nazwie nie daje błędu, tylko run bez filtra za pełną cenę.
    """
    import json  # noqa: PLC0415 — schemat przychodzi jako string, nie jako obiekt

    aid = normalizuj_actor(actor)
    with client_for_token(token, timeout=timeout, env=env) as klient:
        odp = klient.get(f"{API}/acts/{aid}", headers=_naglowki(token))
        odp.raise_for_status()
        dane = (odp.json() or {}).get("data") or {}
        build_id = (((dane.get("taggedBuilds") or {}).get("latest") or {})
                    .get("buildId"))
        if not build_id:
            return {}
        odp = klient.get(f"{API}/actor-builds/{build_id}", headers=_naglowki(token))
        odp.raise_for_status()
        surowy = ((odp.json() or {}).get("data") or {}).get("inputSchema")
    if not surowy:
        return {}
    if isinstance(surowy, dict):
        return surowy
    try:
        return json.loads(surowy)
    except (TypeError, ValueError):
        return {}


def nieznane_pola(wejscie: dict, schemat: dict) -> list[str]:
    """Pola z `wejscie`, których actor NIE deklaruje. Pusty schemat = pusta lista.

    Nie rzucamy i nie usuwamy pól za wołającego: to ma być OSTRZEŻENIE przed
    wydaniem pieniędzy, a nie ciche „naprawienie" wejścia. Actor bez opublikowanego
    schematu (pusty `{}`) nie pozwala niczego stwierdzić — wtedy lista jest pusta,
    bo brak wiedzy to nie to samo co brak problemu.
    """
    wlasciwosci = (schemat or {}).get("properties")
    if not isinstance(wlasciwosci, dict) or not wlasciwosci:
        return []
    return [k for k in wejscie if k not in wlasciwosci]


def _pobierz_itemy(klient, token: str, dataset_id: str, limit: int | None) -> list[dict]:
    """Wszystkie itemy z datasetu runu, stronami. `limit` ucina od góry."""
    itemy: list[dict] = []
    offset = 0
    while True:
        ile = STRONA_DATASETU
        if limit is not None:
            zostalo = limit - len(itemy)
            if zostalo <= 0:
                break
            ile = min(ile, zostalo)
        odp = klient.get(
            f"{API}/datasets/{dataset_id}/items",
            params={"offset": offset, "limit": ile, "clean": "true"},
            headers=_naglowki(token),
        )
        odp.raise_for_status()
        strona = odp.json() or []
        if not isinstance(strona, list) or not strona:
            break
        itemy.extend(x for x in strona if isinstance(x, dict))
        if len(strona) < ile:
            break
        offset += len(strona)
    return itemy


def uruchom(
    token: str,
    actor: str,
    wejscie: dict,
    *,
    timeout_s: int = 300,
    pamiec_mb: int | None = None,
    max_itemow: int | None = None,
    max_czekania_s: float = MAX_CZEKANIA_S,
    odstep_s: float = ODSTEP_POLLU_S,
    env=None,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> Run:
    """Odpal actora, doczekaj końca, oddaj itemy + koszt + czas.

    Nieudany run (`FAILED`/`TIMED-OUT`/`ABORTED`) NIE jest wyjątkiem — jest
    wynikiem z `udany == False` i policzonym kosztem, bo Apify liczy go tak samo
    jak udany. Wyjątki HTTP z Apify lecą wyżej nietknięte, żeby `apify_keys` mógł
    je zaklasyfikować (wyczerpany klucz vs chwilowa awaria proxy).

    `timeout_s` dostaje Apify (ubija run po swojej stronie). `max_czekania_s` to
    nasz własny sufit czekania — o połowę dłuższy, żeby normalnie to Apify kończył
    run pierwszy, a nasz sufit łapał tylko sytuację „run wisi i nikt go nie zamyka".
    """
    aid = normalizuj_actor(actor)
    parametry: dict[str, object] = {"timeout": int(timeout_s)}
    if pamiec_mb:
        parametry["memory"] = int(pamiec_mb)

    with client_for_token(token, timeout=60.0, env=env) as klient:
        odp = klient.post(f"{API}/acts/{aid}/runs", params=parametry,
                          json=wejscie, headers=_naglowki(token))
        odp.raise_for_status()
        run = ((odp.json() or {}).get("data") or {})
        run_id = str(run.get("id") or "")
        if not run_id:
            raise ApifyRunError(f"Apify nie zwrócił id runu dla actora {aid}")
        log(f"[apify-run] {aid}: run {run_id} wystartował")

        czekano = 0.0
        while run.get("status") not in STANY_KONCOWE:
            if czekano >= max_czekania_s:
                log(f"[apify-run] run {run_id} nadal w stanie {run.get('status')} "
                    f"po {czekano:.0f}s — przestaję czekać (run zostaje u Apify)")
                break
            sleep(odstep_s)
            czekano += odstep_s
            odp = klient.get(f"{API}/actor-runs/{run_id}", headers=_naglowki(token))
            odp.raise_for_status()
            run = ((odp.json() or {}).get("data") or {})

        status = str(run.get("status") or "NIEZNANY")
        wynik = Run(
            id=run_id,
            status=status,
            koszt_usd=_float_or_none(run.get("usageTotalUsd")),
            trwanie_s=_trwanie(run),
            surowy=run,
        )
        if status != "SUCCEEDED":
            # Komunikat błędu Apify trzyma w `statusMessage`; bywa pusty przy timeoucie.
            wynik.blad = str(run.get("statusMessage") or f"status={status}")

        dataset_id = str(run.get("defaultDatasetId") or "")
        if dataset_id:
            # Itemy czytamy TAKŻE z runu nieudanego: FAILED po połowie roboty i tak
            # zwykle zostawia w datasecie to, co zdążył pobrać — i i tak za to płacimy.
            wynik.itemy = _pobierz_itemy(klient, token, dataset_id, max_itemow)
        elif wynik.udany:
            raise ApifyRunError(f"Run {run_id} zakończony sukcesem, ale bez datasetu")

    koszt = "?" if wynik.koszt_usd is None else f"{wynik.koszt_usd:.4f} USD"
    czas = "?" if wynik.trwanie_s is None else f"{wynik.trwanie_s:.0f}s"
    log(f"[apify-run] run {run_id}: {status}, {wynik.ile_itemow} itemów, "
        f"{czas}, {koszt}")
    return wynik
