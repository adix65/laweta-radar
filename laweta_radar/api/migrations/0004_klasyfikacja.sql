-- =============================================================================
-- KLASYFIKACJA — wynik workers/classifier.py zapisany przy poście.
--
--   Kolumny odpowiadają POLE W POLE kształtowi JSON-a z kontraktu
--   klasyfikatora. Rozbicie zagnieżdżonych obiektów na płaskie kolumny
--   (odbior_raw, odbior_kod, ...) zamiast jednego JSONB jest świadome: po tych
--   polach się FILTRUJE i GRUPUJE ("pokaż zlecenia z pilnością teraz", "ile
--   kursów z Niemiec w tym miesiącu"), a raport bramki porównuje `ai_zlecenie`
--   z `gate_werdykt`. JSONB wymagałby operatora w każdym takim zapytaniu
--   i indeksu na wyrażeniu przy każdym polu.
--
--   NUMER 0004, nie 002. Zadanie mówi "002_klasyfikacja.sql", ale 0002 zajmuje
--   bramka, a 0003 fetcher; scripts/migrate.sh odpala pliki POSORTOWANE PO
--   NAZWIE i kolejność jest merytoryczna (te kolumny dokładają się do tabeli
--   z 0001). Dwie migracje o tym samym numerze rozjechałyby tę kolejność.
--
--   CZEGO TU NIE MA, BO NALEŻY DO 0003_fetcher.sql: `zrodlo_decyzji`,
--   `czy_zlecenie`, `status`, `stale`, `gate_jezyk`. Fetcher wypełnia je przy
--   INSERCIE i to on jest ich właścicielem. Dopisanie ich tutaj po raz drugi
--   byłoby nieszkodliwe technicznie (ADD COLUMN IF NOT EXISTS), ale zostawiłoby
--   dwa pliki twierdzące, że odpowiadają za tę samą kolumnę — a przy następnej
--   zmianie ktoś poprawiłby jeden z nich.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0004_klasyfikacja.sql
--   albo: bash laweta_radar/scripts/migrate.sh
-- =============================================================================

ALTER TABLE posty
    -- Werdykt SAMEGO MODELU. Nazwa `ai_zlecenie` jest KONTRAKTEM
    -- z scripts/raport_gate.py (KOLUMNA_AI_DOMYSLNA) — to na niej stoi macierz
    -- pomyłek bramki, więc nie zmieniaj jej bez --kolumna-ai.
    --
    -- DLACZEGO OSOBNO OD `czy_zlecenie` z 0003: tamta jest NOT NULL DEFAULT
    -- false i niesie decyzję OPERACYJNĄ — bramka odrzuciła, model odrzucił,
    -- model nie był pytany, wszystko to jest tam `false`. Ta jest NULLOWALNA
    -- i mówi wyłącznie, co powiedział MODEL; NULL znaczy „nie pytaliśmy".
    -- Bez tego rozdziału raport z trybu cienia policzyłby posty odrzucone
    -- przez bramkę jako „model też by odrzucił" i pokazał zero fałszywych
    -- odrzuceń — czyli dokładnie to, przed czym ma chronić.
    ADD COLUMN IF NOT EXISTS ai_zlecenie      BOOLEAN,
    ADD COLUMN IF NOT EXISTS typ              TEXT,     -- holowanie|transport|odpalenie|wyciaganie|pomoc_drogowa|inne

    -- MIEJSCA. `raw` to cytat z posta ("spod Biedronki na Podkarpackiej"),
    -- `kod`/`miasto` są wypełniane TYLKO gdy jednoznaczne. NULL jest tu
    -- wartością poprawną i pożądaną: zgadnięte miasto wysyła człowieka 80 km
    -- w złą stronę, a puste pole każe mu przeczytać post.
    ADD COLUMN IF NOT EXISTS odbior_raw       TEXT,
    ADD COLUMN IF NOT EXISTS odbior_kod       TEXT,     -- wzorzec NN-NNN, walidowany w kodzie
    ADD COLUMN IF NOT EXISTS odbior_miasto    TEXT,
    ADD COLUMN IF NOT EXISTS dostawa_raw      TEXT,
    ADD COLUMN IF NOT EXISTS dostawa_kod      TEXT,
    ADD COLUMN IF NOT EXISTS dostawa_miasto   TEXT,

    ADD COLUMN IF NOT EXISTS pojazd_opis      TEXT,     -- "VW Golf IV"
    ADD COLUMN IF NOT EXISTS pojazd_kategoria TEXT,     -- osobowy|dostawczy|motocykl|ciezarowy|maszyna|inne

    -- STAN decyduje o sprzęcie i cenie. Trzy boole mają wartości DOMYŚLNE
    -- (true/true/false) wpisywane, gdy post milczy — dlatego `stan_uwagi`
    -- niesie cytat z treści: bez niego nie da się odróżnić "autor napisał, że
    -- się toczy" od "nikt nic nie napisał, więc założyliśmy".
    ADD COLUMN IF NOT EXISTS stan_toczy_sie   BOOLEAN,
    ADD COLUMN IF NOT EXISTS stan_ma_kola     BOOLEAN,
    ADD COLUMN IF NOT EXISTS stan_po_wypadku  BOOLEAN,
    ADD COLUMN IF NOT EXISTS stan_uwagi       TEXT,

    ADD COLUMN IF NOT EXISTS pilnosc          TEXT,     -- teraz|dzis|jutro|elastycznie
    ADD COLUMN IF NOT EXISTS kontakt_typ      TEXT,     -- telefon|pw|komentarz|brak
    ADD COLUMN IF NOT EXISTS kontakt_wartosc  TEXT,     -- numer znormalizowany do samych cyfr

    -- Kwota podana PRZEZ AUTORA posta, nie nasza wycena. NUMERIC, nie FLOAT:
    -- to pieniądze i pojawi się w podsumowaniach.
    ADD COLUMN IF NOT EXISTS cena_sugerowana  NUMERIC(10, 2),
    ADD COLUMN IF NOT EXISTS pewnosc          INTEGER,  -- 0-100
    ADD COLUMN IF NOT EXISTS powod            TEXT,     -- jedno zdanie uzasadnienia

    -- Który model orzekał. Bez tego po podmianie modelu w .env stare i nowe
    -- wiersze wyglądają identycznie i nie da się porównać jakości na produkcji
    -- — a to jedyny pomiar, który liczy się bardziej niż zbiór referencyjny.
    ADD COLUMN IF NOT EXISTS ai_model         TEXT,
    ADD COLUMN IF NOT EXISTS ai_at            TIMESTAMPTZ;

-- KOLEJKA DO PONOWIENIA — „co przeszło przez bramkę, a nie ma werdyktu modelu".
-- Trafiają tu dwa różne przypadki i oba wyglądają tak samo z punktu widzenia
-- ponowienia: post pobrany, zanim klasyfikator w ogóle istniał, oraz post,
-- przy którym API padło w połowie przebiegu. Warunek stoi na `ai_zlecenie`,
-- a nie na `zrodlo_decyzji`, bo fetcher wpisuje przy takim poście
-- `zrodlo_decyzji='gate'` (bramka orzekła, model nie zdążył) — NULL-a tam nie
-- będzie. Indeks częściowy, bo w normalnej sytuacji takich wierszy jest garść.
CREATE INDEX IF NOT EXISTS idx_posty_do_klasyfikacji
    ON posty (pobrany_at DESC)
    WHERE ai_zlecenie IS NULL AND status = 'nowe';

-- Werdykty modelu od najświeższych — do raportu jakości i przeglądu „co model
-- realnie uznał za zlecenie". Nazwa NIE może być `idx_posty_zlecenia`: ten
-- indeks należy do 0003_fetcher.sql i stoi na `czy_zlecenie`.
CREATE INDEX IF NOT EXISTS idx_posty_ai_zlecenia
    ON posty (ai_at DESC)
    WHERE ai_zlecenie IS TRUE;

-- Raport bramki (scripts/raport_gate.py) porównuje gate_werdykt z ai_zlecenie
-- w oknie ostatnich dni. Po tej migracji macierz pomyłek zaczyna działać bez
-- żadnego przełącznika — raport sam sprawdza obecność kolumny w katalogu
-- systemowym.

-- Prawa dla roli workerów: kolumny są AKTUALIZOWANE, nie wstawiane, a UPDATE
-- na `posty` jest już nadany w 0001 — ta migracja nie potrzebuje GRANT-a.
