"use client";

import { useState } from "react";
import { zapiszToken } from "@/lib/api";

/**
 * Ekran wpisania tokenu. Pokazuje się TYLKO przy 401/503 z API.
 *
 * JEDEN UŻYTKOWNIK, JEDEN TOKEN, WPISYWANY RAZ. Bez loginu, bez hasła, bez
 * sesji — każdy z tych elementów to coś, co potrafi wygasnąć w najgorszym
 * momencie i zażądać ponownego logowania na telefonie w słońcu, gdy zlecenie
 * jest sprzed czterech minut.
 *
 * `inputMode="text"` i `autoCapitalize="off"` nie są kosmetyką: token wkleja się
 * z menedżera haseł, a Android z domyślnymi ustawieniami potrafi zamienić
 * pierwszą literę na wielką i wtedy 401 wygląda jak zły token.
 */
export default function BramaTokenu({
  powod,
  onZapisano,
}: {
  powod: string;
  onZapisano: () => void;
}) {
  const [wartosc, setWartosc] = useState("");

  // 503 znaczy „API_TOKEN nie jest ustawiony na serwerze" — wpisanie czegokolwiek
  // tutaj tego nie naprawi, więc zamiast pola pokazujemy, co zrobić na VPS-ie.
  const problemSerwera = powod.includes("API_TOKEN");

  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center gap-5 p-5">
      <h1 className="text-liczba">Laweta Radar</h1>

      {problemSerwera ? (
        <div className="rounded-xl border-2 border-ostrzezenie bg-ostrzezenie/15 p-4">
          <p className="font-bold text-ostrzezenie">Serwer nie ma ustawionego tokenu</p>
          <p className="mt-2 text-opis">{powod}</p>
          <p className="mt-2 text-opis text-tekst-cichy">
            Ustaw <code>API_TOKEN</code> w <code>laweta_radar/.env</code> i zrestartuj
            API (<code>pm2 restart laweta-api</code>).
          </p>
        </div>
      ) : (
        <>
          <p className="text-opis text-tekst-cichy">
            Wklej token z <code>API_TOKEN</code> w <code>laweta_radar/.env</code>.
            Zapisuje się na tym telefonie — wpisujesz raz.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (!wartosc.trim()) return;
              zapiszToken(wartosc);
              onZapisano();
            }}
            className="flex flex-col gap-3"
          >
            <input
              type="password"
              value={wartosc}
              onChange={(e) => setWartosc(e.target.value)}
              placeholder="token"
              autoComplete="current-password"
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              aria-label="Token dostępu"
              className="min-h-[56px] rounded-xl border border-obrys bg-karta px-4 text-opis"
            />
            <button
              type="submit"
              className="dotyk rounded-xl bg-tekst font-bold text-tlo"
            >
              Zapisz i wejdź
            </button>
          </form>
          {powod ? <p className="text-opis text-smiec">{powod}</p> : null}
        </>
      )}
    </main>
  );
}
