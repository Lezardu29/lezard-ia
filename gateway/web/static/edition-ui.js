// Offre de l'installation (29.4) : section « Accès par le site » (clé maître). Tout le logiciel est inclus et open source,
// y compris l'utilisation depuis n'importe où avec un compte gratuit lezard-ia.fr (« Créer mon compte web »). Un code
// de licence reste accepté (facultatif).
import { api, getKey } from "./auth.js";
import { loadTotp } from "./totp-ui.js";

const FREE = [
  "Modèles locaux (ce PC, les machines de la maison) et modèles cloud avec votre clé",
  "Chat, documents, voix, agent de contrôle du PC, recherche web",
  "Accès depuis Internet par votre propre tunnel, clés API pour d'autres logiciels",
  "Utilisation depuis n'importe où avec un compte gratuit lezard-ia.fr, protégée par la double authentification de ce PC",
];

const $ = (id) => document.getElementById(id);

function node(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function list(items, cls) {
  const ul = node("ul", `eo-list ${cls}`);
  ul.append(...items.map((t) => node("li", "", t)));
  return ul;
}

const STATES = { valide: "valide", grace: "expirée : encore active quelques jours, le temps de la renouveler" };
const day = (t) => new Date(t * 1000).toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" });

function renderLicense(data) {
  const lic = data.license;
  $("eo-license-state").textContent = lic
    ? `Licence ${STATES[lic.status] || lic.status} — jusqu'au ${day(lic.status === "grace" ? lic.grace_until : lic.expires)}.`
    : data.license_error ? `Licence refusée : ${data.license_error}` : "";
  $("eo-state").hidden = !$("eo-license-state").textContent;
  $("eo-license-remove").hidden = !lic;
  $("eo-activate").hidden = Boolean(lic) && lic.status !== "expiree";
}

const CODE = /^LZ1\.[\w-]+\.[\w-]+$/;
function checkCode() {
  const v = $("eo-license").value.trim();
  $("eo-field").classList.toggle("valid", CODE.test(v));
  $("eo-field").classList.toggle("invalid", v.length > 8 && !CODE.test(v));
  $("eo-license-go").disabled = !CODE.test(v);
}

function message(text, ok = false) {
  $("eo-msg").textContent = text;
  $("eo-msg").className = `alert${text ? " show" : ""}${ok ? " ok-box" : ""}`;
}

async function activate(e) {
  e.preventDefault();
  const r = await api("/v1/admin/license", { method: "POST", body: { license: $("eo-license").value } });
  if (!r.ok) return message(r.data?.error?.message || "Activation impossible.");
  $("eo-license").value = "";
  checkCode();
  render(r.data);
  message("Licence activée : accès par le site web. La page se recharge…", true);
  setTimeout(() => location.reload(), 1200);              // les autres sections suivent
}

async function removeLicense() {
  const r = await api("/v1/admin/license", { method: "DELETE" });
  if (!r.ok) return;
  message("Licence retirée : l'accès par le site web est coupé. La page se recharge…", true);
  render(r.data);
  setTimeout(() => location.reload(), 1200);
}

// « Créer mon compte web » : clé web neuve + adresse publique, remises à lezard-ia.fr dans le fragment #lier=…
// (jamais envoyé à un serveur) ; le site oblige à les lier à un compte, qui les garde chiffrées. La double
// authentification de ce PC est activée d'abord : depuis Internet, la clé web demande toujours un code.
const sayWeb = (text) => { $("eo-webacc-msg").textContent = text; $("eo-webacc-msg").hidden = !text; };

async function webAccount() {
  sayWeb("");
  const t = await api("/v1/admin/totp");
  if (!t.ok) return sayWeb(t.data?.error?.message || "Double authentification illisible.");
  if (t.data.enabled) return createWebAccount();
  const s = await api("/v1/admin/totp/setup", { method: "POST" });
  if (!s.ok) return sayWeb(s.data?.error?.message || "Activation impossible.");
  $("eo-webacc-qr").hidden = !s.data.svg;
  if (s.data.svg) $("eo-webacc-qr").src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(s.data.svg)}`;
  $("eo-webacc-secret").textContent = s.data.secret.replace(/(.{4})/g, "$1 ").trim();
  $("eo-webacc-go").hidden = true;
  $("eo-webacc-2fa").hidden = false;
  $("eo-webacc-code").focus();
}

async function enableWeb2fa(e) {
  e.preventDefault();
  const code = $("eo-webacc-code").value.trim();
  if (!/^\d{6}$/.test(code)) return sayWeb("Saisissez les 6 chiffres affichés par l'application.");
  const r = await api("/v1/admin/totp/enable", { method: "POST", body: { code } });
  if (!r.ok) { $("eo-webacc-code").select(); return sayWeb(r.data?.error?.message || "Code refusé."); }
  sayWeb("");
  $("eo-webacc-2fa").hidden = true;
  $("eo-webacc-list").replaceChildren(...r.data.recovery_codes.map((c) => Object.assign(document.createElement("span"), { textContent: c })));
  $("eo-webacc-codes").hidden = false;
  loadTotp();
}

async function createWebAccount() {
  const tab = window.open("", "_blank");             // ouvert tout de suite : sinon bloqué comme fenêtre surgissante
  sayWeb("Préparation… La première fois, ce PC reçoit son adresse sur Internet (<nom>.lezard-ia.fr) et télécharge "
    + "cloudflared (environ 55 Mo) : une minute au plus.");
  const r = await api("/v1/admin/web-account", { method: "POST", body: {} });
  if (!r.ok) {
    tab?.close();
    return sayWeb(r.data?.error?.message || "Création impossible.");
  }
  $("eo-webacc-codes").hidden = true;
  $("eo-webacc-list").replaceChildren();
  $("eo-webacc-go").hidden = false;
  const link = btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify({ name: r.data.name, url: r.data.url, key: r.data.key }))))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const target = `${r.data.site}#lier=${link}`;
  if (tab) { tab.opener = null; tab.location.href = target; } else location.href = target;
  sayWeb(`Nouvelle clé web créée pour ${r.data.url} (l'ancienne ne marche plus). Terminez sur lezard-ia.fr : créez votre`
      + " compte ou connectez-vous. Depuis Internet, le code de l'application sera demandé." + (r.data.stable ? ""
      : " Attention : cette adresse change à chaque redémarrage du tunnel ; refaites alors ce lien."));
}

// retour du compte client (lezard-ia.fr) : licence dans le fragment #licence=…, jamais envoyé à un serveur
async function received() {
  const m = location.hash.match(/^#licence=(LZ1\.[\w.-]+)$/);
  if (!m) return;
  history.replaceState(null, "", location.pathname + location.search);
  if (!confirm("Activer la licence d'accès web reçue de votre compte ?")) return;
  $("eo-license").value = decodeURIComponent(m[1]);
  await activate(new Event("submit"));
}

export async function initEdition() {
  const r = await api("/v1/admin/edition");
  if (!r.ok) return null;
  $("eo-web-copy").addEventListener("click", async () => {
    const host = $("eo-web-key").textContent.split(".")[1];
    await navigator.clipboard.writeText(`lz.${host}.${getKey()}`);
    message("Clé web copiée : gardez-la pour vous, elle donne accès à votre IA.", true);
  });
  $("eo-webacc-go").addEventListener("click", webAccount);
  $("eo-webacc-form").addEventListener("submit", enableWeb2fa);
  $("eo-webacc-next").addEventListener("click", createWebAccount);
  $("eo-license-form").addEventListener("submit", activate);
  $("eo-license").addEventListener("input", checkCode);
  checkCode();
  $("eo-license-remove").addEventListener("click", removeLicense);
  render(r.data);
  await received();
  return r.data.edition;
}

function render(data) {
  const { edition } = data;
  const free = edition === "gratuit";
  $("eo-pill").textContent = free ? "Open source" : "Licence active";
  $("eo-pill").classList.toggle("pill-on", !free);
  $("eo-intro").textContent = "Tout est gratuit et open source : toutes les fonctions sont incluses.";
  const body = $("eo-body");
  body.replaceChildren(list(FREE, "included"));
  const { keys = 0, cloud = [] } = data.paused || {};       // licence finie : rien n'est perdu (29.5)
  if (free && (keys || cloud.length)) {
    const kept = [];
    if (keys) kept.push(`${keys} clé${keys > 1 ? "s" : ""} API pour d'autres logiciels (suspendue${keys > 1 ? "s" : ""})`);
    if (cloud.length) kept.push(`modèles cloud : ${cloud.join(", ")}`);
    body.append(node("p", "small muted", "Gardé tel quel, et réactivé dès le retour de la licence :"), list(kept, "locked"));
  }
  renderLicense(data);
  // clé web (31.6) : lz.<nom>.<clé de cette session> ; affichée masquée, copiée seulement sur demande
  $("eo-web").hidden = !(data.web_host && !free);
  if (data.web_host) $("eo-web-key").textContent = `lz.${data.web_host}.••••••`;
  $("edition-section").hidden = false;
}
