#!/usr/bin/env bash
# Jedno polecenie odpowiadające na pytanie "czego jeszcze brakuje, żeby to ruszyło".
#
# Kolejność sekcji jest kolejnością ZALEŻNOŚCI: konfiguracja -> baza -> klucze
# Apify -> proxy -> Telegram. Pierwsza czerwona sekcja jest tą, którą trzeba
# naprawić — dalsze i tak nie zadziałają, więc nie ma sensu ich czytać.
#
# UŻYCIE:  bash laweta_radar/scripts/check_setup.sh
# Nie odpytuje sieci poza testem Telegrama (jedna wiadomość) — reszta czyta
# konfigurację i bazę.
set -uo pipefail   # BEZ -e: chcemy dojechać do końca i pokazać WSZYSTKIE problemy

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_DIR="$(cd "$REPO_DIR/.." && pwd)"
export PYTHONPATH="$ROOT_DIR"
cd "$ROOT_DIR"
PY="${PYTHON_BIN:-python3}"

if [[ ! -f "$REPO_DIR/.env" ]]; then
    echo "!! Brak $REPO_DIR/.env — skopiuj wzór:"
    echo "   cp $REPO_DIR/.env.example $REPO_DIR/.env"
    exit 0
fi

echo "=== 1. Konfiguracja (.env) ==="
"$PY" -m laweta_radar.config.settings

echo
echo "=== 2. Grupy FB ==="
"$PY" -m laweta_radar.config.groups

echo
echo "=== 3. Klucze Apify (rotacja) ==="
"$PY" -m laweta_radar.workers.apify_keys

echo
echo "=== 4. Proxy dla Apify (bez sieci) ==="
"$PY" -m laweta_radar.workers.apify_proxy

echo
echo "=== 5. Telegram (WYSYŁA wiadomość testową) ==="
"$PY" -m laweta_radar.services.telegram_notify

echo
echo "=== 6. Baza (wymaga wstającego API albo psql) ==="
echo "Migracje:  bash laweta_radar/scripts/migrate.sh"
echo "Stan:      curl -s localhost:8002/health"
echo
echo "Realne adresy wyjściowe proxy (wymaga sieci):"
echo "  $PY -m laweta_radar.workers.apify_proxy --check --limit 10"
