"""Jedna rzecz: pominięty test integracyjny ma być WIDOCZNY, a nie cichy.

PO CO. Testy integracyjne tego repo (zapis do `posty`, cały przebieg fetchera,
naprawa starych wierszy) wymagają `TEST_DATABASE_URL` i bez niego pomijają się
— pytest pokazuje wtedy literkę `s` i kończy zielonym „passed". Dokładnie tak
przeszła poprawka, która nie zadziałała: testy pilnujące zapisu do bazy nie
uruchomiły się ani razu, a wynik wyglądał identycznie jak wynik testów zdanych.

W repo nie ma CI, więc jedynym miejscem, w którym da się to zauważyć, jest
ostatnia linia wyjścia pytesta. Ta linia tam jest.
"""
from __future__ import annotations

import os

MARKER = "brak TEST_DATABASE_URL"


def pytest_terminal_summary(terminalreporter, exitstatus, config):  # noqa: ARG001
    pominiete = [s for s in terminalreporter.stats.get("skipped", [])
                 if MARKER in str(getattr(s, "longrepr", ""))]
    if not pominiete:
        return
    terminalreporter.write_line("")
    terminalreporter.write_line(
        f"UWAGA: {len(pominiete)} testów INTEGRACYJNYCH pominięto — nie było "
        f"TEST_DATABASE_URL. To one sprawdzają, czy wynik klasyfikatora dojeżdża "
        f"do bazy i czy powstaje powiadomienie. Bez nich zielony wynik nie znaczy, "
        f"że zapis działa:", yellow=True, bold=True)
    terminalreporter.write_line(
        "    createdb laweta_test && TEST_DATABASE_URL="
        "postgresql://localhost/laweta_test python -m pytest laweta_radar/tests/",
        yellow=True)
    if os.environ.get("TEST_DATABASE_URL"):
        terminalreporter.write_line(
            "    (TEST_DATABASE_URL jest ustawione — sprawdź, czy psycopg2 jest "
            "zainstalowane i czy baza odpowiada.)", yellow=True)
