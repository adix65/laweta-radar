-- =============================================================================
-- PANEL — to, co przy zleceniu dopisuje CZŁOWIEK.
--
--   Jedyne kolumny w całej bazie, których źródłem nie jest ani Facebook, ani
--   model. Dlatego są osobno od `0004_klasyfikacja.sql`: tamte przy ponownej
--   klasyfikacji wolno nadpisać, te NIGDY. Notatka „dzwoniłem 14:20, oddzwoni
--   po 16" skasowana przez ponowny przebieg klasyfikatora to jedyna informacja
--   w systemie, której nie da się odtworzyć z niczego.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0005_panel.sql
--   albo: bash laweta_radar/scripts/migrate.sh
--
--   `status` istnieje już od 0003_fetcher.sql — tu dochodzi tylko znacznik
--   KIEDY się zmienił. Bez niego nie da się odpowiedzieć na pytanie „ile
--   wygranych w tym tygodniu": kurs wygrany dziś mógł przyjść przed tygodniem,
--   a `opublikowany_at` mówi o poście, nie o robocie.
-- =============================================================================

ALTER TABLE posty
    ADD COLUMN IF NOT EXISTS notatka       TEXT,

    -- Cena, którą operator realnie dostał. To JEDYNA liczba w tym systemie
    -- mówiąca, ile on zarobił — `cena_sugerowana` z klasyfikatora i szacunek
    -- z `geo.kalkulacja` są zgadywaniem i nie mają prawa jej zastąpić.
    ADD COLUMN IF NOT EXISTS cena_koncowa  NUMERIC(10, 2),

    ADD COLUMN IF NOT EXISTS status_at     TIMESTAMPTZ;

-- Kolejka panelu: zlecenia po świeżości, bez śmieci. `opublikowany_at DESC
-- NULLS LAST`, bo Apify nie zawsze oddaje datę publikacji, a post o nieznanym
-- wieku ma wylądować NA KOŃCU listy — góra ekranu należy do najświeższych,
-- nie do tych, o których nic nie wiadomo.
CREATE INDEX IF NOT EXISTS idx_posty_kolejka
    ON posty (opublikowany_at DESC NULLS LAST)
    WHERE czy_zlecenie AND status <> 'smiec';

-- Dedup treściowy powiadomień pyta „czy w ostatnich 6 h szło coś na ten numer".
-- Numer siedzi w `kontakt_wartosc` (0004_klasyfikacja.sql), znormalizowany do
-- samych cyfr. Indeks częściowy — ma go mniejszość wierszy, a pytanie zawsze
-- go wymaga.
CREATE INDEX IF NOT EXISTS idx_posty_kontakt
    ON posty (kontakt_wartosc)
    WHERE kontakt_wartosc IS NOT NULL;

-- Statystyki („ile wygranych, ile przychodu") filtrują po dacie ZMIANY STATUSU.
CREATE INDEX IF NOT EXISTS idx_posty_status_at
    ON posty (status_at DESC)
    WHERE status_at IS NOT NULL;

-- Prawa dla roli workerów: `posty` ma już SELECT/INSERT/UPDATE z 0001, więc ta
-- migracja nie potrzebuje nowego GRANT-a. Kolumny są dokładane, nie tabela.
