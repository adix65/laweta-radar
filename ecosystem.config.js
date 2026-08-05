// Konfiguracja PM2. Ścieżki liczą się z położenia TEGO pliku (__dirname), więc
// repo działa tak samo w /home/ubuntu/laweta-radar, w /home/ubuntu/laweta-test
// i w katalogu domowym innego użytkownika — bez podmieniania czegokolwiek.
// PM2 nie rozwija tu ~ ani zmiennych powłoki, ale ten plik jest zwykłym JS-em
// wykonywanym przez Node, więc __dirname jest dokładnie tym, czego trzeba.
//
// Skrypty startowe wczytują .env JAWNIE, bo PM2 startuje procesy ze swojego
// środowiska, a nie z powłoki logowania — bez tego API wstałoby bez DATABASE_URL
// na maszynie, na której wszystko jest ustawione.
const fs = require('fs');
const path = require('path');

const KATALOG = __dirname;
const PLIK_ENV = path.join(KATALOG, 'laweta_radar', '.env');

// Czytamy .env SAMI, zamiast liczyć na środowisko powłoki: `pm2 resurrect` po
// restarcie maszyny odpala się bez żadnej powłoki logowania, a nazwy procesów
// muszą wtedy wyjść te same, co przy pierwszym `pm2 start`. Inaczej po reboocie
// instancja testowa wstałaby pod nazwami produkcyjnej.
function zEnv(klucz, domyslna) {
  try {
    const linie = fs.readFileSync(PLIK_ENV, 'utf8').split('\n');
    for (let i = linie.length - 1; i >= 0; i--) {
      const m = linie[i].match(new RegExp(`^\\s*${klucz}\\s*=\\s*(.*)$`));
      if (m) {
        const v = m[1].trim().replace(/\s+#.*$/, '').replace(/^["']|["']$/g, '');
        if (v) return v;
      }
    }
  } catch { /* brak .env przy pierwszym starcie to normalny stan */ }
  return domyslna;
}

// Pusta INSTANCJA = nazwy produkcyjne (laweta-api, laweta-bot, laweta-panel) —
// czyli dokładnie to, co było wcześniej. INSTANCJA=test daje laweta-test-api
// i resztę z przedrostkiem, więc testowa kopia stoi obok produkcyjnej na tym
// samym VPS-ie i `pm2 restart laweta-api` nie ubija nie tej, co trzeba.
const INSTANCJA = process.env.INSTANCJA || zEnv('INSTANCJA', '');
const NAZWA = INSTANCJA ? `laweta-${INSTANCJA}` : 'laweta';

module.exports = {
  apps: [
    {
      name: `${NAZWA}-api`,
      script: path.join(KATALOG, 'laweta_radar', 'scripts', 'start_api.sh'),
      cwd: KATALOG,
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
      name: `${NAZWA}-bot`,
      script: path.join(KATALOG, 'laweta_radar', 'scripts', 'start_bot.sh'),
      cwd: KATALOG,
      autorestart: true,
      max_memory_restart: '256M',
      // Bot bez TELEGRAM_BOT_TOKEN kończy CZYSTO (kod 0) — zasada z całego repo.
      // Bez tej pauzy PM2 restartowałby go w pętli kilkanaście razy na sekundę,
      // zapychając logi na maszynie, na której po prostu nie dokończono .env.
      restart_delay: 10000,
    },
    {
      // Panel jest tutaj, a nie w ręcznym `pm2 start "npm run start"`, bo to
      // JEDNO miejsce musi znać komplet procesów: `update.sh` przeładowuje po
      // nazwach, a `pm2 save` zapisuje to, co faktycznie chodzi. Panel dopisany
      // z palca po deployu wypada z obu tych mechanizmów przy pierwszej pomyłce.
      //
      // Port bierze się z PANEL_PORT (patrz panel/package.json), a ten z .env —
      // ta sama ścieżka, co reszta konfiguracji.
      name: `${NAZWA}-panel`,
      cwd: path.join(KATALOG, 'panel'),
      script: 'npm',
      args: 'run start',
      autorestart: true,
      max_memory_restart: '512M',
      // `next start` bez wcześniejszego builda kończy się błędem od razu. Pauza
      // jak przy bocie: pętla restartów zapchałaby logi w sekundy.
      restart_delay: 10000,
      env: { PANEL_PORT: zEnv('PANEL_PORT', '6200') },
    },
    // Fetcher NIE jest procesem PM2 — chodzi z crona co kilka minut i kończy się
    // po jednym przebiegu. Proces w pętli musiałby sam pilnować odstępów, a przy
    // awarii restartowałby się w kółko, paląc kredyt Apify. Wpis crona jest
    // w README (sekcja Deploy).
  ],
};
