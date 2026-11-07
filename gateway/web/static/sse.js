// Lecture d'un flux SSE de /v1/chat/completions (stream: true) : un objet JSON par événement « data: ».
// Tolère les séparateurs CRLF (proxy, tunnel) et un dernier événement sans ligne vide finale.
export async function* sse(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    // fin du flux : le dernier événement, même sans ligne vide finale, est lu
    buffer += done ? `${decoder.decode()}\n\n` : decoder.decode(value, { stream: true });
    buffer = buffer.replace(/\r\n|\r(?!$)/g, "\n");   // séparateurs CRLF (proxy) ; un \r en fin de morceau attend son \n
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const event = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      for (const line of event.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (data === "[DONE]") return;
        try { yield JSON.parse(data); } catch { /* fragment ignoré */ }
      }
    }
    if (done) return;
  }
}
