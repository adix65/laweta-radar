"""`/zlecenia` — lista, szczegół i zmiana statusu. To jest cały panel.

FRONTEND NIE LICZY NICZEGO. Każdy rekord wychodzi stąd z policzonymi
`km_od_bazy`, `szacunek_pln`, `link_mapy` i `link_nawigacji`. Powód nie jest
estetyczny: te same liczby pokazuje powiadomienie na Telegramie (`services/
powiadomienia.py`) i obie ścieżki liczą je tym samym kodem (`services/geo.py`).
Gdyby panel liczył po swojemu, operator zobaczyłby w alercie 42 km, a w aplikacji
48 km i przestałby ufać obu.

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
              "ai_json", "ai_pewnosc", "ai_pilnosc", "telefon", "notatka",
              "cena_koncowa")


class Zmiana(BaseModel):
    """Ciało PATCH-a. Wszystkie pola opcjonalne — panel wysyła to, co zmienił.

    `None` znaczy „nie ruszaj", a nie „wyczyść". Rozróżnienie jest istotne przy
    notatce: operator dopisujący cenę nie ma stracić tekstu wpisanego minutę
    wcześniej tylko dlatego, że formularz wysłał komplet pól.
    """

    status: str | None = Field(default=None)
    notatka: str | None = Field(default=None, max_length=4000)
    cena_koncowa: float | None = Field(default=None, ge=0, le=1_000_000)


def _rekord(wiersz: dict) -> dict:
    """Wiersz z `posty` -> rekord dla panelu, z policzoną geografią.

    `ai_json` rozpakowujemy NA PŁASKO obok kolumn, a nie zagnieżdżamy — panel ma
    czytać `zlecenie.pojazd`, a nie `zlecenie.ai.pojazd`, bo połowa tych pól i tak
    ma odpowiednik w kolumnie (telefon, pewnosc) i dwa źródła prawdy w jednym
    obiekcie kończą się pytaniem „które jest aktualne".
    """
    dane = dict(wiersz)
    ai = dane.pop("ai_json", None) or {}
    if isinstance(ai, dict):
        # Kolumny wygrywają z JSON-em: to one są aktualizowane migracjami
        # i indeksowane, a JSON jest zapisem tego, co model powiedział wtedy.
        for klucz, wartosc in ai.items():
            dane.setdefault(klucz, wartosc)

    dane.setdefault("pewnosc", dane.get("ai_pewnosc"))
    dane.setdefault("pilnosc", dane.get("ai_pilnosc"))
    dane.setdefault("jezyk", dane.get("gate_jezyk"))

    g = geo.opisz(dane)
    dane.update({
        "km_od_bazy": g.km_od_bazy,
        "km_trasy": g.km_trasy,
        "szacunek_pln": g.szacunek_pln,
        "link_mapy": g.link_mapy,
        "link_nawigacji": g.link_nawigacji,
        # Panel rysuje nad kilometrami wyraźny pasek ostrzegawczy, gdy to nie jest
        # 'miasto' — i pokazuje w nim `lokalizacja_surowa`, czyli to, co REALNIE
        # stało w poście. Bez surowej treści ostrzeżenie mówi „nie ufaj", nie
        # mówiąc czemu, a operator nie ma jak sam rozstrzygnąć.
        "lokalizacja_zrodlo": g.zrodlo,
        "lokalizacja_surowa": g.surowe_od,
        "miejsce_od": g.miejsce_od,
        "miejsce_do": g.miejsce_do,
        # Pinezka na mapie w panelu. `null` przy nierozpoznanym miejscu — panel
        # pokazuje takie zlecenia listą pod mapą, z powodem, zamiast stawiać
        # pinezkę „gdzieś", nieodróżnialną wzrokowo od pewnej.
        "lat": g.lat,
        "lon": g.lon,
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
                f"SELECT {', '.join(POLA_LISTY)} FROM posty "  # noqa: S608 — lista stała
                f" WHERE {' AND '.join(warunki)}"
                "  ORDER BY opublikowany_at DESC NULLS LAST, pobrany_at DESC "
                "  LIMIT %s",
                (*parametry, zapas),
            )
            wiersze = cur.fetchall()

    rekordy = [_rekord(w) for w in wiersze]
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
                "SELECT fb_id, tresc, autor, grupa_nazwa, grupa_url, post_url, "
                "       opublikowany_at, pobrany_at, status, status_at, stale, "
                "       gate_jezyk, gate_powod, zrodlo_decyzji, ai_json, "
                "       ai_pewnosc, ai_pilnosc, ai_at, telefon, notatka, cena_koncowa "
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
    brak = {"ai_json", "ai_pewnosc", "notatka", "status_at"} - db.kolumny(conn, "posty")
    if brak:
        raise db.BazaNiedostepna(
            f"brakuje kolumn {', '.join(sorted(brak))} — odpal migrację "
            "laweta_radar/api/migrations/0004_zlecenie.sql (scripts/migrate.sh)")
