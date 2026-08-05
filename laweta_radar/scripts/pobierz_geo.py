#!/usr/bin/env python3
"""Pobierz bazę kodów pocztowych z GeoNames i scal do jednego data/kody_eu.csv.

PO CO TO ISTNIEJE: services/geo.py nie używa żadnego płatnego geokodera. 90%
przypadków to kod pocztowy albo nazwa miasta, a to załatwia lokalna baza — za
darmo, offline, w mikrosekundy, bez limitu zapytań i bez klucza, który może
wygasnąć w środku nocy. Ten skrypt tę bazę buduje.

WYNIK COMMITUJEMY DO REPO. Razem to kilkanaście MB i tak, to widać w `git
clone`. Alternatywą jest zależność od zewnętrznego hosta przy KAŻDYM deployu:
jeżeli download.geonames.org akurat nie odpowiada, świeży deploy wstaje bez
geokodera i po cichu przestaje pokazywać trasy. Rozmiar repo jest kosztem
jednorazowym, niedostępny host — powtarzalnym.

ŹRÓDŁO: GeoNames, licencja CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/).
Wymaga podania autorstwa — jest w data/README.md i tam ma zostać.

UŻYCIE:
    python laweta_radar/scripts/pobierz_geo.py --sucho     # plan, BEZ sieci
    python laweta_radar/scripts/pobierz_geo.py             # pobierz i scal
    python laweta_radar/scripts/pobierz_geo.py --kraje PL  # tylko Polska

Skrypt jest JEDNORAZOWY (albo raz na rok, gdy dojdą nowe kody) i nie jest
częścią pipeline'u — żaden worker go nie woła.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

KTO = "pobierz-geo"

# PL pierwsza, bo to 90% ruchu. Reszta to kierunki, w które realnie jeżdżą
# lawety z Podkarpacia — auta kupione w Niemczech i Holandii, tranzyt przez
# Czechy i Słowację, giełdy we Francji i Włoszech.
KRAJE_DOMYSLNE = ("PL", "DE", "CZ", "SK", "NL", "BE", "AT", "FR", "IT")

URL = "https://download.geonames.org/export/zip/{kraj}.zip"
TIMEOUT_S = 60

_ROOT = Path(__file__).resolve().parent.parent.parent
WYJSCIE_DOMYSLNE = _ROOT / "data" / "kody_eu.csv"

# Układ pliku GeoNames (TSV, bez nagłówka):
#   0 kraj, 1 kod, 2 miejscowość, 3 admin1(województwo), ..., 9 lat, 10 lng
KOL_KRAJ, KOL_KOD, KOL_MIEJSCOWOSC, KOL_ADMIN1, KOL_LAT, KOL_LNG = 0, 1, 2, 3, 9, 10

NAGLOWEK = ("kraj", "kod", "miejscowosc", "wojewodztwo", "lat", "lng")


def _wyjscie(komunikat: str) -> int:
    """Czyste zakończenie z komunikatem — ta sama zasada co w workerach."""
    print(f"[{KTO}] {komunikat}", file=sys.stderr)
    return 0


def pobierz(kraj: str) -> bytes:
    adres = URL.format(kraj=kraj)
    print(f"[{KTO}] pobieram {adres}")
    with urllib.request.urlopen(adres, timeout=TIMEOUT_S) as r:  # noqa: S310
        return r.read()


def rozpakuj(dane: bytes, kraj: str) -> list[list[str]]:
    """ZIP z GeoNames -> wiersze TSV. W środku jest {KRAJ}.txt i readme.txt."""
    with zipfile.ZipFile(io.BytesIO(dane)) as z:
        nazwa = next((n for n in z.namelist() if n.upper() == f"{kraj}.TXT"), None)
        if nazwa is None:
            raise ValueError(f"w archiwum {kraj}.zip nie ma pliku {kraj}.txt")
        tekst = z.read(nazwa).decode("utf-8")
    return [w.split("\t") for w in tekst.splitlines() if w.strip()]


def przetworz(wiersze: list[list[str]]) -> tuple[list[tuple], int]:
    """Surowe wiersze -> krotki w naszym formacie. Zwraca (wiersze, pominięte).

    Pomijamy wpisy bez współrzędnych. GeoNames ma ich trochę (kody skrytek
    pocztowych, kody wojskowe) i są bezużyteczne: kod bez lat/lng nie da się
    zamienić na punkt, a jego obecność w bazie tylko udaje trafienie.
    """
    wynik: list[tuple] = []
    pominiete = 0
    for w in wiersze:
        if len(w) <= KOL_LNG:
            pominiete += 1
            continue
        lat, lng = w[KOL_LAT].strip(), w[KOL_LNG].strip()
        if not lat or not lng:
            pominiete += 1
            continue
        try:
            float(lat), float(lng)
        except ValueError:
            pominiete += 1
            continue
        wynik.append((
            w[KOL_KRAJ].strip().upper(),
            w[KOL_KOD].strip(),
            w[KOL_MIEJSCOWOSC].strip(),
            w[KOL_ADMIN1].strip(),
            lat,
            lng,
        ))
    return wynik, pominiete


def zapisz(wiersze: list[tuple], sciezka: Path) -> None:
    """Zapis posortowany — żeby `git diff` po odświeżeniu bazy dało się czytać.

    Bez sortowania kolejność zależy od tego, co GeoNames akurat wygenerowało,
    i każde odświeżenie wyglądałoby na przepisanie całego pliku.
    """
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    tmp = sciezka.with_suffix(sciezka.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        pis = csv.writer(f)
        pis.writerow(NAGLOWEK)
        pis.writerows(sorted(set(wiersze)))
    # Podmiana atomowa: przerwany zapis nie może zostawić obciętego CSV, który
    # wygląda jak poprawna baza z brakującą połową Polski.
    tmp.replace(sciezka)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kraje", nargs="+", default=list(KRAJE_DOMYSLNE),
                    help=f"kody krajów do pobrania (domyślnie {' '.join(KRAJE_DOMYSLNE)})")
    ap.add_argument("--wyjscie", type=Path, default=WYJSCIE_DOMYSLNE,
                    help=f"plik wynikowy (domyślnie {WYJSCIE_DOMYSLNE})")
    ap.add_argument("--sucho", action="store_true",
                    help="pokaż plan i zakończ, bez pobierania")
    args = ap.parse_args(argv[1:])

    kraje = [k.strip().upper() for k in args.kraje if k.strip()]
    print(f"[{KTO}] Kraje: {', '.join(kraje)}")
    print(f"[{KTO}] Źródło: {URL.format(kraj='{KRAJ}')} (GeoNames, CC BY 4.0)")
    print(f"[{KTO}] Wynik:  {args.wyjscie}")
    print(f"[{KTO}] Rząd wielkości: ~1 MB dla PL, kilkanaście MB dla kompletu.")
    if args.sucho:
        print(f"[{KTO}] --sucho: kończę bez pobierania.")
        return 0

    wszystkie: list[tuple] = []
    for kraj in kraje:
        try:
            surowe = rozpakuj(pobierz(kraj), kraj)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # Brak sieci to nie awaria tego repo — to warunek zewnętrzny.
            # Kończymy CZYSTO, mówiąc co się stało i co dalej.
            return _wyjscie(f"nie udało się pobrać {kraj}: {e}. "
                            f"Sprawdź sieć/proxy i uruchom ponownie — "
                            f"plik {args.wyjscie} zostaje nietknięty.")
        except (zipfile.BadZipFile, ValueError, UnicodeDecodeError) as e:
            return _wyjscie(f"archiwum {kraj}.zip jest nieczytelne: {e}")

        wiersze, pominiete = przetworz(surowe)
        wszystkie.extend(wiersze)
        print(f"[{KTO}]   {kraj}: {len(wiersze)} kodów"
              + (f" ({pominiete} pominiętych bez współrzędnych)" if pominiete else ""))

    if not wszystkie:
        return _wyjscie("nic nie pobrano — plik wynikowy zostaje nietknięty.")

    zapisz(wszystkie, args.wyjscie)
    rozmiar_mb = args.wyjscie.stat().st_size / 1_048_576
    miasta = len({(k, m) for k, _, m, *_ in wszystkie})
    print(f"[{KTO}] Zapisano {len(set(wszystkie))} unikalnych wierszy "
          f"({miasta} miejscowości), {rozmiar_mb:.1f} MB -> {args.wyjscie}")
    print(f"[{KTO}] Sprawdź: python -m laweta_radar.services.geo Krosno Rzeszow")
    print(f"[{KTO}] Plik NALEŻY zacommitować — patrz nagłówek tego skryptu.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
