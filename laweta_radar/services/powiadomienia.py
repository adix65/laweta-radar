"""Powiadomienie o zleceniu na telefon operatora — moment, w którym system zarabia.

Cała reszta repo jest przygotowaniem do dwóch sekund, w których operator patrzy
na ekran i decyduje, czy dzwonić. Dlatego układ wiadomości jest tu opisany
dokładniej niż logika: logikę da się poprawić, a źle ułożony alert po prostu
przestaje być czytany.

KANAŁ: TELEGRAM. Bez alternatyw na start i to jest decyzja, nie brak czasu.
Działa na każdym telefonie, dźwięk przechodzi przez tryb cichy przy ustawionym
priorytecie, nie wymaga instalacji PWA ani zgód przeglądarki, jest darmowy
i dostarcza w sekundę. Web push (panel/, prompt 7) dokładamy jako UZUPEŁNIENIE
— drugi kanał obok, nigdy zamiennik: push na iOS działa wyłącznie po dodaniu
strony do ekranu głównego, a operator, który tego nie zrobił, po prostu nie
dostaje zleceń i nie ma jak się o tym dowiedzieć.

--------------------------------------------------------------------------
KOLEJNOŚĆ INFORMACJI ODPOWIADA KOLEJNOŚCI DECYZJI. Czytane kciukiem, w słońcu,
często zza kierownicy:

    🚨 PILNE · 42 km · ~340 zł        <- trzy liczby, po których się decyduje

    Krosno (38-400) → Rzeszów          <- gdzie
    VW Golf IV · nie odpala · toczy się <- czym i w jakim stanie

    "potrzebuje lawety z Krosna do        <- ORYGINAŁ, bo model się myli
    Rzeszowa, golf stanal i nie odpala"

    📞 555 111 222                     <- czym zadzwonić
    👥 Pomoc drogowa Podkarpacie · 4 min temu   <- skąd i czy jeszcze aktualne

    [ Trasa w mapach ] [ Otwórz post ] [ Śmieć ]

Cztery zasady redakcyjne, każda kosztowała konkretny błąd:

  - PIERWSZA LINIA TO TRZY LICZBY I NIC WIĘCEJ. Pilność, dystans, szacunek.
    Wszystko inne w tej linii konkuruje o dwie sekundy, których nie ma.
  - CYTAT JEST OBOWIĄZKOWY. Model może się pomylić — wyciągnąć „Golf" ze zdania
    o innym aucie, przekręcić miasto, zgubić „nie" przy „nie odpala". Człowiek
    musi mieć dostęp do oryginału BEZ klikania, bo klikanie to następny ekran,
    a decyzja zapada na tym.
  - WIEK POSTA ZAWSZE. „4 min temu" znaczy, że warto dzwonić. „2 h temu" znaczy,
    że pewnie już ktoś pojechał. Bez tej liczby alert nie mówi, czy jest po co
    sięgać po telefon.
  - NIEPEWNA LOKALIZACJA JEST OZNACZONA. Przy `zrodlo=='miasto_niepewne'` idzie
    znak zapytania i jedno słowo ostrzeżenia. Cicho podane złe kilometry wysyłają
    lawetę nie tam, a dowiaduje się o tym po godzinie jazdy.

--------------------------------------------------------------------------
PROGI WYSYŁKI — TU NAJŁATWIEJ ZŁAMAĆ ZASADĘ NACZELNĄ REPO.

ŻADEN próg w tym module nie usuwa zlecenia z bazy ani z panelu. Progi sterują
WYŁĄCZNIE tym, czy o danym zleceniu brzęczy telefon. Wszystko, co system złapał,
jest widoczne w aplikacji zawsze i bez wyjątku. „Nie wysyłaj" i „ukryj" wyglądają
w kodzie podobnie i dlatego jest o tym osobny akapit.

  MIN_PEWNOSC (40)       zlecenie do panelu BEZ powiadomienia, NIE do kosza
  CISZA_NOCNA (22-6)     w nocy nie brzęczymy; rano jedno zbiorcze podsumowanie

NIE MA progu na kilometry ani na kierunek. Trasa Kolonia-Kraków to 1100 km i to
jest normalny dzień pracy tego operatora, a nie powód do ukrycia zlecenia.

--------------------------------------------------------------------------
ANTYSPAM — WYMÓG, NIE OZDOBA. System wysyłający 40 powiadomień dziennie zostanie
wyciszony po tygodniu i przestanie istnieć. To jest awaria całkowita, tylko
rozłożona na dni, więc kosztuje więcej niż awaria, którą widać od razu.

  1. DEDUP PO fb_id — jeden post = jedno powiadomienie, na zawsze. Broni tego
     UNIQUE INDEX w bazie, nie tylko `if` w kodzie: dwa przebiegi fetchera
     potrafią się nałożyć, bo cron nie czeka na poprzedni.
  2. DEDUP TREŚCIOWY — ten sam post crossowany do pięciu grup ma PIĘĆ różnych
     fb_id, bo hash liczymy z treści, a treść bywa minimalnie inna. Przed wysyłką
     pytamy, czy w ostatnich 6 h nie szło już powiadomienie o zleceniu z tym samym
     numerem telefonu ALBO z tą samą parą miast i tym samym opisem pojazdu.
     Jeśli tak — dopisujemy grupę do ISTNIEJĄCEGO wpisu zamiast wysyłać drugą
     wiadomość. Operator i tak dowie się, że zlecenie krąży po czterech grupach,
     tylko z jednego alertu.
  3. TWARDY LIMIT 15/h — po przekroczeniu leci JEDNA zbiorcza „jeszcze N zleceń
     w panelu" i cisza. Przekroczenie tego limitu prawie zawsze znaczy, że coś
     się zepsuło w bramce albo w klasyfikatorze, więc zbiorcza wiadomość jest
     przy okazji alarmem.

--------------------------------------------------------------------------
TELEGRAM NIGDY NIE WYWALA RUNU FETCHERA. Każde wywołanie w try/except, zwrot
bool, log przy błędzie. `False` znaczy „nie wysłano" i NIE jest zaproszeniem do
ponowienia — powód stoi w logu, a dedup i tak zamieni ponowienie w nic.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from laweta_radar.config import settings
from laweta_radar.services import geo, telegram_notify

KTO = "powiadomienia"

# Ile znaków oryginału pokazujemy. Dwieście to mniej więcej tyle, ile mieści się
# na ekranie telefonu bez przewijania — a cytat, do którego trzeba przewinąć,
# przestaje pełnić swoją funkcję (sprawdzenie modelu jednym rzutem oka).
MAX_CYTAT = 200

# Ikona i etykieta pilności. Trzy poziomy, bo czwartego operator nie odróżni
# w dwie sekundy. Nieznana wartość degraduje do „ZLECENIE" — brak etykiety jest
# lepszy niż etykieta zmyślona.
# Klucze to wartości z `workers/classifier.py` — nie wymyślamy własnych, bo
# etykieta, której model nigdy nie zwraca, jest martwym kodem udającym funkcję.
PILNOSC = {
    "teraz": ("🚨", "TERAZ"),
    "dzis": ("⏱", "DZIŚ"),
    "jutro": ("📅", "JUTRO"),
    "elastycznie": ("🗓", "ELASTYCZNIE"),
}

# Znacznik języka pokazujemy TYLKO dla obcych. Wynika to z docs/WIELOJEZYCZNOSC.md:
# alert niesie znacznik, bo od niego zależy, w jakim języku operator ma oddzwonić
# — a wszystkie pozostałe pola są już po polsku, więc sam post tego nie zdradzi.
# „pl" na każdym polskim alercie byłoby szumem w linii, która ma trzy elementy.
FLAGI = {"de": "🇩🇪 de", "cs": "🇨🇿 cs", "sk": "🇸🇰 sk"}


def _log(msg: str) -> None:
    print(f"[{KTO}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Formatowanie pojedynczych pól — funkcje czyste, sprawdzane testem offline
# ---------------------------------------------------------------------------
def normalizuj_telefon(surowy) -> str:
    """Numer do postaci porównywalnej: same cyfry, bez kierunkowego kraju.

    To jest KLUCZ DEDUPU, nie ozdoba. Ten sam człowiek wkleja swój numer na pięć
    grup raz jako „+48 555 111 222", raz jako „555111222", raz jako „555-111-222".
    Bez sprowadzenia do wspólnej postaci dedup po numerze nie łapie niczego,
    a operator dostaje pięć alertów o jednym kursie.

    Kierunkowe 48/49/420/421 (PL/DE/CZ/SK — dokładnie rynki z config/groups.py)
    zdejmujemy, bo ten sam numer bywa pisany z nim i bez niego.
    """
    cyfry = re.sub(r"\D", "", str(surowy or ""))
    if not cyfry:
        return ""
    for kierunkowy, dlugosc_krajowa in (("420", 9), ("421", 9), ("48", 9), ("49", 10)):
        if cyfry.startswith(kierunkowy) and len(cyfry) > dlugosc_krajowa:
            return cyfry[len(kierunkowy):]
    return cyfry


def telefon_czytelnie(surowy) -> str:
    """Numer w formacie z odstępami — po to, żeby dało się go przeczytać na głos.

    Telegram sam linkuje numery telefonów w zwykłym tekście, więc NIE opakowujemy
    go w link markdownowy: `tel:` w linkach Markdown nie działa we wszystkich
    klientach Telegrama i zamiast klikalnego numeru zostaje surowy nawias.
    """
    cyfry = normalizuj_telefon(surowy)
    if not cyfry:
        return ""
    if len(cyfry) == 9:                       # PL/CZ/SK — klasyczne 3-3-3
        return f"{cyfry[:3]} {cyfry[3:6]} {cyfry[6:]}"
    return " ".join(cyfry[i:i + 3] for i in range(0, len(cyfry), 3))


def wiek_posta(opublikowany, teraz: datetime | None = None) -> str:
    """„4 min temu" / „2 h temu" / „wczoraj". ZAWSZE coś zwraca.

    Brak daty publikacji to normalny stan (Apify nie zawsze ją oddaje), ale
    pominięcie tej linii byłoby błędem: operator patrzący na alert bez wieku
    zakłada, że jest świeży. Dlatego zamiast pustki idzie „wiek nieznany" —
    informacja gorsza, ale prawdziwa.
    """
    if not isinstance(opublikowany, datetime):
        return "wiek nieznany"
    teraz = teraz or datetime.now(timezone.utc)
    if opublikowany.tzinfo is None:
        opublikowany = opublikowany.replace(tzinfo=timezone.utc)
    minuty = int((teraz - opublikowany).total_seconds() // 60)
    if minuty < 0:
        return "przed chwilą"        # zegar FB bywa przed naszym o kilkadziesiąt sekund
    if minuty < 60:
        return f"{minuty} min temu"
    godziny = minuty // 60
    if godziny < 24:
        return f"{godziny} h temu"
    dni = godziny // 24
    return "wczoraj" if dni == 1 else f"{dni} dni temu"


def _cytat(tresc) -> str:
    """Oryginał posta przycięty do ~200 znaków, w jednym akapicie.

    Łamania linii z Facebooka zamieniamy na spacje: post bywa napisany po jednym
    słowie w wierszu i wtedy cytat rozjeżdża alert na pół ekranu, zabierając
    miejsce linii z telefonem.
    """
    tekst = re.sub(r"\s+", " ", str(tresc or "")).strip()
    if not tekst:
        return ""
    if len(tekst) > MAX_CYTAT:
        # Tniemy na granicy słowa — urwane w połowie wyrazu wygląda na błąd
        # systemu, a nie na świadome skrócenie.
        tekst = tekst[:MAX_CYTAT].rsplit(" ", 1)[0] + "…"
    return tekst


def punkty(zlecenie: dict) -> tuple:
    """Odbiór i dostawa jako `geo.Punkt` (albo None). Jedno miejsce, w którym
    ten moduł tłumaczy pola klasyfikatora na geografię — reszta pliku dostaje
    już gotowe punkty."""
    odbior = geo.geokoduj(zlecenie.get("odbior_kod"), zlecenie.get("odbior_miasto"))
    dostawa = geo.geokoduj(zlecenie.get("dostawa_kod"), zlecenie.get("dostawa_miasto"))
    return odbior, dostawa


def _linia_pilnosci(zlecenie: dict, pods: dict) -> str:
    """Trzy liczby, po których zapada decyzja. Nic więcej w tej linii.

    DYSTANS TO DŁUGOŚĆ KURSU (odbiór->dostawa), nie odległość od bazy — przy
    transporcie międzynarodowym „ile km od bazy" nie znaczy nic, bo i tak
    trzeba przejechać całą trasę z autem na lawecie. Dojazd z bazy idzie
    o linijkę niżej, przy trasie, jako liczba pomocnicza. Gdy dostawy nie
    znamy, w pierwszej linii ląduje dojazd — jest wtedy jedyną liczbą, jaką
    mamy, a pusta pierwsza linia byłaby gorsza niż niedokładna.
    """
    ikona, etykieta = PILNOSC.get(str(zlecenie.get("pilnosc") or "").lower(),
                                  ("🔧", "ZLECENIE"))
    czesci = [f"{ikona} {etykieta}"]
    km = pods["km_trasy"] if pods["km_trasy"] is not None else pods["km_od_bazy"]
    # „? km" zamiast pominięcia: brak dystansu to informacja, i to ważna —
    # znaczy, że nie rozpoznaliśmy miejsca i operator ma przeczytać cytat.
    czesci.append(f"{round(km)} km" if km is not None else "? km")
    if pods["szacunek_pln"]:
        czesci.append(f"~{round(pods['szacunek_pln'])} zł")
    return " · ".join(czesci)


def _miejsce(surowe, kod, punkt) -> str:
    """Jedno miejsce na ekran: nazwa, kod pocztowy i ostrzeżenie o niepewności.

    Znak zapytania PRZY nazwie i JEDNO słowo obok — operator ma wiedzieć, że
    kilometry z pierwszej linii są orientacyjne, ZANIM je zaakceptuje. Kod
    i ostrzeżenie dzielą jeden nawias: dwa nawiasy pod rząd („Krosno?
    (niepewne) (38-400)") czyta się dłużej niż całą resztę linii.

    Nazwa idzie z posta (`odbior_miasto`), nie z bazy geo. Baza dokleja do
    nazwy region i kraj („Krosno, podkarpackie (PL)"), co jest w sam raz do
    diagnostyki i o dwa słowa za dużo do alertu czytanego w dwie sekundy.
    """
    nazwa = str(surowe or "").strip() or "?"
    kod = str(kod or "").strip()
    if punkt is None:
        return f"⚠️ {nazwa} (nierozpoznane)"
    ostrzezenie = "niepewne" if punkt.niepewny else ""
    if ostrzezenie:
        nazwa = f"⚠️ {nazwa}?"
    w_nawiasie = ", ".join(c for c in (kod, ostrzezenie) if c)
    return f"{nazwa} ({w_nawiasie})" if w_nawiasie else nazwa


def _linia_trasy(zlecenie: dict, odbior, dostawa, pods: dict) -> str:
    """Skąd → dokąd, plus dojazd z bazy jako liczba pomocnicza.

    Dojazd doklejamy TU, a nie do pierwszej linii, bo pierwsza linia ma trzy
    liczby i ani jednej więcej — a jednocześnie „ile mam do nich jechać" jest
    pytaniem, które pada zaraz po „ile to warte".
    """
    skad = _miejsce(zlecenie.get("odbior_miasto") or zlecenie.get("odbior_raw"),
                    zlecenie.get("odbior_kod"), odbior)
    dokad = _miejsce(zlecenie.get("dostawa_miasto") or zlecenie.get("dostawa_raw"),
                     zlecenie.get("dostawa_kod"), dostawa) if (
                         zlecenie.get("dostawa_miasto") or zlecenie.get("dostawa_raw")) else ""

    linia = f"{skad} → {dokad}" if dokad else skad
    # Tylko gdy w pierwszej linii stoi trasa — inaczej powtarzalibyśmy tę samą
    # liczbę dwa razy pod rząd. Zero pomijamy: „0 km od bazy" wygląda jak błąd
    # zaokrąglenia, a znaczy „w mieście bazy" — co widać już po nazwie obok.
    if pods["km_trasy"] is not None and round(pods["km_od_bazy"] or 0) >= 1:
        linia += f" · {round(pods['km_od_bazy'])} km od bazy"
    return linia


def _linia_pojazdu(zlecenie: dict) -> str:
    """Pojazd, stan, czy się toczy — jednym wierszem, w tej kolejności.

    Pusty wiersz pomijamy zamiast pokazywać myślniki: alert ma być krótki,
    a „· · ·" wygląda jak błąd renderowania.
    """
    czesci = [str(zlecenie.get("pojazd_opis") or "").strip(),
              str(zlecenie.get("stan_uwagi") or "").strip()]
    # `stan_toczy_sie` jest TRÓJSTANOWE i każdy stan znaczy co innego dla
    # sprzętu, który trzeba wziąć: True = wjedzie sam, False = potrzebna wyciągarka,
    # None = model nie wie i trzeba spytać przez telefon. Pokazujemy tylko dwa
    # pierwsze — „nie wiadomo" operator i tak wyczyta z braku informacji.
    toczy = zlecenie.get("stan_toczy_sie")
    if toczy is True:
        czesci.append("toczy się")
    elif toczy is False:
        czesci.append("NIE toczy się")
    return " · ".join(c for c in czesci if c)


def zbuduj_tresc(zlecenie: dict, pods: dict | None = None,
                 teraz: datetime | None = None) -> str:
    """Cała wiadomość jako tekst. Funkcja CZYSTA — bez bazy, bez sieci.

    Wydzielona po to, żeby układ dało się obejrzeć bez wysyłania czegokolwiek
    (`python -m laweta_radar.services.powiadomienia --podglad`) i sprawdzić
    testem. Alert jest produktem tego systemu; produkt, którego nie da się
    obejrzeć bez produkcji, poprawia się na ślepo.
    """
    odbior, dostawa = punkty(zlecenie)
    pods = pods or geo.podsumowanie(odbior, dostawa)
    esc = telegram_notify._escape_md

    linie = [f"*{esc(_linia_pilnosci(zlecenie, pods))}*", ""]
    linie.append(esc(_linia_trasy(zlecenie, odbior, dostawa, pods)))
    pojazd = _linia_pojazdu(zlecenie)
    if pojazd:
        linie.append(esc(pojazd))

    cytat = _cytat(zlecenie.get("tresc"))
    if cytat:
        linie += ["", f'"{esc(cytat)}"']

    linie.append("")
    telefon = telefon_czytelnie(zlecenie.get("kontakt_wartosc"))
    if telefon:
        linie.append(f"📞 {telefon}")
    else:
        # Brak numeru zmienia to, CO operator zrobi (pisze wiadomość zamiast
        # dzwonić), więc jest informacją, a nie pustym miejscem do pominięcia.
        linie.append("📞 brak numeru — kontakt przez post")

    stopka = [str(zlecenie.get("grupa_nazwa") or "grupa nieznana"),
              wiek_posta(zlecenie.get("opublikowany_at"), teraz)]
    flaga = FLAGI.get(str(zlecenie.get("jezyk") or "").lower())
    if flaga:
        stopka.append(flaga)
    linie.append(f"👥 {esc(' · '.join(stopka))}")

    return "\n".join(linie)


def zbuduj_przyciski(zlecenie: dict, pods: dict | None = None) -> list[list[dict]]:
    """Inline keyboard. Bez „Otwórz post" cała wiadomość jest bezużyteczna.

    Telegram odrzuca CAŁĄ klawiaturę, gdy którykolwiek `url` jest pusty albo
    nie jest adresem — dlatego przycisk bez adresu jest POMIJANY, a nie wysyłany
    z pustką. Brak `post_url` logujemy jako BŁĄD (nie ostrzeżenie): operator nie
    odpisuje z systemu, tylko wchodzi na Facebooka i pisze z własnego konta, więc
    alert bez linku do posta jest alertem, z którym nie da się nic zrobić.
    """
    if pods is None:
        odbior, dostawa = punkty(zlecenie)
        pods = geo.podsumowanie(odbior, dostawa)
    fb_id = str(zlecenie.get("fb_id") or "")
    post_url = str(zlecenie.get("post_url") or "").strip()

    gorny: list[dict] = []
    # `link_trasa` bywa PUSTY, gdy nie znamy żadnego punktu — i wtedy przycisku
    # nie ma. Telegram odrzuca CAŁĄ klawiaturę, gdy którykolwiek `url` jest
    # pusty, więc alert bez tego sprawdzenia dotarłby bez ŻADNYCH przycisków.
    if pods["link_trasa"]:
        gorny.append({"text": "🗺 Trasa w mapach", "url": pods["link_trasa"]})
    if post_url.startswith("http"):
        gorny.append({"text": "📄 Otwórz post", "url": post_url})
    else:
        _log(f"BŁĄD: brak post_url dla {fb_id or '?'} — alert bez linku do posta, "
             "operator nie ma jak odpisać")
    # callback_data ma limit 64 BAJTÓW po stronie Telegrama. fb_id to sha256[:32]
    # albo id z Apify, więc mieści się z zapasem — ale gdyby kiedyś przestało,
    # Telegram odrzuci całą klawiaturę, nie sam przycisk.
    gorny.append({"text": "🗑 Śmieć", "callback_data": f"smiec:{fb_id}"[:64]})

    # „Biorę" w osobnym wierszu: to jedyny przycisk, który zmienia stan zlecenia
    # na korzyść operatora, i nie ma powodu, żeby dzielił wiersz z „Śmieć".
    dolny = [{"text": "✅ Biorę", "callback_data": f"biore:{fb_id}"[:64]}]
    if settings.PANEL_URL and fb_id:
        dolny.append({"text": "📱 Panel",
                      "url": f"{settings.PANEL_URL}/zlecenie/{fb_id}"})
    return [gorny, dolny]


# ---------------------------------------------------------------------------
# Decyzja o wysyłce — funkcja CZYSTA, oddzielona od bazy
# ---------------------------------------------------------------------------
def cisza_nocna(teraz: datetime | None = None) -> bool:
    """Czy teraz jest cisza nocna. Okno [OD, DO) przez północ też działa.

    Godzina LOKALNA maszyny, nie UTC — cisza nocna dotyczy snu operatora,
    a nie strefy, w której akurat stoi serwer.
    """
    godzina = (teraz or datetime.now()).astimezone().hour
    od, do = settings.CISZA_NOCNA_OD, settings.CISZA_NOCNA_DO
    if od == do:
        return False
    if od > do:                    # okno przez północ, np. 22-6
        return godzina >= od or godzina < do
    return od <= godzina < do


# ---------------------------------------------------------------------------
# PAUZA — `/stop` i `/start` z bota. Stan w BAZIE, nie w pamięci procesu.
#
# Bot chodzi w PM2, a powiadomienia wysyła FETCHER odpalany z crona — dwa różne
# procesy, z których żaden nie widzi pamięci drugiego. Stan trzymamy więc jako
# zdarzenia w tej samej tabeli `powiadomienia` (kanał 'pauza' / 'wznowienie');
# obowiązuje ostatnie. Zysk uboczny, który okazuje się główny: „od kiedy jest
# cicho" jest pierwszym pytaniem przy zgłoszeniu „nic nie przychodzi", a z pliku
# ze stanem odpowiedzi na nie nie ma.
#
# Pauza wycisza WYŁĄCZNIE brzęczenie. Fetcher zbiera, klasyfikator ocenia, panel
# pokazuje — zasada naczelna repo obowiązuje także wtedy, gdy operator sam
# poprosił o ciszę.
# ---------------------------------------------------------------------------
def pauza_aktywna(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kanal FROM powiadomienia "
            " WHERE kanal IN ('pauza', 'wznowienie') "
            " ORDER BY wyslano_at DESC, id DESC LIMIT 1")
        wiersz = cur.fetchone()
    return bool(wiersz) and wiersz[0] == "pauza"


def ustaw_pauze(conn, wlaczona: bool, powod: str = "") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO powiadomienia (fb_id, kanal, tresc) VALUES (NULL, %s, %s)",
            ("pauza" if wlaczona else "wznowienie", powod))
    conn.commit()


@dataclass(frozen=True)
class Decyzja:
    """Czy brzęczeć — i dlaczego nie, gdy nie.

    `powod` trafia do logu i jest jedyną odpowiedzią na pytanie „czemu nie
    dostałem alertu o tym zleceniu". Bez niego cisza systemu jest nie do
    odróżnienia od awarii, a to jest najgorszy możliwy stan tego produktu.
    """

    wysylac: bool
    kod: str      # '' | 'pewnosc' | 'cisza_nocna' | 'duplikat' | 'crosspost' | 'limit'
    powod: str


def ocen(zlecenie: dict, *, juz_wyslane: bool, crosspost_id: int | None,
         w_ostatniej_godzinie: int, pauza: bool = False,
         teraz: datetime | None = None) -> Decyzja:
    """Cała logika progów i antyspamu, BEZ dotykania bazy.

    Fakty z bazy (`juz_wyslane`, `crosspost_id`, `w_ostatniej_godzinie`) wchodzą
    jako argumenty właśnie po to, żeby dało się sprawdzić testem każdą ścieżkę —
    łącznie z tą, której nigdy nie widać w produkcji, dopóki coś się nie zepsuje
    (przekroczony limit godzinowy).

    KOLEJNOŚĆ MA ZNACZENIE: dedup przed progami. Post już wysłany nie ma być
    ponownie oceniany progiem pewności, bo prompt klasyfikatora mógł się w
    międzyczasie zmienić i ta sama treść dostałaby drugi alert.
    """
    if juz_wyslane:
        return Decyzja(False, "duplikat", "już wysłane — jeden post, jedno powiadomienie")
    if crosspost_id is not None:
        return Decyzja(False, "crosspost",
                       f"to samo zlecenie co powiadomienie #{crosspost_id} "
                       f"(okno {settings.DEDUP_OKNO_H} h) — dopisuję grupę")

    if pauza:
        # Po dedupie, przed progami: operator poprosił o ciszę, a nie o to, żeby
        # system zapomniał, co widział. Wiersza NIE zapisujemy — zlecenie z okresu
        # pauzy trafi do podsumowania po `/start`, tak samo jak nocne.
        return Decyzja(False, "pauza", "powiadomienia wyciszone przez /stop "
                                       "— zlecenia lecą do panelu bez brzęczenia")

    pewnosc = zlecenie.get("pewnosc")
    if pewnosc is not None and int(pewnosc) < settings.MIN_PEWNOSC:
        return Decyzja(False, "pewnosc",
                       f"pewność {pewnosc} < {settings.MIN_PEWNOSC} — "
                       "zlecenie JEST w panelu, tylko bez brzęczenia")
    if cisza_nocna(teraz):
        return Decyzja(False, "cisza_nocna",
                       f"cisza nocna {settings.CISZA_NOCNA_OD}-{settings.CISZA_NOCNA_DO} "
                       "— pójdzie w podsumowaniu rannym")
    if w_ostatniej_godzinie >= settings.MAX_POWIADOMIEN_H:
        return Decyzja(False, "limit",
                       f"limit {settings.MAX_POWIADOMIEN_H}/h przekroczony "
                       f"({w_ostatniej_godzinie}) — prawdopodobnie coś zepsute "
                       "w bramce albo klasyfikatorze")
    return Decyzja(True, "", "")


def klucz_tresci(zlecenie: dict) -> str:
    """Odcisk zlecenia niezależny od tego, na której grupie je wklejono.

    Para miast + opis pojazdu. Świadomie BEZ treści posta: crosspost różni się
    właśnie treścią (ktoś dopisuje „PILNE!!!" w jednej grupie), więc hash z treści
    dałby pięć różnych kluczy — czyli dokładnie ten problem, który ten klucz ma
    rozwiązywać.

    Pusty string, gdy nie ma z czego liczyć — wtedy dedup treściowy po prostu
    nie działa dla tego zlecenia, i to jest lepsze niż klucz zbudowany z pustek,
    który sklei ze sobą wszystkie nierozpoznane posty.
    """
    czesci = [geo.normalizuj_nazwe(str(zlecenie.get(k) or ""))
              for k in ("odbior_miasto", "dostawa_miasto", "pojazd_opis")]
    if not any(czesci):
        return ""
    return hashlib.sha1("|".join(czesci).encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Baza — każde wywołanie osobno opakowane, żaden błąd nie leci wyżej
# ---------------------------------------------------------------------------
def _polacz():
    """Połączenie albo None. None znaczy „nie wysyłamy" — patrz `powiadom_o_zleceniu`."""
    if not settings.DATABASE_URL:
        _log("brak DATABASE_URL — bez bazy nie ma dedupu, więc nie wysyłam")
        return None
    try:
        import psycopg2  # noqa: PLC0415 — leniwie, jak wszędzie w tym repo
    except ImportError:
        _log("brak psycopg2 — pip install -r laweta_radar/requirements.txt")
        return None
    try:
        return psycopg2.connect(settings.DATABASE_URL, connect_timeout=5)
    except Exception as e:  # noqa: BLE001 — alert nie może wywalić przebiegu
        _log(f"baza niedostępna: {type(e).__name__}: {str(e)[:200]}")
        return None


def _stan_dedupu(conn, fb_id: str, telefon: str, klucz: str) -> tuple:
    """Trzy fakty z bazy jednym przejściem: czy było, czy to crosspost, ile w godzinie.

    Jedno wywołanie zamiast trzech, bo to jest ścieżka KAŻDEGO alertu i chodzi
    kilkaset razy dziennie na tej samej maszynie co fetcher i API.
    """
    okno = settings.DEDUP_OKNO_H
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM powiadomienia WHERE fb_id = %s", (fb_id,))
        wiersz = cur.fetchone()
        juz = wiersz is not None

        crosspost = None
        if not juz and (telefon or klucz):
            # Numer telefonu ma pierwszeństwo przed parą miast: ten sam numer to
            # ten sam zlecający, a ta sama para miast bywa przypadkiem (dwa różne
            # auta z Krosna do Rzeszowa tego samego dnia to nie jest rzadkość).
            cur.execute(
                """
                SELECT id FROM powiadomienia
                 WHERE wyslano_at > NOW() - make_interval(hours => %s)
                   AND kanal <> 'zbiorcze'
                   AND ((%s <> '' AND telefon = %s)
                     OR (%s <> '' AND klucz_tresci = %s))
                 ORDER BY (telefon = %s) DESC, wyslano_at DESC
                 LIMIT 1
                """,
                (okno, telefon, telefon, klucz, klucz, telefon),
            )
            wiersz = cur.fetchone()
            crosspost = wiersz[0] if wiersz else None

        # Do limitu liczymy WYŁĄCZNIE realne alerty o zleceniach. Wiersz zbiorczy
        # i wiersz „pominięte" nie brzęczały (albo brzęczały raz za wszystkie),
        # więc wliczanie ich zaniżałoby dostępną pulę i wyciszało system
        # dokładnie wtedy, gdy już jest wyciszony.
        cur.execute(
            "SELECT count(*) FROM powiadomienia "
            " WHERE wyslano_at > NOW() - INTERVAL '1 hour' AND kanal = 'telegram'")
        (ile,) = cur.fetchone()
    return juz, crosspost, ile


def _dopisz_grupe(conn, id_wpisu: int, grupa: str) -> None:
    """Crosspost: podbij licznik grup zamiast wysyłać drugą wiadomość."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE powiadomienia
               SET grup  = grup + 1,
                   -- Parametr MUSI być opakowany w ARRAY[...]::text[]. Doklejony
                   -- gołym `||` do kolumny text[] jest odczytywany jako literał
                   -- tablicy i wywala się na pierwszej nazwie grupy, która nie
                   -- zaczyna się od „{". (W komentarzu do tego SQL-a nie pisz
                   -- znaku procent z „s" — psycopg2 liczy go jako placeholder.)
                   grupy = CASE WHEN %s = ANY(COALESCE(grupy, ARRAY[]::text[]))
                                THEN grupy
                                ELSE COALESCE(grupy, ARRAY[]::text[])
                                     || ARRAY[%s]::text[] END
             WHERE id = %s
            """,
            (grupa, grupa, id_wpisu),
        )
    conn.commit()


def _zapisz(conn, *, fb_id: str | None, kanal: str, tresc: str,
            message_id: int | None, telefon: str = "", klucz: str = "",
            grupa: str = "") -> None:
    """Zapisz fakt wysyłki. ON CONFLICT DO NOTHING — wyścig dwóch przebiegów
    fetchera kończy się jednym wierszem i jednym alertem, nie wyjątkiem."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO powiadomienia (fb_id, kanal, tresc, message_id,
                                       telefon, klucz_tresci, grupy)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fb_id) WHERE fb_id IS NOT NULL DO NOTHING
            """,
            (fb_id or None, kanal, tresc, message_id,
             telefon or None, klucz or None, [grupa] if grupa else None),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Wejście publiczne
# ---------------------------------------------------------------------------
def powiadom_o_zleceniu(zlecenie: dict) -> bool:
    """Wyślij alert o jednym zleceniu. True = operator zobaczył wiadomość.

    KONTRAKT `zlecenie` — klucze czytane przez ten moduł (wszystkie opcjonalne
    poza `fb_id`; brak pola degraduje wiadomość, nigdy jej nie wywala):

        fb_id            wymagany — klucz dedupu i callbacków z przycisków
        tresc            oryginał posta (cytat)
        post_url         link do posta — BEZ NIEGO ALERT JEST BEZUŻYTECZNY
        grupa_nazwa      etykieta grupy w stopce
        opublikowany_at  datetime — wiek posta
        pilnosc          'teraz' | 'dzis' | 'jutro' | 'elastycznie'
        odbior_miasto    nazwa w formie ORYGINALNEJ (patrz docs/WIELOJEZYCZNOSC.md)
        odbior_kod       '38-400' — rozjemca przy nierozpoznanej nazwie
        dostawa_miasto   j.w., opcjonalnie
        dostawa_kod      j.w., opcjonalnie
        pojazd_opis      "VW Golf IV"
        stan_uwagi       "nie odpala"
        stan_toczy_sie   True | False | None — trójstanowe, patrz `_linia_pojazdu`
        kontakt_wartosc  numer w dowolnym formacie, normalizujemy sami
        pewnosc          0-100 z klasyfikatora — próg MIN_PEWNOSC
        jezyk            'pl'|'de'|'cs'|'sk' z bramki

    False znaczy „nie wysłano" i NIE jest zaproszeniem do ponowienia. Powodów
    jest sześć i wszystkie są normalne: duplikat, crosspost, niska pewność, cisza
    nocna, przekroczony limit, awaria transportu. Każdy stoi w logu z nazwą.

    ŻADNA ze ścieżek tej funkcji nie usuwa niczego z bazy ani z panelu.
    """
    try:
        return _powiadom(zlecenie)
    except Exception as e:  # noqa: BLE001 — TO JEST TA GWARANCJA Z DOCSTRINGU MODUŁU
        # Powiadomienie jest skutkiem ubocznym przetwarzania postów. Fetcher
        # w środku przebiegu ma dwieście postów do zapisania i nie może zginąć
        # przez błąd w budowaniu jednej wiadomości.
        _log(f"nieoczekiwany błąd, alert pominięty: {type(e).__name__}: {str(e)[:300]}")
        return False


def _powiadom(zlecenie: dict) -> bool:
    fb_id = str(zlecenie.get("fb_id") or "").strip()
    if not fb_id:
        _log("BŁĄD: zlecenie bez fb_id — bez niego nie ma dedupu ani przycisków")
        return False
    if not telegram_notify.skonfigurowany():
        _log(f"{fb_id}: brak TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID — alerty wyciszone")
        return False

    conn = _polacz()
    if conn is None:
        # Bez bazy nie ma dedupu, a kanał bez dedupu zajeżdża się w jeden dzień:
        # fetcher nie zapisał posta, więc następny przebieg pobierze go znowu
        # i wyśle znowu. Cisza jest tu mniej szkodliwa niż pętla alertów, a fakt
        # niedostępnej bazy zgłasza /zdrowie, nie ten moduł.
        return False

    try:
        telefon = normalizuj_telefon(zlecenie.get("kontakt_wartosc"))
        klucz = klucz_tresci(zlecenie)
        grupa = str(zlecenie.get("grupa_nazwa") or "")
        juz, crosspost, w_godzinie = _stan_dedupu(conn, fb_id, telefon, klucz)

        decyzja = ocen(zlecenie, juz_wyslane=juz, crosspost_id=crosspost,
                       w_ostatniej_godzinie=w_godzinie,
                       pauza=pauza_aktywna(conn))
        if not decyzja.wysylac:
            _obsluz_pominiecie(conn, fb_id, decyzja, crosspost, grupa)
            return False

        odbior, dostawa = punkty(zlecenie)
        pods = geo.podsumowanie(odbior, dostawa)
        tresc = zbuduj_tresc(zlecenie, pods)
        message_id = telegram_notify.wyslij(tresc, zbuduj_przyciski(zlecenie, pods))
        if message_id is None:
            # Transport zawiódł. NIE zapisujemy wiersza — inaczej dedup uznałby
            # zlecenie za obsłużone i alert nigdy by nie poszedł, mimo że nikt
            # go nie zobaczył.
            _log(f"{fb_id}: Telegram nie przyjął wiadomości")
            return False

        _zapisz(conn, fb_id=fb_id, kanal="telegram", tresc=tresc,
                message_id=message_id, telefon=telefon, klucz=klucz, grupa=grupa)
        # Push PO Telegramie i tylko po jego sukcesie: to jest kanał DODATKOWY,
        # a nie równoległy. Gdyby szedł niezależnie, zlecenie odrzucone przez
        # dedup Telegrama nadal brzęczałoby pushem — czyli antyspam istniałby
        # dla jednego kanału, a nie dla powiadomień.
        _wyslij_push(conn, zlecenie, pods)
        _log(f"{fb_id}: wysłane (message_id={message_id}, "
             f"{w_godzinie + 1}/{settings.MAX_POWIADOMIEN_H} w tej godzinie)")
        return True
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — zamknięcie połączenia nie może przesłonić wyniku
            pass


def _obsluz_pominiecie(conn, fb_id: str, decyzja: Decyzja,
                       crosspost: int | None, grupa: str) -> None:
    """Co zrobić, gdy nie wysyłamy. Dla trzech kodów jest to coś więcej niż log."""
    _log(f"{fb_id}: pomijam ({decyzja.kod}) — {decyzja.powod}")

    if decyzja.kod == "crosspost" and crosspost is not None:
        _dopisz_grupe(conn, crosspost, grupa)
        return

    if decyzja.kod == "limit":
        # Wiersz z kanałem 'pominiete_limit' zamyka sprawę tego posta NA ZAWSZE:
        # bez niego zlecenie z godzinnego szczytu dostałoby alert godzinę później,
        # gdy licznik zejdzie — czyli operator zobaczyłby lawinę drugi raz,
        # tylko rozciągniętą w czasie.
        _zapisz(conn, fb_id=fb_id, kanal="pominiete_limit",
                tresc=decyzja.powod, message_id=None)
        _zbiorcze_o_limicie(conn)
        return

    # 'pewnosc' i 'cisza_nocna' NIE zostawiają wiersza. Pierwszy dlatego, że
    # zlecenie czeka w panelu i nic więcej nie trzeba; drugi dlatego, że rano
    # podsumowanie szuka właśnie zleceń BEZ wiersza w `powiadomienia`.


def _zbiorcze_o_limicie(conn) -> None:
    """Jedna wiadomość „jeszcze N zleceń w panelu" na godzinę i cisza.

    Zbiorcza wiadomość jest przy okazji ALARMEM: przekroczenie piętnastu alertów
    w godzinę prawie zawsze znaczy, że bramka albo klasyfikator zaczął przepuszczać
    śmieci. Dlatego mówi wprost, że to nie jest normalny stan.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM powiadomienia "
            " WHERE wyslano_at > NOW() - INTERVAL '1 hour' AND kanal = 'zbiorcze'")
        (byla,) = cur.fetchone()
        if byla:
            return                      # jedna na godzinę — to jest cały sens limitu
        cur.execute(
            "SELECT count(*) FROM powiadomienia "
            " WHERE wyslano_at > NOW() - INTERVAL '1 hour' "
            "   AND kanal = 'pominiete_limit'")
        (ile,) = cur.fetchone()

    tresc = (f"🔕 Limit {settings.MAX_POWIADOMIEN_H} powiadomień na godzinę "
             f"przekroczony — jeszcze {ile} zleceń czeka w panelu.\n\n"
             "Nic nie zginęło: wszystko jest w aplikacji. Taka lawina zwykle "
             "znaczy, że bramka albo klasyfikator zaczął przepuszczać śmieci — "
             "warto zajrzeć w `/statystyki`.")
    przyciski = ([[{"text": "📱 Otwórz panel", "url": settings.PANEL_URL}]]
                 if settings.PANEL_URL else None)
    message_id = telegram_notify.wyslij(tresc, przyciski)
    _zapisz(conn, fb_id=None, kanal="zbiorcze", tresc=tresc, message_id=message_id)


# ---------------------------------------------------------------------------
# Web push — kanał DODATKOWY
# ---------------------------------------------------------------------------
def _wyslij_push(conn, zlecenie: dict, pods: dict) -> None:
    """Powiadomienie systemowe do zapisanych przeglądarek. Nigdy nie rzuca.

    TRZY WARUNKI WYŁĄCZAJĄ TEN KANAŁ PO CICHU i wszystkie trzy są normalne:
    brak kluczy VAPID w `.env`, brak `pywebpush` w środowisku, brak tabeli
    (migracja 0007 nieodpalona). W każdym z tych przypadków Telegram już
    dowiózł alert — push jest dodatkiem i jego brak nie jest awarią.

    `pywebpush` jest zależnością OPCJONALNĄ (patrz requirements.txt): szyfrowanie
    treści push wymaga `cryptography`, czyli największej paczki w całym projekcie.
    Wymuszanie jej na deployu, który korzysta wyłącznie z Telegrama, byłoby
    kilkoma minutami budowania na VPS-ie za funkcję, której nikt nie włączył.
    """
    if not (settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY):
        return
    try:
        from pywebpush import WebPushException, webpush  # noqa: PLC0415
    except ImportError:
        _log("push pominięty: brak pywebpush (pip install pywebpush)")
        return

    fb_id = str(zlecenie.get("fb_id") or "")
    odbior, dostawa = punkty(zlecenie)
    ladunek = json.dumps({
        "tytul": _linia_pilnosci(zlecenie, pods),
        "tresc": (f"{_linia_trasy(zlecenie, odbior, dostawa, pods)}\n"
                  f"{_linia_pojazdu(zlecenie)}").strip(),
        "fb_id": fb_id,
        "url": f"/zlecenie/{fb_id}",
        # `requireInteraction` w service workerze: powiadomienie zostaje na
        # ekranie do dotknięcia zamiast zniknąć po kilku sekundach. Tylko dla
        # „teraz" — przy każdym alercie zamieniłoby się w listę do posprzątania.
        "pilne": str(zlecenie.get("pilnosc") or "") == "teraz",
    }, ensure_ascii=False)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT endpoint, p256dh, auth FROM push_subskrypcje")
            subskrypcje = cur.fetchall()
    except Exception as e:  # noqa: BLE001 — brak tabeli to stan, nie awaria
        _log(f"push pominięty: {type(e).__name__}: {str(e)[:120]}")
        conn.rollback()
        return

    martwe: list[str] = []
    for endpoint, p256dh, auth in subskrypcje:
        try:
            webpush(
                subscription_info={"endpoint": endpoint,
                                   "keys": {"p256dh": p256dh, "auth": auth}},
                data=ladunek,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_KONTAKT},
                timeout=5,
            )
        except WebPushException as e:
            kod = getattr(getattr(e, "response", None), "status_code", None)
            if kod in (404, 410):
                # Dostawca mówi wprost: tej subskrypcji już nie ma (PWA
                # odinstalowana, dane strony wyczyszczone). Trzymanie jej
                # dokłada nieudane wywołanie HTTPS do KAŻDEGO kolejnego alertu,
                # czyli opóźnia ścieżkę, która ma dowieźć zlecenie w sekundę.
                martwe.append(endpoint)
            else:
                _log(f"push {endpoint[:40]}…: {str(e)[:120]}")
        except Exception as e:  # noqa: BLE001 — jeden telefon nie psuje reszty
            _log(f"push {endpoint[:40]}…: {type(e).__name__}: {str(e)[:120]}")

    if martwe:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM push_subskrypcje WHERE endpoint = ANY(%s)",
                        (martwe,))
        conn.commit()
        _log(f"push: usunięto {len(martwe)} martwych subskrypcji")


# ---------------------------------------------------------------------------
# Podsumowanie ranne — to, co uzbierało się w ciszy nocnej
# ---------------------------------------------------------------------------
def podsumowanie_nocne(teraz: datetime | None = None) -> bool:
    """Jedna wiadomość o wszystkim, co przyszło w nocy. Woła to cron rano.

    DLACZEGO ZBIORCZO, A NIE SERIĄ O ŚWICIE: dwadzieścia alertów o 6:00 wygląda
    dokładnie jak awaria i kończy się wyciszeniem bota. Jedna wiadomość
    z listą i linkiem do panelu robi tę samą robotę i nie kosztuje zaufania.

    Zlecenia znajdujemy przez BRAK wiersza w `powiadomienia` — stan trzymamy
    w bazie, nie w pliku ze znacznikiem czasu ostatniego podsumowania, bo taki
    plik ginie przy redeployu i podsumowanie leci drugi raz.
    """
    conn = _polacz()
    if conn is None:
        return False
    try:
        okno = settings.CISZA_NOCNA_OD - settings.CISZA_NOCNA_DO
        godzin = (24 + okno) if okno < 0 else max(okno, 1)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.fb_id, p.grupa_nazwa, p.opublikowany_at,
                       p.odbior_kod, p.odbior_miasto,
                       p.dostawa_kod, p.dostawa_miasto
                  FROM posty p
                  LEFT JOIN powiadomienia w ON w.fb_id = p.fb_id
                 WHERE p.czy_zlecenie
                   AND p.status = 'nowe'
                   AND w.id IS NULL
                   AND p.pobrany_at > NOW() - make_interval(hours => %s)
                 ORDER BY p.opublikowany_at DESC NULLS LAST
                 LIMIT 20
                """,
                (godzin,),
            )
            wiersze = cur.fetchall()

        if not wiersze:
            _log("noc bez zleceń — nie wysyłam pustego podsumowania")
            return False

        linie = [f"🌅 *W nocy przyszło {len(wiersze)} zleceń*", ""]
        for fb_id, grupa, opublikowany, o_kod, o_miasto, d_kod, d_miasto in wiersze:
            pods = geo.podsumowanie(geo.geokoduj(o_kod, o_miasto),
                                    geo.geokoduj(d_kod, d_miasto))
            km = pods["km_trasy"] if pods["km_trasy"] is not None else pods["km_od_bazy"]
            trasa = str(o_miasto or o_kod or "?")
            if d_miasto or d_kod:
                trasa += f" → {d_miasto or d_kod}"
            linie.append(telegram_notify._escape_md(
                f"• {trasa} · {round(km) if km is not None else '?'} km "
                f"· {wiek_posta(opublikowany, teraz)} · {grupa or '?'}"))
        linie += ["", "Szczegóły i przyciski — w panelu."]
        tresc = "\n".join(linie)

        przyciski = ([[{"text": "📱 Otwórz panel", "url": settings.PANEL_URL}]]
                     if settings.PANEL_URL else None)
        message_id = telegram_notify.wyslij(tresc, przyciski)
        if message_id is None:
            return False
        # Wiersze per zlecenie, żeby dedup uznał je za obsłużone — inaczej
        # pierwszy dzienny przebieg fetchera wysłałby je wszystkie pojedynczo.
        for fb_id, *_ in wiersze:
            _zapisz(conn, fb_id=fb_id, kanal="podsumowanie",
                    tresc="ujęte w podsumowaniu nocnym", message_id=message_id)
        _zapisz(conn, fb_id=None, kanal="zbiorcze", tresc=tresc,
                message_id=message_id)
        _log(f"podsumowanie nocne: {len(wiersze)} zleceń")
        return True
    except Exception as e:  # noqa: BLE001 — cron nie może dostać tracebacka
        _log(f"podsumowanie nocne nie poszło: {type(e).__name__}: {str(e)[:200]}")
        return False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# CLI — obejrzenie układu BEZ wysyłania i test dowozu na własny telefon.
#
#   python -m laweta_radar.services.powiadomienia --podglad   # sam tekst, bez sieci
#   python -m laweta_radar.services.powiadomienia --probka    # wyślij przykład
#   python -m laweta_radar.services.powiadomienia --noc       # podsumowanie ranne
#
# `--podglad` istnieje dlatego, że alert jest produktem tego systemu, a produkt,
# którego nie da się obejrzeć bez produkcji, poprawia się na ślepo.
# ---------------------------------------------------------------------------
PRZYKLAD = {
    "fb_id": "przyklad-0001",
    "pilnosc": "teraz",
    "odbior_miasto": "Krosno",
    "odbior_kod": "38-400",
    "dostawa_miasto": "Rzeszow",
    "pojazd_opis": "VW Golf IV",
    "stan_uwagi": "nie odpala",
    "stan_toczy_sie": True,
    "tresc": ("potrzebuje lawety z Krosna do Rzeszowa, golf stanal i nie odpala, "
              "moze byc dzis wieczorem, dzwonic po 16"),
    "kontakt_wartosc": "+48 555 111 222",
    "grupa_nazwa": "Pomoc drogowa Podkarpacie",
    "pewnosc": 88,
    "jezyk": "pl",
    "post_url": "https://www.facebook.com/groups/000/posts/111/",
}


def _main(argv: list[str]) -> int:
    tryb = argv[1] if len(argv) > 1 else "--podglad"

    if tryb == "--noc":
        return 0 if podsumowanie_nocne() else 1

    przyklad = dict(PRZYKLAD)
    przyklad["opublikowany_at"] = datetime.now(timezone.utc) - timedelta(minutes=4)

    if tryb == "--probka":
        tresc = zbuduj_tresc(przyklad)
        ok = telegram_notify.wyslij(tresc, zbuduj_przyciski(przyklad)) is not None
        print(f"Wysłano: {ok}")
        return 0 if ok else 1

    print(zbuduj_tresc(przyklad))
    print()
    print("--- przyciski ---")
    for wiersz in zbuduj_przyciski(przyklad):
        print("  " + "  ".join(
            f"[ {p['text']} -> {p.get('url') or p.get('callback_data')} ]"
            for p in wiersz))
    print()
    print(settings.opis_srodowiska())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
