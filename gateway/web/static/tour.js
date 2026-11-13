// Visite guidée de l'écran de discussion : un cadre lumineux se pose sur chaque élément, avec deux phrases simples.
// Lancée par le lien « Visite guidée » de l'accueil, par le menu, ou par /app?visite=1 (depuis le didacticiel).
// Elle ne clique sur rien et n'envoie rien : elle montre, c'est tout. Échap ou « Terminer » la ferme.
const SEEN = "gw.premiers-pas";

// cible : identifiant d'un élément de app.html ; une étape dont l'élément est caché (téléphone) est sautée
export const STEPS = [
  { target: "prompt", title: "Écrivez ici",
    text: "Posez votre question comme vous la poseriez à quelqu'un, puis appuyez sur Entrée. La réponse s'écrit juste au-dessus." },
  { target: "toolbar", title: "Qui répond",
    text: "Ce bouton choisit le modèle, le « cerveau » de l'IA. Laissez-le sur « Auto » : Lezard IA prend celui qui convient." },
  { target: "attach", title: "Joindre un document",
    text: "Ajoutez un PDF, une image ou un texte, puis posez votre question dessus. Le fichier reste sur votre ordinateur." },
  { target: "mic", title: "Dicter",
    text: "Parlez au lieu d'écrire. La dictée s'installe depuis la marketplace (« Dictée et lecture vocales »)." },
  { target: "sb-new", title: "Nouvelle conversation",
    text: "Changez de sujet ici : l'IA repart de zéro et ne mélange pas vos questions." },
  { target: "conv-list", title: "Vos conversations",
    text: "Elles sont gardées ici. Cliquez sur l'une d'elles pour la reprendre où vous l'aviez laissée." },
  { target: "acct-btn", title: "Le menu",
    text: "Modèles, Marketplace, téléphone, apparence, et le didacticiel : tout le reste se trouve ici." },
];  // fmt: skip

const $ = (id) => document.getElementById(id);
let state = null;

function visible(el) {
  if (!el || el.hidden) return false;
  const r = el.getBoundingClientRect();
  return r.width > 4 && r.height > 4 && r.bottom > 0 && r.top < window.innerHeight;
}

function place() {
  if (!state) return;
  const step = state.steps[state.at];
  const r = $(step.target).getBoundingClientRect();
  const pad = 8;
  const hole = state.hole.style;
  hole.left = `${Math.max(r.left - pad, 4)}px`;
  hole.top = `${Math.max(r.top - pad, 4)}px`;
  hole.width = `${Math.min(r.width + 2 * pad, window.innerWidth - 8)}px`;
  hole.height = `${Math.min(r.height + 2 * pad, window.innerHeight - 8)}px`;
  // la bulle : sous l'élément s'il reste de la place, sinon au-dessus ; toujours dans l'écran
  const card = state.card;
  const cw = card.offsetWidth, ch = card.offsetHeight, gap = 18;
  let top = r.bottom + gap;
  if (top + ch > window.innerHeight - 8) top = r.top - ch - gap;
  if (top < 8) top = Math.max(8, Math.min(window.innerHeight - ch - 8, r.top));
  let left = r.left + r.width / 2 - cw / 2;
  if (r.width > window.innerWidth * 0.5 || top === r.top) left = r.left + 24;
  if (top === r.top && r.right + gap + cw < window.innerWidth) left = r.right + gap;   // à côté d'un élément haut
  card.style.top = `${top}px`;
  card.style.left = `${Math.max(8, Math.min(left, window.innerWidth - cw - 8))}px`;
}

function render() {
  const step = state.steps[state.at];
  state.title.textContent = step.title;
  state.text.textContent = step.text;
  state.count.textContent = `${state.at + 1} / ${state.steps.length}`;
  state.back.hidden = state.at === 0;
  state.next.textContent = state.at === state.steps.length - 1 ? "Terminer" : "Suivant";
  place();
  state.next.focus();
}

function end() {
  if (!state) return;
  state.root.remove();
  window.removeEventListener("resize", place);
  document.removeEventListener("keydown", state.onKey, true);
  state = null;
  try { localStorage.setItem(SEEN, "vu"); } catch { /* stockage indisponible */ }
  if ($("first-steps")) $("first-steps").hidden = true;
  $("prompt")?.focus();
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
}

export function startTour() {
  if (state) return;
  const steps = STEPS.filter((s) => visible($(s.target)));
  if (!steps.length) return;
  const root = el("div", "tour");
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-label", "Visite guidée de l'écran");
  const hole = el("div", "tour-hole");
  const card = el("div", "tour-card");
  const count = el("p", "tour-count");
  const title = el("h2", "tour-title");
  const text = el("p", "tour-text");
  const foot = el("div", "tour-foot");
  const quit = el("button", "btn btn-ghost btn-inline", "Passer");
  const back = el("button", "btn btn-ghost btn-inline", "Précédent");
  const next = el("button", "btn btn-primary btn-inline", "Suivant");
  [quit, back, next].forEach((b) => { b.type = "button"; });
  foot.append(quit, el("span", "grow"), back, next);
  card.append(count, title, text, foot);
  root.append(hole, card);
  document.body.append(root);
  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); end(); }
    if (e.key === "ArrowRight") next.click();
    if (e.key === "ArrowLeft" && !back.hidden) back.click();
  };
  state = { root, hole, card, count, title, text, back, next, steps, at: 0, onKey };
  quit.addEventListener("click", end);
  back.addEventListener("click", () => { state.at = Math.max(0, state.at - 1); render(); });
  next.addEventListener("click", () => {
    if (state.at >= state.steps.length - 1) return end();
    state.at += 1;
    render();
  });
  window.addEventListener("resize", place);
  document.addEventListener("keydown", onKey, true);
  render();
}

/** Lien « Première fois ? » de l'accueil, bouton du menu, et lancement par l'adresse (?visite=1). */
export function initTour() {
  let seen = false;
  try { seen = localStorage.getItem(SEEN) === "vu"; } catch { /* privé */ }
  const first = $("first-steps");
  if (first) {
    first.hidden = seen;
    $("first-steps-x")?.addEventListener("click", () => {
      first.hidden = true;
      try { localStorage.setItem(SEEN, "vu"); } catch { /* ignoré */ }
    });
  }
  $("tour-start")?.addEventListener("click", startTour);
  $("tour-btn")?.addEventListener("click", () => setTimeout(startTour, 150));   // le menu se referme d'abord
  try {
    const top = window.top, params = new URLSearchParams(top.location.search);
    if (params.get("visite")) {
      params.delete("visite");
      top.history.replaceState(null, "", top.location.pathname + (params.toString() ? `?${params}` : ""));
      setTimeout(startTour, 700);
    }
  } catch { /* autre origine */ }
}
