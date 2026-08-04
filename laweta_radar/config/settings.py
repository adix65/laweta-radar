"""Jedno miejsce, w którym czytamy środowisko — i jedno miejsce, które decyduje,
co zrobić, gdy czegoś brakuje.

DLACZEGO NIE pydantic-settings (jak w repo źródłowym): tamten model waliduje przy
imporcie i rzuca `ValidationError`, gdy brakuje pola wymaganego. Tutaj to jest
dokładnie zła reakcja. Workery chodzą z crona co kilka minut i mają zasadę:
brak konfiguracji = CZYSTE WYJŚCIE z komunikatem, nigdy wyjątek. Wyjątek przy
imporcie oznacza ślad w mailu od crona co przebieg, a przy pierwszym uruchomieniu
na świeżym VPS-ie — ścianę tracebacków zamiast jednej linijki "ustaw X w .env".
Dlatego: czytamy leniwie, oddajemy pustki, a decyzję "jechać czy nie" podejmuje
worker przez `brakujace()` + `wyjscie_bez_konfiguracji()`.

Sekrety NIE mają tu getterów maskujących — maskowanie robi ten, kto loguje
(patrz `mask_url` / `proxy_label` w workers/apify_proxy.py). Zasada w całym repo
jest prostsza od API: wartości z tego modułu nie trafiają do logów wprost.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from laweta_radar.config import shared_env

# Katalog pakietu — tu leży .env, tu rotacja kluczy Apify trzyma swój plik stanu.
# Wszystkie moduły liczą tę ścieżkę tak samo (parent.parent od siebie), więc
# .env jest jeden, niezależnie od tego, co odpalasz.
BASE_DIR = Path(__file__).resolve().parent.parent

# override=False: zmienne ustawione JAWNIE w środowisku (PM2, systemd, `export`
# w skrypcie deploya) mają wygrywać z plikiem. Inaczej deploy przez PM2 czytałby
# .env developera, który akurat został na dysku.
load_dotenv(BASE_DIR / ".env", override=False)


# Klucze Apify NIE należą do tego repo — przychodzą ze wspólnego .env
# sales-core-engine (ta sama pula kont, te same proxy). Całość wraz z listą
# przepisywanych zmiennych i uzasadnieniem: config/shared_env.py. Tutaj tylko
# re-eksport, żeby wołający miał jedno miejsce do pytania o konfigurację.
sciezka_wspolnego_env = shared_env.sciezka_wspolnego_env
_wczytaj_wspolne_apify = shared_env.wczytaj
WSPOLNE_APIFY_ILE = shared_env.ILE
WSPOLNE_APIFY_SKAD = shared_env.SKAD


def _txt(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _int(name: str, default: int) -> int:
    """Liczba ze środowiska; śmieć albo brak -> domyślna.

    Świadomie NIE rzucamy na literówce ("30 min" zamiast "30"): pojedyncze
    pokrętło nie może zatrzymać całego przebiegu. Zła wartość degraduje do
    domyślnej, a operator zobaczy to w linijce startowej workera.
    """
    try:
        return int(_txt(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    """Jak `_int`, ale przyjmuje też przecinek dziesiętny.

    Współrzędne bazy kopiuje się z Map Google, a te w polskiej lokalizacji podają
    je z przecinkiem. Cicha zamiana na kropkę jest tu bezpieczna (nie ma innego
    znaczenia przecinka w liczbie) i oszczędza godziny szukania, czemu dystans
    wychodzi 0 km od równika.
    """
    raw = _txt(name)
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Baza — OSOBNA instancja/baza `laweta`, nie współdzielona z innymi projektami.
# Rozdział jest celowy: to inny cykl życia danych (posty z grup żyją godzinami)
# i inne uprawnienia. Rola z tego DSN-a NIE ma praw DDL — tabele zakłada migracja
# odpalana ręcznie jako postgres (api/migrations/, patrz README).
# ---------------------------------------------------------------------------
DATABASE_URL = _txt("DATABASE_URL")

# ---------------------------------------------------------------------------
# Klasyfikator (Anthropic). Model trzymamy w konfiguracji, bo przy zmianie
# progu jakość/koszt chcemy go podmienić bez deployu — a nie dlatego, że
# spodziewamy się częstych zmian.
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = _txt("ANTHROPIC_API_KEY")
CLASSIFIER_MODEL = _txt("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")

# ---------------------------------------------------------------------------
# Telegram — jedyny kanał dowozu do operatora. Brak tokenu wycisza alerty
# (services/telegram_notify._send zwraca False), ale NIE zatrzymuje zbierania:
# posty lecą do bazy i da się je wysłać po skonfigurowaniu.
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = _txt("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _txt("TELEGRAM_CHAT_ID")

# ---------------------------------------------------------------------------
# Baza lawety — punkt, od którego liczymy dystans do zdarzenia. Bez tego nie da
# się odrzucić zlecenia z drugiego końca Polski, a takie zlecenie jest gorsze niż
# brak zlecenia: operator traci czas na czytanie alertu, którego i tak nie weźmie.
# ---------------------------------------------------------------------------
BAZA_LAT = _float("BAZA_LAT", 0.0)
BAZA_LON = _float("BAZA_LON", 0.0)
MAX_DYSTANS_KM = _int("MAX_DYSTANS_KM", 80)

# Posty starsze niż to okno pomijamy przy pobieraniu. Zlecenie na lawetę jest
# ważne kilkadziesiąt minut — po dobie ktoś już przyjechał, a my płacimy Apify
# za pobranie i model za klasyfikację czegoś, co jest nieaktualne.
MAX_WIEK_POSTA_H = _int("MAX_WIEK_POSTA_H", 12)

# ---------------------------------------------------------------------------
# Czego wymaga który kawałek systemu. Trzymane jako dane, a nie rozsiane po
# `if not X` w workerach — dzięki temu komunikat o brakach jest wszędzie taki sam
# i da się go wypisać CAŁY naraz, zamiast wychodzić po pierwszym brakującym
# kluczu i wracać po poprawce po kolejny.
# ---------------------------------------------------------------------------
OPIS_ZMIENNYCH: dict[str, str] = {
    "DATABASE_URL": "DSN do bazy `laweta` — OSOBNEJ od sales-core-engine",
    "ANTHROPIC_API_KEY": "klucz API Anthropic — bez niego klasyfikator nie ruszy",
    "TELEGRAM_BOT_TOKEN": "token bota od @BotFather",
    "TELEGRAM_CHAT_ID": "ID czatu operatora (bot musi tam być dodany)",
    "APIFY_API_TOKEN1": ("klucz Apify — normalnie przychodzi ze WSPÓLNEGO .env "
                         "(SHARED_ENV_PATH), nie ustawiaj go tutaj bez powodu"),
}


def brakujace(*nazwy: str) -> list[str]:
    """Które z podanych zmiennych są puste. Pusta lista = komplet.

    Zwraca LISTĘ, nie bool — worker ma wypisać wszystkie braki naraz.
    """
    return [n for n in nazwy if not _txt(n)]


def wyjscie_bez_konfiguracji(kto: str, braki: list[str], strumien=None) -> int:
    """Wypisz, czego brakuje, i oddaj kod wyjścia dla CZYSTEGO zakończenia.

    Kod 0, nie 1 — i to jest świadome. Dla crona 0 znaczy "nie ma nic do
    zrobienia", a niezerowy kod to awaria, o której trzeba kogoś obudzić.
    Nieskonfigurowany system nie jest awarią: jest systemem, którego jeszcze nie
    włączono. Gdyby brak tokenu dawał kod 1, skrzynka operatora zapełniłaby się
    mailami od crona co 5 minut i realna awaria utonęłaby w szumie.

    Użycie w workerze:
        braki = settings.brakujace("DATABASE_URL", "ANTHROPIC_API_KEY")
        if braki:
            return settings.wyjscie_bez_konfiguracji("classifier", braki)
    """
    out = strumien or sys.stderr
    print(f"[{kto}] Brak konfiguracji — kończę bez działania.", file=out)
    for n in braki:
        print(f"[{kto}]   {n} — {OPIS_ZMIENNYCH.get(n, 'patrz .env.example')}", file=out)
    print(f"[{kto}] Uzupełnij {BASE_DIR / '.env'} (wzór: .env.example) i uruchom ponownie.",
          file=out)
    return 0


def opis_srodowiska() -> str:
    """Jedna linia do logu startowego: co jest ustawione, BEZ wartości.

    Same nazwy i "tak/nie" — wartości to sekrety, a i tak interesuje nas tylko,
    czy klucz jest, gdy trzeba zdiagnozować "czemu nic nie przyszło".
    """
    stan = {
        "db": bool(DATABASE_URL),
        "anthropic": bool(ANTHROPIC_API_KEY),
        "telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "baza_geo": bool(BAZA_LAT or BAZA_LON),
    }
    return "[settings] " + ", ".join(
        f"{k}={'tak' if v else 'BRAK'}" for k, v in stan.items()
    ) + (f", max_dystans={MAX_DYSTANS_KM} km, max_wiek_posta={MAX_WIEK_POSTA_H} h"
         f", wspolny_apify={WSPOLNE_APIFY_ILE} zmiennych z {WSPOLNE_APIFY_SKAD}")


# Podgląd konfiguracji bez odpalania czegokolwiek:
#   python -m laweta_radar.config.settings
if __name__ == "__main__":
    print(opis_srodowiska())
    braki = brakujace(*OPIS_ZMIENNYCH)
    if braki:
        raise SystemExit(wyjscie_bez_konfiguracji("settings", braki, sys.stdout))
    print("[settings] Komplet zmiennych ustawiony.")
