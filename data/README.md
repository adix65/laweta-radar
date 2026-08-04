# `data/` — pliki robocze, które WYPEŁNIA CZŁOWIEK

Jeden plik i jeden powód, dla którego leży w repo, a nie w `/tmp`.

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
