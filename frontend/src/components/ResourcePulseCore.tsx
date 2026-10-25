"use client";

import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { projectUtilisation } from "@/lib/utilisation";
import type { SystemUtilisation } from "@/lib/utilisation";
import styles from "./ResourcePulseCore.module.css";

/** SVG sprite stays vector-sharp. CSS animation does no JS frame work. */
export function ResourcePulseCore() {
  const [telemetry, setTelemetry] = useState<SystemUtilisation | null>(null);
  const [flash, setFlash] = useState(0);
  const previousFast = useRef(0);
  useEffect(() => {
    let alive = true;
    let current: AbortController | null = null;
    async function poll() {
      current?.abort();
      current = new AbortController();
      try {
        const response = await fetch("/api/ui/utilisation", {
          cache: "no-store", signal: current.signal,
        });
        if (!response.ok) throw new Error("telemetry unavailable");
        const raw = await response.json();
        const next = projectUtilisation(raw);
        if (!next) throw new Error("invalid telemetry projection");
        if (!alive) return;
        // A new fast-load surge, not every polling tick, causes a brief flash.
        const surged = next.available && next.system.load2s > 0.35 &&
          next.system.load2s - next.system.load3m >= 0.16 &&
          next.system.load2s - previousFast.current > 0.08;
        previousFast.current = next.system.load2s;
        if (surged) setFlash(value => value + 1);
        setTelemetry(next);
      } catch {
        if (alive && !current?.signal.aborted) setTelemetry(null);
      }
    }
    void poll();
    const interval = window.setInterval(() => void poll(), 2000);
    return () => { alive = false; current?.abort(); window.clearInterval(interval); };
  }, []);
  const available = telemetry?.available === true;
  const slow = available ? telemetry.system.load3m : 0;
  const fast = available ? telemetry.system.load2s : 0;
  const pressure = available ? Math.max(
    telemetry.system.pressure.io.avg10 ?? 0,
    telemetry.system.pressure.memory.avg10 ?? 0,
  ) : 0;
  const agents = available ? telemetry.agents.slice().sort((a, b) => b.heat - a.heat).slice(0, 8) : [];
  const style = { "--monitor-load": slow, "--monitor-fast": fast } as CSSProperties;
  const title = available ? `System utilisation: ${Math.round(slow * 100)}% three-minute average; ${Math.round(fast * 100)}% short-term; memory/IO pressure ${pressure.toFixed(1)}%` :
    "Operator system telemetry unavailable";
  return <div className={styles.sprite} style={style} aria-label={title} role="img" title={title}>
    <div className={`${styles.outerGlow} ${available ? "" : styles.offline}`} />
    <svg className={styles.rings} viewBox="0 0 100 100" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id="potbot-load-arc" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#69ebfe" /><stop offset=".68" stopColor="#5ecbe8" />
          <stop offset="1" stopColor="#ffac60" />
        </linearGradient>
      </defs>
      <circle cx="50" cy="50" r="43" fill="none" stroke="#50d9fc" strokeOpacity=".18" strokeWidth="1" strokeDasharray="1 2.4" />
      <circle cx="50" cy="50" r="35" fill="none" stroke="#7ae8ff" strokeOpacity=".24" strokeWidth="1" />
      <circle cx="50" cy="50" r="39" fill="none" stroke="url(#potbot-load-arc)" strokeWidth="3" strokeLinecap="round"
        strokeDasharray={`${Math.round(slow * 245)} 245`} transform="rotate(-90 50 50)" />
      <path d="M21 52 Q31 24 41 52 T60 52 T79 44" fill="none" stroke="#65f0fe" strokeWidth="2" opacity={available ? .8 : .2} />
      <circle cx="50" cy="50" r="28" fill="none" stroke="#6aecff" strokeOpacity=".2" strokeDasharray="2 4" />
    </svg>
    <span className={styles.value}>{available ? `${Math.round(slow * 100)}%` : "—"}</span>
    {pressure >= 12 && <span className={styles.bottleneck} title="Elevated memory or I/O pressure">!</span>}
    {flash > 0 && <span className={styles.flash} key={flash} aria-hidden="true" />}
    <div className={styles.agents} aria-label="Agent CPU usage contribution">
      {agents.map(agent => <span key={agent.agentId} className={styles.agent}
        role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(agent.heat * 100)}
        aria-label={`Agent ${agent.agentId} CPU contribution`}
        title={`Agent ${agent.agentId}: ${Math.round(agent.heat * 100)}% of host busy CPU`}>
        <span className={styles.agentFill} style={{ width: `${Math.max(1, agent.heat * 100)}%` }} />
      </span>)}
    </div>
  </div>;
}
