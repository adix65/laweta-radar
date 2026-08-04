-- =============================================================================
-- ZLECENIE — werdykt klasyfikatora i stan obsługi po stronie człowieka.
--
--   TA MIGRACJA JEST WSPÓLNYM SZWEM trzech kroków, które powstały po niej:
--   klasyfikatora (pisze `ai_*`), powiadomień (czytają `ai_json`, żeby zbudować
--   treść alertu) i panelu (czyta wszystko, zapisuje `notatka`/`cena_koncowa`).
--   Stoi osobno, a nie w migracji powiadomień, bo należy do POSTA, nie do
--   alertu — post istnieje dalej, gdy alertu nigdy nie wysłano, i to jest stan
--   zupełnie normalny (patrz progi wysyłki w services/powiadomienia.py).
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0004_zlecenie.sql
--   albo: bash laweta_radar/scripts/migrate.sh
--
--   DLACZEGO JSONB, A NIE PIĘTNAŚCIE KOLUMN. Klasyfikator wyciąga z posta rzeczy,
--   których lista będzie się zmieniać przy każdej iteracji promptu: pojazd, stan,
--   telefon, miasta, kod pocztowy, pilność, termin, czasem masę. Kolumna na każde
--   pole znaczyłaby migrację przy każdej poprawce promptu, a w praktyce — prompt
--   poprawiany bez migracji i pola znikające po cichu. `ai_json` przyjmuje
--   cokolwiek model odda; to, co jest potrzebne do SORTOWANIA i FILTROWANIA,
--   wychodzi obok jako zwykłe kolumny, bo indeks na wyrażeniu z JSONB jest
--   trudniejszy do zauważenia niż kolumna.
-- =============================================================================

ALTER TABLE posty
    -- Werdykt modelu. Kolumna zapowiedziana w komentarzu 0002_gate.sql —
    -- to na nią patrzy scripts/raport_gate.py, licząc fałszywe odrzucenia bramki.
    ADD COLUMN IF NOT EXISTS ai_zlecenie   BOOLEAN,
    ADD COLUMN IF NOT EXISTS ai_at         TIMESTAMPTZ,

    -- Pełny wynik klasyfikatora, po polsku (patrz docs/WIELOJEZYCZNOSC.md).
    -- Trafia też do tabeli `feedback` jako materiał do poprawiania promptu:
    -- bez zapisanego werdyktu „oznaczone jako śmieć" nie mówi NIC o tym, co
    -- model źle zrozumiał.
    ADD COLUMN IF NOT EXISTS ai_json       JSONB,

    -- Pewność 0-100. Osobno od JSON-a, bo to po niej idzie próg powiadomienia
    -- (MIN_PEWNOSC) i sortowanie w panelu. UWAGA: próg dotyczy WYŁĄCZNIE tego,
    -- czy brzęczy telefon. Zlecenie z pewnością 12 jest w panelu tak samo jak
    -- zlecenie z pewnością 95 — system pokazuje, decyduje kierowca.
    ADD COLUMN IF NOT EXISTS ai_pewnosc    SMALLINT,

    -- 'pilne' | 'dzis' | 'planowane' — pierwsza rzecz na ekranie alertu.
    ADD COLUMN IF NOT EXISTS ai_pilnosc    TEXT,

    -- Numer telefonu w postaci ZNORMALIZOWANEJ (same cyfry, z kierunkowym).
    -- Wychodzi z JSON-a, bo jest kluczem dedupu treściowego: ten sam post
    -- wklejony na pięć grup ma pięć różnych fb_id, ale jeden numer.
    ADD COLUMN IF NOT EXISTS telefon       TEXT,

    -- --- stan obsługi po stronie CZŁOWIEKA ---------------------------------
    -- `status` istnieje już od 0003_fetcher.sql. Tu dochodzi to, co operator
    -- dopisuje SAM — i co jest jedynym miejscem w całej bazie, gdzie dane
    -- pochodzą od niego, a nie z Facebooka albo od modelu.
    ADD COLUMN IF NOT EXISTS notatka       TEXT,
    ADD COLUMN IF NOT EXISTS cena_koncowa  NUMERIC(10, 2),
    ADD COLUMN IF NOT EXISTS status_at     TIMESTAMPTZ;

-- Kolejka panelu: zlecenia po świeżości. `opublikowany_at DESC NULLS LAST`, bo
-- Apify nie zawsze oddaje datę publikacji, a post bez daty ma wylądować NA KOŃCU
-- listy, nie na jej początku — inaczej posty o nieznanym wieku zajęłyby górę
-- ekranu, czyli dokładnie to miejsce, które ma należeć do najświeższych.
CREATE INDEX IF NOT EXISTS idx_posty_kolejka
    ON posty (opublikowany_at DESC NULLS LAST)
    WHERE czy_zlecenie AND status <> 'smiec';

-- Dedup treściowy powiadomień pyta „czy w ostatnich 6 h szło coś na ten numer".
-- Indeks częściowy — telefon ma mniejszość wierszy, a pytanie zawsze go wymaga.
CREATE INDEX IF NOT EXISTS idx_posty_telefon
    ON posty (telefon)
    WHERE telefon IS NOT NULL;

-- Statystyki („ile wygranych, ile przychodu") filtrują po statusie i dacie
-- zmiany statusu, a nie publikacji — kurs wygrany dziś mógł przyjść wczoraj.
CREATE INDEX IF NOT EXISTS idx_posty_status_at
    ON posty (status_at DESC)
    WHERE status_at IS NOT NULL;

-- Prawa dla roli workerów: `posty` ma już SELECT/INSERT/UPDATE z 0001, więc
-- ta migracja nie potrzebuje nowego GRANT-a. Kolumny są dokładane, nie tabela.
