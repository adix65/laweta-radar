"""Lista grup FB, z których pobieramy posty — dane, nie kod.

Trzymana osobno od fetchera z tego samego powodu co w repo źródłowym: dopisanie
grupy to czynność OPERACYJNA (robi ją człowiek, który zna region, a niekoniecznie
Pythona), więc nie może wymagać grzebania w workerze ani deployu logiki.

STRUKTURA WPISU:
  url      — link do grupy. WYMAGANY; wpis bez url fetcher pomija.
  name     — nazwa grupy. Idzie do promptu klasyfikatora jako kontekst
             ("Pomoc drogowa Podkarpacie" mówi modelowi więcej o postach
             w środku niż sam identyfikator) i służy za etykietę w alertach.
  region   — do której części kraju grupa realnie ciąży. NIE filtruje pobierania
             (grupa "Pomoc drogowa Polska" ma posty z całego kraju) — służy do
             analityki: która grupa dowozi zlecenia w zasięgu lawety, a która
             tylko pali budżet Apify.
  status   — "ok" (grupa ZWERYFIKOWANA: publiczna, żywa, są w niej realne
             zgłoszenia) albo "unverified" (dodana, NIESPRAWDZONA). Fetcher
             pobiera WYŁĄCZNIE status=="ok". Brak klucza = "ok".

DLACZEGO status, a nie po prostu lista: actor Apify czyta tylko grupy PUBLICZNE,
ale run na grupie prywatnej i tak się odpali i i tak zostanie policzony. Czy grupa
jest publiczna, nie da się sprawdzić z zewnątrz — FB pokazuje niezalogowanym
ścianę logowania. Musi to zrobić człowiek. Dlatego nową grupę wrzucamy jako
"unverified", a na "ok" przestawiamy pojedynczo po ręcznym sprawdzeniu, zamiast
palić kredyt na grupy prywatne i martwe.

JAK ZWERYFIKOWAĆ (zalogowany FB, wejdź w grupę):
  1. publiczna czy prywatna,
  2. czy są posty z ostatnich kilku dni,
  3. czy to grupa ZGŁOSZENIOWA (ludzie proszą o pomoc), a nie ogłoszeniowa
     (firmy reklamują swoje usługi). Grupa samych reklam lawet nie dowiezie
     ani jednego zlecenia, a kosztuje tyle samo co każda inna.
Dopiero wtedy status -> "ok".

UWAGA na koszt: każda grupa ze statusem "ok" to osobne wywołanie Apify w KAŻDYM
przebiegu. Lista jest celowo krótka i regionalna — 40 grup ogólnopolskich kosztuje
40 razy więcej niż 5 lokalnych i dowozi te same zlecenia spod Warszawy, po których
nikt stąd nie pojedzie.
"""
from __future__ import annotations

# SKĄD SIĘ WZIĄŁ PODZIAŁ NA "ok" I "unverified" NA TEJ LIŚCIE
#
# Rozstrzygnął go JEDEN sygnał: czy w wynikach wyszukiwarki dało się zobaczyć
# TREŚĆ postów z grupy. To jedyny dowód publiczności dostępny z zewnątrz, bo
# grupa prywatna nie ma zindeksowanych treści — nie ma czego pokazać, nawet gdy
# sama grupa istnieje i ma dziesięć tysięcy członków. Skoro więc wyszukiwarka
# oddaje posty, to samo zobaczy actor Apify i taki wpis dostaje "ok".
# Tam, gdzie w wynikach była SAMA NAZWA grupy bez treści, publiczność jest
# niepotwierdzona: wpis zostaje "unverified", żeby nie płacić za run, który
# może nie zwrócić ani jednego posta.
#
# CZEGO TEN SYGNAŁ NIE MÓWI: czy grupa jest ZGŁOSZENIOWA (ludzie proszą
# o przewóz auta) czy OGŁOSZENIOWA (firmy reklamują swoje wolne miejsca na
# lawecie). Indeksują się tak samo, kosztują tak samo — a ogłoszeniowa nie
# dowiezie ani jednego zlecenia, bo w środku nie ma klientów, tylko konkurencja.
# Statusu "ok" nie należy więc czytać jako "grupa wartościowa", tylko jako
# "grupa da się pobrać". Rozstrzyga to dopiero raport wydajności
# (zlecenia / pobrane posty w oknie OKNO_WYDAJNOSCI_DNI) albo dwie minuty
# człowieka zalogowanego na FB, który spojrzy na ostatnie posty.
FB_GRUPY: list[dict[str, str]] = [
    # ── PUBLICZNOŚĆ POTWIERDZONA (treść postów widoczna w wyszukiwarce) ──
    {"url": "https://www.facebook.com/groups/132548385153051/",
     "name": "Wolne miejsca na lawetach BE/NL/DE",
     "region": "zagranica", "status": "ok"},
    {"url": "https://www.facebook.com/groups/856010917787518/",
     "name": "Wolne miejsce Belgia-Holandia-Niemcy-Polska",
     "region": "zagranica", "status": "ok"},
    {"url": "https://www.facebook.com/groups/330577835353723/",
     "name": "Wolna laweta BE/NL/DE do Polski",
     "region": "zagranica", "status": "ok"},
    {"url": "https://www.facebook.com/groups/422508107838058/",
     "name": "Transport aut Niemcy Belgia Holandia",
     "region": "zagranica", "status": "ok"},
    {"url": "https://www.facebook.com/groups/2369283210002047/",
     "name": "Auto laweta Transport HO/BE/DE",
     "region": "zagranica", "status": "ok"},

    # ── PUBLICZNOŚĆ NIEPOTWIERDZONA (w wynikach była sama nazwa grupy) ──
    {"url": "https://www.facebook.com/groups/1412593546181060/",
     "name": "Transport LAWETA Niemcy-Polska | Przerzuty DE/NL/BE",
     "region": "zagranica", "status": "unverified"},
    {"url": "https://www.facebook.com/groups/478394099303683/",
     "name": "Laweciarze | Wolne lawety | Wolne ladunki",
     "region": "krajowy", "status": "unverified"},
    {"url": "https://www.facebook.com/groups/625258040958010/",
     "name": "Gielda Lawet | Zlece przewoz",
     "region": "krajowy", "status": "unverified"},
    {"url": "https://www.facebook.com/groups/gieldatransportu/",
     "name": "Laweciarze Gielda Transportu",
     "region": "krajowy", "status": "unverified"},
    {"url": "https://www.facebook.com/groups/gieldatransportowa/",
     "name": "Polski Transport Gielda Transportowa",
     "region": "krajowy", "status": "unverified"},
    {"url": "https://www.facebook.com/groups/2036193719947874/",
     "name": "Gielda Transportowa - TRANS Polska",
     "region": "krajowy", "status": "unverified"},
    {"url": "https://www.facebook.com/groups/262962694265908/",
     "name": "Gielda Transportowa",
     "region": "krajowy", "status": "unverified"},
    {"url": "https://www.facebook.com/groups/www.autopomoc.eu/",
     "name": "Bezplatna Gielda Ladunkow",
     "region": "krajowy", "status": "unverified"},
]

# ── Parametry pobierania per grupa ──────────────────────────────────────────
# Zlecenie na lawetę wygrywa ten, kto odpisze pierwszy — nie ten, kto pobierze
# najwięcej postów. Dlatego bierzemy PŁYTKO i CZĘSTO, odwrotnie niż przy leadach
# sprzedażowych, gdzie post sprzed trzech dni jest wart tyle samo co dzisiejszy.
POSTOW_NA_GRUPE = 10        # ile najnowszych postów ciągniemy z jednej grupy
APIFY_ACTOR = "apify~facebook-groups-scraper"
APIFY_SORT = "CHRONOLOGICAL"   # nie "TOP" — interesuje nas świeżość, nie popularność
APIFY_TIMEOUT = 300            # s na jeden run actora; po tym czasie run jest stracony


# ── BUDŻET I HARMONOGRAM (workers/fb_fetcher.py) ────────────────────────────
#
# WSZYSTKO PONIŻEJ LICZY SIĘ W POBRANYCH POSTACH, nie w runach. Apify rozlicza
# tego actora ZA POBRANY POST, więc run jest darmowy, a jego zawartość nie jest.
# To odwraca dwie intuicje:
#   • batchowanie grup w jednym runie NIE oszczędza kredytu — oszczędza tylko
#     narzut uruchomienia (i tak jest zabronione, jeśli pomiar pokaże, że
#     `resultsLimit` jest globalny, patrz docs/POMIAR-ACTORA.md, pytanie 2);
#   • płacimy także za post, który widzieliśmy dwadzieścia razy. Deduplikacja
#     w bazie chroni model i Telegram, ale nie chroni rachunku za Apify.

# Cena katalogowa jednego pobranego posta. UWAGA — NIE ZWERYFIKOWANA na stronie
# actora: polityka sieciowa środowiska, w którym powstawał ten kod, blokuje
# apify.com. W założeniach do fetchera pada rząd wielkości 2,60 USD za 1000
# postów i taką wartość tu przyjęto; `scripts/pomiar_actora.py` ma własną,
# WYŻSZĄ (0,005 USD/post) z wcześniejszego odczytu. Rozjazd jest świadomy
# i rozstrzyga go POMIAR (pytanie 3), a nie kolejny odczyt ze strony — do tego
# czasu ta liczba służy WYŁĄCZNIE do szacunku pokazywanego przed wydaniem
# pieniędzy (`--sucho`), nigdy do decyzji podejmowanych automatycznie.
CENA_USD_ZA_POST = 0.0026

# Widełki odstępu między przebiegami JEDNEJ grupy.
# Dolna granica zależy od ścieżki z pomiaru actora i to jest różnica finansowa,
# nie kosmetyczna: w ścieżce A koszt dobowy nie zależy od częstotliwości (actor
# oddaje sam przyrost), a w ścieżce B każdy przebieg to pełny `resultsLimit`
# opłaconych postów — czyli gęstsze pytanie boli wprost proporcjonalnie.
MIN_INTERWAL_MIN_A = 5
MIN_INTERWAL_MIN_B = 15
MAX_INTERWAL_MIN = 120      # nawet martwa grupa dostaje szansę dwa razy na dobę... i tyle

# Dobowa pula postów dla grupy BEZ historii. Bez niej nowa grupa nigdy nie
# zbierze danych, na podstawie których bandyta mógłby jej cokolwiek przyznać —
# a grupa bez danych wygląda dla bandyty tak samo jak grupa bezwartościowa.
PULA_STARTOWA_POSTOW = 60

# Z ilu dni liczymy wydajność grupy (zlecenia / pobrane posty) dla bandyty.
# Siedem, bo to najkrótsze okno obejmujące pełny tydzień: soboty i niedziele
# mają inny ruch niż wtorek, a okno pięciodniowe kazałoby bandycie porównywać
# grupy zmierzone w różnych dniach tygodnia.
OKNO_WYDAJNOSCI_DNI = 7

# Limit adaptacyjny per grupa — okna liczone w GODZINACH, nie w dniach jak
# w repo źródłowym. Post na lawetę żyje kilkadziesiąt minut, więc doba jest tu
# jednostką bezużyteczną: mieści całe życie i śmierć zlecenia.
OKNO_TEMPA_H = 24 * 7       # z ilu godzin historii liczymy tempo grupy
MIN_POSTOW_NA_GRUPE = 2     # podłoga — cicha grupa nigdy nie spada do zera
DOMYSLNIE_POSTOW_NA_GRUPE = 10   # bootstrap dla grupy bez historii
ZAPAS_NA_PACZKE = 3         # mnożnik nad średnim tempem

# Sufit limitu — RÓŻNY dla obu ścieżek, i to jest sedno różnicy między nimi.
# ŚCIEŻKA A: koszt tnie warunek wieku, więc actor i tak przerwie paginację
#   wcześniej. Hojny sufit jest tu ZALETĄ — chroni przed zgubieniem paczki
#   postów, którą moderator zatwierdził naraz.
# ŚCIEŻKA B: limit JEST kosztem, co do sztuki. Każdy punkt to wydane pieniądze,
#   więc sufit musi być ciasny, a jego podniesienie jest decyzją finansową.
MAX_POSTOW_NA_GRUPE_A = 50
MAX_POSTOW_NA_GRUPE_B = 12

# `onlyPostsNewerThan` liczymy jako odstęp grupy razy ten mnożnik, z podłogą.
# Dwukrotność, bo moderatorzy grup zatwierdzają posty z opóźnieniem: post
# opublikowany tuż przed poprzednim przebiegiem bywa widoczny dopiero teraz,
# a okno równe odstępowi wycięłoby go bezpowrotnie. Podłoga jest po to, żeby
# przy odstępie 5 minut nie pytać o okno, którego actor może nie obsłużyć.
MNOZNIK_OKNA = 2
MIN_OKNO_MIN = 30


def grupy_do_pobrania(grupy=None) -> list[dict[str, str]]:
    """Tylko wpisy nadające się do pobrania: zweryfikowane i z adresem.

    Filtr jest tutaj, a nie w fetcherze, żeby dało się go przetestować bez sieci
    i żeby fetcher nie musiał znać znaczenia statusów.
    """
    src = FB_GRUPY if grupy is None else grupy
    return [g for g in src
            if (g.get("url") or "").strip() and (g.get("status") or "ok") == "ok"]


def opis_listy(grupy=None) -> str:
    """Linia do logu startowego: ile grup pobieramy, ile czeka na weryfikację."""
    src = FB_GRUPY if grupy is None else grupy
    gotowe = len(grupy_do_pobrania(src))
    bez_url = sum(1 for g in src if not (g.get("url") or "").strip())
    return (f"[groups] {gotowe} grup do pobrania z {len(src)} wpisów "
            f"({len(src) - gotowe} niezweryfikowanych, w tym {bez_url} bez adresu)")


# Podgląd bez odpalania fetchera:
#   python -m laweta_radar.config.groups
if __name__ == "__main__":
    print(opis_listy())
    for g in FB_GRUPY:
        znacznik = "ok " if g in grupy_do_pobrania() else "-- "
        print(f"  {znacznik} {g.get('name') or '?'}  [{g.get('region') or '?'}]  "
              f"{g.get('url') or '(brak adresu)'}")
