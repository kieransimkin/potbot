"use client";

import { useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "motion/react";
import { nextWorkspace, orbPosition } from "@/lib/orbit";
import type { Workspace } from "@/lib/model";
import { ResourcePulseCore } from "./ResourcePulseCore";

export function WorkspaceOrbit
export function WorkspaceOrbit({ workspaces, selected, onSelect }: {
  workspaces: Workspace[]; selected: number; onSelect: (value: number) => void;
}) {
  const [revealed, setRevealed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reduceMotion = useReducedMotion();
  function choose(next: number) {
    if (timer.current) clearTimeout(timer.current);
    onSelect(next);
    // The active orb travels to centre; other orbs disappear out of view.
    timer.current = setTimeout(() => setRevealed(false), reduceMotion ? 0 : 450);
  }
  useEffect(() => {
    function onKeys(event: KeyboardEvent) {
      if (event.altKey && !event.ctrlKey && !event.metaKey && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
        const element = event.target as HTMLElement;
        if (element.matches("input,textarea,[contenteditable=true]")) return;
        event.preventDefault();
        choose(nextWorkspace(selected, event.key === "ArrowLeft" ? -1 : 1, workspaces.length));
      }
    }
    document.addEventListener("keydown", onKeys);
    return () => document.removeEventListener("keydown", onKeys);
  }, [selected, workspaces.length]);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  return <header className="orbit-shell" onMouseEnter={() => setRevealed(true)}
    onMouseLeave={() => { if (timer.current) clearTimeout(timer.current); timer.current = setTimeout(() => setRevealed(false), 650); }}>
    <button className="orbit-reveal" onClick={() => setRevealed(value => !value)}
      aria-expanded={revealed} aria-controls="workspace-orbit" title="Reveal workspaces (Alt + arrow keys)">
      <span aria-hidden="true">◌</span> Workspaces <span aria-hidden="true">⌄</span>
    </button>
    <nav id="workspace-orbit" className="orbit-track" aria-label="Switch workspace">
      <ResourcePulseCore />
      {workspaces.map((workspace, index) => {
        const p = orbPosition(index, selected, workspaces.length, revealed);
        const active = index === selected;
        return <motion.button key={workspace.id} type="button"
          className={`workspace-orb ${active ? "selected" : ""}`}
          style={{ "--orb-accent": workspace.accent, zIndex: p.z } as React.CSSProperties}
          animate={{ x: p.x, y: p.y, scale: p.scale, opacity: p.opacity }}
          initial={false}
          transition={reduceMotion ? { duration: 0 } : { type: "spring", stiffness: 250, damping: 29 }}
          onClick={() => choose(index)}
          disabled={!revealed && !active}
          aria-current={active ? "page" : undefined}
          aria-label={`${workspace.name}${active ? ", active workspace" : ""}`}
          title={`${workspace.name} — ${workspace.summary}`}>
          <span className="orb-inner" aria-hidden="true">{workspace.shortName}</span>
          {workspace.status === "working" && <span className="orb-status working" aria-hidden="true" />}
          {workspace.status === "attention" && <span className="orb-status attention" aria-hidden="true" />}
        </motion.button>;
      })}
    </nav>
    <div className="orbit-active-label">{workspaces[selected]?.name ?? ""}</div>
  </header>;
}
