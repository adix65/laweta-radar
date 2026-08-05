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
# Venv repo, tak samo jak start_api.sh i start_bot.sh. Systemowy python3 nie ma
# zależności projektu, więc diagnostyka kończyła się ścianą
# `ModuleNotFoundError: No module named 'dotenv'` — czyli narzędzie od
# odpowiadania "czego brakuje" samo wyglądało na zepsute.
PY="${PYTHON_BIN:-$ROOT_DIR/venv/bin/python3}"
if [[ ! -x "$PY" ]]; then
    echo "!! Brak $PY — używam systemowego python3."
    echo "   Zależności projektu są w venv: ./setup.sh albo python3 -m venv venv"
    echo
    PY=python3
fi

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
echo "=== 7. Klasyfikator (provider modelu, bez sieci) ==="
# Brak paczki providera to awaria CICHA: fetcher wstaje, posty przechodzą przez
# bramkę i czekają w bazie, a nikt nie dostaje alertu. Ma się to objawić tutaj,
# a nie tracebackiem w środku przebiegu o trzeciej w nocy.
"$PY" -m laweta_radar.services.llm
# Tego, czy klucz jest DOBRY i czy nazwa modelu ISTNIEJE, nie da się sprawdzić
# bez sieci — dlatego to osobne polecenie, a nie kolejna sekcja tutaj.
echo "  Czy klucz i nazwa modelu realnie działają (jedno wywołanie na providera):"
echo "    $PY laweta_radar/scripts/test_llm.py"

echo
echo "=== 8. Baza kodów pocztowych (services/geo.py, bez sieci) ==="
"$PY" -c "
from laweta_radar.services import geo
print(geo.stan_bazy())
p = geo.baza()
print(f'  baza operatora: {p.nazwa} ({p.wspolrzedne()})')
"

echo
echo "=== 9. Fetcher — plan i koszt NAJBLIŻSZEGO przebiegu (nic nie wydaje) ==="
# Pierwsza linia mówi, na której ścieżce (A/B) stoi fetcher. „domyślna,
# ostrożna" znaczy, że pomiar actora NIE został wykonany — patrz
# docs/POMIAR-ACTORA.md i README, sekcja „Budżet liczy się w POSTACH".
"$PY" -m laweta_radar.workers.fb_fetcher --sucho 2>&1 | sed 's/^/  /'

echo
echo "=== 10. Telegram (WYSYŁA wiadomość testową) ==="
"$PY" -m laweta_radar.services.telegram_notify

echo
echo "=== 11. Baza danych (wymaga wstającego API albo psql) ==="
# Port z .env, nie na sztywno: instancja testowa stoi na 8012 i podpowiedź
# z 8002 wysyłałaby operatora pod API produkcyjne (albo w pustkę).
PORT_API="$(sed -n 's/^[[:space:]]*API_PORT[[:space:]]*=[[:space:]]*//p' "$REPO_DIR/.env" 2>/dev/null \
            | tail -1 | sed 's/[[:space:]]*#.*$//')"
echo "Migracje:  bash laweta_radar/scripts/migrate.sh"
echo "Stan:      curl -s localhost:${PORT_API:-8002}/health"
echo
echo "Realne adresy wyjściowe proxy (wymaga sieci):"
echo "  $PY -m laweta_radar.workers.apify_proxy --check --limit 10"
