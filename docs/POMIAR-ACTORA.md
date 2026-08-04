# Pomiar actora `apify/facebook-groups-scraper`

> ## ⛔ POMIAR NIE ZOSTAŁ JESZCZE WYKONANY
>
> Ten plik jest **miejscem na wynik**, a nie wynikiem. Narzędzie
> (`laweta_radar/scripts/pomiar_actora.py`) jest gotowe i przetestowane, ale samego
> pomiaru nie dało się przeprowadzić tam, gdzie powstawał kod: nie ma stamtąd dostępu
> do `api.apify.com` ani do puli kluczy. **Pomiar musi odpalić człowiek na VPS-ie**,
> a skrypt nadpisze wtedy ten plik prawdziwymi liczbami.
>
> **Do promptu 2 (`_build_actor_input`):** dopóki widzisz ten blok, **nie zakładaj
> ścieżki A**. Nie ma zmierzonej wartości `onlyPostsNewerThan`, nie wiadomo, czy
> `resultsLimit` jest per grupa, i nie ma kosztu posta do `POSTY_NA_DOBE`.
> Bezpieczne założenia do czasu pomiaru są w sekcji
> [Zanim będzie pomiar](#zanim-będzie-pomiar) na końcu.

---

## Po co ten pomiar

Trzy liczby, na których stoi cała architektura fetchera, a których dokumentacja
actora nie podaje:

1. **Jaką najmniejszą jednostkę czasu przyjmuje `onlyPostsNewerThan`?**
   Bez działającego okna czasowego każdy przebieg pobiera — i płaci za — te same
   posty co poprzedni. Przy cronie co 5 minut to różnica między systemem opłacalnym
   a nieopłacalnym.
   - **ŚCIEŻKA A** — okno działa: liczba itemów maleje wraz ze zwężaniem okna, a wiek
     najstarszego posta mieści się w oknie.
   - **ŚCIEŻKA B** — jednostka jest ignorowana: wynik dla wąskiego okna jest taki sam
     jak dla szerokiego, albo actor odrzuca pole.
2. **Czy `resultsLimit` przy wielu grupach w `startUrls` działa per grupa, czy
   globalnie?** Przy limicie globalnym batch po dziesięć grup zgubiłby posty
   z ośmiu z nich — batchowanie byłoby wprost szkodliwe, nie tylko nieoptymalne.
3. **Ile realnie kosztuje jeden pobrany post?** Liczba wchodzi wprost do
   `POSTY_NA_DOBE` i do decyzji „ile kont / czy płatny plan”.

## Jak odpalić pomiar

```bash
cd /home/ubuntu/laweta-radar
export PYTHONPATH=$PWD

# 1. Sprawdź, że widać klucze i proxy (pomiar ma iść TĄ SAMĄ ścieżką co produkcja)
python -m laweta_radar.workers.apify_keys
python -m laweta_radar.workers.apify_proxy
python -m laweta_radar.workers.apify_credits --limit 3     # saldo kilku kont

# 2. Plan i prognoza kosztu — NIE dotyka sieci, nic nie kosztuje
python laweta_radar/scripts/pomiar_actora.py --sucho

# 3. Realny pomiar (zapyta o potwierdzenie i nadpisze ten plik)
python laweta_radar/scripts/pomiar_actora.py
```

**Grupa testowa musi być publiczna i potwierdzona ręcznie.** Domyślnie skrypt bierze
grupy ze statusem `"ok"` z `laweta_radar/config/groups.py` — a ten status oznacza
dokładnie to, że człowiek wszedł w grupę zalogowany i sprawdził, że jest publiczna
i żywa. Grupę spoza tej listy podajesz jawnie:

```bash
python laweta_radar/scripts/pomiar_actora.py \
  --grupa https://www.facebook.com/groups/... \
  --potwierdzam-publiczne
```

Na grupie prywatnej zmierzyłbyś komunikat błędu zamiast zachowania actora — a run
i tak zostałby policzony.

### Ile to kosztuje

Plan domyślny to **8 wywołań i najwyżej ~240 pobranych postów**
(6 × `resultsLimit` 20 na pytanie 1 + 4 × 30 w najgorszym wypadku na pytanie 2).
Skrypt liczy tę prognozę **przed** odpaleniem, pokazuje ją razem z ceną katalogową
actora i pyta o potwierdzenie. Twardy sufit to `--budzet-postow` (domyślnie **500**)
i działa dwustronnie: blokuje plan, który go przekracza, i przerywa serię w trakcie,
gdyby actor oddał więcej, niż zapowiadał.

Kredyt jest **wspólny z sales-core-engine** (patrz README) — te posty odejmują się
z tej samej puli, z której korzysta drugi system.

## Wynik

_Wypełni skrypt. Poniżej struktura, którą zapisze._

| Pytanie | Odpowiedź |
|---|---|
| 1. Najmniejsza jednostka `onlyPostsNewerThan` | — NIE ZMIERZONO — |
| 2. `resultsLimit` przy wielu grupach | — NIE ZMIERZONO — |
| 3. Koszt jednego pobranego posta | — NIE ZMIERZONO — |

Do tego, per pytanie:

- **Pytanie 1** — tabela: okno, liczba itemów, wiek najstarszego i najnowszego posta,
  czy mieści się w oknie, czas runu, zaczęte minuty, koszt, błąd. Plus wywołanie
  **kontrolne bez pola okna**: porównanie *zestawów* postów (nie tylko ich liczby)
  odróżnia „okno zwróciło wszystko, bo grupa jest mała” od „pole jest ignorowane”.
- **Pytanie 2** — tabela: to samo `resultsLimit` dla jednej i dla trzech grup, wraz
  z rozkładem itemów **na grupę** (limit globalny bywa zjadany w całości przez
  pierwszą grupę i to wygląda inaczej niż limit dzielony po równo).
- **Pytanie 3** — koszt z **salda konta** (różnica odczytów przed i po każdym runie)
  z kontrolą przez `usageTotalUsd` runu, rozbity na **składnik stały wywołania**
  i **koszt krańcowy posta**, plus sprawdzenie hipotezy rozliczania **za zaczętą
  minutę** — ta ostatnia decyduje, czy fetcherowi opłaca się wołać częściej i płycej,
  czy rzadziej i grubiej.
- **Metryka poboczna, a ważna:** nazwa pola z czasem publikacji w itemie. Fetcher
  potrzebuje jej, żeby odsiewać stare posty po swojej stronie — i jest to jedyna
  rzecz, bez której ścieżka B w ogóle nie ma planu awaryjnego. Skrypt ustala ją
  z danych, nie zgaduje.

## Zanim będzie pomiar

Założenia, przy których kod napisany **przed** pomiarem będzie poprawny niezależnie
od tego, co pomiar pokaże — najwyżej droższy, nigdy błędny:

1. **Nie polegaj na `onlyPostsNewerThan`.** Ustawiaj je (nic nie kosztuje, a przy
   ścieżce A od razu działa), ale odsiew wieku rób **także** po swojej stronie, po
   polu z czasem posta. Kod napisany odwrotnie — ufający oknu — przy ścieżce B
   przepuszcza stare posty do klasyfikatora i płaci Claude'owi za każdy z nich.
2. **Jedna grupa na wywołanie.** To wariant poprawny przy obu odpowiedziach na
   pytanie 2; przy limicie per grupa jest tylko droższy, przy globalnym jest
   jedynym, który nie gubi danych. Batchowanie włączysz, gdy pomiar pokaże
   `PER GRUPA`.
3. **`POSTY_NA_DOBE` zostaw jako pojedynczą stałą w konfiguracji**, wyliczaną
   z kosztu posta — nie rozsiewaj limitów po kodzie. Po pomiarze zmieni się jedna
   liczba w jednym miejscu.

---

Ostatnia aktualizacja tego pliku: **2026-08-04** (szkielet, bez pomiaru).
Po odpaleniu skryptu ta linia i całość powyżej zostaną nadpisane wynikiem.
