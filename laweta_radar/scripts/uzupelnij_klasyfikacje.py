#!/usr/bin/env python3
"""Dopisanie klasyfikacji do postów, które ją ZGUBIŁY. Ratunek dla starych wierszy.

PO CO TO ISTNIEJE. Przebiegi fetchera sprzed poprawki zapisu zostawiły w bazie
wiersze z werdyktem modelu (`zrodlo_decyzji='ai'`, `czy_zlecenie` ustawione)
i KOMPLETEM pól ekstrakcji na NULL — bez typu, miejsc, telefonu i pewności.
Za te wyniki zapłacono tokenami, a w panelu nie ma z nich nic.

Poprawiony fetcher naprawia taki wiersz sam, ale tylko wtedy, gdy Apify pokaże
ten post jeszcze raz — a grupy oddają najnowsze posty, więc wczorajszy kurs nie
wróci. Ten skrypt robi to samo bez czekania i BEZ ANI JEDNEGO WYWOŁANIA APIFY:
treść posta jest już w bazie, więc płacimy wyłącznie za tokeny modelu.

CZEGO TEN SKRYPT NIE ROBI: nie wysyła powiadomień. Alert o zleceniu sprzed
kilkunastu godzin nie jest już informacją operacyjną („po tym kursie ktoś dawno
pojechał"), a seria takich alertów to dokładnie ta lawina, po której bot zostaje
wyciszony. Uzupełniamy dane — do panelu, do statystyk i do macierzy pomyłek
bramki — a nie budzimy nikogo przeszłością.

UŻYCIE (ręcznie, nigdy z crona — to jest wydatek, nie rutyna):

    python laweta_radar/scripts/uzupelnij_klasyfikacje.py --sucho     # ile i za ile
    python laweta_radar/scripts/uzupelnij_klasyfikacje.py --limit 50  # naprawa 50
    python laweta_radar/scripts/uzupelnij_klasyfikacje.py --limit 50 --tylko-zlecenia

`--sucho` jest domyślnym pierwszym krokiem: pokazuje liczbę wierszy do naprawy
i szacowany koszt, nie wołając modelu ani razu.
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
from laweta_radar.workers import classifier  # noqa: E402

KTO = "uzupelnij"


def _log(msg: str) -> None:
    print(f"[{KTO}] {msg}")


def wiersze_do_naprawy(conn, *, limit: int, tylko_zlecenia: bool) -> list[tuple]:
    """Posty z werdyktem modelu i pustą ekstrakcją, od najświeższych.

    Warunek „pusta ekstrakcja" budujemy z `classifier.KOLUMNY_EKSTRAKCJI`, a nie
    z listy przepisanej tutaj: druga lista tych samych nazw rozjechałaby się przy
    pierwszej zmianie migracji i skrypt zaczął albo pomijać uszkodzone wiersze,
    albo przepłacać za naprawianie zdrowych.
    """
    warunek = " AND ".join(f"{k} IS NULL" for k in classifier.KOLUMNY_EKSTRAKCJI)
    tylko = " AND czy_zlecenie" if tylko_zlecenia else ""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT fb_id, tresc, grupa_nazwa, gate_jezyk FROM posty "  # noqa: S608
            f" WHERE zrodlo_decyzji = 'ai' AND {warunek}{tylko}"
            f" ORDER BY pobrany_at DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def napraw(conn, wiersz: tuple) -> bool:
    """Jeden post: model -> `wiersz_do_zapisu` -> `SQL_ZAPIS`. True = uzupełniony.

    Wołamy `classifier.SQL_ZAPIS`, czyli tę samą ścieżkę, co klasyfikacja postów
    historycznych — wiersz już istnieje, więc INSERT-a nie ma po co udawać.
    """
    fb_id, tresc, grupa, jezyk = wiersz
    wynik = classifier.klasyfikuj(tresc or "", grupa or "", jezyk or "")
    dane = classifier.wiersz_do_zapisu(wynik, fb_id, tresc=tresc)
    with conn.cursor() as cur:
        cur.execute(classifier.SQL_ZAPIS, dane)
    conn.commit()
    # Sprawdzamy WIERSZ, nie słownik, który poszedł do UPDATE-a. Cała ta awaria
    # wzięła się z pytania pamięci zamiast bazy — powtarzanie tego błędu
    # w skrypcie naprawczym byłoby wyjątkowo kosztownym żartem.
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(classifier.KOLUMNY_EKSTRAKCJI)} "  # noqa: S608
                    f"FROM posty WHERE fb_id = %s", (fb_id,))
        zapisany = cur.fetchone()
    return not classifier.ekstrakcja_pusta(
        dict(zip(classifier.KOLUMNY_EKSTRAKCJI, zapisany or ())))


def run(*, limit: int, sucho: bool, tylko_zlecenia: bool, log=_log) -> int:
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
        wiersze = wiersze_do_naprawy(conn, limit=limit, tylko_zlecenia=tylko_zlecenia)
        if not wiersze:
            log("Nie ma czego naprawiać — żaden wiersz z werdyktem modelu nie ma "
                "pustej ekstrakcji.")
            return 0

        log(f"Do naprawy: {len(wiersze)} postów (limit {limit}). Każdy to jedno "
            f"wywołanie modelu; Apify NIE jest wołane.")
        if sucho:
            for fb_id, tresc, grupa, _ in wiersze[:10]:
                log(f"  {fb_id}  {(tresc or '')[:60]!r}  [{grupa or '?'}]")
            log("--sucho: nie wołam modelu. Nic nie wydano.")
            return 0

        naprawione = puste = bledy = 0
        for wiersz in wiersze:
            try:
                if napraw(conn, wiersz):
                    naprawione += 1
                else:
                    # Model odpowiedział, a w wierszu nadal pustka — to jest ta
                    # sama cicha utrata, tylko w skrypcie. Musi być głośna.
                    puste += 1
                    log(f"OSTRZEŻENIE: {wiersz[0]} nadal bez ekstrakcji po zapisie "
                        f"— sprawdź kolumny z 0004_klasyfikacja.sql.")
            except Exception as e:  # noqa: BLE001 — jeden post nie psuje reszty
                bledy += 1
                conn.rollback()
                log(f"{wiersz[0]}: {type(e).__name__}: {str(e)[:150]}")

        log(f"Gotowe: {naprawione} uzupełnionych, {puste} nadal pustych, "
            f"{bledy} błędów.")
        return 0
    finally:
        conn.close()


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Dopisanie ekstrakcji do postów, które mają werdykt modelu "
                    "i komplet NULL-i. Czyta treść z bazy, nie woła Apify.")
    ap.add_argument("--limit", type=int, default=100, metavar="N",
                    help="ile postów naprawić w tym uruchomieniu (domyślnie 100)")
    ap.add_argument("--sucho", action="store_true",
                    help="pokaż, ile jest do naprawy — bez wołania modelu")
    ap.add_argument("--tylko-zlecenia", action="store_true",
                    help="napraw wyłącznie posty z czy_zlecenie=true (te, które "
                         "operator zobaczy w kolejce)")
    args = ap.parse_args(argv[1:])
    return run(limit=args.limit, sucho=args.sucho,
               tylko_zlecenia=args.tylko_zlecenia)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
