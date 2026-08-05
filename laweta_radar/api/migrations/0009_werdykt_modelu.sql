-- =============================================================================
-- WERDYKT MODELU — jedno źródło prawdy zamiast dwóch kolumn o tym samym.
--
--   0004 dołożyło `ai_zlecenie` z uzasadnieniem, że `czy_zlecenie` (NOT NULL
--   DEFAULT false) nie odróżnia „model powiedział: nie" od „modelu nie pytano".
--   To prawda o SAMEJ kolumnie `czy_zlecenie` — i nieprawda o tabeli, bo
--   `zrodlo_decyzji` z 0003 niesie dokładnie to rozróżnienie:
--
--       zrodlo_decyzji = 'ai'    -> model orzekł, `czy_zlecenie` to JEGO werdykt
--       zrodlo_decyzji = 'gate'  -> post nie doszedł do modelu
--       zrodlo_decyzji IS NULL   -> nikt nie orzekł (awaria, do ponowienia)
--
--   Czyli `ai_zlecenie` niosło informację, którą para (`zrodlo_decyzji`,
--   `czy_zlecenie`) już niosła — i skończyło się tak, jak kończą się wszystkie
--   drugie kopie: ŻADNA ścieżka zapisu jej nie wypełniała. Fetcher wstawiał
--   wiersz bez niej, `classifier.SQL_ZAPIS` (jedyne miejsce, które ją znało)
--   nie było wołane przez nikogo, a `scripts/raport_gate.py` czytał ją jako
--   źródło macierzy pomyłek i w każdym przebiegu wypisywał „BRAK DANYCH".
--   Przez cały ten czas nie dało się stwierdzić, czy bramka gubi zlecenia —
--   czyli jedynej rzeczy, dla której tryb cienia w ogóle istnieje.
--
--   PO TEJ MIGRACJI kolumna zostaje w tabeli, ale nic jej nie czyta i nic nie
--   pisze. NIE kasujemy jej: jest pusta (same NULL-e, bo nigdy nie było czym
--   jej wypełnić), więc DROP niczego by nie uratował, a migracja kasująca
--   kolumnę na cudzej produkcji to ryzyko bez nagrody. Zostaje `COMMENT`,
--   który mówi to wprost każdemu, kto zajrzy w `\d+ posty`.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0009_werdykt_modelu.sql
--   albo: bash laweta_radar/scripts/migrate.sh
-- =============================================================================

COMMENT ON COLUMN posty.ai_zlecenie IS
    'MARTWA, nie używać. Werdykt modelu to czy_zlecenie przy zrodlo_decyzji=''ai'' '
    '(0009_werdykt_modelu.sql). Kolumna nigdy nie była wypełniana i zostaje '
    'wyłącznie po to, żeby nie kasować kolumny na działającej bazie.';

-- --- Indeksy, które stały na martwej kolumnie -------------------------------
--
-- Oba były zbudowane na `ai_zlecenie`, więc oba opisywały stan, który w bazie
-- nie istniał: „kolejka do klasyfikacji" obejmowała KAŻDY post ze statusem
-- `nowe` (także dawno sklasyfikowany), a „werdykty modelu" — żaden.

DROP INDEX IF EXISTS idx_posty_do_klasyfikacji;
DROP INDEX IF EXISTS idx_posty_ai_zlecenia;

-- KOLEJKA DO PONOWIENIA — „przeszło przez bramkę, a nie ma werdyktu modelu".
-- Trafiają tu dwa przypadki, dla ponowienia nieodróżnialne: post pobrany, zanim
-- klasyfikator istniał, oraz post, przy którym API padło w połowie przebiegu.
-- W obu fetcher zapisuje `zrodlo_decyzji='gate'` albo NULL — nigdy 'ai'.
-- IS DISTINCT FROM łapie oba naraz (zwykłe <> przepuściłoby NULL-e).
CREATE INDEX IF NOT EXISTS idx_posty_do_klasyfikacji
    ON posty (pobrany_at DESC)
    WHERE zrodlo_decyzji IS DISTINCT FROM 'ai' AND status = 'nowe';

-- Werdykty modelu od najświeższych — do raportu jakości i przeglądu „co model
-- realnie uznał za zlecenie". Nazwa NIE może być `idx_posty_zlecenia`: tamten
-- należy do 0003_fetcher.sql i stoi na samym `czy_zlecenie`, bez źródła.
CREATE INDEX IF NOT EXISTS idx_posty_ai_zlecenia
    ON posty (ai_at DESC)
    WHERE czy_zlecenie AND zrodlo_decyzji = 'ai';

-- Prawa dla roli workerów: ta migracja nie dokłada kolumn, więc GRANT-y z 0001
-- wystarczają.
