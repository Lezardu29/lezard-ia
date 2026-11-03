// Dictée vocale du chat (13.9) : le micro enregistre (MediaRecorder), la gateway transcrit
// (POST /v1/audio/transcriptions : Whisper local d'abord, sinon cloud facturé) et le texte est INSÉRÉ dans le champ,
// jamais envoyé : on le relit et le corrige avant l'envoi.
// Pas de reconnaissance vocale du navigateur (Web Speech API) : dans Chrome elle envoie l'audio à Google.
import { api, errorMessage, getKey } from "./auth.js";

const MAX_MS = 600_000;              // durée maximale d'une dictée (longue : découpée entre les serveurs locaux)
const SILENCE_MS = 2500;             // arrêt après ce silence, une fois la parole commencée
const SILENCE_RMS = 0.015;           // niveau sous lequel on considère qu'il n'y a pas de voix
const HOLD_MS = 350;                 // appui long (tactile) : « maintenir pour parler »
const TYPES = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm"];

let btn, status, input, rec = null, clearTimer = null;
let privateMode = () => false;       // conversation en mode privé : transcription par un serveur local seulement
const hold = { timer: null, active: false, suppressClick: false };

function say(text, kind = "", ms = 0) {
  clearTimeout(clearTimer);
  status.textContent = text;
  status.className = `dictation${kind ? ` ${kind}` : ""}`;
  status.hidden = !text;
  if (ms) clearTimer = setTimeout(() => say(""), ms);
}

const clock = (ms) => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}`;

function tick() {
  const elapsed = performance.now() - rec.started;
  say(`● ${clock(elapsed)} / ${clock(MAX_MS)} — appuyez à nouveau pour terminer, Échap pour annuler`, "recording");
  if (elapsed >= MAX_MS) stop();
}

function watchSilence(r) {
  try {
    r.audio = new AudioContext();
    const analyser = r.audio.createAnalyser();
    analyser.fftSize = 2048;
    r.audio.createMediaStreamSource(r.stream).connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    const loop = () => {
      analyser.getFloatTimeDomainData(buf);
      const rms = Math.sqrt(buf.reduce((s, v) => s + v * v, 0) / buf.length);
      const now = performance.now();
      if (rms > SILENCE_RMS) { r.heard = true; r.quietSince = null; }
      else if (r.heard) {
        r.quietSince ??= now;
        if (now - r.quietSince > SILENCE_MS) { stop(); return; }
      }
      r.raf = requestAnimationFrame(loop);
    };
    r.raf = requestAnimationFrame(loop);
  } catch { /* sans Web Audio : arrêt manuel ou à la durée maximale */ }
}

async function start() {
  if (rec || btn.disabled) return;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (e) {
    say(e.name === "NotAllowedError" ? "Micro refusé : autorisez-le dans les réglages du navigateur pour ce site."
                                     : "Aucun micro utilisable.", "error", 6000);
    return;
  }
  const mimeType = TYPES.find((t) => MediaRecorder.isTypeSupported(t));
  const recorder = new MediaRecorder(stream, { ...(mimeType ? { mimeType } : {}), audioBitsPerSecond: 32000 });
  const r = rec = { recorder, stream, chunks: [], started: performance.now(), cancelled: false, heard: false };
  recorder.addEventListener("dataavailable", (e) => { if (e.data.size) r.chunks.push(e.data); });
  recorder.addEventListener("stop", () => finish(r));
  recorder.start(1000);
  watchSilence(r);
  btn.classList.add("recording");
  btn.setAttribute("aria-pressed", "true");
  btn.setAttribute("aria-label", "Terminer la dictée");
  r.timer = setInterval(tick, 250);
  tick();
}

function stop() {
  if (rec && rec.recorder.state !== "inactive") rec.recorder.stop();
}

function cancel() {
  if (!rec) return;
  rec.cancelled = true;
  stop();
  say("Dictée annulée.", "", 2000);
}

function release(r) {
  clearInterval(r.timer);
  cancelAnimationFrame(r.raf);
  r.audio?.close().catch(() => {});
  r.stream.getTracks().forEach((t) => t.stop());          // le voyant du micro s'éteint
  btn.classList.remove("recording");
  btn.setAttribute("aria-pressed", "false");
  btn.setAttribute("aria-label", "Dicter un message");
  if (rec === r) rec = null;
}

async function finish(r) {
  release(r);
  if (r.cancelled) return;
  const blob = new Blob(r.chunks, { type: r.recorder.mimeType || "audio/webm" });
  if (!blob.size) return say("Aucun son enregistré.", "error", 4000);
  const sub = (blob.type.split("/")[1] || "webm").split(";")[0];
  const form = new FormData();
  form.append("file", blob, `dictee.${sub === "mp4" ? "m4a" : sub}`);
  form.append("model", "auto");
  // langue de l'interface : sans elle, Whisper la devine, et se trompe souvent sur une phrase courte
  const lang = (navigator.language || document.documentElement.lang || "").slice(0, 2).toLowerCase();
  if (/^[a-z]{2}$/.test(lang)) form.append("language", lang);
  form.append("response_format", "json");
  btn.disabled = true;
  say("Transcription…", "busy");
  let res, data = null;
  try {
    const headers = { Authorization: `Bearer ${getKey()}` };
    if (privateMode()) headers["X-Gateway-Private"] = "true";     // conversation privée : transcription locale
    res = await fetch("/v1/audio/transcriptions", { method: "POST", body: form, headers });
    try { data = await res.json(); } catch { /* corps vide */ }
  } catch {
    res = { ok: false, status: 0 };
  }
  btn.disabled = false;
  if (!res.ok) return say(`Transcription impossible : ${errorMessage({ status: res.status, data })}`, "error", 8000);
  const text = (data?.text || "").trim();
  if (!text) return say("Aucune parole reconnue.", "error", 4000);
  insert(text);
  const m = data.x_gateway || {};
  const cost = m.cost_eur > 0 ? ` · ${m.cost_eur.toFixed(4).replace(".", ",")} €` : "";
  const by = m.split?.parallel ? `${m.split.workers.length} serveurs locaux (${m.split.parts} morceaux)`
    : m.hosting === "local" ? "un serveur local" : m.provider;
  say(`Transcrit par ${by}${cost}. Relisez avant d'envoyer.`, "", 4000);
}

function insert(text) {
  const start = input.selectionStart ?? input.value.length, end = input.selectionEnd ?? start;
  const before = input.value.slice(0, start), after = input.value.slice(end);
  const sep = before && !/\s$/.test(before) ? " " : "";
  input.value = before + sep + text + after;
  const caret = (before + sep + text).length;
  input.focus();
  input.setSelectionRange(caret, caret);
  input.dispatchEvent(new Event("input", { bubbles: true }));     // hauteur du champ, bouton Envoyer
}

// dictée impossible : micro grisé, la raison ne s'affiche qu'au clic (pas de message permanent sous la saisie)
function unavailable(text, detail = "") {
  btn.hidden = false;
  btn.classList.add("off");
  btn.setAttribute("aria-label", "Dictée vocale indisponible");
  btn.title = text;
  btn.addEventListener("click", () => say(detail ? `Dictée vocale indisponible. ${detail}` : text, "", 8000));
}

export async function initDictation({ button, statusEl, field, isPrivate }) {
  if (isPrivate) privateMode = isPrivate;
  btn = button;
  status = statusEl;
  input = field;
  // le micro n'existe qu'en contexte sécurisé : HTTPS, tunnel, LAN HTTPS (12.7) ou localhost
  if (!window.isSecureContext) {
    return unavailable("Dictée vocale indisponible en HTTP sur le réseau local : ouvrez la gateway en HTTPS " +
                             "(tunnel ou mode réseau local HTTPS) ou depuis cet ordinateur.");
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    return unavailable("Dictée vocale non prise en charge par ce navigateur.");
  }
  const r = await api("/v1/audio/status");
  if (!r.ok || !r.data.available) {
    return unavailable("Dictée vocale indisponible : aucun serveur de transcription utilisable.",
                       r.data?.reason || errorMessage(r));
  }
  btn.hidden = false;
  btn.addEventListener("click", () => {
    if (hold.suppressClick) { hold.suppressClick = false; return; }
    if (rec) stop(); else start();
  });
  // tactile : maintenir pour parler, relâcher pour terminer
  btn.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" || rec) return;
    hold.timer = setTimeout(() => { hold.active = true; start(); }, HOLD_MS);
  });
  const up = () => {
    clearTimeout(hold.timer);
    if (hold.active) { hold.active = false; hold.suppressClick = true; stop(); }
  };
  ["pointerup", "pointercancel", "pointerleave"].forEach((t) => btn.addEventListener(t, up));
  btn.addEventListener("contextmenu", (e) => e.preventDefault());      // pas de menu sur appui long
  // Échap annule la dictée sans interrompre une réponse en cours (capture, avant le champ de saisie)
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && rec) { e.preventDefault(); e.stopImmediatePropagation(); cancel(); }
  }, true);
}
