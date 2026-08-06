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

CIASTECZKA — BEZ NICH CAŁE NARZĘDZIE JEST BEZUŻYTECZNE. Wyszukiwarka grup FB
    oddaje NIEZALOGOWANEMU ścianę logowania, a nie wyniki; actor ma pole `cookies`
    dokładnie z tego powodu. Run bez sesji kosztuje tyle samo co udany i zwraca
    zero grup albo śmieci — dlatego brak pliku jest OSTRZEŻENIEM PRZED serią,
    a nie błędem PO niej.

    Ścieżka: `FB_COOKIES_PATH` w laweta_radar/.env (nie we wspólnym — stamtąd
    przychodzą wyłącznie klucze i proxy Apify). Sam plik trzymasz POZA repo,
    w formacie eksportu z rozszerzenia przeglądarki. W logu widać WYŁĄCZNIE
    liczbę wczytanych ciasteczek — nigdy ich treść, bo to żywa sesja Facebooka.

ZANIM POJDZIE KOMPLET FRAZ ZA ~2,5 USD — jedno wywołanie na jednej frazie:
    python -m laweta_radar.scripts.znajdz_grupy --fraza "giełda lawet"
Wypisuje SUROWY wynik actora. To jedyny sposób sprawdzić, czy sesja i nazwy pól
działają, zanim zapłaci się za komplet: zła nazwa pola nie zwraca błędu, tylko
pusty run za pełną cenę.

WZORZEC: `workers/apify_fb_search_fetcher.py` z repo źródłowego (obsługa fraz,
rotacja kluczy, dedup, bezpieczniki). Różnica: tamten szuka POSTÓW, ten szuka GRUP.

UŻYCIE:
    export PYTHONPATH=$PWD
    python -m laweta_radar.scripts.znajdz_grupy --schema     # NAJPIERW: pola actora
    python -m laweta_radar.scripts.znajdz_grupy --sucho      # plan, wejście, koszt
    python -m laweta_radar.scripts.znajdz_grupy --fraza "X"  # POTEM: jedna fraza na próbę
    python -m laweta_radar.scripts.znajdz_grupy              # seria (pyta o potwierdzenie)
    python -m laweta_radar.scripts.znajdz_grupy --raport     # po uzupełnieniu `publiczna`

    --jezyk pl,de        # tylko wybrane bloki językowe
    --min-czlonkow 300   # niższy próg (domyślnie 500)
    --klucz 3            # konkretny klucz Apify
    --limit 5            # ile wyników na frazę (próba: domyślnie 5)
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

try:                               # pakiet widoczny: -m, import pakietowy, testy
    from laweta_radar.scripts._sciezka import dodaj_repo_do_sciezki
except ImportError:                # uruchomienie po ścieżce do pliku
    from _sciezka import dodaj_repo_do_sciezki

dodaj_repo_do_sciezki()

from laweta_radar.config import frazy_grup as cfg  # noqa: E402
from laweta_radar.config import settings  # noqa: E402
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

# Ile wyników bierze PRÓBA (`--fraza`), gdy nie podano `--limit`. Próba ma
# odpowiedzieć „czy sesja i nazwy pól działają", a nie zebrać dane — pięć wyników
# odpowiada na to tak samo jak trzydzieści i kosztuje sześć razy mniej.
LIMIT_PROBY = 5

# Ciasteczka, po których poznajemy, że plik jest ZALOGOWANĄ SESJĄ Facebooka,
# a nie eksportem z przypadkowej karty. Sprawdzamy NAZWY, nigdy wartości.
CIASTECZKA_SESJI = ("c_user", "xs")

# Czyje ciasteczka mają prawo pojechać do actora. Eksport z rozszerzenia bywa
# eksportem CAŁEJ przeglądarki, a plik idzie w całości do CUDZEGO actora —
# jedzie więc wyłącznie to, co dotyczy Facebooka.
DOMENY_CIASTECZEK = ("facebook.com",)

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


def url_frazy(fraza: str) -> str:
    """Fraza -> adres wyszukiwarki grup FB. Tak, i tylko tak, trafia do actora.

    Actor NIE MA pola na frazę (patrz `SCHEMAT_SPRAWDZONY` w config/frazy_grup.py):
    przyjmuje `startUrls`, więc szukanie wyraża się adresem wyszukiwarki.

    Kodowanie procentowe siedzi TUTAJ, a nie u wołającego, bo pominąć je można
    tylko raz i nikt tego nie zauważy: frazy mają spacje i znaki narodowe
    („giełda lawet", „odtahová služba", „Autotransport Börse"), a niezakodowane
    rozwalają adres. Actor dostaje wtedy URL, który nie jest wyszukiwaniem —
    i płacimy pełną cenę za run bez wyników.

    `safe=""` — kodujemy także `/`, `?` i `&`. W treści frazy nie są separatorami
    adresu, a niezakodowane byłyby przez FB przeczytane właśnie jako separatory.
    """
    return cfg.URL_WYSZUKIWARKI.format(q=quote((fraza or "").strip(), safe=""))


def _ciasteczko_facebooka(ciasteczko: dict) -> bool:
    """Czy to ciasteczko dotyczy Facebooka. Brak domeny = zostaje (część
    eksportów jej nie zapisuje, a odsianie ich zabrałoby całą sesję)."""
    domena = str(ciasteczko.get("domain") or "").strip().lower()
    return not domena or any(d in domena for d in DOMENY_CIASTECZEK)


def wczytaj_ciasteczka(sciezka) -> tuple[list[dict], str]:
    """(ciasteczka, jedna linia dla człowieka). NIE RZUCA i NIE POKAZUJE wartości.

    Nie rzuca, bo brak sesji ma zatrzymać człowieka PRZED serią — pytaniem
    o potwierdzenie, a nie tracebackiem w środku opłaconego przebiegu. Cała
    diagnostyka jedzie w drugim elemencie krotki.

    Z pliku wychodzą wyłącznie LICZBY. Wartość ciasteczka `xs` to zalogowana
    sesja Facebooka — raz wpisana w log zostaje tam na zawsze, także w kopii,
    którą ktoś wklei do zgłoszenia albo wyśle dalej.

    Dwa formaty eksportu dają ten sam wynik: goła lista albo `{"cookies": [...]}`.
    """
    if not str(sciezka or "").strip():
        return [], "BRAK — nie ustawiono FB_COOKIES_PATH w .env"
    plik = Path(str(sciezka)).expanduser()
    if not plik.is_file():
        return [], f"BRAK — nie ma pliku {plik}"
    try:
        dane = json.loads(plik.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # Treść błędu bywa fragmentem pliku, a plik jest sesją — do logu idzie
        # sam typ wyjątku, nigdy `str(e)`.
        return [], f"BRAK — {plik}: nie odczytałem ({type(e).__name__})"
    if isinstance(dane, dict):
        dane = dane.get("cookies")
    if not isinstance(dane, list):
        return [], (f"BRAK — {plik}: oczekiwałem listy ciasteczek albo "
                    f"{{\"cookies\": [...]}}")

    nazwane = [c for c in dane
               if isinstance(c, dict) and str(c.get("name") or "").strip()]
    ciasteczka = [c for c in nazwane if _ciasteczko_facebooka(c)]
    obce = len(nazwane) - len(ciasteczka)
    if not ciasteczka:
        return [], f"BRAK — {plik}: ani jedno ciasteczko nie dotyczy Facebooka"

    czesci = [f"{len(ciasteczka)} z {plik}"]
    if obce:
        czesci.append(f"{obce} spoza Facebooka POMINIĘTO")
    nazwy = {str(c.get("name") or "").strip() for c in ciasteczka}
    if not all(n in nazwy for n in CIASTECZKA_SESJI):
        czesci.append(f"UWAGA: brakuje {'/'.join(CIASTECZKA_SESJI)} — to chyba "
                      f"nie jest zalogowana sesja")
    return ciasteczka, "; ".join(czesci)


def zamaskuj_wejscie(wejscie: dict) -> dict:
    """Kopia wejścia nadająca się do wypisania: ciasteczka zastąpione LICZBĄ.

    Pole ZOSTAJE, zmienia się tylko wartość. `--sucho` ma pozwolić porównać
    wejście ze schematem actora pole po polu, a wycięty klucz wygląda w takim
    porównaniu dokładnie jak pole, którego nie wysyłamy.
    """
    podglad = dict(wejscie)
    ciasteczka = podglad.get(cfg.POLE_CIASTECZEK)
    if isinstance(ciasteczka, list):
        podglad[cfg.POLE_CIASTECZEK] = (f"<{len(ciasteczka)} ciasteczek — "
                                        f"wartości ukryte>")
    return podglad


def podglad_wejscia(wejscie: dict) -> str:
    """Wejście actora jako JSON do wklejenia obok schematu. Bez sekretów."""
    return json.dumps(zamaskuj_wejscie(wejscie), ensure_ascii=False, indent=2)


def jezyk_frazy(fraza: str) -> str:
    """Blok językowy, z którego pochodzi fraza. „?" dla frazy spoza listy.

    Potrzebne w trybie `--fraza`: kolumna `jezyk` bierze się z tego, która fraza
    znalazła grupę, a przy frazie wpisanej z ręki nie ma innego źródła.
    """
    szukana = (fraza or "").strip().casefold()
    for jezyk, znana in cfg.frazy():
        if znana.casefold() == szukana:
            return jezyk
    return "?"


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
def _wejscie(fraza: str, limit: int, ciasteczka: list[dict] | None = None) -> dict:
    """Wejście actora dla JEDNEJ frazy — komplet pól, które wysyłamy.

    Nazwy pól są DANYMI (`config/frazy_grup.py`), bo zmieniają się między
    wersjami actora — sprawdzasz je przez `--schema`, nie przez grep.

    FRAZA JEDZIE JAKO ADRES w `startUrls`, bo actor nie ma pola na samą frazę
    (patrz `SCHEMAT_SPRAWDZONY`). Lista obiektów `{"url": ...}` to konwencja list
    żądań w Apify — ta sama, co w fetcherze i w pomiarze actora.

    Ciasteczka doklejamy TYLKO gdy są. Pusta lista znaczy dla actora to samo, co
    brak pola (sesja niezalogowana), ale w podglądzie `--sucho` wyglądałaby jak
    sesja, której nie ma — a to jedyna rzecz, którą ten podgląd ma rozstrzygać.
    """
    wejscie: dict = {
        cfg.POLE_STARTOWE: [{"url": url_frazy(fraza)}],
        cfg.POLE_LIMITU: limit,
        # Odstęp przewijania wyszukiwarki. Nie jest pokrętłem wydajności:
        # zbyt agresywny wygląda dla FB jak bot i psuje SESJĘ, czyli koszt
        # pomyłki płacą wszystkie następne runy, nie ten.
        cfg.POLE_MIN_ODSTEPU: settings.FB_SEARCH_MIN_DELAY_S,
        cfg.POLE_MAX_ODSTEPU: settings.FB_SEARCH_MAX_DELAY_S,
    }
    if ciasteczka:
        wejscie[cfg.POLE_CIASTECZEK] = list(ciasteczka)
    return wejscie


@dataclass
class _Seria:
    kandydaci: list[Kandydat] = field(default_factory=list)
    itemow: int = 0
    koszt_usd: float = 0.0
    bledy: list[str] = field(default_factory=list)
    # Itemy tak, jak przyszły z actora — do `--fraza`. Przy próbie to jedyna
    # rzecz, która odpowiada na pytanie „czym różni się to, co dostaliśmy, od
    # tego, czego szukają POLA_URL".
    surowe: list[dict] = field(default_factory=list)


def _szukaj(rotator: KeyRotator, pary: list[tuple[str, str]], limit: int, log,
            ciasteczka: list[dict] | None = None) -> _Seria:
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
                token, cfg.ACTOR, _wejscie(fraza, limit, ciasteczka),
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
        seria.surowe.extend(run.itemy)
        if run.blad:
            seria.bledy.append(f"{fraza}: {run.blad}")

        znalezione = [k for k in (na_kandydata(it, jezyk, fraza) for it in run.itemy)
                      if k is not None]
        seria.kandydaci.extend(znalezione)
        log(f"[{KTO}] [{jezyk}] „{fraza}”: {len(znalezione)} grup "
            f"z {run.ile_itemow} itemów")

        if i == 0 and not run.ile_itemow:
            # Pierwsza fraza to najmocniejsza fraza z listy. Zero itemów oznacza
            # najczęściej ścianę logowania — a wtedy KAŻDE kolejne wywołanie
            # zwróci to samo zero i zostanie policzone tak samo jak udane.
            # Jedno stracone wywołanie zamiast dwudziestu ośmiu.
            log(f"[{KTO}] PRZERYWAM: pierwsza fraza zwróciła ZERO wyników. "
                f"Najczęstsza przyczyna to brak sesji — wyszukiwarka grup FB "
                f"pokazuje niezalogowanemu ścianę logowania. Sprawdź "
                f"FB_COOKIES_PATH w .env, potem jedną frazę: --fraza „{fraza}”.")
            seria.bledy.append("pierwsza fraza bez wyników — sprawdź sesję "
                               "(ciasteczka)")
            break

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
             "limit": cfg.WYNIKOW_NA_FRAZE, "csv": CSV_PATH, "fraza": "",
             "raport": False, "schema": False, "sucho": False, "tak": False,
             # Czy `--limit` padł JAWNIE. Próba `--fraza` bez tego schodzi na
             # `LIMIT_PROBY` — pyta o działanie sesji, a nie o dane.
             "limit_jawny": False}
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
            opcje["limit_jawny"] = True
            i += 2
        elif a in ("--fraza", "--fraze") and nast:
            opcje["fraza"] = nast.strip()
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

    # Porównujemy KOMPLET pól, które realnie wysyłamy — z ciasteczkiem-atrapą,
    # żeby `cookies` też przeszło przez sprawdzenie, gdy sesji akurat nie ma.
    wzor = _wejscie("próba", 1, [{"name": "atrapa", "value": "atrapa"}])
    print(f"\n[{KTO}] Wysyłamy {len(wzor)} pól (config/frazy_grup.py, schemat "
          f"sprawdzony {cfg.SCHEMAT_SPRAWDZONY}):")
    for pole in wzor:
        znak = "OK  " if pole in wlasciwosci else "BRAK"
        print(f"[{KTO}] {znak} {pole}")
    if apify_run.nieznane_pola(wzor, schemat):
        print(f"[{KTO}] Popraw stałe w laweta_radar/config/frazy_grup.py ZANIM "
              f"odpalisz serię — zła nazwa pola nie zwraca błędu, tylko run bez "
              f"filtra, za pełną cenę.", file=sys.stderr)
        return 1
    print(f"[{KTO}] Komplet pól zgadza się ze schematem. Jeśli coś tu poprawiasz, "
          f"podmień też datę przy SCHEMAT_SPRAWDZONY — to ona mówi następnemu "
          f"człowiekowi, kiedy ktokolwiek ostatni raz to widział.")
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
    brakujace = apify_run.nieznane_pola(
        _wejscie("próba", 1, [{"name": "atrapa", "value": "atrapa"}]), schemat)
    if brakujace:
        log(f"[{KTO}] UWAGA: actor NIE deklaruje pól {brakujace}. Zła nazwa pola "
            f"nie zwraca błędu — zwraca run bez filtra, za pełną cenę. Sprawdź "
            f"`--schema` i popraw laweta_radar/config/frazy_grup.py.")


def _wynik_proby(seria: _Seria, byly_ciasteczka: bool) -> int:
    """Surowy wynik jednej frazy + WERDYKT: czy komplet ma sens.

    Próba istnieje po to, żeby rozstrzygnąć jedną rzecz — czy sesja i nazwy pól
    działają. Sam JSON tego nie mówi: zero itemów i itemy bez adresów grup
    wyglądają w logu podobnie, a znaczą co innego i co innego się po nich robi.
    """
    print(f"\n[{KTO}] ===== SUROWY WYNIK ({len(seria.surowe)} itemów, koszt "
          f"{seria.koszt_usd:.4f} USD) =====")
    print(json.dumps(seria.surowe, ensure_ascii=False, indent=2))
    for blad in seria.bledy[:5]:
        print(f"[{KTO}] błąd: {blad}")

    print(f"\n[{KTO}] ===== WERDYKT =====")
    if not seria.surowe:
        print(f"[{KTO}] ZERO itemów. Najczęstsza przyczyna to brak sesji: "
              f"wyszukiwarka grup FB oddaje niezalogowanemu ścianę logowania.")
        trop = ("Ciasteczka BYŁY wysłane — sesja jest nieważna albo wygasła; "
                "wyeksportuj ją z przeglądarki ponownie." if byly_ciasteczka else
                "Ciasteczek NIE BYŁO — ustaw FB_COOKIES_PATH w .env.")
        print(f"[{KTO}] {trop}")
        print(f"[{KTO}] Drugi trop, gdy sesja na pewno działa: nazwy pól "
              f"WEJŚCIA — `--schema`.")
        return 0
    if not seria.kandydaci:
        print(f"[{KTO}] Itemy są, ale w żadnym nie ma adresu grupy — actor "
              f"zmienił nazwy pól WYJŚCIA. Popraw POLA_URL w tym pliku "
              f"(porównaj z JSON-em wyżej).")
        return 0
    print(f"[{KTO}] OK: {len(seria.kandydaci)} grup rozpoznanych z "
          f"{len(seria.surowe)} itemów — sesja i nazwy pól działają.")
    for k in seria.kandydaci[:5]:
        print(f"[{KTO}]   {k.czlonkowie:>8} członków  {k.url}")
    print(f"[{KTO}] Możesz odpalić komplet: "
          f"python -m laweta_radar.scripts.znajdz_grupy")
    print(f"[{KTO}] Próba NIC nie zapisała do CSV — to było sprawdzenie, "
          f"nie zbieranie.")
    return 0


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

    # --fraza: JEDNO wywołanie na jednej frazie, żeby sprawdzić sesję i nazwy pól,
    # zanim pójdzie komplet za ~2,5 USD. Bez jawnego `--limit` schodzimy na
    # `LIMIT_PROBY` — próba odpowiada na pytanie „czy działa", a nie „ile jest".
    proba = bool(opcje["fraza"])
    if proba:
        pary = [(jezyk_frazy(opcje["fraza"]), opcje["fraza"])]
        if not opcje["limit_jawny"]:
            opcje["limit"] = LIMIT_PROBY
    else:
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

    # --- Sesja FB: bez niej wyszukiwarka nie ma czego pokazać ---------------
    ciasteczka, stan_ciastek = wczytaj_ciasteczka(settings.FB_COOKIES_PATH)

    # --- Koszt PRZED serią -------------------------------------------------
    itemow_max = len(pary) * opcje["limit"]
    szacunek = itemow_max * cfg.CENA_KATALOGOWA_USD_ZA_WYNIK
    jezyki = sorted({j for j, _ in pary})
    print(f"\n[{KTO}] PLAN {'PRÓBY' if proba else 'WYSZUKIWANIA'}")
    if proba:
        print(f"[{KTO}]   1 fraza [{pary[0][0]}] „{pary[0][1]}” = 1 wywołanie, "
              f"najwyżej {opcje['limit']} wyników")
    else:
        print(f"[{KTO}]   {len(pary)} fraz ({', '.join(jezyki)}) = "
              f"{len(pary)} wywołań")
        print(f"[{KTO}]   po {opcje['limit']} wyników = NAJWYŻEJ "
              f"{itemow_max} wyników")
    print(f"[{KTO}]   ≈ {szacunek:.2f} USD (cena katalogowa "
          f"{cfg.CENA_KATALOGOWA_USD_ZA_WYNIK} USD/wynik — SPRAWDŹ na stronie actora)")
    print(f"[{KTO}]   klucz Apify: #{opcje['klucz']}"
          + ("" if proba else f", próg członków: {opcje['min_czlonkow']}"))
    print(f"[{KTO}]   ciasteczka FB: {stan_ciastek}")
    print(f"[{KTO}]   odstęp przewijania: {settings.FB_SEARCH_MIN_DELAY_S}"
          f"-{settings.FB_SEARCH_MAX_DELAY_S} s")
    print(f"[{KTO}]   CSV: " + ("próba NIC nie zapisuje" if proba else
          f"{opcje['csv']}" + (" (istnieje — praca ręczna zostanie ZACHOWANA)"
                               if Path(opcje["csv"]).is_file() else " (nowy)")))

    # Wejście actora WPROST na ekran — po to, żeby dało się je porównać
    # ze schematem (`--schema`) gołym okiem, a nie przez czytanie tego pliku.
    # Ciasteczka są zamaskowane: pokazujemy liczbę, nigdy wartości.
    print(f"\n[{KTO}] WEJŚCIE ACTORA (pierwsza fraza — „{pary[0][1]}”):")
    print(podglad_wejscia(_wejscie(pary[0][1], opcje["limit"], ciasteczka)))
    _sprawdz_schemat(tokeny[opcje["klucz"] - 1])

    if not ciasteczka:
        # OSTRZEŻENIE, nie błąd: plik z sesją bywa gdzie indziej, a decyzja
        # „i tak sprawdzę" należy do człowieka. Ma tylko wiedzieć, za co płaci.
        print(f"\n[{KTO}] OSTRZEŻENIE: LECIMY BEZ SESJI FACEBOOKA")
        print(f"[{KTO}]   Wyszukiwarka grup pokazuje NIEZALOGOWANEMU ścianę "
              f"logowania, a nie wyniki.")
        print(f"[{KTO}]   Ten przebieg najprawdopodobniej zwróci zero grup albo "
              f"śmieci — i zostanie policzony tak samo jak udany (≈ "
              f"{szacunek:.2f} USD).")
        print(f"[{KTO}]   Ustaw FB_COOKIES_PATH w .env (plik JSON z eksportu "
              f"rozszerzenia, trzymany POZA repo).")
        if not proba:
            print(f"[{KTO}]   Taniej niż komplet: --fraza „{pary[0][1]}” "
                  f"(jedno wywołanie, {LIMIT_PROBY} wyników).")

    if opcje["sucho"]:
        print(f"\n[{KTO}] --sucho: nic nie odpalam.")
        return 0
    if not opcje["tak"]:
        # Pytanie NAZYWA to, co się potwierdza. „Odpalić?" po ostrzeżeniu wyżej
        # człowiek odklika odruchowo — i dowie się o braku sesji z rachunku.
        pytanie = ("Odpalić MIMO BRAKU CIASTECZEK? wpisz TAK: " if not ciasteczka
                   else "Odpalić? wpisz TAK: ")
        try:
            odp = input(f"[{KTO}] {pytanie}").strip()
        except (EOFError, KeyboardInterrupt):
            odp = ""
        if odp != "TAK":
            print(f"[{KTO}] Przerwane — nic nie wydano.")
            return 0
    elif not ciasteczka:
        print(f"[{KTO}] --tak: pytanie o brak ciasteczek POMINIĘTE świadomie.")

    # --- Seria -------------------------------------------------------------
    # `state_path=None`: to jest narzędzie odpalane raz w miesiącu na wskazanym
    # kluczu. Zapisanie jego indeksu cofnęłoby produkcyjnego fetchera do klucza,
    # o którym może już wiedzieć, że jest pusty.
    rotator = KeyRotator(
        tokeny, state_path=None, start_index=opcje["klucz"] - 1,
        transient_key_switches=2 if apify_proxy.is_enabled() else 0,
    )
    seria = _szukaj(rotator, pary, opcje["limit"], print, ciasteczka)

    if proba:
        return _wynik_proby(seria, bool(ciasteczka))

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
