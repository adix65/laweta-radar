-- =============================================================================
-- ZASOBY APIFY — stan kluczy i proxy PRZEŻYWAJĄCY równoległe runy.
--
--   PO CO TO ISTNIEJE. `.apify_key_state` (plik obok .env) trzymał tylko
--   "który klucz próbować pierwszy" — podpowiedź, nie prawdę. Dwa przebiegi
--   fetchera odpalone naraz (cron gęstszy niż długość runu) nadpisywały ten
--   plik nawzajem, a klucz oznaczony jako MARTWY w jednym procesie wracał do
--   użycia w drugim, bo plik o tym nie wiedział. Stan musi żyć w jednym
--   miejscu widocznym dla WSZYSTKICH procesów naraz — stąd baza, nie plik.
--
--   DWIE TABELE, DWA RÓŻNE CYKLE ŻYCIA:
--     zasoby_apify        — klucz Apify: aktywny / wyczerpany kredyt (wraca
--                            1. dnia miesiąca) / martwy (401, wypada na stałe,
--                            wymaga interwencji człowieka) / błąd sieci / rate
--                            limit (oba przejściowe, licznik tylko do
--                            diagnostyki, NIE blokują klucza).
--     zasoby_apify_proxy  — adres proxy: aktywne / w kwarantannie (30 min po
--                            awarii, potem wraca do weryfikacji). Proxy i klucz
--                            mają OSOBNE cykle życia: jeden padły adres nie ma
--                            prawa oznaczyć konta jako martwe (patrz
--                            workers/apify_keys.classify_apify_error — błąd
--                            transportu jest ZAWSZE oddzielony od wyczerpania).
--
--   KLUCZ_HASH / PROXY_HASH, NIGDY SUROWA WARTOŚĆ. Token Apify i hasło do
--   proxy nie mają prawa trafić do bazy w postaci, z której dałoby się je
--   odtworzyć — sha256 obcięty do 24 znaków (workers/apify_keys._hash_klucza,
--   workers/apify_proxy._hash_proxy) wystarcza, żeby ROZPOZNAĆ ten sam
--   klucz/adres między przebiegami, a nie da się z niego wrócić do sekretu.
--   `etykieta` przy proxy to WYŁĄCZNIE host:port (workers/apify_proxy.proxy_label)
--   — do pokazania w /limity bez ujawniania loginu i hasła.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0012_zasoby_apify.sql
--   albo: bash laweta_radar/scripts/migrate.sh
-- =============================================================================

CREATE TABLE IF NOT EXISTS zasoby_apify (
    klucz_hash   TEXT PRIMARY KEY,       -- sha256(token)[:24] — NIGDY surowy token

    -- 'aktywny' | 'kredyt_wyczerpany' | 'klucz_martwy' | 'blad_sieci' | 'rate_limit'.
    -- Tylko 'kredyt_wyczerpany' i 'klucz_martwy' WYŁĄCZAJĄ klucz z rotacji —
    -- pozostałe dwa to zapis OSTATNIEGO zdarzenia do diagnostyki (patrz nagłówek).
    status       TEXT NOT NULL DEFAULT 'aktywny',

    -- Czytelny powód BEZ treści tokenu — to, co poszłoby do loga i alertu.
    powod        TEXT NOT NULL DEFAULT '',

    -- Kiedy klucz wszedł w BIEŻĄCY status (reset przy każdej zmianie statusu).
    -- Dla 'kredyt_wyczerpany' to podstawa do "wraca 1. dnia miesiąca".
    od_kiedy     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Licznik zdarzeń w BIEŻĄCYM statusie z rzędu — reset do 0, gdy klucz
    -- wraca do 'aktywny'. Rosnący 'blad_sieci'/'rate_limit' bez powrotu do
    -- 'aktywny' jest sygnałem ostrzegawczym, nawet gdy klucz formalnie żyje.
    ile_bledow   INTEGER NOT NULL DEFAULT 0,

    zmieniono_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS zasoby_apify_proxy (
    proxy_hash   TEXT PRIMARY KEY,       -- sha256 tożsamości proxy (schemat+host+port+ścieżka)

    -- Host:port do pokazania w /limity — BEZ loginu i hasła (workers/apify_proxy.proxy_label).
    etykieta     TEXT NOT NULL,

    -- 'aktywne' | 'kwarantanna'. Kwarantanna kończy się o `wraca_o` — worker,
    -- który zobaczy przeterminowaną kwarantannę, wraca do weryfikacji (test d
    -- z docs/APIFY-PROXY.md), a nie wraca do puli automatycznie.
    status       TEXT NOT NULL DEFAULT 'aktywne',

    od_kiedy     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    wraca_o      TIMESTAMPTZ,            -- NULL, gdy status='aktywne'
    ile_bledow   INTEGER NOT NULL DEFAULT 0,

    -- Który klucz aktualnie z tego proxy korzysta (klucz_hash, nie token) —
    -- do sekcji proxy w /limity: "które konto z którego wychodzi".
    przypisany_klucz_hash TEXT,

    zmieniono_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- `/limity` i alert wyczerpania pytają "które klucze/proxy NIE są aktywne
-- teraz" — indeks na status obsługuje oba bez pełnego skanu małej tabeli.
CREATE INDEX IF NOT EXISTS idx_zasoby_apify_status
    ON zasoby_apify (status);
CREATE INDEX IF NOT EXISTS idx_zasoby_apify_proxy_status
    ON zasoby_apify_proxy (status);

-- Prawa dla roli workerów. Podmień <rola_workerow> na rolę z DATABASE_URL.
-- UPDATE potrzebny do upsertu stanu (ON CONFLICT DO UPDATE) — bez DELETE:
-- wygasłe wpisy nadpisuje kolejny upsert, a historia stanu nie jest tu
-- prowadzona (to nie jest tabela audytowa, tylko bieżący stan puli).
--   GRANT SELECT, INSERT, UPDATE ON zasoby_apify TO <rola_workerow>;
--   GRANT SELECT, INSERT, UPDATE ON zasoby_apify_proxy TO <rola_workerow>;
