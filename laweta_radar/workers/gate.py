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

ZERO WYWOŁAŃ SIECIOWYCH i zero I/O. Moduł działa offline, w mikrosekundach, i da
się go zaimportować bez bazy, bez kluczy i bez internetu. Zapis decyzji do bazy
robi wołający — kontrakt kolumn: `wiersz_do_zapisu()` + api/migrations/0002_gate.sql.

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

_WYGASZENIE = _skompiluj_liste(WYGASZENIE)
_PRZEPUSZCZENIE = _skompiluj_liste(PRZEPUSZCZENIE)
_ODRZUCENIE = _skompiluj_liste(ODRZUCENIE)
_PUNKTACJA = _skompiluj_liste(PUNKTACJA)
_HAMULCE = _skompiluj_liste(HAMULCE)
_HAMULCE_WARUNKOWE = [(_skompiluj(w), waga, etykieta, kat)
                      for w, waga, etykieta, kat in HAMULCE_WARUNKOWE]


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


def _etykieta(nazwa: str, wzorzec: re.Pattern[str], waga: int = 0) -> str:
    """Jedno trafienie w formie czytelnej dla człowieka: KATEGORIA:wzorzec(+waga)."""
    fraza = wzorzec.pattern
    fraza = fraza.replace(r"(?<![a-z0-9])(?:", "", 1)
    for koniec in (r")(?![a-z0-9])", ")"):
        if fraza.endswith(koniec):
            fraza = fraza[: -len(koniec)]
            break
    return f"{nazwa}:{fraza}" + (f"({waga:+d})" if waga else "")


def gate(tresc: str, prog: int | None = None, tryb: str | None = None) -> GateResult:
    """Czy post ma iść do klasyfikatora AI.

    `prog` i `tryb` są nadpisywalne argumentem wyłącznie po to, żeby dało się
    przeliczyć historyczne posty przy innym progu (scripts/raport_gate.py
    --prog) i przetestować obie ścieżki bez dotykania środowiska.
    """
    prog = settings.GATE_PROG if prog is None else prog
    tryb = normalizuj_tryb(settings.GATE_TRYB if tryb is None else tryb)
    oryginal = (tresc or "").strip()
    tekst = normalizuj(tresc)

    def wynik(werdykt: bool, punkty: int, powod: str, trafienia: list[str]) -> GateResult:
        return GateResult(
            przepusc=True if tryb == TRYB_CIEN else werdykt,
            punkty=punkty,
            powod=powod,
            trafienia=trafienia,
            werdykt=werdykt,
            tryb=tryb,
        )

    # WARSTWA 1 — wygaszenie. Pierwsza, bo unieważnia wszystko poniżej.
    for wzorzec, _, etykieta in _WYGASZENIE:
        if wzorzec.search(tekst):
            return wynik(False, 0, "wygaszone", [_etykieta("WYGASZENIE", wzorzec)])

    # WARSTWA 2 — twarde przepuszczenie. PRZED odrzuceniem: sygnał potrzeby
    # zawsze bije sygnał odrzucenia.
    for wzorzec, _, etykieta in _PRZEPUSZCZENIE:
        if wzorzec.search(tekst):
            return wynik(True, 0, etykieta, [_etykieta("PRZEPUSZCZENIE", wzorzec)])

    # WARSTWA 3 — twarde odrzucenie. Tylko wzorce jednoznaczne.
    for wzorzec, _, etykieta in _ODRZUCENIE:
        if wzorzec.search(tekst):
            return wynik(False, 0, etykieta, [_etykieta("ODRZUCENIE", wzorzec)])
    if len(oryginal) < MIN_DLUGOSC:
        return wynik(False, 0, "za krotkie", [f"DLUGOSC:{len(oryginal)}<{MIN_DLUGOSC}"])

    # WARSTWA 4 — punktacja.
    punkty = 0
    trafienia: list[str] = []
    kategorie: set[str] = set()
    for wzorzec, waga, etykieta in _PUNKTACJA:
        if wzorzec.search(tekst):
            punkty += waga
            kategorie.add(etykieta)
            trafienia.append(_etykieta(etykieta, wzorzec, waga))
    if _TELEFON.search(tekst):
        punkty += 1
        kategorie.add("KONTAKT")
        trafienia.append("KONTAKT:numer telefonu(+1)")
    for wzorzec, waga, etykieta in _HAMULCE:
        if wzorzec.search(tekst):
            punkty += waga
            trafienia.append(_etykieta(etykieta, wzorzec, waga))
    for wzorzec, waga, etykieta, blokujaca in _HAMULCE_WARUNKOWE:
        if blokujaca not in kategorie and wzorzec.search(tekst):
            punkty += waga
            trafienia.append(_etykieta(etykieta, wzorzec, waga))

    zdal = punkty >= prog
    return wynik(zdal, punkty, f"punktacja {punkty} {'>=' if zdal else '<'} prog {prog}",
                 trafienia)


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
    }


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
    print(f"WERDYKT BRAMKI: {'PRZEPUSZCZAM' if w.werdykt else 'ODRZUCAM'}  ({w.powod})")
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
