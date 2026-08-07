"""CAŁY PRZEBIEG na prawdziwej bazie: Apify -> bramka -> model -> `posty` -> alert.

PO CO ISTNIEJE — I CZEGO NIE ZŁAPAŁY POPRZEDNIE TESTY. Testy z
`test_zapis_klasyfikacji.py` wołają `_zapisz_post` wprost i przechodziły na
zielono przez cały czas, gdy produkcja zapisywała 27 postów z werdyktem modelu
i kompletem NULL-i. Bo bug nie siedział w tej funkcji: siedział w tym, CO do
niej dociera i co się z jej wynikiem dzieje dalej. Ta różnica — „funkcja
działa" kontra „przebieg dowozi" — jest w tym repo zbyt droga, żeby zostawić ją
bez testu. Dlatego tutaj wołamy `run()`, czyli dokładnie to, co odpala cron,
i zaślepiamy WYŁĄCZNIE brzegi sieciowe: Apify, model, Telegram.

Wszystko pomiędzy jest prawdziwe — bramka, klasyfikator, walidacja, SQL,
migracje z repo, dedup powiadomień, budowanie treści alertu.

URUCHOMIENIE (jak w test_zapis_klasyfikacji.py — osobna baza, NIGDY produkcja):

    TEST_DATABASE_URL=postgresql://user:haslo@localhost/laweta_test \\
        python -m pytest laweta_radar/tests/test_przebieg_do_bazy.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.services import llm, telegram_notify  # noqa: E402
from laweta_radar.tests.test_zapis_klasyfikacji import (  # noqa: E402
    _dsn, _psycopg2, _sprawdz_dsn, baza)
from laweta_radar.workers import classifier as c  # noqa: E402
from laweta_radar.workers import fb_fetcher as f  # noqa: E402

# Zapis dotyka kolumn z 0003/0004, powiadomienia żyją w tabeli z 0006.
MIGRACJE = ("0001_posty.sql", "0002_gate.sql", "0003_fetcher.sql",
            "0004_klasyfikacja.sql", "0005_panel.sql", "0006_powiadomienia.sql",
            "0009_werdykt_modelu.sql", "0010_kategoria_ladunku.sql",
            "0011_kierunek.sql", "0013_kierunek_geo.sql")

GRUPA = {"url": "https://www.facebook.com/groups/testowa", "name": "Grupa testowa"}

TRESC = ("Potrzebna laweta, auto po stluczce, Krosno -> Rzeszow, "
         "golf nie odpala, tel 600 100 200")

# Odpowiedź modelu z KOMPLETEM pól — świadomie taka, w której żadne pole nie
# schodzi na wartość domyślną (post z samymi domyślnymi przeszedłby test nawet
# wtedy, gdyby zapis gubił połowę treści).
ODPOWIEDZ_MODELU = """
{"czy_zlecenie": true, "typ": "holowanie",
 "odbior": {"raw": "Krosno, Podkarpacka", "kod": "38-400", "miasto": "Krosno"},
 "dostawa": {"raw": "warsztat w Rzeszowie", "kod": "35-001", "miasto": "Rzeszow"},
 "pojazd": {"opis": "VW Golf IV", "kategoria": "osobowy"},
 "stan": {"toczy_sie": false, "ma_kola": true, "po_wypadku": true,
          "uwagi": "po stluczce, nie odpala"},
 "pilnosc": "teraz", "kontakt": {"typ": "telefon", "wartosc": "600100200"},
 "cena_sugerowana": 350, "pewnosc": 88, "powod": "prosba o lawete wprost"}
"""


class _Telegram:
    """Atrapa transportu: zapamiętuje wysłane wiadomości, nic nie wysyła."""

    def __init__(self):
        self.wiadomosci: list[str] = []

    def wyslij(self, tresc, przyciski=None):
        self.wiadomosci.append(tresc)
        return 1000 + len(self.wiadomosci)      # message_id z Telegrama


@pytest.fixture
def przebieg(monkeypatch):
    """Świeża baza z migracji repo + zaślepione brzegi sieciowe. Zwraca sterowanie.

    Zaślepiamy DOKŁADNIE trzy rzeczy i ani jednej więcej: wywołanie Apify,
    wywołanie modelu i wysyłkę do Telegrama. Każda dodatkowa atrapa po tej
    stronie zmniejszałaby to, co ten test faktycznie sprawdza — a sprawdza
    właśnie te kawałki, które w produkcji się rozjechały.
    """
    psycopg2 = _psycopg2()
    _sprawdz_dsn(_dsn())
    conn = psycopg2.connect(_dsn())
    katalog = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "api", "migrations")
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS feedback, harmonogram, powiadomienia, "
                    "posty CASCADE")
        for plik in MIGRACJE:
            with open(os.path.join(katalog, plik), encoding="utf-8") as fh:
                cur.execute(fh.read())
    conn.commit()

    monkeypatch.setattr(f.settings, "DATABASE_URL", _dsn())
    monkeypatch.setattr(f.settings, "GATE_TRYB", "aktywny")
    # Cisza nocna liczy się z LOKALNEGO zegara maszyny, więc bez tego test
    # przechodziłby w dzień i wywalał się w nocy — a wtedy pierwszą reakcją
    # byłoby uznanie go za „migający" i wyłączenie. OD == DO wyłącza okno.
    monkeypatch.setattr(f.settings, "CISZA_NOCNA_OD", 0)
    monkeypatch.setattr(f.settings, "CISZA_NOCNA_DO", 0)

    # --- brzegi sieciowe ---
    monkeypatch.setattr(f, "load_apify_tokens", lambda: ["token-testowy"])
    monkeypatch.setattr(f.apify_proxy, "preflight", lambda tokens=None: (True, []))
    monkeypatch.setattr(f.cfg_groups, "grupy_do_pobrania", lambda: [dict(GRUPA)])
    monkeypatch.setattr(llm, "model_domyslny", lambda: "model-testowy")
    monkeypatch.setattr(llm, "zapytaj",
                        lambda *_a, **_k: przebieg.odpowiedz_modelu)
    # Przełącznik „zepsutego deployu": warstwa zapisu nie widzi kolumn
    # ekstrakcji, więc do bazy idzie sam werdykt. Tak wygląda stan z produkcji
    # i tylko przez taki przełącznik da się go odtworzyć W PRZEBIEGU, a potem
    # w tym samym teście naprawić.
    prawdziwe_kolumny = f._kolumny_ekstrakcji
    monkeypatch.setattr(f, "_kolumny_ekstrakcji",
                        lambda: () if przebieg.bez_kolumn else prawdziwe_kolumny())

    telegram = _Telegram()
    monkeypatch.setattr(telegram_notify, "skonfigurowany", lambda: True)
    monkeypatch.setattr(telegram_notify, "wyslij", telegram.wyslij)

    class Sterowanie:
        odpowiedz_modelu = ODPOWIEDZ_MODELU
        bez_kolumn = False        # True = zapis nie zna kolumn ekstrakcji

        def __init__(self):
            self.conn = conn
            self.telegram = telegram
            self.log: list[str] = []

        def posty_z_apify(self, *tresci: str, wiek_min: int = 5) -> None:
            opublikowany = datetime.now(timezone.utc) - timedelta(minutes=wiek_min)
            itemy = [{"text": t, "url": f"https://www.facebook.com/p/{i}",
                      "time": opublikowany.isoformat(), "user": {"name": "Jan K."}}
                     for i, t in enumerate(tresci or (TRESC,), start=1)]
            monkeypatch.setattr(f, "_apify_run_group",
                                lambda *_a, **_k: [dict(i) for i in itemy])

        def uruchom(self) -> int:
            return f.run(log=self.log.append)

        def przewin_harmonogram(self) -> None:
            """Tak, jakby minęło te 15 minut, po których grupa znów wypada.

            Bez tego drugi `run()` w tym samym teście mija się z harmonogramem
            („POMIJAM: nie ta minuta") i NIC nie pobiera — a test dwóch
            przebiegów przechodzi, bo drugiego przebiegu faktycznie nie było.
            """
            with self.conn.cursor() as cur:
                cur.execute("UPDATE harmonogram SET nastepny_run_at = "
                            "NOW() - INTERVAL '1 minute'")
            self.conn.commit()

        def wiersz(self, fb_id: str) -> dict:
            kolumny = (*c.KOLUMNY_EKSTRAKCJI, "zrodlo_decyzji", "czy_zlecenie",
                       "status", "ai_model")
            with self.conn.cursor() as cur:
                cur.execute(f"SELECT {', '.join(kolumny)} FROM posty "  # noqa: S608
                            f"WHERE fb_id = %s", (fb_id,))
                wiersz = cur.fetchone()
            assert wiersz is not None, f"posta {fb_id} nie ma w bazie"
            return dict(zip(kolumny, wiersz))

        def powiadomienia(self) -> list[dict]:
            with self.conn.cursor() as cur:
                cur.execute("SELECT fb_id, kanal, tresc, message_id FROM powiadomienia "
                            " ORDER BY id")
                return [dict(zip(("fb_id", "kanal", "tresc", "message_id"), r))
                        for r in cur.fetchall()]

    przebieg = Sterowanie()
    przebieg.posty_z_apify(TRESC)
    try:
        yield przebieg
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. WYMAGANIE Z ZGŁOSZENIA: komplet pól z klasyfikatora jest w bazie PO PRZEBIEGU
# ---------------------------------------------------------------------------
@baza
def test_przebieg_zapisuje_komplet_pol_ekstrakcji(przebieg):
    """Zamockowany klasyfikator zwraca komplet pól -> odczyt z bazy je pokazuje.

    Dokładnie ten stan, który w produkcji dał `count(typ) = 0` przy 27 wierszach:
    model odpowiadał, werdykt dojeżdżał, ekstrakcja nie.
    """
    assert przebieg.uruchom() == 0

    fb_id = f.fb_id(TRESC)
    w = przebieg.wiersz(fb_id)

    assert w["zrodlo_decyzji"] == "ai" and w["czy_zlecenie"] is True
    # Cztery pola wprost ze zgłoszenia...
    assert w["typ"] == "holowanie"
    assert int(w["pewnosc"]) == 88
    assert w["odbior_miasto"] == "Krosno"
    assert w["kontakt_wartosc"] == "600100200"
    # ...i komplet reszty, bo to on ginął.
    assert [k for k in c.KOLUMNY_EKSTRAKCJI if w[k] is None] == []
    assert w["ai_model"] == "model-testowy"


# ---------------------------------------------------------------------------
# 2. WYMAGANIE Z ZGŁOSZENIA: NULL w pewności NIE MOŻE wyciszyć alertu
# ---------------------------------------------------------------------------
@baza
def test_zlecenie_bez_pewnosci_i_tak_budzi_i_mowi_o_tym_wprost(przebieg):
    """`czy_zlecenie=true` + `pewnosc IS NULL` -> alert JEST, z adnotacją.

    Tak wygląda każdy z 15 wierszy ze zgłoszenia. Porównanie `pewnosc >=
    MIN_PEWNOSC` na NULL-u nie przepuszczało żadnego z nich, więc tabela
    `powiadomienia` została pusta — co z zewnątrz wygląda jak brak zleceń na
    rynku, a jest utratą wszystkich.

    Wiersz bez ekstrakcji robimy tak, jak zrobiła to produkcja: zapisem, który
    nie zna kolumn ekstrakcji. Werdykt dojeżdża, reszta zostaje NULL-em.
    """
    przebieg.bez_kolumn = True

    assert przebieg.uruchom() == 0
    fb_id = f.fb_id(TRESC)
    w = przebieg.wiersz(fb_id)
    assert w["czy_zlecenie"] is True and w["pewnosc"] is None   # stan ze zgłoszenia

    wiersze = przebieg.powiadomienia()
    assert [p["fb_id"] for p in wiersze] == [fb_id], (
        "zlecenie z nieznaną pewnością NIE dostało powiadomienia — cisza jest "
        "tu nie do odróżnienia od braku zleceń")
    assert wiersze[0]["kanal"] == "telegram" and wiersze[0]["message_id"]

    tresc = przebieg.telegram.wiadomosci[0]
    assert "pewność nieznana" in tresc, (
        "alert nie mówi, że pewność jest nieznana — operator przeczyta go jak "
        "zlecenie w pełni rozpoznane")
    assert "trasa nieustalona" in tresc     # bez ekstrakcji nie ma i miejsc
    assert TRESC[:40] in tresc, "bez cytatu operator nie ma z czego odtworzyć braków"


@baza
def test_niska_pewnosc_nadal_nie_budzi(przebieg, monkeypatch):
    """Próg ma dalej działać na ZNANEJ liczbie — inaczej „napraw NULL-e" znaczy
    „wyłącz próg", a operator dostaje każdy śmieć, który model uznał za zlecenie."""
    monkeypatch.setattr(f.settings, "MIN_PEWNOSC", 40)
    przebieg.odpowiedz_modelu = ODPOWIEDZ_MODELU.replace('"pewnosc": 88',
                                                         '"pewnosc": 10')
    assert przebieg.uruchom() == 0

    assert int(przebieg.wiersz(f.fb_id(TRESC))["pewnosc"]) == 10
    assert przebieg.powiadomienia() == []
    assert przebieg.telegram.wiadomosci == []


# ---------------------------------------------------------------------------
# 3. WYMAGANIE Z ZGŁOSZENIA: pusta ekstrakcja przy `zrodlo_decyzji='ai'` KRZYCZY
# ---------------------------------------------------------------------------
@baza
def test_pusta_ekstrakcja_przy_werdykcie_modelu_daje_ostrzezenie_z_fb_id(przebieg):
    """Post z `zrodlo_decyzji='ai'` i kompletem NULL-i -> OSTRZEŻENIE z fb_id.

    Poprzednia wersja tego ostrzeżenia stała na warunku „słownik w pamięci jest
    pusty", a ten był PEŁNY przy każdym z 27 postów — wynik modelu istniał,
    ginął dopiero między pamięcią a tabelą. Ostrzeżenie nie padło ani razu.
    Teraz warunek czyta WIERSZ Z BAZY, więc pyta o to samo, o co pyta operator.
    """
    przebieg.bez_kolumn = True

    assert przebieg.uruchom() == 0

    fb_id = f.fb_id(TRESC)
    assert przebieg.wiersz(fb_id)["zrodlo_decyzji"] == "ai"

    ostrzezenia = [w for w in przebieg.log if "OSTRZEŻENIE" in w and fb_id in w]
    assert ostrzezenia, (
        f"brak OSTRZEŻENIA z fb_id {fb_id} — cicha utrata wyniku, za który "
        f"zapłacono tokenami, przeżyje kolejny przebieg")
    assert "ekstrakcji" in ostrzezenia[0]
    # I podsumowanie przebiegu, żeby dało się to zobaczyć bez grepowania logu.
    assert any("bez ani jednego pola z ekstrakcji" in w for w in przebieg.log)


@baza
def test_naprawa_wiersza_z_poprzedniego_przebiegu(przebieg):
    """Uszkodzony wiersz naprawia się przy KOLEJNYM przebiegu, nie ręcznie.

    Pierwszy przebieg zapisuje post bez kolumn ekstrakcji (stan z produkcji),
    drugi — z nimi. Bez wypadnięcia z dedupu i bez naprawczego ON CONFLICT
    drugi przebieg policzyłby ten post jako duplikat i wiersz zostałby pusty
    na zawsze.
    """
    przebieg.bez_kolumn = True
    assert przebieg.uruchom() == 0
    fb_id = f.fb_id(TRESC)
    assert przebieg.wiersz(fb_id)["typ"] is None

    przebieg.bez_kolumn = False           # poprawka wdrożona
    przebieg.przewin_harmonogram()
    przebieg.posty_z_apify(TRESC)
    assert przebieg.uruchom() == 0

    w = przebieg.wiersz(fb_id)
    assert [k for k in c.KOLUMNY_EKSTRAKCJI if w[k] is None] == []
    assert int(w["pewnosc"]) == 88
    assert any("uzupełniono ekstrakcję w 1" in linia for linia in przebieg.log)


@baza
def test_dwa_przebiegi_daja_jeden_alert(przebieg):
    """Dedup powiadomień stoi na wierszu w bazie — nie na pamięci procesu.

    Fetcher chodzi z crona co pięć minut i przebiegi potrafią się nałożyć.
    Powiadomienie wysłane drugi raz o tym samym poście kosztuje zaufanie do
    kanału, a wyciszony bot to awaria całkowita, tylko rozłożona na dni.
    """
    assert przebieg.uruchom() == 0
    przebieg.przewin_harmonogram()
    przebieg.posty_z_apify(TRESC)         # ten sam post w kolejnej odpowiedzi Apify
    assert przebieg.uruchom() == 0
    assert any("pobrano 1 postów" in linia for linia in przebieg.log[len(przebieg.log) // 2:]), (
        "drugi przebieg nic nie pobrał — test dedupu sprawdzałby wtedy nic")

    assert len(przebieg.telegram.wiadomosci) == 1
    assert len([p for p in przebieg.powiadomienia() if p["kanal"] == "telegram"]) == 1


@baza
def test_post_odrzucony_przez_bramke_nie_budzi_nikogo(przebieg):
    """Bramka odsiewa PRZED modelem — i alert nie ma prawa powstać."""
    przebieg.posty_z_apify("Sprzedam opony zimowe 205/55 R16, komplet, stan bdb")
    assert przebieg.uruchom() == 0

    assert przebieg.powiadomienia() == []
    assert przebieg.telegram.wiadomosci == []


# ---------------------------------------------------------------------------
# 7. TRANSPORT ZWIERZĄT: cisza na telefonie, PEŁNY wiersz w bazie
#
# To jest jedyne miejsce, w którym widać RÓŻNICĘ między „nie brzęczę" a „nie
# zapisuję". Obie ścieżki dają operatorowi tę samą ciszę na telefonie, a różnią
# się tym, czy za pół roku da się odpowiedzieć na pytanie „ile takich kursów
# przeszło obok" — czyli czy decyzja o przyczepie do koni ma się o co oprzeć.
# ---------------------------------------------------------------------------
KON = ("Potrzebny transport busem jednego konia (+dużo sprzętu) z Gajewnik "
       "do 38-400 Krosno, proszę o kontakt")


@baza
def test_transport_zwierzat_laduje_w_bazie_ale_nie_brzeczy(przebieg, monkeypatch):
    monkeypatch.setattr(f.settings, "ALERT_ZWIERZETA", 0)
    przebieg.posty_z_apify(KON)
    assert przebieg.uruchom() == 0

    fb_id = f.fb_id(KON)
    with przebieg.conn.cursor() as cur:
        cur.execute("SELECT czy_zlecenie, status, kategoria_ladunku, gate_werdykt "
                    "  FROM posty WHERE fb_id = %s", (fb_id,))
        czy_zlecenie, status, kategoria, gate_werdykt = cur.fetchone()

    # Zlecenie jak każde inne: przeszło bramkę, poszło do modelu, jest w kolejce.
    assert gate_werdykt is True
    assert czy_zlecenie is True
    assert status == "nowe"
    assert kategoria == "zwierze"

    # ...i ani jednej wiadomości na telefonie.
    assert przebieg.telegram.wiadomosci == []
    # Wiersz w `powiadomienia` MUSI powstać — inaczej podsumowanie ranne szuka
    # zleceń bez wiersza i przysłałoby ten sam kurs o świcie, czyli
    # ALERT_ZWIERZETA=0 opóźniałoby alert zamiast go wyłączać.
    kanaly = [p["kanal"] for p in przebieg.powiadomienia()]
    assert kanaly == ["pominiete_zwierze"]


@baza
def test_alert_zwierzeta_1_wysyla_alert_ze_znacznikiem(przebieg, monkeypatch):
    """Jedna zmienna w .env, zero zmian w reszcie systemu — bo dane o tych
    kursach zbierają się niezależnie od jej wartości."""
    monkeypatch.setattr(f.settings, "ALERT_ZWIERZETA", 1)
    przebieg.posty_z_apify(KON)
    assert przebieg.uruchom() == 0

    assert len(przebieg.telegram.wiadomosci) == 1
    assert "ZWIERZ" in przebieg.telegram.wiadomosci[0].upper()


# ---------------------------------------------------------------------------
# 8. OFERTY PRZEWOŹNIKÓW: cisza na telefonie, PEŁNY wiersz w bazie
#
# Oba posty niżej są z produkcji. Oba przeszły przez bramkę i klasyfikator jako
# zlecenia i oba obudziły telefon — mimo że są ogłoszeniami konkurencji
# z wolnym miejscem. Mają komplet cech zlecenia: trasę, datę i numer telefonu.
#
# Ten test jest jedynym miejscem, w którym widać CAŁĄ ścieżkę: bramka odrzuca,
# kierunek zostaje w bazie, tabela `powiadomienia` jest pusta.
# ---------------------------------------------------------------------------
OFERTY_Z_PRODUKCJI = (
    "Czwartek 06.08.26r wolna laweta Elblag-Lublin tel.501606207",
    "Wolny transport 10.08 na trasie Grudziadz - Warszawa - Siedlce "
    "Woj Maz 25T 9,5m Tel. 607284682",
)


@baza
def test_oferty_przewoznikow_laduja_w_bazie_ale_nie_brzecza(przebieg, monkeypatch):
    """Wymaganie ze zgłoszenia, w komplecie: bramka odrzuca, kierunek='oferta',
    czy_zlecenie=false, ZERO wierszy w `powiadomienia`."""
    monkeypatch.setattr(f.settings, "ALERT_OFERTY", 0)
    # Tryb aktywny, czyli stan docelowy: bramka realnie blokuje i nie płacimy
    # za tokeny. W cieniu ten sam post poszedłby do modelu, a wycisza go wtedy
    # `services/powiadomienia` (patrz test niżej).
    monkeypatch.setattr(f.settings, "GATE_TRYB", "aktywny")
    przebieg.posty_z_apify(*OFERTY_Z_PRODUKCJI)
    assert przebieg.uruchom() == 0

    for tresc in OFERTY_Z_PRODUKCJI:
        with przebieg.conn.cursor() as cur:
            cur.execute("SELECT czy_zlecenie, status, kierunek, gate_werdykt, "
                        "       gate_powod, zrodlo_decyzji, tresc "
                        "  FROM posty WHERE fb_id = %s", (f.fb_id(tresc),))
            wiersz = cur.fetchone()
        assert wiersz is not None, f"post zniknął z bazy: {tresc}"
        (czy_zlecenie, status, kierunek, gate_werdykt, gate_powod,
         zrodlo, w_bazie) = wiersz

        assert gate_werdykt is False, tresc
        assert gate_powod == "oferta przewoznika", tresc
        assert czy_zlecenie is False, tresc
        assert kierunek == "oferta", tresc
        assert status == "smiec", tresc
        # Decyzja bramki, nie modelu — czyli zero zapłaconych tokenów.
        assert zrodlo == "gate", tresc
        # ...i to jest cała różnica między „nie budzę" a „nie zapisuję":
        # treść oferty zostaje w całości, razem z trasą i numerem.
        assert w_bazie == tresc

    # ZERO powiadomień: ani wysłanych, ani pominiętych. Post z
    # `czy_zlecenie=false` nie dochodzi do warstwy alertów w ogóle, a
    # podsumowanie ranne szuka wyłącznie zleceń ze statusem `nowe`.
    assert przebieg.telegram.wiadomosci == []
    assert przebieg.powiadomienia() == []


@baza
def test_oferta_przeoczona_przez_model_nadal_nie_brzeczy(przebieg, monkeypatch):
    """Druga linia obrony i jedyna ścieżka, na której ALERT_OFERTY w ogóle
    pracuje: bramka rozpoznała ofertę (w cieniu niczego nie blokuje), a model
    mimo to orzekł „zlecenie". Wiersz w `powiadomienia` MUSI powstać — inaczej
    podsumowanie ranne przysłałoby ten sam post o świcie, czyli ALERT_OFERTY=0
    opóźniałby alert zamiast go wyłączać."""
    monkeypatch.setattr(f.settings, "ALERT_OFERTY", 0)
    monkeypatch.setattr(f.settings, "GATE_TRYB", "cien")
    # Model przeoczył kierunek i oddał zwykłe zlecenie — dokładnie to, co robił
    # przed poprawką promptu.
    przebieg.odpowiedz_modelu = ODPOWIEDZ_MODELU
    przebieg.posty_z_apify(OFERTY_Z_PRODUKCJI[0])
    assert przebieg.uruchom() == 0

    with przebieg.conn.cursor() as cur:
        cur.execute("SELECT czy_zlecenie, kierunek FROM posty WHERE fb_id = %s",
                    (f.fb_id(OFERTY_Z_PRODUKCJI[0]),))
        czy_zlecenie, kierunek = cur.fetchone()

    # Werdykt modelu zostaje (to on ma ostatnie słowo o tym, czy to zlecenie),
    # ale kierunek z bramki przeżył — model powiedział „nie wiem".
    assert czy_zlecenie is True
    assert kierunek == "oferta"
    assert przebieg.telegram.wiadomosci == []
    assert [p["kanal"] for p in przebieg.powiadomienia()] == ["pominiete_oferta"]


@baza
def test_alert_oferty_1_wysyla_alert_ze_znacznikiem(przebieg, monkeypatch):
    """Jedna zmienna w .env dla operatora, który CHCE wiedzieć, kto jedzie jego
    kierunkami — cudzy kurs bywa okazją na doładunek albo na podnajęcie."""
    monkeypatch.setattr(f.settings, "ALERT_OFERTY", 1)
    monkeypatch.setattr(f.settings, "GATE_TRYB", "cien")
    przebieg.posty_z_apify(OFERTY_Z_PRODUKCJI[0])
    assert przebieg.uruchom() == 0

    assert len(przebieg.telegram.wiadomosci) == 1
    assert "OFERTA PRZEWOŹNIKA" in przebieg.telegram.wiadomosci[0]


KONTROLA = "Szukam wolnego miejsca na lawecie z Kolonii do Krakowa"


@baza
def test_kontrola_szukam_wolnego_miejsca_nadal_dowozi_alert(przebieg, monkeypatch):
    """Ta sama fraza co w ofercie, przeciwna strona rynku — i CAŁA droga do
    telefonu ma zostać nietknięta. To jest test na koszt tej poprawki: gdyby
    „wolne miejsce" zaczęło odpadać samo z siebie, kasowalibyśmy klientów
    szukających doładunku, czyli najlepszy typ zlecenia, jaki ten system zna."""
    monkeypatch.setattr(f.settings, "ALERT_OFERTY", 0)
    monkeypatch.setattr(f.settings, "GATE_TRYB", "aktywny")
    przebieg.posty_z_apify(KONTROLA)
    assert przebieg.uruchom() == 0

    with przebieg.conn.cursor() as cur:
        cur.execute("SELECT czy_zlecenie, status, kierunek, gate_werdykt "
                    "  FROM posty WHERE fb_id = %s", (f.fb_id(KONTROLA),))
        czy_zlecenie, status, kierunek, gate_werdykt = cur.fetchone()

    assert gate_werdykt is True
    assert czy_zlecenie is True
    assert status == "nowe"
    assert kierunek == "zlecenie"
    assert len(przebieg.telegram.wiadomosci) == 1
    assert "OFERTA PRZEWOŹNIKA" not in przebieg.telegram.wiadomosci[0]
