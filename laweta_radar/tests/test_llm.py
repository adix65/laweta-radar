"""Offline testy services/llm.py — bez sieci i bez SDK żadnego providera.

Ta warstwa istnieje z powodu POMIAROWEGO: żeby dało się puścić ten sam zbiór
postów przez kilka modeli i porównać wyniki na WŁASNYCH danych. Testujemy więc
przede wszystkim to, co ten pomiar mogłoby po cichu przekłamać:

  • koszt liczony ze zmyślonej stawki (dlatego brak stawki = None, nie 0.0);
  • tokeny rozumowania policzone dwa razy albo wcale;
  • cache policzony pełną stawką (albo odwrotnie — pominięty);
  • literówka w LLM_PROVIDER przełączająca system na providera bez zależności;
  • brak SDK objawiający się dopiero w środku runu, a nie przy starcie.

ORAZ to, co przy OpenAI wysypuje się w środku przebiegu zamiast przy starcie:
pusta lista `choices`, `content=None` po filtrze treści, obcięcie na limicie
udające „model nie umie w JSON" i odrzucony parametr, którego nazwa zmieniła
się między wersjami API.

Cała ścieżka OpenAI jest testowana BEZ pakietu `openai` — funkcje biorą klienta
argumentem, a wyjątki poznajemy po kodzie HTTP, nie po klasie. Testy, które
wymagałyby zainstalowanego SDK, nie chodziłyby na maszynie, na której ten
provider nie jest używany, czyli w domyślnej konfiguracji repo.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.config import cennik  # noqa: E402
from laweta_radar.config import settings  # noqa: E402
from laweta_radar.services import llm, schemat  # noqa: E402


@contextmanager
def ustaw(**zmienne):
    """Podmień wartości w `settings` na czas testu i przywróć oryginały."""
    stare = {k: getattr(settings, k) for k in zmienne}
    for k, v in zmienne.items():
        setattr(settings, k, v)
    try:
        yield
    finally:
        for k, v in stare.items():
            setattr(settings, k, v)


def blad_api(status: int | None = None, kod: str = "", param: str = "",
             tresc: str = "blad") -> Exception:
    """Atrapa wyjątku SDK. Rozpoznajemy je po atrybutach, nie po klasie."""
    e = RuntimeError(tresc)
    if status is not None:
        e.status_code = status
    e.code = kod
    e.param = param
    return e


def odpowiedz_openai(tresc: str | None = "{}", powod: str = "stop", *,
                     wejscie: int = 100, wyjscie: int = 50, cache: int = 0,
                     rozumowanie: int = 0, odmowa: str | None = None):
    """Atrapa odpowiedzi chat.completions o kształcie, który zwraca API."""
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=tresc, refusal=odmowa),
            finish_reason=powod,
        )],
        usage=SimpleNamespace(
            prompt_tokens=wejscie,
            completion_tokens=wyjscie,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cache),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=rozumowanie),
        ),
    )


class KlientAtrapa:
    """Klient OpenAI, który zapisuje wywołania i oddaje z góry ustaloną odpowiedź.

    `bledy` to lista wyjątków rzucanych po kolei — tak odtwarzamy serwer
    odrzucający parametr przy pierwszym podejściu i przyjmujący przy drugim.
    """

    def __init__(self, bledy=(), odpowiedz=None):
        self.bledy = list(bledy)
        self.odpowiedz = odpowiedz or odpowiedz_openai()
        self.wywolania: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **parametry):
        self.wywolania.append(parametry)
        if self.bledy:
            raise self.bledy.pop(0)
        return self.odpowiedz


# ---------------------------------------------------------------------------
# KONFIGURACJA PROVIDERA
# ---------------------------------------------------------------------------
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


def test_problemy_nazywaja_kazdy_brak_z_osobna():
    """Komunikat ma dać się przeczytać o trzeciej w nocy i powiedzieć, co zrobić.

    Sprawdzamy KAŻDY brak osobno, a nie „jest jakiś problem": na maszynie
    z zainstalowanym SDK i bez klucza ma paść o kluczu, a nie o pakiecie.
    """
    with ustaw(OPENAI_API_KEY="", OPENAI_MODEL="gpt-5-mini"):
        assert any("OPENAI_API_KEY" in b for b in llm.problemy(llm.OPENAI))
    with ustaw(OPENAI_API_KEY="sk-x", OPENAI_MODEL=""):
        assert any("OPENAI_MODEL" in b for b in llm.problemy(llm.OPENAI))
    if not llm._sdk_obecny(llm.GEMINI):
        assert any("pip install" in b for b in llm.problemy(llm.GEMINI))


def test_model_openai_nie_ma_wartosci_domyslnej():
    """Nazwa modelu ma być podana ŚWIADOMIE — nie ma się wziąć z kodu.

    Domyślna znaczyłaby, że po wpisaniu samego klucza system odpala model,
    którego nikt nie wybrał, i płaci stawkę, której nikt nie sprawdzał.
    """
    with ustaw(OPENAI_MODEL=""):
        assert llm.model_domyslny(llm.OPENAI) == ""
        assert any("OPENAI_MODEL" in b for b in llm.problemy(llm.OPENAI))


def test_brak_openai_nie_psuje_anthropic():
    """KRYTERIUM ODBIORU: brak klucza OpenAI przy providerze anthropic = zero skutków."""
    with ustaw(LLM_PROVIDER="anthropic", OPENAI_API_KEY="", OPENAI_MODEL=""):
        assert llm.normalizuj_provider(settings.LLM_PROVIDER) == llm.ANTHROPIC
        assert not any("OPENAI" in b for b in llm.problemy(llm.ANTHROPIC))
        assert "provider=anthropic" in llm.opis()


def test_opis_nie_wywala_bez_konfiguracji():
    """Linia startowa ma powstać zawsze — także na świeżym klonie."""
    tekst = llm.opis()
    assert "provider=" in tekst and "model=" in tekst


def test_gotowe_providery_to_podzbior_znanych():
    assert set(llm.gotowe_providery()) <= set(llm.PROVIDERY)


def test_domyslny_model_to_haiku():
    """Zadanie ekstrakcji, nie rozumowania — Haiku robi to za ułamek ceny.

    Liczy się też czas: każda sekunda opóźnienia to przewaga konkurencji.
    """
    assert llm.model_domyslny(llm.ANTHROPIC).startswith("claude-haiku-4-5")


def test_klucz_api_nie_wycieka_do_zadnego_komunikatu():
    """KRYTERIUM ODBIORU: w logach nie ma ANI FRAGMENTU klucza.

    Sprawdzamy wszystko, co ten moduł potrafi wypisać: linię startową, listę
    braków i komunikat błędu. Klucz w logu zostaje tam na zawsze — logi idą
    do maila od crona i do pliku, którego nikt nie rotuje.
    """
    sekret = "sk-proj-TAJNYKLUCZ1234567890abcdef"
    with ustaw(OPENAI_API_KEY=sekret, OPENAI_MODEL="gpt-5-mini", LLM_PROVIDER="openai"):
        komunikaty = [llm.opis(), *llm.problemy(llm.OPENAI), settings.opis_srodowiska()]
        komunikaty.append(str(llm._zmapuj_blad(
            blad_api(401, tresc=f"Incorrect API key provided: {sekret}"),
            llm.OPENAI, "gpt-5-mini")))
        for tekst in komunikaty:
            # Fragment, nie tylko całość: pół klucza w logu jest tak samo złe.
            for kawalek in (sekret, sekret[:12], sekret[-12:], "TAJNYKLUCZ"):
                assert kawalek not in tekst, f"klucz wyciekł do: {tekst[:120]!r}"


# ---------------------------------------------------------------------------
# CENNIK I ZUŻYCIE
# ---------------------------------------------------------------------------
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


def test_stawka_dopasowuje_sie_do_wariantu_z_data():
    """Wariant z datą ma trafiać na tę samą stawkę co alias."""
    assert cennik.koszt_usd("claude-haiku-4-5-20251001", 1_000_000, 0) == \
        cennik.koszt_usd("claude-haiku-4-5", 1_000_000, 0)


def test_prefiks_nie_lapie_innej_wielkosci_modelu():
    """"gpt-5.1-mini" NIE MOŻE dostać stawki "gpt-5.1".

    Inna wielkość modelu to inna cena, a taka pomyłka niczego nie wywala —
    po cichu zawyża rachunek o rząd wielkości w jedynej kolumnie, dla której
    porównywarka istnieje.
    """
    assert cennik.stawki("gpt-5.1") is not None
    assert cennik.stawki("gpt-5.1-mini") is None


def test_cache_liczony_tansza_stawka():
    """Tokeny z cache kosztują dziesiątą część wejściowych — u obu dostawców."""
    pelne = cennik.koszt_usd("claude-haiku-4-5", 1_000_000, 0, 0)
    z_cache = cennik.koszt_usd("claude-haiku-4-5", 0, 0, 1_000_000)
    assert abs(z_cache - pelne / 10) < 1e-9


def test_rozumowanie_nie_jest_liczone_dwa_razy():
    """Tokeny rozumowania SIEDZĄ w wyjściu — koszt nie może ich dodać ponownie.

    Osobna kolumna w raporcie i osobna pozycja na rachunku to dwie różne
    rzeczy. Pomylenie ich podwaja koszt modelu, który dużo myśli.
    """
    duzo_mysli = llm.Zuzycie(tokeny_wejscia=0, tokeny_wyjscia=1_000_000,
                             tokeny_rozumowania=900_000, model="claude-haiku-4-5")
    nic_nie_mysli = llm.Zuzycie(tokeny_wejscia=0, tokeny_wyjscia=1_000_000,
                                tokeny_rozumowania=0, model="claude-haiku-4-5")
    assert duzo_mysli.koszt_usd() == nic_nie_mysli.koszt_usd()


def test_cennik_extra_dopisuje_model_bez_deployu():
    with ustaw(CENNIK_EXTRA='{"model-testowy": [1.0, 2.0, 0.1]}'):
        assert cennik.stawki("model-testowy") == (1.0, 2.0, 0.1)


def test_cennik_extra_bez_stawki_cache_nie_zaklada_znizki():
    """Dwie liczby = cache płatny jak wejście. Zgadnięta zniżka zaniżyłaby rachunek."""
    with ustaw(CENNIK_EXTRA='{"model-testowy": [1.0, 2.0]}'):
        assert cennik.stawki("model-testowy") == (1.0, 2.0, 1.0)


def test_smiec_w_cennik_extra_nie_zatrzymuje_runu():
    with ustaw(CENNIK_EXTRA="{to nie jest json"):
        assert cennik.stawki("claude-haiku-4-5") is not None


def test_etykieta_niesie_tryb_json_obok_modelu():
    """UCZCIWOŚĆ PORÓWNANIA: tryb ma stać przy modelu, nie osobno w notatkach."""
    z = llm.Zuzycie(model="gpt-5-mini", provider="openai", tryb="schema")
    assert "gpt-5-mini" in z.etykieta() and "schema" in z.etykieta()


# ---------------------------------------------------------------------------
# KSZTAŁT WYNIKU — kontrakt z zadania i zgodność wstecz
# ---------------------------------------------------------------------------
def test_odpowiedz_rozpakowuje_sie_jak_krotka():
    """`tekst, zuzycie = zapytaj_ze_zuzyciem(...)` — kształt z zadania."""
    odp = llm.Odpowiedz("{}", llm.Zuzycie(model="m", ms=120))
    tekst, zuzycie = odp
    assert tekst == "{}" and zuzycie.ms == 120


def test_odpowiedz_ma_stare_nazwy_pol():
    """KRYTERIUM ODBIORU: workers/classifier.py działa BEZ JEDNEJ ZMIANY.

    Klasyfikator czyta `odp.tekst`, `odp.provider`, `odp.model`, `odp.ms`
    i `odp.tokeny_wejscie` / `odp.tokeny_wyjscie`. Warstwa, która przy
    dokładaniu providera każe poprawiać klasyfikator, nie robi tego,
    po co powstała.
    """
    odp = llm.Odpowiedz("{}", llm.Zuzycie(tokeny_wejscia=10, tokeny_wyjscia=20,
                                          model="m", ms=120, provider="anthropic"))
    assert (odp.tekst, odp.provider, odp.model, odp.ms) == ("{}", "anthropic", "m", 120)
    assert (odp.tokeny_wejscie, odp.tokeny_wyjscie) == (10, 20)


def test_zapytaj_bez_konfiguracji_rzuca_wlasnym_typem():
    """Nie ImportError w środku runu — własny wyjątek, łapany przez wołającego."""
    with ustaw(OPENAI_API_KEY="", OPENAI_MODEL=""):
        try:
            llm.zapytaj_ze_zuzyciem("system", "user", 100, provider=llm.OPENAI, model="x")
        except llm.LLMNiedostepny as e:
            assert "openai" in str(e)
            # Brak konfiguracji jest TRWAŁY: ponawianie nie dopisze klucza.
            assert isinstance(e, llm.LLMBladTrwaly)
            return
    raise AssertionError("brak konfiguracji musi dać LLMNiedostepny")


def test_blad_trwaly_jest_podtypem_niedostepnego():
    """Kto łapie LLMNiedostepny, łapie i trwały — fetcher nie wymaga zmiany."""
    assert issubclass(llm.LLMBladTrwaly, llm.LLMNiedostepny)


# ---------------------------------------------------------------------------
# OPENAI: NAZWA PARAMETRU LIMITU
#
# Nowe modele odrzucają `max_tokens` i wymagają `max_completion_tokens`, ale
# część starszych jest odwrotnie. Z nazwy modelu tego nie widać, więc pytamy
# serwer i zapamiętujemy odpowiedź.
# ---------------------------------------------------------------------------
def _wyczysc_pamiec():
    llm._NAZWA_LIMITU.clear()
    llm._BEZ_ROZUMOWANIA.clear()
    llm._ostrzezenia.clear()


def test_probuje_najpierw_nowszej_nazwy_limitu():
    _wyczysc_pamiec()
    klient = KlientAtrapa()
    with ustaw(OPENAI_REASONING="low"):
        llm._wolaj_openai(klient, {"model": "m"}, 700, "m")
    assert "max_completion_tokens" in klient.wywolania[0]
    assert "max_tokens" not in klient.wywolania[0]


def test_wraca_do_starszej_nazwy_po_odmowie_i_zapamietuje():
    """Jedna odmowa na proces, nie jedna na post — inaczej płacimy dwa razy za każdy."""
    _wyczysc_pamiec()
    odmowa = blad_api(400, kod="unsupported_parameter", param="max_completion_tokens",
                      tresc="Unsupported parameter: 'max_completion_tokens'")
    klient = KlientAtrapa(bledy=[odmowa])
    with ustaw(OPENAI_REASONING="low"):
        llm._wolaj_openai(klient, {"model": "stary"}, 700, "stary")
        assert "max_tokens" in klient.wywolania[1]
        assert llm._NAZWA_LIMITU["stary"] == "max_tokens"

        # Drugi post: od razu poprawną nazwą, bez płatnego wywołania na próbę.
        drugi = KlientAtrapa()
        llm._wolaj_openai(drugi, {"model": "stary"}, 700, "stary")
    assert list(drugi.wywolania[0]).count("max_tokens") == 1
    assert "max_completion_tokens" not in drugi.wywolania[0]


def test_nie_zgaduje_z_nazwy_modelu():
    """Ten sam kod ma obsłużyć model, o którym nic nie wie.

    Nazwa modelu nie mówi nic o tym, które parametry przyjmuje — jedynym
    źródłem prawdy jest odpowiedź serwera.
    """
    _wyczysc_pamiec()
    klient = KlientAtrapa()
    with ustaw(OPENAI_REASONING="low"):
        llm._wolaj_openai(klient, {"model": "model-z-przyszlosci"}, 700, "model-z-przyszlosci")
    assert "max_completion_tokens" in klient.wywolania[0]


def test_spor_o_nazwe_limitu_konczy_sie_po_obu_probach():
    """400 o innej przyczynie nie może odbijać żądania między nazwami w kółko.

    Serwer odrzucający OBIE nazwy mówi o czymś innym niż nazwa — wtedy ma paść
    JEGO błąd, a nie nasz komunikat o parametrach, i po dwóch płatnych próbach,
    nie po trzech.
    """
    _wyczysc_pamiec()
    klient = KlientAtrapa(bledy=[
        blad_api(400, kod="unsupported_parameter", param="max_completion_tokens",
                 tresc="Unsupported parameter: 'max_completion_tokens'"),
        blad_api(400, kod="unsupported_parameter", param="max_tokens",
                 tresc="Unsupported parameter: 'max_tokens'"),
        blad_api(400, kod="unsupported_parameter", param="max_completion_tokens",
                 tresc="Unsupported parameter: 'max_completion_tokens'"),
    ])
    with ustaw(OPENAI_REASONING="low"):
        try:
            llm._wolaj_openai(klient, {"model": "sporny"}, 700, "sporny")
        except RuntimeError:
            assert len(klient.wywolania) == 2
            return
    raise AssertionError("po odrzuceniu obu nazw ma paść błąd serwera")


def test_inny_blad_400_nie_kreci_petli_ponowien():
    """Ponawianie po DOWOLNYM 400 zamieniłoby literówkę w płatną pętlę."""
    _wyczysc_pamiec()
    klient = KlientAtrapa(bledy=[blad_api(400, kod="invalid_request_error",
                                          param="messages", tresc="zły prompt")] * 5)
    try:
        llm._wolaj_openai(klient, {"model": "m"}, 700, "m")
    except RuntimeError:
        assert len(klient.wywolania) == 1
        return
    raise AssertionError("błąd niedotyczący adaptowanych parametrów ma lecieć dalej")


# ---------------------------------------------------------------------------
# OPENAI: TOKENY ROZUMOWANIA
# ---------------------------------------------------------------------------
def test_reasoning_wysylany_gdy_ustawiony():
    _wyczysc_pamiec()
    klient = KlientAtrapa()
    with ustaw(OPENAI_REASONING="minimal"):
        llm._wolaj_openai(klient, {"model": "m"}, 700, "m")
    assert klient.wywolania[0]["reasoning_effort"] == "minimal"
    # Skoro sterujemy nakładem rozumowania, limit zostaje taki, jak podano.
    assert klient.wywolania[0]["max_completion_tokens"] == 700


def test_brak_reasoning_podnosi_limit_o_zapas():
    """Bez sterowania rozumowaniem limit musi mieć zapas.

    Inaczej rozumowanie zjada limit, odpowiedź urywa się w środku JSON-a
    i w raporcie widać „model nie umie w JSON" zamiast „za mały limit".
    Zapas to SUFIT, nie wydatek — płacimy za tokeny wygenerowane.
    """
    _wyczysc_pamiec()
    klient = KlientAtrapa()
    with ustaw(OPENAI_REASONING=""):
        llm._wolaj_openai(klient, {"model": "m"}, 700, "m")
    assert "reasoning_effort" not in klient.wywolania[0]
    assert klient.wywolania[0]["max_completion_tokens"] == 700 + llm.ZAPAS_NA_ROZUMOWANIE


def test_reasoning_odrzucony_jest_odstawiany_a_limit_rosnie():
    _wyczysc_pamiec()
    odmowa = blad_api(400, kod="unsupported_parameter", param="reasoning_effort",
                      tresc="Unsupported parameter: 'reasoning_effort'")
    klient = KlientAtrapa(bledy=[odmowa])
    with ustaw(OPENAI_REASONING="minimal"):
        llm._wolaj_openai(klient, {"model": "bez-rozumowania"}, 700, "bez-rozumowania")
    assert "reasoning_effort" not in klient.wywolania[1]
    assert klient.wywolania[1]["max_completion_tokens"] == 700 + llm.ZAPAS_NA_ROZUMOWANIE
    assert "bez-rozumowania" in llm._BEZ_ROZUMOWANIA


def test_zla_wartosc_reasoning_tez_odstawia_parametr():
    """Poziomy zależą od modelu — "minimal" bywa nieznane. Nie zgadujemy."""
    _wyczysc_pamiec()
    odmowa = blad_api(400, kod="invalid_value", param="reasoning_effort",
                      tresc="Invalid value: 'minimal'")
    klient = KlientAtrapa(bledy=[odmowa])
    with ustaw(OPENAI_REASONING="minimal"):
        llm._wolaj_openai(klient, {"model": "m"}, 700, "m")
    assert len(klient.wywolania) == 2
    assert "reasoning_effort" not in klient.wywolania[1]


def test_zapas_liczony_od_nowa_przy_kazdym_podejsciu():
    """Zapas nie może narastać między próbami — 700 + zapas, nigdy + 2 x zapas."""
    _wyczysc_pamiec()
    odmowa = blad_api(400, kod="unsupported_parameter", param="max_completion_tokens",
                      tresc="Unsupported parameter: 'max_completion_tokens'")
    klient = KlientAtrapa(bledy=[odmowa])
    with ustaw(OPENAI_REASONING=""):
        llm._wolaj_openai(klient, {"model": "m"}, 700, "m")
    assert klient.wywolania[1]["max_tokens"] == 700 + llm.ZAPAS_NA_ROZUMOWANIE


# ---------------------------------------------------------------------------
# OPENAI: KSZTAŁT ODPOWIEDZI
#
# Każdy z tych przypadków to realna odpowiedź API, nie teoria — i każdy bez
# tej obsługi daje AttributeError w środku runu zamiast wyjątku, który
# klasyfikator umie złapać.
# ---------------------------------------------------------------------------
def test_pusta_lista_choices_daje_wyjatek_a_nie_attributeerror():
    odp = SimpleNamespace(choices=[], usage=None)
    try:
        llm._rozlicz_openai(odp, "m", "object", 700)
    except llm.LLMNiedostepny as e:
        assert "choices" in str(e)
        return
    raise AssertionError("pusta lista choices musi dać LLMNiedostepny")


def test_content_none_po_filtrze_tresci():
    odp = odpowiedz_openai(tresc=None, powod="content_filter")
    try:
        llm._rozlicz_openai(odp, "m", "object", 700)
    except llm.LLMNiedostepny as e:
        assert "filtr" in str(e).lower()
        return
    raise AssertionError("zadziałany filtr treści musi dać LLMNiedostepny")


def test_odmowa_modelu_jest_wyjatkiem_a_nie_pusta_odpowiedzia():
    odp = odpowiedz_openai(tresc=None, odmowa="nie mogę pomóc")
    try:
        llm._rozlicz_openai(odp, "m", "schema", 700)
    except llm.LLMNiedostepny as e:
        assert "odmówił" in str(e)
        return
    raise AssertionError("odmowa musi dać LLMNiedostepny")


def test_obciecie_na_limicie_nie_udaje_zlego_jsona():
    """Urwany JSON to błąd LIMITU, nie błąd modelu.

    Bez tego rozróżnienia obcięta odpowiedź trafia do raportu jako „model nie
    umie w JSON" i szuka się przyczyny w promptcie zamiast w konfiguracji.
    """
    odp = odpowiedz_openai(tresc='{"czy_zlecenie": tr', powod="length")
    try:
        llm._rozlicz_openai(odp, "m", "object", 2700)
    except llm.LLMNiedostepny as e:
        assert "obcięta" in str(e) and "2700" in str(e)
        assert "NIE jest wina modelu" in str(e)
        return
    raise AssertionError("obcięcie na limicie musi być widoczne jako obcięcie")


def test_wejscie_pomniejszone_o_tokeny_z_cache():
    """OpenAI liczy cache WEWNĄTRZ prompt_tokens — Anthropic obok nich.

    Bez tego odjęcia płacilibyśmy w raporcie pełną stawkę za tokeny, które
    dostawca liczy po dziesiątej części.
    """
    _, tekst = llm._rozlicz_openai(
        odpowiedz_openai(wejscie=1000, cache=800), "m", "object", 700)
    zuzycie, _ = llm._rozlicz_openai(
        odpowiedz_openai(wejscie=1000, cache=800), "m", "object", 700)
    assert tekst == "{}"
    assert zuzycie.tokeny_wejscia == 200
    assert zuzycie.tokeny_cache == 800


def test_rozumowanie_wyciagane_osobno_ale_zostaje_w_wyjsciu():
    zuzycie, _ = llm._rozlicz_openai(
        odpowiedz_openai(wyjscie=500, rozumowanie=400), "m", "object", 700)
    assert zuzycie.tokeny_wyjscia == 500      # całość, tak liczy dostawca
    assert zuzycie.tokeny_rozumowania == 400  # podzbiór, do raportu


def test_brak_licznikow_nie_wywala_rozliczenia():
    """Provider bez `usage` ma dać zera i koszt, nie AttributeError."""
    odp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}", refusal=None),
                                 finish_reason="stop")],
        usage=None)
    zuzycie, tekst = llm._rozlicz_openai(odp, "m", "off", 700)
    assert tekst == "{}" and zuzycie.tokeny_wejscia == 0


def test_tryb_jedzie_w_zuzyciu():
    zuzycie, _ = llm._rozlicz_openai(odpowiedz_openai(), "gpt-5-mini", "schema", 700)
    assert zuzycie.tryb == "schema" and zuzycie.provider == llm.OPENAI


# ---------------------------------------------------------------------------
# BŁĘDY: PRZEJŚCIOWE vs TRWAŁE
# ---------------------------------------------------------------------------
def test_bledy_przejsciowe_nie_zatrzymuja_runu():
    """Limit zapytań, timeout i 5xx mijają — post wraca do kolejki."""
    for blad in (blad_api(429, kod="rate_limit_exceeded", tresc="za dużo zapytań"),
                 blad_api(500, tresc="wewnętrzny błąd serwera"),
                 blad_api(503, tresc="przeciążenie"),
                 blad_api(None, tresc="Request timed out")):
        zmapowany = llm._zmapuj_blad(blad, llm.OPENAI, "m")
        assert isinstance(zmapowany, llm.LLMNiedostepny)
        assert not isinstance(zmapowany, llm.LLMBladTrwaly), str(blad)


def test_bledy_trwale_zatrzymuja_run_z_komunikatem():
    """Zły klucz, brak dostępu, nieznany model, odrzucony parametr.

    Każdy z nich ponawia się w nieskończoność bez najmniejszego skutku, więc
    ma zatrzymać przebieg — i powiedzieć, którą zmienną poprawić.
    """
    przypadki = {
        401: "OPENAI_API_KEY",
        403: "uprawnienia",
        404: "OPENAI_MODEL",
        400: "parametry",
        422: "niepoprawne",
    }
    for status, fragment in przypadki.items():
        zmapowany = llm._zmapuj_blad(blad_api(status), llm.OPENAI, "m")
        assert isinstance(zmapowany, llm.LLMBladTrwaly), status
        assert fragment in str(zmapowany), (status, str(zmapowany))


def test_wyczerpany_limit_konta_jest_trwaly_mimo_kodu_429():
    """429 zwykle znaczy „zwolnij", ale przy braku środków nie zmieni się nigdy."""
    blad = blad_api(429, kod="insufficient_quota", tresc="You exceeded your current quota")
    assert isinstance(llm._zmapuj_blad(blad, llm.OPENAI, "m"), llm.LLMBladTrwaly)


def test_komunikat_bledu_niesie_providera_i_model():
    """Log ma powiedzieć, KTÓRY model padł — przy porównaniu chodzi ich kilka."""
    tekst = str(llm._zmapuj_blad(blad_api(500), llm.OPENAI, "gpt-5-mini"))
    assert "openai" in tekst and "gpt-5-mini" in tekst


# ---------------------------------------------------------------------------
# TRYB JSON
# ---------------------------------------------------------------------------
def test_tryb_json_domyslnie_object():
    with ustaw(OPENAI_JSON_MODE=""):
        assert llm.tryb_json(llm.OPENAI) == llm.JSON_OBJECT


def test_nieznany_tryb_json_degraduje_zamiast_wywalac():
    """Literówka w .env nie zatrzymuje crona — ale ma być widoczna."""
    llm._ostrzezenia.clear()
    with ustaw(OPENAI_JSON_MODE="scheme"):
        assert llm.tryb_json(llm.OPENAI) == llm.JSON_OBJECT


def test_tryb_json_nie_dotyczy_anthropic():
    """Haiku dostaje ten sam prompt i żadnej pomocy — na tym polega porównanie."""
    with ustaw(OPENAI_JSON_MODE="schema"):
        assert llm.tryb_json(llm.ANTHROPIC) == llm.JSON_OFF


def test_response_format_per_tryb():
    assert llm._response_format(llm.JSON_OFF) is None
    assert llm._response_format(llm.JSON_OBJECT) == {"type": "json_object"}
    assert llm._response_format(llm.JSON_SCHEMA)["type"] == "json_schema"


def test_rola_systemowa_domyslnie_system_ale_przelaczalna():
    with ustaw(OPENAI_ROLA_SYSTEMOWA=""):
        assert llm._rola_systemowa() == "system"
    with ustaw(OPENAI_ROLA_SYSTEMOWA="developer"):
        assert llm._rola_systemowa() == "developer"


# ---------------------------------------------------------------------------
# SCHEMAT (structured outputs)
# ---------------------------------------------------------------------------
def test_schemat_spelnia_wymagania_trybu_scislego():
    """Brak `required` albo `additionalProperties` = błąd 400 przy pierwszym poście.

    Sprawdzamy REKURENCYJNIE, bo tryb ścisły wymaga tego na KAŻDYM poziomie,
    a zagnieżdżone obiekty (odbior, stan, kontakt) łatwo przeoczyć.
    """
    def sprawdz(wezel, sciezka="root"):
        if wezel.get("type") == "object":
            assert wezel.get("additionalProperties") is False, sciezka
            assert set(wezel.get("required", [])) == set(wezel["properties"]), sciezka
            for nazwa, pod in wezel["properties"].items():
                sprawdz(pod, f"{sciezka}.{nazwa}")

    sprawdz(schemat.schemat())


def test_schemat_bierze_zbiory_z_klasyfikatora():
    """Jedno źródło prawdy: schemat NIE MOŻE mieć własnej kopii list wartości.

    Druga kopia rozjechałaby się przy pierwszej dopisanej kategorii i tryb
    "schema" gwarantowałby zgodność z kontraktem, którego walidator nie zna.
    """
    from laweta_radar.workers import classifier

    pola = schemat.schemat()["properties"]
    assert pola["typ"]["enum"] == list(classifier._POPRAWNE_TYP)
    assert pola["pilnosc"]["enum"] == list(classifier._POPRAWNE_PILNOSC)
    assert pola["kontakt"]["properties"]["typ"]["enum"] == list(classifier._POPRAWNE_KONTAKT)


# ---------------------------------------------------------------------------
# CAŁA ŚCIEŻKA: KLASYFIKATOR -> LLM -> OPENAI
#
# KRYTERIUM ODBIORU brzmi: „LLM_PROVIDER=openai i LLM_PROVIDER=anthropic
# przechodzą ten sam zestaw testów klasyfikatora, bez ani jednej zmiany
# w workers/classifier.py". Test niżej sprawdza to dosłownie — puszcza post
# przez `klasyfikuj()` z podstawionym SDK i patrzy, czy wychodzi kontrakt.
#
# SDK podstawiamy w `sys.modules`, żeby test chodził TAKŻE na maszynie bez
# pakietu `openai` — czyli w domyślnej konfiguracji tego repo.
# ---------------------------------------------------------------------------
@contextmanager
def podstawione_sdk_openai(klient):
    import importlib.machinery  # noqa: PLC0415
    import types  # noqa: PLC0415

    atrapa = types.ModuleType("openai")
    atrapa.OpenAI = lambda **_: klient
    # Bez `__spec__` `importlib.util.find_spec` uzna moduł za niepełny
    # i `problemy()` nadal krzyczałoby o brakującym pakiecie.
    atrapa.__spec__ = importlib.machinery.ModuleSpec("openai", None)
    stare = sys.modules.get("openai")
    sys.modules["openai"] = atrapa
    llm.zapomnij_klientow()
    try:
        yield
    finally:
        if stare is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = stare
        llm.zapomnij_klientow()


ODPOWIEDZ_MODELU = """{
  "czy_zlecenie": true,
  "typ": "holowanie",
  "odbior":  {"raw": "Krosno, Bieszczadzka 12", "kod": "38-400", "miasto": "Krosno"},
  "dostawa": {"raw": "Rzeszow, warsztat", "kod": null, "miasto": "Rzeszow"},
  "pojazd":  {"opis": "VW Golf IV", "kategoria": "osobowy"},
  "stan":    {"toczy_sie": true, "ma_kola": true, "po_wypadku": false, "uwagi": "nie odpala"},
  "pilnosc": "teraz",
  "kontakt": {"typ": "telefon", "wartosc": "555111222"},
  "cena_sugerowana": null,
  "pewnosc": 90,
  "powod": "autor szuka lawety"
}"""


def test_klasyfikator_dziala_przez_openai_bez_zmian_w_nim_samym():
    _wyczysc_pamiec()
    from laweta_radar.workers import classifier  # noqa: PLC0415

    klient = KlientAtrapa(odpowiedz=odpowiedz_openai(tresc=ODPOWIEDZ_MODELU))
    with ustaw(LLM_PROVIDER="openai", OPENAI_API_KEY="sk-test-1234567890",
               OPENAI_MODEL="gpt-5-mini", OPENAI_JSON_MODE="object",
               OPENAI_REASONING="minimal"), podstawione_sdk_openai(klient):
        wynik = classifier.klasyfikuj("zdechl mi golf w Krosnie, kto wezmie do Rzeszowa",
                                      grupa="Pomoc drogowa", jezyk="pl")

    assert wynik["czy_zlecenie"] is True
    assert wynik["odbior"]["miasto"] == "Krosno"
    assert wynik["kontakt"] == {"typ": "telefon", "wartosc": "555111222"}
    assert classifier.warto_budzic(wynik)


def test_prompt_systemowy_idzie_pierwsza_wiadomoscia_a_post_druga():
    """Anthropic bierze system osobnym parametrem, OpenAI — pierwszą wiadomością.

    Rozdział nie jest kosmetyką: treść posta z grupy FB to NIEZAUFANY input
    i nie wolno jej sklejać z instrukcjami. Gdyby ta warstwa je zlepiła,
    pierwszy lepszy żartowniś decydowałby, co system uzna za zlecenie.
    """
    _wyczysc_pamiec()
    from laweta_radar.workers import classifier  # noqa: PLC0415

    klient = KlientAtrapa(odpowiedz=odpowiedz_openai(tresc=ODPOWIEDZ_MODELU))
    with ustaw(LLM_PROVIDER="openai", OPENAI_API_KEY="sk-test-1234567890",
               OPENAI_MODEL="gpt-5-mini", OPENAI_ROLA_SYSTEMOWA="developer"), \
            podstawione_sdk_openai(klient):
        classifier.klasyfikuj("zdechl mi golf", jezyk="pl")

    wiadomosci = klient.wywolania[0]["messages"]
    assert [w["role"] for w in wiadomosci] == ["developer", "user"]
    assert "analitykiem zgłoszeń" in wiadomosci[0]["content"]
    assert "<post>" in wiadomosci[1]["content"]
    assert "analitykiem zgłoszeń" not in wiadomosci[1]["content"]


def test_awaria_openai_nie_kasuje_posta():
    """Post ma wrócić do kolejki, a nie zostać uznany za „nie zlecenie".

    Zwrócenie „to nie zlecenie" przy padniętym API byłoby cichą utratą kursu
    — najdroższym możliwym błędem w tym repo.
    """
    _wyczysc_pamiec()
    from laweta_radar.workers import classifier  # noqa: PLC0415

    klient = KlientAtrapa(bledy=[blad_api(503, tresc="przeciążenie")] * 3)
    with ustaw(LLM_PROVIDER="openai", OPENAI_API_KEY="sk-test-1234567890",
               OPENAI_MODEL="gpt-5-mini"), podstawione_sdk_openai(klient):
        try:
            classifier.klasyfikuj("zdechl mi golf w Krosnie")
        except llm.LLMNiedostepny:
            return
    raise AssertionError("awaria API nie może dawać cichego wyniku")


def test_tryb_schema_dokleja_response_format_do_wywolania():
    _wyczysc_pamiec()
    from laweta_radar.workers import classifier  # noqa: PLC0415

    klient = KlientAtrapa(odpowiedz=odpowiedz_openai(tresc=ODPOWIEDZ_MODELU))
    with ustaw(LLM_PROVIDER="openai", OPENAI_API_KEY="sk-test-1234567890",
               OPENAI_MODEL="gpt-5-mini", OPENAI_JSON_MODE="schema"), \
            podstawione_sdk_openai(klient):
        classifier.klasyfikuj("zdechl mi golf", jezyk="pl")

    format_odpowiedzi = klient.wywolania[0]["response_format"]
    assert format_odpowiedzi["json_schema"]["strict"] is True


def test_zamaskuj_wycina_klucz_z_dowolnego_tekstu():
    """Dostawca odsyła klucz w treści błędu 401 — nasz komunikat nie może go nieść."""
    with ustaw(OPENAI_API_KEY="sk-proj-ABCDEFGH12345678"):
        zamaskowany = llm.zamaskuj("Incorrect API key provided: sk-proj-ABCDEFGH12345678")
        assert "ABCDEFGH" not in zamaskowany and "***" in zamaskowany
        # Także klucz, którego NIE MAMY w .env — regexp jest drugą linią obrony.
        assert "TAJNY" not in llm.zamaskuj("klucz sk-ant-api03-TAJNYSEKRET odrzucony")


def test_zamaskuj_nie_niszczy_komunikatu_przy_pustym_kluczu():
    """Podstawienie pustej wartości wstawiłoby maskę między każdą parę znaków."""
    with ustaw(OPENAI_API_KEY="", ANTHROPIC_API_KEY="", GEMINI_API_KEY=""):
        assert llm.zamaskuj("nieznany model gpt-5-mini") == "nieznany model gpt-5-mini"


def test_schemat_dopuszcza_nulle_tam_gdzie_kontrakt():
    """Null jest lepszy niż zgadnięta współrzędna — schemat musi go dopuszczać.

    Schemat wymuszający string w `miasto` zmusiłby model do zgadywania, czyli
    do dokładnie tego, czego zabrania prompt.
    """
    pola = schemat.schemat()["properties"]
    assert "null" in pola["odbior"]["properties"]["miasto"]["type"]
    assert "null" in pola["dostawa"]["properties"]["kod"]["type"]
    assert "null" in pola["cena_sugerowana"]["type"]
