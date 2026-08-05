"""Podgląd trasy jako obrazek — jedyna informacja w alercie, której nie da się
zapisać liczbą.

PO CO. Alert ma trzy liczby i nazwy dwóch miast. „Żulte → Jędrzejów, 1180 km"
mówi wszystko o długości kursu i nic o tym, GDZIE ta trasa leży — a przy decyzji
„brać czy nie" liczy się rzut oka na jej kształt: czy prowadzi tam, gdzie
operator i tak jedzie, czy w bok. Dotąd odpowiedź na to pytanie wymagała wyjścia
z Telegrama i otwarcia map. Obrazek pod alertem odpowiada bez klikania.

--------------------------------------------------------------------------
OBRAZEK JEST DODATKIEM I NIGDY NIE MOŻE BYĆ POWODEM, DLA KTÓREGO ZLECENIE NIE
DOTARŁO DO KIEROWCY. Cały ten moduł zwraca ścieżkę do pliku albo `None`; `None`
znaczy „wyślij alert tekstem, tak jak dotąd" i jest normalnym wynikiem w pięciu
sytuacjach:

  - MAPY_W_ALERTACH=0 — świadome wyłączenie,
  - brak `staticmap` w środowisku (paczka OPCJONALNA, patrz requirements.txt),
  - któryś punkt nierozpoznany — mapa z jednym punktem myli bardziej, niż pomaga,
  - wyjątek przy generowaniu (brak sieci, kafelek 429, dysk pełny),
  - przekroczony budżet 5 s.

Żadna z nich nie rzuca wyjątkiem i żadna nie wstrzymuje wysyłki.

--------------------------------------------------------------------------
KAFELKI SĄ CUDZE. `tile.openstreetmap.org` utrzymuje projekt społeczny z darowizn
i ma politykę użycia, której złamanie kończy się blokadą IP — nie naszą awarią,
tylko cudzą decyzją, o której dowiemy się z braku obrazków. Dlatego:

  - NAGŁÓWEK `User-Agent` z nazwą i wersją. Anonimowy ruch bywa blokowany
    hurtem i to jest pierwsza rzecz, którą OSM odcina.
  - CACHE NA DYSKU. Ten sam post crossowany do pięciu grup daje pięć alertów
    o jednej trasie; bez cache'u to pięć kompletów pobrań tych samych kafelków.
    Klucz to para współrzędnych zaokrąglona do 3 miejsc po przecinku (ok. 100 m
    — poniżej tego progu obrazek i tak wygląda identycznie).
  - JEDEN OBRAZEK NA TRASĘ, nie na post. Wpisy starsze niż 7 dni kasujemy;
    trasa, która nie wróciła przez tydzień, nie wróci.

STYL KAFELKÓW JEST ZWERYFIKOWANY RĘCZNIE (trasa Żulte (BE) → Jędrzejów (PL)):
przy zoomie dobranym do trasy widać nazwy krajów i główne miasta, co wystarcza
do orientacji. Sprawdzone i ODRZUCONE: carto-positron, carto-voyager, opentopo
oraz domalowywanie granic państw z Natural Earth — nie poprawiają czytelności,
a dokładają zależności. Nie wracaj do nich.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from laweta_radar.config import settings

KTO = "mapa"

# Kafelki OSM. Adres jest STAŁĄ W KODZIE, nie zmienną z .env: to jedyne miejsce,
# z którego ten moduł wychodzi do sieci, a adres sterowany konfiguracją zamienia
# literówkę w .env w wywołanie pod dowolny obcy serwer.
KAFELKI = "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"
UZYTKOWNIK = "laweta-radar/1.0"

# Kolory. Trasa niebieska, odbiór zielony („stąd"), dostawa czerwona („dotąd"),
# a punkt zgadywany pomarańczowy — ta sama informacja, którą alert podaje słowem
# („⚠ Dębica? (niepewne)"), tylko widoczna w tym samym rzucie oka co kształt
# trasy. Bez niej obrazek wyglądałby na pewniejszy niż podpis pod nim.
KOLOR_TRASY = "#0044ff"
GRUBOSC_TRASY = 5
KOLOR_ODBIORU = "#00aa00"
KOLOR_DOSTAWY = "#dd0000"
KOLOR_NIEPEWNY = "#ff8800"
PROMIEN_MARKERA = 16

# Sufit czasu na CAŁE generowanie, liczony zegarem ściennym. Powiadomienie ma
# dojść w kilka minut od publikacji posta; pięć sekund to najwięcej, co wolno
# temu dodatkowi zabrać z tego budżetu.
BUDZET_S = 5.0

# Po tylu dniach wpis w cache'u przestaje być trasą, a zaczyna być śmieciem.
DNI_CACHE = 7

# Rozsądny zakres wymiarów. Telegram odrzuca zdjęcia o absurdalnych rozmiarach,
# a MAPA_W=7000 z literówki oznacza kilkaset pobranych kafelków na jeden alert —
# czyli dokładnie to zachowanie, przez które OSM blokuje IP.
MIN_BOK, MAX_BOK = 200, 1600

_ostrzezenia: set[str] = set()


def _log(msg: str) -> None:
    print(f"[{KTO}] {msg}", file=sys.stderr)


def _log_raz(klucz: str, msg: str) -> None:
    """Komunikat, który ma paść RAZ na proces.

    Brak `staticmap` jest stanem, nie zdarzeniem: gdyby leciał przy każdym
    alercie, log fetchera zamieniłby się w tę jedną linijkę powtórzoną trzysta
    razy, a realne błędy utonęłyby między nimi.
    """
    if klucz not in _ostrzezenia:
        _ostrzezenia.add(klucz)
        _log(msg)


def wlaczone() -> bool:
    """Czy obrazki mają w ogóle iść. MAPY_W_ALERTACH=0 = alerty tekstowe jak dotąd."""
    return bool(settings.MAPY_W_ALERTACH)


def _biblioteka():
    """Moduł `staticmap` albo None — i JEDEN komunikat w logu, gdy go nie ma.

    Paczka jest OPCJONALNA dokładnie tak samo jak `pywebpush` (patrz
    requirements.txt): ciągnie Pillow, czyli kompilację na części VPS-ów, a
    deploy bez obrazków ma działać bez niej. Import jest leniwy, więc jej brak
    nie może wywalić importu `powiadomienia`.
    """
    try:
        import staticmap  # noqa: PLC0415 — leniwie, jak każda opcjonalna paczka w repo
    except ImportError:
        _log_raz("brak-staticmap",
                 "brak staticmap — alerty lecą bez podglądu trasy "
                 "(pip install staticmap). To nie jest awaria.")
        return None
    except Exception as e:  # noqa: BLE001 — połamana instalacja to też tylko brak obrazków
        # Pillow bez swoich bibliotek systemowych potrafi rzucić przy imporcie
        # czymś innym niż ImportError. Skutek dla alertu jest ten sam.
        _log_raz("zly-staticmap",
                 f"staticmap nie importuje się ({type(e).__name__}: {str(e)[:120]}) "
                 "— alerty lecą bez podglądu trasy")
        return None
    return staticmap


def _bok(nazwa: str, wartosc: int) -> int:
    """Wymiar obrazka przycięty do zakresu, o którym da się powiedzieć, że działa."""
    if MIN_BOK <= wartosc <= MAX_BOK:
        return wartosc
    poprawiony = max(MIN_BOK, min(MAX_BOK, wartosc))
    _log_raz(f"bok-{nazwa}",
             f"{nazwa}={wartosc} poza zakresem {MIN_BOK}-{MAX_BOK} "
             f"— używam {poprawiony}. Popraw .env.")
    return poprawiony


def wymiary() -> tuple[int, int]:
    return _bok("MAPA_W", settings.MAPA_W), _bok("MAPA_H", settings.MAPA_H)


# ---------------------------------------------------------------------------
# CACHE — katalog i klucz
# ---------------------------------------------------------------------------
def katalog() -> Path | None:
    """Katalog na obrazki albo None, gdy nie ma gdzie pisać.

    Najpierw katalog pakietu — tam, gdzie leży `.env` i pliki stanu rotacji
    kluczy Apify, więc jedno miejsce na „stan tej maszyny". Gdy jest tylko do
    odczytu (deploy z kodem na read-only), schodzimy do katalogu tymczasowego:
    cache w /tmp gubi się przy reboocie i to jest w porządku, bo to tylko
    kafelki, które pobierzemy jeszcze raz.
    """
    for kandydat in (settings.BASE_DIR / ".mapy_cache",
                     Path(tempfile.gettempdir()) / "laweta-radar-mapy"):
        try:
            kandydat.mkdir(parents=True, exist_ok=True)
            proba = kandydat / ".zapis"
            proba.write_text("", encoding="utf-8")
            proba.unlink()
            return kandydat
        except OSError as e:  # noqa: PERF203 — dwie próby, nie pętla po tysiącu
            _log_raz(f"katalog-{kandydat}",
                     f"katalog cache {kandydat} nie działa ({type(e).__name__}) "
                     "— próbuję dalej")
    _log_raz("katalog-brak", "nie ma gdzie zapisać obrazka — alerty lecą tekstem")
    return None


def klucz(odbior, dostawa, szer: int, wys: int) -> str:
    """Nazwa pliku dla tej trasy. Ta sama trasa w dwóch postach = jeden klucz.

    TRZY MIEJSCA PO PRZECINKU to ok. 100 m — poniżej tego progu obrazek jest
    piksel w piksel ten sam, więc dokładniejszy klucz oznaczałby wyłącznie
    ponowne pobranie tych samych kafelków.

    W kluczu siedzą też WYMIARY i PEWNOŚĆ obu punktów, bo obie zmieniają
    zawartość pliku: po zmianie MAPA_W stary obrazek ma stary rozmiar,
    a punkt niepewny ma pomarańczowy marker zamiast zielonego. Bez tego cache
    oddawałby obrazek, który nie zgadza się z podpisem pod nim.
    """
    def opis(p) -> str:
        return f"{p.lat:.3f}_{p.lng:.3f}_{'n' if p.niepewny else 'p'}"

    return f"trasa_{opis(odbior)}__{opis(dostawa)}_{szer}x{wys}"


def sprzataj(kat: Path, teraz: float | None = None) -> int:
    """Skasuj obrazki starsze niż DNI_CACHE. Zwraca ile poszło do kosza.

    Wołane przy CHYBIENIU w cache, nie przy każdym alercie: trafienie ma być
    najtańszą ścieżką w tym module (jedno `stat`), a nowe trasy pojawiają się
    codziennie, więc sprzątanie i tak chodzi regularnie.

    Wzorzec `trasa_*` łapie też pliki `.tmp` po procesie ubitym w środku
    zapisu — nikt ich już nie podmieni na gotowy obrazek, więc zostają
    wyłącznie po to, żeby zajmować dysk.

    Nigdy nie rzuca — pełny katalog cache jest problemem dysku, nie alertu.
    """
    granica = (teraz or time.time()) - DNI_CACHE * 86400
    skasowane = 0
    try:
        for plik in kat.glob("trasa_*"):
            try:
                if plik.stat().st_mtime < granica:
                    plik.unlink()
                    skasowane += 1
            except OSError:
                continue
    except OSError as e:
        _log(f"sprzątanie cache pominięte: {type(e).__name__}")
        return skasowane
    if skasowane:
        _log(f"cache: skasowano {skasowane} obrazków starszych niż {DNI_CACHE} dni")
    return skasowane


# ---------------------------------------------------------------------------
# RENDER
# ---------------------------------------------------------------------------
def _kolor(punkt, domyslny: str) -> str:
    """Pomarańczowy, gdy lokalizacja jest zgadywana — patrz nota przy kolorach."""
    return KOLOR_NIEPEWNY if punkt.niepewny else domyslny


def _rysuj(lib, odbior, dostawa, cel: Path) -> None:
    """Pobierz kafelki, narysuj trasę, zapisz plik. RZUCA — łapie wołający.

    `staticmap` przyjmuje współrzędne w kolejności (lng, lat), czyli ODWROTNEJ
    niż `geo.Punkt` i niż wszystko inne w tym repo. Zamiana miejscami daje
    obrazek gdzieś na Oceanie Indyjskim, ale wygląda na poprawny — dlatego
    konwersja siedzi w jednym miejscu, tuż obok tej uwagi.

    ZAPIS JEST ATOMOWY (plik tymczasowy + `os.replace`). Bez tego generowanie
    przerwane po przekroczeniu budżetu zostawiałoby w cache'u obcięty PNG,
    a następny alert wysłałby go jako gotowy obrazek. Nazwa pliku tymczasowego
    niesie PID, bo cron potrafi nałożyć dwa przebiegi fetchera na siebie —
    a dwa procesy piszące do jednej nazwy dają plik przeplatany z obu.
    """
    szer, wys = wymiary()
    mapa = lib.StaticMap(szer, wys, url_template=KAFELKI,
                         headers={"User-Agent": UZYTKOWNIK},
                         # Sufit na POJEDYNCZY kafelek. Bez niego jeden wiszący
                         # serwer trzyma wątek długo po tym, jak alert poszedł
                         # tekstem — a wątek trzymający gniazdo opóźnia wyjście
                         # procesu fetchera.
                         tile_request_timeout=BUDZET_S)
    xy_odbior = (odbior.lng, odbior.lat)
    xy_dostawa = (dostawa.lng, dostawa.lat)

    # Linia PRZED markerami: rysowane są w kolejności dodania, więc odwrotnie
    # trasa przecięłaby markery na pół.
    mapa.add_line(lib.Line((xy_odbior, xy_dostawa), KOLOR_TRASY, GRUBOSC_TRASY))
    mapa.add_marker(lib.CircleMarker(xy_odbior, _kolor(odbior, KOLOR_ODBIORU),
                                     PROMIEN_MARKERA))
    mapa.add_marker(lib.CircleMarker(xy_dostawa, _kolor(dostawa, KOLOR_DOSTAWY),
                                     PROMIEN_MARKERA))

    obraz = mapa.render()
    tmp = cel.with_name(f"{cel.name}.{os.getpid()}.tmp")
    # Format JAWNIE: nazwa tymczasowa kończy się na `.tmp`, więc Pillow nie ma
    # z czego go zgadnąć i bez tego argumentu rzuca.
    obraz.save(str(tmp), "PNG")
    os.replace(tmp, cel)


def podglad_trasy(odbior, dostawa) -> str | None:
    """Ścieżka do obrazka trasy albo None. NIGDY nie rzuca i nigdy nie blokuje
    dłużej niż BUDZET_S.

    None nie jest błędem — patrz lista pięciu normalnych powodów w docstringu
    modułu. Wołający ma wtedy wysłać alert tekstem, dokładnie tak jak dotąd.
    """
    if not wlaczone():
        return None
    # OBA PUNKTY ALBO NIC — ta sama reguła co przy kilometrach (`geo.podsumowanie`).
    # Mapa z jednym punktem nie pokazuje kształtu trasy, tylko sugeruje, że
    # trasa jest znana; przy nierozpoznanym celu myli bardziej, niż pomaga.
    if odbior is None or dostawa is None:
        return None

    lib = _biblioteka()
    if lib is None:
        return None
    kat = katalog()
    if kat is None:
        return None

    szer, wys = wymiary()
    plik = kat / f"{klucz(odbior, dostawa, szer, wys)}.png"
    try:
        if plik.stat().st_size > 0:
            # Odświeżenie mtime = „ta trasa wróciła". Sprzątanie liczy wiek od
            # ostatniego użycia, więc krążący po grupach kurs nie wypada
            # z cache'u w środku swojego życia.
            os.utime(plik, None)
            return str(plik)
    except OSError:
        pass                       # brak pliku albo pusty — generujemy poniżej

    sprzataj(kat)
    return _wygeneruj(lib, odbior, dostawa, plik)


def _wygeneruj(lib, odbior, dostawa, plik: Path) -> str | None:
    """Render pod twardym budżetem czasu.

    OSOBNY WĄTEK, A NIE SAM TIMEOUT NA POBRANIU KAFELKA: kafelków jest
    kilkanaście i limit na każdym z osobna sumuje się do wielokrotności budżetu.
    Liczy się zegar ścienny całości, bo to jego brakuje operatorowi.

    Wątek jest DAEMONEM i celowo go nie ubijamy: gdy skończy po czasie, alert
    już poszedł tekstem, a plik wyląduje w cache'u i następne zlecenie na tej
    samej trasie dostanie obrazek od razu. Nieudany render nie zostawia nic
    (zapis jest atomowy), a proces może się zakończyć, nie czekając na wątek.
    """
    wynik: dict = {}

    def _praca() -> None:
        try:
            _rysuj(lib, odbior, dostawa, plik)
            wynik["ok"] = True
        except Exception as e:  # noqa: BLE001 — obrazek nie ma prawa wywalić alertu
            wynik["blad"] = f"{type(e).__name__}: {str(e)[:200]}"

    start = time.monotonic()
    watek = threading.Thread(target=_praca, name="mapa-render", daemon=True)
    watek.start()
    watek.join(BUDZET_S)

    if watek.is_alive():
        _log(f"generowanie mapy przekroczyło {BUDZET_S:.0f} s — alert leci tekstem "
             "(obrazek dojdzie do cache'u i posłuży następnemu zleceniu)")
        return None
    if "blad" in wynik:
        _log(f"mapa pominięta: {wynik['blad']}")
        return None
    _log(f"mapa gotowa w {time.monotonic() - start:.1f} s: {plik.name}")
    return str(plik)


# ---------------------------------------------------------------------------
# CLI — sprawdzenie, czy Z TEJ MASZYNY da się pobrać kafelki, BEZ wysyłania
# czegokolwiek na Telegram. Diagnostyka sytuacji „alerty przychodzą, ale bez
# obrazka": rozdziela brak paczki, blokadę OSM i literówkę w .env.
#
#   python -m laweta_radar.services.mapa Krosno Rzeszow
#   python -m laweta_radar.services.mapa 38-400 35-001
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    from laweta_radar.services import geo  # noqa: PLC0415 — tylko dla CLI

    if len(argv) < 3:
        print("użycie: python -m laweta_radar.services.mapa <odbiór> <dostawa>",
              file=sys.stderr)
        return 2

    punkty = []
    for surowy in argv[1:3]:
        punkt = (geo.geokoduj(surowy, None) if geo.czy_kod_pocztowy(surowy)
                 else geo.geokoduj(None, surowy))
        if punkt is None:
            print(f"nie rozpoznaję {surowy!r} — bez obu punktów nie ma obrazka",
                  file=sys.stderr)
            return 1
        punkty.append(punkt)

    szer, wys = wymiary()
    print(f"MAPY_W_ALERTACH={settings.MAPY_W_ALERTACH}, rozmiar {szer}x{wys}, "
          f"cache: {katalog()}")
    for etykieta, p in zip(("odbiór", "dostawa"), punkty):
        print(f"{etykieta}: {p.nazwa} [{p.zrodlo}] {p.wspolrzedne()}"
              + ("   <-- POMARAŃCZOWY MARKER (lokalizacja zgadywana)"
                 if p.niepewny else ""))

    sciezka = podglad_trasy(*punkty)
    print(f"obrazek: {sciezka or 'BRAK — alert poszedłby tekstem'}")
    return 0 if sciezka else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
