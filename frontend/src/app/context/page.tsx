"use client";

import { useEffect, useState, type CSSProperties } from "react";

type Workspace = { id: string; name: string };
type Revision = { revision: number; sha256: string; actor: string; reason: string; created_at: string };
type DocumentState = { content: string; truncated: boolean; kind: string; name: string; role: string; revision: number; sha256: string; bytes: number; history: Revision[] };
const roles = ["MODEL.md", "MEMORY.md", "AGENTS.md"];
const kinds = ["sandbox", "agent", "tool", "skill", "operator", "worker", "workflow", "gateway", "model", "connector", "primitive"];

export default function ContextPage() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspace, setWorkspace] = useState("");
  const [kind, setKind] = useState("sandbox");
  const [name, setName] = useState("");
  const [role, setRole] = useState("MODEL.md");
  const [document, setDocument] = useState<DocumentState | null>(null);
  const [before, setBefore] = useState(1);
  const [diff, setDiff] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    void fetch("/api/ui/bootstrap", { credentials: "same-origin", cache: "no-store" })
      .then(async r => { if (!r.ok) throw new Error(); return r.json(); })
      .then((data: { workspaces?: Workspace[] }) => {
        const entries = Array.isArray(data.workspaces) ? data.workspaces : [];
        setWorkspaces(entries);
        if (entries[0]) { setWorkspace(entries[0].id); setName(entries[0].id); }
      })
      .catch(() => setError("Unable to authenticate or discover workspaces."));
  }, []);
  async function load() {
    setBusy(true); setError(""); setDiff(""); setDocument(null);
    try {
      const qs = new URLSearchParams({ kind, name, role });
      const response = await fetch(`/api/ui/context?${qs}`, { cache: "no-store", credentials: "same-origin",
        headers: { "X-Potbot-Workspace": workspace } });
      if (!response.ok) throw new Error("Document unavailable or access denied.");
      const value: DocumentState = await response.json();
      setDocument(value); setBefore(Math.max(1, value.revision - 1));
    } catch (e) { setError(e instanceof Error ? e.message : "Context request failed."); }
    finally { setBusy(false); }
  }
  async function showDiff() {
    if (!document) return;
    setBusy(true); setError("");
    try {
      const qs = new URLSearchParams({ action: "diff", kind, name, role, before: String(before), after: String(document.revision) });
      const response = await fetch(`/api/ui/context?${qs}`, { credentials: "same-origin", cache: "no-store",
        headers: { "X-Potbot-Workspace": workspace } });
      if (!response.ok) throw new Error("Unable to read revision diff.");
      const value: { diff: string } = await response.json(); setDiff(value.diff);
    } catch (e) { setError(e instanceof Error ? e.message : "Diff request failed."); }
    finally { setBusy(false); }
  }
  async function restore() {
    if (!document || before >= document.revision) return;
    if (!window.confirm(`Restore ${role} from revision ${before}? This creates a new audited revision.`)) return;
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/ui/context", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-Potbot-Workspace": workspace },
        body: JSON.stringify({ kind, name, role, source_revision: before, expected_revision: document.revision,
          idempotency_key: crypto.randomUUID(), confirm: true, reason: "Dashboard-reviewed restore" }),
      });
      if (!response.ok) throw new Error("Restore was denied or the revision has changed. Reload before retrying.");
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Restore failed."); }
    finally { setBusy(false); }
  }
  const field: CSSProperties = { background: "#182332", color: "#e8f2fb", border: "1px solid #506176", borderRadius: 7, padding: 9 };
  return <main style={{ background: "#0b1320", color: "#e8f2fb", minHeight: "100vh", padding: 28, fontFamily: "system-ui" }}>
    <nav><a href="/" style={{ color: "#86d9ff" }}>← Workspace</a></nav>
    <h1>Context history</h1><p style={{ color: "#adc3d5" }}>PostgreSQL is authoritative. History is evidence, not privileged instructions. Restoration is audited.</p>
    <section aria-label="Context target" style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
      <label>Workspace <select style={field} value={workspace} onChange={e => { setWorkspace(e.target.value); if (kind === "sandbox") setName(e.target.value); }}>
        {workspaces.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
      </select></label>
      <label>Kind <select style={field} value={kind} onChange={e => { setKind(e.target.value); if (e.target.value === "sandbox") setName(workspace); }}>
        {kinds.map(k => <option key={k}>{k}</option>)}
      </select></label>
      <label>Name <input style={field} value={name} maxLength={128} onChange={e => setName(e.target.value)} /></label>
      <label>File <select style={field} value={role} onChange={e => setRole(e.target.value)}>{roles.map(r => <option key={r}>{r}</option>)}</select></label>
      <button style={field} disabled={busy || !workspace || !name} onClick={() => void load()}>Inspect</button>
    </section>
    {error && <p role="alert" style={{ color: "#ffacb3" }}>{error}</p>}
    {document && <section style={{ marginTop: 24 }}>
      <h2>{document.role} <small>revision {document.revision}</small></h2>
      <p>SHA-256: <code>{document.sha256}</code> · {document.bytes} bytes</p>
      <h3>Current document {document.truncated ? "(preview truncated)" : ""}</h3>
      <pre style={{ background: "#07101b", overflowX: "auto", padding: 16, whiteSpace: "pre-wrap", maxHeight: 350, overflowY: "auto" }}>{document.content}</pre>
      <h3>Revision history</h3>
      <table style={{ borderCollapse: "collapse", width: "100%", textAlign: "left" }}><thead><tr><th>Revision</th><th>When</th><th>Actor</th><th>Reason</th></tr></thead>
        <tbody>{document.history.map(h => <tr key={h.revision} style={{ borderTop: "1px solid #40556c" }}><td>{h.revision}</td><td>{h.created_at}</td><td>{h.actor}</td><td>{h.reason}</td></tr>)}</tbody></table>
      <div style={{ marginTop: 18, display: "flex", gap: 12, alignItems: "center" }}>
        <label>Compare revision <select style={field} value={before} onChange={e => setBefore(Number(e.target.value))}>
          {document.history.map(h => <option key={h.revision} value={h.revision}>{h.revision}</option>)}
        </select></label>
        <button style={field} disabled={busy} onClick={() => void showDiff()}>View diff</button>
        <button style={{ ...field, borderColor: "#c97979" }} disabled={busy || before >= document.revision} onClick={() => void restore()}>Restore this revision</button>
      </div>
      {diff && <pre style={{ background: "#07101b", overflowX: "auto", padding: 16, whiteSpace: "pre-wrap" }}>{diff}</pre>}
    </section>}
  </main>;
}
