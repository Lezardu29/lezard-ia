// Fenêtre de création / édition sur-mesure d'une clé virtuelle (6.3).
// Chaque bloc de permissions se coche/décoche ; un bloc décoché n'est pas envoyé (aucune
// restriction). En édition, permissions et limites envoyées REMPLACENT celles de la clé.
import { api, errorMessage } from "./auth.js";
import { mountGuides } from "./integrations.js";

const $ = (id) => document.getElementById(id);
const list = (s) => s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
let editing = null, onSaved = () => {};

// ---------- blocs optionnels ----------
function opt(name) { return document.querySelector(`.opt[data-opt="${name}"]`); }
function setOpt(name, on) {
  const f = opt(name);
  f.querySelector("[data-toggle]").checked = on;
  f.classList.toggle("on", on);
}
const isOn = (name) => opt(name).classList.contains("on");

function budgetRows() { return [...document.querySelectorAll(".budget-row")]; }

// ---------- lecture du formulaire ----------
function payload() {
  const body = { name: $("k-name").value.trim(), description: $("k-desc").value.trim() || null,
                 project: $("k-project").value || null };
  const perms = {};
  if (isOn("models") || blockedProviders().length) {
    const allow = list($("p-allow").value), deny = [...new Set([...list($("p-deny").value), ...blockedProviders()])];
    if (allow.length || deny.length) perms.models = { ...(allow.length && { allow }), ...(deny.length && { deny }) };
  }
  if (isOn("hosting")) perms.hosting = document.querySelector("[name=p-hosting]:checked").value;
  if (isOn("network")) {
    const ips = list($("p-ips").value), origins = list($("p-origins").value);
    if (ips.length) perms.ip_allow = ips;
    if (origins.length) perms.origins = origins;
  }
  if (isOn("modes")) perms.modes = [...document.querySelectorAll("[name=p-mode]:checked")].map((c) => c.value);
  if (isOn("effort")) perms.max_effort = $("p-effort").value;
  if (isOn("orchestration")) perms.orchestration = false;
  if (isOn("embeddings")) perms.embeddings = false;     // /v1/embeddings (16.1)
  if (isOn("busy")) perms.when_busy = document.querySelector("[name=p-busy]:checked").value;   // PC occupé (18.4)
  if (isOn("tools")) perms.tools = document.querySelector("[name=p-tools]:checked").value;      // outils du chat
  body.permissions = perms;

  const limits = {};
  for (const k of ["rpm", "tpm", "tpd"]) {
    const v = parseInt($(`l-${k}`).value, 10);
    if (v > 0) limits[k] = v;
  }
  const budgets = budgetRows().filter((r) => r.querySelector("[data-on]").checked).map((r) => ({
    period: r.dataset.period, amount: parseFloat(r.querySelector("[data-amount]").value),
    currency: r.querySelector("[data-cur]").value }));
  if (budgets.length) limits.budget = budgets;
  body.limits = limits;

  const meta = {};
  for (const line of $("k-meta").value.split("\n")) {
    const i = line.indexOf("=");
    if (i > 0) meta[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  body.metadata = meta;

  const exp = $("k-exp").value;
  if (exp === "none") body.expires_at = null;
  else if (exp === "date") body.expires_at = $("k-exp-date").value ? new Date($("k-exp-date").value).toISOString() : null;
  else if (exp !== "keep") body.expires_in = exp;
  return body;
}

function check(body) {
  if (!body.name) return "Le nom est requis.";
  if (isOn("modes") && !body.permissions.modes.length) return "Cochez au moins un mode de routage.";
  if ((body.limits.budget || []).some((b) => !(b.amount > 0))) return "Chaque budget coché doit avoir un montant > 0.";
  if ($("k-exp").value === "date" && !$("k-exp-date").value) return "Choisissez une date d'expiration.";
  return null;
}

// ---------- pré-remplissage ----------
function fill(key) {
  $("k-name").value = key?.name || "";
  $("k-desc").value = key?.description || "";
  $("k-project").value = key?.project_id || "";
  $("k-exp").value = key ? "keep" : "none";
  $("k-exp").querySelector('[value="keep"]').hidden = !key;
  $("k-exp-date-wrap").hidden = true;
  const p = key?.permissions || {};
  setOpt("models", !!p.models);
  $("p-allow").value = (p.models?.allow || []).join(", ");
  // « fournisseur/* » (et rien d'autre) = fournisseur entier : il a sa case, le reste reste dans la zone de texte
  const isWhole = (d) => d.endsWith("/*") && !d.slice(0, -2).includes("/");
  const deny = p.models?.deny || [];
  pendingBlocked = new Set(deny.filter(isWhole).map((d) => d.slice(0, -2)));
  $("p-deny").value = deny.filter((d) => !isWhole(d)).join(", ");
  renderProviderChecks();
  setOpt("hosting", !!p.hosting);
  document.querySelectorAll("[name=p-hosting]").forEach((r) => { r.checked = r.value === (p.hosting || "local"); });
  setOpt("network", !!(p.ip_allow || p.origins));
  $("p-ips").value = (p.ip_allow || []).join(", ");
  $("p-origins").value = (p.origins || []).join(", ");
  setOpt("modes", !!p.modes);
  document.querySelectorAll("[name=p-mode]").forEach((c) => { c.checked = (p.modes || ["economy"]).includes(c.value); });
  setOpt("effort", !!p.max_effort);
  $("p-effort").value = p.max_effort || "medium";
  setOpt("orchestration", p.orchestration === false);
  setOpt("embeddings", p.embeddings === false);
  setOpt("busy", !!p.when_busy && p.when_busy !== "free");
  setOpt("tools", !!p.tools);
  document.querySelectorAll("[name=p-tools]").forEach((r) => { r.checked = r.value === (p.tools || "web"); });
  document.querySelectorAll("[name=p-busy]").forEach((r) => { r.checked = r.value === (p.when_busy === "refuse" ? "refuse" : "cloud"); });
  const l = key?.limits || {};
  for (const k of ["rpm", "tpm", "tpd"]) $(`l-${k}`).value = l[k] || "";
  for (const row of budgetRows()) {
    const b = (l.budget || []).find((x) => x.period === row.dataset.period);
    row.querySelector("[data-on]").checked = !!b;
    row.classList.toggle("on", !!b);
    row.querySelector("[data-amount]").value = b ? b.amount : "";
    row.querySelector("[data-cur]").value = b ? b.currency : "EUR";
  }
  $("k-meta").value = Object.entries(key?.metadata || {}).map(([k, v]) => `${k}=${v}`).join("\n");
  $("kd-title").textContent = key ? `Modifier « ${key.name} »` : "Nouvelle clé";
  $("kd-submit").textContent = key ? "Enregistrer" : "Créer la clé";
  showError("");
}

function showError(msg) {
  $("kd-error").textContent = msg;
  $("kd-error").classList.toggle("show", !!msg);
}

async function submit(e) {
  e.preventDefault();
  const body = payload();
  const problem = check(body);
  if (problem) return showError(problem);
  $("kd-submit").disabled = true;
  const r = editing
    ? await api(`/v1/keys/${encodeURIComponent(editing.id)}`, { method: "PATCH", body })
    : await api("/v1/keys", { method: "POST", body });
  $("kd-submit").disabled = false;
  if (!r.ok) return showError(errorMessage(r));
  $("key-dialog").close();
  if (!editing) showSecret(r.data.key);
  onSaved();
}

// ---------- secret + partage en un clic (12.6) ----------
const URL_NOTES = {
  local: ["warn", "Cette adresse ne fonctionne que sur ce PC. Pour partager l'accès : activez l'accès distant " +
                  "(bouton « Accès distant ») ou lancez la gateway en mode réseau local (lancer.ps1 -Lan)."],
  lan: ["", "Fonctionne pour les appareils connectés au même réseau (Wi-Fi / box) que ce PC."],
  tailscale: ["", "Fonctionne pour les appareils connectés à votre réseau Tailscale."],
  internet: ["", "Fonctionne de partout, depuis n'importe quel appareil connecté à Internet."],
};
let shareSecret = null, shareUrls = [], shareTls = null, updateGuides = null;

function shareTexts(url, key) {
  const base = `${url}/v1`;
  return {
    openai: `Base URL : ${base}\nClé API  : ${key}\nModèle   : auto (choix automatique) — liste : ${base}/models`,
    message: `Accès à mon IA (Lezard IA)\n\n• Chat dans le navigateur : ${url}/#key=${encodeURIComponent(key)}\n` +
             `• Applications compatibles OpenAI : base URL ${base}, clé ${key}, modèle « auto »\n`,
  };
}

async function renderShare() {
  const u = shareUrls.find((x) => x.url === $("sd-url").value) || shareUrls[0];
  if (!u || !shareSecret) return;
  const t = shareTexts(u.url, shareSecret);
  $("sd-openai").textContent = t.openai;
  updateGuides(u.url, shareSecret, u.kind);          // Open WebUI, Continue, Cursor, SDK… (16.4)
  let [kind, note] = URL_NOTES[u.kind] || ["", ""];
  if (u.kind === "lan" && !u.secure) {                // 12.7 : clé et conversations en clair sur le Wi-Fi
    kind = "warn";
    note += " Attention : connexion NON chiffrée, la clé est lisible par les autres appareils du réseau. " +
            "Relancez avec lancer.ps1 -Lan -Https, ou utilisez Tailscale.";
  } else if (u.kind === "lan" && shareTls?.enabled) {
    note += ` Chiffré (HTTPS). Sur chaque appareil, installez une fois le certificat de la gateway : ${u.url}` +
            `/ca.crt — vérifiez que son empreinte SHA-256 commence par ${shareTls.fingerprint.slice(0, 23)}.`;
  }
  if (u.kind === "internet" && u.stable === false) {
    note += " Adresse temporaire : elle changera si le tunnel redémarre (vous serez prévenu). Pour une adresse " +
            "fixe : tunnel nommé Cloudflare ou domaine ngrok (panneau Accès distant).";
  }
  $("sd-url-note").textContent = note;
  $("sd-url-note").className = `share-note ${kind}`;
  $("sd-url-note").hidden = !note;
  const secret = shareSecret;
  $("sd-qr-wrap").hidden = true;
  if (u.kind === "local") return;                     // 127.0.0.1 : injoignable depuis un téléphone
  const r = await api("/v1/access/qr", { method: "POST", body: { url: u.url, key: secret } });
  if (secret !== shareSecret) return;                  // fenêtre fermée ou autre clé entre-temps
  $("sd-qr-wrap").hidden = !r.ok;
  if (r.ok) $("sd-qr").src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(r.data.svg)}`;
}

async function copyText(button, text) {
  const label = button.textContent;
  try { await navigator.clipboard.writeText(text); button.textContent = "Copié ✓"; }
  catch { button.textContent = "Sélectionnez et copiez"; }
  setTimeout(() => { button.textContent = label; }, 1500);
}

export async function showSecret(secret) {
  shareSecret = secret;
  $("sd-secret").textContent = secret;
  $("sd-copy").textContent = "Copier";
  $("sd-share").hidden = true;
  $("secret-dialog").showModal();
  const r = await api("/v1/access");
  if (!r.ok || secret !== shareSecret) return;
  shareUrls = r.data.urls || [];
  shareTls = r.data.tls || null;
  const sel = $("sd-url");
  sel.length = 0;
  for (const u of shareUrls) sel.add(new Option(`${u.label} — ${u.url}`, u.url));
  $("sd-share").hidden = !shareUrls.length;
  await renderShare();
}

function clearShare() {                                // le secret ne reste pas dans le DOM
  shareSecret = null;
  for (const id of ["sd-secret", "sd-openai"]) $(id).textContent = "";
  updateGuides?.("", "");
  $("sd-qr").removeAttribute("src");
  $("sd-qr-wrap").hidden = true;
}

// ---------- API du module ----------
export function initKeyDialog({ saved }) {
  onSaved = saved;
  updateGuides = mountGuides($("sd-guides"));
  document.querySelectorAll(".opt [data-toggle]").forEach((cb) =>
    cb.addEventListener("change", () => cb.closest(".opt").classList.toggle("on", cb.checked)));
  budgetRows().forEach((row) => row.querySelector("[data-on]").addEventListener("change", (e) => {
    row.classList.toggle("on", e.target.checked);
    if (e.target.checked) row.querySelector("[data-amount]").focus();
  }));
  $("k-exp").addEventListener("change", () => { $("k-exp-date-wrap").hidden = $("k-exp").value !== "date"; });
  $("key-form").addEventListener("submit", submit);
  document.querySelectorAll("dialog [data-close]").forEach((b) =>
    b.addEventListener("click", () => b.closest("dialog").close()));
  $("sd-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("sd-secret").textContent); $("sd-copy").textContent = "Copié ✓"; }
    catch { $("sd-copy").textContent = "Sélectionnez et copiez"; }
  });
  $("sd-url").addEventListener("change", renderShare);
  document.querySelectorAll("[data-share-copy]").forEach((b) =>
    b.addEventListener("click", () => copyText(b, $(b.dataset.shareCopy).textContent)));
  $("sd-copy-all").addEventListener("click", (e) => {
    if (shareSecret) copyText(e.currentTarget, shareTexts($("sd-url").value, shareSecret).message);
  });
  // le secret ne reste pas dans le DOM une fois la fenêtre fermée
  $("secret-dialog").addEventListener("close", clearShare);
  document.querySelectorAll("[data-model-add]").forEach((b) => b.addEventListener("click", () => addModel(b)));
}

// ---------- modèles autorisés / interdits : choisis dans la liste au lieu d'être tapés ----------
let modelsLoaded = null;
let providerCounts = null;                       // fournisseur -> nombre de modèles disponibles
let pendingBlocked = new Set();                  // fournisseurs bloqués de la clé en cours d'édition

const blockedProviders = () => [...document.querySelectorAll("[data-provider]:checked")].map((c) => `${c.dataset.provider}/*`);

function renderProviderChecks() {
  const box = $("p-providers");
  if (!providerCounts) return;
  if (!providerCounts.size) return box.replaceChildren(el("span", "muted small", "Aucun fournisseur disponible."));
  box.replaceChildren(...[...providerCounts].sort(([a], [b]) => a.localeCompare(b)).map(([p, n]) => {
    const label = document.createElement("label");
    label.className = "check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.dataset.provider = p;
    cb.checked = pendingBlocked.has(p);
    cb.addEventListener("change", () => { if (cb.checked) setOpt("models", true); });
    label.append(cb, document.createTextNode(` ${p} (${n} modèle${n > 1 ? "s" : ""})`));
    return label;
  }));
}

const el = (tag, cls, text) => { const e = document.createElement(tag); e.className = cls; e.textContent = text; return e; };

function loadModelPicks() {
  modelsLoaded ||= api("/v1/models").then((r) => {
    const ids = r.ok ? r.data.data.map((m) => m.id).sort() : [];
    const providers = [...new Set(ids.map((id) => id.split("/")[0]))];
    providerCounts = new Map(providers.map((p) => [p, ids.filter((id) => id.split("/")[0] === p).length]));
    renderProviderChecks();
    for (const sel of document.querySelectorAll("[data-model-pick]")) {
      sel.replaceChildren(new Option(ids.length ? "Choisir un modèle…" : "Aucun modèle disponible", ""));
      const all = document.createElement("optgroup");
      all.label = "Tous les modèles d'un fournisseur";
      providers.forEach((p) => all.append(new Option(`${p}/* (tous)`, `${p}/*`)));
      const one = document.createElement("optgroup");
      one.label = "Un modèle";
      ids.forEach((id) => one.append(new Option(id, id)));
      if (providers.length) sel.append(all, one);
    }
  });
  return modelsLoaded;
}

function addModel(button) {
  const sel = document.querySelector(`[data-model-pick="${button.dataset.modelAdd}"]`);
  const input = $(button.dataset.modelAdd);
  if (!sel.value) return sel.focus();
  const items = list(input.value);
  if (!items.includes(sel.value)) items.push(sel.value);   // pas de doublon
  input.value = items.join(", ");
  sel.value = "";
}

export function setProjects(projects) {
  const sel = $("k-project");
  sel.length = 1;
  for (const p of projects) sel.add(new Option(p.name, p.id));
}

export function openKeyDialog(key = null) {
  editing = key;
  fill(key);
  loadModelPicks();
  $("key-dialog").showModal();
  $("k-name").focus();
}
