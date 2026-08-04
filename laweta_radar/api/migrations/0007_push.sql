-- =============================================================================
-- WEB PUSH — subskrypcje przeglądarek. DRUGI kanał obok Telegrama.
--
--   TELEGRAM ZOSTAJE KANAŁEM PODSTAWOWYM i ta tabela tego nie zmienia. Push
--   jest dodatkiem: działa tylko w przeglądarce, która go obsługuje, tylko po
--   udzieleniu zgody, a na iOS wyłącznie po dodaniu PWA do ekranu głównego.
--   Każdy z tych warunków potrafi przestać być spełniony bez żadnego objawu
--   (użytkownik czyści dane strony, iOS usuwa nieużywaną PWA) — a kanał, który
--   cicho przestaje dowozić, jest gorszy niż brak kanału.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0007_push.sql
--   albo: bash laweta_radar/scripts/migrate.sh
-- =============================================================================

CREATE TABLE IF NOT EXISTS push_subskrypcje (
    -- Endpoint jest naturalnym kluczem głównym: dostawca (Google/Apple/Mozilla)
    -- gwarantuje jego unikalność, a ta sama przeglądarka po odnowieniu
    -- subskrypcji dostaje NOWY endpoint. Dzięki temu ponowny zapis z tego
    -- samego telefonu nie tworzy duplikatu, tylko odświeża wiersz.
    endpoint     TEXT PRIMARY KEY,

    p256dh       TEXT NOT NULL,        -- klucz publiczny przeglądarki (szyfrowanie treści)
    auth         TEXT NOT NULL,        -- sekret uwierzytelniający przeglądarki
    urzadzenie   TEXT,                 -- User-Agent, skrócony — do pytania „który telefon"
    dodano_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Ostatnia UDANA wysyłka i ostatni błąd. Bez tych dwóch kolumn martwa
    -- subskrypcja (odinstalowana PWA) wygląda identycznie jak żywa i zostaje
    -- w tabeli na zawsze, dokładając nieudane wywołanie do każdego alertu.
    ostatnia_ok_at TIMESTAMPTZ,
    ostatni_blad   TEXT
);

-- Wysyłka bierze wszystkie żywe subskrypcje — przy jednym użytkowniku będzie
-- ich kilka (telefon, tablet, laptop). Indeks jest na zapas, na wypadek gdyby
-- tabela zaczęła zbierać martwe wpisy szybciej, niż je kasujemy.
CREATE INDEX IF NOT EXISTS idx_push_dodano
    ON push_subskrypcje (dodano_at DESC);

-- Prawa dla roli workerów. Podmień <rola_workerow> na rolę z DATABASE_URL.
-- DELETE jest tu WYJĄTKIEM w całym repo i ma powód: gdy dostawca odpowie
-- 404/410 („subskrypcja wygasła"), jedyną poprawną reakcją jest usunięcie
-- wiersza. Trzymanie martwych endpointów to nieudane wywołanie HTTPS doklejane
-- do każdego kolejnego alertu — czyli opóźnienie na ścieżce, która ma dowieźć
-- zlecenie w sekundę.
--   GRANT SELECT, INSERT, UPDATE, DELETE ON push_subskrypcje TO <rola_workerow>;
