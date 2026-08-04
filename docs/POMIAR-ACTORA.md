# Pomiar actora `apify/facebook-groups-scraper`

> ## ⚠️ POMIAR NIE ZOSTAŁ JESZCZE WYKONANY
>
> Ten plik jest **zaślepką**. Nie ma w nim ani jednej zmierzonej liczby, bo pomiar
> wymaga żywych kluczy Apify i publicznej grupy testowej — a repo nie ma jednego
> ani drugiego. **Prompt 2 nie może ruszyć, dopóki tu stoi ta ramka.**
>
> Zaślepka istnieje po to, żeby brak pomiaru był widoczny. Brakujący plik wygląda
> jak „jeszcze do niego nie doszliśmy" i kusi, żeby napisać fetchera na wyczucie.

Uruchomienie pomiaru **nadpisze cały ten plik** wynikami:

```bash
export PYTHONPATH=$PWD
python -m laweta_radar.scripts.pomiar_actora \
    --grupa https://www.facebook.com/groups/PUBLICZNA_TESTOWA \
    --grupa https://www.facebook.com/groups/DRUGA \
    --grupa https://www.facebook.com/groups/TRZECIA
```

Zajmuje ~20 minut, kosztuje **najwyżej 270 pobranych postów** (≈ 1,35 USD po cenie
katalogowej, ≈ 27 % miesięcznego kredytu jednego darmowego konta). Skrypt pokaże
tę liczbę jeszcze raz i poczeka na potwierdzenie, zanim cokolwiek wyda.

---

## Po co ten pomiar

Trzy pytania, na które nie da się odpowiedzieć z dokumentacji, bo actory zmieniają
zachowanie między wersjami — a każda z odpowiedzi zmienia architekturę fetchera,
nie jego szczegół.

### Pytanie 1 — czy `onlyPostsNewerThan` naprawdę działa?

Seria wywołań dla jednej grupy, zmienia się **wyłącznie** to pole: `7 days`,
`1 day`, `12 hours`, `1 hour`, `30 minutes`.

| wynik | co znaczy | co z tego wynika |
|---|---|---|
| **ŚCIEŻKA A** — liczba postów albo wiek najstarszego maleje przy zwężaniu okna, nic nie wychodzi poza okno | filtr czasowy działa | fetcher pobiera sam **przyrost** od ostatniego przebiegu; wolno chodzić gęsto, bo gęściej znaczy mniejsze okno, a nie większy rachunek |
| **ŚCIEŻKA B** — wyniki dla `1 hour` i `1 day` identyczne, albo posty wychodzą poza okno, albo actor odrzuca okna poniżej doby | pole jest ignorowane | **każdy** przebieg pobiera i opłaca te same posty od nowa; `resultsLimit` musi być mały, przebieg rzadszy, a koszt liczy się jako `grupy × resultsLimit × przebiegi` bez żadnej ulgi |

To jest najważniejsza liczba w projekcie. Przy przebiegu co 5 minut różnica między
A i B to rząd 288× w rachunku za tę samą grupę.

Deduplikacja w bazie **nie ratuje ścieżki B**: oszczędza model i Telegram, ale za
pobranie postu Apify policzył już wcześniej.

### Pytanie 2 — `resultsLimit` przy wielu grupach w `startUrls`

Ten sam limit raz dla jednej grupy, raz dla trzech.

- **LIMIT PER GRUPA** (trzy grupy ≈ 3 × limit) → grupy wolno batchować w jednym
  wywołaniu.
- **LIMIT GLOBALNY** (trzy grupy ≈ limit) → batchowanie jest **zakazane**. Batch po
  dziesięć grup zgubiłby posty z ośmiu z nich, a run i tak zostałby policzony.

### Pytanie 3 — ile realnie kosztuje jeden pobrany post

Liczone dwoma niezależnymi sposobami: z licznika konta
(`laweta_radar/workers/apify_credits.py`) i z `usageTotalUsd` każdego runu.
Zgodność znaczy, że pomiar jest wiarygodny; rozjazd jest informacją, a nie błędem
— licznik konta agreguje z opóźnieniem, więc tuż po serii bywa zaniżony.

Cena katalogowa ze strony actora to **~0,005 USD za post** (~5 USD za 1000). Nie
jest wynikiem pomiaru — służy tylko do oszacowania kosztu serii **przed** jej
odpaleniem i do porównania z liczbą zmierzoną.

Stąd liczy się flotę kont: darmowe konto to ~5 USD miesięcznie, czyli ~1000 postów
po cenie katalogowej. `POSTY_NA_DOBE × 30 / postów_na_konto` daje liczbę kont —
i to jest miejsce, w którym rozstrzyga się „trzydzieści czy dziewięćset".

---

## Zanim odpalisz

1. **Grupa testowa musi być PUBLICZNA i sprawdzona ręcznie.** Apify czyta tylko
   grupy publiczne. Na prywatnej albo martwej zmierzysz błąd, nie zachowanie
   actora — i zapłacisz za to tyle samo. Sprawdza to człowiek, zalogowany na FB;
   z zewnątrz się nie da (patrz `laweta_radar/config/groups.py`).
2. **Grupa musi być RUCHLIWA.** Przy kilku postach na tydzień filtr działający
   i filtr ignorowany dają identyczny wynik — skrypt wypisze wtedy
   „NIEROZSTRZYGNIĘTE" zamiast udawać, że coś zmierzył. Celuj w grupę, w której
   przez 7 dni przybywa wyraźnie więcej niż 30 postów.
3. **Klucze Apify muszą być widoczne:**
   `python -m laweta_radar.workers.apify_keys` ma pokazać niezerową liczbę.
4. **Sprawdź plan bez wydawania:** `--sucho` pokazuje przewidywany koszt i kończy.

Pytanie 2 wymaga trzech grup (`--grupa` trzy razy). Samo pytanie 1 wystarczy
jedna grupa: `--tylko 1`.

## Powtórka pomiaru

Powtórz przy **zmianie wersji actora** (numer builda jest w wygenerowanym raporcie)
oraz gdy rachunek za Apify przestanie się zgadzać z przewidywaniem. Zachowanie
actorów ze Store zmienia się między wersjami bez ostrzeżenia i bez zmiany nazw pól.
