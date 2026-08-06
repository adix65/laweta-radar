#!/usr/bin/env bash
# Instaluje DWA wpisy crona dla darmowej puli proxy (sekcja 2 zadania „większa
# pula proxy"): PEŁNE odświeżenie co 2h i SZYBKA KONTROLA co 15 min.
#
# DLACZEGO DWA POZIOMY. Darmowe proxy padają w MINUTACH, nie godzinach — sam
# cron co 2h zostawiałby konta na martwych adresach przez większość tego czasu.
# SZYBKA KONTROLA (`odswiez_proxy.py --tylko-pula`) sprawdza WYŁĄCZNIE adresy
# już w puli (bez pobierania źródeł — tania, kilkanaście-kilkadziesiąt adresów),
# wyrzuca martwe i — jeśli po czyszczeniu zostało mniej niż liczba kluczy —
# odpala PEŁNE odświeżenie OD RAZU, nie czekając na następny cykl dwugodzinny.
# Nieudane odświeżenie (żadne z dwóch) nie kasuje poprzedniej puli — patrz
# docstring `odswiez_proxy.py`.
#
# IDEMPOTENTNY. Oba wpisy są oznaczone znacznikiem w komentarzu na końcu linii
# (# laweta-proxy-pelne / # laweta-proxy-szybka) — puszczony drugi raz PODMIENIA
# stare wpisy (np. po przeniesieniu repo), zamiast dokładać kolejne kopie.
# Reszta crontaba (fetcher, cudze zadania) zostaje nietknięta co do linii.
#
# WOŁANE AUTOMATYCZNIE przez setup.sh/update.sh, gdy laweta_radar/.env ma
# APIFY_PROXY_POOL=1 — w PRZECIWIEŃSTWIE do crona FETCHERA (ten instaluje się
# tylko ręcznie, patrz setup.sh). Różnica jest bezpieczna do automatyzacji:
# SZYBKA KONTROLA sama nie dotyka Apify (tylko test proxy -> ipify/Apify TLS
# handshake, bez tokenu), a PEŁNE odświeżenie bez APIFY_PROXY_POOL=1 tylko
# odświeża plik, którego worker i tak jeszcze nie czyta.
#
# UŻYCIE:
#   bash laweta_radar/scripts/setup_cron.sh            # zainstaluj / zaktualizuj
#   bash laweta_radar/scripts/setup_cron.sh --usun      # zdejmij oba wpisy
#   bash laweta_radar/scripts/setup_cron.sh --pokaz     # pokaż wpisy, nic nie zmieniaj
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

log() { echo "[cron-proxy] $*"; }
ostrzez() { echo "[cron-proxy] !! $*" >&2; }

TRYB="${1:-}"

VENV_PY="$REPO_DIR/venv/bin/python"
[[ -x "$VENV_PY" ]] || VENV_PY="python3"

# /var/log/laweta jest już konwencją tego repo (fetcher, patrz setup.sh) — trzymamy
# się jej, ale bez prawa zapisu (VPS bez sudo, kontener testowy) spadamy na katalog
# w repo, zamiast ciszej awarii, w której cron chodzi, a logów nigdzie nie ma.
LOG="/var/log/laweta/proxy.log"
if ! mkdir -p "$(dirname "$LOG")" 2>/dev/null || [[ ! -w "$(dirname "$LOG")" ]]; then
    LOG="$REPO_DIR/laweta_radar/logs/proxy.log"
    mkdir -p "$(dirname "$LOG")"
    ostrzez "brak zapisu do /var/log/laweta — logi crona proxy pójdą do $LOG"
fi

ZNACZNIK_PELNE="laweta-proxy-pelne"
ZNACZNIK_SZYBKA="laweta-proxy-szybka"

WPIS_PELNE="0 */2 * * * cd $REPO_DIR && $VENV_PY laweta_radar/scripts/odswiez_proxy.py >> $LOG 2>&1 # $ZNACZNIK_PELNE"
WPIS_SZYBKA="*/15 * * * * cd $REPO_DIR && $VENV_PY laweta_radar/scripts/odswiez_proxy.py --tylko-pula >> $LOG 2>&1 # $ZNACZNIK_SZYBKA"

if [[ "$TRYB" == "--pokaz" ]]; then
    log "PEŁNE (co 2h):        $WPIS_PELNE"
    log "SZYBKA KONTROLA (15min): $WPIS_SZYBKA"
    exit 0
fi

if ! command -v crontab >/dev/null 2>&1; then
    ostrzez "brak polecenia 'crontab' na tej maszynie — nie instaluję nic."
    ostrzez "Wklej ręcznie (crontab -e):"
    ostrzez "  $WPIS_PELNE"
    ostrzez "  $WPIS_SZYBKA"
    exit 0
fi

# Zdejmujemy STARE wpisy po znaczniku (jeśli są) — `grep -v` na pustym crontabie
# (`crontab -l` bez zainstalowanego crona kończy się niezerowym kodem) nie ma
# prawa wywalić skryptu, stąd `|| true`.
STARY="$(crontab -l 2>/dev/null || true)"
BEZ_NASZYCH="$(printf '%s\n' "$STARY" | grep -vF "$ZNACZNIK_PELNE" | grep -vF "$ZNACZNIK_SZYBKA" || true)"

if [[ "$TRYB" == "--usun" ]]; then
    printf '%s\n' "$BEZ_NASZYCH" | sed '/^$/d' | crontab -
    log "Zdjęto oba wpisy (pełne odświeżenie + szybka kontrola). Reszta crontaba bez zmian."
    exit 0
fi

{ printf '%s\n' "$BEZ_NASZYCH"; echo "$WPIS_PELNE"; echo "$WPIS_SZYBKA"; } \
    | sed '/^$/d' | crontab -

log "Zainstalowano (albo zaktualizowano, jeśli już były):"
log "  PEŁNE odświeżenie co 2h:    $WPIS_PELNE"
log "  SZYBKA KONTROLA co 15 min:  $WPIS_SZYBKA"
log "Logi: $LOG"
log ""
log "Cron SAM NIE WŁĄCZA puli — worker użyje pliku dopiero po APIFY_PROXY_POOL=1"
log "w laweta_radar/.env (bezpiecznik: APIFY_PROXY_REQUIRED=1). Patrz docs/APIFY-PROXY.md."
log "Zdjęcie obu wpisów: bash laweta_radar/scripts/setup_cron.sh --usun"
