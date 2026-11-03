// Installation en application (13.6) : enregistre le service worker et propose « Installer l'application ».
// Une PWA exige un contexte sécurisé (HTTPS, tunnel, LAN HTTPS 12.7, ou localhost) : en HTTP sur le réseau local,
// aucun bouton inopérant ; l'avertissement de connexion non chiffrée (net-warning.js) l'explique.
const IOS = /iPhone|iPad|iPod/i.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);     // iPadOS se présente comme un Mac

export function initPwa(button) {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.register("/sw.js").catch(() => { /* sans service worker : site normal */ });
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  if (!button || standalone) return;                                       // déjà ouverte en application
  let deferred = null;
  window.addEventListener("beforeinstallprompt", (e) => {                  // Chrome, Edge, Android
    e.preventDefault();
    deferred = e;
    button.hidden = false;
  });
  window.addEventListener("appinstalled", () => { deferred = null; button.hidden = true; });
  if (IOS) button.hidden = false;                       // Safari iOS : pas d'invite, installation par le menu Partager
  button.addEventListener("click", async () => {
    if (deferred) {
      deferred.prompt();
      await deferred.userChoice;
      deferred = null;
      button.hidden = true;
    } else if (IOS) {
      alert("Pour installer l'application : touchez le bouton Partager de Safari, puis « Sur l'écran d'accueil ».");
    }
  });
}
