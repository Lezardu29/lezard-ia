// Lecture vocale des réponses (19.4), pendant de la dictée (13.9) : bouton « Écouter » sous chaque réponse. La gateway
// synthétise (POST /v1/audio/speech : Piper / Kokoro local d'abord, sinon cloud facturé). Le Markdown est nettoyé
// (code, liens, tableaux), découpé en morceaux lus à la suite ; le suivant se prépare pendant la lecture, le premier
// est court pour que le son démarre vite. Plusieurs serveurs de voix locaux : autant de morceaux préparés à la fois,
// chacun sur le serveur le moins occupé. Conversation privée : serveur local seulement (en-tête X-Gateway-Private).
import { api, errorMessage, getKey } from "./auth.js";

export const ICONS = {
  listen: '<path d="M11 5 6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
};
// 10 ms de silence (WAV 8 kHz, 16 bits) : joué pendant le clic pour que Safari (iOS) autorise la suite de la lecture
function silence() {
  const v = new DataView(new ArrayBuffer(44 + 160));
  const u16 = (at, n) => v.setUint16(at, n, true), u32 = (at, n) => v.setUint32(at, n, true);
  const tag = (at, t) => [...t].forEach((c, k) => v.setUint8(at + k, c.charCodeAt(0)));
  tag(0, "RIFF"); u32(4, 36 + 160); tag(8, "WAVEfmt "); u32(16, 16); u16(20, 1); u16(22, 1);
  u32(24, 8000); u32(28, 16000); u16(32, 2); u16(34, 16); tag(36, "data"); u32(40, 160);
  return URL.createObjectURL(new Blob([v.buffer], { type: "audio/wav" }));
}
const FIRST_PIECE = 300;                                   // caractères : premier son rapide
const SHARED_PIECE = 600;                                  // plusieurs serveurs : morceaux plus courts, mieux répartis
let status = null;                                         // promesse : { available, local, max_chars, reason }
let current = null;                                        // lecture en cours

export function speechStatus() {
  status ||= api("/v1/audio/status").then((r) => (r.ok && r.data.speech) || { available: false });
  return status;
}

// Texte à lire : sans balises Markdown ni blocs de code (illisibles à voix haute), liens réduits à leur texte.
export function plainText(md) {
  return (md || "")
    .replace(/```[\s\S]*?(```|$)/g, "\n(bloc de code)\n")
    .replace(/`([^`\n]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/<[^>\n]+>/g, "")
    .replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, "")
    .replace(/^[ \t]*>[ \t]?/gm, "")
    .replace(/^[ \t]*([-*+]|\d+[.)])[ \t]+/gm, "")
    .replace(/^[ \t]*\|?[ \t]*:?-{3,}.*$/gm, "")
    .replace(/[ \t]*\|[ \t]*/g, ", ")
    .replace(/(\*\*|\*|~~)(\S(?:[\s\S]*?\S)?)\1/g, "$2")
    .replace(/https?:\/\/\S+/g, "(lien)")
    .replace(/^, |, $/gm, "")
    .replace(/[ \t]+/g, " ")
    .replace(/\n{2,}/g, "\n")
    .trim();
}

// Morceaux d'au plus `max` caractères, coupés en fin de phrase (sinon entre deux mots).
export function pieces(text, max, first = FIRST_PIECE) {
  const out = [];
  let rest = text.trim();
  while (rest) {
    const limit = out.length ? max : Math.min(first, max);
    if (rest.length <= limit) { out.push(rest); break; }
    const slice = rest.slice(0, limit);
    let cut = Math.max(...[". ", "! ", "? ", "… ", "\n", "; "].map((s) => slice.lastIndexOf(s)));
    if (cut < limit / 3) cut = slice.lastIndexOf(" ");
    if (cut <= 0) cut = limit - 1;
    out.push(rest.slice(0, cut + 1).trim());
    rest = rest.slice(cut + 1).trim();
  }
  return out.filter(Boolean);
}

async function fetchClip(input, isPrivate, signal) {
  const headers = { "Content-Type": "application/json" };
  const key = getKey();
  if (key) headers.Authorization = `Bearer ${key}`;
  if (isPrivate) headers["X-Gateway-Private"] = "true";
  const body = { model: "auto", input, response_format: "mp3" };
  const lang = (document.documentElement.lang || navigator.language || "").slice(0, 2).toLowerCase();
  if (/^[a-z]{2}$/.test(lang)) body.language = lang;        // voix par défaut dans la langue de l'interface
  const res = await fetch("/v1/audio/speech", { method: "POST", headers, body: JSON.stringify(body), signal });
  if (!res.ok) {
    let data = null;
    try { data = await res.json(); } catch { /* corps vide */ }
    throw new Error(errorMessage({ status: res.status, data }));
  }
  return URL.createObjectURL(await res.blob());
}

function show(button, state) {
  const playing = state !== "idle";
  button.classList.toggle("busy", state === "busy");
  button.classList.toggle("playing", state === "playing");
  const label = playing ? "Arrêter la lecture" : "Écouter la réponse";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.setAttribute("aria-pressed", String(playing));
  const svg = button.querySelector("svg");
  if (svg) svg.innerHTML = playing ? ICONS.stop : ICONS.listen;
}

export function stopSpeaking() {
  const me = current;
  if (!me) return;
  current = null;
  me.cancelled = true;
  me.controller.abort();
  me.audio.pause();
  me.audio.removeAttribute("src");
  me.urls.forEach((u) => URL.revokeObjectURL(u));
  me.done?.();
  show(me.button, "idle");
}

function play(me, url) {
  return new Promise((resolve, reject) => {
    me.done = resolve;                                       // arrêt : la boucle de lecture se termine
    me.audio.onended = resolve;
    me.audio.onerror = () => reject(new Error("Lecture audio impossible dans ce navigateur."));
    me.audio.src = url;
    me.audio.play().catch(reject);
  });
}

// Lit `markdown` ; un second clic sur le même bouton arrête ; un autre bouton arrête la lecture précédente.
export async function toggleSpeech(button, markdown, { isPrivate = false, onError = () => {} } = {}) {
  const same = current?.button === button;
  stopSpeaking();
  if (same) return;
  // élément audio lancé pendant le clic : Safari (iOS) n'autorise la lecture qu'à partir d'un geste
  const quiet = silence();
  const me = { button, audio: new Audio(quiet), controller: new AbortController(), cancelled: false, urls: [quiet] };
  me.audio.play().catch(() => {});
  current = me;
  show(button, "busy");
  try {
    const st = await speechStatus();
    const ahead = Math.max(1, st.parallel || 1);             // serveurs de voix locaux : morceaux préparés à la fois
    const max = st.max_chars || 4096;
    const parts = pieces(plainText(markdown), ahead > 1 ? Math.min(SHARED_PIECE, max) : max);
    if (!parts.length) throw new Error("Rien à lire dans cette réponse.");
    const clips = [];
    const prepare = (i) => {
      if (i >= parts.length || clips[i]) return;
      clips[i] = fetchClip(parts[i], isPrivate, me.controller.signal).then((url) => (me.urls.push(url), url));
      clips[i].catch(() => {});                              // erreur relevée quand ce morceau sera attendu
    };
    for (let i = 0; i < ahead; i++) prepare(i);
    for (let i = 0; i < parts.length && !me.cancelled; i++) {
      const url = await clips[i];
      if (me.cancelled) break;
      prepare(i + ahead);
      show(button, "playing");
      await play(me, url);
      URL.revokeObjectURL(url);
    }
  } catch (e) {
    if (!me.cancelled) onError(`Lecture vocale impossible : ${e.message}`);
  } finally {
    if (current === me) stopSpeaking();
  }
}
