#!/usr/bin/env python3
"""Wejście do następnej iteracji promptu klasyfikatora — i do poprawek bramki.

PO CO TO ISTNIEJE. Tabela `feedback` zbiera pary (treść posta, werdykt modelu)
za każdym razem, gdy operator klika „Śmieć". To jest jedyna pętla zwrotna w tym
systemie i jedyny materiał, na którym da się poprawić prompt czymś innym niż
zgadywaniem. Bez skryptu, który to WYPISUJE, tabela będzie tylko rosła — dane,
których nikt nie czyta, nie istnieją, a przy dwudziestu wierszach nikt nie będzie
pisał SQL-a z pamięci o drugiej w nocy.

CO Z TYM ZROBIĆ. Raport jest ułożony tak, żeby dało się go wkleić wprost pod
prompt klasyfikatora jako listę kontrprzykładów. Grupowanie po powtarzającym się
wzorcu (`--wzorce`) pokazuje przy okazji to, czego pojedyncze przykłady nie
pokażą: że dziesięć z piętnastu odrzuceń to ten sam typ posta, więc wystarczy
jedno zdanie w prompcie albo jeden wzorzec w słowniku bramki.

UŻYCIE:
    python laweta_radar/scripts/raport_feedback.py                # 30 dni, śmieci
    python laweta_radar/scripts/raport_feedback.py --dni 7
    python laweta_radar/scripts/raport_feedback.py --ocena dobre  # kontrola
    python laweta_radar/scripts/raport_feedback.py --wzorce       # co się powtarza
    python laweta_radar/scripts/raport_feedback.py --json > x.json

`--json` jest do skarmienia następnego promptu maszynowo; domyślne wyjście jest
do czytania oczami, bo prompt i tak poprawia człowiek.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from laweta_radar.config import settings  # noqa: E402
from laweta_radar.services import feedback  # noqa: E402

KTO = "raport-feedback"

# Ile znaków treści pokazujemy w trybie czytelnym. Więcej niż w powiadomieniu,
# bo tu chodzi o ZROZUMIENIE, czemu model się pomylił, a nie o decyzję w dwie
# sekundy — a pomyłka bywa schowana w trzecim zdaniu.
SKROT = 400

# Wzorce do grupowania powtarzalnych pomyłek. To NIE jest bramka i nie ma tu
# żadnej decyzji — to jest podpowiedź, od czego zacząć czytanie. Świadomie
# prymitywne: gdyby były mądrzejsze, ukrywałyby przypadki, których nie
# przewidziano, a to jest dokładnie ten zbiór, po który się tu sięga.
WZORCE = {
    "oferta lawety (autopromocja)": r"\b(oferuj|świadcz|swiadcz|zapraszam|"
                                    r"konkurencyjn|24/7|24h|tanio|faktur)\w*",
    "sprzedaż auta/części": r"\b(sprzedam|na sprzedaż|na sprzedaz|cena do "
                            r"uzgodnienia|felg|opon|silnik na czę)\w*",
    "praca / ogłoszenie": r"\b(zatrudni|praca|kierowc[ęe] szuka|cv |rekrutac)\w*",
    "pytanie o cenę bez zlecenia": r"\b(ile (by )?kosztuje|orientacyjn[ay] koszt|"
                                   r"jaka cena|ile za km)\w*",
    "wygaszone": r"\b(już nie aktualn|nieaktualn|znalazłem|znalazlem|"
                 r"załatwione|zalatwione|dziękuję wszystkim|dziekuje wszystkim)\w*",
    "transport inny niż auto": r"\b(przyczep|maszyn|kopark|quad|motocykl|łód|lod)\w*",
}


def _wyjscie(komunikat: str) -> int:
    """Czyste zakończenie z komunikatem — ta sama zasada co w workerach."""
    print(f"[{KTO}] {komunikat}", file=sys.stderr)
    return 0


def _pobierz(cur, dni: int, ocena: str) -> list[dict]:
    cur.execute(
        """
        SELECT f.fb_id, f.ocena, f.tresc_posta, f.werdykt_ai_json, f.ocenil_at,
               p.grupa_nazwa, p.gate_punkty, p.gate_werdykt, p.gate_jezyk,
               p.ai_pewnosc, p.post_url
          FROM feedback f
          LEFT JOIN posty p ON p.fb_id = f.fb_id
         WHERE f.ocena = %s
           AND f.ocenil_at > NOW() - make_interval(days => %s)
         ORDER BY f.ocenil_at DESC
        """,
        (ocena, dni),
    )
    kolumny = [k[0] for k in cur.description]
    return [dict(zip(kolumny, w)) for w in cur.fetchall()]


def _kategoria(tresc: str) -> str:
    """Do której powtarzalnej pomyłki pasuje ten post. „inne" jest wynikiem
    normalnym i najciekawszym — to są przypadki, których nikt nie przewidział."""
    tekst = (tresc or "").lower()
    for nazwa, wzorzec in WZORCE.items():
        if re.search(wzorzec, tekst):
            return nazwa
    return "inne"


def _skroc(tekst: str | None, n: int = SKROT) -> str:
    t = re.sub(r"\s+", " ", str(tekst or "")).strip()
    return t if len(t) <= n else t[:n].rstrip() + "…"


def _naglowek(tytul: str) -> None:
    print()
    print("=" * 78)
    print(f" {tytul}")
    print("=" * 78)


def _raport(wiersze: list[dict], ocena: str, dni: int, wzorce_tylko: bool) -> None:
    _naglowek(f"FEEDBACK: {ocena.upper()} — ostatnie {dni} dni")
    if not wiersze:
        print(f"\nZero wpisów. Przy ocenie 'smiec' to znaczy jedno z dwóch:")
        print("  • klasyfikator nie myli się na tyle, żeby operator klikał, albo")
        print("  • operator nie klika, bo przycisk jest niewidoczny/nieoczywisty.")
        print("Drugie jest znacznie bardziej prawdopodobne i warte sprawdzenia.")
        return

    print(f"\nwpisów: {len(wiersze)}")
    kategorie = Counter(_kategoria(w["tresc_posta"]) for w in wiersze)

    _naglowek("CO SIĘ POWTARZA — od tego zacznij")
    print()
    for nazwa, ile in kategorie.most_common():
        udzial = 100.0 * ile / len(wiersze)
        print(f"  {ile:>4}  ({udzial:4.1f}%)  {nazwa}")
    print()
    print("  Kategoria z najwyższym udziałem to zwykle JEDNO zdanie do dopisania")
    print("  w prompcie albo JEDEN wzorzec w słowniku bramki (workers/gate.py).")
    print("  Kategoria 'inne' jest najciekawsza — to przypadki nieprzewidziane.")

    # Ile z tych pomyłek bramka i tak by odrzuciła. Liczba ważna operacyjnie:
    # jeśli wysoka, poprawka należy do PROGU bramki, a nie do promptu — i jest
    # darmowa, bo bramka nie kosztuje tokenów.
    z_opinia = [w for w in wiersze if w.get("gate_werdykt") is not None]
    if z_opinia:
        odrzucone = sum(1 for w in z_opinia if w["gate_werdykt"] is False)
        print()
        print(f"  Bramka odrzuciłaby {odrzucone} z {len(z_opinia)} tych postów.")
        if odrzucone:
            print("  -> tę część naprawia PRÓG bramki (GATE_PROG), nie prompt.")
            print("     Sprawdź: python laweta_radar/scripts/raport_gate.py --prog N")

    if wzorce_tylko:
        return

    _naglowek("PRZYKŁADY — wklej pod prompt klasyfikatora jako kontrprzykłady")
    for i, w in enumerate(wiersze, 1):
        pewnosc = w.get("ai_pewnosc")
        print()
        print(f"--- {i}. {w['fb_id']} "
              f"[{w.get('grupa_nazwa') or 'grupa ?'}"
              f"{', ' + w['gate_jezyk'] if w.get('gate_jezyk') else ''}"
              f"{f', pewność {pewnosc}' if pewnosc is not None else ''}"
              f", punkty bramki {w.get('gate_punkty')}] "
              f"[{_kategoria(w['tresc_posta'])}]")
        print(f"    TREŚĆ:   {_skroc(w['tresc_posta'])}")
        print(f"    WERDYKT: {_skroc(feedback.jako_tekst(w['werdykt_ai_json']), 300)}")
        if w.get("post_url"):
            print(f"    POST:    {w['post_url']}")

    _naglowek("CO DALEJ")
    print("""
  1. Przeczytaj kategorię z największym udziałem. Zwykle jedno zdanie
     w prompcie klasyfikatora („to NIE jest zlecenie, gdy autor OFERUJE usługę")
     kasuje kilkanaście przykładów naraz.
  2. Przypadki, które bramka i tak by odrzuciła, napraw progiem bramki —
     to jest darmowe, bo bramka nie płaci za tokeny.
  3. Po zmianie promptu odpal ten raport ponownie za tydzień i porównaj
     liczby. Ocena 'dobre' jest kontrolą: ma NIE spaść.
  4. Nie celuj w zero. Model, który nigdy się nie myli w tę stronę, zwykle
     zaczął mylić się w drugą — a tych pomyłek nie widać, bo odrzucone
     zlecenie nie trafia nigdzie.
""")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Wypisz posty ocenione przez operatora wraz z werdyktem modelu.")
    ap.add_argument("--dni", type=int, default=30)
    ap.add_argument("--ocena", default="smiec", choices=list(feedback.OCENY))
    ap.add_argument("--wzorce", action="store_true",
                    help="tylko podsumowanie kategorii, bez przykładów")
    ap.add_argument("--json", action="store_true", help="wyjście maszynowe")
    args = ap.parse_args(argv[1:])

    if not settings.DATABASE_URL:
        return _wyjscie("Brak DATABASE_URL — nie ma czego czytać.")
    try:
        import psycopg2
    except ImportError:
        return _wyjscie("Brak psycopg2 — pip install -r laweta_radar/requirements.txt")

    try:
        conn = psycopg2.connect(settings.DATABASE_URL, connect_timeout=5)
    except Exception as e:  # noqa: BLE001 — skrypt ma powiedzieć powód, nie rzucić
        return _wyjscie(f"Baza niedostępna: {type(e).__name__}: {str(e)[:200]}")

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.feedback') IS NOT NULL")
            (jest,) = cur.fetchone()
            if not jest:
                return _wyjscie("Brak tabeli `feedback` — odpal migrację "
                                "laweta_radar/api/migrations/0006_feedback.sql")
            wiersze = _pobierz(cur, args.dni, args.ocena)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(
            [{**w, "ocenil_at": str(w["ocenil_at"]),
              "kategoria": _kategoria(w["tresc_posta"])} for w in wiersze],
            ensure_ascii=False, indent=2))
        return 0

    _raport(wiersze, args.ocena, args.dni, args.wzorce)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
