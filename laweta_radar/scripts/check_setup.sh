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
echo
echo "=== 3. Wspólna pula Apify (z .env sales-core-engine) ==="
# Klucze i proxy NIE należą do tego repo — przychodzą ze wspólnego pliku.
# „0 zmiennych" tutaj wyjaśnia z góry każde „brak kluczy Apify" niżej.
"$PY" -c "
from laweta_radar.config import settings as s
sciezka = s.sciezka_wspolnego_env()
print(f'  plik:     {sciezka or \"NIE ZNALEZIONO (ustaw SHARED_ENV_PATH)\"}')
print(f'  wczytano: {s.WSPOLNE_APIFY_ILE} zmiennych APIFY_*')
print('  uwaga:    APIFY_PROXY_POOL* jest świadomie NIE dziedziczone — pula ma być wyłączona')
"

echo
echo "=== 4. Klucze Apify (rotacja) ==="
"$PY" -m laweta_radar.workers.apify_keys

echo
echo "=== 5. Proxy dla Apify (bez sieci) ==="
"$PY" -m laweta_radar.workers.apify_proxy

echo
echo "=== 6. Klasyfikator (provider modelu, bez sieci) ==="
# Brak paczki providera to awaria CICHA: system wstaje, a modelu nie woła.
# Ma się objawić tutaj, a nie tracebackiem w środku runu o trzeciej w nocy.
"$PY" -m laweta_radar.services.llm

echo
echo "=== 7. Baza kodów pocztowych (services/geo.py, bez sieci) ==="
"$PY" -c "
from laweta_radar.services import geo
print(geo.stan_bazy())
p = geo.baza()
print(f'  baza operatora: {p.nazwa} ({p.wspolrzedne()})')
"

echo
echo "=== 8. Telegram (WYSYŁA wiadomość testową) ==="
"$PY" -m laweta_radar.services.telegram_notify

echo
echo "=== 9. Baza danych (wymaga wstającego API albo psql) ==="
echo "Migracje:  bash laweta_radar/scripts/migrate.sh"
echo "Stan:      curl -s localhost:8002/health"
echo
echo "Realne adresy wyjściowe proxy (wymaga sieci):"
echo "  $PY -m laweta_radar.workers.apify_proxy --check --limit 10"
