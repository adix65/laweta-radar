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
    assert przepuszcza("Mam wolne miejsce na lawecie, doładunek z Włoch")
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
    bez = w("Osobówka do zabrania z podwórka, szczegóły w komentarzach")
    z_nr = w("Osobówka do zabrania z podwórka, dzwońcie 601 234 567")
    assert z_nr.punkty == bez.punkty + 1 + 1  # telefon (+1) i słowo "dzwonic" (+1)


def test_trafienia_niosa_wagi_do_kalibracji():
    wynik = w("Rozrząd poszedł, stoję na poboczu przy wjeździe na obwodnicę")
    assert any("PROBLEM:" in t and "+3" in t for t in wynik.trafienia)
    assert any("MIEJSCE:" in t and "+2" in t for t in wynik.trafienia)


def test_powod_mowi_o_ile_zabraklo():
    wynik = w("Ładny ten passat na zdjęciu, gratuluję zakupu kolego", prog=5)
    assert "punktacja" in wynik.powod and "prog 5" in wynik.powod


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
                          ("HAMULCE", g.HAMULCE)):
        for wzorzec, _, _ in tabela:
            assert g._skompiluj(wzorzec), f"{nazwa}: {wzorzec}"


def test_wzorce_sa_zapisane_w_formie_znormalizowanej():
    """Wzorzec z ogonkiem albo wielką literą NIGDY nie trafi — normalizacja go zbija."""
    tabele = (g.WYGASZENIE + g.PRZEPUSZCZENIE + g.ODRZUCENIE + g.PUNKTACJA + g.HAMULCE)
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
    "Mam wolne miejsce na lawecie, wracam pusty z Włoch w sobotę",
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
