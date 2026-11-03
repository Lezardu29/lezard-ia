// Page « Modèles » : fournisseurs (état, clés API chiffrées), ajout de fournisseurs, modèles Ollama.
import { api, clearKey, errorMessage, getKey } from "./auth.js";
import { initThemePanel, syncTheme } from "./theme-panel.js";
import { initRemotePanel } from "./remote-panel.js";
import { initPhonePanel } from "./phone-panel.js";
import { ensureOllama, fmtBytes, pullModel as streamPull, pulling } from "./ollama-pull.js";
import { initHardwareWidget } from "./hardware-widget.js";
import { initMobile } from "./mobile.js";
import { initPwa } from "./pwa.js";
import { initFolds } from "./fold.js";
import { initEmbeddings, refreshEmbeddings } from "./embeddings-ui.js";
import { initVoice, refreshVoice } from "./voice-ui.js";


const $ = (id) => document.getElementById(id);
const KEY_SOURCES = { dashboard: "clé enregistrée ici", config: "clé dans providers.json", env: "clé en variable d'environnement" };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function button(text, cls, onClick) {
  const b = el("button", `btn ${cls} btn-inline`, text);
  b.type = "button";
  b.addEventListener("click", () => onClick(b));
  return b;
}
// Champ de clé API que le navigateur ne doit pas remplir avec l'identifiant mémorisé de la page de connexion
// (la clé maître partait alors chez le fournisseur) : lecture seule tant qu'il n'a pas le focus.
function guardSecret(input) {
  input.readOnly = true;
  const unlock = () => { input.readOnly = false; };
  input.addEventListener("pointerdown", unlock);      // avant le focus : clavier dès le 1er appui sur mobile
  input.addEventListener("focus", unlock);
  input.addEventListener("blur", () => { input.readOnly = true; });
}
function secretInput() {
  const input = el("input");
  input.type = "password";
  input.name = "provider-api-key";
  input.autocomplete = "new-password";
  input.spellcheck = false;
  for (const a of ["data-lpignore", "data-1p-ignore", "data-bwignore"]) input.setAttribute(a, "true");
  guardSecret(input);
  return input;
}

function message(text, ok = false) {
  $("msg").textContent = text;
  $("msg").className = `alert${text ? " show" : ""}${ok ? " ok-box" : ""}`;
  if (text) $("msg").scrollIntoView({ block: "nearest" });
}

// ---------- fournisseurs ----------
function providerItem(p) {
  const li = el("li", `provider${p.enabled ? "" : " off"}`);
  const head = el("div", "pv-head");
  const state = !p.enabled ? ["status off", "Désactivé"] : p.online ? ["status active", "En ligne"] : ["status revoked", "Hors ligne"];
  head.append(el("strong", "", p.name), el("span", `chip-host ${p.hosting}`, p.hosting === "local" ? "local" : "cloud"),
              el("span", state[0], state[1]), el("span", "grow"));
  if (p.custom) head.append(button("Supprimer", "btn-ghost", (b) => removeProvider(p, b)));
  li.append(head);

  const keyInfo = p.key.source ? `${KEY_SOURCES[p.key.source]}${p.key.hint ? ` (${p.key.hint})` : ""}`
    : p.hosting === "cloud" ? `aucune clé${p.key.env ? ` — ou variable ${p.key.env}` : ""}` : "sans clé";
  li.append(el("p", "pv-meta", `${p.base_url} · ${keyInfo}`));

  if (p.models.length) {
    const d = el("details", "pv-models");
    d.append(el("summary", "", `${p.models.length} modèle${p.models.length > 1 ? "s" : ""}`),
             el("p", "", p.models.map((m) => `${p.name}/${m}`).join("  ·  ")));
    li.append(d);
  } else if (!p.enabled && p.hosting === "cloud") {
    li.append(el("p", "pv-meta", "Ajoutez une clé API pour activer ce fournisseur."));
  } else if (!p.online && p.hosting === "local") {
    li.append(el("p", "pv-meta", "Serveur local non démarré sur cette machine."));
  }

  if (p.enabled && !p.online && p.error) {
    li.append(el("p", "alert show", p.hosting === "cloud" && /HTTP 40[013]/.test(p.error)
      ? `Clé refusée par ${p.name} (${p.error}). Remplacez-la ci-dessous par la clé obtenue dans la console du fournisseur.`
      : `${p.name} ne répond pas : ${p.error}`));
  }

  // un serveur local (Ollama, LM Studio…) n'a pas de clé : pas de champ, seulement le retrait d'une clé enregistrée
  if (p.hosting === "cloud" || (p.hosting !== "local" && (p.custom || p.key.source === "dashboard"))) {
    const row = el("div", "pv-key");
    const input = secretInput();
    input.placeholder = p.key.source ? "Remplacer la clé API" : "Clé API";
    input.setAttribute("aria-label", `Clé API ${p.name}`);
    row.append(input, button("Enregistrer", "btn-primary", (b) => saveKey(p, input, b)));
    if (p.key.source === "dashboard") row.append(button("Retirer la clé", "btn-ghost", (b) => removeKey(p, b)));
    li.append(row);
  } else if (p.key.source === "dashboard") {
    li.append(el("div", "pv-key", button("Retirer la clé", "btn-ghost", (b) => removeKey(p, b))));
  }
  if (p.usage?.saturated) li.append(el("p", "alert show", `${p.usage.saturated} Le routage automatique utilise les autres IA en attendant.`));
  li.append(rulesPanel(p));
  return li;
}

// ---------- restrictions de l'IA en entrée ----------
const PERIOD_LABELS = { day: "jour", week: "semaine", month: "mois" };
const fmt = (n) => n.toLocaleString("fr-FR");
const euros = (n) => `${n.toLocaleString("fr-FR", { maximumFractionDigits: 2 })} €`;

function rulesSummary(r) {
  const parts = [];
  if (r.rpm) parts.push(`${fmt(r.rpm)} req/min`);
  if (r.rpd) parts.push(`${fmt(r.rpd)} req/jour`);
  if (r.tpm) parts.push(`${fmt(r.tpm)} tokens/min`);
  if (r.tpd) parts.push(`${fmt(r.tpd)} tokens/jour`);
  for (const b of r.budget || []) parts.push(`${fmt(b.amount)} ${b.currency === "EUR" ? "€" : "$"}/${PERIOD_LABELS[b.period]}`);
  if (r.max_tokens) parts.push(`${fmt(r.max_tokens)} tokens max/réponse`);
  return parts.join(" · ");
}

function field(label, input) {
  const l = el("label", "", label);
  l.append(input);
  return l;
}
function textInput(value, placeholder) {
  const i = el("input");
  i.value = value || "";
  i.placeholder = placeholder;
  return i;
}
function numberInput(value, placeholder, step = "1") {
  const i = textInput(value ?? "", placeholder);
  i.type = "number";
  i.min = step === "1" ? "1" : step;
  i.step = step;
  return i;
}
const int = (input) => (input.value ? Number.parseInt(input.value, 10) : null);

// Champs des restrictions (débit, budget, taille des réponses), partagés par le panneau d'un fournisseur et la
// fenêtre d'ajout. read() → { rules }.
function rulesFields(r) {
  const rpm = numberInput(r.rpm, "illimité"), tpm = numberInput(r.tpm, "illimité"), tpd = numberInput(r.tpd, "illimité");
  const rpd = numberInput(r.rpd, "illimité");
  const budget = (r.budget || [])[0];
  const amount = numberInput(budget?.amount, "aucun", "0.01");
  const period = el("select");
  for (const [v, label] of Object.entries(PERIOD_LABELS)) period.append(new Option(`par ${label}`, v));
  period.value = budget?.period || "month";
  const maxTokens = numberInput(r.max_tokens, "illimité");

  const budgetRow = el("div", "budget-pair");
  budgetRow.append(amount, period);
  const nodes = [
    field("Requêtes / min", rpm), field("Requêtes / jour", rpd), field("Tokens / min", tpm), field("Tokens / jour", tpd),
    field("Budget (€)", budgetRow), field("Tokens max par réponse", maxTokens)];

  function read() {
    const rules = {};
    for (const [k, input] of [["rpm", rpm], ["rpd", rpd], ["tpm", tpm], ["tpd", tpd], ["max_tokens", maxTokens]]) {
      if (input.value) rules[k] = int(input);
    }
    if (amount.value) rules.budget = [{ amount: Number(amount.value), currency: "EUR", period: period.value }];
    return { rules };
  }
  return { nodes, read };
}

function rulesPanel(p) {
  const r = p.rules || {};
  const d = el("details", "pv-rules");
  const summary = el("summary");
  summary.append(el("span", "", "Restrictions"), el("span", "pv-rules-sum", rulesSummary(r) || "aucune"));
  d.append(summary);

  const form = el("form", "rules-form");
  const fields = rulesFields(r);
  form.append(...fields.nodes);

  const s = p.usage || {};
  const spent = s.spend ? `Dépensé : ${euros(s.spend.day.spent_eur)} aujourd'hui · ${euros(s.spend.month.spent_eur)} ce mois`
    : "";
  const live = s.rpm ? ` · ${fmt(s.rpm.used)} req et ${fmt(s.tpm.used)} tokens dans la dernière minute` : "";
  const foot = el("div", "rules-foot");
  foot.append(el("span", "muted small", spent + live), el("span", "grow"));
  if (Object.keys(r).length) foot.append(button("Tout lever", "btn-ghost", (b) => saveRules(p, {}, b)));
  const save = el("button", "btn btn-primary btn-inline", "Enregistrer");
  save.type = "submit";
  foot.append(save);
  form.append(foot);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    saveRules(p, fields.read().rules, save);
  });
  d.append(form);
  return d;
}

async function saveRules(p, rules, b) {
  b.disabled = true;
  const r = await api(`/v1/admin/providers/${encodeURIComponent(p.name)}/rules`, { method: "PUT", body: rules });
  b.disabled = false;
  if (!r.ok) return message(errorMessage(r));
  const sum = rulesSummary(r.data.rules);
  message(sum ? `Restrictions de ${p.name} : ${sum}.` : `${p.name} n'a plus de restriction.`, true);
  openRules = p.name;
  loadProviders();
}
let openRules = null;       // panneau resté ouvert après enregistrement

async function loadProviders() {
  const r = await api("/v1/admin/providers");
  if (!r.ok) return message(errorMessage(r));
  const list = r.data.data.sort((a, b) => (b.online - a.online) || (b.enabled - a.enabled) || a.name.localeCompare(b.name));
  providerNames = new Set(list.map((p) => p.name));
  $("providers").replaceChildren(...list.map(providerItem));
  if (!list.length) {
    $("providers").append(el("li", "empty", "Aucune IA pour l'instant : cliquez sur « + Ajouter un modèle » pour " +
                                            "en brancher une (clé API, nom et restrictions)."));
  }
  if (openRules) {
    const i = list.findIndex((p) => p.name === openRules);
    if (i >= 0) $("providers").children[i].querySelector(".pv-rules").open = true;
    openRules = null;
  }
  const online = list.filter((p) => p.online);
  $("pv-count").textContent = `${online.length} en ligne · ${online.reduce((n, p) => n + p.models.length, 0)} modèles disponibles`;
  renderOllama(list.find((p) => p.name === "ollama") || list.find((p) => p.ollama));
}

async function saveKey(p, input, b) {
  if (input.value.trim().length < 8) return message("Collez la clé API complète.");
  b.disabled = true;
  const r = await api(`/v1/admin/credentials/${encodeURIComponent(p.name)}`, { method: "PUT", body: { api_key: input.value.trim() } });
  b.disabled = false;
  input.value = "";
  if (!r.ok) return message(errorMessage(r));
  message(r.data.online ? `Clé enregistrée : ${p.name} est en ligne.`
    : `Clé enregistrée, mais ${p.name} la refuse ou ne répond pas : ${r.data.error || "vérifiez la clé"}.`, r.data.online);
  loadProviders();
}

async function removeKey(p, b) {
  if (!confirm(`Retirer la clé API de ${p.name} ?`)) return;
  b.disabled = true;
  const r = await api(`/v1/admin/credentials/${encodeURIComponent(p.name)}`, { method: "DELETE" });
  if (!r.ok) { b.disabled = false; return message(errorMessage(r)); }
  message(`Clé de ${p.name} retirée.`, true);
  loadProviders();
}

async function removeProvider(p, b) {
  if (!confirm(`Supprimer le fournisseur ${p.name} (et sa clé) ?`)) return;
  b.disabled = true;
  const r = await api(`/v1/admin/providers/${encodeURIComponent(p.name)}`, { method: "DELETE" });
  if (!r.ok) { b.disabled = false; return message(errorMessage(r)); }
  message(`${p.name} supprimé.`, true);
  loadProviders();
}

// ---------- outils (serveurs MCP) et clés des services (GitHub, Brave…) ----------
let latestServices = { catalog: [], keys: [] };

async function loadTools() {
  const [m, s] = await Promise.all([api("/v1/admin/mcp"), api("/v1/admin/services")]);
  if (!m.ok) return message(errorMessage(m));
  if (s.ok) {                                       // d'abord les clés : la liste des outils les affiche
    latestServices = { catalog: s.data.catalog, keys: s.data.keys };
    if (!$("svc-service").options.length) $("svc-service").replaceChildren(...latestServices.catalog.map((c) => new Option(c.label, c.id)));
    syncScope();
    renderKeys(latestServices.keys);
  }
  renderMcp(m.data.data);
}

const scopeText = (k) => {
  if (k.scope?.mode === "repos") return `${k.scope.repos.length} dépôt(s) précis`;
  return k.service === "github" ? "accès à tous les dépôts" : "sans restriction";
};

function renderMcp(list) {
  const connected = list.filter((x) => x.ok).length;
  $("mcp-count").textContent = list.length ? `${connected} sur ${list.length} connecté(s)` : "";
  const items = list.map((x) => {
    const li = el("li", "provider");
    const head = el("div", "pv-head");
    head.append(el("strong", "", x.name), el("span", x.ok ? "pill ok" : "pill", x.ok ? `connecté · ${x.tools} outil(s)` : "non connecté"));
    li.append(head);
    if (x.error) li.append(el("p", "small muted", x.error.slice(0, 200)));
    const key = latestServices.keys.find((k) => k.id === x.name);    // un outil = une clé, nommée pareil
    if (key) li.append(el("p", "small muted", `Clé « ${key.label} » : ${key.configured ? key.hint : "non configurée"} · ${scopeText(key)}`));
    return li;
  });
  $("mcp-list").replaceChildren(...(items.length ? items : [el("li", "empty", "Aucun serveur MCP connecté pour l'instant.")]));
}

function renderKeys(list) {
  const items = list.map((k) => {
    const li = el("li", "provider");
    const head = el("div", "pv-head");
    const svc = latestServices.catalog.find((c) => c.id === k.service);
    head.append(el("strong", "", k.label), el("span", k.configured ? "pill ok" : "pill", k.configured ? `clé : ${k.hint}` : "non configurée"));
    li.append(head, el("p", "small muted", `${svc?.label || k.service} · ${scopeText(k)}`));
    const remove = el("button", "btn btn-ghost btn-inline", "Retirer");
    remove.type = "button";
    remove.addEventListener("click", () => removeServiceKey(k, remove));
    li.append(remove);
    return li;
  });
  $("svc-list").replaceChildren(...(items.length ? items : [el("li", "empty", "Aucune clé pour l'instant : ajoutez-en une ci-dessus.")]));
}

function syncScope() {
  const c = latestServices.catalog.find((x) => x.id === $("svc-service").value);
  $("svc-scope").hidden = !c?.scope;
  syncRepos();
}

function syncRepos() {
  $("svc-repos-wrap").hidden = document.querySelector("[name=svc-mode]:checked")?.value !== "repos";
}

async function addKey(b) {
  const service = $("svc-service").value;
  const label = $("svc-label").value.trim();
  const secret = $("svc-secret").value.trim();
  if (secret.length < 8) return message("Collez la clé complète.");
  const c = latestServices.catalog.find((x) => x.id === service);
  const mode = document.querySelector("[name=svc-mode]:checked")?.value || "all";
  const repos = $("svc-repos").value.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
  const scope = c?.scope ? { mode, repos } : null;
  b.disabled = true;
  const r = await api("/v1/admin/services/keys", { method: "POST", body: { service, label, secret, scope } });
  b.disabled = false;
  $("svc-secret").value = "";
  if (!r.ok) return message(errorMessage(r));
  $("svc-label").value = "";
  $("svc-repos").value = "";
  message(`Clé « ${label} » ajoutée : outils rechargés.`, true);
  loadTools();
}

async function removeServiceKey(k, b) {
  if (!confirm(`Retirer la clé « ${k.label} » ? Ses outils ne se connecteront plus.`)) return;
  b.disabled = true;
  const r = await api(`/v1/admin/services/keys/${encodeURIComponent(k.id)}`, { method: "DELETE" });
  if (!r.ok) { b.disabled = false; return message(errorMessage(r)); }
  message(`Clé « ${k.label} » retirée.`, true);
  loadTools();
}

async function reloadTools(b) {
  b.disabled = true;
  const r = await api("/v1/admin/mcp/reload", { method: "POST" });
  b.disabled = false;
  if (!r.ok) return message(errorMessage(r));
  message("Outils rechargés.", true);
  loadTools();
}

// ---------- Nextcloud (commande /docs) : adresse, utilisateur, mot de passe d'application ----------
function renderNextcloud(v) {
  $("nc-url").value = v.url;
  $("nc-user").value = v.user;
  $("nc-state").textContent = v.active
    ? "Active : nextcloud_list et nextcloud_read sont disponibles dans le chat (clé maître)."
    : "Inactive : renseignez les trois champs, puis enregistrez.";
  $("nc-clear").hidden = !v.url && !v.user;
}

async function loadNextcloud() {
  const r = await api("/v1/admin/nextcloud");
  if (r.ok) renderNextcloud(r.data.data);
}

async function saveNextcloud(b) {
  b.disabled = true;
  const body = { url: $("nc-url").value, user: $("nc-user").value, password: $("nc-password").value };
  const r = await api("/v1/admin/nextcloud", { method: "PUT", body });
  b.disabled = false;
  $("nc-password").value = "";
  if (!r.ok) return message(errorMessage(r));
  renderNextcloud(r.data.data);
  message(`Nextcloud enregistré : ${r.data.tools.join(", ") || "aucun outil"}.`, true);
}

async function clearNextcloud(b) {
  if (!confirm("Retirer la configuration Nextcloud ? Ses outils disparaîtront du chat.")) return;
  b.disabled = true;
  const r = await api("/v1/admin/nextcloud", { method: "DELETE" });
  b.disabled = false;
  if (!r.ok) return message(errorMessage(r));
  renderNextcloud(r.data.data);
  message("Configuration Nextcloud retirée.", true);
}

// ---------- ajout : fenêtre « Ajouter un modèle » ----------
const NAME_RE = /^[a-z][a-z0-9-]{1,30}$/;
let providerNames = new Set();
let presetList = [];
let addRules = null;

function addError(text) {
  $("ad-error").textContent = text;
  $("ad-error").classList.toggle("show", !!text);
}

function freeName(base) {                       // nom proposé, rendu unique (openai, openai-2…)
  let name = base, i = 2;
  while (providerNames.has(name)) name = `${base}-${i++}`;
  return name;
}

const selectedPreset = () => presetList.find((x) => x.name === $("add-preset").value);

function keyHint() {
  const p = selectedPreset(), hint = $("add-key-hint");
  const local = $("add-hosting").value === "local";
  const envKey = p?.env_key && $("add-name").value.trim() === p.name;
  $("add-key").placeholder = local ? "facultative" : envKey ? `vide = ${p.key_env} du fichier .env` : "requise";
  hint.replaceChildren(local ? "Un serveur local ne demande généralement pas de clé."
    : envKey ? `Une clé ${p.key_env} est déjà dans .env : laissez le champ vide pour l'utiliser.`
    : "La clé est chiffrée en base et ne sera plus jamais affichée.");
  if (!local && p?.key_url) hint.append(" ", link(p.key_url, "Obtenir une clé"));
}

const PICK = "__pick";                          // « Quelle IA ajouter ? » : rien de choisi

function applyPreset() {
  const v = $("add-preset").value, p = selectedPreset();
  const localServer = p?.hosting === "local";     // Ollama, LM Studio… : ce qui est sur ce PC
  $("ad-details").hidden = v === PICK;
  $("add-advanced").open = v === "";              // personnalisé : URL à saisir
  if (p) {
    $("add-name").value = freeName(p.name);
    $("add-url").value = p.base_url;
    $("add-kind").value = p.kind;
    $("add-hosting").value = p.hosting;
  } else {
    $("add-name").value = "";
    $("add-url").value = "";
  }
  $("add-key-field").hidden = $("add-key-hint").hidden = localServer;
  $("ad-local").hidden = !localServer;
  $("add-btn").hidden = v === PICK;
  $("add-btn").textContent = "Ajouter";
  addError("");
  keyHint();
  if (localServer) showLocal(p);
}

// prefill : serveur trouvé sur le réseau qui demande une clé ({ name, base_url, hosting }), ou { preset: "ollama" }
async function openAdd(prefill = null) {
  const r = await api("/v1/admin/providers/presets");
  if (!r.ok) return message(errorMessage(r));
  presetList = r.data.data;
  discovery = null;                               // nouvelle recherche sur ce PC à chaque ouverture
  const sel = $("add-preset");
  sel.replaceChildren(new Option("Choisissez une IA…", PICK));
  for (const [hosting, label] of [["local", "Sur ce PC (gratuit, privé)"], ["cloud", "En ligne (clé API)"]]) {
    const group = el("optgroup");
    group.label = label;
    for (const p of presetList.filter((x) => x.hosting === hosting)) {
      const o = new Option(p.added ? `${p.label} (déjà ajouté)` : p.label, p.name);
      group.append(o);
    }
    if (group.children.length) sel.append(group);
  }
  sel.append(new Option("Personnalisé (autre service, autre machine…)", ""));
  sel.value = prefill?.preset || (prefill ? "" : PICK);
  $("add-key").value = "";
  addRules = rulesFields({});
  $("add-rules").replaceChildren(...addRules.nodes);
  applyPreset();
  if (prefill?.base_url) {
    $("add-name").value = prefill.name;
    $("add-url").value = prefill.base_url;
    $("add-hosting").value = prefill.hosting;
    keyHint();
  }
  $("add-dialog").showModal();
  (prefill?.base_url ? $("add-key") : sel).focus();
}

// ---------- serveur local choisi : modèles déjà sur ce PC, modèles à installer (Ollama) ----------
let discovery = null;                           // promesse de /v1/admin/local/discover, une par ouverture
let onThisPc = null;                            // son résultat

function discover() {
  discovery ||= api("/v1/admin/local/discover").then((r) => {
    if (!r.ok) { discovery = null; throw new Error(errorMessage(r)); }
    onThisPc = r.data;
    return r.data;
  });
  return discovery;
}

function fitText(m, budget) {
  if (m.fit === "vram") return "tient en VRAM (rapide)";
  return budget.gpu ? "VRAM + RAM (plus lent)" : "processeur (lent)";
}

async function showLocal(p) {
  $("loc-status").textContent = `Recherche de ${p.label.replace(/ \(ce PC\)$/, "")} sur ce PC…`;
  $("loc-have").replaceChildren();
  $("loc-have-title").hidden = true;
  $("loc-install").hidden = true;
  $("loc-ollama").hidden = true;
  $("add-btn").hidden = true;
  let d;
  try { d = await discover(); } catch (e) { $("loc-status").textContent = ""; return addError(e.message); }
  if ($("add-preset").value !== p.name) return;   // autre choix entre-temps
  const label = p.label.replace(/ \(ce PC\)$/, "");
  const server = d.servers.find((s) => s.server === p.name);
  const isOllama = p.name === "ollama";
  if (server) {
    $("add-url").value = server.base_url;
    $("loc-have-title").hidden = false;
    $("loc-have").replaceChildren(...server.models.map((m) => {
      const li = el("li", "loc-item");
      li.append(el("strong", "loc-model", m), el("span", "pill", "sur ce PC"));
      return li;
    }));
    if (!server.models.length) {
      $("loc-have").append(el("li", "empty small", isOllama ? "Aucun modèle installé : choisissez-en un ci-dessous."
        : `Aucun modèle chargé dans ${label}.`));
    }
    if (server.provider) {
      $("loc-status").textContent = `${label} est déjà ajouté (« ${server.provider} ») : ses modèles sont utilisables ` +
        "dans le chat." + (isOllama ? " Vous pouvez en installer d'autres ci-dessous." : "");
    } else {
      $("add-name").value = server.suggested_name;
      $("loc-status").textContent = `${label} tourne sur ce PC. « Ajouter » rend ses ${server.models.length} modèle(s) ` +
        "utilisables dans le chat (0 €, rien ne quitte ce PC).";
      $("add-btn").hidden = false;
      $("add-btn").textContent = `Ajouter ${label}`;
      if (server.needs_key) {                     // serveur protégé par une clé
        $("add-key-field").hidden = $("add-key-hint").hidden = false;
        $("loc-status").textContent += " Ce serveur demande une clé.";
      }
    }
  } else if (isOllama) {
    $("loc-status").textContent = "";
    renderOllamaHint(d.ollama);
  } else {
    $("loc-status").textContent = `${label} ne répond pas sur ce PC (${p.base_url}). Démarrez-le et chargez un ` +
      "modèle, puis choisissez-le à nouveau.";
  }
  if (isOllama) {
    $("loc-install").hidden = false;
    renderCatalog(d.catalog, d.ollama);
  }
}

function renderOllamaHint(o) {
  const box = $("loc-ollama");
  box.replaceChildren();
  box.hidden = false;
  if (o.installed) {
    box.append(el("span", "", "Ollama est installé mais arrêté : démarrez-le pour voir et installer des modèles. "),
               button("Démarrer Ollama", "btn-primary", startOllama));
  } else {
    box.append(el("span", "", "Ollama n'est pas installé sur ce PC. Installez-le (gratuit), puis choisissez-le à nouveau. "),
               link(o.download_url, "Télécharger Ollama"));
  }
}

async function startOllama(b) {
  b.disabled = true;
  b.textContent = "Démarrage…";
  const r = await api("/v1/admin/local/ollama/start", { method: "POST" });
  if (!r.ok) { b.disabled = false; b.textContent = "Démarrer Ollama"; return addError(errorMessage(r)); }
  refreshLocal();
}

function refreshLocal() {
  discovery = null;
  const p = selectedPreset();
  if (p?.hosting === "local") showLocal(p);
}

function renderCatalog(catalog, o) {
  const b = catalog.budget;
  $("loc-hw").textContent = [b.gpu ? `${b.gpu} : ${size(b.vram)} de VRAM utilisable` : "Pas de carte graphique NVIDIA : calcul sur le processeur",
                             `RAM utilisable ${size(b.ram)}`, `disque libre ${size(b.disk_free)}`].join(" · ");
  const best = new Set(Object.values(catalog.best).filter(Boolean));
  const usable = catalog.models.filter((m) => m.fit !== "too_big" && !m.installed)   // installés : liste du dessus
    .sort((x, y) => (best.has(y.model) - best.has(x.model)) || ((x.fit === "vram" ? 0 : 1) - (y.fit === "vram" ? 0 : 1))
                    || (y.params_b - x.params_b));
  $("loc-catalog").replaceChildren(...usable.map((m) => catalogItem(m, catalog, o, best.has(m.model))));
  if (!usable.length) $("loc-catalog").append(el("li", "empty small", "Rien d'autre à proposer pour cette machine."));
}

function catalogItem(m, catalog, o, recommended) {
  const li = el("li", `loc-item${recommended ? " best" : ""}`);
  const info = el("div", "scan-info");
  const uses = m.uses.map((u) => catalog.uses[u].toLowerCase()).join(", ");
  info.append(el("strong", "", `${m.label}${recommended ? " — recommandé" : ""}`),
              el("span", "muted small", `${m.model} · ${uses} · ${size(m.download)} · ${fitText(m, catalog.budget)}`));
  li.append(info);
  if (!m.disk_ok) li.append(el("span", "muted small", "espace disque insuffisant"));
  else if (!o.running) li.append(el("span", "muted small", o.installed ? "démarrez Ollama" : "Ollama requis"));
  else li.append(button("Installer", "btn-primary", (btn) => installLocal(m, btn)));
  return li;
}

async function addLocalServer(name, baseUrl) {
  const r = await api("/v1/admin/providers", { method: "POST", body: {
    name, base_url: baseUrl, kind: "openai", hosting: "local", rules: addRules.read().rules } });
  if (!r.ok) { addError(errorMessage(r)); return null; }
  providerNames.add(name);
  message(`${name} ajouté : ${r.data.models.length} modèle(s) local(aux), utilisables dans le chat et par le routage ` +
          "automatique (0 €).", true);
  loadProviders();
  return name;
}

async function installLocal(m, btn) {
  if (pulling()) return addError("Un téléchargement est déjà en cours.");
  if (!confirm(`Télécharger ${m.label} (${m.model}) ? ${size(m.download)} à télécharger.` +
               (m.fit === "offload" ? "\n\nIl ne tient pas entièrement en VRAM : réponses plus lentes." : ""))) return;
  btn.disabled = true;
  let provider = onThisPc.ollama.provider;
  if (!provider) {                                 // Ollama pas encore ajouté : il l'est d'abord
    provider = await addLocalServer(onThisPc.ollama.suggested_name, onThisPc.ollama.base_url);
    if (!provider) { btn.disabled = false; return; }
    onThisPc.ollama.provider = provider;
  }
  const status = el("span", "small loc-progress", "Connexion à Ollama…");
  btn.replaceWith(status);
  const failed = await streamPull(m.model, provider, (ev) => {
    status.textContent = ev.pct !== undefined ? `${ev.pct} % (${size(ev.completed)} / ${size(ev.total)})`
      : ev.status === "done" ? "Terminé" : ev.status;
  });
  if (failed) {
    status.replaceWith(button("Réessayer", "btn-primary", (b) => installLocal(m, b)));
    return addError(`Téléchargement de ${m.model} impossible : ${failed}`);
  }
  status.replaceWith(el("span", "pill", "installé"));
  message(`${m.model} est installé et disponible dans le chat (${provider}/${m.model}).`, true);
  loadProviders();
  refreshLocal();                                  // il apparaît dans « Modèles déjà sur ce PC »
}

async function addProvider(e) {
  e.preventDefault();
  if ($("add-preset").value === PICK) return addError("Choisissez d'abord l'IA à ajouter.");
  const body = { name: $("add-name").value.trim(), base_url: $("add-url").value.trim(), kind: $("add-kind").value,
                 hosting: $("add-hosting").value, api_key: $("add-key").value.trim() || undefined };
  if (!NAME_RE.test(body.name)) {
    return addError("Nom : 2 à 31 caractères, minuscules, chiffres et tirets, en commençant par une lettre.");
  }
  if (providerNames.has(body.name)) return addError(`Le nom « ${body.name} » est déjà utilisé : choisissez-en un autre.`);
  if (!/^https?:\/\/\S+$/.test(body.base_url)) {
    $("add-advanced").open = true;
    return addError("Indiquez l'URL de l'API (ex. https://api.exemple.com/v1).");
  }
  const p = selectedPreset();
  if (body.hosting === "cloud" && !body.api_key && !(p?.env_key && body.name === p.name)) {
    return addError("Collez la clé API (token) de ce fournisseur.");
  }
  body.rules = addRules.read().rules;
  $("add-btn").disabled = true;
  const r = await api("/v1/admin/providers", { method: "POST", body });
  $("add-btn").disabled = false;
  if (!r.ok) return addError(errorMessage(r));
  $("add-key").value = "";
  providerNames.add(body.name);
  const sum = rulesSummary(r.data.rules);
  message((r.data.online ? `${body.name} ajouté : ${r.data.models.length} modèle(s) disponible(s).`
    : `${body.name} ajouté, mais il ne répond pas : ${r.data.error || "URL ou clé à vérifier"}.`) +
    (sum ? ` Restrictions : ${sum}.` : ""), r.data.online);
  loadProviders();
  if (p?.name === "ollama") refreshLocal();        // la fenêtre reste ouverte : installer d'autres modèles
  else $("add-dialog").close();
}

function link(href, text) {
  const a = el("a", "", text);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}

// ---------- tarifs inconnus (10.7) ----------
const dateFr = (ts) => new Date(ts * 1000).toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" });

async function loadPrices() {
  const r = await api("/v1/admin/pricing/unknown");
  if (!r.ok) return message(errorMessage(r));
  const { data, last_import: imp, builtin_date: builtin, auto_refresh: auto } = r.data;
  const since = imp ? `dernière mise à jour le ${dateFr(imp.at)} (${imp.updated} modèle(s))`
    : `grille intégrée du ${dateFr(Date.parse(builtin) / 1000)}, pas encore mise à jour`;
  $("price-info").textContent = `Prix en dollars par million de tokens, ${since}. ` + (auto
    ? "Mise à jour automatique chaque jour, et dès qu'un modèle sans tarif est utilisé."
    : "Mise à jour automatique désactivée (PRICING_AUTO_REFRESH=false).");
  $("price-warn").hidden = !data.length;
  $("price-warn").textContent = data.length
    ? `${data.length} modèle(s) utilisé(s) ces 30 derniers jours sans tarif connu : leurs requêtes ont été comptées ` +
      "à 0 €, ce qui fausse dépenses, budgets et économies. Saisissez leur prix (page du fournisseur) :"
    : "";
  $("unknown-prices").replaceChildren(...data.map(unknownItem));
}

function unknownItem(m) {
  const li = el("li", "unknown-price");
  const info = el("div", "scan-info");
  info.append(el("strong", "", m.ref),
              el("span", "muted small", `${fmt(m.requests)} requête(s) · ${fmt(m.tokens)} tokens · dernière le ${dateFr(m.last_used)}`));
  const input = numberInput("", "entrée $/M", "0.001"), output = numberInput("", "sortie $/M", "0.001");
  input.min = output.min = "0";
  input.setAttribute("aria-label", `Prix d'entrée de ${m.ref} en dollars par million de tokens`);
  output.setAttribute("aria-label", `Prix de sortie de ${m.ref} en dollars par million de tokens`);
  const form = el("form", "price-form");
  const save = el("button", "btn btn-primary btn-inline", "Enregistrer");
  save.type = "submit";
  form.append(input, output, save);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (input.value === "" || output.value === "") return message("Indiquez les deux prix (0 si gratuit).");
    save.disabled = true;
    const r = await api(`/v1/admin/pricing/${m.ref.split("/").map(encodeURIComponent).join("/")}`, { method: "PUT",
      body: { input_per_mtok_usd: Number(input.value), output_per_mtok_usd: Number(output.value) } });
    save.disabled = false;
    if (!r.ok) return message(errorMessage(r));
    message(`Tarif de ${m.ref} enregistré : les prochaines requêtes seront comptées à leur coût réel.`, true);
    loadPrices();
  });
  li.append(info, form);
  return li;
}

async function importPrices(b) {
  b.disabled = true;
  const r = await api("/v1/admin/pricing/import", { method: "POST" });
  b.disabled = false;
  if (!r.ok) return message(errorMessage(r));
  message(`Tarifs mis à jour : ${r.data.updated} modèle(s).`, true);
  loadPrices();
}

// ---------- autres machines du réseau (12.5) ----------
async function scanNetwork(e) {
  e.preventDefault();
  const hosts = $("scan-hosts").value.split(/[,\s]+/).map((h) => h.trim()).filter(Boolean);
  $("scan-btn").disabled = true;
  $("scan-status").textContent = hosts.length ? "Recherche en cours…" : "Recherche sur tout le réseau local (environ 10 secondes)…";
  $("scan-results").replaceChildren();
  const r = await api("/v1/admin/network/scan", { method: "POST", body: hosts.length ? { hosts } : {} });
  $("scan-btn").disabled = false;
  if (!r.ok) { $("scan-status").textContent = ""; return message(errorMessage(r)); }
  const { found, scanned, targets } = r.data;
  $("scan-status").textContent = found.length
    ? `${found.length} serveur(s) trouvé(s) sur ${scanned} adresse(s) (${targets.join(", ")}).`
    : `Aucun serveur de modèles trouvé sur ${scanned} adresse(s) (${targets.join(", ")}). Sur l'autre machine, ` +
      "le serveur doit écouter sur le réseau (Ollama : variable OLLAMA_HOST=0.0.0.0) et son pare-feu l'autoriser.";
  $("scan-results").replaceChildren(...found.map(scanItem));
}

function scanItem(f) {
  const li = el("li", "scan-item");
  const info = el("div", "scan-info");
  info.append(el("strong", "", `${f.server} — ${f.host}:${f.port}`));
  const models = f.needs_key ? "clé API requise" : f.models.length
    ? `${f.models.length} modèle(s) : ${f.models.slice(0, 4).join(", ")}${f.models.length > 4 ? "…" : ""}`
    : "aucun modèle installé";
  info.append(el("span", "muted small", models));
  li.append(info);
  if (f.provider) {
    li.append(el("span", "pill", `déjà ajouté (${f.provider})`));
  } else if (f.needs_key) {
    li.append(button("Compléter…", "btn-ghost", () =>       // clé requise : fenêtre d'ajout pré-remplie
      openAdd({ name: f.suggested_name, base_url: f.base_url, hosting: "local" })));
  } else {
    li.append(button("Ajouter", "btn-primary", (b) => addFound(f, b, li)));
  }
  return li;
}

async function addFound(f, b, li) {
  b.disabled = true;
  const r = await api("/v1/admin/providers", { method: "POST", body: {
    name: f.suggested_name, base_url: f.base_url, kind: "openai", hosting: "local" } });
  if (!r.ok) { b.disabled = false; return message(errorMessage(r)); }
  b.replaceWith(el("span", "pill", `ajouté (${f.suggested_name})`));
  message(`${f.suggested_name} ajouté : ${r.data.models.length} modèle(s) local(aux), utilisables dans le chat ` +
          `(${f.suggested_name}/…) et par le routage automatique.`, true);
  loadProviders();
}

// ---------- Ollama ----------
let ollama = null;

function renderOllama(p) {
  ollama = p || null;
  $("ollama-section").hidden = !p;
  if (!p) return;
  $("ol-state").textContent = p.online ? "Ollama en ligne" : "Ollama arrêté";
  $("pull-form").hidden = !p.online;
  if (p.online) loadReco(); else $("reco").hidden = true;
  const list = $("local-models");
  if (!p.online) {
    list.replaceChildren(el("li", "empty", "Ollama ne répond pas : installez-le depuis ollama.com puis relancez lancer.bat."));
    return;
  }
  list.replaceChildren(...p.models.map((m) => {
    const li = el("li", "local-model");
    li.append(el("span", "", m), button("Supprimer", "btn-ghost", (b) => deleteModel(m, b)));
    return li;
  }));
  if (!p.models.length) list.append(el("li", "empty", "Aucun modèle installé : téléchargez-en un ci-dessus."));
}

const size = fmtBytes;

// ---------- recommandation selon le matériel (13.2) ----------
let reco = null;
let hw = null;              // widget Matériel & IA locales (13.3)

async function loadReco() {
  const r = await api("/v1/admin/models/recommendations");
  $("reco").hidden = !r.ok;
  if (!r.ok) return;
  reco = r.data;
  renderReco();
}

function fitLabel(fit) {
  if (fit === "vram") return "Tient en VRAM";
  if (fit === "offload") return reco.budget.gpu ? "VRAM + RAM (plus lent)" : "RAM, sur le processeur (lent)";
  return "Trop gros";
}

function fitChip(m) {
  return el("span", `fit ${m.fit}`, fitLabel(m.fit));
}

// vitesse estimée par le test de performance (rien tant que le test n'a pas été fait)
function speedChip(m) {
  if (m.speed == null || !m.speed_label) return el("span", "", "");
  const cls = m.speed_label.normalize("NFD").replace(/[̀-ͯ]/g, "").replace(" ", "-");
  const source = m.speed_source ? ` (${m.speed_source})` : "";
  return el("span", `speed ${cls}${m.speed_source === "mesuré" ? " mesure" : ""}`, `≈ ${Math.round(m.speed)} tokens/s · ${m.speed_label}${source}`);
}

// ---------- test de performance : les IA locales se choisissent après l'installation, d'après la vitesse mesurée ----------
function renderBench() {
  const res = reco.benchmark;
  const ready = reco.ollama_running;
  $("bench-run").disabled = !ready;
  $("ollama-install").hidden = ready || reco.ollama_installed || !reco.ollama_installer;
  if (!ready) {
    $("bench-state").textContent = reco.ollama_installed
      ? "Ollama est installé mais arrêté : démarrez-le (ci-dessous), puis lancez le test."
      : reco.ollama_installer
        ? "Aucune IA locale n'est installée. Installez Ollama (≈ 1,5 Go), qui fait tourner les modèles sur ce PC, puis testez ses performances."
        : "Aucune IA locale : installez Ollama (https://ollama.com/download), puis testez les performances de ce PC.";
    return;
  }
  $("bench-state").textContent = res
    ? `Testé le ${new Date(res.at * 1000).toLocaleDateString()} : ${res.gpu.bandwidth_gbs} Go/s sur la carte graphique, ` +
      `${res.cpu.bandwidth_gbs} Go/s sur le processeur. Vitesse estimée de chaque modèle ci-dessous (≥ 10 tokens/s : ` +
      "confortable à lire) ; le choix conseillé est le plus gros modèle qui reste confortable."
    : "Lancez le test (1 à 3 min ; 1,4 Go de petits modèles de test la première fois) pour voir à quelle vitesse " +
      "chaque modèle tournera sur ce PC, puis installez ceux que vous voulez.";
}

async function poll(url, onTick, every = 2000) {
  for (;;) {
    await new Promise((ok) => setTimeout(ok, every));
    const r = await api(url);
    if (!r.ok) return r;
    if (r.data.state !== "running") return r;
    onTick(r.data);
  }
}

async function runBench(b) {
  b.disabled = true;
  const r = await api("/v1/admin/benchmark", { method: "POST" });
  if (!r.ok) { b.disabled = false; return message(errorMessage(r)); }
  const done = await poll("/v1/admin/benchmark", (s) => { $("bench-state").textContent = s.step || "Test en cours…"; });
  b.disabled = false;
  if (!done.ok) return message(errorMessage(done));
  if (done.data.state === "error") return message(done.data.error || "Le test n'a pas abouti.");
  message("Test terminé : la vitesse estimée de chaque modèle est affichée.", true);
  loadReco();
}

async function installOllama(b) {
  b.disabled = true;
  const r = await api("/v1/admin/local/ollama/install", { method: "POST" });
  if (!r.ok) { b.disabled = false; return message(errorMessage(r)); }
  $("bench-state").textContent = "Téléchargement et installation d'Ollama (≈ 1,5 Go, quelques minutes)…";
  const done = await poll("/v1/admin/local/ollama/install", () => {}, 3000);
  b.disabled = false;
  if (!done.ok || done.data.state === "error") return message("Ollama n'a pas pu être installé : voir data\\logs\\service.log.");
  message("Ollama est installé : lancez maintenant le test de performance.", true);
  loadReco();
  refreshLocal();
}

function installControl(m) {
  if (m.installed) return el("span", "pill", "installé");
  if (m.fit === "too_big") return el("span", "muted small", "");
  if (!m.disk_ok) return el("span", "muted small", "espace disque insuffisant");
  if (!reco.ollama && !(reco.ollama_installer || reco.ollama_installed)) return el("span", "muted small", "Ollama de ce PC requis");
  return button(reco.ollama ? "Installer" : "Installer + Ollama", "btn-primary", (b) => install(m, b));
}

function renderReco() {
  renderBench();
  const b = reco.budget;
  $("reco-hw").textContent = [
    b.gpu ? `${b.gpu} : ${size(b.vram)} de VRAM utilisable` : "Aucun GPU NVIDIA détecté : les modèles tournent sur le processeur",
    `RAM utilisable ${size(b.ram)}`, `disque libre ${size(b.disk_free)}`, `fenêtre de ${fmt(reco.context)} tokens`,
  ].join(" · ") + (b.vram_busy > 1e9 ? `. En ce moment, ${size(b.vram_busy)} de VRAM sont pris par d'autres ` +
    "applications (navigateur, jeu…) : fermez-les pour que le modèle tienne en VRAM et réponde vite." : "");
  const byName = new Map(reco.models.map((m) => [m.model, m]));
  $("reco-best").replaceChildren(...Object.entries(reco.uses).map(([use, label]) => {
    const li = el("li", "reco-card");
    li.append(el("span", "use", label));
    const m = byName.get(reco.best[use]);
    if (!m) { li.append(el("span", "muted small", "Aucun modèle de ce type ne tient sur cette machine.")); return li; }
    li.append(el("strong", "", m.label), fitChip(m), speedChip(m),
              el("span", "reco-meta", `${m.model} · ${size(m.download)} à télécharger · ${size(m.memory)} en mémoire`),
              installControl(m));
    return li;
  }));
  $("reco-list").replaceChildren(...reco.models.map((m) => {
    const li = el("li", `reco-item ${m.fit}`);
    const info = el("div", "scan-info");
    const uses = m.uses.map((u) => reco.uses[u].toLowerCase()).join(", ");
    info.append(el("strong", "", `${m.label}${m.note ? ` — ${m.note}` : ""}`),
                el("span", "reco-meta", `${m.model} · ${uses} · ${size(m.download)} · ${size(m.memory)} en mémoire`));
    if (m.vllm?.fits_vram) info.append(el("span", "reco-meta", `vLLM : --model ${m.vllm.hf} (bf16, ${size(m.vllm.memory)})`));
    const side = el("div", "pv-head");
    side.append(fitChip(m), speedChip(m), installControl(m));
    li.append(info, side);
    return li;
  }));
  $("pull-suggestions").replaceChildren(...reco.models.filter((m) => m.fit !== "too_big").map((m) =>
    new Option(`${m.label} — ${fitLabel(m.fit).toLowerCase()}, ${size(m.download)}`, m.model)));
}

// ---------- bibliothèque Ollama : tous les modèles, installés ou non (chargée seulement à l'ouverture) ----------
let library = null;

async function loadLibrary() {
  $("library-state").textContent = "Lecture de la bibliothèque d'Ollama…";
  const r = await api("/v1/admin/models/library");
  if (!r.ok) { $("library-state").textContent = errorMessage(r); return; }
  library = r.data;
  renderLibrary();
}

function renderLibrary() {
  const q = $("library-filter").value.trim().toLowerCase();
  const onlyMissing = $("library-missing").checked;
  const rows = library.models.filter((m) => (!onlyMissing || !m.installed) &&
    (!q || `${m.tag} ${m.description} ${m.capabilities.join(" ")}`.toLowerCase().includes(q)));
  const shown = rows.slice(0, 200);
  const when = new Date(library.fetched_at * 1000).toLocaleDateString();
  $("library-state").textContent = `${rows.length} modèle(s) affiché(s)${rows.length > shown.length ? " (200 premiers : affinez le filtre)" : ""}` +
    ` · liste d'ollama.com du ${when}${library.stale ? " (hors ligne : dernière liste connue)" : ""}` +
    (library.ollama_running ? "" : ". Ollama doit tourner sur ce PC pour installer ou mesurer.");
  $("library-list").replaceChildren(...shown.map((m) => {
    const li = el("li", `reco-item ${m.fit}`);
    const info = el("div", "scan-info");
    const caps = el("span", "caps");
    for (const c of m.capabilities) caps.append(el("span", "pill", c));
    info.append(el("strong", "", m.tag), caps,
                el("span", "reco-meta", `${m.description.slice(0, 160)}${m.description.length > 160 ? "…" : ""}`),
                el("span", "reco-meta", `${size(m.download)} à télécharger · ${size(m.memory)} en mémoire`));
    const side = el("div", "pv-head");
    side.append(fitChip(m), speedChip(m), libraryAction(m));
    li.append(info, side);
    return li;
  }));
}

function libraryAction(m) {
  if (!library.ollama_running) return el("span", "muted small", "Ollama requis");
  if (!m.installed) return button("Installer", "btn-primary", () => installFromLibrary(m));
  return button(m.speed_source === "mesuré" ? "Re-mesurer" : "Mesurer la vitesse", "", (e) => measureModel(m, e.currentTarget));
}

async function installFromLibrary(m) {
  if (pulling()) return message("Un téléchargement est déjà en cours.");
  if (!confirm(`Télécharger ${m.tag} ?\n\n${size(m.download)} à télécharger, ${size(library.budget.disk_free)} libres sur le disque.` +
               (m.fit === "offload" ? "\n\nIl ne tient pas entièrement en VRAM : les réponses seront plus lentes." : ""))) return;
  await pullModel(m.tag, library.ollama);
  loadLibrary();
}

async function measureModel(m, b) {
  b.disabled = true;
  const r = await api("/v1/admin/benchmark/model", { method: "POST", body: { model: m.tag } });
  if (!r.ok) { b.disabled = false; return message(errorMessage(r)); }
  const done = await poll("/v1/admin/benchmark/model", (s) => { $("library-state").textContent = s.step || `Mesure de ${m.tag}…`; });
  b.disabled = false;
  if (!done.ok || done.data.state === "error") return message(done.data?.error || errorMessage(done));
  message(`${m.tag} : mesuré sur ce PC.`, true);
  loadLibrary();
}

async function install(m, b) {
  if (pulling()) return message("Un téléchargement est déjà en cours.");
  const slow = m.fit === "offload" ? "\n\nIl ne tient pas entièrement en VRAM : les réponses seront plus lentes." : "";
  const engine = reco.ollama ? "" : reco.ollama_installed
    ? "Ollama, le moteur qui fait tourner les IA sur ce PC, est installé mais arrêté : Lezard IA le démarre d'abord.\n\n"
    : "Vous installez aussi Ollama, le moteur qui fait tourner les IA sur ce PC (≈ 1,5 Go, installateur officiel vérifié par Lezard IA).\n\n";
  if (!confirm(`${engine}Télécharger ${m.label} (${m.model}) ?\n\n${size(m.download)} à télécharger, ` +
               `${size(reco.budget.disk_free)} libres sur le disque.${slow}`)) return;
  if (!reco.ollama) {                                     // Ollama d'abord : installé, démarré, ajouté
    if (b) b.disabled = true;
    const ready = await ensureOllama((step) => { $("bench-state").textContent = step; });
    if (b) b.disabled = false;
    if (ready.error) return message(ready.error);
    reco.ollama = ready.provider;
    refreshLocal();
  }
  pullModel(m.model, reco.ollama);
  $("pull-progress").scrollIntoView({ block: "nearest" });
}

function pull(e) {
  e.preventDefault();
  pullModel($("pull-model").value.trim(), ollama?.name || "ollama");
}

async function pullModel(model, provider) {
  if (pulling()) return message("Un téléchargement est déjà en cours.");
  $("pull-btn").disabled = true;
  $("pull-progress").hidden = false;
  $("pull-bar").style.width = "0%";
  $("pull-status").textContent = "Connexion à Ollama…";
  const failed = await streamPull(model, provider, (ev) => {
    if (ev.pct !== undefined) {
      $("pull-bar").style.width = `${ev.pct}%`;
      $("pull-status").textContent = `Téléchargement : ${ev.pct} % (${size(ev.completed)} / ${size(ev.total)})`;
    } else {
      $("pull-status").textContent = ev.status === "done" ? "Terminé" : ev.status;
    }
  });
  $("pull-btn").disabled = false;
  if (failed) {
    $("pull-progress").hidden = true;
    return message(`Téléchargement de ${model} impossible : ${failed}`);
  }
  $("pull-bar").style.width = "100%";
  message(`${model} est installé et disponible dans le chat (ollama/${model}).`, true);
  $("pull-model").value = "";
  setTimeout(() => { $("pull-progress").hidden = true; }, 1500);
  loadProviders();
  hw?.loadReco();
}

async function deleteModel(model, b) {
  if (!confirm(`Supprimer ${model} d'Ollama ? Il faudra le retélécharger pour l'utiliser.`)) return;
  b.disabled = true;
  const q = new URLSearchParams({ provider: ollama?.name || "ollama" });
  const r = await api(`/v1/admin/ollama/models/${encodeURIComponent(model)}?${q}`, { method: "DELETE" });
  if (!r.ok) { b.disabled = false; return message(errorMessage(r)); }
  message(`${model} supprimé.`, true);
  loadProviders();
}

// ---------- initialisation ----------
function logout() {
  clearKey();
  location.replace("/");
}

async function init() {
  if (!getKey()) return logout();
  const r = await api("/v1/me");
  if (!r.ok) return logout();
  $("plan").textContent = r.data.edition === "gratuit" ? "Open source" : "Accès web";
  initThemePanel($("theme-btn"));
  syncTheme();
  if (r.data.admin_session?.blocked) $("denied").textContent = "Administration depuis Internet désactivée : activez d'abord la double authentification, depuis ce PC ou votre réseau local. Le chat reste utilisable d'ici.";
  if (!r.data.is_admin || r.data.admin_session?.blocked) { $("denied").hidden = false; return; }
  $("content").hidden = false;
  initRemotePanel($("remote-btn"));                    // mêmes icônes que la page Chat
  initPhonePanel($("phone-btn"));
  hw = initHardwareWidget($("hw-widget"), {
    onInstalled: (model) => {
      message(`${model} est installé et disponible dans le chat (ollama/${model}).`, true);
      loadProviders();
    },
    onAddOllama: () => openAdd({ preset: "ollama" }),     // Ollama tourne mais n'est pas encore ajouté
  });
  guardSecret($("add-key"));
  $("add-open").addEventListener("click", () => openAdd());
  $("add-preset").addEventListener("change", applyPreset);
  $("add-name").addEventListener("input", keyHint);
  $("add-hosting").addEventListener("change", keyHint);
  $("add-form").addEventListener("submit", addProvider);
  document.querySelectorAll("dialog [data-close]").forEach((b) =>
    b.addEventListener("click", () => b.closest("dialog").close()));
  $("pull-form").addEventListener("submit", pull);
  $("scan-form").addEventListener("submit", scanNetwork);
  $("refresh").addEventListener("click", () => { message(""); loadProviders(); loadPrices(); refreshEmbeddings(); refreshVoice(); loadTools(); });
  $("mcp-reload").addEventListener("click", (e) => reloadTools(e.currentTarget));
  $("svc-service").addEventListener("change", syncScope);
  document.querySelectorAll("[name=svc-mode]").forEach((r) => r.addEventListener("change", syncRepos));
  $("svc-form").addEventListener("submit", (e) => { e.preventDefault(); addKey($("svc-add")); });
  $("bench-run").addEventListener("click", (e) => runBench(e.currentTarget));
  $("library").addEventListener("toggle", () => { if ($("library").open && !library) loadLibrary(); });
  $("library-filter").addEventListener("input", () => library && renderLibrary());
  $("library-missing").addEventListener("change", () => library && renderLibrary());
  $("ollama-install").addEventListener("click", (e) => installOllama(e.currentTarget));
  $("nc-form").addEventListener("submit", (e) => { e.preventDefault(); saveNextcloud($("nc-save")); });
  $("nc-clear").addEventListener("click", (e) => clearNextcloud(e.currentTarget));
  loadNextcloud();
  loadTools();
  $("price-import").addEventListener("click", (e) => importPrices(e.currentTarget));
  openRules = new URLSearchParams(location.search).get("rules");     // lien direct : /models?rules=groq
  loadProviders();
  loadPrices();
  initEmbeddings();                                    // catégorie « Embeddings »
  initVoice();                                         // catégorie « Voix » : dictée et lecture vocale
}

document.addEventListener("DOMContentLoaded", () => {
  initMobile();                                        // navigation en tiroir sur téléphone (13.5)
  initPwa($("install-btn"));                           // installation en application (13.6)
  initFolds();                                         // panneaux en aperçu, « Afficher plus »
  $("logout").addEventListener("click", logout);
  init();
});
