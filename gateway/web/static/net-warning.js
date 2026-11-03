// Avertissement de connexion non chiffrée (12.7) : page ouverte en http:// depuis une autre machine que la
// gateway (réseau local). La clé d'accès et les conversations circulent alors en clair sur le Wi-Fi.
const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

function insecure() {
  return location.protocol === "http:" && !LOOPBACK.has(location.hostname);
}

function show() {
  try { if (sessionStorage.getItem("gw.netWarning") === "hidden") return; } catch { /* ignoré */ }
  const bar = document.createElement("div");
  bar.className = "net-warning";
  bar.setAttribute("role", "alert");
  const text = document.createElement("span");
  text.textContent = "Connexion non chiffrée : sur ce réseau, votre clé d'accès et vos conversations peuvent être " +
    "lues par les autres appareils. Utilisez de préférence l'adresse https:// de la gateway (son propriétaire " +
    "l'active avec lancer.ps1 -Https), Tailscale ou l'accès distant. L'installation en application et la dictée " +
    "vocale ne fonctionnent aussi qu'en https://.";
  const close = document.createElement("button");
  close.type = "button";
  close.textContent = "Compris";
  close.addEventListener("click", () => {
    bar.remove();
    try { sessionStorage.setItem("gw.netWarning", "hidden"); } catch { /* ignoré */ }
  });
  bar.append(text, close);
  document.body.prepend(bar);
}

if (insecure()) {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", show);
  else show();
}
