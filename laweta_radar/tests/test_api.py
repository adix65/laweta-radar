"""Testy API — bez bazy i bez sieci.

Baza jest podmieniana atrapą (`_FalszywePolaczenie`), bo sprawdzamy tu rzeczy,
które NIE zależą od Postgresa i psują się niezależnie od niego:

  • autoryzację — czyli to, czy numery telefonów obcych ludzi z grup FB nie
    wyjdą pod gołym URL-em;
  • składanie rekordu z płaskich kolumn klasyfikatora razem z policzoną
    geografią (kilometry, linki, pewność lokalizacji);
  • to, że `max_km` jest FILTREM OPERATORA, a nie progiem systemu.

Ostatni punkt jest tu najważniejszy: to jedyne miejsce, w którym da się zapisać
testem zasadę naczelną repo („system pokazuje, decyduje kierowca"). Kod, który
ją łamie, wygląda jak zwykłe `WHERE km <= 80`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from laweta_radar.api import db
from laweta_radar.api.main import app
from laweta_radar.api.routers import zlecenia as router_zlecen
from laweta_radar.config import settings
from laweta_radar.services import geo

TOKEN = "token-testowy"
TERAZ = datetime.now(timezone.utc)

# Mikro-baza geo — patrz nota w `test_geo.py`: `data/kody_eu.csv` jest zalążkiem
# i zostanie podmieniony pełnym eksportem, więc test na jego zawartości zacząłby
# padać z powodu niezwiązanego z kodem.
FIXTURE_GEO = """kraj,kod,miejscowosc,wojewodztwo,lat,lng
PL,38-400,Krosno,podkarpackie,49.6886,21.7706
PL,35-001,Rzeszow,podkarpackie,50.0412,21.9991
DE,50667,Koln,Nordrhein-Westfalen,50.9375,6.9603
PL,30-001,Krakow,malopolskie,50.0647,19.9450
"""


def _wiersz(fb_id, **nadpisz):
    """Wiersz `posty` z kompletem kolumn czytanych przez router.

    Komplet, a nie podzbiór: `RealDictCursor` w produkcji zwraca wszystkie
    kolumny z SELECT-a, a atrapa, która pomija połowę, przepuściłaby błąd
    „router czyta pole, którego nie ma w zapytaniu".
    """
    baza = {k: None for k in router_zlecen.POLA_LISTY}
    baza.update({
        "fb_id": fb_id, "grupa_nazwa": "Podkarpacie", "grupa_url": "u1",
        "post_url": f"https://fb/{fb_id}", "opublikowany_at": TERAZ,
        "pobrany_at": TERAZ, "status": "nowe", "stale": False,
        "gate_jezyk": "pl", "pewnosc": 90, "pilnosc": "teraz",
    })
    baza.update(nadpisz)
    return baza


WIERSZE = [
    _wiersz("blisko", odbior_miasto="Krosno", odbior_kod="38-400",
            dostawa_miasto="Rzeszow", pojazd_opis="VW Golf IV",
            stan_toczy_sie=True, kontakt_wartosc="555111222",
            opublikowany_at=TERAZ - timedelta(minutes=5)),
    _wiersz("daleko", grupa_nazwa="Transport DE", grupa_url="u2", gate_jezyk="de",
            odbior_miasto="Koln", dostawa_miasto="Krakow", pojazd_opis="BMW",
            pilnosc="elastycznie", pewnosc=70,
            opublikowany_at=TERAZ - timedelta(hours=2)),
    _wiersz("nieznane", odbior_raw="gdzies za lasem",
            odbior_miasto="Zmyslone Miasto", pojazd_opis="Ford",
            pilnosc="dzis", pewnosc=55,
            opublikowany_at=TERAZ - timedelta(minutes=9)),
]


class _Kursor:
    def __init__(self, wiersze):
        self._wiersze = wiersze
        self.zapytania: list[tuple] = []

    def execute(self, sql, parametry=None):
        self.zapytania.append((sql, parametry))

    def fetchall(self):
        return self._wiersze

    def fetchone(self):
        return self._wiersze[0] if self._wiersze else None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _FalszywePolaczenie:
    def __init__(self, wiersze):
        self.kursor = _Kursor(wiersze)
        self.commity = 0

    def cursor(self):
        return self.kursor

    def commit(self):
        self.commity += 1


KOMPLET_KOLUMN = {"odbior_miasto", "pojazd_opis", "pewnosc",
                  "notatka", "cena_koncowa", "status_at",
                  "kategoria_ladunku"}


@pytest.fixture(autouse=True)
def srodowisko(monkeypatch, tmp_path):
    plik = tmp_path / "kody.csv"
    plik.write_text(FIXTURE_GEO, encoding="utf-8")
    geo.zaladuj(plik)
    monkeypatch.setattr(settings, "API_TOKEN", TOKEN)
    monkeypatch.setattr(geo.settings, "BAZA_LAT", 49.65)
    monkeypatch.setattr(geo.settings, "BAZA_LON", 21.60)
    monkeypatch.setattr(geo.settings, "BAZA_LNG", 21.60)
    # Migracje „są odpalone" — sprawdzenie kolumn ma własny test niżej.
    monkeypatch.setattr(db, "kolumny", lambda conn, tabela: KOMPLET_KOLUMN)
    monkeypatch.setattr(router_zlecen.db, "kolumny",
                        lambda conn, tabela: KOMPLET_KOLUMN)
    yield
    geo.zaladuj()


def _podepnij_baze(monkeypatch, wiersze):
    from contextlib import contextmanager

    polaczenie = _FalszywePolaczenie(wiersze)

    @contextmanager
    def atrapa():
        yield polaczenie

    monkeypatch.setattr(router_zlecen.db, "polaczenie", atrapa)
    return polaczenie


klient = TestClient(app)


# ---------------------------------------------------------------------------
# Autoryzacja
# ---------------------------------------------------------------------------
def test_bez_tokenu_401():
    assert klient.get("/zlecenia").status_code == 401


def test_zly_token_401():
    assert klient.get("/zlecenia", headers={"X-Token": "cudzy"}).status_code == 401


def test_brak_api_token_w_konfiguracji_daje_503(monkeypatch):
    """Puste `API_TOKEN` znaczy „konfiguracji nie dokończono", a NIE
    „wpuszczaj każdego". To jedyna interpretacja, przy której literówka w .env
    nie kończy się otwartą bazą numerów telefonów."""
    monkeypatch.setattr(settings, "API_TOKEN", "")
    odp = klient.get("/zlecenia", headers={"X-Token": "cokolwiek"})
    assert odp.status_code == 503
    assert "API_TOKEN" in odp.json()["detail"]


def test_zdrowie_i_health_bez_tokenu():
    """Endpointy diagnostyczne są potrzebne DOKŁADNIE wtedy, gdy konfiguracja
    jest zepsuta — 503 przez zły `API_TOKEN` czyniłoby je bezużytecznymi."""
    assert klient.get("/health").status_code == 200
    assert klient.get("/zdrowie").status_code == 200


# ---------------------------------------------------------------------------
# Niepełna konfiguracja to NIE jest awaria API
#
# Sedno regresji: brak klucza modelu kładł API, PM2 podnosiło je z powrotem
# i proces stawał w pętli restartów ze statusem `errored`. W logach wyglądało
# to na awarię kodu, a było brakiem jednej linijki w .env — czyli objaw kierował
# w najgorsze możliwe miejsce. Klasyfikator jest jednym z kilku podsystemów:
# bez niego fetcher dalej zbiera do bazy, bramka dalej punktuje, panel dalej
# pokazuje zebrane, a Telegram dalej dowozi.
# ---------------------------------------------------------------------------
def test_api_wstaje_bez_klucza_modelu_i_raportuje_braki(monkeypatch):
    """KRYTERIUM ODBIORU: pusty klucz modelu = API wstaje i mówi, czego brakuje."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    # Baza JEST — inaczej test nie odróżniłby braku klucza od braku bazy, czyli
    # dokładnie tego rozróżnienia, którego pilnuje.
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://x@localhost/laweta")

    # `with` odpala `lifespan`, czyli tę samą ścieżkę startową, którą wykonuje
    # uvicorn pod PM2 — test bez niego nie sprawdziłby tego, o co tu chodzi.
    with TestClient(app) as swiezy:
        odp = swiezy.get("/health")

    assert odp.status_code == 200
    dane = odp.json()
    assert dane["status"] == "niepelna_konfiguracja"
    assert "ANTHROPIC_API_KEY" in dane["braki"]["degradujace"]
    assert dane["braki"]["blokujace_start"] == []
    # Skutek, nie sama nazwa: „czy mogę z tym żyć do jutra" to jedyne pytanie,
    # które ktoś zadaje temu endpointowi o trzeciej w nocy.
    assert dane["braki"]["skutki"]["ANTHROPIC_API_KEY"]


def test_health_oddziela_brak_blokujacy_od_degradujacego(monkeypatch):
    """Płaska lista stawiała brak bazy obok nieużywanego klucza providera.

    Z takiej listy nie da się odczytać jedynej rzeczy, po którą się tu
    przychodzi: czy system w ogóle ma z czego żyć.
    """
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")

    braki = klient.get("/health").json()["braki"]
    assert braki["blokujace_start"] == ["DATABASE_URL"]
    assert "ANTHROPIC_API_KEY" in braki["degradujace"]
    assert "DATABASE_URL" not in braki["degradujace"]


def test_health_nie_zglasza_klucza_nieuzywanego_providera(monkeypatch):
    """Instalacja na OpenAI nie ma się dowiadywać o brak klucza Anthropic.

    Jego brak wolno pokazać najwyżej jako informację o zasięgu porównania
    modeli — nigdy jako brak konfiguracji.
    """
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-dziala")
    monkeypatch.setattr(settings, "OPENAI_MODEL", "gpt-5-mini")

    dane = klient.get("/health").json()
    assert "ANTHROPIC_API_KEY" not in dane["braki"]["degradujace"]
    assert "ANTHROPIC_API_KEY" not in dane["braki"]["blokujace_start"]
    assert dane["klasyfikator"]["provider"] == "openai"
    assert "z 3 providerów" in dane["klasyfikator"]["porownanie_modeli"]


def test_zdrowie_nie_alarmuje_o_kluczu_nieuzywanego_providera(monkeypatch):
    """`awaria` budzi człowieka w nocy — pusta linijka w .env nie ma prawa jej wywołać."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-dziala")
    monkeypatch.setattr(settings, "OPENAI_MODEL", "gpt-5-mini")

    powody = klient.get("/zdrowie").json()["powody"]
    assert not any("ANTHROPIC_API_KEY" in p for p in powody)


# ---------------------------------------------------------------------------
# Lista
# ---------------------------------------------------------------------------
def test_lista_liczy_geografie_po_stronie_api(monkeypatch):
    """Frontend nie ma nic liczyć: te same liczby idą do powiadomienia
    i muszą być identyczne w obu miejscach."""
    _podepnij_baze(monkeypatch, WIERSZE)
    dane = klient.get("/zlecenia", headers={"X-Token": TOKEN}).json()
    pierwszy = dane["zlecenia"][0]
    for pole in ("km_trasy", "km_od_bazy", "szacunek_pln", "link_mapy",
                 "link_nawigacji", "lokalizacja_zrodlo", "lat", "lng"):
        assert pole in pierwszy
    assert pierwszy["km_od_bazy"] < 30
    # Kod pocztowy trafia dokładnie — to jest najpewniejsze źródło, jakie mamy.
    assert pierwszy["lokalizacja_zrodlo"] == "kod"


def test_pola_klasyfikatora_ida_pod_nazwami_kolumn(monkeypatch):
    """Nazwa w SQL-u, w API i w TypeScripcie jest ta sama — „skąd to się bierze"
    sprawdza się wtedy grepem, a nie czytaniem mapowania."""
    _podepnij_baze(monkeypatch, WIERSZE)
    z = klient.get("/zlecenia", headers={"X-Token": TOKEN}).json()["zlecenia"][0]
    assert z["pojazd_opis"] == "VW Golf IV"
    assert z["dostawa_miasto"] == "Rzeszow"
    assert z["stan_toczy_sie"] is True
    assert z["pewnosc"] == 90
    assert z["pilnosc"] == "teraz"
    assert z["jezyk"] == "pl"


def test_dlugosc_kursu_jest_policzona(monkeypatch):
    """`km_trasy` to liczba, po której operator decyduje — przy transporcie
    „ile km od bazy" sama nie znaczy nic."""
    _podepnij_baze(monkeypatch, WIERSZE)
    dane = klient.get("/zlecenia", headers={"X-Token": TOKEN}).json()
    blisko = [z for z in dane["zlecenia"] if z["fb_id"] == "blisko"][0]
    daleko = [z for z in dane["zlecenia"] if z["fb_id"] == "daleko"][0]
    assert blisko["km_trasy"] > 0
    assert daleko["km_trasy"] > 1000


def test_domyslnie_bez_progu_na_kilometry(monkeypatch):
    """ZASADA NACZELNA REPO. Wywołanie bez parametrów oddaje WSZYSTKO —
    łącznie z kursem 1100 km, który dla tego operatora jest normalnym dniem."""
    _podepnij_baze(monkeypatch, WIERSZE)
    dane = klient.get("/zlecenia", headers={"X-Token": TOKEN}).json()
    identyfikatory = [z["fb_id"] for z in dane["zlecenia"]]
    assert "daleko" in identyfikatory
    assert len(identyfikatory) == 3


def test_max_km_jest_filtrem_operatora(monkeypatch):
    _podepnij_baze(monkeypatch, WIERSZE)
    dane = klient.get("/zlecenia?max_km=50", headers={"X-Token": TOKEN}).json()
    identyfikatory = [z["fb_id"] for z in dane["zlecenia"]]
    assert "blisko" in identyfikatory
    assert "daleko" not in identyfikatory


def test_nieznane_kilometry_zostaja_przy_filtrze(monkeypatch):
    """Nie wiemy, czy to bliżej czy dalej niż próg. Ukrycie takiego zlecenia
    znaczyłoby, że nierozpoznana nazwa miasta je kasuje."""
    _podepnij_baze(monkeypatch, WIERSZE)
    dane = klient.get("/zlecenia?max_km=50", headers={"X-Token": TOKEN}).json()
    assert "nieznane" in [z["fb_id"] for z in dane["zlecenia"]]


def test_nierozpoznany_cel_nie_dostaje_km_ani_wyceny(monkeypatch):
    """REALNY PRZYPADEK Z PRODUKCJI: „transport Aixam z Dębicy do Turku,
    62-700. Trasa ma około 490 km". Dębica rozpoznana, Turek nie — a panel
    pokazywał „60 km, ~250 zł", bo pod brakujący koniec trasy podstawiał się
    dojazd z bazy operatora. Kierowca odrzuca wtedy kurs na 490 km, widząc
    wycenę lokalnego skoku."""
    wiersz = _wiersz("aixam", odbior_miasto="Krosno", odbior_kod="38-400",
                     dostawa_miasto="Turek", dostawa_kod="62-700",
                     tresc="transport Aixam z Krosna do Turku, 62-700. "
                           "Trasa ma okolo 490 km")
    _podepnij_baze(monkeypatch, [wiersz])
    z = klient.get("/zlecenia", headers={"X-Token": TOKEN}).json()["zlecenia"][0]

    assert z["km_trasy"] is None
    assert z["szacunek_pln"] is None
    # Dojazd z bazy nadal jest policzony — ale jako OSOBNA liczba, pod własną
    # etykietą. Gdyby przeciekał na miejsce trasy, pola wyżej byłyby liczbami.
    assert z["km_od_bazy"] is not None
    # Odbiór rozpoznany, cel nie — panel musi wiedzieć o OBU końcach, inaczej
    # zlecenie idzie na ekran bez jednego ostrzeżenia.
    assert z["lokalizacja_zrodlo"] == "kod"
    assert z["dostawa_zrodlo"] == "brak"
    # Odległość od autora posta: jedyna liczba, jaką tu mamy, i wyraźnie cudza.
    assert z["km_wg_autora"] == 490
    # Treść posta służyła do jej wyciągnięcia, ale nie jedzie na listę —
    # sto pełnych postów przez LTE co 30 sekund nie ma po co lecieć.
    assert "tresc" not in z


def test_szczegol_oddaje_tresc_i_odleglosc_autora(monkeypatch):
    """Na ekranie szczegółu oryginał posta JEST — to jedyne miejsce, w którym
    da się sprawdzić, czy klasyfikator czegoś nie przekręcił."""
    wiersz = _wiersz("aixam", odbior_miasto="Krosno", odbior_kod="38-400",
                     tresc="z Krosna gdzies dalej, trasa ma okolo 490 km")
    _podepnij_baze(monkeypatch, [wiersz])
    z = klient.get("/zlecenia/aixam", headers={"X-Token": TOKEN}).json()
    assert z["tresc"].startswith("z Krosna")
    assert z["km_wg_autora"] == 490
    assert z["km_trasy"] is None and z["szacunek_pln"] is None


def test_przebieg_auta_nie_udaje_odleglosci_wg_autora(monkeypatch):
    """Każdy post w tych grupach ma kilometry, bo każdy ma przebieg."""
    wiersz = _wiersz("golf", odbior_miasto="Krosno", odbior_kod="38-400",
                     tresc="Golf 1.9 TDI, przebieg 245 000 km, nie odpala")
    _podepnij_baze(monkeypatch, [wiersz])
    z = klient.get("/zlecenia", headers={"X-Token": TOKEN}).json()["zlecenia"][0]
    assert z["km_wg_autora"] is None


def test_nierozpoznane_miejsce_bez_pinezki(monkeypatch):
    """Pinezka postawiona „gdzieś" wygląda tak samo jak pinezka pewna."""
    _podepnij_baze(monkeypatch, WIERSZE)
    dane = klient.get("/zlecenia", headers={"X-Token": TOKEN}).json()
    nieznane = [z for z in dane["zlecenia"] if z["fb_id"] == "nieznane"][0]
    assert nieznane["lat"] is None
    assert nieznane["lng"] is None
    assert nieznane["lokalizacja_zrodlo"] == "brak"
    # Surowy fragment z posta zostaje — bez niego pasek ostrzegawczy w panelu
    # mówi „nie ufaj", nie dając operatorowi czym to sprawdzić.
    assert nieznane["odbior_raw"] == "gdzies za lasem"


def test_nieznany_status_daje_400(monkeypatch):
    _podepnij_baze(monkeypatch, WIERSZE)
    odp = klient.get("/zlecenia?status=wymyslony", headers={"X-Token": TOKEN})
    assert odp.status_code == 400


def test_status_wszystkie_znosi_filtr(monkeypatch):
    polaczenie = _podepnij_baze(monkeypatch, WIERSZE)
    klient.get("/zlecenia?status=wszystkie", headers={"X-Token": TOKEN})
    sql, parametry = polaczenie.kursor.zapytania[0]
    assert "status = %s" not in sql


# ---------------------------------------------------------------------------
# TRANSPORT ZWIERZĄT — niżej na liście, ale NA LIŚCIE
# ---------------------------------------------------------------------------
def test_zwierzeta_sortuja_sie_nizej_ale_nie_znikaja(monkeypatch):
    """Sortowanie jest podpowiedzią, nie filtrem: gdyby stało się warunkiem
    w WHERE, zlecenie zniknęłoby operatorowi z oczu — a to już nie jest decyzja
    kierowcy, tylko decyzja panelu."""
    polaczenie = _podepnij_baze(monkeypatch, WIERSZE)
    klient.get("/zlecenia", headers={"X-Token": TOKEN})
    sql, _ = polaczenie.kursor.zapytania[0]
    warunek = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    sortowanie = sql.split("ORDER BY", 1)[1]
    assert "kategoria_ladunku" not in warunek     # nie filtrujemy
    assert "'zwierze') ASC" in sortowanie          # tylko przesuwamy na dół
    # COALESCE, bo NULL (wiersze sprzed migracji 0010) w ORDER BY ASC idzie na
    # KONIEC — cała historia wylądowałaby pod zwierzętami.
    assert "COALESCE(kategoria_ladunku" in sortowanie


def test_kategoria_ladunku_dojezdza_do_panelu(monkeypatch):
    """Panel rysuje z tego znacznik na karcie — pole musi być w odpowiedzi."""
    _podepnij_baze(monkeypatch, [_wiersz("kon", kategoria_ladunku="zwierze",
                                         odbior_miasto="Krosno")])
    dane = klient.get("/zlecenia", headers={"X-Token": TOKEN}).json()
    assert dane["zlecenia"][0]["kategoria_ladunku"] == "zwierze"


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------
def test_patch_zmienia_status(monkeypatch):
    _podepnij_baze(monkeypatch, [{"fb_id": "blisko", "status": "dzwonie",
                                  "notatka": None, "cena_koncowa": None}])
    odp = klient.patch("/zlecenia/blisko", headers={"X-Token": TOKEN},
                       json={"status": "dzwonie"})
    assert odp.status_code == 200
    assert odp.json()["status"] == "dzwonie"


def test_patch_dopuszcza_powrot_do_nowe(monkeypatch):
    """„Śmieć" musi dać się cofnąć — zlecenie wyrzucone przez pomyłkę i nie do
    odzyskania jest dokładnie tym, przed czym broni zasada naczelna repo."""
    _podepnij_baze(monkeypatch, [{"fb_id": "x", "status": "nowe",
                                  "notatka": None, "cena_koncowa": None}])
    odp = klient.patch("/zlecenia/x", headers={"X-Token": TOKEN},
                       json={"status": "nowe"})
    assert odp.status_code == 200


def test_patch_odrzuca_nieznany_status(monkeypatch):
    _podepnij_baze(monkeypatch, WIERSZE)
    odp = klient.patch("/zlecenia/x", headers={"X-Token": TOKEN},
                       json={"status": "usuniete"})
    assert odp.status_code == 400


def test_patch_bez_zmian_daje_400(monkeypatch):
    _podepnij_baze(monkeypatch, WIERSZE)
    odp = klient.patch("/zlecenia/x", headers={"X-Token": TOKEN}, json={})
    assert odp.status_code == 400


def test_patch_nie_czysci_notatki_gdy_jej_nie_podano(monkeypatch):
    """`None` znaczy „nie ruszaj", a nie „wyczyść": operator dopisujący cenę nie
    ma stracić tekstu wpisanego minutę wcześniej."""
    polaczenie = _podepnij_baze(monkeypatch, [{"fb_id": "x", "status": "nowe",
                                               "notatka": "stara",
                                               "cena_koncowa": None}])
    klient.patch("/zlecenia/x", headers={"X-Token": TOKEN},
                 json={"cena_koncowa": 350})
    sql, _ = polaczenie.kursor.zapytania[0]
    # Patrzymy WYŁĄCZNIE na część SET — `notatka` pojawia się też w RETURNING,
    # bo panel dostaje z powrotem pełny stan rekordu.
    czesc_set = sql.split("SET", 1)[1].split("WHERE", 1)[0]
    assert "notatka" not in czesc_set
    assert "cena_koncowa" in czesc_set


# ---------------------------------------------------------------------------
# Nieodpalone migracje
# ---------------------------------------------------------------------------
def test_brak_kolumn_mowi_ktora_migracje_odpalic(monkeypatch):
    """Stan „baza jest, migracji 0004 nie ma" to normalny etap wdrożenia.
    Bez tego sprawdzenia pierwszy deploy kończy się piątką i pytaniem
    „czemu panel nie działa"."""
    _podepnij_baze(monkeypatch, WIERSZE)
    monkeypatch.setattr(router_zlecen.db, "kolumny", lambda conn, tabela: set())
    odp = klient.get("/zlecenia", headers={"X-Token": TOKEN})
    assert odp.status_code == 503
    assert "0004_klasyfikacja.sql" in odp.json()["detail"]


def test_brak_samego_panelu_wskazuje_wlasciwa_migracje(monkeypatch):
    """Dwie migracje, dwa różne komunikaty — „odpal migracje" bez numeru
    znaczy przejrzenie ośmiu plików."""
    _podepnij_baze(monkeypatch, WIERSZE)
    monkeypatch.setattr(router_zlecen.db, "kolumny",
                        lambda conn, tabela: {"odbior_miasto", "pojazd_opis",
                                              "pewnosc"})
    odp = klient.get("/zlecenia", headers={"X-Token": TOKEN})
    assert odp.status_code == 503
    assert "0005_panel.sql" in odp.json()["detail"]
