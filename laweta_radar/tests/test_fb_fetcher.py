"""Offline testy workers/fb_fetcher.py — bez sieci, bez bazy, bez modelu.

Fetcher jest jedynym modułem w tym repo, który WYDAJE PIENIĄDZE. Dlatego testy
skupiają się nie na tym, czy „działa", tylko na tych własnościach, których
złamanie kosztuje i nie widać go w logach:

  1. ŚCIEŻKA A/B jest CZYTANA Z POMIARU, a bez pomiaru schodzi na B. Odwrotna
     domyślna (A przy ignorowanym oknie) to pięćdziesiąt opłaconych postów na
     grupę na przebieg, bez żadnego objawu poza rachunkiem.
  2. BUDŻET JEST TWARDY. Rozdział nie przekracza sufitu, a grupa bez historii
     dostaje pulę startową — bez niej nigdy nie da się jej zmierzyć.
  3. KOLEJNOŚĆ: BRAMKA -> ŚWIEŻOŚĆ -> MODEL. Przestawienie dwóch linijek
     w pętli sprawia, że model dostaje wszystko, co przyszło z Apify. Test woła
     klasyfikator, który WYBUCHA, więc każde wywołanie nie na miejscu jest
     błędem, a nie „trochę wyższym rachunkiem".
  4. WYCIĄGANIE PÓL JEST DEFENSYWNE. Actor zmienia kształt odpowiedzi między
     wersjami; brak pola ma dawać None, a nie wyjątek w środku przebiegu.
  5. TEMPO LICZY SIĘ Z REALNEJ HISTORII. Ta jedna poprawka odróżnia limit
     adaptacyjny od limitu, który po dwóch dniach spada do podłogi i już
     z niej nie wstaje.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.config import groups as cfg_groups  # noqa: E402
from laweta_radar.workers import fb_fetcher as f  # noqa: E402

TERAZ = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _post(tresc: str = "Szukam lawety, auto nie odpala", **nadpisz) -> dict:
    dane = {"tresc": tresc, "post_url": "https://fb.com/p/1", "group_url": "g",
            "group_name": "Grupa", "author_name": "Jan", "post_date": TERAZ}
    dane.update(nadpisz)
    return dane


# ---------------------------------------------------------------------------
# 1. Ścieżka A/B — najdroższa decyzja w systemie
# ---------------------------------------------------------------------------
def test_sciezka_z_pomiaru(tmp_path):
    """Werdykt czytamy z raportu, a nie z przepisanej ręcznie zmiennej.

    Przepisanie jest krokiem, który da się pominąć — i wtedy fetcher pracuje na
    czyjejś intuicji zamiast na pomiarze, nie mówiąc o tym ani słowem.
    """
    # Forma, którą generuje scripts/pomiar_actora.py: kropka W ŚRODKU
    # pogrubienia. To ona odróżnia werdykt od prozy — patrz test niżej.
    raport = tmp_path / "POMIAR-ACTORA.md"
    raport.write_text("Wniosek z pomiaru:\n\n**ŚCIEŻKA A.**\n", encoding="utf-8")
    sciezka, skad = f.wykryj_sciezke(raport, nadpisanie="")
    assert sciezka == "A"
    assert "pomiar" in skad

    raport.write_text("Wniosek z pomiaru:\n\n**ŚCIEŻKA B.**\n", encoding="utf-8")
    assert f.wykryj_sciezke(raport, nadpisanie="")[0] == "B"


def test_proza_zaslepki_NIE_jest_werdyktem(tmp_path):
    """Zaślepka raportu OPISUJE obie ścieżki („**ŚCIEŻKA A** — okno działa").

    Regeks czytający samą nazwę trafiłby w to wyjaśnienie i uznał je za wynik
    pomiaru — akurat ten hojniejszy, czyli najdroższy z możliwych błędów: pełny
    resultsLimit z każdej grupy w każdym przebiegu, bez objawu poza rachunkiem.
    """
    raport = tmp_path / "POMIAR-ACTORA.md"
    raport.write_text(
        "> ## POMIAR NIE ZOSTAŁ JESZCZE WYKONANY\n\n"
        "- **ŚCIEŻKA A** — okno działa: liczba itemów maleje wraz ze zwężaniem\n"
        "- **ŚCIEŻKA B** — jednostka jest ignorowana\n",
        encoding="utf-8")
    sciezka, skad = f.wykryj_sciezke(raport, nadpisanie="")
    assert sciezka == "B", "proza zaślepki została odczytana jako werdykt pomiaru"
    assert "zaślepka" in skad


def test_bez_pomiaru_schodzimy_na_sciezke_b(tmp_path):
    """Brak pomiaru = ścieżka B. To jest asymetria kosztów, nie statystyka.

    Przy założeniu B i rzeczywistości A tracimy część oszczędności. Przy
    założeniu A i rzeczywistości B płacimy pełny `resultsLimit` z każdej grupy
    w każdym przebiegu.
    """
    brak = tmp_path / "nie-ma-takiego-pliku.md"
    sciezka, skad = f.wykryj_sciezke(brak, nadpisanie="")
    assert sciezka == "B"
    assert "brak raportu" in skad

    # Raport bez werdyktu i bez ramki — też ostrożnie.
    pusty = tmp_path / "POMIAR-ACTORA.md"
    pusty.write_text("# Pomiar\n\nNic tu nie ma.\n", encoding="utf-8")
    sciezka, skad = f.wykryj_sciezke(pusty, nadpisanie="")
    assert sciezka == "B"
    assert "nie zawiera rozstrzygnięcia" in skad


def test_nadpisanie_ze_srodowiska_wygrywa(tmp_path):
    raport = tmp_path / "POMIAR-ACTORA.md"
    raport.write_text("**ŚCIEŻKA B.**\n", encoding="utf-8")
    sciezka, skad = f.wykryj_sciezke(raport, nadpisanie="a")
    assert sciezka == "A"
    assert "SCIEZKA_ACTORA" in skad


def test_format_okna_zalezy_od_sciezki():
    """W ścieżce B nie wysyłamy okien poniżej doby.

    Wartość, której actor nie rozumie, to najgorszy wariant: run bez filtra za
    pełną cenę, bez błędu i bez śladu w logach.
    """
    assert f._okno_dla_apify(30, "A") == "30 minutes"
    assert f._okno_dla_apify(60, "A") == "1 hour"
    assert f._okno_dla_apify(180, "A") == "3 hours"
    for minuty in (5, 30, 60, 240):
        assert f._okno_dla_apify(minuty, "B") == "1 day"


def test_wejscie_actora_ma_jedna_grupe():
    """Jedna grupa na wywołanie. Batch nie oszczędza kredytu, a przy globalnym
    `resultsLimit` gubi posty z większości grup w paczce — i tak zostaje
    policzony."""
    wejscie = f._build_actor_input("https://fb.com/groups/x", 12, 60, "A")
    assert wejscie["startUrls"] == [{"url": "https://fb.com/groups/x"}]
    assert wejscie["resultsLimit"] == 12
    assert wejscie["onlyPostsNewerThan"] == "1 hour"
    # `resultsLimit` działa u tego actora tylko przy sortowaniu po nowości.
    assert wejscie["sortingOrder"] == cfg_groups.APIFY_SORT


# ---------------------------------------------------------------------------
# 2. Budżet — twardy sufit i pula startowa
# ---------------------------------------------------------------------------
def test_rozdzial_nie_przekracza_budzetu():
    """Sufit dobowy jest twardy, bo pula kont Apify jest WSPÓLNA z drugim
    systemem. Cichy nadmiar to jego awaria, nie nasza."""
    urle = [f"g{i}" for i in range(5)]
    statystyki = {u: {"pobrane": 100, "zlecenia": 5} for u in urle}
    przydzial = f.rozdziel_budzet(urle, 300, statystyki)
    assert sum(przydzial.values()) <= 300
    assert set(przydzial) == set(urle)


def test_grupa_bez_historii_dostaje_pule_startowa():
    """Bez puli startowej nowa grupa nigdy nie zbierze danych, na podstawie
    których bandyta mógłby jej cokolwiek przyznać — a grupa bez danych wygląda
    dla niego tak samo jak grupa bezwartościowa."""
    urle = ["stara", "nowa"]
    statystyki = {"stara": {"pobrane": 500, "zlecenia": 40}}
    przydzial = f.rozdziel_budzet(urle, 1000, statystyki, pula_startowa=60)
    assert przydzial["nowa"] >= 60
    assert sum(przydzial.values()) <= 1000


def test_pula_startowa_nie_wychodzi_ponad_budzet():
    """Przy budżecie mniejszym od puli startowej nie dokładamy ponad sufit."""
    przydzial = f.rozdziel_budzet(["a", "b"], 20, {}, pula_startowa=60)
    assert sum(przydzial.values()) <= 20


def test_pusty_rozdzial_nie_wybucha():
    assert f.rozdziel_budzet([], 100, {}) == {}
    assert f.rozdziel_budzet(["a"], 0, {}) == {}


# ---------------------------------------------------------------------------
# 3. Odstęp i limit — inne znaczenie w każdej ze ścieżek
# ---------------------------------------------------------------------------
def test_odstep_w_sciezce_a_zalezy_od_tempa_nie_od_budzetu():
    """W ścieżce A koszt dobowy nie zależy od odstępu (płacimy za przyrost),
    więc odstęp jest decyzją o CZASIE REAKCJI: pytamy, gdy uzbierał się post."""
    gadatliwa = f.interwal_min(tempo_h=6.0, przydzial=100, sciezka="A",
                               min_interwal=cfg_groups.MIN_INTERWAL_MIN_A)
    cicha = f.interwal_min(tempo_h=0.2, przydzial=100, sciezka="A",
                           min_interwal=cfg_groups.MIN_INTERWAL_MIN_A)
    assert gadatliwa < cicha
    assert gadatliwa >= cfg_groups.MIN_INTERWAL_MIN_A
    assert cicha <= cfg_groups.MAX_INTERWAL_MIN
    # Ten sam budżet, inne tempo -> inny odstęp; budżet nie steruje tu niczym.
    assert f.interwal_min(6.0, 10, "A", 5) == f.interwal_min(6.0, 10_000, "A", 5)


def test_odstep_w_sciezce_b_zalezy_od_budzetu():
    """W ścieżce B każdy przebieg to pełny `resultsLimit` opłaconych postów —
    częstotliwość boli wprost proporcjonalnie, więc odstęp liczymy z budżetu."""
    bogata = f.interwal_min(tempo_h=1.0, przydzial=400, sciezka="B",
                            min_interwal=cfg_groups.MIN_INTERWAL_MIN_B)
    biedna = f.interwal_min(tempo_h=1.0, przydzial=20, sciezka="B",
                            min_interwal=cfg_groups.MIN_INTERWAL_MIN_B)
    assert bogata < biedna
    assert bogata >= cfg_groups.MIN_INTERWAL_MIN_B
    assert biedna <= cfg_groups.MAX_INTERWAL_MIN


def test_dolna_granica_odstepu_jest_ostrozniejsza_w_sciezce_b():
    """Widełki są różne, bo w B gęstsze pytanie to wprost wyższy rachunek."""
    assert cfg_groups.MIN_INTERWAL_MIN_B > cfg_groups.MIN_INTERWAL_MIN_A


def test_sufit_limitu_jest_hojny_w_a_i_ciasny_w_b():
    """W A limit jest zabezpieczeniem od góry (koszt tnie warunek wieku),
    w B limit JEST kosztem, co do sztuki."""
    assert cfg_groups.MAX_POSTOW_NA_GRUPE_A > cfg_groups.MAX_POSTOW_NA_GRUPE_B
    limit_a, _ = f._adaptive_group_params(tempo_h=20.0, odstep_min=30, sciezka="A")
    limit_b, _ = f._adaptive_group_params(tempo_h=20.0, odstep_min=30, sciezka="B")
    assert limit_a == cfg_groups.MAX_POSTOW_NA_GRUPE_A
    assert limit_b == cfg_groups.MAX_POSTOW_NA_GRUPE_B


def test_okno_w_sciezce_a_to_dwukrotnosc_odstepu():
    """Zapas na opóźnienie moderacji: post zatwierdzony po poprzednim przebiegu
    musi się jeszcze zmieścić w oknie, inaczej przepada bezpowrotnie."""
    _, okno = f._adaptive_group_params(tempo_h=2.0, odstep_min=60, sciezka="A")
    assert okno == 60 * cfg_groups.MNOZNIK_OKNA
    # Podłoga okna — przy odstępie 5 minut nie pytamy o okno, którego actor
    # może nie obsłużyć.
    _, okno_krotkie = f._adaptive_group_params(tempo_h=2.0, odstep_min=5, sciezka="A")
    assert okno_krotkie == cfg_groups.MIN_OKNO_MIN


def test_grupa_bez_historii_dostaje_bootstrap_a_nie_podloge():
    """Podłoga (dwa posty) przy nowej grupie znaczyłaby, że nigdy nie zobaczymy,
    ile ta grupa naprawdę postuje — więc tempo zostanie zerowe na zawsze."""
    limit, _ = f._adaptive_group_params(tempo_h=0.0, odstep_min=30, sciezka="B")
    assert limit > cfg_groups.MIN_POSTOW_NA_GRUPE
    assert limit == min(cfg_groups.DOMYSLNIE_POSTOW_NA_GRUPE,
                        cfg_groups.MAX_POSTOW_NA_GRUPE_B)


def test_nadpisanie_limitu_wylacza_adaptacje():
    limit, _ = f._adaptive_group_params(tempo_h=99.0, odstep_min=30, sciezka="B",
                                        nadpisanie=3)
    assert limit == 3


# ---------------------------------------------------------------------------
# 4. Tempo — poprawka na krótką historię
# ---------------------------------------------------------------------------
def test_tempo_liczy_sie_z_REALNEJ_historii():
    """Grupa scrapowana od doby ma posty tylko z doby. Dzielenie ich przez pełne
    siedem dni zaniża tempo siedmiokrotnie, limit spada do podłogi, a paczka
    postów zatwierdzona przez moderatora naraz przepada — i czego nie
    pobraliśmy, tego nie ma w bazie, więc następny przebieg liczy z jeszcze
    uboższych danych. Ta spirala kosztowała w repo źródłowym tydzień leadów.
    """
    stat = {"ostatnie": 24, "pierwszy_pobrany_at": TERAZ - timedelta(hours=24)}
    tempo = f.tempo_na_godzine(stat, TERAZ, okno_tempa_h=24 * 7)
    assert tempo == pytest.approx(1.0, rel=0.01), "24 posty w dobę to 1 post/h"

    # To samo bez poprawki dałoby 24/168 = 0.14 — siedmiokrotnie za mało.
    bez_poprawki = 24 / (24 * 7)
    assert tempo > bez_poprawki * 6


def test_tempo_dlugiej_historii_dzieli_przez_pelne_okno():
    stat = {"ostatnie": 168, "pierwszy_pobrany_at": TERAZ - timedelta(days=30)}
    assert f.tempo_na_godzine(stat, TERAZ, okno_tempa_h=24 * 7) == pytest.approx(1.0)


def test_brak_historii_to_zero_a_nie_wyjatek():
    assert f.tempo_na_godzine({}, TERAZ, 168) == 0.0
    assert f.tempo_na_godzine({"ostatnie": 0}, TERAZ, 168) == 0.0


# ---------------------------------------------------------------------------
# 5. Plan przebiegu
# ---------------------------------------------------------------------------
def _grupy(ile: int = 2) -> list[dict]:
    return [{"url": f"https://fb.com/groups/{i}", "name": f"Grupa {i}"}
            for i in range(ile)]


def test_plan_pomija_grupe_przed_czasem():
    grupy = _grupy(1)
    harmonogram = {grupy[0]["url"]: {
        "nastepny_run_at": TERAZ + timedelta(minutes=30), "pobrane_doba": 0}}
    plan = f.zbuduj_plan(grupy, {}, harmonogram, budzet=1000, sciezka="B",
                         skad_sciezka="test", teraz=TERAZ)
    assert plan.do_pobrania == []
    assert "nie ta minuta" in plan.grupy[0].powod


def test_plan_pomija_grupe_z_wyczerpanym_przydzialem():
    grupy = _grupy(1)
    url = grupy[0]["url"]
    harmonogram = {url: {"nastepny_run_at": None, "pobrane_doba": 999}}
    plan = f.zbuduj_plan(grupy, {}, harmonogram, budzet=100, sciezka="B",
                         skad_sciezka="test", teraz=TERAZ)
    assert plan.do_pobrania == []
    assert "przydział" in plan.grupy[0].powod


def test_plan_pomija_wszystko_po_wyczerpaniu_sufitu():
    """Cicho przekroczony budżet to spalona pula kont, z której korzysta też
    drugi system — więc wyczerpany sufit ZATRZYMUJE przebieg, a nie spowalnia."""
    grupy = _grupy(2)
    harmonogram = {g["url"]: {"nastepny_run_at": None, "pobrane_doba": 60}
                   for g in grupy}
    plan = f.zbuduj_plan(grupy, {}, harmonogram, budzet=100, sciezka="B",
                         skad_sciezka="test", teraz=TERAZ)
    assert plan.zuzyte_doba == 120
    assert plan.zostalo == 0
    assert plan.do_pobrania == []


def test_licznik_dobowy_obejmuje_grupy_spoza_tego_przebiegu():
    """Sufit jest sufitem SYSTEMU, nie sumą grup, które akurat wypadają.

    Posty pobrane rano przez grupę wyłączoną w południe (albo ręcznym `--grupa`)
    też zostały opłacone i też zabrały kredyt wspólnej puli. Liczenie tylko po
    bieżącej liście grup dawałoby budżet, który da się obejść, wyłączając grupę.
    """
    grupy = _grupy(1)
    harmonogram = {
        grupy[0]["url"]: {"nastepny_run_at": None, "pobrane_doba": 10},
        "https://fb.com/groups/wylaczona": {"nastepny_run_at": None,
                                            "pobrane_doba": 500},
    }
    plan = f.zbuduj_plan(grupy, {}, harmonogram, budzet=400, sciezka="B",
                         skad_sciezka="test", teraz=TERAZ)
    assert plan.zuzyte_doba == 510
    assert plan.zostalo == 0


def test_plan_wymuszony_z_cli_ignoruje_harmonogram():
    """`--grupa URL` ma pobrać TERAZ — inaczej nie da się niczego przetestować
    na żywym actorze bez czekania na slot."""
    grupy = _grupy(1)
    harmonogram = {grupy[0]["url"]: {
        "nastepny_run_at": TERAZ + timedelta(hours=2), "pobrane_doba": 0}}
    plan = f.zbuduj_plan(grupy, {}, harmonogram, budzet=1000, sciezka="B",
                         skad_sciezka="test", teraz=TERAZ,
                         ignoruj_harmonogram=True)
    assert len(plan.do_pobrania) == 1


def test_plan_liczy_koszt_przed_wydaniem_pieniedzy():
    plan = f.zbuduj_plan(_grupy(3), {}, {}, budzet=1000, sciezka="B",
                         skad_sciezka="test", teraz=TERAZ, nadpisanie_limitu=10)
    assert plan.koszt_usd == pytest.approx(3 * 10 * cfg_groups.CENA_USD_ZA_POST)
    opis = "\n".join(f.opis_planu(plan))
    assert "przewidywany koszt" in opis
    assert "NIE zmierzona" in opis, "szacunek musi się przyznawać, że nie jest pomiarem"


# ---------------------------------------------------------------------------
# 6. Kolejność: BRAMKA -> ŚWIEŻOŚĆ -> MODEL
# ---------------------------------------------------------------------------
def _klasyfikator_ktory_wybucha(*args, **kwargs):
    raise AssertionError("model NIE MIAŁ być pytany o ten post")


PROG = TERAZ - timedelta(hours=6)


SMIEC = "Zatrudnię kierowcę kat. C, praca dla kierowcy od zaraz"


def test_w_trybie_AKTYWNYM_odrzucony_post_nie_kosztuje_ani_jednego_tokena(monkeypatch):
    """To jest cała oszczędność tego systemu — nie skrót, tylko powód, dla
    którego stać nas na klasyfikator."""
    monkeypatch.setattr(f.settings, "GATE_TRYB", "aktywny")
    decyzja = f.decyzja_o_poscie(_post(SMIEC), PROG,
                                 klasyfikuj=_klasyfikator_ktory_wybucha)
    assert decyzja.zrodlo == "gate"
    assert decyzja.czy_zlecenie is False
    assert decyzja.status == "smiec"
    assert decyzja.pytano_model is False


def test_w_trybie_CIENIA_ten_sam_post_IDZIE_do_modelu(monkeypatch):
    """Cień NIC nie blokuje — i to jest cena, którą płacimy za wiedzę o tym,
    ile bramka by skasowała. Opinia bramki i tak trafia do bazy.

    Gdyby ten test przestał przechodzić, znaczyłoby to, że tryb cienia zaczął
    blokować — czyli że system po cichu zaczął gubić zlecenia w okresie,
    w którym miał wyłącznie mierzyć.
    """
    monkeypatch.setattr(f.settings, "GATE_TRYB", "cien")
    wolano = []
    decyzja = f.decyzja_o_poscie(
        _post(SMIEC), PROG,
        klasyfikuj=lambda *a: wolano.append(a) or {"czy_zlecenie": False})
    assert wolano, "w cieniu model MA być pytany także o śmieci"
    assert decyzja.pytano_model is True
    assert decyzja.zrodlo == "ai"
    # ...ale OPINIA bramki jest zapisana i to z niej liczy się raport.
    assert decyzja.gate_werdykt is False
    assert decyzja.gate_tryb == "cien"


def test_post_za_stary_nie_idzie_do_modelu_ale_laduje_w_bazie():
    """Zapisujemy (materiał do statystyki grupy), nie płacimy i nie budzimy."""
    stary = _post(post_date=TERAZ - timedelta(hours=9))
    decyzja = f.decyzja_o_poscie(stary, PROG, klasyfikuj=_klasyfikator_ktory_wybucha)
    assert decyzja.stale is True
    assert decyzja.pytano_model is False
    assert decyzja.status == "smiec"
    assert decyzja.czy_zlecenie is False


def test_post_bez_daty_nie_jest_uznawany_za_stary():
    """Brak daty to zwykle layout „przed chwilą", którego actor nie sparsował —
    czyli post NAJŚWIEŻSZY. Odrzucanie go byłoby najgorszą możliwą reakcją."""
    decyzja = f.decyzja_o_poscie(_post(post_date=None), PROG,
                                 klasyfikuj=lambda *a: {"czy_zlecenie": True})
    assert decyzja.stale is False
    assert decyzja.pytano_model is True


def test_swiezy_post_po_bramce_idzie_do_modelu():
    wolania = []

    def klasyfikuj(tresc, grupa, jezyk):
        wolania.append((tresc, grupa, jezyk))
        return {"czy_zlecenie": True, "powod": "prosi o lawetę"}

    decyzja = f.decyzja_o_poscie(_post(), PROG, grupa="Grupa 1", klasyfikuj=klasyfikuj)
    assert len(wolania) == 1
    assert wolania[0][1] == "Grupa 1"
    assert wolania[0][2] == "pl", "klasyfikator dostaje znacznik języka z bramki"
    assert decyzja.zrodlo == "ai"
    assert decyzja.czy_zlecenie is True
    assert decyzja.status == "nowe"


def test_brak_klasyfikatora_zostawia_post_w_kolejce():
    """Bramka mówi „warto zapytać", a nie „to jest zlecenie". Zapisanie jej
    werdyktu jako czy_zlecenie=true zapełniłoby kolejkę operatora reklamami,
    które model by odsiał."""
    decyzja = f.decyzja_o_poscie(_post(), PROG, klasyfikuj=lambda *a: None)
    assert decyzja.zrodlo == "gate"
    assert decyzja.czy_zlecenie is False
    assert decyzja.status == "nowe", "czeka na klasyfikację, nie jest śmieciem"


def test_brak_modulu_klasyfikatora_to_ciche_none():
    """Dopóki `workers/classifier.py` nie istnieje, szew oddaje None bez hałasu.

    Odróżnienie od „moduł jest i się wysypał" jest w `_klasyfikuj` celowe: brak
    `anthropic` w środowisku to zepsuty deploy, a nie etap budowy, i ma lecieć
    wyjątkiem, a nie chować się pod tym samym `except ImportError`.
    """
    import importlib.util

    if importlib.util.find_spec("laweta_radar.workers.classifier") is None:
        assert f._klasyfikuj("treść", "Grupa", "pl") is None
    assert f._bez_klasyfikatora("treść", "Grupa", "pl") is None


def test_znacznik_jezyka_wedruje_z_bramki_do_zapisu():
    """Od dwuliterowego znacznika zależy, w jakim języku operator ma oddzwonić —
    a tego nie wyczyta z pola wypełnionego po polsku przez klasyfikator."""
    niemiecki = _post("Suche Autotransport von München nach Krakau, "
                      "Fahrzeug fährt nicht")
    decyzja = f.decyzja_o_poscie(niemiecki, PROG, klasyfikuj=lambda *a: None)
    assert decyzja.jezyk == "de"
    assert decyzja.status == "nowe", "niemiecki post MA przechodzić przez bramkę"


def test_transport_zwierzat_przechodzi_pipeline_normalnie():
    """Kategoria ładunku NIE jest odrzuceniem: post o koniu idzie do modelu,
    do bazy i do panelu jak każdy inny. Wycisza go dopiero (i wyłącznie)
    `services/powiadomienia`, i tylko przy ALERT_ZWIERZETA=0."""
    kon = _post("Potrzebny transport busem jednego konia z Gajewnik")
    decyzja = f.decyzja_o_poscie(kon, PROG,
                                 klasyfikuj=lambda *a: {"czy_zlecenie": True})
    assert decyzja.kategoria_ladunku == "zwierze"
    assert decyzja.pytano_model is True, "model MA być pytany także o zwierzęta"
    assert decyzja.czy_zlecenie is True
    assert decyzja.status == "nowe"


def test_kategoria_ladunku_jedzie_do_powiadomienia():
    """Bez tego pola alert widziałby transport konia dokładnie tak samo jak
    transport golfa — czyli obudziłby operatora kursem spoza jego oferty."""
    kon = _post("Potrzebny transport busem jednego konia z Gajewnik")
    decyzja = f.decyzja_o_poscie(kon, PROG,
                                 klasyfikuj=lambda *a: {"czy_zlecenie": True})
    zlecenie = f.zlecenie_do_alertu("fb-1", kon, decyzja,
                                    f.Zapis(bez_ekstrakcji=False, wiersz={}))
    assert zlecenie["kategoria_ladunku"] == "zwierze"


def test_kolumna_kategorii_jest_w_insercie():
    """Kategoria jedzie tym samym INSERT-em co werdykt bramki. Dopisywanie jej
    drugim zapytaniem byłoby drugą okazją do porażki bez żadnego objawu."""
    assert ("kategoria_ladunku", "0010_kategoria_ladunku.sql") in f.KOLUMNY_SWIADKOWIE


# ---------------------------------------------------------------------------
# KIERUNEK — oferta przewoźnika nie kosztuje tokena i nie budzi telefonu,
# ale zostaje w bazie z kompletem tego, co o niej wiemy.
# ---------------------------------------------------------------------------
OFERTA = "Czwartek 06.08.26r wolna laweta Elblag-Lublin tel.501606207"


def test_oferta_nie_dochodzi_do_modelu_w_trybie_aktywnym(monkeypatch):
    """Bramka odrzuca ofertę PRZED modelem — post z kompletem cech zlecenia,
    za który nie płacimy ani jednego tokena."""
    monkeypatch.setattr(f.settings, "GATE_TRYB", "aktywny")
    decyzja = f.decyzja_o_poscie(_post(OFERTA), PROG,
                                 klasyfikuj=_klasyfikator_ktory_wybucha)
    assert decyzja.zrodlo == "gate"
    assert decyzja.pytano_model is False
    assert decyzja.czy_zlecenie is False
    assert decyzja.status == "smiec"
    assert decyzja.kierunek == "oferta"
    assert decyzja.powod == "oferta przewoznika"


def test_w_cieniu_oferta_idzie_do_modelu_ale_z_kierunkiem_z_bramki(monkeypatch):
    """Cień NIC nie blokuje, także ofert — i to jest stan produkcji dzisiaj.
    Odczyt bramki jedzie wtedy do bazy razem z werdyktem modelu i to on jest
    ostatnią linią obrony, gdy model ofertę przeoczy."""
    monkeypatch.setattr(f.settings, "GATE_TRYB", "cien")
    decyzja = f.decyzja_o_poscie(
        _post(OFERTA), PROG, klasyfikuj=lambda *a: {"czy_zlecenie": True})
    assert decyzja.pytano_model is True
    assert decyzja.gate_werdykt is False
    assert decyzja.gate_powod == "oferta przewoznika"
    assert decyzja.kierunek == "oferta"


def test_kierunek_z_modelu_bije_kierunek_z_bramki():
    """Model czyta zdanie, bramka dopasowuje wzorzec — przy rozbieżności rację
    ma model. Bramka na tym poście milczy (żadnej frazy oferty), więc jedynym
    źródłem prawdy jest odczyt modelu."""
    post = _post("Grudziadz - Warszawa - Siedlce 10.08, 25T, tel 607284682")
    decyzja = f.decyzja_o_poscie(
        post, PROG, klasyfikuj=lambda *a: {"czy_zlecenie": False, "kierunek": "oferta"})
    assert decyzja.kierunek == "oferta"
    assert decyzja.czy_zlecenie is False


def test_niejasne_z_modelu_nie_kasuje_odczytu_bramki():
    """„Niejasne" znaczy „nie umiem powiedzieć", a nie „bramka się myli".
    Nadpisanie nim odczytu bramki gubiłoby jedyną informację, jaką mamy."""
    post = _post("Jade w piatek z Warszawy do Wroclawia, mam wolne miejsce")
    decyzja = f.decyzja_o_poscie(
        post, PROG, klasyfikuj=lambda *a: {"czy_zlecenie": True, "kierunek": "niejasne"})
    assert decyzja.kierunek == "oferta"


def test_kierunek_jedzie_do_powiadomienia():
    """Ostatnia linia obrony: bramka rozpoznała ofertę, model orzekł „zlecenie".
    Bez tego pola powiadomienie obudziłoby operatora cudzą lawetą."""
    post = _post("Jade w piatek z Warszawy do Wroclawia, mam wolne miejsce")
    decyzja = f.decyzja_o_poscie(
        post, PROG, klasyfikuj=lambda *a: {"czy_zlecenie": True})
    zlecenie = f.zlecenie_do_alertu("fb-1", post, decyzja,
                                    f.Zapis(bez_ekstrakcji=False, wiersz={}))
    assert zlecenie["kierunek"] == "oferta"


def test_kolumna_kierunku_jest_w_insercie():
    """Kierunek jedzie tym samym INSERT-em co werdykt bramki — i to jest cały
    powód, dla którego odrzucona oferta w ogóle zostaje w bazie."""
    assert ("kierunek", "0011_kierunek.sql") in f.KOLUMNY_SWIADKOWIE


def test_zwykle_zlecenie_nie_dostaje_kierunku_oferta():
    zlecenie = f.decyzja_o_poscie(
        _post("Potrzebuję lawety z Krosna do Rzeszowa, golf nie odpala"),
        PROG, klasyfikuj=lambda *a: {"czy_zlecenie": True})
    assert zlecenie.kierunek == "zlecenie"
    assert zlecenie.czy_zlecenie is True


# ---------------------------------------------------------------------------
# 7. Wyciąganie pól — amortyzator zmian actora
# ---------------------------------------------------------------------------
def test_first_str_bierze_pierwsza_niepusta():
    item = {"a": "", "b": "   ", "c": " wartość ", "d": "inna"}
    assert f._first_str(item, "a", "b", "c", "d") == "wartość"
    assert f._first_str(item, "x", "y") == ""
    assert f._first_str({"a": 5}, "a") == "", "liczba to nie tekst"


def test_author_name_czyta_plaskie_i_zagniezdzone():
    assert f._author_name({"authorName": "Jan"}) == "Jan"
    assert f._author_name({"user": {"name": "Anna"}}) == "Anna"
    assert f._author_name({"owner": {"fullName": "Piotr"}}) == "Piotr"
    assert f._author_name({}) == ""


def test_parse_post_date_obsluguje_wszystkie_ksztalty():
    assert f._parse_post_date("2026-08-04T10:00:00Z") == datetime(
        2026, 8, 4, 10, 0, tzinfo=timezone.utc)
    # Bez offsetu -> traktujemy jako UTC, żeby porównanie z progiem świeżości
    # nie wybuchało na naive vs aware.
    assert f._parse_post_date("2026-08-04T10:00:00").tzinfo is not None
    assert f._parse_post_date(1_754_301_600).tzinfo is not None
    for smiec in (None, "", "wczoraj", {}, []):
        assert f._parse_post_date(smiec) is None


def test_epoch_w_milisekundach_i_sekundach_daje_ten_sam_czas():
    sekundy = f._parse_post_date(1_754_301_600)
    milisekundy = f._parse_post_date(1_754_301_600_000)
    assert sekundy == milisekundy


def test_extract_post_bez_tresci_daje_none():
    """Sam obrazek — bramka i model nie mają czego czytać."""
    assert f._extract_post({"url": "https://fb.com/p/1"}, {"url": "g"}) is None


def test_extract_post_woli_config_od_itemu():
    """URL i nazwę grupy znamy z configu NA PEWNO; item bywa niekompletny."""
    post = f._extract_post(
        {"text": "treść", "groupUrl": "zly", "groupTitle": "Zła"},
        {"url": "https://fb.com/groups/dobra", "name": "Dobra"})
    assert post["group_url"] == "https://fb.com/groups/dobra"
    assert post["group_name"] == "Dobra"


def test_extract_post_szuka_linku_w_wielu_kluczach():
    """post_url jest najważniejszym polem w systemie — bez niego operator nie ma
    jak odpisać, więc szukamy go pod każdą nazwą, jakiej actor kiedykolwiek użył."""
    for klucz in ("url", "postUrl", "link", "facebookUrl", "permalink", "topLevelUrl"):
        post = f._extract_post({"text": "t", klucz: "https://fb.com/p/9"}, {})
        assert post["post_url"] == "https://fb.com/p/9", f"nie znaleziono pod {klucz}"


# ---------------------------------------------------------------------------
# 8. Dedup
# ---------------------------------------------------------------------------
def test_fb_id_to_hash_tresci_o_dlugosci_16():
    identyfikator = f.fb_id("Szukam lawety")
    assert len(identyfikator) == 16
    assert identyfikator == f.fb_id("Szukam lawety")


def test_ta_sama_tresc_z_roznych_grup_to_jedno_zlecenie():
    """Ta sama prośba wklejona na pięć grup ma być JEDNYM zleceniem, a nie
    pięcioma alertami o tej samej awarii."""
    assert f.fb_id("Zepsułem się na S19") == f.fb_id("Zepsułem się na S19")
    assert f.fb_id("Zepsułem się na S19") != f.fb_id("Zepsułem się na A4")


# ---------------------------------------------------------------------------
# 9. Samoleczenie — _apify_run_group_samoleczaca (sekcja 4 zadania)
#
# Błąd sieci/proxy -> ten sam klucz, kolejne proxy (max 3 próby), padnięty
# adres w kwarantannie. Klucz martwy/bez kredytu/rate limit NIE są tu obsłużone
# — to decyzje KeyRotatora, ta funkcja łapie WYŁĄCZNIE błąd transportu.
# ---------------------------------------------------------------------------
class _SiecBlad(Exception):
    """Nazwa niesie 'timeout'/'connect' -> classify_apify_error rozpozna jako
    błąd sieci (patrz workers/apify_keys._is_network_error)."""


class _HTTPBlad(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.response = type("R", (), {"status_code": code})()


class _FakeConnDB:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def test_samoleczenie_happy_path_nie_dotyka_bazy(monkeypatch):
    """Pierwsza próba udana — funkcja NIE otwiera bazy w ogóle (hot path)."""
    wolania_bazy = []
    monkeypatch.setattr(f, "_apify_run_group", lambda *a, **k: [{"ok": True}])
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: wolania_bazy.append(1) or None)
    wynik = f._apify_run_group_samoleczaca("url", 10, 30, "B", "tok", log=lambda *a: None)
    assert wynik == [{"ok": True}]
    assert wolania_bazy == []


def test_samoleczenie_blad_niesieciowy_leci_od_razu_wyzej(monkeypatch):
    """401 (klucz martwy) to sprawa KeyRotatora — bez prób proxy, bez bazy."""
    def _run(*a, **k):
        raise _HTTPBlad(401)

    wolania_bazy = []
    monkeypatch.setattr(f, "_apify_run_group", _run)
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: wolania_bazy.append(1) or None)
    with pytest.raises(_HTTPBlad):
        f._apify_run_group_samoleczaca("url", 10, 30, "B", "tok", log=lambda *a: None)
    assert wolania_bazy == []


def test_samoleczenie_bez_proxy_nie_probuje_ponownie(monkeypatch):
    """Błąd sieci, ale klucz w ogóle nie ma proxy — nie ma czym 'przepiąć'."""
    def _run(*a, **k):
        raise _SiecBlad("connection timed out")

    wolania_bazy = []
    monkeypatch.setattr(f, "_apify_run_group", _run)
    monkeypatch.setattr(f.apify_proxy, "proxy_for_token", lambda token, cfg=None: None)
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: wolania_bazy.append(1) or None)
    with pytest.raises(_SiecBlad):
        f._apify_run_group_samoleczaca("url", 10, 30, "B", "tok", log=lambda *a: None)
    assert wolania_bazy == []


def test_samoleczenie_probuje_kolejnego_proxy_po_awarii_sieci(monkeypatch):
    proby = []

    def _run(url, limit, okno, sciezka, token, proxy=None, cfg=None):
        proby.append(proxy)
        if proxy is None:
            raise _SiecBlad("connection reset")
        return [{"ok": proxy}]

    kwarantanna = []
    monkeypatch.setattr(f, "_apify_run_group", _run)
    monkeypatch.setattr(f.apify_proxy, "proxy_for_token", lambda token, cfg=None: "proxy1")
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: _FakeConnDB())
    monkeypatch.setattr(f.apify_proxy, "oznacz_kwarantanna",
                        lambda conn, url, powod, klucz_hash=None: kwarantanna.append(url))
    monkeypatch.setattr(f.apify_proxy, "proxy_zywy_dla_tokenu",
                        lambda token, conn: "proxy2")

    wynik = f._apify_run_group_samoleczaca("url", 10, 30, "B", "tok", log=lambda *a: None)
    assert wynik == [{"ok": "proxy2"}]
    assert proby == [None, "proxy2"]        # pierwsza próba (lepka), potem proxy2
    assert kwarantanna == ["proxy1"]         # PADNIĘTY adres oznaczony, nie proxy2


def test_samoleczenie_przekazuje_ten_sam_cfg_do_klienta_i_do_padnietego(monkeypatch):
    """`cfg` z run() (wyrównanie po hashu) ma dojść ZARÓWNO do klienta
    (`_apify_run_group`), jak i do liczenia „padniętego" adresu
    (`proxy_for_token`) — inaczej mogłyby wskazać RÓŻNE proxy dla tego samego
    tokenu (surowy rendezvous hashing bez cfg vs wyrównane przypisanie z cfg),
    a wtedy do kwarantanny trafiłby adres, który wcale nie padł."""
    znacznik_cfg = object()
    cfgi_run_group: list = []

    def _run(url, limit, okno, sciezka, token, proxy=None, cfg=None):
        cfgi_run_group.append(cfg)
        if proxy is None:
            raise _SiecBlad("timeout")
        return [{"ok": True}]

    cfgi_proxy_for_token: list = []

    def _proxy_for_token(token, cfg=None):
        cfgi_proxy_for_token.append(cfg)
        return "proxy1"

    monkeypatch.setattr(f, "_apify_run_group", _run)
    monkeypatch.setattr(f.apify_proxy, "proxy_for_token", _proxy_for_token)
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: _FakeConnDB())
    monkeypatch.setattr(f.apify_proxy, "oznacz_kwarantanna", lambda *a, **k: None)
    monkeypatch.setattr(f.apify_proxy, "proxy_zywy_dla_tokenu", lambda token, conn: "proxy2")

    f._apify_run_group_samoleczaca("url", 10, 30, "B", "tok", cfg=znacznik_cfg,
                                   log=lambda *a: None)

    assert cfgi_run_group[0] is znacznik_cfg
    assert cfgi_proxy_for_token[0] is znacznik_cfg


def test_samoleczenie_wyczerpuje_pule_proxy_i_oddaje_ostatni_blad(monkeypatch):
    def _run(url, limit, okno, sciezka, token, proxy=None, cfg=None):
        raise _SiecBlad(f"padło {proxy}")

    licznik = {"n": 1}

    def _kolejny(token, conn):
        licznik["n"] += 1
        return f"proxy{licznik['n']}"        # zawsze INNY adres — bez tego pętla kończy się wcześniej

    monkeypatch.setattr(f, "_apify_run_group", _run)
    monkeypatch.setattr(f.apify_proxy, "proxy_for_token", lambda token, cfg=None: "proxy1")
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: _FakeConnDB())
    monkeypatch.setattr(f.apify_proxy, "oznacz_kwarantanna", lambda *a, **k: None)
    monkeypatch.setattr(f.apify_proxy, "proxy_zywy_dla_tokenu", _kolejny)

    with pytest.raises(_SiecBlad):
        f._apify_run_group_samoleczaca("url", 10, 30, "B", "tok", log=lambda *a: None)
    assert licznik["n"] == 1 + (f._PROXY_PROBY_SAMOLECZENIA - 1)   # dokładnie tyle prób, ile deklaruje stała


def test_samoleczenie_cala_ranga_w_kwarantannie_konczy_od_razu(monkeypatch):
    """`proxy_zywy_dla_tokenu` oddaje None, gdy wszystko jest w kwarantannie —
    nie ma sensu kręcić się dalej."""
    monkeypatch.setattr(f, "_apify_run_group",
                        lambda *a, **k: (_ for _ in ()).throw(_SiecBlad("timeout")))
    monkeypatch.setattr(f.apify_proxy, "proxy_for_token", lambda token, cfg=None: "proxy1")
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: _FakeConnDB())
    monkeypatch.setattr(f.apify_proxy, "oznacz_kwarantanna", lambda *a, **k: None)
    monkeypatch.setattr(f.apify_proxy, "proxy_zywy_dla_tokenu", lambda token, conn: None)

    with pytest.raises(_SiecBlad):
        f._apify_run_group_samoleczaca("url", 10, 30, "B", "tok", log=lambda *a: None)


def test_samoleczenie_baza_pada_w_trakcie_ponowien_oddaje_oryginalny_blad(monkeypatch):
    monkeypatch.setattr(f, "_apify_run_group",
                        lambda *a, **k: (_ for _ in ()).throw(_SiecBlad("timeout")))
    monkeypatch.setattr(f.apify_proxy, "proxy_for_token", lambda token, cfg=None: "proxy1")
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: None)   # baza akurat padła
    with pytest.raises(_SiecBlad):
        f._apify_run_group_samoleczaca("url", 10, 30, "B", "tok", log=lambda *a: None)


# ---------------------------------------------------------------------------
# 10. Alert przy wyczerpaniu całej puli (AllKeysExhausted)
# ---------------------------------------------------------------------------
def test_alert_pula_wyczerpana_wysyla_telegram_z_liczbami(monkeypatch):
    from laweta_radar.services import telegram_notify

    wyslane = []
    monkeypatch.setattr(telegram_notify, "wyslij", lambda tekst: wyslane.append(tekst) or 1)
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: _FakeConnDB())
    monkeypatch.setattr(f.apify_keys, "klucze_zywe", lambda conn, tokeny: tokeny[:1])
    monkeypatch.setattr(f.apify_proxy, "load_proxy_config",
                        lambda: type("Cfg", (), {"pool": ()})())

    logi = []
    f._alert_pula_wyczerpana(["t1", "t2", "t3"], log=logi.append)

    assert len(wyslane) == 1
    assert "3" in wyslane[0] and "1/3" in wyslane[0]
    assert any("KRYTYCZNE" in linia for linia in logi)


def test_alert_pula_wyczerpana_dziala_bez_bazy(monkeypatch):
    """Diagnostyka to usprawnienie — alert MA pójść, nawet niepełny."""
    from laweta_radar.services import telegram_notify

    wyslane = []
    monkeypatch.setattr(telegram_notify, "wyslij", lambda tekst: wyslane.append(tekst) or 1)
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: None)

    f._alert_pula_wyczerpana(["t1", "t2"], log=lambda *a: None)
    assert len(wyslane) == 1
    assert "brak bazy" in wyslane[0]


def test_alert_pula_wyczerpana_nie_wywala_gdy_telegram_pada(monkeypatch):
    from laweta_radar.services import telegram_notify

    def _wybuchnij(tekst):
        raise RuntimeError("sieć padła")

    monkeypatch.setattr(telegram_notify, "wyslij", _wybuchnij)
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: None)
    f._alert_pula_wyczerpana(["t1"], log=lambda *a: None)   # nie ma rzucić — test przechodzi


# ---------------------------------------------------------------------------
# 10b. Alert przy wyczerpaniu ŻYWYCH proxy (sekcja 3 zadania „większa pula proxy")
# ---------------------------------------------------------------------------
def test_alert_pula_proxy_wyczerpana_wysyla_telegram(monkeypatch):
    from laweta_radar.services import telegram_notify

    wyslane = []
    monkeypatch.setattr(telegram_notify, "wyslij", lambda tekst: wyslane.append(tekst) or 1)
    cfg = f.apify_proxy.load_proxy_config(
        {"APIFY_PROXY_URLS": "http://u:p@a.example:8000,http://u:p@b.example:8000",
         "APIFY_PROXY_REQUIRED": "1"})

    logi = []
    f._alert_pula_proxy_wyczerpana(cfg, log=logi.append)

    assert len(wyslane) == 1
    assert "2" in wyslane[0]                      # liczba adresów w puli
    assert any("KRYTYCZNE" in linia for linia in logi)


def test_alert_pula_proxy_wyczerpana_nie_wywala_gdy_telegram_pada(monkeypatch):
    from laweta_radar.services import telegram_notify

    monkeypatch.setattr(telegram_notify, "wyslij",
                        lambda tekst: (_ for _ in ()).throw(RuntimeError("sieć padła")))
    cfg = f.apify_proxy.load_proxy_config({"APIFY_PROXY_URLS": "http://u:p@a.example:8000"})
    f._alert_pula_proxy_wyczerpana(cfg, log=lambda *a: None)   # nie ma rzucić


# ---------------------------------------------------------------------------
# 11. Wczesne ostrzeżenie o degradacji puli (_alert_jesli_zdegradowana)
# ---------------------------------------------------------------------------
def test_degradacja_bez_bazy_nic_nie_robi(monkeypatch):
    from laweta_radar.services import telegram_notify

    wyslane = []
    monkeypatch.setattr(telegram_notify, "wyslij", lambda tekst: wyslane.append(tekst) or 1)
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: None)
    f._alert_jesli_zdegradowana(["t1", "t2", "t3"], ["t1", "t2", "t3"], log=lambda *a: None)
    assert wyslane == []


def test_degradacja_cisza_gdy_pula_zdrowa(monkeypatch):
    from laweta_radar.services import telegram_notify

    wyslane = []
    monkeypatch.setattr(telegram_notify, "wyslij", lambda tekst: wyslane.append(tekst) or 1)
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: _FakeConnDB())
    monkeypatch.setattr(f.apify_keys, "wczytaj_stany", lambda conn, tokeny: {})
    monkeypatch.setattr(f.apify_proxy, "load_proxy_config",
                        lambda: type("Cfg", (), {"pool": ()})())
    f._alert_jesli_zdegradowana(["t1", "t2", "t3"], ["t1", "t2", "t3"], log=lambda *a: None)
    assert wyslane == []


def test_degradacja_alertuje_gdy_mniej_niz_dwa_zywe_klucze(monkeypatch):
    from laweta_radar.services import telegram_notify

    wyslane = []
    monkeypatch.setattr(telegram_notify, "wyslij", lambda tekst: wyslane.append(tekst) or 1)
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: _FakeConnDB())
    monkeypatch.setattr(f.apify_keys, "wczytaj_stany", lambda conn, tokeny: {})
    monkeypatch.setattr(f.apify_proxy, "load_proxy_config",
                        lambda: type("Cfg", (), {"pool": ()})())
    logi = []
    f._alert_jesli_zdegradowana(["t1"], ["t1", "t2", "t3"], log=logi.append)
    assert len(wyslane) == 1
    assert "żywych kluczy" in wyslane[0]
    assert any("UWAGA" in linia for linia in logi)


def test_degradacja_alertuje_gdy_martwy_klucz(monkeypatch):
    from laweta_radar.services import telegram_notify

    wyslane = []
    monkeypatch.setattr(telegram_notify, "wyslij", lambda tekst: wyslane.append(tekst) or 1)
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: _FakeConnDB())
    monkeypatch.setattr(
        f.apify_keys, "wczytaj_stany",
        lambda conn, tokeny: {"t3": {"status": f.apify_keys.STATUS_KLUCZ_MARTWY}})
    monkeypatch.setattr(f.apify_proxy, "load_proxy_config",
                        lambda: type("Cfg", (), {"pool": ()})())
    # trzy żywe (>= próg), więc TYLKO klucz martwy ma wywołać alert
    f._alert_jesli_zdegradowana(["t1", "t2", "t3"], ["t1", "t2", "t3"], log=lambda *a: None)
    assert len(wyslane) == 1
    assert "martwych" in wyslane[0]


def test_degradacja_alertuje_gdy_mniej_proxy_niz_kluczy(monkeypatch):
    from laweta_radar.services import telegram_notify

    wyslane = []
    monkeypatch.setattr(telegram_notify, "wyslij", lambda tekst: wyslane.append(tekst) or 1)
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: _FakeConnDB())
    monkeypatch.setattr(f.apify_keys, "wczytaj_stany", lambda conn, tokeny: {})
    monkeypatch.setattr(f.apify_proxy, "load_proxy_config",
                        lambda: type("Cfg", (), {"pool": ("p1", "p2", "p3")})())
    monkeypatch.setattr(f.apify_proxy, "wczytaj_stan_proxy",
                        lambda conn, urls: {"p1": {"status": "kwarantanna"},
                                            "p2": {"status": "kwarantanna"}})
    # 3 proxy w puli, 2 w kwarantannie -> 1 żywe, mniej niż 3 klucze
    f._alert_jesli_zdegradowana(["t1", "t2", "t3"], ["t1", "t2", "t3"], log=lambda *a: None)
    assert len(wyslane) == 1
    assert "żywych proxy" in wyslane[0]


def test_degradacja_diagnostyka_pada_nie_wywala_runu(monkeypatch):
    from laweta_radar.services import telegram_notify

    wyslane = []
    monkeypatch.setattr(telegram_notify, "wyslij", lambda tekst: wyslane.append(tekst) or 1)
    monkeypatch.setattr(f, "_polacz_best_effort", lambda: _FakeConnDB())

    def _wybuchnij(conn, tokeny):
        raise RuntimeError("baza padła w środku zapytania")

    monkeypatch.setattr(f.apify_keys, "wczytaj_stany", _wybuchnij)
    f._alert_jesli_zdegradowana(["t1"], ["t1", "t2"], log=lambda *a: None)   # nie ma rzucić
    assert wyslane == []
