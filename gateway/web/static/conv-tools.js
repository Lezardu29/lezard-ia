// Outils de la conversation : au début de chaque nouvelle conversation, le chat demande quels outils déjà installés
// activer (recherche sur Internet, contrôle du PC, documents, chaque serveur MCP connecté). Les outils cochés
// accompagnent chaque message de la conversation ; une commande tapée (/web, /pc, /mcp…) garde la main sur son message.
// Rien de coché : l'IA répond seule, comme avant. Le choix est gardé par conversation, dans ce navigateur.
// Une fois la conversation commencée, la puce « Outils » sous le champ de saisie permet de changer d'avis.
import { toolGroups } from "./slash-commands.js";

const $ = (id) => document.getElementById(id);
const STORE = "gw.convTools";
const MANY = 40;                                           // au-delà, une IA locale choisit mal parmi les outils
let groups = [];
let selected = [];
let onChange = () => {};

function readAll() {
  try { return JSON.parse(localStorage.getItem(STORE) || "{}") || {}; } catch { return {}; }
}
/** Choix gardé pour une conversation enregistrée (vide pour une nouvelle). */
export function load(convId) {
  selected = convId ? (readAll()[convId] || []).filter((g) => typeof g === "string") : [];
  paint();
  return selected;
}
/** La conversation vient d'être enregistrée (premier message) : son choix est gardé sous son identifiant. */
export function keep(convId) {
  if (!convId) return;
  const all = readAll();
  if (selected.length) all[convId] = selected;
  else delete all[convId];
  const ids = Object.keys(all);
  for (const old of ids.slice(0, Math.max(0, ids.length - 300))) delete all[old];   // les 300 plus récentes
  try { localStorage.setItem(STORE, JSON.stringify(all)); } catch { /* stockage indisponible */ }
}

/** Options de requête des outils cochés ({} si rien, ou si un outil coché n'est plus disponible). */
export function options() {
  const names = groups.filter((g) => selected.includes(g.id)).flatMap((g) => g.names);
  return names.length ? { gateway_tools: [...new Set(names)], orchestration: undefined } : {};
}

function label(group) {
  const box = document.createElement("label");
  box.className = `ct-option${group.warn ? " ct-warn" : ""}`;
  box.title = group.help || "";
  const input = Object.assign(document.createElement("input"), { type: "checkbox", value: group.id });
  input.checked = selected.includes(group.id);
  input.addEventListener("change", () => {
    selected = input.checked ? [...new Set([...selected, group.id])] : selected.filter((g) => g !== group.id);
    paint();
    onChange(selected);
  });
  box.append(input, ` ${group.label}`);
  if (group.count) box.append(Object.assign(document.createElement("span"), { className: "ct-count", textContent: ` ${group.count}` }));
  return box;
}

function fill(list) {
  list.replaceChildren(...groups.map(label));
}

function paint() {
  const active = groups.filter((g) => selected.includes(g.id));
  const total = active.reduce((n, g) => n + g.names.length, 0);
  for (const id of ["ct-list", "ct-pop-list"]) if ($(id)) fill($(id));
  const warn = total > MANY
    ? `${total} outils cochés : c'est beaucoup pour une IA locale, elle risque de choisir le mauvais. Gardez ceux utiles à cette conversation.`
    : active.some((g) => g.warn) ? "Contrôle du PC activé : l'IA peut agir sur ce PC pendant toute la conversation." : "";
  for (const id of ["ct-note", "ct-pop-note"]) if ($(id)) { $(id).textContent = warn; $(id).hidden = !warn; }
  const chip = $("ct-chip");
  if (chip) {
    chip.hidden = !groups.length || !document.body.classList.contains("has-messages") && !active.length;
    chip.textContent = active.length ? `Outils : ${active.map((g) => g.label).join(", ")}` : "Outils : aucun";
    chip.setAttribute("aria-expanded", String(!$("ct-pop").hidden));
  }
  if ($("conv-tools")) $("conv-tools").hidden = !groups.length;
}

/** Relit les outils disponibles (clé, serveurs MCP connectés) et affiche la question ; `changed(selected)` à chaque choix. */
export async function init(changed) {
  onChange = changed || onChange;
  try { groups = await toolGroups(true); } catch { groups = []; }
  selected = selected.filter((g) => groups.some((x) => x.id === g));
  if (!$("ct-chip") && $("toolbar")) {                     // la barre est remplie par settings-bar.js : ajoutée après
    const chip = Object.assign(document.createElement("button"), { type: "button", className: "ct-chip", id: "ct-chip", hidden: true });
    chip.setAttribute("aria-controls", "ct-pop");
    $("toolbar").prepend(chip);
  }
  $("ct-chip")?.addEventListener("click", () => { $("ct-pop").hidden = !$("ct-pop").hidden; paint(); });
  document.addEventListener("click", (e) => {                // clic ailleurs : la liste se referme
    if ($("ct-pop") && !$("ct-pop").hidden && !e.target.closest("#ct-pop, #ct-chip")) { $("ct-pop").hidden = true; paint(); }
  });
  paint();
}

/** Nouvelle conversation : rien de coché, la question s'affiche de nouveau. */
export function reset() {
  selected = [];
  if ($("ct-pop")) $("ct-pop").hidden = true;
  paint();
}
export const refresh = paint;
