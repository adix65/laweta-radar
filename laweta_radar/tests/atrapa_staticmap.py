"""Atrapa paczki `staticmap` — render bez sieci, bez Pillow i bez kafelków.

PO CO OSOBNY PLIK, A NIE FIXTURE W JEDNYM TEŚCIE. `staticmap` jest zależnością
OPCJONALNĄ (patrz requirements.txt, tak samo jak `pywebpush`), więc testy repo
MUSZĄ przechodzić bez niej — inaczej zielony wynik na maszynie z zainstalowaną
paczką znaczyłby co innego niż na VPS-ie, który jej nie ma. Atrapa udaje API
dokładnie w tym zakresie, w którym korzysta z niego `services/mapa.py`, i jest
wspólna dla `test_mapa.py` (jednostkowo) oraz `test_powiadomienia.py` (cała
droga od zlecenia do sendPhoto).

LICZNIK RENDERÓW jest tu funkcją, nie diagnostyką: „ta sama trasa w dwóch
postach = jedno pobranie kafelków" da się sprawdzić WYŁĄCZNIE przez policzenie,
ile razy ktoś sięgnął po obrazek. Kafelki utrzymuje projekt społeczny z darowizn
i to jest jedyny test, który pilnuje, że nie pobieramy ich w kółko.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Każdy wpis = jedno pełne pobranie kompletu kafelków.
RENDERY: list[dict] = []
# Ustaw wyjątek, żeby render padł (brak sieci, kafelek 429, dysk pełny).
BLAD: Exception | None = None
# Sztuczne opóźnienie renderu — do sprawdzenia budżetu czasu.
OPOZNIENIE_S: float = 0.0


def zeruj() -> None:
    global BLAD, OPOZNIENIE_S  # noqa: PLW0603 — stan atrapy, nie kodu produkcyjnego
    RENDERY.clear()
    BLAD = None
    OPOZNIENIE_S = 0.0


class Line:
    def __init__(self, coords, color, width):
        self.coords = list(coords)
        self.color = color
        self.width = width


class CircleMarker:
    def __init__(self, coord, color, width):
        self.coord = coord
        self.color = color
        self.width = width


class _Obraz:
    """To, co w prawdziwej paczce oddaje `render()` — obiekt Pillow z `.save`."""

    def __init__(self, opis: dict):
        self.opis = opis

    def save(self, sciezka, format=None):  # noqa: A002 — podpis Pillow, nie nasz
        self.opis["zapis"] = {"sciezka": str(sciezka), "format": format}
        Path(sciezka).write_bytes(b"\x89PNG\r\n-atrapa-")


class StaticMap:
    def __init__(self, width, height, url_template=None, headers=None,
                 tile_request_timeout=None, **reszta):
        self.opis = {
            "rozmiar": (width, height),
            "url_template": url_template,
            "headers": headers or {},
            "tile_request_timeout": tile_request_timeout,
            "linie": [],
            "markery": [],
            "reszta": reszta,
        }

    def add_line(self, linia: Line) -> None:
        self.opis["linie"].append(linia)

    def add_marker(self, marker: CircleMarker) -> None:
        self.opis["markery"].append(marker)

    def render(self):
        if OPOZNIENIE_S:
            time.sleep(OPOZNIENIE_S)
        if BLAD is not None:
            raise BLAD
        RENDERY.append(self.opis)
        return _Obraz(self.opis)


def zainstaluj(monkeypatch):
    """Podstaw atrapę pod `import staticmap` i wyzeruj stan między testami.

    Czyścimy też `mapa._ostrzezenia` — to zbiór komunikatów „powiedz raz na
    proces". Bez zerowania test sprawdzający, że brak paczki melduje się JEDEN
    raz, przechodziłby albo nie w zależności od kolejności testów.
    """
    from laweta_radar.services import mapa  # noqa: PLC0415 — import w helperze testowym

    zeruj()
    mapa._ostrzezenia.clear()
    monkeypatch.setitem(sys.modules, "staticmap", sys.modules[__name__])
    return sys.modules[__name__]
