"""Doklejenie katalogu repo do `sys.path` — jedna kopia dla wszystkich skryptów.

PO CO TO ISTNIEJE: Python wkłada do `sys.path` katalog URUCHOMIONEGO PLIKU, a nie
katalog, z którego go wywołano. Przy `python laweta_radar/scripts/znajdz_grupy.py`
na ścieżce ląduje więc `laweta_radar/scripts/`, w którym pakietu `laweta_radar`
nie ma — i pierwszy import z niego kończy się `ModuleNotFoundError`. Przez `-m`
to samo działa, bo tam na ścieżkę trafia katalog bieżący.

Wywołanie PO ŚCIEŻCE DO PLIKU nie jest tu jednak przypadkiem, który wolno zbyć
zdaniem „to używaj -m": w tej postaci podaje te skrypty README, komentarz
w `.env.example`, podpowiedź z `check_setup.sh` i wpis crona w `odswiez_proxy.py`.
To jest wzorzec, którym posłuży się człowiek stawiający system o północy — więc
to on ma zadziałać, a nie tylko ten drugi.

DLACZEGO OSOBNY MODUŁ, a nie trzy linijki w każdym skrypcie: skryptów jest osiem
i poprawka była w nich napisana na trzy różne sposoby, a w jednym nie było jej
wcale. Osiem kopii jednego bezpiecznika to osiem miejsc, o których dziewiąty
skrypt zapomni.

UŻYCIE — PRZED pierwszym importem z `laweta_radar`:

    try:                               # pakiet widoczny: -m, import pakietowy, testy
        from laweta_radar.scripts._sciezka import dodaj_repo_do_sciezki
    except ImportError:                # uruchomienie po ścieżce do pliku
        from _sciezka import dodaj_repo_do_sciezki

    dodaj_repo_do_sciezki()

Import w dwóch wariantach jest konieczny, bo ten moduł podlega dokładnie temu
problemowi, który rozwiązuje. Kolejność gałęzi nie jest dowolna — razem pokrywają
WSZYSTKIE trzy sposoby, którymi te pliki bywają wczytywane:

  • `python -m laweta_radar.scripts.X` oraz zwykły import pakietowy
    (`from laweta_radar.scripts import X` w testach) — pakiet jest widoczny,
    więc przechodzi pierwsza gałąź;
  • `importlib.util.spec_from_file_location(...)` — tak dwa testy ładują skrypt
    ze ścieżki jako moduł NAJWYŻSZEGO POZIOMU. Moduł nie należy wtedy do żadnego
    pakietu, więc import WZGLĘDNY (`from ._sciezka import ...`) by tu poległ,
    a bezwzględny przechodzi, bo katalog repo jest już na `sys.path`;
  • `python laweta_radar/scripts/X.py` — pakietu nie widać (to jest właśnie ten
    błąd), za to `_sciezka` leży wprost na `sys.path`, bo Python wstawił tam
    katalog skryptu. Bierze go druga gałąź.

Dlatego też ten moduł NIE IMPORTUJE niczego z `laweta_radar`: musi dać się
wczytać właśnie wtedy, gdy pakiet jest jeszcze niewidoczny.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Dwa poziomy w górę od katalogu tego pliku: scripts/ -> laweta_radar/ -> repo.
KATALOG_REPO = Path(__file__).resolve().parent.parent.parent


def dodaj_repo_do_sciezki() -> str:
    """Wstaw katalog repo na POCZĄTEK `sys.path` i zwróć go. Idempotentnie.

    Na początek — żeby repo miało pierwszeństwo przed tym, co akurat leży
    w środowisku pod tą samą nazwą.

    Idempotentnie — bo przy `python -m ...` z katalogu repo ta ścieżka już tam
    jest. Drugi wpis niczego by nie naprawił, a wydłużałby listę przeglądaną
    przy KAŻDYM imporcie.
    """
    koren = str(KATALOG_REPO)
    if koren not in sys.path:
        sys.path.insert(0, koren)
    return koren
