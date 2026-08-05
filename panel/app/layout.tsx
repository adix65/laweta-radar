import type { Metadata, Viewport } from "next";
import Nawigacja from "@/components/Nawigacja";
import RejestrSW from "@/components/RejestrSW";
import "./globals.css";

export const metadata: Metadata = {
  title: "Laweta Radar",
  description: "Zlecenia z grup FB — km, zł, telefon.",
  manifest: "/manifest.json",
  appleWebApp: {
    // Bez tego iOS otwiera PWA w Safari z paskiem adresu, czyli zjada 15%
    // ekranu i traci `standalone`, od którego zależy web push.
    capable: true,
    statusBarStyle: "black-translucent",
    title: "Laweta",
  },
  icons: {
    icon: [{ url: "/icon-192.png", sizes: "192x192", type: "image/png" }],
    apple: [{ url: "/icon-192.png", sizes: "192x192" }],
  },
};

export const viewport: Viewport = {
  themeColor: "#0a0a0b",
  width: "device-width",
  initialScale: 1,
  // `viewportFit: cover` + safe-area w CSS — inaczej dolne przyciski lądują
  // pod paskiem gestów iPhone'a, czyli w miejscu, w którym dotyk zamyka aplikację.
  viewportFit: "cover",
  // Skalowanie ZOSTAJE dostępne. Zablokowanie zoomu jest wygodne dla układu
  // i szkodliwe dla człowieka, który akurat nie ma przy sobie okularów —
  // a numer telefonu trzeba przepisać dokładnie.
  maximumScale: 5,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="pl">
      <body>
        {children}
        <Nawigacja />
        <RejestrSW />
      </body>
    </html>
  );
}
