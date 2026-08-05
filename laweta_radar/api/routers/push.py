"""`/push` — zapis przeglądarki na web push. Drugi kanał obok Telegrama.

TRZY ENDPOINTY I ANI JEDNEGO WIĘCEJ: oddaj klucz publiczny, zapisz subskrypcję,
skasuj subskrypcję. Cała logika wysyłki siedzi w `services/powiadomienia.py`,
razem z dedupem i limitami — push, który omijałby antyspam, znaczyłby dwa
kanały z dwoma różnymi zestawami reguł i pewność, że jeden z nich zacznie
brzęczeć bez sensu.

KLUCZ PUBLICZNY VAPID JEST JAWNY z definicji (przeglądarka wysyła go dostawcy),
więc oddanie go endpointem nie jest wyciekiem. PRYWATNY nie wychodzi stąd
nigdy — leży w `.env` i używa go wyłącznie wysyłka.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from laweta_radar.api import db
from laweta_radar.api.auth import wymagaj_tokenu
from laweta_radar.config import settings

router = APIRouter(prefix="/push", tags=["push"],
                   dependencies=[Depends(wymagaj_tokenu)])


class Klucze(BaseModel):
    p256dh: str
    auth: str


class Subskrypcja(BaseModel):
    """Kształt obiektu z `PushSubscription.toJSON()` w przeglądarce.

    Przyjmujemy go 1:1, bez przepakowywania po stronie panelu — każde
    przepakowanie to okazja, żeby zgubić pole, a błąd objawi się dopiero przy
    pierwszej próbie wysyłki, czyli przy pierwszym realnym zleceniu.
    """

    endpoint: str
    keys: Klucze


@router.get("/klucz")
def klucz_publiczny() -> dict:
    """Klucz VAPID dla `pushManager.subscribe`. Pusty = push nieskonfigurowany.

    Zwracamy 200 z pustym kluczem, a nie 404: panel ma pokazać zdanie „serwer
    nie ma kluczy VAPID" zamiast błędu sieci, bo to jest stan konfiguracji,
    a nie awaria.
    """
    return {"klucz": settings.VAPID_PUBLIC_KEY}


@router.post("/subskrypcja", status_code=201)
def zapisz(sub: Subskrypcja, user_agent: str = Header(default="")) -> dict:
    """Zapisz albo odśwież subskrypcję tej przeglądarki."""
    if not settings.VAPID_PRIVATE_KEY:
        raise HTTPException(503, "brak kluczy VAPID w .env — push wyłączony")
    with db.polaczenie() as conn:
        if not db.tabela_istnieje(conn, "push_subskrypcje"):
            raise db.BazaNiedostepna("brak tabeli `push_subskrypcje` — odpal "
                                     "migrację 0007_push.sql")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO push_subskrypcje (endpoint, p256dh, auth, urzadzenie)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (endpoint) DO UPDATE
                   SET p256dh = EXCLUDED.p256dh,
                       auth   = EXCLUDED.auth,
                       -- Ponowny zapis KASUJE ostatni błąd: to jest ta sama
                       -- przeglądarka zgłaszająca się od nowa, więc poprzednia
                       -- diagnoza „martwa" przestała być prawdziwa.
                       ostatni_blad = NULL
                """,
                (sub.endpoint, sub.keys.p256dh, sub.keys.auth, user_agent[:200]),
            )
        conn.commit()
    return {"zapisano": True}


@router.delete("/subskrypcja")
def usun(sub: Subskrypcja) -> dict:
    with db.polaczenie() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM push_subskrypcje WHERE endpoint = %s",
                        (sub.endpoint,))
        conn.commit()
    return {"usunieto": True}
