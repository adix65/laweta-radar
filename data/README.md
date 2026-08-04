# data/ — baza kodów pocztowych dla `services/geo.py`

`kody_eu.csv` zamienia kod pocztowy albo nazwę miasta na współrzędne. Format
jest jeden dla wszystkich krajów:

```
kraj,kod,miejscowosc,wojewodztwo,lat,lng
PL,38-400,Krosno,podkarpackie,49.6886,21.7706
```

Pusty `kod` jest dozwolony — taki wiersz obsługuje wyszukiwanie po nazwie,
a nie po kodzie.

## Ten plik w repo jest ZALĄŻKIEM, nie pełną bazą

Ma około siedemdziesięciu wierszy: miasta Podkarpacia (obszar operacyjny),
wojewódzkie w Polsce i kierunki zagraniczne, które realnie pojawiają się
w postach o transporcie aut. Wystarcza, żeby geokoder, testy i CLI działały
od pierwszego `git clone` — i **nie wystarcza do produkcji**: post o aucie
w Cisnej albo w Gelsenkirchen nie znajdzie się w tej próbce i dostanie `null`.

Współrzędne to centra miast z dokładnością do mniej więcej kilometra, a `kod`
w wierszach polskich to **początek zakresu miasta**, wpisany ręcznie. Do
przesiewu („czy to 60 km, czy 600") to w zupełności wystarcza; do niczego
innego ta baza i tak nie służy.

## Pełną bazę pobiera się jedną komendą

```bash
python laweta_radar/scripts/pobierz_geo.py --sucho   # plan, bez sieci
python laweta_radar/scripts/pobierz_geo.py           # PL DE CZ SK NL BE AT FR IT
python -m laweta_radar.services.geo Krosno Rzeszow   # sprawdzenie
```

Skrypt **nadpisuje** ten plik kompletem z GeoNames (kilkanaście MB, kilkaset
tysięcy wierszy). Wynik **commitujemy do repo**: alternatywą jest zależność od
zewnętrznego hosta przy każdym deployu, a niedostępny host oznacza świeży
deploy bez geokodera, który po cichu przestaje pokazywać trasy. Rozmiar repo
to koszt jednorazowy, niedostępny host — powtarzalny.

## Źródło i licencja

Docelowa baza pochodzi z [GeoNames](https://download.geonames.org/export/zip/),
na licencji [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) —
wymagane jest podanie autorstwa i ta wzmianka je realizuje. Zalążek w repo
został złożony ręcznie i nie pochodzi z GeoNames.

## Czego tu nie ma i nie będzie

Płatnego geokodera. 90% przypadków to kod pocztowy albo nazwa miasta, a to
załatwia ten plik: za darmo, offline, w mikrosekundy, bez limitu zapytań i bez
klucza, który może wygasnąć w środku nocy. Google Maps używamy wyłącznie jako
deep link do trasy — darmowy i bez klucza API.
