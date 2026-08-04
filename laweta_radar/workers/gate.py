"""Bramka — DARMOWY prefiltr słownikowy przed modelem AI.

ASYMETRIA KOSZTÓW, która dyktuje KAŻDĄ decyzję w tym module:

    śmieć przepuszczony do AI      ->  ~0,002 zł
    zlecenie odrzucone przez gate  ->  ~300 zł straconego kursu i NIGDY się o tym
                                       nie dowiesz, bo post nie trafi nigdzie

Stosunek mniej więcej jeden do stu pięćdziesięciu tysięcy. Bramka bez filtra
kosztowałaby 150-250 zł miesięcznie w tokenach, więc JEDEN przegapiony kurs
miesięcznie kasuje całą oszczędność. Stąd jedyna reguła, która tu obowiązuje:

    ODRZUCAMY WYŁĄCZNIE TO, CO JEST ŚMIECIEM PONAD WSZELKĄ WĄTPLIWOŚĆ.
    Przy jakimkolwiek wahaniu — PRZEPUSZCZAMY i płacimy grosz za rozstrzygnięcie AI.

Nie celujemy w "5% przepuszczonych". Celujemy w ZERO fałszywych odrzuceń, a
odsetek niech wyjdzie, ile wyjdzie (realistycznie 20-35% i to jest w porządku).

To jest też jedyne miejsce w systemie, które w ogóle coś odrzuca — i odrzuca
wyłącznie posty, które NIE SĄ ZLECENIAMI: reklamę konkurencji, sprzedaż sprzętu,
ogłoszenia o pracę i posty wygaszone przez autora. Ta lista jest zamknięta.
Ocen biznesowych ("za daleko", "za ciężkie", "za tanio") bramka NIE robi i nie
wolno jej ich dopisać — o tym decyduje kierowca, patrząc na ekran.

CZTERY WARSTWY, W TEJ KOLEJNOŚCI. Kolejność jest merytoryczna, nie porządkowa:

  1. WYGASZENIE      — unieważnia wszystko poniżej (post z kompletem słów
                       kluczowych, ale autor dopisał "załatwione")
  2. PRZEPUSZCZENIE  — sygnał POTRZEBY bije sygnał odrzucenia
  3. ODRZUCENIE      — tylko wzorce jednoznaczne
  4. PUNKTACJA       — reszta; próg kalibrowany jedną liczbą w .env

Dwa miejsca, w których ta kolejność ratuje realne pieniądze:

  • "Sprzedam golfa po stłuczce, ale najpierw trzeba go odholować z parkingu"
    — warstwa 3 zobaczyłaby "sprzedam" i skasowała prawdziwe zlecenie.
  • "szukam lawety" ma 13 znaków, a warstwa 3 odrzuca wszystko poniżej 15.
    Warstwa 2 łapie ten post wcześniej i dlatego limit długości jest nieszkodliwy.

CZTERY JĘZYKI: PL / DE / CS / SK. Każdy ma WŁASNY słownik o tej samej strukturze
warstw i tych samych wagach; czeski i słowacki dzielą jeden, z wariantami obu
w środku. Powód jest ten sam co wyżej — asymetria kosztów. Niemieckie „Suche
Autotransport von München nach Krakau, Fahrzeug fährt nicht" to wzorcowe zlecenie
transportowe, a przy samych polskich wzorcach nie trafia ANI JEDNEGO i wylatuje
z zerem punktów, wyglądając w logach dokładnie jak odrzucona reklama felg.
Szczegóły rozstrzygania między słownikami: docstring `gate()`.

ZERO WYWOŁAŃ SIECIOWYCH i zero I/O — także w detekcji języka, która jest
heurystyką na znakach i słowach funkcyjnych, a nie biblioteką. Moduł działa
offline, w mikrosekundach, i da się go zaimportować bez bazy, bez kluczy
i bez internetu. Zapis decyzji do bazy robi wołający — kontrakt kolumn:
`wiersz_do_zapisu()` + api/migrations/0002_gate.sql i 0003_fetcher.sql.

CLI:
    python -m laweta_radar.workers.gate "treść posta"
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from laweta_radar.config import settings

# ---------------------------------------------------------------------------
# NORMALIZACJA
#
# Ludzie piszą bez ogonków, z literówkami, WERSALIKAMI i seriami wykrzykników.
# Wszystkie wzorce w tym module są zapisane JUŻ w formie znormalizowanej —
# inaczej "lawetę" nigdy by nie trafiło.
# ---------------------------------------------------------------------------

# `ł` NIE rozkłada się przez NFKD (to osobna litera, nie l z akcentem), więc
# polskie znaki mapujemy jawnie. NFKD niżej dobiera resztę alfabetów, które
# realnie pojawiają się w tych postach: "Autohaus München", "Liège", "Kolonia".
_POLSKIE = str.maketrans({
    "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n",
    "ó": "o", "ś": "s", "ż": "z", "ź": "z",
    "Ą": "a", "Ć": "c", "Ę": "e", "Ł": "l", "Ń": "n",
    "Ó": "o", "Ś": "s", "Ż": "z", "Ź": "z",
})

_POWTORZONA_INTERPUNKCJA = re.compile(r"([^\w\s])\1+")
_BIALE_ZNAKI = re.compile(r"\s+")


def normalizuj(tresc: str) -> str:
    """Treść posta -> forma, na której dopasowujemy wzorce.

    Kolejność ma znaczenie: najpierw małe litery (bo mapa polskich znaków
    obsługuje oba warianty, ale reszta świata przechodzi przez NFKD), potem
    ogonki, potem reszta diakrytyków, na końcu interpunkcja i spacje.
    """
    s = (tresc or "").lower().translate(_POLSKIE)
    s = "".join(c for c in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(c))
    s = _POWTORZONA_INTERPUNKCJA.sub(r"\1", s)      # "!!!" -> "!", "..." -> "."
    return _BIALE_ZNAKI.sub(" ", s).strip()


# ---------------------------------------------------------------------------
# KOMPILACJA WZORCÓW
#
# Wzorce trzymamy jako dane — listy krotek (wzorzec, waga, etykieta) — a nie
# zaszyte w if-ach. Dopisanie słowa ma być zmianą JEDNEJ linijki, bo ten słownik
# będzie rósł po każdym tygodniu patrzenia w raport.
#
# Granice: dopasowujemy po granicy słowa, żeby "hol" nie łapało "alkoholu",
# "kolo" nie łapało "około", a "tel" nie łapało "telewizora". Używamy lookaroundów
# na [a-z0-9] zamiast \b, bo tekst jest już zredukowany do ASCII i \b zachowywałby
# się inaczej przy cyfrach sklejonych z literą ("dk28").
#
# Prawa granica jest dokładana AUTOMATYCZNIE, ale TYLKO wtedy, gdy wzorzec kończy
# się znakiem, po którym granica ma sens: literą, cyfrą albo domknięciem grupy.
# Wzorzec kończący się kwantyfikatorem (* + ?) lub spacją mówi jawnie "tu może być
# ciąg dalszy" i granicy nie dostaje — inaczej "na dk ?[0-9]*" nie trafiłoby
# "na dk28", a "polecicie " nie trafiłoby "polecicie jakąś lawetę".
#
# Ta reguła jest tu, bo pomyłka w niej jest NIEWIDOCZNA: wzorzec z domkniętą
# granicą po spacji nie trafia NIGDY, a moduł nadal działa i nadal coś zwraca.
# ---------------------------------------------------------------------------
_KONCOWKI_Z_GRANICA = frozenset("abcdefghijklmnopqrstuvwxyz0123456789)]")


def _skompiluj(wzorzec: str) -> re.Pattern[str]:
    prawa = r"(?![a-z0-9])" if wzorzec[-1:] in _KONCOWKI_Z_GRANICA else ""
    return re.compile(rf"(?<![a-z0-9])(?:{wzorzec}){prawa}")


def _skompiluj_liste(wzorce):
    return [(_skompiluj(w), waga, etykieta) for w, waga, etykieta in wzorce]


# ---------------------------------------------------------------------------
# WARSTWA 1 — WYGASZENIE
#
# Sprawdzana PIERWSZA, bo unieważnia wszystko poniżej. W grupach ogłoszeniowych
# autorzy masowo edytują posty i dopisują, że sprawa załatwiona. Taki post nadal
# ma komplet słów kluczowych zlecenia, więc bez tej warstwy przeszedłby przez
# wszystko, kosztował token i wygenerował alert o kursie, którego nie ma. To
# najbardziej irytujący możliwy fałszywy alarm — i jedyny, który operator
# zauważy, więc jedyny, który podkopuje zaufanie do całego systemu.
#
# Waga jest tu bez znaczenia (trafienie = koniec), ale trzymamy jednolity kształt
# krotki, żeby wszystkie warstwy dało się przeglądać i testować tak samo.
#
# TRZY WZORCE Z LISTY ZADANIA SĄ TU ZAWĘŻONE — świadomie, bo w formie dosłownej
# łamałyby regułę naczelną tego modułu (zero fałszywych odrzuceń):
#
#   • "znalazlem"  — "znalazłem auto w Niemczech, kto przywiezie?" to POCZĄTEK
#     zlecenia, nie jego koniec. Zawężone do "znalazłem KOGOŚ/lawetę/firmę".
#   • "mam juz"    — "mam już kupione auto, potrzebuję transportu" j.w.
#     Zawężone do "mam już kogoś/lawetę/załatwione".
#   • "juz po"     — "już po naprawie", "już po szkodzie", "już po godzinach"
#     to nie jest wygaszenie. Zawężone do "już po sprawie/temacie/wszystkim".
#
# Osobno "dziekuje wszystkim": polski post kończący prośbę zwrotem "z góry
# dziękuję wszystkim" jest OTWARTYM zleceniem. Stąd lookaroundy na "z gory".
# ---------------------------------------------------------------------------
_ZAMKNIECIE = "kogos|lawete|firme|chetnego|wykonawce|kierowce|pomoc|transport|laweta"

WYGASZENIE: list[tuple[str, int, str]] = [
    # Wzorce są ROZŁĄCZNE świadomie: "juz nieaktualne" i "sprawa zalatwiona" nie
    # mają tu osobnych wpisów, bo pokrywa je forma krótsza. Martwy wzorzec w tej
    # tabeli jest gorszy niż jego brak — wygląda na zabezpieczenie, którego nie ma.
    (r"nieaktualn[eya]", 0, "wygaszone"),
    (r"zalatwion[eay]", 0, "wygaszone"),
    (rf"znalazl[ae]m (juz )?({_ZAMKNIECIE})", 0, "wygaszone"),
    (rf"juz znalazl[ae]m", 0, "wygaszone"),
    (rf"mam juz ({_ZAMKNIECIE}|zalatwione)", 0, "wygaszone"),
    (r"juz nie potrzebuje", 0, "wygaszone"),
    (r"nie potrzebuje juz", 0, "wygaszone"),
    (r"juz nie potrzeba", 0, "wygaszone"),
    (r"(?<!z gory )dziekuje wszystkim(?! z gory)", 0, "wygaszone"),
    (r"temat zamkniety", 0, "wygaszone"),
    (r"(temat )?do zamkniecia", 0, "wygaszone"),
    (r"rozwiazan[ey]", 0, "wygaszone"),
    (r"odwolane", 0, "wygaszone"),
    (r"odwoluje", 0, "wygaszone"),
    (r"juz po (sprawie|temacie|wszystkim|ptakach)", 0, "wygaszone"),
    (r"podjet[eya]", 0, "wygaszone"),
    # "JUŻ" jest tu nieusuwalne. Samo "ktoś jedzie" to PYTANIE ("ktoś jedzie
    # w tamtą stronę?"), czyli początek zlecenia — wygaszeniem jest dopiero
    # "ktoś JUŻ jedzie". Wyszło na korpusie: bez "juz" wzorzec kasował prośbę
    # o doładunek, czyli najlepszy typ zlecenia, jaki ten system ma znajdować.
    (r"ktos juz jedzie", 0, "wygaszone"),
    (r"juz ktos jedzie", 0, "wygaszone"),
    (r"ktos sie (juz )?zglosil", 0, "wygaszone"),
]

# ---------------------------------------------------------------------------
# WARSTWA 2 — TWARDE PRZEPUSZCZENIE
#
# Sprawdzana PRZED odrzuceniem i to jest poprawka błędu, który łatwo popełnić.
# Sygnał POTRZEBY zawsze bije sygnał odrzucenia.
# ---------------------------------------------------------------------------
_USLUGA = ("lawety|lawete|pomocy drogowej|pomoc drogowa|holowania|odholowania|"
           "wyciagniecia|transportu|przewozu|pomocy|kogos z laweta")

PRZEPUSZCZENIE: list[tuple[str, int, str]] = [
    # --- prośba wprost ---
    (rf"potrzebuje ({_USLUGA})", 0, "prosba wprost"),
    (r"potrzebn[ay] (mi )?(laweta|pomoc drogowa|laweta na juz)", 0, "prosba wprost"),
    (r"potrzebn[ye] (transport|przewoz|holowanie|odholowanie)", 0, "prosba wprost"),
    (r"szukam (lawety|pomocy drogowej|kogos z laweta|firmy|transportu|"
     r"przewoznika|miejsca na lawecie|kogos kto)", 0, "prosba wprost"),
    (r"pilnie laweta", 0, "prosba wprost"),
    (r"laweta pilnie", 0, "prosba wprost"),
    (r"kto (mi )?(odholuje|przewiezie|przywiezie|zawiezie|podskoczy|pomoze|"
     r"zabierze|sciagnie|podjedzie|ma czas|wezmie na lawete)", 0, "prosba wprost"),
    (r"(jest )?ktos (w okolicy|z okolic|z laweta|obok)", 0, "prosba wprost"),
    (r"(ma ktos|kto ma) lawete", 0, "prosba wprost"),
    (r"odholowanie", 0, "prosba wprost"),
    (r"odholowac", 0, "prosba wprost"),
    (r"podholowac", 0, "prosba wprost"),
    (r"wziac na hol", 0, "prosba wprost"),
    (r"na hol", 0, "prosba wprost"),
    (r"przewoz (auta|samochodu|pojazdu)", 0, "prosba wprost"),
    (r"przewiezieni[ae] (auta|samochodu|pojazdu)", 0, "prosba wprost"),
    (r"transport (auta|samochodu|pojazdu|aut|samochodow)", 0, "prosba wprost"),
    (r"przewiez(c|ienia)? ?(auto|samochod|pojazd)", 0, "prosba wprost"),
    (r"sciagn(ac|iecie|iecia) auto", 0, "prosba wprost"),
    (r"zabra(c|nie|nia) auto", 0, "prosba wprost"),
    # "na lawete" (biernik) świadomie NIE jest wzorcem samodzielnym: "zatrudnię
    # kierowcę NA LAWETĘ" to ogłoszenie o pracę. Dlatego tylko w połączeniu
    # z czynnością albo pojazdem.
    (r"(auto|samochod|pojazd) na lawete", 0, "prosba wprost"),
    (r"(zabra(c|nie|nia)|wziac|zaladowa(c|nie|nia)) na lawete", 0, "prosba wprost"),

    # --- prośba o polecenie: PEŁNOPRAWNY LEAD, nie spam ---
    # Autor szuka wykonawcy, czyli daje najczystszy możliwy sygnał zakupowy.
    (r"polec(icie|acie|isz|i mi|a mi)", 0, "prosba o polecenie"),
    (r"znacie (kogos|jakas lawete|firme|dobra lawete)", 0, "prosba o polecenie"),
    (r"kogo (polecacie|polecicie)", 0, "prosba o polecenie"),
    (r"szukam firmy", 0, "prosba o polecenie"),

    # --- transport planowany: GŁÓWNY PRODUKT operatora ---
    # Trasy międzynarodowe zestawem B+E, do trzech aut naraz. To NIE jest wyścig
    # na minuty: "kupiłem auto w Niemczech, kto przywiezie" żyje dniami.
    (r"kupil[ae]m (auto|samochod|pojazd|osobowke|busa)", 0, "transport planowany"),
    (r"kupione auto", 0, "transport planowany"),
    (r"odbior (auta|samochodu)", 0, "transport planowany"),
    (r"sprowadz(enie|am|ilem|ilam) (auta|auto|samochod|samochodu)", 0, "transport planowany"),
    (r"(z|spod) komisu", 0, "transport planowany"),
    (r"z aukcji", 0, "transport planowany"),
    (r"z autohausu", 0, "transport planowany"),
    (r"od dealera", 0, "transport planowany"),
    (r"z (niemiec|holandii|belgii|francji|wloch|austrii|czech|danii|szwecji|hiszpanii)",
     0, "transport planowany"),
    (r"w (niemczech|holandii|belgii|francji|wloszech|austrii)", 0, "transport planowany"),
    (r"zza granicy", 0, "transport planowany"),
    (r"przywiezc auto", 0, "transport planowany"),
    (r"przywoz auta", 0, "transport planowany"),
    (r"przetransportowa(c|nie|nia|niu)", 0, "transport planowany"),
    (r"na lawecie", 0, "transport planowany"),
    (r"laweta na ", 0, "transport planowany"),
    (r"wolne miejsce na lawecie", 0, "transport planowany"),
    (r"szukam miejsca na lawecie", 0, "transport planowany"),
    (r"doladunek", 0, "transport planowany"),
    (r"kurs powrotny", 0, "transport planowany"),
    (r"wracam pusty", 0, "transport planowany"),
    (r"zlec(e|am|enie) (transport|przewoz|kurs|transportu|przewozu)", 0, "transport planowany"),
    (r"kto jedzie (z|do|w strone|na)", 0, "transport planowany"),
    (r"kto bedzie w okolicy", 0, "transport planowany"),
    (r"transport [23] aut", 0, "transport planowany"),
    (r"(dwa|trzy) auta", 0, "transport planowany"),

    # --- zdarzenie drogowe w wariancie TRANSPORTOWYM ---
    # Frazy awaryjne zostają. Auto, które nie jeździ, trzeba przewieźć na lawecie
    # — czy stoi pod Krosnem, czy pod Kolonią, decyduje KIEROWCA po obejrzeniu
    # trasy, a nie bramka po słowie kluczowym.
    (r"auto po szkodzie", 0, "zdarzenie drogowe"),
    (r"po szkodzie", 0, "zdarzenie drogowe"),
    (r"(auto )?powypadkow[ey]", 0, "zdarzenie drogowe"),
    (r"(auto |pojazd |samochod )?niejezdzac[ey]", 0, "zdarzenie drogowe"),
    (r"(auto )?nie na chodzie", 0, "zdarzenie drogowe"),
    (r"uszkodzone auto do przewiezienia", 0, "zdarzenie drogowe"),
    (r"nie (odpala|odpali|pali|zapala|chce odpalic)", 0, "zdarzenie drogowe"),
    (r"zdech(l|la|lo|l mi)", 0, "zdarzenie drogowe"),
]

# ---------------------------------------------------------------------------
# WARSTWA 3 — TWARDE ODRZUCENIE
#
# Sprawdzana DOPIERO gdy warstwa 2 milczała. Każdy wzorzec musi być jednoznaczny
# — przy najmniejszej dwuznaczności trafia do warstwy 4 z ujemną wagą, a nie tutaj.
#
# Dwa wzorce z listy zadania są ZAWĘŻONE z tego samego powodu co w warstwie 1:
#
#   • "oferuje"  — "oferuję 500 zł za przewóz auta z Kolonii" to KLIENT
#     z gotówką, nie konkurencja. Zawężone do "oferuję usługi/transport/lawetę".
#   • "szukam kierowcy" ZOSTAJE w rekrutacji, ale ratuje je warstwa 2: "szukam
#     kierowcy, który przywiezie auto z Niemiec" ma "z niemiec" i przechodzi
#     wcześniej. To jest dokładnie ta sytuacja, dla której istnieje kolejność warstw.
#
# NIE odrzucamy na "faktura vat" ani "na fakture" — to najczęściej firma, która
# ZAMAWIA kurs i potrzebuje dokumentu. Dobry klient B2B, nie spam.
# NIE odrzucamy na "kat. c" ani "kat. b" — właściciel ciężarówki opisujący swój
# unieruchomiony pojazd używa dokładnie tych słów.
# ---------------------------------------------------------------------------
ODRZUCENIE: list[tuple[str, int, str]] = [
    # --- autopromocja konkurencji (wymaga sygnału OFERTY, nie słowa branżowego) ---
    (r"laweta 24/7", 0, "autopromocja"),
    (r"laweta 24h", 0, "autopromocja"),
    (r"laweta calodobowo", 0, "autopromocja"),
    (r"pomoc drogowa (calodobowo|24/7|24h)", 0, "autopromocja"),
    (r"uslugi lawetowe", 0, "autopromocja"),
    (r"oferuje (uslugi|usluge|pomoc drogowa|lawete|holowanie|transport|"
     r"przewoz|wynajem)", 0, "autopromocja"),
    (r"w mojej ofercie", 0, "autopromocja"),
    (r"zapraszam do (kontaktu|wspolpracy)", 0, "autopromocja"),
    (r"polecam (swoje uslugi|sie)", 0, "autopromocja"),
    (r"(konkurencyjne|atrakcyjne|najlepsze) ceny", 0, "autopromocja"),
    (r"wystawiam(y)? fakture", 0, "autopromocja"),
    (r"tanio i solidnie", 0, "autopromocja"),
    (r"szybko i tanio", 0, "autopromocja"),

    # --- sprzedaż sprzętu (wymaga sprzedaży ORAZ przedmiotu z branży) ---
    (r"sprzedam (lawete|autolawete|najazd|najazdy|wciagarke|wyciagarke|"
     r"przyczepe|przyczepke)", 0, "sprzedaz sprzetu"),
    (r"na sprzedaz laweta", 0, "sprzedaz sprzetu"),

    # --- ogłoszenia o pracę (wymaga sygnału REKRUTACJI, nie kategorii prawa jazdy) ---
    (r"zatrudni(e|my|am|amy)", 0, "ogloszenie o pracy"),
    (r"praca dla kierowcy", 0, "ogloszenie o pracy"),
    (r"szukam kierowcy", 0, "ogloszenie o pracy"),
    (r"poszukuje kierowcy", 0, "ogloszenie o pracy"),
    (r"cv na (maila|priv|pw)", 0, "ogloszenie o pracy"),
    (r"oferta pracy", 0, "ogloszenie o pracy"),
    (r"przyjme do pracy", 0, "ogloszenie o pracy"),
    (r"dam prace", 0, "ogloszenie o pracy"),
]

# Krótsze niż to (po strip, na treści ORYGINALNEJ) nie niesie nic, co dałoby się
# klasyfikować. Sprawdzane w warstwie 3, więc PO przepuszczeniu — dzięki temu
# "szukam lawety" (13 znaków) przechodzi.
MIN_DLUGOSC = 15

# ---------------------------------------------------------------------------
# WARSTWA 4 — PUNKTACJA
#
# Dla wszystkiego, co przeszło przez trzy warstwy bez rozstrzygnięcia. Suma wag
# zamiast sztywnej reguły "dwa kubełki" — próg da się wtedy kalibrować JEDNĄ
# liczbą w .env zamiast przepisywaniem reguł, a raport z trybu cienia pokazuje
# rozkład punktów, z którego ten próg się odczytuje.
# ---------------------------------------------------------------------------
PUNKTACJA: list[tuple[str, int, str]] = [
    # --- POJAZD (+2) ---
    (r"auto", 2, "POJAZD"),
    (r"samochod|samochodu|samochody", 2, "POJAZD"),
    (r"osobowka|osobowke", 2, "POJAZD"),
    (r"bus|busa|busem", 2, "POJAZD"),
    (r"dostawczak|dostawczy|dostawcze", 2, "POJAZD"),
    (r"motor|motocykl|motocykla", 2, "POJAZD"),
    (r"skuter|skutera", 2, "POJAZD"),
    (r"quad|quada", 2, "POJAZD"),
    (r"jednoslad|jednosladu", 2, "POJAZD"),
    (r"przyczep(a|e|y|ke)", 2, "POJAZD"),
    (r"kamper|kampera", 2, "POJAZD"),
    (r"ciagnik|ciagnika", 2, "POJAZD"),
    (r"traktor|traktora", 2, "POJAZD"),
    (r"koparka|koparke|koparki", 2, "POJAZD"),
    (r"wozek widlowy", 2, "POJAZD"),
    (r"maszyn(a|e|y)", 2, "POJAZD"),
    (r"ciezarowk(a|e|i)", 2, "POJAZD"),
    (r"tir|tira", 2, "POJAZD"),
    # Marki popularne w treściach — post częściej mówi "golf nie odpala" niż
    # "samochód osobowy nie odpala".
    (r"golf|golfa|golfem", 2, "POJAZD"),
    (r"passat|passata", 2, "POJAZD"),
    (r"octavia|octavie", 2, "POJAZD"),
    (r"astra|astre", 2, "POJAZD"),
    (r"transit|transita", 2, "POJAZD"),
    (r"sprinter|sprintera", 2, "POJAZD"),
    (r"ducato", 2, "POJAZD"),

    # --- PROBLEM (+3) ---
    (r"awari(a|i|e)", 3, "PROBLEM"),
    (r"zepsu(ty|ta|te|lo|la|l sie|l mi sie)", 3, "PROBLEM"),
    (r"unieruchomion[yae]", 3, "PROBLEM"),
    (r"nie (jezdzi|dziala|rusza|ruszy|chce jechac)", 3, "PROBLEM"),
    (r"skrzynia|skrzyni", 3, "PROBLEM"),
    (r"sprzegl(o|a)", 3, "PROBLEM"),
    (r"rozrzad|rozrzadu", 3, "PROBLEM"),
    (r"akumulator|akumulatora", 3, "PROBLEM"),
    (r"alternator|alternatora", 3, "PROBLEM"),
    (r"turbin(a|y|e)", 3, "PROBLEM"),
    (r"silnik|silnika", 3, "PROBLEM"),
    (r"warsztat|warsztatu", 3, "PROBLEM"),
    (r"mechanik|mechanika", 3, "PROBLEM"),
    (r"serwis|serwisu", 3, "PROBLEM"),
    (r"do naprawy", 3, "PROBLEM"),
    (r"po remoncie", 3, "PROBLEM"),
    # Świadomie BEZ "wypadek": "uwaga korek na obwodnicy, wypadek" to ostrzeżenie
    # dla kierowców, a nie zlecenie, i pojawia się w tych grupach codziennie.
    (r"stan(alem|elam|al|ela) ", 3, "PROBLEM"),
    (r"wjecha(lem|lam|l|la) ", 3, "PROBLEM"),
    (r"dachowa(lem|l|lo)", 3, "PROBLEM"),
    (r"urwa(lo|l)", 3, "PROBLEM"),
    (r"flak|kapec", 3, "PROBLEM"),
    (r"przebit(a|e|y)", 3, "PROBLEM"),
    (r"gasnie", 3, "PROBLEM"),
    (r"wyciek", 3, "PROBLEM"),

    # --- AKCJA (+3) ---
    (r"zabra(c|nie|nia|niu)", 3, "AKCJA"),
    (r"zawiez(c|ienie|ienia|ieniu)", 3, "AKCJA"),
    (r"przewiez(c|ienie|ienia|ieniu)|przewoz(u)?", 3, "AKCJA"),
    (r"podwiez(c|ienie|ienia)", 3, "AKCJA"),
    (r"sciagn(ac|iecie|iecia|ieciu)", 3, "AKCJA"),
    (r"przetransportowa(c|nie|nia|niu)", 3, "AKCJA"),
    (r"dostarcz(yc|enie|enia)", 3, "AKCJA"),
    (r"odebra(c|nie|nia)|odbior(u)?", 3, "AKCJA"),
    (r"holowa(nie|nia|c)", 3, "AKCJA"),
    (r"przeholowa(c|nie|nia)", 3, "AKCJA"),
    (r"wyciagn(ac|iecie|iecia|ieciu)", 3, "AKCJA"),
    (r"zaladowa(c|nie|nia)", 3, "AKCJA"),
    (r"transport|transportu", 3, "AKCJA"),

    # --- MIEJSCE (+2) ---
    (r"na dk ?[0-9]*", 2, "MIEJSCE"),
    (r"na s ?[0-9]+", 2, "MIEJSCE"),
    (r"na a ?[0-9]+", 2, "MIEJSCE"),
    (r"na obwodnicy", 2, "MIEJSCE"),
    (r"krajowk(a|e|i)|krajowce", 2, "MIEJSCE"),
    (r"pobocz(e|u)", 2, "MIEJSCE"),
    (r"parking|parkingu", 2, "MIEJSCE"),
    (r"stacj(a|i) paliw", 2, "MIEJSCE"),
    (r"mop", 2, "MIEJSCE"),
    (r"(przy|na) drodze", 2, "MIEJSCE"),
    (r"blokuje", 2, "MIEJSCE"),
    (r"(w|do|z) row(ie|u)", 2, "MIEJSCE"),
    (r"na (autostradzie|ekspresowce|moscie|skarpie)", 2, "MIEJSCE"),
    (r"(w|z|do|na) (polu|pola|pole)", 2, "MIEJSCE"),

    # --- TRASA (+3) ---
    # "z Krosna do Rzeszowa" — para miejscowości jest w tych grupach niemal
    # wyłącznie opisem kursu. Heurystyka celowo prosta (nie znamy słownika
    # miejscowości), stąd waga +3, a nie twarde przepuszczenie.
    (r"z [a-z]{3,} do [a-z]{3,}", 3, "TRASA"),

    # --- PILNOŚĆ (+3) ---
    (r"pilne|pilnie", 3, "PILNOSC"),
    (r"na juz", 3, "PILNOSC"),
    (r"natychmiast", 3, "PILNOSC"),
    (r"sos", 3, "PILNOSC"),
    (r"ratunku", 3, "PILNOSC"),
    (r"awaryjnie", 3, "PILNOSC"),
    (r"jak najszybciej|asap", 3, "PILNOSC"),
    (r"na dzis", 3, "PILNOSC"),
    (r"teraz", 3, "PILNOSC"),

    # --- KONTAKT (+1) ---
    (r"dzwoni(c|cie)|dzwoncie|zadzwon(cie)?", 1, "KONTAKT"),
    (r"tel", 1, "KONTAKT"),
    (r"telefon|telefonu", 1, "KONTAKT"),
    (r"kontakt na pw", 1, "KONTAKT"),
    (r"pw|priv", 1, "KONTAKT"),
]

# Numer telefonu w treści — dziewięć cyfr z opcjonalnymi separatorami i opcjonalnym
# prefiksem +48. Osobno od tabeli, bo to jedyny wzorzec, który nie jest frazą.
_TELEFON = re.compile(r"(?<![0-9])(?:\+?48[\s.\-]?)?(?:[0-9][\s.\-]?){8}[0-9](?![0-9])")

# HAMULCE bezwarunkowe — dla przypadków zbyt dwuznacznych na warstwę 3.
HAMULCE: list[tuple[str, int, str]] = [
    (r"(pytam )?z ciekawosci", -3, "HAMULEC"),
    (r"(pytanie|pytam) (teoretyczne|teoretycznie)", -3, "HAMULEC"),
    (r"czysto teoretycznie", -3, "HAMULEC"),
    (r"(znajomy|kolega) pytal", -2, "HAMULEC"),
    (r"pytam za (kolege|znajomego)", -2, "HAMULEC"),
]

# HAMULCE WARUNKOWE — działają TYLKO wtedy, gdy w treści nie było sygnału
# z podanej kategorii. Osobna tabela, bo warunek jest częścią wzorca, a nie
# jego wagą; wciśnięcie go w trójkę (wzorzec, waga, etykieta) wymagałoby
# kodowania go w stringu.
#
#   • pytanie o cenę bez ŻADNEJ czynności to sondaż rynku ("ile bierzecie za
#     holowanie do 50 km?"), ale "ile kosztuje żeby ZABRAĆ auto z parkingu" to
#     klient, który już wie, czego chce — i skasowanie go kosztuje 300 zł;
#   • "wczoraj" / "w zeszłym tygodniu" osłabia post RELACJONUJĄCY zdarzenie,
#     ale nie post, który jednocześnie krzyczy "pilne".
HAMULCE_WARUNKOWE: list[tuple[str, int, str, str]] = [
    (r"ile (kosztuje|bierzecie|za|by kosztowalo)", -4, "HAMULEC", "AKCJA"),
    (r"jak(a|ie) cen(a|y)", -4, "HAMULEC", "AKCJA"),
    (r"wczoraj", -2, "HAMULEC", "PILNOSC"),
    (r"w zeszlym tygodniu", -2, "HAMULEC", "PILNOSC"),
    (r"kilka dni temu", -2, "HAMULEC", "PILNOSC"),
]

# ===========================================================================
# WIELOJĘZYCZNOŚĆ
#
# Wszystko powyżej to słownik POLSKI. Poniżej ta sama struktura warstw i te same
# wagi dla niemieckiego oraz czesko-słowackiego.
#
# DLACZEGO W OGÓLE: bramka jednojęzyczna jest cicha i śmiertelna. Niemieckie
# „Suche Autotransport von München nach Krakau, Fahrzeug fährt nicht" to wzorcowe
# zlecenie transportowe — i przy polskich wzorcach nie trafia ANI JEDNEGO, dostaje
# zero punktów i wylatuje. W logach wygląda dokładnie jak odrzucona reklama felg,
# więc nikt się o tym nie dowie. To jest ta sama asymetria kosztów, na której stoi
# cały ten moduł, tylko w wariancie, którego nie widać w polskim korpusie.
#
# CZESKI I SŁOWACKI DZIELĄ JEDEN SŁOWNIK, z wariantami obu języków w środku.
# Rozdzielenie ich dałoby dwie listy różniące się w połowie pozycji jedną literą
# („odtahovka" / „odťahovka"), czyli dwa miejsca do zapomnienia przy każdej
# zmianie. Po normalizacji część wariantów i tak się ZLEWA (obie formy dają
# „odtahov"), a część nie („hledam" / „hladam") — dlatego lista nie jest
# symetryczna i nie jest to niedopatrzenie.
#
# Detekcja języka ROZRÓŻNIA cs od sk mimo wspólnego słownika, bo znacznik nie
# służy filtrowaniu — służy człowiekowi, który ma oddzwonić.
#
# WZORCE PISZEMY W FORMIE ZNORMALIZOWANEJ, tak samo jak polskie: bez umlautów,
# haczków i długości („fährt nicht" -> „fahrt nicht", „potřebuji" -> „potrebuji").
# Normalizacja zbija je wszystkie, więc wzorzec z diakrytykiem nie trafiłby nigdy.
# ===========================================================================


@dataclass(frozen=True)
class Slownik:
    """Komplet wzorców dla jednego języka — ta sama struktura we wszystkich.

    `jezyk` jest etykietą słownika, nie zawsze językiem posta: słownik
    czesko-słowacki obsługuje dwa języki, a znacznik w wyniku bierze się
    z detekcji, nie stąd.
    """

    jezyk: str
    wygaszenie: list
    przepuszczenie: list
    odrzucenie: list
    punktacja: list
    hamulce: list
    hamulce_warunkowe: list


# ---------------------------------------------------------------------------
# NIEMIECKI
#
# Rynek transportowy, nie awaryjny: niemiecka grupa dowozi głównie „przywieź mi
# auto z Niemiec do Polski", a nie „stoję na poboczu". Dlatego warstwa
# przepuszczenia jest tu szersza po stronie przewozu niż po stronie awarii.
# ---------------------------------------------------------------------------
WYGASZENIE_DE: list[tuple[str, int, str]] = [
    (r"(hat sich )?erledigt", 0, "wygaszone"),
    (r"nicht mehr aktuell", 0, "wygaszone"),
    (r"schon geregelt", 0, "wygaszone"),
    (r"brauche (es )?nicht mehr", 0, "wygaszone"),
    (r"hat sich gefunden", 0, "wygaszone"),
    # „gefunden" SAMO jest tu ZAWĘŻONE — dokładnie z tego powodu, dla którego
    # zawężono polskie „znalazlem": „habe niemanden gefunden" to POCZĄTEK
    # zlecenia, a nie jego koniec, i wzorzec w formie dosłownej kasowałby je
    # razem z tymi prawdziwie wygaszonymi.
    (r"(jemanden|jemand|eine firma|hilfe|einen abschleppdienst) gefunden",
     0, "wygaszone"),
]

PRZEPUSZCZENIE_DE: list[tuple[str, int, str]] = [
    # --- prośba wprost ---
    (r"suche (einen )?autotransport", 0, "prosba wprost"),
    (r"suche (einen )?abschlepp[a-z]*", 0, "prosba wprost"),
    (r"brauche (einen |eine )?abschlepp[a-z]*", 0, "prosba wprost"),
    (r"brauche (einen )?transport", 0, "prosba wprost"),
    (r"wer kann (mein |mir |mein auto |mein fahrzeug )?abschleppen", 0, "prosba wprost"),
    (r"wer kann helfen", 0, "prosba wprost"),
    (r"kann (mir )?jemand helfen", 0, "prosba wprost"),
    (r"wer holt", 0, "prosba wprost"),
    (r"wer hat platz", 0, "prosba wprost"),
    (r"(fahrzeug|auto|wagen|pkw) transportier[a-z]*", 0, "prosba wprost"),
    (r"(fahrzeug|auto|wagen) uberfuhr[a-z]*", 0, "prosba wprost"),
    # SAMO „abschleppen" / „abschleppdienst" NIE jest tu wzorcem — i to jest ta
    # sama decyzja, dla której polski słownik nie ma w tej warstwie samego
    # „holowanie". Warstwa 2 bije warstwę 3, więc bezokolicznik usługi wpuszczałby
    # KAŻDĄ reklamę konkurencji („Wir bieten Abschleppdienst, günstige Preise").
    # Nazwa usługi żyje w PUNKTACJI jako AKCJA (+3) i tam robi swoją robotę.

    # --- transport planowany (główny produkt na tym rynku) ---
    (r"nach polen (bringen|transportieren|uberfuhren)", 0, "transport planowany"),
    (r"auto gekauft", 0, "transport planowany"),
    (r"gekauftes auto", 0, "transport planowany"),
    (r"vom (handler|autohaus|hof)", 0, "transport planowany"),
    (r"aus (deutschland|holland|belgien|frankreich|italien|osterreich)",
     0, "transport planowany"),
    (r"ruckfahrt", 0, "transport planowany"),
    (r"noch platz (auf|frei)", 0, "transport planowany"),

    # --- zdarzenie / awaria ---
    (r"springt nicht (mehr )?an", 0, "zdarzenie drogowe"),
    (r"startet nicht (mehr)?", 0, "zdarzenie drogowe"),
    (r"motor kaputt", 0, "zdarzenie drogowe"),
    (r"getriebe kaputt", 0, "zdarzenie drogowe"),
    (r"panne", 0, "zdarzenie drogowe"),
    (r"liegen ?geblieben", 0, "zdarzenie drogowe"),
    (r"fahrt nicht (mehr)?", 0, "zdarzenie drogowe"),
    (r"nicht (fahrbereit|fahrtuchtig)", 0, "zdarzenie drogowe"),
    (r"bleibt stehen", 0, "zdarzenie drogowe"),
    (r"unfall", 0, "zdarzenie drogowe"),
    (r"totalschaden", 0, "zdarzenie drogowe"),
]

ODRZUCENIE_DE: list[tuple[str, int, str]] = [
    # „biete" jest ZAWĘŻONE tak samo jak polskie „oferuje": „biete 300 euro fur
    # den transport" to KLIENT Z GOTÓWKĄ, nie konkurencja.
    (r"biete (abschlepp|transport|uberfuhr|meine dienste|service|hilfe an)",
     0, "autopromocja"),
    (r"wir bieten", 0, "autopromocja"),
    (r"wir ubernehmen", 0, "autopromocja"),
    (r"(gunstige|faire|beste) preise", 0, "autopromocja"),
    (r"rund um die uhr", 0, "autopromocja"),
    (r"24/7 service", 0, "autopromocja"),
    (r"verkaufe (anhanger|abschleppwagen|autotransporter|trailer|auflieger)",
     0, "sprzedaz sprzetu"),
    (r"zu verkaufen: (anhanger|abschleppwagen|trailer)", 0, "sprzedaz sprzetu"),
    (r"suche fahrer", 0, "ogloszenie o pracy"),
    (r"wir suchen (einen )?fahrer", 0, "ogloszenie o pracy"),
    (r"stellenangebot", 0, "ogloszenie o pracy"),
    (r"jobangebot", 0, "ogloszenie o pracy"),
    (r"bewerbung an", 0, "ogloszenie o pracy"),
]

PUNKTACJA_DE: list[tuple[str, int, str]] = [
    # --- POJAZD (+2) ---
    (r"auto", 2, "POJAZD"),
    (r"wagen", 2, "POJAZD"),
    (r"fahrzeug", 2, "POJAZD"),
    (r"pkw", 2, "POJAZD"),
    (r"transporter", 2, "POJAZD"),
    (r"motorrad", 2, "POJAZD"),
    (r"roller", 2, "POJAZD"),
    (r"anhanger", 2, "POJAZD"),
    (r"wohnmobil", 2, "POJAZD"),
    (r"oldtimer", 2, "POJAZD"),

    # --- PROBLEM (+3) ---
    (r"defekt", 3, "PROBLEM"),
    (r"kaputt", 3, "PROBLEM"),
    (r"motorschaden", 3, "PROBLEM"),
    (r"getriebeschaden", 3, "PROBLEM"),
    (r"kupplung", 3, "PROBLEM"),
    (r"batterie", 3, "PROBLEM"),
    (r"lichtmaschine", 3, "PROBLEM"),
    (r"turbolader", 3, "PROBLEM"),
    (r"werkstatt", 3, "PROBLEM"),
    (r"mechaniker", 3, "PROBLEM"),
    (r"reparatur", 3, "PROBLEM"),
    (r"tuv", 3, "PROBLEM"),

    # --- AKCJA (+3) ---
    (r"transport(ieren)?", 3, "AKCJA"),
    (r"autotransport", 3, "AKCJA"),
    (r"abschlepp[a-z]*", 3, "AKCJA"),
    (r"uberfuhr[a-z]*", 3, "AKCJA"),
    (r"abhol(en|ung)", 3, "AKCJA"),
    (r"bringen", 3, "AKCJA"),
    (r"liefern|lieferung", 3, "AKCJA"),
    (r"verladen", 3, "AKCJA"),

    # --- MIEJSCE (+2) ---
    (r"autobahn", 2, "MIEJSCE"),
    (r"raststatte", 2, "MIEJSCE"),
    (r"parkplatz", 2, "MIEJSCE"),
    (r"standstreifen", 2, "MIEJSCE"),
    (r"grenze", 2, "MIEJSCE"),
    (r"bab ?[0-9]*", 2, "MIEJSCE"),

    # --- TRASA (+3) ---
    (r"nach (polen|deutschland|tschechien|osterreich)", 3, "TRASA"),
    (r"von (deutschland|polen|holland|belgien)", 3, "TRASA"),
    (r"richtung", 3, "TRASA"),

    # --- PILNOSC (+3) ---
    (r"dringend", 3, "PILNOSC"),
    (r"sofort", 3, "PILNOSC"),
    (r"notfall", 3, "PILNOSC"),
    (r"eilig", 3, "PILNOSC"),
    (r"heute", 3, "PILNOSC"),
    (r"jetzt", 3, "PILNOSC"),

    # --- KONTAKT (+1) ---
    (r"telefon", 1, "KONTAKT"),
    (r"tel", 1, "KONTAKT"),
    (r"anrufen", 1, "KONTAKT"),
    (r"whatsapp", 1, "KONTAKT"),
    (r"pn", 1, "KONTAKT"),
]

HAMULCE_DE: list[tuple[str, int, str]] = [
    (r"nur aus interesse", -3, "HAMULEC"),
    (r"rein theoretisch", -3, "HAMULEC"),
    (r"(ein freund|ein kollege) fragt", -2, "HAMULEC"),
]

HAMULCE_WARUNKOWE_DE: list[tuple[str, int, str, str]] = [
    (r"was kostet", -4, "HAMULEC", "AKCJA"),
    (r"wie viel kostet", -4, "HAMULEC", "AKCJA"),
    (r"gestern", -2, "HAMULEC", "PILNOSC"),
    (r"letzte woche", -2, "HAMULEC", "PILNOSC"),
]


# ---------------------------------------------------------------------------
# CZESKI I SŁOWACKI — jeden słownik, warianty obu języków obok siebie.
# ---------------------------------------------------------------------------
WYGASZENIE_CS_SK: list[tuple[str, int, str]] = [
    (r"vyresen[oy]", 0, "wygaszone"),      # vyřešeno
    (r"vyriesen[ey]", 0, "wygaszone"),     # vyriešené
    (r"neaktualn[iey]", 0, "wygaszone"),   # neaktuální / neaktuálne
    (r"uz nepotrebuj[ie]", 0, "wygaszone"),
    (r"zrusen[oey]", 0, "wygaszone"),
    (r"uz (mam|je) (vyreseno|vyriesene|to)", 0, "wygaszone"),
    # Zawężone tak samo jak polskie „znalazlem" i niemieckie „gefunden".
    (r"(nasel jsem|nasiel som) (uz )?(nekoho|niekoho|odtah|firmu|pomoc)",
     0, "wygaszone"),
]

PRZEPUSZCZENIE_CS_SK: list[tuple[str, int, str]] = [
    # --- prośba wprost (formy, które się NIE zlewają po normalizacji) ---
    (r"hledam odtah[a-z]*", 0, "prosba wprost"),
    (r"hladam odtah[a-z]*", 0, "prosba wprost"),
    (r"potrebuji odtah[a-z]*", 0, "prosba wprost"),
    (r"potrebuju odtah[a-z]*", 0, "prosba wprost"),
    (r"potrebujem odtah[a-z]*", 0, "prosba wprost"),
    (r"prosim o odtah", 0, "prosba wprost"),
    (r"kdo pomuze", 0, "prosba wprost"),
    (r"kto pomoze", 0, "prosba wprost"),
    (r"(kdo|kto) ma (volno|volny cas)", 0, "prosba wprost"),
    (r"(kdo|kto) (privezie|priveze|odveze|odvezie)", 0, "prosba wprost"),
    # --- czynność Z DOPEŁNIENIEM (po normalizacji wspólna dla obu języków) ---
    # Samo „odtahov*" tu NIE stoi z tego samego powodu co niemieckie
    # „abschleppen": warstwa 2 bije warstwę 3, więc nazwa usługi wpuszczałaby
    # reklamy („Nabízíme odtahovou službu, výhodné ceny"). Nazwa żyje
    # w PUNKTACJI jako AKCJA (+3).
    (r"odtah aut[a-z]*", 0, "prosba wprost"),
    (r"prevoz aut[a-z]*", 0, "prosba wprost"),
    (r"preprava aut[a-z]*", 0, "prosba wprost"),
    (r"prevezt|previezt", 0, "prosba wprost"),
    # --- transport planowany ---
    (r"koupil jsem auto", 0, "transport planowany"),
    (r"kupil som auto", 0, "transport planowany"),
    (r"z (nemecka|rakouska|holandska|nemecko)", 0, "transport planowany"),
    (r"z autobazar[a-z]*", 0, "transport planowany"),
    # --- awaria / zdarzenie ---
    (r"nenastartuje", 0, "zdarzenie drogowe"),
    (r"nestartuje", 0, "zdarzenie drogowe"),
    (r"nejde nastartovat", 0, "zdarzenie drogowe"),
    (r"nepojizdn[a-z]*", 0, "zdarzenie drogowe"),
    (r"nepojazdn[a-z]*", 0, "zdarzenie drogowe"),
    (r"zustal jsem stat", 0, "zdarzenie drogowe"),
    (r"zostal som stat", 0, "zdarzenie drogowe"),
    (r"nehod[auy]", 0, "zdarzenie drogowe"),
    (r"havari[aey]", 0, "zdarzenie drogowe"),
]

ODRZUCENIE_CS_SK: list[tuple[str, int, str]] = [
    # „nabizim/ponukam" zawężone jak polskie „oferuje" — sama oferta pieniędzy
    # to klient, nie konkurencja.
    (r"(nabizim|nabizime|ponukam|ponukame) (odtah[a-z]*|prepravu|prevoz|"
     r"sluzby|nase sluzby)", 0, "autopromocja"),
    (r"(vyhodne|levne|lacne|najlepsie) ceny", 0, "autopromocja"),
    (r"nonstop (odtah|servis)", 0, "autopromocja"),
    (r"(prodam|predam) (odtahovku|prives|privesny vozik|navjes)",
     0, "sprzedaz sprzetu"),
    (r"(hledame|hladame) (ridice|vodica|vodicov)", 0, "ogloszenie o pracy"),
    (r"(prijmeme|primeme) (ridice|vodica)", 0, "ogloszenie o pracy"),
    (r"(nabidka|ponuka) prace", 0, "ogloszenie o pracy"),
]

PUNKTACJA_CS_SK: list[tuple[str, int, str]] = [
    # --- POJAZD (+2) ---
    (r"auto", 2, "POJAZD"),
    (r"vozidl[oauy]", 2, "POJAZD"),
    (r"vuz|voz", 2, "POJAZD"),
    (r"dodavk[auy]", 2, "POJAZD"),
    (r"motork[auy]", 2, "POJAZD"),
    (r"prives|privesu", 2, "POJAZD"),
    (r"karavan", 2, "POJAZD"),

    # --- PROBLEM (+3) ---
    (r"poruch[auy]", 3, "PROBLEM"),
    (r"rozbit[eyaou]", 3, "PROBLEM"),
    (r"pokazen[eyao]", 3, "PROBLEM"),
    (r"motor", 3, "PROBLEM"),
    (r"prevodovk[auy]", 3, "PROBLEM"),
    (r"spojk[auy]", 3, "PROBLEM"),
    (r"baterk[auy]|bateri[ei]", 3, "PROBLEM"),
    (r"alternator", 3, "PROBLEM"),
    (r"servis[uy]?", 3, "PROBLEM"),
    (r"mechanik[auy]?", 3, "PROBLEM"),
    (r"oprav[auy]", 3, "PROBLEM"),

    # --- AKCJA (+3) ---
    (r"odtah[a-z]*", 3, "AKCJA"),
    (r"prevoz|prepravu?", 3, "AKCJA"),
    (r"odvez[a-z]*", 3, "AKCJA"),
    (r"nalozit|nalozenie", 3, "AKCJA"),
    (r"dopravit|dopravu", 3, "AKCJA"),

    # --- MIEJSCE (+2) ---
    (r"dalnic[ie]|dialnic[ie]", 2, "MIEJSCE"),
    (r"krajnic[ie]", 2, "MIEJSCE"),
    (r"parkovist[ie]|parkovisk[ou]", 2, "MIEJSCE"),
    (r"benzin[a-z]*", 2, "MIEJSCE"),
    (r"hranic[ie]", 2, "MIEJSCE"),
    (r"d ?[0-9]{1,2}", 2, "MIEJSCE"),

    # --- TRASA (+3) ---
    (r"do (polska|polski|nemecka|rakouska)", 3, "TRASA"),
    (r"smer|smerom", 3, "TRASA"),

    # --- PILNOSC (+3) ---
    (r"nutne|nutno", 3, "PILNOSC"),
    (r"surne|surn[aeo]", 3, "PILNOSC"),
    (r"hned", 3, "PILNOSC"),
    (r"dnes", 3, "PILNOSC"),
    (r"co nejdriv|co najskor", 3, "PILNOSC"),

    # --- KONTAKT (+1) ---
    (r"telefon", 1, "KONTAKT"),
    (r"tel", 1, "KONTAKT"),
    (r"zavolat|zavolejte", 1, "KONTAKT"),
    (r"sms", 1, "KONTAKT"),
]

HAMULCE_CS_SK: list[tuple[str, int, str]] = [
    (r"jen ze zvedavosti|len zo zvedavosti", -3, "HAMULEC"),
    (r"cist[eo] teoreticky", -3, "HAMULEC"),   # čistě (cz) / čisto (sk)
    (r"(kamarad|kolega) se pta|(kamarat|kolega) sa pyta", -2, "HAMULEC"),
]

HAMULCE_WARUNKOWE_CS_SK: list[tuple[str, int, str, str]] = [
    (r"kolik (to )?stoji", -4, "HAMULEC", "AKCJA"),
    (r"kolko (to )?stoji", -4, "HAMULEC", "AKCJA"),
    (r"jaka je cena", -4, "HAMULEC", "AKCJA"),
    (r"vcera", -2, "HAMULEC", "PILNOSC"),
    (r"minuly tyden|minuly tyzden", -2, "HAMULEC", "PILNOSC"),
]


def _zbuduj_slownik(jezyk: str, wygaszenie, przepuszczenie, odrzucenie,
                    punktacja, hamulce, hamulce_warunkowe) -> Slownik:
    """Kompilacja RAZ, przy imporcie — bramka stoi na ścieżce każdego posta."""
    return Slownik(
        jezyk=jezyk,
        wygaszenie=_skompiluj_liste(wygaszenie),
        przepuszczenie=_skompiluj_liste(przepuszczenie),
        odrzucenie=_skompiluj_liste(odrzucenie),
        punktacja=_skompiluj_liste(punktacja),
        hamulce=_skompiluj_liste(hamulce),
        hamulce_warunkowe=[(_skompiluj(w), waga, etykieta, kat)
                           for w, waga, etykieta, kat in hamulce_warunkowe],
    )


_PL = _zbuduj_slownik("pl", WYGASZENIE, PRZEPUSZCZENIE, ODRZUCENIE,
                      PUNKTACJA, HAMULCE, HAMULCE_WARUNKOWE)
_DE = _zbuduj_slownik("de", WYGASZENIE_DE, PRZEPUSZCZENIE_DE, ODRZUCENIE_DE,
                      PUNKTACJA_DE, HAMULCE_DE, HAMULCE_WARUNKOWE_DE)
_CS_SK = _zbuduj_slownik("cs", WYGASZENIE_CS_SK, PRZEPUSZCZENIE_CS_SK,
                         ODRZUCENIE_CS_SK, PUNKTACJA_CS_SK, HAMULCE_CS_SK,
                         HAMULCE_WARUNKOWE_CS_SK)

# Klucze to dwuliterowe znaczniki języka POSTA; „cs" i „sk" celowo wskazują ten
# SAM obiekt. Kod, który iteruje po słownikach, musi więc odróżniać „ile jest
# kluczy" od „ile jest różnych słowników" (patrz `_rozne_slowniki`).
SLOWNIKI: dict[str, Slownik] = {
    "pl": _PL,
    "de": _DE,
    "cs": _CS_SK,
    "sk": _CS_SK,
}

# Zgodność wstecz: przed wielojęzycznością te nazwy były jedynym wejściem do
# skompilowanych warstw. Zostają jako polskie, bo tak je czyta reszta repo.
_WYGASZENIE = _PL.wygaszenie
_PRZEPUSZCZENIE = _PL.przepuszczenie
_ODRZUCENIE = _PL.odrzucenie
_PUNKTACJA = _PL.punktacja
_HAMULCE = _PL.hamulce
_HAMULCE_WARUNKOWE = _PL.hamulce_warunkowe


def _rozne_slowniki() -> list[tuple[str, Slownik]]:
    """(znacznik, słownik) — po jednym wpisie na RÓŻNY słownik, nie na klucz.

    Bez tego przebieg „sprawdź wszystkimi" liczyłby czesko-słowacki dwa razy:
    ten sam wynik, podwójny koszt i mylące logi.
    """
    widziane: set[int] = set()
    wynik: list[tuple[str, Slownik]] = []
    for znacznik, slownik in SLOWNIKI.items():
        if id(slownik) in widziane:
            continue
        widziane.add(id(slownik))
        wynik.append((znacznik, slownik))
    return wynik


# ---------------------------------------------------------------------------
# DETEKCJA JĘZYKA — heurystyka na znakach i słowach funkcyjnych
#
# ŻADNEJ biblioteki i żadnego wywołania sieciowego. Bramka stoi na ścieżce
# każdego pobranego posta; wołanie zewnętrznego detektora zamieniłoby filtr,
# który kosztuje mikrosekundy, w filtr, który kosztuje pieniądze i potrafi paść.
#
# UWAGA — detekcja działa na treści SUROWEJ, nie znormalizowanej. Normalizacja
# zbija dokładnie te znaki, które są tu najmocniejszą przesłanką („ř", „ł",
# „ö"). Stąd dwa różne widoki tego samego tekstu w jednym module; to nie jest
# niedopatrzenie.
# ---------------------------------------------------------------------------

# Znaki ROZSTRZYGAJĄCE: występują w jednym z czterech języków i nie występują
# w pozostałych. Wspólne (á, í, é, ú, ý, ó, č, š, ž) świadomie pominięte — nie
# niosą informacji, a rozmyłyby punktację.
_ZNAKI: dict[str, str] = {
    "pl": "ąęłńśźż",
    "de": "öüß",
    "cs": "řěů",
    "sk": "ľĺŕô",
}

# „ä" jest i niemieckie, i słowackie — punktuje OBA, więc samo z siebie niczego
# nie rozstrzyga i decyzja spada na słowa funkcyjne. To jest poprawne: jeden
# znak wspólny nie powinien przeważać.
_ZNAKI_WSPOLNE: dict[str, tuple[str, ...]] = {"ä": ("de", "sk")}

# Słowa funkcyjne: krótkie, bardzo częste, NIE związane z tematem lawet. Dobrane
# tak, żeby nie pokrywały się między językami — dlatego nie ma tu polskiego
# „nie" (identyczne ze słowackim) ani „na"/„do"/„je" (wspólnych dla wszystkich).
_SLOWA: dict[str, tuple[str, ...]] = {
    "pl": ("się", "jest", "czy", "żeby", "mam", "proszę", "bardzo", "tylko",
           "który", "gdzie", "jakiś", "coś", "kogoś", "przy", "oraz", "moje"),
    "de": ("ich", "nicht", "und", "der", "die", "das", "ist", "von", "nach",
           "mit", "für", "kann", "wer", "einen", "bitte", "mein", "hat", "wird"),
    # UWAGA — z list czeskiej i słowackiej WYCIĘTE są słowa, które istnieją też
    # po polsku: „kto" (identyczne ze słowackim), „jak" i „moc" (identyczne
    # z czeskim). Kosztowało to konkretny błąd: polski post „kupiłem auto
    # w Niemczech, KTO przywiezie na lawecie", napisany bez ogonków, dostawał
    # znacznik „sk", szedł wyłącznie przez słownik czesko-słowacki i wylatywał —
    # czyli dokładnie fałszywe odrzucenie, którego ten moduł ma nie popełniać.
    "cs": ("jsem", "jsme", "se", "nebo", "může", "který", "není", "také",
           "kdo", "prosím", "jsou", "musím", "dobrý den"),
    "sk": ("som", "sme", "sa", "alebo", "môže", "ktorý", "veľmi", "aj",
           "prosím", "musím", "chcem", "však", "dobrý deň"),
}

# Znak diakrytyczny waży więcej niż słowo funkcyjne, bo jest trudniejszy do
# przypadkowego trafienia: „ř" w polskim poście nie wystąpi, a „mám" jest
# i czeskie, i słowackie.
_WAGA_ZNAKU = 3
_WAGA_SLOWA = 2

# O ile najlepszy język musi wyprzedzać drugi, żeby uznać detekcję za pewną.
# Poniżej tej różnicy zwracamy "" i liczymy WSZYSTKIMI słownikami. Wartość jest
# niska celowo: pomyłka w detekcji jest droższa niż trzy zbędne przejścia
# regeksem.
_MARGINES_PEWNOSCI = 2

_WYRAZY = re.compile(r"\w+", re.UNICODE)


def _punkty_jezykow(tresc: str) -> dict[str, int]:
    """Ile przesłanek wskazuje na każdy z czterech języków. BEZ normalizacji."""
    tekst = (tresc or "").casefold()
    punkty = {j: 0 for j in _ZNAKI}
    if not tekst:
        return punkty

    obecne = set(tekst)
    for jezyk, znaki in _ZNAKI.items():
        punkty[jezyk] += _WAGA_ZNAKU * sum(1 for z in znaki if z in obecne)
    for znak, jezyki in _ZNAKI_WSPOLNE.items():
        if znak in obecne:
            for jezyk in jezyki:
                punkty[jezyk] += _WAGA_ZNAKU

    # Słowa funkcyjne szukane na granicach wyrazów — „se" jako podciąg trafiałoby
    # w połowę polskich zdań („się", „sercu"), a to jest właśnie ten rodzaj
    # fałszywej przesłanki, który przewraca detekcję na krótkim tekście.
    wyrazy = set(_WYRAZY.findall(tekst))
    for jezyk, slowa in _SLOWA.items():
        punkty[jezyk] += _WAGA_SLOWA * sum(1 for s in slowa if s in wyrazy)
    return punkty


def wykryj_jezyk(tresc: str) -> str:
    """Dwuliterowy znacznik języka albo "" gdy nie da się rozstrzygnąć.

    "" NIE jest błędem — to normalna odpowiedź dla krótkiego posta bez znaków
    charakterystycznych („Kto podjedzie na S19?"). Wołający sprawdza wtedy
    wszystkimi słownikami, co jest tańsze niż zgadywanie.
    """
    punkty = _punkty_jezykow(tresc)
    ranking = sorted(punkty.items(), key=lambda kv: kv[1], reverse=True)
    najlepszy, wynik = ranking[0]
    drugi = ranking[1][1] if len(ranking) > 1 else 0
    if wynik <= 0 or (wynik - drugi) < _MARGINES_PEWNOSCI:
        return ""
    return najlepszy


def _wariant_cs_sk(tresc: str) -> str:
    """„cs" czy „sk" dla posta obsłużonego wspólnym słownikiem.

    Słownik jest jeden, ale znacznik ma powiedzieć operatorowi, czy dzwoni po
    czesku, czy po słowacku. Przy remisie oddajemy „cs" — nie dlatego, że jest
    bardziej prawdopodobny, tylko dlatego, że pole musi mieć wartość, a te dwa
    języki są wzajemnie zrozumiałe (polski dla obu nie jest).
    """
    punkty = _punkty_jezykow(tresc)
    return "sk" if punkty.get("sk", 0) > punkty.get("cs", 0) else "cs"


# ---------------------------------------------------------------------------
# TRYB PRACY
#
# "cien" — bramka LICZY i ZAPISUJE swoją decyzję, ale NICZEGO NIE BLOKUJE.
#          Wszystkie posty idą do AI. Po tygodniu w bazie jest komplet par
#          (decyzja bramki, werdykt AI) i da się policzyć jedyną liczbę, która
#          ma znaczenie: ile postów bramka by odrzuciła, a AI uznało za zlecenie.
#          To są kursy, które system by przegapił, i musi ich być ZERO.
# "aktywny" — decyzja bramki jest wiążąca.
#
# Nieznana wartość degraduje do "cien", a nie do "aktywny": pomyłka w .env nie
# może po cichu włączyć blokowania.
# ---------------------------------------------------------------------------
TRYB_CIEN = "cien"
TRYB_AKTYWNY = "aktywny"
_TRYBY = {
    "cien": TRYB_CIEN, "cień": TRYB_CIEN, "shadow": TRYB_CIEN,
    "aktywny": TRYB_AKTYWNY, "active": TRYB_AKTYWNY, "on": TRYB_AKTYWNY,
}


def normalizuj_tryb(surowy: str | None) -> str:
    return _TRYBY.get((surowy or "").strip().lower(), TRYB_CIEN)


@dataclass(frozen=True)
class GateResult:
    """Wynik bramki dla jednego posta.

    `przepusc` to DECYZJA OPERACYJNA (czy post idzie do klasyfikatora AI) i
    uwzględnia tryb pracy — w trybie cienia jest zawsze True.
    `werdykt` to OPINIA bramki, niezależna od trybu, i to ją zapisujemy do bazy.

    Rozdzielenie tych dwóch jest sednem trybu cienia: gdyby istniało tylko
    `przepusc`, w cieniu zapisywalibyśmy same jedynki i po tygodniu nie
    dałoby się policzyć niczego.
    """

    przepusc: bool
    punkty: int
    powod: str
    trafienia: list[str] = field(default_factory=list)
    werdykt: bool = True
    tryb: str = TRYB_CIEN
    # Dwuliterowy znacznik języka posta ("pl"/"de"/"cs"/"sk") albo "" gdy nie da
    # się rozstrzygnąć. Idzie do bazy i dalej do powiadomienia, bo od niego
    # zależy, w jakim języku operator ma oddzwonić — a wszystkie pozostałe pola
    # alertu są już po polsku, więc sam post tego nie zdradzi.
    jezyk: str = "pl"


def _etykieta(nazwa: str, wzorzec: re.Pattern[str], waga: int = 0) -> str:
    """Jedno trafienie w formie czytelnej dla człowieka: KATEGORIA:wzorzec(+waga)."""
    fraza = wzorzec.pattern
    fraza = fraza.replace(r"(?<![a-z0-9])(?:", "", 1)
    for koniec in (r")(?![a-z0-9])", ")"):
        if fraza.endswith(koniec):
            fraza = fraza[: -len(koniec)]
            break
    return f"{nazwa}:{fraza}" + (f"({waga:+d})" if waga else "")


def _ocen(tekst: str, oryginal: str, slownik: Slownik, prog: int
          ) -> tuple[bool, int, str, list[str], bool]:
    """Cztery warstwy JEDNYM słownikiem -> (werdykt, punkty, powód, trafienia, wygaszone).

    Wydzielone z `gate`, żeby przy niepewnej detekcji dało się policzyć post
    każdym słownikiem osobno i porównać wyniki. Kolejność warstw jest tu
    nietknięta — to ona ratuje realne pieniądze (patrz docstring modułu).
    """
    # WARSTWA 1 — wygaszenie. Pierwsza, bo unieważnia wszystko poniżej.
    for wzorzec, _, etykieta in slownik.wygaszenie:
        if wzorzec.search(tekst):
            return False, 0, "wygaszone", [_etykieta("WYGASZENIE", wzorzec)], True

    # WARSTWA 2 — twarde przepuszczenie. PRZED odrzuceniem: sygnał potrzeby
    # zawsze bije sygnał odrzucenia.
    for wzorzec, _, etykieta in slownik.przepuszczenie:
        if wzorzec.search(tekst):
            return True, 0, etykieta, [_etykieta("PRZEPUSZCZENIE", wzorzec)], False

    # WARSTWA 3 — twarde odrzucenie. Tylko wzorce jednoznaczne.
    for wzorzec, _, etykieta in slownik.odrzucenie:
        if wzorzec.search(tekst):
            return False, 0, etykieta, [_etykieta("ODRZUCENIE", wzorzec)], False
    if len(oryginal) < MIN_DLUGOSC:
        return (False, 0, "za krotkie",
                [f"DLUGOSC:{len(oryginal)}<{MIN_DLUGOSC}"], False)

    # WARSTWA 4 — punktacja.
    punkty = 0
    trafienia: list[str] = []
    kategorie: set[str] = set()
    for wzorzec, waga, etykieta in slownik.punktacja:
        if wzorzec.search(tekst):
            punkty += waga
            kategorie.add(etykieta)
            trafienia.append(_etykieta(etykieta, wzorzec, waga))
    if _TELEFON.search(tekst):
        punkty += 1
        kategorie.add("KONTAKT")
        trafienia.append("KONTAKT:numer telefonu(+1)")
    for wzorzec, waga, etykieta in slownik.hamulce:
        if wzorzec.search(tekst):
            punkty += waga
            trafienia.append(_etykieta(etykieta, wzorzec, waga))
    for wzorzec, waga, etykieta, blokujaca in slownik.hamulce_warunkowe:
        if blokujaca not in kategorie and wzorzec.search(tekst):
            punkty += waga
            trafienia.append(_etykieta(etykieta, wzorzec, waga))

    zdal = punkty >= prog
    return (zdal, punkty,
            f"punktacja {punkty} {'>=' if zdal else '<'} prog {prog}",
            trafienia, False)


def _rozstrzygnij(wynik: tuple, wykryty: str) -> tuple:
    """Klucz porównania między słownikami. Inny dla przepuszczeń i odrzuceń.

    PRZEPUSZCZENIE bije wszystko — jeden słownik widzący zlecenie wystarczy,
    bo ułamek grosza za niepotrzebne pytanie modelu jest nieporównywalny
    z kursem straconym przez odrzucenie. Wśród przepuszczeń wygrywa wyższa
    punktacja (więcej sygnału), a przy remisie — słownik zgodny z detekcją.

    WŚRÓD ODRZUCEŃ wygrywa uzasadnienie NAJBARDZIEJ KONKRETNE: najpierw to,
    w którym zadziałała nazwana reguła, potem NIŻSZA punktacja. Odwrotnie niż
    przy przepuszczeniach — i to jest celowe. Czeska reklama odrzucona przez
    słownik czeski jako „autopromocja" niesie do raportu z trybu cienia
    informację, z której da się kalibrować próg; ta sama reklama odrzucona przez
    słownik polski jako „punktacja 0 < prog 5" nie niesie żadnej.
    """
    werdykt, punkty, _powod, trafienia, znacznik = wynik
    zgodny_z_detekcja = 1 if (wykryty and znacznik == wykryty) else 0
    # Oba warianty mają tę samą długość, żeby porównanie nigdy nie zależało od
    # kolejności argumentów (krotki różnej długości porównują się poprawnie
    # tylko dopóki różnią się wcześniej — a to jest założenie, które łatwo
    # złamać przy kolejnej zmianie kryteriów).
    if werdykt:
        return (1, punkty, 0, zgodny_z_detekcja)
    return (0, 1 if trafienia else 0, -punkty, zgodny_z_detekcja)


def gate(tresc: str, prog: int | None = None, tryb: str | None = None,
         jezyk: str | None = None) -> GateResult:
    """Czy post ma iść do klasyfikatora AI.

    `prog` i `tryb` są nadpisywalne argumentem wyłącznie po to, żeby dało się
    przeliczyć historyczne posty przy innym progu (scripts/raport_gate.py
    --prog) i przetestować obie ścieżki bez dotykania środowiska. `jezyk`
    wymusza słownik — dla grup, o których wiadomo z góry, w jakim są języku.

    BEZ WYMUSZENIA język jest WYKRYWANY, a przy niepewnej detekcji post idzie
    przez WSZYSTKIE słowniki. Cztery przebiegi regeksem po krótkim tekście to
    mikrosekundy, a zgubione zlecenie to kilkaset złotych — przy tej asymetrii
    nie ma czego optymalizować.

    ZASADY ROZSTRZYGANIA MIĘDZY SŁOWNIKAMI, obie wynikające z reguły naczelnej
    tego modułu („odrzucamy wyłącznie to, co jest śmieciem ponad wszelką
    wątpliwość"):

      1. WYGASZENIE WIDZIANE PRZEZ KTÓRYKOLWIEK SŁOWNIK WYGASZA POST. Samo „weź
         najlepszy wynik" tu nie wystarcza: słownik, który nie zna zwrotu
         „hat sich erledigt", milczy — a milczenie wygląda lepiej niż odrzucenie.
         Post załatwiony przestaje być zleceniem niezależnie od języka.
      2. JEDNO PRZEPUSZCZENIE WYSTARCZY. Jeśli choć jeden słownik widzi
         zlecenie, post idzie do modelu. Kosztuje to ułamek grosza, a wariant
         odwrotny kosztuje kurs.
    """
    prog = settings.GATE_PROG if prog is None else prog
    tryb = normalizuj_tryb(settings.GATE_TRYB if tryb is None else tryb)
    oryginal = (tresc or "").strip()
    tekst = normalizuj(tresc)

    wykryty = wykryj_jezyk(oryginal)
    if jezyk is not None and jezyk in SLOWNIKI:
        kandydaci = [(jezyk, SLOWNIKI[jezyk])]
    else:
        # ZAWSZE wszystkimi słownikami, nie tylko przy niepewnej detekcji.
        # Detekcja służy wtedy do wyboru znacznika i do rozstrzygania remisów,
        # a nie do zawężania wyszukiwania — bo pomyłka w detekcji jest właśnie
        # cichym fałszywym odrzuceniem, czyli jedynym błędem, którego ten moduł
        # ma nie popełniać. Cztery przebiegi regeksem po krótkim tekście
        # kosztują mikrosekundy; zgubione zlecenie kosztuje kurs.
        kandydaci = _rozne_slowniki()

    najlepszy = None          # (werdykt, punkty, powod, trafienia, znacznik)
    wygaszone = None
    for znacznik, slownik in kandydaci:
        werdykt, punkty, powod, trafienia, wyg = _ocen(tekst, oryginal, slownik, prog)
        if wyg and wygaszone is None:
            wygaszone = (werdykt, punkty, powod, trafienia, znacznik)
        biezacy = (werdykt, punkty, powod, trafienia, znacznik)
        if najlepszy is None or _rozstrzygnij(biezacy, wykryty) > _rozstrzygnij(najlepszy, wykryty):
            najlepszy = biezacy

    werdykt, punkty, powod, trafienia, znacznik = wygaszone or najlepszy

    # Znacznik języka dla operatora. Bierzemy go ze słownika, który wygrał — ten
    # „wytłumaczył" post najlepiej. Dla wspólnego czesko-słowackiego rozstrzyga
    # jeszcze wariant, bo od niego zależy język oddzwonienia.
    if znacznik in ("cs", "sk"):
        znacznik = _wariant_cs_sk(oryginal)

    return GateResult(
        przepusc=True if tryb == TRYB_CIEN else werdykt,
        punkty=punkty,
        powod=powod,
        trafienia=trafienia,
        werdykt=werdykt,
        tryb=tryb,
        jezyk=znacznik,
    )


# ---------------------------------------------------------------------------
# KONTRAKT ZAPISU
#
# Bramka sama do bazy NIE PISZE (ma zostać czysta i offline), ale to ona wie,
# co znaczą jej pola. Kolumny opisuje api/migrations/0002_gate.sql, a wołający
# (fb_fetcher z promptu 2) bierze wartości stąd, żeby kontrakt nie rozjechał się
# w dwóch miejscach naraz.
# ---------------------------------------------------------------------------
SQL_ZAPIS = """
UPDATE posty SET
    gate_werdykt   = %(gate_werdykt)s,
    gate_punkty    = %(gate_punkty)s,
    gate_powod     = %(gate_powod)s,
    gate_trafienia = %(gate_trafienia)s,
    gate_tryb      = %(gate_tryb)s,
    gate_jezyk     = %(gate_jezyk)s,
    gate_at        = NOW()
WHERE fb_id = %(fb_id)s
"""


def wiersz_do_zapisu(wynik: GateResult, fb_id: str) -> dict[str, object]:
    """Wynik bramki -> parametry do SQL_ZAPIS. Zapisujemy WERDYKT, nie `przepusc`."""
    return {
        "fb_id": fb_id,
        "gate_werdykt": wynik.werdykt,
        "gate_punkty": wynik.punkty,
        "gate_powod": wynik.powod,
        "gate_trafienia": list(wynik.trafienia),
        "gate_tryb": wynik.tryb,
        # Znacznik języka jedzie do bazy razem z werdyktem, bo powiadomienie
        # bierze go stamtąd — a nie liczy jeszcze raz z treści.
        "gate_jezyk": wynik.jezyk or None,
    }


# ---------------------------------------------------------------------------
# KONTRAKT Z KLASYFIKATOREM
#
# Bramka NIE tłumaczy — tylko wpuszcza. Tłumaczenie robi klasyfikator, i to on
# musi wiedzieć, że post bywa obcojęzyczny. Instrukcja stoi TUTAJ, a nie
# w klasyfikatorze, z jednego powodu: to bramka wpuszcza obce języki do
# pipeline'u, więc to przy niej trzeba pamiętać o konsekwencji. Klasyfikator
# wkleja ją do promptu systemowego przez IMPORT, a nie przepisując — przepisana
# rozjechałaby się przy pierwszej zmianie listy języków.
# ---------------------------------------------------------------------------
INSTRUKCJA_JEZYKOWA_DLA_KLASYFIKATORA = """\
JĘZYK POSTA. Post może być po polsku, niemiecku, czesku albo słowacku — grupy \
z tych czterech obszarów idą przez ten sam pipeline. Nie komentuj języka posta \
i nie dołączaj tłumaczenia treści.

WSZYSTKIE pola wyniku wypełniaj PO POLSKU, niezależnie od języka posta. Czyta \
je polskojęzyczny operator, który ma podjąć decyzję w kilkanaście sekund — \
opis po niemiecku zmusiłby go do tłumaczenia w najgorszym możliwym momencie.

WYJĄTEK: nazwy miejscowości zostawiaj W FORMIE ORYGINALNEJ z posta („München", \
nie „Monachium"; „Praha", nie „Praga"). Te pola idą wprost do geokodowania \
i do linku z mapą — przetłumaczona nazwa albo nie znajdzie się w geokoderze, \
albo znajdzie się w złym miejscu."""


# ---------------------------------------------------------------------------
# CLI — do szybkiego sprawdzania, DLACZEGO konkretny post przeszedł albo nie
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    ap = argparse.ArgumentParser(
        description="Bramka: decyzja, punktacja i trafione wzorce dla jednego posta."
    )
    ap.add_argument("tresc", nargs="?", help="treść posta (bez niej czytam ze stdin)")
    ap.add_argument("--prog", type=int, default=None,
                    help=f"próg punktowy (domyślnie GATE_PROG={settings.GATE_PROG})")
    ap.add_argument("--tryb", default=None, choices=[TRYB_CIEN, TRYB_AKTYWNY],
                    help=f"tryb pracy (domyślnie GATE_TRYB={settings.GATE_TRYB})")
    args = ap.parse_args(argv[1:])

    tresc = args.tresc if args.tresc is not None else sys.stdin.read()
    if not tresc.strip():
        print("Brak treści — podaj ją argumentem albo na stdin.", file=sys.stderr)
        return 0

    w = gate(tresc, prog=args.prog, tryb=args.tryb)
    wykryty = wykryj_jezyk(tresc)
    print(f"WERDYKT BRAMKI: {'PRZEPUSZCZAM' if w.werdykt else 'ODRZUCAM'}  ({w.powod})")
    # Detekcja i znacznik to DWIE różne rzeczy. Pusta detekcja jest normalną
    # ścieżką (krótki post bez znaków charakterystycznych), a znacznik bierze
    # się wtedy ze słownika, który wytłumaczył post najlepiej.
    print(f"JĘZYK:          {w.jezyk or '—'}"
          f"   (detekcja: {wykryty or 'nierozstrzygnięta, liczę wszystkimi słownikami'})")
    print(f"PUNKTY:         {w.punkty}"
          + (f"  (prog {args.prog if args.prog is not None else settings.GATE_PROG})"
             if w.powod.startswith("punktacja") else ""))
    print(f"TRYB:           {w.tryb}")
    if w.tryb == TRYB_CIEN and not w.werdykt:
        print("                w cieniu bramka NIC nie blokuje — post i tak idzie do AI")
    print(f"DO AI:          {'TAK' if w.przepusc else 'NIE'}")
    print("TRAFIENIA:" + ("" if w.trafienia else " (brak)"))
    for t in w.trafienia:
        print(f"  {t}")
    print(f"\nZNORMALIZOWANA TREŚĆ: {normalizuj(tresc)}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
