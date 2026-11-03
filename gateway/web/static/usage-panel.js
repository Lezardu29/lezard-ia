// Panneau « Utilisation » (appui sur le cercle de contexte du chat) : fenêtre de contexte occupée, plafonds de la clé
// (jetons par minute et par jour, budgets du jour, de la semaine, du mois) avec leur remise à zéro, et une prévision
// « à ce rythme… ». En bas : « Compacter la conversation » (/compacte).
// Les chiffres viennent de /v1/me (relu à chaque ouverture et par le bouton ↻) ; rien n'est stocké ici.
import { api } from "./auth.js";

let dialog = null, onCompact = () => {}, readContext = () => null;

const pctOf = (used, limit) => (limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0);
const tokens = (n) => (n >= 1e6 ? `${(n / 1e6).toLocaleString("fr-FR", { maximumFractionDigits: 1 })} M`
  : n >= 1e3 ? `${(n / 1e3).toLocaleString("fr-FR", { maximumFractionDigits: 1 })} k` : String(Math.round(n)));
const money = (v, cur) => `${v.toLocaleString("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${cur === "EUR" ? "€" : "$"}`;
const PERIOD = { day: ["Limites quotidiennes", "Budget du jour"], week: ["Limites hebdomadaires", "Budget de la semaine"],
                 month: ["Limites mensuelles", "Budget du mois"] };
const DAY_PART = (h) => (h < 12 ? "matin" : h < 18 ? "après-midi" : "soir");

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

/** Dans combien de temps (« dans 2 h 4 min »), ou le jour et l'heure (« ven. 06:00 ») au-delà d'un jour. */
function resetText(at) {
  const ms = at * 1000 - Date.now();
  if (ms <= 0) return "Réinitialisation imminente";
  if (ms < 864e5) {
    const h = Math.floor(ms / 36e5), m = Math.floor((ms % 36e5) / 6e4);
    return `Réinitialisation dans ${h ? `${h} h ` : ""}${m} min`;
  }
  const d = new Date(at * 1000);
  return `Réinitialisation : ${d.toLocaleDateString("fr-FR", { weekday: "short" })} ${d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`;
}

function meter(title, pct, foot, { level = null, extra = null } = {}) {
  const card = el("div", "up-card");
  const head = el("div", "up-head");
  head.append(el("span", "up-title", title), el("span", "up-pct", `${pct} % utilisé`));
  const bar = el("div", "up-bar");
  const fill = el("span");
  fill.style.width = `${pct}%`;
  bar.append(fill);
  card.dataset.level = level || (pct >= 90 ? "full" : pct >= 70 ? "warn" : "ok");
  card.append(head, bar);
  if (foot) card.append(el("p", "up-foot", foot));
  if (extra) card.append(extra);
  return card;
}

/** Budget : à ce rythme (dépense depuis le début de la période), épuisé avant la remise à zéro ? */
function forecast(b) {
  const span = { day: 864e5, week: 7 * 864e5, month: 30 * 864e5 }[b.period];
  const end = b.resets_at * 1000, start = end - span, now = Date.now();
  const elapsed = now - start;
  if (b.spent <= 0 || elapsed < 36e5 || b.remaining <= 0) return null;   // moins d'une heure : pas de rythme fiable
  const rate = b.spent / elapsed;
  const out = now + b.remaining / rate;
  if (out >= end) return null;
  const when = new Date(out), reset = new Date(end);
  const day = (d) => d.toLocaleDateString("fr-FR", { weekday: "long" });
  return b.period === "day"
    ? `Attention. À ce rythme, vous épuiserez le budget du jour vers ${when.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}.`
    : `Attention. À ce rythme, vous épuiserez votre limite ${day(when)} ${DAY_PART(when.getHours())}, avant la réinitialisation de ${day(reset)}.`;
}

function section(title) {
  return el("h3", "up-section", title);
}

async function fill() {
  const body = dialog.querySelector(".up-body");
  body.replaceChildren(el("p", "up-muted", "Lecture…"));
  const me = await api("/v1/me");
  const out = [];

  // contexte de la conversation
  const ctx = readContext();
  out.push(section("Contexte"));
  if (ctx && ctx.tokens) {
    const known = !!ctx.size;
    const extra = el("p", "up-foot", (ctx.model ? `Modèle : ${ctx.model}. ` : "")
      + (known ? `Compactage automatique à ${Math.round(ctx.auto * 100)} %.` : "Fenêtre du modèle inconnue : pas de compactage automatique."));
    out.push(meter("Fenêtre de contexte", known ? pctOf(ctx.tokens, ctx.size) : 0,
      known ? `${tokens(ctx.tokens)} sur ${tokens(ctx.size)} jetons` : `environ ${tokens(ctx.tokens)} jetons`, { extra }));
  } else {
    out.push(el("p", "up-muted", "Conversation vide : rien n'occupe encore la fenêtre de contexte."));
  }
  const afterContext = out.length;                         // les avertissements « à ce rythme » vont ici

  if (!me.ok) {
    out.push(el("p", "up-muted", "Utilisation du compte indisponible pour le moment."));
    body.replaceChildren(...out);
    return;
  }
  const u = me.data.usage, limits = me.data.limits || {};
  const warnings = [];
  const plan = [];
  if (u) {
    if (limits.tpd) plan.push(meter("Jetons du jour", pctOf(u.tpd.used, limits.tpd),
      `${tokens(u.tpd.used)} sur ${tokens(limits.tpd)} · ${resetText(u.spend.day.resets_at)}`));
    if (limits.tpm) plan.push(meter("Jetons par minute", pctOf(u.tpm.used, limits.tpm), `${tokens(u.tpm.used)} sur ${tokens(limits.tpm)}`));
    if (limits.rpm) plan.push(meter("Requêtes par minute", pctOf(u.rpm.used, limits.rpm), `${u.rpm.used} sur ${limits.rpm}`));
  }
  if (plan.length) out.push(section("Utilisation du forfait"), ...plan);

  const budgets = u?.budgets || [];
  for (const period of ["day", "week", "month"]) {
    const list = budgets.filter((b) => b.period === period);
    if (!list.length) continue;
    out.push(section(PERIOD[period][0]));
    for (const b of list) {
      const w = forecast(b);
      if (w) warnings.push(w);
      out.push(meter(PERIOD[period][1], pctOf(b.spent, b.amount),
        `${money(b.spent, b.currency)} sur ${money(b.amount, b.currency)} · ${resetText(b.resets_at)}`));
    }
  }
  out.splice(afterContext, 0, ...warnings.map((w) => el("p", "up-warning", w)));
  if (!plan.length && !budgets.length) {
    out.push(section("Utilisation du forfait"));
    out.push(el("div", "up-card up-empty", me.data.is_admin
      ? "Clé maître : aucune limite. Les plafonds se règlent par clé, dans Administration → Clés."
      : "Aucune limite sur cette clé."));
  }
  body.replaceChildren(...out);
}

function build() {
  dialog = el("dialog", "usage-panel");
  dialog.setAttribute("aria-labelledby", "up-title");
  dialog.innerHTML = `
    <div class="up-grab" aria-hidden="true"></div>
    <div class="up-top">
      <button type="button" class="up-icon" data-close aria-label="Fermer">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
      <h2 id="up-title">Utilisation</h2>
      <button type="button" class="up-icon" data-refresh aria-label="Actualiser">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5"/></svg></button>
    </div>
    <div class="up-body"></div>
    <div class="up-actions">
      <button type="button" class="btn btn-primary up-compact">Compacter la conversation</button>
      <p class="up-hint">L'IA repart d'un résumé de la conversation (comme /compacte) : la fenêtre de contexte se libère.</p>
    </div>`;
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  dialog.querySelector("[data-refresh]").addEventListener("click", fill);
  dialog.querySelector(".up-compact").addEventListener("click", () => { dialog.close(); onCompact(); });
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });   // appui hors du panneau
  document.body.append(dialog);
}

/** compact : lance /compacte ; context() : { tokens, size, model, auto } de la conversation affichée. */
export function initUsagePanel(compact, context) {
  onCompact = compact;
  readContext = context;
}

export function openUsagePanel() {
  if (!dialog) build();
  const ctx = readContext();
  dialog.querySelector(".up-compact").disabled = !(ctx && ctx.tokens);
  if (!dialog.open) dialog.showModal();
  fill();
}
