// Export d'une conversation (19.3) : Markdown (fil visible, lisible partout) ou JSON (tout, variantes comprises).
// Fabriqué dans le navigateur : rien ne repasse par le réseau, conversations du navigateur comprises.
import { saveBlob } from "./save-file.js";

const textOf = (content) => (typeof content === "string" ? content
  : (content || []).filter((p) => p.type === "text").map((p) => p.text).join("\n"));
const imageCount = (content) => (typeof content === "string" ? 0
  : (content || []).filter((p) => p.type === "image_url").length);

function slug(title) {
  const s = (title || "conversation").normalize("NFD").replace(/[̀-ͯ]/g, "")
    .replace(/[^\w-]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase().slice(0, 60);
  return s || "conversation";
}

export function toMarkdown(title, messages, when = new Date()) {
  const lines = [`# ${title || "Conversation"}`, "", `_Exportée le ${when.toLocaleString("fr-FR")} — Lezard IA_`, ""];
  for (const m of messages) {
    const who = m.role === "user" ? "Vous" : m.role === "assistant" ? "Assistant" : "Système";
    const model = m.meta?.model ? ` · ${m.meta.provider ? `${m.meta.provider}/` : ""}${m.meta.model}` : "";
    const variant = m.versions ? ` · version ${m.active + 1}/${m.versions.length}` : "";
    lines.push(`## ${who}${model}${variant}`, "");
    const text = textOf(m.content).trim();
    if (text) lines.push(text, "");
    const images = imageCount(m.content);
    if (images) lines.push(`_${images} image${images > 1 ? "s" : ""} jointe${images > 1 ? "s" : ""}_`, "");
    for (const a of m.attachments || []) lines.push(`_Document joint : ${a.filename}_`, "");
  }
  return lines.join("\n");
}

export function toJson(title, messages, when = new Date()) {
  return JSON.stringify({ title: title || "", exported_at: when.toISOString(), format: "smart-ai-gateway.conversation/1",
                          messages }, null, 2);
}

export function download(title, format, messages) {
  const md = format === "md";
  const text = md ? toMarkdown(title, messages) : toJson(title, messages);
  const blob = new Blob([text], { type: md ? "text/markdown;charset=utf-8" : "application/json" });
  saveBlob(blob, `${slug(title)}.${md ? "md" : "json"}`);
  return text;
}
