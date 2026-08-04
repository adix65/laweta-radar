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
echo "=== 6. Bramka słowna (PL/DE/CS/SK, bez sieci) ==="
# Niemiecki post to test wielojęzyczności: przy bramce jednojęzycznej dostałby
# zero punktów i wyleciał — i wyglądałoby to w logach jak odrzucona reklama.
"$PY" -m laweta_radar.workers.gate "Suche Abschleppdienst, Motor kaputt" \
    | sed 's/^/  /' || true

echo
echo "=== 7. Fetcher — plan i koszt NAJBLIŻSZEGO przebiegu (nic nie wydaje) ==="
# Pierwsza linia mówi, na której ścieżce (A/B) stoi fetcher. „domyślna,
# ostrożna" znaczy, że pomiar actora NIE został wykonany — patrz
# docs/POMIAR-ACTORA.md i README, sekcja „Budżet liczy się w POSTACH".
"$PY" -m laweta_radar.workers.fb_fetcher --sucho 2>&1 | sed 's/^/  /'

echo
echo "=== 8. Telegram (WYSYŁA wiadomość testową) ==="
"$PY" -m laweta_radar.services.telegram_notify

echo
echo "=== 9. Baza (wymaga wstającego API albo psql) ==="
echo "Migracje:  bash laweta_radar/scripts/migrate.sh"
echo "Stan:      curl -s localhost:8002/health"
echo
echo "Realne adresy wyjściowe proxy (wymaga sieci):"
echo "  $PY -m laweta_radar.workers.apify_proxy --check --limit 10"
