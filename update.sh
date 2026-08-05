#!/usr/bin/env bash
# Aktualizacja maszyny JEDNYM poleceniem: ściągnij z GitHuba to, co nowe, dołóż
# zależności, zmigruj bazę, przebuduj panel, przeładuj procesy PM2. Po przejściu
# tego skryptu na maszynie chodzi dokładnie to, co jest na gałęzi.
#
# UŻYCIE:
#   ./update.sh                # aktualizuj i przeładuj, jeśli jest co
#   ./update.sh --sucho        # pokaż plan (co przyszło, co się z tego odpali) — nic nie rusza
#   ./update.sh --force        # przeładuj i przebuduj nawet bez nowych commitów
#   ./update.sh --bez-panelu   # pomiń build panelu (sam API + bot, kilka sekund)
#
# DLACZEGO WSZYSTKIE RESTARTY SĄ NA SAMYM KOŃCU, a nie przy okazji każdego kroku:
# `npm run build` trwa minuty i potrafi paść. Restart API zaraz po `git merge`
# otworzyłby okno, w którym API chodzi na nowym kodzie, panel na starym, a baza
# stoi na starym schemacie — i to okno trwałoby dokładnie tyle, ile build. Więc
# kolejność jest: najpierw WSZYSTKO, co może się wywalić (pip, migracje, build),
# a dopiero potem, jednym ciągiem, przeładowanie procesów.
#
# ROBI TYLKO TO, CO WYNIKA Z DIFFA. `pip install` leci, gdy ruszył
# requirements.txt, migracje — gdy przyszedł nowy plik .sql, build panelu — gdy
# ruszyło cokolwiek w panel/. Bezwarunkowe robienie wszystkiego za każdym razem
# znaczy 3 minuty przestoju panelu po literówce poprawionej w README.
set -euo pipefail

ROOT_DIR="${_UPDATE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
cd "$ROOT_DIR"

# TEN SKRYPT AKTUALIZUJE SAM SIEBIE, i to nie jest drobiazg: `git merge` niżej
# podmienia update.sh NA DYSKU w trakcie jego wykonywania, a bash doczytuje plik
# w miarę potrzeby — z przesunięciem policzonym dla pliku, którego już nie ma.
# Dalsza część przebiegu leci wtedy z mieszanki starej i nowej wersji.
#
# Objaw jest mylący do bólu: poprawka w update.sh "nie działa" dokładnie w tym
# uruchomieniu, które ją ściągnęło, i wygląda na niedziałającą poprawkę zamiast
# na przeczytany do połowy plik. Kosztowało to trzy rundy zgadywania.
#
# Dlatego pracujemy z KOPII w /tmp: plik w repo może się zmieniać do woli.
if [[ -z "${_UPDATE_KOPIA:-}" ]]; then
    KOPIA="$(mktemp -t update.sh.XXXXXX)"
    cat "$ROOT_DIR/update.sh" > "$KOPIA"
    KOD=0
    _UPDATE_KOPIA=1 _UPDATE_ROOT="$ROOT_DIR" bash "$KOPIA" "$@" || KOD=$?
    rm -f "$KOPIA"
    exit "$KOD"
fi

FORCE=0
SUCHO=0
PANEL=1
BLEDY=0

# Nazwy procesów i porty biorą się z .env TEJ instancji — inaczej `update.sh`
# odpalony w /home/ubuntu/laweta-test przeładowałby procesy produkcyjne, bo
# nazwy PM2 są globalne, a katalog nie ma z nimi nic wspólnego.
z_env() {   # klucz, wartość domyślna
    local v=""
    if [[ -f laweta_radar/.env ]]; then
        v="$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" laweta_radar/.env \
             | tail -1 | sed 's/[[:space:]]*#.*$//; s/^["'\'']//; s/["'\'']$//')"
    fi
    echo "${v:-$2}"
}

INSTANCJA="${INSTANCJA:-$(z_env INSTANCJA '')}"
NAZWA="laweta${INSTANCJA:+-$INSTANCJA}"
API_PORT="${API_PORT:-$(z_env API_PORT 8002)}"       # patrz laweta_radar/scripts/start_api.sh
PANEL_PORT="${PANEL_PORT:-$(z_env PANEL_PORT 6200)}" # patrz panel/package.json, skrypt `start`

log() { echo "[update] $*"; }
ostrzez() { echo "[update] !! $*" >&2; }

pomoc() {
    cat <<'POMOC'
update.sh — aktualizacja lawety z GitHuba i przeładowanie procesów.

  ./update.sh                aktualizuj i przeładuj, jeśli na origin jest coś nowego
  ./update.sh --sucho        pokaż, co by się stało; nic nie zmienia
  ./update.sh --force        przebuduj i przeładuj nawet bez nowych commitów
  ./update.sh --bez-panelu   pomiń build panelu (sam API + bot)

Migracje odpalają się WYŁĄCZNIE wtedy, gdy przyszedł nowy plik
laweta_radar/api/migrations/*.sql — także przy --force. Na produkcji podaj
admina bazy jawnie:

  DATABASE_URL_ADMIN="postgresql://postgres@localhost/laweta" ./update.sh
POMOC
}

for arg in "$@"; do
    case "$arg" in
        --force)      FORCE=1 ;;
        --sucho)      SUCHO=1 ;;
        --bez-panelu) PANEL=0 ;;
        -h|--help)    pomoc; exit 0 ;;
        *) ostrzez "nieznany argument: $arg"; pomoc >&2; exit 2 ;;
    esac
done

# --- 1. Czy w ogóle jest z czego i gdzie aktualizować -----------------------

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    ostrzez "$ROOT_DIR nie jest repozytorium gita — nie ma czego ciągnąć."
    exit 1
fi

GALAZ="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$GALAZ" == "HEAD" ]]; then
    ostrzez "odłączony HEAD — nie wiem, którą gałąź aktualizować."
    ostrzez "wejdź na gałąź deployu:  git checkout main"
    exit 1
fi

# Blokują TYLKO zmodyfikowane pliki śledzone. Nieśledzone (logi, .env, venv)
# są normalnym stanem maszyny produkcyjnej i nie kolidują z przewinięciem.
# Zatrzymujemy się tutaj świadomie: `git merge` na brudnym drzewie albo padnie
# w połowie, albo cicho zje czyjąś ręczną poprawkę wklepaną wprost na serwerze.
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    ostrzez "lokalne zmiany w śledzonych plikach — nie ruszam:"
    git status --short --untracked-files=no | sed 's/^/           /' >&2
    ostrzez "schowaj je (git stash) albo wyrzuć (git checkout -- .) i puść jeszcze raz."
    exit 1
fi

# --- 2. Co przyszło z GitHuba ----------------------------------------------

log "Sprawdzam origin/$GALAZ…"
for proba in 1 2 3; do
    if git fetch --quiet origin "$GALAZ"; then
        break
    fi
    if [[ $proba -eq 3 ]]; then
        ostrzez "git fetch nie przeszedł 3 razy — sieć albo dostęp do repo."
        exit 1
    fi
    sleep $((proba * 2))
done

PRZED="$(git rev-parse HEAD)"
ZDALNY="$(git rev-parse "origin/$GALAZ")"

if [[ "$PRZED" == "$ZDALNY" && $FORCE -eq 0 ]]; then
    log "Nic nowego na origin/$GALAZ — maszyna jest aktualna ($(git rev-parse --short HEAD))."
    log "Chcesz mimo to przebudować i przeładować:  ./update.sh --force"
    exit 0
fi

ZMIANY="$(git diff --name-only "$PRZED" "$ZDALNY")"
zmienione() { grep -qE "$1" <<<"$ZMIANY"; }

# Migracje NIE reagują na --force, w odróżnieniu od reszty kroków: schemat bazy
# zmienia się wtedy, gdy przyszła nowa migracja, a nie wtedy, gdy ktoś chce
# przeładować procesy. Puszczanie migracji „na wszelki wypadek" na produkcji
# wymagałoby trzymania pod ręką DSN-a z prawami DDL przy każdym restarcie.
MIGRACJE=0; zmienione '^laweta_radar/api/migrations/' && MIGRACJE=1
PIP=0;      { [[ $FORCE -eq 1 ]] || zmienione '^laweta_radar/requirements\.txt$'; } && PIP=1
BUILD=0;    { [[ $PANEL -eq 1 ]] && { [[ $FORCE -eq 1 ]] || zmienione '^panel/'; }; } && BUILD=1
NPM_CI=0;   { [[ $BUILD -eq 1 ]] && { [[ $FORCE -eq 1 ]] || zmienione '^panel/package(-lock)?\.json$' || [[ ! -d panel/node_modules ]]; }; } && NPM_CI=1

if [[ "$PRZED" != "$ZDALNY" ]]; then
    log "Nowe commity ($(git rev-list --count "$PRZED..$ZDALNY")):"
    git log --oneline "$PRZED..$ZDALNY" | sed 's/^/           /'
fi

if [[ $SUCHO -eq 1 ]]; then
    log "SUCHO — poniżej plan, nic nie zostanie zmienione."
    log "  przewinięcie:  $(git rev-parse --short "$PRZED") -> $(git rev-parse --short "$ZDALNY")"
    log "  pip install:   $([[ $PIP -eq 1 ]] && echo TAK || echo 'nie (requirements.txt bez zmian)')"
    log "  migracje:      $([[ $MIGRACJE -eq 1 ]] && echo TAK || echo 'nie (brak nowych .sql)')"
    log "  npm ci:        $([[ $NPM_CI -eq 1 ]] && echo TAK || echo nie)"
    log "  build panelu:  $([[ $BUILD -eq 1 ]] && echo TAK || echo nie)"
    log "  restart:       $NAZWA-api, $NAZWA-bot$([[ $BUILD -eq 1 ]] && echo ", $NAZWA-panel" || true)"
    exit 0
fi

# --- 3. Kod ----------------------------------------------------------------

# --ff-only: aktualizacja maszyny ma być PRZEWINIĘCIEM. Jeśli się nie da, to
# znaczy, że ktoś commitował wprost na serwerze — i lepiej, żeby to wyszło tutaj
# niż jako merge commit, który nigdy nie wróci do repo.
if ! git merge --ff-only --quiet "origin/$GALAZ"; then
    ostrzez "nie da się przewinąć do origin/$GALAZ — historia lokalna się rozjechała."
    ostrzez "zobacz co jest lokalnie:  git log --oneline origin/$GALAZ..HEAD"
    exit 1
fi
log "Kod: $(git rev-parse --short "$PRZED") -> $(git rev-parse --short HEAD)"

# --- 4. Zależności Pythona -------------------------------------------------

PY="${PYTHON_BIN:-$ROOT_DIR/venv/bin/python3}"
if [[ $PIP -eq 1 ]]; then
    if [[ -x "$PY" ]]; then
        log "Zależności Pythona (requirements.txt)…"
        "$PY" -m pip install --quiet --upgrade-strategy only-if-needed -r laweta_radar/requirements.txt
    else
        ostrzez "brak $PY — pomijam pip. Dołóż zależności ręcznie, inaczej API może nie wstać."
        BLEDY=1
    fi
fi

# SDK providera modelu. requirements.txt trzyma tylko `anthropic` i to jest
# świadome: krótka lista to mniej rzeczy, które mogą się nie zbudować na świeżym
# VPS-ie o drugiej w nocy. Ale skoro .env MÓWI, którego providera używamy, deploy
# ma go dowieźć — brak paczki jest awarią CICHĄ: fetcher wstaje, posty przechodzą
# przez bramkę i czekają w bazie, a nikt nie dostaje alertu.
#
# Sprawdzamy import, nie `pip show`: liczy się to, czy proces zaraz to zaimportuje.
sdk_providera() {
    local provider pakiet modul
    provider="$(z_env LLM_PROVIDER anthropic)"
    case "$provider" in
        openai) pakiet="openai>=1.40.0";      modul="openai" ;;
        gemini) pakiet="google-genai>=1.0.0"; modul="google.genai" ;;
        *)      return 0 ;;   # anthropic (i nieznane, które do niego degraduje) jest w requirements.txt
    esac
    [[ -x "$PY" ]] || return 0
    "$PY" -c "import $modul" 2>/dev/null && return 0
    log "LLM_PROVIDER=$provider — brakuje pakietu, dokładam $pakiet"
    if ! "$PY" -m pip install --quiet "$pakiet"; then
        ostrzez "nie udało się doinstalować $pakiet — klasyfikator będzie milczeć."
        BLEDY=1
    fi
}
sdk_providera

# --- 5. Migracje -----------------------------------------------------------

# Kolejność jest tu istotna: schemat MUSI być gotowy, zanim procesy pójdą na
# nowym kodzie. Nowy kod na starym schemacie to błędy SQL na produkcji; stary
# kod na nowym schemacie (migracje są dokładające, IF NOT EXISTS) chodzi dalej.
if [[ $MIGRACJE -eq 1 ]]; then
    log "Nowe migracje — odpalam migrate.sh."
    if ! bash laweta_radar/scripts/migrate.sh; then
        ostrzez "MIGRACJE NIE PRZESZŁY — procesów nie ruszam."
        ostrzez "system chodzi dalej na starym kodzie ($(git rev-parse --short "$PRZED"))."
        ostrzez "napraw bazę i puść ponownie, albo cofnij kod:  git reset --hard $PRZED"
        exit 1
    fi
fi

# --- 6. Panel --------------------------------------------------------------

if [[ $BUILD -eq 1 ]]; then
    if ! command -v npm >/dev/null 2>&1; then
        ostrzez "brak npm w PATH — panelu nie przebuduję (Node 20+, patrz README)."
        BLEDY=1
        BUILD=0
    else
        # Powłoka panelu zmieniona, a WERSJA w service workerze ta sama = telefon,
        # na którym raz zainstalowano PWA, zostaje na starej powłoce NA ZAWSZE
        # (cache-first). Awaria cicha: build przechodzi, panel w przeglądarce jest
        # nowy, a na telefonie operatora nic się nie zmienia.
        SW_DIFF="$(git diff "$PRZED" HEAD -- panel/public/sw.js || true)"
        if zmienione '^panel/' && ! grep -qE '^[-+]const WERSJA' <<<"$SW_DIFF"; then
            ostrzez "powłoka panelu się zmieniła, a WERSJA w panel/public/sw.js — nie."
            ostrzez "zainstalowane PWA zostaną na starej wersji. Podbij WERSJA i puść ponownie."
        fi

        if [[ $NPM_CI -eq 1 ]]; then
            log "Panel: npm ci…"
            (cd panel && npm ci)
        fi
        log "Panel: npm run build…"
        (cd panel && npm run build)
    fi
fi

# --- 7. Przeładowanie procesów ---------------------------------------------

przeladuj() {
    local proc="$1"
    if ! command -v pm2 >/dev/null 2>&1; then
        ostrzez "brak pm2 w PATH — '$proc' NIE został przeładowany."
        BLEDY=1
        return
    fi
    # Nieznany proces to nie błąd aktualizacji, tylko maszyna, na której go nigdy
    # nie podniesiono (albo bot bez tokenu). Reszta deployu ma dojechać do końca.
    if ! pm2 describe "$proc" >/dev/null 2>&1; then
        ostrzez "PM2 nie zna procesu '$proc' — pomijam (pierwsze uruchomienie: README, sekcja Deploy)."
        return
    fi
    # Wyjście PM2 ląduje w zmiennej, a nie w /dev/null: "nie przeszedł" bez
    # powodu zostawia operatora z niczym, a powód PM2 podaje wprost.
    local wyjscie stan
    if wyjscie="$(pm2 restart "$proc" 2>&1)"; then
        log "przeładowany: $proc"
        return
    fi

    # Proces, który NIE chodził, jest osobnym przypadkiem, a nie awarią
    # aktualizacji. Bot bez TELEGRAM_BOT_TOKEN kończy CZYSTO (zasada z całego
    # repo), więc PM2 trzyma go w pętli `waiting restart` i odbija `pm2 restart`
    # przez "Process not found". Nowy kod podejmie sam przy najbliższym
    # nawrocie — traktowanie tego jak błędu uczy operatora ignorować błędy.
    # Wycinamy tablicę od pierwszego [ do ostatniego ], bo `pm2 jlist` potrafi
    # dokleić na stdout baner (np. "In-memory PM2 is out-of-date"), na którym
    # JSON.parse całości się wywala — a wtedy stan wychodzi pusty i proces, który
    # po prostu nie chodził, jest raportowany jako awaria.
    stan="$(pm2 jlist 2>/dev/null | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{try{const i=d.indexOf("["),j=d.lastIndexOf("]");const a=JSON.parse(d.slice(i,j+1)).find(p=>p.name===process.argv[1]);process.stdout.write(a?String(a.pm2_env.status):"")}catch(e){}})' "$proc" 2>/dev/null)"
    if [[ -n "$stan" && "$stan" != "online" ]]; then
        log "pominięty: $proc (stan: $stan — nie chodzi, podejmie nowy kod sam)"
        log "           dlaczego nie chodzi:  pm2 logs $proc --lines 30 --nostream"
        return
    fi

    ostrzez "pm2 restart $proc nie przeszedł:"
    sed 's/^/           /' <<<"$wyjscie" >&2
    ostrzez "logi procesu:  pm2 logs $proc --lines 30"
    BLEDY=1
}

log "Przeładowuję procesy${INSTANCJA:+ (instancja: $INSTANCJA)}."
przeladuj "$NAZWA-api"
przeladuj "$NAZWA-bot"
if [[ $BUILD -eq 1 ]]; then
    przeladuj "$NAZWA-panel"
fi

# --- 8. Czy na pewno wstało ------------------------------------------------

# Bez tego "gotowe" znaczyłoby tylko tyle, że PM2 przyjął polecenie. Proces,
# który wstaje i pada w pętli, wygląda w `pm2 restart` na sukces.
czekaj_na() {
    local url="$1"
    for _ in $(seq 1 20); do
        if curl -sf -o /dev/null --max-time 2 "$url"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

if command -v curl >/dev/null 2>&1; then
    if czekaj_na "http://127.0.0.1:$API_PORT/health"; then
        ZDROWIE="$(curl -s --max-time 3 "http://127.0.0.1:$API_PORT/health" || true)"
        STATUS="$(sed -n 's/.*"status": *"\([^"]*\)".*/\1/p' <<<"$ZDROWIE")"
        log "API: ${STATUS:-odpowiada}"
        # /health zawsze oddaje 200 — także gdy brakuje konfiguracji. Samo "wstało"
        # nie wystarczy, treść odpowiedzi mówi, czy system faktycznie pracuje.
        if [[ "$STATUS" == "niepelna_konfiguracja" ]]; then
            ostrzez "API wstało, ale zgłasza braki: bash laweta_radar/scripts/check_setup.sh"
        fi
    else
        ostrzez "API nie odpowiada na 127.0.0.1:$API_PORT/health — pm2 logs $NAZWA-api"
        BLEDY=1
    fi

    if [[ $BUILD -eq 1 ]]; then
        if czekaj_na "http://127.0.0.1:$PANEL_PORT/"; then
            log "Panel: odpowiada na :$PANEL_PORT"
        else
            ostrzez "panel nie odpowiada na :$PANEL_PORT — pm2 logs $NAZWA-panel"
            BLEDY=1
        fi
    fi
fi

if [[ $BLEDY -ne 0 ]]; then
    ostrzez "zakończone Z BŁĘDAMI — patrz wyżej."
    exit 1
fi

log "Gotowe. Chodzi $(git rev-parse --short HEAD) z gałęzi $GALAZ."
