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
    case "teraz":
      return "bg-pilne";
    case "dzis":
      return "bg-dzis";
    default:
      return "bg-planowane";
  }
}

export function etykietaPilnosci(pilnosc?: Pilnosc | null): string {
  switch (pilnosc) {
    case "teraz":
      return "TERAZ";
    case "dzis":
      return "DZIŚ";
    case "jutro":
      return "JUTRO";
    case "elastycznie":
      return "ELASTYCZNIE";
    default:
      return "ZLECENIE";
  }
}

/** Dystans na pierwszy plan: DŁUGOŚĆ KURSU odbiór→dostawa i nic innego.
 *
 *  `null` to „trasa nieustalona" — i tak ma zostać. Dojazd z bazy NIE JEST
 *  zamiennikiem: kurs Dębica→Turek (490 km wg autora, Turek nierozpoznany)
 *  pokazywał się przez taki fallback jako „60 km", czyli jako lokalny skok,
 *  i kierowca odrzucał go bez czytania. Funkcji „weź km_trasy, a jak nie ma,
 *  to cokolwiek innego" nie ma tu celowo — pierwszą linię wolno zbudować
 *  wyłącznie z `etykietaTrasy`. Ta sama reguła obowiązuje w powiadomieniu na
 *  Telegramie; rozjazd między alertem a panelem znaczy, że operator przestaje
 *  ufać obu. */
export function trasaUstalona(z: Zlecenie): boolean {
  return z.km_trasy != null;
}

/** Pierwsza linia karty: kilometry kursu albo słowna odmowa. Nigdy liczba,
 *  której nie wyliczyliśmy z dwóch znanych punktów. */
export function etykietaTrasy(z: Zlecenie): string {
  return z.km_trasy == null ? "trasa nieustalona" : `${z.km_trasy} km`;
}

/** Odległość podana przez autora posta — zawsze z podpisem, kto ją podał.
 *  `null`, gdy w treści jej nie było. */
export function wgAutora(z: Zlecenie): string | null {
  return z.km_wg_autora == null ? null : `wg autora: ${z.km_wg_autora} km`;
}

/** Kilometry na ekran, w polu z własną etykietą. `null` to „nie wiemy" i tak
 *  ma być napisane — zero zamiast pustki wygląda jak zlecenie tuż za rogiem. */
export function km(wartosc: number | null): string {
  return wartosc == null ? "nie wiadomo" : `${wartosc} km`;
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
  const skad = z.odbior_miasto || z.odbior_raw || z.odbior_kod || "?";
  const dokad = z.dostawa_miasto || z.dostawa_raw || z.dostawa_kod;
  return dokad ? `${skad} → ${dokad}` : skad;
}

export function pojazdJednymWierszem(z: Zlecenie): string {
  const czesci = [z.pojazd_opis, z.stan_uwagi];
  // Trójstanowe `stan_toczy_sie`: pokazujemy tylko dwa znane stany, bo każdy
  // zmienia sprzęt, który trzeba zabrać. „Nie wiadomo" operator wyczyta z braku.
  if (z.stan_toczy_sie === true) czesci.push("toczy się");
  else if (z.stan_toczy_sie === false) czesci.push("NIE toczy się");
  return czesci.filter(Boolean).join(" · ");
}

/** Czy nad kilometrami ma iść pasek ostrzegawczy. `kod` i `miasto` są pewne. */
export function lokalizacjaNiepewna(zrodlo: ZrodloLokalizacji): boolean {
  return zrodlo === "miasto_niepewne" || zrodlo === "brak";
}

export function opisZrodla(zrodlo: ZrodloLokalizacji): string {
  switch (zrodlo) {
    case "miasto_niepewne":
      return "W bazie jest kilka miejscowości o tej nazwie — wzięliśmy największą. Kilometry są orientacyjne; sprawdź treść posta, zanim podasz cenę.";
    case "brak":
      return "Nie rozpoznaliśmy miejsca. Kilometrów nie ma — przeczytaj oryginał posta.";
    default:
      return "";
  }
}

/** Jedno ostrzeżenie o jednym końcu trasy — tyle, ile trzeba, żeby je narysować. */
export interface OstrzezenieLokalizacji {
  /** Klucz Reacta i zarazem nazwa końca trasy: „Odbiór" / „Dostawa". */
  koniec: string;
  tytul: string;
  opis: string;
  /** To, co REALNIE stało w poście. Bez tego ostrzeżenie mówi „nie ufaj",
   *  nie dając czym to sprawdzić. */
  surowa: string | null;
}

/** Ostrzeżenia o OBU końcach trasy.
 *
 *  Wcześniej pasek patrzył wyłącznie na punkt odbioru — więc zlecenie
 *  z rozpoznaną Dębicą i nierozpoznanym Turkiem nie dostawało w panelu
 *  ŻADNEGO ostrzeżenia, mimo że to właśnie ten drugi koniec kasował kilometry.
 *  Brak celu, którego autor w ogóle nie podał, jest osobnym przypadkiem: nie
 *  ma czego nie rozpoznać, a operator ma zadzwonić i spytać. */
export function ostrzezeniaLokalizacji(z: Zlecenie): OstrzezenieLokalizacji[] {
  const wynik: OstrzezenieLokalizacji[] = [];
  const surowa = (...pola: (string | null | undefined)[]) =>
    pola.find((p) => p && p.trim()) ?? null;

  const odbiorSurowa = surowa(z.odbior_raw, z.odbior_miasto, z.odbior_kod);
  if (lokalizacjaNiepewna(z.lokalizacja_zrodlo)) {
    wynik.push({
      koniec: "Odbiór",
      tytul:
        z.lokalizacja_zrodlo === "brak"
          ? "Nie rozpoznaliśmy miejsca odbioru"
          : "Miejsce odbioru niepewne",
      opis: opisZrodla(z.lokalizacja_zrodlo),
      surowa: odbiorSurowa,
    });
  }

  const dostawaSurowa = surowa(z.dostawa_raw, z.dostawa_miasto, z.dostawa_kod);
  if (lokalizacjaNiepewna(z.dostawa_zrodlo)) {
    const bezCelu = z.dostawa_zrodlo === "brak" && !dostawaSurowa;
    wynik.push({
      koniec: "Dostawa",
      tytul: bezCelu
        ? "Autor nie podał celu"
        : z.dostawa_zrodlo === "brak"
          ? "Nie rozpoznaliśmy celu"
          : "Cel niepewny",
      opis: bezCelu
        ? "W poście nie ma dokąd. Bez celu nie ma długości kursu — spytaj przez telefon."
        : opisZrodla(z.dostawa_zrodlo),
      surowa: dostawaSurowa,
    });
  }
  return wynik;
}
