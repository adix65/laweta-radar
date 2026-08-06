"""
Saldo konta Apify — ile z miesięcznego kredytu już poszło i ile zostało.

PO CO TO ISTNIEJE: rotacja kluczy (laweta_radar/workers/apify_keys.py) jest świadomie
REAKTYWNA — nie sprawdza salda z góry, tylko próbuje i przeskakuje na błędzie
wyczerpania. To dobra strategia dla runów produkcyjnych i zła dla POMIARU: żeby
policzyć, ile realnie kosztuje jeden pobrany post, trzeba odczytać stan licznika
PRZED i PO. Ten moduł robi dokładnie to i nic więcej — nie steruje rotacją i nie
podejmuje decyzji „jechać czy nie".

ODCZYT IDZIE PRZEZ PROXY TEGO KLUCZA (client_for_token), nie wprost z VPS-a. To nie
jest kosmetyka: sprawdzenie salda to zapytanie do api.apify.com jak każde inne, a
narzędzie odpytujące po kolei WSZYSTKIE konta z jednego adresu jest dla Apify
najczystszym możliwym sygnałem multi-accountingu — dokładnie tym, przed którym broni
laweta_radar/workers/apify_proxy.py. Odczyt salda ma być tańszy niż run actora, a nie
groźniejszy od niego.

ŹRÓDŁO: GET /v2/users/me/limits — pokazuje zużycie w BIEŻĄCYM cyklu miesięcznym
(`current.monthlyUsageUsd`) i limit konta (`limits.maxMonthlyUsageUsd`). Konta darmowe
mają limit ~5 USD/mies. Interesuje nas RÓŻNICA dwóch odczytów, więc jednostka i tak
się skraca — limit służy tylko do powiedzenia, ile jeszcze zostało.

DLACZEGO SZUKAMY KLUCZY REKURENCYJNIE, a nie po sztywnej ścieżce data.current.*:
kształt tej odpowiedzi to nie jest część kontraktu, na którym warto opierać liczbę
wchodzącą do decyzji „ile kont / czy płatny plan". Gdy Apify przestawi pole o poziom,
sztywna ścieżka po cichu zwróciłaby None i pomiar pokazałby koszt 0 USD za post —
błąd, który wygląda jak świetna wiadomość. Szukanie po NAZWIE klucza przeżywa
przestawienie zagnieżdżenia, a gdy nazwa naprawdę zniknie, mówimy o tym wprost.

UŻYCIE:
    from laweta_radar.workers.apify_credits import saldo
    s = saldo(token)                      # przez proxy przypisane temu kluczowi
    print(s.uzyte_usd, s.zostalo_usd)

    s2 = saldo(token)
    print(f"ten run kosztował {s2.uzyte_usd - s.uzyte_usd:.4f} USD")

Podgląd salda całej puli (WYMAGA SIECI — po jednym zapytaniu na konto):
    python -m laweta_radar.workers.apify_credits
    python -m laweta_radar.workers.apify_credits --limit 5
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from laweta_radar.workers.apify_proxy import client_for_token, load_proxy_config

_ENDPOINT = "https://api.apify.com/v2/users/me/limits"
_TIMEOUT_S = 30.0

# Nazwy pól w odpowiedzi, w kolejności preferencji. Pierwsza znaleziona wygrywa.
_POLA_UZYCIA = ("monthlyUsageUsd", "monthlyUsageUsdWithoutDiscounts", "currentUsageUsd")
_POLA_LIMITU = ("maxMonthlyUsageUsd", "monthlyUsageCreditsUsd")


class SaldoNieznane(RuntimeError):
    """Odpowiedź Apify przyszła, ale nie ma w niej licznika zużycia.

    Osobny wyjątek, bo to INNA sytuacja niż martwy klucz: konto żyje, tylko API
    zmieniło kształt. Rotacja kluczy nie ma tu czego naprawiać — przeskok na
    następny klucz da to samo, a pomiar musi się zatrzymać i powiedzieć operatorowi
    prawdę, zamiast policzyć koszt z brakujących danych.
    """


@dataclass(frozen=True)
class Saldo:
    """Stan licznika jednego konta Apify w bieżącym cyklu miesięcznym."""

    uzyte_usd: float                 # ile z kredytu poszło od początku cyklu
    limit_usd: float | None          # limit konta; None, gdy API go nie podało
    cykl_od: str = ""                # ISO 8601 — początek cyklu rozliczeniowego
    cykl_do: str = ""                # ISO 8601 — koniec cyklu (wtedy licznik siada)

    @property
    def zostalo_usd(self) -> float | None:
        """Ile kredytu jeszcze zostało; None, gdy limit nieznany."""
        return None if self.limit_usd is None else self.limit_usd - self.uzyte_usd

    def opis(self) -> str:
        """Jedna linia do logu — bez tokenu, bo ten dokłada wołający."""
        if self.limit_usd is None:
            return f"użyte {self.uzyte_usd:.4f} USD (limit nieznany)"
        return (f"użyte {self.uzyte_usd:.4f} / {self.limit_usd:.2f} USD "
                f"(zostało {self.zostalo_usd:.4f})")


def _znajdz_liczbe(obiekt, nazwy: tuple[str, ...]) -> float | None:
    """Pierwsza liczba spod którejkolwiek z `nazw`, na dowolnej głębokości JSON-a.

    Przechodzimy wszerz (kolejka), a nie wgłąb, bo pola, których szukamy, siedzą
    płytko (data.current.*, data.limits.*) — a przy przejściu wgłąb pierwszy trafiony
    klucz o tej nazwie mógłby pochodzić z jakiejś zagnieżdżonej rozpiski szczegółowej.
    """
    kolejka = [obiekt]
    while kolejka:
        wezel = kolejka.pop(0)
        if isinstance(wezel, dict):
            for nazwa in nazwy:
                wartosc = wezel.get(nazwa)
                if isinstance(wartosc, (int, float)) and not isinstance(wartosc, bool):
                    return float(wartosc)
            kolejka.extend(wezel.values())
        elif isinstance(wezel, list):
            kolejka.extend(wezel)
    return None


def _znajdz_tekst(obiekt, nazwa: str) -> str:
    """Pierwszy napis spod klucza `nazwa` (te same zasady co `_znajdz_liczbe`)."""
    kolejka = [obiekt]
    while kolejka:
        wezel = kolejka.pop(0)
        if isinstance(wezel, dict):
            wartosc = wezel.get(nazwa)
            if isinstance(wartosc, str) and wartosc.strip():
                return wartosc
            kolejka.extend(wezel.values())
        elif isinstance(wezel, list):
            kolejka.extend(wezel)
    return ""


def z_odpowiedzi(dane) -> Saldo:
    """Zbuduj `Saldo` z rozpakowanego JSON-a /v2/users/me/limits.

    Wydzielone z `saldo()`, żeby dało się przetestować BEZ sieci — cała wiedza
    o kształcie odpowiedzi Apify siedzi tutaj.
    """
    uzyte = _znajdz_liczbe(dane, _POLA_UZYCIA)
    if uzyte is None:
        raise SaldoNieznane(
            f"w odpowiedzi {_ENDPOINT} nie ma żadnego z pól {', '.join(_POLA_UZYCIA)} — "
            f"Apify zmieniło kształt odpowiedzi; bez licznika zużycia NIE da się policzyć "
            f"kosztu i nie zgaduję zera"
        )
    return Saldo(
        uzyte_usd=uzyte,
        limit_usd=_znajdz_liczbe(dane, _POLA_LIMITU),
        cykl_od=_znajdz_tekst(dane, "startAt"),
        cykl_do=_znajdz_tekst(dane, "endAt"),
    )


def saldo(token: str, *, timeout: float = _TIMEOUT_S, env=None, cfg=None) -> Saldo:
    """Saldo konta stojącego za tym tokenem. Ruch idzie przez proxy TEGO klucza.

    Błędy przepuszczamy wyżej BEZ tłumaczenia: wołający (rotacja kluczy albo pomiar)
    ma je zaklasyfikować sam — 401/402 tutaj znaczy dokładnie to samo co przy runie
    actora, czyli „ten klucz jest do wymiany", i ma trafić do
    `classify_apify_error` w laweta_radar/workers/apify_keys.py w oryginalnej postaci.
    """
    with client_for_token(token, timeout=timeout, env=env, cfg=cfg) as klient:
        odp = klient.get(_ENDPOINT, headers={"Authorization": f"Bearer {token}"})
        odp.raise_for_status()
        return z_odpowiedzi(odp.json())


# ---------------------------------------------------------------------------
# Stan konta dla `/limity` — TRZY rozłączne sytuacje, nie dwie.
#
# PO CO TO ISTNIEJE OSOBNO OD `saldo()`. `saldo()` zakłada, że /users/me/limits
# odpowie i rzuca, gdy się myli — dobre dla pomiaru jednego konta, złe dla
# przeglądu całej puli: `/limity` ma pokazać pięć kont naraz i NIE WOLNO mu
# pomylić „konto martwe" z „konto żywe, ale bez licznika". To dokładnie ten
# błąd, który wywołał to zadanie — pięć kont z 401 na /limits wyglądało w starym
# przeglądzie identycznie jak awaria całej puli, a fetcher tymi samymi kluczami
# normalnie pobierał posty.
#
# ROZSTRZYGA KOLEJNOŚĆ DWÓCH ZAPYTAŃ:
#   1. GET /v2/users/me — najlżejszy endpoint, jaki Apify ma. Odpowiada
#      KAŻDEMU ważnemu tokenowi, niezależnie od planu. 401 tutaj naprawdę
#      znaczy „klucz martwy" (unieważniony/odwołany token, zwykle ban).
#   2. GET /v2/users/me/limits — TYLKO gdy (1) się udało. Niepowodzenie TEGO
#      zapytania (401, 403, 404, zmieniony kształt odpowiedzi — cokolwiek) NIE
#      degraduje konta do „martwe": konto już udowodniło, że żyje, więc brak
#      salda jest tylko brakiem salda. Darmowe konta Apify bywają odcięte od
#      tego endpointu — to jest normalny, sprawny stan, nie błąd.
# ---------------------------------------------------------------------------
_ENDPOINT_ME = "https://api.apify.com/v2/users/me"

# Stany `StanKonta.stan` — cztery, bo timeout/awaria transportu to INNA sytuacja
# niż martwy klucz (401), a WYDAJNOSC wymaga ich osobnego pokazania w /limity.
STAN_OK_ZNANE = "ok_saldo_znane"
STAN_OK_NIEZNANE = "ok_saldo_nieznane"
STAN_MARTWY = "martwy"
STAN_BRAK_ODPOWIEDZI = "brak_odpowiedzi"


@dataclass(frozen=True)
class StanKonta:
    """Stan jednego konta z puli — do przeglądu (`/limity`), nie do pomiaru.

    `nazwa` to `username` z /users/me — WYŁĄCZNIE to wolno pokazać operatorowi
    na Telegramie (grupa może mieć więcej niż jedną osobę). Token, jego skrót
    i cokolwiek z niego wyliczonego NIE mają tu prawa się pojawić.
    """

    nazwa: str                  # username z /users/me; "" gdy nieznane/martwe
    stan: str                   # STAN_OK_ZNANE | STAN_OK_NIEZNANE | STAN_MARTWY | STAN_BRAK_ODPOWIEDZI
    saldo: Saldo | None = None  # tylko przy STAN_OK_ZNANE
    powod: str = ""             # dla martwy/brak_odpowiedzi — BEZ treści tokenu


def _bez_tokenu(exc: BaseException, token: str = "") -> str:
    """Opis wyjątku bezpieczny do pokazania na Telegramie.

    Token normalnie NIE wchodzi do komunikatów httpx (leci w nagłówku, nie
    w URL-u), ale wynik trafia na czat, na którym może być więcej niż jedna
    osoba — więc usuwamy go z treści JAWNIE, na wszelki wypadek biblioteki,
    która kiedyś zacznie go dorzucać do wyjątku transportu. Przycinamy do 160
    znaków z tego samego powodu co `mask_url`: komunikat proxy bywa długi.
    """
    tekst = str(exc)[:160]
    if token:
        tekst = tekst.replace(token, "***")
    return f"{type(exc).__name__}: {tekst}"


def stan_konta(token: str, *, timeout: float = 10.0, env=None, cfg=None) -> StanKonta:
    """Stan jednego konta: żywe+saldo, żywe+bez salda, martwe albo brak odpowiedzi.

    Ruch idzie przez proxy TEGO klucza (`client_for_token`), tak jak wszędzie
    indziej w tym module — przegląd całej puli z jednego adresu byłby dla Apify
    dokładnie tym sygnałem multi-accountingu, przed którym broni apify_proxy.py.
    """
    try:
        with client_for_token(token, timeout=timeout, env=env, cfg=cfg) as klient:
            odp = klient.get(_ENDPOINT_ME, headers={"Authorization": f"Bearer {token}"})
    except Exception as e:  # noqa: BLE001 — timeout/sieć/proxy: patrz nagłówek sekcji
        return StanKonta("", STAN_BRAK_ODPOWIEDZI, powod=_bez_tokenu(e, token))

    if odp.status_code == 401:
        return StanKonta("", STAN_MARTWY, powod="401 — klucz nie działa")
    try:
        odp.raise_for_status()
    except Exception as e:  # noqa: BLE001 — 403/404/5xx na /users/me: nie wiadomo, traktuj jak brak odpowiedzi
        return StanKonta("", STAN_BRAK_ODPOWIEDZI, powod=_bez_tokenu(e, token))

    try:
        nazwa = _znajdz_tekst(odp.json(), "username") or "?"
    except Exception:  # noqa: BLE001 — odpowiedź bez poprawnego JSON-a nie jest martwym kluczem
        nazwa = "?"

    try:
        s = saldo(token, timeout=timeout, env=env, cfg=cfg)
    except Exception as e:  # noqa: BLE001 — KONTO JUŻ ŻYJE (odpowiedziało wyżej) —
        # brak salda go nie zabija, patrz nagłówek sekcji.
        return StanKonta(nazwa, STAN_OK_NIEZNANE, powod=_bez_tokenu(e, token))
    return StanKonta(nazwa, STAN_OK_ZNANE, saldo=s)


# Pojedynczy slot cache'u: `/limity` odpytuje ZAWSZE całą bieżącą pulę, więc
# jeden slot kluczowany odciskiem tokenów wystarcza — kilka `/limity` pod rząd
# (albo alias, albo dwie osoby w grupie) nie generuje lawiny zapytań do Apify.
# Odcisk, NIE surowe tokeny, żeby nie trzymać ich drugi raz w pamięci procesu
# dłużej, niż to konieczne.
_CACHE_TTL_S = 300.0
_cache: dict[str, tuple[float, list[StanKonta]]] = {}


def _odcisk(tokens: list[str]) -> str:
    return hashlib.sha256("|".join(tokens).encode("utf-8")).hexdigest()


def pula_stanu(tokens: list[str], *, timeout: float = 10.0,
               cache_ttl: float = _CACHE_TTL_S, env=None, cfg=None,
               _teraz=None) -> list[StanKonta]:
    """Stan WSZYSTKICH kont naraz, RÓWNOLEGLE, z cache na `cache_ttl` sekund.

    Równolegle, bo pięć kont to pięć wywołań HTTP — sekwencyjnie to nawet przy
    krótkim timeoucie kilkadziesiąt sekund czekania na odpowiedź Telegrama.
    Cache chroni przed lawiną: kilka `/limity` pod rząd (albo alias) w oknie
    `cache_ttl` oddaje ten sam, już policzony wynik zamiast pytać Apify znowu.
    """
    import time  # noqa: PLC0415 — moduł ma zostać lekki przy imporcie z workera
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    teraz = _teraz() if _teraz else time.monotonic()
    klucz = _odcisk(tokens)
    wpis = _cache.get(klucz)
    if wpis is not None and (teraz - wpis[0]) < cache_ttl:
        return wpis[1]

    if not tokens:
        wyniki: list[StanKonta] = []
    else:
        cfg_wspolny = load_proxy_config(env) if cfg is None else cfg
        with ThreadPoolExecutor(max_workers=min(16, len(tokens))) as pool:
            wyniki = list(pool.map(
                lambda t: stan_konta(t, timeout=timeout, env=env, cfg=cfg_wspolny), tokens))
    _cache[klucz] = (teraz, wyniki)
    return wyniki


# ---------------------------------------------------------------------------
# CLI — saldo całej puli (WYMAGA SIECI)
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    import argparse  # noqa: PLC0415 — moduł ma zostać lekki przy imporcie z workera

    ap = argparse.ArgumentParser(
        description="Saldo miesięcznego kredytu kont Apify z puli (WYMAGA SIECI — "
                    "po jednym zapytaniu na konto, przez proxy tego konta)."
    )
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="sprawdź tylko N pierwszych kluczy (przy dużej puli)")
    ap.add_argument("--timeout", type=float, default=_TIMEOUT_S,
                    help=f"timeout pojedynczego odczytu w sekundach (domyślnie {_TIMEOUT_S:g})")
    args = ap.parse_args(argv[1:])

    # Import pakietu dociąga wspólny .env sales-core-engine (config/shared_env.py),
    # więc CLI widzi tę samą pulę kont co cron.
    from laweta_radar.workers.apify_keys import _mask, load_apify_tokens  # noqa: PLC0415

    tokeny = load_apify_tokens()
    if not tokeny:
        print("Brak kluczy APIFY_API_TOKEN* w środowisku — nie ma czego pytać o saldo.")
        return 0
    if args.limit is not None:
        tokeny = tokeny[:max(0, args.limit)]

    razem_uzyte = razem_zostalo = 0.0
    bledy = 0
    print(f"Sprawdzam saldo {len(tokeny)} kont ...\n")
    for i, token in enumerate(tokeny, 1):
        try:
            s = saldo(token, timeout=args.timeout)
        except Exception as e:  # noqa: BLE001 — jedno padnięte konto nie kończy przeglądu
            bledy += 1
            print(f"  #{i:<3} {_mask(token)}  BŁĄD  {type(e).__name__}: {e}")
            continue
        razem_uzyte += s.uzyte_usd
        if s.zostalo_usd is not None:
            razem_zostalo += s.zostalo_usd
        print(f"  #{i:<3} {_mask(token)}  {s.opis()}")

    print(f"\n=== Razem: użyte {razem_uzyte:.2f} USD, zostało {razem_zostalo:.2f} USD, "
          f"{bledy} błędów ===")
    if bledy:
        print("UWAGA: konta z błędem mogą być martwe albo mieć zepsute proxy — "
              "sprawdź: python -m laweta_radar.workers.apify_proxy --check")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
