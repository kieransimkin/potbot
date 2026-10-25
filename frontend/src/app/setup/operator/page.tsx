"use client";

import { useEffect, useState } from "react";
import styles from "../apis/setup.module.css";

type Workspace = { id: string; name: string };
type Status = {
  workspaceId: string; configured: boolean; healthy: boolean;
  journal: "healthy" | "unavailable"; releaseGate: "observed" | "unknown";
  pendingReleases: number | null; activeRuns: number | null; epoch: string | null;
};

export default function OperatorStatusPage() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspace, setWorkspace] = useState("");
  const [state, setState] = useState<Status | null>(null);
  const [notice, setNotice] = useState("Loading operator workspaces…");
  useEffect(() => {
    let alive = true;
    fetch("/api/ui/bootstrap", { credentials: "same-origin", cache: "no-store" })
      .then(async res => { if (!res.ok) throw Error(); return res.json(); })
      .then(data => {
        if (!alive || !Array.isArray(data.workspaces)) return;
        const list = (data.workspaces as Workspace[]).filter(w =>
          typeof w.id === "string" && typeof w.name === "string");
        setWorkspaces(list);
        if (list[0]) setWorkspace(list[0].id);
        else setNotice("No authorized workspace is available.");
      }).catch(() => { if (alive) setNotice("Sign in to inspect the operator."); });
    return () => { alive = false; };
  }, []);
  useEffect(() => {
    if (!workspace) return;
    let active = true;
    async function poll() {
      try {
        const res = await fetch("/api/ui/operator", { cache: "no-store", credentials: "same-origin",
          headers: { "X-Potbot-Workspace": workspace } });
        if (!res.ok) throw Error();
        const doc: Status = await res.json();
        if (doc.workspaceId !== workspace) throw Error();
        if (active) { setState(doc); setNotice(""); }
      } catch { if (active) { setState(null); setNotice("Operator attachment unavailable."); } }
    }
    void poll();
    const handle = window.setInterval(() => { if (!document.hidden) void poll(); }, 3000);
    return () => { active = false; window.clearInterval(handle); };
  }, [workspace]);
  return <main className={styles.shell}>
    <header className={styles.header}>
      <a className={styles.back} href="/">← Return to Potbot</a>
      <span className={styles.kicker}>OPERATOR SETUP / WORKSPACE HEALTH</span>
      <h1>Operator attachment</h1>
      <p>This read-only view checks the live operator that owns AI configuration history.
        It does not grant tool execution or override upgrade release gates.</p>
    </header>
    <section className={styles.toolbar}>
      <label>Authorized workspace
        <select value={workspace} onChange={e => { setWorkspace(e.target.value); setState(null); }}>
          {workspaces.map(w => <option value={w.id} key={w.id}>{w.name}</option>)}
        </select>
      </label>
    </section>
    <p role="status" className={styles.status}>{notice ||
      (state?.healthy ? "Operator attached and healthy" : "Operator unavailable or not configured")}</p>
    {state && <section className={styles.cards} aria-label="Operator health">
      <article className={styles.card}>
        <h2>Configuration history</h2>
        <p>{state.journal === "healthy" ? "Watcher healthy" : "No healthy watcher reported"}</p>
        <p>Operator: {state.configured ? "Configured" : "Not configured"}</p>
        <p>Active supervised runs: {state.activeRuns ?? "Unknown"}</p>
      </article>
      <article className={styles.card}>
        <h2>Shared tool release gates</h2>
        <p>{state.releaseGate === "observed" ?
          `${state.pendingReleases ?? 0} unreleased or unresolved upgrade(s)` :
          "Release-gate ledger not observed"}</p>
        <p>Process epoch: {state.epoch?.slice(0, 12) ?? "Unavailable"}</p>
      </article>
    </section>}
    <footer className={styles.footer}>
      <p>Monitoring updates while this page is visible. Release approval and rollback remain
        separate host-authorized workflows. Lost operator connectivity blocks managed sandbox launches.</p>
    </footer>
  </main>;
}
