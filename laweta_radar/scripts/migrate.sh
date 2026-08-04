#!/usr/bin/env bash
# Odpal migracje SQL po kolei, jako rola z prawami DDL.
#
# DLACZEGO OSOBNO, a nie z workera: worker chodzi z crona co kilka minut, na
# cudzych danych, i nie ma powodu móc zmieniać schematu ani go skasować. Tworzenie
# tabel "przy okazji pierwszego uruchomienia" znaczy też, że schemat zmienia się
# w momencie deployu kodu — po cichu i bez możliwości sprawdzenia, co dokładnie
# poszło. Migracje odpala CZŁOWIEK, świadomie, i widzi wynik każdej.
#
# UŻYCIE:
#   DATABASE_URL_ADMIN="postgresql://postgres@localhost/laweta" \
#     bash laweta_radar/scripts/migrate.sh
#
# Bez DATABASE_URL_ADMIN skrypt spada na DATABASE_URL z .env — wygodne lokalnie,
# gdzie i tak jesteś właścicielem bazy. Na produkcji podaj admina JAWNIE: jeśli
# DATABASE_URL workera ma prawa DDL, to znaczy, że rozdział ról nie istnieje.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIGRATIONS_DIR="$REPO_DIR/api/migrations"

if [[ -f "$REPO_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_DIR/.env"
    set +a
fi

DSN="${DATABASE_URL_ADMIN:-${DATABASE_URL:-}}"
if [[ -z "$DSN" ]]; then
    # Czyste wyjście z komunikatem — ta sama zasada co w workerach.
    echo "[migrate] Brak DATABASE_URL_ADMIN i DATABASE_URL — nie wiem, gdzie migrować." >&2
    echo "[migrate] Ustaw jedno z nich w $REPO_DIR/.env (wzór: .env.example)." >&2
    exit 0
fi

shopt -s nullglob
FILES=("$MIGRATIONS_DIR"/*.sql)
if [[ ${#FILES[@]} -eq 0 ]]; then
    echo "[migrate] Brak plików .sql w $MIGRATIONS_DIR — nie ma czego odpalać."
    exit 0
fi

# Sortowanie po nazwie = kolejność numerów w prefiksie (0001_, 0002_, ...).
# Dlatego numer jest obowiązkowy: migracje dokładają kolumny do tabel z migracji
# wcześniejszych i puszczone w złej kolejności po prostu nie przejdą.
IFS=$'\n' FILES=($(sort <<<"${FILES[*]}")); unset IFS

echo "[migrate] Baza: ${DSN%%\?*}"   # bez parametrów połączenia; hasło i tak jest w URL-u wyżej
echo "[migrate] Do odpalenia: ${#FILES[@]} migracji"
for f in "${FILES[@]}"; do
    echo "[migrate] --> $(basename "$f")"
    # ON_ERROR_STOP=1: bez tego psql leci dalej po błędzie i kończy z kodem 0,
    # czyli nieudana migracja wygląda w skrypcie deploya na udaną.
    psql "$DSN" -v ON_ERROR_STOP=1 -q -f "$f"
done
echo "[migrate] Gotowe. Pamiętaj o GRANT-ach dla roli workerów (komentarz na końcu migracji)."
