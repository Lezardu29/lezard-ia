// Section « Comptes » (20.3), clé maître : comptes nommés en base (créer, désactiver, nouveau jeton,
// supprimer) ; ceux de GATEWAY_KEYS (.env) en lecture seule. Le jeton n'est montré qu'une fois.
import { api, errorMessage } from "./auth.js";

const $ = (id) => document.getElementById(id);
let gen = 0;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function button(label, cls, onClick) {
  const b = el("button", `btn btn-inline ${cls}`, label);
  b.type = "button";
  b.addEventListener("click", () => onClick(b));
  return b;
}
function message(text, ok = false) {
  const b = $("ac-msg");
  b.hidden = !text;
  b.className = `alert${text ? " show" : ""}${ok ? " ok-box" : ""}`;
  b.textContent = text || "";
}
const when = (ts) => (ts ? `utilisé le ${new Date(ts * 1000).toLocaleString("fr-FR", { dateStyle: "medium", timeStyle: "short" })}`
  : "jamais utilisé");

function showToken(account, intro) {
  $("ac-token").hidden = false;
  $("ac-token").className = "alert show warn-box";
  $("ac-token-text").textContent = intro;
  $("ac-token-value").textContent = account.token;
  $("ac-token-link").textContent = `${location.origin}/#key=${encodeURIComponent(account.token)}`;
}
function hideToken() {                                    // le jeton ne reste pas dans la page
  $("ac-token").hidden = true;
  $("ac-token-value").textContent = "";
  $("ac-token-link").textContent = "";
}

function item(a) {
  const li = el("li", "bridge");
  const text = el("div", "bridge-text");
  const head = el("div", "bridge-head");
  head.append(el("strong", "", a.name));
  if (a.source === "env") head.append(el("span", "pill", ".env"));
  else if (a.status !== "active") head.append(el("span", "pill busy-pill", "désactivé"));
  text.append(head);
  text.append(el("p", "muted small", a.source === "env"
    ? "Défini dans GATEWAY_KEYS (.env) : modifiable seulement dans ce fichier."
    : `Jeton ${a.token_prefix} · ${when(a.last_used_at)}`));
  li.append(text);
  if (a.source === "env") return li;
  const side = el("div", "gpu-server-side");
  side.append(
    button(a.status === "active" ? "Désactiver" : "Réactiver", "btn-ghost",
           () => update(a, { status: a.status === "active" ? "disabled" : "active" })),
    button("Nouveau jeton", "btn-ghost", () => rotate(a)),
    button("Supprimer", "btn-ghost", () => remove(a)));
  li.append(side);
  return li;
}

async function load() {
  const mine = ++gen;
  const r = await api("/v1/admin/accounts");
  if (mine !== gen || !r.ok) return;
  $("accounts-section").hidden = false;
  const list = $("ac-list");
  list.replaceChildren(...r.data.data.map(item), ...r.data.env.map(item));
  if (!list.children.length) list.append(el("li", "muted small", "Aucun compte : seule la clé maître accède à la gateway."));
}

async function create(e) {
  e.preventDefault();
  const r = await api("/v1/admin/accounts", { method: "POST", body: { name: $("ac-name").value } });
  if (!r.ok) return message(errorMessage(r));
  message("");
  $("ac-name").value = "";
  showToken(r.data, `Compte « ${r.data.name} » créé. Son jeton ne sera plus jamais affiché :`);
  load();
}

async function update(a, body) {
  if (body.status === "disabled"
      && !confirm(`Désactiver « ${a.name} » ? Son jeton et ses clés seront refusés tout de suite.`)) return load();
  const r = await api(`/v1/admin/accounts/${encodeURIComponent(a.id)}`, { method: "PATCH", body });
  message(r.ok ? "" : errorMessage(r));
  load();
}

async function rotate(a) {
  if (!confirm(`Nouveau jeton pour « ${a.name} » ? L'ancien cessera aussitôt de fonctionner (ses clés et données restent).`)) return;
  const r = await api(`/v1/admin/accounts/${encodeURIComponent(a.id)}/rotate`, { method: "POST" });
  if (!r.ok) return message(errorMessage(r));
  showToken(r.data, `Nouveau jeton de « ${a.name} » (l'ancien est refusé) :`);
  load();
}

async function remove(a) {
  if (!confirm(`Supprimer « ${a.name} » ? Son jeton et toutes ses clés virtuelles seront refusés.`)) return;
  const r = await api(`/v1/admin/accounts/${encodeURIComponent(a.id)}`, { method: "DELETE" });
  if (!r.ok) return message(errorMessage(r));
  message(`Compte supprimé${r.data.keys_revoked ? ` · ${r.data.keys_revoked} clé(s) révoquée(s)` : ""}.`, true);
  load();
}

export function initAccounts() {
  $("ac-form").addEventListener("submit", create);
  $("ac-token-close").addEventListener("click", hideToken);
  $("ac-token-copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("ac-token-value").textContent);
      $("ac-token-copy").textContent = "Copié ✓";
    } catch { $("ac-token-copy").textContent = "Sélectionnez et copiez"; }
    setTimeout(() => { $("ac-token-copy").textContent = "Copier"; }, 1500);
  });
  load();
}
