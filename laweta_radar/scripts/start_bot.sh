#!/usr/bin/env bash
# Wejście dla PM2 (patrz ecosystem.config.js w katalogu repo).
#
# Bot jest OSOBNYM procesem od API i od fetchera, i to jest decyzja, nie wygoda:
#   • API musi odpowiadać na żądania panelu — bot wisi na long pollingu i przez
#     30 sekund nic nie robi;
#   • fetcher chodzi z crona i kończy się po jednym przebiegu — a przycisk
#     „Śmieć" ma działać także wtedy, gdy fetchera akurat nie ma.
#
# PM2 startuje procesy ze swojego środowiska, a nie z powłoki logowania, więc
# .env trzeba wczytać JAWNIE — inaczej bot wstaje bez TELEGRAM_BOT_TOKEN
# i kończy się czystym wyjściem na maszynie, na której wszystko jest ustawione.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # .../laweta_radar
ROOT_DIR="$(cd "$REPO_DIR/.." && pwd)"                        # katalog repo

if [[ -f "$REPO_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_DIR/.env"
    set +a
fi

export PYTHONPATH="$ROOT_DIR"
cd "$ROOT_DIR"

PY="${PYTHON_BIN:-$ROOT_DIR/venv/bin/python3}"
exec "$PY" -m laweta_radar.workers.bot
