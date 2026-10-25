"use client";

import { useCallback, useEffect, useState } from "react";
import styles from "./setup.module.css";

type Provider = { id: string; label: string; configured: boolean; status: string;
  source: "native_vault" | "environment" | null; editable: boolean; environmentVariable: string };
type Catalog = { providers: Provider[]; profile: string; storage: string; workspaceId: string };
type Workspace = { id: string; name: string };

function scopes(id: string): Record<string, string> { return { "X-Potbot-Workspace": id }; }
const DISPLAY: Record<string, string> = {
  configured: "Configured", missing: "Not configured", invalid_environment: "Invalid environment override",
  invalid_vault: "Invalid vault entry", vault_unavailable: "Vault unavailable",
};

export default function RemoteApisSetup() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspace, setWorkspace] = useState("");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("Loading operator workspaces…");
  const [show, setShow] = useState<Record<string, boolean>>({});

  const load = useCallback(async (id: string) => {
    setCatalog(null); setNotice("Loading configured API providers…");
    try {
      const response = await fetch("/api/ui/setup/apis", {
        credentials: "same-origin", cache: "no-store", headers: scopes(id),
      });
      if (!response.ok) throw new Error("API setup unavailable");
      const data = await response.json() as Catalog;
      if (data.workspaceId !== id || !Array.isArray(data.providers) || data.providers.length !== 5)
        throw new Error("Unexpected setup response");
      setCatalog(data); setNotice("");
    } catch { setNotice("Cannot load remote API credentials. Check operator login and backend setup access."); }
  }, []);

  useEffect(() => {
    let active = true;
    fetch("/api/ui/bootstrap", { credentials: "same-origin", cache: "no-store" })
      .then(async response => { if (!response.ok) throw new Error("login"); return response.json(); })
      .then(data => {
        if (!active || !Array.isArray(data.workspaces)) return;
        const items = (data.workspaces as Workspace[]).filter(w => typeof w.id === "string" && typeof w.name === "string");
        if (items.length) { setWorkspaces(items); setWorkspace(items[0].id); void load(items[0].id); }
        else setNotice("No operator-authorized workspace has been configured.");
      }).catch(() => { if (active) setNotice("Sign in to the operator workspace to manage API credentials."); });
    return () => { active = false; };
  }, [load]);

  async function act(provider: Provider, method: "PUT" | "DELETE" | "POST") {
    if (!workspace || busy) return;
    const key = secrets[provider.id] || "";
    if (method === "PUT" && (!key || !/^[!-~]{1,4096}$/.test(key))) {
      setNotice("Enter a valid API key containing 1–4096 visible ASCII characters."); return;
    }
    if (method === "DELETE" && !window.confirm(`Remove the saved ${provider.label} API credential from the native OS vault?`)) return;
    if (method === "PUT" && provider.configured && !window.confirm(`Rotate the saved ${provider.label} API credential?`)) return;
    if (method === "POST" && !window.confirm(`Test ${provider.label} using a read-only provider models request?`)) return;
    const origin = window.location.origin;
    const endpoint = `/api/ui/setup/apis/${provider.id}${method === "POST" ? "/test" : ""}`;
    setBusy(provider.id); setNotice(method === "POST" ? "Checking provider connectivity…" : "Saving credential change…");
    try {
      const response = await fetch(endpoint, { method, credentials: "same-origin",
        headers: { ...scopes(workspace), "Content-Type": "application/json", "Origin": origin },
        body: JSON.stringify(method === "PUT" ? { key, confirm: true, replace: provider.configured } : { confirm: true }),
      });
      if (!response.ok) throw new Error("Operation failed");
      const outcome = await response.json() as { verified?: boolean; status?: string };
      setSecrets(prev => ({ ...prev, [provider.id]: "" }));
      if (method === "POST") setNotice(outcome.verified ? `${provider.label}: metadata connection successful.` :
        `${provider.label}: connection result ${outcome.status || "unavailable"}.`);
      else { await load(workspace); setNotice(method === "PUT" ? "API credential saved in the native vault." : "Credential removed from the native vault."); }
    } catch { setNotice("Provider operation failed. No credential value was returned; reload status before retrying."); }
    finally { setBusy(""); }
  }

  return <main className={styles.shell}>
    <header className={styles.header}>
      <a href="/" className={styles.back}>← Return to Potbot</a>
      <span className={styles.kicker}>OPERATOR SETUP / EXTERNAL SERVICES</span>
      <h1>Remote API credentials</h1>
      <p>Configure the providers Potbot can use for hosted models. Keys are stored in the backend operator’s native OS credential vault—not in the browser, chat history or workspace files.</p>
    </header>
    <section className={styles.toolbar} aria-label="Credential profile and workspace">
      <label>Authorized workspace
        <select value={workspace} onChange={e => { setWorkspace(e.target.value); setSecrets({}); void load(e.target.value); }}>
          {workspaces.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
        </select>
      </label>
      <p>Credential profile: <strong>{catalog?.profile || "Unavailable"}</strong><small> Credentials are operator-profile scoped and shared across this operator's workspaces.</small></p>
    </section>
    <p role="status" className={styles.status}>{notice}</p>
    <section className={styles.cards} aria-label="Remote API providers">
      {catalog?.providers.map(provider => <article className={styles.card} key={provider.id}>
        <div className={styles.cardHead}><div><span className={styles.kicker}>REMOTE MODEL API</span>
          <h2>{provider.label}</h2></div><span className={styles.pill}>{DISPLAY[provider.status] || provider.status}</span></div>
        <p className={styles.subtle}>Source: {provider.source === "native_vault" ? "Native OS vault" : provider.source === "environment" ?
          provider.environmentVariable + " environment override" : "None"}</p>
        <label htmlFor={`api-${provider.id}`}>API key</label>
        <div className={styles.secretLine}>
          <input id={`api-${provider.id}`} type={show[provider.id] ? "text" : "password"} autoComplete="off" spellCheck={false}
            placeholder={provider.configured ? "Enter a replacement key" : "Enter a new API key"}
            disabled={!provider.editable || busy !== ""}
            value={secrets[provider.id] || ""}
            onChange={e => setSecrets(prev => ({ ...prev, [provider.id]: e.target.value }))} />
          <button type="button" disabled={busy !== ""} onClick={() => setShow(prev => ({ ...prev, [provider.id]: !prev[provider.id] }))}>
            {show[provider.id] ? "Hide" : "Show"}
          </button>
        </div>
        <div className={styles.actions}>
          <button type="button" disabled={!provider.editable || busy !== ""} onClick={() => void act(provider, "PUT")}>
            {provider.configured ? "Rotate key" : "Save key"}</button>
          <button type="button" disabled={!provider.configured || busy !== ""} onClick={() => void act(provider, "POST")}>Test connection</button>
          <button type="button" className={styles.danger} disabled={!provider.configured || !provider.editable || busy !== ""}
            onClick={() => void act(provider, "DELETE")}>Remove</button>
        </div>
      </article>)}
    </section>
    <footer className={styles.footer}>
      <p>A configured API key does not automatically permit model generation. Potbot's separate provider/model disclosure and budget policies still apply. Connection tests use metadata-only model listing requests.</p>
      <p>For headless services, set provider environment variables directly. Environment overrides are visible here but cannot be edited through this page.</p>
    </footer>
  </main>;
}
