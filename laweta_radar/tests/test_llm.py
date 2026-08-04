"""Offline testy services/llm.py — bez sieci i bez SDK żadnego providera.

Ta warstwa istnieje z powodu POMIAROWEGO: żeby dało się puścić ten sam zbiór
postów przez kilka modeli i porównać wyniki na WŁASNYCH danych. Testujemy więc
przede wszystkim to, co ten pomiar mogłoby po cichu przekłamać:

  • koszt liczony ze zmyślonej stawki (dlatego brak stawki = None, nie 0.0);
  • literówka w LLM_PROVIDER przełączająca system na providera bez zależności;
  • brak SDK objawiający się dopiero w środku runu, a nie przy starcie.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.services import llm  # noqa: E402


def test_nieznany_provider_degraduje_do_anthropic():
    """Literówka w .env nie może przełączyć systemu na coś, czego nie ma.

    Anthropic jest jedynym providerem, którego zależność siedzi
    w requirements.txt — degradacja tam jest jedyną bezpieczną.
    """
    for smiec in ["", None, "  ", "opneai", "Claude", "gpt"]:
        assert llm.normalizuj_provider(smiec) == llm.ANTHROPIC


def test_znane_providery_przechodza():
    for p in llm.PROVIDERY:
        assert llm.normalizuj_provider(p.upper()) == p


def test_brak_stawki_daje_none_a_nie_zero():
    """None znaczy „nie znamy ceny". Zero czytałoby się jako „za darmo".

    Zaniżona suma runu to najgorszy możliwy błąd w porównywarce modeli: nie
    wywala niczego, tylko przesuwa decyzję o wyborze modelu na zmyślonych
    danych.
    """
    assert llm.koszt_usd("model-ktorego-nie-znamy", 1000, 1000) is None


def test_koszt_liczony_ze_stawki():
    # Haiku 4.5: $1.00 za milion wejścia, $5.00 za milion wyjścia.
    koszt = llm.koszt_usd("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert abs(koszt - 6.00) < 1e-9


def test_stawka_dopasowuje_sie_prefiksem():
    """Wariant z datą ma trafiać na tę samą stawkę co alias."""
    assert llm.koszt_usd("claude-haiku-4-5-20251001", 1_000_000, 0) == \
        llm.koszt_usd("claude-haiku-4-5", 1_000_000, 0)


def test_dluzszy_prefiks_wygrywa():
    """Gdyby cennik miał i "claude-opus" i "claude-opus-5", wygrywa dokładniejszy."""
    ceny = llm._cennik()
    assert llm.koszt_usd("claude-opus-5", 1_000_000, 0) == ceny["claude-opus-5"][0]


def test_problemy_mowia_o_braku_sdk_i_klucza():
    """Komunikat ma dać się przeczytać o trzeciej w nocy i powiedzieć, co zrobić."""
    braki = llm.problemy(llm.OPENAI)
    assert braki, "bez klucza i bez SDK musi być co najmniej jeden problem"
    assert any("pip install" in b for b in braki)


def test_zapytaj_bez_konfiguracji_rzuca_wlasnym_typem():
    """Nie ImportError w środku runu — własny wyjątek, łapany przez wołającego."""
    try:
        llm.zapytaj_ze_zuzyciem("system", "user", 100, provider=llm.OPENAI, model="x")
    except llm.LLMNiedostepny as e:
        assert "openai" in str(e)
        return
    raise AssertionError("brak konfiguracji musi dać LLMNiedostepny")


def test_opis_nie_wywala_bez_konfiguracji():
    """Linia startowa ma powstać zawsze — także na świeżym klonie."""
    tekst = llm.opis()
    assert "provider=" in tekst and "model=" in tekst


def test_gotowe_providery_to_podzbior_znanych():
    assert set(llm.gotowe_providery()) <= set(llm.PROVIDERY)


def test_model_domyslny_per_provider():
    """Każdy provider ma własny model — porównywarka odpala je równolegle."""
    modele = {p: llm.model_domyslny(p) for p in llm.PROVIDERY}
    assert modele[llm.ANTHROPIC].startswith("claude-")
    assert len(set(modele.values())) == len(llm.PROVIDERY)


def test_domyslny_model_to_haiku():
    """Zadanie ekstrakcji, nie rozumowania — Haiku robi to za ułamek ceny.

    Liczy się też czas: każda sekunda opóźnienia to przewaga konkurencji.
    """
    assert llm.model_domyslny(llm.ANTHROPIC).startswith("claude-haiku-4-5")


def test_odpowiedz_niesie_dane_do_rozliczenia():
    o = llm.Odpowiedz(tekst="{}", provider="anthropic", model="m", ms=120,
                      tokeny_wejscie=10, tokeny_wyjscie=20)
    assert (o.ms, o.tokeny_wejscie, o.tokeny_wyjscie) == (120, 10, 20)
