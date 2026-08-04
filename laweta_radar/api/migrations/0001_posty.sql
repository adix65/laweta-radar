-- =============================================================================
-- POSTY — surowe posty pobrane z grup FB. Fundament pipeline'u.
--
--   Ta migracja zakłada WYŁĄCZNIE to, co widzi fetcher: treść posta, skąd
--   pochodzi i kiedy się pojawił. Kolumny bramki, klasyfikatora i geokodowania
--   dokładają OSOBNE migracje (0002_, 0003_, ...), bo każdy z tych kroków ma
--   własne pojęcie o tym, co warto zapisać, i własny moment wejścia do systemu.
--   Wrzucenie wszystkiego tutaj z góry oznaczałoby kolumny wymyślone, zanim
--   ktokolwiek napisał kod, który je wypełnia.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0001_posty.sql
--   albo: bash laweta_radar/scripts/migrate.sh
--
--   Workery łączą się rolą BEZ uprawnień DDL — dlatego na końcu pliku jest
--   GRANT. Rola tworząca tabele i rola ich używająca to celowo dwie różne role:
--   worker chodzi z crona na cudzych danych i nie ma powodu móc skasować tabeli.
--
--   DEDUP — fb_id jest kluczem głównym:
--     • gdy Apify odda identyfikator posta, bierzemy go wprost;
--     • gdy nie odda (zdarza się przy części layoutów), liczymy
--       sha256(grupa_url|tresc)[:32].
--   Dzięki temu ten sam post pobrany w kolejnym przebiegu wpada na
--   ON CONFLICT DO NOTHING i NIE jest klasyfikowany po raz drugi. To jest cała
--   ochrona budżetu: cron chodzi co kilka minut po tych samych grupach, więc bez
--   dedupu płacilibyśmy modelowi za ten sam post kilkanaście razy dziennie.
--   Grupa jest częścią hasha, bo ta sama prośba o lawetę bywa wklejana na kilka
--   grup naraz i każda kopia ma inny link — chcemy je widzieć osobno.
-- =============================================================================

CREATE TABLE IF NOT EXISTS posty (
    fb_id            TEXT PRIMARY KEY,                      -- id z Apify albo sha256(grupa_url|tresc)[:32]
    tresc            TEXT NOT NULL,                         -- pełna treść posta
    grupa_url        TEXT NOT NULL,                         -- z której grupy (config/groups.py)
    grupa_nazwa      TEXT,                                  -- etykieta grupy do alertu i promptu
    autor            TEXT,                                  -- nazwa autora, gdy Apify ją odda
    post_url         TEXT,                                  -- link do posta — operator odpisuje RĘCZNIE
    opublikowany_at  TIMESTAMPTZ,                           -- kiedy post powstał na FB (NULL gdy Apify nie poda)
    pobrany_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()     -- kiedy MY go zobaczyliśmy
);

-- Kolejka do obróbki i podgląd "co przyszło ostatnio" — oba pytania sortują po
-- świeżości, bo przy zleceniu na lawetę stary post jest bezwartościowy.
CREATE INDEX IF NOT EXISTS idx_posty_pobrany
    ON posty (pobrany_at DESC);

-- Statystyka per grupa: która grupa realnie dowozi, a która tylko pali budżet
-- Apify. Bez tego nie ma podstaw, żeby którąkolwiek wyłączyć.
CREATE INDEX IF NOT EXISTS idx_posty_grupa
    ON posty (grupa_url, pobrany_at DESC);

-- Prawa dla roli workerów. Podmień <rola_workerow> na rolę z DATABASE_URL
-- w .env. Świadomie BEZ DELETE i bez praw do DDL — czyszczeniem starych postów
-- zajmuje się osobne zadanie odpalane ręcznie, a nie worker w pętli.
-- GRANT SELECT, INSERT, UPDATE ON posty TO <rola_workerow>;
