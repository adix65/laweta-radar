-- =============================================================================
-- ZLECENIA — posty z grup FB razem z DECYZJĄ, czy to zlecenie dla lawety.
--
--   Tabela, do której pisze `workers/fb_fetcher.py`. Jeden wiersz = jeden post
--   pobrany z grupy, niezależnie od tego, czy okazał się zleceniem. Zapisujemy
--   TAKŻE odrzucone, i to jest decyzja, nie zaniedbanie: bez odrzuconych nie da
--   się policzyć wydajności grupy (zlecenia / pobrane posty), a bez wydajności
--   bandyta rozdzielający budżet nie ma czym mierzyć — patrz services/bandit.py.
--
--   STOSUNEK DO `posty` Z MIGRACJI 0001 — ta tabela ją ZASTĘPUJE. 0001 powstała
--   przed fetcherem i zakładała wyłącznie to, co widać w surowym poście; zapis
--   werdyktu miał dojść osobną migracją jako kolumny. Wyszło inaczej: werdykt
--   ma dwa różne źródła (bramka słowna i model), własny status obsługi i własne
--   indeksy, więc doklejanie go do `posty` dawało tabelę, w której połowa kolumn
--   znaczy co innego w zależności od `zrodlo_decyzji`. Żaden worker nigdy do
--   `posty` nie napisał — modułu, który miałby to robić, po prostu nie było.
--   Jeśli odpaliłeś 0001 na swojej bazie, możesz posprzątać RĘCZNIE:
--       DROP TABLE IF EXISTS posty;
--   Świadomie NIE robi tego ta migracja: kasowanie cudzych danych nie może być
--   efektem ubocznym `git pull` + `migrate.sh`.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0002_zlecenia.sql
--   albo: bash laweta_radar/scripts/migrate.sh
--
--   Workery łączą się rolą BEZ uprawnień DDL — GRANT jest na końcu pliku.
--
--   DEDUP — fb_id = sha256(tresc)[:16], ten sam wzór co w repo źródłowym
--   (workers/apify_fb_fetcher.py). Hash liczymy z SAMEJ TREŚCI, bez URL-a grupy:
--   ta sama prośba o lawetę wklejona na pięć grup ma być JEDNYM zleceniem, a nie
--   pięcioma alertami na telefon operatora o tej samej awarii. Kosztem jest to,
--   że przy duplikacie zostaje link do grupy, w której zobaczyliśmy post jako
--   pierwsi — i to jest właściwy wybór, bo pierwsi znaczy najświeższy.
--
--   KOLUMNA post_url JEST NAJWAŻNIEJSZA W CAŁYM SYSTEMIE. Bez niej operator nie
--   ma jak odpisać, więc alert bez linku jest wart tyle co żaden. Fetcher loguje
--   ostrzeżenie z nazwą grupy, gdy actor nie odda URL-a — to znaczy, że zmienił
--   kształt odpowiedzi i trzeba dopisać kolejny klucz do `_first_str`.
--
--   RESZTA KOLUMN (geo, pojazd, pilność) dochodzi osobnymi migracjami razem
--   z klasyfikatorem i geokodowaniem. Wymyślanie ich teraz oznaczałoby kolumny
--   zaprojektowane przed kodem, który je wypełnia.
-- =============================================================================

CREATE TABLE IF NOT EXISTS zlecenia (
    fb_id           TEXT PRIMARY KEY,                   -- sha256(tresc)[:16]
    tresc           TEXT NOT NULL,                      -- pełna treść posta, tak jak przyszła z Apify
    post_url        TEXT,                               -- KLIKALNY LINK DO POSTA — pole krytyczne, patrz nagłówek
    group_url       TEXT,                               -- z której grupy (config/groups.py)
    group_name      TEXT,                               -- etykieta grupy: do alertu i do promptu klasyfikatora
    author_name     TEXT,                               -- autor posta, gdy actor go odda
    post_date       TIMESTAMPTZ,                        -- kiedy post powstał na FB (NULL, gdy actor nie podał)
    pobrano_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(), -- kiedy MY go zobaczyliśmy

    -- Kto podjął decyzję. 'gate' = tani filtr słowny (workers/gate.py), 'ai' =
    -- klasyfikator. Rozdzielone, bo to dwa różne poziomy zaufania do werdyktu:
    -- bramka odsiewa oczywiste śmieci, model ocenia treść. Bez tej kolumny nie
    -- da się odpowiedzieć na pytanie „ile zleceń zjadła bramka", a to jest
    -- jedyny sposób, żeby zauważyć, że filtr zrobił się za ostry.
    zrodlo_decyzji  TEXT,                               -- 'gate' | 'ai'
    czy_zlecenie    BOOLEAN NOT NULL DEFAULT false,     -- czy to realne zlecenie dla lawety

    -- Dwuliterowy znacznik języka posta z bramki ('pl'|'de'|'cs'|'sk'|NULL).
    -- Nie jest ozdobnikiem: od niego zależy, w JAKIM JĘZYKU operator ma
    -- oddzwonić. Powiadomienie niesie go dalej, bo to jest informacja, której
    -- operator nie wyczyta z pola wypełnionego po polsku przez klasyfikator.
    jezyk           TEXT,

    -- Stan obsługi po stronie CZŁOWIEKA. 'smiec' dostają posty odrzucone przez
    -- bramkę — inaczej kolejka 'nowe' zapełniłaby się w kilka godzin postami,
    -- których nikt nigdy nie otworzy, i przestałaby być kolejką.
    status          TEXT NOT NULL DEFAULT 'nowe',       -- nowe | wyslane | dzwonie | wygrane | przegrane | smiec

    -- Post starszy niż okno świeżości (MAX_WIEK_POSTA_H). Trafia do bazy, bo
    -- jest materiałem do statystyki grupy, ale NIE budzi nikogo powiadomieniem:
    -- zlecenie sprzed sześciu godzin jest już cudze, a alert o nim uczy
    -- operatora ignorować alerty.
    stale           BOOLEAN NOT NULL DEFAULT false
);

-- Kolejka operatora: same zlecenia, od najświeższego. Indeks CZĘŚCIOWY, bo
-- zlecenia to kilka procent wierszy — reszta tabeli istnieje wyłącznie po to,
-- żeby dało się policzyć wydajność grup, i nie ma powodu jej indeksować.
CREATE INDEX IF NOT EXISTS idx_zlecenia_zlecenie_data
    ON zlecenia (post_date DESC) WHERE czy_zlecenie;

-- Filtrowanie po stanie obsługi (panel, statystyki „ile dzwonię, ile wygranych").
CREATE INDEX IF NOT EXISTS idx_zlecenia_status
    ON zlecenia (status);

-- Wydajność per grupa w oknie kilku dni — to zapytanie odpala fetcher w KAŻDYM
-- przebiegu, żeby dać bandycie czym mierzyć. Bez tego indeksu jest to pełny skan
-- tabeli, która rośnie o każdy pobrany post, także odrzucony.
CREATE INDEX IF NOT EXISTS idx_zlecenia_grupa_data
    ON zlecenia (group_url, post_date DESC);

-- Prawa dla roli workerów. Podmień <rola_workerow> na rolę z DATABASE_URL
-- w .env. Bez DELETE i bez DDL — czyszczenie starych postów to osobne zadanie
-- odpalane ręcznie, a nie worker w pętli.
--   GRANT SELECT, INSERT, UPDATE ON zlecenia TO <rola_workerow>;
