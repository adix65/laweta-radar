"""Ekstrakcja na zbiorze referencyjnym — dana, która STOI W TREŚCI, ma dojechać
do swojego pola.

SKĄD SIĘ WZIĄŁ TEN PLIK. Produkcja (gpt-5.4-nano, OPENAI_JSON_MODE=object)
pokazała wzorzec: klasyfikator poprawnie wykrywał zlecenia, ale wypełniał pole
tylko wtedy, gdy dana była podana wprost i prosto. Wystarczyła minimalna
interpretacja — nazwa miejscowości zagranicznej („Zulte"), kod pocztowy wśród
innych liczb („z kodu 54-100"), marka w środku zdania („transportu dla Renault
Trafic") — i pole zostawało puste. W bazie wygląda to identycznie jak „w poście
tego nie było", a jest czymś zupełnie innym: niedoczytaniem.

CO TU JEST TESTOWANE, A CO NIE. Jakości MODELU nadal nie mierzymy testem
jednostkowym — od tego jest `scripts/porownaj_modele.py` na tym samym zbiorze.
Testujemy dwie rzeczy, które są NASZE i działają bez sieci:

  1. FALLBACK REGEXOWY (`classifier.uzupelnij_kody`) — czy kod pocztowy stojący
     w treści dojeżdża do pola nawet wtedy, gdy model oddał komplet nulli.
     Model jest tu podstawiony PUSTĄ odpowiedzią, czyli dokładnie tym, co
     realnie robił na produkcji.
  2. WARSTWA WALIDACJI — czy to, co model oddał poprawnie, przeżywa drogę do
     wyniku. Obca nazwa („Zulte", „Eindhoven"), kod w obcym formacie („5678 AB",
     „702 00") i marka pojazdu mają wyjść po drugiej stronie nietknięte.
     Tutaj model jest podstawiony ETYKIETAMI ze zbioru.

ZBIÓR JEST JEDEN (`dane/posty_referencyjne.jsonl`) i celowo ten sam, którego
używa porównywarka modeli. Drugi zbiór „do testów" rozjechałby się z pierwszym
przy pierwszym dopisanym poście, a wtedy testy pilnowałyby czegoś innego niż to,
na czym wybieramy model.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.scripts import porownaj_modele as pm  # noqa: E402
from laweta_radar.services import geo, llm  # noqa: E402
from laweta_radar.workers import classifier as c  # noqa: E402

POSTY, _OSTRZEZENIA = pm.wczytaj(pm.ZBIOR_DOMYSLNY)

# Pola dopisane do zbioru po diagnozie z produkcji. Porównywarka modeli ich nie
# punktuje (ocenia cztery inne) — czyta je wyłącznie ten plik.
POLA_EKSTRAKCJI = ("odbior_kod", "dostawa_kod", "pojazd_opis")

# Posty z RĘCZNIE wpisaną prawdą o ekstrakcji. To jest zbiór, o który chodziło
# w zgłoszeniu: realne treści z tabeli `posty` plus warianty tych samych
# wzorców, na których model się wykładał.
Z_ETYKIETAMI = [p for p in POSTY if any(k in p["oczekiwane"] for k in POLA_EKSTRAKCJI)]

# Posty, w których kod pocztowy realnie stoi — liczone REGEXEM, a nie z etykiet.
# Dzięki temu asercja obejmuje też posty dopisane później, których nikt nie
# opisał ręcznie (np. ref-018, starszy od tego pliku).
Z_KODEM_W_TRESCI = [p for p in POSTY if geo.znajdz_kody(p["tresc"])]


# ---------------------------------------------------------------------------
# PODSTAWIONY MODEL
#
# Dwie skrajności, obie wzięte z produkcji: model, który oddał sam szkielet
# z nullami, i model, który przeczytał post poprawnie. Pierwsza pokazuje, ile
# ratuje fallback; druga — czy nasza walidacja czegoś nie zjada po drodze.
# ---------------------------------------------------------------------------
def _odpowiedz(**pola) -> str:
    baza = {
        "czy_zlecenie": True,
        "typ": "transport",
        "odbior": {"raw": None, "kod": None, "miasto": None},
        "dostawa": {"raw": None, "kod": None, "miasto": None},
        "pojazd": {"opis": None, "kategoria": "inne"},
        "stan": {"toczy_sie": True, "ma_kola": True, "po_wypadku": False, "uwagi": None},
        "pilnosc": "elastycznie",
        "kontakt": {"typ": "brak", "wartosc": None},
        "cena_sugerowana": None,
        "pewnosc": 70,
        "powod": "test ekstrakcji",
    }
    baza.update(pola)
    return json.dumps(baza, ensure_ascii=False)


def _model_zgubil_wszystko() -> str:
    """Odpowiedź modelu, który wykrył zlecenie i nie wyciągnął z niego NIC."""
    return _odpowiedz()


def _model_przeczytal_poprawnie(oczekiwane: dict) -> str:
    """Odpowiedź modelu zbudowana z ręcznych etykiet tego posta."""
    return _odpowiedz(
        czy_zlecenie=oczekiwane.get("czy_zlecenie", True),
        odbior={"raw": None, "kod": oczekiwane.get("odbior_kod"),
                "miasto": oczekiwane.get("odbior_miasto")},
        dostawa={"raw": None, "kod": oczekiwane.get("dostawa_kod"),
                 "miasto": oczekiwane.get("dostawa_miasto")},
        pojazd={"opis": oczekiwane.get("pojazd_opis"), "kategoria": "inne"},
        pilnosc=oczekiwane.get("pilnosc", "elastycznie"),
    )


def _klasyfikuj(tresc: str, surowa: str) -> dict:
    """Cała ścieżka produkcyjna z podstawioną odpowiedzią modelu.

    Wołamy `klasyfikuj`, a nie `zwaliduj` czy `uzupelnij_kody` z osobna — bo
    testujemy między innymi to, CZY fallback jest w tej ścieżce w ogóle wpięty.
    """
    zapamietane = llm.zapytaj
    llm.zapytaj = lambda system, user, max_tokens: surowa
    try:
        return c.klasyfikuj(tresc)
    finally:
        llm.zapytaj = zapamietane


def _kody_z_tresci(tresc: str) -> list[str]:
    return [kod for kod, _kraj in geo.znajdz_kody(tresc)]


# ---------------------------------------------------------------------------
# ZBIÓR
# ---------------------------------------------------------------------------
def test_zbior_wczytuje_sie_bez_ostrzezen():
    assert POSTY, "zbiór referencyjny zniknął albo przestał się wczytywać"
    assert not _OSTRZEZENIA, f"zepsute wiersze w zbiorze: {_OSTRZEZENIA}"


def test_zbior_ma_co_najmniej_dziesiec_postow_z_etykietami_ekstrakcji():
    """Mniej niż dziesięć to anegdota, nie pomiar — jeden post w tę czy w tamtą
    przestawia wynik o dziesięć punktów procentowych."""
    assert len(Z_ETYKIETAMI) >= 10, (
        f"tylko {len(Z_ETYKIETAMI)} postów z etykietami ekstrakcji "
        f"(pola {POLA_EKSTRAKCJI})")


def test_kazdy_kod_z_etykiet_jest_znajdowany_regexem():
    """Etykieta mówi „ten kod stoi w treści" — `geo.znajdz_kody` ma go widzieć.

    To jest test ŹRÓDŁA fallbacku. Gdyby regex nie znał formatu z realnego
    posta, cała warstwa ratunkowa byłaby pusta i nikt by tego nie zauważył:
    wynik wyglądałby jak model, który po prostu nie znalazł kodu.
    """
    braki = []
    for post in Z_ETYKIETAMI:
        znalezione = _kody_z_tresci(post["tresc"])
        for pole in ("odbior_kod", "dostawa_kod"):
            oczekiwany = post["oczekiwane"].get(pole)
            if oczekiwany and oczekiwany not in znalezione:
                braki.append(f"{post['id']}: {pole}={oczekiwany!r}, regex widzi {znalezione}")
    assert not braki, "regex nie widzi kodów, które stoją w treści: " + "; ".join(braki)


# ---------------------------------------------------------------------------
# FALLBACK — to, co model zgubił
# ---------------------------------------------------------------------------
def test_kazdy_post_z_kodem_w_tresci_ma_niepuste_pole_kodu():
    """ASERCJA ZE ZGŁOSZENIA: jest kod w treści -> jest kod w wyniku.

    Model jest podstawiony odpowiedzią, w której NIE MA NIC — czyli tym, co
    realnie oddawał na produkcji przy kodzie stojącym wśród innych liczb.
    Wszystko, co widać po prawej stronie, dokłada więc sam fallback.
    """
    assert Z_KODEM_W_TRESCI, "w zbiorze nie ma ani jednego posta z kodem pocztowym"
    puste = []
    for post in Z_KODEM_W_TRESCI:
        wynik = _klasyfikuj(post["tresc"], _model_zgubil_wszystko())
        if wynik["odbior"]["kod"] is None and wynik["dostawa"]["kod"] is None:
            puste.append(f"{post['id']}: {_kody_z_tresci(post['tresc'])} w treści, w wyniku null")
    assert not puste, "fallback nie uzupełnił kodu: " + "; ".join(puste)


def test_fallback_bierze_kody_z_tresci_i_w_kolejnosci_wystapienia():
    """Pierwszy kod z treści to odbiór, drugi to dostawa — i żadnej wymyślonej.

    Kierunku nie da się wyczytać z kształtu cyfr, więc kolejność jest tu jedyną
    heurystyką. Przy poście, w którym kod stoi wyłącznie przy dostawie, wskaże
    ona pole odbioru — patrz `test_fallback_nie_zna_kierunku`.
    """
    for post in Z_KODEM_W_TRESCI:
        w_tresci = _kody_z_tresci(post["tresc"])
        wynik = _klasyfikuj(post["tresc"], _model_zgubil_wszystko())
        wypelnione = [wynik[m]["kod"] for m in ("odbior", "dostawa") if wynik[m]["kod"]]
        assert wypelnione == w_tresci[:2], (post["id"], wypelnione, w_tresci)


def test_fallback_nie_zna_kierunku_i_zaczyna_od_odbioru():
    """Ograniczenie przypięte testem, żeby nikt nie odkrywał go na produkcji.

    Post ma jeden kod i stoi on przy DOSTAWIE. Fallback wpisze go w odbiór, bo
    czyta kształt cyfr, a nie zdanie. Świadomie: kod z posta pod ręką operatora
    jest wart więcej niż dwa puste pola, a treść posta zostaje obok na ekranie.
    Kierunek jest robotą promptu — i to on ma tu zadziałać jako pierwszy.
    """
    tresc = "Odbior spod Biedronki w Krosnie, zawiezc do 35-001 Rzeszow. Golf nie odpala"
    wynik = _klasyfikuj(tresc, _model_zgubil_wszystko())
    assert wynik["odbior"]["kod"] == "35-001"
    assert wynik["dostawa"]["kod"] is None


def test_model_ma_pierwszenstwo_przed_fallbackiem():
    """Fallback wypełnia WYŁĄCZNIE puste pola i nigdy nie nadpisuje modelu.

    Model czyta zdanie, regex czyta kształt cyfr — przy konflikcie rację ma
    model. Nadpisywanie zamieniłoby warstwę ratunkową w warstwę psującą.
    """
    tresc = "Transport z 38-400 Krosno do 35-001 Rzeszow"
    wynik = _klasyfikuj(tresc, _odpowiedz(
        odbior={"raw": None, "kod": "35-001", "miasto": "Rzeszow"},
        dostawa={"raw": None, "kod": None, "miasto": None}))
    assert wynik["odbior"]["kod"] == "35-001", "fallback nadpisał wartość od modelu"
    # W puli został tylko drugi kod — pierwszy model już zużył, choć w innym polu.
    assert wynik["dostawa"]["kod"] == "38-400"


def test_ten_sam_kod_zapisany_inaczej_nie_lezy_drugi_raz():
    """"38400" od modelu i "38-400" z treści to JEDEN adres, nie dwa.

    Bez normalizacji przy porównaniu ten sam punkt trafiłby do odbioru i do
    dostawy, a operator zobaczyłby kurs "z Krosna do Krosna" — wynik gorszy niż
    puste pole, bo wygląda na dane.
    """
    wynik = _klasyfikuj(
        "Laweta z 38-400 Krosno, auto nie odpala",
        _odpowiedz(odbior={"raw": None, "kod": "38400", "miasto": "Krosno"}))
    assert wynik["odbior"]["kod"] == "38400"
    assert wynik["dostawa"]["kod"] is None


def test_fallback_nie_wymysla_kodu_z_ceny_rocznika_i_przebiegu():
    """Kontrola negatywna — najważniejsza w całym pliku.

    Warstwa, która dokłada dane, jest niebezpieczna dokładnie tym, że dokłada.
    Rocznik, cena i przebieg mają kształt kodu pocztowego, a zła współrzędna
    wysyła człowieka 80 km w złą stronę — czyli kosztuje więcej niż puste pole.
    """
    bez_kodu = [p for p in POSTY if not geo.znajdz_kody(p["tresc"])]
    assert bez_kodu, "kontrola negatywna nie ma na czym działać"
    for post in bez_kodu:
        wynik = _klasyfikuj(post["tresc"], _model_zgubil_wszystko())
        assert wynik["odbior"]["kod"] is None, (post["id"], wynik["odbior"]["kod"])
        assert wynik["dostawa"]["kod"] is None, (post["id"], wynik["dostawa"]["kod"])

    # I jeszcze raz wprost, na treści z kompletem pułapek naraz.
    wynik = _klasyfikuj(
        "Sprzedam golfa 2015, cena 12000 zl, przebieg 180000 km, tel 502 33 44 55",
        _model_zgubil_wszystko())
    assert wynik["odbior"]["kod"] is None
    assert wynik["dostawa"]["kod"] is None


def test_uzupelnienie_zostawia_slad_w_logu(capsys):
    """Bez tego śladu nie da się policzyć, jak często model gubi to, co regex
    znajduje za darmo — czyli nie da się ocenić kolejnej zmiany promptu.

    Znacznik jest stały i jednosłowny właśnie po to, żeby dało się go policzyć
    z `pm2 logs` jednym grepem.
    """
    _klasyfikuj("Laweta z 38-400 Krosno do 35-001 Rzeszow", _model_zgubil_wszystko())
    log = capsys.readouterr().err
    assert log.count(c.ZNACZNIK_FALLBACK_KOD) == 2, log
    assert "odbior_kod='38-400'" in log
    assert "dostawa_kod='35-001'" in log


def test_bez_fallbacku_pola_zostaja_puste():
    """Kontrapunkt do testów wyżej: to naprawdę fallback je wypełnia.

    Test, który nie umie pokazać stanu SPRZED poprawki, przechodzi także
    wtedy, gdy poprawki nie ma.
    """
    wynik = c.rozbierz(_model_zgubil_wszystko())
    assert wynik["odbior"]["kod"] is None
    assert wynik["dostawa"]["kod"] is None


# ---------------------------------------------------------------------------
# WALIDACJA — to, co model oddał poprawnie, ma przeżyć drogę do wyniku
# ---------------------------------------------------------------------------
def test_dane_z_etykiet_dojezdzaja_do_wyniku_nietkniete():
    """Obca nazwa, obcy format kodu i marka pojazdu mają wyjść bez zmian.

    Walidacja pól jest wyrozumiała z rozmysłem (pojedyncze pole spoza zbioru
    nie może skasować posta), ale ma też prawo coś WYRZUCIĆ — kod w formacie,
    którego `geo` nie zna, leci do nulla z samym logiem. Ten test pilnuje, że
    żadna z realnych wartości ze zbioru w ten sposób nie ginie.
    """
    zgubione = []
    for post in Z_ETYKIETAMI:
        ocz = post["oczekiwane"]
        wynik = _klasyfikuj(post["tresc"], _model_przeczytal_poprawnie(ocz))
        pary = [
            ("odbior_kod", wynik["odbior"]["kod"]),
            ("dostawa_kod", wynik["dostawa"]["kod"]),
            ("odbior_miasto", wynik["odbior"]["miasto"]),
            ("dostawa_miasto", wynik["dostawa"]["miasto"]),
            ("pojazd_opis", wynik["pojazd"]["opis"]),
        ]
        for pole, wartosc in pary:
            if ocz.get(pole) and wartosc != ocz[pole]:
                zgubione.append(f"{post['id']}: {pole} {ocz[pole]!r} -> {wartosc!r}")
    assert not zgubione, "walidacja zjadła dane z posta: " + "; ".join(zgubione)


def test_kazde_zlecenie_z_miastem_lub_marka_ma_niepuste_pole():
    """ASERCJA ZE ZGŁOSZENIA, druga połowa: jest nazwa w treści -> jest w polu.

    Miasta i marki nie ratuje żaden regex — ich wyciągnięcie wymaga
    przeczytania zdania, więc odpowiada za nie prompt (sekcja PRZYKŁADY
    w `workers/classifier.py`), a mierzy `scripts/porownaj_modele.py` na tym
    samym zbiorze. Tutaj sprawdzamy jedyną część, która jest nasza i działa bez
    sieci: że poprawnie przeczytany post nie traci tych pól po drodze.
    """
    for post in Z_ETYKIETAMI:
        ocz = post["oczekiwane"]
        if not ocz.get("czy_zlecenie"):
            continue  # nie-zlecenie nie ma pól ekstrakcji i mieć nie powinno
        wynik = _klasyfikuj(post["tresc"], _model_przeczytal_poprawnie(ocz))
        if ocz.get("odbior_miasto"):
            assert wynik["odbior"]["miasto"], post["id"]
        if ocz.get("dostawa_miasto"):
            assert wynik["dostawa"]["miasto"], post["id"]
        if ocz.get("pojazd_opis"):
            assert wynik["pojazd"]["opis"], post["id"]


def test_etykiety_ekstrakcji_opisuja_to_co_stoi_w_tresci():
    """Etykieta wpisana „z głowy" psuje pomiar ciszej, niż go poprawia.

    Kod ma stać w treści dosłownie, marka — po odrzuceniu wielkości liter
    i ogonków. Ten test pilnuje ZBIORU, nie kodu: jest pierwszą rzeczą, która
    zapala się przy dopisaniu postu z odpowiedzią zgadniętą zamiast przeczytanej.
    """
    bledy = []
    for post in Z_ETYKIETAMI:
        tresc = geo.normalizuj_nazwe(post["tresc"])
        for pole in ("odbior_kod", "dostawa_kod"):
            kod = post["oczekiwane"].get(pole)
            if kod and kod.lower() not in post["tresc"].lower():
                bledy.append(f"{post['id']}: {pole}={kod!r} nie stoi w treści")
        opis = post["oczekiwane"].get("pojazd_opis")
        if opis and not all(czlon in tresc for czlon in geo.normalizuj_nazwe(opis).split()):
            bledy.append(f"{post['id']}: pojazd_opis={opis!r} nie stoi w treści")
    assert not bledy, "; ".join(bledy)
