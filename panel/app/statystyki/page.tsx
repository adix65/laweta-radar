"use client";

import { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { statystyki } from "@/lib/api";
import type { Statystyki } from "@/lib/typy";

/**
 * Statystyki: tydzień i miesiąc. DWA WYKRESY, BEZ OZDÓB.
 *
 * NAJWAŻNIEJSZA LICZBA NA TYM EKRANIE TO SKUTECZNOŚĆ PER GRUPA. Ona mówi, co
 * wyrzucić z `config/groups.py` — bez niej płacimy Apify za martwe grupy
 * w nieskończoność, bo grupa, która nie dowozi, wygląda w logach dokładnie tak
 * samo jak grupa, na której akurat był spokojny tydzień.
 *
 * Grupy z za małą próbką są POKAZANE, ale wyszarzone i podpisane. Usunięcie ich
 * z listy znaczyłoby, że nowa grupa jest niewidoczna dokładnie w okresie,
 * w którym trzeba zdecydować, czy ją zostawić — a grupa z jednym pobranym
 * postem i jednym zleceniem ma 100% i wygląda najlepiej w całym zestawieniu.
 */
export default function Statystyki() {
  const [dane, setDane] = useState<Statystyki | null>(null);
  const [blad, setBlad] = useState("");
  const [okno, setOkno] = useState("7");

  useEffect(() => {
    statystyki()
      .then(setDane)
      .catch((e) => setBlad(e instanceof Error ? e.message : "brak danych"));
  }, []);

  if (blad) return <main className="p-5 text-opis text-smiec">{blad}</main>;
  if (!dane) return <main className="p-5 text-opis text-tekst-cichy">Wczytuję…</main>;

  const okna = Object.keys(dane.okna).sort((a, b) => Number(a) - Number(b));
  const biezace = dane.okna[okno] ?? dane.okna[okna[0]];
  const l = biezace.lejek;

  const lejek = [
    { etap: "pobrane", ile: l.pobrane },
    { etap: "do AI", ile: l.do_ai },
    { etap: "zlecenia", ile: l.zlecen },
    { etap: "dzwonię", ile: l.dzwonie },
    { etap: "wygrane", ile: l.wygrane },
  ];

  const grupy = biezace.grupy.map((g) => ({
    ...g,
    // Procenty, nie ułamki: „0,7%" czyta się szybciej niż „0.007" na ekranie
    // trzymanym w jednej ręce.
    procent: Number((g.skutecznosc * 100).toFixed(2)),
  }));

  return (
    <main className="mx-auto max-w-2xl p-4">
      <h1 className="text-liczba">Statystyki</h1>

      <div role="group" aria-label="Okno czasu" className="mt-3 flex gap-2">
        {okna.map((o) => (
          <button
            key={o}
            type="button"
            aria-pressed={o === okno}
            onClick={() => setOkno(o)}
            className={`dotyk flex-1 rounded-full text-opis font-semibold ${
              o === okno ? "bg-tekst text-tlo" : "border border-obrys bg-karta text-tekst-cichy"
            }`}
          >
            {o} dni
          </button>
        ))}
      </div>

      <section className="mt-5 grid grid-cols-2 gap-3">
        <Kafel etykieta="Zleceń" wartosc={l.zlecen} />
        <Kafel etykieta="Wziętych" wartosc={l.dzwonie + l.wygrane} />
        <Kafel etykieta="Wygranych" wartosc={l.wygrane} />
        <Kafel etykieta="Przychód" wartosc={`${Math.round(l.przychod_pln)} zł`} />
        <Kafel etykieta="Oznaczone śmieć" wartosc={l.oznaczone_smiec} />
        <Kafel
          etykieta="Koszt Apify"
          wartosc={`${(l.pobrane * dane.cena_usd_za_post).toFixed(2)} USD`}
        />
      </section>

      <section className="mt-6">
        <h2 className="text-opis font-bold text-tekst-cichy">Lejek</h2>
        <p className="mb-2 text-xs text-tekst-cichy">
          Skok w którymkolwiek miejscu ma inne znaczenie: mało zleceń przy dużej
          liczbie pobranych to zła lista grup, dużo śmieci to zepsuty prompt.
        </p>
        <div className="h-56 w-full">
          <ResponsiveContainer>
            <BarChart data={lejek} margin={{ top: 4, right: 4, bottom: 4, left: -16 }}>
              <CartesianGrid stroke="#33333a" vertical={false} />
              <XAxis dataKey="etap" stroke="#b8b8c0" fontSize={11} />
              <YAxis stroke="#b8b8c0" fontSize={11} />
              <Tooltip
                contentStyle={{
                  background: "#17171a",
                  border: "1px solid #33333a",
                  borderRadius: 12,
                  color: "#ffffff",
                }}
              />
              <Bar dataKey="ile" fill="#ffffff" radius={[6, 6, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>

      <section className="mt-6">
        <h2 className="text-opis font-bold text-tekst-cichy">
          Skuteczność per grupa (zlecenia / pobrane posty)
        </h2>
        <p className="mb-2 text-xs text-tekst-cichy">
          Ta liczba mówi, które grupy wyrzucić z konfiguracji. Wyszarzone =
          mniej niż {dane.min_probka_grupy} pobranych postów, czyli za mała
          próbka, żeby cokolwiek z niej wnioskować.
        </p>
        <div className="h-64 w-full">
          <ResponsiveContainer>
            <BarChart
              data={grupy}
              layout="vertical"
              margin={{ top: 4, right: 12, bottom: 4, left: 4 }}
            >
              <CartesianGrid stroke="#33333a" horizontal={false} />
              <XAxis type="number" stroke="#b8b8c0" fontSize={11} unit="%" />
              <YAxis
                type="category"
                dataKey="grupa"
                stroke="#b8b8c0"
                fontSize={10}
                width={110}
                tickFormatter={(t: string) => (t.length > 18 ? `${t.slice(0, 17)}…` : t)}
              />
              <Tooltip
                contentStyle={{
                  background: "#17171a",
                  border: "1px solid #33333a",
                  borderRadius: 12,
                  color: "#ffffff",
                }}
                formatter={(v) => [`${v}%`, "skuteczność"] as [string, string]}
              />
              <Bar dataKey="procent" radius={[0, 6, 6, 0]}>
                {grupy.map((g) => (
                  <Cell key={g.grupa_url} fill={g.wiarygodne ? "#30d158" : "#4a4a52"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* Tabela pod wykresem, bo wykres nie poda kosztu w dolarach — a to jest
            liczba, po której się DZIAŁA: „ta grupa kosztowała 4 USD i dała zero
            zleceń" jest zdaniem, po którym się ją wyłącza. */}
        <ul className="mt-3 flex flex-col gap-2">
          {grupy.map((g) => (
            <li
              key={g.grupa_url}
              className="flex items-baseline justify-between gap-3 rounded-xl border border-obrys bg-karta p-3 text-opis"
            >
              <span className="min-w-0 flex-1 truncate">
                {g.grupa}
                {!g.wiarygodne && (
                  <span className="ml-2 text-xs text-tekst-cichy">(mała próbka)</span>
                )}
              </span>
              <span className="shrink-0 text-tekst-cichy">
                {g.zlecen}/{g.pobrane} · {g.procent}% · {g.koszt_usd.toFixed(2)} USD
              </span>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}

function Kafel({ etykieta, wartosc }: { etykieta: string; wartosc: number | string }) {
  return (
    <div className="rounded-2xl border border-obrys bg-karta p-4">
      {/* Liczba duża, opis mały — ta sama hierarchia co na karcie zlecenia. */}
      <p className="text-liczba">{wartosc}</p>
      <p className="mt-1 text-xs uppercase tracking-wide text-tekst-cichy">{etykieta}</p>
    </div>
  );
}
