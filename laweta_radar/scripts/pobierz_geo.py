#!/usr/bin/env python3
"""Pobierz bazę kodów i miejscowości z GeoNames i scal do jednego data/kody_eu.csv.

PO CO TO ISTNIEJE: services/geo.py nie używa żadnego płatnego geokodera. 90%
przypadków to kod pocztowy albo nazwa miasta, a to załatwia lokalna baza — za
darmo, offline, w mikrosekundy, bez limitu zapytań i bez klucza, który może
wygasnąć w środku nocy. Ten skrypt tę bazę buduje.

DWA ŹRÓDŁA NA KRAJ, bo jedno nie wystarcza:

  • eksport KODÓW (export/zip/{KRAJ}.zip) — obsługuje wyszukiwanie PO KODZIE.
    Do wyszukiwania po nazwie bywa bezużyteczny: niemiecki plik zawiera masę
    kodów instytucji (Grosskunden-PLZ) z nazwami w rodzaju „Agentur fuer
    Arbeit Dortmund" zamiast miejscowości — „Frankfurt" nie występował w bazie
    ANI RAZU, a „Dortmund" znajdował się przypadkiem, przez nazwę urzędu;
  • dump MIEJSCOWOŚCI (export/dump/{KRAJ}.zip) — wpisy feature_class='P',
    z populacją. Obsługuje wyszukiwanie PO NAZWIE, a populacja rozstrzyga
    wybór między miastami o tej samej nazwie (Frankfurt am Main kontra
    Frankfurt nad Odrą). W pliku wynikowym te wiersze mają pusty `kod`
    i wypełnioną kolumnę `populacja`.

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
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Ten skrypt nie importuje dziś niczego z `laweta_radar` — wywołanie zostaje jako
# bezpiecznik na pierwszy taki import, żeby nie wrócił tu `ModuleNotFoundError`.
try:                               # pakiet widoczny: -m, import pakietowy, testy
    from laweta_radar.scripts._sciezka import dodaj_repo_do_sciezki
except ImportError:                # uruchomienie po ścieżce do pliku
    from _sciezka import dodaj_repo_do_sciezki

dodaj_repo_do_sciezki()

KTO = "pobierz-geo"

# PL pierwsza, bo to 90% ruchu. Reszta to kierunki, w które realnie jeżdżą
# lawety z Podkarpacia — auta kupione w Niemczech i Holandii, tranzyt przez
# Czechy i Słowację, giełdy we Francji i Włoszech.
KRAJE_DOMYSLNE = ("PL", "DE", "CZ", "SK", "NL", "BE", "AT", "FR", "IT")

URL_KODY = "https://download.geonames.org/export/zip/{kraj}.zip"
URL_MIEJSCOWOSCI = "https://download.geonames.org/export/dump/{kraj}.zip"
TIMEOUT_S = 60

_ROOT = Path(__file__).resolve().parent.parent.parent
WYJSCIE_DOMYSLNE = _ROOT / "data" / "kody_eu.csv"

# Układ pliku KODÓW GeoNames (TSV, bez nagłówka):
#   0 kraj, 1 kod, 2 miejscowość, 3 admin1 (nazwa), 4 admin1 (kod),
#   ..., 9 lat, 10 lng
KOL_KRAJ, KOL_KOD, KOL_MIEJSCOWOSC, KOL_ADMIN1, KOL_ADMIN1_KOD, KOL_LAT, KOL_LNG = \
    0, 1, 2, 3, 4, 9, 10

# Układ DUMPU MIEJSCOWOŚCI GeoNames (TSV, bez nagłówka):
#   0 geonameid, 1 name, 2 asciiname, 3 alternatenames, 4 lat, 5 lng,
#   6 feature_class, 7 feature_code, 8 country, 9 cc2, 10 admin1,
#   11-13 admin2-4, 14 population, ...
# Dump podaje admin1 jako KOD ("82", "NW"), nie nazwę — nazwy bierzemy z mapy
# zbudowanej na pliku kodów tego samego kraju (tam stoją obie formy obok
# siebie), żeby „wojewodztwo" znaczyło w obu rodzajach wierszy to samo.
(KOL_M_NAZWA, KOL_M_LAT, KOL_M_LNG, KOL_M_KLASA, KOL_M_KRAJ, KOL_M_ADMIN1,
 KOL_M_POPULACJA) = 1, 4, 5, 6, 8, 10, 14

# Klasa obiektów „miejscowość" (city, village...). Reszta dumpu to góry, rzeki,
# urzędy i hotele — w bazie geokodera tylko udawałyby trafienia.
KLASA_MIEJSCOWOSCI = "P"

NAGLOWEK = ("kraj", "kod", "miejscowosc", "wojewodztwo", "lat", "lng", "populacja")


def _wyjscie(komunikat: str) -> int:
    """Czyste zakończenie z komunikatem — ta sama zasada co w workerach."""
    print(f"[{KTO}] {komunikat}", file=sys.stderr)
    return 0


def pobierz(kraj: str, url: str) -> bytes:
    adres = url.format(kraj=kraj)
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


def przetworz_kody(wiersze: list[list[str]]) -> tuple[list[tuple], int, dict]:
    """Surowe wiersze kodów -> krotki w naszym formacie.

    Zwraca (wiersze, pominięte, nazwy_admin1). Trzeci element to mapa
    {(kraj, kod_admin1): nazwa_admin1} — dump miejscowości zna region tylko
    z kodu, a wyświetlamy nazwę.

    Pomijamy wpisy bez współrzędnych. GeoNames ma ich trochę (kody skrytek
    pocztowych, kody wojskowe) i są bezużyteczne: kod bez lat/lng nie da się
    zamienić na punkt, a jego obecność w bazie tylko udaje trafienie.
    """
    wynik: list[tuple] = []
    pominiete = 0
    nazwy_admin1: dict[tuple[str, str], str] = {}
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
        kraj = w[KOL_KRAJ].strip().upper()
        admin1, admin1_kod = w[KOL_ADMIN1].strip(), w[KOL_ADMIN1_KOD].strip()
        if admin1 and admin1_kod:
            nazwy_admin1.setdefault((kraj, admin1_kod), admin1)
        wynik.append((
            kraj,
            w[KOL_KOD].strip(),
            w[KOL_MIEJSCOWOSC].strip(),
            admin1,
            lat,
            lng,
            "",     # populacja — niesie ją wiersz miejscowości, nie kodu
        ))
    return wynik, pominiete, nazwy_admin1


def przetworz_miejscowosci(wiersze: list[list[str]],
                           nazwy_admin1: dict) -> tuple[list[tuple], int]:
    """Surowe wiersze dumpu -> krotki miejscowości (pusty kod, z populacją).

    Bierzemy WYŁĄCZNIE feature_class='P' — reszta dumpu (góry, rzeki, urzędy,
    hotele) w bazie geokodera tylko udawałaby trafienia; jej odrzucenie nie
    jest „pominięciem", więc nie wchodzi do licznika. Populacja zostaje także
    jako 0 (GeoNames nie zna jej dla wielu wsi): wpis bez populacji nadal
    geokoduje, tylko nie wygrywa remisów.
    """
    wynik: list[tuple] = []
    pominiete = 0
    for w in wiersze:
        if len(w) <= KOL_M_POPULACJA:
            pominiete += 1
            continue
        if w[KOL_M_KLASA].strip() != KLASA_MIEJSCOWOSCI:
            continue
        nazwa = w[KOL_M_NAZWA].strip()
        lat, lng = w[KOL_M_LAT].strip(), w[KOL_M_LNG].strip()
        if not nazwa or not lat or not lng:
            pominiete += 1
            continue
        try:
            float(lat), float(lng)
        except ValueError:
            pominiete += 1
            continue
        populacja = w[KOL_M_POPULACJA].strip()
        if not populacja.isdigit():
            populacja = "0"
        kraj = w[KOL_M_KRAJ].strip().upper()
        wynik.append((
            kraj,
            "",     # pusty kod — wiersz obsługuje wyszukiwanie po nazwie
            nazwa,
            nazwy_admin1.get((kraj, w[KOL_M_ADMIN1].strip()), ""),
            lat,
            lng,
            populacja,
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
    print(f"[{KTO}] Źródła: {URL_KODY.format(kraj='{KRAJ}')} (kody pocztowe)")
    print(f"[{KTO}]         {URL_MIEJSCOWOSCI.format(kraj='{KRAJ}')} "
          f"(miejscowości z populacją)   — oba GeoNames, CC BY 4.0")
    print(f"[{KTO}] Wynik:  {args.wyjscie}")
    print(f"[{KTO}] Rząd wielkości: kilka MB dla PL, kilkadziesiąt MB dla "
          f"kompletu (dump DE jest największy).")
    if args.sucho:
        print(f"[{KTO}] --sucho: kończę bez pobierania.")
        return 0

    wszystkie: list[tuple] = []
    for kraj in kraje:
        # Najpierw kody: budują mapę nazw regionów, z której korzysta
        # przetwarzanie miejscowości tego samego kraju.
        try:
            surowe_kody = rozpakuj(pobierz(kraj, URL_KODY), kraj)
            surowe_miejsca = rozpakuj(pobierz(kraj, URL_MIEJSCOWOSCI), kraj)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # Brak sieci to nie awaria tego repo — to warunek zewnętrzny.
            # Kończymy CZYSTO, mówiąc co się stało i co dalej.
            return _wyjscie(f"nie udało się pobrać {kraj}: {e}. "
                            f"Sprawdź sieć/proxy i uruchom ponownie — "
                            f"plik {args.wyjscie} zostaje nietknięty.")
        except (zipfile.BadZipFile, ValueError, UnicodeDecodeError) as e:
            return _wyjscie(f"archiwum {kraj}.zip jest nieczytelne: {e}")

        kody, pominiete_kody, nazwy_admin1 = przetworz_kody(surowe_kody)
        miejsca, pominiete_miejsc = przetworz_miejscowosci(surowe_miejsca,
                                                          nazwy_admin1)
        pominiete = pominiete_kody + pominiete_miejsc
        wszystkie.extend(kody)
        wszystkie.extend(miejsca)
        print(f"[{KTO}]   {kraj}: {len(kody)} kodów, {len(miejsca)} miejscowości"
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
