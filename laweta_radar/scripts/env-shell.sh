#!/usr/bin/env bash
# Wczytaj .env do BIEŻĄCEJ powłoki — do ręcznego odpalania narzędzi CLI.
#
# DLACZEGO osobny skrypt: workery czytają .env same (python-dotenv), ale narzędzia
# diagnostyczne odpala się z ręki i wtedy powłoka nic o nim nie wie. Bez tego
# `python -m laweta_radar.workers.apify_proxy --check` pokazuje "brak kluczy"
# na maszynie, na której klucze są — i traci się kwadrans na szukanie nieistniejącego
# problemu.
#
# UŻYCIE (kropka albo `source` — inaczej zmienne zginą z podpowłoką):
#   source laweta_radar/scripts/env-shell.sh
#
# `set -a` eksportuje wszystko, co przypisane niżej; `set +a` to wyłącza, żeby
# reszta sesji zachowywała się normalnie.
set -a
# shellcheck disable=SC1091  # ścieżka liczona w czasie działania
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env"
set +a

# PYTHONPATH na KATALOG REPO (nie na pakiet) — importy mają postać
# `laweta_radar.workers.X`, więc na ścieżce musi leżeć katalog NADRZĘDNY.
PYTHONPATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONPATH

echo "[env-shell] .env wczytany, PYTHONPATH=$PYTHONPATH"
