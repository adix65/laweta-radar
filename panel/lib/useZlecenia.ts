"use client";

import { useCallback, useEffect, useState } from "react";
import {
  BrakDostepu,
  listaZlecen,
  odczytajCache,
  zapiszCache,
  type FiltryListy,
} from "./api";
import type { Zlecenie } from "./typy";

/**
 * Lista zleceń z odświeżaniem co 30 s — TYLKO gdy karta jest widoczna.
 *
 * BEZ WEBSOCKETÓW. Przy jednym użytkowniku i zleceniach przychodzących kilka
 * razy dziennie WebSocket nie daje nic poza opóźnieniem liczonym w sekundach,
 * a dokłada połączenie, które potrafi się CICHO rozłączyć: karta wygląda na
 * żywą, dane stoją, i nikt się nie dowie do momentu, w którym zlecenie
 * przepadło. Polling nie ma tego stanu — albo request przechodzi, albo widać
 * błąd.
 *
 * `document.visibilityState` zatrzymuje pętlę, gdy telefon leży w kieszeni.
 * Bez tego panel odpytuje API całą noc i zjada baterię, żeby nikt tego nie
 * zobaczył. Po powrocie na kartę odświeżamy NATYCHMIAST, zanim minie
 * kolejne 30 s — operator wracający do aplikacji chce zobaczyć stan sprzed
 * sekundy, a nie sprzed pół minuty.
 */
const OKRES_MS = 30_000;

export interface StanListy {
  zlecenia: Zlecenie[];
  ladowanie: boolean;
  blad: string;
  /** Ustawione, gdy API odpowiedziało 401/503 — wtedy pokazujemy bramę tokenu. */
  brakDostepu: string;
  zNamiastki: boolean;
  odswiez: () => void;
  /** Zmiana stanu lokalnie, bez czekania na serwer — patrz `page.tsx`. */
  podmien: (zmiana: (z: Zlecenie[]) => Zlecenie[]) => void;
}

export function useZlecenia(filtry: FiltryListy): StanListy {
  const [zlecenia, setZlecenia] = useState<Zlecenie[]>([]);
  const [ladowanie, setLadowanie] = useState(true);
  const [blad, setBlad] = useState("");
  const [brakDostepu, setBrakDostepu] = useState("");
  const [zNamiastki, setZNamiastki] = useState(false);

  // `filtry` przychodzi z `useMemo` w komponencie, więc jest stabilne dopóki
  // operator nie dotknie pigułki. Dzięki temu `pobierz` też jest stabilne
  // i interwał NIE jest przestawiany przy każdym renderze — a gdy filtr się
  // zmieni, restart 30-sekundowego zegara jest zachowaniem właściwym:
  // nowy widok ma się odświeżyć od nowa, nie w połowie starego cyklu.
  const pobierz = useCallback(async () => {
    try {
      const odp = await listaZlecen(filtry);
      setZlecenia(odp.zlecenia);
      setBlad("");
      setBrakDostepu("");
      setZNamiastki(false);
      zapiszCache(odp.zlecenia);
    } catch (e) {
      if (e instanceof BrakDostepu) {
        setBrakDostepu(e.message);
      } else {
        // BRAK ZASIĘGU NIE MOŻE OZNACZAĆ PUSTEGO EKRANU. Operator w dziurze
        // między Krosnem a Sanokiem ma zobaczyć ostatni znany stan — numer
        // telefonu i miasto wystarczą, żeby oddzwonić, gdy zasięg wróci.
        const cache = odczytajCache();
        if (cache?.zlecenia?.length) {
          setZlecenia(cache.zlecenia);
          setZNamiastki(true);
        }
        setBlad(e instanceof Error ? e.message : "brak połączenia");
      }
    } finally {
      setLadowanie(false);
    }
  }, [filtry]);

  // JEDEN efekt, nie dwa. Osobne „pobierz na starcie" byłoby duplikatem:
  // `naZmianeWidocznosci()` niżej wywołuje się od razu przy montowaniu i samo
  // robi pierwsze pobranie. Dwa efekty znaczyły dwa zapytania przy każdym
  // wejściu na listę i przy każdej zmianie filtra — na LTE to widać.
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;

    const start = () => {
      if (timer) return;
      timer = setInterval(() => void pobierz(), OKRES_MS);
    };
    const stop = () => {
      if (!timer) return;
      clearInterval(timer);
      timer = null;
    };
    const naZmianeWidocznosci = () => {
      if (document.visibilityState === "visible") {
        void pobierz();
        start();
      } else {
        stop();
      }
    };

    naZmianeWidocznosci();
    document.addEventListener("visibilitychange", naZmianeWidocznosci);
    return () => {
      document.removeEventListener("visibilitychange", naZmianeWidocznosci);
      stop();
    };
  }, [pobierz]);

  return {
    zlecenia,
    ladowanie,
    blad,
    brakDostepu,
    zNamiastki,
    odswiez: () => void pobierz(),
    podmien: setZlecenia,
  };
}
