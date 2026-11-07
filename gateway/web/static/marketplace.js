// Marketplace : page à part de l'application, ouverte aux visiteurs. Modèles Ollama, compétences, composants de
// Lezard IA, serveurs MCP, outils pour agents et applications, avec leur licence et leurs droits d'auteur.
// - Visiteur (sans clé, ou clé sans droits d'administration) : tout le catalogue ; installer demande la connexion.
// - Clé maître : conseils selon ce PC (matériel, test de performance, modèles installés) et installation en un clic.
// - Bundles : plusieurs éléments installés l'un après l'autre ; avec la clé maître, seuls ceux qui conviennent à ce
//   PC (matériel, Claude Code présent…) sont proposés, avec le meilleur modèle de chaque usage pour ce matériel.
// - « Détails » : les commandes qu'ajoute un élément (/web, /pc, /mcp, /watch, gws…), à quoi elles servent, un exemple.
// - Ouverte depuis la marketplace de lezard-ia.fr (bouton « Installer sur mon PC ») : ?install=type:id, et la clé
//   transmise par message entre les deux onglets (jamais dans l'adresse), seulement depuis SITE_ORIGINS.
import { api, clearKey, getKey, saveKey } from "./auth.js";
import { initThemePanel, syncTheme } from "./theme-panel.js";
import { ensureOllama, fmtBytes, fmtMem, pullModel, pulling } from "./ollama-pull.js";

const $ = (id) => document.getElementById(id);
const LOGIN = "/?next=/marketplace";
const SITE_ORIGINS = ["https://lezard-ia.fr", "https://www.lezard-ia.fr"];
const PAGE = 30;                                            // cartes affichées avant « Afficher plus »
const TABS = [["all", "Tout"], ["bundle", "Bundles"], ["component", "Outils"], ["model", "Modèles"],
              ["skill", "Compétences"], ["mcp", "Serveurs MCP"], ["tool", "Outils pour agents"], ["app", "Applications"]];
const KIND_LABELS = { model: "Modèle", skill: "Compétence", component: "Outil", mcp: "Serveur MCP",
                      tool: "Outil pour agents", app: "Application", bundle: "Bundle" };
const ON_THIS_PC = ["component", "mcp", "tool", "app"];     // clé maître : présent ou non sur ce PC, sur la carte et la fiche
const COMMERCIAL = {
  oui: ["Usage commercial permis", "ok"],
  conditions: ["Sous conditions", "warn"],
  non: ["Non commercial", "bad"],
  inconnu: ["À vérifier", "warn"],
};
const FIT = { vram: ["Rapide sur ce PC", "vram"], offload: ["Plus lent (VRAM + RAM)", "offload"], too_big: ["Trop gros pour ce PC", "too_big"] };
// sans carte graphique, « VRAM + RAM » n'a pas de sens : le modèle tourne sur le processeur
const fitLabel = (fit) => (fit === "offload" && reco && !reco.budget?.gpu ? "Plus lent (sur le processeur)" : FIT[fit][0]);
const CAPS = { vision: "images", tools: "outils", thinking: "raisonnement", embedding: "embeddings", cloud: "cloud" };

let admin = false, data = null, library = null, reco = null, chat = null, items = [], tab = "all", shown = PAGE;
const licenses = new Map();                                 // « nom:taille » → licence lue dans le registre d'Ollama
let licenseQueue = [], licenseBusy = 0;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;             // texte brut : jamais interprété
  return e;
}
function button(text, cls, onClick) {
  const b = el("button", `btn ${cls} btn-inline`, text);
  b.type = "button";
  b.addEventListener("click", () => onClick(b));
  return b;
}
function link(text, href) {
  return Object.assign(el("a", "", text), { href, target: "_blank", rel: "noopener noreferrer" });
}
const loginButton = (text = "Se connecter pour installer") => button(text, "btn-primary", () => { location.href = LOGIN; });
function message(text, ok = false) {
  $("msg").textContent = text;
  $("msg").className = `alert${text ? " show" : ""}${ok ? " ok-box" : ""}`;
  if (text) $("msg").scrollIntoView({ block: "nearest" });
}
const size = (n) => fmtBytes(n);

/** Installation en deux clics : « Installer » montre ce qui va se passer (taille, emplacement, licence) et devient
 *  « Confirmer l'installation » ; le second clic lance. « Annuler » revient en arrière. */
/** Dépendances absentes de ce PC, installées d'abord (winget) : nom, taille, licence. */
function depsList(deps) {
  const wrap = el("div", "deps");
  wrap.append(el("strong", "", "Installé aussi, car absent de ce PC :"));
  const ul = el("ul");
  for (const d of deps) {
    const li = el("li", "", `${d.name} (${d.size}, ${d.license})`);
    if (d.note) li.append(el("span", "small muted", ` ${d.note}`));
    ul.append(li);
  }
  wrap.append(ul);
  return wrap;
}

function twoStep(btn, body, facts, run, deps = [], verb = "installer") {
  const box = el("div", "confirm-box");
  box.setAttribute("role", "status");
  box.append(el("strong", "", `Prêt à ${verb}. Vérifiez, puis confirmez :`));
  const dl = el("dl", "confirm-facts");
  for (const [k, v] of facts.filter(([, v]) => v)) dl.append(el("dt", "", k), el("dd", "", v));
  box.append(dl);
  if (deps?.length) box.append(depsList(deps));
  if (verb === "installer") {
    box.append(el("p", "small muted", "En confirmant, vous acceptez la licence indiquée dans cette fenêtre. Tout s'installe "
      + "directement au bon endroit : rien à déplacer ensuite."));
  }
  const label = btn.textContent;
  const cancel = button("Annuler", "btn-ghost", () => done());
  const second = (e) => { e.stopImmediatePropagation(); done(); run(); };   // avant le gestionnaire du premier clic
  function done() {
    box.remove(); cancel.remove();
    btn.textContent = label; btn.classList.remove("is-confirm");
    btn.removeEventListener("click", second, true);
  }
  body.prepend(box);
  box.scrollIntoView({ block: "nearest" });
  btn.textContent = verb === "installer" ? "Confirmer l'installation" : "Confirmer la désinstallation";
  btn.classList.add("is-confirm");
  btn.before(cancel);
  btn.addEventListener("click", second, true);
}
const plain = (s) => s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();

// ---------- licences ----------
function licenseChip(lic) {
  if (!lic) return el("span", "lic lic-wait", "Licence : lecture…");
  const [label, level] = COMMERCIAL[lic.commercial] || COMMERCIAL.inconnu;
  const chip = el("span", `lic lic-${level}`, `${lic.name} · ${label}`);
  chip.title = lic.summary || "";
  return chip;
}
function licenseBlock(lic, { text = null, textLabel = "Texte complet de la licence" } = {}) {
  const box = el("section", "market-license");
  box.append(el("h3", "", "Licence et droits d'auteur"));
  if (!lic) { box.append(el("p", "small muted", "Lecture de la licence…")); return box; }
  const dl = el("dl", "market-facts");
  const row = (k, v) => { if (v) dl.append(el("dt", "", k), el("dd", "", v)); };
  row("Licence", lic.name);
  row("Usage commercial", (COMMERCIAL[lic.commercial] || COMMERCIAL.inconnu)[0]);
  row("Titulaire des droits", lic.copyright || "non indiqué dans le texte");
  row("Ce qu'elle demande", lic.summary);
  box.append(licenseChip(lic), dl);
  const src = lic.source || lic.url;
  if (src) { const p = el("p", "small"); p.append(link("Texte et conditions à la source ↗", src)); box.append(p); }
  if (text) {
    const det = el("details", "market-license-text");
    det.append(el("summary", "", textLabel), el("pre", "", text));
    box.append(det);
  }
  box.append(el("p", "small muted", data?.disclaimer || ""));
  return box;
}

/** Licences des modèles affichés : lues à la demande, 3 à la fois, dans l'ordre des cartes. Chaque affichage
 *  remplace la file (une recherche ne fait pas attendre les cartes qu'on vient de quitter). */
function wantLicenses(keys) {
  licenseQueue = keys.filter((k) => !licenses.has(k));
  pumpLicenses();
}
async function pumpLicenses() {
  while (licenseBusy < 3 && licenseQueue.length) {
    const key = licenseQueue.shift();
    if (licenses.has(key) || !document.querySelector(`[data-license="${CSS.escape(key)}"]`)) continue;   // plus affichée
    licenseBusy += 1;
    fetchLicense(key).finally(() => { licenseBusy -= 1; pumpLicenses(); });
  }
}
async function fetchLicense(key) {
  const [name, tag] = key.split(":");
  const base = admin ? "/v1/admin/marketplace" : "/v1/marketplace";
  const r = await api(`${base}/models/${encodeURIComponent(name)}/license?tag=${encodeURIComponent(tag)}`, { key: admin ? undefined : null });
  if (r.status === 429) return null;                        // limite des visiteurs : relue à l'affichage suivant
  const lic = r.ok ? r.data : { name: "Illisible", commercial: "inconnu",
    summary: r.data?.error?.message || "Registre d'Ollama injoignable : réessayez plus tard." };
  licenses.set(key, lic);
  document.querySelectorAll(`[data-license="${CSS.escape(key)}"]`).forEach((n) => n.replaceWith(licenseFor(key)));
  return lic;
}
function licenseFor(key) {
  const chip = licenseChip(licenses.get(key));
  if (!licenses.has(key)) chip.dataset.license = key;
  return chip;
}

// ---------- catalogue unifié ----------
/** Modèles regroupés par nom : la liste publique l'est déjà ; celle de la clé maître a une ligne par taille. */
function groupedModels() {
  if (!library) return [];
  if (library.models[0]?.sizes) return library.models;
  const byName = new Map();
  for (const row of library.models) {
    if (!byName.has(row.name)) byName.set(row.name, { name: row.name, description: row.description, capabilities: row.capabilities, sizes: [] });
    byName.get(row.name).sizes.push(row);
  }
  return [...byName.values()];
}
function modelItems() {
  const best = new Set(Object.values(reco?.best || {}).filter(Boolean).map((t) => t.split(":")[0]));
  return groupedModels().map((m) => {
    const fast = m.sizes.filter((s) => s.fit === "vram"), slow = m.sizes.filter((s) => s.fit === "offload");
    // taille conseillée : la plus grosse qui tient en VRAM, sinon la plus petite en VRAM + RAM
    const pick = admin ? (fast.length ? fast[fast.length - 1] : slow[0] || null) : null;
    const tag = admin ? (pick || m.sizes[0])?.tag.split(":")[1] || "latest" : "latest";
    return {
      kind: "model", id: m.name, name: m.name, description: m.description || "",
      tags: m.capabilities.map((c) => CAPS[c] || c),
      installed: m.sizes.some((s) => s.installed), fit: admin ? (pick ? pick.fit : "too_big") : null, pick,
      recommended: best.has(m.name), licenseKey: `${m.name}:${tag}`, model: m,
    };
  });
}
function otherItems() {
  if (!data) return [];
  const active = new Set(chat?.skills || []);
  return [
    ...data.skills.map((s) => ({ kind: "skill", id: s.id, name: s.name, description: s.description, tags: [s.group],
      installed: active.has(s.id), license: { name: s.license, commercial: "oui", copyright: s.source, url: s.url,
        summary: "Adaptation française condensée, fournie avec Lezard IA ; licence d'origine jointe." }, skill: s })),
    ...data.components.map((c) => ({ kind: "component", id: c.id, name: c.name, description: c.description,
      tags: [c.size], installed: !!c.installed, license: c.licenses[0] || null, component: c })),
    ...data.mcp.map((s) => ({ kind: "mcp", id: s.id, name: s.name, description: s.description,
      tags: [`${s.package}@${s.version}`], installed: !!s.installed, license: s.license, server: s })),
    ...data.agent_tools.map((t) => ({ kind: "tool", id: t.id, name: t.name, description: t.description,
      tags: [], installed: !!t.installed, license: t.license, tool: t })),
    ...data.apps.map((a) => ({ kind: "app", id: a.id, name: a.name, description: a.description, tags: [],
      installed: !!a.installed, license: a.license, app: a })),
  ];
}
const fullTag = (t) => (t.includes(":") ? t : `${t}:latest`);   // « nomic-embed-text » → « …:latest »

/** Un bundle et ses éléments, retrouvés dans le catalogue ; un modèle y est le meilleur de son usage pour ce PC. */
function bundleItems() {
  return (data?.bundles || []).map((b) => {
    const parts = b.parts.map((p) => {
      const it = items.find((x) => x.kind === p.kind && x.id === p.id) || null;
      const installed = p.kind !== "model" ? !!it?.installed
        : !!(reco?.models.find((m) => m.model === p.tag)?.installed || it?.model.sizes.find((s) => s.tag === fullTag(p.tag))?.installed);
      return { ...p, it, installed };
    });
    const count = parts.length;
    return { kind: "bundle", id: b.id, name: b.name, description: b.description,
      tags: [`${count} élément${count > 1 ? "s" : ""}`], installed: admin && parts.every((x) => x.installed),
      recommended: admin && b.suggested, hidden: admin && !b.suggested, bundle: { ...b, parts } };
  });
}
function rebuild() {
  items = [...otherItems(), ...modelItems()];
  items = [...bundleItems(), ...items];
}

function visible() {
  const q = plain($("search").value.trim());
  const fitOnly = admin && $("fit-only").checked;
  return items.filter((it) => (tab === "all" || it.kind === tab) && !it.hidden   // bundle pas fait pour ce PC
    && (!fitOnly || it.kind !== "model" || it.fit !== "too_big")
    && (!q || plain([it.name, it.description, ...it.tags.filter(Boolean), it.license?.name || licenses.get(it.licenseKey)?.name || ""].join(" ")).includes(q)))
    .sort(order());
}

/** Ordre choisi (menu « Trier par ») ; sans étoiles (modèles, fonctions de Lezard IA) : toujours à la fin. */
function order() {
  const how = $("sort").value;
  const stars = (it) => entryOf(it)?.stars_count ?? null;
  const byName = (a, b) => a.name.localeCompare(b.name, "fr", { sensitivity: "base" });
  if (how === "name") return byName;
  if (how === "name-desc") return (a, b) => byName(b, a);
  if (how === "stars-desc" || how === "stars-asc") {
    const dir = how === "stars-desc" ? -1 : 1;
    return (a, b) => {
      const x = stars(a), y = stars(b);
      if (x === null || y === null) return (x === null) - (y === null) || byName(a, b);
      return dir * (x - y) || byName(a, b);
    };
  }
  return (a, b) => (b.recommended - a.recommended) || (b.installed - a.installed) || 0;
}

// ---------- rendu ----------
function statusChip(it) {
  if (it.kind === "bundle") {
    const parts = it.bundle.parts, done = parts.filter((p) => p.installed).length;
    if (!admin) return el("span", "pill", "Un clic");
    return el("span", `pill ${it.installed ? "on-pill" : ""}`, it.installed ? "Installé" : done ? `${done} / ${parts.length} installés` : "Un clic");
  }
  if (it.kind === "skill" && admin) return el("span", `pill ${it.installed ? "on-pill" : ""}`, it.installed ? "Active" : "Désactivée");
  if (it.installed) return el("span", "pill on-pill", it.kind === "mcp" ? (it.server.connected ? "Connecté" : "Déclaré") : "Installé");
  if (it.kind === "model" && it.fit) return el("span", `fit ${FIT[it.fit][1]}`, fitLabel(it.fit));
  const entry = it.tool || it.app;
  if (admin && entry && !entry.one_click) return el("span", "pill", "À brancher");
  return el("span", "pill", admin && ON_THIS_PC.includes(it.kind) ? "Non installé" : "Un clic");
}
function card(it) {
  const li = el("li", `market-card kind-${it.kind}${it.recommended ? " is-reco" : ""}`);
  const head = el("div", "mc-head");
  head.append(el("span", "mc-kind", KIND_LABELS[it.kind]), statusChip(it));
  if (it.recommended) head.append(el("span", "pill reco-pill", "Conseillé"));
  li.append(head, el("h3", "mc-name", it.name), el("p", "mc-desc", it.description));
  if (it.tags.filter(Boolean).length) li.append(el("p", "mc-tags", it.tags.filter(Boolean).join(" · ")));
  const gh = entryOf(it);
  if (gh?.stars) li.append(starsLine(gh, "mc-stars"));
  if (it.kind === "model" && it.pick) {
    li.append(el("p", "mc-meta", `Taille conseillée : ${it.pick.tag.split(":")[1]} · ${size(it.pick.download)} · ${size(it.pick.memory)} en mémoire`
      + (it.pick.speed_label ? ` · ${it.pick.speed_label}` : "")));
  } else if (it.kind === "model") {
    li.append(el("p", "mc-meta", `Tailles : ${it.model.sizes.map((s) => s.tag.split(":")[1]).join(", ")}`));
  }
  if (it.kind === "bundle") li.append(el("p", "mc-meta", it.bundle.parts.map(partName).join(" · ")));
  li.append(it.kind === "model" ? licenseFor(it.licenseKey)
    : it.kind === "bundle" ? el("span", "lic lic-wait", "Licence de chaque élément dans les détails")
    : licenseChip(it.license || { name: "Aucune licence tierce", commercial: "oui", summary: "Fonction de Lezard IA." }));
  li.append(button("Détails et licence", "btn-ghost mc-open", () => openDetail(it)));
  return li;
}
function render() {
  const list = visible();
  const hidden = (tab === "all" || tab === "bundle") ? items.filter((it) => it.hidden).length : 0;
  $("count").textContent = `${list.length} élément${list.length > 1 ? "s" : ""}`
    + (hidden ? ` · ${hidden} bundle${hidden > 1 ? "s" : ""} non proposé${hidden > 1 ? "s" : ""} : pas adapté${hidden > 1 ? "s" : ""} à ce PC` : "")
    + (library?.stale ? " · liste des modèles d'Ollama : dernière lecture (ollama.com injoignable)" : "");
  const page = list.slice(0, shown);
  $("grid").replaceChildren(...page.map(card));
  wantLicenses(page.filter((it) => it.kind === "model").map((it) => it.licenseKey));
  $("more").hidden = list.length <= shown;
  for (const b of $("tabs").children) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
}

function renderReco() {
  const box = $("mr-list");
  $("reco-login").hidden = admin;
  if (!admin) {
    $("mr-hw").textContent = "Connectez-vous avec la clé maître de votre Lezard IA : la marketplace lit alors la carte "
      + "graphique, la mémoire, le disque et les modèles déjà installés de votre PC, et vous dit quoi installer pour chaque usage.";
    box.replaceChildren();
    return;
  }
  if (!reco) { $("mr-hw").textContent = "Matériel non lu : la page Modèles détaille ce PC."; box.replaceChildren(); return; }
  const b = reco.budget;
  $("mr-hw").textContent = [
    b.gpu ? `${b.gpu} : ${fmtMem(b.vram)} de VRAM` : "Pas de carte graphique dédiée : les modèles tournent sur le processeur",
    `RAM ${size(b.ram)}`, `disque libre ${size(b.disk_free)}`,
    reco.benchmark ? "vitesses mesurées par le test de performance" : "vitesses estimées (test de performance : page Modèles)",
  ].join(" · ");
  const byTag = new Map(reco.models.map((m) => [m.model, m]));
  const cards = Object.entries(reco.uses).map(([use, label]) => {
    const li = el("li", "mr-card");
    li.append(el("span", "mr-use", label));
    const m = byTag.get(reco.best[use]);
    if (!m) { li.append(el("span", "small muted", "Aucun modèle de ce type ne tient sur ce PC.")); return li; }
    const it = items.find((x) => x.kind === "model" && x.id === m.model.split(":")[0]);
    li.append(el("strong", "", m.label), el("span", "small muted", `${m.model} · ${size(m.download)}` + (m.speed_label ? ` · ${m.speed_label}` : "")));
    li.append(m.installed ? el("span", "pill on-pill", "Installé")
      : it ? button("Voir et installer", "btn-primary", () => openDetail(it, m.model)) : el("span"));
    return li;
  });
  // manques : un usage sans aucun modèle installé
  const installedUses = new Set(reco.models.filter((m) => m.installed).flatMap((m) => m.uses));
  const missing = Object.keys(reco.uses).filter((u) => reco.best[u] && !installedUses.has(u));
  if (missing.length) {
    const li = el("li", "mr-card mr-gap");
    li.append(el("span", "mr-use", "À compléter"),
      el("span", "small", `Aucun modèle installé pour : ${missing.map((u) => reco.uses[u].toLowerCase()).join(", ")}.`));
    cards.unshift(li);
  }
  if (!data.components.find((c) => c.id === "ollama")?.installed && !reco.ollama) {
    const li = el("li", "mr-card mr-gap");
    li.append(el("span", "mr-use", "À installer d'abord"), el("strong", "", "Ollama"),
      el("span", "small", "Le moteur des modèles locaux. Ou cliquez directement sur « Installer » d'un modèle : Ollama "
        + "s'installe avec lui."));
    if (ollamaInstallable()) {
      li.append(button(reco.ollama_installed ? "Démarrer Ollama" : "Installer Ollama", "btn-primary", async (b) => {
        b.disabled = true;
        const ready = await ensureOllama((step) => { b.textContent = step; });
        if (ready.error) { b.disabled = false; b.textContent = "Réessayer"; return message(ready.error); }
        message("Ollama est prêt : choisissez maintenant un modèle.", true);
        await load(false);
      }));
    }
    cards.unshift(li);
  }
  for (const it of items.filter((x) => x.kind === "bundle" && x.recommended && !x.installed).slice(0, 3)) {
    const li = el("li", "mr-card");
    li.append(el("span", "mr-use", "Bundle conseillé"), el("strong", "", it.name),
      el("span", "small muted", it.bundle.parts.map(partName).join(" · ")),
      button("Voir le bundle", "btn-primary", () => openDetail(it)));
    cards.push(li);
  }
  box.replaceChildren(...cards);
}

// ---------- fenêtre de détails ----------
/** L'élément du catalogue derrière une carte (outil, application, serveur, compétence, composant). */
const entryOf = (it) => it.tool || it.app || it.server || it.skill || it.component || null;

/** Commandes ajoutées par un élément : celles du catalogue, ou /competence pour une compétence. */
function commandsOf(it) {
  if (it.kind === "skill") {
    return [{ use: `/competence ${it.id}`, where: "Chat de Lezard IA", example: null,
      what: "Active ou coupe cette compétence dans le chat (4 actives au plus). /competence seul : la liste ; "
        + "/competence off : toutes coupées." }];
  }
  return entryOf(it)?.commands || [];
}
function commandsBlock(cmds, title = "Commandes ajoutées") {
  const box = el("section", "market-cmds");
  box.append(el("h3", "", title));
  const dl = el("dl");
  for (const c of cmds) {
    const dt = el("dt");
    dt.append(el("code", "", c.use), el("span", "pill", c.where));
    const dd = el("dd", "", c.what);
    if (c.example) {
      const ex = el("p", "small");
      ex.append("Exemple : ", el("code", "", c.example));
      dd.append(ex);
    }
    dl.append(dt, dd);
  }
  box.append(dl);
  return box;
}
const DATE_FR = (d) => new Date(d).toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" });
function starsLine(entry, cls, long = false) {
  const p = el("p", cls);
  const a = link(`★ ${entry.stars}${long ? " étoiles" : ""} sur GitHub`, `https://github.com/${entry.github}`);
  a.title = `Relevé du ${DATE_FR(entry.stars_date)}`;
  p.append(a);
  if (long) p.append(` (relevé du ${DATE_FR(entry.stars_date)} : une mesure de popularité, pas un gage de qualité ni de sécurité)`);
  return p;
}

function openDetail(it, tag = null) {
  $("d-title").textContent = it.name;
  const body = $("d-body"), foot = $("d-foot");
  body.replaceChildren(el("p", "mc-kind", KIND_LABELS[it.kind]), el("p", "", it.description));
  if (admin && ON_THIS_PC.includes(it.kind)) {
    const here = el("p", "small", "Sur ce PC : ");
    here.append(statusChip(it));
    body.append(here);
  }
  if (entryOf(it)?.stars) body.append(starsLine(entryOf(it), "small muted", true));
  if (commandsOf(it).length) body.append(commandsBlock(commandsOf(it)));
  foot.replaceChildren(button("Fermer", "btn-ghost", () => $("detail").close()));
  ({ model: modelDetail, skill: skillDetail, component: componentDetail, mcp: mcpDetail, tool: toolDetail,
     app: appDetail, bundle: bundleDetail })[it.kind](it, body, foot, tag);
  if (!$("detail").open) $("detail").showModal();
}

function progressBox() {
  const box = el("div", "pull-progress");
  const bar = el("div", "bar"), fill = el("span");
  bar.append(fill);
  const status = el("span", "small");
  status.setAttribute("aria-live", "polite");
  box.append(bar, status);
  return { box, set: (pct, text) => { fill.style.width = `${pct}%`; status.textContent = text; } };
}

function modelDetail(it, body, foot, focus) {
  const m = it.model;
  if (m.capabilities.length) body.append(el("p", "mc-tags", `Sait faire : ${m.capabilities.map((c) => CAPS[c] || c).join(", ")}`));
  body.append(el("h3", "", admin ? "Tailles et adéquation à ce PC" : "Tailles disponibles"));
  const table = el("table", "market-sizes");
  const head = el("tr");
  for (const h of admin ? ["Taille", "Mémoire", "Téléchargement", "Vitesse", "Sur ce PC", ""] : ["Taille", "Paramètres", "Téléchargement"]) {
    head.append(el("th", "", h));
  }
  table.append(head);
  const ollama = reco?.ollama || library?.ollama;
  for (const s of m.sizes) {
    const tr = el("tr", s.tag === focus || s === it.pick ? "is-pick" : "");
    tr.dataset.tag = s.tag;
    if (!admin) {
      tr.append(el("td", "", s.tag.split(":")[1]), el("td", "", `${String(s.params_b).replace(".", ",")} milliards`),
                el("td", "", `~${size(s.download)}`));
      table.append(tr);
      continue;
    }
    tr.append(el("td", "", s.tag.split(":")[1]), el("td", "", size(s.memory)), el("td", "", `~${size(s.download)}`),
              el("td", "", s.speed_label || "–"));
    const fitCell = el("td");
    fitCell.append(el("span", `fit ${FIT[s.fit][1]}`, fitLabel(s.fit)));
    const act = el("td");
    if (s.installed) {
      act.append(button("Supprimer", "btn-ghost", (b) => removeModel(s.tag, ollama, b)));
    } else if (!ollama && !ollamaInstallable()) {
      act.append(el("span", "small muted", "Ollama requis"));
    } else {
      act.append(button(ollama ? "Installer" : "Installer + Ollama", s.fit === "too_big" ? "btn-ghost" : "btn-primary", (b) => installModel(s, ollama, b, body)));
    }
    tr.append(fitCell, act);
    table.append(tr);
  }
  const wrap = el("div", "market-table");
  wrap.append(table);
  body.append(wrap, el("p", "small muted", admin
    ? "Mémoire et téléchargement estimés (quantification Q4_K_M) ; la vitesse est "
      + (reco?.benchmark ? "mesurée par le test de performance de ce PC." : "estimée : lancez le test de performance (page Modèles) pour la mesurer.")
    : "Téléchargement estimé (quantification Q4_K_M). Connecté avec la clé maître, vous voyez lesquelles tiennent sur votre PC et à quelle vitesse."));
  const key = focus || it.licenseKey;
  const lic = licenses.get(key) || licenses.get(it.licenseKey);
  const slot = el("div");
  body.append(slot);
  const show = (l) => slot.replaceChildren(licenseBlock(l, { text: l?.text }));
  show(lic);
  if (!lic?.text) fetchLicense(key).then((l) => l && show(l));
  const p = el("p", "small");
  p.append(link(`Page du modèle sur ollama.com ↗`, `https://ollama.com/library/${encodeURIComponent(m.name)}`));
  body.append(p);
  if (!admin) foot.prepend(loginButton());
}

/** Ollama absent ou arrêté, mais Lezard IA sait l'installer et le démarrer : le modèle s'installe quand même en un clic. */
function ollamaInstallable() {
  return !!(reco && (reco.ollama_installer || reco.ollama_installed));
}

function installModel(s, provider, btn, body) {
  if (pulling()) return message("Un téléchargement est déjà en cours.");
  const fit = s.fit === "too_big" ? "Trop gros pour ce PC : il risque de ne pas démarrer ou d'être très lent."
    : s.fit === "offload" ? "Ne tient pas entièrement dans la carte graphique : il répondra plus lentement." : "Rapide sur ce PC.";
  const withOllama = !provider;
  twoStep(btn, body, [
    ["Vous installez", withOllama ? `Ollama, le moteur qui fait tourner les IA sur ce PC, puis le modèle ${s.tag}` : `Le modèle ${s.tag}`],
    ["Ollama", withOllama ? (reco?.ollama_installed ? "Déjà sur ce PC : Lezard IA le démarre et l'ajoute"
      : "≈ 1,5 Go, installateur officiel vérifié par Lezard IA, lancé par Lezard IA") : null],
    ["Modèle", `environ ${size(s.download)} à télécharger`],
    ["Espace libre", library.budget?.disk_free ? size(library.budget.disk_free) : null],
    ["Emplacement", "Dans Ollama, sur ce PC : disponible aussitôt dans le chat"], ["Sur ce PC", fit]],
    () => startModel(s, provider, btn, body));
}

async function startModel(s, provider, btn, body) {
  btn.disabled = true;
  const prog = progressBox();
  body.prepend(prog.box);
  if (!provider) {                                          // Ollama d'abord : installé, démarré, ajouté
    prog.set(5, "Préparation d'Ollama…");
    const ready = await ensureOllama((step) => prog.set(10, step));
    if (ready.error) { prog.set(0, ready.error); btn.disabled = false; return; }
    provider = ready.provider;
    if (reco) reco.ollama = provider;
  }
  prog.set(0, "Connexion à Ollama…");
  const failed = await pullModel(s.tag, provider, (ev) => ev.pct !== undefined
    ? prog.set(ev.pct, `Téléchargement : ${ev.pct} % (${size(ev.completed)} / ${size(ev.total)})`)
    : prog.set(100, ev.status === "done" ? "Terminé" : ev.status));
  if (failed) { prog.set(0, failed); btn.disabled = false; return; }
  prog.set(100, `${s.tag} est installé : il est disponible dans le chat.`);
  message(`${s.tag} est installé et disponible dans le chat.`, true);
  await load(false);
}

async function removeModel(tag, provider, btn) {
  if (!confirm(`Supprimer ${tag} de ce PC ? Il faudra le retélécharger pour le réutiliser.`)) return;
  btn.disabled = true;
  const r = await api(`/v1/admin/ollama/models/${encodeURIComponent(tag)}?provider=${encodeURIComponent(provider)}`, { method: "DELETE" });
  if (!r.ok) { btn.disabled = false; return message(r.data?.error?.message || "Suppression impossible."); }
  message(`${tag} supprimé.`, true);
  $("detail").close();
  await load(false);
}

function skillDetail(it, body, foot) {
  const s = it.skill;
  body.append(el("p", "small muted", `${s.group} · environ ${s.tokens} tokens ajoutés à chaque message tant qu'elle est active.`));
  const content = el("details", "market-license-text");
  content.append(el("summary", "", "Consignes envoyées à l'IA"), el("pre", "", "Lecture…"));
  body.append(content);
  const slot = el("div");
  body.append(slot);
  slot.append(licenseBlock(it.license));
  api(`/v1/marketplace/skills/${encodeURIComponent(s.id)}`, { key: null }).then((r) => {
    content.querySelector("pre").textContent = r.ok ? r.data.content : "Illisible.";
    if (r.ok) {
      slot.replaceChildren(licenseBlock({ ...it.license, source: s.url }, { text: `${r.data.notice}\n\n==========\n\n${r.data.license_text}`,
        textLabel: "Origine, modifications et texte de la licence" }));
    }
  });
  foot.prepend(!admin ? loginButton("Se connecter pour l'activer")
    : button(it.installed ? "Désactiver" : "Activer dans le chat", it.installed ? "btn-ghost" : "btn-primary", (b) => toggleSkill(it, b)));
}

async function toggleSkill(it, btn) {
  const on = !it.installed;
  const next = on ? [...new Set([...(chat.skills || []), it.id])] : (chat.skills || []).filter((x) => x !== it.id);
  btn.disabled = true;
  const r = await api("/v1/preferences/chat", { method: "PUT", body: { ...chat, skills: next } });
  btn.disabled = false;
  if (!r.ok) return message(r.data?.error?.message || "Enregistrement impossible.");
  chat = r.data.chat;
  try {   // un chat ouvert dans un autre onglet relit ses réglages : événement « storage »
    localStorage.setItem("gw.chat.settings", JSON.stringify({ ...chat, skills: chat.skills || [], model: chat.model || "", updated_at: r.data.updated_at }));
  } catch { /* stockage indisponible : le chat relira le compte au prochain chargement */ }
  message(on ? `${it.name} est active dans le chat.` : `${it.name} est désactivée.`, true);
  rebuild(); render();
  $("detail").close();
}

/** Recherche sur Internet : plusieurs éléments proches, comparés (coût, ce qui part de ce PC, point fort). */
function webSearchCompare(it, body) {
  const rows = data?.web_search || [];
  if (!rows.some((r) => r.key === `${it.kind}:${it.id}`)) return;
  const d = el("details", "compare");
  d.append(el("summary", "", "Quelle recherche sur Internet choisir ?"));
  const table = el("table", "compare-table");
  const head = el("tr");
  for (const h of ["", "Coût", "Ce qui part de ce PC", "Point fort"]) head.append(el("th", "", h));
  table.append(head);
  for (const r of rows) {
    const tr = el("tr", r.key === `${it.kind}:${it.id}` ? "is-current" : "");
    tr.append(el("th", "", r.name), el("td", "", r.cost), el("td", "", r.sends), el("td", "", r.best));
    table.append(tr);
  }
  d.append(table, el("p", "small muted", "Une seule suffit en général. Offres relevées sur le site de chaque service, susceptibles de changer."));
  body.append(d);
}

function componentDetail(it, body, foot) {
  const c = it.component;
  body.append(el("p", "small muted", `Taille : ${c.size}.${c.where ? ` Emplacement : ${c.where}.` : ""}${c.restart ? " Prise en compte après un redémarrage de Lezard IA." : ""}`));
  webSearchCompare(it, body);
  if (c.licenses.length) for (const l of c.licenses) body.append(licenseBlock(l));
  else body.append(licenseBlock({ name: "Fonction de Lezard IA", commercial: "oui", copyright: "Lezard IA", summary: "Aucun logiciel tiers à télécharger." }));
  if (!admin) { foot.prepend(loginButton()); return; }
  if (c.installed) return;
  if (!c.installable) {
    body.append(el("p", "alert show warn-box", c.missing?.length ? `Requis sur ce PC : ${c.missing.join(", ")}.`
      : c.manual || "Installation non disponible sur ce système."));
    return;
  }
  foot.prepend(button("Installer", "btn-primary", (b) => installComponent(c, b, body)));
}

function installComponent(c, btn, body) {
  twoStep(btn, body, [["Composant", c.name], ["Taille", c.size], ["Emplacement", c.where],
    ["Ensuite", c.restart ? "Redémarrage de Lezard IA (un bouton vous le proposera)" : null]],
    () => runInstall(`/v1/admin/marketplace/components/${encodeURIComponent(c.id)}/install`, btn, body), c.deps);
}

/** Lance une installation en un clic et suit son journal jusqu'au bout (une installation à la fois). */
async function runInstall(url, btn, body) {
  btn.disabled = true;
  const r = await api(url, { method: "POST" });
  if (!r.ok) { btn.disabled = false; return message(r.data?.error?.message || "Installation impossible."); }
  await follow(r, btn, body);
}

const UNINSTALL = { doing: "Désinstallation en cours…", failed: "Échec de la désinstallation", done: "Désinstallé." };

/** Suit une installation (ou une désinstallation) lancée — journal, avancement — jusqu'au bout. */
async function follow(r, btn, body, words = { doing: "Installation en cours…", failed: "Échec de l'installation", done: "Installé." }) {
  const prog = progressBox();
  const log = el("pre", "guide-code market-log");
  log.hidden = true;
  body.prepend(prog.box, log);
  prog.set(30, `${words.doing} (cela peut prendre plusieurs minutes)`);
  let s = { data: { state: r.data.state, restart: r.data.restart } };
  while (s.data.state === "running") {
    await new Promise((ok) => setTimeout(ok, 2000));
    const next = await api("/v1/admin/marketplace/job");
    if (!next.ok) continue;
    s = next;
    if (s.data.log?.length) { log.hidden = false; log.textContent = s.data.log.join("\n"); log.scrollTop = log.scrollHeight; }
  }
  if (s.data.state !== "done") {
    prog.set(0, `${words.failed} (code ${s.data.code}) : le journal est affiché ci-dessous (data/logs/marketplace.log).`);
    btn.disabled = false;
    return;
  }
  prog.set(100, (s.data.restart ? `${words.done} Redémarrez Lezard IA pour l'activer.` : words.done) + (s.data.after ? ` ${s.data.after}` : ""));
  if (s.data.restart) {
    btn.replaceWith(button("Redémarrer Lezard IA", "btn-primary", async (b) => {
      b.disabled = true;
      const x = await api("/v1/admin/restart", { method: "POST" });
      message(x.ok ? "Redémarrage… la page se rechargera dans quelques secondes." : x.data?.error?.message || "Redémarrage impossible.", x.ok);
      if (x.ok) setTimeout(() => location.reload(), 8000);
    }));
  } else {
    btn.remove();
  }
  await load(false);
}

/** Outil ou application : les étapes figées, puis « Installer » (clé maître) ou la connexion (visiteur). */
function oneClick(it, entry, body, foot, route) {
  if (!admin) { foot.prepend(loginButton()); return; }
  if (!entry.one_click) return;
  body.append(el("h3", "", "Installation"));
  if (entry.size || entry.where) {
    const dl = el("dl", "confirm-facts");
    if (entry.size) dl.append(el("dt", "", "Taille"), el("dd", "", entry.size));
    if (entry.where) dl.append(el("dt", "", "Emplacement"), el("dd", "", entry.where));
    body.append(dl);
  }
  const pre = el("pre", "guide-code");
  pre.textContent = entry.steps.join("\n");
  body.append(el("p", "small muted", "Lezard IA lance ces commandes sur ce PC, sans rien d'autre :"), pre);
  if (entry.after) body.append(el("p", "small", `Ensuite : ${entry.after}`));
  if (entry.deps?.length) body.append(depsList(entry.deps));
  if (entry.missing.length) {
    body.append(el("p", "alert show warn-box", `Requis sur ce PC : ${entry.missing.join(", ")}. Installez-le, puis revenez ici.`));
    return;
  }
  foot.prepend(button(entry.installed ? "Réinstaller" : "Installer", entry.installed ? "btn-ghost" : "btn-primary", (b) => {
    twoStep(b, body, [["Élément", it.name], ["Taille", entry.size], ["Emplacement", entry.where]],
      () => runInstall(`/v1/admin/marketplace/${route}/${encodeURIComponent(it.id)}/install`, b, body), entry.deps);
  }));
  if (entry.installed) uninstallButton(it, entry, body, foot, route);
}

/** Désinstaller ce qui est sur ce PC : étapes inverses de la recette, ou comment le faire soi-même. */
function uninstallButton(it, entry, body, foot, route) {
  if (!entry.uninstall?.length) {
    if (entry.uninstall_hint) body.append(el("p", "small muted", `Pour le désinstaller : ${entry.uninstall_hint}`));
    return;
  }
  foot.prepend(button("Désinstaller", "btn-ghost", (b) => {
    twoStep(b, body, [["Élément", it.name], ["Commandes", entry.uninstall.join(" ; ")]], async () => {
      b.disabled = true;
      const r = await api(`/v1/admin/marketplace/${route}/${encodeURIComponent(it.id)}/uninstall`, { method: "POST" });
      if (!r.ok) { b.disabled = false; return message(r.data?.error?.message || "Désinstallation impossible."); }
      await follow(r, b, body, UNINSTALL);
    }, [], "désinstaller");
  }));
}

function mcpDetail(it, body, foot) {
  const s = it.server;
  body.append(el("p", "small muted", `Paquet npm ${s.package}, version figée ${s.version}, lancé par npx sur ce PC. `
    + "Chaque appel d'outil demande votre accord dans le chat (commande /mcp)."));
  if (s.service) body.append(el("p", "alert show warn-box", s.service));
  webSearchCompare(it, body);
  body.append(licenseBlock(s.license));
  if (!admin) { foot.prepend(loginButton()); return; }
  if (s.installed) {
    const use = el("p", "small", "Dans le chat : ");
    use.append(el("code", "", `/mcp ${s.id}`), " explique ses outils ; ", el("code", "", `/mcp ${s.id} <demande>`), " n'utilise que lui.");
    body.append(use);
    if (s.error) body.append(el("p", "alert show warn-box", `Non connecté : ${s.error}`));
    foot.prepend(button("Retirer", "btn-ghost", (b) => removeMcp(s, b)));
    return;
  }
  if (!data.node && !s.deps?.length) { body.append(el("p", "alert show warn-box", "Node.js est requis : installez-le depuis nodejs.org, puis revenez ici.")); return; }
  if (s.deps?.length) body.append(depsList(s.deps));
  let folder = null, secret = null;
  if (s.needs_folder) {
    const label = el("label", "", "Dossier auquel l'IA aura accès (chemin complet)");
    folder = Object.assign(el("input"), { placeholder: "C:\\Users\\vous\\Documents", spellcheck: false });
    label.append(folder);
    body.append(label);
  }
  if (s.secret) {
    const label = el("label", "", s.secret.label);
    secret = Object.assign(el("input"), { type: "password", autocomplete: "off", spellcheck: false });
    label.append(secret);
    const hint = el("p", "small muted");
    hint.append("À créer sur ", link(s.secret.url.replace("https://", ""), s.secret.url),
                ". Elle reste sur ce PC (data/mcp.json) et n'est jamais réaffichée.");
    body.append(label, hint);
  }
  foot.prepend(button("Installer", "btn-primary", (b) => twoStep(b, body, [["Serveur MCP", s.name], ["Taille", s.size],
    ["Emplacement", "Déclaré dans Lezard IA (data/mcp.json), téléchargé par npm et lancé par Lezard IA"]],
    () => addMcp(s, { folder: folder?.value.trim(), secret: secret?.value.trim() }, b, body), s.deps)));
}

async function addMcp(s, fields, btn, dialogBody) {
  btn.disabled = true;
  message("Installation et connexion du serveur MCP…");
  const body = Object.fromEntries(Object.entries(fields).filter(([, v]) => v));
  const r = await api(`/v1/admin/marketplace/mcp/${encodeURIComponent(s.id)}`, { method: "POST", body });
  btn.disabled = false;
  if (!r.ok) return message(r.data?.error?.message || "Installation impossible.");
  if (r.data.state === "running") { message(""); return follow(r, btn, dialogBody); }   // Node.js d'abord
  message(r.data.connected ? `${s.name} est connecté : dans le chat, /mcp ${s.id} explique ses outils.`
    : `${s.name} est déclaré, mais pas encore connecté${r.data.error ? ` : ${r.data.error}` : ""}.`, r.data.connected);
  $("detail").close();
  await load(false);
}

async function removeMcp(s, btn) {
  if (!confirm(`Retirer le serveur MCP « ${s.name} » ?`)) return;
  btn.disabled = true;
  const r = await api(`/v1/admin/marketplace/mcp/${encodeURIComponent(s.id)}`, { method: "DELETE" });
  if (!r.ok) { btn.disabled = false; return message(r.data?.error?.message || "Impossible de le retirer."); }
  message(`${s.name} est retiré.`, true);
  $("detail").close();
  await load(false);
}

function toolDetail(it, body, foot) {
  const t = it.tool;
  body.append(el("p", "small muted", t.lezard));
  if (t.warning) body.append(el("p", "alert show warn-box", t.warning));
  body.append(el("p", "small", `Prérequis : ${t.needs}`));
  body.append(licenseBlock(t.license));
  const p = el("p", "small");
  p.append(link("Dépôt officiel ↗", t.url));
  body.append(p);
  oneClick(it, t, body, foot, "tools");
  guideButton(t, foot);
}

function appDetail(it, body, foot) {
  const a = it.app;
  body.append(el("p", "small muted", "Application installée sur ce PC en un clic, puis branchée sur Lezard IA avec une clé "
    + "dédiée (guide de branchement)."));
  if (a.warning) body.append(el("p", "alert show warn-box", a.warning));
  body.append(licenseBlock(a.license));
  const p = el("p", "small");
  p.append(link("Site officiel ↗", a.url));
  body.append(p);
  oneClick(it, a, body, foot, "apps");
  guideButton(a, foot);
}

/** « Guide de branchement » : la section « Brancher un outil » de l'administration, ouverte sur ce guide. */
function guideButton(e, foot) {
  if (e.guide && admin) foot.prepend(button("Guide de branchement", "btn-ghost", () => { location.href = `/admin#guide-${e.guide}`; }));
}

// ---------- bundles ----------
/** Nom d'un élément de bundle ; un modèle : celui choisi pour ce PC, sinon « le meilleur pour votre PC ». */
function partName(p) {
  if (p.kind === "model") return p.label ? `${p.label} (${p.use_label.toLowerCase()})` : `Modèle « ${p.use_label} » adapté à votre PC`;
  return p.it?.name || p.id;
}
/** Pourquoi un élément ne s'installera pas avec le bundle (null : il s'installe). */
function partBlocked(p, parts) {
  const c = p.it?.component, s = p.it?.server, e = p.it?.tool || p.it?.app;
  if (p.kind === "model") {
    const ollama = reco?.ollama || library?.ollama
      || parts.some((x) => x.kind === "component" && x.id === "ollama" && !x.installed && !partBlocked(x, parts));
    return ollama || ollamaInstallable() ? null : "Ollama est requis : installez-le d'abord.";
  }
  if (!p.it) return "Absent de ce Lezard IA (mise à jour nécessaire ?).";
  if (c && !c.installable) return c.missing?.length ? `Requis sur ce PC : ${c.missing.join(", ")}.` : c.manual || "Non disponible sur ce système.";
  if (s && (s.needs_folder || s.secret)) return "Un dossier ou une clé à indiquer : installez-le depuis sa fiche.";
  if (s && !data.node && !s.deps?.length) return "Node.js est requis (nodejs.org).";
  if (e && !e.one_click) return "À brancher soi-même : voir sa fiche.";
  if (e?.missing?.length) return `Requis sur ce PC : ${e.missing.join(", ")}.`;
  return null;
}

function bundleDetail(it, body, foot) {
  const b = it.bundle;
  body.append(el("p", "small", `Pour qui : ${b.for}`));
  if (admin && b.suggested) body.append(el("p", "alert show ok-box", `Conseillé pour ce PC.${b.why.length ? ` ${b.why.join(" ")}` : ""}`));
  if (admin && !b.suggested) body.append(el("p", "alert show warn-box", `Pas proposé pour ce PC : ${b.why_not.join(" ")}`));
  body.append(el("h3", "", "Ce qu'il contient"));
  const ul = el("ul", "bundle-parts");
  for (const p of b.parts) {
    const li = el("li");
    const name = p.it ? button(partName(p), "btn-ghost bundle-part-open", () => openDetail(p.it, p.tag ? fullTag(p.tag) : null))
      : el("strong", "", partName(p));
    li.append(name);
    if (admin) {
      const blocked = !p.installed && partBlocked(p, b.parts);
      li.append(el("span", `pill ${p.installed ? "on-pill" : ""}`, p.installed ? "Déjà installé" : blocked ? "Non inclus" : "À installer"));
      if (blocked) li.append(el("span", "small muted", ` ${blocked}`));
    }
    if (p.it && p.kind !== "model") li.append(licenseChip(p.it.license || { name: "Fonction de Lezard IA", commercial: "oui" }));
    else if (p.it) {
      const key = fullTag(p.tag);
      li.append(licenseFor(key));
      if (!licenses.has(key)) fetchLicense(key);
    }
    ul.append(li);
  }
  body.append(ul);
  if (!admin) body.append(el("p", "small muted", "Connecté avec la clé maître, la marketplace choisit chaque modèle selon votre "
    + "carte graphique et ne vous propose que les bundles adaptés à votre PC."));
  const cmds = b.parts.flatMap((p) => (p.it ? commandsOf(p.it) : []));
  if (cmds.length) body.append(commandsBlock(cmds, "Commandes que vous aurez ensuite"));
  body.append(el("p", "small muted", "Licence et droits d'auteur : ceux de chaque élément, dans sa fiche (cliquez sur son nom). "
    + "En installant le bundle, vous acceptez la licence de chacun."));
  if (!admin) { foot.prepend(loginButton()); return; }
  const todo = b.parts.filter((p) => !p.installed && !partBlocked(p, b.parts));
  if (!todo.length) return;
  foot.prepend(button("Installer", "btn-primary", (btn) => twoStep(btn, body, [
    ["Bundle", b.name],
    ["À installer", todo.map(partName).join(", ")],
    ["Déjà là", b.parts.filter((p) => p.installed).map(partName).join(", ") || null],
    ["Ordre", "L'un après l'autre ; un échec n'arrête pas les suivants"],
  ], () => installBundle(b, todo, btn, body), [...new Map(todo.flatMap((p) => entryOf(p.it || {})?.deps || []).map((d) => [d.name, d])).values()])));
}

/** Attend la fin de l'installation en cours (une à la fois) : son état final. */
async function waitJob(onLog) {
  for (;;) {
    await new Promise((ok) => setTimeout(ok, 2000));
    const r = await api("/v1/admin/marketplace/job");
    if (!r.ok) continue;
    if (r.data.log?.length) onLog(r.data.log);
    if (r.data.state !== "running") return r.data;
  }
}

/** Installe un élément de bundle par sa route habituelle : { ok, restart, note }. */
async function installPart(p, status, onLog) {
  if (p.kind === "model") {
    let provider = reco?.ollama || library?.ollama;
    if (!provider) {                                           // Ollama installé avec ce bundle : démarré, ajouté
      const ready = await ensureOllama(status);
      if (ready.error) return { ok: false, note: ready.error };
      provider = ready.provider;
      if (reco) reco.ollama = provider;
    }
    const failed = await pullModel(p.tag, provider, (ev) => status(ev.pct !== undefined ? `téléchargement ${ev.pct} %` : ev.status));
    return { ok: !failed, note: failed };
  }
  const id = encodeURIComponent(p.id);
  const url = { component: `/v1/admin/marketplace/components/${id}/install`, mcp: `/v1/admin/marketplace/mcp/${id}`,
                tool: `/v1/admin/marketplace/tools/${id}/install`, app: `/v1/admin/marketplace/apps/${id}/install` }[p.kind];
  const r = await api(url, { method: "POST", body: p.kind === "mcp" ? {} : undefined });
  if (!r.ok) return { ok: false, note: r.data?.error?.message || "Installation impossible." };
  if (r.data.state === "running") {
    const end = await waitJob(onLog);
    return { ok: end.state === "done", restart: end.restart, note: end.state === "done" ? end.after : `échec (code ${end.code})` };
  }
  if (p.kind === "mcp") return { ok: true, note: r.data.connected ? "connecté" : `déclaré${r.data.error ? ` : ${r.data.error}` : ""}` };
  return { ok: true, restart: r.data.restart };
}

async function installBundle(b, todo, btn, body) {
  btn.disabled = true;
  const list = el("ol", "bundle-progress");
  const log = el("pre", "guide-code market-log");
  log.hidden = true;
  body.prepend(list, log);
  const rows = todo.map((p) => {
    const li = el("li", "", `${partName(p)} : en attente`);
    list.append(li);
    return li;
  });
  let restart = false, failed = 0;
  for (const [i, p] of todo.entries()) {
    const set = (text) => { rows[i].textContent = `${partName(p)} : ${text}`; };
    set("installation…");
    rows[i].scrollIntoView({ block: "nearest" });
    const r = await installPart(p, set, (lines) => { log.hidden = false; log.textContent = lines.join("\n"); log.scrollTop = log.scrollHeight; });
    restart ||= !!r.restart;
    if (!r.ok) failed += 1;
    set(r.ok ? `installé${r.note ? ` (${r.note})` : ""}` : `non installé : ${r.note}`);
    rows[i].className = r.ok ? "ok" : "bad";
  }
  message(failed ? `${b.name} : ${todo.length - failed} élément(s) installé(s), ${failed} en échec (détail dans la fenêtre).`
    : `${b.name} est installé.${restart ? " Redémarrez Lezard IA pour l'activer." : ""}`, !failed);
  if (restart) {
    btn.replaceWith(button("Redémarrer Lezard IA", "btn-primary", async (x) => {
      x.disabled = true;
      const r = await api("/v1/admin/restart", { method: "POST" });
      message(r.ok ? "Redémarrage… la page se rechargera dans quelques secondes." : r.data?.error?.message || "Redémarrage impossible.", r.ok);
      if (r.ok) setTimeout(() => location.reload(), 8000);
    }));
  } else {
    btn.remove();
  }
  await load(false);
}

// ---------- chargement ----------
async function load(first = true) {
  if (!admin) {
    const r = await api("/v1/marketplace", { key: null });
    if (!r.ok) return message(r.data?.error?.message || `Marketplace indisponible (erreur ${r.status}).`);
    data = r.data;
    library = r.data.library;
    if (!library && first) message("Liste des modèles d'Ollama indisponible pour le moment : réessayez plus tard.");
  } else {
    const [m, lib, rc, pref] = await Promise.all([
      api("/v1/admin/marketplace"), api("/v1/admin/models/library"), api("/v1/admin/models/recommendations"),
      api("/v1/preferences/chat"),
    ]);
    if (!m.ok) return message(m.data?.error?.message || `Marketplace indisponible (erreur ${m.status}) : redémarrez Lezard IA si elle vient d'être mise à jour.`);
    data = m.data;
    library = lib.ok ? lib.data : null;
    reco = rc.ok ? rc.data : null;
    chat = pref.ok ? pref.data.chat || {} : {};
    if (!lib.ok && first) message(lib.data?.error?.message || "Liste des modèles d'Ollama indisponible : réessayez plus tard.");
  }
  rebuild();
  renderReco();
  render();
  $("legal").textContent = "Les noms et marques cités appartiennent à leurs titulaires ; Lezard IA n'est affilié à aucun des "
    + "éditeurs présentés. Les nombres d'étoiles GitHub sont un relevé daté, pas un gage de qualité ni de sécurité. " + data.disclaimer;
}

function logout() {
  clearKey();
  location.replace("/marketplace");                         // on reste sur la marketplace, en visiteur
}

/** Ouverte depuis Lezard IA (lien Marketplace) : la clé de session de l'onglet d'origine, lue sur le même site
 *  seulement (le navigateur refuse l'accès à un autre site). Gardée pour cet onglet, jamais dans l'adresse. */
function keyFromOpener() {
  try {
    const opener = window.opener;
    if (!opener || opener.location.origin !== location.origin) return null;
    return opener.sessionStorage.getItem("gw.apiKey") || opener.localStorage.getItem("gw.apiKey");
  } catch {
    return null;                                             // autre site, ou onglet d'origine fermé
  }
}

/** Ouverte par la marketplace de lezard-ia.fr : on lui signale qu'on est prête, elle répond avec la clé saisie par
 *  le visiteur. Seuls les messages de SITE_ORIGINS sont lus ; la clé est gardée pour cet onglet seulement. */
function keyFromSite() {
  const opener = window.opener;
  if (!opener || !new URLSearchParams(location.search).has("install")) return Promise.resolve(null);
  return new Promise((resolve) => {
    const done = (key) => { window.removeEventListener("message", onMessage); resolve(key); };
    function onMessage(e) {
      if (!SITE_ORIGINS.includes(e.origin) || e.source !== opener || e.data?.type !== "lezard-market-key") return;
      done(typeof e.data.key === "string" && e.data.key.length < 300 ? e.data.key : null);
    }
    window.addEventListener("message", onMessage);
    for (const origin of SITE_ORIGINS) {
      try { opener.postMessage({ type: "lezard-market-ready" }, origin); } catch { /* autre site : ignoré */ }
    }
    setTimeout(() => done(null), 5000);
  });
}

/** ?install=type:id[:taille] : l'élément demandé depuis lezard-ia.fr, ouvert (clé maître : bouton Installer). */
function openRequested() {
  const wanted = new URLSearchParams(location.search).get("install");
  if (!wanted) return;
  history.replaceState(null, "", location.pathname);
  const [kind, id, tag] = wanted.split(":");
  const it = items.find((x) => x.kind === kind && x.id === id);
  if (!it) return message(`« ${id || wanted} » n'est pas dans la marketplace de ce Lezard IA (mise à jour nécessaire ?).`);
  if (!admin) message("Pour installer, connectez-vous avec la clé maître de ce Lezard IA.");
  openDetail(it, tag ? `${id}:${tag}` : null);
  if (!admin) return;
  // « Installer » déjà cliqué sur le site : reste « Confirmer l'installation », ici, sur ce PC
  const first = kind === "model"
    ? [...document.querySelectorAll("#d-body tr")].find((r) => r.dataset.tag === `${id}:${tag || "latest"}`)?.querySelector("button")
    : [...document.querySelectorAll("#d-foot button")].find((b) => b.textContent === "Installer");
  if (first?.textContent.startsWith("Installer")) first.click();   // « Installer » ou « Installer + Ollama », jamais « Supprimer »
}

/** Qui regarde : la clé maître (installation), une autre clé ou personne (visite). */
async function whoIsThere() {
  const fromSite = await keyFromSite();
  if (fromSite) saveKey(fromSite, false);
  if (!getKey()) {
    const inherited = keyFromOpener();
    if (inherited) saveKey(inherited, false);
  }
  if (!getKey()) return null;
  const r = await api("/v1/me");
  return r.ok ? r.data : null;
}

async function init() {
  initThemePanel($("theme-btn"));
  const me = await whoIsThere();
  if (me) syncTheme();
  admin = !!(me?.is_admin && !me.admin_session?.blocked);
  $("who").textContent = admin ? "Clé maître" : me ? "Connecté" : "Visiteur";
  $("login").hidden = admin;
  $("logout").hidden = !me;
  $("open-app").hidden = !me;
  $("fit-only").closest("label").hidden = !admin;
  if (me && !admin) {
    $("denied").textContent = me.admin_session?.blocked
      ? "Administration depuis Internet désactivée : activez d'abord la double authentification, depuis ce PC ou votre réseau local. Vous pouvez visiter la marketplace."
      : "Cette clé n'a pas les droits d'administration : vous pouvez visiter la marketplace ; pour installer, connectez-vous avec la clé maître.";
    $("denied").hidden = false;
  }
  for (const [id, label] of TABS) {
    const b = el("button", "market-tab", label);
    b.type = "button";
    b.dataset.tab = id;
    b.setAttribute("role", "tab");
    b.addEventListener("click", () => { tab = id; shown = PAGE; render(); });
    $("tabs").append(b);
  }
  $("search").addEventListener("input", () => { shown = PAGE; render(); });
  $("fit-only").addEventListener("change", () => { shown = PAGE; render(); });
  try { $("sort").value = localStorage.getItem("gw.market.sort") || ""; } catch { /* stockage indisponible */ }
  $("sort").addEventListener("change", () => {
    try { localStorage.setItem("gw.market.sort", $("sort").value); } catch { /* ignoré */ }
    shown = PAGE; render();
  });
  $("more").addEventListener("click", () => { shown += PAGE; render(); });
  $("refresh").addEventListener("click", () => { message(""); load(); });
  document.querySelectorAll("#detail [data-close]").forEach((b) => b.addEventListener("click", () => $("detail").close()));
  $("detail").addEventListener("click", (e) => { if (e.target === $("detail")) $("detail").close(); });
  await load();
  openRequested();
}

document.addEventListener("DOMContentLoaded", () => {
  $("logout").addEventListener("click", logout);
  init();
});
