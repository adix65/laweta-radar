# `data/` — dane, które muszą przetrwać `git clone`

Dwa pliki i jeden wspólny powód, dla którego leżą w repo, a nie w `/tmp`:
**odtworzenie każdego z nich kosztuje albo godziny człowieka, albo dostępność
cudzego hosta w momencie deployu.**

| plik | co to jest | kto wypełnia |
|---|---|---|
| `kandydaci_grupy.csv` | lista kandydackich grup FB | człowiek (kolumna `publiczna`) |
| `kody_eu.csv` | kody pocztowe -> współrzędne | `scripts/pobierz_geo.py` |

---

## `kandydaci_grupy.csv`

Lista kandydackich grup FB z `laweta_radar/scripts/znajdz_grupy.py`. Kolumny:

| kolumna | skąd |
|---|---|
| `url`, `nazwa`, `czlonkowie` | z wyszukiwarki |
| `jezyk`, `fraza_zrodlowa` | z frazy, która grupę znalazła |
| `publiczna` | **TYLKO z ręki** — TAK / NIE |
| `status` | `kandydat` / `ok` / `odrzucona` |
| `notatka` | ostrzeżenia skryptu + własne uwagi |

**Ten plik jest wersjonowany świadomie.** Kolumna `publiczna` to godziny klikania
po Facebooku — jedyny krok, którego nie da się zautomatyzować, bo Apify czyta
wyłącznie grupy publiczne, a z zewnątrz tego nie widać. Gdyby plik był
w `.gitignore`, każdy nowy klon zaczynałby tę robotę od zera.

Z tego samego powodu comiesięczne odświeżenie listy **scala** wyniki z tym plikiem
zamiast go nadpisywać: `publiczna`, `status` i `notatka` zostają nietknięte, dochodzą
tylko nowe grupy.

Po uzupełnieniu kolumny `publiczna`:

```bash
python -m laweta_radar.scripts.znajdz_grupy --raport
```

wypisze gotowy blok do wklejenia w `laweta_radar/config/groups.py` — wyłącznie
z grup oznaczonych TAK.

---

## `kody_eu.csv`

Baza dla `services/geo.py`: zamienia kod pocztowy albo nazwę miasta na
współrzędne. Format jest jeden dla wszystkich krajów, ale wiersze są dwojakie:

```
kraj,kod,miejscowosc,wojewodztwo,lat,lng,populacja
PL,38-400,Krosno,podkarpackie,49.6886,21.7706,
PL,,Krosno,podkarpackie,49.6886,21.7706,46000
```

Wiersz **z kodem** obsługuje wyszukiwanie **po kodzie**. Wiersz **bez kodu,
z populacją** (z dumpu miejscowości GeoNames, `feature_class='P'`) obsługuje
wyszukiwanie **po nazwie** — bo w pliku kodów nazwa bywa nazwą instytucji,
nie miejscowości (niemieckie Grosskunden-PLZ w rodzaju „Agentur fuer Arbeit
Dortmund"; „Frankfurt" nie występował w bazie ani razu). Populacja rozstrzyga
wybór między miastami o tej samej nazwie (Frankfurt am Main kontra Frankfurt
nad Odrą) — szczegóły w `services/geo.py`.

Kolumna `populacja` jest opcjonalna: starsze bazy bez niej działają, tylko po
nazwie szukają wtedy po wierszach kodowych, jak dawniej.

### Plik w repo jest ZALĄŻKIEM, nie pełną bazą

Ma około siedemdziesięciu wierszy: miasta Podkarpacia (obszar operacyjny),
wojewódzkie w Polsce i kierunki zagraniczne, które realnie pojawiają się
w postach o transporcie aut — te same cztery obszary językowe, które obsługuje
bramka (PL/DE/CZ/SK), plus giełdy na zachodzie. Wystarcza, żeby geokoder, testy
i CLI działały od pierwszego `git clone` — i **nie wystarcza do produkcji**:
post o aucie w Cisnej albo w Gelsenkirchen nie znajdzie się w tej próbce
i dostanie `null`.

Współrzędne to centra miast z dokładnością do mniej więcej kilometra, a `kod`
w wierszach polskich to **początek zakresu miasta**, wpisany ręcznie. Do
przesiewu („czy to 60 km, czy 600") to w zupełności wystarcza; do niczego
innego ta baza i tak nie służy.

### Pełną bazę pobiera się jedną komendą

```bash
python laweta_radar/scripts/pobierz_geo.py --sucho   # plan, bez sieci
python laweta_radar/scripts/pobierz_geo.py           # PL DE CZ SK NL BE AT FR IT
python -m laweta_radar.services.geo Krosno Rzeszow   # sprawdzenie
```

Skrypt **nadpisuje** ten plik kompletem z GeoNames — kodami pocztowymi
i miejscowościami z populacją (kilkadziesiąt MB, kilkaset tysięcy wierszy). Wynik **commitujemy** — z tego samego powodu, dla którego
wersjonujemy `kandydaci_grupy.csv`, tylko że tu kosztem nie są godziny
człowieka, lecz dostępność cudzego hosta: niedostępne `download.geonames.org`
w momencie deployu oznacza świeży deploy bez geokodera, który po cichu
przestaje pokazywać trasy. Rozmiar repo to koszt jednorazowy, niedostępny
host — powtarzalny.

### Źródło i licencja

Docelowa baza pochodzi z [GeoNames](https://download.geonames.org/) — z dwóch
eksportów: [kodów pocztowych](https://download.geonames.org/export/zip/)
i [dumpu miejscowości](https://download.geonames.org/export/dump/) — na
licencji [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) —
wymagane jest podanie autorstwa i ta wzmianka je realizuje. Zalążek w repo
został złożony ręcznie i nie pochodzi z GeoNames.

### Czego tu nie ma i nie będzie

Płatnego geokodera. 90% przypadków to kod pocztowy albo nazwa miasta, a to
załatwia ten plik: za darmo, offline, w mikrosekundy, bez limitu zapytań i bez
klucza, który może wygasnąć w środku nocy. Google Maps używamy wyłącznie jako
deep link do trasy — darmowy i bez klucza API.
