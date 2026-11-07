// Nouvelle version de Lezard IA (clé maître) : proposée par un bandeau à chaque lancement, jamais installée sans
// accord. La vérification sur GitHub (version signée) est faite par la gateway au démarrage, puis toutes les
// 5 minutes ; la page ouverte la relit aussi toutes les 5 minutes (watchUpdates) : le bandeau apparaît sans recharger.
// « Plus tard » : rien jusqu'au prochain lancement (onglet fermé puis rouvert).
import { api, errorMessage, rawApi } from "./auth.js";

const EVERY = 5 * 60 * 1000;

/** Page ouverte : la nouvelle version, dès que la gateway l'a vue (jamais de demande de code pour ça). */
export function watchUpdates() {
  setInterval(async () => {
    if (document.hidden) return;                       // onglet en arrière-plan : relu au prochain passage
    const r = await rawApi("/v1/me");
    if (r.ok) offerUpdate(r.data);
  }, EVERY);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

/** Installe la dernière version signée (bandeau et notification du tableau de bord) : `go` montre l'avancement,
 * `text` reçoit l'erreur ou la suite ; `others` (ex. « Plus tard ») sont désactivés pendant l'envoi. Vrai si lancée. */
export async function installLatest(version, go, text, others = []) {
  go.disabled = true;
  others.forEach((b) => { b.disabled = true; });
  go.textContent = "Téléchargement et vérification…";
  const r = await api("/v1/admin/update/install", { method: "POST", body: { target: "latest" } });
  if (!r.ok) {
    go.disabled = false;
    others.forEach((b) => { b.disabled = false; });
    go.textContent = "Réessayer";
    text.textContent = `Mise à jour impossible : ${errorMessage(r)}`;
    return false;
  }
  text.textContent = `Installation de la ${version} : Lezard IA redémarre tout seul (une minute environ), `
    + "puis rechargez cette page. Vos conversations et réglages sont gardés.";
  return true;
}

export function offerUpdate(me) {
  const u = me.update;
  if (!me.is_admin || !u?.available || !u.version) return;
  const later = `lezard.update.later.${u.version}`;
  try { if (sessionStorage.getItem(later)) return; } catch { /* stockage indisponible : on propose */ }
  if (document.querySelector(".update-offer")) return;

  const bar = el("div", "update-offer");
  bar.setAttribute("role", "status");
  const text = el("p", "", `Lezard IA ${u.version} est disponible (vous avez la ${me.version}).`);
  const actions = el("div", "update-offer-actions");
  const close = el("button", "btn btn-ghost btn-inline", "Plus tard");
  close.type = "button";
  close.addEventListener("click", () => {
    try { sessionStorage.setItem(later, "1"); } catch { /* ignoré */ }
    bar.remove();
  });
  if (u.installable) {
    const go = el("button", "btn btn-primary btn-inline", "Mettre à jour");
    go.type = "button";
    go.addEventListener("click", async () => {
      if (await installLatest(u.version, go, text, [close])) actions.remove();
    });
    actions.append(go);
  } else {
    text.textContent += " Pour mettre à jour cette copie : sur le PC, lancer.ps1 -Update.";
  }
  actions.append(close);
  bar.append(text, actions);
  document.body.prepend(bar);
}
