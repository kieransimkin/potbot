"use client";

import { useCallback, useEffect, useState } from "react";
import type { UpgradeRecord } from "../../../lib/tool-upgrades.ts";
import styles from "../apis/setup.module.css";

type Workspace = { id: string; name: string };
const headers = (id: string): HeadersInit => ({ "X-Potbot-Workspace": id });

export default function ToolUpgradeSetup() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState("");
  const [items, setItems] = useState<UpgradeRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("Loading approved workspaces…");

  const refresh = useCallback(async (workspace: string) => {
    setItems([]); setNotice("Loading tool test results…");
    try {
      const r = await fetch("/api/ui/tools/upgrades", { credentials: "same-origin", cache: "no-store", headers: headers(workspace) });
      if (!r.ok) throw new Error("unavailable");
      const data = await r.json() as { workspaceId: string; items: UpgradeRecord[] };
      if (data.workspaceId !== workspace || !Array.isArray(data.items)) throw new Error("invalid");
      setItems(data.items); setNotice("");
    } catch { setNotice("Tool upgrade records are unavailable. Check operator login and backend migrations."); }
  }, []);

  useEffect(() => {
    let active = true;
    fetch("/api/ui/bootstrap", { credentials: "same-origin", cache: "no-store" })
      .then(async r => { if (!r.ok) throw new Error("login"); return r.json(); })
      .then(data => {
        if (!active || !Array.isArray(data.workspaces)) return;
        const known = (data.workspaces as Workspace[]).filter(w => typeof w.id === "string" && typeof w.name === "string");
        setWorkspaces(known);
        if (known.length) { setWorkspaceId(known[0].id); void refresh(known[0].id); }
        else setNotice("No authorized workspace configured.");
      }).catch(() => { if (active) setNotice("Sign in to the operator workspace to manage tool upgrades."); });
    return () => { active = false; };
  }, [refresh]);

  async function decide(entry: UpgradeRecord, action: "approve" | "reject") {
    if (busy || !workspaceId || entry.state !== "awaiting_approval") return;
    const verb = action === "approve" ? "approve activation of" : "reject";
    if (!window.confirm(`Do you want to ${verb} ${entry.slug} from ${entry.oldCommit.slice(0, 12)} to ${entry.newCommit.slice(0, 12)}?\n\nS9 verified ${entry.passedTests} tests. Tested evidence: ${entry.evidenceSha256}`)) return;
    setBusy(true); setNotice("Recording operator decision…");
    try {
      const response = await fetch(`/api/ui/tools/upgrades/${entry.id}/${action}`, {
        method: "POST", credentials: "same-origin",
        headers: { ...headers(workspaceId), "Content-Type": "application/json", "Origin": window.location.origin },
        body: JSON.stringify({ revision: entry.revision, evidenceSha256: entry.evidenceSha256, confirm: true }),
      });
      if (!response.ok) throw new Error("cannot approve");
      await refresh(workspaceId);
      setNotice(action === "approve" ? "Upgrade approved. The host operator must execute it and repin verified binaries." : "Upgrade rejected.");
    } catch { setNotice("Approval failed or the evidence changed. Refresh and review before retrying."); }
    finally { setBusy(false); }
  }

  return <main className={styles.shell}>
    <header className={styles.header}>
      <a href="/" className={styles.back}>← Return to Potbot</a>
      <span className={styles.kicker}>OPERATOR SETUP / SHARED TOOLS</span>
      <h1>Tool testing and upgrades</h1>
      <p>Review S9-tested source revisions and explicitly approve promotion. The browser cannot run installers or change executable hashes; an independent host worker enforces the upgrade.</p>
    </header>
    <section className={styles.toolbar} aria-label="Authorized workspace">
      <label>Workspace
        <select value={workspaceId} onChange={e => { setWorkspaceId(e.target.value); void refresh(e.target.value); }}>
          {workspaces.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
        </select>
      </label>
      <button type="button" disabled={!workspaceId || busy} onClick={() => void refresh(workspaceId)}>Reload test results</button>
    </section>
    <p role="status" className={styles.status}>{notice}</p>
    <section className={styles.cards} aria-label="Tested tool upgrades">
      {items.map(item => <article className={styles.card} key={item.id}>
        <div className={styles.cardHead}><h2>{item.slug}</h2><span className={styles.pill}>{item.state.replace(/_/g, " ")}</span></div>
        <p className={styles.subtle}>Revision {item.oldCommit.slice(0, 12)} → {item.newCommit.slice(0, 12)}</p>
        <p>Passed tests: <strong>{item.passedTests}</strong></p>
        <ul>{item.tests.map(test => <li key={test.name}>{test.name} · {test.kind} · {test.elapsedMs} ms · Passed</li>)}</ul>
        <p className={styles.subtle}>Test evidence SHA-256: <code>{item.evidenceSha256}</code></p>
        <p className={styles.subtle}>Test suite: <code>{item.suiteSha256.slice(0, 20)}</code></p>
        {item.state === "awaiting_approval" && <div className={styles.actions}>
          <button type="button" disabled={busy} onClick={() => void decide(item, "approve")}>Approve upgrade</button>
          <button type="button" disabled={busy} onClick={() => void decide(item, "reject")}>Reject</button>
        </div>}
        {item.state === "awaiting_repin" && <p className={styles.subtle}>Activation completed; awaiting host-reviewed runtime executable pins.</p>}
        {item.state === "indeterminate" && <p role="alert">Operator reconciliation required. No automatic retry will occur.</p>}
      </article>)}
      {!items.length && !notice && <p>No tested upgrades have been published for this workspace.</p>}
    </section>
    <footer className={styles.footer}>S10 staging and S9 tests happen inside the qualified workspace. Approvals are stored durably and executed by the operator, not by this page.</footer>
  </main>;
}
