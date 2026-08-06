"""Bot Telegrama — przyciski pod powiadomieniem i cztery komendy.

OSOBNY PROCES W PM2, nie część fetchera. Fetcher chodzi z crona, kończy się po
jednym przebiegu i nie ma go przez większość doby — a przycisk „Śmieć" ma
działać w sekundę po tym, jak operator go dotknie, o czwartej nad ranem
włącznie. Long polling zamiast webhooka, bo webhook wymaga publicznego HTTPS,
certyfikatu i tego, żeby ktoś pilnował, że nginx nie przestał przekierowywać —
a long polling wymaga tylko tego, żeby proces żył.

DLACZEGO PRZYCISKI SĄ WAŻNIEJSZE, NIŻ WYGLĄDAJĄ. „Śmieć" to nie kosmetyka
i nie porządkowanie kolejki: każde kliknięcie to wiersz w tabeli `feedback`,
czyli para (treść posta, werdykt modelu), na której poprawia się bramkę i prompt
klasyfikatora. To jest jedyna pętla zwrotna w tym systemie. Bez niej prompt
poprawia się z pamięci, a pamięć po tygodniu nie odtworzy, KTÓRY post był zły.

BEZPIECZEŃSTWO: przyjmujemy WYŁĄCZNIE wiadomości z `TELEGRAM_CHAT_ID`. Adres
bota jest z natury publiczny — wystarczy znać jego nazwę, żeby do niego napisać
— a komendy tego bota zmieniają statusy zleceń i wyciszają powiadomienia.
Wszystko z innego czatu jest logowane i wyrzucane bez odpowiedzi (odpowiedź
potwierdzałaby, że bot istnieje).

    pm2 start ecosystem.config.js --only laweta-bot
    python -m laweta_radar.workers.bot --raz   # jeden przebieg, do testów
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from laweta_radar.config import settings
from laweta_radar.services import feedback, geo, powiadomienia, telegram_notify

KTO = "bot"

# Long polling: Telegram trzyma połączenie do 30 s i oddaje, gdy coś przyjdzie.
# Ani jedno wywołanie na sekundę (marnotrawstwo), ani nasłuch bez końca
# (proxy i load balancery zrywają wiszące połączenia po minucie).
TIMEOUT_POLL_S = 30

# Po błędzie sieci czekamy, zamiast walić w API. Rośnie do sufitu i wraca do
# minimum po pierwszym udanym odczycie — sieć na VPS-ie potrafi zniknąć na
# kilkanaście sekund i bot ma to przeżyć bez restartu z PM2.
PRZERWA_MIN_S = 2
PRZERWA_MAX_S = 60

DOMYSLNIE_OSTATNICH = 10
MAX_OSTATNICH = 30


def _log(msg: str) -> None:
    print(f"[{KTO}] {msg}", file=sys.stderr, flush=True)


# Pauza (`/stop`, `/start`) mieszka w `services/powiadomienia.py`, nie tutaj:
# stan czyta FETCHER przed każdą wysyłką, a bot go tylko przestawia. Dwie kopie
# tego samego zapytania rozjechałyby się przy pierwszej zmianie schematu.
pauza_aktywna = powiadomienia.pauza_aktywna
_ustaw_pauze = powiadomienia.ustaw_pauze


# ---------------------------------------------------------------------------
# Obsługa callbacków z przycisków
# ---------------------------------------------------------------------------
# Mapowanie przycisk -> status. Trzymane jako dane, bo dopisanie czwartego
# przycisku ma być jedną linijką tutaj i jedną w `powiadomienia.zbuduj_przyciski`,
# a nie kolejnym `elif` w środku pętli zdarzeń.
AKCJE = {
    "smiec": ("smiec", "🗑 oznaczone jako śmieć", "smiec"),
    "biore": ("dzwonie", "✅ biorę — dzwonię", None),
}


def _zmien_status(conn, fb_id: str, status: str, ocena: str | None) -> bool:
    """Zmiana statusu z callbacku. True, gdy taki post istnieje."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE posty SET status = %s, status_at = NOW() "
            " WHERE fb_id = %s RETURNING fb_id", (status, fb_id))
        istnieje = cur.fetchone() is not None
    conn.commit()
    if istnieje and ocena:
        feedback.zapisz(conn, fb_id, ocena)
    return istnieje


def obsluz_callback(conn, callback: dict) -> None:
    """Kliknięcie w przycisk pod powiadomieniem.

    TRZY RZECZY W TEJ KOLEJNOŚCI i kolejność jest ważna:
      1. `answerCallbackQuery` — ZAWSZE i najpierw. Bez tego Telegram trzyma
         kręcące się kółko na przycisku przez kilkanaście sekund, a operator
         klika drugi raz, bo wygląda, że nie zadziałało.
      2. zmiana statusu w bazie,
      3. edycja wiadomości: dopisek i ZDJĘCIE PRZYCISKÓW. Alert, który po
         kliknięciu wygląda identycznie, uczy klikać jeszcze raz — a przy
         „Śmieć" drugie kliknięcie to zlecenie wyrzucone przez pomyłkę.
    """
    dane = str(callback.get("data") or "")
    akcja, _, fb_id = dane.partition(":")
    wiadomosc = callback.get("message") or {}
    message_id = wiadomosc.get("message_id")

    telegram_notify.wywolaj("answerCallbackQuery",
                            {"callback_query_id": callback.get("id"),
                             "text": AKCJE.get(akcja, ("", "nieznana akcja", None))[1]})

    if akcja not in AKCJE or not fb_id:
        _log(f"nieznany callback {dane!r}")
        return

    status, dopisek, ocena = AKCJE[akcja]
    if not _zmien_status(conn, fb_id, status, ocena):
        _log(f"callback {akcja} dla nieznanego fb_id {fb_id}")
        return
    _log(f"{fb_id}: {akcja} -> status={status}")

    if message_id is None:
        return
    _dopisz_i_zdejmij_przyciski(wiadomosc, dopisek)


def _dopisz_i_zdejmij_przyciski(wiadomosc: dict, dopisek: str) -> None:
    """Dopisek pod alertem i ZDJĘCIE przycisków — osobno dla tekstu i dla zdjęcia.

    ALERT Z MAPĄ TRASY NIE MA POLA `text`, tylko `caption` (patrz
    services/powiadomienia._wyslij_alert). `editMessageText` odpowiada na taką
    wiadomość błędem „there is no text in the message to edit", więc przyciski
    zostałyby na ekranie — a alert, który po kliknięciu wygląda identycznie,
    uczy klikać jeszcze raz. Przy „Śmieć" drugie kliknięcie to zlecenie
    wyrzucone przez pomyłkę, więc to nie jest kosmetyka.

    Metodę wybieramy po TYM, CO PRZYSZŁO W CALLBACKU, a nie po pamiętanym
    sposobie wysyłki: bot chodzi w osobnym procesie niż fetcher i o wysyłce wie
    tylko tyle, ile widzi w zdarzeniu.
    """
    chat_id = (wiadomosc.get("chat") or {}).get("id")
    message_id = wiadomosc.get("message_id")
    # Rozstrzyga BRAK POLA `text`, a nie obecność `photo`: `editMessageText`
    # wymaga tekstu i odrzuca każdą wiadomość, która go nie ma. Alert z mapą
    # przychodzi z `caption`, ale reguła jest szersza niż jeden nasz przypadek.
    ze_zdjeciem = wiadomosc.get("text") is None

    if ze_zdjeciem:
        ogon = f"\n\n— {dopisek}"
        stary = wiadomosc.get("caption") or ""
        # Podpis pod zdjęciem ma limit 1024 znaków i alert potrafi go dotykać
        # (powiadomienia.podpis_pod_zdjeciem przycina treść dokładnie do niego).
        # Doklejony dopisek przekroczyłby limit, Telegram odrzuciłby edycję,
        # a przyciski zostałyby na ekranie — czyli dokładnie ten skutek, przed
        # którym ta funkcja broni.
        nadmiar = len(stary) + len(ogon) - telegram_notify.MAX_CAPTION
        if nadmiar > 0:
            stary = stary[:-nadmiar].rstrip()
        telegram_notify.wywolaj("editMessageCaption", {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": f"{stary}{ogon}",
        })
        return

    stary = wiadomosc.get("text") or ""
    telegram_notify.wywolaj("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": f"{stary}\n\n— {dopisek}",
        "disable_web_page_preview": True,
        # BEZ reply_markup = przyciski znikają. Świadomie nie podmieniamy ich na
        # „cofnij": cofanie jest w panelu, gdzie widać pełną treść posta,
        # a nie pod alertem, gdzie operator już podjął decyzję.
    })


# ---------------------------------------------------------------------------
# Komendy
# ---------------------------------------------------------------------------
def _dzis(conn) -> str:
    """Podsumowanie doby: ile zleceń, ile wziętych, ile kilometrów.

    Kilometry liczymy w Pythonie przez `services/geo.py`, a nie w SQL-u —
    zależą od `BAZA_LAT/BAZA_LON`, których w bazie nie ma i celowo nie będzie
    (patrz `api/routers/zlecenia.py`).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, odbior_kod, odbior_miasto, dostawa_kod, dostawa_miasto,
                   cena_koncowa, tresc
              FROM posty
             WHERE czy_zlecenie
               AND pobrany_at > date_trunc('day', NOW())
            """)
        wiersze = cur.fetchall()

    wziete = [w for w in wiersze if w[0] in ("dzwonie", "wygrane")]
    km = 0.0
    for _status, o_kod, o_miasto, d_kod, d_miasto, _cena, tresc_posta in wziete:
        # Treść posta rozstrzyga kraj przy nazwie z wielu krajów — te same
        # kilometry co w alercie, więc i ta sama ścieżka geokodowania.
        pods = geo.podsumowanie(geo.geokoduj(o_kod, o_miasto, tresc=tresc_posta),
                                geo.geokoduj(d_kod, d_miasto, tresc=tresc_posta))
        # Dojazd PLUS trasa: operator pyta „ile dziś przejechałem", a nie
        # „jak długie były kursy". Pusty przebieg z bazy też zużywa paliwo.
        km += (pods["km_od_bazy"] or 0) + (pods["km_trasy"] or 0)
    przychod = sum(float(w[5] or 0) for w in wiersze if w[0] == "wygrane")

    linie = [
        "*📅 Dzisiaj*",
        "",
        f"zleceń: {len(wiersze)}",
        f"wziętych: {len(wziete)}",
        f"kilometrów (dojazd + trasa): ~{round(km)}",
    ]
    if przychod:
        linie.append(f"przychód z wygranych: {przychod:.0f} zł")
    if not wiersze:
        linie.append("")
        linie.append("Zero zleceń to normalny dzień — sprawdź /zdrowie, "
                     "jeśli cisza trwa drugą dobę.")
    return "\n".join(linie)


def _ostatnie(conn, ile: int) -> str:
    """N ostatnich zleceń, zwięźle. Jedna linia na zlecenie, bo to jest lista
    do przewinięcia kciukiem, a nie raport."""
    ile = max(1, min(ile, MAX_OSTATNICH))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fb_id, grupa_nazwa, opublikowany_at, status,
                   odbior_kod, odbior_miasto, dostawa_kod, dostawa_miasto,
                   tresc
              FROM posty
             WHERE czy_zlecenie
             ORDER BY opublikowany_at DESC NULLS LAST, pobrany_at DESC
             LIMIT %s
            """, (ile,))
        wiersze = cur.fetchall()

    if not wiersze:
        return "Brak zleceń w bazie."

    znaczniki = {"nowe": "•", "dzwonie": "📞", "wygrane": "✅",
                 "przegrane": "✖", "smiec": "🗑"}
    teraz = datetime.now(timezone.utc)
    linie = [f"*🕘 Ostatnie {len(wiersze)} zleceń*", ""]
    for (fb_id, grupa, opublikowany, status, o_kod, o_miasto, d_kod, d_miasto,
         tresc_posta) in wiersze:
        pods = geo.podsumowanie(geo.geokoduj(o_kod, o_miasto, tresc=tresc_posta),
                                geo.geokoduj(d_kod, d_miasto, tresc=tresc_posta),
                                tresc_posta)
        trasa = str(o_miasto or o_kod or "?")
        if d_miasto or d_kod:
            trasa += f" → {d_miasto or d_kod}"
        # Bez obu rozpoznanych końców trasy nie ma kilometrów i NIE podstawiamy
        # pod nie dojazdu z bazy — lista, w której „60 km" znaczy raz długość
        # kursu, a raz drogę do odbioru, jest gorsza niż lista przyznająca się
        # do braku. Ta sama reguła co w alercie i w panelu (`services/geo.py`).
        if pods["km_trasy"] is not None:
            km = f"{round(pods['km_trasy'])} km"
        elif pods["km_wg_autora"] is not None:
            km = f"wg autora: {pods['km_wg_autora']} km"
        else:
            km = "trasa nieustalona"
        linie.append(telegram_notify._escape_md(
            f"{znaczniki.get(status, '•')} {trasa} · {km} · "
            f"{powiadomienia.wiek_posta(opublikowany, teraz)} · {grupa or '?'}"))
    return "\n".join(linie)


POMOC = """*Laweta Radar — bot*

/dzis — ile zleceń, ile wziętych, ile km
/ostatnie 10 — ostatnie zlecenia
/stop — pauza powiadomień (fetcher zbiera dalej)
/start — wznowienie powiadomień
/pomoc — ten tekst

Pauza wycisza WYŁĄCZNIE brzęczenie. Zlecenia lecą do bazy i do panelu przez
cały czas — nic nie ginie."""


def obsluz_komende(conn, tekst: str) -> str:
    """Komenda -> odpowiedź. Funkcja czysta poza odczytem z bazy, żeby dało się
    ją sprawdzić testem bez Telegrama."""
    czesci = tekst.strip().split()
    komenda = czesci[0].lower().split("@")[0]     # /dzis@nazwa_bota w grupie

    if komenda == "/dzis":
        return _dzis(conn)
    if komenda == "/ostatnie":
        try:
            ile = int(czesci[1]) if len(czesci) > 1 else DOMYSLNIE_OSTATNICH
        except ValueError:
            ile = DOMYSLNIE_OSTATNICH
        return _ostatnie(conn, ile)
    if komenda == "/stop":
        if pauza_aktywna(conn):
            return "Powiadomienia są już wyciszone. /start żeby wznowić."
        _ustaw_pauze(conn, True, "/stop od operatora")
        return ("🔕 Powiadomienia wyciszone.\n\n"
                "Fetcher zbiera dalej i wszystko trafia do panelu — cichnie "
                "wyłącznie telefon. /start żeby wznowić.")
    if komenda == "/start":
        if not pauza_aktywna(conn):
            return "Powiadomienia są włączone. /pomoc — co jeszcze umiem."
        _ustaw_pauze(conn, False, "/start od operatora")
        return "🔔 Powiadomienia wznowione."
    if komenda in ("/pomoc", "/help"):
        return POMOC
    return POMOC


# ---------------------------------------------------------------------------
# Pętla zdarzeń
# ---------------------------------------------------------------------------
def _czat_operatora() -> str:
    return str(settings.TELEGRAM_CHAT_ID or "").strip()


def _z_naszego_czatu(aktualizacja: dict) -> bool:
    """Filtr, bez którego bot jest publicznym pilotem do cudzych zleceń."""
    wiadomosc = aktualizacja.get("message") or {}
    callback = aktualizacja.get("callback_query") or {}
    czat = ((wiadomosc.get("chat") or {}).get("id")
            or ((callback.get("message") or {}).get("chat") or {}).get("id"))
    return str(czat) == _czat_operatora()


def obsluz_aktualizacje(conn, aktualizacja: dict) -> None:
    if not _z_naszego_czatu(aktualizacja):
        # Bez odpowiedzi — odpowiedź potwierdziłaby obcemu, że bot istnieje
        # i reaguje. Log zostaje, bo to jedyny ślad, gdyby ktoś próbował.
        _log(f"ignoruję update spoza czatu operatora (id={aktualizacja.get('update_id')})")
        return

    if "callback_query" in aktualizacja:
        obsluz_callback(conn, aktualizacja["callback_query"])
        return

    tekst = (aktualizacja.get("message") or {}).get("text") or ""
    if not tekst.startswith("/"):
        return
    telegram_notify.wyslij(obsluz_komende(conn, tekst))


def _polacz():
    """Połączenie do bazy albo None — bot bez bazy nie ma co robić, ale ma
    przeżyć jej chwilową niedostępność bez wyjścia z procesu."""
    return powiadomienia._polacz()


def przebieg(offset: int | None) -> int | None:
    """Jedno wywołanie getUpdates + obsługa tego, co przyszło. Oddaje nowy offset.

    OFFSET POTWIERDZAMY DOPIERO PO OBSŁUŻENIU. Telegram kasuje update dopiero,
    gdy poprosimy o kolejny — więc bot ubity w środku przetwarzania dostanie to
    samo kliknięcie po restarcie. To jest zachowanie POŻĄDANE: wszystkie akcje
    są idempotentne (ustawienie statusu na 'smiec' drugi raz nie zmienia nic,
    a `feedback` ma UNIQUE), a zgubione kliknięcie znaczyłoby, że operator
    oznaczył zlecenie i nic się nie stało.
    """
    payload = {"timeout": TIMEOUT_POLL_S}
    if offset is not None:
        payload["offset"] = offset
    # timeout HTTP musi być DŁUŻSZY niż timeout long pollingu, inaczej każdy
    # spokojny odczyt kończy się wyjątkiem po stronie klienta.
    aktualizacje = telegram_notify.wywolaj("getUpdates", payload,
                                           timeout=TIMEOUT_POLL_S + 10)
    if not aktualizacje:
        return offset

    conn = _polacz()
    if conn is None:
        # NIE potwierdzamy offsetu: kliknięcia poczekają na powrót bazy
        # zamiast zginąć. Telegram trzyma updaty 24 h, co jest z zapasem.
        _log("baza niedostępna — nie potwierdzam updatów, spróbuję ponownie")
        return offset
    try:
        for aktualizacja in aktualizacje:
            try:
                obsluz_aktualizacje(conn, aktualizacja)
            except Exception as e:  # noqa: BLE001 — jeden zły update nie ubija bota
                _log(f"błąd obsługi update {aktualizacja.get('update_id')}: "
                     f"{type(e).__name__}: {str(e)[:200]}")
            offset = aktualizacja["update_id"] + 1
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return offset


def petla() -> int:
    """Nieskończony long polling. Kończy się tylko na Ctrl+C albo na braku tokenu."""
    if not telegram_notify.skonfigurowany():
        return settings.wyjscie_bez_konfiguracji(
            KTO, settings.brakujace("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"))

    _log(f"start; czat operatora={_czat_operatora()}, long polling {TIMEOUT_POLL_S}s")
    offset: int | None = None
    przerwa = PRZERWA_MIN_S
    while True:
        try:
            nowy = przebieg(offset)
        except KeyboardInterrupt:
            _log("Ctrl+C — kończę")
            return 0
        except Exception as e:  # noqa: BLE001 — proces PM2 ma żyć mimo błędów
            _log(f"błąd przebiegu: {type(e).__name__}: {str(e)[:200]} "
                 f"— czekam {przerwa}s")
            time.sleep(przerwa)
            przerwa = min(przerwa * 2, PRZERWA_MAX_S)
            continue
        if nowy != offset:
            przerwa = PRZERWA_MIN_S      # udany odczyt kasuje narastanie przerwy
        offset = nowy


def _main(argv: list[str]) -> int:
    if "--raz" in argv:
        # Jeden przebieg do sprawdzenia konfiguracji bez zostawiania procesu.
        if not telegram_notify.skonfigurowany():
            return settings.wyjscie_bez_konfiguracji(
                KTO, settings.brakujace("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"))
        print(f"offset po przebiegu: {przebieg(None)}")
        return 0
    return petla()


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
