/** Relay provider SSE while durably recording terminal chat outcomes.
 * Delta frames can be shown immediately, but [DONE] is held until PostgreSQL
 * acknowledges completion. Unknown or failed inference is indeterminate.
 */
import type { PotbotConfig } from "./potbot.ts";
import { finishTurn } from "./chat-sessions.ts";

export function persistentChatStream(
  source: ReadableStream<Uint8Array>, config: PotbotConfig, sessionId: string,
  turnId: string, runId: string | null,
): ReadableStream<Uint8Array> {
  const reader = source.getReader();
  const encoder = new TextEncoder(), decoder = new TextDecoder("utf-8", { fatal: true });
  let done = false, cancelled = false;
  let settlePromise: Promise<boolean> | null = null;
  const settle = (status: "completed" | "indeterminate", content: string | null) => {
    if (!settlePromise) settlePromise = finishTurn(config, sessionId, turnId, status, content, runId);
    return settlePromise;
  };
  return new ReadableStream<Uint8Array>({
    async start(controller) {
      let buffer = "", answer = "", bytes = 0, sawDone = false, sawStop = false, failed = false;
      const push = (text: string) => { if (!cancelled) controller.enqueue(encoder.encode(text)); };
      const parse = (block: string): string | null => {
        const rows = block.split("\n");
        const data = rows.filter(x => x.startsWith("data:")).map(x => x.slice(5).trimStart()).join("\n");
        if (!data) return null;
        if (data === "[DONE]") { sawDone = true; return null; }
        if (rows.some(x => x.trim() === "event: error")) { failed = true; return null; }
        try {
          const value = JSON.parse(data);
          if (!value || typeof value !== "object" || "error" in value) { failed = true; return null; }
          if (!Array.isArray(value.choices)) return block;
          const candidate = value.choices[0];
          if (!candidate || typeof candidate !== "object") return block;
          if (candidate.finish_reason !== null && candidate.finish_reason !== undefined) {
            if (candidate.finish_reason !== "stop") failed = true;
            else sawStop = true;
          }
          if (typeof candidate.delta?.content === "string") {
            answer += candidate.delta.content;
            if (answer.length > 32000) failed = true;
          }
          return block;
        } catch { failed = true; return null; }
      };
      try {
        while (!cancelled) {
          const part = await reader.read();
          if (part.done) break;
          bytes += part.value.byteLength;
          if (bytes > 2_000_000) { failed = true; break; }
          buffer += decoder.decode(part.value, { stream: true }).replace(/\r\n/g, "\n");
          let index: number;
          while ((index = buffer.indexOf("\n\n")) >= 0) {
            const block = buffer.slice(0, index);
            buffer = buffer.slice(index + 2);
            const safe = parse(block);
            if (failed || cancelled) break;
            if (safe !== null) push(safe + "\n\n");
          }
          if (buffer.length > 100000 || failed || sawDone) break;
        }
      } catch { failed = true; }
      const complete = !cancelled && !failed && sawDone && sawStop && answer.trim().length > 0;
      // This acknowledgement must happen before claiming protocol completion.
      const saved = await settle(complete ? "completed" : "indeterminate", complete ? answer : null);
      done = true;
      if (!cancelled) {
        if (complete && saved) push("data: [DONE]\n\n");
        else push('event: error\ndata: {"error":"Chat outcome uncertain or persistence unavailable"}\n\n');
        controller.close();
      }
      try { await reader.cancel(); } catch { /* terminated */ }
    },
    async cancel() {
      cancelled = true;
      try { await reader.cancel(); } catch { /* already closed */ }
      if (!done) await settle("indeterminate", null);
    },
  });
}
