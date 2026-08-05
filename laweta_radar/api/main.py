"""API panelu: dane dla PWA (`panel/`) plus diagnostyka „czy to w ogóle chodzi".

TRZY GRUPY ENDPOINTÓW, TRZY RÓŻNE ODPOWIEDZI NA BRAK KONFIGURACJI:

    /health, /zdrowie   BEZ tokenu, zawsze 200. To są endpointy, których
                        potrzeba dokładnie wtedy, gdy konfiguracja jest zepsuta
                        — 503 z powodu złego `API_TOKEN` czyniłoby je
                        bezużytecznymi w jedynej sytuacji, do której powstały.
    /zlecenia, /statystyki   WYMAGAJĄ tokenu (`X-Token`). Wychodzą stąd numery
                        telefonów obcych ludzi z grup FB, a adres panelu jest
                        publiczny, bo PWA musi być dostępna z telefonu.

BEZPIECZEŃSTWO: nasłuch WYŁĄCZNIE na 127.0.0.1 (patrz `uvicorn.run` niżej
i scripts/start_api.sh). Na zewnątrz wystawia to nginx z TLS-em i to on odpowiada
za dostęp; token jest drugą warstwą, nie jedyną. Nasłuch na 0.0.0.0 wystawiłby
bazę zleceń na goły internet niezależnie od tokenu.

Odpowiedzi diagnostyczne NIE niosą sekretów: tylko "ustawione/BRAK" (patrz
config/settings.opis_srodowiska) — endpoint diagnostyczny jest pierwszym miejscem,
przez które wyciekają tokeny, bo wygląda niewinnie.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from laweta_radar.api.routers import push, statystyki, zdrowie, zlecenia
from laweta_radar.config import groups, settings
from laweta_radar.services import geo, llm


@asynccontextmanager
async def _cykl_zycia(_app: FastAPI):
    """Wypisz linię startową i oddaj sterowanie. Nic tu nie może rzucić.

    `lifespan`, a nie `@app.on_event("startup")` — tamto jest w tej wersji
    FastAPI przestarzałe i dokłada ostrzeżenie dokładnie w miejscu, które ma być
    czytelne: w pierwszych linijkach `pm2 logs`.

    Stoi tu, bo `FastAPI(...)` niżej bierze je argumentem; sama treść linii
    startowej siedzi w `_linia_startowa` razem z resztą pomocniczych.
    """
    _linia_startowa()
    yield


app = FastAPI(
    title="Laweta Radar",
    description="Monitoring grup FB pod kątem zleceń dla lawety.",
    lifespan=_cykl_zycia,
)

# CORS wyłącznie dla adresu panelu z `.env`. Świadomie BEZ `allow_origins=["*"]`:
# API oddaje cudze numery telefonów, a gwiazdka znaczy, że dowolna strona
# otwarta na telefonie operatora może je odczytać jego tokenem. Puste
# `PANEL_URL` = zero dozwolonych źródeł, czyli panel serwowany z tej samej
# domeny co API (przez nginx) — i to jest układ domyślny, w którym CORS
# w ogóle nie wchodzi w grę.
if settings.PANEL_URL:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.PANEL_URL],
        allow_methods=["GET", "PATCH", "OPTIONS"],
        allow_headers=["X-Token", "Content-Type"],
    )

app.include_router(zlecenia.router)
app.include_router(statystyki.router)
app.include_router(zdrowie.router)
app.include_router(push.router)


def _linia_startowa() -> None:
    """Co jest ustawione, a co nie — JEDNA linia w `pm2 logs` przy każdym starcie.

    API WSTAJE ZAWSZE, także z niepełną konfiguracją, i to jest jedyne poprawne
    zachowanie: brak klucza modelu wyłącza klasyfikator, ale fetcher dalej zbiera
    posty do bazy, bramka dalej punktuje, panel dalej pokazuje zebrane, a Telegram
    dalej dowozi. Zakończenie procesu odebrałoby operatorowi cztery działające
    podsystemy z powodu jednego niedziałającego — a pod PM2 zrobiłoby z tego pętlę
    restartów ze statusem `errored`, czyli objaw wyglądający na awarię kodu.

    Dlatego brak trafia do logu i do `/health`, a nie do `sys.exit`. Ta linia jest
    tam, gdzie operator i tak zagląda po `pm2 status`.
    """
    print(settings.opis_srodowiska(), file=sys.stderr)
    print(llm.opis(), file=sys.stderr)
    stan = settings.stan_konfiguracji()
    if stan["blokujace_start"]:
        # API stoi dalej — `/health` jest potrzebne DOKŁADNIE teraz — ale bez
        # bazy nie odda żadnych danych i endpointy z danymi zwrócą 503.
        print(f"[api] BRAK {', '.join(stan['blokujace_start'])} — wstaję, ale "
              f"endpointy z danymi oddadzą 503. Diagnostyka: /health i /zdrowie.",
              file=sys.stderr)
    for nazwa in stan["degradujace"]:
        print(f"[api] niepelna_konfiguracja: {nazwa} — {stan['skutki'][nazwa]}",
              file=sys.stderr)


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
                    return {"ok": False, "powod": "brak tabeli `posty` — odpal "
                                                  "migracje (scripts/migrate.sh)"}
                # Trzy liczby, nie jedna: "1200 postów" nie odróżnia systemu,
                # który zbiera i nic nie znajduje, od systemu, który znajduje
                # i nie dowozi. Pierwsze to problem z listą grup, drugie —
                # z klasyfikatorem albo powiadomieniami.
                cur.execute("SELECT count(*), "
                            "count(*) FILTER (WHERE czy_zlecenie), "
                            "count(*) FILTER (WHERE pobrany_at > NOW() - "
                            "INTERVAL '24 hours') FROM posty")
                (ile, zlecen, doba) = cur.fetchone()
        return {"ok": True, "postow": ile, "zlecen": zlecen, "postow_24h": doba}
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
    konfiguracja = settings.stan_konfiguracji()
    stan_config = konfiguracja.pop("status")
    problemy_llm = llm.problemy()
    return {
        "status": "ok" if (baza["ok"] and stan_config == "ok") else "niepelna_konfiguracja",
        "baza": baza,
        # DWIE KLASY, NIE JEDNA LISTA — patrz `settings.stan_konfiguracji`.
        # Płaska lista stawiała brak DATABASE_URL obok nieużywanego klucza
        # nieaktywnego providera, więc nie dało się z niej odczytać jedynej
        # rzeczy, po którą się tu przychodzi: czy system w ogóle działa.
        "braki": konfiguracja,
        "grupy": {
            "do_pobrania": len(groups.grupy_do_pobrania()),
            "wszystkich": len(groups.FB_GRUPY),
        },
        # Klasyfikator i geo mają własne warunki startu, których nie widać
        # w `braki`: brakującą paczkę providera i brakujący plik z kodami
        # pocztowymi. Obie awarie są CICHE — system wstaje, nie woła modelu albo
        # nie pokazuje tras, i nic o tym nie mówi.
        "klasyfikator": {
            "provider": llm.normalizuj_provider(settings.LLM_PROVIDER),
            "model": llm.model_domyslny(),
            "gotowy": not problemy_llm,
            "problemy": problemy_llm,
            # Klucze POZOSTAŁYCH providerów są opcjonalne — ich brak zawęża
            # `scripts/porownaj_modele.py` i nic poza tym. Stoi tu jako
            # informacja, nigdy jako powód alarmu.
            "porownanie_modeli": konfiguracja["porownanie_modeli"],
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
