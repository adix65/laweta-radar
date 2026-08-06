"""`/zlecenia` — lista, szczegół i zmiana statusu. To jest cały panel.

FRONTEND NIE LICZY NICZEGO. Każdy rekord wychodzi stąd z policzonymi
`km_od_bazy`, `szacunek_pln`, `link_mapy` i `link_nawigacji`. Powód nie jest
estetyczny: te same liczby pokazuje powiadomienie na Telegramie (`services/
powiadomienia.py`) i obie ścieżki liczą je tym samym kodem (`services/geo.py`).
Gdyby panel liczył po swojemu, operator zobaczyłby w alercie 42 km, a w aplikacji
48 km i przestałby ufać obu.

`km_trasy` I `szacunek_pln` BYWAJĄ NULL-em i to jest wynik, nie awaria: bez obu
rozpoznanych końców trasy nie ma czego liczyć, a panel ma wtedy napisać „trasa
nieustalona" zamiast pokazać jakąkolwiek liczbę. Panel nie ma prawa podstawić
tam `km_od_bazy` — to jest dokładnie ten błąd, przez który kurs Dębica->Turek
(490 km wg autora, nierozpoznany Turek) wyglądał na ekranie jak „60 km, ~250 zł".

FILTRY TO PYTANIA OPERATORA, NIE PROGI SYSTEMU. `max_km` zawęża listę, bo ktoś
kliknął pigułkę „do 50 km" — i przestaje działać, gdy tę pigułkę odklika.
Domyślne wywołanie bez parametrów zwraca WSZYSTKO, co system złapał, niezależnie
od dystansu i kierunku. Ta różnica jest sednem zasady naczelnej repo i najłatwiej
ją zgubić, dopisując „na wszelki wypadek" domyślne `max_km` do zapytania.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from laweta_radar.api import db
from laweta_radar.api.auth import wymagaj_tokenu
from laweta_radar.services import feedback, geo

router = APIRouter(prefix="/zlecenia", tags=["zlecenia"],
                   dependencies=[Depends(wymagaj_tokenu)])

# Stany obsługi. `nowe` jest na liście CELOWO, mimo że prompt wymienia cztery:
# „Śmieć" musi dać się cofnąć. Nieodwracalne kliknięcie w aplikacji obsługiwanej
# jednym kciukiem na postoju to kwestia czasu, a nie ryzyko — a zlecenie wyrzucone
# przez pomyłkę i nie do odzyskania jest dokładnie tym, przed czym broni zasada
# naczelna repo.
STATUSY = ("nowe", "dzwonie", "wygrane", "przegrane", "smiec")

# Kolumny czytane do listy. Wypisane jawnie, nie `SELECT *`: kolejna migracja
# doda kolumnę, a lista zleceń pobierana co 30 s przez telefon w zasięgu LTE
# nie ma powodu wozić pełnej treści posta dla stu rekordów.
POLA_LISTY = ("fb_id", "grupa_nazwa", "grupa_url", "post_url", "opublikowany_at",
              "pobrany_at", "status", "status_at", "stale", "gate_jezyk",
              "kategoria_ladunku", "kierunek",
              "typ", "odbior_raw", "odbior_kod", "odbior_miasto",
              "dostawa_raw", "dostawa_kod", "dostawa_miasto",
              "pojazd_opis", "pojazd_kategoria", "stan_toczy_sie",
              "stan_ma_kola", "stan_po_wypadku", "stan_uwagi",
              "pilnosc", "kontakt_typ", "kontakt_wartosc",
              "cena_sugerowana", "pewnosc", "powod", "notatka", "cena_koncowa")


class Zmiana(BaseModel):
    """Ciało PATCH-a. Wszystkie pola opcjonalne — panel wysyła to, co zmienił.

    `None` znaczy „nie ruszaj", a nie „wyczyść". Rozróżnienie jest istotne przy
    notatce: operator dopisujący cenę nie ma stracić tekstu wpisanego minutę
    wcześniej tylko dlatego, że formularz wysłał komplet pól.
    """

    status: str | None = Field(default=None)
    notatka: str | None = Field(default=None, max_length=4000)
    cena_koncowa: float | None = Field(default=None, ge=0, le=1_000_000)


def _rekord(wiersz: dict, *, zwroc_tresc: bool = True) -> dict:
    """Wiersz z `posty` -> rekord dla panelu, z policzoną geografią.

    Pola klasyfikatora idą DALEJ PŁASKO, dokładnie pod nazwami kolumn
    z `0004_klasyfikacja.sql`. Świadomie bez przepakowywania na `zlecenie.odbior
    .miasto`: nazwa w SQL-u, w API i w TypeScripcie jest wtedy ta sama, więc
    „gdzie to się bierze" sprawdza się grepem, a nie czytaniem mapowania.

    `zwroc_tresc=False` na liście: pełna treść posta jest tu POTRZEBNA (wyciąga
    się z niej odległość podaną przez autora), ale nie ma po co jechać przez
    LTE dla stu rekordów — więc liczymy z niej i wyrzucamy z odpowiedzi.
    """
    dane = dict(wiersz)
    tresc = dane.get("tresc")
    if not zwroc_tresc:
        dane.pop("tresc", None)
    odbior = geo.geokoduj(dane.get("odbior_kod"), dane.get("odbior_miasto"), tresc=tresc)
    dostawa = geo.geokoduj(dane.get("dostawa_kod"), dane.get("dostawa_miasto"), tresc=tresc)
    pods = geo.podsumowanie(odbior, dostawa, tresc)

    dane.setdefault("jezyk", dane.get("gate_jezyk"))
    dane.update({
        # NULL, gdy którykolwiek koniec trasy jest nierozpoznany — razem
        # z `szacunek_pln`. Panel pisze wtedy „trasa nieustalona" i nie ma
        # czego zaokrąglić: patrz `services/geo.podsumowanie`.
        "km_trasy": pods["km_trasy"],
        # Dojazd baza->odbiór. LICZBA POMOCNICZA, pod własną etykietą i tylko
        # pod nią. Nie wolno jej pokazać w miejscu długości kursu — dokładnie
        # to podstawienie kazało panelowi wyświetlić „60 km" przy kursie
        # Dębica->Turek, którego drugiego końca nie rozpoznaliśmy.
        "km_od_bazy": pods["km_od_bazy"],
        "szacunek_pln": pods["szacunek_pln"],
        # Odległość z treści posta („trasa ma około 490 km"). Osobne pole, bo
        # to CUDZA liczba i na ekranie ma być oznaczona jako cudza.
        "km_wg_autora": pods["km_wg_autora"],
        "link_mapy": pods["link_trasa"],
        "link_nawigacji": pods["link_nawigacja"],
        # Panel rysuje nad kilometrami wyraźny pasek ostrzegawczy, gdy punkt jest
        # niepewny albo nierozpoznany — i pokazuje w nim `odbior_raw`, czyli to,
        # co REALNIE stało w poście. Bez surowej treści ostrzeżenie mówi „nie
        # ufaj", nie mówiąc czemu, a operator nie ma jak sam rozstrzygnąć.
        "lokalizacja_zrodlo": odbior.zrodlo if odbior else "brak",
        # Ten sam znacznik dla DRUGIEGO końca trasy. Bez niego zlecenie
        # z rozpoznanym odbiorem i nierozpoznaną dostawą nie dostawało w panelu
        # żadnego ostrzeżenia — a to jest właśnie ten przypadek, w którym
        # kilometry znikają i trzeba powiedzieć dlaczego.
        "dostawa_zrodlo": dostawa.zrodlo if dostawa else "brak",
        "lokalizacja_niepewne": pods["niepewne"],
        # Pinezka na mapie. `null` przy nierozpoznanym miejscu — panel pokazuje
        # takie zlecenia listą pod mapą, z powodem, zamiast stawiać pinezkę
        # „gdzieś", nieodróżnialną wzrokowo od pewnej.
        "lat": odbior.lat if odbior else None,
        "lng": odbior.lng if odbior else None,
    })
    return dane


@router.get("")
def lista(
    status: str | None = Query(default="nowe",
                               description="stan obsługi; 'wszystkie' znosi filtr"),
    od: date | None = Query(default=None, description="posty od tej daty (publikacji)"),
    do: date | None = Query(default=None, description="posty do tej daty włącznie"),
    max_km: int | None = Query(default=None, ge=0,
                               description="filtr operatora, NIE próg systemu"),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    """Lista zleceń, najnowsze u góry.

    Domyślnie `czy_zlecenie = true` i `status = 'nowe'` — czyli kolejka do
    obsłużenia. Sortowanie po `opublikowany_at DESC NULLS LAST`: post bez daty
    publikacji (Apify nie zawsze ją oddaje) ląduje NA KOŃCU, bo góra listy należy
    do najświeższych, a nie do tych o nieznanym wieku.

    TRANSPORT ZWIERZĄT LĄDUJE NIŻEJ, ale ZOSTAJE NA LIŚCIE — to jest cała
    różnica między „nie wożę koni" a „nie chcę o tym wiedzieć", i tylko pierwsze
    wolno tu zakodować. Kolejność jest podpowiedzią, nie filtrem: zlecenie widać
    w każdym widoku, wchodzi w `limit` i daje się otworzyć jak każde inne.

    `max_km` filtruje PO policzeniu geografii, w Pythonie — kilometrów nie ma
    w bazie i nie ma ich tam z premedytacją: zależą od `BAZA_LAT/BAZA_LON`, które
    operator może zmienić (przeprowadzka, druga baza), a kolumna z kilometrami
    stałaby się wtedy cicho nieprawdziwa dla całej historii.
    """
    warunki = ["czy_zlecenie"]
    parametry: list = []
    if status and status != "wszystkie":
        if status not in STATUSY:
            raise HTTPException(400, f"nieznany status {status!r}; dozwolone: "
                                     f"{', '.join(STATUSY)}, wszystkie")
        warunki.append("status = %s")
        parametry.append(status)
    if od:
        warunki.append("COALESCE(opublikowany_at, pobrany_at) >= %s")
        parametry.append(od)
    if do:
        warunki.append("COALESCE(opublikowany_at, pobrany_at) < %s::date + 1")
        parametry.append(do)

    # Przy aktywnym `max_km` bierzemy z bazy zapas, bo odsiew dzieje się dopiero
    # po policzeniu geografii — inaczej filtr „do 50 km" przy limicie 50 potrafiłby
    # zwrócić trzy rekordy i wyglądać jak pusta baza.
    zapas = min(limit * 5, 1000) if max_km is not None else limit

    with db.polaczenie() as conn:
        _sprawdz_migracje(conn)
        with conn.cursor() as cur:
            cur.execute(
                # `tresc` jest tu do POLICZENIA odległości podanej przez autora
                # posta, nie do zwrócenia — `_rekord(zwroc_tresc=False)`
                # wyrzuca ją z odpowiedzi. Powód, dla którego nie ma jej
                # w `POLA_LISTY`, dotyczył wielkości odpowiedzi na LTE,
                # a nie odczytu z Postgresa.
                f"SELECT {', '.join(POLA_LISTY)}, tresc FROM posty "  # noqa: S608 — lista stała
                f" WHERE {' AND '.join(warunki)}"
                # COALESCE, a nie samo porównanie: `kategoria_ladunku` jest NULL
                # dla wierszy sprzed migracji 0010, a NULL w ORDER BY ... ASC
                # idzie na KONIEC — czyli cała historia wylądowałaby pod
                # zwierzętami. NULL znaczy „bramka nie orzekała" i ma się
                # zachowywać jak zwykłe zlecenie.
                "  ORDER BY (COALESCE(kategoria_ladunku, '') = 'zwierze') ASC,"
                "           opublikowany_at DESC NULLS LAST, pobrany_at DESC "
                "  LIMIT %s",
                (*parametry, zapas),
            )
            wiersze = cur.fetchall()

    rekordy = [_rekord(w, zwroc_tresc=False) for w in wiersze]
    if max_km is not None:
        # Rekord z nieznanymi kilometrami ZOSTAJE. Nie wiemy, czy jest bliżej czy
        # dalej niż próg, a ukrycie go znaczyłoby, że nierozpoznana nazwa miasta
        # kasuje zlecenie — czyli dokładnie to, czego ten system robić nie może.
        rekordy = [r for r in rekordy
                   if r["km_od_bazy"] is None or r["km_od_bazy"] <= max_km]
    return {"zlecen": len(rekordy), "zlecenia": rekordy[:limit]}


@router.get("/{fb_id}")
def szczegol(fb_id: str) -> dict:
    """Jedno zlecenie z PEŁNĄ treścią posta.

    `tresc` jest tu, a nie na liście, świadomie: ekran szczegółu ma pokazywać
    ORYGINAŁ, nie streszczenie modelu, bo to jedyne miejsce, w którym da się
    zweryfikować, czy klasyfikator czegoś nie przekręcił.
    """
    with db.polaczenie() as conn:
        _sprawdz_migracje(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(POLA_LISTY)}, tresc, autor, gate_powod, "  # noqa: S608
                "       zrodlo_decyzji, ai_at, ai_model, "
                # Werdykt modelu ma w tym repo JEDNO źródło: `czy_zlecenie`
                # przy `zrodlo_decyzji='ai'`. Panel dostaje je pod dotychczasową
                # nazwą, ale liczone z pary — NULL znaczy „modelu nie pytano",
                # a nie „model powiedział nie".
                "       CASE WHEN zrodlo_decyzji = 'ai' THEN czy_zlecenie END "
                "            AS ai_zlecenie "
                "  FROM posty WHERE fb_id = %s", (fb_id,))
            wiersz = cur.fetchone()
    if wiersz is None:
        raise HTTPException(404, "nie ma takiego zlecenia")
    return _rekord(wiersz)


@router.patch("/{fb_id}")
def zmien(fb_id: str, zmiana: Zmiana) -> dict:
    """Zmiana statusu, notatki i ceny końcowej.

    ZMIANA NA 'smiec' DOPISUJE WIERSZ DO `feedback`. To jest ta sama pętla
    zwrotna co przycisk „Śmieć" pod powiadomieniem (`workers/bot.py`) i dlatego
    obie ścieżki wołają `services/feedback.zapisz` — kliknięcie w panelu i pod
    alertem znaczą to samo i mają zostawiać ten sam materiał do poprawiania
    promptu.

    `cena_koncowa` przy 'wygrane' jest jedyną liczbą w tym systemie, która mówi,
    ile ten system realnie zarobił. Szacunek z `geo.szacunek_pln` jest zgadywaniem
    i nie ma prawa jej zastąpić.
    """
    pola: list[str] = []
    parametry: list = []

    if zmiana.status is not None:
        if zmiana.status not in STATUSY:
            raise HTTPException(400, f"nieznany status {zmiana.status!r}; "
                                     f"dozwolone: {', '.join(STATUSY)}")
        pola += ["status = %s", "status_at = NOW()"]
        parametry.append(zmiana.status)
    if zmiana.notatka is not None:
        pola.append("notatka = %s")
        parametry.append(zmiana.notatka)
    if zmiana.cena_koncowa is not None:
        pola.append("cena_koncowa = %s")
        parametry.append(zmiana.cena_koncowa)

    if not pola:
        raise HTTPException(400, "nic do zmiany — podaj status, notatkę albo cenę")

    with db.polaczenie() as conn:
        _sprawdz_migracje(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE posty SET {', '.join(pola)} "  # noqa: S608 — lista pól stała
                " WHERE fb_id = %s RETURNING fb_id, status, notatka, cena_koncowa",
                (*parametry, fb_id))
            wiersz = cur.fetchone()
            if wiersz is None:
                raise HTTPException(404, "nie ma takiego zlecenia")
        conn.commit()

        if zmiana.status == "smiec":
            # Wynik świadomie ignorowany: brak wiersza treningowego jest tańszy
            # niż zlecenie, które zostało w kolejce, bo zapis feedbacku padł.
            feedback.zapisz(conn, fb_id, "smiec")
        elif zmiana.status == "wygrane":
            feedback.zapisz(conn, fb_id, "dobre")

    return dict(wiersz)


def _sprawdz_migracje(conn) -> None:
    """Zamień `UndefinedColumn` na zdanie, które mówi, co zrobić.

    Repo ma zasadę, że żaden worker nie tworzy tabel — więc stan „baza jest,
    migracji 0004 nie ma" jest normalnym etapem wdrożenia, a nie awarią. Bez tego
    sprawdzenia pierwszy deploy kończy się piątką i pytaniem „czemu panel nie
    działa" zamiast jedną linijką z nazwą pliku do odpalenia.
    """
    kolumny = db.kolumny(conn, "posty")
    for potrzebne, migracja in (
        ({"odbior_miasto", "pojazd_opis", "pewnosc"}, "0004_klasyfikacja.sql"),
        ({"notatka", "cena_koncowa", "status_at"}, "0005_panel.sql"),
        ({"kategoria_ladunku"}, "0010_kategoria_ladunku.sql"),
        ({"kierunek"}, "0011_kierunek.sql"),
    ):
        brak = potrzebne - kolumny
        if brak:
            raise db.BazaNiedostepna(
                f"brakuje kolumn {', '.join(sorted(brak))} — odpal migrację "
                f"laweta_radar/api/migrations/{migracja} (scripts/migrate.sh)")
