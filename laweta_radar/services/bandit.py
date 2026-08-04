"""Thompson Sampling — rozdział ograniczonego budżetu prób między kandydatów.

W tym repo posłuży do rozdziału runów Apify między grupy FB: budżet jest wspólny
z drugim systemem na tym samym VPS-ie, więc każdy run wydany na martwą grupę to
run zabrany grupie, która realnie dowozi zlecenia.

DLACZEGO Thompson Sampling, a nie epsilon-greedy (uzasadnienie z oryginału):
  - Epsilon-greedy: 20% prób losowo, 80% na najlepszego — płaskie, marnuje próby.
  - Thompson: losuje z Beta(α, β) per kandydat. Automatycznie eksploruje tych
    z dużą niepewnością, eksploatuje tych z dużą liczbą sukcesów. Optymalny
    asymptotycznie, szybciej zbiega.

--- CO ZOSTAŁO PRZENIESIONE, A CO NIE -------------------------------------------

Oryginał (services/bandit.py w repo źródłowym) wybierał TECHNIKĘ SPRZEDAŻY dla
SEGMENTU klienta i czytał tabelę `sales_techniques_log` przez `ai.sales_techniques`.
Tabela, kolumny (meeting/brief/positive), katalog technik i metryka sukcesu
`3*meeting + 2*brief + 1*positive` to domena sprzedażowa — nie da się jej tu
przenieść, bo nie ma czego liczyć.

Przeniesiona jest MATEMATYKA, co do stałej:
  - próg zaufania lokalnym danym            MIN_LOKALNYCH_PROB = 15
  - waga danych globalnych poniżej progu    WAGA_GLOBALNA = 0.3
  - bonus do α dla kandydata preferowanego  BONUS_PRIOR = 0.5
  - podłoga parametrów Beta                 0.01
  - argmax po wylosowanej próbce
Pilnuje tego test `tests/test_bandit.py`, który liczy te same posteriory wprost
ze wzorów z oryginału i porównuje wynik.

Zmieniło się natomiast ŹRÓDŁO DANYCH: zamiast czytać SQL wewnątrz modułu,
statystyki przyjmujemy jako argument. Dwa powody. Po pierwsze, tabeli dla grup
jeszcze nie ma — powstanie razem z fetcherem, a zgadywanie jej schematu teraz
oznaczałoby kolumny wymyślone przed kodem, który je wypełnia. Po drugie,
oryginał łapał `except Exception` wokół zapytania i przy padniętej bazie po cichu
zwracał pustą statystykę, czyli bandyta „resetował się" do priorów i rozdawał
budżet równo — wyglądając przy tym na działającego. Funkcja czysta nie ma jak
tego ukryć: wołający sam decyduje, co zrobić, gdy nie ma danych.

Wobec tego moduł NIE dotyka bazy i nie importuje psycopg2 — jest w całości
testowalny offline.
"""
from __future__ import annotations

import random

# Ile prób lokalnych musi się uzbierać, żeby ufać wyłącznie im. Poniżej progu
# dokładamy dane globalne — inaczej kandydat z dwiema próbami i jednym sukcesem
# wyglądałby lepiej niż ten z pięćdziesięcioma i dwudziestoma.
MIN_LOKALNYCH_PROB = 15

# Waga danych globalnych, gdy lokalnych jest za mało. 0.3, a nie 1.0, bo globalne
# są tylko podpowiedzią: mówią „tak to zwykle wygląda", nie „tak jest tutaj".
WAGA_GLOBALNA = 0.3

# Bonus do α dla kandydata wskazanego jako preferowany. Mały celowo — ma dać
# przewagę na starcie, a nie przykryć danych, gdy te już są.
BONUS_PRIOR = 0.5

# Podłoga parametrów Beta. random.betavariate rozkłada się przy zerze.
_MIN_PARAM = 0.01


def _losuj_beta(alpha: float, beta: float) -> float:
    """Losuj z Beta(alpha, beta). `random.betavariate` jest dokładnie tym."""
    alpha = max(alpha, _MIN_PARAM)
    beta = max(beta, _MIN_PARAM)
    return random.betavariate(alpha, beta)


def posterior(
    proby_lokalne: int,
    sukcesy_lokalne: int,
    proby_globalne: int = 0,
    sukcesy_globalne: int = 0,
    preferowany: bool = False,
) -> tuple[float, float]:
    """Parametry (α, β) rozkładu Beta dla jednego kandydata.

    Wydzielone z pętli wyboru, żeby dało się to sprawdzić testem bez losowości —
    w oryginale ta matematyka siedziała w środku `pick_technique` i jedynym
    sposobem na jej weryfikację było uśrednianie tysięcy losowań.
    """
    if proby_lokalne >= MIN_LOKALNYCH_PROB:
        # Dość własnych danych — globalne tylko by je rozmyły.
        alpha = 1 + sukcesy_lokalne
        beta = 1 + (proby_lokalne - sukcesy_lokalne)
    else:
        alpha = 1 + sukcesy_lokalne + WAGA_GLOBALNA * sukcesy_globalne
        beta = (1 + (proby_lokalne - sukcesy_lokalne)
                + WAGA_GLOBALNA * max(proby_globalne - sukcesy_globalne, 0))
    if preferowany:
        alpha += BONUS_PRIOR
    return alpha, beta


def wybierz(
    kandydaci,
    statystyki_lokalne: dict | None = None,
    statystyki_globalne: dict | None = None,
    preferowani=None,
) -> str | None:
    """Wylosuj kandydata metodą Thompson Sampling. None, gdy nie ma z czego wybierać.

    `statystyki_*` to {klucz: {"proby": N, "sukcesy": M}} — dla rozdziału runów
    Apify: `proby` to ile razy pytaliśmy tę grupę, `sukcesy` to ile realnych
    zleceń z niej wyszło. Brakujący klucz znaczy „zero prób", czyli czysty prior.

    ODSTĘPSTWO OD ORYGINAŁU, świadome i jedyne: przy pustej liście kandydatów
    oryginał wchodził w `random.choice([])` i rzucał IndexError. Tutaj zwracamy
    None. W repo źródłowym lista technik nigdy nie była pusta, więc ta ścieżka
    nie miała jak się wykonać — u nas pusta lista grup to stan NORMALNY (świeży
    klon, wszystko `unverified`), a zasada w tym repo mówi wprost: brak
    konfiguracji kończy się komunikatem, nie wyjątkiem wywalającym crona.
    """
    kandydaci = list(kandydaci)
    if not kandydaci:
        return None

    lokalne = statystyki_lokalne or {}
    globalne = statystyki_globalne or {}
    preferowani = set(preferowani or ())

    najlepszy = None
    najlepsza_probka = -1.0
    for klucz in kandydaci:
        ls = lokalne.get(klucz) or {}
        gs = globalne.get(klucz) or {}
        alpha, beta = posterior(
            proby_lokalne=int(ls.get("proby", 0)),
            sukcesy_lokalne=int(ls.get("sukcesy", 0)),
            proby_globalne=int(gs.get("proby", 0)),
            sukcesy_globalne=int(gs.get("sukcesy", 0)),
            preferowany=klucz in preferowani,
        )
        probka = _losuj_beta(alpha, beta)
        if probka > najlepsza_probka:
            najlepsza_probka = probka
            najlepszy = klucz
    return najlepszy


def rozdziel_budzet(
    kandydaci,
    ile_prob: int,
    statystyki_lokalne: dict | None = None,
    statystyki_globalne: dict | None = None,
    preferowani=None,
) -> dict[str, int]:
    """Rozdziel `ile_prob` prób między kandydatów — {klucz: ile razy wylosowany}.

    Nadbudowa nad `wybierz`, a nie zmiana algorytmu: losujemy tyle razy, ile mamy
    prób, i zliczamy trafienia. Dzięki temu rozdział jest miękki — kandydat słabszy
    nadal dostaje czasem próbę i ma jak się wykazać, zamiast zostać skazany na
    zawsze przez kilka pierwszych pechowych runów. Przy budżecie Apify wspólnym
    z drugim systemem to jest właśnie ta różnica, którą chcemy: budżet płynie tam,
    gdzie coś z niego wychodzi, ale nie zamyka się na resztę.
    """
    wynik: dict[str, int] = {}
    for _ in range(max(0, int(ile_prob))):
        k = wybierz(kandydaci, statystyki_lokalne, statystyki_globalne, preferowani)
        if k is None:
            break
        wynik[k] = wynik.get(k, 0) + 1
    return wynik
