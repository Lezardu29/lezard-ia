// Écran de chargement au démarrage : le logo Lezard IA animé, à la PREMIÈRE page ouverte dans l'onglet seulement
// (pas à chaque changement de page). Retiré une fois la page chargée, après 700 ms au moins pour ne pas clignoter ;
// si la page ne finit jamais de charger, le CSS le fait disparaître seul au bout de 6 s.
// Script classique (pas un module), chargé dans <head> : la classe est posée avant le premier affichage.
(() => {
  let first = true;
  try {
    first = !sessionStorage.getItem("gw.splash");
    sessionStorage.setItem("gw.splash", "1");
  } catch { /* stockage bloqué (navigation privée stricte) : écran montré, sans gêne */ }
  if (!first) return;
  const root = document.documentElement;
  const t0 = performance.now();
  root.classList.add("lz-splashing");
  addEventListener("load", () => {
    setTimeout(() => {
      root.classList.add("lz-splash-out");
      setTimeout(() => root.classList.remove("lz-splashing", "lz-splash-out"), 350);
    }, Math.max(0, 700 - (performance.now() - t0)));
  });
})();
