-- =============================================================================
-- FETCHER — kolumny, które dokłada `workers/fb_fetcher.py`, oraz tabela
--   `harmonogram` (stan pobierania i licznik budżetu per grupa).
--
--   Ta migracja domyka `posty` do postaci, w której da się na niej pracować:
--   0001 zapisało to, co widać w surowym poście, 0002 dołożyło decyzję bramki,
--   a tu dochodzi ŹRÓDŁO DECYZJI, jej wynik i stan obsługi po stronie człowieka.
--   Podział na trzy migracje jest celowy — każdy krok pipeline'u zakłada tylko
--   te kolumny, które sam wypełnia, więc widać, kto za co odpowiada.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0003_fetcher.sql
--   albo: bash laweta_radar/scripts/migrate.sh
--
--   Workery łączą się rolą BEZ uprawnień DDL — GRANT jest na końcu pliku.
-- =============================================================================

-- --- 1. Decyzja: kto ją podjął i jak wypadła --------------------------------
ALTER TABLE posty
    -- 'gate' = rozstrzygnęła bramka słowna, 'ai' = rozstrzygnął model. Bez tego
    -- rozdziału nie da się odpowiedzieć na pytanie „ile zleceń zjadła bramka",
    -- a to jedyny sposób, żeby zauważyć, że filtr zrobił się za ostry.
    ADD COLUMN IF NOT EXISTS zrodlo_decyzji TEXT,

    ADD COLUMN IF NOT EXISTS czy_zlecenie   BOOLEAN NOT NULL DEFAULT false,

    -- Stan obsługi po stronie CZŁOWIEKA. 'smiec' dostają posty odrzucone —
    -- inaczej kolejka 'nowe' zapełniłaby się w kilka godzin postami, których
    -- nikt nigdy nie otworzy, i przestałaby być kolejką.
    ADD COLUMN IF NOT EXISTS status         TEXT NOT NULL DEFAULT 'nowe',
                             -- nowe | wyslane | dzwonie | wygrane | przegrane | smiec

    -- Post starszy niż okno świeżości (MAX_WIEK_POSTA_H, domyślnie 6 h). Trafia
    -- do bazy, bo jest materiałem do statystyki grupy, ale NIE idzie do modelu
    -- i NIE budzi nikogo: zlecenie sprzed sześciu godzin jest już cudze,
    -- a alert o nim uczy operatora ignorować alerty.
    ADD COLUMN IF NOT EXISTS stale          BOOLEAN NOT NULL DEFAULT false,

    -- Dwuliterowy znacznik języka z bramki ('pl'|'de'|'cs'|'sk'|NULL). NIE jest
    -- ozdobnikiem: od niego zależy, w JAKIM JĘZYKU operator ma oddzwonić.
    -- Powiadomienie niesie go dalej, bo wszystkie pozostałe pola alertu są już
    -- po polsku (klasyfikator tłumaczy) i sam post tego nie zdradzi.
    -- NULL = bramka nie rozstrzygnęła; wartość normalna, nie błąd.
    ADD COLUMN IF NOT EXISTS gate_jezyk     TEXT;

-- Kolejka operatora: same zlecenia, od najświeższego. Indeks CZĘŚCIOWY, bo
-- zlecenia to kilka procent wierszy — reszta tabeli istnieje po to, żeby dało
-- się policzyć wydajność grup, i nie ma powodu jej indeksować.
CREATE INDEX IF NOT EXISTS idx_posty_zlecenia
    ON posty (opublikowany_at DESC) WHERE czy_zlecenie;

-- Filtrowanie po stanie obsługi (panel, statystyki „ile dzwonię, ile wygranych").
CREATE INDEX IF NOT EXISTS idx_posty_status
    ON posty (status);

-- --- 2. HARMONOGRAM — stan pobierania per grupa -----------------------------
--
-- DLACZEGO W BAZIE, a nie liczone z zegara. Repo źródłowe (sales-core-engine)
-- ma harmonogram BEZSTANOWY: slot z godziny, faza z hasha URL-a grupy, zero
-- plików do zgubienia. To jest lepsze wszędzie tam, gdzie run jest DARMOWY,
-- a jedynym kosztem jest czas. Tutaj run kosztuje pobrane posty, a budżet jest
-- DOBOWY i wspólny dla wszystkich grup — a licznika dobowego nie da się
-- odtworzyć z zegara: proces startuje z crona, kończy po jednym przebiegu
-- i nie pamięta nic.
--
-- Bez tej tabeli „nie przekraczaj 2000 postów na dobę" jest życzeniem, a nie
-- zabezpieczeniem — a przekroczony po cichu budżet to spalona pula kont Apify,
-- z której korzysta TAKŻE sales-core-engine.
--
-- Jeden wiersz na grupę, kilkanaście wierszy w sumie. Tabela STANU, nie zdarzeń
-- — historię przebiegów czyta się z `posty.pobrany_at`.
CREATE TABLE IF NOT EXISTS harmonogram (
    group_url        TEXT PRIMARY KEY,                   -- klucz z config/groups.py
    ostatni_run_at   TIMESTAMPTZ,                        -- kiedy ostatnio pytaliśmy actora
    nastepny_run_at  TIMESTAMPTZ,                        -- kiedy najwcześniej wolno znowu
    interwal_min     INTEGER,                            -- odstęp z ostatniego przebiegu (diagnostyka)

    -- Licznik dobowy. `doba` to data (UTC), której dotyczy `pobrane_doba` —
    -- zerowanie licznika to porównanie daty, a nie osobne zadanie w cronie,
    -- które można zapomnieć odpalić albo które padnie w nocy razem z resztą.
    doba             DATE,
    pobrane_doba     INTEGER NOT NULL DEFAULT 0,         -- ile postów kosztowała ta grupa w tej dobie
    przydzial_doba   INTEGER,                            -- ile przyznał jej bandyta na tę dobę

    -- Ostatni błąd pobrania. Nie po to, żeby go obsługiwać automatem — po to,
    -- żeby „grupa milczy od trzech dni" dało się odróżnić od „grupa jest pusta"
    -- bez wchodzenia w logi PM2 na VPS-ie.
    ostatni_blad     TEXT,
    zmieniony_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- „Które grupy wypadają teraz" — jedyne zapytanie, które ta tabela obsługuje
-- w ścieżce gorącej. Przy kilkunastu wierszach nic dziś nie zmienia, ale zmieni,
-- gdy lista grup urośnie po włączeniu rynków DE/CZ/SK.
CREATE INDEX IF NOT EXISTS idx_harmonogram_nastepny
    ON harmonogram (nastepny_run_at);

-- Prawa dla roli workerów. Podmień <rola_workerow> na rolę z DATABASE_URL.
-- UPDATE jest niezbędny (licznik dobowy), DELETE nadal nie — wpis grupy
-- usuniętej z configu zostaje i jest historią, a nie śmieciem.
--   GRANT SELECT, INSERT, UPDATE ON posty       TO <rola_workerow>;
--   GRANT SELECT, INSERT, UPDATE ON harmonogram TO <rola_workerow>;
