/** Incremental OpenAI chat SSE parser; does not infer success from EOF alone. */
export type ChatStreamEvent =
  | { type: "delta"; text: string }
  | { type: "complete" }
  | { type: "error"; message: string };

export async function readChatStream(
  stream: ReadableStream<Uint8Array>, onEvent: (event: ChatStreamEvent) => void,
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "", bytes = 0, completed = false, failed = false;
  function process(block: string) {
    const rows = block.split("\n");
    const eventType = rows.find(s => s.startsWith("event:"))?.slice(6).trim();
    const payload = rows.filter(s => s.startsWith("data:")).map(s => s.slice(5).trimStart()).join("\n");
    if (!payload) return;
    if (payload === "[DONE]") { completed = true; return; }
    let value: unknown;
    try { value = JSON.parse(payload); } catch {
      failed = true; onEvent({ type: "error", message: "Invalid response stream from Potbot." }); return;
    }
    if (eventType === "error" || (value && typeof value === "object" && "error" in value)) {
      failed = true; onEvent({ type: "error", message: "Potbot reported a model or transport failure." }); return;
    }
    if (!value || typeof value !== "object" || !Array.isArray((value as { choices?: unknown }).choices)) return;
    const choices = (value as { choices: unknown[] }).choices;
    const first = choices[0];
    if (!first || typeof first !== "object") return;
    const row = first as { delta?: { content?: unknown }; finish_reason?: unknown };
    if (row.finish_reason !== undefined && row.finish_reason !== null && row.finish_reason !== "stop") {
      failed = true;
      onEvent({ type: "error", message: "Potbot stopped before a complete answer; review the run status." });
      return;
    }
    if (row.delta && typeof row.delta.content === "string" && row.delta.content) onEvent({ type: "delta", text: row.delta.content });
  }
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      bytes += next.value.byteLength;
      if (bytes > 2_000_000) throw new Error("Inference response exceeded safety limit");
      buffer += decoder.decode(next.value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      let end: number;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const item = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        process(item);
        if (failed) return;
      }
      if (buffer.length > 100_000) throw new Error("Oversized SSE event");
      if (completed) break;
    }
    if (completed && !failed) onEvent({ type: "complete" });
    else if (!failed) onEvent({ type: "error", message: "Stream ended before completion; outcome is uncertain." });
  } catch {
    if (!failed) onEvent({ type: "error", message: "Connection interrupted; outcome is uncertain." });
  } finally { try { await reader.cancel(); } catch { /* Already closed. */ } reader.releaseLock(); }
}
