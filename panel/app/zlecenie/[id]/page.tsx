"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import PasekOstrzegawczy from "@/components/PasekOstrzegawczy";
import { jednoZlecenie, zmienZlecenie } from "@/lib/api";
import {
  etykietaPilnosci,
  km,
  kolorPilnosci,
  linkTel,
  telefonCzytelnie,
  trasa,
  wiek,
  zl,
} from "@/lib/format";
import type { Status, Zlecenie } from "@/lib/typy";

/**
 * Ekran szczegółu.
 *
 * TRZY PRZYCISKI PRZEZ CAŁĄ SZEROKOŚĆ, W DOLNEJ TRZECIEJ EKRANU: NAWIGUJ,
 * ZADZWOŃ, OTWÓRZ POST. To jest cała funkcja tej aplikacji — reszta ekranu
 * istnieje po to, żeby operator wiedział, który z tych trzech nacisnąć.
 * Przyklejone do dołu, bo tam sięga kciuk, i pełna szerokość, bo trafia się
 * w nie bez patrzenia.
 *
 * PEŁNA TREŚĆ POSTA, ORYGINAŁ, NIE STRESZCZENIE. To jedyne miejsce, w którym
 * da się sprawdzić, czy klasyfikator czegoś nie przekręcił — a przekręca:
 * wyciąga „Golfa" ze zdania o innym aucie, gubi „nie" przy „nie odpala".
 */
// Statusy, które operator ustawia RĘCZNIE, w kolejności, w jakiej realnie
// zapadają decyzje. 'nowe' jest poza tą siatką celowo — patrz przycisk
// „Wróć do nowych" niżej.
const ETYKIETY_STATUSU = {
  dzwonie: "📞 Dzwonię",
  wygrane: "✅ Wygrane",
  przegrane: "✖ Przegrane",
  smiec: "🗑 Śmieć",
} as const;

export default function Szczegol() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [z, setZ] = useState<Zlecenie | null>(null);
  const [blad, setBlad] = useState("");
  const [notatka, setNotatka] = useState("");
  const [cena, setCena] = useState("");
  const [zapisano, setZapisano] = useState(false);

  useEffect(() => {
    // `zywe` chroni przed ustawieniem stanu po opuszczeniu ekranu: operator
    // otwiera zlecenie i natychmiast wraca do listy, a odpowiedź przychodzi
    // sekundę później — bez tej flagi Next zgłasza aktualizację odmontowanego
    // komponentu, a przy szybkim przełączaniu zleceń wygrywa OSTATNIA
    // odpowiedź, nie ostatnie kliknięcie.
    let zywe = true;
    (async () => {
      try {
        const dane = await jednoZlecenie(id);
        if (!zywe) return;
        setZ(dane);
        setNotatka(dane.notatka ?? "");
        setCena(dane.cena_koncowa != null ? String(dane.cena_koncowa) : "");
      } catch (e) {
        if (zywe) setBlad(e instanceof Error ? e.message : "nie udało się wczytać");
      }
    })();
    return () => {
      zywe = false;
    };
  }, [id]);

  async function ustawStatus(status: Status) {
    if (!z) return;
    try {
      await zmienZlecenie(z.fb_id, { status });
      setZ({ ...z, status });
      // Po „śmieć" wracamy na listę: zlecenie odrzucone nie ma po co zostawać
      // na ekranie, a operator i tak już podjął decyzję.
      if (status === "smiec") router.push("/");
    } catch (e) {
      setBlad(e instanceof Error ? e.message : "nie udało się zmienić statusu");
    }
  }

  async function zapisz() {
    if (!z) return;
    try {
      await zmienZlecenie(z.fb_id, {
        notatka,
        ...(cena.trim() ? { cena_koncowa: Number(cena.replace(",", ".")) } : {}),
      });
      setZapisano(true);
      setTimeout(() => setZapisano(false), 2000);
    } catch (e) {
      setBlad(e instanceof Error ? e.message : "nie udało się zapisać");
    }
  }

  if (blad && !z) {
    return (
      <main className="p-5">
        <p className="text-opis text-smiec">{blad}</p>
        <Link href="/" className="dotyk mt-4 inline-flex text-opis underline">
          ← Wróć do listy
        </Link>
      </main>
    );
  }
  if (!z) return <main className="p-5 text-opis text-tekst-cichy">Wczytuję…</main>;

  const telefon = telefonCzytelnie(z.telefon);

  return (
    <main className="mx-auto max-w-2xl p-4">
      <Link href="/" className="dotyk -ml-2 inline-flex text-opis text-tekst-cichy">
        ← Lista
      </Link>

      {/* Pasek ostrzegawczy NAD kilometrami — patrz komentarz w komponencie:
          ostrzeżenie po liczbie przychodzi już po tym, jak liczba została
          zaakceptowana. */}
      <div className="mt-3">
        <PasekOstrzegawczy
          zrodlo={z.lokalizacja_zrodlo}
          surowa={z.lokalizacja_surowa}
        />
      </div>

      <section className="mt-3 flex items-center gap-3">
        <div className={`h-14 w-2 rounded-full ${kolorPilnosci(z.pilnosc)}`} />
        <div>
          <p className="text-xs font-bold uppercase tracking-wide text-tekst-cichy">
            {etykietaPilnosci(z.pilnosc)}
            {z.jezyk && z.jezyk !== "pl" ? ` · ${z.jezyk.toUpperCase()}` : ""}
          </p>
          <p className="flex items-baseline gap-3">
            <span className="text-liczba">{km(z.km_od_bazy)}</span>
            <span className="text-liczba text-tekst-cichy">{zl(z.szacunek_pln)}</span>
          </p>
        </div>
      </section>

      <h1 className="mt-3 text-xl font-bold">{trasa(z)}</h1>
      <dl className="mt-3 grid grid-cols-2 gap-3 text-opis">
        <Pole etykieta="Pojazd" wartosc={z.pojazd} />
        <Pole etykieta="Stan" wartosc={z.stan} />
        <Pole etykieta="Toczenie" wartosc={z.toczenie} />
        <Pole etykieta="Termin" wartosc={z.termin} />
        <Pole etykieta="Trasa (odbiór → dostawa)" wartosc={km(z.km_trasy)} />
        <Pole etykieta="Kod pocztowy" wartosc={z.kod_pocztowy} />
        <Pole etykieta="Pewność modelu" wartosc={z.pewnosc != null ? `${z.pewnosc}/100` : null} />
        <Pole etykieta="Wiek posta" wartosc={wiek(z.opublikowany_at)} />
        <Pole etykieta="Grupa" wartosc={z.grupa_nazwa} />
        <Pole etykieta="Autor" wartosc={z.autor} />
      </dl>

      <section className="mt-5">
        <h2 className="text-opis font-bold text-tekst-cichy">
          Oryginalna treść posta
        </h2>
        {/* `whitespace-pre-wrap` — łamania linii autora zostają. Post z grupy bywa
            listą punktów i sklejenie go w akapit gubi strukturę, którą ktoś
            świadomie nadał. */}
        <p className="mt-2 whitespace-pre-wrap rounded-xl border border-obrys bg-karta p-3 text-opis">
          {z.tresc || "(brak treści)"}
        </p>
      </section>

      <section className="mt-5 flex flex-col gap-3">
        <label className="text-opis font-bold text-tekst-cichy" htmlFor="notatka">
          Notatka
        </label>
        <textarea
          id="notatka"
          value={notatka}
          onChange={(e) => setNotatka(e.target.value)}
          rows={3}
          placeholder="np. dzwoniłem 14:20, oddzwoni po 16"
          className="rounded-xl border border-obrys bg-karta p-3 text-opis"
        />
        <label className="text-opis font-bold text-tekst-cichy" htmlFor="cena">
          Cena końcowa (zł)
        </label>
        <input
          id="cena"
          value={cena}
          onChange={(e) => setCena(e.target.value)}
          inputMode="decimal"
          placeholder="np. 350"
          className="min-h-[56px] rounded-xl border border-obrys bg-karta px-4 text-opis"
        />
        <button
          type="button"
          onClick={zapisz}
          className="dotyk rounded-xl border border-obrys bg-karta-akcent font-bold"
        >
          {zapisano ? "✓ Zapisano" : "Zapisz notatkę i cenę"}
        </button>
      </section>

      <section className="mt-5">
        <h2 className="text-opis font-bold text-tekst-cichy">Status</h2>
        <div className="mt-2 grid grid-cols-2 gap-2">
          {(Object.keys(ETYKIETY_STATUSU) as (keyof typeof ETYKIETY_STATUSU)[]).map((s) => (
            <button
              key={s}
              type="button"
              aria-pressed={z.status === s}
              onClick={() => ustawStatus(s)}
              className={`dotyk rounded-xl font-semibold ${
                z.status === s
                  ? "bg-tekst text-tlo"
                  : "border border-obrys bg-karta text-tekst"
              }`}
            >
              {ETYKIETY_STATUSU[s]}
            </button>
          ))}
        </div>
        {/* „Wróć do nowych" jest tu z tego samego powodu, dla którego API
            dopuszcza status 'nowe': nieodwracalne kliknięcie w aplikacji
            obsługiwanej jednym kciukiem na postoju to kwestia czasu, a nie
            ryzyko. Osobno pod siatką, bo to nie jest decyzja o zleceniu —
            to jest cofnięcie pomyłki. */}
        {z.status !== "nowe" && (
          <button
            type="button"
            onClick={() => ustawStatus("nowe")}
            className="dotyk mt-2 w-full rounded-xl border border-obrys bg-karta text-opis text-tekst-cichy"
          >
            ↩ Wróć do nowych
          </button>
        )}
      </section>

      {blad ? <p className="mt-4 text-opis text-smiec">{blad}</p> : null}

      {/* TRZY PRZYCISKI, PEŁNA SZEROKOŚĆ, PRZYKLEJONE DO DOŁU.
          `sticky` zamiast `fixed`: przy otwartej klawiaturze (notatka) `fixed`
          na Androidzie ląduje pod klawiaturą i przestaje istnieć. */}
      <div
        className="sticky bottom-0 -mx-4 mt-6 flex gap-2 border-t border-obrys bg-tlo/95 p-3 backdrop-blur"
        style={{ marginBottom: "calc(-1rem - env(safe-area-inset-bottom))" }}
      >
        <a
          href={z.link_nawigacji}
          target="_blank"
          rel="noopener noreferrer"
          className="dotyk flex-1 rounded-xl bg-tekst text-sm font-bold text-tlo"
        >
          NAWIGUJ
        </a>
        {/* ZADZWOŃ to `tel:` — otwiera dialer z wpisanym numerem. Gdy numeru
            nie ma, przycisk jest WYŁĄCZONY i mówi czemu; ukrycie go zmieniałoby
            układ trzech przycisków, który operator ma w pamięci mięśniowej. */}
        <a
          href={telefon ? linkTel(z.telefon) : undefined}
          aria-disabled={!telefon}
          className={`dotyk flex-1 rounded-xl text-sm font-bold ${
            telefon
              ? "bg-biore text-tlo"
              : "pointer-events-none border border-obrys bg-karta text-tekst-cichy"
          }`}
        >
          {telefon ? `ZADZWOŃ ${telefon}` : "BRAK NUMERU"}
        </a>
        <a
          href={z.post_url ?? undefined}
          target="_blank"
          rel="noopener noreferrer"
          aria-disabled={!z.post_url}
          className={`dotyk flex-1 rounded-xl text-sm font-bold ${
            z.post_url
              ? "border border-obrys bg-karta-akcent text-tekst"
              : "pointer-events-none border border-obrys bg-karta text-tekst-cichy"
          }`}
        >
          {z.post_url ? "OTWÓRZ POST" : "BRAK LINKU"}
        </a>
      </div>
    </main>
  );
}

function Pole({ etykieta, wartosc }: { etykieta: string; wartosc?: string | null }) {
  if (!wartosc) return null;
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-tekst-cichy">{etykieta}</dt>
      <dd className="font-semibold">{wartosc}</dd>
    </div>
  );
}
