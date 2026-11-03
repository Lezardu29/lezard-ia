// Menus déroulants aux couleurs du thème : la liste d'options native d'un <select> ne suit pas les variables de
// thème (texte clair sur fond blanc avec certains thèmes). Le <select> reste en place, avec son style, son
// libellé et ses événements ; seule la liste qui s'ouvre est remplacée par une liste de l'application.
// Tous les <select> des pages sont concernés, y compris ceux créés plus tard (« Autre modèle… » du chat), sauf
// les listes multiples et data-native. Écran tactile : liste du système (lisible, adaptée au doigt).
let current = null;           // { sel, list, active, observer }
let typed = "", typedAt = 0;  // recherche par les premières lettres
let uid = 0;

const touch = () => window.matchMedia?.("(pointer: coarse)").matches;
const eligible = (el) => el instanceof HTMLSelectElement && !el.multiple && el.size <= 1 && !("native" in el.dataset);

function entries(sel) {
  const out = [];
  for (const child of sel.children) {
    if (child instanceof HTMLOptGroupElement) {
      out.push({ group: child.label });
      for (const opt of child.children) if (!opt.hidden) out.push({ opt, nested: true });
    } else if (child instanceof HTMLOptionElement && !child.hidden) {
      out.push({ opt: child });
    }
  }
  return out;
}

const usable = (opt) => opt && !opt.disabled && !opt.parentElement?.disabled;

function render() {
  const { sel, list } = current;
  const nodes = [];
  for (const e of entries(sel)) {
    if (e.group !== undefined) {
      const g = document.createElement("div");
      g.className = "cs-group";
      g.textContent = e.group;
      nodes.push(g);
      continue;
    }
    const o = document.createElement("div");
    o.className = `cs-opt${e.nested ? " cs-nested" : ""}${e.opt.value === "" ? " cs-placeholder" : ""}`;
    o.id = `${list.id}-${e.opt.index}`;
    o.setAttribute("role", "option");
    o.setAttribute("aria-selected", String(e.opt.selected));
    if (!usable(e.opt)) o.setAttribute("aria-disabled", "true");
    o.dataset.index = e.opt.index;
    o.textContent = e.opt.label || e.opt.text;
    o.title = o.textContent;
    if (e.opt.index === current.active) o.classList.add("cs-active");
    nodes.push(o);
  }
  list.replaceChildren(...nodes);
  sel.setAttribute("aria-activedescendant", `${list.id}-${current.active}`);
  place();
}

function place() {
  const { sel, list } = current;
  const r = sel.getBoundingClientRect();
  const vw = document.documentElement.clientWidth, vh = window.innerHeight;
  list.style.minWidth = `${Math.round(r.width)}px`;
  list.style.maxHeight = "";
  const below = vh - r.bottom - 8, above = r.top - 8;
  const up = list.scrollHeight > below && above > below;
  list.style.maxHeight = `${Math.max(120, Math.min(340, up ? above : below))}px`;
  list.style.top = up ? "" : `${Math.round(r.bottom + 4)}px`;
  list.style.bottom = up ? `${Math.round(vh - r.top + 4)}px` : "";
  list.style.left = `${Math.round(Math.max(8, Math.min(r.left, vw - list.offsetWidth - 8)))}px`;
  reveal();
}

// fait défiler la liste seule (scrollIntoView ferait aussi défiler la page, qui replacerait la liste, en boucle)
function reveal() {
  const { list } = current;
  const o = list.querySelector(".cs-active");
  if (!o) return;
  if (o.offsetTop < list.scrollTop) list.scrollTop = o.offsetTop - 4;
  else if (o.offsetTop + o.offsetHeight > list.scrollTop + list.clientHeight) {
    list.scrollTop = o.offsetTop + o.offsetHeight - list.clientHeight + 4;
  }
}

function open(sel) {
  if (sel.disabled) return;
  close();
  const list = document.createElement("div");
  list.className = "cs-list";
  list.id = `cs-${++uid}`;
  list.setAttribute("role", "listbox");
  if (sel.getAttribute("aria-label")) list.setAttribute("aria-label", sel.getAttribute("aria-label"));
  list.addEventListener("mousedown", (e) => e.preventDefault());        // le focus reste sur le <select>
  list.addEventListener("click", (e) => {
    const o = e.target.closest(".cs-opt");
    if (o && o.getAttribute("aria-disabled") !== "true") choose(Number(o.dataset.index));
  });
  list.addEventListener("mousemove", (e) => {
    const o = e.target.closest(".cs-opt");
    if (o && o.getAttribute("aria-disabled") !== "true") move(Number(o.dataset.index), false);
  });
  (sel.closest("dialog") || document.body).append(list);               // au-dessus d'une boîte de dialogue
  const observer = new MutationObserver(() => current && render());     // options chargées après l'ouverture
  observer.observe(sel, {                               // pas les attributs aria-* posés ici : ils boucleraient
    childList: true, subtree: true, characterData: true,
    attributes: true, attributeFilter: ["disabled", "hidden", "label", "selected", "value"],
  });
  current = { sel, list, active: sel.selectedIndex, observer };
  sel.setAttribute("aria-expanded", "true");
  sel.setAttribute("aria-controls", list.id);
  render();
}

function close() {
  if (!current) return;
  const { sel, list, observer } = current;
  current = null;
  observer.disconnect();
  list.remove();
  sel.setAttribute("aria-expanded", "false");
  sel.removeAttribute("aria-activedescendant");
}

function choose(index) {
  const { sel } = current;
  close();
  sel.focus();
  if (index < 0 || sel.selectedIndex === index) return;
  sel.selectedIndex = index;
  sel.dispatchEvent(new Event("input", { bubbles: true }));
  sel.dispatchEvent(new Event("change", { bubbles: true }));
}

function move(index, scroll = true) {
  current.active = index;
  for (const o of current.list.querySelectorAll(".cs-opt")) {
    o.classList.toggle("cs-active", Number(o.dataset.index) === index);
  }
  current.sel.setAttribute("aria-activedescendant", `${current.list.id}-${index}`);
  if (scroll) reveal();
}

function step(delta) {
  const opts = [...current.sel.options].filter((o) => usable(o) && !o.hidden);
  if (!opts.length) return;
  const at = opts.findIndex((o) => o.index === current.active);
  const last = opts.length - 1;
  const next = !Number.isFinite(delta) ? (delta > 0 ? last : 0)
    : at < 0 ? (delta > 0 ? 0 : last) : Math.max(0, Math.min(last, at + delta));
  move(opts[next].index);
}

function typeahead(key) {
  const now = Date.now();
  typed = now - typedAt > 700 ? key : typed + key;
  typedAt = now;
  const opts = [...current.sel.options].filter((o) => usable(o) && !o.hidden);
  const hit = opts.find((o) => (o.label || o.text).toLowerCase().startsWith(typed.toLowerCase()));
  if (hit) move(hit.index);
}

document.addEventListener("mousedown", (e) => {
  const sel = e.target;
  if (eligible(sel) && e.button === 0 && !touch()) {
    e.preventDefault();                                   // pas de liste native
    if (current?.sel === sel) return close();
    sel.focus();
    open(sel);
  } else if (current && !current.list.contains(e.target)) {
    close();
  }
}, true);

document.addEventListener("keydown", (e) => {
  const sel = e.target;
  if (!eligible(sel) || touch()) return;
  if (!current || current.sel !== sel) {
    if (e.key === " " || e.key === "Enter" || e.key === "F4" || (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowUp"))) {
      if (e.key === "Enter" && sel.form) return;          // Entrée garde son rôle dans un formulaire
      e.preventDefault();
      open(sel);
    }
    return;
  }
  const keys = {
    ArrowDown: () => step(1), ArrowUp: () => step(-1), PageDown: () => step(8), PageUp: () => step(-8),
    Home: () => step(-Infinity), End: () => step(Infinity),
    Enter: () => choose(current.active), " ": () => (typed && Date.now() - typedAt < 700 ? typeahead(" ") : choose(current.active)),
    Escape: () => close(),
  };
  if (keys[e.key]) {
    e.preventDefault();
    e.stopPropagation();                                  // Échap ferme la liste, pas la boîte de dialogue
    keys[e.key]();
  } else if (e.key === "Tab") {
    close();
  } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    typeahead(e.key);
  }
}, true);

document.addEventListener("focusout", (e) => {
  if (current && e.target === current.sel) setTimeout(() => current && document.activeElement !== current.sel && close());
});
let placing = 0;
document.addEventListener("scroll", (e) => {
  if (!current || current.list.contains(e.target) || placing) return;
  placing = requestAnimationFrame(() => { placing = 0; if (current) place(); });   // une fois par image au plus
}, true);
window.addEventListener("resize", close);
