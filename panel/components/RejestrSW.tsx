"use client";

import { useEffect } from "react";

/**
 * Rejestracja service workera. Osobny komponent, bo `layout.tsx` jest serwerowy,
 * a `navigator.serviceWorker` istnieje wyłącznie w przeglądarce.
 *
 * Nie ma tu żadnego UI i to jest celowe: rejestracja SW nie może niczego
 * pokazać ani niczego zablokować. Gdy się nie uda (stary Android, tryb
 * prywatny, wyłączone SW), aplikacja działa normalnie — traci tylko offline.
 */
export default function RejestrSW() {
  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;
    // `load` zamiast natychmiast: rejestracja konkuruje o pasmo z pierwszym
    // pobraniem listy zleceń, a lista jest pilniejsza — offline przyda się
    // dopiero przy następnym uruchomieniu.
    const rejestruj = () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        // Cisza. Brak offline'u nie jest awarią, o której trzeba komukolwiek
        // mówić w momencie, gdy patrzy na listę świeżych zleceń.
      });
    };
    if (document.readyState === "complete") rejestruj();
    else window.addEventListener("load", rejestruj);
    return () => window.removeEventListener("load", rejestruj);
  }, []);

  return null;
}
