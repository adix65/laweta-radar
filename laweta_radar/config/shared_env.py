"""Dociągnięcie kluczy Apify ze WSPÓLNEGO .env sales-core-engine.

Ten system stoi na tym samym VPS-ie co sales-core-engine i korzysta z TEJ SAMEJ
puli kont Apify oraz tych samych proxy. Druga pula kont nie dałaby nic poza
podwojeniem sygnału multi-accountingu — a to jest dokładnie ten sygnał, przed
którym broni cały workers/apify_proxy.py. Dlatego klucze i proxy czytamy z .env
tamtego repo, zamiast trzymać ich kopię tutaj.

PRZENOSIMY WYŁĄCZNIE KLUCZE APIFY I PROXY — nie cały plik. To nie jest ostrożność
na wszelki wypadek, tylko ochrona przed konkretną katastrofą: tamten .env niesie
SWOJE `DATABASE_URL` i `TELEGRAM_*`. Zwykłe `load_dotenv` na obu plikach
sprawiłoby, że laweta bez własnego `DATABASE_URL` po cichu zaczęłaby pisać do bazy
sprzedażowej, a alerty o zleceniach szłyby na czat handlowca — i nikt by tego nie
zauważył, bo wszystko „działa". Dlatego czytamy tamten plik do słownika
(`dotenv_values` NIE dotyka os.environ) i przepisujemy tylko to, co pasuje do
listy niżej.

DLACZEGO OSOBNY MODUŁ, a nie kawałek settings.py: scalanie musi się wykonać przy
imporcie pakietu (patrz laweta_radar/__init__.py), żeby widziały je także punkty
wejścia CLI skopiowanych 1:1 workerów. Gdyby siedziało w settings.py, to
`python -m laweta_radar.config.settings` wykonywałby ten plik DWA RAZY — raz jako
moduł pakietu, raz jako __main__ — co Python sygnalizuje RuntimeWarning-iem
i co realnie psuło raportowaną liczbę wczytanych zmiennych. Osobny moduł
importowany przez oba miejsca wykonuje się dokładnie raz.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# Własny .env lawety wczytujemy TUTAJ, a nie zostawiamy settings.py — bo to w nim
# siedzi `SHARED_ENV_PATH`, czyli odpowiedź na pytanie „gdzie jest wspólny plik".
# Bez tego kolejność byłaby odwrotna do potrzebnej: szukalibyśmy wspólnego .env
# pod ścieżką domyślną, ignorując to, co operator wpisał u siebie. Wywołanie jest
# idempotentne (override=False), więc powtórzenie w settings.py nic nie psuje.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

# Domyślna ścieżka = układ z produkcji. Nadpisujesz przez SHARED_ENV_PATH w .env
# albo dowiązujesz symlinkiem: ln -s /home/ubuntu/sales-core-engine/.env shared.env
DOMYSLNY_SHARED_ENV = "/home/ubuntu/sales-core-engine/.env"

# Co wolno przepisać ze wspólnego .env. Wzorce, a nie prefiks `APIFY_` — bo prefiks
# wciągnąłby też APIFY_PROXY_POOL* i APIFY_CREDITS_*, a tych świadomie NIE chcemy.
WSPOLNE_WZORCE = (
    re.compile(r"^APIFY_API_TOKEN\d*$"),      # pula kont — sedno współdzielenia
    re.compile(r"^APIFY_PROXY\d+$"),          # proxy przypisane wprost do klucza
    re.compile(r"^APIFY_PROXY_URLS?$"),       # pula proxy / brama z {session}
    re.compile(r"^APIFY_PROXY_REQUIRED$"),    # bezpiecznik — ma działać tak samo w obu
)

# Czego NIE przepisujemy, mimo że zaczyna się od APIFY_.
#
# APIFY_PROXY_POOL* — darmowa pula z publicznych list. W repo źródłowym jest
# wyłączona od 2026-07-31 i ma zostać wyłączona TUTAJ. Powód jest pomiarowy:
# odświeżenie zwracało zero zweryfikowanych adresów z 411 kandydatów, a jedyny
# stary wpis, który został w pliku, przejmował przez rendezvous hashing komplet
# kont i zamieniał runy w timeouty. Gdybyśmy dziedziczyli tę zmienną, ktoś
# włączający pulę w tamtym repo włączyłby ją tutaj — po cichu i nie wiedząc o tym.
WYKLUCZONE_ZE_WSPOLNEGO = re.compile(r"^APIFY_PROXY_POOL")


def sciezka_wspolnego_env() -> Path | None:
    """Gdzie leży .env z pulą Apify. None, gdy nie ustawiono i nie ma domyślnego."""
    raw = (os.environ.get("SHARED_ENV_PATH") or "").strip()
    kandydat = Path(raw) if raw else Path(DOMYSLNY_SHARED_ENV)
    return kandydat if kandydat.is_file() else None


def wczytaj() -> tuple[int, str]:
    """Dociągnij klucze Apify i proxy ze wspólnego .env. -> (ile stamtąd, skąd).

    NIE nadpisuje niczego, co już jest w środowisku pod inną wartością: własny wpis
    w .env lawety albo `export` przed uruchomieniem zawsze wygrywa. Dzięki temu da
    się lokalnie podstawić własny klucz testowy, nie ruszając produkcyjnego pliku
    drugiego systemu.

    Zwracana liczba to „ile zmiennych POCHODZI ze wspólnego pliku", a nie „ile
    właśnie zapisałem". Różnica ma znaczenie, bo funkcja bywa wołana drugi raz
    w tym samym procesie: przy liczeniu zapisów drugi przebieg raportowałby zero
    (bo wszystko już siedzi w os.environ) i diagnostyka pokazywałaby „0 kluczy ze
    wspólnej puli" tuż obok listy tych kluczy.

    Brak pliku NIE jest błędem — na maszynie deweloperskiej sales-core-engine po
    prostu nie ma. Wtedy zwracamy zero i lecimy dalej; workery i tak zakończą
    czysto, gdy zabraknie kluczy.
    """
    sciezka = sciezka_wspolnego_env()
    if sciezka is None:
        return 0, "nie znaleziono"
    try:
        wartosci = dotenv_values(sciezka)
    except Exception as e:  # noqa: BLE001 — uszkodzony cudzy plik nie może wywalić importu
        return 0, f"błąd odczytu ({type(e).__name__})"

    ile = 0
    for nazwa, wartosc in wartosci.items():
        if not wartosc or not str(wartosc).strip():
            continue
        if WYKLUCZONE_ZE_WSPOLNEGO.match(nazwa):
            continue
        if not any(w.match(nazwa) for w in WSPOLNE_WZORCE):
            continue
        wartosc = str(wartosc)
        obecna = os.environ.get(nazwa)
        if not obecna:
            os.environ[nazwa] = wartosc
            ile += 1
        elif obecna == wartosc:
            ile += 1          # już wczytane w tym procesie — nadal „ze wspólnego"
        # else: własne ustawienie wygrywa i NIE liczy się jako wspólne
    return ile, str(sciezka)


# Wykonuje się raz na proces — patrz nota „DLACZEGO OSOBNY MODUŁ" w docstringu.
ILE, SKAD = wczytaj()
