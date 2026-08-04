"""Dostęp do bazy dla API — połączenie na żądanie, bez puli.

DLACZEGO BEZ PULI POŁĄCZEŃ. Użytkownik jest JEDEN, panel odpytuje listę co 30 s,
a najcięższe zapytanie w całym API zwraca sto wierszy. Pula w takim ruchu nie
oszczędza niczego mierzalnego, a dokłada warstwę, która potrafi się cicho zepsuć:
połączenie zerwane przez restart Postgresa zostaje w puli jako „żywe" i pierwszy
request po restarcie kończy się `InterfaceError`, którego nikt nie widzi, bo panel
po prostu pokazuje pustą listę.

Połączenie per request kosztuje kilka milisekund i ma tę własność, że po awarii
bazy pierwsze udane zapytanie po prostu działa.

WSZYSTKIE ODPOWIEDZI SĄ SŁOWNIKAMI (`RealDictCursor`). Krotka indeksowana liczbą
w routerze to najłatwiejszy sposób na ciche przestawienie dwóch kolumn przy
edycji SQL-a — a `zlecenie["km"]` zamiast `wiersz[7]` psuje się głośno.
"""
from __future__ import annotations

from contextlib import contextmanager

from fastapi import HTTPException

from laweta_radar.config import settings


class BazaNiedostepna(HTTPException):
    """503, nie 500. Panel ma odróżnić „baza leży" od „mój request był zły" —
    przy pierwszym pokazuje ostatni stan z cache'u service workera i komunikat
    o braku łączności, przy drugim nie ma czego pokazywać."""

    def __init__(self, powod: str):
        super().__init__(status_code=503, detail=f"baza niedostępna: {powod}")


@contextmanager
def polaczenie():
    """Połączenie w bloku `with`. Rzuca `BazaNiedostepna` zamiast 500.

    Import psycopg2 jest LENIWY — `/health` i `/zdrowie` mają odpowiadać także
    w środowisku bez zainstalowanych zależności, bo to właśnie wtedy najbardziej
    ich potrzeba.
    """
    if not settings.DATABASE_URL:
        raise BazaNiedostepna("brak DATABASE_URL w .env")
    try:
        import psycopg2  # noqa: PLC0415 — patrz docstring
        import psycopg2.extras  # noqa: PLC0415
    except ImportError as e:
        raise BazaNiedostepna("brak psycopg2 (pip install -r requirements.txt)") from e

    try:
        conn = psycopg2.connect(settings.DATABASE_URL, connect_timeout=5,
                                cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as e:  # noqa: BLE001 — powód idzie do panelu, nie do tracebacka
        raise BazaNiedostepna(f"{type(e).__name__}: {str(e)[:200]}") from e
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — zamknięcie nie może przesłonić odpowiedzi
            pass


def kolumny(conn, tabela: str) -> set[str]:
    """Jakie kolumny realnie istnieją. Do pytania „czy migracja była odpalona".

    Repo ma zasadę, że żaden worker nie tworzy tabel — więc API MUSI umieć
    odpowiedzieć sensownie na bazie, na której odpalono trzy migracje z sześciu.
    Bez tego pierwszy deploy kończy się 500 z `UndefinedColumn` i pytaniem
    „czemu panel nie działa", zamiast zdaniem „odpal migrację 0004".
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            " WHERE table_schema = 'public' AND table_name = %s", (tabela,))
        return {w["column_name"] for w in cur.fetchall()}


def tabela_istnieje(conn, nazwa: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS jest", (f"public.{nazwa}",))
        return bool(cur.fetchone()["jest"])
