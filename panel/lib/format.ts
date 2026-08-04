/**
 * Formatowanie liczb i dat na ekran. Reguły są te same co w powiadomieniu
 * (laweta_radar/services/powiadomienia.py) i to nie jest przypadek: operator
 * widzi to samo zlecenie raz w Telegramie i raz w panelu, więc „4 min temu"
 * musi znaczyć to samo w obu miejscach.
 */
import type { Pilnosc, Zlecenie, ZrodloLokalizacji } from "./typy";

/** „4 min temu" / „2 h temu" / „wczoraj". ZAWSZE coś zwraca.
 *
 *  Brak daty publikacji jest stanem normalnym (Apify nie zawsze ją oddaje),
 *  ale pominięcie tej informacji byłoby błędem: operator patrzący na kartę bez
 *  wieku zakłada, że jest świeża. „wiek nieznany" jest gorszą informacją, ale
 *  prawdziwą. */
export function wiek(iso?: string | null): string {
  if (!iso) return "wiek nieznany";
  const minuty = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (Number.isNaN(minuty)) return "wiek nieznany";
  if (minuty < 0) return "przed chwilą";
  if (minuty < 60) return `${minuty} min temu`;
  const godziny = Math.floor(minuty / 60);
  if (godziny < 24) return `${godziny} h temu`;
  const dni = Math.floor(godziny / 24);
  return dni === 1 ? "wczoraj" : `${dni} dni temu`;
}

/** Wiek w minutach — do decyzji „czy to jeszcze świeże" w kolorze paska. */
export function wiekMinut(iso?: string | null): number | null {
  if (!iso) return null;
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  return Number.isNaN(m) ? null : m;
}

/** Kolor paska pilności na karcie.
 *
 *  Kolor jest DODATKIEM do tekstu, nigdy jedyną informacją — pasek zawsze idzie
 *  razem z etykietą. Około 8% mężczyzn ma zaburzenia rozróżniania czerwieni
 *  i zieleni, a to jest aplikacja dla kierowcy lawety, nie dla projektanta. */
export function kolorPilnosci(pilnosc?: Pilnosc | null): string {
  switch (pilnosc) {
    case "pilne":
      return "bg-pilne";
    case "dzis":
      return "bg-dzis";
    default:
      return "bg-planowane";
  }
}

export function etykietaPilnosci(pilnosc?: Pilnosc | null): string {
  switch (pilnosc) {
    case "pilne":
      return "PILNE";
    case "dzis":
      return "DZIŚ";
    case "planowane":
      return "PLANOWANE";
    default:
      return "ZLECENIE";
  }
}

/** Kilometry na ekran. `null` to „nie wiemy" i tak ma być napisane —
 *  zero zamiast pustki wygląda jak zlecenie tuż za rogiem. */
export function km(wartosc: number | null): string {
  return wartosc == null ? "? km" : `${wartosc} km`;
}

export function zl(wartosc: number | null): string {
  return wartosc == null ? "—" : `~${wartosc} zł`;
}

/** Numer w formacie z odstępami — po to, żeby dało się go przeczytać na głos. */
export function telefonCzytelnie(surowy?: string | null): string {
  const cyfry = (surowy ?? "").replace(/\D/g, "");
  if (!cyfry) return "";
  if (cyfry.length === 9) {
    return `${cyfry.slice(0, 3)} ${cyfry.slice(3, 6)} ${cyfry.slice(6)}`;
  }
  return cyfry.replace(/(\d{3})(?=\d)/g, "$1 ");
}

/** `tel:` z surowymi cyframi — spacje w href psują wybieranie na części Androidów. */
export function linkTel(surowy?: string | null): string {
  return `tel:${(surowy ?? "").replace(/[^\d+]/g, "")}`;
}

export function trasa(z: Zlecenie): string {
  const skad = z.miejsce_od || z.miasto_od || "?";
  const dokad = z.miejsce_do || z.miasto_do;
  return dokad ? `${skad} → ${dokad}` : skad;
}

export function pojazdJednymWierszem(z: Zlecenie): string {
  return [z.pojazd, z.stan, z.toczenie].filter(Boolean).join(" · ");
}

/** Czy nad kilometrami ma iść pasek ostrzegawczy. */
export function lokalizacjaNiepewna(zrodlo: ZrodloLokalizacji): boolean {
  return zrodlo !== "miasto";
}

export function opisZrodla(zrodlo: ZrodloLokalizacji): string {
  switch (zrodlo) {
    case "kod_pocztowy":
      return "Lokalizacja z kodu pocztowego — środek obszaru, kilometry ±20-30 km.";
    case "miasto_niepewne":
      return "Nazwa miejsca dopasowana NIEPEWNIE — kilometry są orientacyjne. Sprawdź w treści posta, zanim podasz cenę.";
    case "brak":
      return "Nie rozpoznaliśmy miejsca. Kilometrów nie ma — przeczytaj oryginał posta.";
    default:
      return "";
  }
}
