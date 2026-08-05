#!/usr/bin/env bash
# PIERWSZE uruchomienie na VPS-ie: venv, zależności, .env, baza, migracje, build
# panelu, procesy PM2. Od świeżego klona do chodzącej instancji, jednym
# poleceniem. Aktualizacje robi potem ./update.sh.
#
# UŻYCIE:
#   git clone https://github.com/adix65/laweta-radar.git /home/ubuntu/laweta-test
#   cd /home/ubuntu/laweta-test
#   ./setup.sh --instancja test
#
#   ./setup.sh                 # instancja produkcyjna (laweta-api, porty 8002/6200)
#   ./setup.sh --instancja test  # laweta-test-api, porty 8012/6210, baza laweta_test
#   ./setup.sh --bez-panelu    # bez builda panelu (samo API + bot)
#   ./setup.sh --bez-bazy      # nie dotykaj Postgresa (masz własną bazę w .env)
#
# IDEMPOTENTNY. Puszczony drugi raz NIE nadpisuje .env — bo w .env są klucze
# wklepane ręcznie i skrypt, który je kasuje "przy okazji naprawiania czegoś
# innego", jest gorszy od braku skryptu. Uzupełnia tylko to, czego brakuje.
#
# CZEGO ŚWIADOMIE NIE ROBI:
#   • nie instaluje crona fetchera — cron kosztuje kredyt Apify i wysyła alerty
#     na Telegram. Instancja testowa ma odpalać fetcher RĘCZNIE, z --sucho.
#     Wpis crona wypisuje się na końcu, do świadomego wklejenia.
#   • nie instaluje pakietów systemowych (python3, node, postgresql, pm2) — to
#     decyzja o stanie MASZYNY, nie o tym projekcie. Brakujące wypisze naraz,
#     z gotowymi poleceniami.
#   • nie zgaduje sekretów. ANTHROPIC_API_KEY i tokeny Telegrama zostają puste,
#     a system wstaje i mówi, czego mu brak (zasada z całego repo).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

INSTANCJA=""
PANEL=1
BAZA=1
API_PORT=""
PANEL_PORT=""
BLEDY=0
DO_UZUPELNIENIA=()

log() { echo "[setup] $*"; }
ostrzez() { echo "[setup] !! $*" >&2; }

pomoc() {
    cat <<'POMOC'
setup.sh — pierwsze uruchomienie lawety na VPS-ie.

  ./setup.sh                     instancja produkcyjna: laweta-api, porty 8002/6200
  ./setup.sh --instancja test    laweta-test-api, porty 8012/6210, baza laweta_test
  ./setup.sh --port-api 8012     własny port API
  ./setup.sh --port-panel 6210   własny port panelu
  ./setup.sh --bez-panelu        pomiń npm ci + build panelu
  ./setup.sh --bez-bazy          nie twórz roli ani bazy (masz własną w .env)

Nie nadpisuje istniejącego .env i nie instaluje crona fetchera.
Aktualizacje po wdrożeniu: ./update.sh
POMOC
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --instancja)  INSTANCJA="${2:-}"; shift 2 ;;
        --port-api)   API_PORT="${2:-}"; shift 2 ;;
        --port-panel) PANEL_PORT="${2:-}"; shift 2 ;;
        --bez-panelu) PANEL=0; shift ;;
        --bez-bazy)   BAZA=0; shift ;;
        -h|--help)    pomoc; exit 0 ;;
        *) ostrzez "nieznany argument: $1"; pomoc >&2; exit 2 ;;
    esac
done

if [[ -n "$INSTANCJA" && ! "$INSTANCJA" =~ ^[a-z0-9-]+$ ]]; then
    ostrzez "nazwa instancji może mieć tylko małe litery, cyfry i myślnik: '$INSTANCJA'"
    ostrzez "wchodzi do nazw procesów PM2 i do nazwy bazy."
    exit 2
fi

# Instancja testowa dostaje porty przesunięte o 10, żeby stanęła obok
# produkcyjnej bez pytania o nic. Produkcyjna zostaje na 8002/6200 — te liczby
# są w nginxie, w panel/.env.example i w README, i nie ma powodu ich ruszać.
NAZWA="laweta${INSTANCJA:+-$INSTANCJA}"
if [[ -n "$INSTANCJA" ]]; then
    DOMYSLNY_API=8012
    DOMYSLNY_PANEL=6210
    DB_NAME="laweta_${INSTANCJA//-/_}"
    DB_USER="laweta_${INSTANCJA//-/_}"
else
    DOMYSLNY_API=8002
    DOMYSLNY_PANEL=6200
    DB_NAME="laweta"
    DB_USER="laweta"
fi

z_env() {   # klucz -> wartość albo pusto
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" laweta_radar/.env 2>/dev/null \
        | tail -1 | sed 's/[[:space:]]*#.*$//; s/^["'\'']//; s/["'\'']$//'
}

# Na maszynie z kilkunastoma usługami zajęty port objawia się jako proces, który
# wstaje i pada w pętli PM2 — bez żadnego objawu na zewnątrz poza tym, że panel
# nie odpowiada. Taniej sprawdzić to teraz niż czytać potem logi.
port_zajety() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltnH 2>/dev/null | grep -qE "[:.]$1[[:space:]]"
    else
        # Bez iproute2: próba połączenia. Odpowiada ktoś = port zajęty.
        (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3<&- && return 0
        return 1
    fi
}

wolny_port() {   # od którego zacząć
    local p="$1"
    for _ in $(seq 1 60); do
        port_zajety "$p" || { echo "$p"; return 0; }
        p=$((p + 1))
    done
    echo "$1"   # nic wolnego w 60 próbach — oddaj wyjściowy, zgłosi się przy starcie
}

# Kolejność jest istotna: jawny argument > port zapisany w .env > pierwszy wolny.
# Port z .env jest NASZ (zapisało go poprzednie uruchomienie) i nie wolno go
# przesuwać po cichu — siedzi w nginxie i w panel/.env.local.
API_PORT="${API_PORT:-$(z_env API_PORT)}"
PANEL_PORT="${PANEL_PORT:-$(z_env PANEL_PORT)}"
API_PORT="${API_PORT:-$(wolny_port $DOMYSLNY_API)}"
PANEL_PORT="${PANEL_PORT:-$(wolny_port $DOMYSLNY_PANEL)}"
[[ "$API_PORT" == "$DOMYSLNY_API" ]] || log "Port API: $DOMYSLNY_API zajęty -> biorę $API_PORT"
[[ "$PANEL_PORT" == "$DOMYSLNY_PANEL" ]] || log "Port panelu: $DOMYSLNY_PANEL zajęty -> biorę $PANEL_PORT"

# Port zapisany w .env, trzymany przez KOGOŚ INNEGO (nasz proces nie chodzi) —
# to jedyny przypadek, w którym cicha zmiana byłaby gorsza od głośnego pytania.
if command -v pm2 >/dev/null 2>&1; then
    for rola in api panel; do
        port="$([[ $rola == api ]] && echo "$API_PORT" || echo "$PANEL_PORT")"
        if port_zajety "$port" && ! pm2 describe "$NAZWA-$rola" >/dev/null 2>&1; then
            ostrzez "port $port ($rola) jest zajęty, a proces $NAZWA-$rola nie chodzi."
            ostrzez "wskaż inny:  ./setup.sh ${INSTANCJA:+--instancja $INSTANCJA }--port-$rola INNY_PORT"
            BLEDY=1
        fi
    done
fi

log "Instancja:  ${INSTANCJA:-produkcyjna}  (procesy: $NAZWA-api, $NAZWA-bot, $NAZWA-panel)"
log "Katalog:    $ROOT_DIR"
log "Porty:      API $API_PORT, panel $PANEL_PORT"
log "Baza:       $DB_NAME (rola $DB_USER)"
echo

# --- 1. Czego brakuje na maszynie -------------------------------------------

# Wszystkie braki naraz, nie pierwszy z brzegu: instalowanie ich po jednym to
# pięć rund `./setup.sh` przerywanych ściąganiem pakietów.
BRAKI=()
PY_SYS=""
for kandydat in python3.11 python3.12 python3; do
    if command -v "$kandydat" >/dev/null 2>&1; then
        WER="$("$kandydat" -c 'import sys; print(f"{sys.version_info[0]}{sys.version_info[1]:02d}")' 2>/dev/null || echo 0)"
        if [[ "$WER" -ge 311 ]]; then
            PY_SYS="$kandydat"
            break
        fi
    fi
done
# Podpowiedź jest DWUCZĘŚCIOWA, bo nazwa pakietu zależy od wydania: na 24.04
# i nowszych `python3` to już 3.11+, a `python3.11` NIE ISTNIEJE jako pakiet
# (apt przerywa wtedy całą komendę i nie instaluje też reszty z listy).
# Na 22.04 domyślny python3 to 3.10 i wtedy trzeba wersji jawnie.
[[ -n "$PY_SYS" ]] || BRAKI+=("python3.11+   ->  sudo apt install -y python3 python3-venv
                       (Ubuntu 22.04:  sudo apt install -y python3.11 python3.11-venv)")

command -v git >/dev/null 2>&1 || BRAKI+=("git           ->  sudo apt install -y git")
command -v psql >/dev/null 2>&1 || BRAKI+=("psql          ->  sudo apt install -y postgresql postgresql-client")
command -v pm2 >/dev/null 2>&1 || BRAKI+=("pm2           ->  sudo npm install -g pm2")
if [[ $PANEL -eq 1 ]]; then
    if command -v node >/dev/null 2>&1; then
        WER_NODE="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
        [[ "$WER_NODE" -ge 20 ]] || BRAKI+=("node 20+ (jest $WER_NODE)  ->  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs")
    else
        BRAKI+=("node 20+      ->  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs")
    fi
fi

if [[ ${#BRAKI[@]} -gt 0 ]]; then
    ostrzez "brakuje na maszynie:"
    for b in "${BRAKI[@]}"; do echo "        $b" >&2; done
    ostrzez "doinstaluj i puść ponownie — setup.sh nie zmienia stanu maszyny sam."
    exit 1
fi
log "Narzędzia: komplet (python: $PY_SYS)."

# --- 2. venv + zależności ----------------------------------------------------

if [[ ! -x venv/bin/python3 ]]; then
    log "Tworzę venv…"
    "$PY_SYS" -m venv venv
fi
log "Zależności Pythona…"
venv/bin/python3 -m pip install --quiet --upgrade pip
venv/bin/python3 -m pip install --quiet -r laweta_radar/requirements.txt

# --- 3. .env -----------------------------------------------------------------

# Wartości zapisujemy przez podmianę linii w skopiowanym .env.example, a nie
# przez wygenerowanie własnego pliku: w przykładzie jest kilkanaście ekranów
# komentarzy tłumaczących KAŻDĄ zmienną, i to one są tu najcenniejsze.
ustaw_env() {   # klucz, wartość
    local k="$1" v="$2" plik=laweta_radar/.env
    if grep -qE "^[[:space:]]*$k[[:space:]]*=" "$plik"; then
        sed -i "s|^[[:space:]]*$k[[:space:]]*=.*|$k=$v|" "$plik"
    else
        printf '%s=%s\n' "$k" "$v" >> "$plik"
    fi
}
NOWY_ENV=0
if [[ ! -f laweta_radar/.env ]]; then
    cp laweta_radar/.env.example laweta_radar/.env
    chmod 600 laweta_radar/.env          # są w nim klucze API i hasło do bazy
    NOWY_ENV=1
    log "Utworzyłem laweta_radar/.env ze wzoru."
else
    log "laweta_radar/.env już jest — NIE nadpisuję, uzupełniam tylko braki."
fi

ustaw_env INSTANCJA "$INSTANCJA"
ustaw_env API_PORT "$API_PORT"
ustaw_env PANEL_PORT "$PANEL_PORT"

# Token panelu generujemy, bo to jedyny sekret, którego nie trzeba skądś wziąć —
# a puste API_TOKEN znaczy panel bez dostępu do danych i pół godziny szukania.
if [[ -z "$(z_env API_TOKEN)" ]]; then
    ustaw_env API_TOKEN "$(venv/bin/python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
    log "Wygenerowałem API_TOKEN."
fi

DSN="$(z_env DATABASE_URL)"
if [[ $BAZA -eq 1 && ( $NOWY_ENV -eq 1 || "$DSN" == *"haslo@localhost"* ) ]]; then
    DB_PASS="$(venv/bin/python3 -c 'import secrets;print(secrets.token_urlsafe(18))')"
    DSN="postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME"
    ustaw_env DATABASE_URL "$DSN"
    log "Ustawiłem DATABASE_URL na $DB_NAME (hasło wygenerowane)."
else
    DB_PASS=""
    log "DATABASE_URL zostawiam jak jest."
fi

# Panel czyta JEDNĄ zmienną i musi ją znać przy BUILDZIE: `next build` zapieka
# rewrite /api/* do routes-manifest.json. Ustawienie jej dopiero przy starcie
# znaczy panel pukający na 8002, gdy API stoi na 8012 — czyli pusty ekran.
if [[ ! -f panel/.env.local ]]; then
    cp panel/.env.example panel/.env.local
fi
sed -i "s|^[[:space:]]*LAWETA_API_URL[[:space:]]*=.*|LAWETA_API_URL=http://127.0.0.1:$API_PORT|" panel/.env.local
grep -q '^LAWETA_API_URL=' panel/.env.local || printf 'LAWETA_API_URL=http://127.0.0.1:%s\n' "$API_PORT" >> panel/.env.local

for zmienna in ANTHROPIC_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID SHARED_ENV_PATH; do
    [[ -z "$(z_env "$zmienna")" ]] && DO_UZUPELNIENIA+=("$zmienna")
done

# --- 4. Baza -----------------------------------------------------------------

if [[ $BAZA -eq 1 ]]; then
    # Do CREATE ROLE/DATABASE trzeba superusera. Na Ubuntu wchodzi się na niego
    # przez peer auth (`sudo -u postgres`), nie hasłem — i tylko tak próbujemy.
    if sudo -n -u postgres psql -tAc 'SELECT 1' >/dev/null 2>&1; then
        if [[ -n "$DB_PASS" ]]; then
            sudo -u postgres psql -qtAc \
                "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1 \
                && sudo -u postgres psql -qc "ALTER ROLE $DB_USER LOGIN PASSWORD '$DB_PASS'" >/dev/null \
                || sudo -u postgres psql -qc "CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS'" >/dev/null
            log "Rola $DB_USER gotowa."
        fi
        if ! sudo -u postgres psql -qtAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
            sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
            log "Utworzyłem bazę $DB_NAME (właściciel: $DB_USER)."
        else
            log "Baza $DB_NAME już jest."
        fi
    else
        ostrzez "brak dostępu do superusera Postgresa (sudo -u postgres) — bazy nie utworzę."
        ostrzez "zrób to ręcznie i puść ponownie:"
        echo "        sudo -u postgres psql -c \"CREATE ROLE $DB_USER LOGIN PASSWORD 'WYMYSL_HASLO'\"" >&2
        echo "        sudo -u postgres createdb -O $DB_USER $DB_NAME" >&2
        echo "        # potem wpisz to hasło do DATABASE_URL w laweta_radar/.env" >&2
        BLEDY=1
    fi
fi

# Migracje idą z DATABASE_URL, bo w tym układzie rola JEST właścicielem bazy,
# więc ma DDL na własnych tabelach. Na współdzielonej produkcji README każe
# podać DATABASE_URL_ADMIN jawnie — i tam ta zasada dalej obowiązuje.
if [[ -n "$(z_env DATABASE_URL)" ]]; then
    log "Migracje…"
    if ! bash laweta_radar/scripts/migrate.sh; then
        ostrzez "migracje nie przeszły — API wstanie, ale zapyta o tabele przy pierwszym zapytaniu."
        BLEDY=1
    fi
fi

# --- 5. Testy offline --------------------------------------------------------

# Bez sieci i bez bazy. Odpalamy je TUTAJ, bo czerwony pytest na świeżym klonie
# znaczy zepsute środowisko, a nie zepsuty deploy — i lepiej wiedzieć to teraz
# niż tłumaczyć sobie później dziwne zachowanie fetchera.
log "Testy offline…"
if venv/bin/python3 -m pytest laweta_radar/tests -q 2>&1 | tail -3; then
    :
else
    ostrzez "testy offline NIE przeszły — patrz wyżej."
    BLEDY=1
fi

# --- 6. Panel ----------------------------------------------------------------

if [[ $PANEL -eq 1 ]]; then
    log "Panel: npm ci…"
    (cd panel && npm ci)
    log "Panel: npm run build…"
    (cd panel && PANEL_PORT="$PANEL_PORT" npm run build)
fi

# --- 7. PM2 ------------------------------------------------------------------

log "Podnoszę procesy…"
if pm2 describe "$NAZWA-api" >/dev/null 2>&1; then
    INSTANCJA="$INSTANCJA" pm2 restart ecosystem.config.js >/dev/null
else
    INSTANCJA="$INSTANCJA" pm2 start ecosystem.config.js >/dev/null
fi
pm2 save >/dev/null 2>&1 || true
pm2 list | grep -E "^\s*│?\s*[0-9]|$NAZWA" | head -10 || true

# --- 8. Czy wstało -----------------------------------------------------------

czekaj_na() {
    local url="$1"
    for _ in $(seq 1 30); do
        curl -sf -o /dev/null --max-time 2 "$url" && return 0
        sleep 1
    done
    return 1
}

echo
if czekaj_na "http://127.0.0.1:$API_PORT/health"; then
    ZDROWIE="$(curl -s --max-time 3 "http://127.0.0.1:$API_PORT/health" || true)"
    STATUS="$(sed -n 's/.*"status": *"\([^"]*\)".*/\1/p' <<<"$ZDROWIE")"
    log "API na :$API_PORT — ${STATUS:-odpowiada}"
else
    ostrzez "API nie wstało na :$API_PORT — pm2 logs $NAZWA-api"
    BLEDY=1
fi

if [[ $PANEL -eq 1 ]]; then
    if czekaj_na "http://127.0.0.1:$PANEL_PORT/"; then
        log "Panel na :$PANEL_PORT — odpowiada"
    else
        ostrzez "panel nie wstał na :$PANEL_PORT — pm2 logs $NAZWA-panel"
        BLEDY=1
    fi
fi

# --- 9. Co zostało do zrobienia ręcznie --------------------------------------

echo
log "================ CO ZOSTAŁO ================"
if [[ ${#DO_UZUPELNIENIA[@]} -gt 0 ]]; then
    log "Uzupełnij w laweta_radar/.env  (nano laweta_radar/.env):"
    for z in "${DO_UZUPELNIENIA[@]}"; do
        case "$z" in
            ANTHROPIC_API_KEY)  log "  ANTHROPIC_API_KEY  — bez niego klasyfikator nie ruszy" ;;
            TELEGRAM_BOT_TOKEN) log "  TELEGRAM_BOT_TOKEN — token od @BotFather" ;;
            TELEGRAM_CHAT_ID)   log "  TELEGRAM_CHAT_ID   — ID czatu operatora (bot musi tam być)" ;;
            SHARED_ENV_PATH)    log "  SHARED_ENV_PATH    — .env sales-core-engine (klucze Apify); zwykle /home/ubuntu/sales-core-engine/.env" ;;
        esac
    done
    # --bez-panelu, bo panel nie czyta laweta_radar/.env — przebudowa po wklejeniu
    # kluczy to trzy minuty za nic.
    log "Potem:  ./update.sh --force --bez-panelu     (przeładuje API i bota z nową konfiguracją)"
else
    log "Konfiguracja kompletna."
fi

echo
log "Token panelu (wklejasz raz w aplikacji): $(z_env API_TOKEN)"
log "Pełna diagnostyka:  bash laweta_radar/scripts/check_setup.sh"
log "Fetcher NA SUCHO (nic nie wydaje):  venv/bin/python -m laweta_radar.workers.fb_fetcher --sucho"
echo
log "CRON fetchera NIE został zainstalowany — kosztuje kredyt Apify i wysyła"
log "alerty. Gdy instancja ma zbierać naprawdę, wklej (crontab -e):"
echo "        */5 * * * * cd $ROOT_DIR && ./venv/bin/python -m laweta_radar.workers.fb_fetcher >> /var/log/laweta/fetcher.log 2>&1"

if [[ $BLEDY -ne 0 ]]; then
    echo
    ostrzez "zakończone Z BŁĘDAMI — patrz wyżej."
    exit 1
fi
echo
log "Gotowe. Aktualizacje: ./update.sh"
