"""Autoryzacja API — jeden token w nagłówku i ani warstwy więcej.

UŻYTKOWNIK JEST JEDEN. Nie ma ról, nie ma sesji, nie ma odświeżania tokenu, nie
ma logowania. System ról dla jednej osoby to kod, który potrafi się zepsuć, i zero
bezpieczeństwa więcej — a każdy dodatkowy ekran między operatorem a listą zleceń
kosztuje sekundy dokładnie wtedy, gdy ich nie ma (na postoju, jedną ręką).

CO REALNIE CHRONI TEN TOKEN: adres panelu jest publiczny (PWA musi być dostępna
z telefonu przez internet), a lista zleceń zawiera numery telefonów obcych ludzi
z grup FB. To są cudze dane osobowe i nie mogą wisieć pod gołym URL-em.

BRAK TOKENU W KONFIGURACJI = 503 NA WSZYSTKICH ENDPOINTACH Z DANYMI, nie 200
z pustą listą i nie otwarte API. Puste `API_TOKEN` znaczy „konfiguracji jeszcze
nie dokończono", a nie „wpuszczaj każdego" — i to jest jedyna interpretacja,
przy której literówka w `.env` nie kończy się otwartą bazą numerów telefonów.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from laweta_radar.config import settings


def wymagaj_tokenu(x_token: str = Header(default="")) -> None:
    """Zależność FastAPI. Wpuszcza albo rzuca 401/503.

    `hmac.compare_digest` zamiast `==` — porównanie stringów w Pythonie kończy się
    na pierwszym różnym bajcie, więc czas odpowiedzi zdradza, ile znaków tokenu
    zgadł atakujący. Przy jednym użytkowniku i tokenie z `secrets.token_urlsafe`
    to jest ostrożność teoretyczna, ale kosztuje jedno wywołanie funkcji.
    """
    if not settings.API_TOKEN:
        raise HTTPException(
            status_code=503,
            detail=("API_TOKEN nie jest ustawiony w .env — API z danymi jest "
                    "wyłączone. Wygeneruj token: "
                    "python -c \"import secrets;print(secrets.token_urlsafe(32))\""),
        )
    if not x_token or not hmac.compare_digest(x_token, settings.API_TOKEN):
        # Bez podpowiedzi „zły token" vs „brak tokenu" — jedno i drugie znaczy
        # dla wołającego dokładnie to samo, a różnica przydaje się wyłącznie
        # temu, kto zgaduje.
        raise HTTPException(status_code=401, detail="nieprawidłowy token")
