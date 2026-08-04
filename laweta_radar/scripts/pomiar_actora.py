"""Jednorazowy pomiar actora `apify/facebook-groups-scraper` — ZANIM napiszemy fetcher.

NIE jest częścią pipeline'u. Odpalasz raz, ręcznie, i dostajesz `docs/POMIAR-ACTORA.md`
z trzema liczbami, na których stoi cała dalsza architektura. Bez nich fetcher
piszemy na wyczucie, a wyczucie kosztuje tu realne pieniądze.

PYTANIE 1 — jaką najmniejszą jednostkę czasu przyjmuje `onlyPostsNewerThan`?
  Seria wywołań dla JEDNEJ grupy, zmienia się WYŁĄCZNIE to pole: 7 days, 1 day,
  12 hours, 1 hour, 30 minutes.
    ŚCIEŻKA A — okno działa: fetcher pobiera tylko przyrost od ostatniego przebiegu.
    ŚCIEŻKA B — okno ignorowane: KAŻDY przebieg ściąga te same posty od nowa
                i płacimy za nie tyle razy, ile razy odpalimy fetchera.
  Przy przebiegu co 5 minut to różnica rzędu 288× w rachunku za tę samą grupę —
  dlatego jest to najważniejsza liczba w projekcie.

PYTANIE 2 — `resultsLimit` przy WIELU grupach w `startUrls`: per grupa czy globalny?
  Ten sam limit raz dla jednej grupy, raz dla trzech. Limit globalny znaczy, że
  batch po dziesięć grup zgubi posty z ośmiu z nich — a run i tak zostanie
  policzony. To rozstrzyga, czy batchowanie jest w ogóle bezpieczne.

PYTANIE 3 — ile realnie kosztuje jeden pobrany post?
  Liczymy DWOMA niezależnymi sposobami: z licznika konta (`apify_credits`) i z
  `usageTotalUsd` każdego runu. Zgodność = pomiar wiarygodny; rozjazd jest
  informacją, a nie błędem. Wynik wchodzi wprost do `POSTY_NA_DOBE` w prompcie 2
  i do decyzji „ile kont Apify / czy płatny plan".

ŚCIEŻKA WYWOŁANIA JEST TA SAMA CO W PRODUKCJI: `KeyRotator` + `client_for_token`.
Pomiar zrobiony obok tej ścieżki mierzyłby coś, czego fetcher nigdy nie zobaczy.

ZASADY, KTÓRE TEN SKRYPT EGZEKWUJE SAM:
  - liczy i pokazuje przewidywany koszt PRZED serią i czeka na potwierdzenie,
  - twardy sufit pobranych postów (domyślnie 500) — odmawia startu powyżej,
  - odstęp między wywołaniami, zero zrównoleglania,
  - gdy najszersze okno zwróci ZERO postów, przerywa: to znak, że grupa nie jest
    publiczna albo jest martwa, a wtedy mierzymy błąd, nie zachowanie actora.

UŻYCIE:
    export PYTHONPATH=$PWD
    python -m laweta_radar.scripts.pomiar_actora \\
        --grupa https://www.facebook.com/groups/PUBLICZNA_TESTOWA \\
        --grupa https://www.facebook.com/groups/DRUGA \\
        --grupa https://www.facebook.com/groups/TRZECIA

    --tylko 1        # samo pytanie 1 (najtańsze; jedna grupa wystarczy)
    --klucz 3        # zmierz na kluczu #3 zamiast #1
    --sucho          # pokaż plan i koszt, NIE odpalaj niczego
    --tak            # bez pytania o potwierdzenie (do skryptów)
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from laweta_radar.config import groups as cfg_groups
from laweta_radar.workers import apify_credits, apify_proxy, apify_run
from laweta_radar.workers.apify_keys import AllKeysExhausted, KeyRotator, load_apify_tokens

# --- Co mierzymy -----------------------------------------------------------
ACTOR = cfg_groups.APIFY_ACTOR          # ten sam, którego użyje fetcher
KTO = "pomiar-actora"

# Okna od najszerszego do najwęższego. Kolejność ma znaczenie: interpretacja czyta
# je jako ciąg zwężający i pyta, czy wyniki za nim nadążają.
OKNA = ("7 days", "1 day", "12 hours", "1 hour", "30 minutes")

# Limit itemów w pytaniu 1. 30 wystarcza, żeby zobaczyć spadek liczby postów przy
# zwężaniu okna, i mieści całą serię w budżecie pomiaru.
LIMIT_P1 = 30
# Limit w pytaniu 2 — MUSI być identyczny w obu wywołaniach, bo to jego zachowanie
# przy jednej i przy trzech grupach jest tu mierzoną wielkością.
LIMIT_P2 = 30
# Ile grup w drugim wywołaniu pytania 2.
GRUP_W_BATCHU = 3

# Twardy sufit pobranych postów w całym pomiarze. Pomiar ma odpowiedzieć na trzy
# pytania, a nie zjeść darmowy kredyt konta (~5 USD/mies. = ~1000 postów po cenie
# katalogowej). Powyżej tej liczby skrypt odmawia startu.
SUFIT_ITEMOW = 500

# Cena katalogowa ze strony actora — TYLKO do oszacowania kosztu PRZED serią
# i do porównania z ceną zmierzoną. Nigdy nie wchodzi do wyniku pomiaru.
CENA_KATALOGOWA_USD_ZA_POST = 0.005

ODSTEP_S = 5.0                 # przerwa między wywołaniami; bez zrównoleglania
TIMEOUT_RUNU_S = cfg_groups.APIFY_TIMEOUT

# Tolerancja przy sprawdzaniu „czy najstarszy post mieści się w oknie". Znacznik
# czasu posta bywa zaokrąglany przez FB do minut, a między startem runu a pobraniem
# ostatniej strony mija realny czas — bez marginesu każdy poprawnie działający
# filtr wyglądałby na nieszczelny.
TOL_WZGLEDNA = 1.10            # +10%
TOL_BEZWZGLEDNA_H = 0.25       # + 15 minut

# Gdzie item niesie czas publikacji i adres grupy. To NIE jest zgadywanie na
# wszelki wypadek: nazwy pól różnią się między wersjami actora, a pomiar ma
# USTALIĆ, która jest aktualna, i zapisać ją w raporcie dla prompta 2.
POLA_CZASU = ("time", "timestamp", "publishedAt", "postedAt", "createdAt", "date")
POLA_GRUPY = ("groupUrl", "facebookUrl", "groupId", "groupTitle", "group", "url")

ROOT = Path(__file__).resolve().parent.parent.parent
RAPORT = ROOT / "docs" / "POMIAR-ACTORA.md"


# ===========================================================================
# CZYSTE FUNKCJE — cała interpretacja siedzi tutaj, żeby dała się przetestować
# bez sieci i bez wydawania ani centa (tests/test_pomiar_actora.py).
# ===========================================================================
@dataclass
class WynikOkna:
    """Jedno wywołanie z pytania 1."""

    okno: str
    godziny: float | None
    ile: int = 0
    najstarszy_h: float | None = None
    najnowszy_h: float | None = None
    trwanie_s: float | None = None
    koszt_usd: float | None = None
    blad: str | None = None


@dataclass
class WynikLimitu:
    """Jedno wywołanie z pytania 2."""

    ile_grup_w_wejsciu: int
    limit: int
    ile: int = 0
    na_grupe: dict[str, int] = field(default_factory=dict)
    trwanie_s: float | None = None
    koszt_usd: float | None = None
    blad: str | None = None


@dataclass
class Rozstrzygniecie:
    """Werdykt: `A` / `B` / `?` plus zdanie, DLACZEGO tak."""

    sciezka: str
    powod: str

    @property
    def jednoznaczne(self) -> bool:
        return self.sciezka in ("A", "B")


def okno_na_godziny(okno: str) -> float | None:
    """"12 hours" -> 12.0. None, gdy nie umiemy tego przeliczyć.

    Rozumiemy tylko własne stałe z `OKNA` — to nie jest parser dla użytkownika,
    tylko przeliczenie okien, które sami wysyłamy.
    """
    czesci = (okno or "").strip().split()
    if len(czesci) != 2:
        return None
    try:
        ile = float(czesci[0])
    except ValueError:
        return None
    jednostka = czesci[1].rstrip("s").lower()
    mnoznik = {"minute": 1 / 60, "hour": 1.0, "day": 24.0, "week": 168.0}.get(jednostka)
    return None if mnoznik is None else ile * mnoznik


def czas_posta(item: dict) -> tuple[datetime | None, str | None]:
    """(czas publikacji, nazwa pola, z którego go wzięliśmy).

    Nazwa pola jest tu równie ważna co czas: trafia do raportu, żeby prompt 2
    nie musiał jej odkrywać drugi raz — tym razem już na produkcji.
    """
    for pole in POLA_CZASU:
        wartosc = item.get(pole)
        if wartosc in (None, ""):
            continue
        if isinstance(wartosc, (int, float)):
            # Unix. Sekundy vs milisekundy rozstrzygamy po rzędzie wielkości:
            # 10^11 s to rok 5138, więc większa liczba na pewno jest w ms.
            sekundy = float(wartosc) / (1000.0 if wartosc > 1e11 else 1.0)
            try:
                return datetime.fromtimestamp(sekundy, tz=timezone.utc), pole
            except (OverflowError, OSError, ValueError):
                continue
        try:
            czas = datetime.fromisoformat(str(wartosc).replace("Z", "+00:00"))
        except ValueError:
            continue
        return (czas if czas.tzinfo else czas.replace(tzinfo=timezone.utc)), pole
    return None, None


def wieki_postow(itemy: list[dict], teraz: datetime | None = None
                 ) -> tuple[float | None, float | None, str | None, int]:
    """(wiek najstarszego [h], wiek najnowszego [h], pole z czasem, ile z datą).

    „Ile z datą" jest istotne: jeśli actor zwraca posty bez czasu, wiek najstarszego
    nie mówi nic o szczelności filtra i pomiar musi to przyznać, a nie policzyć
    średnią z tego, co akurat było.
    """
    teraz = teraz or datetime.now(timezone.utc)
    wieki: list[float] = []
    pole: str | None = None
    for item in itemy:
        czas, skad = czas_posta(item)
        if czas is None:
            continue
        pole = pole or skad
        wieki.append((teraz - czas).total_seconds() / 3600.0)
    if not wieki:
        return None, None, None, 0
    return max(wieki), min(wieki), pole, len(wieki)


def klucz_grupy(item: dict) -> str:
    """Do której grupy należy item — do rozbicia wyników pytania 2 na grupy."""
    for pole in POLA_GRUPY:
        wartosc = item.get(pole)
        if isinstance(wartosc, str) and wartosc.strip():
            return wartosc.strip()
        if isinstance(wartosc, dict):
            for podpole in ("url", "id", "name", "title"):
                v = wartosc.get(podpole)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return "(nieznana)"


def _poza_oknem(w: WynikOkna) -> bool:
    """Czy najstarszy post WYSZEDŁ poza zadane okno (z marginesem)."""
    if w.najstarszy_h is None or w.godziny is None:
        return False
    return w.najstarszy_h > w.godziny * TOL_WZGLEDNA + TOL_BEZWZGLEDNA_H


def _maleje(wartosci: list[float | None]) -> bool:
    """Ciąg nierosnący, w którym choć raz spadło. None-y pomijamy.

    „Choć raz spadło" jest tu kluczowe: sam brak wzrostu spełnia też grupa martwa,
    w której każde okno zwraca to samo zero — a to nie dowodzi niczego o filtrze.
    """
    znane = [v for v in wartosci if v is not None]
    if len(znane) < 2:
        return False
    if any(b > a + 1e-9 for a, b in zip(znane, znane[1:])):
        return False
    return any(b < a - 1e-9 for a, b in zip(znane, znane[1:]))


def rozstrzygnij_okno(wyniki: list[WynikOkna]) -> Rozstrzygniecie:
    """ŚCIEŻKA A / B / „?" dla `onlyPostsNewerThan`. Kolejność reguł jest istotna.

    Sprawdzamy od dowodów najmocniejszych do najsłabszych, bo tylko tak „?" znaczy
    naprawdę „nie wiadomo", a nie „nie trafiło w żadną regułę".
    """
    if not wyniki:
        return Rozstrzygniecie("?", "Brak wyników — seria się nie odbyła.")

    udane = [w for w in wyniki if w.blad is None]
    if not udane:
        return Rozstrzygniecie(
            "?", "KAŻDE wywołanie zakończyło się błędem — to awaria dostępu do actora, "
                 "nie zachowanie filtra. Napraw dostęp i powtórz pomiar.")

    # 1. Actor odrzuca WĄSKIE okna, a szerokie łyka. Jednostka poniżej doby nie
    #    jest obsługiwana — a to jest dokładnie definicja ścieżki B.
    waskie = [w for w in wyniki if (w.godziny or 999) <= 12]
    szerokie = [w for w in wyniki if (w.godziny or 0) > 12]
    if waskie and all(w.blad for w in waskie) and any(w.blad is None for w in szerokie):
        return Rozstrzygniecie(
            "B", "Actor przyjmuje okna dobowe, ale ODRZUCA wszystkie poniżej 12 h "
                 f"(błąd: {waskie[0].blad}). Jednostki krótszej niż doba nie ma.")

    # 2. Filtr policzony, ale NIESZCZELNY: przyszły posty starsze niż zadane okno.
    #    Najmocniejszy dowód ścieżki B — pole jest przyjmowane i ignorowane.
    poza = [w for w in udane if _poza_oknem(w)]
    if poza:
        w = poza[0]
        return Rozstrzygniecie(
            "B", f"Dla okna „{w.okno}” najstarszy post ma {w.najstarszy_h:.1f} h — "
                 f"poza oknem. Pole jest przyjmowane i IGNOROWANE, więc każdy przebieg "
                 f"pobierze (i policzy) te same posty od nowa.")

    # 3. Filtr działa: przy zwężaniu okna spada liczba postów ALBO wiek najstarszego.
    #    Wystarczy jedno z dwojga — w grupie ruchliwej limit ścina liczbę do stałej,
    #    a wtedy jedynym widocznym objawem działania filtra jest młodniejący ogon.
    ile_maleje = _maleje([w.ile for w in udane])
    wiek_maleje = _maleje([w.najstarszy_h for w in udane])
    if ile_maleje or wiek_maleje:
        czym = "liczba postów" if ile_maleje else "wiek najstarszego posta"
        return Rozstrzygniecie(
            "A", f"Przy zwężaniu okna maleje {czym}, a żaden post nie wychodzi poza "
                 f"zadane okno. Filtr czasowy DZIAŁA — fetcher może pobierać sam "
                 f"przyrost od ostatniego przebiegu.")

    # 4. Wszystko płaskie i w oknie. Grupa jest za cicha, żeby cokolwiek rozstrzygnąć:
    #    filtr działający i filtr ignorowany dają tu identyczny wynik.
    razem = sum(w.ile for w in udane)
    return Rozstrzygniecie(
        "?", f"Wyniki są PŁASKIE ({razem} postów łącznie, żaden poza oknem) — przy tak "
             f"małym ruchu filtr działający i ignorowany wyglądają tak samo. Powtórz "
             f"pomiar na grupie, w której przez 7 dni przybywa wyraźnie więcej niż "
             f"{LIMIT_P1} postów.")


def rozstrzygnij_limit(jedna: WynikLimitu | None, trzy: WynikLimitu | None
                       ) -> Rozstrzygniecie:
    """Czy `resultsLimit` jest per grupa (A/„PER GRUPA"), czy globalny („GLOBALNY")."""
    if jedna is None or trzy is None:
        return Rozstrzygniecie("?", "Brak kompletu wywołań (potrzebne oba: 1 grupa i 3).")
    if jedna.blad or trzy.blad:
        return Rozstrzygniecie(
            "?", f"Błąd wywołania — 1 grupa: {jedna.blad or 'ok'}, "
                 f"3 grupy: {trzy.blad or 'ok'}.")

    limit = trzy.limit
    grup = max(1, trzy.ile_grup_w_wejsciu)
    reprezentowane = sum(1 for v in trzy.na_grupe.values() if v > 0)

    # Limit globalny: batch nie dostaje więcej niż pojedyncza grupa. Wynik
    # rozstrzygający, bo oznacza, że batchowanie GUBI dane, za które płacimy.
    if trzy.ile <= limit * 1.10:
        return Rozstrzygniecie(
            "GLOBALNY",
            f"3 grupy przy resultsLimit={limit} dały {trzy.ile} itemów (jedna grupa: "
            f"{jedna.ile}); posty pojawiły się z {reprezentowane} z {grup} grup. Limit "
            f"jest DZIELONY między grupy — batch po dziesięć grup zgubiłby posty "
            f"z ośmiu, a run i tak zostałby policzony. Batchowanie jest NIEBEZPIECZNE.")

    # Limit per grupa: batch skaluje się z liczbą grup.
    if trzy.ile >= limit * grup * 0.80 and reprezentowane >= grup - 1:
        return Rozstrzygniecie(
            "PER GRUPA",
            f"3 grupy przy resultsLimit={limit} dały {trzy.ile} itemów z "
            f"{reprezentowane} grup (≈{limit}×{grup}). Limit liczy się OSOBNO dla "
            f"każdej grupy — batchowanie jest bezpieczne i oszczędza wywołania.")

    return Rozstrzygniecie(
        "?", f"Wynik pośredni: {trzy.ile} itemów z {reprezentowane} z {grup} grup przy "
             f"limicie {limit} (jedna grupa: {jedna.ile}). Najczęstsza przyczyna to "
             f"grupa uboższa niż limit — powtórz na trzech grupach, z których KAŻDA "
             f"ma więcej niż {limit} postów w oknie.")


def koszt_na_post(koszt_usd: float | None, itemow: int) -> float | None:
    """USD za jeden pobrany post. None, gdy nie ma czego dzielić."""
    if koszt_usd is None or itemow <= 0:
        return None
    return koszt_usd / itemow


# ===========================================================================
# RAPORT
# ===========================================================================
def _lb(v, fmt: str = "{:.1f}", pusty: str = "—") -> str:
    return pusty if v is None else fmt.format(v)


def raport_md(*, wyniki_okien, werdykt_okna, jedna, trzy, werdykt_limitu,
              saldo_przed, saldo_po, koszt_z_runow, itemow_razem, pole_czasu,
              build, grupa, data_pomiaru) -> str:
    """Cały `docs/POMIAR-ACTORA.md`. Osobna funkcja, bo jest testowalna offline."""
    z_salda = None
    if saldo_przed is not None and saldo_po is not None:
        z_salda = max(0.0, saldo_po.zuzyte_usd - saldo_przed.zuzyte_usd)

    cena_z_runow = koszt_na_post(koszt_z_runow, itemow_razem)
    cena_z_salda = koszt_na_post(z_salda, itemow_razem)
    cena = cena_z_salda if cena_z_salda is not None else cena_z_runow

    L: list[str] = []
    L.append("# Pomiar actora `apify/facebook-groups-scraper`")
    L.append("")
    L.append("**Ten plik jest wynikiem pomiaru, nie notatką.** Generuje go")
    L.append("`laweta_radar/scripts/pomiar_actora.py`; ręczne poprawki zniknie przy")
    L.append("następnym uruchomieniu. Prompt 2 czyta go PRZED napisaniem")
    L.append("`_build_actor_input` — liczby stąd decydują o kształcie fetchera.")
    L.append("")
    L.append(f"- **Data pomiaru:** {data_pomiaru}")
    L.append(f"- **Actor:** `{ACTOR}`")
    L.append(f"- **Wersja (build):** {build or 'nieznana'}")
    L.append(f"- **Grupa testowa (pytanie 1):** {grupa}")
    L.append(f"- **Pobrano łącznie:** {itemow_razem} postów")
    L.append(f"- **Pole z czasem publikacji w itemie:** "
             f"{'`' + pole_czasu + '`' if pole_czasu else 'NIE ZNALEZIONO'}")
    L.append("")

    # --- 1 ---
    L.append("## Pytanie 1 — czy `onlyPostsNewerThan` działa?")
    L.append("")
    L.append("| okno | itemów | najstarszy post | najnowszy post | czas runu | koszt | błąd |")
    L.append("|---|---:|---:|---:|---:|---:|---|")
    for w in wyniki_okien:
        L.append(
            f"| `{w.okno}` | {w.ile} | {_lb(w.najstarszy_h, '{:.1f} h')} | "
            f"{_lb(w.najnowszy_h, '{:.1f} h')} | {_lb(w.trwanie_s, '{:.0f} s')} | "
            f"{_lb(w.koszt_usd, '{:.4f} USD')} | {w.blad or '—'} |")
    L.append("")
    L.append(f"### ROZSTRZYGNIĘCIE: **ŚCIEŻKA {werdykt_okna.sciezka}**")
    L.append("")
    L.append(werdykt_okna.powod)
    L.append("")
    if werdykt_okna.sciezka == "A":
        L.append("**Co z tego wynika dla prompta 2:** `_build_actor_input` ustawia")
        L.append("`onlyPostsNewerThan` na okno od ostatniego udanego przebiegu tej grupy.")
        L.append("Fetcher płaci wtedy za PRZYROST, nie za całą historię, i wolno mu")
        L.append("chodzić gęsto — bo gęściej znaczy tu mniejsze okno, nie większy rachunek.")
    elif werdykt_okna.sciezka == "B":
        L.append("**Co z tego wynika dla prompta 2:** okna czasowego NIE ma. Każdy")
        L.append("przebieg pobiera i opłaca te same posty od nowa, więc koszt rośnie")
        L.append("liniowo z częstotliwością. Deduplikacja po stronie bazy oszczędza")
        L.append("model i Telegram, ale **nie oszczędza Apify** — za pobranie już")
        L.append("zapłacono. Wnioski, które trzeba wyciągnąć zamiast okna:")
        L.append("`resultsLimit` musi być mały (kilka najnowszych postów), przebieg")
        L.append("rzadszy niż przy ścieżce A, a `POSTY_NA_DOBE` liczy się jako")
        L.append("`liczba_grup × resultsLimit × przebiegów_na_dobę` — bez żadnej ulgi.")
    else:
        L.append("**Co z tego wynika dla prompta 2:** NIC — pomiar trzeba powtórzyć.")
        L.append("Pisanie fetchera na tym wyniku to zgadywanie najdroższej zmiennej")
        L.append("w projekcie.")
    L.append("")

    # --- 2 ---
    L.append("## Pytanie 2 — `resultsLimit` przy wielu grupach")
    L.append("")
    if jedna is None and trzy is None:
        L.append("_Nie mierzone w tym przebiegu._")
    else:
        L.append("| grup w `startUrls` | `resultsLimit` | itemów | rozkład na grupy | czas | koszt |")
        L.append("|---:|---:|---:|---|---:|---:|")
        for w in (jedna, trzy):
            if w is None:
                continue
            rozklad = ", ".join(f"{k}: {v}" for k, v in w.na_grupe.items()) or "—"
            L.append(
                f"| {w.ile_grup_w_wejsciu} | {w.limit} | {w.ile} | {rozklad} | "
                f"{_lb(w.trwanie_s, '{:.0f} s')} | {_lb(w.koszt_usd, '{:.4f} USD')} |")
        L.append("")
        L.append(f"### ROZSTRZYGNIĘCIE: **LIMIT {werdykt_limitu.sciezka}**")
        L.append("")
        L.append(werdykt_limitu.powod)
        L.append("")
        if werdykt_limitu.sciezka == "GLOBALNY":
            L.append("**Co z tego wynika dla prompta 2:** jedna grupa = jedno wywołanie.")
            L.append("Batchowanie grup w `startUrls` jest zakazane — nie dlatego, że jest")
            L.append("wolniejsze, tylko dlatego, że gubi posty, za które płacimy.")
        elif werdykt_limitu.sciezka == "PER GRUPA":
            L.append("**Co z tego wynika dla prompta 2:** grupy wolno batchować w jednym")
            L.append("`startUrls`. Zanim to wejdzie do fetchera, sprawdź jeszcze, czy")
            L.append("Apify liczy batch jako jeden run czy jako N — to jest w kolumnie")
            L.append("„koszt” powyżej.")
    L.append("")

    # --- 3 ---
    L.append("## Pytanie 3 — ile kosztuje jeden post")
    L.append("")
    L.append("| źródło liczby | koszt serii | postów | USD za post |")
    L.append("|---|---:|---:|---:|")
    L.append(f"| licznik konta (`apify_credits`) | {_lb(z_salda, '{:.4f} USD')} | "
             f"{itemow_razem} | {_lb(cena_z_salda, '{:.5f}')} |")
    L.append(f"| suma `usageTotalUsd` runów | {_lb(koszt_z_runow, '{:.4f} USD')} | "
             f"{itemow_razem} | {_lb(cena_z_runow, '{:.5f}')} |")
    L.append(f"| cena katalogowa ze strony actora | — | — | "
             f"{CENA_KATALOGOWA_USD_ZA_POST:.5f} |")
    L.append("")
    if cena is None:
        L.append("**Nie udało się policzyć ceny.** Ani licznik konta, ani obiekty runów")
        L.append("nie dały kosztu — bez tej liczby nie da się odpowiedzieć na pytanie")
        L.append("„ile kont Apify”. Sprawdź `python -m laweta_radar.workers.apify_credits`.")
    else:
        # Spacja jako separator tysięcy — zapis polski. `.replace` puszczamy na
        # SAMEJ liczbie, nie na całym zdaniu: zdanie ma własne przecinki.
        na_konto = f"{5.0 / cena:,.0f}".replace(",", " ") if cena > 0 else "?"
        L.append(f"**ZMIERZONA CENA: {cena:.5f} USD za post.**")
        L.append("")
        L.append(f"Darmowe konto Apify to ~5 USD miesięcznie, czyli "
                 f"**~{na_konto} postów na konto i miesiąc**.")
        L.append("")
        L.append("Stąd liczy się liczbę kont: `POSTY_NA_DOBE × 30 / postów_na_konto`.")
        L.append("`POSTY_NA_DOBE` bierze się z pytania 1 — przy ścieżce A jest to sam")
        L.append("przyrost, przy ścieżce B pełne `grupy × resultsLimit × przebiegi`.")
        if cena_z_salda is not None and cena_z_runow is not None:
            rozjazd = abs(cena_z_salda - cena_z_runow)
            if rozjazd > cena_z_runow * 0.25:
                L.append("")
                L.append("> **Uwaga: dwie metody się rozjeżdżają.** Licznik konta agreguje")
                L.append("> z opóźnieniem, więc tuż po serii bywa zaniżony. Do decyzji")
                L.append("> o liczbie kont bierz WYŻSZĄ z dwóch liczb — pomyłka w tę stronę")
                L.append("> kosztuje jedno konto za dużo, w drugą: martwy pipeline w środku")
                L.append("> miesiąca.")
    L.append("")

    if saldo_po is not None:
        L.append(f"Stan konta po pomiarze: {saldo_po.opis()}.")
        L.append("")

    L.append("---")
    L.append("")
    L.append("Powtórzenie pomiaru (np. po zmianie wersji actora):")
    L.append("")
    L.append("```bash")
    L.append("export PYTHONPATH=$PWD")
    L.append(f"python -m laweta_radar.scripts.pomiar_actora --grupa {grupa}")
    L.append("```")
    return "\n".join(L) + "\n"


# ===========================================================================
# CZĘŚĆ SIECIOWA
# ===========================================================================
def _wejscie(urle: list[str], limit: int, okno: str | None) -> dict:
    """Wejście actora. JEDNO miejsce, w którym powstaje — pomiar zmienia w nim
    dokładnie jedno pole naraz, bo inaczej nie wiadomo, co spowodowało różnicę."""
    wejscie: dict[str, object] = {
        "startUrls": [{"url": u} for u in urle],
        "resultsLimit": limit,
        # Świeżość, nie popularność — ta sama wartość, której użyje fetcher.
        "viewOption": cfg_groups.APIFY_SORT,
    }
    if okno:
        wejscie["onlyPostsNewerThan"] = okno
    return wejscie


def _sprawdz_schemat(token: str, log=print) -> None:
    """Ostrzeż, jeśli actor nie deklaruje pól, które zamierzamy wysłać.

    Za darmo (schemat nie jest runem) i PRZED pytaniem o potwierdzenie. Cała wartość
    pomiaru zależy od tego, że `onlyPostsNewerThan` naprawdę tak się nazywa: pod
    zmienioną nazwą actor nie zwróci błędu — zwróci komplet postów bez filtra, za
    pełną cenę, a pomiar wypisze wtedy ŚCIEŻKĘ B na podstawie naszej literówki.

    Nie zatrzymuje: actor bez opublikowanego schematu nie pozwala niczego
    stwierdzić. Ma ostrzec człowieka, zanim wpisze TAK.
    """
    try:
        schemat = apify_run.schemat_wejscia(token, ACTOR)
    except Exception as e:  # noqa: BLE001 — to jest podpowiedź, nie bramka
        log(f"[{KTO}]   (nie sprawdziłem schematu actora: {type(e).__name__})")
        return
    brakujace = apify_run.nieznane_pola(_wejscie(["https://x"], 1, OKNA[0]), schemat)
    if brakujace:
        log(f"[{KTO}]   UWAGA: actor NIE deklaruje pól {brakujace} — pod zmienioną "
            f"nazwą pole jest po cichu ignorowane i pomiar zmierzy naszą literówkę, "
            f"nie actora. Sprawdź stronę actora przed potwierdzeniem.")
    else:
        log(f"[{KTO}]   schemat actora: wszystkie wysyłane pola rozpoznane")


class _Wolacz:
    """Owija `KeyRotator` + `apify_run` i pamięta, które klucze zostały użyte.

    Pamiętanie kluczy nie jest ozdobą: koszt liczony z licznika KONTA ma sens
    tylko wtedy, gdy cała seria poszła z jednego konta. Gdy rotacja przeskoczyła
    w trakcie, różnica salda przestaje być kosztem serii i raport musi to przyznać.
    """

    def __init__(self, rotator: KeyRotator, log=print):
        self._rotator = rotator
        self._log = log
        self.uzyte: list[str] = []
        self.runy: list[apify_run.Run] = []

    def __call__(self, wejscie: dict) -> apify_run.Run:
        def fn(token: str) -> apify_run.Run:
            if token not in self.uzyte:
                self.uzyte.append(token)
            return apify_run.uruchom(
                token, ACTOR, wejscie,
                timeout_s=TIMEOUT_RUNU_S,
                max_czekania_s=TIMEOUT_RUNU_S * 1.5,
                log=self._log,
            )

        run = self._rotator.call(fn)
        self.runy.append(run)
        return run

    @property
    def koszt_usd(self) -> float | None:
        znane = [r.koszt_usd for r in self.runy if r.koszt_usd is not None]
        return sum(znane) if znane else None

    @property
    def itemow(self) -> int:
        return sum(r.ile_itemow for r in self.runy)


def _pytanie_1(wolaj: _Wolacz, grupa: str, log) -> tuple[list[WynikOkna], str | None]:
    """Seria po oknach. Zwraca (wyniki, pole z czasem posta)."""
    wyniki: list[WynikOkna] = []
    pole_czasu: str | None = None
    for i, okno in enumerate(OKNA):
        if i:
            time.sleep(ODSTEP_S)      # bez zrównoleglania — patrz nagłówek modułu
        log(f"\n[{KTO}] pytanie 1 — okno „{okno}” (resultsLimit={LIMIT_P1})")
        w = WynikOkna(okno=okno, godziny=okno_na_godziny(okno))
        try:
            run = wolaj(_wejscie([grupa], LIMIT_P1, okno))
        except AllKeysExhausted as e:
            w.blad = str(e)[:120]
            wyniki.append(w)
            break                     # dalsza seria i tak nie ruszy
        except Exception as e:  # noqa: BLE001 — patrz niżej: błąd JEST wynikiem
            # Odrzucone wejście (HTTP 400 „Input is not valid") to nie awaria
            # skryptu — to jeden z DWÓCH możliwych wyników pytania 1. Gdyby leciało
            # wyżej, seria kończyłaby się w połowie, raport nigdy by nie powstał,
            # a runy, za które już zapłaciliśmy, przepadłyby razem z nim. Dlatego
            # zapisujemy błąd jako wynik dla tego okna i lecimy dalej: dopiero
            # KOMPLET okien pokazuje, czy actor odrzuca same wąskie, czy wszystkie.
            w.blad = f"{type(e).__name__}: {str(e)[:100]}"
            wyniki.append(w)
            log(f"[{KTO}] okno „{okno}”: {w.blad}")
            continue
        w.ile = run.ile_itemow
        w.trwanie_s = run.trwanie_s
        w.koszt_usd = run.koszt_usd
        w.blad = run.blad
        w.najstarszy_h, w.najnowszy_h, pole, _ = wieki_postow(run.itemy)
        pole_czasu = pole_czasu or pole
        wyniki.append(w)

        if i == 0 and run.ile_itemow == 0 and run.blad is None:
            # NAJSZERSZE okno bez ani jednego posta. Grupa jest prywatna, martwa albo
            # adres jest zły — w każdym z tych przypadków dalsza seria mierzyłaby
            # tę awarię, a nie actora. Za to i tak byśmy zapłacili.
            log(f"[{KTO}] PRZERYWAM: najszersze okno („{okno}”) zwróciło ZERO postów. "
                f"Grupa nie jest publiczna, jest martwa albo adres jest zły — "
                f"dalsza seria mierzyłaby ten błąd, nie zachowanie actora.")
            break
    return wyniki, pole_czasu


def _pytanie_2(wolaj: _Wolacz, grupy: list[str], log
               ) -> tuple[WynikLimitu | None, WynikLimitu | None]:
    """To samo `resultsLimit` raz dla jednej grupy, raz dla trzech."""
    def jedno(urle: list[str]) -> WynikLimitu:
        w = WynikLimitu(ile_grup_w_wejsciu=len(urle), limit=LIMIT_P2)
        try:
            # Bez `onlyPostsNewerThan` — w tym pytaniu jedyną zmienną ma być liczba
            # grup. Okno w tle przycinałoby wyniki nierówno i nic by z tego nie wyszło.
            run = wolaj(_wejscie(urle, LIMIT_P2, None))
        except AllKeysExhausted as e:
            w.blad = str(e)[:120]
            return w
        except Exception as e:  # noqa: BLE001 — raport ma powstać także po błędzie
            w.blad = f"{type(e).__name__}: {str(e)[:100]}"
            log(f"[{KTO}] {len(urle)} grup: {w.blad}")
            return w
        w.ile = run.ile_itemow
        w.trwanie_s = run.trwanie_s
        w.koszt_usd = run.koszt_usd
        w.blad = run.blad
        for item in run.itemy:
            k = klucz_grupy(item)
            w.na_grupe[k] = w.na_grupe.get(k, 0) + 1
        return w

    log(f"\n[{KTO}] pytanie 2 — JEDNA grupa, resultsLimit={LIMIT_P2}")
    jedna = jedno(grupy[:1])
    time.sleep(ODSTEP_S)
    log(f"\n[{KTO}] pytanie 2 — {GRUP_W_BATCHU} grupy, resultsLimit={LIMIT_P2} "
        f"(ten sam limit!)")
    trzy = jedno(grupy[:GRUP_W_BATCHU])
    return jedna, trzy


# ===========================================================================
# CLI
# ===========================================================================
def _parsuj(argv: list[str]) -> dict:
    opcje = {"grupy": [], "tylko": {1, 2, 3}, "klucz": 1, "tak": False,
             "sucho": False, "sufit": SUFIT_ITEMOW, "wyjscie": RAPORT}
    i = 1
    while i < len(argv):
        a = argv[i]
        nast = argv[i + 1] if i + 1 < len(argv) else ""
        if a == "--grupa" and nast:
            opcje["grupy"].append(nast.strip())
            i += 2
        elif a == "--tylko" and nast:
            opcje["tylko"] = {int(x) for x in nast.replace(",", " ").split() if x.isdigit()}
            i += 2
        elif a == "--klucz" and nast.isdigit():
            opcje["klucz"] = int(nast)
            i += 2
        elif a == "--sufit" and nast.isdigit():
            opcje["sufit"] = int(nast)
            i += 2
        elif a == "--wyjscie" and nast:
            opcje["wyjscie"] = Path(nast)
            i += 2
        elif a in ("--tak", "--yes"):
            opcje["tak"] = True
            i += 1
        elif a in ("--sucho", "--dry-run"):
            opcje["sucho"] = True
            i += 1
        elif a in ("-h", "--help"):
            opcje["pomoc"] = True
            i += 1
        else:
            opcje.setdefault("nieznane", []).append(a)
            i += 1
    return opcje


def _main(argv: list[str]) -> int:
    opcje = _parsuj(argv)
    if opcje.get("pomoc"):
        print(__doc__)
        return 0
    if opcje.get("nieznane"):
        print(f"[{KTO}] Nieznane argumenty: {' '.join(opcje['nieznane'])}. "
              f"Pomoc: --help", file=sys.stderr)
        return 0

    grupy = opcje["grupy"]
    if not grupy:
        print(f"[{KTO}] Brak grupy testowej — kończę bez działania.\n"
              f"[{KTO}]   --grupa https://www.facebook.com/groups/...\n"
              f"[{KTO}] Grupa MUSI być publiczna i sprawdzona ręcznie. Na prywatnej "
              f"albo martwej zmierzysz błąd, nie zachowanie actora — i zapłacisz "
              f"za to tyle samo.", file=sys.stderr)
        return 0

    tylko = opcje["tylko"]
    if 2 in tylko and len(grupy) < GRUP_W_BATCHU:
        print(f"[{KTO}] Pytanie 2 wymaga {GRUP_W_BATCHU} grup (--grupa × "
              f"{GRUP_W_BATCHU}), a podano {len(grupy)} — pomijam je.")
        tylko = tylko - {2}

    tokeny = load_apify_tokens()
    if not tokeny:
        print(f"[{KTO}] Brak kluczy Apify (APIFY_API_TOKEN*) — kończę bez działania.\n"
              f"[{KTO}] Klucze przychodzą ze WSPÓLNEGO .env: "
              f"python -m laweta_radar.config.settings", file=sys.stderr)
        return 0
    if not 1 <= opcje["klucz"] <= len(tokeny):
        print(f"[{KTO}] Nie ma klucza #{opcje['klucz']} — widzę {len(tokeny)}.",
              file=sys.stderr)
        return 0

    ok, linie = apify_proxy.preflight(tokens=tokeny)
    for linia in linie:
        print(linia)
    if not ok:
        return 0

    # --- Koszt PRZED serią -------------------------------------------------
    plan: list[str] = []
    itemow_max = 0
    if 1 in tylko:
        plan.append(f"pytanie 1: {len(OKNA)} wywołań × resultsLimit {LIMIT_P1}")
        itemow_max += len(OKNA) * LIMIT_P1
    if 2 in tylko:
        plan.append(f"pytanie 2: 2 wywołania (1 grupa + {GRUP_W_BATCHU} grupy) "
                    f"× resultsLimit {LIMIT_P2}")
        itemow_max += LIMIT_P2 * (1 + GRUP_W_BATCHU)
    if not plan:
        print(f"[{KTO}] Nic do zrobienia (--tylko {sorted(tylko)}).")
        return 0

    szacunek = itemow_max * CENA_KATALOGOWA_USD_ZA_POST
    print(f"\n[{KTO}] PLAN POMIARU")
    for p in plan:
        print(f"[{KTO}]   {p}")
    print(f"[{KTO}]   grupa testowa: {grupy[0]}")
    print(f"[{KTO}]   klucz Apify:   #{opcje['klucz']}")
    print(f"[{KTO}]   NAJWYŻEJ {itemow_max} postów ≈ {szacunek:.2f} USD "
          f"(po cenie katalogowej {CENA_KATALOGOWA_USD_ZA_POST} USD/post)")
    print(f"[{KTO}]   to ≈ {szacunek / 5.0 * 100:.0f}% miesięcznego kredytu "
          f"jednego darmowego konta")
    _sprawdz_schemat(tokeny[opcje["klucz"] - 1])

    if itemow_max > opcje["sufit"]:
        print(f"[{KTO}] ODMAWIAM: plan to {itemow_max} postów, sufit pomiaru "
              f"{opcje['sufit']}. Zawęź (--tylko) albo podnieś świadomie (--sufit).",
              file=sys.stderr)
        return 0
    if opcje["sucho"]:
        print(f"[{KTO}] --sucho: nic nie odpalam.")
        return 0
    if not opcje["tak"]:
        try:
            odp = input(f"[{KTO}] Odpalić? wpisz TAK: ").strip()
        except (EOFError, KeyboardInterrupt):
            odp = ""
        if odp != "TAK":
            print(f"[{KTO}] Przerwane — nic nie wydano.")
            return 0

    # --- Seria -------------------------------------------------------------
    token_startowy = tokeny[opcje["klucz"] - 1]
    # `state_path=None` ŚWIADOMIE: to jest pomiar przypięty do wybranego klucza,
    # a zapisanie jego indeksu jako „ostatnio działającego" cofnęłoby produkcyjnego
    # fetchera do klucza, o którym może już wiedzieć, że jest pusty.
    rotator = KeyRotator(
        tokeny,
        state_path=None,
        start_index=opcje["klucz"] - 1,
        transient_key_switches=2 if apify_proxy.is_enabled() else 0,
    )
    wolaj = _Wolacz(rotator)

    saldo_przed = None
    try:
        saldo_przed = apify_credits.zuzycie(token_startowy)
    except Exception as e:  # noqa: BLE001 — brak salda nie może zatrzymać pomiaru
        print(f"[{KTO}] Nie odczytałem salda PRZED serią ({type(e).__name__}) — "
              f"koszt policzę z usageTotalUsd runów.", file=sys.stderr)
    if saldo_przed:
        print(f"[{KTO}] saldo przed: {saldo_przed.opis()}")

    wyniki_okien: list[WynikOkna] = []
    pole_czasu = None
    jedna = trzy = None
    if 1 in tylko:
        wyniki_okien, pole_czasu = _pytanie_1(wolaj, grupy[0], print)
    if 2 in tylko:
        # Gdy pytanie 1 przerwało się na pustej grupie, drugie pytanie mierzyłoby
        # to samo nieporozumienie — tyle że drożej.
        if 1 in tylko and wyniki_okien and wyniki_okien[0].ile == 0:
            print(f"[{KTO}] Pomijam pytanie 2 — grupa testowa nie zwróciła postów.")
        else:
            time.sleep(ODSTEP_S)
            jedna, trzy = _pytanie_2(wolaj, grupy, print)

    saldo_po = None
    if saldo_przed is not None:
        if len(wolaj.uzyte) == 1 and wolaj.uzyte[0] == token_startowy:
            try:
                saldo_po = apify_credits.zuzycie(token_startowy)
            except Exception as e:  # noqa: BLE001
                print(f"[{KTO}] Nie odczytałem salda PO serii ({type(e).__name__}).",
                      file=sys.stderr)
        else:
            print(f"[{KTO}] Rotacja przeskoczyła na inny klucz w trakcie serii "
                  f"({len(wolaj.uzyte)} kluczy) — różnica salda JEDNEGO konta nie "
                  f"jest już kosztem całej serii. Liczę z usageTotalUsd runów.")

    # --- Raport ------------------------------------------------------------
    werdykt_okna = rozstrzygnij_okno(wyniki_okien)
    werdykt_limitu = rozstrzygnij_limit(jedna, trzy)
    build = next((str(r.surowy.get("buildNumber") or "") for r in wolaj.runy
                  if r.surowy.get("buildNumber")), "")

    tresc = raport_md(
        wyniki_okien=wyniki_okien, werdykt_okna=werdykt_okna,
        jedna=jedna, trzy=trzy, werdykt_limitu=werdykt_limitu,
        saldo_przed=saldo_przed, saldo_po=saldo_po,
        koszt_z_runow=wolaj.koszt_usd, itemow_razem=wolaj.itemow,
        pole_czasu=pole_czasu, build=build, grupa=grupy[0],
        data_pomiaru=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    sciezka = Path(opcje["wyjscie"])
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    sciezka.write_text(tresc, encoding="utf-8")

    print(f"\n[{KTO}] ===== WYNIK =====")
    print(f"[{KTO}] Pytanie 1: ŚCIEŻKA {werdykt_okna.sciezka} — {werdykt_okna.powod}")
    if jedna or trzy:
        print(f"[{KTO}] Pytanie 2: LIMIT {werdykt_limitu.sciezka} — "
              f"{werdykt_limitu.powod}")
    cena = koszt_na_post(wolaj.koszt_usd, wolaj.itemow)
    print(f"[{KTO}] Pytanie 3: {wolaj.itemow} postów za "
          f"{_lb(wolaj.koszt_usd, '{:.4f} USD')} = {_lb(cena, '{:.5f} USD')}/post")
    print(f"[{KTO}] Raport: {sciezka}")
    if not werdykt_okna.jednoznaczne:
        print(f"[{KTO}] UWAGA: pytanie 1 NIEROZSTRZYGNIĘTE — powtórz pomiar na "
              f"ruchliwszej grupie ZANIM napiszesz fetchera.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
