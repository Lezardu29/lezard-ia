// Catégorie « Embeddings » de la page Modèles : tous les modèles d'embeddings détectés, classés (qualité en français,
// place sur ce PC), celui qui sert à la recherche dans les documents, et le choix automatique ou fixé.
import { api, errorMessage } from "./auth.js";

const $ = (id) => document.getElementById(id);
const FIT = { vram: "Tient en VRAM", offload: "VRAM + RAM (plus lent)", too_big: "Trop gros", cloud: "Cloud",
              unknown: "Autre machine" };
let data = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function note(text, ok = false) {
  const b = $("emb-msg");
  b.hidden = !text;
  b.className = `alert${text ? " show" : ""}${ok ? " ok-box" : ""}`;
  b.textContent = text || "";
}

function item(m) {
  const li = el("li", `emb-item${m.chosen ? " chosen" : ""}`);
  const text = el("div", "emb-text");
  const head = el("div", "emb-head");
  head.append(el("strong", "", m.label));
  if (m.chosen) head.append(el("span", "pill emb-used", "utilisé"));
  const fit = m.fit === "offload" && !data?.gpu ? "RAM, sur le processeur (lent)" : FIT[m.fit] || m.fit;   // sans carte graphique : pas de VRAM
  head.append(el("span", `fit ${m.fit === "cloud" || m.fit === "unknown" ? "" : m.fit}`, fit));
  text.append(head);
  const facts = [m.ref, `qualité ${Math.round(m.quality * 100)} / 100`,
                 m.multilingual === true ? "multilingue" : m.multilingual === false ? "surtout anglais" : "",
                 m.memory_gb ? `${String(m.memory_gb).replace(".", ",")} Go en mémoire` : "",
                 m.available ? "" : "injoignable pour l'instant"].filter(Boolean);
  text.append(el("p", "muted small", facts.join(" · ")));
  li.append(text);
  return li;
}

function render(d) {
  data = d;
  $("emb-section").hidden = false;
  const list = $("emb-list");
  list.replaceChildren(...d.models.map(item));
  if (!d.models.length) {
    list.append(el("li", "empty", "Aucun modèle d'embeddings détecté : installez-en un ci-dessus (ex. bge-m3, très "
                                  + "bon en français) pour la recherche par le sens dans vos documents."));
  }
  const pick = $("emb-pick");
  pick.replaceChildren(new Option("Automatique : le meilleur pour ce PC (recommandé)", ""),
                       ...d.models.map((m) => new Option(m.label + (m.hosting === "cloud" ? " (cloud)" : ""), m.ref)));
  pick.value = d.pinned || "";
  pick.disabled = d.mode === "env";
  const status = [];
  if (d.mode === "env") status.push(`Fixé dans .env (DOCUMENT_EMBEDDINGS_MODEL=${d.env.documents}) : retirez la ligne pour choisir ici.`);
  if (d.documents_embeddings === "off") status.push("Recherche par le sens coupée (DOCUMENT_EMBEDDINGS=off) : mots-clés seulement.");
  else if (!d.chosen) status.push("Aucun modèle utilisable : la recherche dans les documents se fait par mots-clés seulement.");
  else if (d.documents_to_vectorize) status.push(`${d.documents_to_vectorize} document(s) en cours de préparation avec ce modèle.`);
  $("emb-status").textContent = status.join(" ");
}

async function load() {
  const r = await api("/v1/admin/embeddings");
  if (r.ok) render(r.data);
}

async function choose() {
  const model = $("emb-pick").value || null;
  $("emb-pick").disabled = true;
  const r = await api("/v1/admin/embeddings", { method: "PUT", body: { model } });
  $("emb-pick").disabled = false;
  if (!r.ok) { note(errorMessage(r)); return load(); }
  render(r.data);
  note(model ? `${r.data.models.find((m) => m.ref === model)?.label || model} sert désormais à la recherche dans vos documents.`
             : "Choix automatique : le meilleur modèle pour ce PC.", true);
}

export function initEmbeddings() {
  $("emb-pick").addEventListener("change", choose);
  load();
}
export { load as refreshEmbeddings };
