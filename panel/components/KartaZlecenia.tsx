"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import {
  etykietaPilnosci,
  etykietaTrasy,
  kolorPilnosci,
  lokalizacjaNiepewna,
  pojazdJednymWierszem,
  trasa,
  trasaUstalona,
  wgAutora,
  wiek,
  zl,
} from "@/lib/format";
import type { Status, Zlecenie } from "@/lib/typy";

/**
 * Karta zlecenia na liście.
 *
 * HIERARCHIA: NAJPIERW KM I ZŁ. To są dwie liczby, po których operator decyduje,
 * czy w ogóle czytać dalej — więc mają rozmiar `--text-liczba`, a wszystko inne
 * `--text-opis`. Opis pojazdu jest ważny, ale dopiero wtedy, gdy odległość
 * i stawka się zgadzają.
 *
 * GEST JEST SKRÓTEM, NIE JEDYNĄ DROGĄ. Swipe w prawo = biorę, w lewo = śmieć —
 * i OBOK tego zawsze widoczne przyciski. Gest jest niewidoczny z definicji:
 * nie da się go odkryć, patrząc na ekran, nie działa przy nawigacji klawiaturą
 * i nie istnieje dla czytnika ekranu. Aplikacja, w której jedyną drogą do akcji
 * jest przesunięcie palcem, jest aplikacją, której część funkcji po prostu nie ma.
 */

// Ile pikseli trzeba przesunąć, żeby gest się liczył. 96 px to dużo — i o to
// chodzi: karta jest na przewijanej liście, a przypadkowe „śmieć" przy
// przewijaniu kciukiem to zlecenie wyrzucone bez patrzenia.
const PROG_GESTU = 96;

interface Props {
  zlecenie: Zlecenie;
  onZmiana: (fbId: string, status: Status) => void;
}

export default function KartaZlecenia({ zlecenie: z, onZmiana }: Props) {
  const [przesuniecie, setPrzesuniecie] = useState(0);
  const startX = useRef<number | null>(null);
  const startY = useRef<number | null>(null);
  const poziomy = useRef(false);

  function dotykStart(e: React.TouchEvent) {
    startX.current = e.touches[0].clientX;
    startY.current = e.touches[0].clientY;
    poziomy.current = false;
  }

  function dotykRuch(e: React.TouchEvent) {
    if (startX.current == null || startY.current == null) return;
    const dx = e.touches[0].clientX - startX.current;
    const dy = e.touches[0].clientY - startY.current;

    // Kierunek rozstrzygamy RAZ, na starcie ruchu, i trzymamy się go do końca.
    // Bez tego przewijanie listy pod kątem zaczyna po drodze przesuwać kartę,
    // a operator widzi migającą czerwień pod palcem przy zwykłym scrollu.
    if (!poziomy.current) {
      if (Math.abs(dx) < 12 && Math.abs(dy) < 12) return;
      poziomy.current = Math.abs(dx) > Math.abs(dy);
      if (!poziomy.current) {
        startX.current = null;
        return;
      }
    }
    setPrzesuniecie(dx);
  }

  function dotykKoniec() {
    if (przesuniecie > PROG_GESTU) onZmiana(z.fb_id, "dzwonie");
    else if (przesuniecie < -PROG_GESTU) onZmiana(z.fb_id, "smiec");
    setPrzesuniecie(0);
    startX.current = null;
    poziomy.current = false;
  }

  const kierunek = przesuniecie > 24 ? "biore" : przesuniecie < -24 ? "smiec" : null;

  return (
    <li className="relative overflow-hidden rounded-2xl">
      {/* Tło pod kartą pokazuje, CO SIĘ STANIE po puszczeniu palca — słowem,
          nie samym kolorem. Sam kolor pod przesuwaną kartą jest nieczytelny
          w słońcu i bezużyteczny przy daltonizmie. */}
      <div
        aria-hidden
        className={`absolute inset-0 flex items-center px-6 text-lg font-bold ${
          kierunek === "biore"
            ? "justify-start bg-biore text-tlo"
            : kierunek === "smiec"
              ? "justify-end bg-smiec text-tlo"
              : "bg-transparent"
        }`}
      >
        {kierunek === "biore" ? "✅ BIORĘ" : kierunek === "smiec" ? "ŚMIEĆ 🗑" : null}
      </div>

      <article
        onTouchStart={dotykStart}
        onTouchMove={dotykRuch}
        onTouchEnd={dotykKoniec}
        style={{
          transform: `translateX(${przesuniecie}px)`,
          transition: przesuniecie === 0 ? "transform 160ms ease-out" : "none",
        }}
        className="relative flex gap-3 rounded-2xl border border-obrys bg-karta"
      >
        {/* Pasek pilności: kolor + etykieta pionowo. Kolor sam w sobie nie niesie
            tu żadnej informacji, której nie ma w tekście. */}
        <div className={`w-2 shrink-0 rounded-l-2xl ${kolorPilnosci(z.pilnosc)}`} />

        <div className="min-w-0 flex-1 py-3 pr-3">
          <Link href={`/zlecenie/${encodeURIComponent(z.fb_id)}`} className="block">
            {/* PIERWSZA LINIA: DŁUGOŚĆ KURSU I CENA — albo słowna odmowa.
                Gdy któryś koniec trasy jest nierozpoznany, nie ma tu ŻADNEJ
                liczby: dojazd z bazy w tym miejscu czytało się jak kurs i tak
                właśnie kurs Dębica→Turek (490 km) wyglądał na „60 km, ~250 zł".
                „wg autora" wolno pokazać, bo jest podpisane cudzym nazwiskiem
                — to liczba z posta, nie nasze wyliczenie. */}
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              {trasaUstalona(z) ? (
                <>
                  <span className="text-liczba">{etykietaTrasy(z)}</span>
                  <span className="text-liczba text-tekst-cichy">{zl(z.szacunek_pln)}</span>
                </>
              ) : (
                <span className="text-opis font-bold text-ostrzezenie">
                  {etykietaTrasy(z)}
                </span>
              )}
              {wgAutora(z) && (
                <span className="text-opis text-tekst-cichy">{wgAutora(z)}</span>
              )}
              {lokalizacjaNiepewna(z.lokalizacja_zrodlo) && trasaUstalona(z) && (
                <span
                  className="text-opis font-bold text-ostrzezenie"
                  title="kilometry orientacyjne"
                >
                  ?
                </span>
              )}
            </div>

            <p className="mt-1 text-xs font-bold uppercase tracking-wide text-tekst-cichy">
              {etykietaPilnosci(z.pilnosc)}
              {z.jezyk && z.jezyk !== "pl" ? ` · ${z.jezyk.toUpperCase()}` : ""}
            </p>

            <p className="mt-2 truncate text-opis font-semibold">{trasa(z)}</p>
            {pojazdJednymWierszem(z) && (
              <p className="truncate text-opis text-tekst-cichy">
                {pojazdJednymWierszem(z)}
              </p>
            )}
            <p className="mt-1 truncate text-xs text-tekst-cichy">
              {wiek(z.opublikowany_at)} · {z.grupa_nazwa ?? "grupa nieznana"}
            </p>
          </Link>

          {/* Przyciski ZAWSZE widoczne, obok gestu. Zajmują pełną szerokość
              karty, bo dwa duże cele trafia się kciukiem bez patrzenia. */}
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={() => onZmiana(z.fb_id, "dzwonie")}
              className="dotyk flex-1 rounded-xl bg-biore font-bold text-tlo"
            >
              ✅ Biorę
            </button>
            <button
              type="button"
              onClick={() => onZmiana(z.fb_id, "smiec")}
              aria-label="Oznacz jako śmieć"
              className="dotyk rounded-xl border border-obrys bg-karta-akcent px-5 font-bold text-smiec"
            >
              🗑
            </button>
          </div>
        </div>
      </article>
    </li>
  );
}
