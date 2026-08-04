"""Ile kredytu zużyło JEDNO konto Apify — minimalny odczyt, przez proxy tego konta.

DLACZEGO TO ISTNIEJE (i dlaczego jest takie małe): `docs/APIFY-PROXY.md` zapisało
świadomą decyzję, że monitora kredytów z repo źródłowego NIE przenosimy — worker
odpytujący saldo WSZYSTKICH kont naraz to dokładnie ten sygnał (dziesiątki kont,
jeden moment, jeden adres), przed którym broni cały `apify_proxy.py`. Ten moduł
nie jest tamtym monitorem: czyta saldo POJEDYNCZEGO tokenu, na żądanie wołającego,
i wychodzi przez proxy przypisane właśnie temu tokenowi.

DO CZEGO SŁUŻY: policzenie, ile realnie kosztuje jeden pobrany post. Cena ze strony
actora jest ceną katalogową — do decyzji „ile kont Apify potrzebujemy" wchodzi
liczba ZMIERZONA, bo różnica między 0,002 a 0,005 USD za post to różnica między
trzydziestoma kontami a dziewięciuset. Pierwszym konsumentem jest
`scripts/pomiar_actora.py`.

DWA ŹRÓDŁA KOSZTU I PO CO OBA:
  - `zuzycie(token)` — stan licznika konta (`GET /v2/users/me/limits`). Widzi
    WSZYSTKO, co konto wydało, także rzeczy, których nie widać w pojedynczym runie.
    Bywa jednak agregowany z opóźnieniem, więc różnica policzona tuż po runie
    potrafi być zaniżona.
  - `koszt_runu(run)` — `usageTotalUsd` z obiektu runu (patrz `apify_run.py`).
    Natychmiastowy i przypisany DOKŁADNIE do tego runu, ale pokazuje tylko to,
    co Apify zaksięgował na tym runie.
Zgodne wyniki znaczą, że pomiar jest wiarygodny. Rozjazd jest informacją, a nie
błędem — i dlatego pomiar raportuje obie liczby zamiast wybierać „tę ładniejszą".

BRAK TOKENU / BŁĄD ODCZYTU NIE JEST AWARIĄ. Funkcje oddają `None` albo rzucają
wyjątek HTTP do klasyfikacji przez `apify_keys.classify_apify_error` — decyzję,
czy to zatrzymuje robotę, podejmuje wołający (zasada 3 z README).

CLI (odpytuje sieć — jedno zapytanie na wskazany klucz):
    python -m laweta_radar.workers.apify_credits            # klucz #1
    python -m laweta_radar.workers.apify_credits --klucz 3  # klucz #3
    python -m laweta_radar.workers.apify_credits --raw      # surowy JSON z API
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from laweta_radar.workers.apify_keys import load_apify_tokens
from laweta_radar.workers.apify_proxy import client_for_token, proxy_label, proxy_for_token

API = "https://api.apify.com/v2"

# Odczyt salda to jedno małe zapytanie — długi timeout tylko przedłużałby czekanie
# na padnięte proxy. Sam run actora ma własny, znacznie dłuższy (apify_run.py).
TIMEOUT_S = 30.0


@dataclass(frozen=True)
class Zuzycie:
    """Stan licznika konta Apify w bieżącym cyklu rozliczeniowym.

    `zuzyte_usd` to liczba, o którą chodzi: różnica dwóch odczytów wokół serii
    runów jest kosztem tej serii. `limit_usd` mówi, ile w tym cyklu jeszcze
    zostało (na darmowym koncie zwykle 5 USD) — bez tego „zużyto 4,90" wygląda
    tak samo groźnie na koncie darmowym i na płatnym.
    """

    zuzyte_usd: float
    limit_usd: float | None
    cykl_od: str
    cykl_do: str

    @property
    def zostalo_usd(self) -> float | None:
        if self.limit_usd is None:
            return None
        return max(0.0, self.limit_usd - self.zuzyte_usd)

    def opis(self) -> str:
        """Jedna linia do logu — bez tokenu i bez czegokolwiek, co jest sekretem."""
        if self.limit_usd is None:
            return f"zużyto {self.zuzyte_usd:.4f} USD (limit nieznany)"
        return (f"zużyto {self.zuzyte_usd:.4f} / {self.limit_usd:.2f} USD "
                f"(zostało {self.zostalo_usd:.4f})")


def _float_or_none(value) -> float | None:
    """Liczba z odpowiedzi API albo None. API bywa zmienne — nie rzucamy na kształcie.

    Odczyt salda jest pomocniczy: gdy Apify zmieni nazwę pola, pomiar ma zgłosić
    „nie umiem odczytać salda" i policzyć koszt z runów, a nie wywalić się w połowie
    serii, za którą już zapłaciliśmy.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def z_odpowiedzi(dane: dict) -> Zuzycie | None:
    """Wyciągnij `Zuzycie` z ciała odpowiedzi `/users/me/limits`. None = nieznany kształt.

    Wydzielone z `zuzycie()`, żeby dało się to przetestować bez sieci — a kształt
    odpowiedzi Apify jest dokładnie tym, co najłatwiej zmieni się pod nami.
    """
    d = (dane or {}).get("data") or {}
    biezace = d.get("current") or {}
    limity = d.get("limits") or {}
    cykl = d.get("monthlyUsageCycle") or {}
    zuzyte = _float_or_none(biezace.get("monthlyUsageUsd"))
    if zuzyte is None:
        return None
    return Zuzycie(
        zuzyte_usd=zuzyte,
        limit_usd=_float_or_none(limity.get("maxMonthlyUsageUsd")),
        cykl_od=str(cykl.get("startAt") or ""),
        cykl_do=str(cykl.get("endAt") or ""),
    )


def zuzycie(token: str, *, timeout: float = TIMEOUT_S, env=None) -> Zuzycie | None:
    """Ile to konto zużyło w bieżącym cyklu. None, gdy odpowiedź ma nieznany kształt.

    Wychodzi przez proxy PRZYPISANE TEMU TOKENOWI (`client_for_token`), nie wprost
    z VPS-a — powód w docstringu modułu i w `docs/APIFY-PROXY.md`.

    Błędy HTTP i sieci LECĄ WYŻEJ nietknięte: mają zostać zaklasyfikowane przez
    `apify_keys.classify_apify_error` (401/402/403 to wyczerpany klucz, a nie
    „zepsuty odczyt salda"). Tłumienie ich tutaj kazałoby wołającemu zgadywać.
    """
    with client_for_token(token, timeout=timeout, env=env) as klient:
        odp = klient.get(
            f"{API}/users/me/limits",
            headers={"Authorization": f"Bearer {token}"},
        )
        odp.raise_for_status()
        return z_odpowiedzi(odp.json())


def koszt_runu(run: dict) -> float | None:
    """`usageTotalUsd` z obiektu runu Apify. None, gdy pola nie ma.

    Drugie, niezależne od salda źródło kosztu — patrz docstring modułu.
    """
    return _float_or_none((run or {}).get("usageTotalUsd"))


# ---------------------------------------------------------------------------
# CLI — jedno zapytanie na wskazany klucz. NIE odpytuje całej puli.
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    surowy = "--raw" in argv
    numer = 1
    if "--klucz" in argv:
        i = argv.index("--klucz")
        if i + 1 >= len(argv) or not argv[i + 1].isdigit():
            print("Użycie: --klucz N  (N liczony od 1)", file=sys.stderr)
            return 0
        numer = int(argv[i + 1])

    tokeny = load_apify_tokens()
    if not tokeny:
        # Brak konfiguracji = czyste wyjście z komunikatem (zasada 3 z README).
        print("[apify-credits] Brak kluczy Apify (APIFY_API_TOKEN*) — kończę bez "
              "działania. Sprawdź: python -m laweta_radar.config.settings", file=sys.stderr)
        return 0
    if not 1 <= numer <= len(tokeny):
        print(f"[apify-credits] Nie ma klucza #{numer} — widzę {len(tokeny)}.",
              file=sys.stderr)
        return 0

    token = tokeny[numer - 1]
    print(f"[apify-credits] klucz #{numer}, wyjście: "
          f"{proxy_label(proxy_for_token(token)) or 'BEZ PROXY (goły IP VPS-a)'}")
    try:
        with client_for_token(token, timeout=TIMEOUT_S) as klient:
            odp = klient.get(f"{API}/users/me/limits",
                             headers={"Authorization": f"Bearer {token}"})
            odp.raise_for_status()
            dane = odp.json()
    except Exception as e:  # noqa: BLE001 — CLI diagnostyczne: pokaż powód, nie traceback
        print(f"[apify-credits] błąd odczytu: {type(e).__name__}: {str(e)[:200]}",
              file=sys.stderr)
        return 1

    if surowy:
        import json  # noqa: PLC0415 — potrzebny tylko w tej gałęzi CLI

        print(json.dumps(dane, indent=2, ensure_ascii=False))
        return 0

    stan = z_odpowiedzi(dane)
    if stan is None:
        print("[apify-credits] Odpowiedź w NIEZNANYM kształcie — API Apify mogło "
              "zmienić pola. Zobacz surowy JSON: --raw", file=sys.stderr)
        return 1
    print(f"[apify-credits] {stan.opis()}")
    if stan.cykl_od or stan.cykl_do:
        print(f"[apify-credits] cykl: {stan.cykl_od} → {stan.cykl_do}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
