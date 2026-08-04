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
from laweta_radar.workers import apify_proxy, gate
from laweta_radar.workers.apify_keys import AllKeysExhausted, KeyRotator, load_apify_tokens

KTO = "fb-fetcher"
API = "https://api.apify.com/v2"

# Raport z pomiaru actora. Fetcher tylko go CZYTA — pisze go
# `scripts/pomiar_actora.py`, uruchamiany ręcznie.
RAPORT_POMIARU = Path(__file__).resolve().parent.parent.parent / "docs" / "POMIAR-ACTORA.md"

# Werdykt pomiaru stoi w raporcie w linii „### ROZSTRZYGNIĘCIE: **ŚCIEŻKA A**".
# Czytamy go regeksem, a nie ręcznym przepisaniem do .env, bo przepisanie jest
# krokiem, który da się pominąć — i wtedy fetcher pracuje na cudzej intuicji
# zamiast na pomiarze, nie mówiąc o tym ani słowem.
_WZORZEC_SCIEZKI = re.compile(r"ROZSTRZYGNI[ĘE]CIE:\s*\*\*ŚCIEŻKA\s+([AB])\*\*")


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

    trafienie = _WZORZEC_SCIEZKI.search(tresc)
    if trafienie:
        return trafienie.group(1), f"z pomiaru ({plik.name})"
    return "B", (f"{plik.name} nie zawiera rozstrzygnięcia (pomiar nie został "
                 f"wykonany) — domyślna, ostrożna")


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
                     token: str) -> list[dict]:
    """Synchroniczne wywołanie actora dla jednej grupy -> lista surowych itemów.

    `run-sync-get-dataset-items` oddaje od razu zawartość datasetu, bez
    pollowania. Token leci w NAGŁÓWKU (nie w URL-u — URL-e trafiają do logów),
    a ruch przez proxy PRZYPISANE DO TEGO KLUCZA: bez tego cała pula kont wychodzi
    z jednego adresu VPS-a, co dla Apify wygląda jak multi-accounting i kończy się
    utratą całej puli naraz, nie jednego konta.

    Błędy HTTP lecą wyżej NIETKNIĘTE — po nich `apify_keys` poznaje wyczerpany
    klucz i odróżnia go od chwilowej awarii proxy. Opakowanie ich we własny
    wyjątek zamieniłoby rotację kluczy w zgadywankę.
    """
    url = f"{API}/acts/{cfg_groups.APIFY_ACTOR}/run-sync-get-dataset-items"
    with apify_proxy.client_for_token(token, timeout=cfg_groups.APIFY_TIMEOUT) as klient:
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


def _istniejace_id(conn) -> set[str]:
    """Wszystkie znane fb_id — dedup w pamięci na czas przebiegu.

    Jedno zapytanie zamiast SELECT-a per post: przebieg dotyka kilkuset postów,
    a tabela rośnie wolno (kilkanaście tysięcy wierszy na miesiąc), więc komplet
    identyfikatorów mieści się w pamięci bez dyskusji.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT fb_id FROM zlecenia")
        return {r[0] for r in cur.fetchall()}


def _statystyki_grup(conn, okno_dni: int, okno_tempa_h: int) -> dict[str, dict]:
    """Surowiec dla bandyty i dla limitu adaptacyjnego — jednym zapytaniem.

    Zwraca per grupa:
      pobrane / zlecenia  — wydajność w oknie `okno_dni` (wejście bandyty),
      ostatnie / pierwszy_pobrany_at — tempo postowania w oknie `okno_tempa_h`
                            (wejście limitu adaptacyjnego i odstępu).

    Jedno zapytanie, nie N — korzysta z indeksu (group_url, post_date DESC)
    z migracji 0002. Bez niego to pełny skan tabeli, która rośnie o KAŻDY
    pobrany post, także odrzucony przez bramkę.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT group_url,
                   COUNT(*) FILTER (
                       WHERE pobrano_at > NOW() - (%s || ' days')::interval
                   ) AS pobrane,
                   COUNT(*) FILTER (
                       WHERE pobrano_at > NOW() - (%s || ' days')::interval
                         AND czy_zlecenie
                   ) AS zlecenia,
                   COUNT(*) FILTER (
                       WHERE pobrano_at > NOW() - (%s || ' hours')::interval
                   ) AS ostatnie,
                   MIN(pobrano_at) AS pierwszy_pobrany_at
            FROM zlecenia
            WHERE group_url IS NOT NULL
            GROUP BY group_url
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


def _zapisz_post(conn, identyfikator: str, post: dict, *, zrodlo: str,
                 czy_zlecenie: bool, jezyk: str, status: str, stale: bool) -> None:
    """Zapis jednego posta. ON CONFLICT (fb_id) DO NOTHING — dedup jest darmowy.

    Commit per post, jak w repo źródłowym: przebieg trwa minutami i przeplata
    zapisy z długimi wywołaniami sieciowymi, więc jedna wielka transakcja
    znaczyłaby, że błąd na ostatniej grupie kasuje pracę wszystkich wcześniejszych.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO zlecenia (fb_id, tresc, post_url, group_url, group_name,
                                  author_name, post_date, zrodlo_decyzji,
                                  czy_zlecenie, jezyk, status, stale)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fb_id) DO NOTHING
            """,
            (identyfikator, post["tresc"], post.get("post_url") or None,
             post.get("group_url") or None, post.get("group_name") or None,
             post.get("author_name") or None, post.get("post_date"),
             zrodlo, czy_zlecenie, jezyk or None, status, stale),
        )
    conn.commit()


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
                 min_interwal: int) -> int:
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
    """
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

    urle = [g["url"] for g in grupy]
    przydzialy = rozdziel_budzet(urle, budzet, statystyki)

    for grupa in grupy:
        url = grupa["url"]
        stan = harmonogram.get(url) or {}
        stat = statystyki.get(url) or {}
        tempo = tempo_na_godzine(stat, teraz, cfg_groups.OKNO_TEMPA_H)
        przydzial = przydzialy.get(url, 0)
        odstep = interwal_min(tempo, przydzial, sciezka, min_interwal)
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
    linie = [
        f"[{KTO}] ścieżka actora: {plan.sciezka} ({plan.skad_sciezka})",
        f"[{KTO}] budżet dobowy: {plan.budzet} postów, zużyte {plan.zuzyte_doba}, "
        f"zostało {plan.zostalo}",
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

    werdykt = gate.gate(post.get("tresc") or "")
    if not werdykt.przepuszczony:
        return Decyzja(zrodlo="gate", czy_zlecenie=False, jezyk=werdykt.jezyk,
                       status="smiec", stale=stale, powod=werdykt.powod,
                       pytano_model=False)
    if stale:
        return Decyzja(zrodlo="gate", czy_zlecenie=False, jezyk=werdykt.jezyk,
                       status="smiec", stale=True,
                       powod=f"za stary ({data.isoformat()}) — bez modelu i bez alertu",
                       pytano_model=False)

    wynik = klasyfikuj(post.get("tresc") or "", grupa, werdykt.jezyk)
    if wynik is None:
        return Decyzja(zrodlo="gate", czy_zlecenie=False, jezyk=werdykt.jezyk,
                       status="nowe", stale=False,
                       powod=f"przez bramkę ({werdykt.powod}) — czeka na klasyfikator",
                       pytano_model=False)

    czy = bool(wynik.get("czy_zlecenie"))
    return Decyzja(zrodlo="ai", czy_zlecenie=czy, jezyk=werdykt.jezyk,
                   status="nowe" if czy else "smiec", stale=False,
                   powod=str(wynik.get("powod") or ""), pytano_model=True)


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
                braki = [t for t in ("zlecenia", "harmonogram")
                         if not _tabela_istnieje(conn, t)]
                if braki:
                    log(f"[{KTO}] Brak tabel: {', '.join(braki)}. Odpal migracje "
                        f"jako postgres:\n    bash laweta_radar/scripts/migrate.sh")
                    if not sucho:
                        return 0
                else:
                    baza_gotowa = True
                    istniejace = _istniejace_id(conn)
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

    rotator = KeyRotator.for_tokens(
        tokeny, transient_key_switches=2 if apify_proxy.is_enabled() else 0)
    prog_swiezosci = teraz - timedelta(hours=settings.MAX_WIEK_POSTA_H)
    nowe = zlecenia = duplikaty = odsiane = stare = bez_linku = 0
    niezapisane = 0
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
                    _apify_run_group(u, lim, okno, sciezka, tok))
        except AllKeysExhausted:
            raise   # wszystkie klucze puste — kolejne grupy nie mają szans
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
            if identyfikator in istniejace:
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
                _zapis(identyfikator, post, zrodlo=decyzja.zrodlo,
                       czy_zlecenie=decyzja.czy_zlecenie, jezyk=decyzja.jezyk,
                       status=decyzja.status, stale=decyzja.stale)
            except Exception as e:  # noqa: BLE001 — jeden zły post nie wywala reszty
                # Post pobrany i opłacony, ale niezapisany — zniknie bez śladu,
                # jeśli tego nie policzymy. Pełny komunikat tylko przy pierwszym
                # (padnięta baza daje ich tyle, ile postów w przebiegu).
                if not niezapisane:
                    log(f"[{KTO}] BŁĄD ZAPISU: {_jedna_linia(e)}")
                niezapisane += 1
                continue
            istniejace.add(identyfikator)
            nowe += 1
            if decyzja.stale:
                stare += 1
            elif decyzja.zrodlo == "gate" and decyzja.status == "smiec":
                odsiane += 1
            if decyzja.czy_zlecenie:
                zlecenia += 1
                log(f"[{KTO}] {_skrot(post['tresc'])}: ZLECENIE "
                    f"[{decyzja.jezyk or '??'}] "
                    f"{post.get('post_url') or '(BRAK LINKU)'}")
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

    log(f"[{KTO}] gotowe: {nowe} nowych postów ({zlecenia} zleceń, {odsiane} "
        f"odsianych przez bramkę, {stare} za starych), {duplikaty} duplikatów; "
        f"pobrane w tej dobie: {pobrane_lacznie}/{plan.budzet}")
    if bez_linku:
        log(f"[{KTO}] UWAGA: {bez_linku} postów bez post_url — sprawdź _first_str.")
    if niezapisane:
        # Te posty zostały pobrane i OPŁACONE, a mimo to nie ma ich w bazie.
        # Bez tej linii różnica między „cicha noc" a „baza odrzuca zapisy" jest
        # niewidoczna aż do momentu, w którym ktoś zapyta o statystyki.
        log(f"[{KTO}] UWAGA: {niezapisane} pobranych postów NIE trafiło do bazy "
            f"— zapłacone i utracone. Sprawdź prawa roli i stan bazy.")
    if klasyfikator_padl:
        # Osobna, ostatnia linia: bez niej „0 zleceń" wygląda jak cicha noc na
        # grupach, a jest zepsutym deployem.
        log(f"[{KTO}] UWAGA: klasyfikator nie działał w tym przebiegu "
            f"({klasyfikator_padl}). Posty czekają w bazie ze statusem 'nowe'.")
    return 0


def _zapis(identyfikator: str, post: dict, **kwargi) -> None:
    """Zapis jednego posta na ŚWIEŻYM połączeniu (patrz komentarz w `run`)."""
    import psycopg2  # noqa: PLC0415 — leniwie, jak wszędzie w tym repo

    conn = psycopg2.connect(settings.DATABASE_URL)
    try:
        _zapisz_post(conn, identyfikator, post, **kwargi)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Pobranie postów z grup FB przez Apify + bramka słowna + zapis "
                    "do tabeli `zlecenia`. Bez kluczy / bez grup / bez migracji "
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
