"use client";

/**
 * Pigułki filtrów u góry listy.
 *
 * TO SĄ PYTANIA OPERATORA, NIE PROGI SYSTEMU. „do 50 km" zawęża listę, bo ktoś
 * o to poprosił — i przestaje działać, gdy tę pigułkę odklika. Domyślny widok
 * pokazuje WSZYSTKO, co system złapał, niezależnie od dystansu i kierunku:
 * trasa Kolonia-Kraków to 1100 km i normalny dzień pracy tego operatora.
 * Różnica między filtrem a progiem jest sednem zasady naczelnej repo i najłatwiej
 * ją zgubić, ustawiając „na wszelki wypadek" domyślne `max_km`.
 *
 * Pigułki, nie rozwijana lista: rozwijana lista to dwa dotknięcia i menu, które
 * na małym ekranie zasłania to, co się filtruje. Cztery pigułki mieszczą się
 * w jednym rzędzie i widać po nich stan bez otwierania czegokolwiek.
 */

export type Filtr = "pilne" | "blisko" | "dzis" | "wszystkie";

export const FILTRY: { id: Filtr; etykieta: string }[] = [
  { id: "pilne", etykieta: "Pilne" },
  { id: "blisko", etykieta: "do 50 km" },
  { id: "dzis", etykieta: "Dziś" },
  { id: "wszystkie", etykieta: "Wszystkie" },
];

export default function Filtry({
  aktywny,
  onZmiana,
}: {
  aktywny: Filtr;
  onZmiana: (f: Filtr) => void;
}) {
  return (
    <div
      role="group"
      aria-label="Filtry listy"
      className="flex gap-2 overflow-x-auto pb-1"
    >
      {FILTRY.map((f) => {
        const wybrany = f.id === aktywny;
        return (
          <button
            key={f.id}
            type="button"
            aria-pressed={wybrany}
            onClick={() => onZmiana(f.id)}
            className={`dotyk shrink-0 rounded-full px-5 text-opis font-semibold ${
              wybrany
                ? "bg-tekst text-tlo"
                : "border border-obrys bg-karta text-tekst-cichy"
            }`}
          >
            {f.etykieta}
          </button>
        );
      })}
    </div>
  );
}
