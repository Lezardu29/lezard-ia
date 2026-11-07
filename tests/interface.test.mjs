// Correctifs de l'interface du chat, vérifiés dans Node sur le vrai code du navigateur (modules sans DOM).
// Lancé par tests/test_interface_js.py (node --test).
import { test } from "node:test";
import assert from "node:assert/strict";

const STATIC = new URL("../gateway/web/static/", import.meta.url);
const { sse } = await import(new URL("sse.js", STATIC));
const { renderMarkdown } = await import(new URL("markdown.js", STATIC));
const { toMarkdown } = await import(new URL("chat-export.js", STATIC));
const { pullModel } = await import(new URL("ollama-pull.js", STATIC));

// réponse HTTP dont le corps arrive en morceaux choisis (coupures n'importe où)
const streamed = (...parts) => new Response(new ReadableStream({
  start(c) { for (const p of parts) c.enqueue(new TextEncoder().encode(p)); c.close(); },
}));
const all = async (it) => { const out = []; for await (const x of it) out.push(x); return out; };

test("SSE : le dernier événement sans ligne vide finale n'est plus perdu", async () => {
  const got = await all(sse(streamed('data: {"a":1}\n\n', 'data: {"b":2}')));
  assert.deepEqual(got, [{ a: 1 }, { b: 2 }]);
});

test("SSE : séparateurs CRLF, même coupés entre deux morceaux", async () => {
  const got = await all(sse(streamed('data: {"a":1}\r', '\n\r\ndata: {"b":', '2}\r\n\r\ndata: [DONE]\r\n\r\n')));
  assert.deepEqual(got, [{ a: 1 }, { b: 2 }]);
});

test("SSE : [DONE] arrête la lecture, un caractère accentué coupé en deux reste entier", async () => {
  const bytes = new TextEncoder().encode('data: {"t":"é"}\n\n');
  const cut = bytes.indexOf(0xc3) + 1;                  // au milieu du « é »
  const res = new Response(new ReadableStream({ start(c) {
    c.enqueue(bytes.slice(0, cut)); c.enqueue(bytes.slice(cut));
    c.enqueue(new TextEncoder().encode('data: [DONE]\n\ndata: {"x":1}\n\n')); c.close();
  } }));
  assert.deepEqual(await all(sse(res)), [{ t: "é" }]);
});

test("Markdown : un octet NUL du modèle ne remplace plus un lien ni n'affiche « undefined »", () => {
  const html = renderMarkdown("voir https://exemple.fr et \u00000\u0000 puis \u00009\u0000");
  assert.equal((html.match(/<a /g) || []).length, 1);
  assert.ok(!html.includes("undefined") && !html.includes("\u0000"), html);
});

test("Markdown : le HTML du modèle reste échappé", () => {
  const html = renderMarkdown('<img src=x onerror=alert(1)> [x](javascript:alert(1)) `<b>`');
  assert.ok(!/<img|<b>|href="javascript/i.test(html), html);
});

test("Export Markdown : sans la réflexion du modèle, comme la copie", () => {
  const md = toMarkdown("T", [
    { role: "user", content: "<think>pas une réflexion</think> question" },
    { role: "assistant", content: "<think>je réfléchis…</think>La réponse." },
  ], new Date(0));
  assert.ok(md.includes("La réponse.") && !md.includes("je réfléchis"), md);
  assert.ok(md.includes("<think>pas une réflexion</think> question"));   // un message de la personne : intact
});

test("Téléchargement Ollama : un flux coupé avant « done » n'est plus annoncé réussi", async () => {
  const real = globalThis.fetch;
  try {
    globalThis.fetch = async () => streamed('{"status":"pulling","completed":5,"total":10}\n');
    assert.match(await pullModel("m", "ollama"), /interrompu/);
    globalThis.fetch = async () => streamed('{"status":"pulling","completed":10,"total":10}\n', '{"status":"done"}');
    assert.equal(await pullModel("m", "ollama"), null);     // dernière ligne sans « \n » : lue quand même
    globalThis.fetch = async () => streamed('{"error":"Ollama : manifest unknown"}\n{"status":"done"}\n');
    assert.equal(await pullModel("m", "ollama"), "Ollama : manifest unknown");
  } finally {
    globalThis.fetch = real;
  }
});
