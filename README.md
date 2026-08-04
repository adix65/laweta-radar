# Laweta Radar

Monitoruje grupy na Facebooku pod kątem zleceń dla lawety (pomoc drogowa,
transport aut) i wysyła je operatorowi na telefon w kilka minut od publikacji
posta.

> ## SYSTEM POKAZUJE ZLECENIA. DECYDUJE KIEROWCA.
>
> To jest zasada naczelna całego repo i bije każdą inną regułę, jaką znajdziesz
> niżej. Kod **nie ma prawa** odrzucić zlecenia dlatego, że mu się wydaje, że nie
> pasuje: za ciężkie, za daleko, za tanio, zły kierunek, nie ta data. Kierowca zna
> swój sprzęt, swój kalendarz i swoją cenę lepiej niż model językowy szacujący masę
> Golfa z literówki w poście. Może przełożyć, wziąć dwa auta zamiast trzech,
> podnająć albo pojechać po jedno, bo stawka dobra.
>
> System odrzuca **wyłącznie** posty, które w ogóle nie są zleceniami: reklamę
> konkurencji, sprzedaż sprzętu, ogłoszenia o pracę i posty wygaszone przez autora.
> Ta lista jest zamknięta i nie wolno jej rozszerzać o oceny biznesowe. Wszystko
> poza tym — wagi, kilometry, sugestie kompletów — jest **informacją na ekranie,
> nigdy filtrem**. Etykieta „ok. 3,8 t" pomaga. Ukrycie zlecenia, bo kod policzył
> 4,1 t, jest błędem.

Operator robi trasy międzynarodowe zestawem B+E, do trzech aut naraz. Stąd trzy
konsekwencje dla całego kodu:

1. **To nie jest wyścig na minuty.** Zlecenie „kupiłem auto w Niemczech, kto
   przywiezie" żyje dniami, nie kwadransem. Dlatego runy co godzinę, nie co pięć
   minut — a stąd dziesięciokrotnie niższy rachunek Apify.
2. **Trzy kliknięcia i koniec.** Z każdego zlecenia operator dostaje jednym
   dotknięciem: TRASĘ w mapach, POST na Facebooku i TELEFON, jeśli był w treści.
   To jest cała funkcja tej aplikacji; reszta jest dodatkiem.
3. **Limity sprzętu są etykietą, nie bramką.** Wartości w `.env` służą do
   WYŚWIETLENIA podsumowania przy komplecie. Żaden kod nie używa ich do ukrywania
   rekordów.

## Jak to działa

```
grupy FB ──> fb_fetcher ──> gate ──> classifier ──> geo ──> Telegram
            (Apify)      (filtr    (Claude:      (dystans   (operator
                          słowny)   zlecenie?)    od bazy)    odpisuje
                                                              RĘCZNIE)
```

Każdy krok istnieje po to, żeby następny dostał mniej roboty:

- **fb_fetcher** — pobiera najnowsze posty ze zweryfikowanych grup przez Apify,
  24/7. Klucze rotują się po **wspólnej z sales-core-engine** puli kont, ruch
  wychodzi przez proxy per konto. Budżet — liczony w **pobranych postach**, nie
  w runach — rozdziela między grupy bandyta (`services/bandit.py`), bo kredyt jest
  wspólny z drugim systemem.
- **gate** — darmowy filtr słowny **przed** modelem, po polsku, niemiecku, czesku
  i słowacku. Bez niego płacilibyśmy Claude'owi za każdy post o sprzedaży felg.
  Odrzuca wyłącznie cztery kategorie wymienione wyżej i nic poza nimi; bez
  wielojęzyczności gubiłby w całości zlecenia z grup DE/CZ/SK.
- **classifier** — model decyduje, czy to realne zlecenie, i wyciąga z posta to,
  co operator musi wiedzieć, zanim kliknie — **zawsze po polsku**, także z posta
  niemieckiego czy czeskiego. Domyślnie Haiku, ale provider jest wymienny jedną
  zmienną w `.env` (`services/llm.py`) — bo o tym, który model lepiej czyta post
  bez ogonków, decyduje pomiar na własnych danych, a nie benchmark.
- **geo** — liczy dystans i trasę, żeby **pokazać** je przy zleceniu. Nie ukrywa
  rekordów: o tym, czy kurs pod Kolonię się opłaca, decyduje kierowca.
- **Telegram** — jedyny kanał dowozu. Alert niesie link do posta; odpowiada
  **człowiek**, z własnego konta.

### Stan repo

Działa: konfiguracja (w tym dociąganie wspólnej puli Apify), rotacja kluczy, proxy,
bandyta, transport Telegrama, API diagnostyczne, migracje, dwa narzędzia
rozpoznawcze (**pomiar actora**, **wyszukiwarka grup**) oraz cztery pierwsze kroki
pipeline'u: **bramka słowna** (`workers/gate.py`), **fetcher**
(`workers/fb_fetcher.py`), **klasyfikator** (`workers/classifier.py` + wymienna
warstwa `services/llm.py`) i **geo** (`services/geo.py`).

Brakuje ostatniego kroku: **powiadomienia**, czyli złożenia alertu z werdyktu
modelu i trasy z geo i wypchnięcia go na Telegram (sam transport,
`services/telegram_notify.py`, jest gotowy). Do czasu, aż powstanie, fetcher
pobiera, odsiewa i klasyfikuje normalnie, a zlecenia czekają w tabeli `posty`
ze statusem `nowe` — czyli nie giną, tylko nikt ich jeszcze nie dostaje na
telefon. Każdy gotowy klocek da się przy tym odpalić z ręki na pojedynczej
treści (patrz tabela w „Diagnostyce").

Bramka jest przy tym w **trybie cienia** (`GATE_TRYB=cien`): liczy i zapisuje swoją
decyzję, ale niczego nie blokuje. Inaczej się nie da — bramka odrzuca posty, zanim
zobaczy je model, więc jej pomyłki są niewidoczne z definicji: odrzucone zlecenie
nie trafia nigdzie i nikt się o nim nie dowie. Bramka kasująca co dziesiąty kurs
wygląda w produkcji dokładnie tak samo jak bramka idealna. Włączamy ją dopiero,
gdy raport (`scripts/raport_gate.py`) pokaże ZERO fałszywych odrzuceń na sensownej
próbce. **W cieniu fetcher pobiera i zapisuje normalnie, ale nie oszczędza na
modelu** — oszczędność zaczyna się dopiero po przełączeniu na `aktywny`.

> **Zanim włączysz fetchera na produkcji: wykonaj pomiar actora.** Bez niego fetcher
> schodzi na ostrożniejszą **ścieżkę B** (patrz „Budżet liczy się w postach"), która
> działa poprawnie, ale kosztuje wielokrotnie więcej niż musi. Fetcher czyta werdykt
> wprost z `docs/POMIAR-ACTORA.md` — po pomiarze przełączy się sam, bez zmiany kodu.

## Zanim powstanie fetcher: dwa pomiary, nie dwie intuicje

Fetchera nie da się dobrze napisać, nie znając dwóch rzeczy, których nie ma
w dokumentacji: **jak actor naprawdę zachowuje się przy zawężaniu okna czasowego**
i **z jakich grup w ogóle warto pobierać**. Obie odpowiedzi produkują narzędzia
z `laweta_radar/scripts/` — uruchamiane ręcznie, nie z crona.

```bash
export PYTHONPATH=$PWD

# 1. POMIAR ACTORA (~20 min, ≤ 270 pobranych postów ≈ 1,35 USD)
python -m laweta_radar.scripts.pomiar_actora --sucho --grupa <URL>   # sam plan i koszt
python -m laweta_radar.scripts.pomiar_actora --grupa <URL> --grupa <URL2> --grupa <URL3>
#    -> docs/POMIAR-ACTORA.md

# 2. WYSZUKIWARKA GRUP (raz na start, potem raz w miesiącu)
python -m laweta_radar.scripts.znajdz_grupy --schema   # jakie pola actor przyjmuje
python -m laweta_radar.scripts.znajdz_grupy --sucho    # plan i koszt, bez wydawania
python -m laweta_radar.scripts.znajdz_grupy            # seria (pyta o potwierdzenie)
#    -> data/kandydaci_grupy.csv  ->  KROK RĘCZNY  ->  --raport  ->  config/groups.py
```

**Pomiar actora** odpowiada na trzy pytania, z których każde zmienia architekturę,
a nie szczegół:

| pytanie | co rozstrzyga |
|---|---|
| czy `onlyPostsNewerThan` działa | czy fetcher pobiera **przyrost**, czy za każdym razem to samo od nowa — przy przebiegu co 5 minut to rząd 288× w rachunku |
| czy `resultsLimit` jest per grupa | czy batchowanie grup jest bezpieczne; przy limicie globalnym batch po dziesięć grup gubi posty z ośmiu, a run i tak zostaje policzony |
| ile kosztuje jeden post | ile kont Apify trzeba — trzydzieści czy dziewięćset |

Wynik ląduje w `docs/POMIAR-ACTORA.md`. **Dopóki stoi tam ramka „POMIAR NIE ZOSTAŁ
WYKONANY", fetchera piszemy na wyczucie** — a wyczucie kosztuje tu realne pieniądze.

**Wyszukiwarka grup** buduje listę kandydatów przez wyszukiwarkę FB (cztery bloki
językowe: PL / DE / CS / SK — laweta na trasie do Niemiec wraca pusta, jeśli
zleceń szuka tylko po polsku). Kończy się **krokiem ręcznym, którego nie da się
pominąć**: człowiek otwiera każdy URL i wpisuje w kolumnie `publiczna` TAK/NIE, bo
Apify czyta wyłącznie grupy publiczne, a z zewnątrz tego nie widać. Powtórne
uruchomienie **scala** wynik z istniejącym CSV — praca ręczna nie ginie.

Oba narzędzia liczą i pokazują przewidywany koszt **przed** serią i czekają na
potwierdzenie; oba mają twardy sufit i odstęp między wywołaniami, bez
zrównoleglania.

## Budżet liczy się w POSTACH, nie w runach

Apify rozlicza actora grup FB **za pobrany post**. Run jest darmowy, jego zawartość
nie — i to przewraca dwie intuicje naraz:

- **batchowanie grup nie oszczędza kredytu**, tylko narzut uruchomienia;
- **płacimy za post widziany po raz dwudziesty**. Dedup w bazie chroni model
  i Telegram, ale nie rachunek: za pobranie zapłacono, zanim dedup cokolwiek
  zobaczył.

Główną dźwignią jest więc `onlyPostsNewerThan`, a nie częstotliwość — **o ile actor
to pole honoruje**. Rozstrzyga to pomiar, a fetcher **czyta jego werdykt** z
`docs/POMIAR-ACTORA.md` zamiast zgadywać:

| ścieżka | okno | `resultsLimit` | odstęp | czym płacimy za gęstsze pytanie |
|---|---|---|---|---|
| **A** — actor tnie po wieku | odstęp × 2, min. 30 min | hojny (do 50) — i tak nie zostanie zużyty | od 5 min | niczym: koszt dobowy = tempo grupy |
| **B** — actor przyjmuje tylko doby | `1 day` | ciasny (do 12) — **każdy punkt to pieniądze** | od 15 min | wprost proporcjonalnie |

**Bez pomiaru fetcher schodzi na B.** Nie dlatego, że jest bardziej prawdopodobna —
dlatego, że pomyłka w tę stronę kosztuje trochę nadmiarowego pobierania, a pomyłka
w drugą (hojny limit 50 przy ignorowanym oknie) to pięćdziesiąt opłaconych postów
z każdej grupy w każdym przebiegu, bez żadnego objawu poza rachunkiem.

Sufit dobowy (`POSTY_NA_DOBE`, start: 2000) jest **twardy i wspólny dla całego
systemu**; rozdziela go między grupy bandyta (`services/bandit.py`) proporcjonalnie
do wydajności = zlecenia / pobrane posty w oknie 7 dni. Grupa bez historii dostaje
pulę startową, żeby dało się ją w ogóle zmierzyć. Po wyczerpaniu sufitu fetcher
**nie wykonuje kolejnych wywołań** i mówi to wyraźnie — cicho przekroczony budżet to
spalona pula kont, z której korzysta też sales-core-engine.

Zysk uboczny, który jest właściwie głównym: po dwóch tygodniach system sam pokaże,
które grupy są warte pieniędzy, a które tylko paliły budżet.

```bash
export PYTHONPATH=$PWD
python -m laweta_radar.workers.fb_fetcher --sucho          # plan i koszt, zero wywołań
python -m laweta_radar.workers.fb_fetcher --budzet 300     # inny sufit dobowy
python -m laweta_radar.workers.fb_fetcher --grupa <URL>    # jedna grupa, bez harmonogramu
```

## Wielojęzyczność: PL / DE / CS / SK

Laweta na trasie do Niemiec wraca pusta, jeśli zleceń szuka tylko po polsku. Bramka
ma więc **osobny słownik na język** (czeski i słowacki dzielą jeden, z wariantami)
o tej samej strukturze warstw i tych samych wagach, plus własną, mikrosekundową
detekcję języka — bez bibliotek i bez sieci. Post jest liczony **wszystkimi**
słownikami, a detekcja służy do wyboru znacznika i rozstrzygania remisów: cztery
przebiegi regeksem to mikrosekundy, a pomyłka detekcji byłaby cichym fałszywym
odrzuceniem, czyli jedynym błędem, którego bramka ma nie popełniać.

To nie jest kosmetyka. Bramka jednojęzyczna jest **cicha i śmiertelna**: niemieckie
„Suche Autotransport von München nach Krakau, Fahrzeug fährt nicht" nie trafia ani
jednego polskiego wzorca, dostaje zero punktów i wylatuje — w logach wyglądając
identycznie jak odrzucona reklama felg.

```bash
python -m laweta_radar.workers.gate "Suche Abschleppdienst, Motor kaputt"
```

Kto co robi z językiem — kontrakt spisany jest w `docs/WIELOJEZYCZNOSC.md`:
**bramka nie tłumaczy, tylko wpuszcza**; tłumaczy klasyfikator (i wypełnia wszystkie
pola **po polsku**, zostawiając nazwy miejscowości w oryginale, żeby geokodowanie
trafiało tam, gdzie trzeba); powiadomienie niesie dwuliterowy znacznik języka, bo od
niego zależy, w jakim języku operator ma oddzwonić.

## Struktura

```
laweta_radar/
  workers/
    fb_fetcher.py      # CRON: Apify -> bramka -> baza; budżet w postach
    gate.py            # tani filtr słowny PRZED modelem, PL/DE/CS/SK
    apify_keys.py      # rotacja puli kluczy APIFY_API_TOKEN1..N     [kopia 1:1]
    apify_proxy.py     # przypisanie token->proxy, sesje lepkie      [kopia 1:1]
    apify_run.py       # odpal actora, doczekaj, oddaj itemy + koszt + czas
    apify_credits.py   # saldo miesięcznego kredytu konta (do pomiaru kosztu)
    classifier.py      # ekstrakcja zlecenia z posta: prompt, rozbiór, walidacja
  services/
    telegram_notify.py # transport alertów (sam _send/_escape/_truncate)
    bandit.py          # Thompson Sampling — rozdział budżetu runów Apify
    llm.py             # JEDNA funkcja `zapytaj` — provider wymienny w .env
    geo.py             # kody -> współrzędne, kilometry, deep linki do map
  config/
    settings.py        # jedyne miejsce czytające środowisko
    shared_env.py      # dociąga klucze Apify ze WSPÓLNEGO .env sales-core-engine
    groups.py          # lista grup FB — dane, nie kod
    frazy_grup.py      # frazy wyszukiwania grup (PL/DE/CS/SK) — dane, nie kod
  api/
    main.py            # FastAPI: /health
    migrations/        # SQL odpalany RĘCZNIE, nigdy z workera
      0001_posty.sql       # surowe posty z grup
      0002_gate.sql        # kolumny decyzji bramki (tryb cienia)
      0003_fetcher.sql     # kolumny fetchera + tabela `harmonogram`
      0004_klasyfikacja.sql # pola wyciągnięte z posta przez model
  scripts/             # env-shell, migrate, start_api, check_setup
    pomiar_actora.py   # JEDNORAZOWA diagnostyka actora — nie część pipeline'u
    znajdz_grupy.py    # RĘCZNIE, raz w miesiącu -> data/kandydaci_grupy.csv
    raport_gate.py     # rozliczenie trybu cienia bramki
    porownaj_modele.py # wybór modelu na WŁASNYCH danych, nie na benchmarku
    pobierz_geo.py     # jednorazowe pobranie bazy kodów z GeoNames
  tests/               # testy offline (bez sieci i bez bazy)
    dane/posty_referencyjne.jsonl   # zbiór do porównania modeli
  .env.example
  requirements.txt
data/kandydaci_grupy.csv  # lista grup do ręcznego sprawdzenia (kolumna `publiczna`)
data/kody_eu.csv          # baza kodów pocztowych — commitowana, patrz data/README.md
docs/APIFY-PROXY.md       # po co proxy i jak je skonfigurować
docs/POMIAR-ACTORA.md     # co actor realnie robi i ile kosztuje (wynik pomiaru)
docs/WIELOJEZYCZNOSC.md   # kto co robi z językiem: bramka / klasyfikator / alert
```

Podział `workers/` vs `scripts/` jest celowy: **worker odpala się sam**, z crona, co
kilka minut — więc brak konfiguracji kończy w nim ciszą i czystym wyjściem.
**Skrypt odpala człowiek**, świadomie i zwykle raz — więc wolno mu zadać pytanie
i wypisać ścianę tekstu, ale musi powiedzieć, ile będzie kosztował, zanim
cokolwiek wyda.

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

Pięć, i są nienegocjowalne — reszta kodu na nich stoi:

0. **System pokazuje zlecenia, decyduje kierowca.** Patrz ramka na górze. Ta bije
   pozostałe cztery; jeśli któraś z nich każe coś ukryć przed operatorem, to ta
   wygrywa.
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
   `status` na `"ok"`. Listy kandydatów nie wpisuj z pamięci — zbuduj ją
   wyszukiwarką: `python -m laweta_radar.scripts.znajdz_grupy` (patrz sekcja
   „Zanim powstanie fetcher").

3. zmierz actora, zanim zbudujesz wokół niego fetcher:
   `python laweta_radar/scripts/pomiar_actora.py --sucho` (plan i koszt, bez sieci),
   potem bez `--sucho`. Odpowiada na trzy pytania, których dokumentacja actora nie
   rozstrzyga: czy `onlyPostsNewerThan` w ogóle tnie i do jakiej jednostki, czy
   `resultsLimit` przy wielu grupach jest per grupa czy globalny, i ile realnie
   kosztuje jeden pobrany post. Wynik ląduje w `docs/POMIAR-ACTORA.md` i to jego
   czyta się przed pisaniem `_build_actor_input` — bez działającego okna czasowego
   płacimy za wielokrotne pobieranie tych samych postów.

### Bramka i jej kalibracja

Bramka jest darmowa i chodzi offline, więc możesz ją oglądać, zanim cokolwiek
zostanie pobrane:

```bash
python -m laweta_radar.workers.gate "kupiłem auto w Kolonii, kto przywiezie?"
python -m laweta_radar.workers.gate "Laweta 24/7, konkurencyjne ceny"
```

CLI pokazuje werdykt, punktację i **które wzorce zadziałały, z wagami** — czyli
odpowiada na jedyne sensowne pytanie przy strojeniu słownika: dlaczego akurat tak.

Kalibracja idzie tak, i kolejność jest istotna:

1. zbieraj tydzień z `GATE_TRYB=cien` (domyślnie). Bramka liczy i zapisuje, ale nie
   blokuje — wszystkie posty idą do modelu,
2. `python laweta_radar/scripts/raport_gate.py` — raport pokazuje macierz pomyłek,
   pełną listę **fałszywych odrzuceń** (z treścią i punktacją), rozkłady punktów
   osobno dla zleceń i śmieci oraz to, co by się stało przy innym progu,
3. każde fałszywe odrzucenie napraw w `workers/gate.py` i sprawdź poprawkę przez
   `raport_gate.py --przelicz` — to przepuszcza ZAPISANE treści przez AKTUALNY
   słownik, więc masz odpowiedź w sekundę, bez czekania na kolejny tydzień
   i bez płacenia Apify po raz drugi,
4. dopiero gdy fałszywych odrzuceń jest **zero** na sensownej próbce, ustaw
   `GATE_TRYB=aktywny`.

Nie celuj w wysoki odsetek odsianych. Realistycznie wychodzi 20-35% i to jest
w porządku: śmieć przepuszczony do modelu kosztuje ~0,002 zł, a zlecenie odrzucone
przez bramkę ~300 zł straconego kursu — i nigdy się o nim nie dowiesz, bo post nie
trafi nigdzie. Jeden przegapiony kurs miesięcznie kasuje całą oszczędność
na tokenach.

Proxy jest już skonfigurowane po stronie wspólnego `.env` — sprawdź tylko, czy
przypisanie doszło: `python -m laweta_radar.workers.apify_proxy`. Jeśli pokazuje
„BRAK proxy", nie ruszaj z pulą kont, dopóki tego nie naprawisz
(`docs/APIFY-PROXY.md`). Pula wychodząca z jednego IP wygląda dla Apify jak
multi-accounting i kończy się utratą **całej puli naraz**, nie jednego konta.

### Klasyfikator i wybór modelu

Bramka odpowiada, czy w ogóle warto wydać na post token. Klasyfikator
(`workers/classifier.py`) odpowiada na pytanie drugie i ostatnie: czy to realne
zlecenie, skąd dokąd, czym, w jakim stanie i jak pilnie. Podejrzysz go na
pojedynczej treści:

```bash
python -m laweta_radar.workers.classifier --prompt          # sam prompt, bez kosztu
python -m laweta_radar.workers.classifier --prompt --jezyk de   # z instrukcją językową
python -m laweta_radar.workers.classifier "zdechlem w Sanoku, akumulator padl"
```

Fetcher woła go trzema argumentami — `klasyfikuj(tresc, grupa, jezyk)` — gdzie
`jezyk` to znacznik z bramki. Post z grupy DE/CZ/SK jest rozumiany w oryginale,
ale **wynik wraca po polsku**, bo czyta go polskojęzyczny operator, który ma
zdecydować w kilkanaście sekund. Wyjątkiem są nazwy miejscowości: te zostają
w formie oryginalnej, bo idą wprost do geokodera (`docs/WIELOJEZYCZNOSC.md`).

**Domyślny model to Haiku** (`CLASSIFIER_MODEL`). To zadanie ekstrakcji, nie
rozumowania — Haiku robi je równie dobrze za ułamek ceny, a liczy się też czas:
każda sekunda opóźnienia to przewaga konkurencji.

Model jest jednak **wymienny bez dotykania logiki**. Cała komunikacja z modelem
przechodzi przez jedną funkcję w `services/llm.py`, a `LLM_PROVIDER` w `.env`
wybiera implementację (`anthropic`, `openai`, `gemini`). Parsowanie JSON-a,
walidacja pól i obsługa błędów zostają w klasyfikatorze — wspólne dla
wszystkich, żeby porównanie mierzyło modele, a nie nasz kod.

Powód tej warstwy jest **pomiarowy, nie estetyczny**. Różnica w cenie między
najdroższym a najtańszym modelem przy tym wolumenie to około 25 zł miesięcznie,
czyli mniej niż szum. Różnica w JAKOŚCI na polskich postach pisanych bez ogonków,
z literówkami i skrótami drogowymi może być duża — i nie da się jej przewidzieć
z benchmarków, bo żaden nie mierzy „wyciąganie miejscowości z posta
laweciarskiego". Dlatego mierzymy na swoich danych:

```bash
python laweta_radar/scripts/porownaj_modele.py --sucho   # plan i koszt, BEZ sieci
python laweta_radar/scripts/porownaj_modele.py           # tabela porównawcza
```

Skrypt puszcza `tests/dane/posty_referencyjne.jsonl` przez wszystkie
skonfigurowane providery i liczy per model: trafność `czy_zlecenie`, trafność
miasta odbioru i dostawy, trafność `pilnosc`, ile razy wynik nie dał się
sparsować, medianę czasu odpowiedzi i realny koszt runu.

Zbiór w repo to na razie **22 seedy napisane razem z promptem** — dość, żeby
sprawdzić, że skrypt chodzi, za mało na decyzję i skrypt mówi to wprost.
Po pierwszym tygodniu produkcji dopisz do niego **min. 40 realnych postów
z bazy** z ręcznie wpisanym wynikiem (instrukcja jest w nagłówku tego pliku).
To robota do zrobienia raz; bez niej wybór modelu pozostaje zgadywaniem.

### Geo: kody pocztowe, kilometry, jeden klikalny link

`services/geo.py` zamienia to, co model wyciągnął z posta, na współrzędne,
kilometry i **jeden link, który na telefonie otwiera Google Maps z gotową trasą**.

```bash
python -m laweta_radar.services.geo Krosno Rzeszow
python -m laweta_radar.services.geo --kody "auto w 50667 Koln, moge dac 2500 zl"
```

**Nie używamy płatnego geokodera** i nie zamierzamy. 90% przypadków to kod
pocztowy albo nazwa miasta, a to załatwia lokalna baza — za darmo, offline,
w mikrosekundy, bez limitu zapytań i bez klucza, który może wygasnąć w środku
nocy. Google Maps jest wyłącznie deep linkiem: darmowym i bez API key.

Świeży klon ma w `data/kody_eu.csv` **zalążek** (~70 miast). Pełną bazę
pobierasz raz i **commitujesz** — szczegóły i licencja w `data/README.md`:

```bash
python laweta_radar/scripts/pobierz_geo.py --sucho   # plan, bez sieci
python laweta_radar/scripts/pobierz_geo.py           # PL DE CZ SK NL BE AT FR IT
```

Trzy rzeczy, które w tym module są decyzją, a nie szczegółem:

- **Null jest lepszy niż zła współrzędna.** Geokoder ma prawo powiedzieć „nie
  wiem" i nie ma prawa zgadnąć. Zła współrzędna wysyła człowieka 80 km w złą
  stronę i wygląda przy tym dokładnie tak samo jak trafiona.
- **`zrodlo` jest częścią produktu.** Gdy nazwa jest niejednoznaczna
  (kilkanaście „Nowych Wsi" w Polsce), bierzemy największą i oznaczamy wynik
  jako `miasto_niepewne`. To **musi** trafić na ekran: operator ma zobaczyć,
  że lokalizacja jest zgadywana, zanim pojedzie 60 km.
- **Trasa, nie promień.** Pierwszą liczbą jest długość kursu odbiór→dostawa;
  dystans od bazy idzie obok, jako pomocniczy. Żadna z nich niczego nie
  filtruje, a `kalkulacja()` jest etykietą na ekranie, nie bramką i nie wyceną
  (dystans liczymy Haversine razy 1,25 — to szacunek do przesiewu, nie na
  fakturę).

Formaty kodów pocztowych są tu **jednym źródłem prawdy dla całego repo**:
klasyfikator pyta `geo.czy_kod_pocztowy()`, zamiast trzymać własną listę.
Kryterium jest proste — kodem jest to, z czego geokoder umie zrobić punkt.
Własna lista po stronie klasyfikatora wyrzucałaby niemieckie „50667" w dniu,
w którym bramka wpuściła pierwszą grupę DE, i objawiłaby się jako zlecenia
bez trasy, bez jednego błędu w logu.

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

Fetcher chodzi z crona — gęsto, bo liczy się czas reakcji, i **24/7, bez okna
nocnego**: auto psuje się o trzeciej w nocy i wtedy jest najmniej konkurencji.
Ciszę nocną robimy po stronie powiadomień, bo zebrać zlecenie i nie budzić nim
człowieka to dwie różne decyzje.

```cron
*/5 * * * * cd /home/ubuntu/laweta-radar && ./venv/bin/python -m laweta_radar.workers.fb_fetcher >> /var/log/laweta/fetcher.log 2>&1
```

Cron może chodzić **gęściej** niż odstęp grupy — harmonogram i tak przepuści tylko
te grupy, którym wypada. Odwrotnie się nie da: cron rzadszy niż `MIN_INTERWAL_MIN`
jest sufitem, którego harmonogram nie przeskoczy.

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
| dlaczego bramka przepuściła/odrzuciła post | `python -m laweta_radar.workers.gate "treść"` |
| czy bramkę można już włączyć | `python laweta_radar/scripts/raport_gate.py` |
| co model wyciąga z konkretnego posta | `python -m laweta_radar.workers.classifier "treść"` (wymaga sieci) |
| jak brzmi prompt systemowy | `python -m laweta_radar.workers.classifier --prompt` |
| który provider modelu jest gotowy | `python -m laweta_radar.services.llm` |
| który model wybrać | `python laweta_radar/scripts/porownaj_modele.py` (wymaga sieci) |
| czy geokoder zna to miasto | `python -m laweta_radar.services.geo "Krosno" "Rzeszow"` |
| jakie kody widzi geokoder w treści | `python -m laweta_radar.services.geo --kody "treść"` |
| rotator widzi 0 kluczy | zła ścieżka do wspólnego `.env` — `python -m laweta_radar.config.settings` |
| przez jakie IP realnie wychodzimy | `python -m laweta_radar.workers.apify_proxy --check` |
| ile kredytu zostało na koncie #N | `python -m laweta_radar.workers.apify_credits --klucz N` |
| jakie pola przyjmuje actor wyszukiwarki | `python -m laweta_radar.scripts.znajdz_grupy --schema` |
| ile kosztowałby pomiar / seria wyszukiwania | dowolny z dwóch skryptów z `--sucho` |
| co i za ile pobierze najbliższy przebieg | `python -m laweta_radar.workers.fb_fetcher --sucho` |
| na której ścieżce (A/B) stoi fetcher | pierwsza linia wyjścia `--sucho` |
| czemu ten post nie przeszedł bramki | `python -m laweta_radar.workers.gate "treść posta"` |
| ile budżetu zostało na dzisiaj | `--sucho` (linia „budżet dobowy") albo tabela `harmonogram` |
| stan całości | `bash laweta_radar/scripts/check_setup.sh` |
| stan API i bazy | `curl -s localhost:8002/health` |

Wszystkie narzędzia CLI działają bez sieci poza tymi, przy których napisano inaczej.
