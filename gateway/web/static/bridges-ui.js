// Panneau « Ponts vers vos modèles locaux » (7.4) : création (jeton affiché une fois), état en ligne, suppression.
import { api, errorMessage } from "./auth.js";

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function message(text, ok = false) {
  const m = $("br-msg");
  m.textContent = text;
  m.className = `alert${text ? " show" : ""}${ok ? " ok-box" : ""}`;
}

const ago = (ts) => {
  if (!ts) return "jamais connecté";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "vu à l'instant";
  if (s < 3600) return `vu il y a ${Math.round(s / 60)} min`;
  if (s < 86400) return `vu il y a ${Math.round(s / 3600)} h`;
  return `vu le ${new Date(ts * 1000).toLocaleDateString("fr-FR", { day: "numeric", month: "long" })}`;
};

function render(data) {
  const managed = data.instance === "managed";
  $("br-instance").textContent = managed ? "Gateway hébergée" : "Auto-hébergée";
  const locked = $("br-locked");
  locked.hidden = true;
  $("br-form").hidden = !data.allowed;
  const list = $("br-list");
  list.replaceChildren();
  for (const b of data.data) {
    const li = el("li", "bridge");
    const head = el("div", "bridge-head");
    head.append(el("strong", "", b.name),
                el("span", `status ${b.online ? "active" : "revoked"}`, b.online ? "En ligne" : "Hors ligne"));
    const models = b.models.length ? b.models.map((m) => `${b.name}/${m}`).join(", ") : "aucun modèle détecté";
    const info = el("p", "muted small", `${models} · ${b.online ? "connecté" : ago(b.last_seen_at)} · jeton ${b.token_prefix}…`);
    const del = el("button", "btn btn-ghost btn-inline", "Supprimer");
    del.type = "button";
    del.addEventListener("click", () => remove(b, del));
    const text = el("div", "bridge-text");
    text.append(head, info);
    li.append(text, del);
    list.append(li);
  }
  if (!data.data.length && data.allowed) {
    list.append(el("li", "empty", "Aucun pont. Créez-en un pour utiliser vos modèles locaux depuis cette gateway."));
  }
}

export async function loadBridges() {
  const r = await api("/v1/bridges");
  if (!r.ok) return;
  $("bridges-section").hidden = false;
  render(r.data);
}

async function create(e) {
  e.preventDefault();
  const button = $("br-create");
  button.disabled = true;
  const r = await api("/v1/bridges", { method: "POST", body: { name: $("br-name").value.trim() } });
  button.disabled = false;
  if (!r.ok) return message(errorMessage(r));
  message("");
  $("br-name").value = "";
  $("bd-command").textContent = r.data.command;
  $("bd-docker").textContent = r.data.docker;
  $("bridge-dialog").showModal();
  loadBridges();
}

async function remove(b, button) {
  if (!confirm(`Supprimer le pont « ${b.name} » ? L'agent est déconnecté et son jeton révoqué.`)) return;
  button.disabled = true;
  const r = await api(`/v1/bridges/${encodeURIComponent(b.id)}`, { method: "DELETE" });
  if (!r.ok) { button.disabled = false; return message(errorMessage(r)); }
  message(`Pont « ${b.name} » supprimé.`, true);
  loadBridges();
}

export function initBridges() {
  $("br-form").addEventListener("submit", create);
  const dlg = $("bridge-dialog");
  dlg.querySelectorAll("[data-copy]").forEach((btn) => btn.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($(btn.dataset.copy).textContent); btn.textContent = "Copié ✓"; }
    catch { btn.textContent = "Sélectionnez et copiez"; }
    setTimeout(() => { btn.textContent = "Copier"; }, 1500);
  }));
  dlg.querySelector("[data-close]").addEventListener("click", () => dlg.close());
  // les commandes contiennent le jeton : rien ne reste dans le DOM après fermeture
  dlg.addEventListener("close", () => { $("bd-command").textContent = ""; $("bd-docker").textContent = ""; });
  loadBridges();
}
