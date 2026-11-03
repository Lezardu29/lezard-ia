// Tableau de bord & administration (6.3) : KPI, économies, gestion des clés.
import { api, clearKey, errorMessage, getKey, unlockAdmin } from "./auth.js";
import { fmtEur, fmtInt, fmtMs } from "./metrics.js";
import { initThemePanel, syncTheme } from "./theme-panel.js";
import { initRemotePanel } from "./remote-panel.js";
import { initPhonePanel } from "./phone-panel.js";
import { initKeyDialog, openKeyDialog, setProjects, showSecret } from "./key-dialog.js";
import { initCharts, renderCharts, setChartsLoading } from "./charts.js";
import { initAlerts, initExports, initNotifications, loadAlerts, setAlertKeys } from "./alerts-ui.js";
import { initBridges, loadBridges } from "./bridges-ui.js";
import { initBackups } from "./backups-ui.js";
import { initTotp } from "./totp-ui.js";
import { initAudit } from "./audit-ui.js";
import { initUpdates } from "./update-ui.js";
import { initGpu } from "./gpu-ui.js";
import { initTools } from "./tools-ui.js";
import { initEdition } from "./edition-ui.js";
import { initWelcome } from "./welcome-ui.js";
import { initAccounts } from "./accounts-ui.js";
import { initSupervision } from "./supervision-ui.js";
import { initRowMenus } from "./row-menu.js";
import { initHardwareWidget } from "./hardware-widget.js";
import { initMobile } from "./mobile.js";
import { initPwa } from "./pwa.js";
import { initFolds } from "./fold.js";
let loadNotifications = async () => {};

const $ = (id) => document.getElementById(id);
const STATUS_LABELS = { active: "Active", suspended: "Suspendue", revoked: "Révoquée", expired: "Expirée" };
const PERIOD_LABELS = { day: "jour", week: "semaine", month: "mois" };
let me = null, period = 604800, keysById = new Map();

function logout() { clearKey(); location.replace("/"); }

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function ago(ts) {
  if (!ts) return "jamais";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "à l'instant";
  if (s < 3600) return `il y a ${Math.floor(s / 60)} min`;
  if (s < 86400) return `il y a ${Math.floor(s / 3600)} h`;
  return new Date(ts * 1000).toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
}

// jauge colorée : accent < 70 %, ambre < 90 %, rouge au-delà
function gauge(used, limit, label) {
  const wrap = el("div", "gauge-cell");
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  const g = el("div", `gauge${pct >= 90 ? " crit" : pct >= 70 ? " warn" : ""}`);
  g.setAttribute("role", "meter");
  g.setAttribute("aria-valuemin", "0");
  g.setAttribute("aria-valuemax", String(limit));
  g.setAttribute("aria-valuenow", String(Math.round(used * 100) / 100));
  g.setAttribute("aria-label", label);
  g.style.setProperty("--pct", `${pct}%`);
  g.append(el("span"));
  wrap.append(g);
  return wrap;
}

// ---------- graphiques (6.4) ----------
async function loadCharts() {
  setChartsLoading();                                     // garde le rendu précédent, atténué
  const params = new URLSearchParams({ tz_offset: new Date().getTimezoneOffset() });
  if (period) params.set("since", Date.now() / 1000 - period);
  const r = await api(`/v1/metrics/charts?${params}`);
  if (r.ok) renderCharts(r.data);
}

// ---------- KPI ----------
async function loadSummary() {
  const since = period ? Date.now() / 1000 - period : undefined;
  const r = await api(`/v1/metrics/summary${since ? `?since=${since}` : ""}`);
  if (!r.ok) return;
  const s = r.data;
  const set = (k, v, sub) => {
    document.querySelector(`[data-kpi="${k}"]`).textContent = v;
    document.querySelector(`[data-sub="${k}"]`).textContent = sub;
  };
  set("spend", fmtEur(s.spend_eur), `${fmtInt(s.requests)} requêtes · coût providers ${fmtEur(s.provider_cost_eur)}`);
  set("tokens", fmtInt(s.tokens.total), `Entrée ${fmtInt(s.tokens.prompt)} · sortie ${fmtInt(s.tokens.completion)}`);
  const sv = s.savings;
  const kv = document.querySelector('[data-kpi="savings"]');
  set("savings", sv ? fmtEur(sv.saved_eur) : "–",
    sv ? `${String(sv.saved_pct).replace(".", ",")} % vs tout sur ${sv.baseline_model}` : "Référence sans prix connu");
  kv.classList.toggle("neg", !!sv && sv.saved_eur < 0);
  set("latency", fmtMs(s.avg_latency_ms), `1er token ${fmtMs(s.avg_ttft_ms)} · erreurs ${fmtInt(s.errors)} ` +
    `(${(s.error_rate * 100).toFixed(1).replace(".", ",")} %)`);
  renderSavings(sv);
}

function renderSavings(sv) {
  const box = $("savings-bars");
  box.replaceChildren();
  if (!sv) { box.append(el("p", "empty", "Économies indisponibles.")); return; }
  $("sv-base").textContent = `Référence : tout sur ${sv.baseline_model}`;
  const rows = [["Modèles locaux", sv.breakdown_eur.local_models], ["Cloud gratuit", sv.breakdown_eur.free_cloud ?? 0],
                ["Routage intelligent", sv.breakdown_eur.smart_routing],
                ["Prompt caching", sv.breakdown_eur.prompt_caching], ["Cache de réponses", sv.breakdown_eur.response_cache]];
  const max = Math.max(...rows.map(([, v]) => Math.abs(v)), 1e-9);
  for (const [label, v] of rows) {
    const row = el("div", "bar-row");
    const track = el("div", "bar-track");
    const fill = el("div", `bar-fill${v < 0 ? " neg" : ""}`);
    fill.style.width = `${(Math.abs(v) / max) * 100}%`;
    track.append(fill);
    row.append(el("span", "", label), track, el("span", "amount", fmtEur(v)));
    row.title = v < 0 ? "Négatif : des requêtes ont été routées vers plus cher que la référence" : "";
    box.append(row);
  }
}

// ---------- clés ----------
function keyStatus(k) {
  if (k.status === "active" && k.expires_at && k.expires_at <= Date.now() / 1000) return "expired";
  return k.status;
}

function budgetCell(k) {
  const budgets = k.usage?.budgets || [];
  if (!budgets.length) {
    const m = k.usage?.spend?.month;
    return el("span", "gauge-text", m ? `${fmtEur(m.spent_eur)} ce mois · illimité` : "illimité");
  }
  // la jauge la plus remplie est la plus parlante
  const b = budgets.reduce((a, x) => (x.spent / x.amount > a.spent / a.amount ? x : a));
  const cell = gauge(b.spent, b.amount, `Budget ${PERIOD_LABELS[b.period]}`);
  const sym = b.currency === "EUR" ? "€" : "$";
  cell.append(el("span", "gauge-text", `${b.spent.toFixed(2).replace(".", ",")} / ${b.amount} ${sym} par ${PERIOD_LABELS[b.period]}`));
  return cell;
}

function tokensCell(k) {
  const u = k.usage || {};
  for (const [name, label] of [["tpd", "jour"], ["tpm", "min"], ["rpm", "min"]]) {
    if (u[name]?.limit) {
      const unit = name === "rpm" ? "req." : "tokens";
      const cell = gauge(u[name].used, u[name].limit, `${unit} par ${label}`);
      cell.append(el("span", "gauge-text", `${fmtInt(u[name].used)} / ${fmtInt(u[name].limit)} ${unit}/${label}`));
      return cell;
    }
  }
  return el("span", "gauge-text", "illimité");
}

function actionsCell(k) {
  const menu = el("details", "row-menu");
  const sum = el("summary", "", "⋯");
  sum.setAttribute("aria-label", `Actions pour ${k.name}`);
  const box = el("div", "menu");
  const add = (label, fn, cls = "") => {
    const b = el("button", cls, label);
    b.type = "button";
    b.addEventListener("click", () => { menu.open = false; fn(); });
    box.append(b);
  };
  const st = keyStatus(k);
  if (st !== "revoked") add("Modifier", () => openKeyDialog(k));
  if (st === "active" || st === "expired") add("Suspendre", () => action(k, "suspend", "Motif de la suspension (optionnel) :"));
  if (st === "suspended") add("Réactiver", () => action(k, "resume"));
  if (st !== "revoked") add("Nouveau secret (rotation)", () => rotate(k));
  add("Journal", () => showEvents(k));
  if (st !== "revoked") add("Révoquer définitivement", () => action(k, "revoke", "Révocation DÉFINITIVE. Motif :"), "danger");
  add("Supprimer", () => remove(k), "danger");
  menu.append(sum, box);                              // placement et fermeture : row-menu.js (13.7)
  return menu;
}

function renderKeys(keys) {
  const body = $("keys-body");
  body.replaceChildren();
  keysById = new Map(keys.map((k) => [k.id, k]));
  $("keys-empty").hidden = keys.length > 0;
  for (const k of keys) {
    const tr = el("tr");
    const name = el("td");
    name.append(el("span", "key-name", k.name), el("span", "key-prefix", k.key_prefix));
    if (k.description) name.title = k.description;
    const st = keyStatus(k);
    const status = el("td");
    const badge = el("span", `status ${st}`, STATUS_LABELS[st] || st);
    if (k.status_reason) badge.title = k.status_reason;
    status.append(badge);
    const cells = [name, status];
    for (const content of [budgetCell(k), tokensCell(k)]) { const td = el("td"); td.append(content); cells.push(td); }
    cells.push(el("td", "muted", ago(k.last_used_at)));
    const act = el("td");
    act.append(actionsCell(k));
    cells.push(act);
    // libellés des colonnes, affichés quand le tableau devient une liste de cartes sur téléphone (13.5)
    ["", "Statut", "Plan", "Budget", "Tokens", "Dernière utilisation", ""].forEach((label, i) => {
      if (label) cells[i].dataset.label = label;
    });
    tr.append(...cells);
    body.append(tr);
  }
}

async function loadKeys() {
  const r = await api("/v1/keys?with_usage=true");
  if (r.ok) {
    renderKeys(r.data.data);
    setAlertKeys(r.data.data.filter((k) => k.status !== "revoked"));
  }
}

async function action(k, verb, prompt) {
  let reason;
  if (prompt) {
    reason = window.prompt(prompt, "");
    if (reason === null) return;                       // annulé
  }
  const r = await api(`/v1/keys/${encodeURIComponent(k.id)}/${verb}`, { method: "POST", body: { reason: reason || null } });
  if (!r.ok) alert(errorMessage(r));
  loadKeys();
}

async function rotate(k) {
  if (!confirm(`Générer un nouveau secret pour « ${k.name} » ? L'ancien cessera immédiatement de fonctionner.`)) return;
  const r = await api(`/v1/keys/${encodeURIComponent(k.id)}/rotate`, { method: "POST" });
  if (!r.ok) return alert(errorMessage(r));
  showSecret(r.data.key);                       // nouveau secret : bloc de partage à jour (12.6)
  loadKeys();
}

async function remove(k) {
  if (!confirm(`Supprimer « ${k.name} » ? Les applications qui l'utilisent perdront l'accès.`)) return;
  const r = await api(`/v1/keys/${encodeURIComponent(k.id)}`, { method: "DELETE" });
  if (!r.ok) alert(errorMessage(r));
  loadKeys();
}

async function showEvents(k) {
  const r = await api(`/v1/keys/${encodeURIComponent(k.id)}/events`);
  const list = $("ed-list");
  list.replaceChildren();
  $("ed-title").textContent = `Journal de « ${k.name} »`;
  for (const ev of r.ok ? r.data.data : []) {
    const li = el("li");
    const t = el("time", "", new Date(ev.at * 1000).toLocaleString("fr-FR"));
    t.dateTime = new Date(ev.at * 1000).toISOString();
    li.append(t, el("span", "", `${ev.action}${ev.reason ? ` — ${ev.reason}` : ""}`));
    list.append(li);
  }
  $("events-dialog").showModal();
}

// ---------- initialisation ----------
async function refresh() {
  await Promise.all([loadSummary(), loadCharts(), loadNotifications(),
                     me.can_manage_keys ? loadKeys() : null, me.can_manage_keys ? loadAlerts() : null,
                     me.can_manage_keys ? loadBridges() : null]);
}

async function init() {
  if (!getKey()) return logout();
  const r = await api("/v1/me");
  if (!r.ok) return logout();
  me = r.data;
  if (me.admin_session?.blocked) {                    // depuis Internet sans double authentification
    $("remote-blocked").textContent = "Administration depuis Internet désactivée : activez d'abord la double authentification, depuis ce PC ou votre réseau local. Le chat reste utilisable d'ici.";
    $("remote-blocked").hidden = false;
    me.is_admin = false;                               // panneaux d'administration non affichés
  }
  // à distance avec la double authentification (20.1) : code demandé d'emblée, avant les panneaux d'administration
  if (me.is_admin && me.admin_session?.required && !me.admin_session.active) await unlockAdmin();
  $("plan").textContent = me.edition === "gratuit" ? "Open source" : "Accès web";  // offre (29.4)
  $("who").textContent = me.key?.name || (me.type === "account" ? "Compte" : "");
  $("nav-models").hidden = !me.is_admin;               // page Modèles : clé maître
  if (me.version) {                                    // version : clé maître seulement (15.4)
    $("app-version").textContent = `v${me.version}`;
    $("app-version").hidden = false;
  }
  if (me.is_admin) initHardwareWidget($("hw-widget")); // matériel du PC : clé maître seulement (14.3)
  if (me.is_admin) initBackups();                     // sauvegardes de la base : clé maître (18.2)
  if (me.is_admin) initTotp();                        // double authentification de l'administration (20.1)
  if (me.is_admin) initAudit();                       // journal d'audit (20.2)
  if (me.is_admin) initUpdates(me);                   // version installée, nouveautés (18.3)
  if (me.is_admin) initSupervision();                 // /metrics et alertes d'exploitation (18.5)
  if (me.is_admin) initAccounts();                    // comptes nommés (20.3)
  if (me.is_admin) initGpu();                         // PC occupé, files d'attente, keep_alive (18.4)
  if (me.is_admin) initEdition();                     // « Accès par le site » : licence, compte web (29.4)
  if (me.is_admin) initWelcome(me);                   // premier lancement guidé (32.4)
  initThemePanel($("theme-btn"));
  initPhonePanel($("phone-btn"));                      // mêmes icônes que la page Chat
  if (me.is_admin) initRemotePanel($("remote-btn"));
  syncTheme();
  initExports(() => period);
  loadNotifications = initNotifications();
  initTools({ ownKey: !me.can_manage_keys });         // « brancher un outil » (16.4) : toutes les sessions
  if (me.can_manage_keys) {
    initBridges();                                     // ponts vers les modèles locaux (7.4)
    $("alerts-section").hidden = false;
    initAlerts();
    $("keys-section").hidden = false;
    initKeyDialog({ saved: loadKeys });
    $("new-key").addEventListener("click", () => openKeyDialog(null));
    const p = await api("/v1/projects");
    if (p.ok) setProjects(p.data.data);
  }
  $("period").addEventListener("click", (e) => {
    const b = e.target.closest("[data-period]");
    if (!b) return;
    period = +b.dataset.period;
    $("period").querySelectorAll("[data-period]").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
    loadSummary();                                      // le filtre de période s'applique à tout
    loadCharts();
  });
  initCharts();
  await refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 30000);   // rafraîchissement discret
}

document.addEventListener("DOMContentLoaded", () => {
  $("logout").addEventListener("click", logout);
  initMobile();                                        // navigation en tiroir sur téléphone (13.5)
  initPwa($("install-btn"));                           // installation en application (13.6)
  initRowMenus();
  initFolds();                                         // panneaux en aperçu, « Afficher plus »
  init();
});
