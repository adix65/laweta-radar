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
wyłącznie posty, które NIE SĄ ZLECENIAMI: reklamę konkurencji (w formie
tradycyjnej — „laweta 24/7, konkurencyjne ceny" — i w formie z giełd, czyli
przewoźnika ogłaszającego własne wolne miejsce), sprzedaż sprzętu, ogłoszenia
o pracę i posty wygaszone przez autora. Ta lista jest zamknięta.
Ocen biznesowych ("za daleko", "za ciężkie", "za tanio") bramka NIE robi i nie
wolno jej ich dopisać — o tym decyduje kierowca, patrząc na ekran.

CZTERY WARSTWY, W TEJ KOLEJNOŚCI. Kolejność jest merytoryczna, nie porządkowa:

  1. WYGASZENIE      — unieważnia wszystko poniżej (post z kompletem słów
                       kluczowych, ale autor dopisał "załatwione")
  2. PRZEPUSZCZENIE  — sygnał POTRZEBY bije sygnał odrzucenia
  3. ODRZUCENIE      — tylko wzorce jednoznaczne
  4. PUNKTACJA       — reszta; próg kalibrowany jedną liczbą w .env

PRZED tymi czterema warstwami stoi KIERUNEK ZGŁOSZENIA ("zlecenie" / "oferta" /
"niejasne") — jedyna rzecz w tym module, która rozstrzyga PONAD kolejnością
warstw. Odpowiada na pytanie, które ma komplet cech zlecenia po obu stronach:
czy autor SZUKA kogoś, kto przewiezie (zlecenie), czy OFERUJE własne wolne
miejsce (oferta konkurencji). "Czwartek 06.08 wolna laweta Elbląg-Lublin
tel. 501606207" ma trasę, datę i telefon — i nie jest zleceniem ani przez
sekundę. Dlaczego PRZED warstwami, a nie w warstwie 3: warstwa 2 zna frazę
„wolne miejsce" jako zgłoszenie z giełdy i przepuściłaby ofertę, zanim
odrzucenie doszłoby do głosu. Szczegóły i mechanizm zabezpieczający (POPYT bije
OFERTĘ) — przy tabelach `POPYT` i `OFERTA`.

OBOK werdyktu bramka zwraca KATEGORIĘ ŁADUNKU ("pojazd" / "zwierze" / "inne").
To NIE jest piąta warstwa i nie ma prawa nią zostać: kategoria niczego nie
odrzuca i nie dodaje ani jednego punktu. Odpowiada wyłącznie na pytanie „co
miałoby jechać", bo giełdy transportowe mieszają auta z końmi i bydłem, a ten
operator zwierząt nie wozi. Post o zwierzętach przechodzi bramkę normalnie,
dostaje widoczny znacznik w panelu i w alercie, ląduje niżej na liście —
i (przy ALERT_ZWIERZETA=0) nie brzęczy telefonem. Twarde odrzucenie byłoby
tu skasowaniem danych, których nikt potem nie odtworzy: gdyby operator dokupił
przyczepę do koni albo zaczął podnajmować takie kursy, historia już czeka
w bazie. Szczegóły: `_kategoria` i kolumna `kategoria_ladunku`
(api/migrations/0010_kategoria_ladunku.sql).

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
    # „poszukuję" obok „szukam": to samo zgłoszenie, inna forma czasownika.
    # Produkcyjne „Poszukuję transportu dla ..." nie trafiało TU w nic i spadało
    # do punktacji, a warstwa 3 ma tuż obok „poszukuje kierowcy" (ogłoszenie
    # o pracy) — czyli jedyny wzorzec, który tę formę w ogóle znał, ODRZUCAŁ.
    (r"(szukam|poszukuje) (lawety|pomocy drogowej|kogos z laweta|firmy|"
     r"transportu|przewoznika|miejsca na lawecie|kogos kto)", 0, "prosba wprost"),
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

    # --- ZGŁOSZENIE Z GIEŁDY TRANSPORTOWEJ: forma bezokolicznikowa ---
    #
    # Na giełdach zlecenie zaczyna się najczęściej NIE od prośby, tylko od
    # rzeczownika odczasownikowego: „Do przywiezienia Citroen Berlingo z 18556
    # do 63-505". Nie ma tu ani „potrzebuję", ani awarii, ani pary miast
    # w formie „z X do Y" — więc taki post trafiał w warstwie 4 w ZERO wzorców,
    # dostawał zero punktów i wylatywał jako śmieć. To jest najdroższy błąd,
    # jaki ten moduł potrafi popełnić: kompletne zlecenie z trasą i pojazdem,
    # skasowane bez śladu, bo słownik nie znał formy gramatycznej.
    #
    # DLACZEGO TWARDE PRZEPUSZCZENIE, A NIE PUNKTY. Te zwroty są jednoznaczne po
    # stronie POPYTU: nikt nie reklamuje własnej lawety zdaniem „do zabrania
    # iveco solówka spod Paryża". Sygnał poszlakowy (punkty) wymagałby drugiego
    # trafienia, żeby przekroczyć próg — a te posty są z założenia telegraficzne
    # i drugiego sygnału zwykle nie mają.
    (r"do (przywiezienia|przywozu|zabrania|odebrania|odbioru|przewiezienia|"
     r"przewozu|sciagniecia|podjecia|zawiezienia|dowiezienia|"
     r"przetransportowania|zaladowania)", 0, "zgloszenie z gieldy"),
    # „szukam wolnego JEDNEGO miejsca" — między „wolnego" a „miejsca" ludzie
    # wtrącają liczebnik albo przymiotnik, więc jedno słowo jest tu dozwolone.
    # Wzorzec bez tej luki nie trafiłby posta, od którego zaczęło się zgłoszenie.
    (r"(szukam|poszukuje) wolnego( [a-z]+)? miejsca", 0, "zgloszenie z gieldy"),
    (r"(szukam|poszukuje) miejsca (w transporcie|na transport|na aucie|"
     r"w aucie|na lawete)", 0, "zgloszenie z gieldy"),
    # DWA NASTĘPNE WZORCE SĄ DWUZNACZNE I DLATEGO WARUNKOWE. „Szukam wolnego
    # miejsca" to zlecenie, „mam wolne miejsce" to oferta przewoźnika — ta sama
    # fraza, przeciwne strony rynku. Rozstrzyga czasownik, więc rozstrzyga to
    # KIERUNEK (tabele `POPYT` i `OFERTA`), sprawdzany PRZED tymi warstwami.
    # Te wpisy nie są martwe: fraza z sygnałem popytu dochodzi tutaj i działa
    # jak dotąd, a sama — bez czasownika — nie dochodzi, bo została odrzucona
    # jako oferta.
    (r"wolne miejsc[ae]", 0, "zgloszenie z gieldy"),
    (r"jest wolne miejsce", 0, "zgloszenie z gieldy"),
    # „kto wraca z Niemiec", „kto jedzie w kierunku Wrocławia" — pytanie
    # o doładunek. Istniejące „kto jedzie (z|do|w strone|na)" nie znało formy
    # „w kierunku" ani żadnego wariantu z „wraca".
    (r"kto (wraca|bedzie wracal|wracal)", 0, "zgloszenie z gieldy"),
    (r"kto jedzie w kierunku", 0, "zgloszenie z gieldy"),

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
    # Warunkowy tak samo jak „wolne miejsce" wyżej — patrz nota przy tamtym.
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
# WZORCE NIEZALEŻNE OD JĘZYKA
#
# Trzy rzeczy w tych postach wyglądają tak samo po polsku, niemiecku, czesku
# i słowacku: kod pocztowy, marka auta i nazwa domu aukcyjnego. Trzymanie ich
# w każdym słowniku osobno znaczyłoby cztery kopie tej samej listy i trzy
# miejsca do zapomnienia przy dopisaniu marki — a zapomniana kopia nie daje
# ŻADNEGO objawu, tylko cicho gubi punkty w jednym języku.
#
# Dlatego stoją TU, raz, i `_zbuduj_slownik` dokleja je do punktacji KAŻDEGO
# słownika. Kod pocztowy jest osobno (jak numer telefonu), bo nie jest frazą
# — liczy się nie samo trafienie, tylko ILE ich jest w treści.
# ===========================================================================

# MARKI (+3). Na giełdzie post prawie nigdy nie mówi „samochód osobowy" — mówi
# „Citroena C5", „Opel Meriva", „iveco solówka". Bez marek taki post trafiał
# najwyżej w AKCJĘ i kończył z trójką przy progu pięć.
#
# Waga WYŻSZA niż ogólne „auto" (+2), bo marka jest sygnałem mocniejszym: słowo
# „auto" pada też w reklamie warsztatu i w ogłoszeniu o pracę, a „Berlingo
# z kodu 18556" pada w zleceniu.
MARKI: list[tuple[str, int, str]] = [
    (r"citroen[a-z]*", 3, "POJAZD"),
    (r"opel[a-z]*|opla|oplem|oplu", 3, "POJAZD"),
    (r"iveco", 3, "POJAZD"),
    (r"mercedes[a-z]*", 3, "POJAZD"),
    (r"volkswagen[a-z]*|vw", 3, "POJAZD"),
    (r"renault[a-z]*", 3, "POJAZD"),
    (r"ford[a-z]*", 3, "POJAZD"),
    (r"audi", 3, "POJAZD"),
    (r"bmw", 3, "POJAZD"),
    (r"skod[aeyu]", 3, "POJAZD"),
    (r"fiat[a-z]*", 3, "POJAZD"),
    (r"peugeot[a-z]*", 3, "POJAZD"),
    (r"toyot[aeyu]", 3, "POJAZD"),
    (r"nissan[a-z]*", 3, "POJAZD"),
]

# ŹRÓDŁO AUTA (+2) — dom aukcyjny, komis, autohaus. Sygnał słabszy od marki
# i dlatego niższa waga: sam „komis" bywa też w reklamie komisu. Ale w parze
# z czymkolwiek innym („Opel Meriva … Copart Buddingen") domyka post do progu.
ZRODLA_AUT: list[tuple[str, int, str]] = [
    (r"copart", 2, "ZRODLO"),
    (r"autohaus[a-z]*", 2, "ZRODLO"),
    # Pusta alternatywa zamiast „*": „komis[a-z]*" trafiałoby w „komisariat"
    # i „komisję" (patrz nota o prawej granicy przy `_skompiluj`).
    (r"komis(u|ie|em|ow|y|)", 2, "ZRODLO"),
    (r"aukcj[a-z]*", 2, "ZRODLO"),
    # Warianty obcojęzyczne tej samej rzeczy — lista jest wspólna, więc nie ma
    # powodu, żeby czeski „autobazar" albo niemiecki „Auktion" jej nie widziały.
    (r"aukc[ei][a-z]*", 2, "ZRODLO"),
    (r"auktion[a-z]*", 2, "ZRODLO"),
    (r"autobazar[a-z]*", 2, "ZRODLO"),
    (r"dom aukcyjny", 2, "ZRODLO"),
]

# KODY POCZTOWE. Dwa różne kody w jednej treści to praktycznie definicja trasy
# — i jedyny sygnał, jaki niosą posty telegraficzne („z kodu 18556 … 63-505"),
# w których nie ma ani czasownika, ani nazwy miasta znanej słownikowi.
#
# Formaty czterech rynków naraz: PL „38-400", DE/FR/IT/ES pięć cyfr, AT/BE/DK/NL
# cztery, CZ/SK „110 00". Świadomie SZEROKO — ta reguła wyłącznie DODAJE punkty,
# więc jej fałszywe trafienie (rok „2009" wzięty za kod belgijski) kosztuje
# ułamek grosza za pytanie do modelu, a fałszywy brak kosztuje kurs.
#
# Lewy lookaround dopuszcza „-", żeby zadziałało niemieckie „D-64354"; prawy go
# NIE dopuszcza, bo inaczej numer telefonu pisany „48-123-456" rozpadałby się na
# kody. Litera po prawej też blokuje trafienie — „2009r" to rok, nie kod.
_KOD_POCZTOWY = re.compile(
    r"(?<![0-9a-z])(?:[0-9]{2}-[0-9]{3}|[0-9]{3} [0-9]{2}|[0-9]{4,5})(?![0-9a-z-])"
)
WAGA_DWA_KODY = 4


def kody_pocztowe(tekst: str) -> list[str]:
    """Kody pocztowe z treści ZNORMALIZOWANEJ, bez powtórzeń, w kolejności.

    Powtórzenia liczymy po formie BEZ separatorów: „97-400" i „97400" to ten sam
    kod zapisany dwa razy, a nie trasa. Bez tego post powtarzający jeden adres
    w dwóch zapisach dostawałby +4 za trasę, której nie ma.
    """
    widziane: set[str] = set()
    wynik: list[str] = []
    for kod in _KOD_POCZTOWY.findall(tekst):
        klucz = kod.replace("-", "").replace(" ", "")
        if klucz in widziane:
            continue
        widziane.add(klucz)
        wynik.append(kod)
    return wynik


# ---------------------------------------------------------------------------
# ZWIERZĘTA — KATEGORIA ŁADUNKU, NIE ODRZUCENIE
#
# Te giełdy mieszają transport aut z transportem koni i zwierząt gospodarskich.
# Operator zwierząt NIE wozi, ale post o koniu NIE JEST śmieciem — jest kursem
# spoza jego oferty, a to są dwie różne rzeczy i tylko jedną z nich wolno tej
# bramce kasować. Zasada naczelna repo: system pokazuje zlecenia, decyduje
# kierowca. Twarde odrzucenie zabrałoby mu tę decyzję i skasowało dane, których
# nikt nie odtworzy — a gdyby kiedyś doszła przyczepa do koni albo chęć
# podnajmowania takich kursów dalej, historia musi już być w bazie.
#
# Stąd ta tabela NIE MA WAGI (zero) i nie stoi w żadnej z czterech warstw.
# Odpowiada na osobne pytanie: co miałoby jechać.
#
# WZORCE SĄ WĄSKIE CELOWO. „kon" bez granicy słowa łapałoby „kontakt",
# „konkurencję" i „koniec" — czyli oznaczałoby jako transport zwierząt połowę
# postów o lawetach. Granice dokłada `_skompiluj`, ale alternatywy piszemy tak,
# żeby żadna nie kończyła się fragmentem częstego słowa.
#
# JEDNA KOLIZJA JEST NIE DO ROZSTRZYGNIĘCIA: „źrebak" i „zrębak" (rębak do
# gałęzi, realny ładunek dla lawety) po normalizacji to ten sam ciąg „zrebak".
# Zostawiamy go w tabeli, bo transport koni jest tu problemem realnym, a rębak
# — hipotetycznym; skutkiem pomyłki jest znacznik i brak brzęczenia, nie
# zniknięcie zlecenia z panelu.
# ---------------------------------------------------------------------------
ZWIERZETA: list[tuple[str, int, str]] = [
    # Konie — najczęstszy przypadek na tych grupach, w komplecie odmian.
    #
    # PUSTA ALTERNATYWA NA KOŃCU, A NIE „?" — i to jest tu warunek działania,
    # nie stylistyka. `_skompiluj` dokłada prawą granicę słowa TYLKO wzorcom
    # kończącym się literą, cyfrą albo domknięciem grupy; wzorzec kończący się
    # kwantyfikatorem mówi „tu może być ciąg dalszy" i granicy nie dostaje.
    # `kon(...)?` bez granicy trafiał w „KONtakt", „KONkurencję" i „KONieczna",
    # czyli oznaczałby jako transport zwierząt połowę grupy o lawetach.
    (r"kon(ia|iu|ie|i|iem|iami|mi|)", 0, "ZWIERZE"),
    (r"koniowo[zs][a-z]*", 0, "ZWIERZE"),
    (r"walach[a-z]*", 0, "ZWIERZE"),
    (r"klacz[a-z]*", 0, "ZWIERZE"),
    (r"ogier[a-z]*", 0, "ZWIERZE"),
    (r"zrebak[a-z]*|zrebi(e|ec|eta|at)[a-z]*", 0, "ZWIERZE"),
    (r"kucyk[a-z]*", 0, "ZWIERZE"),
    # Gospodarskie.
    (r"osiol[a-z]*|osla|oslem|oslami", 0, "ZWIERZE"),
    (r"krow[aeyu]|krowami", 0, "ZWIERZE"),
    (r"byk|byka|bykiem|bykow", 0, "ZWIERZE"),
    (r"ciel(e|ak|aka|eta|at)", 0, "ZWIERZE"),
    (r"owc[aeyu]|owiec", 0, "ZWIERZE"),
    # Kolejność alternatyw też nie jest dowolna: wariant z „*" musi stać PRZED
    # zwykłym, żeby cały wzorzec kończył się literą i dostał prawą granicę —
    # inaczej „koz[ae]" trafiałoby w „kozaki".
    (r"kozl[a-z]*|koz[ae]|kozami", 0, "ZWIERZE"),
    (r"stado|stada|stadem", 0, "ZWIERZE"),
    # „transport zwierząt" i „przewóz zwierząt" NIE mają tu osobnych wpisów —
    # pokrywa je sam rzeczownik, a martwy wzorzec w tabeli jest gorszy niż jego
    # brak (wygląda na zabezpieczenie, którego nie ma).
    (r"zwierz(e|eta|at|etami|akow|aki)", 0, "ZWIERZE"),
    # Obcojęzyczne warianty tej samej rzeczy — tabela jest wspólna dla wszystkich
    # słowników, a „Pferdetransport" znaczy dokładnie to samo co „transport koni".
    (r"pferd[a-z]*", 0, "ZWIERZE"),
    (r"tiertransport[a-z]*", 0, "ZWIERZE"),
    (r"kobyl[a-z]*|kone|kun", 0, "ZWIERZE"),
]

# Wartości kolumny `kategoria_ladunku` — trzy i tylko trzy. Stałe, a nie gołe
# stringi, bo tę samą wartość czyta panel, powiadomienia i SQL.
KAT_POJAZD = "pojazd"
KAT_ZWIERZE = "zwierze"
KAT_INNE = "inne"


# ===========================================================================
# KIERUNEK ZGŁOSZENIA — kto kogo szuka
#
# Na giełdach transportowych obie strony rynku piszą posty o TYM SAMYM
# KSZTAŁCIE: trasa, data, telefon. Różnią się jednym — kierunkiem.
#
#     ZLECENIE:  „szukam kogoś, kto przewiezie MOJE auto"      -> nasz klient
#     OFERTA:    „jadę tamtędy i mam wolne miejsce, dzwońcie"  -> konkurencja
#
# Dwa posty, które to zgłoszenie wywołały, przeszły przez wszystko i obudziły
# telefon:
#     „Czwartek 06.08.26r wolna laweta Elblag-Lublin tel.501606207"
#     „Wolny transport 10.08 na trasie Grudziadz - Warszawa - Siedlce 25T 9,5m"
# Pierwszy nie miał nawet dość punktów, żeby przejść (bramka stała w cieniu,
# więc niczego nie blokowała). Drugi miał: „transport" +3, „tel" +1, numer +1 —
# równo próg. Punktacja nie ma jak ich odróżnić od zlecenia, bo mierzy OBECNOŚĆ
# słów, a nie stronę rynku, po której stoi autor.
#
# DLACZEGO OSOBNY MECHANIZM, A NIE KOLEJNE WPISY W WARSTWIE 3. Bo warstwa 2 bije
# warstwę 3, a to właśnie w warstwie 2 leżą frazy dwuznaczne („wolne miejsce",
# „Rückfahrt"). Oferta wpadałaby więc w przepuszczenie, zanim odrzucenie
# doszłoby do głosu. Kierunek rozstrzyga się PRZED czterema warstwami,
# w `gate()`, i jest — jak kategoria ładunku — WSPÓLNY DLA WSZYSTKICH JĘZYKÓW:
# „mam wolne miejsce", „habe noch Platz" i „volné místo" znaczą to samo, a
# cztery kopie tej listy to trzy miejsca do zapomnienia przy dopisaniu frazy.
#
# ZABEZPIECZENIE, BEZ KTÓREGO TO BYŁBY NAJDROŻSZY BŁĄD W TYM REPO: odrzucamy
# TYLKO wtedy, gdy w treści NIE MA ŻADNEGO SYGNAŁU POPYTU. „Jadę do Warszawy,
# auto stanęło na A4" ma frazę oferty („jadę do") i jest zleceniem — ratuje je
# sygnał awarii z tabeli `POPYT`. Dlatego `POPYT` jest CELOWO SZEROKI i wolno go
# rozdmuchiwać bez namysłu: każdy jego wpis wyłącznie POWSTRZYMUJE odrzucenie.
# Cena za szeroki POPYT to oferta puszczona do modelu (ułamek grosza — model ma
# własne pole `kierunek` i tam ją zatrzyma); cena za wąski to skasowane
# zlecenie, o którym nikt się nie dowie.
#
# `POPYT` NIE JEST WYPROWADZONY Z TABEL PRZEPUSZCZENIA i to jest decyzja, nie
# przeoczenie. Tamte zawierają dokładnie te frazy dwuznaczne, przed którymi ten
# mechanizm ma bronić („wolne miejsce"), więc wyprowadzenie z nich unieważniłoby
# całość. Odpowiada na inne pytanie: nie „czy warto zapytać model", tylko „czy
# autor czegoś POTRZEBUJE".
# ===========================================================================

# Wartości kolumny `kierunek` — trzy i tylko trzy, tak samo jak przy kategorii
# ładunku. Te same trzy zwraca klasyfikator (workers/classifier.py), bo to jedno
# pole i jeden słownik wartości; model może zdanie tylko doczytać lepiej.
KIERUNEK_ZLECENIE = "zlecenie"
KIERUNEK_OFERTA = "oferta"
KIERUNEK_NIEJASNY = "niejasne"

# SYGNAŁY POPYTU — autor czegoś potrzebuje. Bije ofertę zawsze i bez wyjątku.
POPYT: list[tuple[str, int, str]] = [
    # --- prośba wprost (PL) ---
    (r"szuka(m|my)", 0, "POPYT"),
    (r"poszukuj(e|emy)", 0, "POPYT"),
    (r"potrzebuj(e|emy)", 0, "POPYT"),
    (r"potrzebn[aeyo]", 0, "POPYT"),
    (r"prosz(e|ba) o", 0, "POPYT"),
    (r"zlec(e|am|enie|enia)", 0, "POPYT"),
    (r"kto (mi )?(przewiezie|przywiezie|zawiezie|dowiezie|odholuje|zabierze|"
     r"sciagnie|podjedzie|pomoze|ma czas|wezmie|odbierze|podskoczy)", 0, "POPYT"),
    (r"(ma ktos|kto ma) lawete", 0, "POPYT"),
    (r"(jest )?ktos (w okolicy|z okolic|z laweta|obok)", 0, "POPYT"),
    (r"polec(icie|acie|isz|i mi|a mi)", 0, "POPYT"),
    (r"kogo (polecacie|polecicie)", 0, "POPYT"),
    (r"znacie (kogos|jakas|jakies|firme|dobra)", 0, "POPYT"),
    # Forma bezokolicznikowa z giełdy — po stronie POPYTU tak samo jednoznaczna
    # jak w warstwie 2 („Do przywiezienia Citroen Berlingo z 18556 do 63-505").
    (r"do (przywiezienia|przywozu|zabrania|odebrania|odbioru|przewiezienia|"
     r"przewozu|sciagniecia|podjecia|zawiezienia|dowiezienia|"
     r"przetransportowania|zaladowania)", 0, "POPYT"),
    # PYTANIE o czyjś kurs to prośba o doładunek, czyli popyt — mimo że mówi
    # o cudzej jeździe. „Kto jedzie" i „jadę" to dwie różne strony rynku.
    (r"kto (jedzie|wraca|bedzie|jechal|wracal|mogl)", 0, "POPYT"),

    # --- awaria i zdarzenie (PL) ---
    # NAJWAŻNIEJSZA CZĘŚĆ TEJ TABELI. Post o zepsutym aucie nigdy nie jest
    # ofertą przewoźnika, a bardzo często zawiera frazę z `OFERTA` („jadę do
    # Warszawy i stanąłem na A4"). Bez tych wpisów odrzucalibyśmy zlecenia
    # awaryjne, czyli te, które trzeba obsłużyć najszybciej.
    (r"awari(a|i|e)", 0, "POPYT"),
    (r"zepsu(l|la|lo|ty|ta|te|l sie|l mi sie)", 0, "POPYT"),
    (r"unieruchomion[yae]", 0, "POPYT"),
    (r"nie (odpala|odpali|pali|zapala|jezdzi|dziala|rusza|ruszy|"
     r"chce jechac|chce odpalic|na chodzie)", 0, "POPYT"),
    (r"(auto |pojazd |samochod )?niejezdzac[ey]", 0, "POPYT"),
    (r"po (szkodzie|wypadku|stluczce|kolizji)", 0, "POPYT"),
    (r"(auto )?powypadkow[ey]", 0, "POPYT"),
    (r"uszkodzon[ye]", 0, "POPYT"),
    (r"dachowa(lem|l|lo)", 0, "POPYT"),
    (r"zdech(l|la|lo|l mi)", 0, "POPYT"),
    (r"stan(alem|elam|al mi|ela mi)", 0, "POPYT"),
    (r"(w|do) row(ie|u)", 0, "POPYT"),
    (r"holowani[ae]|odholowa(c|nie|nia)", 0, "POPYT"),

    # --- kupno auta, czyli transport planowany (PL) ---
    (r"kupil[ae]m", 0, "POPYT"),
    (r"kupione auto", 0, "POPYT"),
    (r"sprowadz(enie|am|ilem|ilam)", 0, "POPYT"),
    (r"odbior (auta|samochodu)", 0, "POPYT"),
    (r"(z|spod) komisu", 0, "POPYT"),
    (r"z (aukcji|autohausu)", 0, "POPYT"),
    (r"od dealera", 0, "POPYT"),

    # --- DE ---
    (r"such(e|en|t)", 0, "POPYT"),
    (r"gesucht", 0, "POPYT"),
    (r"brauch(e|en|t)", 0, "POPYT"),
    # Obie pisownie umlautu — patrz nota przy „Rückfahrt" w OFERTA.
    (r"ben(o|oe)tig(e|t|en)", 0, "POPYT"),
    (r"wer (kann|hat|holt|bringt|fahrt)", 0, "POPYT"),
    (r"bitte um", 0, "POPYT"),
    (r"panne|kaputt|defekt|totalschaden|unfall", 0, "POPYT"),
    (r"(springt|startet|fahrt) nicht", 0, "POPYT"),
    (r"liegen ?geblieben", 0, "POPYT"),
    (r"nicht (fahrbereit|fahrtuchtig)", 0, "POPYT"),
    (r"gekauft", 0, "POPYT"),

    # --- CS / SK ---
    (r"hleda(m|me)|hlada(m|me)", 0, "POPYT"),
    (r"potrebuj(i|u|e|em|eme)", 0, "POPYT"),
    # „zháním/sháním" (cs) i „zháňam" (sk) — te same czasowniki szukania,
    # których warstwa 2 używa w parze z rzeczownikiem transportu.
    (r"zhani(m|me)|zhana(m|me)|shani(m|me)", 0, "POPYT"),
    (r"prosim o", 0, "POPYT"),
    (r"(kdo|kto) (pomuze|pomoze|privezie|priveze|preveze|prevezie|odveze|"
     r"odvezie|ma|jede|pojede)", 0, "POPYT"),
    (r"poruch[auy]", 0, "POPYT"),
    (r"nepojizdn[a-z]*|nepojazdn[a-z]*", 0, "POPYT"),
    (r"ne(startuje|nastartuje)", 0, "POPYT"),
    (r"nehod[aeuy]|havari[aey]", 0, "POPYT"),
    (r"(koupil jsem|kupil som)", 0, "POPYT"),
]

# SYGNAŁY OFERTY — autor sprzedaje WŁASNY przejazd. Odrzucamy TYLKO gdy wyżej
# nie padł żaden sygnał popytu.
OFERTA: list[tuple[str, int, str]] = [
    # --- wolne miejsce oferowane (PL) ---
    (r"wolna laweta", 0, "OFERTA"),
    (r"wolny transport", 0, "OFERTA"),
    (r"wolne miejsc[ae]", 0, "OFERTA"),
    (r"mam wolne", 0, "OFERTA"),
    (r"mam miejsce", 0, "OFERTA"),
    # Jedno słowo luki, tak samo jak przy „szukam wolnego JEDNEGO miejsca"
    # w warstwie 2: między czasownikiem a rzeczownikiem ludzie wtrącają
    # liczebnik („zostało jedno miejsce", „zostały dwa miejsca").
    (r"zosta(lo|ly)( [a-z]+)? miejsc[ae]", 0, "OFERTA"),
    (r"jedno miejsce wolne", 0, "OFERTA"),
    (r"doladunek wolny", 0, "OFERTA"),

    # --- własna trasa (PL) ---
    # „jadę" kontra „kto jedzie" — jedna litera różnicy w brzmieniu, dwie różne
    # strony rynku. Ta druga forma stoi w `POPYT` i wygrywa.
    (r"jade (z|ze|do|na trasie|w kierunku)", 0, "OFERTA"),
    (r"wracam (z|ze|do)", 0, "OFERTA"),
    (r"powrot (z|ze|do)", 0, "OFERTA"),
    (r"trasa dnia", 0, "OFERTA"),
    (r"kursuje", 0, "OFERTA"),

    # --- podejmę ładunek (PL) ---
    (r"podejme (ladunek|transport|zlecenie|kurs)", 0, "OFERTA"),
    (r"przyjme (ladunek|transport|zlecenie|kurs)", 0, "OFERTA"),
    (r"zabiore po drodze", 0, "OFERTA"),
    (r"moge zabrac", 0, "OFERTA"),
    (r"moge dolozyc", 0, "OFERTA"),

    # --- DE ---
    # OBIE PISOWNIE UMLAUTU, i to nie jest nadmiarowość: normalizacja zbija
    # „Rückfahrt" do „ruckfahrt", ale NIE tyka zastępczego „Rueckfahrt", które
    # Niemcy piszą z telefonu bez niemieckiej klawiatury. Wzorzec na jedną formę
    # milczy przy drugiej i nie ma jak tego zauważyć — post po prostu przechodzi.
    (r"frei(er|e) (platz|platze|plaetze)", 0, "OFERTA"),
    (r"habe (noch )?platz", 0, "OFERTA"),
    (r"fahre (am|nach|von|heute|morgen)", 0, "OFERTA"),
    (r"r(u|ue)ckfahrt", 0, "OFERTA"),
    (r"leerfahrt", 0, "OFERTA"),

    # --- CS / SK ---
    (r"volne (misto|miesto)", 0, "OFERTA"),
    (r"jedu (z|ze|do)", 0, "OFERTA"),
    (r"volny odtah", 0, "OFERTA"),
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
    # Czasownik potrzeby + „Transport" istniał tu tylko w wariancie „brauche".
    # „Suche Transport für ..." i „Benötige Transport" spadały do punktacji
    # i przy progu 5 wylatywały — ta sama dziura co w CS/SK, ten sam powód.
    # Prawa granica słowa (dokładana przez `_skompiluj`) odcina „Transporte"
    # i „Transportaufträge", czyli przewoźnika szukającego ładunków.
    (r"suche (einen )?transport", 0, "prosba wprost"),
    # OBIE PISOWNIE UMLAUTU — jak przy „Rückfahrt" w OFERTA: normalizacja zbija
    # „benötige" do „benotige", ale NIE tyka zastępczego „benoetige".
    (r"ben(o|oe)tig(e|en) (einen |eine )?(transport|abschlepp[a-z]*)",
     0, "prosba wprost"),
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
    # OBA NASTĘPNE SĄ WARUNKOWE — z tego samego powodu co polskie „wolne
    # miejsce" (patrz nota tam). „Rückfahrt" i „noch Platz frei" pisze zarówno
    # klient szukający doładunku, jak i przewoźnik reklamujący własny powrót;
    # rozstrzyga KIERUNEK (`POPYT` / `OFERTA`), sprawdzany przed warstwami.
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
    #
    # CZASOWNIK POTRZEBY/SZUKANIA + RZECZOWNIK TRANSPORTU w jednym wpisie.
    # Pierwsza wersja znała ten czasownik WYŁĄCZNIE w parze z „odtah" — więc
    # „Potrebujem prepravu auta z Bratislavy do Kosic", czyli najczęstsza forma
    # zlecenia na tym rynku, nie trafiała tu w nic i wisiała w punktacji na
    # samym „prepravu" (+3): przy progu 3 przechodziła ledwo, przy 5 wylatywała.
    # Para „potrzebuję/szukam + przewóz" jest po stronie popytu dokładnie tak
    # samo jednoznaczna jak z „odtah" — reklama konkurencji zaczyna się od
    # „nabizime/ponukame" (warstwa 3), nie od czasownika potrzeby.
    #
    # Alternatywy wypisane PŁASKO, bez grupy w grupie: „))" jest powtórzoną
    # interpunkcją, którą normalizacja zbija do „)" — a wzorzec musi być równy
    # własnej formie znormalizowanej (test_wzorce_obcojezyczne_sa_znormalizowane).
    (r"(potrebuji|potrebuju|potrebuje|potrebujem|potrebujeme|hledam|hledame|"
     r"hladam|hladame|zhanim|zhanime|zhanam|zhaname|shanim|shanime) "
     r"(odtah[a-z]*|prepravu|prepravy|prevoz|prevozu|odvoz|odvozu|dopravu|"
     r"transport[a-z]*)", 0, "prosba wprost"),
    # To samo z bezokolicznikiem zamiast rzeczownika: „potrebujem prepravit",
    # „potrebuji prevezt", „potrebujem odviezt". Goły bezokolicznik przewozu
    # („prevezt|previezt") stoi niżej i łapie formy bez czasownika potrzeby.
    (r"(potrebuji|potrebuju|potrebuje|potrebujem|potrebujeme) "
     r"(prepravit|prevezt|previezt|odvezt|odviezt|dopravit)",
     0, "prosba wprost"),
    (r"prosim o odtah", 0, "prosba wprost"),
    (r"kdo pomuze", 0, "prosba wprost"),
    (r"kto pomoze", 0, "prosba wprost"),
    (r"(kdo|kto) ma (volno|volny cas)", 0, "prosba wprost"),
    # „preveze/prevezie" obok „priveze": „hledam nekoho kdo preveze" /
    # „hladam niekoho kto preveze" to pytanie o PRZEWÓZ, nie o przywóz,
    # i bez tej pary spadało do punktacji.
    (r"(kdo|kto) (privezie|priveze|preveze|prevezie|odveze|odvezie)",
     0, "prosba wprost"),
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
    (r"nehod[aeuy]", 0, "zdarzenie drogowe"),  # nehoda / „po nehodě" -> „po nehode"
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
    """Kompilacja RAZ, przy imporcie — bramka stoi na ścieżce każdego posta.

    MARKI i ŹRÓDŁA AUT dokładamy TUTAJ, a nie w każdej liście z osobna: „Citroen"
    i „Copart" znaczą to samo we wszystkich czterech językach, a cztery kopie
    tej samej listy to trzy miejsca do zapomnienia przy dopisaniu marki.
    """
    return Slownik(
        jezyk=jezyk,
        wygaszenie=_skompiluj_liste(wygaszenie),
        przepuszczenie=_skompiluj_liste(przepuszczenie),
        odrzucenie=_skompiluj_liste(odrzucenie),
        punktacja=_skompiluj_liste([*punktacja, *MARKI, *ZRODLA_AUT]),
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
# KATEGORIA ŁADUNKU — co miałoby jechać na lawecie
#
# ODPOWIEDŹ JEST NIEZALEŻNA OD WERDYKTU i liczona osobno, poza czterema
# warstwami. Powód jest praktyczny: warstwy 1-3 kończą pracę natychmiast po
# trafieniu, więc post przepuszczony przez „szukam transportu" NIGDY nie
# dochodzi do punktacji i nie ma z czego odczytać, czy chodziło o auto, czy
# o konia. Kategoria musi działać na KAŻDEJ ścieżce, także tej najkrótszej.
#
# Wzorce pojazdu bierzemy z PUNKTACJI wszystkich słowników (etykieta „POJAZD"),
# a nie z osobnej listy — inaczej dopisanie marki w jednym miejscu zmieniałoby
# punkty, ale nie kategorię, i po pół roku te dwie listy mówiłyby co innego.
# ---------------------------------------------------------------------------
_ZWIERZETA = _skompiluj_liste(ZWIERZETA)

_WZORCE_POJAZDU: list[re.Pattern[str]] = [
    wzorzec
    for _znacznik, slownik in _rozne_slowniki()
    for wzorzec, _waga, etykieta in slownik.punktacja
    if etykieta == "POJAZD"
]


def _kategoria(tekst: str) -> tuple[str, list[str]]:
    """Kategoria ładunku + ślad, na treści JUŻ znormalizowanej.

    ZWIERZĘ BIJE POJAZD i to nie jest przeoczenie: „potrzebny transport busem
    jednego konia" mówi o obu naraz, bo pojazd jest tu ŚRODKIEM transportu,
    a nie ładunkiem. Odwrotna kolejność oznaczałaby taki post jako „pojazd"
    i cały ten mechanizm byłby ozdobnikiem.

    Ślad wraca razem z kategorią, bo operator patrzący na wyszarzone zlecenie
    ma prawo wiedzieć, KTÓRE słowo je tak oznaczyło — inaczej znacznik „zwierzę"
    przy transporcie rębaka wygląda na awarię, a nie na trafioną literówkę
    języka polskiego.

    „inne" ZNACZY „nie rozpoznałem słowa oznaczającego pojazd", a nie „to nie
    jest pojazd" — i nic w systemie nie traktuje go inaczej niż „pojazd".
    Jedyną wartością, która cokolwiek zmienia, jest „zwierze"; pozostałe dwie
    są informacją dla człowieka i materiałem do statystyki.
    """
    for wzorzec, _waga, etykieta in _ZWIERZETA:
        if wzorzec.search(tekst):
            return KAT_ZWIERZE, [_etykieta(etykieta, wzorzec)]
    for wzorzec in _WZORCE_POJAZDU:
        if wzorzec.search(tekst):
            return KAT_POJAZD, []
    return KAT_INNE, []


def kategoria_ladunku(tresc: str) -> str:
    """Kategoria ładunku dla SUROWEJ treści — "pojazd" | "zwierze" | "inne".

    Publiczna, bo pyta o nią `scripts/raport_gate.py` — liczy kategorię z TREŚCI,
    więc odpowiada także dla wierszy sprzed migracji 0010, w których kolumna jest
    pusta. `gate()` woła wariant wewnętrzny, żeby nie normalizować tej samej
    treści drugi raz.
    """
    return _kategoria(normalizuj(tresc))[0]


# ---------------------------------------------------------------------------
# KIERUNEK ZGŁOSZENIA — liczony raz, na treści znormalizowanej, poza słownikami.
#
# Tak samo jak kategoria ładunku: wzorce są wspólne dla czterech języków, a
# odpowiedź musi istnieć na KAŻDEJ ścieżce — także tej, na której post został
# odrzucony i nigdy nie zobaczy modelu. To właśnie te posty mają zostać w bazie
# z `kierunek='oferta'`, bo tylko z nich da się kiedyś policzyć, ile kursów
# przejechało obok z wolnym miejscem na trasie, którą operator i tak jedzie.
# ---------------------------------------------------------------------------
_POPYT = _skompiluj_liste(POPYT)
_OFERTA = _skompiluj_liste(OFERTA)

# ZNAK ZAPYTANIA JEST SYGNAŁEM POPYTU. Pytanie jest prośbą — przewoźnik
# ogłaszający własne wolne miejsce nie pyta, tylko podaje trasę i telefon
# („Wolna laweta Elbląg-Lublin tel. 501606207"). Bez tej reguły odpadałby
# produkcyjny „Wolne miejsce w ten piątek?", czyli klient pytający, czy ktoś
# ma miejsce — post bez ANI JEDNEGO czasownika, więc nie do uratowania żadnym
# wzorcem z `POPYT`.
#
# Osobno od tabeli, bo to nie jest fraza: `_skompiluj` dokleja lewą granicę
# słowa, a po „piątek" znak zapytania stoi tuż za literą i wzorzec nie
# trafiłby NIGDY (patrz nota o prawej granicy przy `_skompiluj`).
#
# Cena: oferta zapisana jako pytanie („Wolne miejsce Kraków-Berlin, ktoś
# chętny?") idzie do modelu zamiast odpaść tutaj. To ułamek grosza, a model ma
# na nią własne pole `kierunek`. Cena pomyłki w drugą stronę to kurs.
_PYTANIE = re.compile(r"\?")


def _kierunek(tekst: str) -> tuple[str, list[str]]:
    """Kierunek zgłoszenia + ślad, na treści JUŻ znormalizowanej.

    POPYT SPRAWDZAMY PIERWSZY i to jest cała bezpieczna strona tego mechanizmu.
    Post, w którym autor czegokolwiek potrzebuje, NIE JEST ofertą — nawet gdy
    zawiera frazę oferty („jadę do Warszawy, auto stanęło na A4"). Odwrotna
    kolejność kasowałaby zlecenia awaryjne, czyli te najpilniejsze.

    OBA SYGNAŁY NARAZ TO "niejasne". Bramka naprawdę nie wie — a „niejasne"
    niczego nie odrzuca i niczego nie wycisza, więc post idzie do modelu, który
    przeczyta zdanie zamiast dopasowywać wzorce. Udawanie tu pewności byłoby
    zgadywaniem w jedyną stronę, która kosztuje kurs.

    Ślad wraca razem z kierunkiem z tego samego powodu co przy zwierzętach:
    człowiek patrzący na odrzucony post ma prawo wiedzieć, KTÓRE słowo go tak
    oznaczyło.
    """
    wzorzec_popytu = next((w for w, _waga, _e in _POPYT if w.search(tekst)), None)
    oferta = next((w for w, _waga, _e in _OFERTA if w.search(tekst)), None)
    popyt = (_etykieta("POPYT", wzorzec_popytu) if wzorzec_popytu is not None
             else ("POPYT:pytanie" if _PYTANIE.search(tekst) else None))
    if popyt is not None and oferta is not None:
        return KIERUNEK_NIEJASNY, [popyt, _etykieta("OFERTA", oferta)]
    if popyt is not None:
        return KIERUNEK_ZLECENIE, [popyt]
    if oferta is not None:
        return KIERUNEK_OFERTA, [_etykieta("OFERTA", oferta)]
    return KIERUNEK_NIEJASNY, []


def kierunek(tresc: str) -> str:
    """Kierunek dla SUROWEJ treści — "zlecenie" | "oferta" | "niejasne".

    Publiczna z tego samego powodu co `kategoria_ladunku`: raport z trybu cienia
    (scripts/raport_gate.py) liczy kierunek z TREŚCI, więc odpowiada także dla
    wierszy sprzed migracji 0011, w których kolumna jest pusta.
    """
    return _kierunek(normalizuj(tresc))[0]


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
    # CO miałoby jechać: "pojazd" | "zwierze" | "inne". NIE wpływa ani na
    # `werdykt`, ani na `punkty` — post o koniach przechodzi bramkę tak samo jak
    # każdy inny. Zmienia tylko to, co widać w panelu (znacznik i miejsce na
    # liście) oraz czy brzęczy telefon (ALERT_ZWIERZETA w .env).
    #
    # Domyślne "inne", a nie "pojazd": wynik zbudowany ręcznie (test, skrypt) nie
    # oglądał treści, więc nie ma prawa twierdzić, że widział auto.
    kategoria_ladunku: str = KAT_INNE
    # KTO KOGO SZUKA: "zlecenie" | "oferta" | "niejasne". W odróżnieniu od
    # kategorii ładunku to pole ODRZUCA: "oferta" znaczy „autor sprzedaje własny
    # przejazd", czyli konkurencja, a nie klient. Post i tak ląduje w bazie
    # (`zrodlo_decyzji='gate'`, `czy_zlecenie=false`) razem z tą wartością —
    # oferta na trasie, którą operator i tak jedzie, bywa okazją na doładunek,
    # więc dane zostają, cichnie tylko telefon (ALERT_OFERTY w .env).
    #
    # Domyślne "niejasne", nie "zlecenie": wynik zbudowany ręcznie nie oglądał
    # treści, więc nie ma prawa twierdzić, po której stronie rynku stoi autor.
    kierunek: str = KIERUNEK_NIEJASNY


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
    # DWA RÓŻNE KODY POCZTOWE = TRASA. Waga wyższa niż jakiegokolwiek pojedynczego
    # słowa (+4), bo para kodów jest w tych postach praktycznie definicją kursu —
    # i bywa JEDYNYM sygnałem, gdy autor napisał samo „z 18556 do 63-505".
    # Liczone tu, a nie w tabeli wzorców, bo warunkiem jest LICZBA trafień,
    # a nie trafienie — tak samo jak przy numerze telefonu wyżej.
    kody = kody_pocztowe(tekst)
    if len(kody) >= 2:
        punkty += WAGA_DWA_KODY
        kategorie.add("TRASA")
        trafienia.append(
            f"TRASA:dwa kody pocztowe ({', '.join(kody[:2])})({WAGA_DWA_KODY:+d})")
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
    z kursem straconym przez odrzucenie.

    WŚRÓD PRZEPUSZCZEŃ NAJPIERW NAZWANA REGUŁA (warstwy 1-3), potem wyższa
    punktacja, na końcu zgodność z detekcją. Kolejność tych dwóch pierwszych
    kryteriów jest odwrotna niż w pierwszej wersji i to jest poprawka realnego
    błędu: wzorce NIEZALEŻNE OD JĘZYKA (marki, kody pocztowe) punktują w KAŻDYM
    słowniku, więc polski „Do zabrania iveco … do 08-110 siedlce" wygrywał
    słownikiem NIEMIECKIM — polskie twarde przepuszczenie ma z definicji zero
    punktów, a niemiecki dokładał marce i kodom siódemkę. Skutek widział
    operator: flaga 🇩🇪 przy polskim zleceniu, czyli podpowiedź, żeby zadzwonić
    po niemiecku (docs/WIELOJEZYCZNOSC.md), i „punktacja 7 >= prog" w raporcie
    zamiast nazwy reguły, która naprawdę zadziałała.

    Trafienie w nazwaną regułę jest przy tym MOCNIEJSZYM sygnałem niż suma wag:
    znaczy „ten słownik rozpoznał tu zgłoszenie", a nie „nazbierało się punktów".

    WŚRÓD ODRZUCEŃ wygrywa uzasadnienie NAJBARDZIEJ KONKRETNE: najpierw to,
    w którym zadziałała nazwana reguła, potem NIŻSZA punktacja. Odwrotnie niż
    przy przepuszczeniach — i to jest celowe. Czeska reklama odrzucona przez
    słownik czeski jako „autopromocja" niesie do raportu z trybu cienia
    informację, z której da się kalibrować próg; ta sama reklama odrzucona przez
    słownik polski jako „punktacja 0 < prog 5" nie niesie żadnej.
    """
    werdykt, punkty, powod, trafienia, znacznik = wynik
    zgodny_z_detekcja = 1 if (wykryty and znacznik == wykryty) else 0
    # „punktacja …" to jedyny powód, którego NIE wystawia nazwana reguła —
    # składa go `_ocen` z sumy wag (patrz koniec tamtej funkcji).
    nazwana_regula = 0 if powod.startswith("punktacja") else 1
    # Oba warianty mają tę samą długość, żeby porównanie nigdy nie zależało od
    # kolejności argumentów (krotki różnej długości porównują się poprawnie
    # tylko dopóki różnią się wcześniej — a to jest założenie, które łatwo
    # złamać przy kolejnej zmianie kryteriów).
    if werdykt:
        return (1, nazwana_regula, punkty, zgodny_z_detekcja)
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

    KIERUNEK ROZSTRZYGA SIĘ PRZED SŁOWNIKAMI i jako jedyny stoi ponad ich
    kolejnością warstw. Powód jest konkretny, nie porządkowy: frazy dwuznaczne
    („wolne miejsce", „Rückfahrt") leżą w warstwie 2, czyli w przepuszczeniu,
    które bije odrzucenie — więc oferta przewoźnika wychodziłaby stąd jako
    zgłoszenie z giełdy, zanim jakikolwiek wzorzec odrzucenia doszedłby do
    głosu. Sam mechanizm jest bezpieczny w tę samą stronę co reszta modułu:
    odrzuca WYŁĄCZNIE przy zerowym sygnale popytu (patrz `_kierunek`).
    """
    prog = settings.GATE_PROG if prog is None else prog
    tryb = normalizuj_tryb(settings.GATE_TRYB if tryb is None else tryb)
    oryginal = (tresc or "").strip()
    tekst = normalizuj(tresc)

    # Kategoria ładunku i kierunek liczone NIEZALEŻNIE od słowników i od tego,
    # która warstwa rozstrzygnęła — oba mają wartość na każdej ścieżce, także
    # tej najkrótszej. Bez tego post odrzucony jako oferta wyszedłby stąd bez
    # kierunku, czyli bez jedynej informacji, która go tłumaczy.
    kategoria, slad_kategorii = _kategoria(tekst)
    kier, slad_kierunku = _kierunek(tekst)

    if kier == KIERUNEK_OFERTA:
        # OFERTA KONKURENCJI — autor sprzedaje własny przejazd. Ma komplet cech
        # zlecenia (trasa, data, telefon), więc punktacja przepuściłaby go bez
        # mrugnięcia; różnica jest w kierunku, nie w słowach. Nie pytamy o niego
        # modelu, a mimo to WSZYSTKO o nim zostaje: wołający zapisze wiersz
        # z `kierunek='oferta'` i `czy_zlecenie=false` (workers/fb_fetcher.py).
        return GateResult(
            przepusc=True if tryb == TRYB_CIEN else False,
            punkty=0,
            powod="oferta przewoznika",
            trafienia=[*slad_kierunku, *slad_kategorii],
            werdykt=False,
            tryb=tryb,
            # Bez przebiegu słownikami nie mamy zwycięzcy, z którego bierze się
            # znacznik języka — zostaje sama detekcja. Pusta wartość jest tu
            # poprawną odpowiedzią („nie rozstrzygnięto"), a nie brakiem:
            # odrzucona oferta i tak nie trafi do nikogo, kto ma oddzwonić.
            jezyk=wykryj_jezyk(oryginal),
            kategoria_ladunku=kategoria,
            kierunek=kier,
        )

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

    # Kategoria ładunku i kierunek policzone są WYŻEJ, przed słownikami — oba
    # są wspólne dla wszystkich języków, a post przepuszczony przez warstwę 2
    # nigdy nie dochodzi do punktacji, więc nie ma z czego ich odczytać później.
    # Ślad kierunku dokładamy tylko wtedy, gdy coś powiedział: „niejasne" bez
    # trafienia znaczy „żaden wzorzec nie padł" i nie ma czego pokazywać.
    trafienia = [*trafienia, *slad_kierunku, *slad_kategorii]

    return GateResult(
        przepusc=True if tryb == TRYB_CIEN else werdykt,
        punkty=punkty,
        powod=powod,
        trafienia=trafienia,
        werdykt=werdykt,
        tryb=tryb,
        jezyk=znacznik,
        kategoria_ladunku=kategoria,
        kierunek=kier,
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
    gate_werdykt      = %(gate_werdykt)s,
    gate_punkty       = %(gate_punkty)s,
    gate_powod        = %(gate_powod)s,
    gate_trafienia    = %(gate_trafienia)s,
    gate_tryb         = %(gate_tryb)s,
    gate_jezyk        = %(gate_jezyk)s,
    kategoria_ladunku = %(kategoria_ladunku)s,
    kierunek          = %(kierunek)s,
    gate_at           = NOW()
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
        # Kategoria ładunku też jedzie do bazy, i to jest cały sens rozdzielenia
        # jej od odrzucenia: post o transporcie konia ZOSTAJE w tabeli razem
        # z powodem, dla którego jest wyszarzony. Twarde odrzucenie kasowałoby
        # tę informację bezpowrotnie i bez śladu — a gdyby operator dokupił
        # przyczepę do koni, dane potrzebne do decyzji już tu czekają.
        "kategoria_ladunku": wynik.kategoria_ladunku or None,
        # Kierunek z DOKŁADNIE tego samego powodu, tylko po drugiej stronie:
        # oferta konkurencji jest odrzucana, ale nie kasowana. Wiersz zostaje
        # z `kierunek='oferta'`, bo cudzy kurs na trasie, którą operator i tak
        # jedzie, bywa okazją na doładunek albo na podnajęcie — a tego nie da
        # się zobaczyć w danych, których się nie zapisało.
        "kierunek": wynik.kierunek or None,
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
    print(f"ŁADUNEK:        {w.kategoria_ladunku}"
          + ("   (idzie do panelu ze znacznikiem i NIŻEJ na liście; alert tylko "
             "przy ALERT_ZWIERZETA=1)" if w.kategoria_ladunku == KAT_ZWIERZE else ""))
    # Kierunek obok werdyktu, bo to jedyny powód odrzucenia, którego NIE widać
    # w punktacji: oferta przewoźnika ma komplet cech zlecenia i różni się samą
    # stroną rynku.
    print(f"KIERUNEK:       {w.kierunek}"
          + ("   (oferta konkurencji — zapisujemy do bazy, ale bez alertu; "
             "ALERT_OFERTY=1 przywraca brzęczenie)"
             if w.kierunek == KIERUNEK_OFERTA else ""))
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
