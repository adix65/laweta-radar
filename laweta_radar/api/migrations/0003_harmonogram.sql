-- =============================================================================
-- HARMONOGRAM — stan pobierania per grupa: kiedy ostatnio, kiedy następny,
--   ile postów już kosztowała w tej dobie.
--
--   DLACZEGO TO JEST W BAZIE, a nie liczone z zegara. Repo źródłowe ma
--   harmonogram BEZSTANOWY: slot z godziny, faza z hasha URL-a grupy, zero
--   plików do zgubienia (workers/apify_fb_fetcher.py: `_is_due`). To rozwiązanie
--   jest lepsze wszędzie tam, gdzie run jest DARMOWY, a jedynym kosztem jest
--   czas. Tutaj run kosztuje pobrane posty, a te kosztują pieniądze — i budżet
--   jest DOBOWY, wspólny dla wszystkich grup i wspólny z drugim systemem na tym
--   samym VPS-ie. Licznika dobowego nie da się odtworzyć z zegara: proces
--   startuje z crona, kończy się po jednym przebiegu i nie pamięta nic.
--
--   Bez tej tabeli „nie przekraczaj 2000 postów na dobę" jest życzeniem, a nie
--   zabezpieczeniem — a przekroczony po cichu budżet to spalona pula kont Apify,
--   z której korzysta też sales-core-engine.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0003_harmonogram.sql
--   albo: bash laweta_radar/scripts/migrate.sh
--
--   ROZMIAR: jeden wiersz na grupę, kilkanaście wierszy w sumie. Tabela stanu,
--   nie tabela zdarzeń — historię przebiegów czyta się z `zlecenia.pobrano_at`,
--   tutaj trzymamy tylko to, co musi przeżyć do następnego przebiegu.
-- =============================================================================

CREATE TABLE IF NOT EXISTS harmonogram (
    group_url        TEXT PRIMARY KEY,                   -- klucz z config/groups.py
    ostatni_run_at   TIMESTAMPTZ,                        -- kiedy ostatnio pytaliśmy actora o tę grupę
    nastepny_run_at  TIMESTAMPTZ,                        -- kiedy najwcześniej wolno zapytać znowu
    interwal_min     INTEGER,                            -- odstęp policzony w ostatnim przebiegu (do logu i diagnostyki)

    -- Licznik dobowy. `doba` to data (UTC), której dotyczy `pobrane_doba` —
    -- zerowanie licznika to porównanie daty, a nie osobne zadanie w cronie,
    -- które można zapomnieć odpalić albo które padnie w nocy razem z resztą.
    doba             DATE,
    pobrane_doba     INTEGER NOT NULL DEFAULT 0,         -- ile postów ta grupa kosztowała w tej dobie
    przydzial_doba   INTEGER,                            -- ile postów przyznał jej bandyta na tę dobę

    -- Ostatni błąd pobrania (typ + skrócony komunikat). Nie po to, żeby go
    -- obsługiwać automatem — po to, żeby „grupa milczy od trzech dni" dało się
    -- odróżnić od „grupa jest pusta" bez wchodzenia w logi PM2 na VPS-ie.
    ostatni_blad     TEXT,
    zmieniony_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- „Które grupy wypadają teraz" — jedyne zapytanie, które ta tabela obsługuje
-- w ścieżce gorącej. Przy kilkunastu wierszach indeks nic nie zmienia dziś,
-- ale zmieni, gdy lista grup urośnie po włączeniu rynków DE/CZ/SK.
CREATE INDEX IF NOT EXISTS idx_harmonogram_nastepny
    ON harmonogram (nastepny_run_at);

-- Prawa dla roli workerów. Podmień <rola_workerow> na rolę z DATABASE_URL.
-- Tu UPDATE jest niezbędny (licznik dobowy), DELETE nadal nie — wpis grupy
-- usuniętej z configu zostaje i jest historią, a nie śmieciem.
--   GRANT SELECT, INSERT, UPDATE ON harmonogram TO <rola_workerow>;
