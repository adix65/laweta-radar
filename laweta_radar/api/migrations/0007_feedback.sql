-- =============================================================================
-- FEEDBACK — zbiór treningowy do poprawiania bramki i promptu klasyfikatora.
--
--   (W treści prompta ta migracja nosi numer 004. Repo używa czterocyfrowego
--   schematu i jest już przy 0005_powiadomienia — numer inny, zawartość ta sama.)
--
--   PO CO TO ISTNIEJE. Każde kliknięcie „Śmieć" to zdanie: model orzekł
--   „zlecenie", człowiek spojrzał i powiedział „nie". Ta para jest jedyną pętlą
--   zwrotną, jaką ten system ma — bez niej prompt klasyfikatora poprawia się
--   z pamięci, a pamięć po tygodniu nie odtworzy, KTÓRY post został odrzucony
--   i co model o nim sądził.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0006_feedback.sql
--   albo: bash laweta_radar/scripts/migrate.sh
--
--   TREŚĆ POSTA I WERDYKT SĄ TU SKOPIOWANE, nie dociągane joinem z `posty`.
--   Materiał treningowy ma przeżyć czyszczenie tabeli postów (posty z grup żyją
--   godzinami, a zbiór do strojenia promptu jest wart tyle, ile ma przykładów).
--   Klucz obcy związałby te dwa cykle życia i pierwsza retencja skasowałaby
--   dorobek kilku miesięcy.
--
--   Wejście do następnej iteracji promptu:
--     python laweta_radar/scripts/raport_feedback.py
--   Bez tego skryptu ta tabela będzie tylko rosła — i to jest realny scenariusz,
--   nie ostrzeżenie na wyrost: dane, których nikt nie wypisuje, nie istnieją.
-- =============================================================================

CREATE TABLE IF NOT EXISTS feedback (
    id               BIGSERIAL PRIMARY KEY,
    fb_id            TEXT NOT NULL,

    -- 'smiec' = model się pomylił w tę stronę, 'dobre' = potwierdzone zlecenie.
    -- Obie oceny mają wartość, ale NIE tę samą: 'smiec' pokazuje, co dopisać do
    -- bramki i czym doprecyzować prompt; 'dobre' jest kontrolą, że kolejna wersja
    -- promptu nie zaczęła odrzucać rzeczy, które wcześniej łapała.
    ocena            TEXT NOT NULL,

    -- Kopia, nie referencja — patrz nagłówek.
    tresc_posta      TEXT,
    werdykt_ai_json  JSONB,

    ocenil_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Jedna ocena danego rodzaju na post. Operator klika „Śmieć" pod powiadomieniem,
-- a potem jeszcze raz w panelu — i to jest normalne zachowanie, nie błąd, więc
-- ma kończyć się jednym wierszem, a nie duplikatem, który zaważy w statystyce
-- dwa razy. UNIQUE, bo `services/feedback.py` liczy na ON CONFLICT DO NOTHING.
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_fb_ocena
    ON feedback (fb_id, ocena);

-- Raport czyta okno ostatnich dni, od najnowszych.
CREATE INDEX IF NOT EXISTS idx_feedback_ocenil
    ON feedback (ocenil_at DESC);

-- Prawa dla roli workerów. Podmień <rola_workerow> na rolę z DATABASE_URL.
-- Bez DELETE: skasowany przykład treningowy to przykład, którego nikt nie odtworzy.
--   GRANT SELECT, INSERT ON feedback TO <rola_workerow>;
--   GRANT USAGE, SELECT ON SEQUENCE feedback_id_seq TO <rola_workerow>;
