"""API panelu — na razie sama diagnostyka: "czy ten system w ogóle jest włączony".

DLACZEGO to istnieje już teraz, zanim jest co pokazywać: pipeline chodzi z crona
i milczy, gdy czegoś brakuje (taka jest zasada w tym repo — brak konfiguracji to
czyste wyjście, nie wyjątek). Cisza jest wtedy poprawna, ale nie do odróżnienia od
ciszy "nikt nic nie wrzucił na grupy". `/health` odpowiada na to jednym zapytaniem,
bez wchodzenia na VPS i czytania logów PM2.

BEZPIECZEŃSTWO: nasłuch WYŁĄCZNIE na 127.0.0.1 (patrz `uvicorn.run` niżej i
scripts/start_api.sh). Ten serwis nie ma autoryzacji — na zewnątrz wystawia go
nginx, i to nginx odpowiada za dostęp. Nasłuch na 0.0.0.0 wystawiłby stan bazy
i konfiguracji na goły internet.

Odpowiedzi NIE niosą sekretów: tylko "ustawione/BRAK" (patrz
config/settings.opis_srodowiska) — endpoint diagnostyczny jest pierwszym miejscem,
przez które wyciekają tokeny, bo wygląda niewinnie.
"""
from __future__ import annotations

from fastapi import FastAPI

from laweta_radar.config import groups, settings
from laweta_radar.services import geo, llm

app = FastAPI(
    title="Laweta Radar",
    description="Monitoring grup FB pod kątem zleceń dla lawety.",
)


def _stan_bazy() -> dict:
    """Czy da się połączyć z bazą i czy migracje były odpalone.

    Import psycopg2 jest LENIWY: `/health` ma odpowiedzieć nawet wtedy, gdy
    środowisko jest niekompletne — a to właśnie wtedy najbardziej go potrzeba.
    """
    if not settings.DATABASE_URL:
        return {"ok": False, "powod": "brak DATABASE_URL"}
    try:
        import psycopg2  # noqa: PLC0415 — patrz docstring
    except ImportError:
        return {"ok": False, "powod": "brak psycopg2 (pip install -r requirements.txt)"}
    try:
        with psycopg2.connect(settings.DATABASE_URL, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.posty') IS NOT NULL")
                (jest_tabela,) = cur.fetchone()
                if not jest_tabela:
                    # Rozróżnienie jest ważne: "baza działa, ale nie ma tabel"
                    # znaczy, że ktoś pominął migrację — a to wygląda w logach
                    # workera identycznie jak zerwane połączenie.
                    return {"ok": False, "powod": "brak tabeli `posty` — odpal migracje "
                                                  "(scripts/migrate.sh)"}
                cur.execute("SELECT count(*) FROM posty")
                (ile,) = cur.fetchone()
        return {"ok": True, "postow": ile}
    except Exception as e:  # noqa: BLE001 — diagnostyka ma oddać powód, nie 500
        return {"ok": False, "powod": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/health")
def health() -> dict:
    """Stan systemu jednym zapytaniem: konfiguracja, baza, lista grup.

    Zawsze HTTP 200, także gdy coś nie gra — kod odpowiedzi mówi "API żyje",
    a treść mówi, co jest zepsute. 503 na braku konfiguracji kazałoby nginxowi
    i monitoringowi traktować "niewłączony system" jak awarię.
    """
    baza = _stan_bazy()
    braki = settings.brakujace(*settings.OPIS_ZMIENNYCH)
    return {
        "status": "ok" if (baza["ok"] and not braki) else "niepelna_konfiguracja",
        "baza": baza,
        "brakujace_zmienne": braki,
        "grupy": {
            "do_pobrania": len(groups.grupy_do_pobrania()),
            "wszystkich": len(groups.FB_GRUPY),
        },
        # Klasyfikator i geo mają własne warunki startu, których nie widać
        # w `brakujace_zmienne`: brakującą paczkę providera i brakujący plik
        # z kodami pocztowymi. Obie awarie są CICHE — system wstaje, nie woła
        # modelu albo nie pokazuje tras, i nic o tym nie mówi.
        "klasyfikator": {
            "provider": llm.normalizuj_provider(settings.LLM_PROVIDER),
            "model": llm.model_domyslny(),
            "gotowy": not llm.problemy(),
            "problemy": llm.problemy(),
        },
        "geo": {
            "baza_ustawiona": bool(settings.BAZA_LAT or settings.BAZA_LON),
            "max_dystans_km": settings.MAX_DYSTANS_KM,
            "kody": geo.stan_bazy(),
        },
        # Klucze Apify nie należą do tego repo — przychodzą ze wspólnego .env
        # sales-core-engine. Bez tej sekcji „brak kluczy Apify" wyglądałoby na
        # problem lawety, a najczęstszą przyczyną jest zła ŚCIEŻKA do tamtego
        # pliku. Podajemy ścieżkę i liczbę zmiennych — nigdy wartości.
        "wspolna_pula_apify": {
            "plik": str(settings.sciezka_wspolnego_env() or ""),
            "znaleziony": settings.sciezka_wspolnego_env() is not None,
            "wczytanych_zmiennych": settings.WSPOLNE_APIFY_ILE,
        },
    }


if __name__ == "__main__":
    import uvicorn

    # host=127.0.0.1 na sztywno — patrz nota o bezpieczeństwie w docstringu modułu.
    uvicorn.run("laweta_radar.api.main:app", host="127.0.0.1", port=8002, reload=True)
