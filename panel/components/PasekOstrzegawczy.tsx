"use client";

import { lokalizacjaNiepewna, opisZrodla } from "@/lib/format";
import type { ZrodloLokalizacji } from "@/lib/typy";

/**
 * Pasek nad kilometrami, gdy lokalizacja pochodzi z niepewnego dopasowania.
 *
 * POKAZUJEMY SUROWĄ TREŚĆ MIEJSCA Z POSTA. Ostrzeżenie „kilometry orientacyjne"
 * bez tego jest bezużyteczne — mówi operatorowi, żeby nie ufał liczbie, nie
 * dając mu niczego, czym mógłby ją sprawdzić. Z surowym tekstem („okolice
 * Krosna", „38-400", „za Sanokiem") operator rozstrzyga sam w dwie sekundy,
 * bo zna teren lepiej niż jakikolwiek słownik nazw.
 *
 * Pasek jest NAD kilometrami, nie pod: przy czytaniu z góry na dół ostrzeżenie
 * po liczbie przychodzi już po tym, jak liczba została zaakceptowana.
 */
export default function PasekOstrzegawczy({
  zrodlo,
  surowa,
}: {
  zrodlo: ZrodloLokalizacji;
  surowa?: string | null;
}) {
  if (!lokalizacjaNiepewna(zrodlo)) return null;

  return (
    <div
      role="status"
      className="rounded-xl border-2 border-ostrzezenie bg-ostrzezenie/15 p-3"
    >
      <p className="text-opis font-bold text-ostrzezenie">
        ⚠️ Lokalizacja niepewna
      </p>
      <p className="mt-1 text-opis">{opisZrodla(zrodlo)}</p>
      {surowa ? (
        <p className="mt-2 text-opis">
          <span className="text-tekst-cichy">W poście stało: </span>
          <span className="font-semibold">„{surowa}”</span>
        </p>
      ) : null}
    </div>
  );
}
