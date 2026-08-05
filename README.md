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
  **człowiek**, z własnego konta. Wysyła go **fetcher**, zaraz po udanym zapisie
  posta (`workers/fb_fetcher._powiadom` -> `services/powiadomienia`) — dedup
  alertów stoi na wierszu w bazie, więc alert o poście, którego w `posty` nie ma,
  poszedłby ponownie w każdym kolejnym przebiegu.

### Stan repo

Działa: konfiguracja (w tym dociąganie wspólnej puli Apify), rotacja kluczy, proxy,
bandyta, migracje, dwa narzędzia rozpoznawcze (**pomiar actora**, **wyszukiwarka
grup**) oraz **cały pipeline, od pobrania do telefonu w kieszeni**: bramka słowna
(`workers/gate.py`), fetcher (`workers/fb_fetcher.py`), klasyfikator
(`workers/classifier.py` + wymienna warstwa `services/llm.py`), geo
(`services/geo.py`), powiadomienia (`services/powiadomienia.py`), API panelu
(`api/`), bot Telegrama (`workers/bot.py`) i PWA (`panel/`).

Nie brakuje już żadnego kroku. Każdy klocek da się przy tym odpalić z ręki na
pojedynczej treści — patrz tabela w „Diagnostyce"; to jest sposób na
rozstrzygnięcie „czemu to zlecenie nie przyszło" bez czytania logów PM2.

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

## Dowóz: telefon, panel, mapa

To jest moment, w którym system zarabia — cała reszta repo jest przygotowaniem do
dwóch sekund, w których operator patrzy na ekran i decyduje, czy dzwonić.

### Telegram jest kanałem podstawowym

Bez alternatyw na start i to jest decyzja, nie brak czasu: działa na każdym
telefonie, dźwięk przechodzi przez tryb cichy przy ustawionym priorytecie, nie
wymaga instalacji PWA ani zgód przeglądarki, jest darmowy i dostarcza w sekundę.
Web push z panelu jest **uzupełnieniem, nigdy zamiennikiem**.

```
🚨 TERAZ · 53 km · ~250 zł          <- trzy liczby, po których zapada decyzja

Krosno (38-400) → Rzeszów · 12 km od bazy
VW Golf IV · nie odpala · toczy się

"potrzebuje lawety z Krosna do Rzeszowa, golf   <- ORYGINAŁ, bo model się myli
stanal i nie odpala, moze byc dzis wieczorem"

📞 555 111 222
👥 Pomoc drogowa Podkarpacie · 4 min temu

[ Trasa w mapach ] [ Otwórz post ] [ Śmieć ]
[ Biorę ]
```

Cytat jest **obowiązkowy**: model potrafi wyciągnąć „Golfa" ze zdania o innym
aucie albo zgubić „nie" przy „nie odpala", a człowiek musi mieć dostęp do
oryginału bez klikania. Wiek posta jest **zawsze**: „4 min temu" znaczy, że warto
dzwonić, „2 h temu" — że pewnie już ktoś pojechał.

Pierwszą liczbą jest **długość kursu** (odbiór→dostawa), nie odległość od bazy:
przy transporcie międzynarodowym „ile km od bazy" samo nic nie znaczy, bo i tak
trzeba przejechać całą trasę z autem na lawecie. Dojazd idzie linijkę niżej,
jako liczba pomocnicza. Gdy dostawy nie znamy, w pierwszej linii ląduje dojazd —
jest wtedy jedyną liczbą, jaką mamy.

```bash
python -m laweta_radar.services.powiadomienia --podglad   # sam układ, bez sieci
python -m laweta_radar.services.powiadomienia --probka    # wyślij przykład
python -m laweta_radar.services.powiadomienia --noc       # podsumowanie ranne
```

### Progi sterują brzęczeniem, nigdy widocznością

**Żaden próg nie usuwa zlecenia z bazy ani z panelu.** To jest zasada naczelna
repo i tutaj najłatwiej ją złamać, bo „nie wysyłaj" i „ukryj" wyglądają w kodzie
podobnie.

| próg | co robi | czego NIE robi |
|---|---|---|
| `MIN_PEWNOSC` (40) | zlecenie idzie do panelu bez alertu | nie kasuje go |
| `CISZA_NOCNA` (22-6) | nocne zlecenia idą jednym podsumowaniem rano | nie gubi ich |
| `MAX_POWIADOMIEN_H` (15) | po przekroczeniu jedna zbiorcza „jeszcze N w panelu" | nie ucisza panelu |

**BRAK DANYCH NIE JEST NISKĄ WARTOŚCIĄ — I NIGDY NIE WYCISZA ALERTU.** Próg
działa wyłącznie na liczbie, którą naprawdę mamy. Nieznana pewność (`NULL`
w kolumnie, pusty string, śmieć) przechodzi próg i idzie na telefon, a alert
mówi wprost, czego nie wiemy: „⚠️ pewność nieznana · trasa nieustalona".
Napisane raz wprost, bo najprostsza wersja tego kodu (`int(pewnosc or 0)`)
zamienia brak danych w zero, zero jest poniżej każdego progu — i tak właśnie
15 zleceń nie dostało ani jednego powiadomienia, przy zerze w tabeli
`powiadomienia` i logu bez jednej linijki o pominięciu. Cisza jest tu najgorszym
trybem awarii: wygląda jak brak zleceń na rynku, a jest utratą wszystkich.

**Nie ma progu na kilometry ani na kierunek.** Trasa Kolonia-Kraków to 1100 km
i normalny dzień pracy tego operatora. Filtr „do 50 km" istnieje w panelu jako
pigułka, którą operator włącza i wyłącza sam.

### Antyspam jest wymogiem, nie ozdobą

System wysyłający 40 powiadomień dziennie zostanie wyciszony po tygodniu
i przestanie istnieć — to jest awaria całkowita, tylko rozłożona na dni.

1. **dedup po `fb_id`** — jeden post = jedno powiadomienie, na zawsze; pilnuje
   tego UNIQUE INDEX, nie tylko `if` w kodzie (dwa przebiegi fetchera potrafią
   się nałożyć, bo cron nie czeka na poprzedni);
2. **dedup treściowy** — ten sam post crossowany do pięciu grup ma pięć różnych
   `fb_id`, bo hash liczymy z treści. Przed wysyłką pytamy, czy w ostatnich 6 h
   nie szło już powiadomienie o tym samym numerze telefonu **albo** o tej samej
   parze miast i tym samym pojeździe; jeśli tak — dopisujemy grupę do
   istniejącego wpisu zamiast wysyłać drugą wiadomość;
3. **twardy limit 15/h** — przekroczenie prawie zawsze znaczy, że coś się zepsuło
   w bramce albo klasyfikatorze, więc zbiorcza wiadomość jest przy okazji alarmem.

### Pewność lokalizacji jest częścią alertu

`services/geo.py` (patrz sekcja o geo wyżej) przy każdym punkcie oddaje `zrodlo`:
`kod` / `miasto` / `miasto_niepewne` / brak dopasowania. Powiadomienie i panel
**muszą** to pokazać, i pokazują:

- w alercie — znak zapytania przy nazwie i jedno słowo ostrzeżenia
  („Nowa Wies? (niepewne)"), a przy braku dopasowania „(nierozpoznane)" i `? km`;
- w panelu — pasek nad kilometrami z **surową treścią miejsca z posta**
  (`odbior_raw`), bo ostrzeżenie bez niej mówi „nie ufaj", nie dając czym to
  sprawdzić.

Cicho podana zła liczba kilometrów wysyła lawetę nie tam, a dowiadujesz się
o tym po godzinie jazdy — dlatego geokoder ma prawo powiedzieć „nie wiem",
ale nie ma prawa zgadnąć po cichu.

### API i bot

```bash
GET   /zlecenia?status=&od=&do=&max_km=&limit=   # km, zł i linki policzone po stronie API
GET   /zlecenia/{fb_id}
PATCH /zlecenia/{fb_id}   {"status": "...", "notatka": "...", "cena_koncowa": 350}
GET   /statystyki        # lejek + skuteczność per grupa (7 i 30 dni)
GET   /zdrowie           # bez tokenu — patrz niżej
```

Autoryzacja: jeden token w nagłówku `X-Token` (`API_TOKEN` w `.env`). Bez ról
i bez sesji, bo użytkownik jest jeden. **Puste `API_TOKEN` = 503 na endpointach
z danymi**, a nie otwarte API: adres panelu jest publiczny, a lista zleceń zawiera
numery telefonów obcych ludzi z grup FB.

`/zdrowie` i `/health` chodzą **bez tokenu** i zawsze zwracają 200 — są potrzebne
dokładnie wtedy, gdy konfiguracja jest zepsuta. `/zdrowie` odpowiada na jedyne
pytanie, którego nikt nie zada w porę: **czy fetcher jeszcze chodzi**. Cichy
fetcher, który przestał działać trzy dni temu, wygląda dla crona identycznie jak
spokojny tydzień na grupach. Pole `status` ma trzy wartości (`ok` / `uwaga` /
`awaria`), żeby monitoring alarmował na jednej, a nie parsował dziesięciu liczb.

```bash
curl -s localhost:8002/zdrowie | python -m json.tool
curl -s "localhost:8002/zdrowie?glebokie=1"   # dopytuje Apify o saldo (WYMAGA SIECI)
```

Bot (`workers/bot.py`, osobny proces PM2, long polling) obsługuje przyciski pod
powiadomieniem i cztery komendy: `/dzis`, `/ostatnie 10`, `/stop`, `/start`.
`/stop` wycisza **wyłącznie brzęczenie** — fetcher zbiera dalej i wszystko trafia
do panelu. Przyjmuje wiadomości **tylko z `TELEGRAM_CHAT_ID`**: nazwa bota jest
publiczna, a jego komendy zmieniają statusy zleceń.

### Pętla zwrotna: każde „Śmieć" to dane

Kliknięcie „Śmieć" — pod alertem albo w panelu — zapisuje wiersz do tabeli
`feedback` razem z **treścią posta i werdyktem modelu**. To jest jedyna pętla
zwrotna w tym systemie: bez niej prompt klasyfikatora poprawia się z pamięci,
a pamięć po tygodniu nie odtworzy, KTÓRY post był zły i co model o nim sądził.

```bash
python laweta_radar/scripts/raport_feedback.py            # 30 dni, z przykładami
python laweta_radar/scripts/raport_feedback.py --wzorce   # co się powtarza
python laweta_radar/scripts/raport_feedback.py --json     # wejście maszynowe
```

Raport grupuje pomyłki po powtarzalnym wzorcu i mówi, ile z nich **bramka i tak
by odrzuciła** — bo tę część naprawia próg bramki, a nie prompt, i jest to
darmowe.

## Panel: PWA na telefon

`panel/` — Next.js 16, React 19, Tailwind 4, TypeScript. Wersje przepisane 1:1
z `our-site`, żeby deploy szedł tą samą ścieżką.

**To jest aplikacja używana jedną ręką, na postoju, często w słońcu.** Wszystkie
decyzje projektowe są podporządkowane temu zdaniu:

- minimalna wysokość elementu dotykowego **56 px** (nie 44-48 — to dotyk
  w rękawicy, przy uruchomionym silniku);
- główne akcje w **dolnej trzeciej ekranu**, w zasięgu kciuka;
- kontrast tekstu podstawowego **min. 7:1** (realnie 20:1 dla tekstu głównego
  i 10,9:1 dla opisów — w słońcu ekran traci kontrast kilkukrotnie);
- **żadnych hoverów ani tooltipów** jako jedynej ścieżki;
- liczby duże, opisy małe — na karcie zlecenia najpierw widać km i zł;
- **jeden font zmienny, dwa rozmiary robocze**, bez trzeciego kroju.

| ekran | co robi |
|---|---|
| `/` | karty zleceń; swipe w prawo = biorę, w lewo = śmieć, **plus zawsze widoczne przyciski** (gest jest skrótem, nie jedyną drogą); pigułki: pilne / do 50 km / dziś / wszystkie |
| `/zlecenie/[id]` | pełna treść posta (oryginał), wszystkie pola i trzy przyciski przez całą szerokość: **NAWIGUJ / ZADZWOŃ / OTWÓRZ POST**; notatka i cena końcowa |
| `/mapa` | pinezki wokół bazy, Leaflet + kafelki OSM (bez klucza i bez rachunku) |
| `/statystyki` | tydzień i miesiąc, Recharts, dwa wykresy — lejek i skuteczność per grupa |

```bash
cd panel && npm install
npm run dev                  # http://localhost:3000, API przez rewrite na :8002
npm run build && npm start   # produkcyjnie, port 6200
```

Odświeżanie listy: **polling co 30 s, tylko gdy karta jest widoczna**
(`document.visibilityState`). Bez WebSocketów — przy jednym użytkowniku to
warstwa, która nic nie przyspiesza, a potrafi się cicho rozłączyć: karta wygląda
na żywą, dane stoją, i nikt się nie dowie.

PWA: `manifest.json`, **prawdziwe pliki ikon** w `public/` (generator jest
w `panel/scripts/ikony.py` i odpala się ręcznie, nie w runtime), `display:
standalone`, ciemny `theme-color`. Service worker cache'uje powłokę aplikacji
i ostatnią listę zleceń — w miejscu bez zasięgu widać co najmniej ostatni stan,
bo numer telefonu i miasto wystarczą, żeby oddzwonić, gdy zasięg wróci.

**Web push (VAPID) jest DRUGIM kanałem obok Telegrama.** Interfejs mówi wprost, że
na iOS działa **wyłącznie po dodaniu do ekranu głównego** — to ograniczenie Apple,
nie błąd aplikacji, i bez tego zdania pierwszym efektem będzie zgłoszenie „push
nie działa", na które nie ma odpowiedzi.

## Struktura

```
laweta_radar/
  workers/
    fb_fetcher.py      # CRON: Apify -> bramka -> baza; budżet w postach
    gate.py            # tani filtr słowny PRZED modelem, PL/DE/CS/SK
    bot.py             # PM2: przyciski pod alertem + /dzis /ostatnie /stop /start
    apify_keys.py      # rotacja puli kluczy APIFY_API_TOKEN1..N     [kopia 1:1]
    apify_proxy.py     # przypisanie token->proxy, sesje lepkie      [kopia 1:1]
    apify_run.py       # odpal actora, doczekaj, oddaj itemy + koszt + czas
    apify_credits.py   # saldo miesięcznego kredytu konta (do pomiaru kosztu)
    classifier.py      # ekstrakcja zlecenia z posta: prompt, rozbiór, walidacja
  services/
    telegram_notify.py # transport alertów (sam _send/_escape/_truncate + wyslij/wywolaj)
    powiadomienia.py   # TREŚĆ alertu, progi wysyłki, antyspam, cisza nocna
    geo.py             # dystans od bazy, linki do map, pewność lokalizacji
    feedback.py        # zapis oceny operatora — wspólny dla API i bota
    bandit.py          # Thompson Sampling — rozdział budżetu runów Apify
    llm.py             # JEDNA funkcja `zapytaj` — provider wymienny w .env
    schemat.py         # kontrakt klasyfikatora jako JSON Schema (OPENAI_JSON_MODE=schema)
    geo.py             # kody -> współrzędne, kilometry, deep linki do map
  config/
    settings.py        # jedyne miejsce czytające środowisko
    cennik.py          # stawki modeli + DATA sprawdzenia — jedyne miejsce z cenami
    shared_env.py      # dociąga klucze Apify ze WSPÓLNEGO .env sales-core-engine
    groups.py          # lista grup FB — dane, nie kod
    frazy_grup.py      # frazy wyszukiwania grup (PL/DE/CS/SK) — dane, nie kod
  api/
    main.py            # FastAPI: montuje routery + /health
    auth.py            # jeden token w nagłówku, zero ról
    db.py              # połączenie per request, bez puli
    routers/
      zlecenia.py      # lista, szczegół, PATCH statusu — z policzoną geografią
      statystyki.py    # lejek i skuteczność per grupa
      zdrowie.py       # czy fetcher jeszcze chodzi
      push.py          # subskrypcje web push
    migrations/        # SQL odpalany RĘCZNIE, nigdy z workera
      0001_posty.sql       # surowe posty z grup
      0002_gate.sql        # kolumny decyzji bramki (tryb cienia)
      0003_fetcher.sql     # kolumny fetchera + tabela `harmonogram`
      0004_klasyfikacja.sql # pola wyciągnięte z posta przez model
      0005_panel.sql       # notatka, cena końcowa, znacznik zmiany statusu
      0006_powiadomienia.sql # dedup, limity, callbacki z przycisków
      0007_feedback.sql    # zbiór treningowy do poprawiania promptu
      0008_push.sql        # subskrypcje web push
      0009_werdykt_modelu.sql # jedno źródło werdyktu AI + indeksy na parze kolumn
  scripts/             # env-shell, migrate, start_api, check_setup
    pomiar_actora.py   # JEDNORAZOWA diagnostyka actora — nie część pipeline'u
    znajdz_grupy.py    # RĘCZNIE, raz w miesiącu -> data/kandydaci_grupy.csv
    raport_gate.py     # rozliczenie trybu cienia bramki
    raport_feedback.py # co operator odrzucił i co model o tym sądził
    uzupelnij_klasyfikacje.py # dopisuje ekstrakcję wierszom, które ją zgubiły
    test_llm.py        # jedno wywołanie na providera — czy klucz i model działają
    porownaj_modele.py # wybór modelu na WŁASNYCH danych, nie na benchmarku
    pobierz_geo.py     # jednorazowe pobranie bazy kodów z GeoNames
    odswiez_proxy.py   # publiczna lista proxy z GitHuba -> WERYFIKACJA -> plik puli
  tests/               # testy offline (bez sieci) + integracyjne (z bazą)
    test_zapis_klasyfikacji.py  # sam INSERT do `posty`, na prawdziwym Postgresie
    test_przebieg_do_bazy.py    # CAŁY przebieg: Apify -> model -> baza -> alert
    test_uzupelnij_klasyfikacje.py # naprawa wierszy z pustą ekstrakcją
    test_ekstrakcja_referencyjna.py # dana z treści -> jej pole: fallback + walidacja
    conftest.py                 # krzyczy, gdy testy integracyjne się pominęły —
                                # bez TEST_DATABASE_URL zielony wynik nic nie znaczy
    dane/posty_referencyjne.jsonl   # zbiór do porównania modeli i pomiaru ekstrakcji
  .env.example
  requirements.txt
data/kandydaci_grupy.csv  # lista grup do ręcznego sprawdzenia (kolumna `publiczna`)
data/kody_eu.csv          # baza kodów pocztowych — commitowana, patrz data/README.md
docs/APIFY-PROXY.md       # po co proxy i jak je skonfigurować
docs/POMIAR-ACTORA.md     # co actor realnie robi i ile kosztuje (wynik pomiaru)
docs/WIELOJEZYCZNOSC.md   # kto co robi z językiem: bramka / klasyfikator / alert
panel/                    # PWA na telefon (Next.js 16 / React 19 / Tailwind 4)
  app/                    # lista, szczegół, mapa, statystyki
  components/             # karta zlecenia, filtry, pasek ostrzegawczy, push
  lib/                    # klient API, formatowanie, polling
  public/                 # manifest, service worker, PRAWDZIWE pliki ikon
  scripts/ikony.py        # generator ikon — odpalany RĘCZNIE, nie w runtime
ecosystem.config.js       # PM2: api + bot + panel (fetcher chodzi z crona)
setup.sh                  # PIERWSZE uruchomienie: venv, .env, baza, migracje, build, PM2
update.sh                 # aktualizacja maszyny: git -> pip -> migracje -> build -> restart
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

   **Ale „czyste wyjście" należy się WYŁĄCZNIE brakom blokującym start.** Braki
   dzielą się na dwie klasy (`config/settings.py`):

   | klasa | zmienne | reakcja |
   |---|---|---|
   | blokujące start | `DATABASE_URL` | bez bazy nie ma czego pokazywać |
   | degradujące | klucz modelu, Telegram, `API_TOKEN`, klucze Apify | podsystem milczy, reszta działa |

   Degradujący brak **nie ma prawa zatrzymać procesu**. Klasyfikator jest jednym
   z kilku podsystemów: bez klucza modelu fetcher dalej zbiera posty do bazy,
   bramka dalej punktuje, panel dalej pokazuje zebrane, a Telegram dalej dowozi.
   API wstaje wtedy normalnie, `/health` wypisuje braki w polu `braki`, a status
   brzmi `niepelna_konfiguracja` — i to jest stan **poprawny**, nie awaria.
   Proces kończący się na braku opcjonalnego klucza daje pod PM2 pętlę restartów
   ze statusem `errored`: w logach wygląda jak awaria kodu, a jest brakiem jednej
   linijki w `.env`.

   **Krytyczny jest wyłącznie klucz AKTYWNEGO providera** (`LLM_PROVIDER`):
   `anthropic` → `ANTHROPIC_API_KEY`, `openai` → `OPENAI_API_KEY` **oraz**
   niepusty `OPENAI_MODEL`, `gemini` → `GEMINI_API_KEY`. Klucze pozostałych są
   opcjonalne — ich brak pojawia się najwyżej jako informacja („porównanie modeli
   obejmie 1 z 3 providerów"), nigdy jako powód zatrzymania.
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

# Zapis wyniku klasyfikatora ma JEDEN test na prawdziwej bazie — bez DSN-a się
# pomija. Warto go puścić po każdej zmianie w kolumnach `posty`: sprawdza, czy
# komplet pól z ekstrakcji realnie dojeżdża do tabeli, a nie tylko do SQL-a.
#
# OSOBNA BAZA, NIGDY PRODUKCYJNA — test kasuje i zakłada tabele od zera.
# Nazwa MUSI zawierać "test", inaczej test odmawia startu.
createdb laweta_test    # raz
TEST_DATABASE_URL="postgresql://user:haslo@localhost/laweta_test" \
    python -m pytest laweta_radar/tests/test_zapis_klasyfikacji.py -q

export PYTHONPATH=$PWD
python -m uvicorn laweta_radar.api.main:app --host 127.0.0.1 --port 8002
curl -s localhost:8002/health | python -m json.tool
```

`/health` odpowiada **zawsze 200**, także gdy konfiguracja jest niepełna — kod mówi
„API żyje", a treść mówi, co jest zepsute. Traktowanie niewłączonego systemu jak
awarii mieszałoby dwie zupełnie różne sytuacje.

Braki wychodzą w polu `braki`, rozdzielone na `blokujace_start` i `degradujace`,
razem ze `skutki` — zdaniem o tym, co przez dany brak nie działa. Płaska lista
stawiała brak `DATABASE_URL` obok nieużywanego klucza nieaktywnego providera,
więc nie dało się z niej odczytać jedynej rzeczy, po którą się tu przychodzi:
czy system ma z czego żyć. Tę samą linię wypisuje API przy starcie, żeby dało się
ją znaleźć w `pm2 logs` bez odpytywania endpointu.

### Zanim cokolwiek pobierze

`config/groups.py` niesie 13 grup, z czego **5 ma status `ok`** i tylko te fetcher
pobiera. Podział wziął się z widoczności treści w wyszukiwarce — jedynego sygnału
publiczności dostępnego bez logowania na FB — i nie mówi nic o tym, czy w grupie
padają zgłoszenia, czy same reklamy lawet (szczegóły w komentarzu nad `FB_GRUPY`).
Żeby ruszyło:

1. wskaż wspólny `.env` z pulą Apify — `SHARED_ENV_PATH` w `.env` lawety albo
   symlink. Sprawdź, że działa:
   `python -m laweta_radar.workers.apify_keys` ma pokazać niezerową liczbę kluczy.
   Własnych `APIFY_API_TOKEN*` **nie wpisujesz** (patrz sekcja o współdzieleniu),
2. przejrzyj `config/groups.py`: wpisy `unverified` sprawdź ręcznie (publiczna?
   żywa? zgłoszeniowa czy sama reklama lawet?) i dopiero wtedy przestaw `status`
   na `"ok"`. To samo pytanie o treść zadaj piątce, która już ma `ok` — indeks
   wyszukiwarki dowiódł, że da się je pobrać, nie że warto. Kolejnych kandydatów
   nie wpisuj z pamięci — zbuduj listę wyszukiwarką:
   `python -m laweta_radar.scripts.znajdz_grupy` (patrz sekcja
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

#### Dwie różne pustki w polu

Produkcja na małym modelu (gpt-5.4-nano) pokazała wzorzec, który w bazie wygląda
jak brak danych, a jest niedoczytaniem: **pole jest wypełniane, gdy dana stoi
wprost i prosto, a pomijane, gdy wymaga choćby minimalnej interpretacji** —
nazwa miejscowości zagranicznej („Zulte"), kod pocztowy wśród innych liczb
(„z kodu 54-100"), marka w środku zdania („transportu dla Renault Trafic").
Zlecenia były wykrywane poprawnie; ginęła sama trasa.

Odpowiedź jest dwuwarstwowa, bo prompt można wzmocnić, ale nie da się go
wymusić:

1. **Prompt rozróżnia dwie pustki.** „Tego w poście NIE MA" (null jest wtedy
   poprawną odpowiedzią i tak ma zostać) oraz „jest, tylko trzeba przeczytać
   uważniej" (null jest wtedy błędem). Trzy reguły mówią wprost, że **każda**
   nazwa miejscowości, **każdy** ciąg wyglądający na kod i **każda** marka
   z treści mają trafić do swojego pola — a zakaz zgadywania zostaje nietknięty,
   bo to on broni przed wysłaniem człowieka 80 km w złą stronę. Na końcu promptu
   stoją cztery pary „post → oczekiwany JSON": trzy uczą, co wyciągnąć, czwarta
   — kiedy zostawić null. Na modelach tej klasy few-shot działa mocniej niż sama
   instrukcja, a bez tej czwartej pary zestaw uczyłby wypełniania pól za wszelką
   cenę, czyli halucynacji geo.
2. **Fallback regexowy działa POZA modelem.** Po klasyfikacji treść idzie przez
   `geo.znajdz_kody()` i uzupełnia `odbior_kod`/`dostawa_kod`, **wyłącznie gdy
   pole jest puste** — model ma pierwszeństwo, bo czyta zdanie, a regex kształt
   cyfr. Kolejność wystąpienia jest jedyną heurystyką kierunku (pierwszy kod to
   zwykle odbiór). Każde uzupełnienie leci na stderr ze znacznikiem
   `fallback-kod`, żeby dało się policzyć, jak często model gubi to, co regex
   znajduje za darmo — bez tej liczby nie da się ocenić kolejnej zmiany promptu:

   ```bash
   pm2 logs laweta-fetcher --lines 2000 --nostream | grep -c fallback-kod
   ```

Pilnuje tego `tests/test_ekstrakcja_referencyjna.py` na tym samym zbiorze, na
którym wybieramy model. Testy nie mierzą jakości modelu (od tego jest
`porownaj_modele.py`) — sprawdzają, że kod stojący w treści dojeżdża do pola
także wtedy, gdy model oddał komplet nulli, i że nic z poprawnie przeczytanego
posta nie ginie po drodze w walidacji.

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
python laweta_radar/scripts/test_llm.py                  # czy klucz i model w ogóle działają
python laweta_radar/scripts/porownaj_modele.py --sucho   # plan i koszt, BEZ sieci
python laweta_radar/scripts/porownaj_modele.py           # tabela porównawcza
```

Kolejność jest celowa. `test_llm.py` wysyła JEDNO krótkie pytanie na providera
i odpowiada na pytania „czy klucz działa", „czy nazwa modelu istnieje" i „czy
tryb JSON nie wywala błędu 400" — za ułamek ceny pełnego porównania. Trwały
błąd (zły klucz, nieznany model, odrzucony parametr) jest tam nazwany trwałym
i nie jest ponawiany.

`porownaj_modele.py` puszcza `tests/dane/posty_referencyjne.jsonl` przez
wszystkie skonfigurowane providery **w jednym przebiegu** i liczy per model:
trafność `czy_zlecenie`, trafność miasta odbioru i dostawy, trafność `pilnosc`,
**halucynacje geo**, **zgodność ze schematem**, ile razy wynik nie dał się
sparsować, medianę czasu odpowiedzi i realny koszt runu (z tokenami rozumowania
wliczonymi do rachunku, ale raportowanymi osobno).

**Halucynacja geo** to odsetek wpisanych miast, których w treści posta nie było
— najdroższy błąd tego systemu, bo zgadnięte miasto wysyła człowieka 80 km
w złą stronę, a puste pole tylko każe mu przeczytać post. **Zgodność ze
schematem** mówi, ile odpowiedzi mieści się w kontrakcie BEZ napraw walidatora:
wysoka trafność przy niskiej zgodności znaczy, że wynik ratuje nasz kod, a nie
model — i przestanie ratować przy pierwszej zmianie kontraktu.

#### OpenAI: co jest inne i dlaczego jest w konfiguracji

Anthropic bierze prompt systemowy osobnym parametrem, OpenAI wkłada go
pierwszą wiadomością (`OPENAI_ROLA_SYSTEMOWA`, domyślnie `system`). Nowe modele
OpenAI odrzucają `max_tokens` i wymagają `max_completion_tokens` — kod próbuje
nowszej nazwy, przy odmowie powtarza ze starszą i **zapamiętuje wynik na czas
procesu**, zamiast zgadywać z nazwy modelu. Część tych modeli generuje
wewnętrzne **tokeny rozumowania**: rozliczane jak wyjściowe i wliczane do
limitu, więc `OPENAI_REASONING` ustawia najniższy poziom, jaki dany model
przyjmuje, a gdy go nie przyjmuje — limit tokenów dostaje zapas, żeby
odpowiedź nie urwała się w środku JSON-a (co w raporcie wyglądałoby jak „model
nie umie w JSON").

`OPENAI_JSON_MODE` ma trzy wartości: `off` (najuczciwsze porównanie z Haiku —
obie strony dostają dokładnie to samo), `object` (wymuszony poprawny JSON, bez
schematu; domyślne) i `schema` (pełne structured outputs ze schematem
klasyfikatora, `services/schemat.py`).

> **Tryb JSON gwarantuje KSZTAŁT odpowiedzi, nie jej PRAWDZIWOŚĆ.** Model nadal
> może wpisać do `odbior.miasto` nazwę, której w poście nie było — dostaniesz ją
> tylko ładnie sformatowaną. Kolumny `halucyn.` tryb JSON nie poprawia i nie
> zastępuje. Przy `schema` nie porównujesz zresztą dwóch modeli, tylko dwa
> stacki: jeden z gwarancją schematu i jeden bez. Dlatego raport wypisuje tryb
> obok nazwy modelu — nie zestawiaj tabel zrobionych przy różnych ustawieniach.

Stawki modeli siedzą w `config/cennik.py` **razem z datą sprawdzenia**, nigdy
w kodzie providerów. Model spoza cennika daje koszt `None` i ostrzeżenie w logu,
nigdy cichą zerową kwotę — zero czyta się jako „za darmo" i przesuwa decyzję
o wyborze modelu na zmyślonych danych.

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

`geo.znajdz_kody()` **skanuje surową treść** i jest źródłem fallbacku
klasyfikatora, więc fałszywe trafienie kosztuje tu tyle, co zgadnięte miasto.
Formaty dwuznaczne wymagają kontekstu: czterocyfrowa liczba z przedziału
roczników (1950–2035) potrzebuje słowa wskazującego kraj albo skrótu kodu przed
sobą — sama nazwa własna obok nie wystarcza, bo w „Skoda Octavia 2012" jest nią
model auta, a nie miejscowość.

## Deploy (VPS + PM2 + nginx)

```bash
# na VPS-ie, jako użytkownik aplikacji
git clone <repo> /home/ubuntu/laweta-radar && cd /home/ubuntu/laweta-radar
./setup.sh                    # venv, zależności, .env, baza, migracje, panel, PM2
$EDITOR laweta_radar/.env     # klucze: ANTHROPIC_API_KEY, TELEGRAM_*, SHARED_ENV_PATH
./update.sh --force --bez-panelu   # przeładuj API i bota z uzupełnioną konfiguracją
pm2 startup                   # żeby wstało po reboocie (pm2 save robi setup.sh)
```

`setup.sh` **nie instaluje pakietów systemowych** (python3.11, node 20+, psql, pm2)
— to decyzja o stanie maszyny, nie o tym projekcie. Brakujące wypisze naraz,
z gotowymi poleceniami. **Nie nadpisuje istniejącego `.env`** i **nie instaluje
crona fetchera** (kredyt Apify i alerty to świadome włączenie, nie efekt uboczny
deployu — wpis wypisuje się na końcu do wklejenia).

Wygeneruje za to `API_TOKEN`, hasło do bazy, rolę i bazę w Postgresie, odpali
migracje i testy offline. Puszczony drugi raz uzupełnia tylko to, czego brakuje.

Ścieżki liczą się z położenia `ecosystem.config.js` (`__dirname`), więc repo
działa w dowolnym katalogu — `/home/ubuntu/laweta-radar`, `/srv/laweta`, katalog
domowy innego użytkownika. PM2 startuje procesy ze swojego środowiska, nie
z powłoki logowania, dlatego `scripts/start_api.sh` wczytuje `.env` jawnie.

### Instancja testowa obok produkcyjnej

```bash
git clone <repo> /home/ubuntu/laweta-test && cd /home/ubuntu/laweta-test
./setup.sh --instancja test
```

`INSTANCJA=test` w `.env` przestawia **wszystko, co jest globalne na maszynie**:
procesy nazywają się `laweta-test-api`, `laweta-test-bot`, `laweta-test-panel`,
API stoi na 8012, panel na 6210, baza to `laweta_test`. Bez tego `pm2 restart
laweta-api` z katalogu testowego ubiłby produkcję — nazwy PM2 są globalne
i katalog nie ma z nimi nic wspólnego.

Puste `INSTANCJA` (domyślne) = `laweta-api`, porty 8002/6200 — czyli dokładnie
to, co było. `update.sh` czyta tę samą zmienną, więc przeładowuje procesy TEJ
instancji, w której stoi.

Instancja testowa **nie ma crona**, więc niczego sama nie zbiera i nic nie
kosztuje. Do sprawdzenia, co by zrobiła:

```bash
./venv/bin/python -m laweta_radar.workers.fb_fetcher --sucho   # plan i koszt, bez sieci
bash laweta_radar/scripts/check_setup.sh                       # czego brakuje
```

W `.env` **nie wpisujesz kluczy Apify** — ustaw tylko `SHARED_ENV_PATH` na `.env`
sales-core-engine (domyślna ścieżka `/home/ubuntu/sales-core-engine/.env` zwykle
wystarcza). Użytkownik, z którego chodzi PM2, musi mieć prawo odczytu tamtego
pliku — jeśli go nie ma, laweta wstanie i będzie milczeć, bo rotator zobaczy zero
kluczy. Sprawdzisz to `bash laweta_radar/scripts/check_setup.sh`.

`pm2 start ecosystem.config.js` podnosi **trzy** procesy: `laweta-api`,
`laweta-bot` i `laweta-panel`. Bot jest osobny, bo wisi na long pollingu i przez
30 sekund nic nie robi — wpięcie go w pętlę uvicorna znaczyłoby, że panel czeka
na Telegrama. Panel jest w tym samym pliku, a nie dokładany z palca po deployu,
bo to jedno miejsce musi znać komplet procesów: `update.sh` przeładowuje po
nazwach, a `pm2 save` zapisuje to, co faktycznie chodzi.

Panel wymaga Node 20+ i zbudowanej powłoki — `setup.sh` i `update.sh` robią to
same. Ręcznie:

```bash
cd /home/ubuntu/laweta-radar/panel && npm ci && npm run build
```

nginx — API **musi** zostać na loopbacku. Token w nagłówku jest DRUGĄ warstwą,
nie jedyną: nasłuch na `0.0.0.0` wystawiłby bazę zleceń wprost do internetu
niezależnie od tokenu.

```nginx
server {
    server_name laweta.twojadomena.pl;

    # Panel (PWA) — to on jest publiczny i to on musi mieć TLS: bez HTTPS
    # przeglądarka nie zarejestruje service workera, więc nie ma ani offline'u,
    # ani web push.
    location / {
        proxy_pass http://127.0.0.1:6200;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # API pod /api — ta sama domena co panel, więc CORS w ogóle nie wchodzi
    # w grę (i dlatego `PANEL_URL` w .env zwykle zostaje puste).
    location /api/ {
        proxy_pass http://127.0.0.1:8002/;
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

# Podsumowanie tego, co uzbierało się w ciszy nocnej. Godzina musi wypadać PO
# `CISZA_NOCNA_DO` — inaczej podsumowanie samo trafia w ciszę nocną i nie idzie.
5 6 * * * cd /home/ubuntu/laweta-radar && ./venv/bin/python -m laweta_radar.services.powiadomienia --noc >> /var/log/laweta/powiadomienia.log 2>&1
```

Cron może chodzić **gęściej** niż odstęp grupy — harmonogram i tak przepuści tylko
te grupy, którym wypada. Odwrotnie się nie da: cron rzadszy niż `MIN_INTERWAL_MIN`
jest sufitem, którego harmonogram nie przeskoczy.

### Aktualizacja

Jedno polecenie — ściąga z GitHuba, dokłada zależności, migruje bazę, przebudowuje
panel i przeładowuje procesy:

```bash
cd /home/ubuntu/laweta-radar && ./update.sh
```

```
./update.sh --sucho        # co przyszło i co się z tego odpali; nic nie rusza
./update.sh --force        # przebuduj i przeładuj mimo braku nowych commitów
./update.sh --bez-panelu   # sam API + bot, bez trzyminutowego builda panelu
```

Skrypt robi **tylko to, co wynika z diffa**: `pip install` przy zmianie
`requirements.txt`, migracje przy nowym pliku `api/migrations/*.sql`, build panelu
przy zmianie w `panel/`. Bezwarunkowe robienie wszystkiego znaczyłoby trzy minuty
przestoju panelu po literówce poprawionej w README.

Restarty idą **na samym końcu**, po wszystkim, co może paść. Inaczej build panelu
(minuty) trwałby przy API na nowym kodzie, panelu na starym i bazie na starym
schemacie. Z tego samego powodu nieudane migracje **zatrzymują deploy przed
restartem** — system zostaje na starym kodzie, czyli w stanie, który działa.

Skrypt pilnuje też `WERSJA` w `panel/public/sw.js`: powłoka panelu zmieniona bez
podbicia wersji znaczy telefon, na którym raz zainstalowano PWA, zostający ze
starą wersją na zawsze (cache-first). Ostrzeżenie wypada przed buildem.

Na końcu odpytuje `/health` i port panelu — „gotowe" ma znaczyć, że **wstało**,
a nie że PM2 przyjął polecenie. Proces wstający i padający w pętli wygląda
w `pm2 restart` na sukces.

Ręcznie to samo, gdyby skrypt nie wchodził w grę:

```bash
cd /home/ubuntu/laweta-radar && git pull
./venv/bin/pip install -r laweta_radar/requirements.txt
bash laweta_radar/scripts/migrate.sh          # migracje są idempotentne (IF NOT EXISTS)
pm2 restart laweta-api laweta-bot
(cd panel && npm ci && npm run build) && pm2 restart laweta-panel
```

## Diagnostyka

| Objaw | Sprawdź |
|---|---|
| nic nie przychodzi na Telegram | `python -m laweta_radar.services.telegram_notify` |
| „brak kluczy Apify", choć są w `.env` | `source laweta_radar/scripts/env-shell.sh` |
| ile kluczy widzi rotator | `python -m laweta_radar.workers.apify_keys` |
| czy darmowe proxy w ogóle dochodzi do Apify | `python laweta_radar/scripts/odswiez_proxy.py` (wymaga sieci) |
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
| **czy fetcher jeszcze chodzi** | `curl -s localhost:8002/zdrowie` — pole `status` |
| czy klucze Apify mają kredyt i czy proxy odpowiada | `curl -s "localhost:8002/zdrowie?glebokie=1"` |
| jak wygląda alert, bez wysyłania | `python -m laweta_radar.services.powiadomienia --podglad` |
| czemu to zlecenie pokazuje 900 km | `python -m laweta_radar.services.geo "nazwa z posta"` |
| czemu ten alert nie przyszedł | log fetchera — `[powiadomienia] <fb_id>: pomijam (...)` |
| zlecenia są, alertów zero | log fetchera — linia `UWAGA: N zleceń i ANI JEDNEGO wysłanego alertu` |
| zlecenie bez typu, miasta i telefonu | log fetchera — `OSTRZEŻENIE: post <fb_id> ma w bazie werdykt modelu i ZERO pól z ekstrakcji` |
| jak odzyskać stare zlecenia bez ekstrakcji | `python laweta_radar/scripts/uzupelnij_klasyfikacje.py --sucho` |
| czy powiadomienia nie są wyciszone | `/stop` czy `/start` — ostatni wpis w tabeli `powiadomienia` |
| które grupy wyrzucić z konfiguracji | `curl -s localhost:8002/statystyki` albo `/statystyki` w panelu |
| co operator odrzucił i czemu model się mylił | `python laweta_radar/scripts/raport_feedback.py` |
| bot nie reaguje na przyciski | `pm2 logs laweta-bot`; sprawdź `TELEGRAM_CHAT_ID` |
| panel pokazuje ekran z tokenem | `API_TOKEN` w `.env` vs token wklejony w panelu |

Wszystkie narzędzia CLI działają bez sieci poza tymi, przy których napisano inaczej.
