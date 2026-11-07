// Premier lancement guidé (32.4) : modèles conseillés selon la carte graphique (13.2), protection du tableau de
// bord (clé d'administration, double authentification), offres. Affiché jusqu'à « C'est parti » ; « Plus tard »
// le cache seulement pour cette visite.
import { api } from "./auth.js";

const $ = (id) => document.getElementById(id);
const FITS = { vram: "rapide, tient dans la carte graphique", offload: "plus lent, déborde en mémoire vive" };

function item(text, done) {
  const li = document.createElement("li");
  li.textContent = text;
  if (done) li.className = "wg-done";
  return li;
}

async function models() {
  const r = await api("/v1/admin/models/recommendations");
  if (!r.ok) {
    $("wg-hw").textContent = "Les modèles conseillés sont sur la page Modèles.";
    return;
  }
  const { budget, best, uses, models: all } = r.data;
  const gb = (b) => `${Math.round(b / 1024 ** 3)} Go`;   // Go binaires, comme Windows : 16 Go pour une carte de 16 Go
  $("wg-hw").textContent = budget.gpu
    ? `Carte graphique : ${budget.gpu}${budget.vram ? ` (${gb(budget.vram)} utilisables pour l'IA)` : ""}. Conseillés pour vous :`
    : "Pas de carte graphique reconnue : des modèles légers, sur le processeur. Conseillés pour vous :";
  const picked = Object.entries(best).filter(([, m]) => m);
  $("wg-models").replaceChildren(...picked.map(([use, name]) => {
    const m = all.find((x) => x.model === name) || {};
    const state = m.installed ? "déjà installé" : FITS[m.fit] || "";
    return item(`${uses[use] || use} : ${m.label || name}${state ? ` (${state})` : ""}`, m.installed);
  }));
  if (!picked.length) $("wg-models").replaceChildren(item("Aucun modèle du catalogue ne tient sur ce PC."));
}

async function totp() {
  const r = await api("/v1/admin/totp");
  const on = r.ok && r.data.enabled;
  $("wg-totp").textContent = on ? "✓ Double authentification activée." : "";
  $("wg-totp-link").hidden = on;
}

export async function initWelcome(me) {
  const r = await api("/v1/admin/welcome");
  if (!r.ok || r.data.done) return;
  const steps = [...document.querySelectorAll("#welcome-section .wg-step")];
  let at = 0;
  const show = () => {
    steps.forEach((s, i) => { s.hidden = i !== at; });
    $("wg-step").textContent = `${at + 1} / ${steps.length}`;
    $("wg-back").hidden = at === 0;
    $("wg-next").textContent = at === steps.length - 1 ? "C'est parti" : "Suivant";
  };
  $("wg-next").addEventListener("click", async () => {
    if (at < steps.length - 1) {
      at += 1;
      if (at === 1) totp();
      return show();
    }
    await api("/v1/admin/welcome", { method: "POST", body: {} });
    $("welcome-section").hidden = true;
  });
  $("wg-back").addEventListener("click", () => { at -= 1; show(); });
  $("wg-skip").addEventListener("click", () => { $("welcome-section").hidden = true; });
  show();
  $("welcome-section").hidden = false;
  models();
}
