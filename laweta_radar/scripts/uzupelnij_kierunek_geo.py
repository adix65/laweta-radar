#!/usr/bin/env python3
"""Przeliczenie kraju i kierunku geograficznego dla postów sprzed migracji
0013_kierunek_geo.sql. Jednorazowy backfill, BEZ ani jednego wywołania modelu.

PO CO TO ISTNIEJE. `workers/classifier.wiersz_do_zapisu` liczy `odbior_kraj`,
`dostawa_kraj` i `kierunek_geo` na bieżąco, od migracji 0013 — ale posty
sklasyfikowane WCZEŚNIEJ mają te trzy kolumny puste, choć mają już miasta
i kody pocztowe w `odbior_miasto`/`odbior_kod`/`dostawa_miasto`/`dostawa_kod`.
Bez tego skryptu "pokaż wyjazdy z Polski" milczałoby o całej historii sprzed
włączenia tej funkcji, mimo że dane do policzenia tego są już w bazie.

DLACZEGO BEZ MODELU. Kraj ustala geokoder (`services/geo.geokoduj`) z tego,
co już stoi w `odbior_kod`/`odbior_miasto`/`dostawa_kod`/`dostawa_miasto` —
te pola wyciągnął klasyfikator w swoim czasie i nie trzeba pytać go drugi raz.
Geokodowanie liczy się w locie i offline, więc backfill to czysta funkcja tego,
co już jest w wierszu: żadnego kosztu Apify, żadnego kosztu modelu.

CZEGO TEN SKRYPT NIE ROBI: nie wysyła powiadomień i nie dotyka `czy_zlecenie`
ani `status`. Uzupełnia trzy kolumny geograficzne — nic więcej.

UŻYCIE (ręcznie, po odpaleniu migracji 0013, nigdy z crona):

    python laweta_radar/scripts/uzupelnij_kierunek_geo.py --sucho     # ile do zrobienia
    python laweta_radar/scripts/uzupelnij_kierunek_geo.py             # wszystkie
    python laweta_radar/scripts/uzupelnij_kierunek_geo.py --limit 500

`--sucho` jest domyślnym pierwszym krokiem: pokazuje liczbę wierszy do
przeliczenia bez ruszania bazy.
"""
from __future__ import annotations

import argparse
import sys

try:                               # pakiet widoczny: -m, import pakietowy, testy
    from laweta_radar.scripts._sciezka import dodaj_repo_do_sciezki
except ImportError:                # uruchomienie po ścieżce do pliku
    from _sciezka import dodaj_repo_do_sciezki

dodaj_repo_do_sciezki()

from laweta_radar.config import settings  # noqa: E402
from laweta_radar.services import geo  # noqa: E402

KTO = "uzupelnij-geo"


def _log(msg: str) -> None:
    print(f"[{KTO}] {msg}")


def wiersze_do_przeliczenia(conn, limit: int) -> list[tuple]:
    """Posty z jakimkolwiek rozpoznanym końcem trasy, którym nikt jeszcze nie
    policzył `kierunek_geo`. Od najświeższych — jeśli operator przerwie
    skrypt limitem, historia świeża wchodzi pierwsza.

    WARUNEK "MA MIASTA": post bez ANI JEDNEGO pola lokalizacji (bramka
    odrzuciła przed modelem, albo model nic nie znalazł) i tak dałby
    `kierunek_geo="nieznany"` — a NULL w kolumnie czyta się PO TEJ MIGRACJI
    dokładnie tak samo (patrz komentarz w 0013_kierunek_geo.sql). Przeliczanie
    takich wierszy byłoby UPDATE-em, który niczego nie zmienia.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fb_id, odbior_kod, odbior_miasto, dostawa_kod, dostawa_miasto, tresc
              FROM posty
             WHERE kierunek_geo IS NULL
               AND (odbior_kod IS NOT NULL OR odbior_miasto IS NOT NULL
                    OR dostawa_kod IS NOT NULL OR dostawa_miasto IS NOT NULL)
             ORDER BY pobrany_at DESC
             LIMIT %s
            """, (limit,))
        return cur.fetchall()


def przelicz(conn, wiersz: tuple) -> str:
    """Jeden post: geokoder -> UPDATE. Zwraca policzony `kierunek_geo`.

    Ta sama para wywołań `geo.geokoduj`, co przy zapisie klasyfikacji
    (`classifier.wiersz_do_zapisu`) — inna implementacja tej samej reguły
    rozjechałaby się przy pierwszej zmianie geokodera i backfill zacząłby
    liczyć inaczej niż bieżący zapis.
    """
    fb_id, o_kod, o_miasto, d_kod, d_miasto, tresc = wiersz
    odbior = geo.geokoduj(o_kod, o_miasto, tresc=tresc)
    dostawa = geo.geokoduj(d_kod, d_miasto, tresc=tresc)
    odbior_kraj = odbior.kraj if odbior else None
    dostawa_kraj = dostawa.kraj if dostawa else None
    kierunek = geo.kierunek_geo(odbior_kraj, dostawa_kraj)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE posty SET odbior_kraj = %s, dostawa_kraj = %s, "
            "kierunek_geo = %s WHERE fb_id = %s",
            (odbior_kraj, dostawa_kraj, kierunek, fb_id))
    conn.commit()
    return kierunek


def run(*, limit: int, sucho: bool, log=_log) -> int:
    if not settings.DATABASE_URL:
        log("Brak DATABASE_URL — kończę bez działania.")
        return 0
    try:
        import psycopg2  # noqa: PLC0415 — leniwie, jak wszędzie w tym repo
    except ImportError:
        log("Brak psycopg2 — pip install -r laweta_radar/requirements.txt")
        return 0

    conn = psycopg2.connect(settings.DATABASE_URL)
    try:
        wiersze = wiersze_do_przeliczenia(conn, limit)
        if not wiersze:
            log("Nie ma czego przeliczać — żaden wiersz z miastem nie ma "
                "pustego kierunek_geo.")
            return 0

        log(f"Do przeliczenia: {len(wiersze)} postów (limit {limit}). "
            f"Zero wywołań Apify, zero wywołań modelu.")
        if sucho:
            for fb_id, o_kod, o_miasto, d_kod, d_miasto, _tresc in wiersze[:10]:
                log(f"  {fb_id}  {o_miasto or o_kod or '?'} -> "
                    f"{d_miasto or d_kod or '?'}")
            log("--sucho: nic nie zapisano.")
            return 0

        policzone: dict[str, int] = {}
        bledy = 0
        for wiersz in wiersze:
            try:
                kierunek = przelicz(conn, wiersz)
                policzone[kierunek] = policzone.get(kierunek, 0) + 1
            except Exception as e:  # noqa: BLE001 — jeden post nie psuje reszty
                bledy += 1
                conn.rollback()
                log(f"{wiersz[0]}: {type(e).__name__}: {str(e)[:150]}")

        rozklad = ", ".join(f"{k}={v}" for k, v in sorted(policzone.items()))
        log(f"Gotowe: {sum(policzone.values())} przeliczonych ({rozklad}), "
            f"{bledy} błędów.")
        return 0
    finally:
        conn.close()


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Przeliczenie odbior_kraj/dostawa_kraj/kierunek_geo dla "
                    "postów sprzed migracji 0013. Czyta miasta z bazy, nie "
                    "woła ani Apify, ani modelu.")
    ap.add_argument("--limit", type=int, default=5000, metavar="N",
                    help="ile postów przeliczyć w tym uruchomieniu (domyślnie 5000)")
    ap.add_argument("--sucho", action="store_true",
                    help="pokaż, ile jest do przeliczenia — bez ruszania bazy")
    args = ap.parse_args(argv[1:])
    return run(limit=args.limit, sucho=args.sucho)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
