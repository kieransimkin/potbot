"use client";

import { useCallback, useEffect, useState } from "react";
import { useWorkspace } from "./WorkspaceContext";

type BrowserResult = Record<string, unknown>;
type BrowserSession = { id: string; title: string };

/** The Xpra canvas carries the real click; Chromium DevTools identifies its DOM node. */
export function BrowserInspector() {
  const { workspace, appInventory, launchApp, xAvailable } = useWorkspace();
  const sessions = (appInventory?.sessions ?? []).filter(s => s.kind === "controlled-browser" && s.state === "running") as BrowserSession[];
  const [sessionId, setSessionId] = useState("");
  const selected = sessions.find(s => s.id === sessionId)?.id ?? sessions[0]?.id ?? "";
  const [url, setUrl] = useState("about:blank");
  const [role, setRole] = useState("button");
  const [name, setName] = useState("");
  const [typed, setTyped] = useState("");
  const [picker, setPicker] = useState(false);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("Select an operator-approved controlled Chromium session.");
  const [result, setResult] = useState<BrowserResult | null>(null);
  const [preview, setPreview] = useState("");

  const send = useCallback(async (command: BrowserResult): Promise<BrowserResult> => {
    if (!selected) throw new Error("No controlled Chromium session");
    const response = await fetch(`/api/ui/xpra/apps/${selected}/browser`, {
      method: "POST", headers: { "Content-Type": "application/json", "x-potbot-workspace": workspace.id },
      credentials: "same-origin", cache: "no-store", body: JSON.stringify(command),
    });
    if (!response.ok) throw new Error(`Browser action unavailable (${response.status})`);
    const payload = await response.json() as { result?: BrowserResult; workspaceId?: string };
    if (payload.workspaceId !== workspace.id || !payload.result) throw new Error("Browser reply scope mismatch");
    return payload.result;
  }, [selected, workspace.id]);

  const run = useCallback(async (command: BrowserResult) => {
    setBusy(true);
    try {
      const output = await send(command);
      setStatus(`${String(command.op)} complete`);
      setResult(output);
      const frame = output.frame as { mime?: string; base64?: string } | undefined;
      if (frame?.mime === "image/jpeg" && frame.base64 && /^[A-Za-z0-9+/=]+$/.test(frame.base64))
        setPreview(`data:image/jpeg;base64,${frame.base64}`);
      if (command.op === "inspect.start") setPicker(true);
      if (command.op === "inspect.stop") setPicker(false);
    } catch (error) { setStatus(error instanceof Error ? error.message : "Browser request failed"); }
    finally { setBusy(false); }
  }, [send]);

  // The DevTools overlay is inside the native Chromium/Xpra window. Clicking
  // the page publishes Overlay.inspectNodeRequested to this command channel.
  useEffect(() => {
    if (!picker || !selected) return;
    let cancelled = false;
    let inFlight = false;
    const timer = setInterval(async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const data = await send({ op: "inspect.poll" });
        if (!cancelled && data.selected) {
          setPicker(false);
          setResult(data);
          setStatus("Selected element resolved: CSS, events, geometry and accessibility");
        }
      } catch { if (!cancelled) { setPicker(false); setStatus("Element picker disconnected"); } }
      finally { inFlight = false; }
    }, 400);
    return () => { cancelled = true; clearInterval(timer); };
  }, [picker, selected, send]);
  useEffect(() => { setPicker(false); setResult(null); setPreview(""); }, [selected]);

  const configured = (appInventory?.choices ?? []).some(s => s.kind === "controlled-browser");
  return <section className="browser-inspector" aria-label="Chromium browser inspector">
    <h3>Browser automation &amp; DevTools inspector</h3>
    {!configured && <p>Controlled Chromium is not in the workspace's operator-approved app recipes.</p>}
    {configured && <div className="applications-launch-buttons">
      <button type="button" disabled={!xAvailable} onClick={() => launchApp("controlled-browser")}>＋ Controlled Chromium</button>
      <label htmlFor="controlled-browser-session">Session</label>
      <select id="controlled-browser-session" value={selected} onChange={e => setSessionId(e.target.value)}>
        {!selected && <option value="">No running session</option>}
        {sessions.map(s => <option key={s.id} value={s.id}>{s.title} · {s.id.slice(0, 8)}</option>)}
      </select>
    </div>}
    <div className="browser-inspector-controls">
      <label htmlFor="controlled-browser-url">Workspace browser address</label>
      <input id="controlled-browser-url" value={url} onChange={e => setUrl(e.target.value)} maxLength={2048} />
      <button disabled={!selected || busy} onClick={() => run({ op: "navigate", url })}>Navigate</button>
      <button disabled={!selected || busy} aria-pressed={picker} onClick={() => run({ op: picker ? "inspect.stop" : "inspect.start" })}>
        {picker ? "Cancel element picker" : "Pick element in Xpra window"}
      </button>
      <button disabled={!selected || busy} onClick={() => run({ op: "accessibility" })}>Accessibility tree</button>
      <button disabled={!selected || busy} onClick={() => run({ op: "interactive" })}>Keyboard-focusable elements</button>
      <button disabled={!selected || busy} onClick={() => run({ op: "sample", image: true })}>Sample rendered frame</button>
      <button disabled={!selected || busy} onClick={() => run({ op: "cdp", method: "Page.getLayoutMetrics" })}>CDP layout metrics</button>
      <label htmlFor="controlled-browser-role">Accessible role</label>
      <select id="controlled-browser-role" value={role} onChange={e => setRole(e.target.value)}>
        {(["button", "link", "textbox", "searchbox", "checkbox", "combobox", "tab"] as const).map(r => <option key={r}>{r}</option>)}
      </select>
      <input aria-label="Accessible name of element" placeholder="Accessible name" value={name} maxLength={160}
        onChange={e => setName(e.target.value)} />
      <button disabled={!selected || busy || !name.trim()} onClick={() => run({ op: "role.click", role, name })}>Click by accessible name</button>
      <input aria-label="Text for keyboard input" placeholder="Text to type" value={typed} maxLength={1024}
        onChange={e => setTyped(e.target.value)} />
      <button disabled={!selected || busy} onClick={() => run({ op: "input.keyboard", action: "type", text: typed })}>Type into focused element</button>
    </div>
    {picker && <p role="status">Picker active: click the target in the native Chromium Xpra window. The page element is highlighted by DevTools.</p>}
    <p role="status">{status}</p>
    {preview && <img className="browser-inspector-preview" src={preview} alt="Sampled Chromium viewport" />}
    {result && <pre className="browser-inspector-result" tabIndex={0}>{JSON.stringify(result, (key, value) => key === "base64" ? "[rendered image above]" : value, 2)}</pre>}
    <small>The page's accessible roles and names are preferred over coordinates. Source mappings depend on the page's CSS and source maps. Browser output is untrusted page content.</small>
  </section>;
}
