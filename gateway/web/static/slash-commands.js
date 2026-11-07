// Commandes « / » du chat, tapées en début de message (« /pc ouvre le bloc-notes ») et proposées dès le « / ».
// Commandes d'envoi (/pc, /web) : outils activés pour ce message seulement, le texte part sans la commande.
// Commandes locales (/memoire, /retiens, /oublie, /nouveau, /compacte, /prive, /aide, /modele, /mode, /effort, /multi, /competence) : exécutées dans la page,
// rien n'est envoyé à l'IA (/compacte fait résumer la conversation par la gateway). /modele choisit l'IA qui répond ; /mode, /effort et /multi règlent le routage. La mémoire du contrôle du PC (préférences, leçons tirées des erreurs) est sur la gateway.
// /mcp : seul ou avec un nom de serveur, explique (local) ; suivie d'une demande, elle part à l'IA, limitée au serveur nommé.
// Outils de la marketplace installés sur ce PC (GET /v1/apps, clé maître) : une commande chacun (/openclaw, /aider,
// /gws, /jan…) ; l'outil répond dans la conversation à la place de l'IA du chat (chat.js, askTool).
import { api, rawApi } from "./auth.js";
import { activeSkills, chatModels, forcedModel, routing, setEffort, setForcedModel, setMode, setMulti, setSkill,
         skillCatalog } from "./settings-bar.js";

const COMMANDS = [
  { name: "pc", args: "<tâche>", help: "L'IA contrôle ce PC pour cette demande", needs: "pc", send: true },
  { name: "web", args: "<question>", help: "L'IA cherche sur Internet pour cette demande", needs: "web", send: true },
  { name: "retiens", args: "<préférence>", help: "Mémoriser une préférence pour le contrôle du PC", needs: "pc" },
  { name: "memoire", help: "Ce que l'IA sait de ce PC : vos préférences et le guide de vos applications", needs: "pc" },
  { name: "oublie", args: "<n° | tout>", help: "Effacer un souvenir (numéro donné par /memoire) ou tous", needs: "pc" },
  { name: "nouveau", help: "Nouvelle conversation" },
  { name: "compacte", args: "[consigne]", help: "Résumer la conversation : l'IA repart du résumé (contexte allégé)" },
  { name: "prive", args: "[on | off]", help: "Mode privé : IA locales seulement (sans argument : bascule)" },
  { name: "modele", args: "[nom | auto]", help: "Choisir l'IA qui répond (sans nom : la liste ; auto : choix automatique)" },
  { name: "mode", args: "[vitesse | eco | equilibre]", help: "Mode de routage (sans nom : le mode actuel)" },
  { name: "effort", args: "[auto | bas | moyen | eleve]", help: "Effort de raisonnement (sans nom : l'effort actuel)" },
  { name: "mcp", args: "[serveur] [demande]", help: "Serveurs MCP : seul, la liste et les commandes ; avec un serveur, ses outils expliqués ou une demande pour lui seul ; sinon, l'IA choisit parmi tous", needs: "mcp", send: true },
  { name: "docs", args: "<question>", help: "L'IA cherche dans vos documents Paperless-ngx et Nextcloud", needs: "docs", send: true },
  { name: "image", args: "<description>", help: "Générer une image avec votre ComfyUI (COMFYUI_URL dans .env)" },
  { name: "multi", args: "[auto | on | off]", help: "Multi-agents : auto selon la complexité, on toujours, off une IA seule" },
  { name: "competence", args: "[nom | off]", help: "Compétences que l'IA suit, ex. réponses concises (sans nom : la liste ; nom : activer ou couper)" },
  { name: "aide", help: "Liste des commandes" },
];
const MODE_NAMES = { vitesse: "speed", eco: "economy", equilibre: "balanced" };
const MODE_LABELS = { speed: "Vitesse", economy: "Éco", balanced: "Équilibré" };
const EFFORT_NAMES = { auto: "auto", bas: "low", moyen: "medium", eleve: "high" };
const EFFORT_LABELS = { auto: "Auto", low: "Bas", medium: "Moyen", high: "Élevé" };
const KINDS = { preference: "Préférences", lesson: "Anciennes leçons (plus rappelées à l'IA)" };
const MULTI_NAMES = { auto: "auto", on: "multi", oui: "multi", off: "single", non: "single" };
const MULTI_LABELS = {
  auto: "automatiques (plusieurs IA pour une demande complexe, une seule sinon)",
  multi: "toujours activés",
  single: "désactivés (une IA seule)",
};
const SOURCES = { user: "dictée par vous", agent: "notée par l'IA", auto: "apprise d'une erreur" };
const $ = (id) => document.getElementById(id);

let tools = null;                                          // outils permis à cette clé (GET /v1/tools)
let servers = [];                                          // serveurs MCP de cette clé, connectés ou non
let numbered = [];                                         // ids des souvenirs, dans l'ordre affiché par /memoire
let apps = [];                                             // outils de la marketplace installés sur ce PC (clé maître)
const ARGS = { agent: "<demande>", cli: "<arguments>" };

/** Commandes du chat : celles de Lezard IA, puis une par outil installé (/openclaw, /aider, /gws, /jan…). */
function commands() {
  return [...COMMANDS, ...apps.map((a) => ({ name: a.command, args: ARGS[a.kind], app: a, send: true,
                                            help: `${a.name} ${a.what}` }))];
}

async function rights(fresh = false) {
  if (!tools || fresh) {
    const [r, installed] = await Promise.all([api("/v1/tools"), rawApi("/v1/apps")]);
    tools = r.ok ? r.data.data : [];
    servers = r.ok ? r.data.mcp_servers || [] : [];
    apps = installed.ok ? installed.data?.data || [] : [];
  }
  return tools;
}
function allowed(cmd) {
  if (!cmd.needs) return true;
  if (!tools) return false;
  if (cmd.needs === "mcp" && servers.length) return true;   // même non connecté : /mcp dit pourquoi
  if (["pc", "web", "mcp", "docs"].includes(cmd.needs)) return tools.some((t) => t.level === cmd.needs);
  return tools.length > 0;
}
const plain = (s) => s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();   // « mémoire » → « memoire »

/** Ce que dit /mcp : { server, rest } si le premier mot est le nom d'un serveur (« /mcp rea ouvre… »), sinon la demande
 *  pour tous les serveurs (« /mcp liste mes fichiers »). */
function mcpTarget(rest) {
  const m = /^(\S+)(?:\s+([\s\S]*))?$/.exec(rest);
  const server = m && servers.find((s) => plain(s.id) === plain(m[1]));
  return server ? { server, rest: (m[2] || "").trim() } : { server: null, rest };
}

/** Pour l'affichage d'un appel d'outil MCP : { server, tool, help } ; null pour les autres outils. */
export function mcpTool(name) {
  const t = (tools || []).find((x) => x.name === name);
  return t?.server ? { server: t.server, tool: t.tool, help: t.help } : null;
}

/** « /pc ouvre… » → { cmd, rest } ; cmd null si la commande est inconnue ; null si le texte n'en est pas une
 *  (« /usr/bin, c'est quoi ? » part normalement : un nom de commande n'a que des lettres). */
export function parseCommand(text) {
  const m = /^\/(\p{L}+)(?:\s+([\s\S]*))?$/u.exec(text.trim());
  if (!m) return null;
  return { name: m[1], cmd: commands().find((c) => c.name === plain(m[1])) || null, rest: (m[2] || "").trim() };
}

/** Commande reconnue au début d'un message affiché : { label: "/pc", rest } (bulle de l'utilisateur). */
export function commandLabel(text) {
  const p = parseCommand(text || "");
  if (p?.cmd?.name === "mcp") {
    const target = mcpTarget(p.rest);
    if (target.server) return { label: `/mcp ${target.server.id}`, rest: target.rest };
  }
  return p?.cmd ? { label: `/${p.cmd.name}`, rest: p.rest } : null;
}

/** Options de requête de la commande d'envoi qui ouvre `text` ({} sans commande). */
export function commandOptions(text) {
  const parsed = parseCommand(text || "");
  const cmd = parsed?.cmd;
  if (!cmd?.send) return {};
  if (cmd.name === "mcp") {                                 // un serveur nommé : ses outils seulement
    const only = mcpTarget(parsed.rest).server?.id;
    return { gateway_tools: (tools || []).filter((t) => t.level === "mcp" && (!only || t.server === only)).map((t) => t.name), orchestration: undefined };
  }
  if (cmd.name === "pc") return { gateway_tools: true, orchestration: undefined };
  if (cmd.name === "docs") return { gateway_tools: (tools || []).filter((t) => t.level === "docs").map((t) => t.name), orchestration: undefined };
  return { gateway_tools: (tools || []).filter((t) => !["pc", "mcp", "docs"].includes(t.level)).map((t) => t.name), orchestration: undefined };
}

/** Outil de la marketplace qui répond à ce message (« /aider … ») : { app, rest } ; null pour l'IA du chat. */
export function appRequest(text) {
  const p = parseCommand(text || "");
  return p?.cmd?.app ? { app: p.cmd.app, rest: p.rest } : null;
}

/** Contenu d'un message sans la commande d'envoi du début (l'IA reçoit la demande seule). */
export function stripCommand(content) {
  const strip = (s) => {
    const p = parseCommand(s);
    if (p?.cmd?.name === "mcp") return mcpTarget(p.rest).rest;
    return p?.cmd?.send ? p.rest : s;
  };
  if (typeof content === "string") return strip(content);
  if (!Array.isArray(content)) return content;
  let first = true;
  return content.map((part) => {
    if (part.type !== "text" || !first) return part;
    first = false;
    return { ...part, text: strip(part.text) };
  });
}

/** Message tapé : null si ce n'est pas une commande (envoi normal), sinon { send } — true : à envoyer à l'IA. */
export async function handleCommand(text, ui) {
  const p = parseCommand(text);
  if (!p) return null;
  ui.notice("");
  await rights(p.cmd?.name === "mcp" || !p.cmd);           // un serveur ou un outil vient peut-être d'être installé
  if (!p.cmd) p.cmd = commands().find((c) => c.name === plain(p.name)) || null;
  if (!p.cmd) {
    ui.notice(`Commande inconnue : /${p.name}. Tapez /aide pour la liste.`, true);
    return { send: false, keep: true };
  }
  if (!allowed(p.cmd)) {
    ui.notice(p.cmd.needs === "pc" ? "Contrôle du PC non disponible pour cette clé (ou désactivé : TOOLS_PC)."
      : p.cmd.needs === "docs" ? "Aucune source de documents : Nextcloud se configure dans la page Modèles (Clés des services) ; Paperless-ngx avec PAPERLESS_URL et PAPERLESS_TOKEN dans .env, puis redémarrez Lezard IA."
      : p.cmd.needs === "mcp" ? "Aucun serveur MCP connecté : déclarez-les dans data/mcp.json, puis redémarrez Lezard IA."
      : p.cmd.needs === "web" ? "Recherche web non disponible : coupée dans les réglages, ou non permise pour cette clé."
      : "Outils non permis pour cette clé.", true);
    return { send: false, keep: true };
  }
  if (p.cmd.name === "mcp") {
    const target = mcpTarget(p.rest);
    if (!p.rest) { mcpList(ui); return { send: false }; }
    if (target.server && !target.rest) { mcpDetail(target.server, ui); return { send: false }; }
    if (target.server && !target.server.connected) {
      ui.notice(`Le serveur ${target.server.id} n'est pas connecté : tapez /mcp pour voir l'état de vos serveurs.`, true);
      return { send: false, keep: true };
    }
    if (!tools.some((t) => t.level === "mcp")) {
      ui.notice("Aucun serveur MCP connecté : tapez /mcp pour voir l'état de vos serveurs.", true);
      return { send: false, keep: true };
    }
    return { send: true };
  }
  if (p.cmd.app) {                                         // outil de la marketplace : il répond à la place de l'IA
    if (p.cmd.app.kind === "agent" && !p.rest) {
      ui.notice(`Indiquez la demande après la commande, ex. ${p.cmd.app.example || `/${p.cmd.name} …`}`, true);
      return { send: false, keep: true };
    }
    return { send: true };
  }
  if (p.cmd.send) {
    if (!p.rest) {
      ui.notice(`Indiquez la demande après la commande, ex. /${p.cmd.name} ${p.cmd.name === "pc"
        ? "ouvre le bloc-notes"
        : p.cmd.name === "docs" ? "trouve ma dernière facture d'électricité" : "météo à Brest demain"}`, true);
      return { send: false, keep: true };
    }
    return { send: true };
  }
  await LOCAL[p.cmd.name](p.rest, ui);
  return { send: false };
}

// ---------- commandes locales ----------
const LOCAL = {
  aide: (_, ui) => {
    const box = panel("Commandes");
    const list = document.createElement("dl");
    list.className = "slash-help";
    for (const c of commands().filter(allowed)) {
      const dt = document.createElement("dt");
      dt.textContent = `/${c.name}${c.args ? ` ${c.args}` : ""}`;
      const dd = document.createElement("dd");
      dd.textContent = c.help;
      list.append(dt, dd);
    }
    const keys = document.createElement("p");
    keys.className = "slash-keys";
    keys.textContent = "Raccourcis clavier : Ctrl+/ (⌘+/ sur Mac).";
    box.append(list, keys);
    ui.show(box);
  },
  nouveau: (_, ui) => ui.newConversation(),
  compacte: (text, ui) => ui.compact(text),
  prive: (text, ui) => {
    const want = plain(text.trim());
    const on = ["on", "oui"].includes(want) ? true : ["off", "non"].includes(want) ? false : !ui.isPrivate();
    ui.setPrivate(on);
    ui.notice(on ? "Mode privé activé : IA locales seulement." : "Mode privé désactivé.");
  },
  modele: async (text, ui) => {
    const want = text.trim().toLowerCase();
    const list = await chatModels();
    if (!want) {
      const box = panel("Modèles de chat");
      const current = document.createElement("p");
      current.className = "small";
      current.textContent = `Choisi : ${forcedModel() || "automatique (selon votre demande)"}`;
      const ul = document.createElement("ul");
      for (const m of list) {
        const li = document.createElement("li");
        li.textContent = m.id;
        ul.append(li);
      }
      box.append(current, ul);
      return ui.show(box);
    }
    if (["auto", "automatique"].includes(want)) {
      setForcedModel("");
      return ui.notice("Choix automatique rétabli : l'IA est choisie selon votre demande.");
    }
    const ids = list.map((m) => m.id);
    const exact = ids.filter((id) => id.toLowerCase() === want);
    const found = exact.length ? exact : ids.filter((id) => id.toLowerCase().includes(want));
    if (!found.length) return ui.notice(`Aucun modèle ne correspond à « ${text.trim()} ». Tapez /modele pour la liste.`, true);
    if (found.length > 1) return ui.notice(`Plusieurs modèles correspondent : ${found.slice(0, 4).join(", ")}. Précisez le nom.`, true);
    setForcedModel(found[0]);
    ui.notice(`Modèle choisi : ${found[0]}. Tapez /modele auto pour revenir au choix automatique.`);
  },
  mode: (text, ui) => {
    const r = routing();
    const want = plain(text.trim());
    const note = forcedModel() ? " (sans effet tant qu'un modèle est imposé)" : "";
    if (!want) return ui.notice(`Mode : ${MODE_LABELS[r.mode]}${note}. Choix : /mode vitesse, /mode eco, /mode equilibre.`);
    const id = MODE_NAMES[want];
    if (!id) return ui.notice(`Mode inconnu : « ${text.trim()} ». Choix : vitesse, eco, equilibre.`, true);
    if (!setMode(id)) return ui.notice("Ce mode n'est pas permis pour votre clé.", true);
    ui.notice(`Mode : ${MODE_LABELS[id]}${note}.`);
  },
  effort: (text, ui) => {
    const r = routing();
    const want = plain(text.trim());
    if (!want) return ui.notice(`Effort : ${EFFORT_LABELS[r.effort]}. Choix : /effort auto, bas, moyen, eleve.`);
    const id = EFFORT_NAMES[want];
    if (!id) return ui.notice(`Effort inconnu : « ${text.trim()} ». Choix : auto, bas, moyen, eleve.`, true);
    const chosen = setEffort(id);
    ui.notice(chosen === id ? `Effort : ${EFFORT_LABELS[chosen]}.`
      : `Effort plafonné à ${EFFORT_LABELS[chosen]} par votre clé.`);
  },
  multi: (text, ui) => {
    const r = routing();
    const want = plain(text.trim());
    if (!want) {
      return ui.notice(!r.multiAllowed ? "Multi-agents : non permis pour cette clé."
        : `Multi-agents : ${MULTI_LABELS[r.multi] || MULTI_LABELS.auto}. Choix : /multi auto, /multi on, /multi off.`);
    }
    const value = MULTI_NAMES[want];
    if (!value) return ui.notice("Indiquez auto, on ou off, ex. /multi auto", true);
    if (!setMulti(value)) return ui.notice("Multi-agents : non permis pour cette clé.", true);
    ui.notice(`Multi-agents : ${MULTI_LABELS[value]}.`);
  },
  competence: async (text, ui) => {
    const { data: list, max_active: max } = await skillCatalog();
    const want = plain(text.trim());
    const on = new Set(activeSkills());
    if (!list.length) return ui.notice("Aucune compétence disponible sur cette gateway.", true);
    if (!want) {
      const box = panel("Compétences");
      const dl = document.createElement("dl");
      dl.className = "slash-help";
      for (const s of list) {
        const dt = document.createElement("dt");
        dt.textContent = `${on.has(s.id) ? "● " : "○ "}${s.name} (/competence ${s.id})`;
        const dd = document.createElement("dd");
        dd.textContent = `${s.description}. D'après ${s.source}, licence ${s.license}.`;
        dl.append(dt, dd);
      }
      const hint = document.createElement("p");
      hint.className = "small";
      hint.textContent = `● active. ${max} au plus ; /competence off les coupe toutes. Aussi dans le sélecteur de modèle.`;
      box.append(dl, hint);
      return ui.show(box);
    }
    if (["off", "aucune", "non"].includes(want)) {
      for (const id of on) setSkill(id, false);
      return ui.notice("Compétences désactivées.");
    }
    const found = list.filter((s) => s.id === want || plain(s.name).includes(want));
    if (!found.length) return ui.notice(`Aucune compétence ne correspond à « ${text.trim()} ». Tapez /competence pour la liste.`, true);
    if (found.length > 1) return ui.notice(`Plusieurs compétences correspondent : ${found.map((s) => s.id).join(", ")}. Précisez.`, true);
    const s = found[0], next = !on.has(s.id);
    if (!setSkill(s.id, next)) return ui.notice(`${max} compétences actives au plus : /competence off, ou coupez-en une.`, true);
    ui.notice(next ? `Compétence activée : ${s.name}.` : `Compétence désactivée : ${s.name}.`);
  },
  image: async (text, ui) => {
    const prompt = text.trim();
    if (!prompt) return ui.notice("Décrivez l'image, ex. /image un lézard vert sur une pierre au soleil", true);
    ui.notice("Image en cours de génération… (quelques dizaines de secondes)");
    const r = await api("/v1/images/generations", { method: "POST", body: { prompt } });
    const b64 = r.ok && r.data?.data?.[0]?.b64_json;
    if (!b64) {
      const why = r.data?.error?.message
        || (r.status === 404 ? "Génération d'images absente de la version qui tourne : redémarrez Lezard IA pour charger la mise à jour."
          : `Génération impossible (réponse ${r.status || "aucune"}) : voir data/logs/gateway.log.`);
      return ui.notice(why, true);
    }
    ui.notice("");
    const box = panel("Image générée");
    const url = `data:image/png;base64,${b64}`;       // CSP : img-src data:
    const img = document.createElement("img");
    img.src = url;
    img.alt = prompt;
    img.className = "slash-image";
    const save = document.createElement("a");
    save.href = url;
    save.download = "image.png";
    save.textContent = "Enregistrer l'image";
    box.append(img, save);
    ui.show(box);
  },
  retiens: async (text, ui) => {
    if (!text) return ui.notice("Indiquez la préférence, ex. /retiens j'utilise Firefox, pas Edge", true);
    const r = await api("/v1/tools/memory", { method: "POST", body: { kind: "preference", text } });
    ui.notice(r.ok ? `Retenu : « ${r.data.text} ». L'IA en tiendra compte quand elle contrôle le PC.`
                   : r.data?.error?.message || "Impossible d'enregistrer.", !r.ok);
  },
  oublie: async (arg, ui) => {
    if (plain(arg) === "tout") {
      if (!confirm("Effacer toute la mémoire du contrôle du PC (préférences et leçons) ?")) return;
      const r = await api("/v1/tools/memory", { method: "DELETE" });
      numbered = [];
      return ui.notice(r.ok ? `Mémoire effacée (${r.data.deleted} souvenirs).` : "Impossible d'effacer.", !r.ok);
    }
    const id = numbered[Number(arg) - 1];
    if (!id) return ui.notice("Numéro inconnu : affichez d'abord la liste avec /memoire, puis /oublie 3.", true);
    const r = await api(`/v1/tools/memory/${id}`, { method: "DELETE" });
    ui.notice(r.ok ? `Souvenir n° ${arg} effacé.` : "Souvenir introuvable (déjà effacé ?).", !r.ok);
    if (r.ok) numbered[Number(arg) - 1] = null;
  },
  memoire: async (_, ui) => {
    const r = await api("/v1/tools/memory");
    if (!r.ok) return ui.notice(r.data?.error?.message || "Mémoire indisponible.", true);
    const box = panel("Mémoire du contrôle du PC");
    const rows = r.data.data;
    numbered = [];
    if (!rows.length) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "Aucune préférence pour l'instant : dictez-en une avec /retiens (ex. /retiens j'utilise Arc).";
      box.append(p);
    }
    for (const [kind, title] of Object.entries(KINDS)) {
      const items = rows.filter((m) => m.kind === kind);
      if (!items.length) continue;
      const h = document.createElement("h4");
      h.textContent = title;
      const ol = document.createElement("ol");
      ol.className = "slash-memory";
      ol.start = numbered.length + 1;
      for (const m of items) {
        numbered.push(m.id);
        const li = document.createElement("li");
        const text = document.createElement("span");
        text.textContent = m.text;
        const info = document.createElement("small");
        info.textContent = `${SOURCES[m.source] || m.source}${m.hits > 1 ? ` · vu ${m.hits} fois` : ""}`;
        const del = document.createElement("button");
        del.type = "button";
        del.className = "slash-forget";
        del.textContent = "Oublier";
        del.addEventListener("click", async () => {
          const d = await api(`/v1/tools/memory/${m.id}`, { method: "DELETE" });
          if (d.ok || d.status === 404) li.remove();
          numbered[numbered.indexOf(m.id)] = null;
        });
        li.append(text, " ", info, del);
        ol.append(li);
      }
      box.append(h, ol);
    }
    const apps = r.data.apps || [];                          // guide des applications : lu par l'IA, écrit à part
    const h = document.createElement("h4");
    h.textContent = "Guide des applications de ce PC";
    const p = document.createElement("p");
    p.className = "muted";
    const used = apps.filter((a) => a.usage !== "jamais");
    p.textContent = used.length
      ? `${used.map((a) => `${a.name} (${a.usage})`).join(", ")}. L'IA lit à chaque tâche le guide des applications ` +
        "dont parle votre demande."
      : "Pas encore de guide : il se prépare d'après les applications que vous utilisez (data/pc_apps.json).";
    box.append(h, p);
    ui.show(box);
  },
};

const offline = (s) => `Non connecté${s.error ? ` : ${s.error.replace(/\.+$/, "")}` : ""}.`;
const ask = (s) => (s.example ? `/mcp ${s.id} ${s.example}` : `/mcp ${s.id} <votre demande>`);

/** /mcp : chaque serveur, ce qu'il fait, et les commandes pour s'en servir. */
function mcpList(ui) {
  const box = panel("Serveurs MCP");
  const list = document.createElement("dl");
  list.className = "slash-help";
  for (const s of servers) {
    const dt = document.createElement("dt");
    dt.textContent = `${s.connected ? "● " : "○ "}${s.id}`;
    const dd = document.createElement("dd");
    const what = document.createElement("div");
    what.textContent = `${s.name}. ${s.description}`;
    const how = document.createElement("div");
    if (s.connected) {
      const example = document.createElement("code");
      example.textContent = ask(s);
      const detail = document.createElement("code");
      detail.textContent = `/mcp ${s.id}`;
      how.append("Exemple : ", example, ` · ses ${s.tools} outil${s.tools > 1 ? "s" : ""} expliqué${s.tools > 1 ? "s" : ""} : `, detail);
    } else {
      how.textContent = offline(s);
    }
    dd.append(what, how);
    list.append(dt, dd);
  }
  const hint = document.createElement("p");
  hint.className = "small";
  hint.textContent = "● connecté. /mcp <serveur> <demande> : l'IA n'utilise que ce serveur. /mcp <demande> : elle choisit parmi "
    + "tous. Chaque appel d'outil affiche le serveur utilisé et attend votre accord, sauf sur un serveur déclaré de confiance.";
  box.append(list, hint);
  ui.show(box);
}

/** /mcp rea : à quoi sert le serveur, comment s'en servir, et chacun de ses outils expliqué. */
function mcpDetail(s, ui) {
  const box = panel(`Serveur MCP : ${s.id}`);
  const what = document.createElement("p");
  what.textContent = `${s.name}. ${s.description}`;
  box.append(what);
  if (!s.connected) {
    const off = document.createElement("p");
    off.className = "muted";
    off.textContent = offline(s);
    box.append(off);
    return ui.show(box);
  }
  const how = document.createElement("p");
  const example = document.createElement("code");
  example.textContent = ask(s);
  how.append("Pour l'utiliser : ", example);
  const h = document.createElement("h4");
  h.textContent = "Ses outils";
  const list = document.createElement("dl");
  list.className = "slash-help";
  for (const t of tools.filter((x) => x.server === s.id)) {
    const dt = document.createElement("dt");
    dt.textContent = t.tool;
    const dd = document.createElement("dd");
    dd.textContent = t.help;
    list.append(dt, dd);
  }
  box.append(how, h, list);
  ui.show(box);
}

function panel(title) {
  const box = document.createElement("div");
  box.className = "slash-panel";
  const h = document.createElement("h3");
  h.textContent = title;
  box.append(h);
  return box;
}

// ---------- suggestions pendant la frappe : ↑ ↓ pour choisir, Tab / Entrée pour compléter, Échap ----------
// Deux listes : les commandes (« / »), et après « /modele » les modèles de chat, filtrés par ce qui est tapé. Pour
// les modèles, Tab (Maj+Tab : précédent) passe au suivant et écrit son nom dans la saisie ; le nom reste visible
// en tête de liste pendant le choix. Entrée valide le modèle sans l'envoyer : Entrée une seconde fois envoie /modele.
export function initSlash(prompt) {
  rights();                                                // /web régénéré au rechargement : outils déjà connus
  const menu = document.createElement("ul");
  menu.className = "slash-menu";
  menu.id = "slash-menu";
  menu.setAttribute("role", "listbox");
  menu.hidden = true;
  prompt.closest("form").prepend(menu);
  prompt.setAttribute("aria-controls", "slash-menu");
  let items = [], active = 0, models = false;

  const close = () => { menu.hidden = true; items = []; models = false; prompt.setAttribute("aria-expanded", "false"); };
  const show = () => {
    menu.hidden = false;
    prompt.setAttribute("aria-expanded", "true");
    paint();
  };
  const choose = (i) => {
    const it = items[i];
    if (!it) return;
    prompt.value = it.fill;
    close();
    prompt.focus();
    prompt.dispatchEvent(new Event("input"));           // « /modele » ouvre aussitôt la liste des modèles
  };
  const paint = () => {
    menu.innerHTML = "";
    if (models && items.length) {                       // le modèle en cours de choix reste affiché
      const head = document.createElement("li");
      head.className = "slash-head";
      head.setAttribute("role", "presentation");
      head.textContent = `Modèle : ${items[active].label} (${active + 1}/${items.length}) · Tab suivant · Maj+Tab précédent · Entrée valide`;
      menu.append(head);
    }
    items.forEach((it, i) => {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === active));
      const name = document.createElement("code");
      name.textContent = it.label;
      const help = document.createElement("span");
      help.textContent = it.help;
      li.append(name, help);
      li.addEventListener("pointerdown", (e) => { e.preventDefault(); choose(i); });
      menu.append(li);
    });
  };
  const updateCommands = async (typed) => {
    await rights();
    if (plain(/^\/(\S*)$/.exec(prompt.value)?.[1] ?? "\u0000") !== typed) return;   // la saisie a changé entre-temps
    const found = commands().filter((c) => allowed(c) && c.name.startsWith(typed));
    if (!found.length || (found.length === 1 && found[0].name === typed && !found[0].args)) return close();
    models = false;
    items = found.map((c) => ({
      label: `/${c.name}${c.args ? ` ${c.args}` : ""}`,
      help: c.help,
      fill: `/${c.name}${c.args ? " " : ""}`,
    }));
    active = Math.min(active, items.length - 1);
    show();
  };
  const updateModels = async (query) => {
    const typed = query.trim().toLowerCase();
    const list = await chatModels();
    if (!/^\/modele\s/i.test(prompt.value)) return;      // la saisie a changé pendant le chargement
    const ids = list.map((m) => m.id).filter((id) => id.toLowerCase().includes(typed));
    const rank = (id) => (id.toLowerCase().startsWith(typed) ? 0 : 1);   // début de nom d'abord
    ids.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
    if (!ids.length) return close();
    models = true;
    items = ids.map((id) => ({
      label: id,
      help: id === forcedModel() ? "choisi" : "",
      fill: `/modele ${id}`,
    }));
    active = Math.min(active, items.length - 1);
    show();
  };
  const updateServers = async (typed) => {                // « /mcp r » : les serveurs, pour ne pas devoir retenir leur nom
    await rights();
    if (plain(/^\/mcp\s+(\S*)$/i.exec(prompt.value)?.[1] ?? "\u0000") !== typed) return;
    const found = servers.filter((s) => plain(s.id).startsWith(typed));
    if (!found.length) return close();
    models = false;
    items = found.map((s) => ({ label: `/mcp ${s.id}`, help: s.name, fill: `/mcp ${s.id} ` }));
    active = Math.min(active, items.length - 1);
    show();
  };
  const update = () => {
    const mod = /^\/modele\s(.*)$/i.exec(prompt.value);
    if (mod) return updateModels(mod[1]);
    const srv = /^\/mcp\s+(\S*)$/i.exec(prompt.value);
    if (srv) return updateServers(plain(srv[1]));
    const cmd = /^\/(\S*)$/.exec(prompt.value);
    if (!cmd) return close();
    updateCommands(plain(cmd[1]));
  };
  prompt.addEventListener("input", () => { active = 0; update(); });
  prompt.addEventListener("blur", close);
  prompt.addEventListener("keydown", (e) => {        // avant l'envoi par Entrée (chat.js)
    if (menu.hidden || !items.length) return;
    const step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
    if (e.key === "Tab" && models) {                    // Tab : modèle suivant, son nom reste dans la saisie
      active = (active + (e.shiftKey ? -1 : 1) + items.length) % items.length;
      prompt.value = items[active].fill;
      paint();
    } else if (step) {
      active = (active + step + items.length) % items.length;
      if (models) prompt.value = items[active].fill;
      paint();
    } else if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
      choose(active);
    } else if (e.key === "Escape") {
      close();
    } else {
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
  });
}
