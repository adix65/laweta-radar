"use client";

import { useMemo, useState } from "react";
import BramaTokenu from "@/components/BramaTokenu";
import Filtry, { type Filtr } from "@/components/Filtry";
import KartaZlecenia from "@/components/KartaZlecenia";
import PowiadomieniaPush from "@/components/PowiadomieniaPush";
import { zmienZlecenie } from "@/lib/api";
import { useZlecenia } from "@/lib/useZlecenia";
import type { Status, Zlecenie } from "@/lib/typy";

/**
 * Ekran główny: karty zleceń, najnowsze u góry.
 *
 * Sortowanie robi API (`opublikowany_at DESC NULLS LAST`) i panel go NIE
 * zmienia. Post bez daty publikacji ląduje na końcu — góra listy należy do
 * najświeższych, a nie do tych o nieznanym wieku.
 */
export default function Lista() {
  const [filtr, setFiltr] = useState<Filtr>("wszystkie");
  const [odswiezacz, setOdswiezacz] = useState(0);

  const filtryApi = useMemo(() => {
    switch (filtr) {
      case "blisko":
        return { status: "nowe", max_km: 50 };
      case "dzis":
        return { status: "nowe", od: new Date().toISOString().slice(0, 10) };
      // Kierunek geograficzny filtrujemy W API (`kierunek_geo` jest kolumną,
      // nie zależy od konfiguracji operatora jak `max_km`) — w odróżnieniu od
      // „pilne" niżej, które zostaje po stronie panelu.
      case "wyjazdy":
        return { status: "nowe", kierunek_geo: "wyjazd" };
      case "przywozy":
        return { status: "nowe", kierunek_geo: "przywoz" };
      case "krajowe":
        return { status: "nowe", kierunek_geo: "krajowy" };
      case "pilne":
      case "wszystkie":
      default:
        return { status: "nowe" };
    }
  }, [filtr]);

  const stan = useZlecenia(filtryApi);

  // „Pilne" filtrujemy po stronie panelu, a nie w SQL-u. `pilnosc` JEST kolumną
  // (0004_klasyfikacja.sql), więc dałoby się to zrobić zapytaniem — ale przy
  // kilkudziesięciu rekordach, które i tak już przyszły, oznaczałoby to nowy
  // parametr API i drugi round-trip za odsiew, który tutaj kosztuje mikrosekundy.
  const widoczne =
    filtr === "pilne" ? stan.zlecenia.filter((z) => z.pilnosc === "teraz") : stan.zlecenia;

  async function zmien(fbId: string, status: Status) {
    // OPTYMISTYCZNIE: karta znika natychmiast, request leci w tle. Czekanie na
    // odpowiedź serwera przy każdym kliknięciu znaczy 200-400 ms zawieszenia na
    // LTE — a operator w tym czasie klika drugi raz, bo wygląda, że nie działa.
    const przed = stan.zlecenia;
    stan.podmien((lista: Zlecenie[]) => lista.filter((z) => z.fb_id !== fbId));
    try {
      await zmienZlecenie(fbId, { status });
    } catch {
      // Nie udało się — karta WRACA. Zniknięcie zlecenia, którego serwer nie
      // przyjął, to zlecenie stracone po cichu, czyli najgorszy możliwy skutek
      // nieudanego requestu w tej aplikacji.
      stan.podmien(() => przed);
    }
  }

  if (stan.brakDostepu) {
    return (
      <BramaTokenu
        powod={stan.brakDostepu}
        onZapisano={() => {
          setOdswiezacz((n) => n + 1);
          stan.odswiez();
        }}
      />
    );
  }

  return (
    <main key={odswiezacz} className="mx-auto max-w-2xl p-4">
      <header className="mb-3 flex items-baseline justify-between gap-3">
        <h1 className="text-liczba">
          {widoczne.length}
          <span className="ml-2 text-opis font-normal text-tekst-cichy">
            {widoczne.length === 1 ? "zlecenie" : "zleceń"}
          </span>
        </h1>
        <button
          type="button"
          onClick={stan.odswiez}
          aria-label="Odśwież listę"
          className="dotyk rounded-xl border border-obrys bg-karta px-4 text-opis"
        >
          ⟳
        </button>
      </header>

      <Filtry aktywny={filtr} onZmiana={setFiltr} />

      {stan.zNamiastki && (
        <p className="mt-3 rounded-xl border-2 border-ostrzezenie bg-ostrzezenie/15 p-3 text-opis">
          ⚠️ Brak połączenia — to jest ostatni zapamiętany stan. Statusów nie da
          się teraz zmienić.
        </p>
      )}

      <ul className="mt-4 flex flex-col gap-3">
        {widoczne.map((z) => (
          <KartaZlecenia key={z.fb_id} zlecenie={z} onZmiana={zmien} />
        ))}
      </ul>

      {!stan.ladowanie && widoczne.length === 0 && (
        <div className="mt-10 text-center">
          <p className="text-opis text-tekst-cichy">
            {filtr === "wszystkie"
              ? "Brak nowych zleceń."
              : "Nic pod tym filtrem — spróbuj „Wszystkie”."}
          </p>
          {filtr === "wszystkie" && (
            <p className="mt-2 text-opis text-tekst-cichy">
              Zero zleceń to normalny dzień. Jeśli cisza trwa drugą dobę,
              sprawdź <code>/zdrowie</code> — cichy fetcher wygląda tak samo.
            </p>
          )}
        </div>
      )}

      <PowiadomieniaPush />
    </main>
  );
}
