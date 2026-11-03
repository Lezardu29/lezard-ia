// Panneau « Journal d'audit » (20.2), clé maître : qui a fait quoi, d'où, avec quel résultat. Filtres, pages,
// export CSV (fetch authentifié -> fichier), conservation réglable.
import { api, errorMessage, getKey } from "./auth.js";
import { saveBlob } from "./save-file.js";

const $ = (id) => document.getElementById(id);
const PAGE = 50;
const CATEGORY_LABELS = {
  key: "Clés virtuelles", credentials: "Clés des fournisseurs", provider: "Fournisseurs", remote: "Accès distant",
  lan: "Réseau local", ollama: "Ollama", model: "Modèles", security: "Blocages d'IP", backup: "Sauvegardes",
  encryption_key: "Clé de chiffrement", gateway: "Redémarrage", pricing: "Tarifs", totp: "Double authentification",
  admin_session: "Sessions d'administration", bridge: "Ponts", alert: "Alertes", audit: "Journal",
  outil: "Contrôle du PC",
};
const RETENTION = [[30, "30 jours"], [90, "90 jours"], [180, "6 mois"], [365, "1 an"], [730, "2 ans"], [0, "illimitée"]];
let offset = 0;
const FOLDED = 10;                   // lignes visibles quand le journal est replié
let total = 0;
let expanded = remembered();
let timer = null;
let generation = 0;                  // un filtre changé pendant un chargement : seule la dernière réponse compte

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

const when = (ts) => new Date(ts * 1000).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "medium" });

function params() {
  const p = new URLSearchParams();
  if ($("au-category").value) p.set("action", $("au-category").value);
  if ($("au-result").value) p.set("result", $("au-result").value);
  const q = $("au-search").value.trim();
  if (q) p.set("q", q);
  return p;
}

function message(text) {
  const b = $("au-msg");
  b.hidden = !text;
  b.className = `alert${text ? " show" : ""}`;
  b.textContent = text || "";
}

function cell(label, content, cls) {
  const td = el("td", cls);
  td.dataset.label = label;
  if (content instanceof Node) td.append(content);
  else td.textContent = content ?? "—";
  return td;
}

function row(e) {
  const tr = el("tr");
  const t = el("time", "", when(e.at));
  t.dateTime = new Date(e.at * 1000).toISOString();
  const action = el("span");
  action.append(el("span", "audit-label", e.label));
  if (e.detail) {
    const d = Object.entries(e.detail).filter(([, v]) => v !== null && v !== false && v !== "")
      .map(([k, v]) => `${k} : ${Array.isArray(v) ? v.length : typeof v === "object" ? JSON.stringify(v) : v}`);
    if (d.length) action.append(el("span", "audit-detail", d.join(" · ")));
  }
  if (e.capture) {                   // capture d'écran chiffrée, prise après l'action (21.4)
    const b = el("button", "btn btn-ghost btn-inline audit-capture", "Voir la capture");
    b.type = "button";
    b.addEventListener("click", () => showCapture(e, b));
    action.append(b);
  }
  const ok = e.status < 400;
  const result = el("span", `status ${ok ? "active" : "revoked"}`, ok ? "réussie" : `refusée (${e.status})`);
  tr.append(cell("Date", t), cell("Acteur", e.actor), cell("Action", action),
            cell("Cible", e.target ? el("span", "audit-target", e.target) : null),
            cell("Adresse IP", e.ip), cell("Résultat", result));
  return tr;
}

async function load(append = false) {
  if (!append) offset = 0;
  const mine = ++generation;
  const p = params();
  p.set("limit", PAGE);
  p.set("offset", offset);
  const r = await api(`/v1/admin/audit?${p}`);
  if (mine !== generation) return;
  if (!r.ok) return message(errorMessage(r));
  message("");
  const d = r.data;
  $("audit-section").hidden = false;
  if (!append) {
    fillFilters(d);
    $("au-body").replaceChildren();
  }
  for (const e of d.data) $("au-body").append(row(e));
  offset += d.data.length;
  $("au-empty").hidden = d.total > 0;
  total = d.total;
  renderFold();
  $("au-count").textContent = d.total ? `${d.total} entrée${d.total > 1 ? "s" : ""}` : "";
  $("au-retention").value = String(d.retention_days || 0);
}

// ---------- replié par défaut : les FOLDED dernières actions, « Afficher tout » pour le reste ----------
function remembered() {
  try { return localStorage.getItem("gw.audit.expanded") === "1"; } catch { return false; }
}

function renderFold() {
  const rows = $("au-body").children.length;
  $("au-body").closest("table").classList.toggle("folded", !expanded);
  $("au-toggle").hidden = rows <= FOLDED && offset >= total;
  $("au-toggle").textContent = expanded ? "Afficher moins" : `Afficher tout (${total})`;
  $("au-toggle").setAttribute("aria-expanded", String(expanded));
  $("au-more").hidden = !expanded || offset >= total;        // plus anciennes : seulement en vue complète
}

function toggleFold() {
  expanded = !expanded;
  try { localStorage.setItem("gw.audit.expanded", expanded ? "1" : "0"); } catch { /* navigation privée */ }
  renderFold();
  if (!expanded) $("audit-section").scrollIntoView({ block: "start" });   // replié : retour en haut du journal
}

function fillFilters(d) {
  const sel = $("au-category");
  if (sel.options.length > 1) return;
  for (const c of d.categories) sel.append(new Option(CATEGORY_LABELS[c] || c, c));
}

let captureUrl = null;
async function showCapture(e, button) {
  button.disabled = true;
  try {
    const res = await fetch(`/v1/admin/audit/${e.id}/capture`, { headers: { Authorization: `Bearer ${getKey()}` } });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error?.message || `HTTP ${res.status}`);
    if (captureUrl) URL.revokeObjectURL(captureUrl);
    captureUrl = URL.createObjectURL(await res.blob());
    $("cd-img").src = captureUrl;
    $("cd-info").textContent = `${when(e.at)} · ${e.target || ""} · ${e.detail?.action || ""}`;
    $("capture-dialog").showModal();
  } catch (err) {
    message(`Capture indisponible : ${err.message}`);
  } finally {
    button.disabled = false;
  }
}

async function exportCsv() {
  const b = $("au-export");
  b.disabled = true;
  try {
    const res = await fetch(`/v1/admin/audit/export?${params()}`, { headers: { Authorization: `Bearer ${getKey()}` } });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error?.message || `HTTP ${res.status}`);
    const name = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") || "")?.[1] || "journal-audit.csv";
    await saveBlob(await res.blob(), name);
  } catch (e) {
    message(`Export impossible : ${e.message}`);
  } finally {
    b.disabled = false;
  }
}

async function setRetention() {
  const days = Number($("au-retention").value);
  const r = await api("/v1/admin/audit/settings", { method: "PUT", body: { retention_days: days } });
  if (!r.ok) return message(errorMessage(r));
  load();
}

export function initAudit() {
  for (const [v, label] of RETENTION) $("au-retention").append(new Option(label, String(v)));
  $("au-category").addEventListener("change", () => load());
  $("au-result").addEventListener("change", () => load());
  $("au-search").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => load(), 300); });
  $("au-more").addEventListener("click", () => load(true));
  $("au-toggle").addEventListener("click", toggleFold);
  $("au-export").addEventListener("click", exportCsv);
  $("au-retention").addEventListener("change", setRetention);
  $("capture-dialog").querySelector("[data-close]").addEventListener("click", () => $("capture-dialog").close());
  load();
}
