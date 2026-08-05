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
biblioteki, żadnej sieci.

**Post jest liczony WSZYSTKIMI słownikami**, nie tylko tym z detekcji — detekcja
służy do wyboru znacznika i do rozstrzygania remisów. Powód jest ten sam co
w całym module: pomyłka detekcji byłaby cichym fałszywym odrzuceniem. Kosztował
to konkretny błąd: polski post „kupiłem auto w Niemczech, **kto** przywiezie",
napisany bez ogonków, dostawał znacznik `sk` (bo „kto" jest też słowackie), szedł
wyłącznie przez słownik czesko-słowacki i wylatywał.

Dwie zasady rozstrzygania między słownikami:

- **jedno przepuszczenie wystarczy** — jeśli choć jeden słownik widzi zlecenie,
  post idzie do modelu;
- **wygaszenie widziane przez którykolwiek słownik wygasza post**, bo „już
  załatwione" przestaje być zleceniem niezależnie od tego, w jakim języku to
  napisano. Samo „weź najlepszy wynik" tu nie wystarcza: słownik, który nie zna
  zwrotu „hat sich erledigt", po prostu milczy — a milczenie wygląda lepiej niż
  odrzucenie;
- **wśród przepuszczeń wygrywa NAZWANA REGUŁA, dopiero potem wyższa punktacja.**
  Część wzorców jest niezależna od języka (marki aut, kody pocztowe, nazwy domów
  aukcyjnych) i punktuje w KAŻDYM słowniku, a twarde przepuszczenie ma z definicji
  zero punktów. Bez tej kolejności polski post „Do zabrania iveco … do 08-110
  siedlce" wygrywał słownikiem **niemieckim** i dostawał znacznik `de` — czyli
  podpowiedź, żeby oddzwonić po niemiecku do kogoś, kto pisał po polsku.

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

Prompt klasyfikatora dokłada do tego jedną zasadę, która NIE JEST tłumaczeniem:
nazwę zapisuje się w **mianowniku** („z Dębicy" → „Dębica", „pod Krosnem" →
„Krosno"). Odmiana to końcówka przypadka, a nie inny język — i to na niej wykłada
się dopasowanie do bazy kodów, bo `geo.normalizuj_nazwe` zdejmuje ogonki
i wielkość liter, ale nie odmienia. Nazwa zagraniczna zwykle w polskim zdaniu
i tak stoi nieodmieniona („z Belgii Zulte"), więc obie zasady się nie gryzą.

Kontrakt wywołania (szew w fetcherze: `_klasyfikuj`; wypełniony przez
`workers/classifier.py`):

```python
klasyfikuj(tresc: str, grupa: str, jezyk: str) -> dict | None
# dict zawiera co najmniej {"czy_zlecenie": bool}
```

`jezyk` przychodzi z bramki i jest tam po to, żeby klasyfikator nie musiał wykrywać
języka drugi raz.

Klasyfikator dokleja wtedy do promptu systemowego `INSTRUKCJA_JEZYKOWA_DLA_KLASYFIKATORA`
— stałą z `workers/gate.py`, żeby ten tekst nie istniał w repo w dwóch wersjach.
Dokleja ją **zawsze poza `jezyk="pl"`**, także gdy bramka nie rozstrzygnęła i pole
jest puste: instrukcja kosztuje ułamek grosza na wywołanie, a jej brak przy poście
niemieckim daje operatorowi pola po niemiecku dokładnie wtedy, gdy ma zdecydować
w kilkanaście sekund.

Sprawdzenie z ręki, bez sieci i bez kosztu:

```bash
python -m laweta_radar.workers.classifier --prompt --jezyk de
```

## Powiadomienie

Alert niesie **dwuliterowy znacznik** (kolumna `posty.gate_jezyk`). Bez niego operator
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
