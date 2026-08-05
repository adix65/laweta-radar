// Tailwind 4 wchodzi wyłącznie przez wtyczkę PostCSS — nie ma już
// tailwind.config.js z contentem. Konfiguracja motywu siedzi w app/globals.css
// (dyrektywa @theme), czyli w tym samym pliku co style. Jedno miejsce zamiast
// dwóch, które trzeba trzymać w zgodzie.
const config = { plugins: { "@tailwindcss/postcss": {} } };
export default config;
