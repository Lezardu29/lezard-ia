// Projets du chat (comme ceux de Claude) : des conversations regroupées qui partagent des instructions et des
// fichiers de référence. La gateway les applique à chaque réponse (/v1/projects, champ « project » du chat).
// - « Projets » (barre latérale) : la liste, création, ouverture.
// - Projet ouvert : nouvelle conversation dans ce projet ; l'accueil devient la page du projet (conversations,
//   instructions, fichiers) jusqu'au premier message.
// Noms, descriptions et noms de fichiers : texte brut, jamais interprété.
import { api, errorMessage, getKey } from "./auth.js";
import { currentConversation, newConversation, openConversation, projectDeleted } from "./chat.js";

const $ = (id) => document.getElementById(id);
const ui = { enabled: false, cache: new Map(), editing: null, token: 0, onLeave: () => {} };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function when(ts) {
  return new Date(ts * 1000).toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
}

async function project(id, fresh = false) {
  if (!fresh && ui.cache.has(id)) return ui.cache.get(id);
  const r = await api(`/v1/projects/${encodeURIComponent(id)}`);
  if (!r.ok) return null;
  ui.cache.set(id, r.data);
  return r.data;
}

// ---------- liste des projets ----------
export async function showProjects() {
  if (!ui.enabled) return;
  ui.onLeave();
  document.body.classList.add("projects-mode");
  $("projects-list").hidden = false;
  $("scroller").scrollTop = 0;
  const r = await api("/v1/projects");
  const items = r.ok ? [...r.data.data].sort((a, b) => b.updated_at - a.updated_at) : [];
  for (const p of items) ui.cache.set(p.id, p);
  $("projects-grid").replaceChildren(...items.map(card));
  $("projects-empty").hidden = items.length > 0;
  if (!r.ok) $("projects-empty").textContent = errorMessage(r);
}

function hideProjects() {
  document.body.classList.remove("projects-mode");
  $("projects-list").hidden = true;
}

function card(p) {
  const li = el("li");
  const b = el("button", "pl-card");
  b.type = "button";
  b.append(el("strong", "", p.name), el("span", "pl-desc", p.description || ""),
           el("span", "pl-date", `Modifié le ${when(p.updated_at)}`));
  b.addEventListener("click", () => enter(p.id));
  li.append(b);
  return li;
}

function enter(id) {
  hideProjects();
  newConversation(id);                                  // la page du projet s'affiche (événement gw-conversation)
}

// ---------- page du projet (accueil d'une conversation vide du projet) ----------
async function sync() {
  hideProjects();
  const token = ++ui.token;
  const conv = currentConversation();
  const p = conv.project ? await project(conv.project) : null;
  if (token !== ui.token) return;                       // une autre conversation a été affichée entre-temps
  $("conv-project").hidden = !p;
  if (p) $("conv-project").textContent = p.name;
  const home = !!p && conv.empty;
  document.body.classList.toggle("in-project", home);
  $("project-head").hidden = $("project-home").hidden = !home;
  if (!home) return;
  $("ph-name").textContent = p.name;
  $("ph-desc").textContent = p.description || "";
  $("ph-desc").hidden = !p.description;
  $("ph-instructions").value = p.system_prompt || "";
  $("ph-instr-status").textContent = "";
  await Promise.all([conversations(p, token), files(p, token)]);
}

async function conversations(p, token) {
  const r = await api(`/v1/conversations?project=${encodeURIComponent(p.id)}&limit=50`);
  if (token !== ui.token) return;
  const items = r.ok ? r.data.data : [];
  $("ph-list").replaceChildren(...items.map((c) => {
    const li = el("li");
    const b = el("button", "ph-conv");
    b.type = "button";
    b.append(el("span", "ph-conv-title", c.title || "Sans titre"), el("span", "ph-conv-date", when(c.updated_at)));
    b.addEventListener("click", () => openConversation(c.id));
    li.append(b);
    return li;
  }));
  $("ph-list-empty").hidden = items.length > 0;
}

async function files(p, token) {
  const r = await api("/v1/documents");
  if (token !== ui.token) return;
  const docs = new Map((r.ok ? r.data.data : []).map((d) => [d.id, d]));
  const mine = p.documents.map((id) => docs.get(id)).filter(Boolean);
  $("ph-files").replaceChildren(...mine.map((d) => {
    const li = el("li", "ph-file");
    li.append(el("span", "ph-file-name", d.filename));
    const x = el("button", "icon-btn ph-file-x", "✕");
    x.type = "button";
    x.title = `Retirer « ${d.filename} » du projet`;
    x.setAttribute("aria-label", x.title);
    x.addEventListener("click", () => removeFile(p, d));
    li.append(x);
    return li;
  }));
  $("ph-files-empty").hidden = mine.length > 0;
}

const shown = () => ui.cache.get(currentConversation().project);

async function saveInstructions() {
  const p = shown();
  if (!p) return;
  $("ph-instr-status").textContent = "Enregistrement…";
  const r = await api(`/v1/projects/${encodeURIComponent(p.id)}`, {
    method: "PATCH", body: { system_prompt: $("ph-instructions").value.trim() || null },
  });
  if (r.ok) ui.cache.set(p.id, r.data);
  $("ph-instr-status").textContent = r.ok ? "Enregistré ✓" : errorMessage(r);
}

async function addFiles(list) {
  const p = shown();
  if (!p || !list.length) return;
  for (const [i, file] of [...list].entries()) {
    $("ph-files-status").textContent = `Envoi de « ${file.name} » (${i + 1}/${list.length})…`;
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("project", p.id);                       // rattaché au projet par la gateway
    const key = getKey();
    let res, data = null;
    try {
      res = await fetch("/v1/documents", { method: "POST", body: form, headers: key ? { Authorization: `Bearer ${key}` } : {} });
      data = await res.json();
    } catch { /* gateway injoignable, ou réponse vide */ }
    if (!res?.ok) {
      $("ph-files-status").textContent = `« ${file.name} » : ${errorMessage({ status: res?.status ?? 0, data })}`;
      break;
    }
    $("ph-files-status").textContent = "";
  }
  await refresh();
}

async function removeFile(p, d) {
  if (!confirm(`Retirer « ${d.filename} » du projet ? Le fichier est supprimé de la gateway.`)) return;
  const r = await api(`/v1/documents/${encodeURIComponent(d.id)}`, { method: "DELETE" });
  if (!r.ok && r.status !== 404) return alert(errorMessage(r));
  await refresh();
}

async function refresh() {
  const id = currentConversation().project;
  if (id) await project(id, true);
  await sync();
}

// ---------- création, modification, suppression ----------
function openDialog(p = null) {
  ui.editing = p;
  $("pd-title").textContent = p ? "Modifier le projet" : "Nouveau projet";
  $("pd-ok").textContent = p ? "Enregistrer" : "Créer le projet";
  $("pd-name").value = p?.name || "";
  $("pd-desc").value = p?.description || "";
  $("pd-error").hidden = true;
  $("project-dialog").showModal();
  $("pd-name").focus();
}

async function submitDialog(e) {
  e.preventDefault();
  const body = { name: $("pd-name").value.trim(), description: $("pd-desc").value.trim() || null };
  if (!body.name) return;
  const p = ui.editing;
  const r = p ? await api(`/v1/projects/${encodeURIComponent(p.id)}`, { method: "PATCH", body })
              : await api("/v1/projects", { method: "POST", body });
  if (!r.ok) {
    $("pd-error").textContent = errorMessage(r);
    $("pd-error").hidden = false;
    return;
  }
  ui.cache.set(r.data.id, r.data);
  $("project-dialog").close();
  if (p) await sync();
  else enter(r.data.id);
}

async function removeProject() {
  const p = shown();
  if (!p || !confirm(`Supprimer le projet « ${p.name} » ? Ses conversations restent, hors projet ; ses instructions sont perdues.`)) return;
  const r = await api(`/v1/projects/${encodeURIComponent(p.id)}`, { method: "DELETE" });
  if (!r.ok && r.status !== 404) return alert(errorMessage(r));
  ui.cache.delete(p.id);
  projectDeleted(p.id);
  showProjects();
}

// onLeave : la barre latérale se referme sur téléphone quand une page de projets s'ouvre
export function initProjects(me, server, { onLeave } = {}) {
  ui.enabled = server && !!me.conversations?.stored;    // conversations enregistrées sur la gateway seulement
  if (onLeave) ui.onLeave = onLeave;
  $("sb-projects").hidden = $("conv-projects").hidden = !ui.enabled;
  if (!ui.enabled) return;
  $("sb-projects").addEventListener("click", showProjects);
  $("conv-projects").addEventListener("click", showProjects);
  $("ph-back").addEventListener("click", showProjects);
  $("conv-project").addEventListener("click", () => enter(currentConversation().project));
  $("project-create").addEventListener("click", () => openDialog());
  $("ph-edit").addEventListener("click", () => openDialog(shown()));
  $("ph-delete").addEventListener("click", removeProject);
  $("project-form").addEventListener("submit", submitDialog);
  $("pd-cancel").addEventListener("click", () => $("project-dialog").close());
  $("ph-save").addEventListener("click", saveInstructions);
  $("ph-add").addEventListener("click", () => $("ph-file").click());
  $("ph-file").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
  document.addEventListener("gw-conversation", sync);
  sync();
}
