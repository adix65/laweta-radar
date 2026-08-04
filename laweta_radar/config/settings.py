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
# Klasyfikator. Model trzymamy w konfiguracji, bo przy zmianie progu
# jakość/koszt chcemy go podmienić bez deployu — a nie dlatego, że spodziewamy
# się częstych zmian.
#
# LLM_PROVIDER przełącza CAŁĄ implementację wołania modelu (services/llm.py),
# nie tylko nazwę. Istnieje z powodu pomiarowego: jakości ekstrakcji z polskiego
# posta bez ogonków nie da się odczytać z żadnego benchmarku, więc mierzymy ją
# na swoich danych (scripts/porownaj_modele.py). Nieznana wartość degraduje do
# "anthropic" — jedynego providera, którego zależność jest w requirements.txt.
#
# Model per provider, a nie jeden wspólny, bo porównywarka odpala WSZYSTKIE
# skonfigurowane naraz i musi wiedzieć, co puścić na każdym.
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = _txt("ANTHROPIC_API_KEY")
CLASSIFIER_MODEL = _txt("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")

LLM_PROVIDER = _txt("LLM_PROVIDER", "anthropic")
OPENAI_API_KEY = _txt("OPENAI_API_KEY")
OPENAI_MODEL = _txt("OPENAI_MODEL", "gpt-5-mini")
GEMINI_API_KEY = _txt("GEMINI_API_KEY")
GEMINI_MODEL = _txt("GEMINI_MODEL", "gemini-2.5-flash")

# Stawki providerów spoza Anthropic — JSON {"model": [usd_wejscie, usd_wyjscie]}
# za MILION tokenów. Świadomie NIE są zaszyte w kodzie: zła stawka nie wywala
# niczego, tylko po cichu przekłamuje jedyną liczbę, dla której porównywarka
# istnieje. Pusto = raport pokaże koszt jako nieznany, i to jest poprawna
# odpowiedź, dopóki nikt nie sprawdził cennika.
CENNIK_EXTRA = _txt("CENNIK_EXTRA")
# Kurs do przeliczenia kosztu runu na złotówki. Wartość domyślna jest
# ZAOKRĄGLONYM PLACEHOLDEREM — ustaw realny, jeśli liczba ma być czymś więcej
# niż rzędem wielkości.
KURS_USD_PLN = _float("KURS_USD_PLN", 4.00)

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
# BAZA_LNG i BAZA_LON to ta sama rzecz — długość geograficzna. `LON` był tu od
# pierwszej wersji .env, `LNG` przyszedł z zadania geo i jest formą, którą
# ludzie kopiują z Map Google. Przyjmujemy OBIE zamiast wybierać jedną, bo
# jedyny efekt wyboru byłby taki, że czyjaś działająca konfiguracja po cichu
# przestaje działać i dystanse liczą się od równika.
BAZA_LAT = _float("BAZA_LAT", 0.0)
BAZA_LON = _float("BAZA_LNG", 0.0) or _float("BAZA_LON", 0.0)
BAZA_LNG = BAZA_LON
BAZA_NAZWA = _txt("BAZA_NAZWA", "Krosno")

# UWAGA: ta zmienna NIE JEST bramką i nie wolno jej nią zrobić. Zasada naczelna
# repo mówi: system pokazuje zlecenia, decyduje kierowca. Dystans jest etykietą
# na ekranie — przy transporcie międzynarodowym „ile km od bazy" i tak nie znaczy
# nic, bo liczy się długość kursu odbiór->dostawa (services/geo.py). Zmienna
# została, bo pokazuje ją /health i bo służy do WYRÓŻNIENIA zleceń blisko bazy.
MAX_DYSTANS_KM = _int("MAX_DYSTANS_KM", 80)

# Stawki do etykiety „szacunek" przy zleceniu (services/geo.kalkulacja).
# To ETYKIETA NA EKRANIE, nie wycena i nie bramka — nic się na jej podstawie
# nie ukrywa ani nie odrzuca. Kierowca patrzy na liczbę i sam decyduje.
STAWKA_ZA_KM = _float("STAWKA_ZA_KM", 4.0)
STAWKA_MINIMALNA = _float("STAWKA_MINIMALNA", 250.0)

# Posty starsze niż to okno pomijamy przy pobieraniu. Zlecenie na lawetę jest
# ważne kilkadziesiąt minut — po dobie ktoś już przyjechał, a my płacimy Apify
# za pobranie i model za klasyfikację czegoś, co jest nieaktualne.
MAX_WIEK_POSTA_H = _int("MAX_WIEK_POSTA_H", 12)

# ---------------------------------------------------------------------------
# BRAMKA (workers/gate.py) — darmowy prefiltr słownikowy przed modelem.
#
# GATE_PROG: suma wag, od której post idzie do AI. Piątka jest CELOWO niska.
# Asymetria kosztów jest brutalna: śmieć przepuszczony do AI to ~0,002 zł, a
# zlecenie odrzucone przez bramkę to ~300 zł straconego kursu, o którym nigdy
# się nie dowiemy — post nie trafi nigdzie. Jeden przegapiony kurs miesięcznie
# kasuje CAŁĄ oszczędność na tokenach. Właściwą wartość odczytuje się
# z rozkładu punktów w scripts/raport_gate.py, a nie zgaduje.
#
# GATE_TRYB: "cien" albo "aktywny". W cieniu bramka liczy i zapisuje swoją
# decyzję, ale NICZEGO nie blokuje — wszystkie posty idą do AI. Dopiero to daje
# pary (decyzja bramki, werdykt AI), z których widać jedyną liczbę, która ma
# znaczenie: ile zleceń bramka by skasowała. Przełączenie na "aktywny" ma sens
# wyłącznie wtedy, gdy ta liczba wynosi zero. Nieznana wartość degraduje do
# "cien" — literówka w .env nie może po cichu włączyć blokowania.
# ---------------------------------------------------------------------------
GATE_PROG = _int("GATE_PROG", 5)
GATE_TRYB = _txt("GATE_TRYB", "cien")

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
         f", gate={GATE_TRYB}(prog {GATE_PROG})"
         f", llm={LLM_PROVIDER}/{CLASSIFIER_MODEL}"
         f", wspolny_apify={WSPOLNE_APIFY_ILE} zmiennych z {WSPOLNE_APIFY_SKAD}")


# Podgląd konfiguracji bez odpalania czegokolwiek:
#   python -m laweta_radar.config.settings
if __name__ == "__main__":
    print(opis_srodowiska())
    braki = brakujace(*OPIS_ZMIENNYCH)
    if braki:
        raise SystemExit(wyjscie_bez_konfiguracji("settings", braki, sys.stdout))
    print("[settings] Komplet zmiennych ustawiony.")
