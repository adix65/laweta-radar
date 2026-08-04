"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { listaZlecen } from "@/lib/api";
import { etykietaPilnosci, km, trasa, wiek, zl } from "@/lib/format";
import type { Zlecenie } from "@/lib/typy";

/**
 * Mapa poglądowa: wszystkie aktywne zlecenia jako pinezki wokół bazy.
 *
 * LEAFLET + KAFELKI OpenStreetMap, ZERO KLUCZY. Google Maps JS API kosztuje,
 * wymaga klucza, karty w Google Cloud i pilnowania limitów — a to jest widok
 * POGLĄDOWY, otwierany kilka razy w tygodniu, żeby zobaczyć, czy zlecenia
 * układają się w trasę. Płacenie za to abonamentu jest marnotrawstwem.
 *
 * SAM LEAFLET, BEZ react-leaflet. Warstwa reactowa nad Leafletem dokłada
 * własny cykl życia i własną zgodność z wersjami Reacta, a jedyne, czego tu
 * potrzeba, to „narysuj pinezki raz i podmień je, gdy przyjdą nowe dane".
 * Import jest DYNAMICZNY, bo Leaflet dotyka `window` przy samym wczytaniu
 * modułu i wywala render po stronie serwera.
 *
 * WSPÓŁRZĘDNE PRZYCHODZĄ Z API (`lat`/`lon`), nie są wyłuskiwane z linku.
 * Link nawigacji dla dokładnego trafienia niesie NAZWĘ miasta, bo Google
 * znajdzie jego centrum lepiej niż nasza tablica — czyli parsowanie linku
 * zostawiłoby bez pinezki akurat najlepiej rozpoznane zlecenia.
 * `lat === null` znaczy „nie wiemy, gdzie to jest": takie zlecenia idą listą
 * pod mapą, z powodem. Pinezka postawiona „gdzieś" jest gorsza niż jej brak,
 * bo wygląda dokładnie tak samo jak pinezka pewna.
 */

// Kolory zgodne z paskiem pilności na karcie — ten sam czerwony znaczy to samo
// na obu ekranach. Rozjazd palet między widokami to najtańszy sposób na to,
// żeby kolor przestał cokolwiek znaczyć.
const KOLORY: Record<string, string> = {
  pilne: "#ff453a",
  dzis: "#ffd60a",
  planowane: "#30d158",
};

export default function Mapa() {
  const kontener = useRef<HTMLDivElement>(null);
  const mapa = useRef<unknown>(null);
  const [zlecenia, setZlecenia] = useState<Zlecenie[]>([]);
  const [wybrane, setWybrane] = useState<Zlecenie | null>(null);
  const [blad, setBlad] = useState("");

  useEffect(() => {
    listaZlecen({ status: "nowe", limit: 200 })
      .then((o) => setZlecenia(o.zlecenia))
      .catch((e) => setBlad(e instanceof Error ? e.message : "brak danych"));
  }, []);

  useEffect(() => {
    if (!kontener.current || zlecenia.length === 0) return;
    let zywe = true;

    (async () => {
      const L = (await import("leaflet")).default;
      await import("leaflet/dist/leaflet.css");
      if (!zywe || !kontener.current) return;

      const punkty = zlecenia
        .filter((z) => z.lat != null && z.lon != null)
        .map((z) => ({ z, ll: [z.lat as number, z.lon as number] as [number, number] }));
      if (punkty.length === 0) return;

      if (!mapa.current) {
        const m = L.map(kontener.current, { zoomControl: true, attributionControl: true });
        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
          maxZoom: 18,
          attribution: "© OpenStreetMap",
        }).addTo(m);
        mapa.current = m;
      }
      const m = mapa.current as ReturnType<typeof L.map>;

      punkty.forEach(({ z, ll }) => {
        L.circleMarker(ll, {
          radius: 11,
          color: "#0a0a0b",
          weight: 2,
          fillColor: KOLORY[z.pilnosc ?? "planowane"] ?? KOLORY.planowane,
          fillOpacity: 1,
        })
          .addTo(m)
          // Kliknięcie pinezki -> karta zlecenia pod mapą, nie popup Leafleta:
          // popup ma kilkanaście pikseli wysokości przycisku zamykającego,
          // czyli poniżej progu 56 px, i nie da się go trafić kciukiem.
          .on("click", () => setWybrane(z));
      });

      m.fitBounds(L.latLngBounds(punkty.map((p) => p.ll)).pad(0.25), {
        maxZoom: 11,
      });
    })();

    return () => {
      zywe = false;
    };
  }, [zlecenia]);

  const bezPozycji = zlecenia.filter((z) => z.lat == null || z.lon == null);

  return (
    <main className="mx-auto max-w-2xl p-4">
      <h1 className="text-liczba">Mapa</h1>
      <p className="mt-1 text-opis text-tekst-cichy">
        {zlecenia.length} aktywnych zleceń. Dotknij pinezki, żeby zobaczyć kartę.
      </p>

      {blad ? <p className="mt-3 text-opis text-smiec">{blad}</p> : null}

      <div
        ref={kontener}
        role="application"
        aria-label="Mapa zleceń"
        className="mt-3 h-[55vh] w-full overflow-hidden rounded-2xl border border-obrys bg-karta"
      />

      {wybrane ? (
        <Link
          href={`/zlecenie/${encodeURIComponent(wybrane.fb_id)}`}
          className="mt-3 block rounded-2xl border border-obrys bg-karta p-4"
        >
          <p className="flex items-baseline gap-3">
            <span className="text-liczba">{km(wybrane.km_od_bazy)}</span>
            <span className="text-liczba text-tekst-cichy">
              {zl(wybrane.szacunek_pln)}
            </span>
          </p>
          <p className="mt-1 text-xs font-bold uppercase tracking-wide text-tekst-cichy">
            {etykietaPilnosci(wybrane.pilnosc)}
          </p>
          <p className="mt-1 font-semibold">{trasa(wybrane)}</p>
          <p className="text-opis text-tekst-cichy">
            {wiek(wybrane.opublikowany_at)} · {wybrane.grupa_nazwa ?? "grupa nieznana"}
          </p>
          <p className="mt-2 text-opis underline">Otwórz szczegóły →</p>
        </Link>
      ) : null}

      {bezPozycji.length > 0 && (
        <section className="mt-5">
          <h2 className="text-opis font-bold text-tekst-cichy">
            Bez pozycji na mapie ({bezPozycji.length})
          </h2>
          <p className="mt-1 text-opis text-tekst-cichy">
            Nie rozpoznaliśmy miejsca na tyle pewnie, żeby postawić pinezkę.
            Zlecenia SĄ w systemie — otwórz i przeczytaj oryginał posta.
          </p>
          <ul className="mt-2 flex flex-col gap-2">
            {bezPozycji.map((z) => (
              <li key={z.fb_id}>
                <Link
                  href={`/zlecenie/${encodeURIComponent(z.fb_id)}`}
                  className="dotyk w-full justify-between rounded-xl border border-obrys bg-karta px-4 text-opis"
                >
                  <span className="truncate">{trasa(z)}</span>
                  <span className="text-tekst-cichy">{wiek(z.opublikowany_at)}</span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}
