// Flat config wprost z `eslint-config-next` (v16 eksportuje gotową tablicę).
//
// Świadomie BEZ `FlatCompat`: warstwa zgodności ze starym `.eslintrc` wywala się
// przy tej wersji na cyklicznej strukturze podczas walidacji schematu — a nie ma
// tu czego kompatybilizować, bo ten projekt nie ma żadnej konfiguracji w starym
// formacie.
import next from "eslint-config-next";

const konfiguracja = [
  ...next,
  {
    ignores: [
      ".next/**",
      "node_modules/**",
      // Service worker chodzi w INNYM środowisku (globalne `self`, zero DOM-u),
      // więc reguły pisane dla kodu strony dają tu same fałszywe trafienia.
      "public/sw.js",
    ],
  },
];

export default konfiguracja;
