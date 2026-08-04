"""Zbiór treningowy do poprawiania bramki i promptu klasyfikatora.

KAŻDE KLIKNIĘCIE „ŚMIEĆ" JEST DANYMI, nie tylko zmianą statusu. To jest jedyna
pętla zwrotna, jaką ten system ma: model orzekł „zlecenie", człowiek spojrzał
i powiedział „nie". Para (treść posta, werdykt modelu) zapisana w tym momencie
jest dokładnie tym, czego brakuje przy pisaniu następnej wersji promptu — bo za
tydzień nikt nie pamięta, KTÓRY post został odrzucony i co model o nim sądził.

Bez zapisanego `werdykt_ai_json` wpis „oznaczone jako śmieć" nie mówi NIC:
wiadomo, że model się pomylił, ale nie wiadomo w czym. Dlatego treść posta
i werdykt kopiujemy TU, a nie trzymamy przez klucz obcy — post można wyczyścić
z `posty` (retencja), a materiał treningowy ma zostać.

Moduł jest wspólny dla panelu (`api/routers/zlecenia.py`) i bota
(`workers/bot.py`), bo oba prowadzą do tego samego kliknięcia — raz na telefonie
w aplikacji, raz pod powiadomieniem. Dwie kopie tego zapisu rozjechałyby się
przy pierwszej zmianie schematu.
"""
from __future__ import annotations

import json
import sys

KTO = "feedback"

OCENY = ("smiec", "dobre")


def _log(msg: str) -> None:
    print(f"[{KTO}] {msg}", file=sys.stderr)


def zapisz(conn, fb_id: str, ocena: str) -> bool:
    """Dopisz ocenę operatora razem z materiałem, który ją tłumaczy.

    Treść posta i werdykt modelu dociągamy z `posty` JEDNYM zapytaniem, zamiast
    kazać wołającemu je podać — wołających jest dwóch (API i bot) i każdy z nich
    ma pod ręką inny podzbiór danych. Zapytanie jest po kluczu głównym.

    Zwraca True, gdy wpis powstał. False, gdy posta nie ma albo baza odmówiła —
    i to NIE jest powód do przerwania zmiany statusu: strata jednego wiersza
    treningowego jest tańsza niż zlecenie, które zostało w kolejce, bo zapis
    feedbacku się nie udał.
    """
    if ocena not in OCENY:
        _log(f"nieznana ocena {ocena!r} — pomijam")
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO feedback (fb_id, ocena, tresc_posta, werdykt_ai_json)
                SELECT p.fb_id, %s, p.tresc, p.ai_json
                  FROM posty p
                 WHERE p.fb_id = %s
                ON CONFLICT (fb_id, ocena) DO NOTHING
                """,
                (ocena, fb_id),
            )
            dodano = cur.rowcount
        conn.commit()
        return bool(dodano)
    except Exception as e:  # noqa: BLE001 — patrz docstring: to nie może przerwać zmiany statusu
        _log(f"{fb_id}: nie zapisałem feedbacku: {type(e).__name__}: {str(e)[:200]}")
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def jako_tekst(werdykt) -> str:
    """Werdykt modelu do jednej linii raportu. `None` i `{}` są normalne."""
    if not werdykt:
        return "(brak werdyktu — post nie przeszedł przez klasyfikator)"
    if isinstance(werdykt, str):
        return werdykt
    return json.dumps(werdykt, ensure_ascii=False, sort_keys=True)
