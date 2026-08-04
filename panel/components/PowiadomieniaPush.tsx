"use client";

import { useEffect, useState } from "react";
import { token } from "@/lib/api";

/**
 * Zapis na web push — DRUGI KANAŁ OBOK TELEGRAMA, nigdy zamiennik.
 *
 * Telegram zostaje kanałem podstawowym: działa na każdym telefonie, dźwięk
 * przechodzi przez tryb cichy przy ustawionym priorytecie, nie wymaga zgód
 * przeglądarki i nie znika przy przeinstalowaniu aplikacji. Push jest dodatkiem
 * dla kogoś, kto woli natywne powiadomienie systemowe.
 *
 * iOS: PUSH DZIAŁA WYŁĄCZNIE PO DODANIU DO EKRANU GŁÓWNEGO. To jest
 * ograniczenie Apple (od iOS 16.4 i tylko dla PWA w trybie standalone), a nie
 * błąd tej aplikacji — i dlatego jest NAPISANE W INTERFEJSIE, a nie ukryte
 * w dokumentacji. Bez tego zdania pierwszym efektem będzie zgłoszenie „push nie
 * działa", na które nie ma odpowiedzi poza „tak, i nie da się tego naprawić".
 */

type Stan = "sprawdzam" | "niedostepne" | "ios-wymaga-instalacji" | "wylaczone" | "wlaczone" | "odmowa";

function czyIOS(): boolean {
  return /iPad|iPhone|iPod/.test(navigator.userAgent);
}

function czyStandalone(): boolean {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    // iOS ma własną, niestandardową flagę i nie wspiera `display-mode`
    // w każdej wersji — bez tego sprawdzenia PWA dodana do ekranu głównego
    // wyglądałaby dla nas jak zwykła karta Safari.
    (window.navigator as unknown as { standalone?: boolean }).standalone === true
  );
}

export default function PowiadomieniaPush() {
  const [stan, setStan] = useState<Stan>("sprawdzam");
  const [blad, setBlad] = useState("");

  useEffect(() => {
    (async () => {
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        setStan(czyIOS() && !czyStandalone() ? "ios-wymaga-instalacji" : "niedostepne");
        return;
      }
      if (czyIOS() && !czyStandalone()) {
        setStan("ios-wymaga-instalacji");
        return;
      }
      if (Notification.permission === "denied") {
        setStan("odmowa");
        return;
      }
      const rej = await navigator.serviceWorker.ready;
      const sub = await rej.pushManager.getSubscription();
      setStan(sub ? "wlaczone" : "wylaczone");
    })().catch(() => setStan("niedostepne"));
  }, []);

  async function wlacz() {
    try {
      const zgoda = await Notification.requestPermission();
      if (zgoda !== "granted") {
        setStan("odmowa");
        return;
      }
      const odp = await fetch("/api/push/klucz", { headers: { "X-Token": token() } });
      const { klucz } = await odp.json();
      if (!klucz) {
        setBlad("Serwer nie ma kluczy VAPID — patrz README panelu.");
        return;
      }
      const rej = await navigator.serviceWorker.ready;
      const sub = await rej.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: base64ToBufor(klucz),
      });
      await fetch("/api/push/subskrypcja", {
        method: "POST",
        headers: { "X-Token": token(), "Content-Type": "application/json" },
        body: JSON.stringify(sub),
      });
      setStan("wlaczone");
      setBlad("");
    } catch (e) {
      setBlad(e instanceof Error ? e.message : "nie udało się włączyć");
    }
  }

  if (stan === "sprawdzam" || stan === "wlaczone") return null;

  return (
    <section className="mt-8 rounded-2xl border border-obrys bg-karta p-4">
      <h2 className="text-opis font-bold">Powiadomienia w telefonie</h2>

      {stan === "ios-wymaga-instalacji" && (
        <p className="mt-2 text-opis text-tekst-cichy">
          Na iPhonie powiadomienia push działają <strong>tylko po dodaniu tej
          strony do ekranu głównego</strong> (Udostępnij → Dodaj do ekranu
          początkowego). To ograniczenie systemu Apple, nie błąd aplikacji.
          <br />
          <br />
          Zlecenia i tak przychodzą na <strong>Telegrama</strong> — push jest
          tylko dodatkiem.
        </p>
      )}

      {stan === "wylaczone" && (
        <>
          <p className="mt-2 text-opis text-tekst-cichy">
            Dodatek do Telegrama: natywne powiadomienie systemowe. Telegram
            zostaje kanałem podstawowym i działa niezależnie od tego.
          </p>
          <button
            type="button"
            onClick={wlacz}
            className="dotyk mt-3 w-full rounded-xl bg-tekst font-bold text-tlo"
          >
            Włącz powiadomienia push
          </button>
        </>
      )}

      {stan === "odmowa" && (
        <p className="mt-2 text-opis text-tekst-cichy">
          Powiadomienia zostały zablokowane w ustawieniach przeglądarki dla tej
          strony. Odblokuj je tam, jeśli chcesz je włączyć — zlecenia i tak
          przychodzą na Telegrama.
        </p>
      )}

      {stan === "niedostepne" && (
        <p className="mt-2 text-opis text-tekst-cichy">
          Ta przeglądarka nie obsługuje web push. Zlecenia przychodzą na
          Telegrama — nic nie tracisz.
        </p>
      )}

      {blad ? <p className="mt-2 text-opis text-smiec">{blad}</p> : null}
    </section>
  );
}

/** VAPID public key (base64url) -> ArrayBuffer, którego wymaga `pushManager`.
 *
 *  Konwersja jest ręczna, bo `applicationServerKey` nie przyjmuje stringa
 *  base64url — a przekazany string kończy się `InvalidCharacterError` bez
 *  jakiejkolwiek wskazówki, że chodzi o format klucza.
 *
 *  Zwracamy ArrayBuffer, nie Uint8Array: `BufferSource` w typach DOM wymaga
 *  widoku nad zwykłym `ArrayBuffer`, a `Uint8Array` jest typowany szerzej
 *  (`ArrayBufferLike`, więc też `SharedArrayBuffer`) i nie przechodzi. */
function base64ToBufor(base64url: string): ArrayBuffer {
  const dopelnienie = "=".repeat((4 - (base64url.length % 4)) % 4);
  const base64 = (base64url + dopelnienie).replace(/-/g, "+").replace(/_/g, "/");
  const surowe = window.atob(base64);
  const bufor = new ArrayBuffer(surowe.length);
  const widok = new Uint8Array(bufor);
  for (let i = 0; i < surowe.length; i += 1) widok[i] = surowe.charCodeAt(i);
  return bufor;
}
