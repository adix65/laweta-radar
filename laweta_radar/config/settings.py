"""Jedno miejsce, w którym czytamy środowisko — i jedno miejsce, które decyduje,
co zrobić, gdy czegoś brakuje.

DLACZEGO NIE pydantic-settings (jak w repo źródłowym): tamten model waliduje przy
imporcie i rzuca `ValidationError`, gdy brakuje pola wymaganego. Tutaj to jest
dokładnie zła reakcja. Workery chodzą z crona co kilka minut i mają zasadę:
brak konfiguracji = CZYSTE WYJŚCIE z komunikatem, nigdy wyjątek. Wyjątek przy
imporcie oznacza ślad w mailu od crona co przebieg, a przy pierwszym uruchomieniu
na świeżym VPS-ie — ścianę tracebacków zamiast jednej linijki "ustaw X w .env".
Dlatego: czytamy leniwie, oddajemy pustki, a decyzję "jechać czy nie" podejmuje
worker przez `brakujace()` + `wyjscie_bez_konfiguracji()`.

Sekrety NIE mają tu getterów maskujących — maskowanie robi ten, kto loguje
(patrz `mask_url` / `proxy_label` w workers/apify_proxy.py). Zasada w całym repo
jest prostsza od API: wartości z tego modułu nie trafiają do logów wprost.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from laweta_radar.config import shared_env

# Katalog pakietu — tu leży .env, tu rotacja kluczy Apify trzyma swój plik stanu.
# Wszystkie moduły liczą tę ścieżkę tak samo (parent.parent od siebie), więc
# .env jest jeden, niezależnie od tego, co odpalasz.
BASE_DIR = Path(__file__).resolve().parent.parent

# override=False: zmienne ustawione JAWNIE w środowisku (PM2, systemd, `export`
# w skrypcie deploya) mają wygrywać z plikiem. Inaczej deploy przez PM2 czytałby
# .env developera, który akurat został na dysku.
load_dotenv(BASE_DIR / ".env", override=False)


# Klucze Apify NIE należą do tego repo — przychodzą ze wspólnego .env
# sales-core-engine (ta sama pula kont, te same proxy). Całość wraz z listą
# przepisywanych zmiennych i uzasadnieniem: config/shared_env.py. Tutaj tylko
# re-eksport, żeby wołający miał jedno miejsce do pytania o konfigurację.
sciezka_wspolnego_env = shared_env.sciezka_wspolnego_env
_wczytaj_wspolne_apify = shared_env.wczytaj
WSPOLNE_APIFY_ILE = shared_env.ILE
WSPOLNE_APIFY_SKAD = shared_env.SKAD


def _txt(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _int(name: str, default: int) -> int:
    """Liczba ze środowiska; śmieć albo brak -> domyślna.

    Świadomie NIE rzucamy na literówce ("30 min" zamiast "30"): pojedyncze
    pokrętło nie może zatrzymać całego przebiegu. Zła wartość degraduje do
    domyślnej, a operator zobaczy to w linijce startowej workera.
    """
    try:
        return int(_txt(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    """Jak `_int`, ale przyjmuje też przecinek dziesiętny.

    Współrzędne bazy kopiuje się z Map Google, a te w polskiej lokalizacji podają
    je z przecinkiem. Cicha zamiana na kropkę jest tu bezpieczna (nie ma innego
    znaczenia przecinka w liczbie) i oszczędza godziny szukania, czemu dystans
    wychodzi 0 km od równika.
    """
    raw = _txt(name)
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Baza — OSOBNA instancja/baza `laweta`, nie współdzielona z innymi projektami.
# Rozdział jest celowy: to inny cykl życia danych (posty z grup żyją godzinami)
# i inne uprawnienia. Rola z tego DSN-a NIE ma praw DDL — tabele zakłada migracja
# odpalana ręcznie jako postgres (api/migrations/, patrz README).
# ---------------------------------------------------------------------------
DATABASE_URL = _txt("DATABASE_URL")

# ---------------------------------------------------------------------------
# Klasyfikator. Model trzymamy w konfiguracji, bo przy zmianie progu
# jakość/koszt chcemy go podmienić bez deployu — a nie dlatego, że spodziewamy
# się częstych zmian.
#
# LLM_PROVIDER przełącza CAŁĄ implementację wołania modelu (services/llm.py),
# nie tylko nazwę. Istnieje z powodu pomiarowego: jakości ekstrakcji z polskiego
# posta bez ogonków nie da się odczytać z żadnego benchmarku, więc mierzymy ją
# na swoich danych (scripts/porownaj_modele.py). Nieznana wartość degraduje do
# "anthropic" — jedynego providera, którego zależność jest w requirements.txt.
#
# Model per provider, a nie jeden wspólny, bo porównywarka odpala WSZYSTKIE
# skonfigurowane naraz i musi wiedzieć, co puścić na każdym.
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = _txt("ANTHROPIC_API_KEY")
CLASSIFIER_MODEL = _txt("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")

LLM_PROVIDER = _txt("LLM_PROVIDER", "anthropic")
OPENAI_API_KEY = _txt("OPENAI_API_KEY")
# BEZ WARTOŚCI DOMYŚLNEJ — świadomie. Nazwy modeli tej rodziny zmieniają się co
# kilka miesięcy, a domyślna w kodzie znaczyłaby, że po wpisaniu samego klucza
# system odpala model, którego nikt nie wybrał, i płaci stawkę, której nikt nie
# sprawdzał. Pusto = `llm.problemy("openai")` mówi wprost, czego brakuje.
OPENAI_MODEL = _txt("OPENAI_MODEL")
GEMINI_API_KEY = _txt("GEMINI_API_KEY")
GEMINI_MODEL = _txt("GEMINI_MODEL", "gemini-2.5-flash")

# KTÓRY KLUCZ JEST WYMAGANY, ZALEŻY OD `LLM_PROVIDER` — i nie ma innej poprawnej
# odpowiedzi. Sprawdzanie ANTHROPIC_API_KEY bezwarunkowo zgłasza brak krytyczny
# na maszynie, na której klasyfikator DZIAŁA — tylko na OpenAI. Klucz nieużywanego
# providera jest wtedy pustą linijką w .env, a nie awarią.
#
# Klucze POZOSTAŁYCH providerów są OPCJONALNE. Ich brak zawęża dokładnie jedną
# rzecz: `scripts/porownaj_modele.py` puści porównanie na mniejszej liczbie
# modeli. Tyle ma o nim mówić raport („porównanie modeli obejmie 1 z 3
# providerów") i nigdy nie jest to powód, żeby cokolwiek zatrzymać.
#
# Para (klucz, nazwa modelu), bo przy OpenAI wymagane jest OBOJE: nazwa modelu
# nie ma tam wartości domyślnej (patrz nota przy OPENAI_MODEL), więc pusta
# znaczy „nie ma czym wołać". U pozostałych domyślna siedzi w kodzie, więc pusta
# zmienna środowiskowa NIE jest brakiem — dlatego `braki_providera()` patrzy na
# wartość SKUTECZNĄ, a nie na to, czy ktoś wpisał linijkę w .env.
#
# To jest też jedyne miejsce, w którym mieszkają nazwy zmiennych per provider:
# `services/llm.py` czyta je stąd, żeby dołożenie providera było jedną zmianą,
# a nie dwiema, z których druga zostaje zapomniana.
ZMIENNE_PROVIDERA: dict[str, tuple[str, str]] = {
    # provider  -> (zmienna z kluczem,   zmienna z nazwą modelu)
    "anthropic": ("ANTHROPIC_API_KEY",   "CLASSIFIER_MODEL"),
    "openai":    ("OPENAI_API_KEY",      "OPENAI_MODEL"),
    "gemini":    ("GEMINI_API_KEY",      "GEMINI_MODEL"),
}

# Kolejność jest kolejnością sekcji w .env.example i w tej kolejności wchodzą
# do raportów.
PROVIDERY: tuple[str, ...] = tuple(ZMIENNE_PROVIDERA)

# Nieznana wartość LLM_PROVIDER degraduje TUTAJ — do jedynego providera, którego
# zależność siedzi w requirements.txt. Literówka w .env nie może zatrzymać crona
# ani przełączyć systemu na coś, czego na maszynie nie ma.
PROVIDER_DOMYSLNY = "anthropic"

# --- OpenAI: różnice API, których nie da się schować w jednej implementacji ---
#
# OPENAI_JSON_MODE — "off" | "object" | "schema". Opis trybów i ich PUŁAPKI
# (tryb JSON gwarantuje kształt, NIE prawdziwość wartości) stoi przy
# `services/llm._response_format`. Domyślne "object" wymusza poprawny JSON bez
# narzucania schematu.
OPENAI_JSON_MODE = _txt("OPENAI_JSON_MODE", "object")
# Nakład rozumowania (`reasoning_effort`). PUSTE = nie wysyłamy parametru wcale.
# Dopuszczalne wartości ZALEŻĄ OD MODELU (bywa "none", "minimal", "low"...),
# więc żadnej nie zgadujemy: model, który tego parametru nie przyjmie, dostanie
# wywołanie bez niego i podniesiony limit tokenów (services/llm.py).
OPENAI_REASONING = _txt("OPENAI_REASONING")
# Rola pierwszej wiadomości: "system" (zgodne wstecz) albo "developer" (nazwa
# używana przez nowsze modele). Anthropic bierze prompt systemowy osobnym
# parametrem i ta zmienna go nie dotyczy.
OPENAI_ROLA_SYSTEMOWA = _txt("OPENAI_ROLA_SYSTEMOWA", "system")
# Sufit czasu jednego wywołania. Trzydzieści sekund to dużo jak na ekstrakcję
# z jednego posta — wywołanie, które tyle trwa, i tak jest już spóźnione wobec
# konkurencji, a wisząca sesja blokuje resztę przebiegu.
OPENAI_TIMEOUT_S = _int("OPENAI_TIMEOUT_S", 30)

# Stawki modeli spoza wbudowanego cennika — JSON
# {"model": [usd_wejscie, usd_wyjscie]} albo [..., usd_cache] za MILION tokenów.
# Wbudowane ceny siedzą w config/cennik.py razem z datą sprawdzenia; ta zmienna
# jest po to, żeby dopisać model bez deployu. Pusto = raport pokaże koszt jako
# nieznany, i to jest poprawna odpowiedź, dopóki nikt nie sprawdził cennika.
CENNIK_EXTRA = _txt("CENNIK_EXTRA")
# Kurs do przeliczenia kosztu runu na złotówki. Wartość domyślna jest
# ZAOKRĄGLONYM PLACEHOLDEREM — ustaw realny, jeśli liczba ma być czymś więcej
# niż rzędem wielkości.
KURS_USD_PLN = _float("KURS_USD_PLN", 4.00)

# ---------------------------------------------------------------------------
# Telegram — jedyny kanał dowozu do operatora. Brak tokenu wycisza alerty
# (services/telegram_notify._send zwraca False), ale NIE zatrzymuje zbierania:
# posty lecą do bazy i da się je wysłać po skonfigurowaniu.
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = _txt("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _txt("TELEGRAM_CHAT_ID")

# ---------------------------------------------------------------------------
# Baza lawety — punkt, od którego liczymy dystans do zdarzenia. Bez tego nie da
# się odrzucić zlecenia z drugiego końca Polski, a takie zlecenie jest gorsze niż
# brak zlecenia: operator traci czas na czytanie alertu, którego i tak nie weźmie.
# ---------------------------------------------------------------------------
# BAZA_LNG i BAZA_LON to ta sama rzecz — długość geograficzna. `LON` był tu od
# pierwszej wersji .env, `LNG` przyszedł z zadania geo i jest formą, którą
# ludzie kopiują z Map Google. Przyjmujemy OBIE zamiast wybierać jedną, bo
# jedyny efekt wyboru byłby taki, że czyjaś działająca konfiguracja po cichu
# przestaje działać i dystanse liczą się od równika.
BAZA_LAT = _float("BAZA_LAT", 0.0)
BAZA_LON = _float("BAZA_LNG", 0.0) or _float("BAZA_LON", 0.0)
BAZA_LNG = BAZA_LON
BAZA_NAZWA = _txt("BAZA_NAZWA", "Krosno")

# UWAGA: ta zmienna NIE JEST bramką i nie wolno jej nią zrobić. Zasada naczelna
# repo mówi: system pokazuje zlecenia, decyduje kierowca. Dystans jest etykietą
# na ekranie — przy transporcie międzynarodowym „ile km od bazy" i tak nie znaczy
# nic, bo liczy się długość kursu odbiór->dostawa (services/geo.py). Zmienna
# została, bo pokazuje ją /health i bo służy do WYRÓŻNIENIA zleceń blisko bazy.
MAX_DYSTANS_KM = _int("MAX_DYSTANS_KM", 80)

# Stawki do etykiety „szacunek" przy zleceniu (services/geo.kalkulacja).
# To ETYKIETA NA EKRANIE, nie wycena i nie bramka — nic się na jej podstawie
# nie ukrywa ani nie odrzuca. Kierowca patrzy na liczbę i sam decyduje.
STAWKA_ZA_KM = _float("STAWKA_ZA_KM", 4.0)
STAWKA_MINIMALNA = _float("STAWKA_MINIMALNA", 250.0)

# Po ilu godzinach post przestaje być zleceniem. Starszy trafia do bazy ze
# znacznikiem `stale`, ale NIE budzi nikogo powiadomieniem i nie idzie do modelu.
#
# UWAGA — TA LICZBA JEST DO USTALENIA I ZALEŻY OD PROFILU OPERATORA. Sześć godzin
# pochodzi z założenia „awaria na poboczu": zlecenie żyje kilkadziesiąt minut,
# więc post sprzed sześciu godzin jest już cudzy.
#
# README opisuje jednak profil INNY — trasy międzynarodowe zestawem B+E, gdzie
# „kupiłem auto w Niemczech, kto przywiezie" żyje DNIAMI, nie kwadransem. Przy
# tym profilu sześć godzin kasuje powiadomienia o zleceniach, które są jeszcze
# w pełni aktualne, a to jest dokładnie ten rodzaj decyzji, który zasada naczelna
# repo („system pokazuje, decyduje kierowca") odbiera kodowi.
#
# Dla profilu transportowego ustaw 48 albo więcej. Mechanizm jest ten sam —
# różni się tylko liczba, i to świadomie, bo nikt poza operatorem nie wie,
# którego rodzaju zleceń realnie szuka.
MAX_WIEK_POSTA_H = _int("MAX_WIEK_POSTA_H", 6)

# ---------------------------------------------------------------------------
# BUDŻET APIFY — TWARDY SUFIT POBRANYCH POSTÓW NA DOBĘ dla całego systemu.
#
# Liczony w POSTACH, nie w runach, bo tak rozlicza się ten actor (patrz
# config/groups.py). Sufit jest wspólny dla wszystkich grup i pilnuje go fetcher
# przez licznik w tabeli `harmonogram` — po jego wyczerpaniu NIE wykonuje
# kolejnych wywołań i mówi to wyraźnie w logu.
#
# DLACZEGO TAK OSTRO: pula kont Apify jest WSPÓLNA z sales-core-engine. Cicho
# przekroczony budżet to nie jest „trochę wyższy rachunek" — to spalona pula,
# z której korzysta też drugi system, i awaria dwóch rzeczy naraz.
#
# 2000 postów/dobę przy cenie rzędu 2,60 USD/1000 to ok. 156 USD miesięcznie.
# ---------------------------------------------------------------------------
POSTY_NA_DOBE = _int("POSTY_NA_DOBE", 2000)

# Nadpisanie ścieżki z pomiaru actora ("A" albo "B"). Puste = fetcher czyta
# werdykt z docs/POMIAR-ACTORA.md, a gdy pomiaru nie ma — schodzi na ścieżkę B,
# czyli droższą w pobraniu, ale tańszą w pomyłce (patrz workers/fb_fetcher.py).
SCIEZKA_ACTORA = _txt("SCIEZKA_ACTORA").upper()

# ---------------------------------------------------------------------------
# BRAMKA (workers/gate.py) — darmowy prefiltr słownikowy przed modelem.
#
# GATE_PROG: suma wag, od której post idzie do AI. Piątka jest CELOWO niska.
# Asymetria kosztów jest brutalna: śmieć przepuszczony do AI to ~0,002 zł, a
# zlecenie odrzucone przez bramkę to ~300 zł straconego kursu, o którym nigdy
# się nie dowiemy — post nie trafi nigdzie. Jeden przegapiony kurs miesięcznie
# kasuje CAŁĄ oszczędność na tokenach. Właściwą wartość odczytuje się
# z rozkładu punktów w scripts/raport_gate.py, a nie zgaduje.
#
# GATE_TRYB: "cien" albo "aktywny". W cieniu bramka liczy i zapisuje swoją
# decyzję, ale NICZEGO nie blokuje — wszystkie posty idą do AI. Dopiero to daje
# pary (decyzja bramki, werdykt AI), z których widać jedyną liczbę, która ma
# znaczenie: ile zleceń bramka by skasowała. Przełączenie na "aktywny" ma sens
# wyłącznie wtedy, gdy ta liczba wynosi zero. Nieznana wartość degraduje do
# "cien" — literówka w .env nie może po cichu włączyć blokowania.
# ---------------------------------------------------------------------------
GATE_PROG = _int("GATE_PROG", 5)
GATE_TRYB = _txt("GATE_TRYB", "cien")

# TRANSPORT ZWIERZĄT — czy o takim zleceniu ma brzęczeć telefon.
#
# 0 (domyślnie) = post z `kategoria_ladunku='zwierze'` trafia do panelu BEZ
#                 powiadomienia na Telegramie. Jest w bazie, jest na liście
#                 (ze znacznikiem i niżej), tylko nie budzi.
# 1             = alertuje normalnie, jak każde inne zlecenie.
#
# TO NIE JEST BRAMKA i nie wolno jej nią zrobić. Bramka takich postów NIE
# odrzuca — operator nie wozi zwierząt, ale „nie wozi" i „nie chcę wiedzieć"
# to dwie różne rzeczy, a tylko o pierwszej wolno rozstrzygać kodowi. Gdyby
# doszła przyczepa do koni albo chęć podnajmowania takich kursów dalej, dane
# już są zebrane; twarde odrzucenie kasowałoby je bezpowrotnie i bez śladu.
ALERT_ZWIERZETA = _int("ALERT_ZWIERZETA", 0)

# ---------------------------------------------------------------------------
# POWIADOMIENIA (services/powiadomienia.py) — progi sterują WYŁĄCZNIE tym, czy
# brzęczy telefon. ŻADEN z nich nie usuwa zlecenia z bazy ani z panelu; to jest
# zasada naczelna repo i najłatwiejsza tutaj do złamania, bo „nie wysyłaj" i
# „ukryj" wyglądają w kodzie podobnie.
# ---------------------------------------------------------------------------
# Poniżej tej pewności klasyfikatora zlecenie trafia do panelu BEZ powiadomienia.
# Nie do kosza — do panelu. Czterdziestka jest niska celowo: alert o zleceniu,
# którego model nie jest pewny, kosztuje trzy sekundy uwagi, a niewysłany alert
# o realnym kursie kosztuje kurs.
#
# UWAGA NA DRUGI PRÓG: `workers/classifier.PROG_PEWNOSCI` (50) jest stałą MODUŁU
# i służy jego własnemu `warto_budzic()` — podpowiedzi dla wołającego, nie decyzji.
# JEDYNYM miejscem, które rozstrzyga, czy brzęczy telefon, jest
# `services/powiadomienia.ocen()` i to ono czyta tę zmienną. Gdyby oba progi
# rozstrzygały naraz, obowiązywałby ostrzejszy, a operator zmieniający
# `MIN_PEWNOSC` w .env nie zobaczyłby żadnej różnicy i nie miałby jak się
# dowiedzieć dlaczego.
MIN_PEWNOSC = _int("MIN_PEWNOSC", 40)

# Cisza nocna [OD, DO) w godzinach lokalnych. W tych godzinach nie brzęczymy —
# wszystko z nocy idzie jednym zbiorczym podsumowaniem rano. Transport planowany
# nie ucieka przez osiem godzin, a operator wyciszający bota po trzeciej nocnej
# pobudce przestaje dostawać także te alerty, które były coś warte.
CISZA_NOCNA_OD = _int("CISZA_NOCNA_OD", 22)
CISZA_NOCNA_DO = _int("CISZA_NOCNA_DO", 6)

# Twardy sufit powiadomień na godzinę. Po przekroczeniu leci JEDNA zbiorcza
# wiadomość „jeszcze N zleceń w panelu" i cisza. Przekroczenie tego limitu prawie
# zawsze znaczy, że coś się zepsuło w bramce albo w klasyfikatorze — system
# wysyłający 40 alertów dziennie zostanie wyciszony po tygodniu i przestanie
# istnieć, a to jest awaria całkowita, tylko rozłożona na dni.
MAX_POWIADOMIEN_H = _int("MAX_POWIADOMIEN_H", 15)

# Okno dedupu treściowego. Ten sam post crossowany do pięciu grup ma pięć różnych
# fb_id (hash liczymy z treści, a treść bywa minimalnie inna), więc dedup po
# identyfikatorze go nie złapie.
DEDUP_OKNO_H = _int("DEDUP_OKNO_H", 6)

# ---------------------------------------------------------------------------
# API panelu — JEDEN użytkownik, jeden token w nagłówku. Świadomie bez ról,
# bez sesji i bez OAuth: system ról dla jednej osoby to warstwa, która potrafi
# się zepsuć, i zero bezpieczeństwa więcej.
#
# Puste = API odpowiada WYŁĄCZNIE na /health i /zdrowie, a każdy endpoint
# z danymi zwraca 503. Nie 200 z pustą listą — brak tokenu to niedokończona
# konfiguracja, a nie „brak zleceń".
# ---------------------------------------------------------------------------
API_TOKEN = _txt("API_TOKEN")

# Adres panelu — wchodzi do powiadomienia jako link „Otwórz w panelu" i do
# nagłówka CORS. Puste = przycisk się nie pojawia (lepiej niż link donikąd).
PANEL_URL = _txt("PANEL_URL").rstrip("/")

# ---------------------------------------------------------------------------
# WEB PUSH (VAPID) — DRUGI kanał obok Telegrama, nigdy zamiennik.
#
# Puste klucze = push po prostu wyłączony i panel mówi to wprost. To jest stan
# domyślny i całkowicie poprawny: Telegram dowozi sam, a push wymaga zgody
# przeglądarki, obsługi po jej stronie, a na iOS dodania PWA do ekranu głównego
# — czyli trzech warunków, z których każdy potrafi przestać być spełniony bez
# żadnego objawu.
#
# Wygenerowanie pary kluczy (raz, na stałe):
#   python -c "from py_vapid import Vapid01; v=Vapid01(); v.generate_keys(); \
#     print(v.public_key_urlsafe_base64(), v.private_key_urlsafe_base64())"
# ---------------------------------------------------------------------------
VAPID_PUBLIC_KEY = _txt("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = _txt("VAPID_PRIVATE_KEY")
# Adres kontaktowy wymagany przez specyfikację VAPID — dostawca push używa go,
# gdy coś jest nie tak z ruchem z tego serwera. Bez niego część dostawców
# odrzuca żądania.
VAPID_KONTAKT = _txt("VAPID_KONTAKT", "mailto:admin@example.com")

# ---------------------------------------------------------------------------
# CZEGO BRAKUJE — I CZY TO W OGÓLE JEST POWÓD, ŻEBY SIĘ ZATRZYMAĆ
#
# Braki trzymamy jako dane, a nie rozsiane po `if not X` w workerach: dzięki temu
# komunikat jest wszędzie taki sam i da się wypisać CAŁY naraz, zamiast wychodzić
# po pierwszym brakującym kluczu i wracać po poprawce po kolejny.
#
# Ale ważniejsze od listy jest to, że braki dzielą się na DWIE KLASY:
#
#   BLOKUJĄCE START  Bez tego nie ma czego pokazywać ani gdzie zapisać. Pozycja
#                    jest JEDNA: DATABASE_URL.
#   DEGRADUJĄCE      Cała reszta — klucz modelu, Telegram, klucze Apify, token
#                    panelu. Każde z nich wyłącza JEDEN podsystem, a system stoi
#                    dalej: bez klucza modelu fetcher wciąż zbiera posty do bazy,
#                    bramka wciąż punktuje, panel wciąż pokazuje to, co zebrane,
#                    a Telegram wciąż dowozi.
#
# DLACZEGO TO JEST OSOBNY BYT, A NIE JEDNA LISTA. Brak opcjonalnego klucza, który
# kładzie API, daje najgorszy możliwy tryb pracy: proces kończy się „czysto", PM2
# podnosi go z powrotem, i po chwili w `pm2 status` stoi pętla restartów ze
# statusem `errored`. W logach wygląda to na awarię kodu, a jest brakiem jednej
# linijki w .env — czyli szuka się tego w najgorszym możliwym miejscu.
#
# Klasa braku NIE zależy od tego, kto pyta. Zależy od niej natomiast reakcja:
# worker z crona kończy czysto (`wyjscie_bez_konfiguracji`), a API wstaje
# ZAWSZE i mówi prawdę w `/health` — bo endpoint diagnostyczny jest potrzebny
# dokładnie wtedy, gdy konfiguracja jest niepełna.
# ---------------------------------------------------------------------------
OPIS_ZMIENNYCH: dict[str, str] = {
    "DATABASE_URL": "DSN do bazy `laweta` — OSOBNEJ od sales-core-engine",
    "ANTHROPIC_API_KEY": "klucz API Anthropic (LLM_PROVIDER=anthropic)",
    "OPENAI_API_KEY": "klucz API OpenAI (LLM_PROVIDER=openai)",
    "OPENAI_MODEL": "nazwa modelu OpenAI — bez wartości domyślnej, podaj świadomie",
    "GEMINI_API_KEY": "klucz API Gemini (LLM_PROVIDER=gemini)",
    "TELEGRAM_BOT_TOKEN": "token bota od @BotFather",
    "TELEGRAM_CHAT_ID": "ID czatu operatora (bot musi tam być dodany)",
    "API_TOKEN": ("token panelu — jeden użytkownik, nagłówek `X-Token`. "
                  "Wygeneruj: python -c \"import secrets;print(secrets.token_urlsafe(32))\""),
    "APIFY_API_TOKEN1": ("klucz Apify — normalnie przychodzi ze WSPÓLNEGO .env "
                         "(SHARED_ENV_PATH), nie ustawiaj go tutaj bez powodu"),
}

# Co PRZESTAJE DZIAŁAĆ przy tym braku — zdanie, nie nazwa zmiennej. To jest
# jedyna treść, która odpowiada na pytanie zadawane przy `/health`: „czy mogę
# z tym żyć do jutra". Bez niej lista braków wygląda jednakowo groźnie, więc
# operator albo naprawia wszystko naraz, albo nie naprawia nic.
SKUTEK_BRAKU: dict[str, str] = {
    "DATABASE_URL": "BLOKUJE: nie ma gdzie zapisać ani czego pokazać",
    "ANTHROPIC_API_KEY": "klasyfikator nie ocenia postów — lecą do bazy surowe",
    "OPENAI_API_KEY": "klasyfikator nie ocenia postów — lecą do bazy surowe",
    "OPENAI_MODEL": "klasyfikator nie ma czym wołać — posty lecą do bazy surowe",
    "GEMINI_API_KEY": "klasyfikator nie ocenia postów — lecą do bazy surowe",
    "TELEGRAM_BOT_TOKEN": "alerty nie wychodzą; zlecenia widać w panelu",
    "TELEGRAM_CHAT_ID": "alerty nie wychodzą; zlecenia widać w panelu",
    "API_TOKEN": "panel dostaje 503 na danych (/health i /zdrowie działają)",
    "APIFY_API_TOKEN1": "fetcher nie pobierze nic nowego; zebrane zostaje",
}

# JEDYNY brak, po którym nie ma sensu ruszać. Reszta systemu opiera się o bazę:
# fetcher zapisuje, panel czyta, powiadomienia biorą stamtąd treść.
BLOKUJACE_START: tuple[str, ...] = ("DATABASE_URL",)

# Degradujące, których lista nie zależy od konfiguracji. Zmienne aktywnego
# providera dochodzą do nich osobno (`braki_degradujace`), bo które to są,
# wiadomo dopiero po przeczytaniu LLM_PROVIDER.
DEGRADUJACE_STALE: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "API_TOKEN", "APIFY_API_TOKEN1",
)


def _wartosc(nazwa: str) -> str:
    """Wartość SKUTECZNA zmiennej — ta, którą realnie posłuży się system.

    Najpierw stała tego modułu, bo ona niesie wartość domyślną z kodu: puste
    `CLASSIFIER_MODEL` w .env NIE jest brakiem, skoro model i tak ma nazwę.
    Pytanie brzmi „czy system ma czym działać", a nie „czy ktoś wpisał linijkę".

    Dopiero potem samo środowisko — dla zmiennych, które nie mają tu stałej
    (klucze Apify przychodzą ze wspólnego .env sales-core-engine).
    """
    wartosc = globals().get(nazwa)
    if isinstance(wartosc, str):
        return wartosc.strip()
    return _txt(nazwa)


def brakujace(*nazwy: str) -> list[str]:
    """Które z podanych zmiennych są puste. Pusta lista = komplet.

    Zwraca LISTĘ, nie bool — worker ma wypisać wszystkie braki naraz.
    """
    return [n for n in nazwy if not _wartosc(n)]


# ---------------------------------------------------------------------------
# PROVIDER MODELU — który klucz jest w tym przebiegu wymagany
# ---------------------------------------------------------------------------
def provider_llm(surowy: str | None = None) -> str:
    """Nazwa providera -> jedna z PROVIDERY. Nieznana degraduje do domyślnego.

    Degradacja, a nie wyjątek: literówka w .env nie może zatrzymać crona, a
    `PROVIDER_DOMYSLNY` jest jedynym providerem, którego zależność siedzi
    w requirements.txt. `services/llm.normalizuj_provider` woła to samo.
    """
    s = (LLM_PROVIDER if surowy is None else surowy).strip().lower()
    return s if s in ZMIENNE_PROVIDERA else PROVIDER_DOMYSLNY


def klucz_providera(provider: str | None = None) -> str:
    """Klucz API tego providera. Pusty = nieustawiony."""
    return _wartosc(ZMIENNE_PROVIDERA[provider_llm(provider)][0])


def model_providera(provider: str | None = None) -> str:
    """Nazwa modelu tego providera — po domyślnych z kodu. Pusta = nie podano."""
    return _wartosc(ZMIENNE_PROVIDERA[provider_llm(provider)][1])


def braki_providera(provider: str | None = None) -> list[str]:
    """Czego brakuje, żeby TEN provider ruszył. Pusta lista = komplet.

    Bez argumentu pyta o providera z .env — czyli o jedyny, który w tym
    przebiegu cokolwiek zrobi. Klucze pozostałych są opcjonalne i ich brak nie
    ma prawa się tu pojawić.

    NIE sprawdza pakietu SDK ani tego, czy klucz jest DOBRY — pierwsze umie
    `services/llm.problemy()` (to ono zna nazwy paczek), drugiego nie da się
    sprawdzić bez sieci (`scripts/test_llm.py`).
    """
    zmienna_klucza, zmienna_modelu = ZMIENNE_PROVIDERA[provider_llm(provider)]
    return brakujace(zmienna_klucza, zmienna_modelu)


def providery_z_kompletem() -> list[str]:
    """Providery, dla których klucz i nazwa modelu są ustawione.

    Do informacji, NIGDY do zatrzymania czegokolwiek: mówi tylko, jak szerokie
    będzie `scripts/porownaj_modele.py`.
    """
    return [p for p in PROVIDERY if not braki_providera(p)]


def opis_porownania() -> str:
    """Jedno zdanie o zasięgu porównania modeli — informacja, nie ostrzeżenie.

    MÓWI WPROST, CO POLICZYŁ. Ten moduł widzi .env i nic więcej, więc liczy
    klucze i nazwy modeli — nie pakiety SDK, o które pyta `llm.gotowe_providery()`.
    Bez tego dopisku obie liczby stoją w jednym raporcie i różnią się bez
    wyjaśnienia, a z takiej sprzeczności operator wyciąga jeden wniosek:
    że narzędziu nie można wierzyć.
    """
    return (f"porównanie modeli obejmie {len(providery_z_kompletem())} "
            f"z {len(PROVIDERY)} providerów (klucz i model w .env)")


# ---------------------------------------------------------------------------
# DWIE KLASY BRAKÓW — patrz nota nad OPIS_ZMIENNYCH
# ---------------------------------------------------------------------------
def braki_blokujace_start() -> list[str]:
    """Bez tego nie ma sensu ruszać. Pusta lista w 99% instalacji."""
    return brakujace(*BLOKUJACE_START)


def braki_degradujace() -> list[str]:
    """Braki, które wyłączają podsystem — i NIGDY nie są powodem zatrzymania.

    Klucz modelu wchodzi tu z aktywnego providera, nie z listy wszystkich:
    przy LLM_PROVIDER=openai pusty ANTHROPIC_API_KEY nie jest brakiem, tylko
    niewykorzystaną linijką w .env.
    """
    return brakujace(*ZMIENNE_PROVIDERA[provider_llm()], *DEGRADUJACE_STALE)


def stan_konfiguracji() -> dict:
    """Komplet odpowiedzi o konfiguracji: status, obie klasy braków, skutki.

    Jedno źródło dla `/health`, `/zdrowie` i linii startowej — żeby te trzy
    miejsca nie mogły powiedzieć o tej samej maszynie trzech różnych rzeczy.

    `niepelna_konfiguracja` przy samych brakach degradujących jest stanem
    POPRAWNYM, a nie awarią: system chodzi, tylko węziej.
    """
    blokujace = braki_blokujace_start()
    degradujace = braki_degradujace()
    return {
        "status": "ok" if not (blokujace or degradujace) else "niepelna_konfiguracja",
        "blokujace_start": blokujace,
        "degradujace": degradujace,
        # Nazwa zmiennej mówi, CZEGO nie ma; to zdanie mówi, co przez to nie
        # działa. Dopiero drugie pozwala zdecydować, czy naprawiać teraz.
        "skutki": {n: SKUTEK_BRAKU.get(n, "patrz .env.example")
                   for n in (*blokujace, *degradujace)},
        "porownanie_modeli": opis_porownania(),
    }


def wyjscie_bez_konfiguracji(kto: str, braki: list[str], strumien=None) -> int:
    """Wypisz, czego brakuje, i oddaj kod wyjścia dla CZYSTEGO zakończenia.

    Kod 0, nie 1 — i to jest świadome. Dla crona 0 znaczy "nie ma nic do
    zrobienia", a niezerowy kod to awaria, o której trzeba kogoś obudzić.
    Nieskonfigurowany system nie jest awarią: jest systemem, którego jeszcze nie
    włączono. Gdyby brak tokenu dawał kod 1, skrzynka operatora zapełniłaby się
    mailami od crona co 5 minut i realna awaria utonęłaby w szumie.

    WOŁAJ TO TYLKO NA BRAKACH, KTÓRE ODBIERAJĄ TEMU PROCESOWI CAŁĄ PRACĘ. Bot
    Telegrama bez tokenu nie ma czego robić i kończy słusznie; API bez klucza
    modelu traci JEDEN podsystem z kilku i kończyć się nie ma prawa. Zakończenie
    procesu, który miał jeszcze co robić, jest pod PM2 pętlą restartów ze statusem
    `errored`, a pod cronem mailem co pięć minut — objawem wyglądającym na awarię
    kodu tam, gdzie brakuje linijki w .env. Brak degradujący idzie do logu i do
    `/health`, nie do `sys.exit`.

    Użycie w workerze:
        braki = settings.braki_blokujace_start()
        if braki:
            return settings.wyjscie_bez_konfiguracji("classifier", braki)
    """
    out = strumien or sys.stderr
    print(f"[{kto}] Brak konfiguracji — kończę bez działania.", file=out)
    for n in braki:
        print(f"[{kto}]   {n} — {OPIS_ZMIENNYCH.get(n, 'patrz .env.example')}", file=out)
    print(f"[{kto}] Uzupełnij {BASE_DIR / '.env'} (wzór: .env.example) i uruchom ponownie.",
          file=out)
    return 0


def opis_srodowiska() -> str:
    """Jedna linia do logu startowego: co jest ustawione, BEZ wartości.

    Same nazwy i "tak/nie" — wartości to sekrety, a i tak interesuje nas tylko,
    czy klucz jest, gdy trzeba zdiagnozować "czemu nic nie przyszło".
    """
    stan = {
        "db": bool(DATABASE_URL),
        # Klucz TEGO providera, nie zawsze Anthropic. Wcześniej stało tu
        # `anthropic=BRAK` obok działającego OpenAI — linia startowa krzyczała
        # o kluczu, którego ten przebieg nie tknie.
        "klucz_modelu": bool(klucz_providera()),
        "telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "baza_geo": bool(BAZA_LAT or BAZA_LON),
        "api_token": bool(API_TOKEN),
    }
    # Model TEGO providera — z tego samego powodu co klucz wyżej.
    provider = provider_llm()
    model_llm = model_providera(provider) or "(brak)"
    return "[settings] " + ", ".join(
        f"{k}={'tak' if v else 'BRAK'}" for k, v in stan.items()
    ) + (f", cisza_nocna={CISZA_NOCNA_OD}-{CISZA_NOCNA_DO}"
         f", min_pewnosc={MIN_PEWNOSC}, limit_powiadomien={MAX_POWIADOMIEN_H}/h"
         f", max_dystans={MAX_DYSTANS_KM} km, max_wiek_posta={MAX_WIEK_POSTA_H} h"
         f", gate={GATE_TRYB}(prog {GATE_PROG})"
         # Widoczne w linii startowej, bo „czemu nie przyszedł alert o tym
         # koniu" jest pytaniem, na które inaczej nie ma odpowiedzi bez czytania
         # kodu. Zlecenie JEST w panelu niezależnie od tej wartości.
         f", alert_zwierzeta={'tak' if ALERT_ZWIERZETA else 'nie (tylko panel)'}"
         f", llm={provider}/{model_llm}"
         + (f"(json={OPENAI_JSON_MODE})" if provider == "openai" else "")
         # Literówka w LLM_PROVIDER degraduje po cichu do domyślnego — ma być
         # WIDOCZNA, bo inaczej operator czyta linię startową i widzi providera,
         # o którego nie prosił, bez śladu skąd się wziął.
         + (f"(LLM_PROVIDER={LLM_PROVIDER!r} nieznany)"
            if LLM_PROVIDER.strip().lower() != provider else "")
         + f", {opis_porownania()}"
         + f", budzet={POSTY_NA_DOBE} postow/dobe"
         f", sciezka_actora={SCIEZKA_ACTORA or 'z pomiaru'}"
         f", wspolny_apify={WSPOLNE_APIFY_ILE} zmiennych z {WSPOLNE_APIFY_SKAD}")


def raport_konfiguracji(strumien=None) -> int:
    """Braki OBIEMA klasami osobno, dla `check_setup.sh` i CLI tego modułu.

    Zawsze kod 0. Niepełna konfiguracja nie jest awarią diagnostyki — narzędzie
    od odpowiadania „czego brakuje" nie ma prawa samo wyglądać na zepsute.
    """
    out = strumien or sys.stdout
    stan = stan_konfiguracji()
    print(f"[settings] {stan['porownanie_modeli']} "
          f"({', '.join(providery_z_kompletem()) or 'brak — klasyfikator nie ruszy'})",
          file=out)

    if stan["blokujace_start"]:
        print("[settings] BLOKUJE START — bez tego nie ma sensu ruszać:", file=out)
        for n in stan["blokujace_start"]:
            print(f"[settings]   {n} — {OPIS_ZMIENNYCH.get(n, 'patrz .env.example')}", file=out)
    if stan["degradujace"]:
        # „Wstanie" mówi się tu wprost, bo pytanie brzmi zwykle „czy mogę
        # deployować" — a odpowiedź brzmi tak, tylko węziej.
        print("[settings] DEGRADUJE — system wstanie, te podsystemy nie:", file=out)
        for n in stan["degradujace"]:
            print(f"[settings]   {n} — {stan['skutki'][n]}", file=out)
    if stan["status"] == "ok":
        print("[settings] Komplet zmiennych ustawiony.", file=out)
    else:
        print(f"[settings] Uzupełnij {BASE_DIR / '.env'} (wzór: .env.example).", file=out)
    return 0


# Podgląd konfiguracji bez odpalania czegokolwiek:
#   python -m laweta_radar.config.settings
if __name__ == "__main__":
    print(opis_srodowiska())
    raise SystemExit(raport_konfiguracji(sys.stdout))
