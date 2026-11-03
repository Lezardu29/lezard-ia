// Panneau « Connecter un téléphone » : QR code à scanner avec l'application Android (android/) ou l'appareil photo
// du téléphone. Il porte l'adresse de la gateway et la clé de CETTE session (lien <adresse>/#key=…, 12.6) : le
// téléphone retrouve les mêmes conversations. Le QR n'est fabriqué qu'à l'ouverture et retiré à la fermeture.
import { api, errorMessage, getKey } from "./auth.js";

const NOTES = {
  internet: "Fonctionne de partout (4G, autre Wi-Fi).",
  tailscale: "Fonctionne sur les téléphones connectés à votre réseau Tailscale.",
  lan: "Fonctionne seulement quand le téléphone est sur le même Wi-Fi que ce PC.",
  current: "Adresse utilisée par ce navigateur.",
};
let panel, button, urls = [];

const $p = (sel) => panel.querySelector(sel);

function build() {
  panel = document.createElement("div");
  panel.className = "theme-panel phone-panel";
  panel.id = "phone-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-labelledby", "phone-title");
  panel.hidden = true;
  panel.innerHTML = `
    <div class="theme-head"><h2 id="phone-title">Connecter un téléphone</h2>
      <button type="button" class="icon-btn" data-close aria-label="Fermer">✕</button></div>
    <ol class="phone-steps">
      <li>Ouvrez l'application Lezard IA sur le téléphone.</li>
      <li>Touchez « Scanner le QR code » et visez ce code.</li>
    </ol>
    <label class="phone-url" data-url-wrap hidden>Adresse <select data-url></select></label>
    <figure class="phone-qr" data-qr-wrap hidden>
      <img data-qr alt="QR code de connexion du téléphone" width="220" height="220">
    </figure>
    <p class="theme-status" data-note></p>
    <p class="alert" data-error role="alert"></p>
    <p class="theme-status phone-warn">Ce code donne accès à votre IA avec votre clé : ne le montrez qu'à vos propres
      appareils. Sans l'application, l'appareil photo du téléphone ouvre le chat dans le navigateur.</p>`;
  document.body.append(panel);
  $p("[data-close]").addEventListener("click", close);
  $p("[data-url]").addEventListener("change", render);
}

function message(text) {
  $p("[data-error]").textContent = text || "";
  $p("[data-error]").classList.toggle("show", !!text);
}

async function render() {
  const url = $p("[data-url]").value;
  const u = urls.find((x) => x.url === url);
  $p("[data-qr-wrap]").hidden = true;
  let note = NOTES[u?.kind] || "";
  if (u?.kind === "lan" && !u.secure) note += " Connexion non chiffrée : la dictée vocale n'y fonctionne pas.";
  $p("[data-note]").textContent = note;
  if (!u) return;
  const r = await api("/v1/access/qr", { method: "POST", body: { url: u.url, key: getKey() } });
  if (panel.hidden || $p("[data-url]").value !== url) return;    // fermé ou autre adresse entre-temps
  if (!r.ok) return message(errorMessage(r));
  $p("[data-qr]").src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(r.data.svg)}`;
  $p("[data-qr-wrap]").hidden = false;
}

async function open() {
  panel.hidden = false;
  button.setAttribute("aria-expanded", "true");
  message("");
  $p("[data-note]").textContent = "";
  const r = await api("/v1/access");
  if (panel.hidden) return;
  if (!r.ok) return message(errorMessage(r));
  urls = (r.data.urls || []).filter((u) => u.kind !== "local");   // 127.0.0.1 : injoignable depuis un téléphone
  if (!urls.length) {
    return message("Aucun autre appareil ne peut joindre ce PC pour l'instant. Activez l'accès distant " +
                   "ou, à la maison, le mode réseau local (lancer.ps1 -Lan).");
  }
  const sel = $p("[data-url]");
  sel.length = 0;
  for (const u of urls) sel.add(new Option(`${u.label} — ${u.url}`, u.url));
  $p("[data-url-wrap]").hidden = urls.length < 2;
  await render();
}

function close() {
  panel.hidden = true;
  button.setAttribute("aria-expanded", "false");
  $p("[data-qr]").removeAttribute("src");                       // la clé ne reste pas dans la page
  $p("[data-qr-wrap]").hidden = true;
  button.focus();
}

// Bouton visible pour une session qui gère ses accès (clé maître ou compte) ; pas dans l'application Android, qui
// est déjà le téléphone.
export async function initPhonePanel(trigger) {
  if (window.LezardApp || !getKey()) return;
  const r = await api("/v1/access");
  if (!r.ok) return;
  button = trigger;
  button.hidden = false;
  build();
  button.setAttribute("aria-controls", "phone-panel");
  button.setAttribute("aria-expanded", "false");
  button.addEventListener("click", () => (panel.hidden ? open() : close()));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) close(); });
  document.addEventListener("mousedown", (e) => {
    if (!panel.hidden && !panel.contains(e.target) && !button.contains(e.target)) close();
  });
}
