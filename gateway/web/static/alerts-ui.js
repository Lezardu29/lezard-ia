// Exports, alertes budgétaires et notifications du tableau de bord (6.5).
import { api, errorMessage, getKey } from "./auth.js";
import { fmtEur } from "./metrics.js";
import { saveBlob } from "./save-file.js";

const $ = (id) => document.getElementById(id);
const PERIODS = { day: "par jour", week: "par semaine", month: "par mois" };
let keyNames = new Map();

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
const money = (v, cur) => (cur === "EUR" ? fmtEur(v) : `${v.toFixed(2).replace(".", ",")} $`);

// ---------- exports (fetch authentifié -> fichier) ----------
export function initExports(getPeriod) {
  document.querySelectorAll("[data-export]").forEach((b) => b.addEventListener("click", async () => {
    b.closest("details").open = false;
    const params = new URLSearchParams({ format: b.dataset.export, tz_offset: new Date().getTimezoneOffset() });
    const period = getPeriod();
    if (period) params.set("since", Date.now() / 1000 - period);
    const label = b.textContent;
    b.textContent = "Préparation…";
    try {
      const res = await fetch(`/v1/metrics/export?${params}`, { headers: { Authorization: `Bearer ${getKey()}` } });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error?.message || `HTTP ${res.status}`);
      const name = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") || "")?.[1] || `export.${b.dataset.export}`;
      await saveBlob(await res.blob(), name);
    } catch (e) {
      alert(`Export impossible : ${e.message}`);
    } finally {
      b.textContent = label;
    }
  }));
}

// ---------- notifications ----------
async function loadNotifications() {
  const r = await api("/v1/notifications?limit=30");
  if (!r.ok) return;
  const badge = $("notif-badge");
  badge.hidden = !r.data.unread;
  badge.textContent = r.data.unread > 9 ? "9+" : String(r.data.unread);
  $("notif-btn").setAttribute("aria-label", `Notifications (${r.data.unread} non lue${r.data.unread > 1 ? "s" : ""})`);
  const list = $("notif-list");
  list.replaceChildren();
  if (!r.data.data.length) list.append(el("li", "empty", "Aucune notification."));
  for (const n of r.data.data) {
    const li = el("li", `notif ${n.level}${n.read ? "" : " unread"}`);
    const t = el("time", "", new Date(n.ts * 1000).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }));
    li.append(el("strong", "", n.title), el("span", "", n.message), t);
    list.append(li);
  }
}

export function initNotifications() {
  const panel = $("notif-panel"), btn = $("notif-btn");
  btn.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    btn.setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) loadNotifications();
  });
  $("notif-read").addEventListener("click", async () => {
    await api("/v1/notifications/read", { method: "POST", body: {} });
    loadNotifications();
  });
  document.addEventListener("mousedown", (e) => {
    if (!panel.hidden && !panel.contains(e.target) && !btn.contains(e.target)) {
      panel.hidden = true;
      btn.setAttribute("aria-expanded", "false");
    }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) { panel.hidden = true; btn.focus(); } });
  return loadNotifications;
}

// ---------- règles d'alerte ----------
function ruleCard(rule) {
  const card = el("article", `alert-rule${rule.enabled ? "" : " off"}`);
  const head = el("div", "alert-rule-head");
  const title = el("div");
  title.append(el("strong", "", rule.name),
    el("span", "muted", ` · ${rule.key_id ? `clé « ${keyNames.get(rule.key_id) || rule.key_id} »` : "tout le compte"} · ${PERIODS[rule.period]}`));
  const actions = el("div", "alert-actions");
  const toggle = el("button", "chip", rule.enabled ? "Active" : "En pause");
  toggle.type = "button";
  toggle.setAttribute("aria-pressed", String(rule.enabled));
  toggle.addEventListener("click", async () => {
    await api(`/v1/alerts/${rule.id}`, { method: "PATCH", body: { enabled: !rule.enabled } });
    loadAlerts();
  });
  actions.append(toggle);
  if (rule.webhook_url) {
    const test = el("button", "btn btn-ghost btn-inline", "Tester le webhook");
    test.type = "button";
    test.addEventListener("click", async () => {
      test.textContent = "Envoi…";
      const r = await api(`/v1/alerts/${rule.id}/test`, { method: "POST" });
      test.textContent = r.ok && r.data.delivered ? "Reçu ✓" : "Échec ✗";
      setTimeout(() => { test.textContent = "Tester le webhook"; }, 2500);
    });
    actions.append(test);
  }
  const del = el("button", "btn btn-ghost btn-inline danger-text", "Supprimer");
  del.type = "button";
  del.addEventListener("click", async () => {
    if (!confirm(`Supprimer l'alerte « ${rule.name} » ?`)) return;
    await api(`/v1/alerts/${rule.id}`, { method: "DELETE" });
    loadAlerts();
  });
  actions.append(del);
  head.append(title, actions);

  const pct = rule.current?.pct ?? 0;
  const gauge = el("div", `gauge big${pct >= 100 ? " crit" : pct >= 80 ? " warn" : ""}`);
  gauge.setAttribute("role", "meter");
  gauge.setAttribute("aria-label", `Budget consommé : ${pct} %`);
  gauge.setAttribute("aria-valuenow", String(pct));
  gauge.setAttribute("aria-valuemin", "0");
  gauge.setAttribute("aria-valuemax", "100");
  gauge.style.setProperty("--pct", `${Math.min(100, pct)}%`);
  gauge.append(el("span"));
  for (const t of rule.thresholds) {                          // repères des paliers
    if (t > 100) continue;
    const mark = el("i", "tick-mark");
    mark.style.left = `${t}%`;
    mark.title = `Palier ${t} %`;
    gauge.append(mark);
  }
  const foot = el("p", "gauge-text",
    `${money(rule.current?.spent ?? 0, rule.currency)} / ${money(rule.amount, rule.currency)} (${pct} %) · paliers ${rule.thresholds.join(", ")} %` +
    (rule.webhook_url ? ` · webhook ${new URL(rule.webhook_url).hostname}` : ""));
  card.append(head, gauge, foot);
  return card;
}

export async function loadAlerts() {
  const r = await api("/v1/alerts");
  if (!r.ok) return;
  const list = $("alerts-list");
  list.replaceChildren(...r.data.data.map(ruleCard));
  $("alerts-empty").hidden = r.data.data.length > 0;
}

export function setAlertKeys(keys) {
  keyNames = new Map(keys.map((k) => [k.id, k.name]));
  const sel = $("a-scope");
  sel.length = 1;
  for (const k of keys) sel.add(new Option(`Clé « ${k.name} »`, k.id));
}

export function initAlerts() {
  const dlg = $("alert-dialog");
  const showErr = (m) => { $("ad-error").textContent = m; $("ad-error").classList.toggle("show", !!m); };
  $("new-alert").addEventListener("click", () => { $("alert-form").reset(); showErr(""); dlg.showModal(); $("a-name").focus(); });
  dlg.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => dlg.close()));
  $("alert-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const thresholds = [...$("a-thresholds").querySelectorAll("input:checked")].map((c) => +c.value);
    const body = { name: $("a-name").value.trim(), key_id: $("a-scope").value || null, period: $("a-period").value,
                   amount: parseFloat($("a-amount").value), currency: $("a-currency").value, thresholds,
                   webhook_url: $("a-webhook").value.trim() || null };
    if (!body.name) return showErr("Le nom est requis.");
    if (!(body.amount > 0)) return showErr("Indiquez un budget > 0.");
    if (!thresholds.length) return showErr("Cochez au moins un palier.");
    $("ad-submit").disabled = true;
    const r = await api("/v1/alerts", { method: "POST", body });
    $("ad-submit").disabled = false;
    if (!r.ok) return showErr(errorMessage(r));
    dlg.close();
    if (r.data.webhook_secret) {
      prompt("Secret de signature du webhook (vérifiez l'en-tête X-Gateway-Signature) :", r.data.webhook_secret);
    }
    loadAlerts();
  });
}
