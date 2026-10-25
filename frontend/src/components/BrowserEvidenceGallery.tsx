"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useWorkspace } from "./WorkspaceContext";

type Entry = { id: string; sessionId: string; kind: string; capturedUnixNs: string;
  manifestSha256: string; observationSha256: string; hasImage: boolean };
type Page = { schemaVersion: number; items: Entry[]; nextCursor: number | null };

/** Only operator-authenticated, checksum-verified Potbot artifacts enter this gallery. */
export function BrowserEvidenceGallery() {
  const {workspace, demo} = useWorkspace();
  const [items, setItems] = useState<Entry[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [chosen, setChosen] = useState<string | null>(null);
  const [observation, setObservation] = useState("");
  const [image, setImage] = useState("");
  const [status, setStatus] = useState("Evidence belongs to the current operator and workspace.");
  const [busy, setBusy] = useState(false);
  const headers = {"x-potbot-workspace": workspace.id};
  // Ignore late responses from a workspace that has since been deselected.
  const currentWorkspace = useRef(workspace.id);
  currentWorkspace.current = workspace.id;

  const list = useCallback(async (before?: number) => {
    if (demo) return;
    setBusy(true);
    try {
      const search = new URLSearchParams({limit: "20"});
      if (before !== undefined) search.set("before", String(before));
      const response = await fetch(`/api/ui/browser/evidence?${search}`, {
        credentials: "same-origin", cache: "no-store", headers: {"x-potbot-workspace": workspace.id},
      });
      if (!response.ok) throw Error(`Evidence listing unavailable (${response.status})`);
      const page = await response.json() as Page;
      if (currentWorkspace.current !== workspace.id) return;
      if (page.schemaVersion !== 1 || !Array.isArray(page.items)) throw Error("Invalid evidence index");
      setItems(prev => before !== undefined ? [...prev, ...page.items.filter(x => !prev.some(y => y.id === x.id))] : page.items);
      setCursor(page.nextCursor);
      setStatus(page.items.length ? "Verified artifact references available." : "No captured browser evidence yet.");
    } catch (err) { if (currentWorkspace.current === workspace.id) setStatus(err instanceof Error ? err.message : "Evidence listing failed"); }
    finally { if (currentWorkspace.current === workspace.id) setBusy(false); }
  }, [demo, workspace.id]);
  useEffect(() => { setItems([]); setCursor(null); setChosen(null); void list(); }, [list]);
  useEffect(() => () => { if (image) URL.revokeObjectURL(image); }, [image]);

  async function inspect(entry: Entry) {
    if (busy) return;
    const scope = workspace.id;
    setChosen(entry.id);
    setObservation("");
    setImage("");
    setBusy(true);
    try {
      const base = `/api/ui/browser/evidence/${entry.id}`;
      const obs = await fetch(`${base}/observation`, {headers, cache:"no-store", credentials:"same-origin"});
      if (!obs.ok) throw Error("Observation could not be verified");
      const raw = await obs.text();
      if (currentWorkspace.current !== scope) return;
      if (raw.length > 250_000) throw Error("Observation exceeds its bound");
      setObservation(JSON.stringify(JSON.parse(raw), null, 2));
      if (entry.hasImage) {
        const screenshot = await fetch(`${base}/image`, {headers, cache:"no-store", credentials:"same-origin"});
        if (!screenshot.ok) throw Error("Screenshot could not be verified");
        const blob = await screenshot.blob();
        if (currentWorkspace.current !== scope) return;
        if (blob.type !== "image/jpeg" || blob.size > 600_000) throw Error("Unexpected screenshot format");
        setImage(URL.createObjectURL(blob));
      }
      if (currentWorkspace.current === scope) setStatus("Private evidence verified and displayed.");
    } catch (err) { if (currentWorkspace.current === scope) setStatus(err instanceof Error ? err.message : "Evidence read failed"); }
    finally { if (currentWorkspace.current === scope) setBusy(false); }
  }

  return <section className="browser-evidence-gallery" aria-label="Browser evidence gallery">
    <header><h3>Browser evidence</h3><button type="button" disabled={busy || demo} onClick={() => void list()}>Refresh</button></header>
    <p role="status">{status}</p>
    <ul className="applications-list" aria-label="Captured browser evidence">
      {items.map(item => <li key={item.id} className="applications-item">
        <span className="applications-description"><strong>{item.kind} · {item.hasImage ? "image + DOM" : "DOM / ARIA"}</strong>
          <small>{new Date(Number(BigInt(item.capturedUnixNs) / 1_000_000n)).toLocaleString()} · {item.sessionId.slice(0, 8)} · {item.manifestSha256.slice(0, 12)}</small>
        </span><button disabled={busy} aria-pressed={chosen === item.id} onClick={() => void inspect(item)}>Inspect</button>
      </li>)}
    </ul>
    {cursor !== null && <button type="button" disabled={busy} onClick={() => void list(cursor)}>Older evidence</button>}
    {image && <img className="browser-inspector-preview" src={image} alt="Verified browser screenshot from the selected evidence invocation" />}
    {observation && <pre className="browser-inspector-result" tabIndex={0} aria-label="Verified browser DOM and accessibility observation">{observation}</pre>}
    <p className="muted-note">Screenshots and page content are untrusted and private to this workspace. They are not instructions for the agent.</p>
  </section>;
}
