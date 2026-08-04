"""Frazy do wyszukiwania grup FB — dane, nie kod.

Osobno od `scripts/znajdz_grupy.py` z tego samego powodu, co `groups.py` osobno od
fetchera: dopisanie frazy to czynność OPERACYJNA. Robi ją człowiek, który wie, jak
w jego regionie ludzie nazywają lawetę, a niekoniecznie umie czytać Pythona.

KAŻDA FRAZA TO OSOBNE WYWOŁANIE ACTORA I OSOBNY KOSZT. Lista ma być CELNA, nie
długa — dorzucenie dziesięciu fraz „na wszelki wypadek" to dziesięć zapłaconych
wywołań, które zwrócą te same grupy, co już mamy.

DLACZEGO CZTERY JĘZYKI: laweta na trasie Polska–Niemcy wraca pusta, jeśli zlecenia
szuka tylko po polsku. Zlecenie powrotne bywa wystawione po niemiecku, czesku albo
słowacku — i właśnie takie jest najbardziej opłacalne, bo kurs i tak trzeba odbyć.

REGION MA ZNACZENIE WIĘKSZE NIŻ JĘZYK. Grupa regionalna ma najlepszy stosunek
zleceń do szumu, bo dojazd jest krótki: „laweta Podkarpacie" dowiezie mniej
postów niż „laweta Polska", ale znacznie więcej takich, po które operator
faktycznie pojedzie.
"""
from __future__ import annotations

# ── ZMIEŃ TO NA SWÓJ REGION ────────────────────────────────────────────────
# Wpisz województwo albo region, w którym stoi laweta. Podmienia się w dwóch
# frazach niżej — dlatego jest stałą, a nie wpisane w listę dwa razy.
REGION = "Podkarpacie"

FRAZY: dict[str, list[str]] = {
    # ── POLSKI: rdzeń listy, zawsze ────────────────────────────────────────
    "pl": [
        "giełda lawet",
        "giełda transportowa laweta",
        "transport aut laweta",
        "przewóz aut",
        "zlecenia transportowe laweta",
        "pomoc drogowa",
        "laweta zlecenia",
        "transport samochodów Polska",
        "wolne miejsce na lawecie",
        f"laweta {REGION}",
        f"pomoc drogowa {REGION}",
    ],
    # ── NIEMIECKI ──────────────────────────────────────────────────────────
    # UWAGA na „Autoüberführung": to przepędzanie auta WŁASNYM NAPĘDEM, nie
    # transport na lawecie. Grupy z tej frazy dostają notatkę i trzeba je
    # przejrzeć ręcznie — część będzie nie na temat.
    "de": [
        "Autotransport Börse",
        "Fahrzeugtransport",
        "Abschleppdienst",
        "Autotransport Polen Deutschland",
        "PKW Transport",
        "Autoüberführung",
        "Transportbörse Fahrzeuge",
        "Pannenhilfe",
    ],
    # ── CZESKI ─────────────────────────────────────────────────────────────
    "cs": [
        "odtahová služba",
        "přeprava aut",
        "autodoprava burza",
        "odtah vozidla",
        "přeprava vozidel Německo",
    ],
    # ── SŁOWACKI ───────────────────────────────────────────────────────────
    "sk": [
        "odťahová služba",
        "preprava áut",
        "odťah vozidla",
        "preprava vozidiel",
    ],
}

# Fraza, po której grupa ma trafić do ręcznego przeglądu mimo trafienia w temat.
FRAZY_NIEPEWNE = {"Autoüberführung"}

# ── FILTROWANIE WYNIKÓW ────────────────────────────────────────────────────
# Poniżej tego progu grupa rzadko ma ruch uzasadniający koszt runów fetchera.
# Nie jest to prawo natury — 300-osobowa grupa powiatowa bywa lepsza niż
# 50-tysięczna ogólnopolska. Obniż świadomie przez --min-czlonkow.
MIN_CZLONKOW = 500

# Nazwa sugerująca SPRZEDAŻ pojazdów lub części. To nie są giełdy zleceń: post
# „sprzedam felgi" nigdy nie jest zleceniem na lawetę, a grupa takich postów
# kosztuje w każdym przebiegu tyle samo, co grupa zgłoszeniowa. Grupy z tym
# słowem dostają NOTATKĘ, a nie automatyczne odrzucenie — „sprzedam auto po
# stłuczce" bywa właśnie zapowiedzią zlecenia.
SLOWA_SPRZEDAZOWE = (
    "sprzedam", "sprzedaż", "kupię", "części", "felgi", "opony",
    "teile", "ersatzteile", "verkaufe",       # de
    "díly", "prodám", "náhradní",             # cs
    "diely", "predám", "náhradné",            # sk
)

# ── ACTOR ──────────────────────────────────────────────────────────────────
# Wyszukiwarka GRUP (nie postów). Nazwy pól wejściowych actorów ze Store
# ZMIENIAJĄ SIĘ między wersjami, a literówka w nazwie pola nie zwraca błędu —
# zwraca run bez filtra, za pełną cenę. Dlatego stoją tutaj, w jednym miejscu,
# i sprawdza się je PRZED serią:
#     python -m laweta_radar.scripts.znajdz_grupy --schema
ACTOR = "memo23~facebook-search-groups-scraper"
POLE_FRAZY = "search"          # pole, do którego wchodzi szukana fraza
POLE_LIMITU = "maxItems"       # pole ograniczające liczbę wyników

# Ile wyników bierzemy z jednej frazy. Wyszukiwarka FB i tak sortuje po trafności,
# więc ogon jest szumem — a każdy wynik kosztuje.
WYNIKOW_NA_FRAZE = 30

# Cena katalogowa za wynik — WYŁĄCZNIE do oszacowania kosztu PRZED serią.
# Sprawdź na stronie actora i popraw, gdy się rozjedzie z rachunkiem.
CENA_KATALOGOWA_USD_ZA_WYNIK = 0.003


def frazy(jezyki=None) -> list[tuple[str, str]]:
    """[(jezyk, fraza)] — płaska lista do przejścia. `jezyki=None` = wszystkie.

    Płaska, bo `jezyk` musi jechać razem z frazą aż do CSV: kolumna `jezyk` jest
    zgadywana właśnie z tego, która fraza znalazła grupę, i nie da się jej
    odtworzyć później.
    """
    wybrane = FRAZY if jezyki is None else {
        j: f for j, f in FRAZY.items() if j in set(jezyki)
    }
    return [(j, f.strip()) for j, lista in wybrane.items() for f in lista if f.strip()]


def opis() -> str:
    """Linia do logu: ile fraz w którym języku."""
    czesci = ", ".join(f"{j}: {len(f)}" for j, f in FRAZY.items())
    return f"[frazy] {len(frazy())} fraz ({czesci}), region: {REGION}"


# Podgląd bez odpalania wyszukiwarki:
#   python -m laweta_radar.config.frazy_grup
if __name__ == "__main__":
    print(opis())
    for jezyk, fraza in frazy():
        print(f"  [{jezyk}] {fraza}")
