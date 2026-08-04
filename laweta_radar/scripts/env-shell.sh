#!/usr/bin/env bash
# Wczytaj .env do BIEŻĄCEJ powłoki — do ręcznego odpalania narzędzi CLI.
#
# DLACZEGO osobny skrypt: workery czytają .env same (python-dotenv), ale narzędzia
# w rodzaju psql odpala się z ręki i wtedy powłoka nic o nim nie wie.
#
# UŻYCIE (kropka albo `source` — inaczej zmienne zginą z podpowłoką):
#   source laweta_radar/scripts/env-shell.sh
#
# CZEGO TEN SKRYPT ŚWIADOMIE NIE ROBI: nie wciąga wspólnego .env sales-core-engine.
# Klucze Apify i proxy dociąga stamtąd `config/settings.py` przy imporcie pakietu
# (patrz laweta_radar/__init__.py), i robi to WYBIÓRCZO — tylko APIFY_*.
# Zassanie tamtego pliku do powłoki wciągnęłoby też jego DATABASE_URL, a wtedy
# `psql $DATABASE_URL` z tej sesji poszłoby do bazy sprzedażowej. Każde
# `python -m laweta_radar.*` widzi wspólne klucze bez tego kroku.

_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$_REPO/.env" ]]; then
    set -a
    # shellcheck disable=SC1091  # ścieżka liczona w czasie działania
    source "$_REPO/.env"
    set +a
    echo "[env-shell] wczytano $_REPO/.env"
else
    # Czyste wyjście z komunikatem — ta sama zasada co w workerach.
    echo "[env-shell] BRAK $_REPO/.env — skopiuj wzór:"
    echo "[env-shell]   cp $_REPO/.env.example $_REPO/.env"
fi

# PYTHONPATH na KATALOG REPO (nie na pakiet) — importy mają postać
# `laweta_radar.workers.X`, więc na ścieżce musi leżeć katalog NADRZĘDNY.
PYTHONPATH="$(cd "$_REPO/.." && pwd)"
export PYTHONPATH
echo "[env-shell] PYTHONPATH=$PYTHONPATH"
unset _REPO
