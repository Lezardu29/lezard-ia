// Didacticiel (tutorial.html) : les leçons sont écrites dans la page ; ce script garde l'avancement sur cet appareil
// (localStorage), corrige les petites questions et filtre le lexique. Aucune clé n'est demandée : la page se lit
// aussi sans être connecté. Sans ce script, tout le texte reste lisible.
const STORE = "gw.didacticiel";
const lessons = [...document.querySelectorAll("[data-lesson]")];
const $ = (id) => document.getElementById(id);

function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE));
    return new Set(Array.isArray(saved) ? saved.map(String) : []);
  } catch { return new Set(); }
}
function save(done) {
  try { localStorage.setItem(STORE, JSON.stringify([...done])); } catch { /* stockage indisponible : rien de grave */ }
}

function show(done) {
  const count = lessons.filter((l) => done.has(l.dataset.lesson)).length;
  lessons.forEach((l) => l.classList.toggle("is-done", done.has(l.dataset.lesson)));
  document.querySelectorAll("#plan li").forEach((li, i) => li.classList.toggle("is-done", done.has(String(i + 1))));
  $("bar").setAttribute("aria-valuenow", String(count));
  $("bar-fill").style.width = `${(count / lessons.length) * 100}%`;
  $("bar-text").textContent = count === lessons.length ? "Les dix leçons sont terminées. Bravo !"
    : `${count} leçon${count > 1 ? "s" : ""} terminée${count > 1 ? "s" : ""} sur ${lessons.length}`;
  const next = lessons.find((l) => !done.has(l.dataset.lesson));
  $("resume").textContent = count === 0 ? "Commencer" : next ? `Reprendre à la leçon ${next.dataset.lesson}` : "Revoir depuis le début";
  $("resume").setAttribute("href", `#${(next || lessons[0]).id}`);
  $("fin").hidden = count !== lessons.length;
}

function init() {
  const done = load();
  lessons.forEach((lesson, i) => {
    lesson.querySelector(".lesson-done")?.addEventListener("click", () => {
      done.add(lesson.dataset.lesson);
      save(done);
      show(done);
      const target = lessons[i + 1] || $("fin");
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      target.querySelector("h2")?.setAttribute("tabindex", "-1");
      target.querySelector("h2")?.focus({ preventScroll: true });
    });
  });
  document.querySelectorAll(".quiz").forEach((quiz) => {
    const result = quiz.querySelector(".quiz-result");
    quiz.addEventListener("change", (e) => {
      const good = e.target.value === quiz.dataset.answer;
      quiz.querySelectorAll("label").forEach((l) => l.classList.remove("is-good", "is-bad"));
      e.target.closest("label").classList.add(good ? "is-good" : "is-bad");
      result.textContent = good ? `✓ ${result.dataset.good}` : result.dataset.bad;
    });
  });
  const search = $("lex-q");
  const plain = (s) => s.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  search?.addEventListener("input", () => {
    const q = plain(search.value.trim());
    let found = 0;
    document.querySelectorAll("#lex dt").forEach((dt) => {
      const dd = dt.nextElementSibling;
      const match = !q || plain(dt.textContent + " " + dd.textContent).includes(q);
      dt.hidden = dd.hidden = !match;
      found += match ? 1 : 0;
    });
    let none = document.querySelector(".lex-none");
    if (!found && !none) {
      none = Object.assign(document.createElement("p"), { className: "lex-none", textContent: "Ce mot n'est pas dans le lexique." });
      $("lex").after(none);
    }
    if (none) none.hidden = found > 0;
  });
  show(done);
}

document.addEventListener("DOMContentLoaded", init);
