"""Kontrakt klasyfikatora wyrażony jako JSON Schema — dla trybu
OPENAI_JSON_MODE="schema" (structured outputs).

DLACZEGO SCHEMAT JEST WYLICZANY, A NIE PRZEPISANY. Zbiory dopuszczalnych
wartości (`typ`, `pilnosc`, kategorie pojazdu, typy kontaktu) mieszkają
w `workers/classifier.py` i to one decydują, co przejdzie walidację. Gdyby ten
plik miał WŁASNĄ kopię tych list, tryb "schema" gwarantowałby zgodność
z kontraktem, którego walidator nie zna — i pierwsza dopisana kategoria
pojazdu dawałaby model, który grzecznie zwraca wartość natychmiast zamienianą
na "inne", bez jednego błędu po drodze. Dlatego czytamy te same krotki, których
używa walidator.

DLACZEGO IMPORT JEST LENIWY. `classifier` importuje `services.llm`, a `llm`
sięga tutaj po schemat. Import klasyfikatora na górze tego pliku zamknąłby
koło i wywalił się przy starcie — czyli w miejscu, w którym w tym repo
z zasady nic się nie wywala.

CZEGO SCHEMAT NIE ROBI — patrz komentarz przy trybach JSON w services/llm.py:
gwarantuje KSZTAŁT i TYPY, nie prawdziwość wartości. Model nadal może wpisać
do `odbior.miasto` nazwę, której w poście nie było; dostaniesz ją tylko ładnie
sformatowaną.
"""
from __future__ import annotations

from typing import Any

NAZWA = "klasyfikacja_zlecenia"


def _tekst_lub_null() -> dict[str, Any]:
    # Nullowalne pole w structured outputs to UNIA TYPÓW, nie brak w `required`.
    # Tryb ścisły wymaga, żeby każde pole z `properties` stało w `required` —
    # „pole opcjonalne" wyraża się przez dopuszczenie nulla, i tylko tak.
    return {"type": ["string", "null"]}


def _obiekt(properties: dict[str, Any]) -> dict[str, Any]:
    # `additionalProperties: false` i pełne `required` są w trybie ścisłym
    # OBOWIĄZKOWE na KAŻDYM poziomie — brak któregokolwiek daje błąd 400 przy
    # pierwszym wywołaniu, a nie cichą degradację.
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _miejsce() -> dict[str, Any]:
    return _obiekt({
        "raw": _tekst_lub_null(),
        "kod": _tekst_lub_null(),
        "miasto": _tekst_lub_null(),
    })


def schemat() -> dict[str, Any]:
    """JSON Schema wyniku klasyfikacji, zbudowany z kontraktu klasyfikatora."""
    from laweta_radar.workers import classifier  # noqa: PLC0415 — patrz nagłówek

    return _obiekt({
        "czy_zlecenie": {"type": "boolean"},
        "typ": {"type": "string", "enum": list(classifier._POPRAWNE_TYP)},
        "odbior": _miejsce(),
        "dostawa": _miejsce(),
        "pojazd": _obiekt({
            "opis": _tekst_lub_null(),
            "kategoria": {"type": "string", "enum": list(classifier._POPRAWNE_KATEGORIE)},
        }),
        "stan": _obiekt({
            "toczy_sie": {"type": "boolean"},
            "ma_kola": {"type": "boolean"},
            "po_wypadku": {"type": "boolean"},
            "uwagi": _tekst_lub_null(),
        }),
        "pilnosc": {"type": "string", "enum": list(classifier._POPRAWNE_PILNOSC)},
        "kontakt": _obiekt({
            "typ": {"type": "string", "enum": list(classifier._POPRAWNE_KONTAKT)},
            "wartosc": _tekst_lub_null(),
        }),
        # Bez `minimum`/`maximum` — tryb ścisły ich nie obsługuje, a i tak nie
        # miałyby czego pilnować: zakres 0-100 domyka walidator klasyfikatora.
        # To dobra ilustracja granicy tego trybu: wymusi liczbę, nie wymusi
        # SENSOWNEJ liczby.
        "cena_sugerowana": {"type": ["number", "null"]},
        "pewnosc": {"type": "integer"},
        "powod": _tekst_lub_null(),
    })


def response_format() -> dict[str, Any]:
    """Gotowy `response_format` do wywołania OpenAI w trybie "schema"."""
    return {
        "type": "json_schema",
        "json_schema": {"name": NAZWA, "strict": True, "schema": schemat()},
    }


if __name__ == "__main__":
    import json

    print(json.dumps(response_format(), ensure_ascii=False, indent=2))
