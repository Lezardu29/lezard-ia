// Interface de chat (5.2) : envoi, streaming SSE, rendu Markdown progressif, arrêt, réessai, copie du code.
// Persistance (19.1) : sur la gateway (chiffrée, retrouvée sur tous les appareils avec la même clé) ou, au choix
// ou sans clé, dans ce navigateur seulement.
// Confort d'édition (19.3) : modifier un message, régénérer (même réglage ou autre modèle), variantes ‹ 1/2 ›
// gardées avec leur suite, copie d'une réponse, export Markdown / JSON. Lecture vocale d'une réponse (19.4).
// Compactage (/compacte) : la conversation est résumée par la gateway ; seuls le résumé et la suite partent à l'IA.
import { api, getKey } from "./auth.js";
import { ICONS as SPEECH_ICONS, speechStatus, stopSpeaking, toggleSpeech } from "./read-aloud.js";
import { download } from "./chat-export.js";
import { saveCode } from "./save-file.js";
import { renderMarkdown } from "./markdown.js";
import { renderMetrics } from "./metrics.js";
import { attachmentsReady, hasAttachments, sizeLabel, takeAttachments } from "./attachments.js";
import { commandLabel, commandOptions, handleCommand, initSlash, stripCommand } from "./slash-commands.js";
import { AUTO_COMPACT, contextRatio, mountContextRing, updateContextRing } from "./context-ring.js";
import { initRowMenus } from "./row-menu.js";

const $ = (id) => document.getElementById(id);
// attente de la réponse : le lézard de l'avatar s'anime (chat.css), le texte l'annonce
const THINKING = '<div class="thinking" role="status">Réflexion en cours…</div>';
const AVATAR = '<span class="lz-loader" aria-hidden="true"></span>';  // le lézard du logo : animé tant qu'il réfléchit

const state = { messages: [], controller: null, storageKey: null, onMeta: () => {}, onAuthError: () => {},
                getOptions: () => ({ model: "auto" }), onStart: () => {}, onRestore: () => {}, getSystem: () => null,
                private: false, onPrivate: () => {},
                // conversation enregistrée sur la gateway (19.1)
                server: false, convId: null, revision: null, title: "", onChange: () => {}, onNotice: () => {},
                project: null,                            // projet de la conversation : instructions et fichiers
                generation: 0, noticeIsError: false, speech: false };
const MAX_STORED = 500;                                   // messages par conversation (côté serveur aussi)
const SERVER_BUDGET = 7_500_000;                         // caractères : sous la limite de la gateway (8 Mo)
const LOCAL_BUDGET = 2_000_000;                          // localStorage : ~5 Mo par site, en UTF-16
const REQUEST_BUDGET = 8_500_000;                        // corps de requête : 10 Mo au plus (9.1)
const textOf = (content) => (typeof content === "string" ? content
  : (content || []).filter((p) => p.type === "text").map((p) => p.text).join("\n"));
const imagesOf = (content) => (typeof content === "string" ? []
  : (content || []).filter((p) => p.type === "image_url").map((p) => p.image_url.url));
const OLD_IMAGE = { type: "text", text: "[image d'un message précédent, non conservée]" };
// réflexion des modèles qui raisonnent (DeepSeek-R1, Qwen3…) : « <think>…</think> » en tête de la réponse, ou champ
// reasoning du flux (gardé sous la même forme). Affichée dans un bloc repliable, comme Claude ; jamais renvoyée à l'IA,
// ni copiée, ni lue à voix haute.
const THINK = /^\s*<think>([\s\S]*?)(<\/think>|$)/i;
const answerOf = (text) => text.replace(THINK, "").trimStart();
const forModel = (content) => (typeof content === "string" ? answerOf(content) : content);

function renderAnswer(text, { streaming = false, open = null } = {}) {
  const m = text.match(THINK);
  if (!m || !m[1].trim()) return renderMarkdown(m ? answerOf(text) : text);
  const thinking = streaming && !m[2];                      // pas encore de réponse : le bloc reste ouvert
  return `<details class="reasoning"${open ?? thinking ? " open" : ""}><summary>` +
    `${thinking ? "Réflexion en cours…" : "Réflexion"}</summary>` +
    `<div class="reasoning-body">${renderMarkdown(m[1].trim())}</div></details>${renderMarkdown(answerOf(text))}`;
}
// début du contexte envoyé à l'IA : le dernier résumé (/compacte), sinon le premier message
const contextStart = () => Math.max(0, state.messages.findLastIndex((m) => m.meta?.compact));
const IMAGE_TOKENS = 1000;                                // ordre de grandeur d'une image pour un modèle vision
const estimate = (m) => Math.ceil(textOf(m.content).length / 4) + 4 + imagesOf(m.content).length * IMAGE_TOKENS;

/** Contexte envoyé à l'IA (cercle de contexte) : les tokens mesurés de la dernière réponse, plus l'estimation de ce
 *  qui a suivi ; à défaut, une estimation (4 caractères par token, comme la gateway). */
function contextUsage() {
  const recent = state.messages.slice(contextStart());
  const k = recent.findLastIndex((m) => m.role === "assistant" && m.meta?.usage?.prompt_tokens);
  const u = k >= 0 && recent[k].meta.usage;
  const tokens = (u ? u.prompt_tokens + (u.completion_tokens || 0) : 0)
    + recent.slice(k + 1).reduce((n, m) => n + estimate(m), 0);
  const by = state.messages.findLast((m) => m.role === "assistant" && m.meta?.model)?.meta;   // même avant un résumé
  return [tokens, by ? `${by.provider}/${by.model}` : null];
}

// images jointes (19.2) : au-delà du budget, les plus anciennes sont remplacées par une mention
function fitBudget(list, budget) {
  let size = JSON.stringify(list).length;
  if (size <= budget) return list;
  const out = list.map((m) => ({ ...m }));
  for (const m of out) {
    if (size <= budget) break;
    if (typeof m.content === "string") continue;
    const before = JSON.stringify(m.content).length;
    m.content = m.content.map((p) => (p.type === "image_url" ? OLD_IMAGE : p));
    size -= before - JSON.stringify(m.content).length;
  }
  return out;
}
const MAX_VERSIONS = 20;                                  // variantes d'un message (comme la gateway)
const snapshot = (m) => ({ content: m.content, ...(m.meta && { meta: m.meta }),
                           ...(m.attachments?.length && { attachments: m.attachments }) });
const stored = (list, budget = SERVER_BUDGET) => fitBudget(list.slice(-MAX_STORED).map(
  (m) => ({ role: m.role, ...snapshot(m),
            ...(m.versions && { versions: m.versions.map((v, k) => (k === m.active ? snapshot(m) : v)),
                                active: m.active }) })), budget);

// ---------- variantes (19.3) : chaque version garde la suite de la conversation qui la suivait ----------
function freeze(i) {                                      // la suite visible est rangée dans la version active
  const m = state.messages[i];
  if (!m.versions) { m.versions = [snapshot(m)]; m.active = 0; }
  m.versions[m.active] = { ...snapshot(m), tail: state.messages.slice(i + 1) };
}
function activate(i, k) {                                 // la version k devient visible, avec sa suite
  const m = state.messages[i];
  const { tail = [], ...v } = m.versions[k];
  m.content = v.content;
  for (const f of ["meta", "attachments"]) { if (v[f]) m[f] = v[f]; else delete m[f]; }
  m.versions[k] = snapshot(m);
  m.active = k;
  state.messages = [...state.messages.slice(0, i + 1), ...tail];
}
function showVersion(i, k) {
  if (state.controller || !state.messages[i]?.versions?.[k]) return;
  freeze(i);
  activate(i, k);
  save();
  renderAll();
  const last = [...state.messages].reverse().find((m) => m.role === "assistant" && m.meta);
  state.onRestore(last?.meta || null);
}
function regenerate(i, model) {
  const m = state.messages[i];
  if (state.controller || m?.role !== "assistant") return;
  if ((m.versions?.length || 1) >= MAX_VERSIONS) return notice(`${MAX_VERSIONS} versions au plus pour une réponse.`);
  freeze(i);
  const before = { versions: m.versions, active: m.active, index: i };
  state.messages = state.messages.slice(0, i);
  renderAll();
  generate({ model, before });
}
function restoreVersions(before) {                        // régénération ratée : la réponse d'avant revient
  const k = before.active;
  state.messages = state.messages.slice(0, before.index);
  state.messages.push({ role: "assistant", ...before.versions[k], versions: before.versions, active: k });
  activate(before.index, k);
  renderAll();
}
function editMessage(i, text) {
  const m = state.messages[i];
  if (state.controller || m?.role !== "user") return;
  if ((m.versions?.length || 1) >= MAX_VERSIONS) return notice(`${MAX_VERSIONS} versions au plus pour un message.`);
  freeze(i);
  const images = typeof m.content === "string" ? [] : m.content.filter((p) => p.type === "image_url");
  m.content = images.length ? [...(text ? [{ type: "text", text }] : []), ...images] : text;
  m.versions.push(snapshot(m));
  m.active = m.versions.length - 1;
  state.messages = state.messages.slice(0, i + 1);
  save();
  renderAll();
  generate();
}

// ---------- actions sous un message ----------
const ACTION_ICONS = {
  copy: '<path d="M9 9h10v12H9z"/><path d="M5 15V3h10"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  regen: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
  listen: SPEECH_ICONS.listen,
};
function actionButton(kind, label, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "msg-act";
  b.title = label;
  b.setAttribute("aria-label", label);
  b.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true">${ACTION_ICONS[kind]}</svg>`;
  b.addEventListener("click", onClick);
  return b;
}
let modelList = null;
/** Menu « Réessayer » (row-menu.js) : même modèle en tête, puis les autres, chargés à la première ouverture. */
function retryMenu(i) {
  const menu = document.createElement("details");
  menu.className = "row-menu align-start msg-retry";
  const summary = document.createElement("summary");
  summary.className = "msg-act";
  summary.title = "Réessayer";
  summary.setAttribute("aria-label", "Réessayer");
  summary.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true">${ACTION_ICONS.regen}</svg>`;
  const box = document.createElement("div");
  box.className = "menu";
  box.setAttribute("role", "menu");
  const pick = (label, model) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.textContent = label;                                 // texte brut
    b.addEventListener("click", () => { menu.open = false; regenerate(i, model); });
    return b;
  };
  box.append(pick("Réessayer"));
  menu.append(summary, box);
  menu.addEventListener("toggle", async () => {
    if (!menu.open || box.dataset.loaded) return;
    box.dataset.loaded = "1";                              // un seul chargement, pas de doublons
    modelList ||= api("/v1/models").then((r) => (r.ok ? r.data.data : []));
    const models = (await modelList).filter((m) => m.metadata?.chat !== false);  // de chat (embeddings, voix exclus)
    for (const [label, list] of [["Avec un modèle local", models.filter((m) => m.metadata?.hosting === "local")],
                                 ["Avec un modèle cloud", models.filter((m) => m.metadata?.hosting !== "local")]]) {
      if (!list.length) continue;
      const head = document.createElement("p");
      head.className = "menu-head";
      head.textContent = label;
      box.append(head, ...list.map((m) => pick(m.id, m.id)));
    }
  });
  return menu;
}
function renderActions(el, i) {
  el.querySelector(".msg-actions")?.remove();
  const m = state.messages[i];
  if (!m) return;
  const bar = document.createElement("div");
  bar.className = "msg-actions";
  if (m.versions) {                                       // ‹ 2/3 ›
    const nav = document.createElement("span");
    nav.className = "msg-versions";
    const prev = document.createElement("button"), next = document.createElement("button");
    const label = document.createElement("span");
    prev.type = next.type = "button";
    prev.textContent = "‹";
    next.textContent = "›";
    prev.setAttribute("aria-label", "Version précédente");
    next.setAttribute("aria-label", "Version suivante");
    prev.disabled = m.active === 0;
    next.disabled = m.active === m.versions.length - 1;
    label.textContent = `${m.active + 1}/${m.versions.length}`;
    prev.addEventListener("click", () => showVersion(i, m.active - 1));
    next.addEventListener("click", () => showVersion(i, m.active + 1));
    nav.append(prev, label, next);
    bar.append(nav);
  }
  if (m.role === "assistant") {
    const copy = actionButton("copy", "Copier la réponse", async () => {
      await copyText(answerOf(textOf(m.content)));
      copy.classList.add("done");
      setTimeout(() => copy.classList.remove("done"), 1500);
    });
    const listen = actionButton("listen", "Écouter la réponse", () =>
      toggleSpeech(listen, answerOf(textOf(m.content)), { isPrivate: state.private, onError: (t) => notice(t, true) }));
    listen.classList.add("listen");
    listen.hidden = !state.speech;                        // aucun serveur de synthèse vocale : pas de bouton
    bar.append(copy, listen, retryMenu(i));              // Réessayer : même modèle ou un autre, dans un seul menu
    el.querySelector(".content").append(bar);
  } else {
    bar.append(actionButton("edit", "Modifier le message", () => startEdit(el, i)));
    el.querySelector(".user-col").append(bar);
  }
}
function startEdit(el, i) {
  if (state.controller) return;
  const m = state.messages[i];
  const body = el.querySelector(".body");
  const form = document.createElement("form");
  form.className = "msg-edit";
  const area = document.createElement("textarea");
  area.value = textOf(m.content);
  area.rows = Math.min(10, Math.max(2, area.value.split("\n").length));
  area.setAttribute("aria-label", "Modifier le message");
  const ok = document.createElement("button"), cancel = document.createElement("button");
  ok.type = "submit";
  ok.className = "btn btn-primary btn-inline";
  ok.textContent = "Envoyer";
  cancel.type = "button";
  cancel.className = "btn btn-ghost btn-inline";
  cancel.textContent = "Annuler";
  const row = document.createElement("div");
  row.append(cancel, ok);
  form.append(area, row);
  body.hidden = true;
  el.querySelector(".msg-actions")?.setAttribute("hidden", "");
  body.after(form);
  area.focus();
  const close = () => { form.remove(); body.hidden = !body.textContent; el.querySelector(".msg-actions")?.removeAttribute("hidden"); };
  cancel.addEventListener("click", close);
  area.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = area.value.trim();
    if (!text && !imagesOf(m.content).length) return;
    if (text === textOf(m.content).trim()) return close();
    editMessage(i, text);
  });
}

// ---------- export (19.3) ----------
export function exportConversation(format) {
  if (!state.messages.length) return null;
  return download(state.title || "Conversation", format,
                  format === "md" ? state.messages : stored(state.messages, Infinity));
}

// ---------- persistance dans ce navigateur (sans clé, ou au choix) ----------
function load() {
  try { return JSON.parse(localStorage.getItem(state.storageKey)) || []; } catch { return []; }
}
function saveLocal() {
  try { localStorage.setItem(state.storageKey, JSON.stringify(stored(state.messages.slice(-200), LOCAL_BUDGET))); }
  catch { /* plein / privé */ }
}
function remember(id) {                                   // dernière conversation ouverte sur cet appareil
  try {
    if (id) localStorage.setItem(`${state.storageKey}.current`, id);
    else localStorage.removeItem(`${state.storageKey}.current`);
  } catch { /* ignoré */ }
}

// ---------- persistance sur la gateway : une écriture à la fois, toujours l'état le plus récent ----------
let queue = Promise.resolve();
function save() {
  updateContextRing(...contextUsage());
  if (!state.server) return saveLocal();
  queue = queue.then(sync, sync);
  return queue;
}
async function sync() {
  const messages = stored(state.messages);
  if (!messages.length && !state.convId) return;
  const r = state.convId
    ? await api(`/v1/conversations/${encodeURIComponent(state.convId)}/messages`,
                { method: "PUT", body: { messages, revision: state.revision } })
    : await api("/v1/conversations", { method: "POST",
                                       body: { messages, private: state.private, project: state.project } });
  if (r.ok) {
    if (!state.convId) { state.convId = r.data.id; remember(r.data.id); }
    state.revision = r.data.revision;
    if (state.noticeIsError) notice("");                  // enregistrement rétabli
    state.onChange();
    return;
  }
  if (r.status === 401) return state.onAuthError();
  if (r.status === 409 && state.convId) return conflict();
  if (r.status === 404 && state.convId) {                 // supprimée depuis un autre appareil : recréée
    state.convId = state.revision = null;
    return sync();
  }
  notice(`Conversation non enregistrée : ${r.data?.error?.message || `erreur ${r.status}`}`, true);
}
function notice(text, isError = false) {
  state.noticeIsError = isError && !!text;
  state.onNotice(text);
}
async function conflict() {
  // un autre appareil a écrit entre-temps : sa version fait foi ; le message non enregistré revient dans le champ
  const mine = state.messages[state.messages.length - 1];
  const r = await api(`/v1/conversations/${encodeURIComponent(state.convId)}`);
  if (!r.ok) return notice("Conversation modifiée ailleurs, et impossible à recharger.", true);
  const theirs = r.data.messages;
  if (mine?.role === "user" && textOf(theirs[theirs.length - 1]?.content) !== textOf(mine.content)
      && !$("prompt").value) {
    $("prompt").value = textOf(mine.content);
    autosize();
  }
  state.controller?.abort();                             // la réponse en cours concernait l'ancienne version
  apply(r.data);
  notice("Conversation modifiée sur un autre appareil : version la plus récente chargée.");
}
function apply(conv) {
  state.generation++;                                     // une réponse encore en cours n'y sera pas ajoutée
  state.convId = conv.id;
  state.revision = conv.revision;
  state.title = conv.title;
  state.messages = conv.messages || [];
  state.private = conv.private;
  state.project = conv.project_id || null;
  state.onPrivate(state.private);
  renderAll();
  conversationShown();
  const last = [...state.messages].reverse().find((m) => m.role === "assistant" && m.meta);
  state.onRestore(last?.meta || null);
  state.onChange();
}
async function giveTitle() {
  // après le premier échange : titre par une IA locale de la gateway (sinon début du message)
  await queue;
  if (!state.server || !state.convId || state.title) return;
  const id = state.convId;
  const r = await api(`/v1/conversations/${encodeURIComponent(id)}/title`, { method: "POST" });
  if (r.ok && state.convId === id) { state.title = r.data.title; state.onChange(); }
}

// ---------- mode privé de la conversation : IA locales seulement (ce PC ou le réseau local) ----------
function loadPrivate() {
  try { return localStorage.getItem(`${state.storageKey}.private`) === "1"; } catch { return false; }
}
export function isPrivate() { return state.private; }
export { notice };
export function setPrivate(on) {
  state.private = !!on;
  if (state.server) {                                     // propre à la conversation, enregistré avec elle
    if (state.convId) {
      api(`/v1/conversations/${encodeURIComponent(state.convId)}`, { method: "PATCH", body: { private: state.private } });
    }
  } else {
    try {
      if (state.private) localStorage.setItem(`${state.storageKey}.private`, "1");
      else localStorage.removeItem(`${state.storageKey}.private`);
    } catch { /* ignoré */ }
  }
  state.onPrivate(state.private);
}

// ---------- rendu ----------
function nearBottom() {
  const s = $("scroller");
  return s.scrollHeight - s.scrollTop - s.clientHeight < 120;
}
function scrollToBottom(force = false) {
  const s = $("scroller");
  if (force || nearBottom()) s.scrollTop = s.scrollHeight;
}

function attachMetrics(el, meta) {
  const m = renderMetrics(meta);
  if (m) el.querySelector(".content").appendChild(m);
}

function renderAttachments(el, content, attachments) {
  const images = imagesOf(content);
  if (!images.length && !attachments?.length) return;
  const box = document.createElement("div");
  box.className = "msg-attachments";
  for (const url of images) {
    const img = document.createElement("img");
    img.src = url;                                        // data:image/… (vérifiée par la gateway)
    img.alt = "Image jointe";
    img.loading = "lazy";
    box.append(img);
  }
  for (const a of attachments || []) {
    const chip = document.createElement("span");
    chip.className = "msg-doc";
    chip.textContent = a.filename;
    if (a.size_bytes) chip.title = `${a.filename} · ${sizeLabel(a.size_bytes)}`;
    box.append(chip);
  }
  el.prepend(box);
}

function addMessage(role, content = "", meta = null, attachments = null, index = null) {
  const el = document.createElement("article");
  el.className = `msg ${role}`;
  if (role === "system") {                                // résumé de /compacte : replié, la suite part de là
    el.className = "msg compact";
    const details = document.createElement("details");
    const label = document.createElement("summary");
    const n = meta?.compact?.summarized_messages;
    label.textContent = `Conversation compactée${n ? ` · ${n} messages résumés` : ""} — l'IA ne voit plus que ce résumé et la suite`;
    const body = Object.assign(document.createElement("div"), { className: "body" });
    body.innerHTML = renderMarkdown(textOf(content).replace(/^[^\n]*\n/, ""));   // sans l'en-tête « Résumé des échanges… »
    details.append(label, body);
    el.append(details);
    $("messages").appendChild(el);
    document.body.classList.add("has-messages");
    return el;
  }
  if (role === "assistant") {
    el.innerHTML = `<div class="avatar">${AVATAR}</div><div class="content"><div class="body"></div></div>`;
    el.querySelector(".body").innerHTML = renderAnswer(textOf(content));
    if (meta) attachMetrics(el, meta);
  } else {
    el.innerHTML = '<div class="user-col"><div class="body"></div></div>';
    const body = el.querySelector(".body");
    body.textContent = textOf(content);                   // texte brut : jamais interprété
    const command = commandLabel(textOf(content));
    if (command) {                                        // « /pc » mis en évidence
      const chip = Object.assign(document.createElement("span"), { className: "slash-chip", textContent: command.label });
      body.replaceChildren(chip, ` ${command.rest}`);
    }
    body.hidden = !body.textContent;
    renderAttachments(el.querySelector(".user-col"), content, attachments);
  }
  $("messages").appendChild(el);
  if (index !== null) renderActions(el, index);
  document.body.classList.add("has-messages");
  return el;
}

function renderAll() {
  stopSpeaking();                                         // autre conversation, nouvelle conversation…
  $("messages").innerHTML = "";
  document.body.classList.toggle("has-messages", state.messages.length > 0);
  const start = contextStart();
  state.messages.forEach((m, i) => addMessage(m.role, m.content, m.meta, m.attachments, i)
    .classList.toggle("archived", i < start));            // avant le dernier compactage : affiché, plus envoyé
  updateContextRing(...contextUsage());
  scrollToBottom(true);
}

// ---------- envoi & streaming ----------
function setBusy(on) {
  $("composer").classList.toggle("busy", on);
  $("send").setAttribute("aria-label", on ? "Arrêter la génération" : "Envoyer");
  updateSend();
}
function updateSend() {
  const busy = $("composer").classList.contains("busy");
  $("send").disabled = !busy && (!attachmentsReady() || (!$("prompt").value.trim() && !hasAttachments()));
}

async function* sse(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const event = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      for (const line of event.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (data === "[DONE]") return;
        try { yield JSON.parse(data); } catch { /* fragment ignoré */ }
      }
    }
  }
}

async function generate({ model = null, before = null } = {}) {
  const el = addMessage("assistant");
  el.classList.add("streaming");
  const body = el.querySelector(".body");
  body.innerHTML = THINKING;
  scrollToBottom(true);

  state.controller = new AbortController();
  setBusy(true);
  const generation = state.generation;
  const current = () => generation === state.generation;  // toujours la même conversation affichée ?
  let text = "", reasoning = "", meta = null, usage = null, frame = 0, ttft, thinkOpen = null;
  const t0 = performance.now();
  // réflexion reçue à part (champ reasoning) : gardée devant la réponse, sous la forme « <think>…</think> »
  const full = () => (reasoning ? `<think>${reasoning}${text ? "</think>" : ""}${text}` : text);
  const paint = (final = false) => {
    frame = 0;
    const stick = nearBottom();
    body.innerHTML = renderAnswer(full(), { streaming: !final, open: thinkOpen });
    if (stick) scrollToBottom(true);
  };
  // bloc « Réflexion » ouvert ou fermé à la main : il le reste malgré l'affichage refait à chaque morceau
  body.addEventListener("click", (e) => {
    const summary = e.target.closest(".reasoning > summary");
    if (summary) thinkOpen = !summary.parentElement.open;
  });

  state.onStart();
  const asked = [...state.messages].reverse().find((m) => m.role === "user");
  try {
    const res = await fetch("/v1/chat/completions", {
      method: "POST", signal: state.controller.signal,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${getKey()}` },
      body: JSON.stringify({ ...state.getOptions(), ...(model && { model }),   // autre modèle demandé (19.3)
                             ...commandOptions(textOf(asked?.content)),    // « /pc … », « /web … »
                             stream: true,                                 // mode / effort / multi-agents (5.4)
                             ...(state.private && { private: true }),      // refusé par le serveur hors IA locale
                             ...(state.project && { project: state.project }),   // instructions et fichiers du projet
                             ...documentsOption(),                         // documents joints (19.2)
                             // préférences personnelles et style (profile.js) : en tête, jamais enregistrés
                             messages: [...[state.getSystem()].filter(Boolean),
                                        ...fitBudget(state.messages.slice(contextStart()).map(({ role, content }) =>
                                          ({ role, content: role === "user" ? stripCommand(content) : forModel(content) })),
                                        REQUEST_BUDGET)] }),
    });
    if (!res.ok) {
      let msg = `Erreur ${res.status}`;
      try { msg = (await res.json()).error?.message || msg; } catch { /* corps vide */ }
      if (res.status === 401) state.onAuthError();
      throw new Error(msg);
    }
    for await (const chunk of sse(res)) {
      if (chunk.error) throw new Error(chunk.error.message || "Erreur du fournisseur.");
      const delta = chunk.choices?.[0]?.delta?.content;
      const thought = chunk.choices?.[0]?.delta?.reasoning_content ?? chunk.choices?.[0]?.delta?.reasoning;
      if (delta || thought) {
        if (ttft === undefined) ttft = Math.round(performance.now() - t0);      // premier token (5.5)
        if (delta) text += delta;
        if (thought && !text) reasoning += thought;
        if (!frame) frame = requestAnimationFrame(() => paint());
      }
      const split = chunk.x_gateway_progress?.split;       // message trop long, lu par parties (13.4)
      if (split && !text && !reasoning) {
        const done = Math.min(split.done + 1, split.parts);
        body.innerHTML = THINKING +
          `<p class="split-progress" role="status">Message long, lu en ${split.parts} parties : ` +
          (split.done < split.parts ? `partie ${done} sur ${split.parts}…` : "synthèse…") + "</p>";
      }
      if (chunk.x_gateway_tool) toolEvent(el, chunk.x_gateway_tool);   // outils exécutés par la gateway
      if (chunk.usage) usage = chunk.usage;
      if (chunk.x_gateway) meta = chunk.x_gateway;
    }
    cancelAnimationFrame(frame);
    paint(true);
    if (!answerOf(full()).trim()) body.insertAdjacentHTML("beforeend", '<p class="muted">(réponse vide)</p>');
    if (meta) meta = { ...meta, usage: usage || meta.usage, ttft_ms: ttft };
    if (!current()) return;
    keep(el, { role: "assistant", content: full(), meta }, before);
    if (state.server && !state.title) giveTitle();
    if (meta) attachMetrics(el, meta);
    renderActions(el, state.messages.length - 1);
    state.onMeta(meta, el);
  } catch (err) {
    cancelAnimationFrame(frame);
    if (!current()) {
      el.remove();
    } else if (err.name === "AbortError") {
      paint(true);
      if (text || reasoning) {
        body.insertAdjacentHTML("beforeend", '<p class="stopped">Réponse interrompue.</p>');
        keep(el, { role: "assistant", content: full(), meta: null }, before);
        renderActions(el, state.messages.length - 1);
      } else if (before) {
        restoreVersions(before);
      } else {
        el.remove();
      }
    } else if (before) {                                   // régénération ratée : la réponse d'avant revient
      restoreVersions(before);
      notice(`Régénération impossible : ${err.message}`, true);
    } else {
      showError(el, err.message, text);
    }
  } finally {
    if (!meta) state.onMeta(null, el);                 // arrêt / erreur : le badge sort de « Routage… »
    el.classList.remove("streaming");
    state.controller = null;
    setBusy(false);
    $("prompt").focus();
  }
  if (meta && current() && contextRatio() >= AUTO_COMPACT) compact("", true);   // contexte presque plein
}

// ---------- outils de la gateway : étapes au-dessus de la réponse, validation des actions sur le PC ----------
function toolEvent(el, ev) {
  let log = el.querySelector(".tool-log");
  if (!log) {
    log = document.createElement("ol");
    log.className = "tool-log";
    log.setAttribute("aria-label", "Outils utilisés");
    el.querySelector(".content").prepend(log);
  }
  let row = [...log.children].find((li) => li.dataset.id === ev.id);
  if (ev.type === "call") {
    row = document.createElement("li");
    row.className = "tool-step running";
    row.dataset.id = ev.id;
    const name = document.createElement("code");
    name.textContent = ev.name;
    const args = document.createElement("span");
    args.className = "tool-args";
    args.textContent = argsLabel(ev.args);
    row.append(name, " ", args);
    log.append(row);
  } else if (row && ev.type === "approval") {
    row.append(approvalCard(ev));
  } else if (row && ev.type === "result") {
    row.classList.remove("running");
    row.classList.add(ev.ok ? "ok" : "failed");
    row.querySelector(".tool-approval")?.remove();
    row.title = ev.text || "";
  }
  if (nearBottom()) scrollToBottom(true);
}

function argsLabel(args) {
  const text = Object.entries(args || {})
    .map(([k, v]) => `${k} : ${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ");
  return text.length > 140 ? `${text.slice(0, 139)}…` : text;
}

function approvalCard(ev) {
  const box = document.createElement("div");
  box.className = "tool-approval";
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", "Action à valider");
  if (ev.preview) {
    const img = document.createElement("img");
    img.src = ev.preview;                                  // data:image/jpeg uniquement (CSP img-src data:)
    img.alt = "Aperçu de l'écran, cible de l'action entourée en rouge";
    box.append(img);
  }
  const p = document.createElement("p");
  p.textContent = "L'IA veut agir sur ce PC. Autoriser cette action ?";
  const row = document.createElement("div");
  row.className = "row";
  const decide = async (approve) => {
    for (const b of row.children) b.disabled = true;
    const r = await api(`/v1/tools/approvals/${encodeURIComponent(ev.approval)}`, { method: "POST", body: { approve } });
    p.textContent = r.ok ? (approve ? "Autorisée, en cours…" : "Refusée.") : (r.data?.error?.message || "Validation impossible.");
  };
  for (const [label, approve, cls] of [["Autoriser", true, "btn btn-primary"], ["Refuser", false, "btn"]]) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.textContent = label;
    b.addEventListener("click", () => decide(approve));
    row.append(b);
  }
  box.append(p, row);
  return box;
}

// nouvelle réponse : ajoutée, ou version supplémentaire d'une réponse régénérée
function keep(el, message, before) {
  if (before) {
    const versions = [...before.versions, snapshot(message)];
    Object.assign(message, { versions, active: versions.length - 1 });
  }
  state.messages.push(message);
  save();
}

// tous les documents joints à la conversation : la gateway en injecte le texte, ou les extraits utiles
function documentsOption() {
  const ids = [...new Set(state.messages.slice(contextStart()).flatMap((m) => (m.attachments || []).map((a) => a.id)))];
  return ids.length ? { documents: ids } : {};
}

function showError(el, message, partial) {
  const body = el.querySelector(".body");
  body.innerHTML = renderMarkdown(partial || "");
  const box = document.createElement("div");
  box.className = "msg-error";
  box.setAttribute("role", "alert");
  const span = document.createElement("span");
  span.textContent = message;
  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = "Réessayer";
  retry.addEventListener("click", () => { el.remove(); generate(); });
  box.append(span, retry);
  body.appendChild(box);
}

// /compacte [consigne] : résumé de la conversation par la gateway, gardé dans le fil (rôle system)
async function compact(instructions, auto = false) {
  if (state.controller) return notice("Attendez la fin de la réponse avant de compacter.", true);
  const recent = state.messages.slice(contextStart());
  if (!recent.some((m) => !m.meta?.compact)) return notice("Rien à compacter depuis le dernier résumé.", true);
  const generation = state.generation;
  notice(auto ? `Contexte plein à ${Math.round(contextRatio() * 100)} % : compactage automatique de la conversation…`
              : "Compactage de la conversation…");
  const r = await api("/v1/compact", { method: "POST", body: {
    messages: fitBudget(recent.map(({ role, content }) =>
      ({ role, content: role === "user" ? stripCommand(content) : forModel(content) })), REQUEST_BUDGET),
    ...(instructions && { instructions }),
    ...(state.private && { private: true }),                // résumé par une IA locale seulement
  } });
  if (generation !== state.generation) return;            // autre conversation ouverte entre-temps
  if (!r.ok) {
    if (r.status === 401) state.onAuthError();
    return notice(r.data?.error?.message || `Compactage impossible (erreur ${r.status}).`, true);
  }
  const { summary, ...report } = r.data;
  state.messages.push({ role: "system", content: summary, meta: { compact: report } });
  save();
  renderAll();
  notice(`Conversation compactée${auto ? " automatiquement" : ""} : ${report.original_tokens} → ${report.summary_tokens} tokens environ.`);
}

// commandes « / » exécutées dans la page (/memoire, /aide…) : leur résultat s'affiche dans le fil, sans être gardé
const slashUi = {
  notice, newConversation, setPrivate, isPrivate, compact,
  show(box) {
    const el = document.createElement("article");
    el.className = "msg assistant slash-result";
    el.innerHTML = `<div class="avatar">${AVATAR}</div><div class="content"></div>`;
    el.querySelector(".content").append(box);
    $("messages").appendChild(el);
    document.body.classList.add("has-messages");
    scrollToBottom(true);
  },
};

async function submit(event) {
  event?.preventDefault();
  if (state.controller) { state.controller.abort(); return; }      // le bouton sert aussi d'arrêt
  let text = $("prompt").value.trim();
  if (!attachmentsReady() || (!text && !hasAttachments())) return;
  const command = await handleCommand(text, slashUi);
  if (command && !command.send) {
    if (!command.keep) { $("prompt").value = ""; autosize(); }
    return;
  }
  const { images, docs } = takeAttachments();
  if (!text && !images.length) text = docs.length > 1 ? "Analyse les documents joints." : "Analyse le document joint.";
  $("prompt").value = "";
  autosize();
  notice("");
  // images : message en parties (texte + image_url), comme l'API OpenAI ; la gateway choisit un modèle vision
  const content = images.length
    ? [...(text ? [{ type: "text", text }] : []), ...images.map((url) => ({ type: "image_url", image_url: { url } }))]
    : text;
  const message = { role: "user", content, ...(docs.length && { attachments: docs }) };
  state.messages.push(message);
  save();
  addMessage("user", content, null, message.attachments, state.messages.length - 1);
  generate();
}

function autosize() {
  const t = $("prompt");
  t.style.height = "auto";
  t.style.height = `${Math.min(t.scrollHeight, window.innerHeight * 0.4)}px`;
  updateSend();
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}

async function copyCode(button) {
  await copyText(document.getElementById(button.dataset.copy)?.textContent ?? "");
  button.textContent = "Copié ✓";
  setTimeout(() => { button.textContent = "Copier"; }, 1500);
}

// conversation affichée (ouverte, nouvelle) : la page des projets (projects-ui.js) suit
function conversationShown() {
  document.dispatchEvent(new CustomEvent("gw-conversation"));
}

// project : nouvelle conversation dans ce projet (ses instructions et ses fichiers servent à chaque réponse)
// ---------- suggestions de l'accueil : 4 tirées au hasard, une par thème, à chaque nouvelle conversation ----------
const SUGGESTIONS = [
  ["Écris un e-mail poli pour décaler un rendez-vous à la semaine prochaine",
   "Corrige l'orthographe et le style d'un texte que je vais coller",
   "Propose cinq titres accrocheurs pour un article sur le jardinage",
   "Rédige une courte présentation de moi pour un profil professionnel"],
  ["Écris une fonction Python qui déduplique une liste en gardant l'ordre",
   "Explique cette erreur et propose une correction : je colle le message",
   "Crée une page HTML avec un compteur qui augmente à chaque clic",
   "Compare SQLite et PostgreSQL sous forme de tableau"],
  ["Explique simplement comment fonctionne un réseau de neurones",
   "Résume les causes de la Première Guerre mondiale en 5 points",
   "Aide-moi à réviser : pose-moi 5 questions sur les fractions",
   "Explique-moi le routage intelligent entre IA locales et cloud"],
  ["Prépare un menu de la semaine équilibré pour deux personnes",
   "Organise ma journée : j'ai 3 tâches importantes et 2 rendez-vous",
   "Donne-moi un programme de course à pied pour débutant sur 4 semaines",
   "Trouve des idées de cadeaux pour un ami qui aime la photo"],
];

function showSuggestions() {
  const pick = (list) => list[Math.floor(Math.random() * list.length)];
  $("suggestions").replaceChildren(...SUGGESTIONS.map((theme) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "suggestion";
    b.textContent = pick(theme);
    return b;
  }));
}

export function newConversation(project = null) {
  state.controller?.abort();
  state.generation++;
  state.messages = [];
  state.project = state.server && typeof project === "string" ? project : null;
  if (state.server) {
    state.convId = state.revision = null;
    state.title = "";
    remember(null);
    state.onChange();
  } else {
    save();
  }
  setPrivate(false);                                        // le mode privé suit la conversation
  showSuggestions();
  renderAll();
  state.onRestore(null);
  conversationShown();
  $("prompt").focus();
}

// ---------- conversations enregistrées (19.1), pour le panneau latéral ----------
export function currentConversation() {
  return { id: state.convId, title: state.title, server: state.server, project: state.project,
           empty: !state.messages.length };
}
export async function openConversation(id, focus = null) {
  if (id === state.convId) { conversationShown(); reveal(focus); return true; }
  state.controller?.abort();
  await queue;                                              // la conversation quittée finit de s'enregistrer
  const r = await api(`/v1/conversations/${encodeURIComponent(id)}`);
  if (!r.ok) { notice(r.data?.error?.message || "Conversation introuvable.", true); return false; }
  apply(r.data);
  remember(id);
  $("prompt").focus();
  reveal(focus);
  return true;
}
function reveal(index) {                                  // message trouvé par la recherche (19.3)
  const el = index === null ? null : $("messages").children[index];
  if (!el) return;
  el.scrollIntoView({ block: "center" });
  el.classList.remove("flash");
  void el.offsetWidth;                                    // relance l'animation
  el.classList.add("flash");
}
export function conversationDeleted(id) {
  if (id === state.convId) newConversation();
}
// projet supprimé : la conversation affichée n'y est plus rattachée (le serveur l'a détachée)
export function projectDeleted(id) {
  if (state.project === id) { state.project = null; conversationShown(); }
}
// conversation rangée dans un projet (menu « ⋯ ») : la conversation affichée suit
export function conversationMoved(id, project) {
  if (id === state.convId) { state.project = project; conversationShown(); }
}
export function conversationRenamed(id, title) {
  if (id === state.convId) state.title = title;
}
// conversation gardée dans ce navigateur avant 19.1 : proposée à l'import
export function localLeftover() {
  return state.server ? load() : [];
}
export async function importLeftover() {
  const r = await api("/v1/conversations", { method: "POST", body: { messages: stored(load()) } });
  if (!r.ok) return r;
  forgetLeftover();
  await openConversation(r.data.id);
  giveTitle();
  return r;
}
export function forgetLeftover() {
  try { localStorage.removeItem(state.storageKey); localStorage.removeItem(`${state.storageKey}.private`); } catch { /* ignoré */ }
}

export function attachmentsChanged() { updateSend(); }

export async function initChat({ onMeta, onAuthError, getOptions, onStart, onRestore, onPrivate, onChange, onNotice,
                                 getSystem, server = false } = {}) {
  const key = getKey() || "";
  state.storageKey = `gw.chat.${key.slice(-10)}`;          // une conversation par clé, sans stocker la clé
  Object.assign(state, Object.fromEntries(Object.entries({ onMeta, onAuthError, getOptions, onStart, onRestore,
                                                           onPrivate, onChange, onNotice, getSystem })
    .filter(([, v]) => v)));
  state.server = server;
  mountContextRing($("toolbar"), () => compact());          // un appui sur le cercle : /compacte
  speechStatus().then((s) => {                             // bouton « Écouter » (19.4) si un serveur TTS répond
    state.speech = !!s.available;
    document.querySelectorAll(".msg-act.listen").forEach((b) => { b.hidden = !state.speech; });
  });
  if (server) {
    let current = null;
    try { current = localStorage.getItem(`${state.storageKey}.current`); } catch { /* ignoré */ }
    if (!(current && await openConversation(current))) {   // dernière ouverte sur cet appareil, sinon une neuve
      remember(null);
      notice("");
      renderAll();
    }
  } else {
    state.messages = load();
    state.private = loadPrivate();
    state.onPrivate(state.private);
    renderAll();
    const last = [...state.messages].reverse().find((m) => m.role === "assistant" && m.meta);
    if (last) state.onRestore(last.meta);                  // badge de la dernière IA utilisée
  }

  $("composer").addEventListener("submit", submit);
  initSlash($("prompt"));                                  // ses touches passent avant l'envoi par Entrée
  $("prompt").addEventListener("input", autosize);
  $("prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) submit(e);
    if (e.key === "Escape" && state.controller) state.controller.abort();
  });
  showSuggestions();
  $("suggestions").addEventListener("click", (e) => {
    const s = e.target.closest(".suggestion");
    if (!s) return;
    $("prompt").value = s.textContent.trim();
    submit();
  });
  initRowMenus();                                         // menu « Réessayer » sous les réponses
  $("messages").addEventListener("click", (e) => {
    const b = e.target.closest(".copy");
    if (b) copyCode(b);
    const dl = e.target.closest(".code-dl");
    if (dl) saveCode(dl);
    // écran tactile (pas de survol) : un appui sur un de vos messages montre ses actions (crayon), comme dans Claude
    const mine = e.target.closest(".msg.user");
    document.querySelectorAll(".msg.user.acts").forEach((m) => { if (m !== mine) m.classList.remove("acts"); });
    if (mine && e.target.closest(".body")) mine.classList.toggle("acts");
  });
  $("new-chat").addEventListener("click", newConversation);
  $("prompt").focus();
}
