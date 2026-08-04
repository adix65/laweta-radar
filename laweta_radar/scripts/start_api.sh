#!/usr/bin/env bash
# Wejście dla PM2 (patrz ecosystem.config.js w katalogu repo).
#
# PM2 startuje procesy ze swojego środowiska, a nie z powłoki logowania, więc
# .env trzeba wczytać JAWNIE — inaczej API wstaje bez DATABASE_URL i odpowiada
# "niepelna_konfiguracja" na maszynie, na której wszystko jest ustawione.
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

# --host 127.0.0.1 NA SZTYWNO. Endpointy z danymi mają token w nagłówku
# (`API_TOKEN`), ale to jest DRUGA warstwa, nie jedyna: na zewnątrz wystawia to
# nginx z TLS-em i to on odpowiada za dostęp. Nasłuch na 0.0.0.0 wystawiłby bazę
# zleceń — łącznie z numerami telefonów obcych ludzi z grup FB — wprost do
# internetu, niezależnie od tokenu.
PY="${PYTHON_BIN:-$ROOT_DIR/venv/bin/python3}"
exec "$PY" -m uvicorn laweta_radar.api.main:app --host 127.0.0.1 --port 8002
