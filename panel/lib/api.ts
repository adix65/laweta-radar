/**
 * Klient API. Jedno miejsce, w którym panel dotyka sieci.
 *
 * TOKEN W localStorage, WPISYWANY RAZ. Użytkownik jest jeden, a każdy ekran
 * logowania między nim a listą zleceń kosztuje sekundy dokładnie wtedy, gdy
 * ich nie ma — na postoju, jedną ręką. Sesje, odświeżanie tokenu i cookies
 * to warstwy, które potrafią wygasnąć w najgorszym momencie i wymagać
 * ponownego logowania na telefonie w słońcu.
 *
 * Co to realnie chroni: adres panelu jest publiczny (PWA musi być dostępna
 * z telefonu), a lista zleceń zawiera numery telefonów obcych ludzi z grup FB.
 * To są cudze dane i nie mogą wisieć pod gołym URL-em.
 */
import type { OdpowiedzListy, Statystyki, Status, Zlecenie } from "./typy";

const KLUCZ_TOKENU = "laweta_token";

export function token(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(KLUCZ_TOKENU) ?? "";
}

export function zapiszToken(wartosc: string): void {
  window.localStorage.setItem(KLUCZ_TOKENU, wartosc.trim());
}

export function wyczyscToken(): void {
  window.localStorage.removeItem(KLUCZ_TOKENU);
}

/** 401 od API. Wyróżniony typ, bo wymaga INNEJ reakcji niż awaria sieci:
 *  przy 401 pokazujemy ekran wpisania tokenu, przy błędzie sieci — ostatni
 *  znany stan z cache'u service workera. Pomylenie tych dwóch znaczy, że
 *  operator w miejscu bez zasięgu dostaje prośbę o token, którego nie ma
 *  przy sobie. */
export class BrakDostepu extends Error {}

async function pobierz<T>(sciezka: string, opcje: RequestInit = {}): Promise<T> {
  const odp = await fetch(`/api${sciezka}`, {
    ...opcje,
    headers: {
      "X-Token": token(),
      ...(opcje.body ? { "Content-Type": "application/json" } : {}),
      ...opcje.headers,
    },
  });

  if (odp.status === 401 || odp.status === 503) {
    const tresc = await odp.json().catch(() => ({ detail: "" }));
    throw new BrakDostepu(tresc.detail || "brak dostępu");
  }
  if (!odp.ok) {
    const tresc = await odp.json().catch(() => ({ detail: odp.statusText }));
    throw new Error(tresc.detail || `HTTP ${odp.status}`);
  }
  return odp.json() as Promise<T>;
}

export interface FiltryListy {
  status?: string;
  max_km?: number;
  od?: string;
  /** przywoz|wyjazd|krajowy|tranzyt|nieznany — patrz `typy.KierunekGeo`.
   *  Bez tego pola API zwraca zlecenia WSZYSTKICH kierunków, tak jak dziś. */
  kierunek_geo?: string;
  limit?: number;
}

export function listaZlecen(filtry: FiltryListy = {}): Promise<OdpowiedzListy> {
  const p = new URLSearchParams();
  if (filtry.status) p.set("status", filtry.status);
  if (filtry.max_km != null) p.set("max_km", String(filtry.max_km));
  if (filtry.od) p.set("od", filtry.od);
  if (filtry.kierunek_geo) p.set("kierunek_geo", filtry.kierunek_geo);
  p.set("limit", String(filtry.limit ?? 50));
  return pobierz<OdpowiedzListy>(`/zlecenia?${p}`);
}

export function jednoZlecenie(fbId: string): Promise<Zlecenie> {
  return pobierz<Zlecenie>(`/zlecenia/${encodeURIComponent(fbId)}`);
}

export function zmienZlecenie(
  fbId: string,
  zmiana: { status?: Status; notatka?: string; cena_koncowa?: number },
): Promise<Partial<Zlecenie>> {
  return pobierz(`/zlecenia/${encodeURIComponent(fbId)}`, {
    method: "PATCH",
    body: JSON.stringify(zmiana),
  });
}

export function statystyki(): Promise<Statystyki> {
  return pobierz<Statystyki>("/statystyki");
}

// --- pamięć podręczna listy -------------------------------------------------
// Ostatnia lista ląduje w localStorage, nie tylko w cache'u service workera.
// Powód jest konkretny: operator wjeżdża w dziurę bez zasięgu w połowie trasy
// i musi zobaczyć CO NAJMNIEJ ostatni stan — numer telefonu i miasto wystarczą,
// żeby oddzwonić, gdy zasięg wróci. Pusty ekran z komunikatem „brak połączenia"
// jest w tym momencie bezużyteczny.
const KLUCZ_CACHE = "laweta_ostatnia_lista";

export function zapiszCache(zlecenia: Zlecenie[]): void {
  try {
    window.localStorage.setItem(
      KLUCZ_CACHE,
      JSON.stringify({ czas: Date.now(), zlecenia }),
    );
  } catch {
    // Przepełniony localStorage nie może wywalić listy. Cache jest wygodą,
    // a nie warunkiem działania aplikacji.
  }
}

export function odczytajCache(): { czas: number; zlecenia: Zlecenie[] } | null {
  try {
    const surowe = window.localStorage.getItem(KLUCZ_CACHE);
    return surowe ? JSON.parse(surowe) : null;
  } catch {
    return null;
  }
}
