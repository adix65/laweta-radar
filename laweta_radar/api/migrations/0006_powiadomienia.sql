-- =============================================================================
-- POWIADOMIENIA — co, kiedy i którym kanałem poszło na telefon operatora.
--
--   (W treści prompta ta migracja nosi numer 003. Repo jest już przy 0003_fetcher
--   i czterocyfrowym schemacie, więc numer jest inny; zawartość ta sama.)
--
--   TABELA ISTNIEJE Z TRZECH POWODÓW, KAŻDY OSOBNY:
--
--   1. DEDUP. Jeden post = jedno powiadomienie, NA ZAWSZE. Bez zapisanego faktu
--      wysyłki fetcher po restarcie albo po zmianie statusu wysłałby to samo
--      zlecenie drugi raz — a druga kopia tego samego alertu uczy operatora, że
--      alerty można przewijać.
--   2. LIMIT GODZINOWY. „Max 15 na godzinę" wymaga policzenia, ile poszło
--      w ostatniej godzinie. Licznik w pamięci procesu nie działa, bo fetcher
--      startuje z crona i kończy się po jednym przebiegu — dla niego każda
--      godzina jest pusta.
--   3. CALLBACKI Z PRZYCISKÓW. Bot (workers/bot.py) dostaje z Telegrama
--      `message_id` i musi wiedzieć, KTÓRĄ wiadomość edytować po kliknięciu
--      „Śmieć". Bez tego przycisk działa raz i zostawia na ekranie wiadomość
--      wyglądającą tak, jakby nic się nie stało.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0005_powiadomienia.sql
--   albo: bash laweta_radar/scripts/migrate.sh
--
--   ZASADA NACZELNA: ta tabela NIE decyduje o widoczności zlecenia. Brak wiersza
--   tutaj znaczy „nie brzęczał telefon", nigdy „zlecenia nie ma". Panel czyta
--   `posty` i pokazuje WSZYSTKO, co system złapał.
-- =============================================================================

CREATE TABLE IF NOT EXISTS powiadomienia (
    id           BIGSERIAL PRIMARY KEY,

    -- Bez FK do `posty`. Wiersz zbiorczy (podsumowanie nocne, komunikat
    -- o przekroczonym limicie) nie dotyczy JEDNEGO posta, a klucz obcy
    -- wymusiłby wtedy albo sztuczny fb_id, albo osobną tabelę na dwa wiersze
    -- dziennie. `fb_id IS NULL` czyta się wprost: „to nie było o konkretnym
    -- zleceniu".
    fb_id        TEXT,

    -- Kanał ORAZ rodzaj zdarzenia. Wartości:
    --   'telegram'         realny alert o zleceniu (jedyny liczony do limitu 15/h)
    --   'push'             web push z panelu (prompt 7) — drugi kanał, nie zamiennik
    --   'zbiorcze'         podsumowanie nocne albo komunikat o przekroczonym limicie
    --   'podsumowanie'     znacznik przy zleceniu ujętym w podsumowaniu zbiorczym
    --   'pominiete_limit'  zlecenie, o którym świadomie nie brzęczeliśmy
    --   'pauza'/'wznowienie'  `/stop` i `/start` z bota — obowiązuje OSTATNI wpis
    --
    -- Pauza siedzi TUTAJ, a nie w osobnej tabeli ustawień, bo to jest zdarzenie
    -- dotyczące powiadomień i musi być widoczne dla DWÓCH procesów: bota (PM2)
    -- i fetchera (cron). Przy okazji dostajemy historię, a „od kiedy jest cicho"
    -- to pierwsze pytanie przy zgłoszeniu „nic nie przychodzi".
    kanal        TEXT NOT NULL DEFAULT 'telegram',
    wyslano_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tresc        TEXT,                               -- dokładnie to, co zobaczył operator
    message_id   BIGINT,                             -- id wiadomości w Telegramie (edycja po callbacku)

    -- --- KLUCZE DEDUPU TREŚCIOWEGO -----------------------------------------
    -- Ten sam post wklejony na pięć grup ma PIĘĆ różnych fb_id, bo hash liczymy
    -- z treści, a treść bywa minimalnie inna („pilne!" dopisane w jednej grupie).
    -- Dedup po identyfikatorze go nie złapie, a operator dostanie pięć wiadomości
    -- o jednym kursie i po tygodniu wyciszy bota.
    --
    -- Klucze leżą TUTAJ, a nie są dociągane joinem z `posty`, z jednego powodu:
    -- zapytanie dedupu chodzi w ścieżce KAŻDEGO wysyłanego alertu i ma być jednym
    -- indeksowym trafieniem w jedną tabelę. Join po kolumnach klasyfikatora przy
    -- każdym poście to skan tabeli rosnącej o kilkaset wierszy dziennie.
    telefon      TEXT,      -- same cyfry, znormalizowane (z `posty.kontakt_wartosc`)
    klucz_tresci TEXT,      -- sha1(odbior_miasto|dostawa_miasto|pojazd_opis)
                            -- patrz services/powiadomienia.klucz_tresci()

    -- Ile grup niosło TO SAMO zlecenie. Rośnie zamiast wysyłać drugą wiadomość:
    -- crosspost nie jest nowym kursem, ale wiedza „widziano to na czterech
    -- grupach" mówi operatorowi, że zlecenie jest realne i że konkurencja
    -- też je widzi.
    grup         INTEGER NOT NULL DEFAULT 1,
    grupy        TEXT[]                              -- nazwy grup, w kolejności pojawienia
);

-- „Czy o TYM poście już szło" — najczęstsze pytanie tej tabeli, raz na post.
-- UNIQUE, nie zwykły indeks: to jest ostatnia linia obrony przed podwójnym
-- alertem, gdy dwa przebiegi fetchera nałożą się w czasie (cron nie gwarantuje,
-- że poprzedni skończył). Warunkowy, bo wiersze zbiorcze mają fb_id NULL
-- i jest ich z założenia wiele.
CREATE UNIQUE INDEX IF NOT EXISTS idx_powiadomienia_fb_id
    ON powiadomienia (fb_id)
    WHERE fb_id IS NOT NULL;

-- Limit godzinowy i dedup treściowy — oba pytają o OKNO CZASU wstecz.
CREATE INDEX IF NOT EXISTS idx_powiadomienia_wyslano
    ON powiadomienia (wyslano_at DESC);

-- Dedup po numerze telefonu w oknie 6 h.
CREATE INDEX IF NOT EXISTS idx_powiadomienia_telefon
    ON powiadomienia (telefon, wyslano_at DESC)
    WHERE telefon IS NOT NULL;

-- Dedup po parze miast i opisie pojazdu, gdy numeru w poście nie było.
CREATE INDEX IF NOT EXISTS idx_powiadomienia_klucz
    ON powiadomienia (klucz_tresci, wyslano_at DESC)
    WHERE klucz_tresci IS NOT NULL;

-- Bot szuka wiersza po `message_id`, żeby wiedzieć, którego zlecenia dotyczy
-- kliknięty przycisk, gdy callback_data zostanie obcięte przez Telegram
-- (limit 64 bajty) — rzadkie, ale wtedy jedyna droga.
CREATE INDEX IF NOT EXISTS idx_powiadomienia_message
    ON powiadomienia (message_id)
    WHERE message_id IS NOT NULL;

-- Prawa dla roli workerów. Podmień <rola_workerow> na rolę z DATABASE_URL.
-- UPDATE jest potrzebny: dopisanie kolejnej grupy do istniejącego wpisu
-- (crosspost) to UPDATE, nie INSERT. DELETE nadal nie — historia wysyłek jest
-- materiałem do odpowiedzi „czemu ten alert przyszedł/nie przyszedł".
--   GRANT SELECT, INSERT, UPDATE ON powiadomienia TO <rola_workerow>;
--   GRANT USAGE, SELECT ON SEQUENCE powiadomienia_id_seq TO <rola_workerow>;
