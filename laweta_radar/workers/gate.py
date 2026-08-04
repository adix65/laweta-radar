"""Bramka słowna — tani filtr PRZED modelem. Wielojęzyczna: PL / DE / CS / SK.

Jedyne zadanie: odsiać posty, za których klasyfikację nie warto płacić. Bramka
NIE decyduje, czy coś jest zleceniem — decyduje, czy warto o to zapytać model.
Pomyłka bramki w jedną stronę kosztuje ułamek grosza (niepotrzebne wywołanie
Anthropica), a w drugą — całe zlecenie. Cała punktacja jest ustawiona pod tę
asymetrię i każda zmiana progu musi ją uwzględniać.

--- DLACZEGO WIELE JĘZYKÓW -------------------------------------------------

Bramka jednojęzyczna jest cicha i śmiertelna. Niemiecki post „Suche
Autotransport von München nach Krakau, Fahrzeug fährt nicht" to wzorcowe
zlecenie — i przy polskich wzorcach nie trafia ANI JEDNEGO, dostaje zero
punktów i wylatuje. Nic tego nie zgłosi: w logach wygląda identycznie jak
odrzucona reklama felg. Dlatego wzorce są rozdzielone na słowniki per język,
a nie sklejone w jedną listę — sklejone dawałyby fałszywe trafienia między
językami bliskimi (czeskie „nehoda" to wypadek, ale polskie „nie ha..." to nic).

CZESKI I SŁOWACKI dzielą JEDEN słownik, z wariantami obu języków w środku.
Rozdzielenie ich dałoby dwie listy różniące się w połowie pozycji jedną literą
(„odtahovka" / „odťahovka"), czyli dwa miejsca do zapomnienia przy każdej
zmianie. Detekcja języka nadal je ROZRÓŻNIA — bo od tego zależy, w jakim języku
operator ma oddzwonić, a to jest informacja dla człowieka, nie dla filtra.

--- WARSTWY I WAGI ----------------------------------------------------------

Każdy słownik ma te same trzy warstwy i te same wagi:

  przepuszczenie  (+3)  ktoś PROSI o lawetę albo opisuje awarię
  odrzucenie      (-2)  ktoś OFERUJE usługę, sprzedaje, szuka pracownika
  wygaszenie    (-100)  sprawa już załatwiona — post jest martwy

Waga odrzucenia jest MNIEJSZA od wagi przepuszczenia i to jest celowe: „szukam
lawety, oferuję dobrą zapłatę" ma trafić do modelu, a nie wylecieć na słowie
„oferuję". Jedna reklama z jednym słowem-kluczem przechodzi (3-2=1 < próg),
prawdziwe zgłoszenie z jednym słowem reklamowym zwykle nie — bo ma po dwa,
trzy trafienia w warstwie przepuszczenia.

Wygaszenie jest wagą, nie osobnym mechanizmem, ale w praktyce działa jak weto:
-100 nie da się odrobić żadną liczbą trafień. Trzymamy to jako wagę, żeby
struktura warstw była identyczna we wszystkich językach i żeby dało się ją
zmienić w jednym miejscu.

TO SAMO ZDANIE LICZY SIĘ RAZ. Liczymy trafienia RÓŻNYCH wzorców, nie wystąpień —
inaczej post „laweta laweta laweta" wygrywałby ze zgłoszeniem napisanym po
ludzku.

--- DOPASOWANIE BEZ ZNAKÓW DIAKRYTYCZNYCH -----------------------------------

Wzorce i treść porównujemy PO UPROSZCZENIU (bez ogonków, umlautów, haczków).
Ludzie piszą z telefonu, w stresie, przy zepsutym aucie na poboczu — „potrzebuje
lawety" bez ogonka jest normą, nie wyjątkiem, a bramka bez tej normalizacji
gubiłaby dokładnie te posty, które są najpilniejsze.

Detekcja języka działa ODWROTNIE — na tekście z zachowanymi znakami, bo to
właśnie one są najmocniejszą przesłanką. Stąd dwa różne widoki tego samego
tekstu w jednym module; to nie jest niedopatrzenie.

--- CO ROBI, GDY NIE WIE ----------------------------------------------------

Przy niepewnej detekcji sprawdzamy WSZYSTKIMI słownikami i bierzemy najwyższy
wynik. Cztery przebiegi regeksów po krótkim tekście to mikrosekundy, a zgubione
zlecenie to kilkaset złotych — przy tej asymetrii nie ma czego optymalizować.

Bramka nie ma dostępu do sieci, nie importuje żadnej biblioteki do detekcji
języka i nie tłumaczy. Tłumaczenie należy do klasyfikatora (patrz
INSTRUKCJA_JEZYKOWA_DLA_KLASYFIKATORA na końcu modułu).

CLI — sprawdzenie pojedynczego posta bez odpalania pipeline'u:
    python -m laweta_radar.workers.gate "Suche Abschleppdienst, Motor kaputt"
    echo "treść posta" | python -m laweta_radar.workers.gate
"""
from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Wagi warstw i próg. Jedno miejsce — patrz uzasadnienie w docstringu modułu.
# ---------------------------------------------------------------------------
WAGA_PRZEPUSZCZENIE = 3
WAGA_ODRZUCENIE = -2
WAGA_WYGASZENIE = -100

# Próg równy jednej wadze przepuszczenia: JEDNO trafienie w warstwie zgłoszeń
# wystarczy, żeby zapytać model. Próg wyższy oznaczałby, że post „Zepsułem się
# na S19, ktoś podjedzie?" (jedno trafienie) wylatuje bez pytania.
PROG = 3


@dataclass(frozen=True)
class Slownik:
    """Komplet wzorców dla jednego języka. Ta sama struktura we wszystkich.

    `jezyk` to etykieta słownika, NIE zawsze język posta: słownik czesko-słowacki
    obsługuje dwa języki i wynik dostaje znacznik z detekcji, nie stąd.
    """

    jezyk: str
    przepuszczenie: tuple[str, ...]
    odrzucenie: tuple[str, ...]
    wygaszenie: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    """Werdykt bramki dla jednego posta.

    `jezyk` to dwuliterowy znacznik ("pl"/"de"/"cs"/"sk") albo "" gdy nie udało
    się rozstrzygnąć. Idzie do bazy i dalej do powiadomienia, bo od niego zależy,
    w jakim języku operator ma oddzwonić — a to jest decyzja, którą podejmuje
    w kilkanaście sekund, patrząc w telefon.

    `trafienia` niesie wzorce, które zadecydowały. Bez nich każde „czemu ten post
    wyleciał" kończy się zgadywaniem albo doklejaniem printów do produkcji.
    """

    przepuszczony: bool
    punkty: int
    jezyk: str
    powod: str
    trafienia: tuple[str, ...] = ()
    wygaszony: bool = False


# ---------------------------------------------------------------------------
# SŁOWNIKI
#
# Zawartość to DANE, nie kod — dopisanie zwrotu, który przeszedł koło nosa, ma
# być jedną linijką, a nie zmianą logiki. Każda lista jest posortowana tematami,
# nie alfabetycznie, żeby dało się zobaczyć dziurę w temacie.
#
# ZASADA DOBORU WZORCA: ma być jednoznaczny W SWOJEJ WARSTWIE. Zwrot, który
# pojawia się i w zgłoszeniu, i w reklamie, nie należy do żadnej warstwy —
# dokłada szumu do obu. Najdroższy błąd to zwrot grzecznościowy wrzucony do
# wygaszenia: polskie „z góry dziękuję za pomoc" stoi w KAŻDYM drugim zgłoszeniu
# i jako wzorzec wygaszający kasowałby zlecenia hurtowo. Dlatego w wygaszeniu
# stoją wyłącznie zwroty, które mówią wprost „już po sprawie".
#
# WZORCE PISZEMY JAKO RDZENIE, nie jako pełne formy — dopasowanie ma wolny koniec
# (patrz `_skompiluj`), więc „awari" łapie awaria/awarii/awarię, a „odholowa"
# łapie odholować/odholowanie/odholowania. Bez tego bramka rozumiałaby wyłącznie
# mianownik, czyli formę, w której po polsku, czesku i słowacku prawie nikt nie
# pisze. Tam, gdzie odmiana zmienia rdzeń („wypadek" / „wypadku"), stoją obie
# formy — to nie jest duplikat.
#
# WZORCE WOLNO NAKŁADAĆ. Regeks skanuje bez zachodzenia i od najdłuższego, więc
# „motor kaputt" zjada w tym miejscu także krótsze „kaputt" — jeden fragment
# tekstu liczy się raz, niezależnie od tego, ile wzorców by go opisało.
#
# CZESKI I SŁOWACKI: po uproszczeniu część wariantów SIĘ ZLEWA („odtahová" /
# „odťahová" -> „odtahova") i wystarczy jeden wpis; część nie („hledám" /
# „hľadám" -> „hledam" / „hladam") i wtedy stoją oba. Dlatego lista nie jest
# symetryczna — i nie jest to niedopatrzenie.
# ---------------------------------------------------------------------------

_PL = Slownik(
    jezyk="pl",
    przepuszczenie=(
        # prośba wprost
        "szukam lawet", "potrzebna lawet", "potrzebuję lawet", "pilnie lawet",
        "kto ma wolną lawet", "podeślijcie lawet", "kto podjedzie", "kto pomoże",
        "potrzebna pomoc drogow", "potrzebuję pomocy drogow", "proszę o pomoc drogow",
        # sama czynność — reklamy wycina warstwa odrzucenia
        "holowan", "odholowa", "przewóz aut", "transport aut", "przewieź",
        "przewiezienie aut", "przetransportowa", "zabrać aut",
        # objawy awarii: człowiek często nie pisze „laweta", tylko co się stało
        "nie odpala", "nie zapala", "nie chce odpalić", "nie da się jechać",
        "zgasło mi", "zepsuł mi się", "zepsuło mi się",
        "zepsut",   # zepsuty / zepsute / zepsuta — rdzeń, bo końcówka się zmienia
        "awari", "utknął", "stoję na", "urwał", "nie ruszy", "nie jedzie",
        # zdarzenia drogowe
        "wypadek", "wypadk", "stłuczk", "dachowa", "wjechałem w",
    ),
    odrzucenie=(
        "oferuj", "świadczymy usług", "w ofercie", "zapraszam",
        "atrakcyjne cen", "konkurencyjne cen", "tanio i solidnie",
        "sprzeda",  # sprzedam / sprzedaż / sprzedaje
        "wynajm", "faktura vat", "faktury vat",
        "zatrudni", "szukam kierowc", "praca dla kierowc", "oferta pracy",
    ),
    wygaszenie=(
        "nieaktualn", "temat zamknięt", "załatwione", "problem rozwiązan",
        "znalazł",  # znalazłem / znalazłam / znalazł
        "odwołuj", "już nie potrzebne", "już niepotrzebne", "już nie trzeba",
        "jest już pomoc",
    ),
)

_DE = Slownik(
    jezyk="de",
    przepuszczenie=(
        # prośba wprost — minimum z założeń, rozbudowane analogicznie do PL
        "suche autotransport", "suche abschlepp", "brauche abschlepp",
        "wer kann abschleppen", "wer hat platz", "wer kann helfen",
        "kann jemand helfen", "wer holt",
        "fahrzeug transportier", "auto transportier", "auto überführ",
        # sama czynność
        "abschlepp", "überführ", "autotransport",
        # objawy awarii
        "springt nicht an", "startet nicht", "motor kaputt", "kaputt",
        "panne", "liegengeblieben", "liegen geblieben", "fährt nicht",
        "nicht fahrbereit", "bleibt stehen",
        # zdarzenia drogowe
        "unfall", "totalschaden",
    ),
    odrzucenie=(
        "biete", "wir bieten", "wir übernehmen", "günstige preise", "faire preise",
        "verkauf", "zu verkaufen", "vermiet",
        "suche fahrer", "stellenangebot", "jobangebot",
    ),
    wygaszenie=(
        # UWAGA: „gefunden" bywa też częścią zdania „habe niemanden gefunden".
        # Zostaje, bo w praktyce zdecydowanie częściej znaczy „już znalazłem
        # pomoc" — ale to jest pierwszy wzorzec do sprawdzenia, gdy okaże się,
        # że gubimy niemieckie zlecenia.
        "erledigt", "hat sich erledigt", "nicht mehr aktuell", "gefunden",
        "schon geregelt", "brauche nicht mehr",
    ),
)

# Jeden słownik dla dwóch języków: warianty czeskie i słowackie leżą obok siebie
# w tych samych krotkach. Rozdzielenie dałoby dwie listy różniące się w połowie
# pozycji jedną literą — czyli dwa miejsca do zapomnienia przy każdej zmianie.
_CS_SK = Slownik(
    jezyk="cs",
    przepuszczenie=(
        # prośba wprost — czeskie i słowackie formy, które się NIE zlewają
        "hledám odtah", "hledám odvoz", "potřebuji odtah", "potřebuju odtah",
        "hľadám odťah", "hľadám odvoz", "potrebujem odťah",
        "kdo pomůže", "kto pomôže", "kdo má volno", "kto má voľno",
        "prosím o odtah",   # = „prosím o odťah" po uproszczeniu
        # sama czynność — po uproszczeniu wspólne dla obu języków
        "odtahov",          # odtahová služba / odtahovka / odťahová / odťahovka
        "odtah aut", "prevoz aut", "preprava aut",
        # objawy awarii
        "nenastartuje", "nestartuje", "nejde nastartovat",   # = warianty z „š"
        "nepojízdn", "nepojazdn", "poruch", "rozbit",
        "zůstal jsem stát", "zostal som stáť",
        # zdarzenia drogowe — „havárie" i „havária" zlewają się w jeden rdzeń
        "nehod", "havári",
    ),
    odrzucenie=(
        "nabíz", "ponúk", "nabídka prác",
        "výhodné cen", "levné cen", "lacné cen",
        "prodá", "predá", "na prodej", "na predaj",
        "hledám řidič", "hledáme řidič", "hľadám vodič", "hľadáme vodič",
        "přijmeme řidič", "prijmeme vodič",
    ),
    wygaszenie=(
        "vyřešen", "vyriešen", "neaktuáln",
        "už nepotřebuj", "už nepotrebuj", "zrušen",
        "našel jsem", "našiel som",
    ),
)

# Klucze to dwuliterowe znaczniki języka POSTA; "cs" i "sk" celowo wskazują ten
# sam obiekt. Kod, który iteruje po słownikach, musi więc odróżniać „ile jest
# kluczy" od „ile jest różnych słowników" (patrz _rozne_slowniki).
SLOWNIKI: dict[str, Slownik] = {
    "pl": _PL,
    "de": _DE,
    "cs": _CS_SK,
    "sk": _CS_SK,
}


# ---------------------------------------------------------------------------
# Normalizacja i kompilacja wzorców (raz, przy imporcie)
# ---------------------------------------------------------------------------

# Znaki, których rozkład NFD nie rozbija na literę + znak łączący. „ł" jest tu
# najważniejsze: bez tej podmianki „laweta" i „ławeta" byłyby dla bramki dwoma
# różnymi słowami, a druga forma pada w postach regularnie.
_PODMIANY = str.maketrans({
    "ł": "l", "Ł": "L",
    "ß": "ss",
    "đ": "d", "Đ": "D",
    "ø": "o", "Ø": "O",
})


def uprosc(tekst: str) -> str:
    """Tekst do POROWNYWANIA: małe litery, bez znaków diakrytycznych.

    Nie służy do detekcji języka — ta potrzebuje dokładnie tych znaków, które ta
    funkcja usuwa (patrz `wykryj_jezyk`).
    """
    if not tekst:
        return ""
    plaski = tekst.casefold().translate(_PODMIANY)
    rozlozony = unicodedata.normalize("NFD", plaski)
    return "".join(z for z in rozlozony if not unicodedata.combining(z))


def _skompiluj(wzorce: tuple[str, ...]) -> tuple[re.Pattern[str], tuple[str, ...]]:
    """Jedna alternatywa regeksowa na całą warstwę + wzorce w formie uproszczonej.

    Jeden regeks zamiast pętli po wzorcach to jedno przejście po tekście na
    warstwę — przy dwunastu warstwach (cztery klucze, trzy warstwy) różnica jest
    między „mikrosekundy" a „zauważalne", a bramka stoi na ścieżce KAŻDEGO posta.

    GRANICA SŁOWA TYLKO Z PRZODU (`\\b` na początku, koniec wolny) — i to jest
    decyzja, bez której bramka nie działa w trzech z czterech języków. Polski,
    czeski i słowacki odmieniają wszystko: wzorzec „odholowanie" ma trafić
    w „auto do odholowania", „porucha" w „mám poruchu", „odťah" w „odťahu".
    Domknięcie granicy z tyłu wycinałoby dokładnie te formy, w których ludzie
    naprawdę piszą — czyli wszystkie poza mianownikiem. Z przodu granica ZOSTAJE,
    bo bez niej krótkie wzorce trafiają w środki niezwiązanych wyrazów.

    Koszt tej decyzji jest znany i przyjęty: niemieckie „biete" trafia też
    w „bietet" (dobrze) i w „Gebiete" (źle, ale to jedno trafienie w warstwie
    odrzucenia, czyli -2 pkt — nie kasuje zgłoszenia z dwoma trafieniami).
    """
    proste = tuple(uprosc(w) for w in wzorce)
    if not proste:
        return re.compile(r"(?!)"), ()   # regeks, który nigdy nie trafia
    alternatywa = "|".join(re.escape(w) for w in sorted(proste, key=len, reverse=True))
    return re.compile(rf"\b(?:{alternatywa})"), proste


# Warstwy skompilowane RAZ, przy imporcie modułu. Kluczem jest id() słownika,
# żeby wspólny słownik czesko-słowacki nie był kompilowany dwa razy.
_WARSTWY: dict[int, dict[str, re.Pattern[str]]] = {}
for _slownik in (_PL, _DE, _CS_SK):
    _WARSTWY[id(_slownik)] = {
        "przepuszczenie": _skompiluj(_slownik.przepuszczenie)[0],
        "odrzucenie": _skompiluj(_slownik.odrzucenie)[0],
        "wygaszenie": _skompiluj(_slownik.wygaszenie)[0],
    }


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
# Detekcja języka — heurystyka na znakach i słowach funkcyjnych
# ---------------------------------------------------------------------------

# Znaki ROZSTRZYGAJĄCE: występują w jednym z czterech języków i nie występują
# w pozostałych. Wspólne (á, í, é, ú, ý, ó, č, š, ž) świadomie pominięte — nie
# niosą informacji, a rozmyłyby punktację.
#   pl: ą ę ł ń ś ź ż
#   de: ö ü ß  (ä dzielone ze słowackim, patrz niżej)
#   cs: ř ě ů
#   sk: ľ ĺ ŕ ô
_ZNAKI: dict[str, str] = {
    "pl": "ąęłńśźż",
    "de": "öüß",
    "cs": "řěů",
    "sk": "ľĺŕô",
}

# „ä" jest i niemieckie, i słowackie — punktuje OBA, więc samo z siebie niczego
# nie rozstrzyga i decyzja spada na słowa funkcyjne. To jest poprawne zachowanie:
# jeden znak wspólny nie powinien przeważać.
_ZNAKI_WSPOLNE: dict[str, tuple[str, ...]] = {"ä": ("de", "sk")}

# Słowa funkcyjne: krótkie, bardzo częste, NIE związane z tematem lawet. Dobrane
# tak, żeby nie pokrywały się między językami — dlatego nie ma tu polskiego „nie"
# (identyczne ze słowackim) ani „na"/„do"/„je" (wspólne dla wszystkich).
_SLOWA: dict[str, tuple[str, ...]] = {
    "pl": ("się", "jest", "czy", "żeby", "mam", "proszę", "bardzo", "tylko",
           "który", "gdzie", "jakiś", "coś", "kogoś", "przy", "oraz", "moje"),
    "de": ("ich", "nicht", "und", "der", "die", "das", "ist", "von", "nach",
           "mit", "für", "kann", "wer", "einen", "bitte", "mein", "hat", "wird"),
    "cs": ("jsem", "jsme", "se", "nebo", "může", "který", "není", "také", "jak",
           "kdo", "moc", "prosím", "mám", "jsou", "musím"),
    "sk": ("som", "sme", "sa", "alebo", "môže", "ktorý", "veľmi", "aj",
           "kto", "prosím", "mám", "musím", "chcem", "však"),
}

# Ile waży jedna przesłanka. Znak diakrytyczny waży więcej niż słowo funkcyjne,
# bo jest trudniejszy do przypadkowego trafienia: „ř" w polskim poście nie
# wystąpi, a „mám" jest i czeskie, i słowackie.
_WAGA_ZNAKU = 3
_WAGA_SLOWA = 2

# O ile najlepszy język musi wyprzedzać drugi, żeby uznać detekcję za pewną.
# Poniżej tej różnicy zwracamy "" i wołający sprawdza WSZYSTKIMI słownikami.
# Wartość jest niska celowo: pomyłka w detekcji jest droższa niż trzy zbędne
# przejścia regeksem.
_MARGINES_PEWNOSCI = 2


def _punkty_jezykow(tresc: str) -> dict[str, int]:
    """Ile przesłanek wskazuje na każdy z czterech języków. Bez normalizacji!"""
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
    wyrazy = set(re.findall(r"\w+", tekst, flags=re.UNICODE))
    for jezyk, slowa in _SLOWA.items():
        punkty[jezyk] += _WAGA_SLOWA * sum(1 for s in slowa if s in wyrazy)
    return punkty


def wykryj_jezyk(tresc: str) -> str:
    """Dwuliterowy znacznik języka albo "" gdy nie da się rozstrzygnąć.

    Heurystyka na znakach diakrytycznych i słowach funkcyjnych — ŻADNEJ
    biblioteki i żadnego wywołania sieciowego. Bramka stoi na ścieżce każdego
    pobranego posta; wołanie zewnętrznego detektora zamieniłoby filtr, który
    kosztuje mikrosekundy, w filtr, który kosztuje pieniądze i potrafi paść.

    "" NIE jest błędem — to normalna odpowiedź dla krótkiego posta bez znaków
    charakterystycznych („Kto podjedzie na S19?"). Wołający ma wtedy sprawdzić
    wszystkimi słownikami, co jest tańsze niż zgadywanie.
    """
    punkty = _punkty_jezykow(tresc)
    ranking = sorted(punkty.items(), key=lambda kv: kv[1], reverse=True)
    najlepszy, wynik = ranking[0]
    drugi_wynik = ranking[1][1] if len(ranking) > 1 else 0
    if wynik <= 0 or (wynik - drugi_wynik) < _MARGINES_PEWNOSCI:
        return ""
    return najlepszy


def _wariant_cs_sk(tresc: str) -> str:
    """„cs" czy „sk" dla posta obsłużonego wspólnym słownikiem.

    Słownik jest jeden, ale znacznik w powiadomieniu ma powiedzieć operatorowi,
    czy dzwoni po czesku, czy po słowacku. Przy remisie oddajemy "cs" — nie
    dlatego, że jest bardziej prawdopodobny, tylko dlatego, że pole musi mieć
    wartość, a różnica jest dla operatora niewielka (te języki są wzajemnie
    zrozumiałe; polski dla obu nie jest).
    """
    punkty = _punkty_jezykow(tresc)
    return "sk" if punkty.get("sk", 0) > punkty.get("cs", 0) else "cs"


# ---------------------------------------------------------------------------
# Punktacja
# ---------------------------------------------------------------------------
def punktacja(tresc: str, slownik: Slownik) -> tuple[int, tuple[str, ...], bool]:
    """(punkty, trafione wzorce, czy wygaszony) dla JEDNEGO słownika.

    Wydzielone z `gate`, żeby dało się sprawdzić testem samą arytmetykę warstw,
    bez detekcji języka i bez wyboru słownika.
    """
    tekst = uprosc(tresc)
    if not tekst:
        return 0, (), False

    warstwy = _WARSTWY[id(slownik)]
    punkty = 0
    trafienia: list[str] = []

    for nazwa, waga in (("przepuszczenie", WAGA_PRZEPUSZCZENIE),
                        ("odrzucenie", WAGA_ODRZUCENIE),
                        ("wygaszenie", WAGA_WYGASZENIE)):
        # set() bo liczymy RÓŻNE wzorce, nie wystąpienia — patrz docstring modułu.
        znalezione = sorted(set(warstwy[nazwa].findall(tekst)))
        if not znalezione:
            continue
        punkty += waga * len(znalezione)
        trafienia.extend(znalezione)

    wygaszony = bool(warstwy["wygaszenie"].search(tekst))
    return punkty, tuple(trafienia), wygaszony


def _powod(punkty: int, trafienia: tuple[str, ...], wygaszony: bool,
           przepuszczony: bool) -> str:
    """Jedno zdanie do logu i do bazy: DLACZEGO tak, a nie inaczej."""
    if wygaszony:
        return f"wygaszony ({', '.join(trafienia) or 'brak wzorca'})"
    if not trafienia:
        return "brak jakiegokolwiek wzorca — 0 pkt"
    lista = ", ".join(trafienia)
    stan = "przepuszczony" if przepuszczony else "odrzucony"
    return f"{stan} ({punkty} pkt: {lista})"


def gate(tresc: str, jezyk: str | None = None) -> GateResult:
    """Czy warto zapytać model o ten post.

    `jezyk` wymusza słownik (do testów i do grup, o których wiadomo z góry, że są
    jednojęzyczne). Bez niego język jest wykrywany, a przy niepewnej detekcji
    post idzie przez WSZYSTKIE słowniki i liczy się najwyższy wynik.

    „Najwyższy wynik" ma jeden wyjątek: jeśli KTÓRYKOLWIEK słownik zobaczył
    wygaszenie, post jest wygaszony. Bez tego wyjątku niemieckie „hat sich
    erledigt" przegrywałoby ze słownikiem polskim, który tego zwrotu nie zna,
    więc daje 0 punktów — a zero jest wyższe niż minus sto. Post załatwiony
    przestaje być zleceniem niezależnie od tego, w jakim języku to napisano.
    """
    if not (tresc or "").strip():
        return GateResult(przepuszczony=False, punkty=0, jezyk="",
                          powod="pusta treść")

    wykryty = jezyk if jezyk is not None else wykryj_jezyk(tresc)

    if wykryty and wykryty in SLOWNIKI:
        kandydaci = [(wykryty, SLOWNIKI[wykryty])]
    else:
        # Nie wiemy — liczymy wszystkimi. Cztery przebiegi po krótkim tekście to
        # mikrosekundy; zgubione zlecenie to kilkaset złotych.
        kandydaci = _rozne_slowniki()

    najlepszy_wynik: tuple[int, tuple[str, ...], str] = (0, (), "")
    wygaszony = False
    pierwszy = True
    for znacznik, slownik in kandydaci:
        punkty, trafienia, wyg = punktacja(tresc, slownik)
        wygaszony = wygaszony or wyg
        if pierwszy or punkty > najlepszy_wynik[0]:
            najlepszy_wynik = (punkty, trafienia, znacznik)
            pierwszy = False

    punkty, trafienia, znacznik = najlepszy_wynik

    # Znacznik języka dla operatora. Bierzemy go ze słownika, który wygrał — ten
    # słownik „wytłumaczył" post najlepiej. Dla wspólnego czesko-słowackiego
    # rozstrzyga jeszcze wariant, bo to od niego zależy język oddzwonienia.
    if znacznik in ("cs", "sk"):
        znacznik = _wariant_cs_sk(tresc)
    elif not znacznik or punkty <= 0:
        # Nic nie trafiło — nie zmyślamy języka na podstawie zerowego wyniku.
        znacznik = wykryty if wykryty in SLOWNIKI else ""
        if znacznik in ("cs", "sk"):
            znacznik = _wariant_cs_sk(tresc)

    przepuszczony = (not wygaszony) and punkty >= PROG
    return GateResult(
        przepuszczony=przepuszczony,
        punkty=punkty,
        jezyk=znacznik,
        powod=_powod(punkty, trafienia, wygaszony, przepuszczony),
        trafienia=trafienia,
        wygaszony=wygaszony,
    )


# ---------------------------------------------------------------------------
# Kontrakt z klasyfikatorem (worker z kolejnego kroku)
# ---------------------------------------------------------------------------
# Bramka NIE tłumaczy — tylko wpuszcza. Tłumaczenie robi klasyfikator, i to on
# musi wiedzieć, że post bywa obcojęzyczny. Instrukcja stoi TUTAJ, a nie
# w klasyfikatorze, z jednego powodu: to bramka wpuszcza obce języki do
# pipeline'u, więc to przy niej trzeba pamiętać o konsekwencji. Klasyfikator
# wkleja ją do swojego promptu systemowego przez import, zamiast przepisywać —
# przepisana rozjechałaby się przy pierwszej zmianie listy języków.
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
# CLI — diagnostyka pojedynczego posta, bez sieci i bez bazy.
#   python -m laweta_radar.workers.gate "Suche Abschleppdienst, Motor kaputt"
#   pbpaste | python -m laweta_radar.workers.gate
# Kod wyjścia: 0 = przepuszczony, 1 = odrzucony. Dzięki temu da się tego użyć
# w pętli po pliku z postami bez parsowania wypisanego tekstu.
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    tresc = " ".join(argv[1:]).strip()
    if not tresc:
        if sys.stdin.isatty():
            print("Użycie: python -m laweta_radar.workers.gate \"treść posta\"",
                  file=sys.stderr)
            print("        albo podaj treść na standardowym wejściu.", file=sys.stderr)
            return 1
        tresc = sys.stdin.read().strip()

    wynik = gate(tresc)
    wykryty = wykryj_jezyk(tresc)
    punkty_jez = _punkty_jezykow(tresc)
    print(f"treść:          {tresc[:120]}{'…' if len(tresc) > 120 else ''}")
    # Dwie różne rzeczy, celowo rozdzielone: co powiedziała DETEKCJA i który
    # SŁOWNIK wygrał. Przy niepewnej detekcji (druga linia pusta) liczone jest
    # wszystkimi słownikami, a znacznik bierze się z tego, który wytłumaczył
    # post najlepiej — i to jest normalna ścieżka, nie awaria.
    opis_detekcji = wykryty or "(nierozstrzygnięta — liczę wszystkimi słownikami)"
    print(f"detekcja:       {opis_detekcji}")
    print("  przesłanki:   " + ", ".join(f"{j}={p}" for j, p in
                                         sorted(punkty_jez.items(),
                                                key=lambda kv: -kv[1])))
    print(f"znacznik:       {wynik.jezyk or '—'}   (z tym operator oddzwania)")
    print(f"punkty:         {wynik.punkty} (próg {PROG})")
    print(f"trafienia:      {', '.join(wynik.trafienia) or '—'}")
    print(f"WERDYKT:        {'PRZEPUSZCZONY' if wynik.przepuszczony else 'ODRZUCONY'}"
          f"{' [WYGASZONY]' if wynik.wygaszony else ''}")
    print(f"powód:          {wynik.powod}")
    return 0 if wynik.przepuszczony else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
