#!/usr/bin/env python3
"""Raport z trybu cienia — jedyny sposób, żeby stwierdzić, czy bramka jest dobra.

PO CO TO ISTNIEJE: bramka (workers/gate.py) odrzuca posty ZANIM zobaczy je model,
więc jej pomyłki są z definicji niewidoczne. Odrzucone zlecenie nie trafia nigdzie
— nie ma alertu, nie ma wiersza w logu, nie ma nikogo, kto by je zauważył. Bramka,
która kasuje co dziesiąty kurs, wygląda w produkcji DOKŁADNIE tak samo jak bramka
idealna: cicho i tanio.

Dlatego GATE_TRYB=cien: bramka liczy i zapisuje swoją decyzję, ale niczego nie
blokuje, wszystkie posty idą do AI, a tutaj porównujemy jedno z drugim. Liczba,
dla której powstał ten skrypt, to FAŁSZYWE ODRZUCENIA — posty, które bramka by
skasowała, a model uznał za zlecenie. Muszą wyjść ZERO. Odsetek odsianych,
histogramy i macierz są dodatkiem, który mówi, DLACZEGO wyszło, jak wyszło.

UŻYCIE:
    python laweta_radar/scripts/raport_gate.py                 # ostatnie 7 dni
    python laweta_radar/scripts/raport_gate.py --dni 30
    python laweta_radar/scripts/raport_gate.py --przelicz      # po zmianie słownika
    python laweta_radar/scripts/raport_gate.py --prog 7        # co przy innym progu

--przelicz jest tu najważniejszą opcją operacyjną: przepuszcza ZAPISANE treści
przez AKTUALNY słownik zamiast czytać stare decyzje. Dzięki temu poprawkę
w warstwie 2 sprawdzasz na tygodniu realnych postów w sekundę, bez czekania na
kolejny tydzień zbierania i bez płacenia Apify po raz drugi.
"""
from __future__ import annotations

import sys

try:                               # pakiet widoczny: -m, import pakietowy, testy
    from laweta_radar.scripts._sciezka import dodaj_repo_do_sciezki
except ImportError:                # uruchomienie po ścieżce do pliku
    from _sciezka import dodaj_repo_do_sciezki

dodaj_repo_do_sciezki()

from laweta_radar.config import settings  # noqa: E402
from laweta_radar.workers import gate as gate_mod  # noqa: E402

KTO = "raport-gate"

# Domyślna nazwa kolumny z werdyktem modelu. Kolumny jeszcze nie ma — dokłada ją
# migracja klasyfikatora (prompt 3). Do tego czasu raport działa w okrojonej
# formie i mówi o tym wprost, zamiast pokazywać puste zera jako "zero pomyłek".
KOLUMNA_AI_DOMYSLNA = "ai_zlecenie"

# Poniżej tylu sklasyfikowanych postów "zero fałszywych odrzuceń" nie znaczy nic.
# Przy pięćdziesięciu postach zero jest wynikiem najbardziej prawdopodobnym także
# dla bramki, która kasuje co dwudzieste zlecenie — a to jest dokładnie ta bramka,
# przed którą ten raport ma chronić.
MIN_PROBKA = 200


def _wyjscie(komunikat: str) -> int:
    """Czyste zakończenie z komunikatem — ta sama zasada co w workerach."""
    print(f"[{KTO}] {komunikat}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Odczyt z bazy
# ---------------------------------------------------------------------------
def _kolumny(cur, tabela: str = "posty") -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (tabela,),
    )
    return {r[0] for r in cur.fetchall()}


def _pobierz(cur, dni: int, kolumna_ai: str | None) -> list[dict]:
    """Posty z okna wraz z decyzją bramki i (jeśli kolumna istnieje) werdyktem AI."""
    ai_select = f", {kolumna_ai}" if kolumna_ai else ", NULL"
    cur.execute(
        f"""
        SELECT fb_id, tresc, post_url, grupa_nazwa,
               gate_werdykt, gate_punkty, gate_powod, gate_tryb
               {ai_select}
        FROM posty
        WHERE pobrany_at > NOW() - make_interval(days => %s)
        ORDER BY pobrany_at DESC
        """,
        (dni,),
    )
    out = []
    for r in cur.fetchall():
        out.append({
            "fb_id": r[0], "tresc": r[1] or "", "post_url": r[2],
            "grupa": r[3], "werdykt": r[4], "punkty": r[5],
            "powod": r[6] or "", "tryb": r[7], "ai": r[8],
        })
    return out


# ---------------------------------------------------------------------------
# Liczenie
# ---------------------------------------------------------------------------
def _przelicz(wiersze: list[dict], prog: int) -> None:
    """Nadpisz decyzje bramki wynikiem AKTUALNEGO słownika (opcja --przelicz).

    Tryb wymuszamy na "aktywny", bo interesuje nas werdykt, a nie to, czy w danym
    momencie wolno mu było cokolwiek zablokować.
    """
    for w in wiersze:
        wynik = gate_mod.gate(w["tresc"], prog=prog, tryb=gate_mod.TRYB_AKTYWNY)
        w["werdykt"] = wynik.werdykt
        w["punkty"] = wynik.punkty
        w["powod"] = wynik.powod
        w["trafienia"] = wynik.trafienia


def _werdykt_przy_progu(w: dict, prog: int) -> bool | None:
    """Co bramka orzekłaby przy innym progu, bez przeliczania słownika.

    Decyzje warstw 1-3 są od progu NIEZALEŻNE (wygaszenie, twarde przepuszczenie,
    twarde odrzucenie), więc zmiana progu ich nie rusza. Rusza wyłącznie posty
    rozstrzygnięte punktacją — i tylko te przeliczamy.
    """
    if w["werdykt"] is None:
        return None
    if w["powod"].startswith("punktacja") and w["punkty"] is not None:
        return w["punkty"] >= prog
    return w["werdykt"]


def _macierz(wiersze: list[dict], prog: int) -> dict[str, int]:
    """Macierz pomyłek dla postów, które mają OBA werdykty."""
    m = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for w in wiersze:
        werdykt = _werdykt_przy_progu(w, prog)
        if werdykt is None or w["ai"] is None:
            continue
        if w["ai"] and werdykt:
            m["tp"] += 1                    # zlecenie przepuszczone — dobrze
        elif w["ai"] and not werdykt:
            m["fn"] += 1                    # ZLECENIE ODRZUCONE — to liczymy
        elif not w["ai"] and werdykt:
            m["fp"] += 1                    # śmieć przepuszczony — kosztuje grosz
        else:
            m["tn"] += 1                    # śmieć odrzucony — oszczędność
    return m


def _histogram(punkty: list[int]) -> str:
    if not punkty:
        return "    (brak danych)"
    licznik: dict[int, int] = {}
    for p in punkty:
        licznik[p] = licznik.get(p, 0) + 1
    naj = max(licznik.values())
    linie = []
    for p in sorted(licznik):
        slupek = "#" * max(1, round(licznik[p] * 40 / naj))
        linie.append(f"    {p:>4} pkt | {slupek} {licznik[p]}")
    return "\n".join(linie)


def _skroc(tekst: str, n: int = 240) -> str:
    t = " ".join((tekst or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _zlecen(n: int) -> str:
    """Polska odmiana po liczbie — raport czyta człowiek, nie parser."""
    if n == 1:
        return "1 zlecenie"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} zlecenia"
    return f"{n} zleceń"


def _kategoria_powodu(powod: str) -> str:
    """"punktacja 3 < prog 5" -> "punktacja"; reszta powodów zostaje w całości.

    Grupujemy po powodzie, a punktacja ma go innego przy każdym wyniku — bez tego
    zestawienie miałoby po jednym wierszu na każdą możliwą sumę punktów.
    """
    return "punktacja" if powod.startswith("punktacja") else (powod or "(brak)")


# ---------------------------------------------------------------------------
# Wypisywanie
# ---------------------------------------------------------------------------
def _naglowek(tytul: str) -> None:
    print(f"\n=== {tytul} " + "=" * max(0, 72 - len(tytul)))


def _raport(wiersze: list[dict], prog: int, kolumna_ai: str | None, limit: int) -> int:
    z_bramka = [w for w in wiersze if w["werdykt"] is not None]
    z_ai = [w for w in z_bramka if w["ai"] is not None]

    _naglowek("PRÓBKA")
    print(f"  postów w oknie:              {len(wiersze)}")
    print(f"  z decyzją bramki:            {len(z_bramka)}")
    print(f"  z werdyktem AI (obie pary):  {len(z_ai)}")
    tryby = {w["tryb"] for w in z_bramka if w["tryb"]}
    if tryby:
        print(f"  tryby w próbce:              {', '.join(sorted(tryby))}")
    if tryby - {gate_mod.TRYB_CIEN}:
        print("  UWAGA: część decyzji zapadła POZA trybem cienia — te posty mogły")
        print("         nie trafić do AI, więc ich brak w macierzy nie znaczy 'zgoda'.")

    if not z_bramka:
        print("\n  Brak postów z decyzją bramki. Uruchom fetcher (prompt 2) — to on")
        print("  woła bramkę i zapisuje wynik (kontrakt: workers/gate.SQL_ZAPIS).")
        return 0

    odsiane = sum(1 for w in z_bramka if _werdykt_przy_progu(w, prog) is False)
    _naglowek("ILE BRAMKA ODSIEWA")
    print(f"  odrzuconych:                 {odsiane} z {len(z_bramka)}"
          f"  ({100.0 * odsiane / len(z_bramka):.1f}%)")
    print("  Realistyczny wynik to 20-35%. Niższy nie jest porażką — celem jest")
    print("  zero fałszywych odrzuceń, a odsetek ma wyjść, ile wyjdzie.")

    powody: dict[str, int] = {}
    for w in z_bramka:
        if _werdykt_przy_progu(w, prog) is False:
            kat = _kategoria_powodu(w["powod"])
            powody[kat] = powody.get(kat, 0) + 1
    if powody:
        print("\n  z czego:")
        for p, n in sorted(powody.items(), key=lambda kv: -kv[1]):
            print(f"    {p:<20} {n}")

    if not kolumna_ai:
        _naglowek("MACIERZ POMYŁEK — NIEDOSTĘPNA")
        print(f"  Tabela `posty` nie ma kolumny z werdyktem modelu"
              f" (szukałem: {KOLUMNA_AI_DOMYSLNA}).")
        print("  Dokłada ją migracja klasyfikatora — patrz prompt 3 i komentarz")
        print("  na końcu api/migrations/0002_gate.sql. Bez niej NIE DA SIĘ policzyć")
        print("  fałszywych odrzuceń, czyli jedynej liczby, która tu ma znaczenie.")
        print("  Inna nazwa kolumny: --kolumna-ai <nazwa>.")
        _naglowek("ROZKŁAD PUNKTÓW (bez podziału — brak werdyktów AI)")
        print(_histogram([w["punkty"] for w in z_bramka
                          if w["powod"].startswith("punktacja") and w["punkty"] is not None]))
        print(f"\n[{KTO}] Raport NIEPEŁNY — trybu cienia nie da się rozliczyć bez AI.")
        return 0

    if not z_ai:
        _naglowek("MACIERZ POMYŁEK — BRAK DANYCH")
        print(f"  Kolumna `{kolumna_ai}` istnieje, ale żaden post w oknie jej nie ma.")
        print("  Klasyfikator jeszcze nie przeszedł po tych postach.")
        return 0

    m = _macierz(z_ai, prog)
    _naglowek(f"MACIERZ POMYŁEK (prog {prog})")
    print("                        AI: ZLECENIE   AI: śmieć")
    print(f"  bramka przepuszcza      {m['tp']:>9}   {m['fp']:>9}")
    print(f"  bramka odrzuca          {m['fn']:>9}   {m['tn']:>9}   <- lewa liczba MUSI być 0")

    _naglowek("FAŁSZYWE ODRZUCENIA — to są kursy, które system by przegapił")
    falszywe = [w for w in z_ai if w["ai"] and _werdykt_przy_progu(w, prog) is False]
    if not falszywe:
        print("  ZERO. To jest warunek przełączenia GATE_TRYB na 'aktywny'.")
    else:
        print(f"  {len(falszywe)} — bramka NIE MOŻE zostać włączona w tym stanie.\n")
        for i, w in enumerate(falszywe[:limit], 1):
            print(f"  [{i}] {w['powod']}  (punkty {w['punkty']})")
            print(f"      {_skroc(w['tresc'])}")
            if w.get("trafienia"):
                print(f"      trafienia: {', '.join(w['trafienia'])}")
            if w["post_url"]:
                print(f"      {w['post_url']}")
            print()
        if len(falszywe) > limit:
            print(f"  ... i {len(falszywe) - limit} więcej (--limit)")

    _naglowek("ROZKŁAD PUNKTÓW — z niego odczytujesz próg")
    punktowane = [w for w in z_ai
                  if w["powod"].startswith("punktacja") and w["punkty"] is not None]
    print(f"  ZLECENIA wg AI ({sum(1 for w in punktowane if w['ai'])}):")
    print(_histogram([w["punkty"] for w in punktowane if w["ai"]]))
    print(f"\n  ŚMIECI wg AI ({sum(1 for w in punktowane if not w['ai'])}):")
    print(_histogram([w["punkty"] for w in punktowane if not w["ai"]]))
    print("\n  Próg stawia się PONIŻEJ najniższego słupka zleceń, nie w miejscu,")
    print("  gdzie rozkłady się rozchodzą — pomyłka w dół kosztuje grosz, w górę kurs.")

    _naglowek("CO BY BYŁO PRZY INNYM PROGU")
    print("   prog | falszywe odrzucenia | odsianych")
    for p in range(0, 16):
        mm = _macierz(z_ai, p)
        ods = sum(1 for w in z_bramka if _werdykt_przy_progu(w, p) is False)
        znacznik = "  <- GATE_PROG" if p == prog else ""
        print(f"   {p:>4} | {mm['fn']:>19} | {ods:>6} "
              f"({100.0 * ods / len(z_bramka):.0f}%){znacznik}")

    _naglowek("WERDYKT")
    if falszywe:
        print(f"  NIE PRZEŁĄCZAJ. {_zlecen(len(falszywe))} zostałoby skasowanych po cichu.")
        print("  Napraw słownik (workers/gate.py), sprawdź poprawkę przez --przelicz,")
        print("  i dopiero wtedy wróć tutaj.")
        return 0
    if len(z_ai) < MIN_PROBKA:
        print(f"  ZA WCZEŚNIE. Zero fałszywych odrzuceń, ale próbka to {len(z_ai)}"
              f" postów (minimum {MIN_PROBKA}).")
        print("  Przy tak małej próbce zero jest wynikiem najbardziej prawdopodobnym")
        print("  także dla bramki, która kasuje co dwudzieste zlecenie. Zbieraj dalej.")
        return 0
    print(f"  MOŻNA PRZEŁĄCZYĆ: 0 fałszywych odrzuceń na {len(z_ai)} sklasyfikowanych")
    print(f"  postach. Ustaw GATE_TRYB=aktywny w .env. Oszczędność: {odsiane} postów")
    print("  z tego okna nie poszłoby do modelu.")
    return 0


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Raport z trybu cienia bramki: ile zleceń bramka by przegapiła."
    )
    ap.add_argument("--dni", type=int, default=7, help="okno w dniach (domyślnie 7)")
    ap.add_argument("--prog", type=int, default=None,
                    help=f"próg do raportu (domyślnie GATE_PROG={settings.GATE_PROG})")
    ap.add_argument("--przelicz", action="store_true",
                    help="przepuść ZAPISANE treści przez AKTUALNY słownik zamiast "
                         "czytać stare decyzje — do sprawdzania poprawek")
    ap.add_argument("--kolumna-ai", default=KOLUMNA_AI_DOMYSLNA,
                    help=f"kolumna z werdyktem modelu (domyślnie {KOLUMNA_AI_DOMYSLNA})")
    ap.add_argument("--limit", type=int, default=50,
                    help="ile fałszywych odrzuceń wypisać w całości (domyślnie 50)")
    args = ap.parse_args(argv[1:])
    prog = settings.GATE_PROG if args.prog is None else args.prog

    braki = settings.brakujace("DATABASE_URL")
    if braki:
        return settings.wyjscie_bez_konfiguracji(KTO, braki)

    try:
        import psycopg2
    except ImportError:
        return _wyjscie("Brak psycopg2 — pip install -r laweta_radar/requirements.txt")

    try:
        conn = psycopg2.connect(settings.DATABASE_URL)
    except Exception as e:  # noqa: BLE001 — baza niedostępna to nie awaria raportu
        return _wyjscie(f"Nie mogę połączyć się z bazą: {type(e).__name__}: {e}")

    try:
        with conn, conn.cursor() as cur:
            kolumny = _kolumny(cur)
            if "gate_werdykt" not in kolumny:
                return _wyjscie(
                    "Tabela `posty` nie ma kolumn bramki — odpal migrację: "
                    "bash laweta_radar/scripts/migrate.sh (0002_gate.sql)"
                )
            kolumna_ai = args.kolumna_ai if args.kolumna_ai in kolumny else None
            wiersze = _pobierz(cur, args.dni, kolumna_ai)
    finally:
        conn.close()

    print(f"[{KTO}] okno: {args.dni} dni | prog: {prog}"
          + ("  | PRZELICZONE aktualnym słownikiem" if args.przelicz else ""))
    if args.przelicz:
        _przelicz(wiersze, prog)
    return _raport(wiersze, prog, kolumna_ai, args.limit)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
