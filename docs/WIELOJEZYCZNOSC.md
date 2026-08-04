# Wielojęzyczność: kto co robi z językiem

Grupy niemieckie, czeskie i słowackie idą przez **ten sam pipeline** co polskie.
Ten dokument jest kontraktem między jego krokami — spisany osobno, bo każdy z nich
robi z językiem coś innego i najłatwiej to zepsuć, dokładając „na wszelki wypadek"
tłumaczenie tam, gdzie go nie powinno być.

## Podział ról — w jednym zdaniu na krok

| krok | co robi z językiem | czego NIE robi |
|---|---|---|
| **fetcher** (`workers/fb_fetcher.py`) | nic — przenosi treść i znacznik z bramki do bazy | nie zgaduje języka |
| **bramka** (`workers/gate.py`) | wykrywa język, filtruje słownikiem tego języka, oddaje dwuliterowy znacznik | **nie tłumaczy** |
| **klasyfikator** (`workers/classifier.py`) | rozumie post w oryginale, **wypełnia wynik po polsku** | nie zwraca tekstu w języku posta |
| **powiadomienie** | pokazuje znacznik języka operatorowi | nie tłumaczy niczego drugi raz |

Zasada, z której to wynika: **tłumaczy się raz, w miejscu, w którym i tak stoi
model.** Tłumaczenie w bramce znaczyłoby wywołanie sieciowe na ścieżce każdego
pobranego posta — czyli zamianę filtru, który kosztuje mikrosekundy, w filtr, który
kosztuje pieniądze i potrafi paść.

## Bramka

Cztery znaczniki (`pl`, `de`, `cs`, `sk`), **trzy słowniki** — czeski i słowacki
dzielą jeden, z wariantami obu języków w środku. Detekcja mimo to **rozróżnia** te
dwa języki, bo znacznik nie służy filtrowaniu, tylko człowiekowi.

Detekcja jest heurystyką na znakach diakrytycznych i słowach funkcyjnych: żadnej
biblioteki, żadnej sieci. Gdy nie rozstrzyga (krótki post bez znaków
charakterystycznych — sytuacja zupełnie normalna), post liczony jest **wszystkimi**
słownikami i wygrywa najwyższy wynik. Wyjątek: **wygaszenie widziane przez
którykolwiek słownik wygasza post**, bo „już załatwione" przestaje być zleceniem
niezależnie od tego, w jakim języku to napisano.

Szczegóły warstw, wag i doboru wzorców: docstring `workers/gate.py`.

```bash
python -m laweta_radar.workers.gate "Suche Abschleppdienst, Motor kaputt"
```

## Klasyfikator

Prompt systemowy **musi** zawierać instrukcję językową. Nie przepisuj jej — jest
wyeksportowana ze wspólnego miejsca i importuje się ją, żeby nie rozjechała się
przy pierwszej zmianie listy języków:

```python
from laweta_radar.workers.gate import INSTRUKCJA_JEZYKOWA_DLA_KLASYFIKATORA
```

Mówi ona trzy rzeczy:

1. post może być po polsku, niemiecku, czesku albo słowacku;
2. **wszystkie pola wyniku wypełniaj po polsku** — czyta je polskojęzyczny operator,
   który ma podjąć decyzję w kilkanaście sekund, a nie tłumaczyć niemiecki opis
   w najgorszym możliwym momencie;
3. **nazwy miejscowości zostają w formie oryginalnej** („München", nie „Monachium";
   „Praha", nie „Praga"). To jest wyjątek z powodu technicznego, nie stylistycznego:
   te pola idą wprost do geokodowania i do linku z mapą. Przetłumaczona nazwa albo
   nie znajdzie się w geokoderze, albo znajdzie się w złym miejscu — a zlecenie
   z błędną pozycją jest gorsze niż brak zlecenia, bo zjada uwagę operatora i wysyła
   go nie tam.

Kontrakt wywołania (szew jest już w fetcherze, `_klasyfikuj`):

```python
klasyfikuj(tresc: str, grupa: str, jezyk: str) -> dict | None
# dict zawiera co najmniej {"czy_zlecenie": bool}
```

`jezyk` przychodzi z bramki i jest tam po to, żeby klasyfikator nie musiał wykrywać
języka drugi raz.

## Powiadomienie

Alert niesie **dwuliterowy znacznik** (kolumna `zlecenia.jezyk`). Bez niego operator
nie wie, w jakim języku oddzwonić — a wszystkie pozostałe pola alertu są już po
polsku, więc sam post tego nie zdradzi. To jedna z niewielu informacji w alercie,
która zmienia to, co człowiek **zrobi**, a nie tylko to, co przeczyta.

`NULL` w tej kolumnie znaczy „bramka nie rozstrzygnęła" i jest wartością normalną,
nie błędem — wtedy operator patrzy na treść posta.

## Zanim włączysz obcojęzyczne grupy

1. Dopisz grupy do `config/groups.py` i **zweryfikuj każdą ręcznie** (publiczna?
   żywa? zgłoszeniowa czy sama reklama?) — tak samo jak polskie. Wyszukiwarka grup
   ma gotowe bloki fraz DE/CS/SK: `config/frazy_grup.py`.
2. Sprawdź kilka realnych postów z tych grup przez CLI bramki. Wzorce w słownikach
   to **dane, nie kod** — dopisanie zwrotu, który przeszedł koło nosa, jest jedną
   linijką w `workers/gate.py` i jednym przypadkiem w `tests/test_gate.py`.
3. Pamiętaj o dystansie. Grupa niemiecka dowozi głównie zlecenia **transportowe**
   (przewóz auta na trasie), a nie awarie w zasięgu lawety — filtr geograficzny
   odetnie ich większość, jeśli będzie ustawiony jak dla rynku lokalnego.
