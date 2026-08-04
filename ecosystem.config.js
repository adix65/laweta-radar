// Konfiguracja PM2. Ścieżki zakładają rozpakowanie w /home/ubuntu/laweta-radar —
// podmień, jeśli deployujesz gdzie indziej (PM2 nie rozwija tu ~ ani zmiennych).
//
// Skrypty startowe wczytują .env JAWNIE, bo PM2 startuje procesy ze swojego
// środowiska, a nie z powłoki logowania — bez tego API wstałoby bez DATABASE_URL
// na maszynie, na której wszystko jest ustawione.
module.exports = {
  apps: [
    {
      name: 'laweta-api',
      script: '/home/ubuntu/laweta-radar/laweta_radar/scripts/start_api.sh',
      cwd: '/home/ubuntu/laweta-radar',
      autorestart: true,
      max_memory_restart: '512M',
    },
    // Fetcher NIE jest procesem PM2 — chodzi z crona co kilka minut i kończy się
    // po jednym przebiegu. Proces w pętli musiałby sam pilnować odstępów, a przy
    // awarii restartowałby się w kółko, paląc kredyt Apify. Wpis crona jest
    // w README (sekcja Deploy).
  ],
};
