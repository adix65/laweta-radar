"""Pobieranie postów z grup FB przez Apify -> bramka -> baza. Chodzi z crona.

Adaptacja `workers/apify_fb_fetcher.py` z repo sales-core-engine. Architektura
i mechanizmy zostają — zmienia się cel (zlecenia dla lawety zamiast leadów
sprzedażowych) i tempo (minuty zamiast pół godziny). Przeniesione bez zmian, bo
są sprawdzone produkcyjnie i nie ma powodu ich ulepszać:

  - wywołanie actora `apify/facebook-groups-scraper` przez REST (bez pakietu
    apify-client — jedno wołanie HTTP wystarcza),
  - defensywne wyciąganie pól (`_first_str`, `_author_name`, `_parse_post_date`):
    actor zmienia kształt odpowiedzi między wersjami, a ta warstwa to amortyzator,
  - dedup po `fb_id = sha256(tresc)[:16]`,
  - rotacja kluczy przez `KeyRotator`, wyjście przez proxy przypisane do klucza,
  - bezpieczniki: brak tokenu / pusta lista grup / brak migracji = CZYSTE wyjście,
  - filtr `status=="ok"` na liście grup (`config/groups.grupy_do_pobrania`).

=== CO JEST TU INNE, I DLACZEGO ==============================================

1. BUDŻET LICZY SIĘ W POBRANYCH POSTACH, NIE W RUNACH.

   Apify rozlicza tego actora za POBRANY POST. Run jest darmowy, jego zawartość
   nie. Stąd cała reszta:

     • `resultsLimit=15` co 20 minut to piętnaście opłaconych postów po to, żeby
       dostać średnio półtora nowego. Dziewięćdziesiąt procent pieniędzy idzie
       na posty, które widzieliśmy dwadzieścia razy.
     • Deduplikacja w bazie NIE ratuje — za pobranie zapłacono, zanim dedup
       cokolwiek zobaczył. Chroni model i Telegram, nie rachunek.
     • Batchowanie grup w jednym runie nie oszczędza kredytu, tylko narzut
       uruchomienia. Nie ma po co budować wokół tego architektury (a przy
       globalnym `resultsLimit` byłoby wręcz szkodliwe — patrz pomiar, pytanie 2).

   GŁÓWNA DŹWIGNIA TO `onlyPostsNewerThan`, NIE CZĘSTOTLIWOŚĆ — o ile actor to
   pole honoruje. Rozstrzyga to pomiar (`docs/POMIAR-ACTORA.md`), a fetcher
   CZYTA jego werdykt zamiast zgadywać (`wykryj_sciezke`).

   ŚCIEŻKA A — actor przyjmuje okna poniżej doby:
     okno = odstęp grupy × 2 (zapas na opóźnienie moderacji), minimum 30 minut.
     `resultsLimit` może być hojny, bo actor przerwie paginację na warunku wieku
     i tak go nie zużyje. Hojny limit jest tu ZALETĄ: chroni przed zgubieniem
     paczki postów wypuszczonych przez moderatora naraz.

   ŚCIEŻKA B — actor przyjmuje tylko pełne dni:
     okno = „1 day", a CAŁA kontrola kosztu przechodzi na `resultsLimit`. Każdy
     punkt limitu to wydane pieniądze, świeżość dociskamy po stronie Pythona,
     a odstępy są szersze (MIN_INTERWAL_MIN_B), bo tu częstotliwość boli wprost
     proporcjonalnie.

   BEZ POMIARU SCHODZIMY NA B. Nie dlatego, że jest bardziej prawdopodobna —
   dlatego, że pomyłka w tę stronę kosztuje trochę nadmiarowego pobierania,
   a pomyłka w drugą (hojny limit 50 przy ignorowanym oknie) to pięćdziesiąt
   opłaconych postów na grupę na przebieg.

   ROZDZIAŁ BUDŻETU MIĘDZY GRUPY ROBI BANDYTA (`services/bandit.py`), a nie
   heurystyka. To jest dokładnie problem, do którego Thompson Sampling służy:
   dzielimy ograniczoną pulę między źródła o nieznanej i zmiennej wydajności,
   nie przestając eksplorować. Wydajność = zlecenia z grupy / pobrane posty
   z grupy w oknie siedmiu dni.

   Zysk uboczny, który jest właściwie głównym: po dwóch tygodniach system sam
   pokaże, które grupy są warte pieniędzy, a które tylko paliły budżet.

2. BRAK OKNA NOCNEGO. Repo źródłowe milczy 00:00-07:00. Tutaj to byłby błąd:
   auto psuje się o trzeciej w nocy i wtedy jest najmniej konkurencji. System
   chodzi 24/7. Ciszę nocną robimy po stronie POWIADOMIEŃ — zebrać zlecenie
   i nie budzić nim człowieka to dwie różne decyzje.

3. ŚWIEŻOŚĆ W GODZINACH, NIE W DNIACH (`MAX_WIEK_POSTA_H`, domyślnie 6). Post
   starszy trafia do bazy ze znacznikiem `stale`, nie idzie do modelu i nie
   generuje powiadomienia. Zapisujemy go, bo jest materiałem do statystyki
   grupy — ale alert o zleceniu sprzed sześciu godzin uczy operatora ignorować
   alerty, a to psuje jedyny kanał, jaki ten system ma.

4. BRAMKA PRZED MODELEM. Każdy post przechodzi przez `workers.gate.gate()`
   ZANIM ktokolwiek zapłaci za token. Odrzucone lądują w bazie z
   `zrodlo_decyzji='gate'`, `czy_zlecenie=false` i statusem `smiec`. To jest
   cała oszczędność tego systemu — nie skrót, tylko powód, dla którego stać nas
   na klasyfikator.

   UWAGA — DOPÓKI BRAMKA JEST W TRYBIE CIENIA (`GATE_TRYB=cien`, domyślnie),
   ŻADNEJ OSZCZĘDNOŚCI NIE MA. W cieniu bramka liczy i zapisuje swoją opinię,
   ale niczego nie blokuje: wszystkie posty idą do modelu. Fetcher decyduje po
   `przepusc` (decyzja operacyjna, uwzględnia tryb), a do bazy zapisuje
   `werdykt` (opinia, niezależna od trybu) — i to z tych par raport
   `scripts/raport_gate.py` liczy jedyną liczbę, która pozwala bramkę włączyć:
   ile ZLECEŃ by skasowała. Podsumowanie przebiegu wypisuje ją wprost, bo bez
   tego tryb cienia wygląda w logach identycznie jak brak bramki.

5. LIMIT ADAPTACYJNY (`_adaptive_group_params`) zostaje, ale okna przeliczone
   z dni na godziny. Zostaje też korekta na krótką historię grupy: bez niej
   `recent_count` dzielony przez pełne okno zaniża tempo kilkunastokrotnie,
   limit spada do podłogi i paczka postów zatwierdzona przez moderatora naraz
   przepada — a czego nie pobraliśmy, tego nie ma w bazie, więc tempo stoi nisko
   dalej. Ta spirala wraca tu identycznie, tylko szybciej.

=== CLI ======================================================================
    python -m laweta_radar.workers.fb_fetcher                # normalny przebieg
    python -m laweta_radar.workers.fb_fetcher --sucho        # plan i koszt, zero wywołań
    python -m laweta_radar.workers.fb_fetcher --budzet 300   # inny sufit dobowy
    python -m laweta_radar.workers.fb_fetcher --grupa URL    # jedna grupa, bez harmonogramu

Cron (bez okna nocnego — patrz punkt 2):
    */5 * * * * cd /home/ubuntu/laweta-radar && ./venv/bin/python \\
        -m laweta_radar.workers.fb_fetcher >> /var/log/laweta/fetcher.log 2>&1
Cron może chodzić gęściej niż odstęp grupy — harmonogram i tak przepuści tylko
te grupy, którym wypada. Odwrotnie się nie da: cron rzadszy niż MIN_INTERWAL_MIN
jest sufitem, którego harmonogram nie przeskoczy.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from laweta_radar.config import groups as cfg_groups
from laweta_radar.config import settings
from laweta_radar.services import bandit
from laweta_radar.workers import apify_keys, apify_proxy, gate
from laweta_radar.workers.apify_keys import AllKeysExhausted, KeyRotator, load_apify_tokens

KTO = "fb-fetcher"
API = "https://api.apify.com/v2"

# Raport z pomiaru actora. Fetcher tylko go CZYTA — pisze go
# `scripts/pomiar_actora.py`, uruchamiany ręcznie.
RAPORT_POMIARU = Path(__file__).resolve().parent.parent.parent / "docs" / "POMIAR-ACTORA.md"

# Werdykt pomiaru czytamy z raportu regeksem, a nie z ręcznie przepisanej
# zmiennej: przepisanie jest krokiem, który da się pominąć — i wtedy fetcher
# pracuje na cudzej intuicji zamiast na pomiarze, nie mówiąc o tym ani słowem.
#
# DWA WARUNKI, i drugi jest ważniejszy od pierwszego. Zaślepka raportu OPISUJE
# obie ścieżki prozą („**ŚCIEŻKA A** — okno działa: ..."), więc sam regeks na
# nazwę ścieżki trafiłby w wyjaśnienie i odczytał je jako werdykt — akurat ten
# hojniejszy, czyli najdroższy z możliwych błędów. Dlatego:
#   1. obecność ramki „POMIAR NIE ZOSTAŁ" unieważnia cokolwiek innego w pliku,
#   2. werdykt bierzemy WYŁĄCZNIE z formy, którą generuje skrypt pomiarowy
#      (`**ŚCIEŻKA A.**` — z kropką w środku pogrubienia), a nie z prozy.
_WZORZEC_SCIEZKI = re.compile(r"\*\*ŚCIEŻKA\s+([AB])\.\*\*")
_WZORZEC_ZASLEPKI = re.compile(r"POMIAR NIE ZOSTA[ŁL]")


# ---------------------------------------------------------------------------
# Ścieżka A / B — czyli czy wolno nam pobierać sam przyrost
# ---------------------------------------------------------------------------
def wykryj_sciezke(raport: Path | None = None, nadpisanie: str | None = None
                   ) -> tuple[str, str]:
    """('A'|'B', skąd to wiemy). Kolejność: .env -> raport z pomiaru -> B.

    B jako domyślna NIE jest wyborem statystycznym, tylko asymetrią kosztów.
    Przy założeniu B i rzeczywistości A tracimy część oszczędności. Przy
    założeniu A i rzeczywistości B płacimy pełny `resultsLimit` (do 50 postów)
    z każdej grupy w każdym przebiegu — czyli rząd wielkości więcej, i to bez
    żadnego objawu poza rachunkiem na koniec miesiąca.
    """
    wybor = (nadpisanie if nadpisanie is not None else settings.SCIEZKA_ACTORA).strip().upper()
    if wybor in ("A", "B"):
        return wybor, "nadpisane w .env (SCIEZKA_ACTORA)"

    plik = raport if raport is not None else RAPORT_POMIARU
    try:
        tresc = plik.read_text(encoding="utf-8")
    except OSError:
        return "B", f"brak raportu {plik.name} — domyślna, ostrożna"

    if _WZORZEC_ZASLEPKI.search(tresc):
        return "B", f"{plik.name} to zaślepka (pomiar NIE wykonany) — domyślna, ostrożna"

    trafienie = _WZORZEC_SCIEZKI.search(tresc)
    if trafienie:
        return trafienie.group(1), f"z pomiaru ({plik.name})"
    return "B", (f"{plik.name} nie zawiera rozstrzygnięcia pomiaru "
                 f"— domyślna, ostrożna")


def _parametry_sciezki(sciezka: str) -> tuple[int, int]:
    """(minimalny odstęp w minutach, sufit postów na grupę) dla ścieżki."""
    if sciezka == "A":
        return cfg_groups.MIN_INTERWAL_MIN_A, cfg_groups.MAX_POSTOW_NA_GRUPE_A
    return cfg_groups.MIN_INTERWAL_MIN_B, cfg_groups.MAX_POSTOW_NA_GRUPE_B


# ---------------------------------------------------------------------------
# Apify — wołanie actora
# ---------------------------------------------------------------------------
def _okno_dla_apify(okno_min: int, sciezka: str) -> str:
    """Wartość pola `onlyPostsNewerThan` — format zależny od ścieżki z pomiaru.

    W ścieżce B wysyłamy „1 day" i nic poniżej: actor albo takich okien nie
    obsługuje, albo je ignoruje, a wysyłanie wartości, której nie rozumie, to
    najgorszy z wariantów — run bez filtra za pełną cenę, bez błędu i bez śladu.
    """
    if sciezka != "A":
        return "1 day"
    if okno_min % 60 == 0 and okno_min >= 60:
        godziny = okno_min // 60
        return f"{godziny} hour" if godziny == 1 else f"{godziny} hours"
    return f"{okno_min} minutes"


def _build_actor_input(group_url: str, limit: int, okno_min: int, sciezka: str) -> dict:
    """Wejście actora dla JEDNEJ grupy.

    Jedna grupa na wywołanie, nie batch. Batchowanie oszczędza wyłącznie narzut
    uruchomienia (posty i tak są liczone pojedynczo), a przy globalnym
    `resultsLimit` gubi posty z większości grup w paczce — i tak też zostaje
    policzone. Dopóki pomiar nie pokaże, że limit jest PER GRUPA, batch jest
    ryzykiem bez zysku.

    `sortingOrder` musi zostać przy `resultsLimit`: limit działa u tego actora
    tylko przy sortowaniu po nowości.
    """
    return {
        "startUrls": [{"url": group_url}],
        "resultsLimit": limit,
        "sortingOrder": cfg_groups.APIFY_SORT,
        "onlyPostsNewerThan": _okno_dla_apify(okno_min, sciezka),
    }


def _apify_run_group(group_url: str, limit: int, okno_min: int, sciezka: str,
                     token: str, *, proxy: str | None = None,
                     cfg: apify_proxy.ProxyConfig | None = None) -> list[dict]:
    """Synchroniczne wywołanie actora dla jednej grupy -> lista surowych itemów.

    `run-sync-get-dataset-items` oddaje od razu zawartość datasetu, bez
    pollowania. Token leci w NAGŁÓWKU (nie w URL-u — URL-e trafiają do logów),
    a ruch przez proxy PRZYPISANE DO TEGO KLUCZA: bez tego cała pula kont wychodzi
    z jednego adresu VPS-a, co dla Apify wygląda jak multi-accounting i kończy się
    utratą całej puli naraz, nie jednego konta.

    `proxy` — gdy podane, WYMUSZA konkretny adres (`apify_proxy.client_for_proxy`)
    zamiast domyślnego, lepkiego wyboru dla tokenu. Używa tego WYŁĄCZNIE pętla
    samoleczenia (`_apify_run_group_samoleczaca`) do próby KOLEJNEGO proxy po
    awarii transportu — bez tego druga próba tym samym kluczem zawsze wracałaby
    na to samo, padnięte wyjście.

    `cfg` — konfiguracja proxy z `run()`, policzona RAZ dla wszystkich żywych
    kluczy tego przebiegu (`apify_proxy.load_proxy_config(tokens=tokeny_zywe)`),
    czyli z WYRÓWNANIEM po hashu (`cfg.balanced` — sekcja 4 zadania „większa pula
    proxy"). `None` (domyślnie) = `client_for_token` sam dociągnie świeżą
    konfigurację BEZ wyrównania — zgodność wstecz dla wołających spoza `run()`.

    Błędy HTTP lecą wyżej NIETKNIĘTE — po nich `apify_keys` poznaje wyczerpany
    klucz i odróżnia go od chwilowej awarii proxy. Opakowanie ich we własny
    wyjątek zamieniłoby rotację kluczy w zgadywankę.
    """
    url = f"{API}/acts/{cfg_groups.APIFY_ACTOR}/run-sync-get-dataset-items"
    klient_cm = (apify_proxy.client_for_proxy(proxy, timeout=cfg_groups.APIFY_TIMEOUT)
                if proxy is not None else
                apify_proxy.client_for_token(token, timeout=cfg_groups.APIFY_TIMEOUT, cfg=cfg))
    with klient_cm as klient:
        odp = klient.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=_build_actor_input(group_url, limit, okno_min, sciezka),
        )
    odp.raise_for_status()
    dane = odp.json()
    if isinstance(dane, list):
        return [it for it in dane if isinstance(it, dict)]
    # Obronnie: gdyby kształt odpowiedzi się zmienił (np. {"items": [...]}).
    if isinstance(dane, dict) and isinstance(dane.get("items"), list):
        return [it for it in dane["items"] if isinstance(it, dict)]
    return []


# ---------------------------------------------------------------------------
# Samoleczenie — próba KOLEJNEGO proxy tego samego klucza po awarii transportu
# (sekcja 4 zadania „samolecząca się pula"). Klucz martwy / bez kredytu / rate
# limit NIE mają tu czego robić — to decyzje na poziomie KLUCZA, rozstrzyga je
# `KeyRotator`/`classify_apify_error` wyżej w stosie; ta funkcja łapie
# WYŁĄCZNIE błąd transportu (`apify_keys.STATUS_BLAD_SIECI`).
# ---------------------------------------------------------------------------
_PROXY_PROBY_SAMOLECZENIA = 3


def _polacz_best_effort():
    """Krótkie połączenie tylko do odczytu/zapisu stanu klucza/proxy — degraduje
    do None zamiast rzucać. Samoleczenie ma być USPRAWNIENIEM, nie warunkiem
    runu: brak bazy nie może zablokować pobierania postów."""
    if not settings.DATABASE_URL:
        return None
    try:
        import psycopg2  # noqa: PLC0415 — leniwie, jak wszędzie w tym repo

        return psycopg2.connect(settings.DATABASE_URL, connect_timeout=5)
    except Exception:  # noqa: BLE001
        return None


def _apify_run_group_samoleczaca(group_url: str, limit: int, okno_min: int, sciezka: str,
                                 token: str, cfg: apify_proxy.ProxyConfig | None = None,
                                 log=print) -> list[dict]:
    """Jak `_apify_run_group`, ale przy błędzie SIECI/PROXY próbuje TEGO SAMEGO
    klucza z KOLEJNYM proxy (max `_PROXY_PROBY_SAMOLECZENIA`), a padnięty adres
    ląduje w kwarantannie na 30 min (`apify_proxy.oznacz_kwarantanna`).

    PIERWSZA próba idzie zwykłą, lepką ścieżką BEZ dotykania bazy — baza wchodzi
    do gry dopiero PO pierwszej awarii transportu, żeby happy path (99% wywołań)
    nie płacił za nic. Bez proxy w ogóle (`proxy_for_token` zwraca None) nie ma
    czym „przepiąć" — błąd jest wtedy realną awarią sieci, nie proxy, i leci
    wyżej bez prób dodatkowych.

    `cfg` (patrz `_apify_run_group`) idzie też do `proxy_for_token` PONIŻEJ —
    inaczej „padnięty adres" liczony tutaj (świeżym, NIEwyrównanym rendezvous
    hashingiem) mógłby różnić się od adresu, którym REALNIE poszła pierwsza
    próba (przez `cfg` z wyrównaniem) — i do kwarantanny trafiłby zły adres.
    """
    try:
        return _apify_run_group(group_url, limit, okno_min, sciezka, token, cfg=cfg)
    except Exception as e:  # noqa: BLE001 — klasyfikujemy poniżej
        if apify_keys.classify_apify_error(e) != apify_keys.STATUS_BLAD_SIECI:
            raise
        proxy_padniety = apify_proxy.proxy_for_token(token, cfg)
        if proxy_padniety is None:
            raise
        ostatni_blad = e

    for proba in range(2, _PROXY_PROBY_SAMOLECZENIA + 1):
        conn = _polacz_best_effort()
        if conn is None:
            raise ostatni_blad     # bez bazy nie wiadomo, co jest wolne — oddaj oryginalny błąd
        try:
            apify_proxy.oznacz_kwarantanna(
                conn, proxy_padniety, _jedna_linia(ostatni_blad),
                klucz_hash=apify_keys._hash_klucza(token))
            log(f"[{KTO}] proxy {apify_proxy.proxy_label(proxy_padniety)} padło "
                f"({_jedna_linia(ostatni_blad)}) — kwarantanna 30 min, próbuję "
                f"kolejnego (próba {proba}/{_PROXY_PROBY_SAMOLECZENIA})")
            nastepny_proxy = apify_proxy.proxy_zywy_dla_tokenu(token, conn)
        finally:
            conn.close()
        if nastepny_proxy is None or nastepny_proxy == proxy_padniety:
            raise ostatni_blad     # cała ranga proxy tego klucza w kwarantannie
        try:
            return _apify_run_group(group_url, limit, okno_min, sciezka, token,
                                    proxy=nastepny_proxy, cfg=cfg)
        except Exception as e2:  # noqa: BLE001
            if apify_keys.classify_apify_error(e2) != apify_keys.STATUS_BLAD_SIECI:
                raise
            ostatni_blad = e2
            proxy_padniety = nastepny_proxy
    raise ostatni_blad


def _alert_pula_wyczerpana(tokeny: list[str], log=print) -> None:
    """Telegram + log KRYTYCZNY, gdy `AllKeysExhausted` — to jest AWARIA CAŁEGO
    ŹRÓDŁA DANYCH (żaden klucz nie odpowie teraz), nie zwykłe ostrzeżenie w logu
    crona, które nikt nie czyta, dopóki nie zapyta ktoś inny „czemu cisza".

    Liczby (żywych kluczy, stanu proxy) są best-effort z bazy — brak bazy albo
    błąd diagnostyki NIE mają prawa zablokować samego alertu: operator ma
    dostać wiadomość, nawet niepełną, zamiast żadnej.
    """
    total = len(tokeny)
    zywe_ile = total
    proxy_opis = "proxy: stan nieznany (brak bazy)"
    conn = _polacz_best_effort()
    if conn is not None:
        try:
            zywe_ile = len(apify_keys.klucze_zywe(conn, tokeny))
            cfg = apify_proxy.load_proxy_config()
            if cfg.pool:
                stan_proxy = apify_proxy.wczytaj_stan_proxy(conn, cfg.pool)
                w_kwarantannie = sum(1 for s in stan_proxy.values()
                                     if s["status"] == "kwarantanna")
                proxy_opis = (f"proxy: {len(cfg.pool) - w_kwarantannie} aktywnych "
                             f"z {len(cfg.pool)} (w kwarantannie: {w_kwarantannie})")
            else:
                proxy_opis = "proxy: bez puli (APIFY_PROXY_URLS) — patrz /limity"
        except Exception:  # noqa: BLE001 — alert ma pójść nawet, gdy diagnostyka padnie
            pass
        finally:
            conn.close()

    log(f"[{KTO}] KRYTYCZNE: PULA APIFY WYCZERPANA — {zywe_ile}/{total} żywych "
        f"kluczy. {proxy_opis}. Kolejne grupy w tym przebiegu NIE ZOSTANĄ pobrane.")
    tekst = (f"🆘 *PULA APIFY WYCZERPANA*\n\n"
            f"Żaden z {total} kluczy nie odpowiada teraz — pobieranie w tym "
            f"przebiegu się zatrzymało.\n\n"
            f"Żywych kluczy: {zywe_ile}/{total}\n{proxy_opis}\n\n"
            f"Sprawdź /limity na Telegramie albo:\n"
            f"`python -m laweta_radar.workers.apify_keys`")
    try:
        from laweta_radar.services import telegram_notify  # noqa: PLC0415 — leniwie, jak _powiadom

        telegram_notify.wyslij(tekst)
    except Exception as e:  # noqa: BLE001 — alert nie może dorzucić drugiego błędu
        log(f"[{KTO}] nie udało się wysłać alertu o wyczerpanej puli: {_jedna_linia(e)}")


def _alert_pula_proxy_wyczerpana(cfg: apify_proxy.ProxyConfig, log=print) -> None:
    """Telegram + log KRYTYCZNY, gdy WSZYSTKIE proxy z puli są NARAZ w
    kwarantannie (sekcja 3 zadania „większa pula proxy") — inaczej niż pojedynczy
    padnięty adres (ten obsługuje samoleczenie w `_apify_run_group_samoleczaca`),
    to jest stan, w którym ŻADEN klucz nie ma czym pojechać bez wyjścia z gołego
    IP VPS-a. `run()` woła to WYŁĄCZNIE, gdy `APIFY_PROXY_REQUIRED=1` — bez tej
    flagi brak żywych proxy jest dozwolonym (choć gorszym) stanem.
    """
    log(f"[{KTO}] KRYTYCZNE: WSZYSTKIE proxy z puli ({len(cfg.pool)}) są w "
        f"kwarantannie naraz — przerywam przebieg, żeby nie wyjść z gołego IP VPS-a.")
    tekst = (f"🆘 *PULA PROXY WYCZERPANA*\n\n"
            f"Wszystkie {len(cfg.pool)} adresów proxy są teraz w kwarantannie — "
            f"pobieranie się NIE ROZPOCZĘŁO (APIFY_PROXY_REQUIRED=1 blokuje wyjście "
            f"z IP VPS-a).\n\n"
            f"Sprawdź /limity na Telegramie albo:\n"
            f"`python -m laweta_radar.workers.apify_proxy --check`")
    try:
        from laweta_radar.services import telegram_notify  # noqa: PLC0415 — leniwie, jak _powiadom

        telegram_notify.wyslij(tekst)
    except Exception as e:  # noqa: BLE001 — alert nie może dorzucić drugiego błędu
        log(f"[{KTO}] nie udało się wysłać alertu o wyczerpanej puli proxy: {_jedna_linia(e)}")


# Próg żywych kluczy, poniżej którego pula jest o JEDNĄ awarię od zera.
PROG_MIN_ZYWYCH_KLUCZY = 2

# Trzy poziomy hałasu ALERT_DEGRADACJA_APIFY — patrz .env.example. Nieznana
# wartość degraduje do "krytyczny", tak samo jak GATE_TRYB w workers/gate.py:
# literówka w .env ma zawęzić hałas, nie otworzyć go na oścież.
_TRYB_ALERTU_OFF = "off"
_TRYB_ALERTU_KRYTYCZNY = "krytyczny"
_TRYB_ALERTU_ZAWSZE = "zawsze"
_TRYBY_ALERTU_DEGRADACJI = (_TRYB_ALERTU_OFF, _TRYB_ALERTU_KRYTYCZNY, _TRYB_ALERTU_ZAWSZE)


def _tryb_alertu_degradacji() -> str:
    """ALERT_DEGRADACJA_APIFY znormalizowany. Nieznana/pusta wartość -> "krytyczny"."""
    s = settings.ALERT_DEGRADACJA_APIFY.strip().lower()
    return s if s in _TRYBY_ALERTU_DEGRADACJI else _TRYB_ALERTU_KRYTYCZNY


def _alert_jesli_zdegradowana(tokeny_zywe: list[str], tokeny: list[str], log=print) -> None:
    """WCZESNE ostrzeżenie (run jedzie dalej — część kluczy jeszcze działa),
    nie awaria: operator ma się dowiedzieć, ZANIM pula spadnie do zera i
    zadziała `_alert_pula_wyczerpana`. Sekcja 5 zadania „samolecząca się pula":

        żywych kluczy < PROG_MIN_ZYWYCH_KLUCZY               [KRYTYCZNY powód]
        żywych proxy < liczba kluczy (mniej wyjść niż kont — korelacja rośnie)
        którykolwiek klucz oznaczony jako martwy (zwykle ban — wymaga człowieka)

    Best-effort: bez bazy funkcja nic nie robi (nie ma czego sprawdzić) —
    diagnostyka nie może zablokować runu, tak samo jak w `_alert_pula_wyczerpana`.

    ALERT_DEGRADACJA_APIFY steruje WYŁĄCZNIE tym, czy powyższe budzi telefon —
    stan puli trafia do logu przebiegu ZAWSZE (niezależnie od trybu) i jest
    zawsze widoczny w /limity na żądanie. Przy modelu z pulą darmowych kont
    martwe klucze są normalnym stanem pracy (rotacja sama je pomija), więc
    domyślny tryb "krytyczny" budzi telefon TYLKO dla powodu, który realnie
    zapowiada utratę zleceń — mało żywych kluczy.
    """
    conn = _polacz_best_effort()
    if conn is None:
        return
    try:
        stany = apify_keys.wczytaj_stany(conn, tokeny)
        martwe = [t for t, s in stany.items() if s["status"] == apify_keys.STATUS_KLUCZ_MARTWY]
        cfg = apify_proxy.load_proxy_config()
        zywe_proxy = None
        if cfg.pool:
            stan_proxy = apify_proxy.wczytaj_stan_proxy(conn, cfg.pool)
            w_kwarantannie = sum(1 for s in stan_proxy.values() if s["status"] == "kwarantanna")
            zywe_proxy = len(cfg.pool) - w_kwarantannie
    except Exception as e:  # noqa: BLE001 — diagnostyka nie może zablokować runu
        log(f"[{KTO}] nie udało się sprawdzić kondycji puli: {_jedna_linia(e)}")
        return
    finally:
        conn.close()

    # (opis, czy_krytyczny) — "krytyczny" znaczy: przetrwa filtr trybu "krytyczny".
    powody: list[tuple[str, bool]] = []
    if len(tokeny_zywe) < PROG_MIN_ZYWYCH_KLUCZY:
        powody.append((f"żywych kluczy: {len(tokeny_zywe)} (próg {PROG_MIN_ZYWYCH_KLUCZY})", True))
    if zywe_proxy is not None and zywe_proxy < len(tokeny):
        powody.append((f"żywych proxy: {zywe_proxy} < {len(tokeny)} kluczy", False))
    if martwe:
        powody.append((f"{len(martwe)} kluczy martwych (401) — sprawdź konta w Apify", False))
    if not powody:
        return

    opisy = [p for p, _ in powody]
    # Log przebiegu widzi PEŁNY stan ZAWSZE, niezależnie od ALERT_DEGRADACJA_APIFY
    # — wyciszamy Telegram, nie diagnostykę (ta sama zasada co przy /limity).
    log(f"[{KTO}] UWAGA: pula Apify zdegradowana — {'; '.join(opisy)}.")

    tryb = _tryb_alertu_degradacji()
    if tryb == _TRYB_ALERTU_OFF:
        return
    if tryb == _TRYB_ALERTU_KRYTYCZNY and not any(krytyczny for _, krytyczny in powody):
        return

    tekst = ("⚠️ *Pula Apify zdegradowana*\n\n" + "\n".join(f"• {p}" for p in opisy)
            + "\n\nSprawdź /limity na Telegramie.")
    try:
        from laweta_radar.services import telegram_notify  # noqa: PLC0415 — leniwie, jak _powiadom

        telegram_notify.wyslij(tekst)
    except Exception as e:  # noqa: BLE001 — alert nie może dorzucić drugiego błędu
        log(f"[{KTO}] nie udało się wysłać alertu o degradacji puli: {_jedna_linia(e)}")


# ---------------------------------------------------------------------------
# Wyciąganie pól z surowego itemu — warstwa amortyzująca zmiany actora.
# Przeniesione 1:1 z repo źródłowego: brak pola to nie błąd, tylko None.
# ---------------------------------------------------------------------------
def _first_str(item: dict, *klucze: str) -> str:
    """Pierwsza niepusta wartość tekstowa spod podanych kluczy, albo ''."""
    for k in klucze:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _author_name(item: dict) -> str:
    """Autor posta — płaskie pole albo zagnieżdżony obiekt user/author/owner."""
    plaski = _first_str(item, "authorName", "author", "userName", "ownerName")
    if plaski:
        return plaski
    for k in ("user", "author", "owner"):
        obj = item.get(k)
        if isinstance(obj, dict):
            nazwa = _first_str(obj, "name", "fullName", "title")
            if nazwa:
                return nazwa
    return ""


def _parse_post_date(wartosc) -> datetime | None:
    """Data posta -> aware datetime albo None (brak / nieznany format).

    Obsługuje ISO 8601 (także z „Z") oraz epoch w sekundach i milisekundach.
    None NIE blokuje posta — po prostu nie znamy daty. Blokowanie byłoby tu
    najgorszą reakcją: post bez daty jest zwykle najświeższy, bo to layout
    „przed chwilą", którego actor nie umiał sparsować.
    """
    if wartosc is None:
        return None
    if isinstance(wartosc, (int, float)):
        try:
            ts = float(wartosc)
            if ts > 1e12:          # milisekundy -> sekundy
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    s = str(wartosc).strip()
    if not s:
        return None
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    # Bez offsetu w tekście `fromisoformat` zwraca naive — traktujemy jako UTC,
    # żeby porównanie z progiem świeżości nie wybuchało na naive vs aware.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _extract_post(item: dict, grupa: dict) -> dict | None:
    """Surowy item -> rekord posta, albo None gdy nie ma treści (nie ma czego oceniać).

    URL i nazwę grupy bierzemy przede wszystkim z configu — znamy je na pewno,
    a item bywa niekompletny. Z itemu tylko jako zapasowe źródło.
    """
    tresc = _first_str(item, "text", "postText", "message", "content", "caption")
    if not tresc:
        return None   # np. sam obrazek — bramka i model nie mają czego czytać
    return {
        "tresc": tresc,
        "post_url": _first_str(item, "url", "postUrl", "link",
                               "facebookUrl", "permalink", "topLevelUrl"),
        "group_url": (grupa.get("url") or "").strip() or _first_str(
            item, "groupUrl", "facebookGroupUrl", "groupLink"),
        "group_name": (grupa.get("name") or "").strip() or _first_str(
            item, "groupTitle", "groupName"),
        "author_name": _author_name(item),
        "post_date": _parse_post_date(
            item.get("time") or item.get("date")
            or item.get("timestamp") or item.get("publishedAt")),
    }


def fb_id(tresc: str) -> str:
    """sha256(treść)[:16] — ten sam wzór co w repo źródłowym.

    Hash z SAMEJ TREŚCI, bez URL-a grupy: ta sama prośba o lawetę wklejona na
    pięć grup ma być JEDNYM zleceniem, a nie pięcioma alertami o tej samej
    awarii. Kosztem jest to, że zostaje link do grupy, w której zobaczyliśmy
    post jako pierwsi — czyli do tej, w której jesteśmy najszybsi.
    """
    return hashlib.sha256((tresc or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Baza — odczyt stanu przed przebiegiem
# ---------------------------------------------------------------------------
def _tabela_istnieje(conn, nazwa: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{nazwa}",))
        return bool(cur.fetchone()[0])


# Po jednej kolumnie-świadku na migrację, której dotyka zapis posta. JEDEN
# INSERT niesie i werdykt bramki, i komplet ekstrakcji, więc brak którejkolwiek
# z tych migracji wywala KAŻDY zapis — a to jest komunikat, który ma paść raz,
# przed pobieraniem, a nie kilkaset razy już PO opłaceniu Apify.
KOLUMNY_SWIADKOWIE = (
    ("zrodlo_decyzji", "0003_fetcher.sql"),
    ("pewnosc", "0004_klasyfikacja.sql"),
    ("kategoria_ladunku", "0010_kategoria_ladunku.sql"),
    ("kierunek", "0011_kierunek.sql"),
    ("kierunek_geo", "0013_kierunek_geo.sql"),
)


def _brakujace_migracje(conn) -> list[str]:
    """Których migracji brakuje w `posty` — po nazwach plików, nie po kolumnach."""
    with conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'posty'")
        maja = {r[0] for r in cur.fetchall()}
    return [f"kolumny z {plik} (nie ma `{kolumna}` w `posty`)"
            for kolumna, plik in KOLUMNY_SWIADKOWIE if kolumna not in maja]


def _istniejace_id(conn) -> tuple[set[str], set[str]]:
    """(wszystkie znane fb_id, te do naprawy) — dedup w pamięci na czas przebiegu.

    Jedno zapytanie zamiast SELECT-a per post: przebieg dotyka kilkuset postów,
    a tabela rośnie wolno (kilkanaście tysięcy wierszy na miesiąc), więc komplet
    identyfikatorów mieści się w pamięci bez dyskusji.

    DRUGI ZBIÓR ISTNIEJE PO TO, ŻEBY DEDUP NIE ZAMRAŻAŁ USZKODZONYCH WIERSZY.
    Post z `zrodlo_decyzji='ai'` i kompletem NULL-i to post, za który zapłacono
    tokenami i nie dostano nic. Sam dedup („fb_id już znam") nie odróżnia go od
    posta obsłużonego poprawnie, więc kolejne przebiegi pomijałyby go w
    nieskończoność, a naprawcza ścieżka ON CONFLICT nigdy by się nie wykonała.
    Takie posty przepuszczamy jeszcze raz: klasyfikator dostaje je ponownie
    (koszt tokenów, ale NIE Apify — post i tak przyszedł w tej odpowiedzi),
    a `_zapisz_post` dopisuje wynik do istniejącego wiersza. Po naprawie wiersz
    ma ekstrakcję i wypada z tego zbioru, więc nie płacimy za niego drugi raz.

    Brak kolumn ekstrakcji (klasyfikatora nie ma w drzewie) daje pusty drugi
    zbiór — nie ma czym naprawiać, więc nie ma po co pytać modelu.
    """
    kolumny = _kolumny_ekstrakcji()
    with conn.cursor() as cur:
        cur.execute("SELECT fb_id FROM posty")
        wszystkie = {r[0] for r in cur.fetchall()}
        if not kolumny:
            return wszystkie, set()
        warunek = " AND ".join(f"{k} IS NULL" for k in kolumny)
        cur.execute(f"SELECT fb_id FROM posty "  # noqa: S608 — nazwy ze stałej classifiera
                    f"WHERE zrodlo_decyzji = 'ai' AND {warunek}")
        return wszystkie, {r[0] for r in cur.fetchall()}


def _statystyki_grup(conn, okno_dni: int, okno_tempa_h: int) -> dict[str, dict]:
    """Surowiec dla bandyty i dla limitu adaptacyjnego — jednym zapytaniem.

    Zwraca per grupa:
      pobrane / zlecenia  — wydajność w oknie `okno_dni` (wejście bandyty),
      ostatnie / pierwszy_pobrany_at — tempo postowania w oknie `okno_tempa_h`
                            (wejście limitu adaptacyjnego i odstępu).

    Jedno zapytanie, nie N — korzysta z indeksu (grupa_url, pobrany_at DESC)
    z migracji 0001. Bez niego to pełny skan tabeli, która rośnie o KAŻDY
    pobrany post, także odrzucony przez bramkę.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT grupa_url,
                   COUNT(*) FILTER (
                       WHERE pobrany_at > NOW() - (%s || ' days')::interval
                   ) AS pobrane,
                   COUNT(*) FILTER (
                       WHERE pobrany_at > NOW() - (%s || ' days')::interval
                         AND czy_zlecenie
                   ) AS zlecenia,
                   COUNT(*) FILTER (
                       WHERE pobrany_at > NOW() - (%s || ' hours')::interval
                   ) AS ostatnie,
                   MIN(pobrany_at) AS pierwszy_pobrany_at
            FROM posty
            WHERE grupa_url IS NOT NULL
            GROUP BY grupa_url
            """,
            (okno_dni, okno_dni, okno_tempa_h),
        )
        return {
            r[0]: {"pobrane": r[1], "zlecenia": r[2], "ostatnie": r[3],
                   "pierwszy_pobrany_at": r[4]}
            for r in cur.fetchall()
        }


def _wczytaj_harmonogram(conn, doba: str) -> dict[str, dict]:
    """Stan pobierania per grupa. Licznik z INNEJ doby czytamy jako zero.

    Zerowanie przez porównanie daty, a nie osobnym zadaniem w cronie — zadanie
    da się zapomnieć dodać, a jego awaria o północy zablokowałaby pobieranie na
    całą dobę i wyglądała identycznie jak wyczerpany budżet.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT group_url, ostatni_run_at, nastepny_run_at, doba, "
                    "pobrane_doba, przydzial_doba FROM harmonogram")
        wynik = {}
        for url, ostatni, nastepny, dzien, pobrane, przydzial in cur.fetchall():
            ta_sama_doba = dzien is not None and dzien.isoformat() == doba
            wynik[url] = {
                "ostatni_run_at": ostatni,
                "nastepny_run_at": nastepny,
                "pobrane_doba": pobrane if ta_sama_doba else 0,
                "przydzial_doba": przydzial if ta_sama_doba else None,
            }
        return wynik


def _zapisz_harmonogram(conn, url: str, doba: str, *, pobrane_doba: int,
                        przydzial: int, interwal_min: int,
                        ostatni_run_at: datetime | None,
                        nastepny_run_at: datetime | None,
                        blad: str | None) -> None:
    """Stan grupy po przebiegu. UPSERT, bo grupa może wejść do configu w każdej chwili."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO harmonogram (group_url, ostatni_run_at, nastepny_run_at,
                                     interwal_min, doba, pobrane_doba,
                                     przydzial_doba, ostatni_blad, zmieniony_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (group_url) DO UPDATE SET
                ostatni_run_at  = COALESCE(EXCLUDED.ostatni_run_at,
                                           harmonogram.ostatni_run_at),
                nastepny_run_at = EXCLUDED.nastepny_run_at,
                interwal_min    = EXCLUDED.interwal_min,
                doba            = EXCLUDED.doba,
                pobrane_doba    = EXCLUDED.pobrane_doba,
                przydzial_doba  = EXCLUDED.przydzial_doba,
                ostatni_blad    = EXCLUDED.ostatni_blad,
                zmieniony_at    = NOW()
            """,
            (url, ostatni_run_at, nastepny_run_at, interwal_min, doba,
             pobrane_doba, przydzial, (blad or None)),
        )
    conn.commit()


def _mediana_opoznienia_alertow_min(conn, godziny: int = 24) -> float | None:
    """Mediana (minuty) opublikowany_at -> wyslano_at, z ostatnich `godziny`
    godzin. None, gdy w oknie nie ma ani jednego alertu z obiema datami znanymi
    — inaczej „nie ma z czego liczyć" wyglądałoby jak „mediana zero minut",
    czyli najlepszy możliwy wynik zamiast braku danych.

    PIĄTA POPRAWKA — jedyny sposób, żeby efekt `MIN_INTERWAL_MIN` /
    `PROG_WYKORZYSTANIA_BUDZETU` był widoczny w LOGU, a nie tylko odczuwalny
    (albo nieodczuwalny) na telefonie operatora. Cel: mediana < 10 min
    (`cfg_groups.CEL_MEDIANY_OPOZNIENIA_MIN`) — patrz wywołanie w `run`.

    ŁĄCZYMY `powiadomienia` (wyslano_at — jedyne miejsce z faktem wysyłki)
    i `posty` (opublikowany_at — jedyne miejsce z datą z Facebooka) PO fb_id.
    Bez FK między tabelami (patrz 0006_powiadomienia.sql — wiersze zbiorcze
    mają fb_id NULL), więc JOIN, nie kolumna dopisana do `powiadomienia`.
    WYŁĄCZNIE kanał 'telegram': wiersze zbiorcze/pauza/wznowienie nie niosą
    opóźnienia ŻADNEGO konkretnego zlecenia i rozmyłyby medianę w dowolną
    stronę, zależnie od tego, kiedy akurat poszły.

    ŚWIADOMIE bez odcinania ujemnych opóźnień (zegar FB bywa przed naszym
    o kilkadziesiąt sekund — patrz `powiadomienia.wiek_posta`): to jest metryka
    do diagnozy, nie do pokazania operatorowi, a filtrowanie niewygodnych
    wartości ukrywałoby też PRAWDZIWE problemy z zegarem/danymi zamiast je
    pokazać.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (p.wyslano_at - t.opublikowany_at)) / 60.0
                   )
              FROM powiadomienia p
              JOIN posty t ON t.fb_id = p.fb_id
             WHERE p.kanal = 'telegram'
               AND p.wyslano_at > NOW() - (%s || ' hours')::interval
               AND t.opublikowany_at IS NOT NULL
            """,
            (godziny,),
        )
        (mediana,) = cur.fetchone()
    return float(mediana) if mediana is not None else None


def _kolumny_ekstrakcji() -> tuple[str, ...]:
    """Nazwy płaskich kolumn z ekstrakcji — z klasyfikatora, nie z kopii tutaj.

    Import jest LENIWY z tego samego powodu, dla którego `_klasyfikuj` sprawdza
    `find_spec`: fetcher ma działać także wtedy, gdy klasyfikatora nie ma
    w drzewie. Bez modułu nie ma wyniku do zapisania, więc pusta krotka opisuje
    tę sytuację dokładnie — INSERT schodzi wtedy do kolumn sprzed klasyfikacji.
    """
    import importlib.util  # noqa: PLC0415

    if importlib.util.find_spec("laweta_radar.workers.classifier") is None:
        return ()
    from laweta_radar.workers import classifier  # noqa: PLC0415

    return tuple(classifier.KOLUMNY_EKSTRAKCJI)


def _kolumny_geo() -> tuple[str, ...]:
    """Kraj obu końców trasy i kierunek geograficzny — osobno od `_kolumny_ekstrakcji`.

    CELOWO OSOBNA FUNKCJA, nie dopisanie do `_kolumny_ekstrakcji`: tamta lista
    zasila też `_pusto` (detektor „model odpowiedział, a nic nie wpadło do
    bazy"), a `kierunek_geo` NIGDY nie jest NULL-em, gdy klasyfikator w ogóle
    zadziałał (brak obu krajów to string "nieznany"). Wrzucenie go do tamtej
    listy oślepiłoby ten detektor na dokładnie tę awarię, po którą powstał.
    """
    import importlib.util  # noqa: PLC0415

    if importlib.util.find_spec("laweta_radar.workers.classifier") is None:
        return ()
    from laweta_radar.workers import classifier  # noqa: PLC0415

    return tuple(classifier.KOLUMNY_GEO)


def _wiersz_ekstrakcji(decyzja: "Decyzja", identyfikator: str, tresc: str) -> dict[str, object]:
    """Wynik z `Decyzja` -> płaskie kolumny. Pusty słownik = nie ma czego zapisać.

    `tresc` idzie dalej do `geo.geokoduj` (przez `wiersz_do_zapisu`) wyłącznie
    do rozstrzygania kraju przy nazwie miejscowości występującej w kilku
    krajach — ta sama treść, którą Apify oddał dla TEGO posta.
    """
    if decyzja.wynik_ai is None:
        return {}
    from laweta_radar.workers import classifier  # noqa: PLC0415 — wynik jest, więc moduł też

    return classifier.wiersz_do_zapisu(decyzja.wynik_ai, identyfikator, tresc=tresc)


def _pusto(wiersz: dict[str, object], kolumny: tuple[str, ...]) -> bool:
    """Czy w `wierszu` nie ma ANI JEDNEGO pola ekstrakcji.

    PUSTA LISTA KOLUMN TO TAKŻE PUSTKA, i to najgorszego rodzaju: znaczy, że
    zapis nie miał czego wpisać, bo warstwa SQL nie zna ani jednej kolumny
    ekstrakcji. Wiersz wychodzi wtedy z samym werdyktem, wygląda poprawnie
    i nie ma jak tego zgłosić — dlatego `not kolumny` jest tu równoznaczne
    z utratą, a nie ze stanem neutralnym.

    `is None`, nie fałszywość: `stan_po_wypadku=False`, `pewnosc=0` i `powod=""`
    są POPRAWNYMI wartościami.
    """
    return not kolumny or all(wiersz.get(k) is None for k in kolumny)


@dataclass(frozen=True)
class Zapis:
    """Co po zapisie NAPRAWDĘ stoi w wierszu — odczytane z bazy, nie z pamięci.

    `bez_ekstrakcji` liczy się z `wiersz`, czyli z tego, co zwróciła baza. To
    jest cała różnica między tą wersją a poprzednią: tamta pytała słownik
    zbudowany w Pythonie („czy mam co zapisać"), a nie tabelę („czy się
    zapisało"). Słownik był pełny w KAŻDYM zgłoszonym przypadku — także wtedy,
    gdy INSERT nie wymieniał kolumn ekstrakcji i gdy ON CONFLICT odmówił
    aktualizacji. Ostrzeżenie stało więc na warunku, którego produkcja nie
    potrafiła spełnić, i milczało przy 27 wierszach z kompletem NULL-i.

    `wiersz` idzie dalej do powiadomienia: alert budujemy z tego, co jest
    w bazie, a nie z tego, co mieliśmy nadzieję tam wpisać.
    """

    bez_ekstrakcji: bool
    wiersz: dict[str, object]


def _zapisz_post(conn, identyfikator: str, post: dict, decyzja: "Decyzja",
                 log=print) -> Zapis:
    """Zapis jednego posta — WRAZ z kompletem pól z ekstrakcji, ze SPRAWDZENIEM
    w bazie, że komplet tam dojechał.

    JEDEN INSERT NA POST, nie insert + update. Post trafiający do bazy PRZED
    klasyfikacją i dopisywany drugim zapytaniem to klasyczna pułapka: drugie
    zapytanie ma własne warunki, własną transakcję i własne okazje do porażki,
    a jego brak nie zostawia żadnego objawu — wiersz jest, więc wygląda dobrze.
    Kolumny bramki (`gate_*`) i klasyfikatora wypełniamy więc TU, w momencie,
    w którym fetcher zna komplet. `classifier.SQL_ZAPIS` zostaje dla postów
    HISTORYCZNYCH (klasyfikacja z kolejki `idx_posty_do_klasyfikacji`), gdzie
    wiersz już istnieje i innej drogi nie ma.

    ON CONFLICT: dedup jest darmowy, więc powtórka posta bez werdyktu modelu
    nie robi nic. Ale powtórka Z werdyktem DOPISUJE go do wiersza, który go nie
    miał — inaczej post pobrany, zanim klasyfikator zaczął działać (albo w runie,
    w którym API padło), zostawałby bez klasyfikacji na zawsze, mimo że przy
    kolejnym podejściu za nią zapłaciliśmy. `DO NOTHING` w tym miejscu jest tym
    samym błędem co brak kolumn w INSERT-cie, tylko trudniejszym do zauważenia.

    „GOTOWY WERDYKT" TO WERDYKT Z EKSTRAKCJĄ, nie samo `zrodlo_decyzji='ai'`.
    Poprzedni warunek (`posty.zrodlo_decyzji IS DISTINCT FROM 'ai'`) chronił
    także wiersze, które NIE MIAŁY CZEGO CHRONIĆ: 27 postów z pierwszych
    przebiegów stoi w bazie z werdyktem modelu i kompletem NULL-i, więc ten
    warunek zamroził je na zawsze — ponowny zapis tego samego posta, z pełnym
    wynikiem w ręku, odbijał się od bazy bez śladu w logu (dowód:
    `test_integracja_wiersz_z_werdyktem_bez_ekstrakcji_daje_sie_naprawic`
    w tests/test_zapis_klasyfikacji.py). Dlatego aktualizujemy również wiersz
    z werdyktem, w którym ekstrakcja jest PUSTA. Wiersz z realną ekstrakcją
    zostaje nietknięty, a statusu nie cofamy, gdy operator już go przestawił.

    Do bazy idzie `werdykt`, a NIE `przepusc` — to jest sedno trybu cienia:
    w cieniu `przepusc` jest zawsze prawdziwe, więc zapisanie go dawałoby same
    jedynki i po tygodniu nie dałoby się policzyć niczego.

    Commit per post, jak w repo źródłowym: przebieg trwa minutami i przeplata
    zapisy z długimi wywołaniami sieciowymi, więc jedna wielka transakcja
    znaczyłaby, że błąd na ostatniej grupie kasuje pracę wszystkich wcześniejszych.
    """
    ekstrakcja = _wiersz_ekstrakcji(decyzja, identyfikator, post["tresc"])
    kolumny_ai = _kolumny_ekstrakcji()
    kolumny_geo = _kolumny_geo()

    stale_kolumny = ("fb_id", "tresc", "post_url", "grupa_url", "grupa_nazwa",
                     "autor", "opublikowany_at",
                     "zrodlo_decyzji", "czy_zlecenie", "status", "stale",
                     "gate_werdykt", "gate_punkty", "gate_powod",
                     "gate_trafienia", "gate_tryb", "gate_jezyk",
                     "kategoria_ladunku", "kierunek")
    wartosci = [
        identyfikator, post["tresc"], post.get("post_url") or None,
        post.get("group_url") or None, post.get("group_name") or None,
        post.get("author_name") or None, post.get("post_date"),
        decyzja.zrodlo, decyzja.czy_zlecenie, decyzja.status, decyzja.stale,
        decyzja.gate_werdykt, decyzja.gate_punkty, decyzja.gate_powod,
        list(decyzja.gate_trafienia), decyzja.gate_tryb, decyzja.jezyk or None,
        # Kategoria ładunku jedzie tym samym INSERT-em co werdykt bramki — to ta
        # sama decyzja, podjęta w tym samym momencie i przez ten sam moduł.
        # Dopisywanie jej drugim zapytaniem byłoby drugą okazją do porażki bez
        # żadnego objawu (patrz nota o jednym INSERT-cie wyżej).
        decyzja.kategoria_ladunku or None,
        # Kierunek stoi tu, a nie wśród kolumn ekstrakcji, bo ma wartość TAKŻE
        # dla postów, których model nigdy nie zobaczył — a to właśnie one są
        # ofertami odrzuconymi przez bramkę. W liście ekstrakcji byłby NULL-em
        # dokładnie tam, gdzie jest najbardziej potrzebny.
        decyzja.kierunek or None,
    ]
    # Kolumny ekstrakcji lecą ZAWSZE, także gdy modelu nie pytano — wtedy jako
    # NULL-e, czyli dokładnie to, co i tak byłoby w wierszu. Jedna ścieżka SQL
    # zamiast dwóch znaczy, że nie da się poprawić jednej i zapomnieć o drugiej.
    wartosci += [ekstrakcja.get(k) for k in kolumny_ai]
    # Kraj i kierunek geograficzny tym samym trybem co ekstrakcja (NULL-e, gdy
    # modelu nie pytano) — ale osobną listą (`kolumny_geo`), bo `pusta_w_bazie`
    # niżej pyta wyłącznie o `kolumny_ai` i musi zostać na nią ślepa, patrz
    # `_kolumny_geo`.
    wartosci += [ekstrakcja.get(k) for k in kolumny_geo]
    wartosci += [ekstrakcja.get("ai_model")]

    kolumny = ", ".join((*stale_kolumny, *kolumny_ai, *kolumny_geo,
                         "ai_model", "gate_at", "ai_at"))
    znaki = ", ".join(["%s"] * len(wartosci))
    # `gate_at` i `ai_at` z zegara BAZY, jak dotąd. `ai_at` tylko dla wiersza
    # z werdyktem modelu: pusty znacznik czasu jest tu informacją („nie pytano"),
    # a nie brakiem.
    # `kierunek` jest tu — w odróżnieniu od `kategoria_ladunku` — bo jako jedyna
    # z kolumn bramki bywa NADPISYWANY przez model. Post zapisany w przebiegu,
    # w którym API padło, ma kierunek z samego wzorca; gdy model odpowie przy
    # kolejnym podejściu, jego odczyt ma dojechać do wiersza, a nie odbić się
    # od ON CONFLICT. `kolumny_geo` z tego samego powodu co `kolumny_ai`: drugie
    # podejście z gotowym wynikiem modelu ma nadpisać kraj i kierunek geo tak
    # samo, jak nadpisuje miasta, z których je policzono.
    aktualizowane = (*kolumny_ai, *kolumny_geo, "ai_model", "zrodlo_decyzji",
                     "czy_zlecenie", "kierunek")
    # Ten sam warunek co `_pusto`, tylko po stronie bazy. Jedno źródło nazw
    # (`kolumny_ai`), dwa renderowania — inaczej SQL i Python zaczęłyby się
    # różnić w tym, co uznają za „pusty wynik", a to jest dokładnie ta klasa
    # rozjazdu, przez którą powstało to zgłoszenie. Bez kolumn ekstrakcji nie
    # ma czego naprawiać, więc warunek schodzi do `false`.
    pusta_w_bazie = " AND ".join(f"posty.{k} IS NULL" for k in kolumny_ai) or "false"
    sql = f"""
        INSERT INTO posty ({kolumny})
        VALUES ({znaki}, NOW(), CASE WHEN %s::text = 'ai' THEN NOW() END)
        ON CONFLICT (fb_id) DO UPDATE SET
            {', '.join(f'{k} = EXCLUDED.{k}' for k in aktualizowane)},
            ai_at  = EXCLUDED.ai_at,
            -- Statusu NIE cofamy: operator mógł już wziąć ten post na telefon,
            -- a klasyfikacja dopisywana po fakcie nie ma prawa go wrócić do kolejki.
            status = CASE WHEN posty.status IN ('nowe', 'smiec')
                          THEN EXCLUDED.status ELSE posty.status END
        WHERE EXCLUDED.zrodlo_decyzji = 'ai'
          AND (posty.zrodlo_decyzji IS DISTINCT FROM 'ai' OR ({pusta_w_bazie}))
        RETURNING {', '.join(('fb_id', *kolumny_ai))}
    """  # noqa: S608 — nazwy kolumn pochodzą ze stałej w classifier.py, nie z wejścia
    with conn.cursor() as cur:
        cur.execute(sql, (*wartosci, decyzja.zrodlo))
        zwrocony = cur.fetchone()
        # BRAK ZWROTU NIE ZNACZY BŁĘDU: `ON CONFLICT DO UPDATE ... WHERE` nie
        # zwraca wiersza, gdy warunek go nie przepuścił (powtórka posta z gotową
        # klasyfikacją — stan normalny). Ale nie znaczy też, że jest dobrze:
        # dokładnie tą drogą uciekał zablokowany zapis. Dlatego wiersz czytamy.
        if zwrocony is None:
            cur.execute(
                f"SELECT {', '.join(('fb_id', *kolumny_ai))} FROM posty "  # noqa: S608
                f"WHERE fb_id = %s", (identyfikator,))
            zwrocony = cur.fetchone()
    conn.commit()

    w_bazie = dict(zip(kolumny_ai, (zwrocony or ())[1:]))
    if decyzja.zrodlo != "ai" or not _pusto(w_bazie, kolumny_ai):
        return Zapis(bez_ekstrakcji=False, wiersz=w_bazie)

    # OSTRZEŻENIE, nie wyjątek: wiersz jest już w bazie i lepiej mieć go bez
    # ekstrakcji niż nie mieć wcale. Ale MUSI być głośno — za ten wynik
    # zapłaciliśmy tokenami, a cicha strata przeżywa całe przebiegi (przeżyła
    # dwa: 27 postów z `zrodlo_decyzji='ai'` i kompletem NULL-i, bez jednej
    # linijki w logu, bo poprzednia wersja pytała słownik z pamięci zamiast bazy).
    if not kolumny_ai:
        powod = ("INSERT nie wymienił ANI JEDNEJ kolumny ekstrakcji — warstwa "
                 "zapisu nie widzi `workers/classifier.py` (`_kolumny_ekstrakcji` "
                 "zwróciło pustkę), więc do bazy poszedł sam werdykt")
    elif zwrocony is None:
        powod = ("baza nie zwróciła wiersza ani po INSERT-cie, ani po SELECT-cie "
                 "— zapis nie doszedł do skutku")
    elif not ekstrakcja:
        powod = ("`Decyzja` przyszła z `zrodlo='ai'`, ale bez `wynik_ai` — wynik "
                 "modelu zgubił się przed zapisem")
    else:
        powod = ("wynik modelu był w pamięci, a w wierszu po zapisie stoi komplet "
                 "NULL-i — ON CONFLICT odmówił aktualizacji albo nazwy kolumn "
                 "rozjechały się z 0004_klasyfikacja.sql")
    log(f"[{KTO}] OSTRZEŻENIE: post {identyfikator} ma w bazie werdykt modelu "
        f"i ZERO pól z ekstrakcji (typ, miejsca, pojazd, stan, pilność, kontakt, "
        f"pewność — wszystko NULL). Zapłacone tokeny, zerowy zapis. Powód: "
        f"{powod}.")
    return Zapis(bez_ekstrakcji=True, wiersz=w_bazie)


# ---------------------------------------------------------------------------
# Budżet, tempo, odstęp — cała arytmetyka pieniędzy w jednym miejscu
# ---------------------------------------------------------------------------
def tempo_na_godzine(stat: dict, teraz: datetime, okno_tempa_h: int) -> float:
    """Postów na godzinę z realnej historii grupy. 0.0 = brak historii.

    MIANOWNIK TO REALNE OKNO HISTORII, nie sztywne `okno_tempa_h` — i to jest ta
    poprawka, bez której cały mechanizm się zwija. Grupa scrapowana od dwóch dni
    ma posty tylko z dwóch dni; dzielenie ich przez pełne siedem zaniża tempo
    ponad trzykrotnie, limit spada do podłogi, a paczka postów zatwierdzona przez
    moderatora naraz przepada. Czego nie pobraliśmy, tego nie ma w bazie — więc
    następny przebieg liczy tempo z jeszcze uboższych danych i spirala się
    zamyka. W repo źródłowym ten sam błąd kosztował tydzień zgubionych leadów.
    """
    ile = stat.get("ostatnie") or 0
    if ile <= 0:
        return 0.0
    pierwszy = stat.get("pierwszy_pobrany_at")
    if pierwszy is not None:
        historia_h = max((teraz - pierwszy).total_seconds() / 3600.0, 1.0)
    else:
        historia_h = float(okno_tempa_h)
    return ile / min(float(okno_tempa_h), historia_h)


def rozdziel_budzet(urle: list[str], budzet: int, statystyki: dict[str, dict],
                    pula_startowa: int = cfg_groups.PULA_STARTOWA_POSTOW
                    ) -> dict[str, int]:
    """Ile postów na dobę dostaje każda grupa. Rozdziela BANDYTA, nie heurystyka.

    Wejście bandyty: `proby` = pobrane posty, `sukcesy` = z tego zlecenia. To
    jest dosłownie wydajność grupy, a Thompson Sampling jest algorytmem
    napisanym pod ten problem — dzielenie ograniczonej puli między źródła
    o nieznanej i zmiennej wydajności, bez rezygnacji z eksploracji. Własna
    heurystyka („daj więcej temu, kto dowiózł") skazywałaby grupę na zawsze po
    kilku pechowych przebiegach, a nowej nie dałaby się w ogóle zmierzyć.

    PULA STARTOWA dla grupy bez historii jest PODŁOGĄ, nie osobnym mechanizmem:
    bandyta i tak eksploruje (prior Beta(1,1)), ale przy dwudziestu grupach
    losowo przyznane dwadzieścia postów nie wystarczy, żeby cokolwiek zmierzyć.
    Podłoga gwarantuje, że nowa grupa dostanie szansę POWIEDZIEĆ COŚ O SOBIE,
    zanim bandyta zacznie ją porównywać z resztą.
    """
    if not urle or budzet <= 0:
        return {}

    lokalne = {
        url: {"proby": (statystyki.get(url) or {}).get("pobrane", 0),
              "sukcesy": (statystyki.get(url) or {}).get("zlecenia", 0)}
        for url in urle
    }
    przydzial = bandit.rozdziel_budzet(urle, budzet, statystyki_lokalne=lokalne)

    # Podłoga dla grup bez historii — dociągamy je do puli startowej, a różnicę
    # zabieramy grupom, które dostały najwięcej. Świadomie NIE dokładamy ponad
    # budżet: sufit dobowy jest twardy, bo pula kont Apify jest wspólna z drugim
    # systemem i cichy nadmiar to jego awaria, nie nasza.
    bez_historii = [u for u in urle if not (statystyki.get(u) or {}).get("pobrane")]
    for url in bez_historii:
        brakuje = min(pula_startowa, budzet) - przydzial.get(url, 0)
        while brakuje > 0:
            dawca = max((u for u in urle if u != url and przydzial.get(u, 0) > 0),
                        key=lambda u: przydzial.get(u, 0), default=None)
            if dawca is None:
                break
            ile = min(brakuje, przydzial[dawca])
            przydzial[dawca] -= ile
            przydzial[url] = przydzial.get(url, 0) + ile
            brakuje -= ile
    return {u: przydzial.get(u, 0) for u in urle}


def interwal_min(tempo_h: float, przydzial: int, sciezka: str,
                 min_interwal: int, wykorzystanie_budzetu: float | None = None) -> int:
    """Co ile minut pytać o tę grupę. Sens tej liczby jest INNY w każdej ścieżce.

    ŚCIEŻKA A — koszt dobowy grupy nie zależy od odstępu. Actor oddaje przyrost
    od zadanego okna, więc dwa razy rzadsze pytanie daje dwa razy większe okno
    i tyle samo postów. Odstęp jest tu WYŁĄCZNIE decyzją o czasie reakcji:
    pytamy mniej więcej wtedy, gdy statystycznie uzbierał się jeden nowy post.
    Budżet pilnuje w tej ścieżce nie odstęp, tylko sufit dobowy i przydział.

    ŚCIEŻKA B — każdy przebieg to pełny `resultsLimit` opłaconych postów, więc
    częstotliwość boli wprost proporcjonalnie i odstęp liczymy WPROST z budżetu:
    tyle przebiegów na dobę, ile stać przydział.

    Wynik przycięty do [min_interwal, MAX_INTERWAL_MIN]. Górna granica to wprost
    opóźnienie, z jakim w najgorszym razie zobaczymy zlecenie z cichej grupy.

    PIĄTA POPRAWKA — `wykorzystanie_budzetu` (zuzyte_doba/budzet CAŁEGO systemu,
    liczone RAZ w `zbuduj_plan`, nie na grupę) zmienia rolę `min_interwal` z
    dolnej granicy formuły w jej WARTOŚĆ DOMYŚLNĄ: dopóki jest poniżej
    `cfg_groups.PROG_WYKORZYSTANIA_BUDZETU`, formuła (i ścieżka A/B) w ogóle się
    nie liczy — zwracamy wprost `cfg_groups.MIN_INTERWAL_MIN`. Produkcyjne dane
    (26-40 min od publikacji do alertu) pokazały, że formuła rozciągała odstęp
    do 15-120 minut, mimo że budżetu było pod dostatkiem — a na giełdzie kursów
    liczy się pierwszy telefon, nie zaoszczędzony run Apify.
    `None` (domyślnie — i w każdym dotychczasowym wywołaniu tej funkcji, w tym
    w testach) znaczy „zużycie nieznane, licz z formuły" — stare zachowanie
    tam, gdzie wołający nie zna jeszcze stanu budżetu całego systemu.
    """
    if (wykorzystanie_budzetu is not None
            and wykorzystanie_budzetu < cfg_groups.PROG_WYKORZYSTANIA_BUDZETU):
        return cfg_groups.MIN_INTERWAL_MIN
    if sciezka == "A":
        minuty = (60.0 / tempo_h) if tempo_h > 0 else float(min_interwal)
    else:
        koszt_runu = max(
            cfg_groups.MIN_POSTOW_NA_GRUPE,
            math.ceil(tempo_h * (min_interwal / 60.0) * cfg_groups.ZAPAS_NA_PACZKE),
        )
        runow_na_dobe = przydzial / koszt_runu if koszt_runu > 0 else 0.0
        minuty = (1440.0 / runow_na_dobe) if runow_na_dobe >= 1.0 \
            else float(cfg_groups.MAX_INTERWAL_MIN)
    return int(min(max(round(minuty), min_interwal), cfg_groups.MAX_INTERWAL_MIN))


def _adaptive_group_params(tempo_h: float, odstep_min: int, sciezka: str,
                           nadpisanie: int | None = None) -> tuple[int, int]:
    """(resultsLimit, okno w minutach) dla JEDNEJ grupy.

    `nadpisanie` (CLI `--limit`) wyłącza adaptację — płaska wartość dla
    wszystkich grup, do ręcznych testów, gdzie chcemy przewidywalności zamiast
    sprytu.

    ROLA LIMITU ZALEŻY OD ŚCIEŻKI i to nie jest niuans:
      A — limit jest tylko zabezpieczeniem od góry. Koszt tnie warunek wieku,
          actor przerwie paginację wcześniej i limitu nie zużyje, więc hojny
          sufit nic nie kosztuje, a chroni przed zgubieniem paczki postów.
      B — limit JEST kosztem, co do sztuki. Każde jego podniesienie to decyzja
          finansowa i wchodzi wprost do rozliczenia budżetu z bandyty.

    Grupa bez historii dostaje bootstrap — nie podłogę. Podłoga (dwa posty) przy
    nowej grupie znaczyłaby, że nigdy nie zobaczymy, ile ta grupa naprawdę
    postuje, więc tempo zostanie zerowe na zawsze.
    """
    okno_min = (max(odstep_min * cfg_groups.MNOZNIK_OKNA, cfg_groups.MIN_OKNO_MIN)
                if sciezka == "A" else 1440)
    _, sufit = _parametry_sciezki(sciezka)

    if nadpisanie is not None:
        return max(1, nadpisanie), okno_min
    if tempo_h <= 0:
        return min(cfg_groups.DOMYSLNIE_POSTOW_NA_GRUPE, sufit), okno_min

    # W ścieżce A limit ma pokryć okno (bo tyle actor odda). W ścieżce B okno
    # jest fikcyjne („1 day"), więc limit liczymy z ODSTĘPU — tyle, ile realnie
    # przybyło od poprzedniego przebiegu.
    godzin = (okno_min / 60.0) if sciezka == "A" else (odstep_min / 60.0)
    surowy = tempo_h * godzin * cfg_groups.ZAPAS_NA_PACZKE
    return int(min(max(math.ceil(surowy), cfg_groups.MIN_POSTOW_NA_GRUPE), sufit)), okno_min


# ---------------------------------------------------------------------------
# Plan przebiegu — policzony PRZED jakimkolwiek wywołaniem sieciowym, żeby dało
# się go pokazać (`--sucho`) i policzyć jego koszt.
# ---------------------------------------------------------------------------
@dataclass
class PlanGrupy:
    url: str
    nazwa: str
    limit: int
    okno_min: int
    odstep_min: int
    przydzial: int
    pobrane_doba: int
    wypada: bool
    powod: str = ""

    @property
    def koszt_usd(self) -> float:
        return self.limit * cfg_groups.CENA_USD_ZA_POST


@dataclass
class Plan:
    sciezka: str
    skad_sciezka: str
    budzet: int
    zuzyte_doba: int
    grupy: list[PlanGrupy] = field(default_factory=list)

    @property
    def do_pobrania(self) -> list[PlanGrupy]:
        return [g for g in self.grupy if g.wypada]

    @property
    def zostalo(self) -> int:
        return max(0, self.budzet - self.zuzyte_doba)

    @property
    def wykorzystanie_budzetu(self) -> float:
        """zuzyte_doba/budzet — patrz `interwal_min`, próg `PROG_WYKORZYSTANIA_BUDZETU`."""
        return (self.zuzyte_doba / self.budzet) if self.budzet > 0 else 1.0

    @property
    def koszt_usd(self) -> float:
        return sum(g.koszt_usd for g in self.do_pobrania)


def zbuduj_plan(grupy: list[dict], statystyki: dict[str, dict],
                harmonogram: dict[str, dict], *, budzet: int, sciezka: str,
                skad_sciezka: str, teraz: datetime,
                nadpisanie_limitu: int | None = None,
                ignoruj_harmonogram: bool = False) -> Plan:
    """Kto, ile i kiedy — cała decyzja przed pierwszym wywołaniem sieciowym.

    Rozdzielenie planu od wykonania nie jest estetyką: `--sucho` ma pokazać
    DOKŁADNIE to, co poleci, razem z przewidywanym kosztem. Plan liczony
    w środku pętli dałoby się co najwyżej opisać, a opis i kod rozjeżdżają się
    przy pierwszej poprawce.
    """
    min_interwal, _ = _parametry_sciezki(sciezka)
    # Licznik dobowy sumujemy po CAŁYM harmonogramie, nie po grupach z tego
    # przebiegu. Sufit jest sufitem systemu: posty pobrane przez grupę wyłączoną
    # dziś w południe albo przez ręczne `--grupa` też zostały opłacone i też
    # zabrały kredyt wspólnej puli.
    zuzyte = sum((v or {}).get("pobrane_doba", 0) for v in harmonogram.values())
    plan = Plan(sciezka=sciezka, skad_sciezka=skad_sciezka, budzet=budzet,
                zuzyte_doba=zuzyte)

    # PIĄTA POPRAWKA — zużycie liczone RAZ, dla całego systemu, PRZED pętlą po
    # grupach: to jest właśnie "wykorzystanie budżetu poniżej progu" z
    # `interwal_min`, nie coś liczone od nowa per grupa. `budzet <= 0` (sufit
    # zdjęty albo błędna konfiguracja) traktujemy jako "budżetu już nie ma"
    # (1.0, patrz `Plan.wykorzystanie_budzetu`) — formuła ma wtedy szansę
    # zaoszczędzić, zamiast pytać co 5 minut bez żadnego ograniczenia.
    wykorzystanie_budzetu = plan.wykorzystanie_budzetu

    urle = [g["url"] for g in grupy]
    przydzialy = rozdziel_budzet(urle, budzet, statystyki)

    for grupa in grupy:
        url = grupa["url"]
        stan = harmonogram.get(url) or {}
        stat = statystyki.get(url) or {}
        tempo = tempo_na_godzine(stat, teraz, cfg_groups.OKNO_TEMPA_H)
        przydzial = przydzialy.get(url, 0)
        odstep = interwal_min(tempo, przydzial, sciezka, min_interwal,
                              wykorzystanie_budzetu=wykorzystanie_budzetu)
        limit, okno = _adaptive_group_params(tempo, odstep, sciezka, nadpisanie_limitu)
        pobrane = stan.get("pobrane_doba", 0)

        wypada, powod = True, ""
        nastepny = stan.get("nastepny_run_at")
        if ignoruj_harmonogram:
            powod = "wymuszone z CLI"
        elif nastepny is not None and nastepny > teraz:
            wypada = False
            za_ile = (nastepny - teraz).total_seconds() / 60.0
            powod = f"nie ta minuta (za {za_ile:.0f} min)"
        elif przydzial > 0 and pobrane >= przydzial:
            wypada = False
            powod = f"wyczerpany przydział dobowy ({pobrane}/{przydzial} postów)"
        elif plan.zostalo <= 0:
            wypada = False
            powod = "wyczerpany sufit dobowy całego systemu"

        plan.grupy.append(PlanGrupy(
            url=url, nazwa=grupa.get("name") or url, limit=limit, okno_min=okno,
            odstep_min=odstep, przydzial=przydzial, pobrane_doba=pobrane,
            wypada=wypada, powod=powod))
    return plan


def opis_planu(plan: Plan) -> list[str]:
    """Plan jako tekst — to samo wypisuje `--sucho` i normalny przebieg."""
    # PIĄTA POPRAWKA — poniżej progu odstęp każdej grupy "ok" to WPROST
    # MIN_INTERWAL_MIN (patrz `interwal_min`), nie coś, co trzeba by wyliczyć
    # z widełek niżej w tabeli. Ta linia mówi wprost, który tryb obowiązuje w
    # TYM przebiegu — bez niej "co 5 min" w tabeli grup wygląda jak przypadek
    # dobrego tempa, nie jak reguła.
    tryb_odstepu = ("DOMYŚLNY (MIN_INTERWAL_MIN)" if
                    plan.wykorzystanie_budzetu < cfg_groups.PROG_WYKORZYSTANIA_BUDZETU
                    else "ZE WZORU (tempo/budżet — próg zużycia przekroczony)")
    linie = [
        f"[{KTO}] ścieżka actora: {plan.sciezka} ({plan.skad_sciezka})",
        f"[{KTO}] budżet dobowy: {plan.budzet} postów, zużyte {plan.zuzyte_doba} "
        f"({plan.wykorzystanie_budzetu:.0%}), zostało {plan.zostalo}",
        f"[{KTO}] odstęp grup: {tryb_odstepu} — próg {cfg_groups.PROG_WYKORZYSTANIA_BUDZETU:.0%}",
        f"[{KTO}] grup w tym przebiegu: {len(plan.do_pobrania)} z {len(plan.grupy)}",
    ]
    for g in plan.grupy:
        znacznik = "->" if g.wypada else "  "
        linie.append(
            f"[{KTO}] {znacznik} {g.nazwa}: limit {g.limit}, okno "
            f"{_okno_dla_apify(g.okno_min, plan.sciezka)}, co {g.odstep_min} min, "
            f"przydział {g.przydzial} (zużyte {g.pobrane_doba})"
            + (f" — POMIJAM: {g.powod}" if not g.wypada else ""))
    linie.append(
        f"[{KTO}] przewidywany koszt tego przebiegu: do {plan.koszt_usd:.4f} USD "
        f"({sum(g.limit for g in plan.do_pobrania)} postów po "
        f"{cfg_groups.CENA_USD_ZA_POST} USD — cena katalogowa, NIE zmierzona)")
    return linie


# ---------------------------------------------------------------------------
# Klasyfikator — na razie tylko szew. Wypełnia go osobny krok.
# ---------------------------------------------------------------------------
def _klasyfikuj(tresc: str, grupa: str, jezyk: str) -> dict | None:
    """Werdykt modelu albo None, gdy klasyfikatora jeszcze nie ma.

    KONTRAKT dla modułu, który tu wejdzie (`workers/classifier.py`):
        klasyfikuj(tresc: str, grupa: str, jezyk: str) -> dict | None
        dict zawiera co najmniej {"czy_zlecenie": bool}.
    `jezyk` jest w podpisie, bo post bywa obcojęzyczny — bramka wpuszcza DE/CS/SK
    i to klasyfikator ma oddać wynik PO POLSKU (gotowy tekst do promptu
    systemowego: `workers.gate.INSTRUKCJA_JEZYKOWA_DLA_KLASYFIKATORA`).

    Brak modułu NIE jest błędem — jest stanem „jeszcze nie zbudowane". Post
    przepuszczony przez bramkę ląduje wtedy w bazie ze statusem `nowe`
    i `zrodlo_decyzji='gate'`, czyli czeka na klasyfikację, zamiast zniknąć.

    ROZRÓŻNIAMY „modułu nie ma" od „moduł jest i się wysypał". Pierwsze sprawdza
    `find_spec` i kończy się cichym None; drugie leci wyjątkiem wyżej, bo brak
    `anthropic` w środowisku albo literówka w imporcie to zepsuty deploy, a nie
    etap budowy — i schowanie tego pod tym samym `except ImportError` znaczyłoby,
    że system po cichu przestał klasyfikować i nikt się nie dowie.
    """
    import importlib.util  # noqa: PLC0415

    if importlib.util.find_spec("laweta_radar.workers.classifier") is None:
        return None
    from laweta_radar.workers import classifier  # noqa: PLC0415

    funkcja = getattr(classifier, "klasyfikuj", None)
    if funkcja is None:
        return None
    return funkcja(tresc, grupa, jezyk)


def _bez_klasyfikatora(*_a, **_k) -> None:
    """Zaślepka na czas przebiegu, w którym klasyfikator padł — patrz `run`."""
    return None


@dataclass(frozen=True)
class Decyzja:
    """Co zrobić z jednym postem — policzone BEZ dotykania bazy i sieci.

    Wydzielone z pętli przebiegu, żeby dało się to sprawdzić testem offline.
    Kolejność kroków w `decyzja_o_poscie` jest tu najważniejsza i jednocześnie
    najłatwiejsza do cichego zepsucia przy refaktorze: wystarczy przestawić
    dwie linijki i model zaczyna dostawać wszystko, co przyszło z Apify.
    """

    zrodlo: str            # 'gate' | 'ai' — kto podjął decyzję
    czy_zlecenie: bool
    jezyk: str             # dwuliterowy znacznik z bramki (dla powiadomienia)
    status: str            # 'nowe' (do obsługi) | 'smiec' (poza kolejką)
    stale: bool
    powod: str
    pytano_model: bool     # czy zapłaciliśmy za tokeny
    # PEŁNY wynik klasyfikatora, nie tylko werdykt. Jest tu, bo `Decyzja` to
    # JEDYNA rzecz, którą pętla przebiegu przekazuje do zapisu — pole wyciągnięte
    # z wyniku do osobnej zmiennej i nieprzepisane tutaj przestaje istnieć
    # w momencie następnej iteracji. Pierwsza wersja niosła `czy_zlecenie`
    # i `powod`, więc komplet ekstrakcji (typ, miejsca, pojazd, stan, pilność,
    # kontakt, pewność) ginął cicho, mimo że model go zwrócił i mimo że był
    # opłacony. None = modelu nie pytano albo nie odpowiedział.
    wynik_ai: dict | None = None
    # OPINIA bramki, niezależna od trybu pracy — to ją zapisujemy do bazy.
    # W trybie cienia `gate_werdykt` bywa False przy `pytano_model=True`:
    # bramka mówi „odrzuciłabym", ale niczego nie blokuje. Właśnie z tych par
    # (opinia bramki, werdykt modelu) raport liczy fałszywe odrzucenia.
    gate_werdykt: bool = True
    gate_punkty: int = 0
    gate_powod: str = ""
    gate_trafienia: tuple = ()
    gate_tryb: str = ""
    # 'pojazd' | 'zwierze' | 'inne' — CO miałoby jechać. Nie zmienia ani
    # `czy_zlecenie`, ani `status`: post o transporcie konia idzie przez pipeline
    # normalnie. Zmienia to, co widać w panelu i czy brzęczy telefon
    # (services/powiadomienia.ocen + ALERT_ZWIERZETA).
    kategoria_ladunku: str = gate.KAT_INNE
    # 'zlecenie' | 'oferta' | 'niejasne' — PO KTÓREJ STRONIE RYNKU stoi autor.
    # W odróżnieniu od kategorii ładunku to pole bierze udział w werdykcie:
    # „oferta" znaczy, że autor sprzedaje własny przejazd, więc `czy_zlecenie`
    # jest fałszem, a status to `smiec`. Wiersz mimo to powstaje z kompletem
    # danych — cudzy kurs na naszej trasie bywa okazją na doładunek, a tego nie
    # widać w danych, których się nie zapisało.
    #
    # WARTOŚĆ MOŻE POCHODZIĆ Z DWÓCH ŹRÓDEŁ: z bramki (wzorzec) albo z modelu
    # (przeczytane zdanie). Rozstrzyga to `decyzja_o_poscie`.
    kierunek: str = gate.KIERUNEK_NIEJASNY


def decyzja_o_poscie(post: dict, prog_swiezosci: datetime, *, grupa: str = "",
                     klasyfikuj=_klasyfikuj) -> Decyzja:
    """Bramka -> świeżość -> model. Ta kolejność jest całą oszczędnością systemu.

    1. BRAMKA PIERWSZA. Post, który nie przeszedł, nie kosztuje ANI JEDNEGO
       tokena. Przy kilkuset postach na dobę i grupach ogłoszeniowych to jest
       różnica między rachunkiem, na który stać ten system, a takim, na który
       nie stać.
    2. ŚWIEŻOŚĆ PRZED MODELEM. Post starszy niż `MAX_WIEK_POSTA_H` trafia do
       bazy (materiał do statystyki grupy), ale nie idzie do modelu i nie budzi
       nikogo. Płacenie za klasyfikację zlecenia, po które ktoś już przyjechał,
       to koszt bez żadnej możliwej korzyści.
    3. MODEL NA KOŃCU, i tylko dla postów, które mają szansę być zleceniem.

    Brak klasyfikatora nie jest błędem: post czeka wtedy w bazie ze statusem
    `nowe`. Świadomie nie zgadujemy za model — bramka mówi „warto zapytać",
    a nie „to jest zlecenie", i zapisanie jej werdyktu jako `czy_zlecenie=true`
    zapełniłoby kolejkę operatora reklamami, które model by odsiał.
    """
    data = post.get("post_date")
    stale = data is not None and data < prog_swiezosci

    b = gate.gate(post.get("tresc") or "")
    wspolne = {
        "jezyk": b.jezyk,
        "gate_werdykt": b.werdykt,
        "gate_punkty": b.punkty,
        "gate_powod": b.powod,
        "gate_trafienia": tuple(b.trafienia),
        "gate_tryb": b.tryb,
        "kategoria_ladunku": b.kategoria_ladunku,
        # Kierunek z bramki jest wartością WYJŚCIOWĄ — dla postów, których model
        # nigdy nie zobaczy (odrzucone, stare, przebieg bez klasyfikatora) jest
        # też ostateczną. Model może go poniżej nadpisać.
        "kierunek": b.kierunek,
    }

    # `przepusc`, nie `werdykt` — to jest DECYZJA OPERACYJNA i uwzględnia tryb
    # pracy. W cieniu bramka niczego nie blokuje, więc post idzie do modelu mimo
    # negatywnej opinii; jej opinia i tak ląduje w bazie i to z niej raport
    # policzy, ile zleceń bramka by skasowała, gdyby ją włączyć.
    if not b.przepusc:
        return Decyzja(zrodlo="gate", czy_zlecenie=False, status="smiec",
                       stale=stale, powod=b.powod, pytano_model=False, **wspolne)
    if stale:
        return Decyzja(zrodlo="gate", czy_zlecenie=False, status="smiec",
                       stale=True, pytano_model=False,
                       powod=f"za stary ({data.isoformat()}) — bez modelu i bez alertu",
                       **wspolne)

    wynik = klasyfikuj(post.get("tresc") or "", grupa, b.jezyk)
    if wynik is None:
        return Decyzja(zrodlo="gate", czy_zlecenie=False, status="nowe",
                       stale=False, pytano_model=False,
                       powod=f"przez bramkę ({b.powod}) — czeka na klasyfikator",
                       **wspolne)

    czy = bool(wynik.get("czy_zlecenie"))
    # KIERUNEK: model bije bramkę, ale TYLKO gdy cokolwiek rozstrzygnął. Model
    # czyta zdanie, bramka dopasowuje wzorzec — przy rozbieżności rację ma model
    # (ta sama zasada co przy kodach pocztowych w `classifier.uzupelnij_kody`,
    # tylko w drugą stronę). Ale „niejasne" znaczy „nie umiem powiedzieć",
    # a wtedy odczyt bramki jest lepszy niż nic: nadpisanie go pustą odpowiedzią
    # gubiłoby jedyną informację, jaką o tym poście mamy.
    kierunek_modelu = str(wynik.get("kierunek") or "")
    if kierunek_modelu in (gate.KIERUNEK_ZLECENIE, gate.KIERUNEK_OFERTA):
        wspolne["kierunek"] = kierunek_modelu
    return Decyzja(zrodlo="ai", czy_zlecenie=czy,
                   status="nowe" if czy else "smiec", stale=False,
                   powod=str(wynik.get("powod") or ""), pytano_model=True,
                   # CAŁY wynik idzie dalej, nie tylko te dwa pola, które czyta
                   # ta funkcja. Zapis potrzebuje reszty — patrz `_zapisz_post`.
                   wynik_ai=wynik,
                   **wspolne)


# ---------------------------------------------------------------------------
# Przebieg
# ---------------------------------------------------------------------------
def _skrot(tresc: str | None, n: int = 60) -> str:
    tresc = (tresc or "").strip().replace("\n", " ")
    if not tresc:
        return "(pusty post)"
    return tresc[:n] + "…" if len(tresc) > n else tresc


def _jedna_linia(e: BaseException, n: int = 200) -> str:
    """Wyjątek jako JEDNA linia. Komunikaty psycopg2 są wielolinijkowe, a log
    workera czyta się `grep`-em — rozbity na trzy linie błąd gubi się w szumie
    i nie da się go policzyć."""
    return f"{type(e).__name__}: {' '.join(str(e).split())[:n]}"


def _doganiaj_kierunek_geo(log=print) -> None:
    """Domyka zaległość `odbior_kraj`/`dostawa_kraj`/`kierunek_geo` W KAŻDYM
    przebiegu — ta sama pętla sprzątająca co naprawa ekstrakcji (`naprawione`
    w `run()`), tylko bez czekania, aż Apify pokaże post jeszcze raz.

    PO CO ODDZIELNIE OD `do_naprawy`. Naprawa ekstrakcji działa na postach,
    które fetcher I TAK zobaczył w tym przebiegu (Apify je zwrócił) — nie ma
    jak nią naprawić wiersza sprzed tygodni, bo grupy oddają tylko najnowsze
    posty. Kolumny geo są inne: liczą się WYŁĄCZNIE z miast/kodów już
    zapisanych w wierszu (`geo.geokoduj`, offline), więc backfill nie
    potrzebuje ani Apify, ani modelu — może przeliczać całą historię, jedno
    zapytanie SQL na przebieg, za darmo. Migracja 0013 zostawiła 134 takie
    wiersze; bez tego haka jedyną drogą do ich naprawy byłoby pamiętanie
    o ręcznym odpaleniu `scripts/uzupelnij_kierunek_geo.py`.

    WZORZEC DLA KOLEJNYCH MIGRACJI: każda następna migracja, która dokłada
    kolumnę liczoną z danych JUŻ w bazie (nie z nowego wywołania Apify ani
    modelu), ma dostać analogiczny hak tutaj — funkcja czytająca do
    `KIERUNEK_GEO_BACKFILL_LIMIT` wierszy i wołana z tego samego miejsca
    w `run()`. System ma sam nadganiać zaległości; jednorazowy skrypt
    (jak `uzupelnij_kierunek_geo.py`) zostaje wyłącznie jako narzędzie do
    NATYCHMIASTOWEGO przeliczenia całej historii zaraz po migracji.

    PONOWNE UŻYCIE, NIE KOPIA: `wiersze_do_przeliczenia`/`przelicz` importujemy
    z `scripts/uzupelnij_kierunek_geo.py` zamiast przepisywać tu SQL i wywołanie
    geokodera — druga kopia tej samej reguły rozjechałaby się przy pierwszej
    zmianie geokodera (patrz nota w tamtym module).

    BEST-EFFORT, jak reszta diagnostyki/samoleczenia w tym pliku: błąd tutaj
    nie ma prawa zablokować pobierania, za które już zapłaciliśmy Apify.
    """
    limit = settings.KIERUNEK_GEO_BACKFILL_LIMIT
    if limit <= 0:
        return
    from laweta_radar.scripts import uzupelnij_kierunek_geo as geo_backfill  # noqa: PLC0415

    conn = _polacz_best_effort()
    if conn is None:
        return
    try:
        wiersze = geo_backfill.wiersze_do_przeliczenia(conn, limit)
        if not wiersze:
            return
        policzone: dict[str, int] = {}
        bledy = 0
        for wiersz in wiersze:
            try:
                kierunek = geo_backfill.przelicz(conn, wiersz)
                policzone[kierunek] = policzone.get(kierunek, 0) + 1
            except Exception as e:  # noqa: BLE001 — jeden wiersz nie psuje reszty
                bledy += 1
                conn.rollback()
                log(f"[{KTO}] kierunek_geo: {wiersz[0]}: {_jedna_linia(e)}")
        rozklad = ", ".join(f"{k}={v}" for k, v in sorted(policzone.items()))
        bledy_txt = f", {bledy} błędów" if bledy else ""
        log(f"[{KTO}] doliczono kierunek_geo w {sum(policzone.values())} "
            f"wierszach sprzed migracji 0013 ({rozklad}){bledy_txt}.")
    except Exception as e:  # noqa: BLE001 — backfill to usprawnienie, nie warunek runu
        log(f"[{KTO}] nie policzyłem zaległości kierunek_geo: {_jedna_linia(e)}")
    finally:
        conn.close()


def run(*, budzet: int | None = None, tylko_grupa: str | None = None,
        sucho: bool = False, nadpisanie_limitu: int | None = None,
        log=print) -> int:
    """Jeden przebieg. Zwraca kod wyjścia (0 także wtedy, gdy nic nie zrobił).

    Kod 0 przy braku konfiguracji jest świadomy: dla crona niezerowy kod to
    awaria, o której trzeba kogoś obudzić, a nieskonfigurowany system nie jest
    awarią — jest systemem, którego jeszcze nie włączono.
    """
    sciezka, skad = wykryj_sciezke()

    # BEZPIECZNIK 1 — klucze. Bez nich system jest gotowy, ale nieaktywny.
    tokeny = load_apify_tokens()
    if not tokeny and not sucho:
        log(f"[{KTO}] Brak kluczy Apify (wspólna pula z sales-core-engine) — "
            f"kończę bez działania. Sprawdź: python -m laweta_radar.workers.apify_keys")
        return 0

    # BEZPIECZNIK 2 — grupy. Świeży klon ma wszystkie `unverified` i bez adresu.
    wszystkie = cfg_groups.grupy_do_pobrania()
    if tylko_grupa:
        wszystkie = [{"url": tylko_grupa, "name": tylko_grupa}]
    if not wszystkie:
        log(f"[{KTO}] {cfg_groups.opis_listy()} — nie ma czego pobierać. "
            f"Zweryfikuj grupy w config/groups.py i przestaw status na 'ok'.")
        return 0

    # BEZPIECZNIK 3 — proxy. Wyjście z gołego IP VPS-a to właśnie to, co ściąga
    # bany na CAŁĄ pulę kont naraz, a nie na jedno.
    if not sucho:
        proxy_ok, linie = apify_proxy.preflight(tokens=tokeny)
        for linia in linie:
            log(linia)
        if not proxy_ok:
            log(f"[{KTO}] Proxy Apify niegotowe — wstrzymuję przebieg.")
            return 0

    limit_dobowy = budzet if budzet is not None else settings.POSTY_NA_DOBE
    teraz = datetime.now(timezone.utc)
    doba = teraz.date().isoformat()

    # BEZPIECZNIK 4 — baza i migracje. W trybie `--sucho` brak bazy NIE kończy
    # przebiegu: plan bez historii grup jest wtedy mniej dokładny, ale to jedyny
    # moment, w którym ktoś chce go zobaczyć — zanim cokolwiek zadziała.
    statystyki: dict[str, dict] = {}
    harmonogram: dict[str, dict] = {}
    istniejace: set[str] = set()
    # Posty z werdyktem modelu i pustą ekstrakcją — dedup ich NIE pomija, bo
    # inaczej nigdy nie doczekałyby się naprawy (patrz `_istniejace_id`).
    do_naprawy: set[str] = set()
    baza_gotowa = False

    if not settings.DATABASE_URL:
        if not sucho:
            log(f"[{KTO}] Brak DATABASE_URL — kończę bez działania.")
            return 0
        log(f"[{KTO}] --sucho bez DATABASE_URL: plan bez historii grup (zgrubny).")
    else:
        try:
            import psycopg2  # noqa: PLC0415 — patrz zasada o czystym wyjściu
        except ImportError:
            log(f"[{KTO}] Brak psycopg2 — pip install -r requirements.txt")
            return 0
        try:
            conn = psycopg2.connect(settings.DATABASE_URL)
        except Exception as e:  # noqa: BLE001 — patrz komentarz w środku
            # Padnięta baza to AWARIA, nie brak konfiguracji — i jedyny alarm,
            # jaki ten system ma, to poczta od crona. Dlatego kod 1, ale JEDNA
            # czytelna linia zamiast tracebacku co pięć minut.
            log(f"[{KTO}] BAZA NIEDOSTĘPNA: {_jedna_linia(e)}")
            if not sucho:
                return 1
            # `--sucho` ma pokazać plan także wtedy, gdy nic jeszcze nie stoi —
            # to jest dokładnie ten moment, w którym ktoś chce go zobaczyć.
            log(f"[{KTO}] --sucho: liczę plan bez historii grup (będzie zgrubny).")
        else:
            try:
                braki = [t for t in ("posty", "harmonogram")
                         if not _tabela_istnieje(conn, t)]
                if not braki:
                    braki = _brakujace_migracje(conn)
                if braki:
                    log(f"[{KTO}] Brak tabel: {', '.join(braki)}. Odpal migracje "
                        f"jako postgres:\n    bash laweta_radar/scripts/migrate.sh")
                    if not sucho:
                        return 0
                else:
                    baza_gotowa = True
                    istniejace, do_naprawy = _istniejace_id(conn)
                    statystyki = _statystyki_grup(
                        conn, cfg_groups.OKNO_WYDAJNOSCI_DNI, cfg_groups.OKNO_TEMPA_H)
                    harmonogram = _wczytaj_harmonogram(conn, doba)
            finally:
                # Połączenie zamykamy PRZED pętlą sieciową. W pętli lecą
                # wywołania trwające minuty; trzymane przez nie połączenie stoi
                # bezczynnie i serwer je ubija — pierwszy zapis leci wtedy „SSL
                # connection has been closed unexpectedly", a każdy kolejny
                # „connection already closed". Zapis otwiera własne, świeże.
                conn.close()

    plan = zbuduj_plan(wszystkie, statystyki, harmonogram, budzet=limit_dobowy,
                       sciezka=sciezka, skad_sciezka=skad, teraz=teraz,
                       nadpisanie_limitu=nadpisanie_limitu,
                       ignoruj_harmonogram=bool(tylko_grupa))
    for linia in opis_planu(plan):
        log(linia)

    if sucho:
        log(f"[{KTO}] --sucho: nie wołam actora. Nic nie wydano.")
        return 0

    # Backfill kierunek_geo/odbior_kraj/dostawa_kraj — NIEZALEŻNIE od budżetu
    # i planu pobierania niżej: to zapytanie do bazy, zero Apify, więc ma się
    # wykonać w KAŻDYM realnym przebiegu, także takim, w którym sufit dobowy
    # jest wyczerpany i `plan.do_pobrania` zaraz każe wyjść. Patrz
    # `_doganiaj_kierunek_geo` i `KIERUNEK_GEO_BACKFILL_LIMIT`.
    if baza_gotowa:
        _doganiaj_kierunek_geo(log=log)

    if not plan.do_pobrania:
        # Nie jest to błąd — to normalny wynik przy gęstym cronie i rzadkich
        # grupach. Ale gdy powodem jest wyczerpany budżet, musi być to widać
        # WYRAŹNIE: cicho przekroczony (albo cicho zablokowany) budżet to
        # najdroższa cisza w tym systemie.
        if plan.zostalo <= 0:
            log(f"[{KTO}] SUFIT DOBOWY WYCZERPANY ({plan.zuzyte_doba}/"
                f"{plan.budzet} postów). NIE wykonuję wywołań do końca doby UTC.")
        return 0
    if not baza_gotowa:
        log(f"[{KTO}] Baza niegotowa — nie ruszam pobierania (byłby to czysty koszt).")
        return 0

    # Filtr martwych/tegomiesięcznie-wyczerpanych kluczy — z BAZY, nie z pliku
    # (patrz apify_keys.klucze_zywe): plik stanu nie przeżywa równoległych
    # przebiegów, a martwy klucz oznaczony w JEDNYM przebiegu ma zostać martwy
    # we WSZYSTKICH następnych, nie tylko w tym procesie. Best-effort: bez bazy
    # (albo przy jej chwilowej awarii) rotujemy po WSZYSTKICH kluczach — filtr
    # jest usprawnieniem, nie warunkiem runu.
    tokeny_zywe = tokeny
    _conn_stan = _polacz_best_effort()
    if _conn_stan is not None:
        try:
            tokeny_zywe = apify_keys.klucze_zywe(_conn_stan, tokeny)
            if len(tokeny_zywe) < len(tokeny):
                log(f"[{KTO}] {len(tokeny) - len(tokeny_zywe)} kluczy pominiętych "
                    f"(martwe albo wyczerpane w tym miesiącu wg zasoby_apify) — "
                    f"{len(tokeny_zywe)} z {len(tokeny)} idzie do rotacji.")
        except Exception as e:  # noqa: BLE001 — filtr to usprawnienie, nie warunek runu
            log(f"[{KTO}] nie udało się odczytać stanu kluczy z bazy "
                f"({_jedna_linia(e)}) — rotuję po WSZYSTKICH kluczach.")
        finally:
            _conn_stan.close()
    _alert_jesli_zdegradowana(tokeny_zywe, tokeny, log=log)

    # Konfiguracja proxy z WYRÓWNANIEM po hashu (sekcja 4 zadania „większa pula
    # proxy") — liczona RAZ dla wszystkich żywych kluczy tego przebiegu, a nie
    # per wywołanie `_apify_run_group`, żeby przypisanie było stabilne w całym
    # runie. `None` przy błędzie konfiguracji: workery i tak dociągną świeżą
    # konfigurację same (patrz `client_for_token`), tylko bez wyrównania —
    # diagnostyka nie ma prawa zablokować pobierania.
    cfg_proxy = None
    try:
        cfg_proxy = apify_proxy.load_proxy_config(tokens=tokeny_zywe)
    except apify_proxy.ApifyProxyError as e:
        log(f"[{KTO}] nie policzyłem wyrównanej konfiguracji proxy ({_jedna_linia(e)}) "
            f"— jadę bez wyrównania.")

    # BEZPIECZNIK — wyczerpanie ŻYWYCH proxy (sekcja 3 zadania „większa pula
    # proxy"). Padnięty klucz dostaje kolejne proxy z rankingu (samoleczenie
    # niżej), ale gdy WSZYSTKIE adresy puli są NARAZ w kwarantannie, nie ma czym
    # pojechać dalej bez wyjścia z gołego IP VPS-a — lepiej pominąć CAŁY
    # przebieg niż to. Dotyczy WYŁĄCZNIE APIFY_PROXY_REQUIRED=1: bez tej flagi
    # brak proxy i tak jest dozwolonym stanem (zgodność wstecz).
    if cfg_proxy is not None and cfg_proxy.required and cfg_proxy.pool:
        _conn_px = _polacz_best_effort()
        if _conn_px is not None:
            zywe_proxy = None
            try:
                zywe_proxy = apify_proxy.zywe_proxy_w_puli(_conn_px, cfg_proxy)
            except Exception as e:  # noqa: BLE001 — bezpiecznik nie może wywalić się na diagnostyce
                log(f"[{KTO}] nie sprawdziłem stanu puli proxy ({_jedna_linia(e)}) — jadę dalej.")
            finally:
                _conn_px.close()
            if zywe_proxy == 0:
                _alert_pula_proxy_wyczerpana(cfg_proxy, log=log)
                return 1

    def _zapisz_stan_klucza(token: str, stan: str, powod: str) -> None:
        # Sukces NIE wymaga zapisu: klucz bez wpisu w `zasoby_apify` jest już
        # czytany jako 'aktywny' (patrz apify_keys.wczytaj_stany), więc pisanie
        # na KAŻDYM udanym wywołaniu byłoby zapytaniem do bazy bez żadnej
        # nowej informacji — tylko koszt na hot path.
        if stan == apify_keys.STATUS_AKTYWNY:
            return
        conn = _polacz_best_effort()
        if conn is None:
            return
        try:
            apify_keys.zapisz_stan(conn, token, stan, powod)
        except Exception:  # noqa: BLE001 — perzystencja stanu to usprawnienie, nie warunek runu
            pass
        finally:
            conn.close()

    rotator = KeyRotator.for_tokens(
        tokeny_zywe, transient_key_switches=2 if apify_proxy.is_enabled() else 0,
        on_wynik=_zapisz_stan_klucza)
    prog_swiezosci = teraz - timedelta(hours=settings.MAX_WIEK_POSTA_H)
    nowe = zlecenia = duplikaty = odsiane = stare = bez_linku = 0
    # Ile postów bramka ODRZUCIŁABY, gdyby była aktywna. W trybie cienia to
    # jedyna liczba, z której widać, ile by kosztowało jej włączenie — sama
    # `odsiane` jest tam zawsze zerem, bo cień niczego nie blokuje.
    bramka_by_odrzucila = 0
    niezapisane = 0
    bez_ekstrakcji = 0
    # Ile alertów realnie poszło i ile wierszy uzupełniliśmy o zgubioną wcześniej
    # ekstrakcję. Obie liczby są tu dlatego, że ich brak wygląda identycznie jak
    # cicha noc na grupach — a to najdroższa pomyłka w diagnozie tego systemu.
    wyslane = 0
    naprawione = 0
    pobrane_lacznie = plan.zuzyte_doba
    klasyfikator_padl = ""

    import psycopg2  # noqa: PLC0415 — po preflighcie wiemy, że jest

    for g in plan.do_pobrania:
        # Sufit sprawdzany PRZED każdym wywołaniem, na najgorszym przypadku
        # (pełny limit). Sprawdzanie po fakcie znaczyłoby, że budżet da się
        # przekroczyć o jedno wywołanie — a przy hojnym limicie ścieżki A to
        # jest pięćdziesiąt postów.
        if pobrane_lacznie + g.limit > plan.budzet:
            log(f"[{KTO}] SUFIT DOBOWY: {pobrane_lacznie}/{plan.budzet} postów, "
                f"kolejne wywołanie ({g.nazwa}, limit {g.limit}) już się nie mieści "
                f"— PRZERYWAM pobieranie do końca doby UTC.")
            break

        blad = None
        itemy: list[dict] = []
        try:
            itemy = rotator.call(
                lambda tok, u=g.url, lim=g.limit, okno=g.okno_min:
                    _apify_run_group_samoleczaca(u, lim, okno, sciezka, tok,
                                                 cfg=cfg_proxy, log=log))
        except AllKeysExhausted:
            # AWARIA CAŁEGO ŹRÓDŁA DANYCH, nie zwykłe ostrzeżenie — patrz
            # docstring `_alert_pula_wyczerpana`. Kolejne grupy nie mają szans,
            # więc alert i re-raise, zanim cokolwiek inne spróbuje jechać dalej.
            _alert_pula_wyczerpana(tokeny, log=log)
            raise
        except Exception as e:  # noqa: BLE001 — zła grupa nie wywala reszty
            blad = _jedna_linia(e)
            log(f"[{KTO}] {g.nazwa}: błąd pobierania {blad}")

        pobrane_lacznie += len(itemy)
        log(f"[{KTO}] {g.nazwa}: pobrano {len(itemy)} postów "
            f"(limit {g.limit}, okno {_okno_dla_apify(g.okno_min, sciezka)})")

        for item in itemy:
            post = _extract_post(item, {"url": g.url, "name": g.nazwa})
            if post is None:
                continue    # brak treści — nie ma czego oceniać
            identyfikator = fb_id(post["tresc"])
            if identyfikator in istniejace and identyfikator not in do_naprawy:
                duplikaty += 1
                continue

            if not post.get("post_url"):
                # NAJWAŻNIEJSZE POLE W SYSTEMIE. Bez linku operator nie ma jak
                # odpisać, więc zlecenie jest bezwartościowe. Brak URL-a znaczy,
                # że actor zmienił kształt odpowiedzi — i trzeba dopisać kolejny
                # klucz do `_first_str`, a nie czekać, aż ktoś zauważy w panelu.
                bez_linku += 1
                log(f"[{KTO}] OSTRZEŻENIE: post z grupy {g.nazwa!r} bez post_url "
                    f"— actor zmienił kształt odpowiedzi, dopisz klucz "
                    f"do _first_str. Treść: {_skrot(post['tresc'])}")

            # Klasyfikator, który się wysypał, NIE przerywa przebiegu — inaczej
            # jeden zepsuty deploy kasowałby też zbieranie, a posty przepuszczone
            # przez bramkę i tak lądują w bazie ze statusem `nowe` i doczekają
            # klasyfikacji. Ale po pierwszym błędzie przestajemy go pytać do
            # końca przebiegu: sto identycznych wyjątków pod rząd to sto razy
            # ten sam koszt i sto razy ta sama linijka w logu.
            try:
                decyzja = decyzja_o_poscie(
                    post, prog_swiezosci, grupa=g.nazwa,
                    klasyfikuj=_bez_klasyfikatora if klasyfikator_padl else _klasyfikuj)
            except Exception as e:  # noqa: BLE001 — patrz komentarz wyżej
                klasyfikator_padl = _jedna_linia(e)
                log(f"[{KTO}] KLASYFIKATOR PADŁ ({klasyfikator_padl}) — do końca "
                    f"tego przebiegu zapisuję sam werdykt bramki. Posty czekają "
                    f"w bazie ze statusem 'nowe'.")
                decyzja = decyzja_o_poscie(post, prog_swiezosci, grupa=g.nazwa,
                                           klasyfikuj=_bez_klasyfikatora)

            try:
                zapis = _zapis(identyfikator, post, decyzja, log=log)
                if zapis.bez_ekstrakcji:
                    bez_ekstrakcji += 1
            except Exception as e:  # noqa: BLE001 — jeden zły post nie wywala reszty
                # Post pobrany i opłacony, ale niezapisany — zniknie bez śladu,
                # jeśli tego nie policzymy. Pełny komunikat tylko przy pierwszym
                # (padnięta baza daje ich tyle, ile postów w przebiegu).
                if not niezapisane:
                    log(f"[{KTO}] BŁĄD ZAPISU: {_jedna_linia(e)}")
                niezapisane += 1
                continue
            if identyfikator in do_naprawy:
                # Wiersz był w bazie z werdyktem i bez ekstrakcji; ten przebieg
                # go uzupełnił. Nie jest to nowy post i nie wolno go tak liczyć —
                # inaczej statystyka grupy zaczyna rosnąć od naprawiania.
                do_naprawy.discard(identyfikator)
                naprawione += 1
            else:
                nowe += 1
            istniejace.add(identyfikator)
            if not decyzja.gate_werdykt:
                bramka_by_odrzucila += 1
            if decyzja.stale:
                stare += 1
            elif decyzja.zrodlo == "gate" and decyzja.status == "smiec":
                odsiane += 1
            if decyzja.czy_zlecenie:
                zlecenia += 1
                # Znacznik ładunku W LOGU, nie tylko w bazie. Przy
                # ALERT_ZWIERZETA=0 taki post nie brzęczy telefonem, więc bez tej
                # linii jedyny ślad po nim byłby w panelu — a cisza, której nie
                # widać w logu, jest nie do odróżnienia od awarii wysyłki.
                znacznik = ("  [ZWIERZĘ]"
                            if decyzja.kategoria_ladunku == gate.KAT_ZWIERZE else "")
                log(f"[{KTO}] {_skrot(post['tresc'])}: ZLECENIE "
                    f"[{decyzja.jezyk or '??'}]{znacznik} "
                    f"{post.get('post_url') or '(BRAK LINKU)'}")
                # Alert idzie TU, a nie „gdzieś dalej w pipelinie" — dalej nie ma
                # nic. To jest jedyne miejsce w systemie, w którym wiadomo, że
                # pojawiło się nowe zlecenie i że jest już zapisane.
                if _powiadom(identyfikator, post, decyzja, zapis, log=log):
                    wyslane += 1
            elif not decyzja.pytano_model and decyzja.status == "nowe":
                log(f"[{KTO}] {_skrot(post['tresc'])}: {decyzja.powod}")

        # Stan grupy zapisujemy TAKŻE po błędzie pobrania — inaczej padająca
        # grupa byłaby odpytywana w KAŻDYM przebiegu, bo nie zdążyła dostać
        # `nastepny_run_at`, i awaria jednej grupy zamieniałaby się w koszt.
        try:
            conn = psycopg2.connect(settings.DATABASE_URL)
            try:
                _zapisz_harmonogram(
                    conn, g.url, doba,
                    pobrane_doba=g.pobrane_doba + len(itemy),
                    przydzial=g.przydzial, interwal_min=g.odstep_min,
                    ostatni_run_at=teraz,
                    nastepny_run_at=teraz + timedelta(minutes=g.odstep_min),
                    blad=blad)
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            # Niezapisany harmonogram znaczy, że ta grupa zostanie zapytana
            # w następnym przebiegu — czyli koszt, a nie utrata danych. Warte
            # jednej linii w logu, nie przerwania przebiegu.
            log(f"[{KTO}] {g.nazwa}: nie udało się zapisać harmonogramu "
                f"({type(e).__name__}) — grupa wypadnie ponownie w następnym "
                f"przebiegu.")

    tryb_bramki = gate.normalizuj_tryb(settings.GATE_TRYB)
    log(f"[{KTO}] gotowe: {nowe} nowych postów ({zlecenia} zleceń, {wyslane} "
        f"wysłanych alertów, {odsiane} odsianych przez bramkę, {stare} za "
        f"starych), {duplikaty} duplikatów; pobrane w tej dobie: "
        f"{pobrane_lacznie}/{plan.budzet}")
    if naprawione:
        log(f"[{KTO}] uzupełniono ekstrakcję w {naprawione} wierszach, które "
            f"miały werdykt modelu i komplet NULL-i (posty z przebiegów sprzed "
            f"poprawki zapisu).")
    if zlecenia and not wyslane:
        # NAJWAŻNIEJSZA LINIA W TYM PODSUMOWANIU. Zlecenia w bazie i zero
        # alertów wygląda z zewnątrz jak spokojny dzień na grupach, a znaczy,
        # że operator nie dowiedział się o ANI JEDNYM kursie. Powód każdego
        # pominięcia stoi w liniach `[powiadomienia]` wyżej.
        log(f"[{KTO}] UWAGA: {zlecenia} zleceń i ANI JEDNEGO wysłanego alertu. "
            f"Operator o nich nie wie. Sprawdź linie `[powiadomienia]` wyżej "
            f"(brak TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID, pauza po /stop, cisza "
            f"nocna, limit godzinowy) — zlecenia są w bazie i w panelu.")
    if tryb_bramki == gate.TRYB_CIEN:
        # Bez tej linii tryb cienia wygląda jak brak bramki. Liczba po prawej to
        # dokładnie to, co system zacznie oszczędzać po przełączeniu na
        # `aktywny` — i dokładnie to, czym ryzykuje, jeśli raport pokaże
        # fałszywe odrzucenia.
        log(f"[{KTO}] bramka w TRYBIE CIENIA — nic nie zablokowała, ale "
            f"odrzuciłaby {bramka_by_odrzucila} z {nowe} postów. "
            f"Rozliczenie: python laweta_radar/scripts/raport_gate.py")
    if bez_linku:
        log(f"[{KTO}] UWAGA: {bez_linku} postów bez post_url — sprawdź _first_str.")
    if niezapisane:
        # Te posty zostały pobrane i OPŁACONE, a mimo to nie ma ich w bazie.
        # Bez tej linii różnica między „cicha noc" a „baza odrzuca zapisy" jest
        # niewidoczna aż do momentu, w którym ktoś zapyta o statystyki.
        log(f"[{KTO}] UWAGA: {niezapisane} pobranych postów NIE trafiło do bazy "
            f"— zapłacone i utracone. Sprawdź prawa roli i stan bazy.")
    if bez_ekstrakcji:
        # Wiersz JEST, więc żadna statystyka tego nie pokaże — a mimo to wynik
        # modelu przepadł. To jedyna linia, po której da się to zauważyć w dniu,
        # w którym się dzieje, zamiast przy pierwszym `SELECT count(typ)`.
        log(f"[{KTO}] UWAGA: {bez_ekstrakcji} postów zapisano z werdyktem modelu, "
            f"ale bez ani jednego pola z ekstrakcji — zapłacone tokeny, zerowy "
            f"zapis. Szczegóły w liniach OSTRZEŻENIE wyżej (z fb_id).")
    if klasyfikator_padl:
        # Osobna, ostatnia linia: bez niej „0 zleceń" wygląda jak cicha noc na
        # grupach, a jest zepsutym deployem.
        log(f"[{KTO}] UWAGA: klasyfikator nie działał w tym przebiegu "
            f"({klasyfikator_padl}). Posty czekają w bazie ze statusem 'nowe'.")

    # PIĄTA POPRAWKA — ostatnia linia podsumowania, celowo: to jest liczba, po
    # której widać, czy MIN_INTERWAL_MIN/PROG_WYKORZYSTANIA_BUDZETU realnie
    # skróciły czas do alertu, czy tylko wyglądają dobrze w planie. Best-effort
    # (jak reszta diagnostyki w tym pliku) — brak tej jednej liczby w logu nie
    # ma prawa zablokować przebiegu, który już wysłał, co miał wysłać.
    conn_mediana = _polacz_best_effort()
    if conn_mediana is not None:
        try:
            mediana = _mediana_opoznienia_alertow_min(conn_mediana)
        except Exception as e:  # noqa: BLE001 — metryka nie może wywalić przebiegu
            log(f"[{KTO}] nie policzyłem mediany opóźnienia alertów (24h): "
                f"{_jedna_linia(e)}")
        else:
            if mediana is None:
                log(f"[{KTO}] mediana opóźnienia alertów (24h): brak danych "
                    f"(zero alertów z opublikowany_at w oknie)")
            else:
                ocena = ("OK" if mediana < cfg_groups.CEL_MEDIANY_OPOZNIENIA_MIN
                         else "PONAD CELEM")
                log(f"[{KTO}] mediana opóźnienia alertów (24h, opublikowany_at -> "
                    f"wyslano_at): {mediana:.1f} min — cel <"
                    f"{cfg_groups.CEL_MEDIANY_OPOZNIENIA_MIN} min: {ocena}")
        finally:
            conn_mediana.close()
    return 0


def _zapis(identyfikator: str, post: dict, decyzja: Decyzja, log=print) -> Zapis:
    """Zapis jednego posta na ŚWIEŻYM połączeniu (patrz komentarz w `run`)."""
    import psycopg2  # noqa: PLC0415 — leniwie, jak wszędzie w tym repo

    conn = psycopg2.connect(settings.DATABASE_URL)
    try:
        return _zapisz_post(conn, identyfikator, post, decyzja, log=log)
    finally:
        conn.close()


def zlecenie_do_alertu(identyfikator: str, post: dict, decyzja: Decyzja,
                       zapis: Zapis) -> dict:
    """Kontrakt `services/powiadomienia.powiadom_o_zleceniu` — z tego, co W BAZIE.

    Pola ekstrakcji bierzemy z `zapis.wiersz`, czyli z wiersza odczytanego po
    zapisie, a NIE ze słownika, który szedł do INSERT-a. Różnica jest dokładnie
    tej samej natury co bug, przez który powstało to zgłoszenie: alert zbudowany
    z pamięci opisywałby zlecenie, którego w bazie może nie być w tym kształcie
    — a operator ma dostać to, co panel mu pokaże po kliknięciu.

    Brakujące pola zostają BRAKUJĄCE (None) i to powiadomienie ma je nazwać
    („pewność nieznana", „trasa nieustalona"). Podstawianie tu wartości
    domyślnych zamieniłoby brak danych w cichą, wiarygodnie wyglądającą
    nieprawdę — a to jest gorsze niż pusty nawias w wiadomości.
    """
    return {
        **zapis.wiersz,
        "fb_id": identyfikator,
        "tresc": post.get("tresc"),
        "post_url": post.get("post_url") or None,
        "grupa_nazwa": post.get("group_name") or None,
        "opublikowany_at": post.get("post_date"),
        "jezyk": decyzja.jezyk or "",
        # Kategoria ładunku decyduje, czy alert w ogóle wyjdzie (ALERT_ZWIERZETA)
        # i czy dostanie znacznik w treści. Bez tego pola powiadomienie widziałoby
        # transport konia dokładnie tak samo jak transport golfa.
        "kategoria_ladunku": decyzja.kategoria_ladunku,
        # Kierunek trafia tu na wypadek ROZBIEŻNOŚCI: bramka rozpoznała ofertę,
        # a model mimo to orzekł „zlecenie". Wtedy — i tylko wtedy — ta funkcja
        # w ogóle się wykonuje dla oferty, bo `czy_zlecenie=false` nie dochodzi
        # do wysyłki. Powiadomienia mają wtedy ostatnie słowo (ALERT_OFERTY),
        # zamiast budzić operatora cudzą lawetą jadącą własną trasą.
        "kierunek": decyzja.kierunek,
    }


def _powiadom(identyfikator: str, post: dict, decyzja: Decyzja, zapis: Zapis,
              log=print) -> bool:
    """Alert o jednym zleceniu. NIGDY nie wywala przebiegu i zawsze zostawia ślad.

    DLACZEGO TO TU JEST. `services/powiadomienia.py` był kompletny — treść,
    dedup, antyspam, przyciski, podsumowanie nocne — i nie miał ANI JEDNEGO
    wołającego. Fetcher wypisywał „ZLECENIE" do logu i przechodził do
    następnego posta, więc tabela `powiadomienia` została pusta przy 15
    zleceniach w bazie, a system wyglądał jak rynek bez zleceń. Pipeline
    z README (`fetcher -> gate -> classifier -> geo -> Telegram`) kończył się
    na klasyfikatorze.

    Wysyłamy PO zapisie i tylko po udanym zapisie: dedup powiadomień stoi na
    wierszu w bazie, więc alert o poście, którego nie ma w `posty`, poszedłby
    ponownie przy każdym kolejnym przebiegu.
    """
    try:
        from laweta_radar.services import powiadomienia  # noqa: PLC0415 — leniwie
    except Exception as e:  # noqa: BLE001 — brak modułu nie może zabić przebiegu
        log(f"[{KTO}] UWAGA: nie mogę wysłać powiadomienia ({_jedna_linia(e)}) "
            f"— zlecenie {identyfikator} jest w bazie i w panelu, ale nikt o nim "
            f"nie wie.")
        return False
    return powiadomienia.powiadom_o_zleceniu(
        zlecenie_do_alertu(identyfikator, post, decyzja, zapis))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Pobranie postów z grup FB przez Apify + bramka słowna + zapis "
                    "do tabeli `posty`. Bez kluczy / bez grup / bez migracji "
                    "kończy czysto, nic nie robiąc.")
    ap.add_argument("--budzet", type=int, default=None, metavar="N",
                    help="nadpisz dobowy sufit pobranych postów dla całego systemu "
                         f"(domyślnie POSTY_NA_DOBE z .env = {settings.POSTY_NA_DOBE})")
    ap.add_argument("--grupa", default=None, metavar="URL",
                    help="pobierz JEDNĄ grupę, z pominięciem harmonogramu — do testów")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="płaski resultsLimit dla wszystkich grup, wyłącza adaptację")
    ap.add_argument("--sucho", action="store_true",
                    help="pokaż plan wywołań i przewidywany koszt, NIE wołaj actora")
    args = ap.parse_args(argv[1:])

    try:
        return run(budzet=args.budzet, tylko_grupa=args.grupa, sucho=args.sucho,
                   nadpisanie_limitu=args.limit)
    except AllKeysExhausted as e:
        # Czysty, czytelny komunikat BEZ stack trace — to jest stan operacyjny
        # („skończył się kredyt"), a nie błąd programu.
        print(f"[{KTO}] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
