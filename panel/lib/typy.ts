/**
 * Kształt danych z API (laweta_radar/api/routers/zlecenia.py).
 *
 * WSZYSTKIE LICZBY PRZYCHODZĄ POLICZONE. Panel nie liczy kilometrów ani wyceny
 * i nie ma prawa zacząć: te same liczby pokazuje powiadomienie na Telegramie,
 * a oba źródła liczą je jednym kodem (`laweta_radar/services/geo.py`). Gdyby
 * frontend liczył po swojemu, operator zobaczyłby w alercie 42 km, a w aplikacji
 * 48 km i przestałby ufać obu.
 *
 * Prawie wszystko jest opcjonalne, bo prawie wszystko pochodzi od modelu
 * czytającego cudzy post pisany z telefonu. Pole, którego nie ma, jest stanem
 * normalnym — komponenty mają je pomijać, nie pokazywać „undefined".
 */

export type Status = "nowe" | "dzwonie" | "wygrane" | "przegrane" | "smiec";

/** Skąd wiemy, gdzie to jest. Steruje paskiem ostrzegawczym na ekranie szczegółu. */
export type ZrodloLokalizacji =
  | "miasto"          // dokładne trafienie w słowniku — kilometry są dobre
  | "kod_pocztowy"    // środek obszaru kodu, ±20-30 km
  | "miasto_niepewne" // dopasowanie rozmyte albo nazwa pasująca do kilku miejsc
  | "brak";           // nie wiemy nic

export type Pilnosc = "pilne" | "dzis" | "planowane";

export interface Zlecenie {
  fb_id: string;
  status: Status;

  // --- policzone po stronie API ------------------------------------------
  km_od_bazy: number | null;
  km_trasy: number | null;
  szacunek_pln: number | null;
  link_mapy: string;
  link_nawigacji: string;
  lokalizacja_zrodlo: ZrodloLokalizacji;
  /** To, co REALNIE stało w poście. Idzie do paska ostrzegawczego — bez tego
   *  ostrzeżenie mówi „nie ufaj", nie mówiąc czemu. */
  lokalizacja_surowa: string;
  miejsce_od: string;
  miejsce_do: string;
  /** Pinezka na mapie. `null` = miejsca nie rozpoznano — takie zlecenie idzie
   *  listą pod mapą, bo pinezka „gdzieś" wygląda tak samo jak pinezka pewna. */
  lat: number | null;
  lon: number | null;

  // --- z klasyfikatora (ai_json rozpakowany na płasko) --------------------
  pilnosc?: Pilnosc | null;
  pojazd?: string | null;
  stan?: string | null;
  toczenie?: string | null;
  miasto_od?: string | null;
  miasto_do?: string | null;
  kod_pocztowy?: string | null;
  telefon?: string | null;
  pewnosc?: number | null;
  termin?: string | null;
  /** Dwuliterowy znacznik z bramki. Decyduje, w jakim języku operator oddzwoni. */
  jezyk?: string | null;

  // --- z posta ------------------------------------------------------------
  /** Pełna, oryginalna treść. Jest TYLKO w odpowiedzi szczegółu — lista
   *  pobierana co 30 s przez telefon w LTE nie ma powodu wozić stu postów. */
  tresc?: string | null;
  autor?: string | null;
  grupa_nazwa?: string | null;
  grupa_url?: string | null;
  post_url?: string | null;
  opublikowany_at?: string | null;
  pobrany_at?: string | null;
  stale?: boolean;

  // --- od operatora -------------------------------------------------------
  notatka?: string | null;
  cena_koncowa?: number | null;
  status_at?: string | null;
}

export interface OdpowiedzListy {
  zlecen: number;
  zlecenia: Zlecenie[];
}

export interface Lejek {
  pobrane: number;
  odsiane_bramka: number;
  do_ai: number;
  zlecen: number;
  oznaczone_smiec: number;
  dzwonie: number;
  wygrane: number;
  przegrane: number;
  za_stare: number;
  przychod_pln: number;
  powiadomienia: Record<string, number | string>;
}

export interface Grupa {
  grupa: string;
  grupa_url: string;
  pobrane: number;
  zlecen: number;
  wygrane: number;
  skutecznosc: number;
  /** false = za mała próbka, żeby ze skuteczności cokolwiek wnioskować. */
  wiarygodne: boolean;
  koszt_usd: number;
  ostatni_post: string | null;
}

export interface Statystyki {
  okna: Record<string, { lejek: Lejek; grupy: Grupa[] }>;
  min_probka_grupy: number;
  cena_usd_za_post: number;
}
