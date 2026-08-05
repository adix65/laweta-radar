"""Budowa listy kandydackich grup FB przez wyszukiwarkę — zamiast wpisywania z pamięci.

NIE jest częścią pipeline'u. Odpalasz RAZ na początku, potem raz w miesiącu.
Wynikiem jest `data/kandydaci_grupy.csv` — lista do ręcznego przejrzenia, a nie
działający fetcher.

DLACZEGO WYSZUKIWARKA, A NIE PAMIĘĆ: lista wpisana z głowy jest listą grup, które
akurat się pamięta. Grupy regionalne — te z najlepszym stosunkiem zleceń do szumu,
bo dojazd jest krótki — zwykle nie są tymi, które się pamięta.

KROK RĘCZNY, KTÓREGO NIE DA SIĘ POMINĄĆ
    Kolumna `publiczna` zostaje PUSTA. Apify czyta wyłącznie grupy publiczne,
    a wyszukiwarka nie jest wiarygodnym źródłem tej informacji. Człowiek otwiera
    każdy URL z CSV i wpisuje TAK albo NIE. Dopiero wtedy:

        python -m laweta_radar.scripts.znajdz_grupy --raport

    wypisze gotowy blok do wklejenia w `config/groups.py` — WYŁĄCZNIE z grup
    oznaczonych TAK. Bez tego kroku fetcher płaciłby za runy do grup, których
    i tak nie przeczyta.

    Gdy actor sam poda, czy grupa jest publiczna, trafia to do kolumny `notatka`
    jako podpowiedź („wyszukiwarka: prywatna”) — żeby dało się pominąć oczywiste
    przypadki. Podpowiedź NIE wypełnia kolumny `publiczna`: to nadal decyzja
    człowieka, bo wyszukiwarka pokazuje stan sprzed nieznanego czasu.

PONOWNE URUCHOMIENIE NIE KASUJE PRACY RĘCZNEJ. Skrypt SCALA wynik z istniejącym
CSV po URL-u: `publiczna`, `status` i dopisana ręcznie `notatka` zostają nietknięte,
dochodzą tylko nowe grupy. Gdyby nadpisywał plik, comiesięczne odświeżenie kasowałoby
godziny klikania po Facebooku — i po drugim razie nikt by go już nie odpalił.

WZORZEC: `workers/apify_fb_search_fetcher.py` z repo źródłowego (obsługa fraz,
rotacja kluczy, dedup, bezpieczniki). Różnica: tamten szuka POSTÓW, ten szuka GRUP.

UŻYCIE:
    export PYTHONPATH=$PWD
    python -m laweta_radar.scripts.znajdz_grupy --schema     # NAJPIERW: pola actora
    python -m laweta_radar.scripts.znajdz_grupy --sucho      # plan i koszt, bez wydawania
    python -m laweta_radar.scripts.znajdz_grupy              # seria (pyta o potwierdzenie)
    python -m laweta_radar.scripts.znajdz_grupy --raport     # po uzupełnieniu `publiczna`

    --jezyk pl,de        # tylko wybrane bloki językowe
    --min-czlonkow 300   # niższy próg (domyślnie 500)
    --klucz 3            # konkretny klucz Apify
"""
from __future__ import annotations

import csv
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:                               # pakiet widoczny: -m, import pakietowy, testy
    from laweta_radar.scripts._sciezka import dodaj_repo_do_sciezki
except ImportError:                # uruchomienie po ścieżce do pliku
    from _sciezka import dodaj_repo_do_sciezki

dodaj_repo_do_sciezki()

from laweta_radar.config import frazy_grup as cfg  # noqa: E402
from laweta_radar.workers import apify_proxy, apify_run  # noqa: E402
from laweta_radar.workers.apify_keys import (  # noqa: E402
    AllKeysExhausted,
    KeyRotator,
    load_apify_tokens,
)

KTO = "znajdz-grupy"
ROOT = Path(__file__).resolve().parent.parent.parent
CSV_PATH = ROOT / "data" / "kandydaci_grupy.csv"

KOLUMNY = ("url", "nazwa", "czlonkowie", "jezyk", "fraza_zrodlowa",
           "publiczna", "status", "notatka")

# Kolumny, które WYPEŁNIA CZŁOWIEK. Przy scalaniu nowy wynik ich nie dotyka.
KOLUMNY_RECZNE = ("publiczna", "status", "notatka")

ODSTEP_S = 5.0          # przerwa między frazami; bez zrównoleglania
TIMEOUT_RUNU_S = 300

# Gdzie item niesie poszczególne dane. Actory ze Store zmieniają nazwy pól między
# wersjami — kolejność w krotce jest kolejnością prób, pierwsze trafienie wygrywa.
POLA_URL = ("url", "groupUrl", "link", "facebookUrl", "groupLink")
POLA_NAZWY = ("name", "title", "groupName", "groupTitle", "text")
POLA_CZLONKOW = ("membersCount", "members", "memberCount", "membersCountText",
                 "groupMembers", "stats", "subtitle")
POLA_PRYWATNOSCI = ("privacy", "isPublic", "groupPrivacy", "type", "visibility")

# Skróty tysięcy/milionów w liczbie członków — tak jak je pisze FB w każdym
# z czterech przeszukiwanych języków. Dopasowanie jest DOKŁADNE, nie po prefiksie:
# przy prefiksie „1200 members" byłoby 1,2 miliarda członków, bo „members" zaczyna
# się od „m".
MNOZNIKI = {
    "k": 1_000, "tys": 1_000, "tysiąc": 1_000, "tysiące": 1_000, "tysięcy": 1_000,
    "tyś": 1_000, "tis": 1_000, "tisíc": 1_000, "tisic": 1_000, "tsd": 1_000,
    "tausend": 1_000,
    "m": 1_000_000, "mln": 1_000_000, "mil": 1_000_000, "milion": 1_000_000,
    "miliony": 1_000_000, "milionów": 1_000_000, "mio": 1_000_000,
}

# Słowo, po którym poznajemy, że liczba obok JEST liczbą członków. FB wsadza do
# tego samego podpisu także inne liczby („3 posty dziennie", rok założenia), więc
# bez tej kotwicy trafiałaby pierwsza lepsza.
SLOWA_CZLONKOW = ("człon", "czlon", "member", "mitglied", "člen", "clen", "osób")

_LICZBA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([a-ząćęłńóśźżáäéíóöúüýčďěňřšťůž]*)")


# ===========================================================================
# CZYSTE FUNKCJE — bez sieci, testowalne (tests/test_znajdz_grupy.py)
# ===========================================================================
@dataclass
class Kandydat:
    url: str
    nazwa: str = ""
    czlonkowie: int = 0
    jezyk: str = ""
    fraza_zrodlowa: str = ""
    publiczna: str = ""          # PUSTE — wypełnia człowiek
    status: str = "kandydat"     # "kandydat" | "ok" | "odrzucona"
    notatka: str = ""

    def wiersz(self) -> dict[str, str]:
        return {
            "url": self.url, "nazwa": self.nazwa,
            "czlonkowie": str(self.czlonkowie), "jezyk": self.jezyk,
            "fraza_zrodlowa": self.fraza_zrodlowa, "publiczna": self.publiczna,
            "status": self.status, "notatka": self.notatka,
        }


def parsuj_czlonkow(wartosc) -> int:
    """„12,5 tys. członków” / „1.2K members” / 3400 -> liczba. Nie da się -> 0.

    FB podaje tę liczbę raz jako int, raz jako tekst ze skrótem — i to w języku
    wyszukiwania. Zero znaczy „nie wiem”, więc próg `MIN_CZLONKOW` takich grup nie
    odrzuca: odrzucenie grupy za nieodczytany format byłoby karą za nasz błąd,
    a nie za jej rozmiar.

    Gdy w tekście jest kilka liczb („Grupa publiczna · 3 posty dziennie ·
    12 tys. członków"), bierzemy tę, przy której stoi skrót tysięcy albo słowo
    „członkowie". Dopiero gdy żadna nie ma takiej kotwicy — największą, bo liczba
    członków jest w takim podpisie praktycznie zawsze największa.
    """
    if isinstance(wartosc, bool):
        return 0
    if isinstance(wartosc, (int, float)):
        return max(0, int(wartosc))
    tekst = str(wartosc or "").strip().lower()
    if not tekst:
        return 0
    # Przecinek dziesiętny („12,5 tys.”) vs separator tysięcy („12,500 members”):
    # przecinek/kropka przed DOKŁADNIE trzema cyframi to separator — usuwamy.
    tekst = re.sub(r"(?<=\d)[.,](?=\d{3}\b)", "", tekst)

    zakotwiczone: list[int] = []
    wszystkie: list[int] = []
    for m in _LICZBA_RE.finditer(tekst):
        try:
            liczba = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        jednostka = m.group(2)
        mnoznik = MNOZNIKI.get(jednostka)
        wartosc_i = int(liczba * (mnoznik or 1))
        wszystkie.append(wartosc_i)
        ogon = tekst[m.end():m.end() + 16]
        if mnoznik is not None or any(s in jednostka or s in ogon
                                      for s in SLOWA_CZLONKOW):
            zakotwiczone.append(wartosc_i)
    if zakotwiczone:
        return zakotwiczone[0]
    return max(wszystkie) if wszystkie else 0


def normalizuj_url(url: str) -> str:
    """Adres grupy sprowadzony do postaci porównywalnej — do dedupu.

    Ta sama grupa wraca z różnych fraz raz jako `/groups/123`, raz
    `m.facebook.com/groups/123/?ref=share`. Bez normalizacji dedup po URL-u nie
    działa i CSV puchnie o duplikaty, których człowiek nie ma jak odsiać.
    """
    u = (url or "").strip()
    if not u:
        return ""
    u = re.sub(r"[?#].*$", "", u)                       # parametry i kotwice
    u = re.sub(r"^https?://", "", u, flags=re.I)
    u = re.sub(r"^(www|m|web|mbasic)\.", "", u, flags=re.I)
    u = u.rstrip("/")
    m = re.search(r"facebook\.com/groups/([^/]+)", u, flags=re.I)
    return f"https://www.facebook.com/groups/{m.group(1)}" if m else "https://" + u


def wyciagnij(item: dict, pola: tuple[str, ...]):
    """Pierwsza niepusta wartość z podanych pól. Wchodzi też w zagnieżdżone dicty."""
    for pole in pola:
        wartosc = item.get(pole)
        if isinstance(wartosc, str) and wartosc.strip():
            return wartosc.strip()
        if isinstance(wartosc, (int, float)) and not isinstance(wartosc, bool):
            return wartosc
        if isinstance(wartosc, bool):
            return wartosc
        if isinstance(wartosc, dict):
            for podpole in ("text", "value", "count", "url", "name"):
                v = wartosc.get(podpole)
                if v not in (None, "", {}, []):
                    return v
    return None


def podpowiedz_prywatnosci(item: dict) -> str:
    """„publiczna” / „prywatna” / „” — CO MÓWI WYSZUKIWARKA, nie co jest prawdą.

    Ląduje w notatce, nigdy w kolumnie `publiczna`. Wyszukiwarka pokazuje stan
    sprzed nieznanego czasu, a pomyłka w tę stronę kosztuje runy do grupy, której
    actor i tak nie przeczyta.
    """
    wartosc = wyciagnij(item, POLA_PRYWATNOSCI)
    if wartosc is None:
        return ""
    if isinstance(wartosc, bool):
        return "publiczna" if wartosc else "prywatna"
    tekst = str(wartosc).strip().lower()
    if any(w in tekst for w in ("public", "publiczn", "öffentlich", "veřejn", "verejn")):
        return "publiczna"
    if any(w in tekst for w in ("private", "closed", "prywatn", "privat", "soukrom",
                                "súkrom")):
        return "prywatna"
    return ""


def notatki(nazwa: str, fraza: str, prywatnosc: str) -> str:
    """Ostrzeżenia dla człowieka, który będzie tę listę przeglądał."""
    uwagi: list[str] = []
    niska = (nazwa or "").lower()
    trafione = [s for s in cfg.SLOWA_SPRZEDAZOWE if s in niska]
    if trafione:
        uwagi.append(f"nazwa sugeruje SPRZEDAŻ ({', '.join(trafione[:3])}) — "
                     f"to może nie być giełda zleceń")
    if fraza in cfg.FRAZY_NIEPEWNE:
        uwagi.append(f"fraza „{fraza}” bywa nie na temat — przejrzyj ręcznie")
    if prywatnosc:
        uwagi.append(f"wyszukiwarka: {prywatnosc}")
    return "; ".join(uwagi)


def na_kandydata(item: dict, jezyk: str, fraza: str) -> Kandydat | None:
    """Item z actora -> `Kandydat`. None, gdy nie ma nawet adresu grupy."""
    url = normalizuj_url(str(wyciagnij(item, POLA_URL) or ""))
    if not url or "/groups/" not in url:
        return None
    nazwa = str(wyciagnij(item, POLA_NAZWY) or "").strip()
    return Kandydat(
        url=url,
        nazwa=nazwa,
        czlonkowie=parsuj_czlonkow(wyciagnij(item, POLA_CZLONKOW)),
        jezyk=jezyk,
        fraza_zrodlowa=fraza,
        notatka=notatki(nazwa, fraza, podpowiedz_prywatnosci(item)),
    )


def odsiej(kandydaci: list[Kandydat], min_czlonkow: int
           ) -> tuple[list[Kandydat], dict[str, int]]:
    """Dedup po URL, próg członków, sortowanie malejąco. -> (lista, statystyki).

    Statystyki wracają razem z listą, bo „odrzucono 40 grup” bez powodu wygląda
    jak awaria; z powodem jest informacją, czy próg nie jest ustawiony za wysoko.
    """
    widziane: dict[str, Kandydat] = {}
    stat = {"wszystkie": len(kandydaci), "duplikaty": 0, "za_male": 0}
    for k in kandydaci:
        if k.url in widziane:
            stat["duplikaty"] += 1
            # Ta sama grupa z drugiej frazy: dopisujemy frazę, bo to informacja
            # o tym, jak szeroko grupa łapie — a nie powtórzenie do wyrzucenia.
            stara = widziane[k.url]
            if k.fraza_zrodlowa and k.fraza_zrodlowa not in stara.fraza_zrodlowa:
                stara.fraza_zrodlowa = f"{stara.fraza_zrodlowa}, {k.fraza_zrodlowa}"
            stara.czlonkowie = max(stara.czlonkowie, k.czlonkowie)
            continue
        # 0 = „nie odczytaliśmy”, nie „grupa jest pusta” — patrz `parsuj_czlonkow`.
        if 0 < k.czlonkowie < min_czlonkow:
            stat["za_male"] += 1
            continue
        widziane[k.url] = k
    lista = sorted(widziane.values(), key=lambda k: (-k.czlonkowie, k.nazwa.lower()))
    stat["zostalo"] = len(lista)
    return lista, stat


def scal(nowe: list[Kandydat], istniejace: list[dict]) -> list[dict]:
    """Nowe wyniki + to, co człowiek już wypełnił. Praca ręczna ma pierwszeństwo.

    Reguła jest jedna: kolumny z `KOLUMNY_RECZNE` NIGDY nie są nadpisywane, gdy
    mają już wartość. Reszta (nazwa, liczba członków, frazy) aktualizuje się —
    grupa rośnie i bywa przemianowana, więc świeższa wartość jest lepsza.
    """
    po_url = {(w.get("url") or "").strip(): dict(w) for w in istniejace}
    for k in nowe:
        stary = po_url.get(k.url)
        if stary is None:
            po_url[k.url] = k.wiersz()
            continue
        swiezy = k.wiersz()
        for kolumna, wartosc in swiezy.items():
            if kolumna in KOLUMNY_RECZNE and (stary.get(kolumna) or "").strip():
                continue          # człowiek już to wypełnił — nie ruszamy
            if kolumna == "fraza_zrodlowa":
                frazy = [f.strip() for f in
                         f"{stary.get(kolumna, '')}, {wartosc}".split(",") if f.strip()]
                stary[kolumna] = ", ".join(dict.fromkeys(frazy))
                continue
            if wartosc:
                stary[kolumna] = wartosc
    return sorted(po_url.values(),
                  key=lambda w: (-parsuj_czlonkow(w.get("czlonkowie")),
                                 (w.get("nazwa") or "").lower()))


def czytaj_csv(sciezka: Path) -> list[dict]:
    """Istniejący CSV albo pusta lista. Brak pliku to normalny pierwszy przebieg."""
    if not sciezka.is_file():
        return []
    with sciezka.open(encoding="utf-8", newline="") as f:
        return [w for w in csv.DictReader(f) if (w.get("url") or "").strip()]


def zapisz_csv(sciezka: Path, wiersze: list[dict]) -> None:
    """Zapis ATOMOWY: najpierw plik obok, potem podmiana nazwy.

    Zwykłe otwarcie „w" ucina plik na starcie, więc przerwanie zapisu (Ctrl+C,
    padnięty dysk) zostawiłoby CSV obcięty w połowie. To akurat ten plik, w którym
    siedzi kolumna `publiczna` — godziny klikania po Facebooku, jedyna rzecz tutaj,
    której nie da się odtworzyć ponownym uruchomieniem czegokolwiek.
    """
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    tymczasowy = sciezka.with_suffix(sciezka.suffix + ".tmp")
    with tymczasowy.open("w", encoding="utf-8", newline="") as f:
        pisarz = csv.DictWriter(f, fieldnames=list(KOLUMNY), extrasaction="ignore")
        pisarz.writeheader()
        for w in wiersze:
            pisarz.writerow({k: w.get(k, "") for k in KOLUMNY})
    tymczasowy.replace(sciezka)      # atomowe w obrębie jednego systemu plików


def _bezpieczna_nazwa(nazwa) -> str:
    """Nazwa grupy nadająca się do wklejenia w literał `"..."` w groups.py."""
    czysta = str(nazwa or "?").replace("\\", "/").replace('"', "'")
    czysta = " ".join(czysta.split())        # nowe linie i tabulatory z FB
    return czysta or "?"


def blok_do_groups_py(wiersze: list[dict]) -> tuple[str, dict[str, int]]:
    """Gotowy blok `FB_GRUPY` — WYŁĄCZNIE z grup oznaczonych `publiczna=TAK`.

    Status w wygenerowanym bloku to `unverified`, mimo że człowiek już coś
    sprawdził. Świadomie: `publiczna=TAK` odpowiada na JEDNO z trzech pytań
    z `config/groups.py`. Zostają dwa — czy grupa żyje i czy jest zgłoszeniowa,
    a nie ogłoszeniowa. Wpisanie tu „ok” ominęłoby je po cichu.
    """
    stat = {"wszystkie": len(wiersze), "tak": 0, "nie": 0, "puste": 0}
    gotowe: list[dict] = []
    for w in wiersze:
        odp = (w.get("publiczna") or "").strip().upper()
        if odp in ("TAK", "T", "YES", "Y"):
            stat["tak"] += 1
            if (w.get("status") or "").strip().lower() != "odrzucona":
                gotowe.append(w)
        elif odp in ("NIE", "N", "NO"):
            stat["nie"] += 1
        else:
            stat["puste"] += 1

    gotowe.sort(key=lambda w: -parsuj_czlonkow(w.get("czlonkowie")))
    linie = ["FB_GRUPY: list[dict[str, str]] = ["]
    for w in gotowe:
        # Nazwa grupy pochodzi z Facebooka i wchodzi WPROST do kodu, który
        # człowiek wklei. Cudzysłów rozwaliłby literał, a odwrotny ukośnik
        # zamienił się w sekwencję ucieczki — obu pozbywamy się tutaj, bo
        # wklejany blok ma się wykonać bez poprawek.
        nazwa = _bezpieczna_nazwa(w.get("nazwa"))
        ilu = parsuj_czlonkow(w.get("czlonkowie"))
        linie.append(f'    # {ilu} członków, fraza: {w.get("fraza_zrodlowa") or "?"}')
        linie.append(f'    {{"url": "{w.get("url")}", "name": "{nazwa}", '
                     f'"region": "{w.get("jezyk") or "?"}", "status": "unverified"}},')
    linie.append("]")
    return "\n".join(linie), stat


# ===========================================================================
# CZĘŚĆ SIECIOWA
# ===========================================================================
def _wejscie(fraza: str, limit: int) -> dict:
    """Wejście actora. Nazwy pól są DANYMI (`config/frazy_grup.py`), bo zmieniają
    się między wersjami actora — sprawdzasz je przez `--schema`, nie przez grep."""
    return {cfg.POLE_FRAZY: fraza, cfg.POLE_LIMITU: limit}


@dataclass
class _Seria:
    kandydaci: list[Kandydat] = field(default_factory=list)
    itemow: int = 0
    koszt_usd: float = 0.0
    bledy: list[str] = field(default_factory=list)


def _szukaj(rotator: KeyRotator, pary: list[tuple[str, str]], limit: int, log) -> _Seria:
    """Jedno wywołanie na frazę — bo tylko tak wiadomo, KTÓRA fraza znalazła grupę.

    Kolumna `fraza_zrodlowa` jest w CSV nie dla ozdoby: z niej bierze się `jezyk`
    i ocena, czy fraza w ogóle zarabia na swoje wywołanie. Wrzucenie wszystkich
    fraz do jednego runu byłoby tańsze i bezużyteczne — atrybucji nie da się
    odtworzyć po fakcie.
    """
    seria = _Seria()
    for i, (jezyk, fraza) in enumerate(pary):
        if i:
            time.sleep(ODSTEP_S)
        log(f"\n[{KTO}] [{jezyk}] „{fraza}” ({i + 1}/{len(pary)})")
        try:
            run = rotator.call(lambda token: apify_run.uruchom(
                token, cfg.ACTOR, _wejscie(fraza, limit),
                timeout_s=TIMEOUT_RUNU_S, max_czekania_s=TIMEOUT_RUNU_S * 1.5,
                max_itemow=limit, log=log))
        except AllKeysExhausted as e:
            seria.bledy.append(f"{fraza}: {e}")
            log(f"[{KTO}] PRZERYWAM serię — {e}")
            break
        except Exception as e:  # noqa: BLE001 — jedna fraza nie może zabrać reszty
            # Kilkanaście fraz to kilkanaście osobnych, już opłaconych wywołań.
            # Wyjątek lecący wyżej zabrałby ze sobą CSV z wynikami wszystkich
            # poprzednich — czyli pieniądze wydane, plik nie zapisany.
            seria.bledy.append(f"{fraza}: {type(e).__name__}: {str(e)[:100]}")
            log(f"[{KTO}] [{jezyk}] „{fraza}”: BŁĄD {type(e).__name__} — lecę dalej")
            continue
        seria.itemow += run.ile_itemow
        seria.koszt_usd += run.koszt_usd or 0.0
        if run.blad:
            seria.bledy.append(f"{fraza}: {run.blad}")

        znalezione = [k for k in (na_kandydata(it, jezyk, fraza) for it in run.itemy)
                      if k is not None]
        seria.kandydaci.extend(znalezione)
        log(f"[{KTO}] [{jezyk}] „{fraza}”: {len(znalezione)} grup "
            f"z {run.ile_itemow} itemów")

        if i == 0 and run.ile_itemow and not znalezione:
            # Itemy są, ale ani jeden nie ma adresu grupy: actor zmienił nazwy pól
            # (POLA_URL). Dalsza seria kosztowałaby tyle samo i dała pusty CSV.
            log(f"[{KTO}] PRZERYWAM: actor zwrócił {run.ile_itemow} itemów, ale w "
                f"żadnym nie ma adresu grupy. Nazwy pól się zmieniły — sprawdź "
                f"`--schema` i popraw POLA_URL w tym pliku.")
            seria.bledy.append("nie rozpoznano pól wyjściowych actora")
            break
    return seria


# ===========================================================================
# CLI
# ===========================================================================
def _parsuj(argv: list[str]) -> dict:
    opcje = {"jezyki": None, "min_czlonkow": cfg.MIN_CZLONKOW, "klucz": 1,
             "limit": cfg.WYNIKOW_NA_FRAZE, "csv": CSV_PATH,
             "raport": False, "schema": False, "sucho": False, "tak": False}
    i = 1
    while i < len(argv):
        a, nast = argv[i], (argv[i + 1] if i + 1 < len(argv) else "")
        if a == "--jezyk" and nast:
            opcje["jezyki"] = [x for x in nast.replace(",", " ").split() if x]
            i += 2
        elif a == "--min-czlonkow" and nast.isdigit():
            opcje["min_czlonkow"] = int(nast)
            i += 2
        elif a == "--klucz" and nast.isdigit():
            opcje["klucz"] = int(nast)
            i += 2
        elif a == "--limit" and nast.isdigit():
            opcje["limit"] = int(nast)
            i += 2
        elif a == "--csv" and nast:
            opcje["csv"] = Path(nast)
            i += 2
        elif a == "--raport":
            opcje["raport"] = True
            i += 1
        elif a == "--schema":
            opcje["schema"] = True
            i += 1
        elif a in ("--sucho", "--dry-run"):
            opcje["sucho"] = True
            i += 1
        elif a in ("--tak", "--yes"):
            opcje["tak"] = True
            i += 1
        elif a in ("-h", "--help"):
            opcje["pomoc"] = True
            i += 1
        else:
            opcje.setdefault("nieznane", []).append(a)
            i += 1
    return opcje


def _tryb_raport(sciezka: Path) -> int:
    """Po uzupełnieniu kolumny `publiczna` — blok do wklejenia w config/groups.py."""
    wiersze = czytaj_csv(sciezka)
    if not wiersze:
        print(f"[{KTO}] Pusty albo brakujący {sciezka} — najpierw odpal serię "
              f"(bez --raport).", file=sys.stderr)
        return 0
    blok, stat = blok_do_groups_py(wiersze)
    print(f"[{KTO}] {stat['wszystkie']} grup w CSV: {stat['tak']}× TAK, "
          f"{stat['nie']}× NIE, {stat['puste']}× jeszcze nieoznaczone")
    if stat["puste"]:
        print(f"[{KTO}] UWAGA: {stat['puste']} grup bez odpowiedzi w kolumnie "
              f"`publiczna` — NIE MA ICH w bloku poniżej. Otwórz każdy URL "
              f"i wpisz TAK/NIE.")
    if not stat["tak"]:
        print(f"[{KTO}] Ani jedna grupa nie ma `publiczna=TAK` — nie ma czego "
              f"wklejać. To jest ten krok ręczny, którego nie da się pominąć.",
              file=sys.stderr)
        return 0
    print(f"\n[{KTO}] Wklej do laweta_radar/config/groups.py (podmień FB_GRUPY):\n")
    print(blok)
    print(f"\n[{KTO}] Status zostaje `unverified` ŚWIADOMIE: „publiczna” to jedno "
          f"z trzech pytań z groups.py. Zostają dwa — czy grupa żyje i czy jest "
          f"zgłoszeniowa, a nie ogłoszeniowa. Przestawiaj na „ok” pojedynczo.")
    return 0


def _tryb_schema(token: str) -> int:
    """Jakie pola actor NAPRAWDĘ przyjmuje. Za darmo — schemat nie jest runem."""
    try:
        schemat = apify_run.schemat_wejscia(token, cfg.ACTOR)
    except Exception as e:  # noqa: BLE001 — diagnostyka: pokaż powód, nie traceback
        print(f"[{KTO}] Nie odczytałem schematu: {type(e).__name__}: {str(e)[:200]}",
              file=sys.stderr)
        return 1
    wlasciwosci = (schemat or {}).get("properties") or {}
    if not wlasciwosci:
        print(f"[{KTO}] Actor {cfg.ACTOR} nie publikuje schematu wejścia — pola "
              f"trzeba sprawdzić na jego stronie w Apify Store.")
        return 0
    print(f"[{KTO}] {cfg.ACTOR} przyjmuje {len(wlasciwosci)} pól:")
    for nazwa, opis in sorted(wlasciwosci.items()):
        typ = (opis or {}).get("type", "?")
        tytul = (opis or {}).get("title", "")
        print(f"    {nazwa:<28} {typ:<10} {tytul}")
    print()
    for stala, wartosc in (("POLE_FRAZY", cfg.POLE_FRAZY),
                           ("POLE_LIMITU", cfg.POLE_LIMITU)):
        znak = "OK  " if wartosc in wlasciwosci else "BRAK"
        print(f"[{KTO}] {znak} {stala} = „{wartosc}”")
    if apify_run.nieznane_pola(_wejscie("test", 1), schemat):
        print(f"[{KTO}] Popraw stałe w laweta_radar/config/frazy_grup.py ZANIM "
              f"odpalisz serię — zła nazwa pola nie zwraca błędu, tylko run bez "
              f"filtra, za pełną cenę.", file=sys.stderr)
        return 1
    return 0


def _sprawdz_schemat(token: str, log=print) -> None:
    """Ostrzeż, jeśli actor nie deklaruje pól, które zamierzamy wysłać.

    Wołane PRZED serią i za darmo (schemat nie jest runem). Nie zatrzymuje —
    actor bez opublikowanego schematu nie pozwala niczego stwierdzić, a i nasza
    lista pól bywa świeższa niż jego schemat. Ma OSTRZEC, zanim człowiek wpisze
    TAK, a nie decydować za niego.
    """
    try:
        schemat = apify_run.schemat_wejscia(token, cfg.ACTOR)
    except Exception as e:  # noqa: BLE001 — to jest podpowiedź, nie bramka
        log(f"[{KTO}] (nie sprawdziłem schematu actora: {type(e).__name__})")
        return
    brakujace = apify_run.nieznane_pola(_wejscie("test", 1), schemat)
    if brakujace:
        log(f"[{KTO}] UWAGA: actor NIE deklaruje pól {brakujace}. Zła nazwa pola "
            f"nie zwraca błędu — zwraca run bez filtra, za pełną cenę. Sprawdź "
            f"`--schema` i popraw laweta_radar/config/frazy_grup.py.")


def _main(argv: list[str]) -> int:
    opcje = _parsuj(argv)
    if opcje.get("pomoc"):
        print(__doc__)
        return 0
    if opcje.get("nieznane"):
        print(f"[{KTO}] Nieznane argumenty: {' '.join(opcje['nieznane'])}. "
              f"Pomoc: --help", file=sys.stderr)
        return 0

    # --raport nie dotyka sieci ani kluczy — to czytanie pliku, który wypełnił człowiek.
    if opcje["raport"]:
        return _tryb_raport(Path(opcje["csv"]))

    pary = cfg.frazy(opcje["jezyki"])
    if not pary:
        print(f"[{KTO}] Pusta lista fraz — kończę bez działania.\n"
              f"[{KTO}] Uzupełnij FRAZY w laweta_radar/config/frazy_grup.py "
              f"(albo popraw --jezyk {opcje['jezyki']}).", file=sys.stderr)
        return 0

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

    if opcje["schema"]:
        return _tryb_schema(tokeny[opcje["klucz"] - 1])

    # --- Koszt PRZED serią -------------------------------------------------
    itemow_max = len(pary) * opcje["limit"]
    szacunek = itemow_max * cfg.CENA_KATALOGOWA_USD_ZA_WYNIK
    jezyki = sorted({j for j, _ in pary})
    print(f"\n[{KTO}] PLAN WYSZUKIWANIA")
    print(f"[{KTO}]   {len(pary)} fraz ({', '.join(jezyki)}) = {len(pary)} wywołań")
    print(f"[{KTO}]   po {opcje['limit']} wyników = NAJWYŻEJ {itemow_max} wyników")
    print(f"[{KTO}]   ≈ {szacunek:.2f} USD (cena katalogowa "
          f"{cfg.CENA_KATALOGOWA_USD_ZA_WYNIK} USD/wynik — SPRAWDŹ na stronie actora)")
    print(f"[{KTO}]   próg członków: {opcje['min_czlonkow']}, klucz Apify: "
          f"#{opcje['klucz']}")
    print(f"[{KTO}]   CSV: {opcje['csv']}"
          + (" (istnieje — praca ręczna zostanie ZACHOWANA)"
             if Path(opcje["csv"]).is_file() else " (nowy)"))
    _sprawdz_schemat(tokeny[opcje["klucz"] - 1])
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
    # `state_path=None`: to jest narzędzie odpalane raz w miesiącu na wskazanym
    # kluczu. Zapisanie jego indeksu cofnęłoby produkcyjnego fetchera do klucza,
    # o którym może już wiedzieć, że jest pusty.
    rotator = KeyRotator(
        tokeny, state_path=None, start_index=opcje["klucz"] - 1,
        transient_key_switches=2 if apify_proxy.is_enabled() else 0,
    )
    seria = _szukaj(rotator, pary, opcje["limit"], print)

    lista, stat = odsiej(seria.kandydaci, opcje["min_czlonkow"])
    sciezka = Path(opcje["csv"])
    istniejace = czytaj_csv(sciezka)
    wiersze = scal(lista, istniejace)
    zapisz_csv(sciezka, wiersze)

    nowych = len(wiersze) - len(istniejace)
    print(f"\n[{KTO}] ===== WYNIK =====")
    print(f"[{KTO}] {seria.itemow} wyników z wyszukiwarki, koszt "
          f"{seria.koszt_usd:.4f} USD")
    print(f"[{KTO}] {stat['wszystkie']} trafień → {stat['duplikaty']} duplikatów, "
          f"{stat['za_male']} poniżej {opcje['min_czlonkow']} członków, "
          f"{stat['zostalo']} zostało")
    print(f"[{KTO}] CSV: {sciezka} — {len(wiersze)} grup łącznie "
          f"({nowych} nowych w tym przebiegu)")
    for blad in seria.bledy[:5]:
        print(f"[{KTO}] błąd: {blad}")

    puste = sum(1 for w in wiersze if not (w.get("publiczna") or "").strip())
    print(f"\n[{KTO}] KROK RĘCZNY: {puste} grup czeka na kolumnę `publiczna`.")
    print(f"[{KTO}] Apify czyta TYLKO grupy publiczne. Otwórz każdy URL, wpisz "
          f"TAK/NIE, potem:")
    print(f"[{KTO}]     python -m laweta_radar.scripts.znajdz_grupy --raport")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
