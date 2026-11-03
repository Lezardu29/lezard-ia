// Connexion : avec son compte lezard-ia.fr d'abord (identifiant, mot de passe), « Se connecter sans compte » ensuite
// (clé maître ou clé d'accès). « #manuel » ouvre directement la connexion sans compte.
import { api, clearKey, errorMessage, getKey, saveKey } from "./auth.js";
import { accountMachines, keyForThisPc } from "./account-login.js";

const $ = (id) => document.getElementById(id);

function showError(text) {
  $("error-text").textContent = text;
  $("error").classList.add("show");
}

function hideError() {
  $("error").classList.remove("show");
}

function busy(button, on) {
  button.disabled = on;
  button.setAttribute("aria-busy", String(on));
}

// Lien de partage (12.6) : <adresse>/#key=<clé>. Le fragment n'est jamais envoyé au serveur ; il est retiré de
// la barre d'adresse et de l'historique avant toute requête.
// Après la connexion : la page demandée (« ?next=/marketplace »), sinon l'application. Liste fermée : jamais une
// adresse fournie par un lien.
const NEXT = new URLSearchParams(location.search).get("next") === "/marketplace" ? "/marketplace" : "/app";

async function fromLink() {
  const m = /^#key=([^&]+)/.exec(location.hash);
  if (!m) return false;
  history.replaceState(null, "", location.pathname + location.search);
  let key;
  try { key = decodeURIComponent(m[1]).trim(); } catch { key = ""; }
  if (!key) return false;
  const r = await api("/v1/me", { key });
  if (!r.ok) {
    showError(`Lien d'accès invalide ou révoqué : ${errorMessage(r)}`);
    return true;
  }
  saveKey(key, true);                                 // appareil de la personne invitée : on s'en souvient
  location.replace(NEXT);
  return true;
}

// Ouverte depuis Internet (adresse publique du tunnel) sans clé : la connexion se fait sur lezard-ia.fr, avec son
// compte, qui revient ici avec la clé (#key=…). Sur ce PC, en Wi-Fi (adresse privée, nom local), par Tailscale ou
// dans l'application Android, le formulaire reste ; « #manuel » le garde aussi (connexion « sans compte » du site).
export const ACCOUNT_PAGE = "https://lezard-ia.fr/web.html";

export function publicAddress(host) {
  const h = String(host || "").toLowerCase().replace(/^\[|\]$/g, "");
  if (!h.includes(".") || /\.(local|lan|home|internal|ts\.net)$/.test(h)) return false;   // nom local, Tailscale
  const ip = /^(\d+)\.(\d+)\.\d+\.\d+$/.exec(h);
  if (ip) {
    const [a, b] = [Number(ip[1]), Number(ip[2])];
    return !(a === 10 || a === 127 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31)
             || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127));
  }
  return !h.includes(":");                                  // IPv6 : réseau local ou lien, on garde le formulaire
}

function toAccountPage() {
  if (location.hash === "#manuel" || window.LezardApp || !publicAddress(location.hostname)) return false;
  location.replace(ACCOUNT_PAGE);
  return true;
}

// Déjà connecté avec une clé encore valide : direction l'application.
async function resume() {
  if (await fromLink()) return;
  const key = getKey();
  if (!key) { toAccountPage(); return; }
  const r = await api("/v1/me", { key });
  if (r.ok) location.replace(NEXT);
  else {
    clearKey();
    toAccountPage();
  }
}

async function login(event) {
  event.preventDefault();
  hideError();
  const key = $("key").value.trim();
  if (!key) {
    showError("Saisissez votre clé d'accès.");
    $("key").focus();
    return;
  }
  busy($("submit"), true);
  const r = await api("/v1/me", { key });
  busy($("submit"), false);
  if (!r.ok) {
    showError(errorMessage(r));
    $("key").select();
    return;
  }
  saveKey(key, $("remember").checked);
  location.replace(NEXT);
}

async function accountLogin(event) {
  event.preventDefault();
  hideError();
  const user = $("user").value.trim(), password = $("password").value;
  if (!user || !password) {
    showError("Saisissez votre identifiant et votre mot de passe.");
    (user ? $("password") : $("user")).focus();
    return;
  }
  busy($("account-submit"), true);
  try {
    const machines = await accountMachines(user, password);
    const key = machines.length ? await keyForThisPc(machines) : null;
    if (!key) {
      showError(machines.length ? "Ce compte n'est pas lié à ce PC : connectez-vous sans compte avec la clé maître, puis "
        + "Accès par le site › « Créer mon compte web » pour le lier." : "Aucun PC n'est lié à ce compte : connectez-vous sans compte "
        + "avec la clé maître, puis Accès par le site › « Créer mon compte web ».");
      return;
    }
    const r = await api("/v1/me", { key });                 // depuis Internet : le code de double authentification
    if (!r.ok) { showError(errorMessage(r)); return; }
    saveKey(key, $("account-remember").checked);
    location.replace(NEXT);
  } catch (e) {
    showError(e.message || "Connexion impossible.");
  } finally {
    busy($("account-submit"), false);
    $("password").value = "";
  }
}

function withoutAccount(show = true) {
  $("without-account").hidden = !show;
  $("no-account").setAttribute("aria-expanded", String(show));
  $("no-account").hidden = show;
  if (show) $("key").focus();
}

function toggleVisibility() {
  const input = $("key");
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  $("toggle").setAttribute("aria-pressed", String(show));
  $("toggle").setAttribute("aria-label", show ? "Masquer la clé" : "Afficher la clé");
  input.focus();
}

document.addEventListener("DOMContentLoaded", () => {
  $("account-form").addEventListener("submit", accountLogin);
  $("no-account").addEventListener("click", () => withoutAccount());
  if (location.hash === "#manuel") withoutAccount();
  $("login-form").addEventListener("submit", login);
  $("toggle").addEventListener("click", toggleVisibility);
  $("key").addEventListener("input", hideError);
  if (window.LezardApp?.changeServer) {             // application Android : revenir à l'écran de l'adresse du PC
    $("change-server").hidden = false;
    $("change-server").addEventListener("click", () => window.LezardApp.changeServer());
  }
  resume();
});
