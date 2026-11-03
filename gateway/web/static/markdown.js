// Rendu Markdown sûr + coloration syntaxique, sans dépendance (5.2).
// Sécurité : tout le texte est échappé AVANT la mise en forme ; seuls les liens
// http(s)/mailto sont produits ; aucun HTML brut du modèle n'est interprété.

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ESC[c]);

// ---------- coloration syntaxique ----------

const KW = {
  python: "and as assert async await break class continue def del elif else except finally for from global if import in is lambda None nonlocal not or pass raise return True False try while with yield match case self",
  js: "async await break case catch class const continue debugger default delete do else export extends false finally for function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while with yield interface type enum implements private public protected readonly",
  go: "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var nil true false",
  rust: "as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while None Some Ok Err",
  java: "abstract boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import instanceof int interface long new null package private protected public return short static super switch this throw throws true false try void volatile while var",
  c: "auto break case char const continue default do double else enum extern float for goto if inline int long register return short signed sizeof static struct switch typedef union unsigned void volatile while class namespace template typename public private protected new delete nullptr true false bool std include define",
  sql: "select from where and or not insert into values update set delete create table drop alter add join left right inner outer on group by order having limit offset as distinct null is in like between case when then else end primary key foreign references index union all exists count sum avg min max",
  bash: "if then else elif fi for while do done case esac in function return export local echo exit set unset source cd sudo",
};
const ALIASES = {
  py: "python", python3: "python", javascript: "js", jsx: "js", ts: "js", tsx: "js", typescript: "js",
  node: "js", mjs: "js", golang: "go", rs: "rust", kotlin: "java", kt: "java", csharp: "java", cs: "java",
  cpp: "c", "c++": "c", h: "c", hpp: "c", sh: "bash", shell: "bash", zsh: "bash", console: "bash",
  powershell: "bash", ps1: "bash", postgres: "sql", mysql: "sql", sqlite: "sql",
};
const HASH_COMMENT = new Set(["python", "bash"]);

export function highlight(code, lang) {
  const l = ALIASES[lang] || lang;
  if (l === "json") return highlightWith(code, /("(?:\\.|[^"\\])*")(\s*:)?|\b(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b|\b(true|false|null)\b/g, null, true);
  const kw = KW[l] ? new Set(KW[l].split(" ").map((k) => (l === "sql" ? k.toUpperCase() : k))) : null;
  if (!kw) return escapeHtml(code);
  const comment = HASH_COMMENT.has(l) ? "#[^\\n]*" : l === "sql" ? "--[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/" : "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/";
  const str = l === "python" ? `"""[\\s\\S]*?"""|'''[\\s\\S]*?'''|` : "";
  const re = new RegExp(`(${comment})|(${str}"(?:\\\\.|[^"\\\\\\n])*"|'(?:\\\\.|[^'\\\\\\n])*'|\`(?:\\\\.|[^\`\\\\])*\`)|\\b(\\d+(?:\\.\\d+)?)\\b|([A-Za-z_][\\w]*)`, "g");
  return highlightWith(code, re, (word) => kw.has(l === "sql" ? word.toUpperCase() : word));
}

function highlightWith(code, re, isKeyword, json = false) {
  let out = "", last = 0, m;
  re.lastIndex = 0;
  while ((m = re.exec(code))) {
    out += escapeHtml(code.slice(last, m.index));
    const t = escapeHtml(m[0]);
    if (json) {
      if (m[1]) out += m[2] ? `<span class="tk-prop">${escapeHtml(m[1])}</span>${escapeHtml(m[2])}` : `<span class="tk-str">${t}</span>`;
      else if (m[3]) out += `<span class="tk-num">${t}</span>`;
      else out += `<span class="tk-kw">${t}</span>`;
    } else if (m[1]) out += `<span class="tk-com">${t}</span>`;
    else if (m[2]) out += `<span class="tk-str">${t}</span>`;
    else if (m[3]) out += `<span class="tk-num">${t}</span>`;
    else if (m[4] && isKeyword(m[4])) out += `<span class="tk-kw">${t}</span>`;
    else if (m[4] && /^[A-Z][a-z]/.test(m[4])) out += `<span class="tk-type">${t}</span>`;
    else out += t;
    last = re.lastIndex;
    if (m[0] === "") re.lastIndex++;
  }
  return out + escapeHtml(code.slice(last));
}

// ---------- inline ----------

function safeUrl(url) {
  const u = url.replace(/&amp;/g, "&").trim();
  return /^(https?:\/\/|mailto:)/i.test(u) ? escapeHtml(u) : null;
}

function inline(text) {
  const slots = [];
  const keep = (html) => `\u0000${slots.push(html) - 1}\u0000`;
  let s = escapeHtml(text);
  s = s.replace(/`([^`\n]+)`/g, (_, c) => keep(`<code>${c}</code>`));
  s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
    const u = safeUrl(url);
    return u ? keep(`<a href="${u}" target="_blank" rel="noopener noreferrer">${label}</a>`) : m;
  });
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?])/g, (m, pre, url) => {
    const u = safeUrl(url);
    return u ? pre + keep(`<a href="${u}" target="_blank" rel="noopener noreferrer">${url}</a>`) : m;
  });
  s = s.replace(/\*\*([^*\n]+)\*\*|__([^_\n]+)__/g, (_, a, b) => `<strong>${a || b}</strong>`);
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)|(^|[^_\w])_([^_\n]+)_(?![\w_])/g,
    (_, p1, a, p2, b) => `${p1 ?? p2 ?? ""}<em>${a ?? b}</em>`);
  s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[+i]);
}

// ---------- blocs ----------

const RE = {
  fence: /^\s*(`{3,}|~{3,})\s*([\w+#.-]*)/,
  heading: /^(#{1,6})\s+(.*?)\s*#*\s*$/,
  hr: /^\s*([-*_])(\s*\1){2,}\s*$/,
  quote: /^\s*>\s?/,
  ul: /^(\s*)[-*+]\s+(.*)$/,
  ol: /^(\s*)(\d+)[.)]\s+(.*)$/,
  tableSep: /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$|^\s*\|\s*:?-+:?\s*\|\s*$/,
};

const isBlockStart = (line, next) =>
  RE.fence.test(line) || RE.heading.test(line) || RE.hr.test(line) || RE.quote.test(line) ||
  RE.ul.test(line) || RE.ol.test(line) || (line.includes("|") && next !== undefined && RE.tableSep.test(next));

const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

let codeId = 0;

// blocs affichables dans le panneau d'aperçu (preview-panel.js) : page HTML, image SVG, diagramme Mermaid
export const PREVIEW_KINDS = { html: "Page HTML", svg: "Image SVG", mermaid: "Diagramme" };

function previewKind(lang, lines) {
  if (lang === "html" || lang === "htm") return "html";
  if (lang === "svg" || (lang === "xml" && /^\s*(<\?xml[^>]*>\s*)?<svg[\s>]/i.test(lines.join("\n")))) return "svg";
  if (lang === "mermaid") return "mermaid";
  return "";
}

export function renderMarkdown(src) {
  const lines = (src || "").replace(/\r\n?/g, "\n").split("\n");
  let html = "", i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    const fence = line.match(RE.fence);
    if (fence) {
      const lang = fence[2].toLowerCase();
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
      i++;                                              // fence fermante (ou fin du flux)
      const id = `code-${++codeId}`;
      const kind = previewKind(lang, body);
      const preview = kind ? `<button type="button" class="preview-btn" data-preview="${id}" data-kind="${kind}" ` +
        `aria-label="Ouvrir l'aperçu">Aperçu</button>` : "";
      const label = kind ? `<span class="code-kind">${PREVIEW_KINDS[kind]}</span>` : "";   // carte (interface Claude)
      html += `<div class="code${kind ? " previewable" : ""}"><div class="code-head"><span>${escapeHtml(lang || "code")}</span>${label}` +
        `${preview}<button type="button" class="code-dl" data-download="${id}" data-lang="${escapeHtml(lang)}" ` +
        `aria-label="Télécharger le code">Télécharger</button>` +
        `<button type="button" class="copy" data-copy="${id}" aria-label="Copier le code">Copier</button></div>` +
        `<pre><code id="${id}">${highlight(body.join("\n"), lang)}</code></pre></div>`;
      continue;
    }
    const h = line.match(RE.heading);
    if (h) { html += `<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`; i++; continue; }
    if (RE.hr.test(line)) { html += "<hr>"; i++; continue; }
    if (RE.quote.test(line)) {
      const body = [];
      while (i < lines.length && RE.quote.test(lines[i])) body.push(lines[i++].replace(RE.quote, ""));
      html += `<blockquote>${renderMarkdown(body.join("\n"))}</blockquote>`;
      continue;
    }
    if (line.includes("|") && RE.tableSep.test(lines[i + 1] || "")) {
      const head = cells(line);
      i += 2;
      let rows = "";
      while (i < lines.length && lines[i].includes("|") && lines[i].trim())
        rows += `<tr>${cells(lines[i++]).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`;
      html += `<div class="table"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead>` +
        `<tbody>${rows}</tbody></table></div>`;
      continue;
    }
    if (RE.ul.test(line) || RE.ol.test(line)) {
      const ordered = RE.ol.test(line);
      const first = ordered ? line.match(RE.ol) : line.match(RE.ul);
      const start = ordered ? +first[2] : 1;
      const indent = first[1].length;
      const items = [];
      while (i < lines.length) {
        const m = ordered ? lines[i].match(RE.ol) : lines[i].match(RE.ul);
        // élément de même niveau (une ligne plus indentée est une sous-liste : continuation)
        if (m && m[1].length <= indent) { items.push([ordered ? m[3] : m[2]]); i++; continue; }
        // ligne de continuation indentée (y compris sous-listes, rendues récursivement)
        if (items.length && /^\s{2,}\S/.test(lines[i])) { items[items.length - 1].push(lines[i].replace(/^\s{2,4}/, "")); i++; continue; }
        break;
      }
      const tag = ordered ? "ol" : "ul";
      html += `<${tag}${ordered && start !== 1 ? ` start="${start}"` : ""}>` +
        items.map(([first, ...rest]) => `<li>${inline(first)}${rest.length ? renderMarkdown(rest.join("\n")) : ""}</li>`).join("") +
        `</${tag}>`;
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !(para.length && isBlockStart(lines[i], lines[i + 1]))) para.push(lines[i++]);
    html += `<p>${para.map(inline).join("<br>")}</p>`;
  }
  return html;
}
