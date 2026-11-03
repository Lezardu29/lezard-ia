// Panneau « Supervision » (18.5), clé maître : serveurs locaux surveillés, disque, seuil d'alerte, webhook,
// alerte d'essai et branchement de Prometheus sur /metrics.
import { api, errorMessage } from "./auth.js";

const $ = (id) => document.getElementById(id);
const gb = (b) => `${(b / 1024 ** 3).toFixed(1).replace(".", ",")} Go`;
let gen = 0;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function message(text, ok = false) {
  const b = $("sv2-msg");
  b.hidden = !text;
  b.className = `alert${text ? " show" : ""}${ok ? " ok-box" : ""}`;
  b.textContent = text || "";
}

function render(d) {
  $("supervision-section").hidden = false;
  const list = $("sv2-servers");
  list.replaceChildren();
  for (const s of d.servers) {
    const li = el("li", "bridge");
    const text = el("div", "bridge-text");
    const head = el("div", "bridge-head");
    head.append(el("strong", "", s.name),
                el("span", `pill${s.up === false ? " busy-pill" : ""}`, s.up ? "joignable" : s.up === false ? "injoignable" : "…"));
    text.append(head);
    if (s.error && s.up === false) text.append(el("p", "muted small", s.error));
    li.append(text);
    list.append(li);
  }
  if (!d.servers.length) list.append(el("li", "muted small", "Serveurs locaux vérifiés chaque minute ; premier passage après le démarrage."));
  $("sv2-disk").textContent = d.disk
    ? `Disque de la base : ${gb(d.disk.free)} libres sur ${gb(d.disk.total)}${d.disk_low ? " — sous le seuil d'alerte" : ""}.`
    : "Espace disque inconnu.";
  $("sv2-enabled").checked = d.enabled;
  $("sv2-min").value = d.disk_min_gb;
  $("sv2-hook").value = d.webhook_url || "";
  $("sv2-secret").hidden = !d.webhook_secret;
  $("sv2-secret-value").textContent = d.webhook_secret || "";
  $("sv2-scrape").textContent = `${location.origin}/metrics`;
  $("sv2-token").textContent = d.metrics_token
    ? "Jeton de lecture GATEWAY_METRICS_TOKEN configuré : Authorization: Bearer <jeton> dans Prometheus."
    : "Depuis ce PC sans configuration ; à distance, définissez GATEWAY_METRICS_TOKEN (24 caractères ou plus) dans .env.";
}

async function load() {
  const mine = ++gen;
  const r = await api("/v1/admin/supervision");
  if (mine === gen && r.ok) render(r.data);
}

async function save(e) {
  e.preventDefault();
  const body = { enabled: $("sv2-enabled").checked, disk_min_gb: Number($("sv2-min").value || 0),
                 webhook_url: $("sv2-hook").value.trim() || null };
  const r = await api("/v1/admin/supervision", { method: "PUT", body });
  if (!r.ok) return message(errorMessage(r));
  render(r.data);
  message("Réglages enregistrés.", true);
}

async function test() {
  $("sv2-test").disabled = true;
  const r = await api("/v1/admin/supervision/check", { method: "POST", body: { test: true } });
  $("sv2-test").disabled = false;
  if (!r.ok) return message(errorMessage(r));
  render(r.data);
  message("Alerte d'essai envoyée (notifications" + (r.data.webhook_url ? " et webhook" : "") + ").", true);
}

export function initSupervision() {
  $("sv2-form").addEventListener("submit", save);
  $("sv2-test").addEventListener("click", test);
  load();
}
