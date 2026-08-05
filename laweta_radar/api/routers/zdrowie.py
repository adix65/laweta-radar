"""`/zdrowie` — wykrywanie najgorszego możliwego stanu tego systemu.

CICHY FETCHER, KTÓRY PRZESTAŁ DZIAŁAĆ TRZY DNI TEMU. Nie ma wyjątku, nie ma
alertu, nie ma niczego — bo repo ma zasadę „brak konfiguracji = czyste wyjście,
NIGDY wyjątek", a zepsute proxy albo wypalona pula kluczy wygląda dla crona
identycznie jak spokojny dzień na grupach. Telefon po prostu milczy, a operator
zakłada, że nikt nic nie wrzucił.

Ten endpoint istnieje po to i tylko po to. Odpowiada na trzy pytania, każde
o innej awarii:

    kiedy ostatni udany run PER GRUPA   -> czy fetcher w ogóle chodzi
    ile kluczy Apify ma jeszcze kredyt  -> czy jest za co chodzić
    czy proxy odpowiada                 -> czy runy nie kończą się timeoutem

ODPOWIEDŹ TO JEDNO SŁOWO PLUS SZCZEGÓŁY. `status` ma trzy wartości i monitoring
alarmuje na jednej z nich, zamiast parsować dziesięć liczb:

    ok      wszystko chodzi
    uwaga   coś jest nie tak, ale system nadal dowozi (jedna grupa milczy)
    awaria  system NIE dowozi (żadna grupa nie odpowiedziała od godzin)

BEZ AUTORYZACJI, jak `/health`. To jest świadome: endpoint, który przy zepsutym
`API_TOKEN` odpowiada 503, jest bezużyteczny dokładnie w tej sytuacji, do której
został napisany. Nie wychodzą z niego żadne dane osobowe ani sekrety — same
liczby, znaczniki czasu i „ustawione/BRAK" (ta sama zasada co w `/health`).

WYWOŁANIA SIECIOWE SĄ OPCJONALNE (`?glebokie=1`). Sprawdzenie salda puli kont to
jedno zapytanie HTTPS na konto przez proxy tego konta — przy trzydziestu kontach
i timeoucie liczonym w sekundach endpoint odpytywany co minutę stałby się
własnym problemem. Domyślnie leci wersja z samej bazy i konfiguracji; głęboka
wersja jest do odpalenia ręcznie albo raz na godzinę z crona.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from laweta_radar.api import db
from laweta_radar.config import groups, settings

router = APIRouter(tags=["zdrowie"])

# Po ilu godzinach cisza grupy przestaje być normalna. Grupa jest odpytywana
# najrzadziej co MAX_INTERWAL_MIN (domyślnie 120 min), więc sześć godzin to
# trzykrotność najdłuższego dopuszczalnego odstępu — z zapasem na jeden
# nieudany przebieg i jeden pominięty przez wyczerpany budżet dobowy.
PROG_CISZY_H = 6

# Poniżej tylu USD kredytu na koncie klucz jest praktycznie zużyty. Pula jest
# WSPÓLNA z sales-core-engine, więc „prawie pusto" ma być widoczne, zanim
# skończy się na obu systemach naraz.
PROG_KREDYTU_USD = 1.0

# Timeout pojedynczego sprawdzenia salda. Krótszy niż domyślne 30 s z
# apify_credits — tam liczy się pewność pomiaru, tu szybkość odpowiedzi.
TIMEOUT_GLEBOKI_S = 8.0


def _grupy_z_bazy() -> tuple[list[dict], str | None]:
    """Ostatni run per grupa. Źródłem prawdy jest `harmonogram`, nie logi PM2."""
    with db.polaczenie() as conn:
        if not db.tabela_istnieje(conn, "harmonogram"):
            return [], "brak tabeli `harmonogram` — odpal migrację 0003_fetcher.sql"
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT h.group_url,
                       h.ostatni_run_at,
                       h.nastepny_run_at,
                       h.ostatni_blad,
                       h.pobrane_doba,
                       h.przydzial_doba,
                       EXTRACT(EPOCH FROM (NOW() - h.ostatni_run_at)) / 3600.0
                           AS godzin_temu,
                       (SELECT max(p.pobrany_at) FROM posty p
                         WHERE p.grupa_url = h.group_url) AS ostatni_post_at
                  FROM harmonogram h
                 ORDER BY h.ostatni_run_at DESC NULLS FIRST
                """)
            wiersze = [dict(w) for w in cur.fetchall()]

    for w in wiersze:
        godzin = w.get("godzin_temu")
        w["godzin_temu"] = round(float(godzin), 2) if godzin is not None else None
        # „Milczy" znaczy: nie było udanego runu albo ostatni skończył się błędem.
        # Grupa, która odpowiedziała pustką, jest ZDROWA — brak postów to stan
        # grupy, nie stan systemu.
        w["milczy"] = (w["ostatni_run_at"] is None
                       or (w["godzin_temu"] or 0) > PROG_CISZY_H
                       or bool(w["ostatni_blad"]))
    return wiersze, None


def _pula_apify(glebokie: bool) -> dict:
    """Klucze i proxy. Głęboka wersja pyta Apify o saldo KAŻDEGO konta.

    Jedno zapytanie odpowiada na dwa pytania naraz i to jest cała elegancja tego
    rozwiązania: ruch do api.apify.com idzie PRZEZ PROXY TEGO KLUCZA
    (`apify_credits.saldo` -> `apify_proxy.client_for_token`), więc udana
    odpowiedź znaczy jednocześnie „klucz żyje" i „proxy tego klucza odpowiada".
    Osobny ping proxy byłby drugim wywołaniem sprawdzającym to samo.
    """
    from laweta_radar.workers import apify_keys, apify_proxy  # noqa: PLC0415

    wynik: dict = {"glebokie": glebokie}
    try:
        tokeny = apify_keys.load_apify_tokens()
    except Exception as e:  # noqa: BLE001 — diagnostyka oddaje powód, nie 500
        return {"blad": f"{type(e).__name__}: {str(e)[:200]}"}

    wynik["kluczy"] = len(tokeny)
    wynik["zrodlo_kluczy"] = settings.WSPOLNE_APIFY_SKAD
    try:
        cfg = apify_proxy.load_proxy_config()
        wynik["proxy"] = {
            "skonfigurowane": cfg.enabled,
            "wymagane": cfg.required,
            "sticky_per_key": cfg.sticky_per_key,
            "ostrzezenia": list(cfg.warnings),
        }
    except Exception as e:  # noqa: BLE001
        cfg = None
        wynik["proxy"] = {"blad": f"{type(e).__name__}: {str(e)[:200]}"}

    if not glebokie or not tokeny:
        return wynik

    from laweta_radar.workers import apify_credits  # noqa: PLC0415

    z_kredytem, bez_kredytu, martwe = 0, 0, []
    for token in tokeny:
        try:
            saldo = apify_credits.saldo(token, timeout=TIMEOUT_GLEBOKI_S, cfg=cfg)
        except Exception as e:  # noqa: BLE001 — jeden martwy klucz nie kończy sprawdzenia
            # Token NIGDY nie trafia do odpowiedzi — maskuje go apify_keys._mask.
            martwe.append({"klucz": apify_keys._mask(token),
                           "powod": f"{type(e).__name__}: {str(e)[:120]}"})
            continue
        zostalo = saldo.zostalo_usd
        if zostalo is None or zostalo > PROG_KREDYTU_USD:
            z_kredytem += 1
        else:
            bez_kredytu += 1
    wynik.update({"z_kredytem": z_kredytem, "bez_kredytu": bez_kredytu,
                  "nieodpowiadajace": martwe,
                  "prog_kredytu_usd": PROG_KREDYTU_USD})
    return wynik


def _werdykt(grupy: list[dict], pula: dict, blad_bazy: str | None) -> tuple[str, list[str]]:
    """Jedno słowo dla monitoringu plus lista powodów dla człowieka."""
    powody: list[str] = []
    if blad_bazy:
        return "awaria", [blad_bazy]

    aktywne = [g for g in grupy if g.get("nastepny_run_at") is not None]
    milczace = [g for g in (aktywne or grupy) if g["milczy"]]
    if not grupy:
        powody.append("żadna grupa nie ma wpisu w harmonogramie — fetcher nigdy "
                      "nie wystartował (config/groups.py: wszystkie `unverified`?)")
    elif milczace and len(milczace) == len(aktywne or grupy):
        powody.append(f"WSZYSTKIE grupy ({len(milczace)}) milczą dłużej niż "
                      f"{PROG_CISZY_H} h — system nie dowozi")
    elif milczace:
        powody.append(f"{len(milczace)} z {len(aktywne or grupy)} grup milczy "
                      f"dłużej niż {PROG_CISZY_H} h: "
                      + ", ".join(g["group_url"] for g in milczace[:5]))

    if pula.get("blad"):
        powody.append(f"pula Apify: {pula['blad']}")
    elif not pula.get("kluczy"):
        powody.append("zero kluczy Apify — sprawdź SHARED_ENV_PATH "
                      "(python -m laweta_radar.workers.apify_keys)")
    elif pula.get("glebokie"):
        if pula.get("z_kredytem") == 0:
            powody.append("żaden klucz Apify nie ma kredytu — pula wypalona")
        if pula.get("nieodpowiadajace"):
            powody.append(f"{len(pula['nieodpowiadajace'])} kluczy nie odpowiada "
                          "(zły token albo martwe proxy)")

    # DWIE KLASY BRAKÓW, DWA RÓŻNE WERDYKTY (settings.stan_konfiguracji).
    # Wrzucenie ich do jednego worka kazało budzić człowieka w nocy z powodu
    # pustej linijki w .env — a przy LLM_PROVIDER=openai także z powodu klucza
    # Anthropic, którego ta instalacja nigdy nie tknie.
    konfiguracja = settings.stan_konfiguracji()
    if konfiguracja["blokujace_start"]:
        powody.append("brak w .env: " + ", ".join(konfiguracja["blokujace_start"])
                      + " — system nie dowozi")
    for nazwa in konfiguracja["degradujace"]:
        powody.append(f"brak {nazwa} w .env — {konfiguracja['skutki'][nazwa]}")

    if not powody:
        return "ok", []
    # „Nie dowozi" to awaria; wszystko inne to uwaga. Rozróżnienie jest po to,
    # żeby dało się na jednym z tych stanów wołać człowieka w nocy, a na drugim nie.
    ciezkie = any("nie dowozi" in p or "wypalona" in p or "nigdy nie wystartował" in p
                  for p in powody)
    return ("awaria" if ciezkie else "uwaga"), powody


@router.get("/zdrowie")
def zdrowie(glebokie: bool = Query(default=False,
                                   description="dopytaj Apify o saldo kont "
                                               "(wolne: 1 zapytanie na konto)")) -> dict:
    """Stan systemu jednym zapytaniem. ZAWSZE HTTP 200 — patrz docstring modułu.

    Kod odpowiedzi mówi „API żyje", a treść mówi, co jest zepsute. 503 na
    niepełnej konfiguracji kazałoby monitoringowi traktować niewłączony system
    jak awarię i mieszałoby dwie zupełnie różne sytuacje.
    """
    try:
        grupy, blad_bazy = _grupy_z_bazy()
    except db.BazaNiedostepna as e:
        grupy, blad_bazy = [], str(e.detail)

    pula = _pula_apify(glebokie)
    status, powody = _werdykt(grupy, pula, blad_bazy)

    return {
        "status": status,
        "powody": powody,
        "grupy": {
            "w_konfiguracji": len(groups.FB_GRUPY),
            "do_pobrania": len(groups.grupy_do_pobrania()),
            "prog_ciszy_h": PROG_CISZY_H,
            "harmonogram": grupy,
        },
        "apify": pula,
        "budzet": {
            "postow_na_dobe": settings.POSTY_NA_DOBE,
            "zuzyte_dzis": sum(g.get("pobrane_doba") or 0 for g in grupy),
        },
        "powiadomienia": {
            "telegram_skonfigurowany": bool(settings.TELEGRAM_BOT_TOKEN
                                            and settings.TELEGRAM_CHAT_ID),
            "cisza_nocna": f"{settings.CISZA_NOCNA_OD}-{settings.CISZA_NOCNA_DO}",
            "limit_na_godzine": settings.MAX_POWIADOMIEN_H,
        },
    }
