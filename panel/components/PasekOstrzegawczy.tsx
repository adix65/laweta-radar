"use client";

import { ostrzezeniaLokalizacji } from "@/lib/format";
import type { Zlecenie } from "@/lib/typy";

/**
 * Pasek nad kilometrami, gdy któryś koniec trasy jest zgadywany albo
 * nierozpoznany.
 *
 * OBA KOŃCE, NIE TYLKO ODBIÓR. Pasek patrzył wcześniej wyłącznie na punkt
 * odbioru — więc post „z Dębicy do Turku, trasa ma około 490 km" z rozpoznaną
 * Dębicą i nierozpoznanym Turkiem szedł na ekran bez jednego ostrzeżenia,
 * mimo że to właśnie ten drugi koniec sprawia, że kilometrów nie ma.
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
export default function PasekOstrzegawczy({ zlecenie }: { zlecenie: Zlecenie }) {
  const ostrzezenia = ostrzezeniaLokalizacji(zlecenie);
  if (ostrzezenia.length === 0) return null;

  return (
    <div className="flex flex-col gap-2">
      {ostrzezenia.map((o) => (
        <div
          key={o.koniec}
          role="status"
          className="rounded-xl border-2 border-ostrzezenie bg-ostrzezenie/15 p-3"
        >
          <p className="text-opis font-bold text-ostrzezenie">
            ⚠️ {o.koniec}: {o.tytul}
          </p>
          <p className="mt-1 text-opis">{o.opis}</p>
          {o.surowa ? (
            <p className="mt-2 text-opis">
              <span className="text-tekst-cichy">W poście stało: </span>
              <span className="font-semibold">„{o.surowa}”</span>
            </p>
          ) : null}
        </div>
      ))}
    </div>
  );
}
