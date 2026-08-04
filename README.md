# Laweta Radar

Monitoruje grupy na Facebooku pod kątem zleceń dla lawety (pomoc drogowa,
transport aut) i wysyła je operatorowi na telefon w kilka minut od publikacji
posta.

**Cała wartość tego systemu to czas reakcji.** Zlecenie na lawetę wygrywa ten, kto
odpisze pierwszy — post sprzed dwóch godzin jest wart tyle, co żaden. Dlatego
pipeline jest zbudowany wokół jednej liczby (minuty od publikacji do alertu), a nie
wokół kompletności: lepiej przegapić jedno zlecenie niż dowieźć wszystkie z
półgodzinnym opóźnieniem.

## Jak to działa

```
grupy FB ──> fb_fetcher ──> gate ──> classifier ──> geo ──> Telegram
            (Apify)      (filtr    (Claude:      (dystans   (operator
                          słowny)   zlecenie?)    od bazy)    odpisuje
                                                              RĘCZNIE)
```

Każdy krok istnieje po to, żeby następny dostał mniej roboty:

- **fb_fetcher** — pobiera najnowsze posty ze zweryfikowanych grup przez Apify.
  Klucze rotują się po **wspólnej z sales-core-engine** puli kont, ruch wychodzi
  przez proxy per konto. Budżet runów rozdziela między grupy bandyta
  (`services/bandit.py`), bo kredyt jest wspólny z drugim systemem.
- **gate** — tani filtr słowny **przed** modelem. Bez niego płacilibyśmy Claude'owi
  za każdy post o sprzedaży felg.
- **classifier** — Claude decyduje, czy to realne zlecenie, i wyciąga z posta to,
  co operator musi wiedzieć, zanim kliknie.
- **geo** — odrzuca zdarzenia poza zasięgiem. Zlecenie z drugiego końca Polski jest
  gorsze niż brak zlecenia: zjada uwagę operatora, który i tak po nie nie pojedzie.
- **Telegram** — jedyny kanał dowozu. Alert niesie link do posta; odpowiada
  **człowiek**, z własnego konta.

### Stan repo

Ten commit to **szkielet + przeniesiona infrastruktura Apify**. Działa już:
konfiguracja (w tym dociąganie wspólnej puli Apify), rotacja kluczy, proxy,
bandyta, transport Telegrama, API diagnostyczne, migracja tabeli `posty`. Kroki
pipeline'u (`workers/gate.py`, `workers/fb_fetcher.py`, `workers/classifier.py`,
`services/geo.py`) dochodzą w kolejnych krokach — dopóki ich nie ma, system nic
nie pobiera i nic nie wysyła, ale wstaje czysto i mówi, czego mu brakuje.

## Struktura

```
laweta_radar/
  workers/
    apify_keys.py      # rotacja puli kluczy APIFY_API_TOKEN1..N     [kopia 1:1]
    apify_proxy.py     # przypisanie token->proxy, sesje lepkie      [kopia 1:1]
    apify_credits.py   # saldo miesięcznego kredytu konta (do pomiaru kosztu)
  services/
    telegram_notify.py # transport alertów (sam _send/_escape/_truncate)
    bandit.py          # Thompson Sampling — rozdział budżetu runów Apify
  config/
    settings.py        # jedyne miejsce czytające środowisko
    shared_env.py      # dociąga klucze Apify ze WSPÓLNEGO .env sales-core-engine
    groups.py          # lista grup FB — dane, nie kod
  api/
    main.py            # FastAPI: /health
    migrations/        # SQL odpalany RĘCZNIE, nigdy z workera
  scripts/             # env-shell, migrate, start_api, check_setup
    pomiar_actora.py   # JEDNORAZOWA diagnostyka actora — nie część pipeline'u
  tests/               # testy offline (bez sieci i bez bazy)
  .env.example
  requirements.txt
docs/APIFY-PROXY.md    # po co proxy i jak je skonfigurować
docs/POMIAR-ACTORA.md  # co actor realnie robi i ile kosztuje (wynik pomiaru)
```

Moduły oznaczone `[kopia 1:1]` pochodzą z repo, w którym chodzą produkcyjnie.
Zmieniły się w nich **wyłącznie** ścieżki pakietu i komunikaty wskazujące na moduły
nieprzeniesione tutaj — logika jest nietknięta i pilnują tego testy w
`laweta_radar/tests/`.

`bandit.py` to przypadek osobny: przeniesiona jest **matematyka Thompson Samplingu
co do stałej** (sprawdza to `test_bandit.py`, licząc posteriory wprost ze wzorów
z oryginału), ale źródło danych — tabela `sales_techniques_log` i katalog technik
sprzedażowych — zostało wycięte jako domena. Szczegóły w docstringu modułu.

## Infrastruktura Apify jest WSPÓŁDZIELONA

Ten system **nie ma własnych kont Apify**. Stoi na tym samym VPS-ie co
sales-core-engine i korzysta z **tej samej puli kont oraz tych samych proxy** —
klucze dociągane są z `.env` tamtego repo (`SHARED_ENV_PATH`, domyślnie
`/home/ubuntu/sales-core-engine/.env`). Druga pula kont dałaby tylko dwa razy
mocniejszy sygnał multi-accountingu przy zerowym zysku.

Przepisywane są **wyłącznie** `APIFY_API_TOKEN*`, `APIFY_PROXY{N}`,
`APIFY_PROXY_URL(S)` i `APIFY_PROXY_REQUIRED`. Reszta tamtego pliku — jego
`DATABASE_URL`, jego `TELEGRAM_*` — jest świadomie ignorowana. Bez tego
ograniczenia laweta bez własnego `DATABASE_URL` po cichu pisałaby do bazy
sprzedażowej, a alerty o zleceniach szłyby na czat handlowca. Testy
(`test_config.py`) pilnują, że nic spoza listy `APIFY_*` nie przechodzi.

**Baza danych pozostaje osobna** (`laweta`) — współdzielimy tylko wyjście do
Apify, nie dane.

> **Budżet Apify jest wspólny.** Każdy run lawety odejmuje kredyt tej samej puli,
> z której korzysta sales-core-engine. Dlatego fetcher rozdziela runy między grupy
> bandytą (`services/bandit.py`): run wydany na martwą grupę to run **zabrany
> drugiemu systemowi**, a nie tylko zmarnowany.

**Darmowa pula proxy zostaje wyłączona.** `APIFY_PROXY_POOL=1` nie jest tu
wspierane i nie ma wpisu odświeżania w cronie — odświeżanie zwracało zero żywych
adresów z 411 kandydatów, a jedyny stary wpis przejmował przez rendezvous hashing
komplet kont i zamieniał runy w timeouty. Zmienna jest też wykluczona
z dziedziczenia, więc włączenie puli w tamtym repo nie włączy jej tutaj.
Szczegóły: `docs/APIFY-PROXY.md`.

## Zasady obowiązujące w całym repo

Cztery, i są nienegocjowalne — reszta kodu na nich stoi:

1. **Komentarze i docstringi po polsku, wyjaśniają DLACZEGO, nie CO.** Kod pokazuje,
   co się dzieje; komentarz ma powiedzieć, czemu akurat tak, żeby następna osoba nie
   „uprościła" czegoś, co jest takie z powodu.
2. **Żaden worker nie tworzy tabel.** Migracje SQL odpala człowiek, jako `postgres`
   (`scripts/migrate.sh`). Rola workerów nie ma praw DDL.
3. **Brak konfiguracji = czyste wyjście z komunikatem, NIGDY wyjątek.** Workery
   chodzą z crona: wyjątek to awaria powtarzana co kilka minut, a niezerowy kod
   wyjścia zapycha skrzynkę do momentu, w którym prawdziwa awaria nie ma jak się
   przebić. Brak tokenu to nie awaria — to system, którego jeszcze nie włączono.
4. **Zero sekretów w logach.** Proxy logujemy jako `host:port`, hasła maskujemy,
   tokeny skracamy do czterech ostatnich znaków.

## Uruchomienie lokalne

Wymagane: Python 3.11, PostgreSQL.

```bash
git clone <repo> && cd laweta-radar

python3.11 -m venv venv && source venv/bin/activate
pip install -r laweta_radar/requirements.txt

cp laweta_radar/.env.example laweta_radar/.env
$EDITOR laweta_radar/.env          # minimum: DATABASE_URL
```

Baza (osobna, **nie** współdzielona z innymi projektami) i migracje:

```bash
sudo -u postgres createdb laweta
sudo -u postgres createuser laweta --pwprompt

DATABASE_URL_ADMIN="postgresql://postgres@localhost/laweta" \
  bash laweta_radar/scripts/migrate.sh
```

Na koniec nadaj rolę workerów prawa do tabel — komenda `GRANT` jest w komentarzu
na końcu każdej migracji. Świadomie nie robi tego skrypt: prawa to decyzja, a nie
efekt uboczny.

Sprawdź, czego jeszcze brakuje, i odpal API:

```bash
bash laweta_radar/scripts/check_setup.sh
python -m pytest laweta_radar/tests/ -q

export PYTHONPATH=$PWD
python -m uvicorn laweta_radar.api.main:app --host 127.0.0.1 --port 8002
curl -s localhost:8002/health | python -m json.tool
```

`/health` odpowiada **zawsze 200**, także gdy konfiguracja jest niepełna — kod mówi
„API żyje", a treść mówi, co jest zepsute. Traktowanie niewłączonego systemu jak
awarii mieszałoby dwie zupełnie różne sytuacje.

### Zanim cokolwiek pobierze

Świeży klon **nie strzela do Apify** — wszystkie wpisy w `config/groups.py` są bez
adresu i ze statusem `unverified`. Żeby ruszyło:

1. wskaż wspólny `.env` z pulą Apify — `SHARED_ENV_PATH` w `.env` lawety albo
   symlink. Sprawdź, że działa:
   `python -m laweta_radar.workers.apify_keys` ma pokazać niezerową liczbę kluczy.
   Własnych `APIFY_API_TOKEN*` **nie wpisujesz** (patrz sekcja o współdzieleniu),
2. dodaj realne grupy w `config/groups.py`, zweryfikuj każdą ręcznie
   (publiczna? żywa? zgłoszeniowa czy sama reklama lawet?) i dopiero wtedy przestaw
   `status` na `"ok"`.

3. zmierz actora, zanim zbudujesz wokół niego fetcher:
   `python laweta_radar/scripts/pomiar_actora.py --sucho` (plan i koszt, bez sieci),
   potem bez `--sucho`. Odpowiada na trzy pytania, których dokumentacja actora nie
   rozstrzyga: czy `onlyPostsNewerThan` w ogóle tnie i do jakiej jednostki, czy
   `resultsLimit` przy wielu grupach jest per grupa czy globalny, i ile realnie
   kosztuje jeden pobrany post. Wynik ląduje w `docs/POMIAR-ACTORA.md` i to jego
   czyta się przed pisaniem `_build_actor_input` — bez działającego okna czasowego
   płacimy za wielokrotne pobieranie tych samych postów.

Proxy jest już skonfigurowane po stronie wspólnego `.env` — sprawdź tylko, czy
przypisanie doszło: `python -m laweta_radar.workers.apify_proxy`. Jeśli pokazuje
„BRAK proxy", nie ruszaj z pulą kont, dopóki tego nie naprawisz
(`docs/APIFY-PROXY.md`). Pula wychodząca z jednego IP wygląda dla Apify jak
multi-accounting i kończy się utratą **całej puli naraz**, nie jednego konta.

## Deploy (VPS + PM2 + nginx)

```bash
# na VPS-ie, jako użytkownik aplikacji
git clone <repo> /home/ubuntu/laweta-radar && cd /home/ubuntu/laweta-radar
python3.11 -m venv venv
./venv/bin/pip install -r laweta_radar/requirements.txt

cp laweta_radar/.env.example laweta_radar/.env && $EDITOR laweta_radar/.env
DATABASE_URL_ADMIN="postgresql://postgres@localhost/laweta" \
  bash laweta_radar/scripts/migrate.sh

pm2 start ecosystem.config.js
pm2 save && pm2 startup
```

`ecosystem.config.js` zakłada `/home/ubuntu/laweta-radar` — podmień, jeśli
rozpakowujesz gdzie indziej. PM2 startuje procesy ze swojego środowiska, nie
z powłoki logowania, dlatego `scripts/start_api.sh` wczytuje `.env` jawnie.

W `.env` **nie wpisujesz kluczy Apify** — ustaw tylko `SHARED_ENV_PATH` na `.env`
sales-core-engine (domyślna ścieżka `/home/ubuntu/sales-core-engine/.env` zwykle
wystarcza). Użytkownik, z którego chodzi PM2, musi mieć prawo odczytu tamtego
pliku — jeśli go nie ma, laweta wstanie i będzie milczeć, bo rotator zobaczy zero
kluczy. Sprawdzisz to `bash laweta_radar/scripts/check_setup.sh`.

nginx — API **musi** zostać na loopbacku (nie ma własnej autoryzacji, więc dostępem
zarządza wyłącznie nginx):

```nginx
server {
    server_name laweta.twojadomena.pl;
    location / {
        proxy_pass http://127.0.0.1:8002;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Fetcher chodzi z crona — gęsto, bo liczy się czas reakcji:

```cron
*/5 * * * * cd /home/ubuntu/laweta-radar && ./venv/bin/python -m laweta_radar.workers.fb_fetcher >> /var/log/laweta/fetcher.log 2>&1
```

(wpis dokładamy razem z fetcherem — dziś ten moduł jeszcze nie istnieje)

### Aktualizacja

```bash
cd /home/ubuntu/laweta-radar && git pull
./venv/bin/pip install -r laweta_radar/requirements.txt
bash laweta_radar/scripts/migrate.sh          # migracje są idempotentne (IF NOT EXISTS)
pm2 restart laweta-api
```

## Diagnostyka

| Objaw | Sprawdź |
|---|---|
| nic nie przychodzi na Telegram | `python -m laweta_radar.services.telegram_notify` |
| „brak kluczy Apify", choć są w `.env` | `source laweta_radar/scripts/env-shell.sh` |
| ile kluczy widzi rotator | `python -m laweta_radar.workers.apify_keys` |
| ile kredytu zostało na kontach | `python -m laweta_radar.workers.apify_credits` (wymaga sieci) |
| rotator widzi 0 kluczy | zła ścieżka do wspólnego `.env` — `python -m laweta_radar.config.settings` |
| przez jakie IP realnie wychodzimy | `python -m laweta_radar.workers.apify_proxy --check` |
| stan całości | `bash laweta_radar/scripts/check_setup.sh` |
| stan API i bazy | `curl -s localhost:8002/health` |

Wszystkie narzędzia CLI działają bez sieci poza tymi, przy których napisano inaczej.
