"""Offline testy workers/gate.py — bramka słowna PL / DE / CS / SK.

Bez sieci, bez bazy, bez modelu. Bramka z założenia nie dotyka żadnego z nich
i ten plik jest tego dowodem: gdyby ktoś wstawił do niej wywołanie detektora
języka albo modelu, testy zaczęłyby wisieć albo wymagać kluczy.

CZEGO TU PILNUJEMY — po jednym powodzie na sekcję:

  1. KOMPLET JĘZYKÓW. Po dziesięć-kilkanaście przypadków na język, w tym po
     jednym wygaszonym. Bramka jednojęzyczna nie wygląda na zepsutą: obcy post
     dostaje zero punktów i wylatuje tak samo cicho jak reklama felg. Jedynym
     sposobem, żeby to zauważyć, jest test per język.
  2. ASYMETRIA KOSZTÓW. Zgłoszenie ze słowem reklamowym („szukam lawety,
     oferuję zapłatę") MA przechodzić. Odwrotna pomyłka kosztuje ułamek grosza,
     ta kosztuje zlecenie.
  3. ZWROTY GRZECZNOŚCIOWE NIE WYGASZAJĄ. „Z góry dziękuję za pomoc" stoi
     w co drugim polskim zgłoszeniu. Gdyby trafiło do warstwy wygaszenia,
     bramka kasowałaby zlecenia hurtowo — i nikt by się nie dowiedział.
  4. ODMIANA. Wzorce dopasowują się z wolnym końcem, bo w trzech z czterech
     języków mianownik jest formą, w której ludzie nie piszą.
  5. NIEPEWNA DETEKCJA. Post bez znaków charakterystycznych ma iść przez
     WSZYSTKIE słowniki, a nie przez domyślny.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.workers import gate  # noqa: E402


def _p(tresc: str) -> gate.GateResult:
    """Skrót — bramka bez wymuszania języka (tak, jak woła ją fetcher)."""
    return gate.gate(tresc)


# ---------------------------------------------------------------------------
# POLSKI
# ---------------------------------------------------------------------------
PL_PRZEPUSZCZONE = [
    "Szukam lawety z Krakowa do Wrocławia, auto nie odpala",
    "Potrzebna laweta pilnie, stoję na S19 pod Rzeszowem",
    "Zepsuł mi się samochód pod Jasłem, kto podjedzie?",
    "Awaria na trasie, auto nie chce odpalić, potrzebuję pomocy drogowej",
    "Kto pomoże? Wypadek na obwodnicy, auto do odholowania",
    "Nie da się jechać, urwało półoś, transport auta do warsztatu",
    "Kto ma wolną lawetę na jutro? Przewóz auta z komisu do domu",
    "Stłuczka na rondzie, potrzebna laweta, auto nie ruszy",
    "Zgasło mi auto na obwodnicy i nie zapala, ktoś pomoże?",
    # Zgłoszenie ze słowem reklamowym w środku — MA przejść (punkt 2 z docstringu).
    "Szukam lawety, auto nie odpala, oferuję dobrą zapłatę",
    # Zwrot grzecznościowy NIE wygasza (punkt 3 z docstringu).
    "Potrzebna laweta pod Sanokiem, auto nie odpala, z góry dziękuję za pomoc",
    # Odmiana: wzorzec „odholowanie" ma trafić w „odholowania" (punkt 4).
    "Auto po wypadku, potrzebuję odholowania do warsztatu",
]

PL_ODRZUCONE = [
    "Oferujemy holowanie 24h, atrakcyjne ceny, faktura VAT",
    "Sprzedam lawetę, stan idealny, więcej info na priv",
    "Zatrudnię kierowcę na lawetę, praca dla kierowcy od zaraz",
    "Sprzedam felgi 17 cali do golfa, komplet z oponami",
    "Świadczymy usługi transportu, zapraszamy do współpracy",
    "Dzień dobry, czy ktoś zna dobrego mechanika w okolicy?",
]

PL_WYGASZONE = [
    "Nieaktualne, znalazłem już pomoc",
    "Potrzebna laweta pod Sanokiem — TEMAT ZAMKNIĘTY, dziękuję wszystkim",
]


# ---------------------------------------------------------------------------
# NIEMIECKI — przypadek, dla którego cała ta wielojęzyczność powstała.
# ---------------------------------------------------------------------------
DE_PRZEPUSZCZONE = [
    # Post z założeń: przy polskich wzorcach dostawał zero punktów i wylatywał.
    "Suche Autotransport von München nach Krakau, Fahrzeug fährt nicht",
    "Brauche Abschleppdienst, mein Auto springt nicht an",
    "Wer kann abschleppen? Motor kaputt, stehe auf der A4",
    "Panne auf der Autobahn, bei Dresden liegengeblieben",
    "Unfall, Fahrzeug nicht fahrbereit, wer hat Platz?",
    "Auto transportieren von Berlin nach Poznań, Überführung möglich?",
    "Getriebe kaputt, das Auto startet nicht mehr, kann jemand helfen?",
    "Mein Wagen bleibt stehen und springt nicht an, brauche einen Abschleppwagen",
    "Totalschaden nach Unfall, Fahrzeug transportieren nach Polen",
    # Bez ani jednego umlautu i bez słów funkcyjnych — detekcja NIE rozstrzygnie,
    # więc post musi przejść ścieżką „sprawdź wszystkimi słownikami" (punkt 5).
    "Suche Autotransport, Motor kaputt",
]

DE_ODRZUCONE = [
    "Wir bieten Abschleppdienst, günstige Preise",
    "Verkaufe Anhänger, guter Zustand, Preis VB",
    "Suche Fahrer für Abschleppwagen, Stellenangebot ab sofort",
    "Biete Winterreifen, günstige Preise",
    "Guten Tag, kennt jemand eine gute Werkstatt in der Nähe?",
]

DE_WYGASZONE = [
    "Hat sich erledigt, danke euch",
    "Suche Abschleppdienst von Wien nach Brünn — nicht mehr aktuell",
]


# ---------------------------------------------------------------------------
# CZESKI
# ---------------------------------------------------------------------------
CS_PRZEPUSZCZONE = [
    "Hledám odtahovku, auto nenastartuje, jsem u Brna",
    "Potřebuji odtah auta z Prahy do Ostravy",
    "Porucha na D1, nepojízdné auto, kdo pomůže?",
    "Nehoda u Plzně, potřebuji odtahovku co nejdřív",
    "Nejde nastartovat, zůstal jsem stát u Kolína",
    "Převoz auta z autobazaru, sháním odtahovku",
    "Auto nestartuje, mám poruchu na dálnici",
    "Havárie u Liberce, auto je nepojízdné",
    "Prosím o odtah, rozbité auto stojí na krajnici",
    "Přeprava auta do servisu, kdo má volno zítra?",
]

CS_ODRZUCONE = [
    "Nabízíme odtahovou službu, výhodné ceny",
    "Prodám auto, nepojízdné, cena dohodou",
    "Hledáme řidiče, nabídka práce v dopravě",
    "Prodám zimní pneumatiky, málo jeté",
    "Dobrý den, zná někdo dobrého mechanika?",
]

CS_WYGASZONE = [
    "Již vyřešeno, děkuji",
    "Potřebuji odtah z Brna — neaktuální, našel jsem pomoc",
]


# ---------------------------------------------------------------------------
# SŁOWACKI — ten sam słownik co czeski, inny znacznik języka.
# ---------------------------------------------------------------------------
SK_PRZEPUSZCZONE = [
    "Hľadám odťahovku, auto neštartuje, som pri Žiline",
    "Potrebujem odťah auta z Bratislavy do Košíc",
    "Porucha na D1, nepojazdné auto, kto pomôže?",
    "Nehoda pri Nitre, potrebujem odťahovku",
    "Nejde naštartovať, zostal som stáť pri Trenčíne",
    "Prevoz auta z autobazáru, hľadám odťahovku",
    "Auto neštartuje, mám poruchu na diaľnici",
    "Havária pri Prešove, auto je nepojazdné",
    "Prosím o odťah, auto stojí na krajnici",
    "Preprava auta do servisu, kto má voľno?",
]

SK_ODRZUCONE = [
    "Ponúkame odťahovú službu, lacné ceny",
    "Predám auto, nepojazdné, cena dohodou",
    "Prijmeme vodiča, ponuka práce v doprave",
    "Predám zimné pneumatiky, málo jazdené",
    "Dobrý deň, pozná niekto dobrého mechanika?",
]

SK_WYGASZONE = [
    "Už vyriešené, ďakujem",
    "Potrebujem odťah z Košíc — neaktuálne, našiel som pomoc",
]


# ---------------------------------------------------------------------------
# Testy właściwe. Pętla po listach zamiast osobnej funkcji na przypadek —
# przypadek dopisuje się wtedy jedną linijką, a nie kopią funkcji, i widać
# w jednym miejscu, ile ich jest na język.
# ---------------------------------------------------------------------------
def test_pl_przepuszczone():
    for tresc in PL_PRZEPUSZCZONE:
        wynik = _p(tresc)
        assert wynik.przepuszczony, f"PL miało przejść: {tresc!r} -> {wynik.powod}"
        assert not wynik.wygaszony, f"PL nie miało być wygaszone: {tresc!r}"


def test_pl_odrzucone():
    for tresc in PL_ODRZUCONE:
        wynik = _p(tresc)
        assert not wynik.przepuszczony, f"PL miało wylecieć: {tresc!r} -> {wynik.powod}"


def test_pl_wygaszone():
    for tresc in PL_WYGASZONE:
        wynik = _p(tresc)
        assert wynik.wygaszony, f"PL miało być wygaszone: {tresc!r} -> {wynik.powod}"
        assert not wynik.przepuszczony


def test_de_przepuszczone():
    for tresc in DE_PRZEPUSZCZONE:
        wynik = _p(tresc)
        assert wynik.przepuszczony, f"DE miało przejść: {tresc!r} -> {wynik.powod}"
        assert not wynik.wygaszony


def test_de_odrzucone():
    for tresc in DE_ODRZUCONE:
        wynik = _p(tresc)
        assert not wynik.przepuszczony, f"DE miało wylecieć: {tresc!r} -> {wynik.powod}"


def test_de_wygaszone():
    for tresc in DE_WYGASZONE:
        wynik = _p(tresc)
        assert wynik.wygaszony, f"DE miało być wygaszone: {tresc!r} -> {wynik.powod}"
        assert not wynik.przepuszczony


def test_cs_przepuszczone():
    for tresc in CS_PRZEPUSZCZONE:
        wynik = _p(tresc)
        assert wynik.przepuszczony, f"CS miało przejść: {tresc!r} -> {wynik.powod}"
        assert not wynik.wygaszony


def test_cs_odrzucone():
    for tresc in CS_ODRZUCONE:
        wynik = _p(tresc)
        assert not wynik.przepuszczony, f"CS miało wylecieć: {tresc!r} -> {wynik.powod}"


def test_cs_wygaszone():
    for tresc in CS_WYGASZONE:
        wynik = _p(tresc)
        assert wynik.wygaszony, f"CS miało być wygaszone: {tresc!r} -> {wynik.powod}"
        assert not wynik.przepuszczony


def test_sk_przepuszczone():
    for tresc in SK_PRZEPUSZCZONE:
        wynik = _p(tresc)
        assert wynik.przepuszczony, f"SK miało przejść: {tresc!r} -> {wynik.powod}"
        assert not wynik.wygaszony


def test_sk_odrzucone():
    for tresc in SK_ODRZUCONE:
        wynik = _p(tresc)
        assert not wynik.przepuszczony, f"SK miało wylecieć: {tresc!r} -> {wynik.powod}"


def test_sk_wygaszone():
    for tresc in SK_WYGASZONE:
        wynik = _p(tresc)
        assert wynik.wygaszony, f"SK miało być wygaszone: {tresc!r} -> {wynik.powod}"
        assert not wynik.przepuszczony


def test_minimum_przypadkow_na_jezyk():
    """Minimum dziesięć przypadków na język, w tym co najmniej jeden wygaszony.

    Test o samych testach, bo to jest wymóg, który najłatwiej cicho złamać:
    dopisując język i zapominając o przypadkach, dostajemy zieloną suitę
    i bramkę, która ten język przepuszcza na oślep.
    """
    komplety = {
        "pl": (PL_PRZEPUSZCZONE, PL_ODRZUCONE, PL_WYGASZONE),
        "de": (DE_PRZEPUSZCZONE, DE_ODRZUCONE, DE_WYGASZONE),
        "cs": (CS_PRZEPUSZCZONE, CS_ODRZUCONE, CS_WYGASZONE),
        "sk": (SK_PRZEPUSZCZONE, SK_ODRZUCONE, SK_WYGASZONE),
    }
    assert set(komplety) == set(gate.SLOWNIKI), (
        "Doszedł język do SLOWNIKI, ale nie doszły przypadki testowe.")
    for jezyk, listy in komplety.items():
        assert sum(len(x) for x in listy) >= 10, f"za mało przypadków dla {jezyk}"
        assert listy[2], f"brak przypadku wygaszonego dla {jezyk}"


# ---------------------------------------------------------------------------
# Detekcja języka
# ---------------------------------------------------------------------------
def test_wykryj_jezyk_po_znakach_i_slowach():
    assert gate.wykryj_jezyk("Szukam lawety, auto się nie odpala") == "pl"
    assert gate.wykryj_jezyk("Ich brauche einen Abschleppwagen, das Auto "
                             "springt nicht an") == "de"
    assert gate.wykryj_jezyk("Auto nenastartuje, zůstal jsem stát") == "cs"
    assert gate.wykryj_jezyk("Auto neštartuje, zostal som stáť, veľmi súrne") == "sk"


def test_wykryj_jezyk_oddaje_pustkę_gdy_nie_wiadomo():
    """Krótki post bez znaków charakterystycznych: "" zamiast zgadywania.

    To nie jest porażka detekcji, tylko jej poprawne zachowanie — a od niego
    zależy, czy post pójdzie przez wszystkie słowniki, czy przez jeden losowy.
    """
    assert gate.wykryj_jezyk("") == ""
    assert gate.wykryj_jezyk("Auto 2015 diesel") == ""
    assert gate.wykryj_jezyk("Suche Autotransport, Motor kaputt") == ""


def test_czeski_i_slowacki_maja_wspolny_slownik_ale_osobny_znacznik():
    """Jeden słownik, dwa znaczniki — od znacznika zależy język oddzwonienia."""
    assert gate.SLOWNIKI["cs"] is gate.SLOWNIKI["sk"]
    assert _p("Hledám odtahovku, auto nenastartuje, jsem u Brna").jezyk == "cs"
    assert _p("Hľadám odťahovku, auto neštartuje, som pri Žiline").jezyk == "sk"


def test_niepewna_detekcja_liczy_wszystkimi_slownikami():
    """Post bez przesłanek językowych ma być policzony każdym słownikiem.

    Bez tego niemieckie zgłoszenie napisane bez umlautów trafiałoby na słownik
    polski, dostawało zero punktów i wylatywało — czyli dokładnie ten błąd,
    dla którego powstała wielojęzyczność.
    """
    tresc = "Suche Autotransport, Motor kaputt"
    assert gate.wykryj_jezyk(tresc) == "", "warunek testu: detekcja ma być niepewna"
    wynik = _p(tresc)
    assert wynik.przepuszczony
    assert wynik.jezyk == "de", "znacznik ma pochodzić ze słownika, który wygrał"


def test_wygaszenie_dziala_ponad_jezykami():
    """Wygaszenie widziane przez JAKIKOLWIEK słownik wygasza post.

    Samo „weź najwyższy wynik" tu nie wystarcza: słownik polski nie zna zwrotu
    „erledigt", więc daje 0 punktów — a zero jest wyższe niż minus sto. Post
    załatwiony przestaje być zleceniem niezależnie od języka.
    """
    tresc = "Abschleppdienst erledigt"
    assert gate.wykryj_jezyk(tresc) == "", "warunek testu: detekcja ma być niepewna"
    wynik = _p(tresc)
    assert wynik.wygaszony
    assert not wynik.przepuszczony


def test_wymuszony_jezyk_omija_detekcje():
    """`jezyk=` wymusza słownik — dla grup, o których wiadomo z góry, czym są."""
    tresc = "Suche Autotransport, Motor kaputt"
    assert gate.gate(tresc, jezyk="de").przepuszczony
    assert not gate.gate(tresc, jezyk="pl").przepuszczony


# ---------------------------------------------------------------------------
# Punktacja i warstwy
# ---------------------------------------------------------------------------
def test_wagi_warstw():
    """Arytmetyka warstw, bez detekcji i bez wyboru słownika."""
    punkty, trafienia, wygaszony = gate.punktacja("szukam lawety", gate.SLOWNIKI["pl"])
    assert punkty == gate.WAGA_PRZEPUSZCZENIE
    # W trafieniach jest RDZEŃ wzorca, nie forma z posta — po nim szuka się
    # wpisu w słowniku, gdy trzeba zrozumieć, czemu post przeszedł.
    assert trafienia == ("szukam lawet",)
    assert not wygaszony

    punkty, _, _ = gate.punktacja("sprzedam felgi", gate.SLOWNIKI["pl"])
    assert punkty == gate.WAGA_ODRZUCENIE

    punkty, _, wygaszony = gate.punktacja("nieaktualne", gate.SLOWNIKI["pl"])
    assert punkty == gate.WAGA_WYGASZENIE
    assert wygaszony


def test_ten_sam_wzorzec_liczy_sie_raz():
    """Powtórzenie nie podbija wyniku — inaczej wygrywałby spam, nie zgłoszenie."""
    jeden, _, _ = gate.punktacja("szukam lawety", gate.SLOWNIKI["pl"])
    trzy, _, _ = gate.punktacja("szukam lawety szukam lawety szukam lawety",
                                gate.SLOWNIKI["pl"])
    assert jeden == trzy == gate.WAGA_PRZEPUSZCZENIE


def test_prog_jest_rowny_jednemu_trafieniu():
    """JEDNO trafienie w warstwie zgłoszeń wystarczy, żeby zapytać model."""
    assert gate.PROG == gate.WAGA_PRZEPUSZCZENIE
    assert _p("Awaria pod Duklą").przepuszczony


def test_brak_diakrytykow_nie_gubi_postu():
    """Ludzie piszą z telefonu, przy zepsutym aucie. Ogonki wtedy znikają."""
    assert _p("Szukam lawety, auto nie odpala").przepuszczony
    assert _p("Szukam lawety, auto nie odpala".replace("ł", "l")).przepuszczony
    assert _p("Potrzebuje lawety pilnie").przepuszczony
    assert _p("Hladam odtahovku, auto nestartuje").przepuszczony


def test_odmiana_nie_gubi_wzorca():
    """Wzorzec ma trafiać w formy odmienione — mianownik to nie jest to, czym
    ludzie piszą w trzech z czterech obsługiwanych języków."""
    assert "odholowa" in _p("auto do odholowania").trafienia
    assert "awari" in _p("mam awarię na S19, kto podjedzie").trafienia
    assert "poruch" in _p("mám poruchu na dálnici").trafienia
    assert "odtah aut" in _p("kolik stojí odtah auta do servisu?").trafienia
    # Rdzeń „wypadek" nie łapie „wypadku" — dlatego w słowniku stoją obie formy.
    assert "wypadk" in _p("auto po wypadku, potrzebuję odholowania").trafienia


def test_pusta_tresc_nie_wybucha():
    """Worker karmi bramkę tym, co przyszło z Apify — także pustką."""
    for tresc in ("", "   ", "\n"):
        wynik = gate.gate(tresc)
        assert not wynik.przepuszczony
        assert wynik.jezyk == ""
        assert wynik.punkty == 0


def test_bramka_jest_szybka():
    """Sufit czasowy — luźny celowo, bo mierzy JEDNĄ rzecz: czy ktoś nie wstawił
    na ścieżkę bramki wywołania sieciowego albo biblioteki do detekcji języka.

    Realny czas to jednostki mikrosekund na post; próg ustawiony sto razy wyżej,
    żeby test nie migotał na obciążonym CI, ale nadal wywalał się natychmiast
    przy pierwszym `import langdetect` czy `requests.post`.
    """
    prubki = PL_PRZEPUSZCZONE + DE_PRZEPUSZCZONE + CS_PRZEPUSZCZONE + SK_PRZEPUSZCZONE
    start = time.perf_counter()
    for _ in range(50):
        for tresc in prubki:
            gate.gate(tresc)
    trwanie = time.perf_counter() - start
    ile = 50 * len(prubki)
    assert trwanie / ile < 0.001, (
        f"bramka zwolniła do {trwanie / ile * 1000:.3f} ms/post — sprawdź, czy nie "
        f"doszło wywołanie sieciowe albo biblioteka detekcji języka")


def test_instrukcja_dla_klasyfikatora_niesie_kontrakt():
    """Klasyfikator ma dostać ten tekst przez import, nie przez przepisanie.

    Test pilnuje trzech rzeczy, które w tym tekście MUSZĄ zostać: lista języków,
    „po polsku" i wyjątek na nazwy miejscowości. Bez ostatniego geokodowanie
    dostanie „Monachium" i zlecenie wyląduje na mapie w złym miejscu.
    """
    tekst = gate.INSTRUKCJA_JEZYKOWA_DLA_KLASYFIKATORA.lower()
    for fragment in ("niemieck", "czesk", "słowack", "po polsku", "oryginal"):
        assert fragment in tekst, f"z instrukcji dla klasyfikatora zniknęło: {fragment}"
