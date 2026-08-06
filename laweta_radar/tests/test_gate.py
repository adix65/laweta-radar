"""Offline testy workers/gate.py.

Bramka jest jedynym miejscem w systemie, które cokolwiek odrzuca, więc jej błąd
NIE MA jak się ujawnić: odrzucony post nie trafia nigdzie, nie ma alertu, nie ma
wiersza w logu. Bramka kasująca co dziesiąte zlecenie wygląda w produkcji tak
samo jak bramka idealna. Te testy są jedynym miejscem, w którym różnicę widać
przed wdrożeniem.

Każdy przypadek "ma przejść" jest wart ~300 zł kursu. Każdy "ma odpaść" jest
wart ~0,002 zł tokenów. Ta asymetria decyduje, które testy są tu naprawdę ważne.

Bez sieci i bez bazy — moduł jest czysty z założenia.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.workers import gate as g  # noqa: E402


def w(tresc: str, prog: int | None = None):
    """Werdykt bramki niezależny od trybu — w cieniu `przepusc` jest zawsze True."""
    return g.gate(tresc, prog=prog, tryb=g.TRYB_AKTYWNY)


def przepuszcza(tresc: str, prog: int | None = None) -> bool:
    return w(tresc, prog).werdykt


# ===========================================================================
# PRZYPADKI OBOWIĄZKOWE z zadania — jeśli któryś pada, bramka jest zepsuta
# ===========================================================================
def test_obowiazkowy_prosba_wprost_z_trasa():
    assert przepuszcza("Potrzebuję lawety z Krosna do Rzeszowa, golf nie odpala")


def test_obowiazkowy_reklama_konkurencji():
    assert not przepuszcza("Laweta 24/7 Podkarpacie, konkurencyjne ceny, zapraszam")


def test_obowiazkowy_prosba_o_polecenie():
    assert przepuszcza("Polecicie kogoś kto przewiezie auto do warsztatu?")


def test_obowiazkowy_sprzedaz_sprzetu():
    assert not przepuszcza("Sprzedam lawetę Iveco Daily, stan bdb")


def test_obowiazkowy_ogloszenie_o_pracy():
    assert not przepuszcza("Zatrudnię kierowcę kat. C na lawetę")


def test_obowiazkowy_bez_ogonkow():
    """Ludzie piszą bez ogonków — normalizacja musi to wyrównać."""
    assert przepuszcza("stanalem na dk28 auto nie pali")


def test_obowiazkowy_ostrzezenie_o_korku_to_nie_zlecenie():
    assert not przepuszcza("Uwaga korek na obwodnicy, wypadek")


def test_obowiazkowy_przepuszczenie_bije_odrzucenie():
    """Jeśli ten test pada, warstwy są w złej kolejności — i to jest cały test.

    "Sprzedam" jest w warstwie 3, "odholować" w warstwie 2. Filtr sprawdzający
    odrzucenia jako pierwsze wyrzuciłby prawdziwe zlecenie.
    """
    assert przepuszcza("Sprzedam golfa po stłuczce, ale trzeba go odholować z parkingu")


def test_obowiazkowy_faktura_to_klient_b2b_nie_spam():
    assert przepuszcza("Potrzebna laweta, firma, proszę o fakturę VAT")


def test_obowiazkowy_kategoria_prawa_jazdy_to_nie_rekrutacja():
    assert przepuszcza("Mam ciężarówkę kat. C, zepsuła się skrzynia, kto przewiezie")


def test_obowiazkowy_wygaszenie_wersalikami():
    assert not przepuszcza("NIEAKTUALNE - dziękuję wszystkim, znalazłem kogoś")


def test_obowiazkowy_wygaszenie_dopisane_edytem():
    """Post ma komplet słów zlecenia — ratuje nas TYLKO to, że warstwa 1 jest pierwsza."""
    assert not przepuszcza("Potrzebuję lawety pilnie!! EDIT: załatwione, dzięki")


def test_obowiazkowy_transport_planowany_zza_granicy():
    assert przepuszcza("kupilem auto w niemczech, kto przywiezie na lawecie")


def test_obowiazkowy_zdarzenie_bez_slowa_laweta():
    assert przepuszcza("wjechalem w rowie pod Sanokiem, potrzebuje wyciagniecia")


def test_obowiazkowy_sondaz_cenowy_z_ciekawosci():
    assert not przepuszcza("Ile bierzecie za holowanie do 50 km? Pytam z ciekawości")


def test_obowiazkowy_awaria_w_trasie():
    assert przepuszcza("flak na s19, nie mam zapasowego, ktos w okolicy?")


# ===========================================================================
# WARSTWA 1 — WYGASZENIE
# ===========================================================================
def test_wygaszenie_ma_powod_wygaszone():
    assert w("Potrzebna laweta. NIEAKTUALNE").powod == "wygaszone"


def test_wygaszenie_temat_zamkniety():
    assert not przepuszcza("Potrzebna laweta pilnie. Temat zamknięty, dziękuję")


def test_wygaszenie_ktos_juz_jedzie():
    assert not przepuszcza("Potrzebna laweta na dk19, ktoś już jedzie")


def test_wygaszenie_juz_nie_potrzebuje():
    assert not przepuszcza("Szukam lawety do Rzeszowa — już nie potrzebuję, dzięki")


def test_wygaszenie_odwolane():
    assert not przepuszcza("Transport auta z Niemiec — odwołane, kupujący się rozmyślił")


def test_znalazlem_AUTO_to_poczatek_zlecenia_a_nie_koniec():
    """Zawężenie wzorca "znalazlem" — bez niego kasujemy prawdziwy lead.

    "Znalazłem auto w Niemczech, kto przywiezie" to POCZĄTEK zlecenia.
    Wygaszeniem jest "znalazłem KOGOŚ", nie "znalazłem cokolwiek".
    """
    assert przepuszcza("Znalazłem auto w Niemczech, kto przywiezie na lawecie?")
    assert not przepuszcza("Dzięki za odzew, znalazłem kogoś do przewiezienia auta")


def test_mam_juz_kupione_auto_to_nie_wygaszenie():
    assert przepuszcza("Mam już kupione auto w Belgii, potrzebuję transportu")
    assert not przepuszcza("Mam już lawetę, dzięki za pomoc wszystkim zainteresowanym")


def test_juz_po_naprawie_to_nie_wygaszenie():
    """"Już po" wygasza tylko w połączeniu ze sprawą/tematem — nie z naprawą."""
    assert przepuszcza("Auto już po naprawie, kto przewiezie je z warsztatu do domu?")
    assert not przepuszcza("Potrzebna laweta na dk28. Już po sprawie, dzięki")


def test_z_gory_dziekuje_wszystkim_to_OTWARTA_prosba():
    """Polska formuła grzecznościowa nie może kasować zlecenia."""
    assert przepuszcza("Auto stoi na parkingu i nie rusza. Z góry dziękuję wszystkim")
    assert not przepuszcza("Dziękuję wszystkim, sprawa rozwiązana, laweta była na miejscu")


# ===========================================================================
# WARSTWA 2 — TWARDE PRZEPUSZCZENIE
# ===========================================================================
def test_prosba_o_polecenie_to_pelnoprawny_lead():
    """Autor szuka WYKONAWCY — najczystszy możliwy sygnał zakupowy."""
    assert przepuszcza("Polecicie jakąś lawetę w okolicy Krosna?")
    assert przepuszcza("Znacie kogoś z lawetą, kto pojedzie do Kolonii?")
    assert przepuszcza("Kogo polecacie do transportu auta z aukcji?")


def test_transport_planowany_jest_glownym_produktem():
    assert przepuszcza("Kupiłem auto z komisu pod Hanowerem, szukam kogoś na powrót")
    assert przepuszcza("Sprowadzam auto z Holandii w przyszłym tygodniu")
    # „SZUKAM wolnego miejsca", nie „MAM wolne miejsce". Ten przypadek brzmiał
    # tu wcześniej „Mam wolne miejsce na lawecie, doładunek z Włoch" — czyli był
    # ofertą przewoźnika zapisaną jako zlecenie, i dokładnie taki post budził
    # w produkcji telefon. Odwrócenie czasownika jest sednem poprawki: fraza
    # została ta sama, zmieniła się strona rynku (patrz KORPUS_OFERTY).
    assert przepuszcza("Szukam wolnego miejsca na lawecie, doładunek z Włoch")
    assert przepuszcza("Zlecę transport, dwa auta z Austrii do Podkarpacia")


def test_frazy_awaryjne_zostaja_w_slowniku():
    """Auto, które nie jeździ, trzeba przewieźć — czy spod Krosna, czy spod Kolonii.

    O tym, czy kurs się opłaca, decyduje KIEROWCA po obejrzeniu trasy, a nie
    bramka po słowie kluczowym.
    """
    assert przepuszcza("Passat nie odpala pod domem, akumulator chyba zdechł")
    assert przepuszcza("Auto po szkodzie stoi na parkingu strzeżonym w Gdańsku")
    assert przepuszcza("Ciągnik niejezdzący, trzeba go zabrać z pola")


def test_krotki_post_przechodzi_bo_warstwa_2_jest_wczesniej():
    """"szukam lawety" ma 13 znaków, a limit długości to 15.

    Gdyby limit był sprawdzany przed przepuszczeniem, ten post by zniknął.
    """
    assert len("szukam lawety") < g.MIN_DLUGOSC
    assert przepuszcza("szukam lawety")


def test_szukam_kierowcy_ktory_przywiezie_auto_to_klient():
    """Rekrutacja jest w warstwie 3, ale warstwa 2 łapie ten post wcześniej."""
    assert przepuszcza("Szukam kierowcy, który przywiezie auto z Niemiec, płacę od ręki")
    assert not przepuszcza("Szukam kierowcy z doświadczeniem, praca od zaraz, CV na maila")


def test_pytanie_o_cene_z_czynnoscia_to_klient():
    """Hamulec cenowy nie może zabijać postów, w których ktoś już wie, czego chce."""
    assert przepuszcza("Ile kosztuje żeby zabrać auto z parkingu w Jaśle do warsztatu?")


# ===========================================================================
# WARSTWA 3 — TWARDE ODRZUCENIE
# ===========================================================================
def test_autopromocja_wymaga_sygnalu_oferty():
    assert not przepuszcza("Pomoc drogowa całodobowo, w mojej ofercie także transport")
    assert not przepuszcza("Usługi lawetowe, wystawiam fakturę, zapraszam do kontaktu")


def test_oferuje_pieniadze_to_KLIENT_a_nie_konkurencja():
    """Zawężenie wzorca "oferuje" — bez niego kasujemy klienta z gotówką."""
    assert przepuszcza("Oferuję 500 zł za przewiezienie auta z Kolonii do Krosna")
    assert not przepuszcza("Oferuję usługi lawetowe na terenie całego Podkarpacia")


def test_sprzedaz_wymaga_przedmiotu_z_branzy():
    assert not przepuszcza("Sprzedam najazd aluminiowy 3m, stan idealny, mało używany")
    # Sprzedaż AUTA to nie sprzedaż sprzętu — takie posty często kończą się kursem.
    assert przepuszcza("Sprzedam golfa, kupujący z Gdańska pyta o transport auta")


def test_rekrutacja_wymaga_sygnalu_pracy():
    assert not przepuszcza("Przyjmę do pracy kierowcę, laweta nowa, stawka do ustalenia")
    assert not przepuszcza("Oferta pracy: kierowca kat. B+E, wyjazdy zagraniczne")


def test_za_krotkie_odpada():
    assert not przepuszcza("hej")
    assert w("hej").powod == "za krotkie"


def test_pusta_tresc_nie_wywala_sie():
    """Worker chodzi z crona — pusty post nie może rzucić wyjątkiem."""
    assert not przepuszcza("")
    assert not przepuszcza("   ")
    assert w(None).werdykt is False  # type: ignore[arg-type]


# ===========================================================================
# WARSTWA 4 — PUNKTACJA
# ===========================================================================
def test_sam_pojazd_nie_wystarcza():
    wynik = w("Ładny ten passat na zdjęciu, gratuluję zakupu kolego")
    assert wynik.punkty < 5 and not wynik.werdykt


def test_pojazd_plus_akcja_wystarcza():
    """POJAZD (+2) + AKCJA (+3) = 5, czyli dokładnie próg."""
    wynik = w("Trzeba zabrać ten samochód z podwórka, stoi od miesiąca")
    assert wynik.punkty >= 5 and wynik.werdykt


def test_problem_plus_miejsce_wystarcza():
    """PROBLEM (+3) + MIEJSCE (+2) = 5."""
    wynik = w("Rozrząd poszedł, stoję na poboczu przy wjeździe na obwodnicę")
    assert wynik.punkty >= 5 and wynik.werdykt


def test_prog_jest_konfigurowalny_jedna_liczba():
    tresc = "Trzeba zabrać ten samochód z podwórka, stoi od miesiąca"
    assert przepuszcza(tresc, prog=5)
    assert not przepuszcza(tresc, prog=99)


def test_hamulec_ciekawosci_dziala_bezwarunkowo():
    assert not przepuszcza("Tak z ciekawości, ile bierzecie za taki kurs w tę stronę?")


def test_ciekawosc_NIE_bije_nazwanej_uslugi():
    """Granica hamulca "z ciekawości": nazwanie usługi wprost wygrywa.

    "Przewóz auta" to warstwa 2, więc post przechodzi mimo "z ciekawości" — i tak
    ma być. Ktoś, kto pyta o cenę KONKRETNEJ usługi, jest klientem niezależnie od
    tego, jak grzecznie zmiękczył pytanie; koszt pomyłki to 0,002 zł. Hamulec ma
    gasić sondaż rynku ("ile bierzecie za kurs"), a nie zapytanie ofertowe.
    """
    assert przepuszcza("Tak z ciekawości, ile kosztuje przewóz auta do Niemiec?")


def test_hamulec_cenowy_nie_dziala_gdy_jest_akcja():
    """Warunkowość hamulca: pytanie o cenę + czynność = klient, nie sondaż."""
    bez_akcji = w("Jaka cena? Pytam o rynek, nic konkretnego na razie")
    z_akcja = w("Jaka cena za przetransportować passata z Wrocławia do Krosna?")
    assert bez_akcji.punkty < z_akcja.punkty


def test_hamulec_czasowy_nie_dziala_przy_pilnosci():
    """"wczoraj" osłabia relację ze zdarzenia, ale nie post krzyczący "pilne"."""
    relacja = w("Wczoraj widziałem tam rozbitą osobówkę na poboczu drogi")
    pilne = w("Wczoraj się zepsuł, ale PILNE — auto blokuje wjazd do warsztatu")
    assert pilne.punkty > relacja.punkty


def test_numer_telefonu_daje_punkt():
    # Treść MUSI omijać warstwę 2 — inaczej post nigdy nie dochodzi do punktacji
    # i test mierzy zero wobec zera. „Osobówka do zabrania" tego nie omijała:
    # „do zabrania" jest twardym przepuszczeniem (forma bezokolicznikowa z giełd).
    bez = w("Osobówka po stłuczce stoi na parkingu, szczegóły w komentarzach")
    z_nr = w("Osobówka po stłuczce stoi na parkingu, dzwońcie 601 234 567")
    assert z_nr.punkty == bez.punkty + 1 + 1  # telefon (+1) i słowo "dzwonic" (+1)


def test_trafienia_niosa_wagi_do_kalibracji():
    wynik = w("Rozrząd poszedł, stoję na poboczu przy wjeździe na obwodnicę")
    assert any("PROBLEM:" in t and "+3" in t for t in wynik.trafienia)
    assert any("MIEJSCE:" in t and "+2" in t for t in wynik.trafienia)


def test_powod_mowi_o_ile_zabraklo():
    wynik = w("Ładny ten passat na zdjęciu, gratuluję zakupu kolego", prog=5)
    assert "punktacja" in wynik.powod and "prog 5" in wynik.powod


# ===========================================================================
# GIEŁDY TRANSPORTOWE — POSTY, KTÓRE BRAMKA REALNIE ZGUBIŁA
#
# Wszystkie sześć poniżej to PRODUKCJA: `czy_zlecenie=true` w tabeli `posty`,
# a bramka je odrzuciła. Cztery są tu z nazwiskami, bo to wzorcowe kursy tego
# operatora — a jeden dostał ZERO punktów, czyli wyglądał w logu dokładnie tak
# samo jak reklama felg.
#
# Każdy z tych testów jest wart ~300 zł. Jeżeli któryś pada, ktoś właśnie
# przywrócił dziurę, przez którą wyleciało sześć kursów.
# ===========================================================================
GIELDA_ZGUBIONE = [
    # 3 pkt przy progu 5 — sam „transportu", bez trasy i bez pojazdu.
    "Witam wszystkich. Szukam wolnego jednego miejsca do transportu Citroena C5 "
    "z 64354 Reinheim Niemcy do Polski 97-400 Belchatow",
    # 3 pkt — „odebrania" trafiało w AKCJĘ, reszta posta w nic.
    "Do odebrania Opel Meriva 2009 Frankfurt (Copart Buddingen) - Wroclaw",
    # 3 pkt — j.w., „zabrania".
    "Do zabrania iveco solowka okolice paryza do 08-110 siedlce",
    # ZERO punktów. Najpoważniejsza dziura: komplet zlecenia (pojazd, dwa kody,
    # kierunek) i ani jednego trafienia w słowniku.
    "Do przywiezienia Citroen Berlingo z kodu 18556 Wiek Do Doruchow 63-505",
]


def test_gielda_wszystkie_zgubione_posty_przechodza_przy_progu_3():
    for tresc in GIELDA_ZGUBIONE:
        assert przepuszcza(tresc, prog=3), tresc


def test_gielda_forma_bezokolicznikowa_to_TWARDE_przepuszczenie():
    """Nie punkty, tylko warstwa 2 — post z giełdy bywa telegraficzny i drugiego
    sygnału po prostu nie ma. Próg absurdalnie wysoki, żeby punktacja nie mogła
    tego przypadkiem uratować."""
    for tresc in ("Do przywiezienia Citroen Berlingo z kodu 18556 Do Doruchow 63-505",
                  "Do zabrania solowka spod Paryza",
                  "Do odebrania auto po aukcji",
                  "Do sciagniecia spod Berlina",
                  "Do podjecia bus z Hamburga",
                  "Szukam wolnego jednego miejsca do transportu",
                  "Szukam miejsca w transporcie na przyszly tydzien",
                  "Wolne miejsce w ten piatek?",
                  "Kto wraca z Holandii?",
                  "Kto jedzie w kierunku Wroclawia?"):
        wynik = w(tresc, prog=99)
        assert wynik.werdykt, tresc
        assert wynik.powod == "zgloszenie z gieldy", (tresc, wynik.powod)


def test_dwa_kody_pocztowe_to_trasa():
    """Para kodów bywa JEDYNYM sygnałem trasy — słownika miejscowości nie mamy."""
    jeden = w("Berlingo z kodu 18556, odbior po poludniu")
    dwa = w("Berlingo z kodu 18556 do Doruchow 63-505, odbior po poludniu")
    assert dwa.punkty == jeden.punkty + g.WAGA_DWA_KODY
    assert any("dwa kody pocztowe" in t for t in dwa.trafienia)


def test_ten_sam_kod_dwa_razy_to_nie_trasa():
    """„97-400" i „97400" to jeden adres zapisany dwa razy, a nie kurs."""
    assert g.kody_pocztowe(g.normalizuj("Odbior 97-400 Belchatow, kod 97400")) == ["97-400"]


def test_kody_pocztowe_czterech_rynkow():
    kody = g.kody_pocztowe(g.normalizuj(
        "z 38-400 przez 64354 i 110 00 do 1234, tel 601 234 567"))
    assert "38-400" in kody and "64354" in kody and "110 00" in kody and "1234" in kody


def test_marka_pojazdu_punktuje():
    """Post z giełdy mówi „Citroena C5", nie „samochód osobowy"."""
    for marka in ("Citroen", "Opel", "Iveco", "Mercedes", "VW", "Renault", "Ford",
                  "Audi", "BMW", "Skoda", "Fiat", "Peugeot", "Toyota", "Nissan"):
        wynik = w(f"{marka} stoi na parkingu od tygodnia, wlasciciel nie odbiera")
        assert any("POJAZD:" in t and "+3" in t for t in wynik.trafienia), marka


def test_marki_widza_wszystkie_slowniki():
    """Marka nie jest polskim słowem — niemiecki post ma za nią dostać tyle samo."""
    for jezyk in ("pl", "de", "cs"):
        wynik = g.gate("Peugeot Boxer, Autohaus", prog=5, tryb=g.TRYB_AKTYWNY,
                       jezyk=jezyk)
        assert wynik.punkty >= 5, jezyk


def test_zrodlo_auta_punktuje():
    for zrodlo in ("Copart", "Autohaus Muller", "komis", "aukcja"):
        wynik = w(f"Opel Meriva 2009 Frankfurt ({zrodlo}) - Wroclaw")
        assert any("ZRODLO:" in t and "+2" in t for t in wynik.trafienia), zrodlo


def test_polski_post_nie_dostaje_flagi_niemieckiej_przez_marke():
    """Wzorce niezależne od języka (marki, kody) punktują w KAŻDYM słowniku,
    a polskie twarde przepuszczenie ma z definicji zero punktów. Bez preferencji
    dla nazwanej reguły „Do zabrania iveco … do 08-110 siedlce" wygrywał
    słownikiem NIEMIECKIM — i operator dostawał w alercie flagę 🇩🇪, czyli
    podpowiedź, żeby zadzwonić po niemiecku (docs/WIELOJEZYCZNOSC.md)."""
    for tresc in GIELDA_ZGUBIONE:
        wynik = w(tresc, prog=3)
        assert wynik.jezyk == "pl", (tresc, wynik.jezyk)
        assert wynik.powod == "zgloszenie z gieldy", (tresc, wynik.powod)


def test_obcy_post_nadal_dostaje_swoj_znacznik():
    """Poprawka wyżej nie może przechylić WSZYSTKIEGO na polski — niemieckie
    i czeskie zgłoszenie mają dalej trafiać w swój słownik."""
    assert w("Suche Autotransport von Munchen nach Krakau, Fahrzeug fahrt nicht").jezyk == "de"
    assert w("Hledam odtah auta z Prahy do Brna, nenastartuje").jezyk == "cs"


# ===========================================================================
# KATEGORIA ŁADUNKU — ZWIERZĘTA
#
# Te giełdy mieszają transport aut z transportem koni. Operator zwierząt NIE
# wozi — i to jest dokładnie powód, dla którego bramka NIE MA prawa ich
# odrzucać: „nie wożę" i „nie chcę o tym wiedzieć" to dwie różne rzeczy, a druga
# należy do kierowcy. Post przechodzi, dostaje znacznik i ląduje niżej.
#
# Test na odrzucenie (`przepuszcza(...) is True`) jest tu WAŻNIEJSZY niż test
# na samą kategorię: pomyłka w kategorii wycisza jeden alert, a twarde
# odrzucenie kasuje zlecenie bez śladu.
# ===========================================================================
ZWIERZETA_Z_PRODUKCJI = [
    "Szukam transportu dla walacha: Kebliny -> stado ogierow Boguslawice",
    "Poszukuje transportu dla 8 mc osiolka z 42-262 Nowa Wies do 33-100 Tarnow",
    "Potrzebny transport busem jednego konia (+duzo sprzetu) z Gajewnik",
]


def test_zwierzeta_przechodza_bramke_przy_progu_3():
    for tresc in ZWIERZETA_Z_PRODUKCJI:
        assert przepuszcza(tresc, prog=3), tresc


def test_zwierzeta_dostaja_kategorie():
    for tresc in ZWIERZETA_Z_PRODUKCJI:
        assert w(tresc, prog=3).kategoria_ladunku == g.KAT_ZWIERZE, tresc


def test_zwierzeta_zostawiaja_slad_w_trafieniach():
    """Znacznik bez powodu wygląda na awarię — operator ma widzieć, KTÓRE słowo."""
    wynik = w("Potrzebny transport busem jednego konia z Gajewnik")
    assert any(t.startswith("ZWIERZE:") for t in wynik.trafienia), wynik.trafienia


def test_zwierze_bije_pojazd_bo_bus_jest_srodkiem_a_nie_ladunkiem():
    assert g.kategoria_ladunku("transport busem jednego konia") == g.KAT_ZWIERZE


def test_slownik_zwierzat_z_zadania_w_komplecie():
    for slowo in ("kon", "konia", "koni", "koniowoz", "walach", "klacz", "ogier",
                  "zrebak", "kucyk", "osiolek", "osiol", "krowa", "byk", "ciele",
                  "owca", "koza", "transport zwierzat", "przewoz zwierzat", "stado"):
        assert g.kategoria_ladunku(f"Potrzebny transport, {slowo}, plac gotowka") \
            == g.KAT_ZWIERZE, slowo


def test_zwierzeta_nie_lapia_czestych_slow_z_postow_o_lawetach():
    """„kon" bez granicy słowa oznaczyłoby jako konia połowę grupy: kontakt,
    konkurencję, koniec, konserwację. To jest test na tę granicę."""
    for tresc in ("Kontakt na priv, konieczna laweta, koniec tematu",
                  "Konkurencja wozi taniej, ale konserwacja podwozia u mnie",
                  "Potrzebuje lawety, golf nie odpala, dzwonic po 16",
                  "Do przywiezienia Citroen Berlingo z kodu 18556 do 63-505"):
        assert g.kategoria_ladunku(tresc) != g.KAT_ZWIERZE, tresc


# ===========================================================================
# KIERUNEK ZGŁOSZENIA — kto kogo szuka
#
# Oferta przewoźnika ma KOMPLET cech zlecenia: trasę, datę i telefon. Punktacja
# nie ma jej jak odróżnić, bo mierzy obecność słów, a nie stronę rynku. Oba
# posty niżej przeszły w produkcji jako zlecenia i obudziły telefon.
#
# Test na rozstrzyganie czasownikiem (`test_czasownik_rozstrzyga...`) jest tu
# NAJWAŻNIEJSZY: ta sama fraza po obu stronach rynku znaczy co innego, więc
# pomyłka w tę drugą stronę kasuje realne zlecenie o doładunek.
# ===========================================================================
OFERTY_Z_PRODUKCJI = [
    "Czwartek 06.08.26r wolna laweta Elblag-Lublin tel.501606207",
    "Wolny transport 10.08 na trasie Grudziadz - Warszawa - Siedlce "
    "Woj Maz 25T 9,5m Tel. 607284682",
]


def test_oferty_z_produkcji_odpadaja_na_bramce():
    for tresc in OFERTY_Z_PRODUKCJI:
        wynik = w(tresc)
        assert wynik.werdykt is False, tresc
        assert wynik.powod == "oferta przewoznika", (tresc, wynik.powod)


def test_oferty_z_produkcji_dostaja_kierunek():
    """Werdykt mówi „nie pytamy modelu", kierunek mówi CZEMU — i to on zostaje
    w bazie, bo cudzy kurs na naszej trasie bywa okazją na doładunek."""
    for tresc in OFERTY_Z_PRODUKCJI:
        assert w(tresc).kierunek == g.KIERUNEK_OFERTA, tresc


def test_oferty_zostawiaja_slad_w_trafieniach():
    wynik = w(OFERTY_Z_PRODUKCJI[0])
    assert any(t.startswith("OFERTA:") for t in wynik.trafienia), wynik.trafienia


def test_oferta_odpada_przy_dowolnie_niskim_progu():
    """To NIE jest odrzucenie punktowe. Próg zero przepuszcza wszystko, co doszło
    do warstwy 4 — oferta nie dochodzi, bo kierunek rozstrzyga się wcześniej."""
    for tresc in OFERTY_Z_PRODUKCJI:
        assert w(tresc, prog=0).werdykt is False, tresc


def test_czasownik_rozstrzyga_a_nie_sama_fraza():
    """Sedno poprawki: „wolne miejsce" po obu stronach rynku, dwa różne werdykty."""
    zlecenie = w("Szukam wolnego miejsca na lawecie z Kolonii do Krakowa")
    assert zlecenie.werdykt is True
    assert zlecenie.kierunek == g.KIERUNEK_ZLECENIE

    oferta = w("Mam wolne miejsce na lawecie z Kolonii do Krakowa")
    assert oferta.werdykt is False
    assert oferta.kierunek == g.KIERUNEK_OFERTA


def test_slownik_ofert_z_zadania_w_komplecie():
    """Każda fraza z listy zgłoszenia — sprawdzana na gołym poście, bez żadnego
    innego sygnału, bo to jedyny sposób, żeby zobaczyć, że wzorzec w ogóle trafia."""
    for fraza in ("wolna laweta", "wolny transport", "wolne miejsce", "wolne miejsca",
                  "mam wolne", "mam miejsce", "zostalo miejsce", "jedno miejsce wolne",
                  "jade z Krosna", "jade do Krosna", "jade na trasie", "wracam z Berlina",
                  "wracam do Krosna", "powrot z Berlina", "powrot do Krosna",
                  "trasa dnia", "kursuje", "podejme ladunek", "podejme transport",
                  "przyjme ladunek", "zabiore po drodze", "moge zabrac",
                  "moge dolozyc", "doladunek wolny",
                  # DE / CS — te same frazy, ten sam werdykt
                  "freier platz", "habe platz", "fahre am Freitag", "rueckfahrt",
                  "leerfahrt", "volne misto", "jedu z Prahy", "volny odtah"):
        tresc = f"{fraza} Elblag - Lublin 25T, tel 501606207"
        assert g.kierunek(tresc) == g.KIERUNEK_OFERTA, fraza
        assert w(tresc).werdykt is False, fraza


def test_popyt_bije_oferte_bo_pomylka_w_te_strone_kosztuje_kurs():
    """Post z frazą oferty, który MIMO TO jest zleceniem. Każdy z nich zginąłby,
    gdyby oferta rozstrzygała sama z siebie — a to najdroższy błąd tego modułu."""
    for tresc in (
        # awaria złapana w drodze: „jadę do" i zepsute auto w jednym zdaniu
        "Jade do Warszawy, auto stanelo na A4 i nie odpala, potrzebna laweta",
        "Wracam z Niemiec i zepsula mi sie skrzynia pod Poznaniem",
        # klient pytający o czyjś powrót
        "Kto wraca z Holandii? Mam osobowke do zabrania",
        # kupno auta z frazą trasy
        "Kupilem auto w Kolonii, jade do Krosna, kto przywiezie?",
        # niemiecka awaria z frazą oferty
        "Fahre am Freitag nach Polen, aber mein Auto springt nicht an, suche Hilfe",
    ):
        assert przepuszcza(tresc), tresc
        assert w(tresc).kierunek != g.KIERUNEK_OFERTA, tresc


def test_pytanie_jest_sygnalem_popytu():
    """Przewoźnik ogłaszający wolne miejsce nie pyta — podaje trasę i telefon.
    Bez tej reguły odpadałby produkcyjny „Wolne miejsce w ten piątek?", czyli
    post bez ani jednego czasownika, którego nie ratuje żaden wzorzec popytu."""
    assert przepuszcza("Wolne miejsce w ten piatek?")
    assert w("Wolne miejsce w ten piatek?").kierunek != g.KIERUNEK_OFERTA
    # ...i to samo zdanie bez znaku zapytania jest już ogłoszeniem.
    assert not przepuszcza("Wolne miejsce w ten piatek")


def test_kierunek_niejasny_gdy_padly_oba_sygnaly():
    """Bramka nie udaje, że wie. „Niejasne" niczego nie odrzuca i niczego nie
    wycisza — post idzie do modelu, który przeczyta zdanie."""
    wynik = w("Jade do Berlina w piatek, szukam lawety dla golfa")
    assert wynik.kierunek == g.KIERUNEK_NIEJASNY
    assert wynik.werdykt is True


def test_kierunek_niejasny_dla_zwyklego_zlecenia_bez_frazy_oferty():
    """„Zlecenie" wymaga sygnału popytu; jego brak to „niejasne", nie „oferta"."""
    assert g.kierunek("Do przywiezienia Citroen Berlingo z 18556 do 63-505") \
        == g.KIERUNEK_ZLECENIE
    assert g.kierunek("Golf 4, 38-400 Krosno") == g.KIERUNEK_NIEJASNY


def test_kierunek_nie_rusza_korpusu_smieci_ani_zlecen():
    """Nowy mechanizm ma odrzucać OFERTY, a nie przestawiać werdykty, które
    działały. Korpus zleceń pilnuje tego osobno; tu chodzi o to, że żaden post
    z korpusu śmieci nie zaczyna nagle przechodzić."""
    for tresc in KORPUS_SMIECI:
        assert not przepuszcza(tresc) or g.kierunek(tresc) != g.KIERUNEK_OFERTA, tresc


def test_kategoria_pojazdu_i_inne():
    assert g.kategoria_ladunku("Potrzebuje lawety, golf nie odpala") == g.KAT_POJAZD
    assert g.kategoria_ladunku("Dzien dobry wszystkim, pozdrawiam") == g.KAT_INNE


def test_kategoria_nie_zmienia_werdyktu_ani_punktow():
    """Zwierzę to ETYKIETA, nie warstwa. Gdyby zaczęła cokolwiek odejmować,
    stałaby się cichym odrzuceniem — czyli tym, czego ten moduł nie robi."""
    auto = w("Potrzebny transport busem jednego przyczepy z Gajewnik")
    kon = w("Potrzebny transport busem jednego konia z Gajewnik")
    assert kon.werdykt == auto.werdykt
    assert kon.punkty == auto.punkty


def test_kategoria_dziala_takze_przy_twardym_przepuszczeniu():
    """Warstwa 2 kończy pracę przed punktacją — kategoria musi być liczona osobno,
    inaczej najkrótsza ścieżka nigdy by jej nie ustaliła."""
    wynik = w("Szukam transportu dla walacha do stada ogierow", prog=99)
    assert wynik.powod == "prosba wprost"          # rozstrzygnęła warstwa 2
    assert wynik.kategoria_ladunku == g.KAT_ZWIERZE


def test_kategoria_jedzie_do_bazy():
    wiersz = g.wiersz_do_zapisu(w("transport konia z Gajewnik"), "fb-1")
    assert wiersz["kategoria_ladunku"] == g.KAT_ZWIERZE
    assert "kategoria_ladunku" in g.SQL_ZAPIS


# ===========================================================================
# NORMALIZACJA I GRANICE SŁÓW
# ===========================================================================
def test_normalizacja_zbija_ogonki_wersaliki_i_interpunkcje():
    assert g.normalizuj("POTRZEBUJĘ   ŁAWETY!!!  ") == "potrzebuje lawety!"


def test_normalizacja_radzi_sobie_z_obcymi_diakrytykami():
    """"Autohaus München" trafia w te posty regularnie."""
    assert "munchen" in g.normalizuj("Odbiór z Autohaus München")


def test_granica_slowa_hol_nie_lapie_alkoholu():
    assert not przepuszcza("Kolega był po alkoholu i teraz ma problem z prawkiem")


def test_granica_slowa_kolo_nie_lapie_okolo():
    """Sprawdzamy mechanizm granic, nie konkretny wzorzec."""
    assert g._skompiluj("kolo").search("kolo zamachowe")
    assert not g._skompiluj("kolo").search("okolo stu kilometrow")


def test_granica_slowa_tel_nie_lapie_telewizora():
    assert g._skompiluj("tel").search("tel. 601234567")
    assert not g._skompiluj("tel").search("telewizor do przewiezienia")


def test_wzorzec_z_kwantyfikatorem_dopuszcza_ciag_dalszy():
    """"na dk" musi trafić "na dk28", "na dk 19" i samo "na dk"."""
    wz = g._skompiluj("na dk ?[0-9]*")
    assert wz.search("stoje na dk28") and wz.search("na dk 19") and wz.search("na dk")


# ===========================================================================
# TRYB CIENIA — bez niego nie da się stwierdzić, czy bramka jest dobra
# ===========================================================================
def test_w_cieniu_nic_nie_jest_blokowane():
    wynik = g.gate("Laweta 24/7, konkurencyjne ceny", tryb=g.TRYB_CIEN)
    assert wynik.przepusc is True, "w cieniu WSZYSTKO idzie do AI"
    assert wynik.werdykt is False, "ale werdykt bramki ma być zapisany"


def test_w_trybie_aktywnym_werdykt_jest_wiazacy():
    wynik = g.gate("Laweta 24/7, konkurencyjne ceny", tryb=g.TRYB_AKTYWNY)
    assert wynik.przepusc is False and wynik.werdykt is False


def test_nieznany_tryb_degraduje_do_cienia():
    """Literówka w .env nie może po cichu WŁĄCZYĆ blokowania."""
    assert g.normalizuj_tryb("aktywne") == g.TRYB_CIEN
    assert g.normalizuj_tryb("") == g.TRYB_CIEN
    assert g.normalizuj_tryb(None) == g.TRYB_CIEN
    assert g.normalizuj_tryb("AKTYWNY") == g.TRYB_AKTYWNY


def test_do_bazy_idzie_werdykt_a_nie_decyzja_operacyjna():
    """Sedno trybu cienia: gdyby zapisywać `przepusc`, byłyby same jedynki."""
    wynik = g.gate("Zatrudnię kierowcę kat. C", tryb=g.TRYB_CIEN)
    wiersz = g.wiersz_do_zapisu(wynik, "fb123")
    assert wiersz["gate_werdykt"] is False
    assert wiersz["gate_tryb"] == g.TRYB_CIEN
    assert wiersz["fb_id"] == "fb123"


def test_kontrakt_zapisu_pokrywa_sie_z_migracja():
    """Nazwy parametrów muszą zgadzać się z SQL_ZAPIS — inaczej worker padnie w nocy."""
    wiersz = g.wiersz_do_zapisu(g.gate("cokolwiek dluzszego niz limit"), "x")
    for klucz in wiersz:
        assert f"%({klucz})s" in g.SQL_ZAPIS


# ===========================================================================
# WŁASNOŚCI CAŁEGO MODUŁU
# ===========================================================================
def test_wszystkie_wzorce_sa_poprawnymi_regexami():
    """Literówka w słowniku ma paść tutaj, a nie przy pierwszym poście o 3 w nocy."""
    for nazwa, tabela in (("WYGASZENIE", g.WYGASZENIE), ("PRZEPUSZCZENIE", g.PRZEPUSZCZENIE),
                          ("ODRZUCENIE", g.ODRZUCENIE), ("PUNKTACJA", g.PUNKTACJA),
                          ("HAMULCE", g.HAMULCE), ("POPYT", g.POPYT), ("OFERTA", g.OFERTA)):
        for wzorzec, _, _ in tabela:
            assert g._skompiluj(wzorzec), f"{nazwa}: {wzorzec}"


def test_wzorce_sa_zapisane_w_formie_znormalizowanej():
    """Wzorzec z ogonkiem albo wielką literą NIGDY nie trafi — normalizacja go zbija."""
    tabele = (g.WYGASZENIE + g.PRZEPUSZCZENIE + g.ODRZUCENIE + g.PUNKTACJA + g.HAMULCE
              + g.POPYT + g.OFERTA)
    for wzorzec, _, _ in tabele:
        litery = [c for c in wzorzec if c.isalpha()]
        assert all(c.islower() and c.isascii() for c in litery), wzorzec


def test_wagi_punktacji_sa_dodatnie_a_hamulcow_ujemne():
    assert all(waga > 0 for _, waga, _ in g.PUNKTACJA)
    assert all(waga < 0 for _, waga, _ in g.HAMULCE)
    assert all(waga < 0 for _, waga, _, _ in g.HAMULCE_WARUNKOWE)


def test_modul_nie_dotyka_sieci_ani_dysku():
    """"Zero wywołań sieciowych" — sprawdzamy to na imporcie, nie na obietnicy."""
    import laweta_radar.workers.gate as modul

    zrodlo = open(modul.__file__, encoding="utf-8").read()
    for zakazane in ("import httpx", "import requests", "import psycopg2", "urlopen"):
        assert zakazane not in zrodlo, f"bramka ma zostać offline, a widzę {zakazane}"


def test_bramka_jest_szybka():
    """Bramka chodzi po każdym poście z każdego runu — musi być darmowa w czasie."""
    import time

    tresc = "Potrzebuję lawety z Krosna do Rzeszowa, golf nie odpala, tel 601234567"
    start = time.perf_counter()
    for _ in range(200):
        w(tresc)
    assert (time.perf_counter() - start) / 200 < 0.01


# ===========================================================================
# KORPUS — najważniejszy test w tym pliku
#
# Pojedyncze przypadki wyżej pilnują konkretnych mechanizmów. Ten pilnuje
# WŁASNOŚCI CAŁEJ BRAMKI: na próbce postów napisanych tak, jak ludzie piszą
# w tych grupach, liczba fałszywych odrzuceń ma wynosić ZERO.
#
# Trzy błędy znalazły się dopiero tutaj i żaden nie miał jak wyjść w testach
# jednostkowych: "ktoś jedzie w tamtą stronę?" gaszone przez wzorzec na "ktoś
# już jedzie", "ciągnik do ZABRANIA" nietrafiony przez wzorzec w bezokoliczniku
# i "koparka do PRZETRANSPORTOWANIA" — ta sama odmiana. Wszystkie trzy kasowały
# realne kursy i wszystkie trzy wyglądały w kodzie na poprawne.
#
# Dopisując słowo do słownika, dopisz tu post, który je uzasadnia.
# ===========================================================================
KORPUS_ZLECENIA = [
    # transport planowany — główny produkt operatora
    "Kupiłem BMW w Duesseldorfie, potrzebuję transportu do Rzeszowa na przyszły tydzień",
    "Odbiór auta spod komisu w Hanowerze, ktoś jedzie w tamtą stronę?",
    "Zlecę przewóz dwóch aut z Belgii, płatne przelewem, faktura VAT",
    "Kto jedzie do Holandii w okolicach 20-go? Mam osobówkę do zabrania",
    "Szukam miejsca na lawecie dla Golfa IV, trasa Kolonia - Krosno",
    # Ta pozycja brzmiała wcześniej „Mam wolne miejsce na lawecie, wracam pusty
    # z Włoch w sobotę" — czyli była OFERTĄ przewoźnika w korpusie zleceń.
    # Przeniesiona do KORPUS_OFERTY; tutaj zostaje ta sama sytuacja widziana
    # od strony klienta, bo to ona jest zleceniem.
    "Szukam wolnego miejsca na lawecie, powrót z Włoch w sobotę",
    "Sprowadzam auto z aukcji w Niemczech, ile za transport do Jasła?",
    "Trzy auta z Austrii do Podkarpacia, termin elastyczny, proszę o wycenę na PW",
    "Znalazłem ładnego passata w Belgii, kto go przywiezie?",
    "Potrzebny transport busa dostawczego z Czech, waga ok 2,8t",
    # awaria i zdarzenie drogowe
    "Golf nie odpala pod domem w Korczynie, akumulator chyba padł. Ktoś w okolicy?",
    "Stanąłem na DK28 za Duklą, sprzęgło poszło. Potrzebna laweta",
    "Wjechałem do rowu pod Sanokiem, auto całe ale trzeba wyciągnąć",
    "Skrzynia się zepsuła, auto stoi na parkingu przy Biedronce, trzeba zabrać do warsztatu",
    "Flak na S19, brak zapasowego, ktoś obok?",
    "Auto powypadkowe do przewiezienia z parkingu policyjnego w Krośnie",
    "Ciężarówka kat. C stoi na MOP przy A4, turbina padła, kto pomoże?",
    "Mam ciągnik niejezdzący do zabrania z pola, jakieś 12 km od Krosna",
    "Silnik zgasł na obwodnicy i nie chce ruszyć, blokuję pas",
    "Motocykl po stłuczce, trzeba przewieźć do serwisu w Rzeszowie",
    # prośba o polecenie
    "Polecicie kogoś sprawdzonego do transportu auta z Niemiec?",
    "Znacie jakąś lawetę w okolicy Brzozowa? Potrzebna na jutro",
    "Kogo polecacie do przewiezienia kampera? Dość duży",
    "Szukam firmy, która przywiezie auto z Autohaus w Monachium",
    # sformułowania nietypowe — tu bramka najłatwiej się myli
    "Trzeba zabrać osobówkę z podwórka, stoi od pół roku i nie odpala",
    "Ile kosztuje żeby ściągnąć auto z parkingu strzeżonego do domu?",
    "Oferuję 800 zł za przewiezienie auta z Kolonii do Krosna, termin dowolny",
    "Sprzedam golfa po stłuczce, kupujący prosi o transport do Gdańska",
    "Potrzebna pomoc drogowa, auto nie na chodzie, ul. Spacerowa",
    "Koparka do przetransportowania, 4 km, mam własne najazdy",
]

KORPUS_SMIECI = [
    "Laweta 24/7 Podkarpacie, konkurencyjne ceny, zapraszam do kontaktu",
    "Usługi lawetowe, wystawiam fakturę, tanio i solidnie",
    "Oferuję usługi lawetowe na terenie całego województwa, atrakcyjne ceny",
    "Sprzedam lawetę Iveco Daily 2015, stan bardzo dobry, zadbana",
    "Sprzedam najazdy aluminiowe 3m, nośność 2t, mało używane",
    "Sprzedam wciągarkę elektryczną 12V, 4500 lbs, do lawety",
    "Sprzedam przyczepkę lekką, hamowana, stan idealny",
    "Zatrudnię kierowcę kat. C na lawetę, wyjazdy zagraniczne",
    "Praca dla kierowcy B+E, stała trasa Niemcy-Polska, CV na maila",
    "Przyjmę do pracy mechanika i kierowcę, dobre warunki",
    "Oferta pracy: kierowca lawety, umowa o pracę, Krosno",
    "NIEAKTUALNE - dziękuję wszystkim, znalazłem kogoś",
    "Potrzebuję lawety pilnie!! EDIT: załatwione, dzięki",
    "Szukam lawety do Rzeszowa. Temat zamknięty, ktoś już jedzie",
    "Transport auta z Niemiec - odwołane, sprzedający się rozmyślił",
    "Potrzebna laweta na DK19. Już po sprawie, dzięki wszystkim",
    "Uwaga korek na obwodnicy, wypadek przy zjeździe",
    "Ile bierzecie za holowanie do 50 km? Pytam z ciekawości",
    "Ładny ten passat, gratuluję zakupu kolego",
    "Uwaga patrol na krajówce za Miejscem Piastowym",
    "Kto zna dobrego blacharza w Krośnie?",
    "Wczoraj widziałem tam rozbitą osobówkę na poboczu",
    "Sprzedam opony zimowe 205/55 R16, komplet, stan dobry",
    "Dzień dobry wszystkim, nowy na grupie",
]


def test_korpus_zero_falszywych_odrzucen():
    """Jedyna liczba, która ma znaczenie. Nie ma tu miejsca na "prawie zero"."""
    zabite = [(t, w(t)) for t in KORPUS_ZLECENIA if not przepuszcza(t)]
    assert not zabite, "\n" + "\n".join(
        f"  ZABITE ZLECENIE [{r.powod}] {t}\n    {r.trafienia}" for t, r in zabite
    )


def test_korpus_bramka_realnie_cokolwiek_odsiewa():
    """Druga strona: bramka, która przepuszcza wszystko, jest tylko kosztem.

    Próg jest niski (25%), bo celem NIE jest wysoki odsetek — realistycznie
    wychodzi 20-35% i to jest w porządku. Ten test pilnuje tylko, żeby po
    kolejnym rozluźnieniu słownika bramka nie zamieniła się w atrapę.
    """
    korpus = KORPUS_ZLECENIA + KORPUS_SMIECI
    odsiane = sum(1 for t in korpus if not przepuszcza(t))
    assert odsiane >= len(korpus) * 0.25, f"odsiane tylko {odsiane}/{len(korpus)}"


def test_korpus_smieci_ida_w_wiekszosci_do_kosza():
    """Reklamy, sprzedaż sprzętu, rekrutacja i wygaszenia mają odpadać.

    Nie żądamy kompletu: "transport aut" w reklamie konkurencji trafia
    w warstwę 2 i przechodzi, bo sygnał potrzeby BIJE sygnał odrzucenia.
    To jest świadomy koszt tej kolejności — 0,002 zł za post, który AI
    i tak odrzuci. Odwrotna kolejność kosztowałaby kursy.
    """
    odsiane = sum(1 for t in KORPUS_SMIECI if not przepuszcza(t))
    assert odsiane >= len(KORPUS_SMIECI) * 0.8, f"odsiane tylko {odsiane}/{len(KORPUS_SMIECI)}"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))


# ===========================================================================
# WIELOJĘZYCZNOŚĆ — PL / DE / CS / SK
#
# Bramka jednojęzyczna nie wygląda na zepsutą: obcojęzyczne zlecenie dostaje
# zero punktów i wylatuje tak samo cicho jak reklama felg. Jedynym miejscem,
# w którym tę różnicę widać, jest test per język — stąd korpusy niżej.
#
# Wymóg minimalny: po dziesięć przypadków na język, w tym po jednym wygaszonym.
# ===========================================================================
PL_PRZEPUSZCZONE = [
    "Potrzebuję lawety z Krosna do Rzeszowa, golf nie odpala",
    "Potrzebna laweta pilnie, stoję na S19 pod Rzeszowem",
    "Zepsuł mi się samochód pod Jasłem, kto podjedzie?",
    "Szukam lawety, auto po szkodzie, trzeba zabrać z parkingu",
    "Kto przywiezie auto z Niemiec? Kupiłem osobówkę pod Kolonią",
    "Auto niejeżdżące, potrzebne odholowanie do warsztatu",
    "Polecicie jakąś lawetę w okolicy? Skrzynia padła",
    "Szukam miejsca na lawecie, doładunek z Belgii do Krosna",
    "Nie odpala, akumulator zdechł, ktoś z okolic pomoże?",
    # Regresja: post BEZ OGONKÓW ze słowem „kto" — identycznym ze słowackim.
    # Detekcja pokazywała „sk", post szedł tylko przez słownik czesko-słowacki
    # i wylatywał. Fałszywe odrzucenie, czyli błąd, którego bramka ma nie robić.
    "kupilem auto w niemczech, kto przywiezie na lawecie",
]

PL_ODRZUCONE = [
    "Laweta 24/7, konkurencyjne ceny, zapraszam do kontaktu",
    "Sprzedam lawetę, stan idealny, więcej info na priv",
    "Zatrudnię kierowcę na lawetę, praca dla kierowcy od zaraz",
]

PL_WYGASZONE = [
    "Potrzebna laweta pod Sanokiem — TEMAT ZAMKNIĘTY",
    "Nieaktualne, znalazłem już kogoś",
]

DE_PRZEPUSZCZONE = [
    # Post z założenia zadania: przy samych polskich wzorcach dostawał zero.
    "Suche Autotransport von München nach Krakau, Fahrzeug fährt nicht",
    "Brauche Abschleppdienst, mein Auto springt nicht an",
    "Wer kann mein Auto abschleppen? Motor kaputt, stehe auf der A4",
    "Panne auf der Autobahn, bei Dresden liegengeblieben",
    "Unfall, Fahrzeug nicht fahrbereit, wer hat Platz?",
    "Auto transportieren von Berlin nach Poznań, wer kann helfen?",
    "Mein Wagen bleibt stehen und startet nicht mehr",
    "Fahrzeug überführen nach Polen, Auto gekauft vom Händler",
    "Totalschaden nach Unfall, brauche einen Transport nach Polen",
    # Bez umlautów i bez słów funkcyjnych — detekcja NIE rozstrzygnie.
    "Suche Abschleppdienst, Motor kaputt",
]

DE_ODRZUCONE = [
    "Wir bieten Abschleppdienst, günstige Preise",
    "Verkaufe Anhänger, guter Zustand, Preis VB",
    "Suche Fahrer für Abschleppwagen, Stellenangebot ab sofort",
]

DE_WYGASZONE = [
    "Hat sich erledigt, danke euch",
    "Suche Abschleppdienst von Wien nach Brünn — nicht mehr aktuell",
]

CS_PRZEPUSZCZONE = [
    "Hledám odtahovku, auto nenastartuje, jsem u Brna",
    "Potřebuji odtah auta z Prahy do Ostravy",
    "Nepojízdné auto na D1, kdo pomůže?",
    "Nehoda u Plzně, potřebuji odtahovku co nejdřív",
    "Nejde nastartovat, zůstal jsem stát u Kolína",
    "Převoz auta z autobazaru do servisu",
    "Auto nestartuje, mám poruchu na dálnici, je to nutné dnes",
    "Havárie u Liberce, auto je nepojízdné, prosím o odtah",
    "Přeprava auta do servisu, kdo má volno zítra?",
    "Koupil jsem auto z Německa, kdo přiveze?",
]

CS_ODRZUCONE = [
    "Nabízíme odtahovou službu, výhodné ceny",
    "Hledáme řidiče, nabídka práce v dopravě",
    "Prodám odtahovku, dobrý stav",
]

CS_WYGASZONE = [
    "Již vyřešeno, děkuji",
    "Potřebuji odtah z Brna — neaktuální, našel jsem někoho",
]

SK_PRZEPUSZCZONE = [
    "Hľadám odťahovku, auto neštartuje, som pri Žiline",
    "Potrebujem odťah auta z Bratislavy do Košíc",
    "Nepojazdné auto na D1, kto pomôže?",
    "Nehoda pri Nitre, potrebujem odťahovku",
    "Nejde naštartovať, zostal som stáť pri Trenčíne",
    "Prevoz auta z autobazáru do servisu",
    "Auto neštartuje, mám poruchu na diaľnici, súrne",
    "Havária pri Prešove, auto je nepojazdné, prosím o odťah",
    "Preprava auta do servisu, kto má voľno?",
    "Kúpil som auto z Nemecka, kto privezie?",
]

SK_ODRZUCONE = [
    "Ponúkame odťahovú službu, lacné ceny",
    "Prijmeme vodiča, ponuka práce v doprave",
    "Predám odťahovku, dobrý stav",
]

SK_WYGASZONE = [
    "Už vyriešené, ďakujem",
    "Potrebujem odťah z Košíc — neaktuálne, našiel som niekoho",
]

KORPUSY = {
    "pl": (PL_PRZEPUSZCZONE, PL_ODRZUCONE, PL_WYGASZONE),
    "de": (DE_PRZEPUSZCZONE, DE_ODRZUCONE, DE_WYGASZONE),
    "cs": (CS_PRZEPUSZCZONE, CS_ODRZUCONE, CS_WYGASZONE),
    "sk": (SK_PRZEPUSZCZONE, SK_ODRZUCONE, SK_WYGASZONE),
}


def test_jezyki_kazde_zlecenie_przechodzi():
    """NAJWAŻNIEJSZY test w tym pliku — każdy z tych postów jest wart kurs."""
    for jezyk, (przepuszczone, _, _) in KORPUSY.items():
        for tresc in przepuszczone:
            assert przepuszcza(tresc), f"[{jezyk}] miało przejść: {tresc!r}"


def test_jezyki_smieci_odpadaja():
    for jezyk, (_, odrzucone, _) in KORPUSY.items():
        for tresc in odrzucone:
            assert not przepuszcza(tresc), f"[{jezyk}] miało odpaść: {tresc!r}"


def test_jezyki_wygaszone_odpadaja_z_powodem():
    for jezyk, (_, _, wygaszone) in KORPUSY.items():
        for tresc in wygaszone:
            wynik = w(tresc)
            assert not wynik.werdykt, f"[{jezyk}] miało być wygaszone: {tresc!r}"
            assert wynik.powod == "wygaszone", f"[{jezyk}] zły powód: {tresc!r}"


def test_minimum_dziesieciu_przypadkow_na_jezyk():
    """Test o samych testach — wymóg najłatwiejszy do cichego złamania.

    Dopisanie języka do SLOWNIKI bez dopisania korpusu daje zieloną suitę
    i bramkę, która ten język przepuszcza albo kasuje na oślep.
    """
    assert set(KORPUSY) == set(g.SLOWNIKI), (
        "doszedł język do SLOWNIKI, ale nie doszedł korpus testowy")
    for jezyk, listy in KORPUSY.items():
        assert sum(len(x) for x in listy) >= 10, f"za mało przypadków dla {jezyk}"
        assert listy[2], f"brak przypadku wygaszonego dla {jezyk}"


def test_detekcja_jezyka_po_znakach_i_slowach():
    assert g.wykryj_jezyk("Szukam lawety, auto się nie odpala") == "pl"
    assert g.wykryj_jezyk("Ich brauche einen Abschleppwagen, das Auto "
                          "springt nicht an") == "de"
    assert g.wykryj_jezyk("Auto nenastartuje, zůstal jsem stát") == "cs"
    assert g.wykryj_jezyk("Auto neštartuje, zostal som stáť, veľmi súrne") == "sk"


def test_detekcja_oddaje_pustke_gdy_nie_wiadomo():
    """"" nie jest porażką detekcji, tylko jej poprawnym zachowaniem."""
    assert g.wykryj_jezyk("") == ""
    assert g.wykryj_jezyk("Auto 2015 diesel") == ""
    assert g.wykryj_jezyk("Suche Abschleppdienst, Motor kaputt") == ""


def test_czeski_i_slowacki_dziela_slownik_ale_nie_znacznik():
    """Jeden słownik, dwa znaczniki — od znacznika zależy język oddzwonienia."""
    assert g.SLOWNIKI["cs"] is g.SLOWNIKI["sk"]
    assert w("Hledám odtahovku, auto nenastartuje, jsem u Brna").jezyk == "cs"
    assert w("Hľadám odťahovku, auto neštartuje, som pri Žiline").jezyk == "sk"


def test_znacznik_jezyka_wraca_z_bramki():
    """Powiadomienie bierze znacznik stąd — bez niego operator nie wie, w jakim
    języku oddzwonić, bo reszta alertu jest już po polsku."""
    assert w("Potrzebuję lawety, golf nie odpala").jezyk == "pl"
    assert w("Suche Autotransport von München nach Krakau").jezyk == "de"


def test_wygaszenie_dziala_ponad_jezykami():
    """Wygaszenie widziane przez JAKIKOLWIEK słownik wygasza post.

    Samo „weź najlepszy wynik" tu nie wystarcza: słownik, który nie zna zwrotu
    „hat sich erledigt", po prostu milczy — a milczenie wygląda lepiej niż
    odrzucenie. Post załatwiony przestaje być zleceniem niezależnie od języka.
    """
    wynik = w("Suche Abschleppdienst — hat sich erledigt")
    assert not wynik.werdykt
    assert wynik.powod == "wygaszone"


def test_wymuszony_jezyk_zawezanie_do_jednego_slownika():
    """`jezyk=` jest dla grup, o których wiadomo z góry, czym są."""
    tresc = "Suche Abschleppdienst, Motor kaputt"
    assert g.gate(tresc, tryb=g.TRYB_AKTYWNY, jezyk="de").werdykt
    assert not g.gate(tresc, tryb=g.TRYB_AKTYWNY, jezyk="pl").werdykt


def test_bledna_detekcja_nie_kasuje_zlecenia():
    """Nawet gdy detekcja wskaże ZŁY język, post nadal idzie przez wszystkie
    słowniki — pomyłka detekcji nie może być cichym fałszywym odrzuceniem."""
    tresc = "kupilem auto w niemczech, kto przywiezie na lawecie"
    assert przepuszcza(tresc)


def test_wszystkie_wzorce_obcojezyczne_sa_poprawnymi_regexami():
    """Literówka w niemieckim czy czeskim słowniku ma paść tutaj, a nie przy
    pierwszym poście o trzeciej w nocy."""
    tabele = (g.WYGASZENIE_DE + g.PRZEPUSZCZENIE_DE + g.ODRZUCENIE_DE
              + g.PUNKTACJA_DE + g.HAMULCE_DE
              + g.WYGASZENIE_CS_SK + g.PRZEPUSZCZENIE_CS_SK
              + g.ODRZUCENIE_CS_SK + g.PUNKTACJA_CS_SK + g.HAMULCE_CS_SK)
    for wzorzec, _, _ in tabele:
        assert g._skompiluj(wzorzec), wzorzec


def test_wzorce_obcojezyczne_sa_znormalizowane():
    """Wzorzec z umlautem albo haczkiem NIGDY nie trafi — normalizacja go zbija,
    a wzorzec i tekst muszą być w tej samej formie."""
    tabele = (g.WYGASZENIE_DE + g.PRZEPUSZCZENIE_DE + g.ODRZUCENIE_DE
              + g.PUNKTACJA_DE + g.HAMULCE_DE
              + g.WYGASZENIE_CS_SK + g.PRZEPUSZCZENIE_CS_SK
              + g.ODRZUCENIE_CS_SK + g.PUNKTACJA_CS_SK + g.HAMULCE_CS_SK)
    for wzorzec, _, _ in tabele:
        assert wzorzec == g.normalizuj(wzorzec), (
            f"wzorzec nie jest w formie znormalizowanej: {wzorzec!r}")


# ===========================================================================
# REGRESJA — czasownik potrzeby + rzeczownik transportu (CS/SK i DE)
#
# „Potrebujem prepravu auta z Bratislavy do Kosic" to najczęstsza forma
# zlecenia na tym rynku, a przechodziła WYŁĄCZNIE punktacją, na 3 punkty:
# przy GATE_PROG=3 ledwo, przy jakimkolwiek podniesieniu progu — wcale.
# Słownik znał czasownik potrzeby tylko w parze z „odtah".
#
# Wszystkie przypadki niżej sprawdzane przy JAWNYM prog=5, żeby werdykt nie
# zależał od kalibracji progu — wzorcowe zlecenie ma być twardym
# przepuszczeniem, nie przypadkiem na granicy punktacji.
# ===========================================================================
def test_cs_sk_potrzeba_z_rzeczownikiem_transportu_to_twarde_przepuszczenie():
    for tresc in (
        "Potrebujem prepravu auta z Bratislavy do Kosic",
        "Potrebuji prepravu vozu z Prahy do Brna",
        "Hladam prepravu vozidla do Polska",
        "Zhanim odvoz auta po nehode",
    ):
        wynik = w(tresc, prog=5)
        assert wynik.werdykt, f"miało przejść przy progu 5: {tresc!r}"
        assert wynik.powod == "prosba wprost", (
            f"miało przejść WARSTWĄ 2, nie punktacją: {tresc!r} -> {wynik.powod}")


def test_cs_sk_potrzeba_z_bezokolicznikiem_lub_zdaniem_wzglednym():
    for tresc in (
        "Potrebujem prepravit auto do Zvolena",
        "Potrebuji prevezt auto z Plzne",
        "Potrebujem odviezt auto zo servisu",
        "Hladam niekoho kto preveze auto do Polska",
        "Hledam nekoho kdo preveze auto do Nemecka",
    ):
        assert przepuszcza(tresc, prog=5), f"miało przejść przy progu 5: {tresc!r}"


def test_cs_sk_kontrola_oferty_nadal_odpadaja():
    """Nowe wzorce wymagają czasownika potrzeby — autopromocja i oferta
    przewoźnika mają odpadać dokładnie tak jak przed zmianą."""
    assert not przepuszcza("Ponukam odtahovu sluzbu 24/7")
    assert not przepuszcza("Volne miesto na odtahovke Praha - Wien")


# ===========================================================================
# REGRESJA — „vyťazovák", potoczna słowacka nazwa lawety z wciągarką
#
# Realny post z grupy „Vyťazováky CZ/SK - odťah a preprava vozidel":
# „Hladam vytazovak Polsko Slovensko" dostawał ZERO punktów i wylatywał, bo
# słownik nie znał słowa podstawowego dla całej grupy. Przypadki przy JAWNYM
# prog=5 z tego samego powodu co wyżej: zlecenie z czasownikiem szukania ma
# być twardym przepuszczeniem, nie przypadkiem na granicy punktacji.
# ===========================================================================
def test_cs_sk_vytazovak_z_czasownikiem_szukania_to_twarde_przepuszczenie():
    for tresc in (
        # Post z zadania, bez diakrytyków — tak wygląda w grupie.
        "Hladam vytazovak Polsko Slovensko",
        # To samo z diakrytykami — normalizacja ma je zbić do tej samej formy.
        "Hľadám vyťahovák Poľsko Slovensko",
        # „ť" pisane jako „t" z apostrofem — klawiatura bez słowackiego układu.
        "Zhanam vyt'ahovak Zilina - Krakov",
        "Potrebujem plosinu do Zvolena",
        "Hladam navijak na vytiahnutie auta z priekopy",
    ):
        wynik = w(tresc, prog=5)
        assert wynik.werdykt, f"miało przejść przy progu 5: {tresc!r}"
        assert wynik.powod == "prosba wprost", (
            f"miało przejść WARSTWĄ 2, nie punktacją: {tresc!r} -> {wynik.powod}")


def test_cs_sk_vytazovak_punktuje_bez_czasownika_szukania():
    """Posty telegraficzne nie mają „hladam" — samo słowo waży +3 jak „odtah".

    Przed zmianą: „surne" +3 i koniec, 3 < 5. Po zmianie rzeczownik domyka
    post do progu punktacją, bez twardego przepuszczenia.
    """
    assert przepuszcza("Treba vytazovak Kosice - Presov, surne", prog=5)
    assert przepuszcza("Treba plosinu Kosice - Zvolen, surne", prog=5)


def test_cs_sk_vytazovak_kontrola_podazy_nadal_odpada():
    """Rzeczownik wszedł do punktacji, więc strona PODAŻY musi odpadać nazwaną
    regułą — inaczej reklama dozbierałaby punkty z „tel" i trasy."""
    wynik = w("Ponukam vytazovak 24/7")
    assert not wynik.werdykt, "autopromocja miała odpaść"
    assert wynik.powod == "autopromocja", wynik.powod
    assert not przepuszcza("Predam vytazovak, dobry stav")


def test_cs_sk_volny_vytazovak_to_oferta_przewoznika():
    """Odpowiednik polskiej „wolnej lawety" — kierunek rozstrzyga przed
    warstwami, a POPYT bije OFERTĘ."""
    wynik = w("Volny vytazovak Kosice - Zilina, tel. 0908 123 456")
    assert not wynik.werdykt
    assert wynik.powod == "oferta przewoznika"
    # Ten sam rzeczownik z czasownikiem szukania NIE odpada jako oferta —
    # sygnał popytu powstrzymuje odrzucenie kierunkiem, jak wszędzie w tej
    # tabeli. Oba sygnały naraz dają „niejasne", które niczego nie odrzuca.
    wynik = w("Hladam volny vytazovak Kosice - Zilina", prog=5)
    assert wynik.kierunek != g.KIERUNEK_OFERTA
    assert wynik.powod != "oferta przewoznika"


def test_de_potrzeba_z_transportem_to_twarde_przepuszczenie():
    """Ta sama konstrukcja po niemiecku: przed zmianą twardym przepuszczeniem
    był tylko wariant „brauche ... transport"; „suche Transport" wisiał na
    punktacji, a „benoetige Transport" przy progu 5 wylatywał."""
    for tresc in (
        "Suche Transport fuer mein Auto nach Polen",
        "Benoetige Transport von Hamburg nach Warschau",
        "Benötige einen Transport für meinen PKW",
        "Brauche einen Transport nach Polen",
    ):
        assert przepuszcza(tresc, prog=5), f"miało przejść przy progu 5: {tresc!r}"


def test_de_suche_transport_nie_lapie_przewoznika_szukajacego_ladunkow():
    """„Suche Transportaufträge" to przewoźnik szukający ładunków, nie klient —
    prawa granica słowa po „transport" ma go tu NIE wpuścić."""
    wynik = w("Suche Transportauftraege, LKW 3,5t, faire Preise", prog=5)
    assert wynik.powod != "prosba wprost", wynik.powod


def test_instrukcja_dla_klasyfikatora_niesie_kontrakt():
    """Klasyfikator dostaje ten tekst przez import, nie przez przepisanie.

    Bez wyjątku na nazwy miejscowości geokodowanie dostanie „Monachium"
    i zlecenie wyląduje na mapie w złym miejscu.
    """
    tekst = g.INSTRUKCJA_JEZYKOWA_DLA_KLASYFIKATORA.lower()
    for fragment in ("niemieck", "czesk", "słowack", "po polsku", "oryginal"):
        assert fragment in tekst, f"z instrukcji zniknęło: {fragment}"
