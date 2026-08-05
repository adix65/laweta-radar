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
  | "kod"             // dokładne trafienie kodu pocztowego — najpewniejsze
  | "miasto"          // nazwa jednoznaczna w bazie
  | "miasto_niepewne" // kilka miejscowości o tej nazwie, wzięliśmy największą
  | "brak";           // nie rozpoznaliśmy miejsca

/** Wartości z `workers/classifier.py` — nie wymyślamy własnych. */
export type Pilnosc = "teraz" | "dzis" | "jutro" | "elastycznie";

/** Co miałoby jechać — z bramki (`workers/gate.py`, kolumna `kategoria_ladunku`).
 *
 *  „zwierze" NIE ukrywa zlecenia i nie ma prawa zacząć: te giełdy mieszają
 *  transport aut z transportem koni, operator zwierząt nie wozi — ale „nie wożę"
 *  i „nie pokazuj mi tego" to dwie różne rzeczy. Taki rekord dostaje widoczny
 *  znacznik i ląduje NIŻEJ na liście (sortuje API), a decyzję podejmuje kierowca.
 *  `null` = bramka nie orzekała (rekordy sprzed migracji 0010) i zachowuje się
 *  dokładnie jak „pojazd". */
export type KategoriaLadunku = "pojazd" | "zwierze" | "inne";

export interface Zlecenie {
  fb_id: string;
  status: Status;

  // --- policzone po stronie API ------------------------------------------
  /** DŁUGOŚĆ KURSU odbiór→dostawa. To ona jest pierwszą liczbą na ekranie:
   *  przy transporcie międzynarodowym „ile km od bazy" nie znaczy nic.
   *
   *  `null` znaczy „trasa nieustalona" — któryś koniec jest nierozpoznany.
   *  W to miejsce NIE WOLNO podstawić `km_od_bazy`: kurs Dębica→Turek
   *  (490 km) pokazywał się wtedy jako „60 km, ~250 zł", czyli jako wycena
   *  lokalnego skoku, i kierowca odrzucał go bez czytania. */
  km_trasy: number | null;
  /** Dojazd z bazy do odbioru — liczba pomocnicza, nigdy filtr i nigdy
   *  zamiennik `km_trasy`. Wolno ją pokazać WYŁĄCZNIE pod własną etykietą. */
  km_od_bazy: number | null;
  /** `null`, gdy `km_trasy` jest `null` — nieznany dystans nie ma ceny. */
  szacunek_pln: number | null;
  /** Odległość, którą autor podał w treści posta („trasa ma około 490 km").
   *  CUDZA liczba: na ekranie zawsze z podpisem „wg autora", nigdy zlana
   *  z naszym wyliczeniem. Autor zna trasę lepiej niż nasz geokoder. */
  km_wg_autora: number | null;
  /** Pusty string, gdy nie znamy żadnego punktu — wtedy przycisku nie ma. */
  link_mapy: string;
  link_nawigacji: string;
  /** Skąd znamy punkt ODBIORU. */
  lokalizacja_zrodlo: ZrodloLokalizacji;
  /** To samo dla DOSTAWY — bez tego zlecenie z rozpoznanym odbiorem
   *  i nierozpoznanym celem nie dostawało w panelu żadnego ostrzeżenia. */
  dostawa_zrodlo: ZrodloLokalizacji;
  /** Nazwy punktów, przy których geokoder zgadywał. Niepusta lista = pasek
   *  ostrzegawczy nad kilometrami. */
  lokalizacja_niepewne: string[];
  /** Pinezka na mapie. `null` = miejsca nie rozpoznano — takie zlecenie idzie
   *  listą pod mapą, bo pinezka „gdzieś" wygląda tak samo jak pinezka pewna. */
  lat: number | null;
  lng: number | null;

  // --- z klasyfikatora (0004_klasyfikacja.sql, nazwy 1:1 z kolumnami) -----
  typ?: string | null;
  pilnosc?: Pilnosc | null;
  /** Surowy fragment posta z miejscem — idzie do paska ostrzegawczego, bo bez
   *  niego ostrzeżenie mówi „nie ufaj", nie mówiąc czemu. */
  odbior_raw?: string | null;
  odbior_kod?: string | null;
  odbior_miasto?: string | null;
  dostawa_raw?: string | null;
  dostawa_kod?: string | null;
  dostawa_miasto?: string | null;
  pojazd_opis?: string | null;
  pojazd_kategoria?: string | null;
  /** TRÓJSTANOWE: true = wjedzie sam, false = potrzebna wyciągarka,
   *  null = model nie wie i trzeba spytać przez telefon. */
  stan_toczy_sie?: boolean | null;
  stan_ma_kola?: boolean | null;
  stan_po_wypadku?: boolean | null;
  stan_uwagi?: string | null;
  kontakt_typ?: string | null;
  kontakt_wartosc?: string | null;
  cena_sugerowana?: number | null;
  pewnosc?: number | null;
  powod?: string | null;
  /** Dwuliterowy znacznik z bramki. Decyduje, w jakim języku operator oddzwoni. */
  jezyk?: string | null;
  /** Z bramki. „zwierze" = znacznik na karcie i miejsce niżej na liście. */
  kategoria_ladunku?: KategoriaLadunku | null;

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
