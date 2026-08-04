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

# Wszystkie wpisy startują jako "unverified" — repo nie zna jeszcze regionu
# operatora ani tego, które grupy są publiczne. Odblokowuj pojedynczo, po
# weryfikacji opisanej wyżej.
FB_GRUPY: list[dict[str, str]] = [
    # ── POMOC DROGOWA / LAWETY — grupy zgłoszeniowe ──
    {"url": "", "name": "Pomoc drogowa — Twoje województwo", "region": "lokalny", "status": "unverified"},
    {"url": "", "name": "Laweta / transport aut — Twoje województwo", "region": "lokalny", "status": "unverified"},

    # ── MOTORYZACJA REGIONALNA — tu padają "zepsułem się pod...", nawet gdy
    #    grupa nie jest o pomocy drogowej ──
    {"url": "", "name": "Motoryzacja — Twoje miasto", "region": "lokalny", "status": "unverified"},
    {"url": "", "name": "Ogłoszenia / kupię-sprzedam — Twoje miasto", "region": "lokalny", "status": "unverified"},

    # ── TRANSPORT AUT / GIEŁDY ZLECEŃ — zlecenia planowane (przewóz auta
    #    z komisu, z zagranicy), inny rytm niż awaria: mniej pilne, ale
    #    większa wartość pojedynczego kursu ──
    {"url": "", "name": "Transport aut / giełda zleceń", "region": "krajowy", "status": "unverified"},
]

# ── Parametry pobierania per grupa ──────────────────────────────────────────
# Zlecenie na lawetę wygrywa ten, kto odpisze pierwszy — nie ten, kto pobierze
# najwięcej postów. Dlatego bierzemy PŁYTKO i CZĘSTO, odwrotnie niż przy leadach
# sprzedażowych, gdzie post sprzed trzech dni jest wart tyle samo co dzisiejszy.
POSTOW_NA_GRUPE = 10        # ile najnowszych postów ciągniemy z jednej grupy
APIFY_ACTOR = "apify~facebook-groups-scraper"
APIFY_SORT = "CHRONOLOGICAL"   # nie "TOP" — interesuje nas świeżość, nie popularność
APIFY_TIMEOUT = 300            # s na jeden run actora; po tym czasie run jest stracony


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
