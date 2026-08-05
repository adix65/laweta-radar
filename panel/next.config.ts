import type { NextConfig } from "next";

// Panel gada z FastAPI (laweta_radar/api) przez rewrite, a nie bezpośrednio.
// DWA POWODY, oba praktyczne:
//   1. Przeglądarka widzi jedno źródło, więc CORS w ogóle nie wchodzi w grę —
//      a CORS na telefonie objawia się pustym ekranem bez komunikatu.
//   2. Service worker cache'uje `/api/...` z tej samej domeny; ścieżki
//      cross-origin wymagają CORS także w cache'u i pierwsza offline'owa
//      próba kończy się błędem, którego nie widać w devtools telefonu.
const API = process.env.LAWETA_API_URL ?? "http://127.0.0.1:8002";

const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:sciezka*", destination: `${API}/:sciezka*` }];
  },
  // Nagłówki dla service workera: bez `no-cache` przeglądarka potrafi trzymać
  // starą wersję sw.js tygodniami, czyli aktualizacja panelu nigdy nie dociera
  // do telefonu, na którym raz go zainstalowano.
  async headers() {
    return [
      {
        source: "/sw.js",
        headers: [
          { key: "Cache-Control", value: "no-cache, no-store, must-revalidate" },
          { key: "Service-Worker-Allowed", value: "/" },
        ],
      },
    ];
  },
};

export default nextConfig;
