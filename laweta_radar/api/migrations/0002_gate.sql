-- =============================================================================
-- BRAMKA — decyzja workers/gate.py zapisana przy poście.
--
--   Kolumny są tu z JEDNEGO powodu: bez nich nie da się stwierdzić, czy bramka
--   jest dobra. Bramka odrzuca posty ZANIM zobaczy je model, więc jej pomyłki
--   są z definicji niewidoczne — odrzucone zlecenie nie trafia nigdzie i nikt
--   się o nim nie dowie. Jedyny sposób, żeby je policzyć, to przepuścić przez
--   AI WSZYSTKO (tryb cienia, GATE_TRYB=cien), zapisywać decyzję bramki obok
--   werdyktu modelu i porównać. Raport: scripts/raport_gate.py.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0002_gate.sql
--   albo: bash laweta_radar/scripts/migrate.sh
--
--   ZAPISUJEMY WERDYKT, NIE DECYZJĘ OPERACYJNĄ. W trybie cienia bramka
--   przepuszcza wszystko, więc kolumna "czy poszło do AI" byłaby w całości
--   wypełniona jedynkami i nie dałoby się z niej policzyć niczego.
--   `gate_werdykt` mówi, co bramka SĄDZI — niezależnie od tego, czy w tym
--   trybie ma prawo cokolwiek zablokować.
-- =============================================================================

ALTER TABLE posty
    ADD COLUMN IF NOT EXISTS gate_werdykt   BOOLEAN,      -- czy bramka przepuściłaby post
    ADD COLUMN IF NOT EXISTS gate_punkty    INTEGER,      -- suma wag z warstwy 4 (0 gdy rozstrzygnęła wcześniejsza)
    ADD COLUMN IF NOT EXISTS gate_powod     TEXT,         -- "wygaszone" / "autopromocja" / "punktacja 7 >= prog 5"
    ADD COLUMN IF NOT EXISTS gate_trafienia TEXT[],       -- które wzorce zadziałały, z wagami
    ADD COLUMN IF NOT EXISTS gate_tryb      TEXT,         -- "cien" albo "aktywny" w momencie decyzji
    ADD COLUMN IF NOT EXISTS gate_at        TIMESTAMPTZ;  -- kiedy bramka orzekała

-- `gate_tryb` przy każdym wierszu, a nie w konfiguracji, bo próg i słownik będą
-- się zmieniać w trakcie kalibracji. Bez tego po przełączeniu na "aktywny" stare
-- wiersze wyglądałyby tak samo jak nowe i raport mieszałby dwa różne reżimy.

-- Raport bramki czyta okno ostatnich dni i grupuje po werdykcie — bez indeksu
-- byłby to pełny skan tabeli za każdym uruchomieniem.
CREATE INDEX IF NOT EXISTS idx_posty_gate
    ON posty (gate_at DESC)
    WHERE gate_at IS NOT NULL;

-- Najczęstsze pytanie w trybie cienia: "pokaż posty, które bramka by odrzuciła".
-- Indeks częściowy, bo interesuje nas tylko jedna wartość i jest ich mniejszość.
CREATE INDEX IF NOT EXISTS idx_posty_gate_odrzucone
    ON posty (pobrany_at DESC)
    WHERE gate_werdykt IS FALSE;

-- -----------------------------------------------------------------------------
-- KONTRAKT DLA KLASYFIKATORA (prompt 3) — kolumny werdyktu AI dokłada JEGO
-- migracja, nie ta. Zasada z 0001_posty.sql: każdy krok pipeline'u zakłada
-- własne kolumny, gdy powstaje kod, który je wypełnia; wymyślanie ich z góry
-- kończy się kolumnami, których nikt nie pisze.
--
-- Raport bramki (scripts/raport_gate.py) sprawdza obecność kolumny w katalogu
-- systemowym i bez niej pokazuje tylko rozkład punktów, mówiąc wprost, czego
-- brakuje. Żeby macierz pomyłek zaczęła działać, klasyfikator ma dołożyć:
--
--   ALTER TABLE posty
--       ADD COLUMN IF NOT EXISTS ai_zlecenie BOOLEAN,
--       ADD COLUMN IF NOT EXISTS ai_at       TIMESTAMPTZ;
--
-- Inna nazwa jest OK — raport przyjmuje ją przez --kolumna-ai.
-- -----------------------------------------------------------------------------

-- Prawa dla roli workerów (podmień <rola_workerow> na rolę z DATABASE_URL).
-- Kolumny bramki są aktualizowane, nie wstawiane — UPDATE na `posty` jest już
-- nadany w 0001, więc ta migracja nie potrzebuje nowego GRANT-a.
