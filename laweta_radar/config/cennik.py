"""Stawki modeli — JEDNO miejsce w repo, w którym stoją ceny.

DLACZEGO OSOBNY PLIK, a nie stała w implementacji providera: cennik zmienia się
u dostawcy, a nie u nas. Gdyby stawki siedziały w `services/llm.py`, każda ich
korekta wchodziłaby w plik, który woła sieć — czyli w kod, którego nie da się
poprawić bez przeczytania całej ścieżki wywołania. Tutaj poprawka to jedna
liczba i data obok niej.

TRZY LICZBY NA MODEL, nie dwie: (wejście, wyjście, wejście_z_cache). Trzecia
istnieje, bo obaj dostawcy liczą trafienie w cache jako 10% stawki wejściowej,
ale RAPORTUJĄ je zupełnie inaczej — i pomyłka w tę stronę zawyża rachunek
dziesięciokrotnie na tym samym ruchu. Normalizacja jest po stronie providera
(services/llm.py); tutaj są same ceny.

KONTRAKT LICZNIKÓW (żeby nic nie policzyło się dwa razy):
  • `tokeny_wejscia`     — wejście płatne PEŁNĄ stawką, BEZ tego, co przyszło
                           z cache;
  • `tokeny_cache`       — wejście przeczytane z cache, płatne stawką trzecią;
  • `tokeny_wyjscia`     — CAŁE wyjście, RAZEM z tokenami rozumowania (dostawca
                           rozlicza je jak wyjściowe);
  • `tokeny_rozumowania` — podzbiór wyjścia, podawany OSOBNO do raportu.
    Do kosztu NIE dodajemy go drugi raz — jest już w `tokeny_wyjscia`. To jest
    najłatwiejszy sposób na podwojenie rachunku w tabeli porównawczej i jedyny
    powód, dla którego ten akapit tu stoi.

NIEZNANY MODEL = None I OSTRZEŻENIE, nigdy cicha zerowa kwota. Zero czyta się
jako „za darmo" i przesuwa decyzję o wyborze modelu na zmyślonych danych —
a to jedyna liczba, dla której scripts/porownaj_modele.py istnieje.

Podgląd:  python -m laweta_radar.config.cennik
"""
from __future__ import annotations

import json
import re
import sys

from laweta_radar.config import settings

# ---------------------------------------------------------------------------
# STAWKI — USD za MILION tokenów: (wejście, wyjście, wejście z cache).
#
# SPRAWDZONE 2026-08-05 wg cenników dostawców:
#   Anthropic — platform.claude.com/docs/en/about-claude/pricing
#   OpenAI    — openai.com/api/pricing
# Trafienie w cache kosztuje u obu 10% stawki wejściowej; zapis do cache
# (Anthropic, 1.25x wejścia) NIE ma tu osobnej pozycji, bo ten system nadal nie
# cache'uje promptu.
#
# UWAGA NA PRZYSZŁOŚĆ: po dołożeniu przykładów few-shot prompt klasyfikatora
# urósł do ~2,7 tys. tokenów, czyli przestał być „krótszy niż próg opłacalności"
# — jest identyczny przy każdym wywołaniu i mieści się w minimum cache'owania.
# Włączenie cache to osobna zmiana (w `services/llm.py`), ale liczba, dla której
# warto ją rozważyć, stoi już po tej stronie: dziesiąta część stawki wejściowej.
#
# DATA MA ZNACZENIE. Stawka sprzed roku nie wywali niczego — po cichu przekłamie
# jedyną liczbę, dla której porównywarka modeli istnieje. Aktualizując wpis,
# przesuń też datę wyżej; wpis bez daty jest wart tyle co jego brak.
# ---------------------------------------------------------------------------
CENNIK_USD_ZA_MTOK: dict[str, tuple[float, float, float]] = {
    # Anthropic
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
    "claude-sonnet-5": (3.00, 15.00, 0.30),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    # OpenAI
    "gpt-5-mini": (0.25, 2.00, 0.025),
    "gpt-5.1": (1.25, 10.00, 0.125),
    "gpt-5.4": (2.50, 15.00, 0.25),
    "gpt-5.5": (5.00, 30.00, 0.50),
}

# Wariant z datą ma trafiać na stawkę aliasu: "claude-haiku-4-5-20251001" to ten
# sam model i ta sama cena co "claude-haiku-4-5". Dopuszczamy WYŁĄCZNIE takie
# ogonki — datę i "latest".
#
# Zwykły prefiks byłby tu pułapką: "gpt-5.1" jest prefiksem hipotetycznego
# "gpt-5.1-mini", więc mini dostałoby po cichu cenę modelu kilka razy droższego.
# Inna wielkość modelu to inna cena, a cicha pomyłka w tę stronę jest dokładnie
# tym, przed czym ten plik ma bronić. Nieznany wariant ma dać None.
_OGONEK_WARIANTU = re.compile(r"^-(\d{4}-\d{2}-\d{2}|\d{6,8}|latest)$")


def _z_env() -> dict[str, tuple[float, float, float]]:
    """Nadpisania z CENNIK_EXTRA (JSON). Śmieć w .env -> ignorujemy w ciszy.

    Zła składnia nie może zatrzymać runu: raport pokaże koszt jako nieznany
    i to wystarczy, żeby autor zauważył literówkę.

    Przyjmujemy DWIE i TRZY liczby. Przy dwóch stawka cache'owa równa się
    wejściowej — czyli zakładamy BRAK zniżki. To jedyne bezpieczne domyślne:
    zgadnięte 10% zaniżyłoby rachunek o rząd wielkości, gdyby dostawca liczył
    inaczej, a zaniżony rachunek nie ma jak się objawić.
    """
    surowy = settings.CENNIK_EXTRA
    if not surowy:
        return {}
    dodatkowe: dict[str, tuple[float, float, float]] = {}
    try:
        for model, stawka in json.loads(surowy).items():
            wej, wyj = float(stawka[0]), float(stawka[1])
            cache = float(stawka[2]) if len(stawka) > 2 else wej
            dodatkowe[str(model)] = (wej, wyj, cache)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return {}
    return dodatkowe


def cennik() -> dict[str, tuple[float, float, float]]:
    """Stawki wbudowane + nadpisania z .env. Nadpisanie wygrywa."""
    return {**CENNIK_USD_ZA_MTOK, **_z_env()}


def stawki(model: str) -> tuple[float, float, float] | None:
    """Stawki dla modelu albo None, gdy ceny nie znamy.

    Najpierw trafienie dokładne, potem alias z ogonkiem wariantu. Przy kilku
    pasujących aliasach wygrywa DŁUŻSZY — dokładniejszy wpis ma pierwszeństwo
    przed ogólniejszym.
    """
    ceny = cennik()
    nazwa = (model or "").strip()
    if nazwa in ceny:
        return ceny[nazwa]
    for prefiks in sorted(ceny, key=len, reverse=True):
        if nazwa.startswith(prefiks) and _OGONEK_WARIANTU.match(nazwa[len(prefiks):]):
            return ceny[prefiks]
    return None


# Modele, o których braku już krzyknęliśmy. Bez tego porównanie na 50 postach
# daje 50 identycznych linii i realne ostrzeżenia toną w powtórkach.
_juz_ostrzegalismy: set[str] = set()


def koszt_usd(
    model: str,
    tokeny_wejscia: int,
    tokeny_wyjscia: int,
    tokeny_cache: int = 0,
) -> float | None:
    """Koszt jednego wywołania w USD. None = nie znamy stawki dla tego modelu.

    Tokeny rozumowania NIE są osobnym argumentem świadomie: dostawca rozlicza
    je jak wyjściowe i siedzą już w `tokeny_wyjscia`. Osobny argument kusiłby,
    żeby dodać je drugi raz.
    """
    ceny = stawki(model)
    if ceny is None:
        if model not in _juz_ostrzegalismy:
            _juz_ostrzegalismy.add(model)
            print(f"[cennik] nie znam stawki dla modelu {model!r} — koszt zgłaszam "
                  f"jako NIEZNANY. Dopisz go do config/cennik.py albo do CENNIK_EXTRA "
                  f"w .env, inaczej kolumna z kosztem nic nie znaczy.", file=sys.stderr)
        return None
    wej, wyj, cache = ceny
    return (tokeny_wejscia / 1e6 * wej
            + tokeny_wyjscia / 1e6 * wyj
            + tokeny_cache / 1e6 * cache)


def _main() -> int:
    ceny = cennik()
    wbudowane = set(CENNIK_USD_ZA_MTOK)
    print(f"[cennik] {len(ceny)} modeli (USD za milion tokenów)")
    print(f"{'model':<26} {'wejście':>9} {'wyjście':>9} {'cache':>9}  skąd")
    for nazwa in sorted(ceny):
        wej, wyj, cache = ceny[nazwa]
        skad = "kod" if nazwa in wbudowane else "CENNIK_EXTRA"
        print(f"{nazwa:<26} {wej:9.3f} {wyj:9.3f} {cache:9.3f}  {skad}")
    print("\nModel spoza tej listy = koszt NIEZNANY (None) i ostrzeżenie w logu.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
