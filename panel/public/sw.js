/*
 * Service worker: powłoka aplikacji offline + odbiór web push.
 *
 * CO CACHE'UJEMY I DLACZEGO AKURAT TO:
 *   • POWŁOKA (`/`, `/mapa`, `/statystyki`, manifest, ikony) — żeby aplikacja
 *     w ogóle WSTAŁA bez zasięgu. PWA, która w dziurze pokazuje dinozaura
 *     przeglądarki, jest w tym momencie gorsza od zakładki.
 *   • OSTATNIA LISTA ZLECEŃ (`/api/zlecenia`) — żeby po wstaniu było widać
 *     CO NAJMNIEJ ostatni znany stan. Operator w dziurze między Krosnem
 *     a Sanokiem potrzebuje numeru telefonu i miasta; oddzwoni, gdy zasięg wróci.
 *
 * DWIE RÓŻNE STRATEGIE, BO TO DWA RÓŻNE RODZAJE DANYCH:
 *   • powłoka  -> cache first  (kod się nie zmienia między wersjami)
 *   • /api/... -> network first (dane starsze niż sekundy są mylące)
 * Odwrotnie byłoby katastrofą: cache-first na `/api/zlecenia` znaczy panel
 * pokazujący wczorajszą listę przy pełnym zasięgu, bez żadnego objawu.
 *
 * PISANE RĘCZNIE, BEZ WORKBOXA. Cały plik ma sto linii i dwie strategie —
 * generator dołożyłby zależność build-time i warstwę abstrakcji nad czymś,
 * co i tak trzeba przeczytać w całości, zanim się uwierzy, że nie serwuje
 * starych danych.
 */

// Zmiana wersji unieważnia stary cache. MUSI się zmieniać przy każdym deployu
// zmieniającym powłokę — inaczej telefon, na którym raz zainstalowano panel,
// zostaje ze starą wersją na zawsze.
const WERSJA = "laweta-v1";
const CACHE_POWLOKI = `${WERSJA}-powloka`;
const CACHE_DANYCH = `${WERSJA}-dane`;

const POWLOKA = ["/", "/mapa", "/statystyki", "/manifest.json", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches
      .open(CACHE_POWLOKI)
      // `addAll` jest atomowe: jeden brakujący plik unieważnia cały cache
      // i instalacja się nie udaje. Dlatego każdy element listy dokładamy
      // osobno — brak jednej ikony nie może zostawić aplikacji bez offline'u.
      .then((c) => Promise.allSettled(POWLOKA.map((u) => c.add(u))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((klucze) =>
        Promise.all(
          klucze.filter((k) => !k.startsWith(WERSJA)).map((k) => caches.delete(k)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return; // PATCH-e statusów nigdy nie idą z cache'u

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/api/")) {
    e.respondWith(najpierwSiec(req));
    return;
  }
  e.respondWith(najpierwCache(req));
});

/** Dane: świeże albo ostatnie znane. Cache'ujemy WYŁĄCZNIE listę zleceń —
 *  statystyki i /zdrowie bez zasięgu są bezużyteczne, a zajmowałyby miejsce. */
async function najpierwSiec(req) {
  const url = new URL(req.url);
  const wartoCache = url.pathname.startsWith("/api/zlecenia");
  try {
    const odp = await fetch(req);
    if (wartoCache && odp.ok) {
      const cache = await caches.open(CACHE_DANYCH);
      cache.put(req, odp.clone());
    }
    return odp;
  } catch (blad) {
    const zapas = wartoCache ? await caches.match(req) : null;
    if (zapas) return zapas;
    // 503 z czytelnym ciałem, nie rzucony wyjątek: panel rozpoznaje po nim
    // „brak połączenia" i pokazuje ostatni stan z localStorage zamiast
    // pustego ekranu z błędem sieci.
    return new Response(
      JSON.stringify({ detail: "brak połączenia i brak danych w pamięci podręcznej" }),
      { status: 503, headers: { "Content-Type": "application/json" } },
    );
  }
}

/** Powłoka: z cache'u natychmiast, odświeżana w tle. */
async function najpierwCache(req) {
  const zapas = await caches.match(req);
  const zsieci = fetch(req)
    .then((odp) => {
      if (odp.ok) {
        caches.open(CACHE_POWLOKI).then((c) => c.put(req, odp.clone()));
      }
      return odp;
    })
    .catch(() => zapas);
  return zapas || zsieci;
}

/*
 * WEB PUSH — DRUGI KANAŁ, NIGDY ZAMIENNIK TELEGRAMA.
 * Telegram działa na każdym telefonie, przechodzi przez tryb cichy przy
 * ustawionym priorytecie i nie wymaga żadnej zgody przeglądarki. Push jest
 * dodatkiem dla kogoś, kto woli natywne powiadomienie systemowe — i który
 * na iOS MUSI najpierw dodać aplikację do ekranu głównego (patrz komponent
 * PowiadomieniaPush).
 */
self.addEventListener("push", (e) => {
  let dane = {};
  try {
    dane = e.data ? e.data.json() : {};
  } catch {
    dane = { tytul: "Nowe zlecenie", tresc: e.data ? e.data.text() : "" };
  }
  e.waitUntil(
    self.registration.showNotification(dane.tytul || "Nowe zlecenie", {
      body: dane.tresc || "",
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      // Wibracja jest tu funkcją, nie ozdobą: telefon leży na fotelu pasażera
      // ekranem w dół i dźwięk ginie w hałasie silnika.
      vibrate: [120, 60, 120],
      // `tag` po fb_id: to samo zlecenie z dwóch kanałów daje JEDNO
      // powiadomienie na ekranie, a nie dwa pod sobą.
      tag: dane.fb_id || "laweta",
      data: { url: dane.url || "/" },
      requireInteraction: dane.pilne === true,
    }),
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const cel = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((okna) => {
      // Jeśli panel jest już otwarty, PRZENOSIMY GO na właściwy ekran zamiast
      // otwierać drugą kartę — dwie kopie aplikacji na telefonie to dwa
      // niezależne pollingi i pewność, że operator patrzy na tę nieaktualną.
      for (const okno of okna) {
        if ("focus" in okno) {
          okno.navigate(cel);
          return okno.focus();
        }
      }
      return self.clients.openWindow(cel);
    }),
  );
});
