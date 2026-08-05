-- =============================================================================
-- KATEGORIA ŁADUNKU — co miałoby jechać na lawecie: 'pojazd' | 'zwierze' | 'inne'.
--
--   PO CO OSOBNA KOLUMNA, SKORO OPERATOR ZWIERZĄT NIE WOZI. Bo „nie wozi" i „nie
--   chcę o tym wiedzieć" to dwie różne rzeczy, a bramka ma prawo rozstrzygać
--   tylko o pierwszej. Giełdy transportowe mieszają auta z końmi i bydłem;
--   post o transporcie konia nie jest śmieciem — jest kursem spoza dzisiejszej
--   oferty. Twarde odrzucenie takich postów kasowałoby dane bezpowrotnie
--   i BEZ ŚLADU: nie dałoby się potem odpowiedzieć ani na pytanie „ile tego
--   w ogóle przechodzi przez te grupy", ani „czy opłaciłaby się przyczepa do
--   koni", ani „czy jest co podnajmować dalej". Kolumna jest po to, żeby te
--   pytania dały się zadać danym, a nie pamięci.
--
--   CO ROBI WARTOŚĆ 'zwierze' (i czego NIE robi):
--     • panel pokazuje zlecenie ze znacznikiem i stawia je NIŻEJ na liście,
--     • powiadomienie na Telegramie leci tylko przy ALERT_ZWIERZETA=1 w .env,
--     • NIC nie znika: wiersz jest w tabeli, zlecenie jest w panelu, treść jest
--       w `tresc`. Zasada naczelna repo („system pokazuje, decyduje kierowca")
--       obowiązuje tu tak samo jak wszędzie indziej.
--
--   NULL = bramka nie orzekała (wiersze sprzed tej migracji). To wartość
--   NORMALNA, nie brak do naprawienia — panel i alerty traktują ją dokładnie
--   jak 'pojazd', czyli nie zmieniają niczego. Historii NIE przeliczamy: robi to
--   `scripts/raport_gate.py`, licząc kategorię z TREŚCI, więc pytanie „ile tego
--   przechodzi przez te grupy" ma odpowiedź także dla starych wierszy.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0010_kategoria_ladunku.sql
--   albo: bash laweta_radar/scripts/migrate.sh
-- =============================================================================

ALTER TABLE posty
    ADD COLUMN IF NOT EXISTS kategoria_ladunku TEXT;   -- 'pojazd' | 'zwierze' | 'inne' | NULL

COMMENT ON COLUMN posty.kategoria_ladunku IS
    'Co miałoby jechać: pojazd | zwierze | inne (workers/gate.py). NIE jest '
    'filtrem — steruje wyłącznie znacznikiem w panelu, kolejnością na liście '
    'i tym, czy alert idzie na Telegram (ALERT_ZWIERZETA). NULL = bramka nie '
    'orzekała, traktowane jak pojazd.';

-- Lista zleceń sortuje „zwierzęta na dół", a raport pyta „ile tego w ogóle
-- jest". Indeks CZĘŚCIOWY, bo interesuje nas jedna wartość i jest jej garść —
-- reszta tabeli nie ma powodu puchnąć o kolejny pełny indeks.
CREATE INDEX IF NOT EXISTS idx_posty_zwierzeta
    ON posty (opublikowany_at DESC)
    WHERE kategoria_ladunku = 'zwierze';

-- -----------------------------------------------------------------------------
-- NOWA WARTOŚĆ W `powiadomienia.kanal`: 'pominiete_zwierze'
--
-- Kolumna `kanal` nie ma CHECK-a, więc migracji nie potrzebuje — ale wartość
-- dochodzi do słownika z 0006_powiadomienia.sql i musi być gdzieś opisana.
-- Wiersz powstaje przy każdym zleceniu wyciszonym przez ALERT_ZWIERZETA=0
-- i robi DWIE rzeczy:
--   1. zamyka sprawę posta — podsumowanie ranne szuka zleceń BEZ wiersza
--      w `powiadomienia`, więc bez niego transport konia przyszedłby o świcie,
--      czyli ALERT_ZWIERZETA=0 opóźniałby alert zamiast go wyłączać;
--   2. daje odpowiedź na „ile takich kursów przeszło obok":
--      SELECT count(*) FROM powiadomienia WHERE kanal = 'pominiete_zwierze';
-- Do limitu 15/h NIE jest liczony (liczą się tylko wiersze 'telegram').
-- -----------------------------------------------------------------------------

-- Prawa dla roli workerów: ta migracja dokłada kolumnę do `posty`, a UPDATE
-- i INSERT na tej tabeli są nadane w 0001 — nowy GRANT nie jest potrzebny.
