-- =============================================================================
-- KIERUNEK ZGŁOSZENIA — po której stronie rynku stoi autor posta:
-- 'zlecenie' | 'oferta' | 'niejasne'.
--
--   PROBLEM, KTÓRY TO ROZWIĄZUJE. Na giełdach transportowych obie strony rynku
--   piszą posty o TYM SAMYM KSZTAŁCIE: trasa, data, numer telefonu. Dwa poniższe
--   przeszły przez bramkę i klasyfikator jako zlecenia i obudziły telefon:
--       „Czwartek 06.08.26r wolna laweta Elblag-Lublin tel.501606207"
--       „Wolny transport 10.08 na trasie Grudziadz - Warszawa - Siedlce 25T"
--   To są posty PRZEWOŹNIKÓW oferujących wolne miejsce, czyli konkurencji.
--   Różnica jest w kierunku, nie w słowach: zlecenie to „szukam kogoś, kto
--   przewiezie", oferta to „jadę tamtędy i mam miejsce".
--
--   PO CO OSOBNA KOLUMNA, SKORO TO NIE ZLECENIA. Z dokładnie tego samego powodu
--   co przy `kategoria_ladunku` (0010): „nie budzić" i „nie zapisywać" to dwie
--   różne rzeczy. Cudza laweta jadąca 10.08 z Grudziądza do Siedlec to na trasie,
--   którą operator i tak jedzie, okazja na doładunek albo na podnajęcie kursu.
--   Skasowany post nie odpowie już na żadne pytanie — ani „ile tego przechodzi
--   przez te grupy", ani „kto wozi na naszych kierunkach", ani „czy jest z kim
--   gadać o stałej współpracy".
--
--   CO ROBI WARTOŚĆ 'oferta' (i czego NIE robi):
--     • bramka odrzuca taki post PRZED modelem — nie płacimy za tokeny,
--     • wiersz powstaje normalnie, z kompletem pól, `czy_zlecenie=false`
--       i `status='smiec'` (czyli poza kolejką operatora, tak jak każdy inny
--       post nie-będący zleceniem),
--     • powiadomienie leci tylko przy ALERT_OFERTY=1 w .env,
--     • NIC nie znika: wiersz jest w tabeli, treść jest w `tresc`, kierunek
--       tłumaczy werdykt. Pary (czy_zlecenie=false, kierunek='oferta')
--       i (czy_zlecenie=false, kierunek='zlecenie') wyglądałyby bez tej kolumny
--       identycznie, a znaczą co innego: pierwsza to konkurencja na naszej
--       trasie, druga to reklama albo sprzedaż auta.
--
--   DWA ŹRÓDŁA JEDNEJ WARTOŚCI. Kierunek wystawia bramka (`workers/gate.py`,
--   wzorzec) albo klasyfikator (`workers/classifier.py`, przeczytane zdanie).
--   Model bije bramkę, ale tylko gdy cokolwiek rozstrzygnął — „niejasne"
--   zostawia w mocy odczyt bramki. Słownik wartości jest JEDEN i mieszka
--   w bramce (KIERUNEK_*), żeby kolumna nie zaczęła nieść dwóch naraz.
--
--   NULL = nikt nie orzekał (wiersze sprzed tej migracji). Wartość NORMALNA,
--   nie brak do naprawienia — panel i alerty traktują ją dokładnie jak
--   'zlecenie', czyli nie zmieniają niczego. Historii NIE przeliczamy: robi to
--   `scripts/raport_gate.py`, licząc kierunek z TREŚCI, więc pytanie „ile ofert
--   przechodzi przez te grupy" ma odpowiedź także dla starych wierszy.
--
--   ODPALANIE — jako postgres, RĘCZNIE, nigdy z workera:
--     psql "$DATABASE_URL_ADMIN" -f laweta_radar/api/migrations/0011_kierunek.sql
--   albo: bash laweta_radar/scripts/migrate.sh
-- =============================================================================

ALTER TABLE posty
    ADD COLUMN IF NOT EXISTS kierunek TEXT;   -- 'zlecenie' | 'oferta' | 'niejasne' | NULL

COMMENT ON COLUMN posty.kierunek IS
    'Po której stronie rynku stoi autor: zlecenie (szuka przewoźnika) | oferta '
    '(sam oferuje przejazd — konkurencja) | niejasne. Wystawia bramka '
    '(workers/gate.py) albo klasyfikator (workers/classifier.py). "oferta" '
    'oznacza czy_zlecenie=false i brak alertu (ALERT_OFERTY), ale NIE oznacza '
    'skasowania wiersza. NULL = nikt nie orzekał, traktowane jak zlecenie.';

-- „Ile ofert przeszło przez te grupy i na jakich kierunkach" — jedyne pytanie,
-- do którego ta kolumna służy poza samym werdyktem. Indeks CZĘŚCIOWY, jak przy
-- zwierzętach: interesuje nas jedna wartość, a reszta tabeli nie ma powodu
-- puchnąć o kolejny pełny indeks.
CREATE INDEX IF NOT EXISTS idx_posty_oferty
    ON posty (opublikowany_at DESC)
    WHERE kierunek = 'oferta';

-- -----------------------------------------------------------------------------
-- NOWA WARTOŚĆ W `powiadomienia.kanal`: 'pominiete_oferta'
--
-- Kolumna `kanal` nie ma CHECK-a, więc migracji nie potrzebuje — ale wartość
-- dochodzi do słownika z 0006_powiadomienia.sql i musi być gdzieś opisana.
--
-- Wiersz powstaje w JEDNYM przypadku: bramka rozpoznała ofertę, a model mimo to
-- orzekł „zlecenie". Post ma wtedy `czy_zlecenie=true` i `status='nowe'`, więc
-- podsumowanie ranne (szukające zleceń BEZ wiersza w `powiadomienia`) przysłałoby
-- go o świcie — czyli ALERT_OFERTY=0 opóźniałby alert zamiast go wyłączać.
-- Dokładnie ta sama konieczność co przy 'pominiete_zwierze' w 0010.
--
-- Przy zgodnym werdykcie (`czy_zlecenie=false`) wiersz NIE powstaje, bo alert
-- nigdy nie jest rozważany — oferta rozpoznana po obu stronach zostawia
-- w `powiadomienia` czystą kartę.
--
-- Ile okazji na doładunek przeszło obok:
--      SELECT count(*) FROM powiadomienia WHERE kanal = 'pominiete_oferta';
-- Do limitu 15/h NIE jest liczony (liczą się tylko wiersze 'telegram').
-- -----------------------------------------------------------------------------

-- Prawa dla roli workerów: ta migracja dokłada kolumnę do `posty`, a UPDATE
-- i INSERT na tej tabeli są nadane w 0001 — nowy GRANT nie jest potrzebny.
