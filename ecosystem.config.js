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
    {
      // Bot Telegrama: przyciski pod powiadomieniami i komendy (/dzis, /ostatnie,
      // /stop, /start). Osobny proces od API, bo wisi na long pollingu i przez
      // 30 sekund nic nie robi — wpięcie tego w pętlę uvicorna znaczyłoby, że
      // panel czeka na Telegrama.
      //
      // max_memory_restart niżej niż w API: bot trzyma w pamięci jeden update
      // naraz, więc wzrost do 256 MB znaczy wyciek, a nie obciążenie.
      name: 'laweta-bot',
      script: '/home/ubuntu/laweta-radar/laweta_radar/scripts/start_bot.sh',
      cwd: '/home/ubuntu/laweta-radar',
      autorestart: true,
      max_memory_restart: '256M',
      // Bot bez TELEGRAM_BOT_TOKEN kończy CZYSTO (kod 0) — zasada z całego repo.
      // Bez tej pauzy PM2 restartowałby go w pętli kilkanaście razy na sekundę,
      // zapychając logi na maszynie, na której po prostu nie dokończono .env.
      restart_delay: 10000,
    },
    // Fetcher NIE jest procesem PM2 — chodzi z crona co kilka minut i kończy się
    // po jednym przebiegu. Proces w pętli musiałby sam pilnować odstępów, a przy
    // awarii restartowałby się w kółko, paląc kredyt Apify. Wpis crona jest
    // w README (sekcja Deploy).
  ],
};
