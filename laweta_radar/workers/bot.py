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

from laweta_radar.config import groups as cfg_groups
from laweta_radar.config import settings
from laweta_radar.services import feedback, geo, powiadomienia, telegram_notify
from laweta_radar.workers import apify_credits, apify_proxy
from laweta_radar.workers.apify_keys import load_apify_tokens

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


# ---------------------------------------------------------------------------
# /limity — stan puli kont Apify na żądanie, zamiast logowania na VPS.
#
# TRZY ROZŁĄCZNE STANY KONTA (workers/apify_credits.StanKonta) muszą zostać
# pokazane OSOBNO, nie zlepione w jedno „błąd": konto, które odpowiada na
# /users/me, ale nie na /users/me/limits (typowe dla darmowych kont), jest
# SPRAWNE — tylko nie podaje salda. Pokazanie go jako błędu wygląda jak awaria
# całej puli, choć fetcher tymi samymi kluczami normalnie pobiera posty.
# Dokładnie to pomylenie wywołało to zadanie.
#
# BEZPIECZEŃSTWO: wiadomość idzie na czat, na którym może być więcej niż jedna
# osoba. Jedyne, co wolno pokazać per konto, to `username` z /users/me (albo
# numer porządkowy, gdy go nie znamy) — nigdy token ani jego fragment.
# ---------------------------------------------------------------------------
PROG_KONTO_OSTRZEZENIE = 90    # % zużycia konta -> ⚠ przy wierszu
PROG_PULA_OSTRZEZENIE = 80     # % zużycia CAŁEJ puli -> linia ostrzegawcza na końcu
GODZIN_MIN_NA_PROGNOZE = 12    # mniej historii w oknie 24h -> "za wcześnie"


def _pasek(pct: float, szerokosc: int = 10) -> str:
    """Pasek postępu ▓/░. `pct` spoza 0-100 przycinamy — API bywa niedokładne
    (zaokrąglenia po stronie Apify potrafią dać 100.4%)."""
    pct = max(0.0, min(100.0, pct))
    pelne = round(pct / 100 * szerokosc)
    return "▓" * pelne + "░" * (szerokosc - pelne)


def _etykieta_konta(i: int, stan) -> str:
    """`username` z Apify, gdy go znamy — inaczej numer porządkowy. NIGDY token."""
    return stan.nazwa if stan.nazwa and stan.nazwa != "?" else f"konto #{i}"


def _wiersz_konta(i: int, stan) -> str:
    """Jedna linia w /limity — format zależy od stanu, patrz nagłówek sekcji."""
    etykieta = _etykieta_konta(i, stan)
    if stan.stan == apify_credits.STAN_MARTWY:
        return f"#{i} {etykieta} — BŁĄD 401: klucz nie działa"
    if stan.stan == apify_credits.STAN_BRAK_ODPOWIEDZI:
        return f"#{i} {etykieta} — brak odpowiedzi (timeout/sieć)"
    if stan.stan == apify_credits.STAN_OK_NIEZNANE:
        return f"#{i} {etykieta} — działa, saldo nieznane (darmowy plan?)"

    s = stan.saldo
    if s is None or s.limit_usd is None:
        uzyte = s.uzyte_usd if s else 0.0
        return f"#{i} {etykieta} — użyte ${uzyte:.2f} (limit nieznany)"
    pct = 0.0 if s.limit_usd <= 0 else (s.uzyte_usd / s.limit_usd) * 100
    ostrzezenie = "  ⚠ prawie wyczerpane" if pct >= PROG_KONTO_OSTRZEZENIE else ""
    return (f"#{i} {etykieta}   ${s.uzyte_usd:.2f} / ${s.limit_usd:.2f}   "
            f"{_pasek(pct)} {round(pct)}%{ostrzezenie}")


def _tempo_zuzycia(conn) -> tuple[float | None, int]:
    """(USD/dobę wg tempa z ostatnich 24h, liczba pobranych w tym oknie).

    None zamiast liczby, gdy w oknie jest MNIEJ NIŻ `GODZIN_MIN_NA_PROGNOZE`
    historii — inaczej świeżo zebrane trzy godziny danych ekstrapolowałyby się
    na dobę i dałyby liczbę z sufitu, tylko ładnie sformatowaną.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), min(pobrany_at) FROM posty "
            " WHERE pobrany_at > NOW() - INTERVAL '24 hours'")
        ile, najstarszy = cur.fetchone()
    ile = ile or 0
    if not ile or najstarszy is None:
        return None, ile
    if najstarszy.tzinfo is None:
        najstarszy = najstarszy.replace(tzinfo=timezone.utc)
    rozpietosc_h = (datetime.now(timezone.utc) - najstarszy).total_seconds() / 3600
    if rozpietosc_h < GODZIN_MIN_NA_PROGNOZE:
        return None, ile
    return ile * cfg_groups.CENA_USD_ZA_POST, ile


def _pobrane_dzisiaj(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM posty WHERE pobrany_at > date_trunc('day', NOW())")
        (ile,) = cur.fetchone()
    return ile or 0


def _sekcja_proxy(conn, tokeny: list[str], stany) -> list[str]:
    """Widoczność puli proxy: ile w puli, ile aktywnych, ile w kwarantannie,
    które konto z którego wychodzi — host:port, NIGDY hasło (`proxy_label`).

    Stan czytamy z BAZY (`zasoby_apify_proxy`), nie odpalamy tu ŻADNEJ świeżej
    weryfikacji sieciowej — /limity ma być szybkie i tanie (patrz cache w
    `apify_credits.pula_stanu`), a cztery testy z docs/APIFY-PROXY.md robi
    osobno pętla samoleczenia w fetcherze, w reakcji na realną awarię.
    """
    # tokens=tokeny włącza wyrównanie PO hashu (patrz apify_proxy._wyrownaj_przypisania)
    # — bez tego /limity pokazywałoby surowy rendezvous hashing, czyli czasem inny
    # adres niż ten, którym fetcher REALNIE poszedł (fetcher liczy cfg tak samo, z
    # pełną listą tokenów tego przebiegu).
    cfg = apify_proxy.load_proxy_config(tokens=tokeny)
    if not cfg.enabled:
        return ["", "🌐 *Proxy*: nieskonfigurowane — konta wychodzą z IP VPS-a"]

    linie = ["", "🌐 *Proxy*"]
    w_kwarantannie: set[str] = set()
    if cfg.pool:
        try:
            stan_proxy = apify_proxy.wczytaj_stan_proxy(conn, cfg.pool)
        except Exception:  # noqa: BLE001 — sekcja proxy nie może wywalić /limity
            stan_proxy = {}
        w_kwarantannie = {u for u, s in stan_proxy.items() if s["status"] == "kwarantanna"}
        linie.append(f"Pula: {len(cfg.pool)} adresów · aktywnych "
                     f"{len(cfg.pool) - len(w_kwarantannie)} · "
                     f"w kwarantannie {len(w_kwarantannie)}")
    else:
        linie.append("Pula: pojedyncze przypisania / brama z sesją (bez APIFY_PROXY_URLS)")

    esc = telegram_notify._escape_md
    for i, (token, stan) in enumerate(zip(tokeny, stany), 1):
        etykieta = _etykieta_konta(i, stan)
        try:
            proxy = apify_proxy.proxy_for_token(token, cfg)
        except apify_proxy.ApifyProxyError:
            proxy = None
        if proxy is None:
            linie.append(esc(f"#{i} {etykieta} → IP VPS-a (bez proxy)"))
            continue
        znacznik = "  ⚠ w kwarantannie" if proxy in w_kwarantannie else ""
        linie.append(esc(f"#{i} {etykieta} → {apify_proxy.proxy_label(proxy)}{znacznik}"))
    return linie


def _limity(conn) -> str:
    """Stan puli kont Apify — jedna wiadomość, czytelna na telefonie."""
    tokeny = load_apify_tokens()
    if not tokeny:
        return ("💳 *PULA APIFY*\n\n"
                "Brak skonfigurowanych kluczy (APIFY\\_API\\_TOKEN\\* — wspólna "
                "pula z sales\\-core\\-engine).")

    stany = apify_credits.pula_stanu(tokeny)
    esc = telegram_notify._escape_md
    kreska = "━" * 20

    linie = [f"💳 *PULA APIFY — {len(stany)} kont*", kreska]
    for i, s in enumerate(stany, 1):
        linie.append(esc(_wiersz_konta(i, s)))
    linie.append(kreska)

    znane = [s.saldo for s in stany
             if s.stan == apify_credits.STAN_OK_ZNANE and s.saldo is not None
             and s.saldo.limit_usd is not None]
    razem_zostalo = None
    pct_puli = None
    if znane:
        razem_uzyte = sum(sd.uzyte_usd for sd in znane)
        razem_limit = sum(sd.limit_usd for sd in znane)
        razem_zostalo = razem_limit - razem_uzyte
        pct_puli = 0.0 if razem_limit <= 0 else (razem_uzyte / razem_limit) * 100
        linie.append(esc(f"Razem: ${razem_uzyte:.2f} z ${razem_limit:.2f} · "
                         f"zostało ${razem_zostalo:.2f}"))

    tempo, _ile_24h = _tempo_zuzycia(conn)
    if tempo is None:
        linie.append("Tempo zużycia: za wcześnie na prognozę (mniej niż "
                     f"{GODZIN_MIN_NA_PROGNOZE}h historii)")
    elif tempo <= 0:
        linie.append("Tempo zużycia: brak pobrań w ostatnich 24h")
    elif razem_zostalo is not None:
        dni = razem_zostalo / tempo
        linie.append(esc(f"Przy obecnym tempie (~${tempo:.2f}/dobę) starczy na ~{dni:.0f} dni"))
    else:
        linie.append(esc(f"Tempo zużycia: ~${tempo:.2f}/dobę (saldo puli nieznane — "
                         "nie policzę, na ile starczy)"))

    if pct_puli is not None and pct_puli >= PROG_PULA_OSTRZEZENIE:
        linie.append("⚠ pula na wyczerpaniu, dolóż konta albo zejdź z częstotliwością")

    martwe = sum(1 for s in stany if s.stan == apify_credits.STAN_MARTWY)
    if martwe:
        zywe = len(stany) - martwe
        linie.append(f"🔑 Żywe klucze: {zywe} z {len(stany)} — {martwe} "
                     "martwych (401), sprawdź konta w Apify")

    dzisiaj = _pobrane_dzisiaj(conn)
    linie.append(f"Pobrane dziś: {dzisiaj} postów · budżet {settings.POSTY_NA_DOBE}/dobę")

    linie.extend(_sekcja_proxy(conn, tokeny, stany))

    return "\n".join(linie)


POMOC = """*Laweta Radar — bot*

/dzis — ile zleceń, ile wziętych, ile km
/ostatnie 10 — ostatnie zlecenia
/limity — stan puli kont Apify (bez logowania na VPS)
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
    if komenda in ("/limity", "/limityapi"):
        return _limity(conn)
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


# Komendy podpowiadane w Telegramie (menu przy „/"). Jedna lista, jedno miejsce
# — POMOC i to menu mają identyczny zestaw z tego samego powodu, dla którego
# AKCJE jest daną, a nie serią `elif`: dopisanie komendy ma być jedną zmianą,
# nie dwiema, z których druga zostaje zapomniana. `/limityapi` jest aliasem
# `/limity` i nie wchodzi do menu — Telegram i tak podpowiada tylko jedną nazwę
# na akcję, a druga nazwa istnieje wyłącznie dla wygody wpisywania z pamięci.
KOMENDY_BOTFATHER = [
    {"command": "dzis", "description": "ile zleceń dzisiaj, ile wziętych, ile km"},
    {"command": "ostatnie", "description": "ostatnie zlecenia"},
    {"command": "limity", "description": "stan puli kont Apify"},
    {"command": "stop", "description": "pauza powiadomień"},
    {"command": "start", "description": "wznowienie powiadomień"},
    {"command": "pomoc", "description": "lista komend"},
]


def _zarejestruj_komendy() -> None:
    """`setMyCommands` przy starcie — podpowiedź komend w Telegramie.

    BEST-EFFORT i NIEBLOKUJĄCE: to jest kosmetyka menu, nie funkcja bota. Bot
    ma długi polling do obsłużenia niezależnie od tego, czy Telegram akurat
    przyjął tę jedną konfiguracyjną wiadomość — nieudane wywołanie loguje się
    i pętla jedzie dalej, tak samo jak przy każdym innym wywołaniu `wywolaj`.
    """
    try:
        wynik = telegram_notify.wywolaj("setMyCommands", {"commands": KOMENDY_BOTFATHER})
    except Exception as e:  # noqa: BLE001 — kosmetyka menu nie może zablokować startu bota
        _log(f"setMyCommands: {type(e).__name__}: {str(e)[:200]}")
        return
    if wynik is None:
        _log("setMyCommands: nie udało się zarejestrować komend (patrz log wyżej)")
    else:
        _log(f"setMyCommands: zarejestrowano {len(KOMENDY_BOTFATHER)} komend")


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
    _zarejestruj_komendy()
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
