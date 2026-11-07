// Panneau « Carte graphique et modèles locaux » (18.4), clé maître : mode PC occupé (bouton, durée, horaires),
// mode économie (ressources du PC au minimum pendant une durée), file d'attente de chaque serveur local, modèles
// chargés en mémoire (Ollama) et keep_alive.
import { api, errorMessage } from "./auth.js";

const $ = (id) => document.getElementById(id);
const DAYS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"];
let state = null, gen = 0, timer = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function message(text, ok = false) {
  const b = $("gpu-msg");
  b.hidden = !text;
  b.className = `alert${text ? " show" : ""}${ok ? " ok-box" : ""}`;
  b.textContent = text || "";
}

const hour = (ts) => {
  const d = new Date(ts * 1000);
  const sameDay = d.toDateString() === new Date().toDateString();
  return d.toLocaleString("fr-FR", sameDay ? { timeStyle: "short" } : { weekday: "short", hour: "2-digit", minute: "2-digit" });
};
const gb = (b) => (b ? `${(b / 1e9).toFixed(1).replace(".", ",")} Go` : "");

function renderState(busy) {
  const pill = $("gpu-state");
  pill.classList.toggle("busy", busy.on);
  pill.textContent = !busy.on ? "Disponible pour tous"
    : `PC occupé${busy.until ? ` jusqu'à ${hour(busy.until)}` : ""}${busy.source === "schedule" ? " (horaire)" : ""}`;
  const btn = $("gpu-busy-btn");
  btn.textContent = busy.on ? (busy.source === "schedule" ? "Plage horaire en cours" : "Libérer le PC") : "PC occupé";
  btn.disabled = busy.on && busy.source === "schedule";
  btn.className = `btn btn-inline ${busy.on ? "btn-ghost" : "btn-primary"}`;
  $("gpu-minutes").closest("label").hidden = busy.on;
}

function renderEco(eco) {
  const pill = $("gpu-eco-state");
  pill.classList.toggle("eco", eco.on);
  pill.textContent = eco.on ? `actif${eco.until ? ` jusqu'à ${hour(eco.until)}` : ""}` : "inactif";
  const btn = $("gpu-eco-btn");
  btn.textContent = eco.on ? "Arrêter l'économie" : "Économiser";
  btn.className = `btn btn-inline ${eco.on ? "btn-ghost" : "btn-primary"}`;
  $("gpu-eco-minutes").closest("label").hidden = eco.on;
}

function serverItem(s) {
  const li = el("li", "bridge");
  const text = el("div", "bridge-text");
  const head = el("div", "bridge-head");
  head.append(el("strong", "", s.name), el("span", "pill", s.this_pc ? "ce PC" : "réseau"));
  if (s.busy_reserved && state.busy.on) head.append(el("span", "pill busy-pill", "réservé"));
  if (state.eco?.on) head.append(el("span", "pill eco-pill", "économie"));
  text.append(head);
  const queue = `${s.active} en cours sur ${s.max_concurrent}` + (s.waiting ? ` · ${s.waiting} en attente` : "")
    + (s.queued ? ` · ${s.queued} requête(s) mises en attente depuis le démarrage` : "");
  text.append(el("p", "muted small", queue));
  if (s.error) text.append(el("p", "muted small", s.error));
  if (s.loaded?.length) {
    const ul = el("ul", "gpu-loaded");
    for (const m of s.loaded) {
      const row = el("li");
      row.append(el("code", "", m.model), el("span", "muted small",
        [gb(m.vram) && `${gb(m.vram)} en mémoire vidéo`, m.expires_at && `jusqu'à ${hour(Date.parse(m.expires_at) / 1000)}`]
          .filter(Boolean).join(" · ")));
      const b = el("button", "btn btn-ghost btn-inline", "Décharger");
      b.type = "button";
      b.addEventListener("click", () => unload(s.name, m.model, b));
      row.append(b);
      ul.append(row);
    }
    text.append(ul);
  } else if (s.ollama && !s.error) {
    text.append(el("p", "muted small", "Aucun modèle chargé en mémoire."));
  } else if (!s.ollama) {
    text.append(el("p", "muted small", "Déchargement depuis l'application du serveur (LM Studio : Eject)."));
  }
  li.append(text);
  const side = el("div", "gpu-server-side");
  if (s.loaded?.length > 1) {
    const all = el("button", "btn btn-ghost btn-inline", "Tout décharger");
    all.type = "button";
    all.addEventListener("click", () => unload(s.name, null, all));
    side.append(all);
  }
  li.append(side);
  return li;
}

function slotRow(slot = { days: [1, 2, 3, 4, 5], start: "09:00", end: "18:00" }) {
  const li = el("li", "gpu-slot");
  const days = el("div", "gpu-days");
  DAYS.forEach((d, i) => {
    const l = el("label");
    const c = el("input");
    c.type = "checkbox";
    c.value = i + 1;
    c.checked = slot.days.includes(i + 1);
    l.append(c, d);
    days.append(l);
  });
  const start = el("input"), end = el("input");
  for (const [input, v, label] of [[start, slot.start, "Début"], [end, slot.end, "Fin"]]) {
    input.type = "time";
    input.value = v;
    input.required = true;
    input.setAttribute("aria-label", label);
  }
  start.dataset.role = "start";
  end.dataset.role = "end";
  const rm = el("button", "btn btn-ghost btn-inline", "Retirer");
  rm.type = "button";
  rm.addEventListener("click", () => li.remove());
  li.append(days, start, el("span", "muted small", "→"), end, rm);
  return li;
}

function fillSettings(d) {
  const c = d.config;
  const keep = c.keep_alive === 60 ? null : c.keep_alive;          // 60 s : le défaut (« 1 minute »)
  $("gpu-keep").value = keep === null || keep === undefined ? "" : String(keep);
  if ($("gpu-keep").value !== String(keep ?? "")) {               // valeur posée par l'API, hors liste
    $("gpu-keep").append(new Option(`${keep} s`, String(keep)));
    $("gpu-keep").value = String(keep);
  }
  $("gpu-max").value = c.max_concurrent || "";
  $("gpu-max").placeholder = d.defaults.max_concurrent;
  $("gpu-wait").value = c.queue_wait || "";
  $("gpu-wait").placeholder = d.defaults.queue_wait;
  $("gpu-unload").checked = c.busy.unload !== false;
  const reserved = $("gpu-reserved");
  reserved.replaceChildren();
  for (const s of d.servers) {
    const l = el("label", "check");
    const box = el("input");
    box.type = "checkbox";
    box.value = s.name;
    box.checked = d.busy.providers.includes(s.name);
    l.append(box, ` ${s.name}${s.this_pc ? " (ce PC)" : ""}`);
    reserved.append(l);
  }
  if (!d.servers.length) reserved.append(el("span", "muted small", "Aucun serveur local configuré."));
  $("gpu-slots").replaceChildren(...c.busy.schedule.map(slotRow));
  const eco = d.eco.settings;
  $("eco-max").value = eco.max_concurrent;
  $("eco-keep").value = String(eco.keep_alive);
  if ($("eco-keep").value !== String(eco.keep_alive)) {           // valeur posée par l'API, hors liste
    $("eco-keep").append(new Option(`${eco.keep_alive} s`, String(eco.keep_alive)));
    $("eco-keep").value = String(eco.keep_alive);
  }
  $("eco-tokens").value = eco.max_tokens;
  $("eco-params").value = String(eco.max_params_b);
  if ($("eco-params").value !== String(eco.max_params_b)) {
    $("eco-params").append(new Option(`${eco.max_params_b} milliards au plus`, String(eco.max_params_b)));
    $("eco-params").value = String(eco.max_params_b);
  }
  for (const [id, key, label] of [["eco-gpu", "gpu_share", "%"], ["eco-cpu", "cpu_share", "%"],
                                  ["eco-ctx", "context", " tokens"]]) {
    $(id).value = String(eco[key]);
    if ($(id).value !== String(eco[key])) {                     // valeur posée par l'API, hors liste
      $(id).append(new Option(`${eco[key]}${label}`, String(eco[key])));
      $(id).value = String(eco[key]);
    }
  }
  $("eco-priority").checked = eco.low_priority;
  $("eco-index").checked = eco.pause_indexing;
  $("eco-unload").checked = eco.unload;
}

function render(d, settingsToo) {
  state = d;
  $("gpu-section").hidden = false;
  renderState(d.busy);
  renderEco(d.eco);
  $("gpu-servers").replaceChildren(...d.servers.map(serverItem));
  if (!d.servers.length) $("gpu-servers").append(el("li", "muted small", "Aucun serveur local (Ollama, LM Studio…) détecté."));
  if (settingsToo) fillSettings(d);
}

async function load(settingsToo = false) {
  const mine = ++gen;
  const r = await api("/v1/admin/gpu");
  if (mine !== gen) return;                                      // une réponse plus récente est déjà là
  if (r.ok) render(r.data, settingsToo);
}

async function toggleBusy() {
  const on = !state?.busy.on;
  const minutes = parseInt($("gpu-minutes").value, 10);
  const btn = $("gpu-busy-btn");
  btn.disabled = true;
  const r = await api("/v1/admin/gpu/busy", { method: "POST", body: on ? { on, ...(minutes && { minutes }) } : { on } });
  btn.disabled = false;
  if (!r.ok) return message(errorMessage(r));
  message(on ? "PC réservé : les autres utilisateurs passent par le cloud." : "PC de nouveau partagé.", true);
  load();
}

async function toggleEco() {
  const on = !state?.eco.on;
  const minutes = parseInt($("gpu-eco-minutes").value, 10);
  const btn = $("gpu-eco-btn");
  btn.disabled = true;
  const r = await api("/v1/admin/gpu/eco", { method: "POST", body: on ? { on, ...(minutes && { minutes }) } : { on } });
  btn.disabled = false;
  if (!r.ok) return message(errorMessage(r));
  message(on ? "Mode économie actif : les IA locales ménagent ce PC." : "Mode économie arrêté : tout est rétabli.", true);
  load();
}

async function unload(provider, model, btn) {
  btn.disabled = true;
  const r = await api("/v1/admin/gpu/unload", { method: "POST", body: { provider, ...(model && { model }) } });
  btn.disabled = false;
  if (!r.ok) return message(errorMessage(r));
  message(r.data.unloaded.length ? `Déchargé : ${r.data.unloaded.join(", ")}.` : "Aucun modèle chargé.", true);
  setTimeout(load, 600);                                         // Ollama libère la mémoire en quelques instants
}

function readSettings() {
  const num = (id) => parseInt($(id).value, 10) || null;
  const keep = $("gpu-keep").value;
  const schedule = [...$("gpu-slots").children].map((li) => ({
    days: [...li.querySelectorAll(".gpu-days input:checked")].map((c) => Number(c.value)),
    start: li.querySelector("[data-role=start]").value, end: li.querySelector("[data-role=end]").value }));
  const servers = {};
  for (const s of state.servers) {
    if (s.max_concurrent_override) servers[s.name] = { max_concurrent: s.max_concurrent_override };
  }
  // serveurs réservés : si c'est la liste par défaut (ceux de ce PC), rien d'enregistré — un serveur ajouté plus
  // tard sur ce PC sera réservé lui aussi
  const picked = [...$("gpu-reserved").querySelectorAll("input:checked")].map((c) => c.value).sort();
  const thisPc = state.servers.filter((s) => s.this_pc).map((s) => s.name).sort();
  const providers = picked.join() === thisPc.join() ? null : picked;
  return { keep_alive: keep === "" ? null : Number(keep), max_concurrent: num("gpu-max"), queue_wait: num("gpu-wait"),
           servers, busy: { unload: $("gpu-unload").checked, schedule, providers },
           eco: { max_concurrent: num("eco-max") || 1, keep_alive: Number($("eco-keep").value),
                  max_tokens: Math.max(0, parseInt($("eco-tokens").value, 10) || 0),
                  max_params_b: Number($("eco-params").value), low_priority: $("eco-priority").checked,
                  gpu_share: Number($("eco-gpu").value), cpu_share: Number($("eco-cpu").value),
                  context: Number($("eco-ctx").value),
                  pause_indexing: $("eco-index").checked, unload: $("eco-unload").checked } };
}

async function save(e) {
  e.preventDefault();
  const body = readSettings();
  if (body.busy.schedule.some((s) => !s.days.length)) return message("Chaque plage horaire doit avoir au moins un jour.");
  $("gpu-save").disabled = true;
  const r = await api("/v1/admin/gpu/settings", { method: "PUT", body });
  $("gpu-save").disabled = false;
  if (!r.ok) return message(errorMessage(r));
  render(r.data, true);
  message("Réglages enregistrés.", true);
}

export function initGpu() {
  $("gpu-busy-btn").addEventListener("click", toggleBusy);
  $("gpu-eco-btn").addEventListener("click", toggleEco);
  $("gpu-form").addEventListener("submit", save);
  $("gpu-add-slot").addEventListener("click", () => $("gpu-slots").append(slotRow()));
  load(true);
  clearInterval(timer);
  timer = setInterval(() => { if (!document.hidden && !$("gpu-section").hidden) load(); }, 10000);   // files d'attente
}
