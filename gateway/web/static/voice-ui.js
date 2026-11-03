// Catégorie « Voix » de la page Modèles, comme « Embeddings » : modèles de dictée et de lecture vocale détectés,
// classés pour ce PC (qualité en français, place en mémoire), celui qui sert, et le choix automatique ou fixé ;
// réglages de la voix de lecture (Kokoro) : voix, prononciation (accent), mélange de deux voix, vitesse, pauses.
import { api, errorMessage, getKey } from "./auth.js";

const $ = (id) => document.getElementById(id);
const FIT = { vram: "Tient en VRAM", offload: "VRAM + RAM (plus lent)", too_big: "Trop gros", cloud: "Cloud",
              unknown: "Autre machine", cpu: "Processeur" };
const KINDS = {
  stt: { pick: "voice-stt-pick", list: "voice-stt-list", what: "la dictée",
         none: "Aucun modèle de dictée détecté : installez faster-whisper (Whisper intégré) ou ajoutez un serveur " +
               "(Speaches, LocalAI) sur un PC du réseau." },
  tts: { pick: "voice-tts-pick", list: "voice-tts-list", what: "la lecture vocale",
         none: "Aucun modèle de lecture vocale détecté : téléchargez Kokoro (python -m gateway.providers.local_tts " +
               "download) ou ajoutez un serveur (Speaches, LocalAI)." },
};

const SAMPLE = "Bonjour ! Voici la voix qui lira les réponses de Lezard IA. Elle peut parler plus vite, ou plus " +
               "lentement… et marquer des pauses entre les phrases.";
const KOKORO_PAUSE = 0.25;                           // pause par défaut de Kokoro entre deux phrases (s)
let saving = Promise.resolve(), trial = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function note(text, ok = false) {
  const b = $("voice-msg");
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
  head.append(el("span", `fit ${["vram", "offload", "too_big"].includes(m.fit) ? m.fit : ""}`, FIT[m.fit] || m.fit));
  text.append(head);
  const facts = [m.ref, `qualité en français ${Math.round(m.score * 100)} / 100`,
                 m.multilingual === false ? "surtout anglais" : "",
                 m.memory_gb ? `${String(m.memory_gb).replace(".", ",")} Go en mémoire` : "",
                 m.available ? "" : "injoignable pour l'instant"].filter(Boolean);
  text.append(el("p", "muted small", facts.join(" · ")));
  li.append(text);
  return li;
}

function render(d) {
  $("voice-section").hidden = false;
  for (const [kind, k] of Object.entries(KINDS)) {
    const part = d[kind];
    const list = $(k.list);
    list.replaceChildren(...part.models.map(item));
    if (!part.models.length) list.append(el("li", "empty", k.none));
    const pick = $(k.pick);
    pick.replaceChildren(new Option("Automatique : le meilleur pour ce PC (recommandé)", ""),
                         ...part.models.map((m) => new Option(m.label + (m.hosting === "cloud" ? " (cloud)" : ""), m.ref)));
    pick.value = part.pinned && !part.pinned_missing ? part.pinned : "";
  }
  const status = [];
  if (d.whisper) {
    status.push(d.whisper.auto ? `Whisper intégré : ${d.whisper.model}, choisi pour ce PC (${d.whisper.reason}).`
                               : `Whisper intégré : ${d.whisper.model} (${d.whisper.reason}).`);
  }
  for (const [kind, k] of Object.entries(KINDS)) {
    if (d[kind].pinned_missing) status.push(`Le modèle fixé pour ${k.what} n'est plus détecté : le meilleur disponible sert à sa place.`);
  }
  $("voice-status").textContent = status.join(" ");
  renderReading(d.reading, d.tts.models.length > 0);
}

// ----- voix de lecture -----
function voiceOptions(select, voices, first) {
  const groups = new Map();
  for (const v of voices) {
    if (!groups.has(v.language)) groups.set(v.language, []);
    groups.get(v.language).push(v);
  }
  const french = [...groups.keys()].sort((a, b) => (b === "français") - (a === "français") || a.localeCompare(b));
  select.replaceChildren(new Option(first, ""), ...french.map((lang) => {
    const g = document.createElement("optgroup");
    g.label = lang[0].toUpperCase() + lang.slice(1);
    g.append(...groups.get(lang).map((v) => new Option(v.label, v.id)));
    return g;
  }));
}

function showRange(id, value, text) {
  const input = $(id);
  input.value = value;
  const share = (input.value - input.min) / (input.max - input.min);
  input.style.setProperty("--fill", `${Math.round(share * 100)}%`);
  $(`${id}-out`).textContent = text(Number(input.value));
}

const fmt = {
  "vr-share": (v) => `${v} %`,
  "vr-speed": (v) => `${v.toFixed(2).replace(".", ",")}×`,
  "vr-pause": (v) => `${v.toFixed(2).replace(".", ",")} s`,
};

function renderReading(r, hasTts) {
  $("voice-reading").hidden = !hasTts;
  if (!hasTts) return;
  voiceOptions($("vr-voice"), r.voices, "Par défaut (selon la langue du texte)");
  voiceOptions($("vr-blend"), r.voices, "Aucune");
  $("vr-voice").value = r.voice || "";
  $("vr-blend").value = r.blend_voice || "";
  $("vr-pron").value = r.pronunciation;
  showRange("vr-share", Math.round(r.blend_share * 100), fmt["vr-share"]);
  showRange("vr-speed", r.speed, fmt["vr-speed"]);
  showRange("vr-pause", r.pause ?? KOKORO_PAUSE, fmt["vr-pause"]);
  $("vr-voice").disabled = !r.kokoro;
  $("vr-blend").disabled = !r.kokoro || !r.builtin;
  $("vr-pron").disabled = $("vr-pause").disabled = !r.builtin;
  $("vr-share").disabled = !r.builtin || !r.blend_voice;
  $("vr-note").textContent = !r.kokoro
    ? "Voix au choix avec Kokoro seulement : avec ce modèle, seule la vitesse se règle."
    : !r.builtin
      ? "Prononciation, mélange et pauses : avec le Kokoro intégré à Lezard IA seulement (voix et vitesse partout)."
      : "Une voix étrangère avec « Langue du texte » lit le français avec son accent. Le mélange donne une voix entre les deux.";
}

function readingBody() {
  return {
    voice: $("vr-voice").value || null,
    pronunciation: $("vr-pron").value,
    blend_voice: $("vr-blend").value || null,
    blend_share: Number($("vr-share").value) / 100,
    speed: Number($("vr-speed").value),
    pause: Number($("vr-pause").value) === KOKORO_PAUSE ? null : Number($("vr-pause").value),
  };
}

function saveReading(body = readingBody()) {
  saving = saving.then(async () => {
    const r = await api("/v1/admin/voice/reading", { method: "PUT", body });
    if (!r.ok) { note(errorMessage(r)); return load(); }
    render(r.data);
  });
  return saving;
}

async function tryVoice() {
  const button = $("vr-try");
  if (trial) { trial.pause(); trial = null; button.textContent = "Écouter un essai"; return; }
  await saving;                                       // l'essai lit les réglages enregistrés
  button.disabled = true;
  try {
    const res = await fetch("/v1/audio/speech", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${getKey()}` },
      body: JSON.stringify({ model: "auto", input: SAMPLE, response_format: "mp3", language: "fr" }),
    });
    if (!res.ok) {
      let data = null;
      try { data = await res.json(); } catch { /* corps vide */ }
      throw new Error(errorMessage({ status: res.status, data }));
    }
    const url = URL.createObjectURL(await res.blob());
    trial = new Audio(url);
    const done = () => { URL.revokeObjectURL(url); trial = null; button.textContent = "Écouter un essai"; };
    trial.addEventListener("ended", done);
    trial.addEventListener("pause", done);
    button.textContent = "Arrêter l'essai";
    await trial.play();
  } catch (e) {
    note(`Essai impossible : ${e.message}`);
  } finally {
    button.disabled = false;
  }
}

async function load() {
  const r = await api("/v1/admin/voice");
  if (r.ok) render(r.data);
}

async function choose(kind) {
  const pick = $(KINDS[kind].pick);
  const model = pick.value || null;
  pick.disabled = true;
  const r = await api("/v1/admin/voice", { method: "PUT", body: { kind, model } });
  pick.disabled = false;
  if (!r.ok) { note(errorMessage(r)); return load(); }
  render(r.data);
  const label = r.data[kind].models.find((m) => m.ref === model)?.label || model;
  note(model ? `${label} sert désormais à ${KINDS[kind].what}.` : `Choix automatique pour ${KINDS[kind].what} : le meilleur modèle pour ce PC.`, true);
}

export function initVoice() {
  for (const kind of Object.keys(KINDS)) $(KINDS[kind].pick).addEventListener("change", () => choose(kind));
  for (const id of ["vr-voice", "vr-pron", "vr-blend"]) $(id).addEventListener("change", () => { note(""); saveReading(); });
  for (const id of Object.keys(fmt)) {
    $(id).addEventListener("input", () => showRange(id, $(id).value, fmt[id]));   // affichage pendant le glissement
    $(id).addEventListener("change", () => { note(""); saveReading(); });          // enregistré au lâcher
  }
  $("vr-try").addEventListener("click", tryVoice);
  $("vr-reset").addEventListener("click", () => { note(""); saveReading({}); });
  load();
}
export { load as refreshVoice };
