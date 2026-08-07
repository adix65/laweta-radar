-- =============================================================================
-- KIERUNEK GEOGRAFICZNY — kraj obu końców trasy i kierunek względem Polski.
--
--   PROBLEM, KTÓRY TO ROZWIĄZUJE. System zna miasta i kody pocztowe, ale nie
--   kraje — nie da się odpytać "pokaż wyjazdy z Polski" ani "pokaż przywozy
--   do Polski", a to są DWA RÓŻNE PRODUKTY dla przewoźnika: wyjazd trzeba
--   połączyć z ładunkiem powrotnym (pusty powrót zjada marżę), przywóz zwykle
--   JEST już główną nogą kursu. Bramka wyjazdów nie wycina takich zleceń —
--   trafiają do bazy tak samo jak każde inne — brakowało tylko wymiaru,
--   po którym da się je znaleźć.
--
--   SKĄD SIĘ BIERZE KRAJ. `services/geo.py` już czyta kolumnę `kraj`
--   z `kody_eu.csv` przy KAŻDYM geokodowaniu (rozstrzyga nią kolizje kodów
--   i nazw między krajami) — ta migracja tylko wystawia to, co geokoder i tak
--   wie, jako kolumny do filtrowania. To jest odczyt istniejącej informacji,
--   nie nowa logika geokodowania.
--
--   DWIE KOLUMNY KRAJU + JEDNA POCHODNA, nie tylko `kierunek_geo`. Panel
--   i diagnostyka pytają czasem o KONKRETNY kraj drugiej strony ("ile
--   przywozów z Niemiec"), nie tylko o to, że to przywóz — a wyliczanie tego
--   z powrotem z `kierunek_geo` jest niemożliwe (kierunek nie pamięta,
--   z którego kraju).
--
--   `kierunek_geo` WARTOŚCI: 'przywoz' (X->PL) | 'wyjazd' (PL->X) |
--   'krajowy' (PL->PL) | 'tranzyt' (X->Y, oba poza PL) | 'nieznany'.
--   NULL = nikt jeszcze nie liczył (wiersze sprzed tej migracji, zanim
--   przejdzie `scripts/uzupelnij_kierunek_geo.py`) — panel i alerty mają go
--   traktować tak samo jak 'nieznany': widoczny wszędzie, tylko poza
--   pigułkami filtra kierunku.
--
--   TO JEST WYMIAR DO FILTROWANIA, NIGDY POWÓD ODRZUCENIA. Zasada naczelna
--   repo: system pokazuje zlecenia, decyduje kierowca. Domyślny widok, alerty
--   i `/ostatnie` obejmują WSZYSTKIE kierunki bez zmian — kolumny tej migracji
--   dokładają tylko możliwość zawężenia, nigdy automatycznego wycięcia.
--
--   KTO WYPEŁNIA. `workers/classifier.wiersz_do_zapisu` — tym samym
--   geokodowaniem co `odbior_kod`/`odbior_miasto` już tam policzone, w tym
--   samym momencie zapisu. Dla wierszy sprzed tej zmiany `workers/fb_fetcher.run()`
--   doprzelicza wstecz do KIERUNEK_GEO_BACKFILL_LIMIT wierszy w KAŻDYM przebiegu
--   (`_doganiaj_kierunek_geo`, ta sama pętla sprzątająca co naprawa ekstrakcji) —
--   BEZ jednego wywołania modelu ani Apify, bo geokodowanie jest czystą funkcją
--   tego, co już stoi w wierszu. `scripts/uzupelnij_kierunek_geo.py` robi to samo
--   ręcznie i bez limitu, do natychmiastowego przeliczenia całej zaległości naraz.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0013_kierunek_geo.sql
--   albo: bash laweta_radar/scripts/migrate.sh
-- =============================================================================

ALTER TABLE posty
    ADD COLUMN IF NOT EXISTS odbior_kraj  TEXT,   -- dwuliterowy kod kraju odbioru, np. 'PL', 'DE'
    ADD COLUMN IF NOT EXISTS dostawa_kraj TEXT,   -- dwuliterowy kod kraju dostawy
    ADD COLUMN IF NOT EXISTS kierunek_geo TEXT;   -- przywoz|wyjazd|krajowy|tranzyt|nieznany|NULL

COMMENT ON COLUMN posty.odbior_kraj IS
    'Dwuliterowy kod kraju punktu odbioru, z tej samej kolumny `kraj` bazy '
    'kody_eu.csv, którą geo.geokoduj() już czyta przy rozstrzyganiu kolizji. '
    'NULL = punkt nierozpoznany przez geokoder (nie: kraj nieznany dla '
    'rozpoznanego punktu — to się nie zdarza, bo kraj wchodzi do dopasowania).';

COMMENT ON COLUMN posty.dostawa_kraj IS
    'To samo co odbior_kraj, dla drugiego końca trasy.';

COMMENT ON COLUMN posty.kierunek_geo IS
    'Kierunek trasy względem Polski: przywoz (X->PL) | wyjazd (PL->X) | '
    'krajowy (PL->PL) | tranzyt (X->Y, oba poza PL) | nieznany (którykolwiek '
    'koniec nierozpoznany). Liczy geo.kierunek_geo() z odbior_kraj/dostawa_kraj. '
    'NULL = nikt jeszcze nie liczył (sprzed tej migracji / przed backfillem), '
    'traktowane jak "nieznany" — WYMIAR DO FILTROWANIA, nigdy powód, żeby '
    'zlecenie zniknęło z domyślnego widoku czy alertu.';

-- Komendy bota (/wyjazdy, /przywozy, /krajowe, /tranzyt) i pigułka filtra
-- w panelu pytają o JEDNĄ wartość kierunku wśród zleceń, od najświeższych —
-- dokładnie ten sam kształt zapytania co `idx_posty_zlecenia`, tylko zawężony
-- też po kierunku. Bez tego każde odpytanie kierunku byłoby skanem całej
-- tabeli zleceń.
CREATE INDEX IF NOT EXISTS idx_posty_kierunek_geo
    ON posty (kierunek_geo, opublikowany_at DESC)
    WHERE czy_zlecenie;

-- Prawa dla roli workerów: kolumny są aktualizowane, nie wstawiane, a UPDATE
-- na `posty` jest już nadany w 0001 — ta migracja nie potrzebuje GRANT-a.
