"use client";
import { useEffect, useState } from "react";

type LeaseState = { token: string; mode: "connecting" | "reserved" | "legacy" | "blocked"; status: string };

/** Operator browser reservations: tokens are ephemeral and never persisted. */
export function useBrowserLease(sessionId: string, workspaceId: string): LeaseState {
  const [state, setState] = useState<LeaseState>({token:"", mode:"connecting", status:"Requesting exclusive browser control"});
  useEffect(() => {
    if (!sessionId) { setState({token:"", mode:"blocked", status:"No controlled browser"}); return; }
    let closed = false;
    let token = "";
    setState({token:"", mode:"connecting", status:"Requesting exclusive browser control"});
    const endpoint = `/api/ui/xpra/apps/${sessionId}/lease`;
    const call = async (payload: Record<string,string>, keepalive = false) => fetch(endpoint, {
      method:"POST", headers:{"Content-Type":"application/json", "x-potbot-workspace":workspaceId},
      credentials:"same-origin", cache:"no-store", keepalive, body:JSON.stringify(payload),
    });
    void (async () => {
      try {
        const r = await call({action:"acquire"});
        if (!r.ok) throw Error(`Browser reservation unavailable, reserved or fenced (${r.status})`);
        const data = await r.json() as {token?:string; legacy?:boolean};
        if (data.legacy === true) {
          if (!closed) setState({token:"", mode:"legacy", status:"Legacy unreserved operator control — enable browserLeases in the Xpra policy for multi-agent isolation"});
          return;
        }
        if (!data.token) throw Error("Missing browser reservation token");
        token = data.token;
        if (closed) { void call({action:"release", token}, true); return; }
        setState({token, mode:"reserved", status:"Exclusive operator browser reservation active"});
      } catch (err) { if (!closed) setState({token:"", mode:"blocked", status:err instanceof Error ? err.message : "Reservation failed"}); }
    })();
    const renewal = setInterval(() => {
      if (!token || closed) return;
      void (async () => {
        try {
          const r = await call({action:"renew", token});
          if (!r.ok) throw Error();
        } catch { setState({token:"", mode:"blocked", status:"Browser reservation lost; do not retry an uncertain action"}); token=""; }
      })();
    }, 20000);
    return () => {
      closed = true; clearInterval(renewal);
      if (token) void call({action:"release", token}, true);
    };
  }, [sessionId, workspaceId]);
  return state;
}
