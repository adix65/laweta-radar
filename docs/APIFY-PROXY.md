# Proxy do Apify — żeby pula kont nie wychodziła z jednego IP VPS-a

> Ten dokument jest przeniesiony z repo, w którym opisany tu mechanizm chodzi
> produkcyjnie. Sam mechanizm (`laweta_radar/workers/apify_proxy.py`) jest kopią
> 1:1 — zmieniły się tylko ścieżki pakietu i to, co wypisano niżej w sekcji
> „Czego tu nie ma".

## Pula jest WSPÓŁDZIELONA — przeczytaj to najpierw

Ten system **nie ma własnych kont Apify**. Stoi na tym samym VPS-ie co
sales-core-engine i korzysta z **tej samej puli kont oraz tych samych proxy**.
Klucze i konfigurację proxy `config/settings.py` dociąga z `.env` tamtego repo
(ścieżka: `SHARED_ENV_PATH`, domyślnie `/home/ubuntu/sales-core-engine/.env`).

Zakładanie drugiej puli byłoby działaniem **wprost przeciwnym** do celu tej
strony: dwa razy więcej kont z tego samego adresu to dwa razy mocniejszy sygnał
multi-accountingu, przy zerowym zysku.

Przepisywane są **wyłącznie** `APIFY_API_TOKEN*`, `APIFY_PROXY{N}`,
`APIFY_PROXY_URL(S)` i `APIFY_PROXY_REQUIRED`. Reszta tamtego pliku — jego
`DATABASE_URL`, jego `TELEGRAM_*` — jest ignorowana, bo zwykłe wczytanie obu
plików sprawiłoby, że laweta pisze do bazy sprzedażowej, a alerty o zleceniach
idą na czat handlowca. Pilnują tego testy w `laweta_radar/tests/test_config.py`.

> **Budżet też jest wspólny.** Każdy run lawety odejmuje kredyt tej samej puli.
> Dlatego fetcher rozdziela runy między grupy bandytą (`services/bandit.py`):
> run wydany na martwą grupę to nie tylko zmarnowany run, ale run **zabrany
> drugiemu systemowi**.

Sprawdzenie, co realnie zostało wczytane i skąd:

```bash
python -m laweta_radar.config.settings
python -m laweta_radar.workers.apify_keys
```

## Po co to jest

Pula darmowych kont Apify (`APIFY_API_TOKEN1..N`, rotacja w
`laweta_radar/workers/apify_keys.py`) to źródło kredytu na scrapery FB. Każde
konto to ~5 USD na miesiąc, więc puli robi się dużo — i cała siedzi na JEDNYM
adresie IP naszego VPS-a. Z punktu widzenia Apify wygląda to jak podręcznikowy multi-accounting:

- kilkadziesiąt kont łączy się z tego samego adresu,
- narzędzia, które dotykają **wszystkich** kont naraz (choćby odczyt salda),
  robią to w kilka sekund — idealnie skorelowane w czasie,
- konta były zakładane pod ten sam schemat użycia (ten sam actor, te same grupy).

Wystarczy, że Apify połączy kropki raz, i tracimy **całą pulę naraz**, nie jedno
konto. Ten mechanizm rozwiązuje dokładnie to: każde konto dostaje **swoje, stałe
wyjście do internetu**.

Uwaga na granicę odpowiedzialności: to proxy dotyczy **naszego ruchu do
`api.apify.com`**. Samo scrapowanie Facebooka robi Apify po swojej stronie (ich
infrastruktura, ich proxy) — tego nie zmieniamy i nie musimy.

## Zasada działania

`laweta_radar/workers/apify_proxy.py` liczy przypisanie **token → proxy** i oddaje
gotowego klienta HTTP. Trzy rzeczy są tu istotne:

1. **Lepkość.** Przypisanie jest deterministyczne (hash tokenu), więc to samo konto
   zawsze wychodzi z tego samego adresu — także po restarcie, deployu i po zmianie
   kolejności kluczy w `.env`. Konto, które co run loguje się z innego kraju, jest
   podejrzane bardziej niż konto siedzące spokojnie na jednym adresie.
2. **Minimalne przetasowania.** Rozkład po puli robi rendezvous hashing: dołożenie
   kolejnego proxy przenosi na nie ~1/N kont, a nie prawie wszystkie.
3. **Zero zmiany zachowania bez konfiguracji.** Pusty `.env` = wszystko działa
   (ruch wprost z VPS-a), tylko workery wypisują ostrzeżenie w logu.

Objęte jest każde miejsce, które gada z Apify — dziś to fetcher grup
(`laweta_radar/workers/fb_fetcher.py`), bo innego wejścia do Apify w tym repo
nie ma. Każdy nowy worker dotykający Apify ma korzystać z `client_for_token`,
a nie budować `httpx.Client` samodzielnie.

## Konfiguracja

Wszystko przez `.env` (opis skrócony jest też w `laweta_radar/.env.example`).
Priorytet od najwyższego:

### 1. Brama z sesją lepką — najlepsze przy dużej puli kont

```
APIFY_PROXY_URL=http://klient-session-{session}:haslo@gw.dostawca.com:7000
```

`{session}` podmieniamy na stały identyfikator wyliczony z tokenu
(`sha256(token)[:12]` — sam token nigdzie nie wycieka). Każde konto = inna sesja =
inny, ale **stały** adres wyjściowy. Jeden wpis obsługuje dowolnie wiele kont.

Dokładny format loginu bierzesz z panelu swojego dostawcy — my podmieniamy tylko
`{session}`. U typowych dostawców wygląda to jak
`user-country-pl-session-{session}` albo `customer-XYZ-sessid-{session}`.

> **Lepkość sesji ma u dostawcy swój czas życia.** My gwarantujemy tyle, że dane
> konto zawsze poprosi o TĘ SAMĄ sesję. Czy dostawca odda pod nią ten sam adres,
> zależy od jego TTL (u części to 10-30 minut, u części dokupujesz „sticky" na
> godziny lub dni). Scrapery chodzą często, więc przy krótkim TTL konto będzie
> zmieniać adres między runami — jeśli ci na tym zależy, wybierz taryfę z długim
> TTL albo sesją bezterminową. Sprawdzisz to `--check` puszczonym dwa razy
> w odstępie kilkudziesięciu minut.

> Brama **bez** `{session}` też zadziała, ale wtedy wszystkie konta idą przez jeden
> adres — problem „jednego IP" zostaje, zmienia się tylko adres. Worker to wykryje
> i ostrzeże w logu.

### 2. Pula proxy — lista adresów

```
APIFY_PROXY_URLS=http://user:haslo@a.dostawca:8000, http://user:haslo@b.dostawca:8000
```

Rozdzielać można przecinkiem, średnikiem albo nową linią. Jeśli dostawca daje sesje
lepkie w schemacie „port = sesja", wystarczy zakres portów:

```
APIFY_PROXY_URLS=http://user:haslo@gw.dostawca:10001-10100
```

(rozwinie się do 100 osobnych proxy).

> Pula rozdziela konta po hashu, więc **nie jest to przydział 1:1**. Nawet mając
> tyle adresów, ile kont, część adresów obsłuży dwa i więcej kont, a część zostanie
> nietknięta — tak działa rozrzut losowy. Dla anty-banu to bez znaczenia (2-3 konta
> na adres nikogo nie zastanawiają), ale jeśli chcesz **ściśle** jeden adres na
> konto, użyj bramy z `{session}` albo `APIFY_PROXY{N}`.
> `python -m laweta_radar.workers.apify_proxy` pokazuje realny rozkład
> („Najwięcej kont na jednym proxy").

### 3. Proxy przypisane wprost do klucza

```
APIFY_PROXY1=http://user:haslo@1.2.3.4:8000     # dla APIFY_API_TOKEN1
APIFY_PROXY2=http://user:haslo@5.6.7.8:8000     # dla APIFY_API_TOKEN2
```

Numer odpowiada numerowi tokenu. Klucz bez swojego wpisu spada na pulę/bramę.
Sensowne przy kilku kontach, nie przy stu.

> Jeśli używasz **wyłącznie** `APIFY_PROXY{N}` i kluczy jest więcej niż wpisów,
> reszta kont wyjdzie z gołego IP VPS-a. Worker to wyłapie i napisze wprost:
> `UWAGA: 97 z 100 kluczy NIE ma przypisanego proxy`. Dołóż wtedy wspólny
> `APIFY_PROXY_URLS`/`APIFY_PROXY_URL` jako podkładkę pod resztę.

### Znaki specjalne w haśle

Login i hasło w URL-u trzeba zakodować procentowo, jeśli zawierają `@ : / ? # %`.
Na przykład hasło `p@ss:word` zapisujesz jako `p%40ss%3Aword`. Niezakodowane `@`
rozjeżdża parsowanie adresu i ruch pójdzie nie tam, gdzie miał iść.

### 4. Pula z pliku — WYŁĄCZONA, nie włączaj

`APIFY_PROXY_POOL` jest **wyłączone i ma takie zostać.** W repo źródłowym pulę
wyłączono 2026-07-31 i decyzja przenosi się tutaj bez zmian.

Powód jest pomiarowy, nie ideologiczny. Odświeżenie zwracało **zero
zweryfikowanych adresów z 411 kandydatów**. W pliku zostawał jeden stary wpis,
odpowiadający w ~20% prób — a rendezvous hashing kierował przez ten jeden wpis
**komplet kont**. Scrapery zbierały timeouty na wywołaniach, za które Apify i tak
nalicza. **Pula, której nikt nie odświeża, jest gorsza niż jej brak**, a plik
leżący na dysku wygląda w logu identycznie w obu przypadkach.

Dlatego:

- nie ustawiaj `APIFY_PROXY_POOL=1`,
- nie dopisuj wpisu odświeżania do crona,
- generatora puli świadomie **nie przenieśliśmy** (patrz „Czego tu nie ma").

`APIFY_PROXY_POOL*` jest też celowo **wykluczone z dziedziczenia** ze wspólnego
`.env` — gdyby ktoś włączył pulę w sales-core-engine, nie włączy jej tutaj po
cichu.

Kod czytający plik puli został w module nietknięty (jest kopią 1:1) i zadziała,
jeśli ktoś świadomie ustawi `APIFY_PROXY_POOL=1` i `APIFY_PROXY_POOL_FILE`.
Format: `{"updated_at": "<ISO8601>", "proxies": [{"url": "...", "apify_ok": true}]}`,
brane są wyłącznie wpisy z `apify_ok: true`. Zanim to zrobisz, przeczytaj akapit
wyżej jeszcze raz.

**Uczciwie o darmowych adresach**, gdyby kusiło: żyją krótko, są współdzielone
przez tysiące ludzi i część jest już spalona na popularnych serwisach. Psują też
lepkość — gdy adres umiera, konto przenosi się na inne. Bywa, że darmowe proxy
jest gorsze niż czysty VPS.

### Bezpiecznik

```
APIFY_PROXY_REQUIRED=1
```

Bez działającego proxy workery Apify kończą **czysto** (komunikat + wyjście),
zamiast wychodzić z gołego IP VPS-a. Domyślnie `0`. Zły URL proxy zatrzymuje run
**zawsze**, niezależnie od tej flagi: cichy fallback na bezpośrednie wyjście byłby
dokładnie tym, czego chcemy uniknąć.

## Weryfikacja (zrób to po konfiguracji)

```bash
source laweta_radar/scripts/env-shell.sh

python -m laweta_radar.workers.apify_proxy          # samo przypisanie, bez sieci
python -m laweta_radar.workers.apify_proxy --check   # REALNY adres wyjściowy per konto
python -m laweta_radar.workers.apify_proxy --check --limit 10   # szybki test przy dużej puli
```

`--check` puszcza po jednym zapytaniu na konto **przez jego proxy** i pokazuje, z
jakiego IP faktycznie wychodzi. Na końcu podsumowanie: ile różnych adresów, który
adres jest najgęstszy i — najważniejsze — czy któreś konto nie wychodzi mimo
wszystko z gołego IP VPS-a.

Całość razem z resztą konfiguracji: `bash laweta_radar/scripts/check_setup.sh`.

Hasła do proxy nie trafiają do logów: w logach workerów widać tylko `host:port`, a
w CLI login z zamaskowanym hasłem (`user:***@host:port`).

`--check` obejmuje **wszystkie** klucze `APIFY_API_TOKEN*`, także te za dziurą w
numeracji (jest `...TOKEN40` i `...TOKEN42`, brakuje `41`). Rotator takich kluczy
nie widzi, ale dziura to literówka, którą ktoś kiedyś naprawi — i wtedy klucz
nagle zacznie być używany. Weryfikacja tylko widocznych dawałaby zielone światło
kontom, które po naprawie wyjdą z gołego IP VPS-a.

Kod wyjścia `--check` to 0 tylko wtedy, gdy wszystko się udało i dało się porównać
z adresem bezpośrednim. Jeśli adresu bezpośredniego nie da się ustalić, wyciek nie
jest sprawdzany i narzędzie mówi o tym wprost („WERYFIKACJA NIEPEŁNA").

## Jakie proxy kupić

Ruch idzie do `api.apify.com`, nie do Facebooka — nie potrzebujemy więc drogich
proxy „pod antybota". Potrzebujemy **wielu różnych, stabilnych adresów**.

- **Statyczne datacenter / ISP, rozliczane od adresu (flat, bez limitu transferu)** —
  pragmatyczny wybór. Scrapery ściągają treść postów, więc transfer nie jest
  symboliczny, a przy rozliczeniu od adresu jest to nieistotne. 5-20 adresów
  rozkłada pulę kont na tyle, że korelacja „wszyscy z jednego IP" znika.
- **Residential z sesją lepką (rozliczane za GB)** — bezpieczniejsze (adresy
  wyglądają jak domowe łącza) i daje **osobny adres na każde konto** jednym wpisem
  `{session}`. Tu trzeba pilnować transferu: scrapery potrafią przejeść gigabajty.
- **Mobilne** — przesada do tego zastosowania.

`socks5://` też działa, ale wymaga doinstalowania: `pip install "httpx[socks]"`.
Dla `http://` / `https://` nie trzeba nic dokładać. `socks5h://` **nie** przejdzie —
httpx zna wyłącznie `socks5`, więc odrzucamy go od razu w konfiguracji, zamiast
pozwalać, żeby run wywalił się dopiero przy pierwszym wołaniu Apify.

Jeśli dostawca daje endpoint `https://`, wybierz go zamiast `http://`: przy
`http://` nagłówek `Proxy-Authorization` (czyli twój login i hasło do proxy) leci
do bramy otwartym tekstem. Sam ruch do Apify jest szyfrowany w obu przypadkach
(idzie tunelem CONNECT), chodzi wyłącznie o dane logowania do proxy.

## Co się dzieje, gdy proxy padnie

Awaria proxy to dla rotatora kluczy błąd **przejściowy**
(`laweta_radar/workers/apify_keys.py`): trzy próby tym samym kluczem, co 3 sekundy.
Świadomie sprawdzamy to **przed** analizą treści komunikatu — wyczerpanie kredytu
Apify przychodzi zawsze jako odpowiedź HTTP, nigdy jako błąd transportu, a komunikat
od dostawcy proxy potrafi nieść słowo „limit" czy „quota" (wyczerpany transfer).
Bez tej kolejności zdrowe klucze byłyby po kolei oznaczane jako puste, aż do
`AllKeysExhausted` — czyli awaria proxy zjadłaby całą pulę kont w logach.

Jeśli po ponowieniach klucz nadal nie przechodzi, a proxy **jest** skonfigurowane,
warto pozwolić fetcherowi spróbować **kilku kolejnych kluczy** — każdy ma inne
wyjście, więc jedno zdechłe proxy nie kończy całego runu. Robi to parametr
`transient_key_switches` w `KeyRotator` (rozsądnie: 2). Limit jest po to, żeby
prawdziwa awaria łącza nadal kończyła się szybko i prawdziwym błędem transportu,
a nie godziną ponowień po stu kluczach. Bez skonfigurowanego proxy zostaw 0:
awaria sieci jest wtedy globalna, więc nie ma po co przeskakiwać.

Wołający, który sam steruje kolejnością prób, ma do dyspozycji
`proxies_for_token()` — cały ranking proxy dla danego tokenu, od pierwszego
wyboru po zapasowe. Kolejność jest deterministyczna, więc konto, które spadło na
adres zapasowy, będzie tam wracać, dopóki pula się nie zmieni.

Gdy nie wiadomo, przez co realnie wychodzimy:

```bash
python -m laweta_radar.workers.apify_proxy --check --limit 10
```

## Czego tu nie ma (w stosunku do repo źródłowego)

Świadome pominięcia, żeby nikt ich nie szukał:

- **Generatora darmowej puli proxy.** Moduł, który pobierał publiczną listę,
  weryfikował adresy i zapisywał `.apify_proxy_pool.json`, nie został przeniesiony —
  i nie ma być. Sama pula jest wyłączona (sekcja 4), więc generator bez niej nie ma
  zastosowania, a jego obecność kusiłaby do odtworzenia wpisu w cronie, który
  w repo źródłowym został świadomie usunięty.
- **Monitora kredytów Apify.** Osobny worker odpytujący saldo wszystkich kont
  (i cała rodzina `APIFY_CREDITS_*`) nie został przeniesiony. Gdy się pojawi, ma
  korzystać z `proxies_for_token()` i **nie** wychodzić bezpośrednio: poll
  wszystkich kont naraz z jednego adresu to dokładnie ten sygnał, przed którym
  broni cała ta strona.

## Czego to NIE załatwia

- **Kont zakładanych z jednego IP.** Proxy pilnuje ruchu *bieżącego*. Jeśli
  wszystkie konta były rejestrowane z adresu VPS-a, ten ślad już u Apify jest —
  nowe konta warto zakładać z tego samego wyjścia, którego potem będą używać.
- **Reszty sygnałów.** Identyczny schemat użycia (ten sam actor, te same grupy, ten
  sam rytm) też koreluje konta. Proxy zdejmuje najmocniejszy sygnał, nie wszystkie.
- **Regulaminu Apify.** Pula darmowych kont to obejście limitu darmowego planu i
  Apify ma prawo ją ubić niezależnie od adresów. Przy stabilnym wolumenie płatny
  plan jest po prostu przewidywalny — proxy kupuje czas, nie gwarancję.
