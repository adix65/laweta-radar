"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/**
 * Dolny pasek nawigacji.
 *
 * NA DOLE, NIE NA GÓRZE. Aplikacja jest obsługiwana jedną ręką, kciukiem —
 * górna krawędź telefonu o przekątnej 6,5" jest poza jego zasięgiem bez
 * przekładania urządzenia, a przekładanie na postoju to moment, w którym
 * telefon ląduje pod fotelem.
 *
 * Każda pozycja ma IKONĘ I PODPIS. Same ikony wymagają nauczenia się, co
 * znaczą, a tej aplikacji używa się kilka razy dziennie w pośpiechu — nie na
 * tyle często, żeby cokolwiek wejść w nawyk.
 */
const POZYCJE = [
  { href: "/", ikona: "📋", etykieta: "Lista" },
  { href: "/mapa", ikona: "🗺", etykieta: "Mapa" },
  { href: "/statystyki", ikona: "📊", etykieta: "Statystyki" },
];

export default function Nawigacja() {
  const sciezka = usePathname();

  return (
    <nav
      aria-label="Nawigacja główna"
      className="fixed bottom-0 left-0 right-0 z-40 flex border-t border-obrys bg-karta/95 backdrop-blur"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      {POZYCJE.map((p) => {
        const aktywna = p.href === "/" ? sciezka === "/" : sciezka.startsWith(p.href);
        return (
          <Link
            key={p.href}
            href={p.href}
            aria-current={aktywna ? "page" : undefined}
            className={`dotyk flex-1 flex-col gap-0.5 py-2 text-xs ${
              aktywna ? "text-tekst" : "text-tekst-cichy"
            }`}
          >
            <span aria-hidden className="text-xl leading-none">
              {p.ikona}
            </span>
            <span>{p.etykieta}</span>
            {/* Stan aktywny NIE jest oznaczony wyłącznie kolorem — kreska pod
                spodem działa też dla kogoś, kto nie rozróżnia odcieni szarości
                na przyćmionym ekranie. */}
            <span
              className={`h-0.5 w-6 rounded-full ${aktywna ? "bg-tekst" : "bg-transparent"}`}
            />
          </Link>
        );
      })}
    </nav>
  );
}
