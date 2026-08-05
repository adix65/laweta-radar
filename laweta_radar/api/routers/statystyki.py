"""`/statystyki` — liczby, które decydują, na co wydajemy pieniądze.

NAJWAŻNIEJSZA LICZBA W CAŁYM ENDPOINCIE TO SKUTECZNOŚĆ PER GRUPA: zlecenia
podzielone przez pobrane posty. Bez niej płacimy Apify za martwe grupy w
nieskończoność i nigdy się o tym nie dowiemy — grupa, która nie dowozi, wygląda
w logach dokładnie tak samo jak grupa, na której akurat był spokojny tydzień.
Ta jedna kolumna mówi, co wyrzucić z `config/groups.py`.

Reszta liczb opisuje LEJEK, i to w tej kolejności, w której koszt rośnie:

    pobrane posty       płacimy Apify za każdy (config/groups.CENA_USD_ZA_POST)
      -> odsiane bramką darmowe, im więcej tym lepiej
      -> wysłane do AI   płacimy za tokeny
      -> zlecenia        to, po co ten system istnieje
      -> powiadomienia   to, co realnie zobaczył operator
      -> śmieci          pomyłki modelu, czyli materiał do poprawiania promptu

Skok w którymkolwiek miejscu ma inne znaczenie i inną cenę. „Dużo pobranych, mało
zleceń" to zła lista grup. „Dużo zleceń, mało powiadomień" to za ostry próg
pewności albo cisza nocna. „Dużo powiadomień, dużo śmieci" to zepsuty prompt —
i to jest jedyny z tych trzech przypadków, w którym operator sam się zorientuje,
bo telefon dzwoni bez sensu.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from laweta_radar.api import db
from laweta_radar.api.auth import wymagaj_tokenu

router = APIRouter(tags=["statystyki"], dependencies=[Depends(wymagaj_tokenu)])

# Poniżej tylu pobranych postów skuteczność grupy jest szumem, nie pomiarem.
# Grupa z jednym pobranym postem i jednym zleceniem ma 100% i wygląda najlepiej
# w całym zestawieniu — a to jest dokładnie ten wniosek, przez który wyrzuca się
# grupę dobrą i zostawia przypadek.
MIN_PROBKA_GRUPY = 30


def _okno(cur, dni: int) -> dict:
    """Lejek za ostatnie N dni. Jedno zapytanie, bo to jedno przejście po tabeli."""
    cur.execute(
        """
        SELECT count(*)                                             AS pobrane,
               count(*) FILTER (WHERE gate_werdykt IS FALSE)        AS odsiane_bramka,
               count(*) FILTER (WHERE ai_at IS NOT NULL)            AS do_ai,
               count(*) FILTER (WHERE czy_zlecenie)                 AS zlecen,
               count(*) FILTER (WHERE status = 'smiec' AND czy_zlecenie)
                                                                    AS oznaczone_smiec,
               count(*) FILTER (WHERE status = 'dzwonie')           AS dzwonie,
               count(*) FILTER (WHERE status = 'wygrane')           AS wygrane,
               count(*) FILTER (WHERE status = 'przegrane')         AS przegrane,
               count(*) FILTER (WHERE stale)                        AS za_stare,
               COALESCE(sum(cena_koncowa) FILTER (WHERE status = 'wygrane'), 0)
                                                                    AS przychod_pln
          FROM posty
         WHERE pobrany_at > NOW() - make_interval(days => %s)
        """,
        (dni,),
    )
    wynik = dict(cur.fetchone())

    # Powiadomienia liczymy osobno, bo mieszkają w innej tabeli i mają własny
    # znacznik czasu — alert o poście sprzed sześciu dni mógł pójść wczoraj.
    if db.tabela_istnieje(cur.connection, "powiadomienia"):
        cur.execute(
            "SELECT count(*) FILTER (WHERE kanal = 'telegram')  AS wyslane, "
            "       count(*) FILTER (WHERE kanal = 'zbiorcze')  AS zbiorcze, "
            "       count(*) FILTER (WHERE kanal = 'pominiete_limit') AS pominiete "
            "  FROM powiadomienia "
            " WHERE wyslano_at > NOW() - make_interval(days => %s)", (dni,))
        wynik["powiadomienia"] = dict(cur.fetchone())
    else:
        wynik["powiadomienia"] = {"blad": "brak tabeli — odpal migrację 0005"}

    wynik["przychod_pln"] = float(wynik["przychod_pln"] or 0)
    return wynik


def _grupy(cur, dni: int) -> list[dict]:
    """Skuteczność per grupa: zlecenia / pobrane posty. Sortowanie po niej.

    Grupy poniżej `MIN_PROBKA_GRUPY` są NA LIŚCIE, ale z `wiarygodne=false`
    i na końcu — usunięcie ich z wyniku znaczyłoby, że nowa grupa jest niewidoczna
    dokładnie w tym okresie, w którym trzeba zdecydować, czy ją zostawić.
    """
    cur.execute(
        """
        SELECT COALESCE(grupa_nazwa, grupa_url)              AS grupa,
               grupa_url,
               count(*)                                      AS pobrane,
               count(*) FILTER (WHERE czy_zlecenie)          AS zlecen,
               count(*) FILTER (WHERE status = 'wygrane')    AS wygrane,
               max(pobrany_at)                               AS ostatni_post
          FROM posty
         WHERE pobrany_at > NOW() - make_interval(days => %s)
         GROUP BY 1, 2
        """,
        (dni,),
    )
    grupy = []
    for w in cur.fetchall():
        wiersz = dict(w)
        pobrane = wiersz["pobrane"] or 0
        wiersz["skutecznosc"] = round(wiersz["zlecen"] / pobrane, 4) if pobrane else 0.0
        wiersz["wiarygodne"] = pobrane >= MIN_PROBKA_GRUPY
        # Koszt w USD po cenie KATALOGOWEJ — realną zna tylko pomiar actora
        # (docs/POMIAR-ACTORA.md). Podana wprost, bo „ta grupa kosztowała 4 USD
        # i dała zero zleceń" jest zdaniem, po którym się działa, a „0.3%
        # skuteczności" nie jest.
        wiersz["koszt_usd"] = round(pobrane * _cena_za_post(), 4)
        grupy.append(wiersz)

    return sorted(grupy, key=lambda g: (g["wiarygodne"], g["skutecznosc"]), reverse=True)


def _cena_za_post() -> float:
    from laweta_radar.config import groups  # noqa: PLC0415 — tylko do tej liczby

    return float(getattr(groups, "CENA_USD_ZA_POST", 0.0))


@router.get("/statystyki")
def statystyki(dni: list[int] = Query(default=[7, 30])) -> dict:
    """Lejek i skuteczność grup za podane okna (domyślnie 7 i 30 dni).

    Dwa okna, a nie jedno, bo mówią różne rzeczy: siedem dni pokazuje, co się
    dzieje TERAZ (po ostatniej zmianie promptu albo listy grup), a trzydzieści
    daje próbkę, na której skuteczność grupy przestaje być szumem.
    """
    wynik: dict = {"okna": {}}
    with db.polaczenie() as conn:
        with conn.cursor() as cur:
            for okno in sorted(set(dni))[:4]:
                wynik["okna"][str(okno)] = {
                    "lejek": _okno(cur, okno),
                    "grupy": _grupy(cur, okno),
                }
    wynik["min_probka_grupy"] = MIN_PROBKA_GRUPY
    wynik["cena_usd_za_post"] = _cena_za_post()
    return wynik
