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
4. **Wyrównanie PO hashu.** Sam rendezvous hashing rozkłada konta po puli tylko
   STATYSTYCZNIE równo (przy puli wielkości równej liczbie kluczy część adresów
   dostaje dwa-trzy konta, część żadnego). `apify_proxy._wyrownaj_przypisania`
   (wołane, gdy `run()` policzy konfigurację ze WSZYSTKIMI żywymi kluczami tego
   przebiegu) dokłada drugi przebieg: konto, którego pierwszy wybór jest już
   zajęty, dostaje najlepiej dopasowany WOLNY adres zamiast dzielić się z kimś
   innym. Przy puli ≥ liczba kluczy każde konto kończy z WŁASNYM adresem.
   Przypisanie zostaje stabilne dla tego samego zestawu (klucze, pula) —
   zmienia się wyłącznie, gdy któryś z tych zbiorów faktycznie się zmieni.

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

> Sam rendezvous hashing rozdziela konta po puli tylko STATYSTYCZNIE równo —
> zobacz **wyrównanie PO hashu** (punkt 4 wyżej): każde miejsce, które zna
> PEŁNĄ listę kluczy tego przebiegu (`run()`, `preflight`, `/limity`, CLI
> `python -m laweta_radar.workers.apify_proxy`), dokłada drugi przebieg i przy
> puli ≥ liczba kluczy każde konto kończy z WŁASNYM, niedzielonym adresem —
> bez tego byłby to zwykły rozrzut losowy (część adresów po dwa-trzy konta,
> część nietkniętych). Gdyby mimo to trzeba było wyjść poza pulę (mniej
> adresów niż kluczy) albo chcesz przypisania niezależnego od reszty zestawu
> kluczy, użyj bramy z `{session}` albo `APIFY_PROXY{N}`.
> `python -m laweta_radar.workers.apify_proxy` pokazuje realny rozkład
> („Najwięcej kont na jednym proxy") — liczony już PO wyrównaniu.

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

### 4. Pula z pliku — darmowa, odświeżana z WIELU źródeł

`APIFY_PROXY_POOL` jest **domyślnie wyłączone** — włączasz je świadomie, PO
zobaczeniu, ile adresów realnie przeżywa weryfikację (patrz generator niżej).

Pierwszy pomiar w tym repo (2026-07-31, **jedno** źródło) wyszedł źle: **zero
zweryfikowanych adresów z 411 kandydatów**, a jeden stary wpis, który został
w pliku, przejmował przez rendezvous hashing **komplet kont** i zamieniał runy
w timeouty — za które Apify i tak nalicza. Wniosek nie brzmiał „darmowe proxy
nie działa" — brzmiał: **pula, której nikt nie odświeża, jest gorsza niż jej
brak**, i **jedno źródło daje za mało kandydatów, żeby ten pomiar w ogóle miał
sens**. Generator dziś pobiera z **dziesięciu** źródeł naraz i weryfikuje
etapowo — patrz niżej.

`APIFY_PROXY_POOL*` jest celowo **wykluczone z dziedziczenia** ze wspólnego
`.env` — gdyby ktoś włączył pulę w sales-core-engine, nie włączy jej tutaj po
cichu.

Format pliku: `{"updated_at": "<ISO8601>", "proxies": [{"url": "...",
"apify_ok": true, "zrodlo": "...", "czas_ms": 842, "passy_pod_rzad": 3}]}` —
`workers/apify_proxy.py` bierze wyłącznie wpisy z `apify_ok: true` i czyta
`url`; `zrodlo`/`czas_ms`/`passy_pod_rzad` to RANKING do wglądu (patrz niżej),
nieużywany przez samo przypisanie token→proxy.

#### Generator: `scripts/odswiez_proxy.py` — dziesięć źródeł, weryfikacja etapowa

Zdanie „pula, której nikt nie odświeża, jest gorsza niż jej brak" ma drugą
połowę: **pulę odświeżaną da się w ogóle rozważać, jeśli ma z czego wybierać.**
Generator pobiera z listy zdefiniowanej w `laweta_radar/config/zrodla_proxy.py`
(proxifly, TheSpeedX, monosans, jetkai, ShiftyTR, roosterkid, zloi-user —
protokoły + kraje, po jednym pliku na wpis), scala i weryfikuje.

```bash
python laweta_radar/scripts/odswiez_proxy.py --sucho        # plan, bez sieci
python laweta_radar/scripts/odswiez_proxy.py                # pełne odświeżenie
python laweta_radar/scripts/odswiez_proxy.py --limit 2000   # szybciej, na próbę
python laweta_radar/scripts/odswiez_proxy.py --tylko-pula   # SZYBKA KONTROLA (patrz cron niżej)
```

**Każde źródło jest niezależne.** Awaria jednego (404, zmieniona struktura
repo, timeout) NIE przerywa pobierania pozostałych — log pokazuje, ile
kandydatów dało każde źródło i ile z nich przeszło. Repozytoria proxy
zmieniają strukturę i znikają (patrz komentarz przy `mmpx12` w
`config/zrodla_proxy.py` — usunięty po weryfikacji, nie zgadywany), więc
literówkę albo martwe źródło widać jednym spojrzeniem w logu, a naprawia się
jedną linią w pliku źródeł, bez dotykania logiki.

**Skala zmienia wąskie gardło.** Dziesięć źródeł to rząd 20-50 tysięcy
kandydatów po deduplikacji (po `host:port`, niezależnie od protokołu i
źródła) — pełna weryfikacja WSZYSTKICH trwałaby godziny. Dlatego idzie w
**trzech etapach**, od najtańszego:

1. **Filtr formalny** (bez sieci) — poprawny `host:port`, odrzucenie adresów
   prywatnych (10.x/192.168.x/127.x i pokrewne), dedup.
2. **TCP connect** (`PROXY_CHECK_PARALLEL_TCP`, domyślnie 250 naraz, timeout
   2 s) — goły connect, bez TLS i HTTP, odsiewa 80-90% martwych adresów za
   ułamek kosztu etapu 3.
3. **Cztery pełne testy** (`PROXY_CHECK_PARALLEL_HTTP`, domyślnie 32 naraz) —
   dokładnie te z sekcji „Weryfikacja przed dopuszczeniem do puli" niżej, na
   tym, co przeżyło etap 2. **PRZERYWA**, gdy zbierze `DOCELOWA_LICZBA_PROXY`
   (domyślnie 3x liczba kluczy `APIFY_API_TOKEN*`) zaakceptowanych adresów —
   reszta kandydatów zostaje NIEPRZETESTOWANA, bo nie ma sensu sprawdzać
   dziesiątek tysięcy adresów, żeby użyć czterdziestu.

Kolejność kandydatów jest **losowana przy każdym odświeżeniu** (po dedupie,
przed etapem 2) — inaczej zawsze przechodziłyby te same adresy z początku
listy pierwszego źródła, a pula byłaby ciągle ta sama mimo dziesięciu źródeł.

**Ranking, nie goła lista.** Do pliku, obok adresu, trafia `zrodlo` (które
źródło go dało — po tygodniu widać, które źródła warto trzymać), `czas_ms`
(czas ostatniej odpowiedzi) i `passy_pod_rzad` (ile PEŁNYCH odświeżeń z rzędu
przeszedł, dziedziczone z poprzedniego pliku — adres, który wypadł choć raz,
wraca z passą 1). Plik jest zapisany posortowany od najlepszego. Samo
przypisanie token→proxy nadal liczy czysty rendezvous hashing (kolejność w
pliku go nie rusza) — ranking jest do wglądu operatora, nie do algorytmu.

**Zrób najpierw jeden przebieg i spójrz na liczbę.** Zero zweryfikowanych
adresów nie jest wynikiem, który generator unieważnia — jest wynikiem, który
POKAZUJE, zamiast pozwolić mu gnić w pliku. Jeśli u ciebie wyjdzie zero,
odpowiedź brzmi: płatne proxy (`APIFY_PROXY_URLS` / `APIFY_PROXY_URL`), nie
częstszy cron.

**Cron — dwupoziomowy, instaluje się SAM** (`scripts/setup_cron.sh`, wołany
automatycznie przez `setup.sh`/`update.sh`, gdy `laweta_radar/.env` ma
`APIFY_PROXY_POOL=1`):

```cron
0 */2 * * *   cd /home/ubuntu/laweta-radar && ./venv/bin/python laweta_radar/scripts/odswiez_proxy.py             >> /var/log/laweta/proxy.log 2>&1
*/15 * * * *  cd /home/ubuntu/laweta-radar && ./venv/bin/python laweta_radar/scripts/odswiez_proxy.py --tylko-pula >> /var/log/laweta/proxy.log 2>&1
```

Darmowe proxy padają w **minutach**, nie godzinach — sam cykl co 2h zostawiałby
konta na martwych adresach przez większość tego czasu. `--tylko-pula`
(SZYBKA KONTROLA) sprawdza WYŁĄCZNIE adresy już w puli (tanio — bez
pobierania źródeł), wyrzuca martwe i, jeśli po czyszczeniu zostało mniej niż
liczba kluczy, odpala PEŁNE odświeżenie od razu, nie czekając na następny
cykl dwugodzinny. Instalacja jest idempotentna (`bash
laweta_radar/scripts/setup_cron.sh`, zdjęcie: `--usun`) — puszczona drugi raz
podmienia stare wpisy zamiast dokładać kopie.

Dwa zachowania, które są tu decyzją, a nie szczegółem (bez zmian względem
wersji jednożródłowej):

- **Brak sieci NIE czyści puli.** Nieudane pobranie WSZYSTKICH źródeł
  zostawia stary plik nietknięty i kończy kodem 1. Stara pula jest zła, ale
  pusta jest gorsza, gdy powodem jest zerwane łącze, a nie martwe adresy.
- **Zero działających adresów zapisuje pustą pulę**, jawnie, i też kończy
  kodem 1. Pusta pula przy `APIFY_PROXY_REQUIRED=1` zatrzymuje zbieranie,
  zamiast puścić komplet kont przez jeden przeżyty wpis.

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

**Wyczerpanie ŻYWYCH proxy** (wszystkie adresy puli naraz w kwarantannie) to
INNY przypadek niż pojedynczy padnięty adres — ten obsługuje samoleczenie
niżej. Przy `APIFY_PROXY_REQUIRED=1` fetcher sprawdza to na starcie przebiegu
(`apify_proxy.zywe_proxy_w_puli`) i, jeśli wynik to zero, **przerywa PRZED
pierwszym wywołaniem Apify** oraz wysyła alert na Telegram
(`_alert_pula_proxy_wyczerpana`) — zamiast puszczać każdy klucz po kolei przez
padnięte adresy, aż wyczerpie limit prób samoleczenia na każdym z nich.

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

Awaria proxy to dla rotatora kluczy `STATUS_BLAD_SIECI`
(`laweta_radar/workers/apify_keys.classify_apify_error`) — jeden z **czterech**
rozłącznych stanów, na jakie rozbity jest dawny worek „wyczerpany":

| Stan | Sygnał | Reakcja |
|------|--------|---------|
| `STATUS_KLUCZ_MARTWY` | 401/403 albo „token/user-not-found" | klucz WYPADA NA STAŁE (zwykle ban) |
| `STATUS_KREDYT_WYCZERPANY` | 402 albo usage/limit/exceeded/credit/quota | pomiń w TYM przebiegu, wraca 1. dnia miesiąca |
| `STATUS_RATE_LIMIT` | 429 | backoff 5/15/45 s, TEN SAM klucz |
| `STATUS_BLAD_SIECI` | timeout/sieć/proxy/5xx | ponów TYM SAMYM kluczem |

Rozróżnienie martwy/wyczerpany jest celowe: „wyczerpany kredyt" system naprawia
sam (miesięczny reset), „martwy klucz" nigdy nie wróci bez człowieka. Zlepienie
ich w jeden worek (jak wcześniej) chowa alarm w szumie normalnego resetu —
dokładnie to się stało, gdy pięć kont padło naraz z `user-or-token-not-found`
i rotator to zaraportował jako zwykłe „wyczerpane".

Stan każdego klucza żyje w tabeli `zasoby_apify` (migracja `0012`), NIE w pliku
— plik nie przeżywa równoległych przebiegów. `apify_keys.klucze_zywe()` filtruje
martwe i tegomiesięcznie-wyczerpane klucze PRZED rotacją, w każdym przebiegu.

Świadomie sprawdzamy klasyfikację **przed** analizą treści komunikatu —
wyczerpanie kredytu Apify przychodzi zawsze jako odpowiedź HTTP, nigdy jako
błąd transportu, a komunikat od dostawcy proxy potrafi nieść słowo „limit" czy
„quota" (wyczerpany transfer). Bez tej kolejności zdrowe klucze byłyby po
kolei oznaczane jako martwe/wyczerpane, aż do `AllKeysExhausted`.

### Samoleczenie — kolejne proxy i kwarantanna

Przy `STATUS_BLAD_SIECI` fetcher (`_apify_run_group_samoleczaca` w
`workers/fb_fetcher.py`) próbuje TEGO SAMEGO klucza z KOLEJNYM proxy — max
3 razy — zamiast od razu przeskakiwać na inny klucz. Padnięty adres ląduje w
kwarantannie na 30 minut (`apify_proxy.oznacz_kwarantanna`, tabela
`zasoby_apify_proxy`) i po tym czasie wraca do **weryfikacji** (test „dochodzi
do Apify" niżej), nie prosto do puli. Pierwsza próba idzie zwykłą, lepką
ścieżką bez dotykania bazy — baza wchodzi do gry dopiero po pierwszej awarii,
żeby happy path (99% wywołań) nie płacił za nic.

**Eskalacja.** Trzy awarie Z RZĘDU (licznik `ile_bledow` zerowany wyłącznie
przez udaną ponowną weryfikację, `apify_proxy.oznacz_aktywne`) wydłużają
kwarantannę z 30 minut do **doby** (`KWARANTANNA_ESKALACJA_PROG` /
`KWARANTANNA_ESKALACJA_MIN`) — adres, który wraca za pół godziny tylko po to,
żeby paść znowu, marnuje cykle weryfikacji bez szans na co innego. Darmowe
adresy bywają chwilowo przeciążone (stąd 30 minut na start, nie od razu doba),
ale trzy awarie z rzędu to już wzorzec, nie przypadek.

Alternatywnie, `transient_key_switches` w `KeyRotator` (rozsądnie: 2) przerzuca
na **kolejny klucz** po kilku nieudanych próbach transportu tego samego —
sensowne, gdy samoleczenie proxy per klucz akurat nie pomogło. Bez
skonfigurowanego proxy zostaw 0: awaria sieci jest wtedy globalna.

Wołający, który sam steruje kolejnością prób, ma do dyspozycji
`proxies_for_token()` — cały ranking proxy dla danego tokenu — oraz
`proxy_zywy_dla_tokenu()`, który dodatkowo POMIJA adresy aktualnie w
kwarantannie. Kolejność jest deterministyczna, więc konto, które spadło na
adres zapasowy, będzie tam wracać, dopóki pula się nie zmieni.

Gdy nie wiadomo, przez co realnie wychodzimy:

```bash
python -m laweta_radar.workers.apify_proxy --check --limit 10
```

### Weryfikacja przed dopuszczeniem do puli — cztery testy

`apify_proxy.weryfikuj_proxy()` / `zweryfikuj_pule()` sprawdzają KAŻDY adres
czterema testami, zanim uzna się go za użyteczny — adres, który „działa"
(odpowiada na ping), potrafi być bezużyteczny na trzy różne sposoby:

  a) **odpowiada w ogóle** — proxy żyje;
  b) **NIE przepuszcza gołego IP VPS-a** — inaczej to nie jest proxy, tylko
     przezroczysty pass-through, i problem „jednego IP" zostaje;
  c) **nie dubluje adresu innego proxy z puli** — dwa „różne" wpisy prowadzące
     do jednego adresu to jedno wyjście, nie dwa;
  d) **DOCHODZI do api.apify.com po HTTPS** — CONNECT na 443, bez tokenu ma
     wrócić 401 od Apify (sukces: dowodzi poprawnego TLS-handshake'u), nie
     błąd połączenia. To jest test, na którym kończy się większość darmowych
     list — patrz `scripts/odswiez_proxy.py` i pomiar 0 z 411 wyżej.

Test (d) jest pomijany, gdy (a) albo (b) już padły — adres bezużyteczny na
pierwszym z dwóch pierwszych testów nie potrzebuje trzeciego zapytania.

## Czego tu nie ma (w stosunku do repo źródłowego)

Świadome pominięcia, żeby nikt ich nie szukał:

- ~~**Generatora darmowej puli proxy.**~~ Był tu pominięty świadomie; wrócił jako
  `scripts/odswiez_proxy.py` (sekcja 4), bo pominięcie zostawiało tylko gorszą
  z dwóch opcji: pulę bez odświeżania. Od pierwszej wersji (jedno źródło, 0 z
  411) urósł do dziesięciu źródeł (`config/zrodla_proxy.py`) z etapową
  weryfikacją i cronem dwupoziomowym instalującym się samemu. Pula nadal jest
  **domyślnie wyłączona** — włączasz ją świadomie, z liczbą działających
  adresów przed oczami, a nie na wiarę.
- ~~**Monitora kredytów Apify.**~~ Był tu pominięty świadomie z tego samego
  powodu co darmowa pula: poll wszystkich kont naraz z jednego adresu to
  dokładnie ten sygnał multi-accountingu, przed którym broni cała ta strona.
  Wrócił jako komenda Telegrama `/limity` (`laweta_radar/workers/bot.py` +
  `apify_credits.pula_stanu`), bo operator bez niej musiał logować się na
  VPS, żeby sprawdzić, czy pula żyje. Trzy różnice wobec worka z crona, które
  czynią to bezpiecznym:
    1. **Każde konto WYCHODZI PRZEZ SWOJE proxy** (`client_for_token` per
       token) — pięć kont odpytanych "naraz" to i tak pięć różnych adresów
       wyjściowych, nie jeden.
    2. **Wyzwala CZŁOWIEK, na żądanie.** Nie ma crona odpytującego w kółko —
       jest komenda, którą ktoś wpisuje, kiedy chce wiedzieć.
    3. **Cache 5 minut.** Kilka `/limity` pod rząd (albo alias `/limityapi`)
       oddaje ten sam wynik zamiast generować kolejną serię zapytań.

  `apify_credits.py` ma teraz DWA wejścia: `saldo()` — odczyt **pojedynczego**
  konta na żądanie pomiaru (`scripts/pomiar_actora.py`, licznik przed serią
  i po niej) — oraz `stan_konta()`/`pula_stanu()` — trzy ROZŁĄCZNE stany konta
  (saldo znane / saldo nieznane / klucz martwy) dla `/limity`. Rozróżnienie
  jest celowe: konto, które odpowiada na `/users/me`, ale nie na
  `/users/me/limits` (typowe dla darmowych kont), jest SPRAWNE — tylko nie
  podaje salda, i nie wolno tego pokazać jako błędu.

## Czego to NIE załatwia

- **Kont zakładanych z jednego IP.** Proxy pilnuje ruchu *bieżącego*. Jeśli
  wszystkie konta były rejestrowane z adresu VPS-a, ten ślad już u Apify jest —
  nowe konta warto zakładać z tego samego wyjścia, którego potem będą używać.
- **Reszty sygnałów.** Identyczny schemat użycia (ten sam actor, te same grupy, ten
  sam rytm) też koreluje konta. Proxy zdejmuje najmocniejszy sygnał, nie wszystkie.
- **Regulaminu Apify.** Pula darmowych kont to obejście limitu darmowego planu i
  Apify ma prawo ją ubić niezależnie od adresów. Przy stabilnym wolumenie płatny
  plan jest po prostu przewidywalny — proxy kupuje czas, nie gwarancję.
