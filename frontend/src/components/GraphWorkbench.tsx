"use client";

import dynamic from "next/dynamic";
import { useDeferredValue, useMemo, useState } from "react";
import { graphCounts, graphEventLabel, searchGraph } from "@/lib/graph-workbench";
import type { GraphRenderMetrics } from "./GraphScene";
import type { GraphNode } from "@/lib/model";
import { DEMO_GRAPH } from "@/lib/model";
import { useWorkspace } from "./WorkspaceContext";

const GraphScene = dynamic(() => import("./GraphScene"), {
  ssr: false, loading: () => <div className="graph-fallback">Loading 3D renderer…</div>,
});

const STATES: (GraphNode["state"] | "all")[] = ["all", "pending", "running", "completed", "rejected", "indeterminate"];
const KINDS: (GraphNode["kind"] | "all")[] = ["all", "objective", "hypothesis", "task", "evidence", "result"];
const LIST_PAGE = 100;

/** Shared inspector: floating, docked and fullscreen views use the same filters.
 * No hidden reasoning details or raw event payloads are displayed.
 */
export function GraphWorkbench({ fullscreen = false }: { fullscreen?: boolean }) {
  const { demo, graph, graphState, graphTimeline, graphView: view,
    updateGraphView, runId } = useWorkspace();
  const [metrics, setMetrics] = useState<GraphRenderMetrics | null>(null);
  const [visibleRows, setVisibleRows] = useState(LIST_PAGE);
  const data = demo ? DEMO_GRAPH : graph;
  const deferredQuery = useDeferredValue(view.query);
  const result = useMemo(() => data ? searchGraph(data, {
    query: deferredQuery, state: view.state, kind: view.kind,
  }) : null, [data, deferredQuery, view.state, view.kind]);
  const selected = data?.nodes.find(node => node.id === view.selectedNodeId) ?? null;
  const visibleSelection = selected && result?.graph.nodes.some(node => node.id === selected.id);
  const counts = useMemo(() => data ? graphCounts(data.nodes) : null, [data]);
  const chooseNode = (node: GraphNode) => updateGraphView({ selectedNodeId: node.id });
  const showList = view.display === "list" || view.showNodes;
  const entries = result?.graph.nodes.slice(0, visibleRows) ?? [];

  return <div className={`graph-workbench ${fullscreen ? "graph-workbench-full" : ""}`}>
    <div className="graph-filter-bar" role="group" aria-label="Graph search and filters">
      <label className="graph-search-label"><span className="sr-only">Search reasoning nodes</span>
        <input type="search" value={view.query} maxLength={120} placeholder="Search nodes…"
          onChange={event => { setVisibleRows(LIST_PAGE); updateGraphView({ query: event.target.value }); }}
          aria-label="Search reasoning nodes" />
      </label>
      <label><span className="sr-only">Node status</span>
        <select value={view.state} aria-label="Filter by node status"
          onChange={event => { setVisibleRows(LIST_PAGE); updateGraphView({ state: event.target.value as typeof view.state }); }}>
          {STATES.map(state => <option value={state} key={state}>{state === "all" ? "All states" : state}</option>)}
        </select></label>
      <label><span className="sr-only">Node kind</span>
        <select value={view.kind} aria-label="Filter by node type"
          onChange={event => { setVisibleRows(LIST_PAGE); updateGraphView({ kind: event.target.value as typeof view.kind }); }}>
          {KINDS.map(kind => <option value={kind} key={kind}>{kind === "all" ? "All kinds" : kind}</option>)}
        </select></label>
      <div className="graph-view-toggles">
        <button type="button" aria-pressed={view.display === "list"}
          title={view.display === "list" ? "Switch to 3D" : "Accessible list-only view"}
          onClick={() => updateGraphView({ display: view.display === "list" ? "3d" : "list" })}>
          {view.display === "list" ? "3D" : "List"}</button>
        {view.display === "3d" && <button type="button" aria-pressed={view.showNodes}
          onClick={() => { setVisibleRows(LIST_PAGE); updateGraphView({ showNodes: !view.showNodes }); }}>
          Nodes</button>}
        <button type="button" aria-pressed={view.showTimeline}
          onClick={() => updateGraphView({ showTimeline: !view.showTimeline })}>Events</button>
        <button type="button" aria-pressed={view.showDiagnostics}
          onClick={() => updateGraphView({ showDiagnostics: !view.showDiagnostics })}>Perf</button>
      </div>
    </div>
    {counts && <div className="graph-overview" aria-label="Graph state counts">
      <span>{result?.matches ?? 0} matches / {result?.totalNodes ?? 0} nodes</span>
      {result && result.contextNodes > 0 && <span>+{result.contextNodes} ancestor nodes</span>}
      <span>{counts.completed} completed · {counts.running} running · {counts.indeterminate} uncertain</span>
      {demo && <span className="demo-indicator">SYNTHETIC DEMO</span>}
    </div>}
    <div className="graph-visual-area">
      {!data ? <div className="graph-unavailable"><strong>No active reasoning graph</strong>
        <p>Start a Potbot inference to inspect recorded graph nodes.</p><small>{graphState}</small></div> :
        !result || result.graph.nodes.length === 0 ? <div className="graph-unavailable"><strong>No matching nodes</strong>
          <p>Adjust the search or filters. The recorded graph has not been modified.</p>
          <button type="button" onClick={() => updateGraphView({ query: "", state: "all", kind: "all" })}>Clear filters</button></div> : <>
          {view.display === "3d" && <GraphScene graph={result.graph} onInspect={chooseNode}
            selectedNodeId={visibleSelection ? view.selectedNodeId : null} onMetrics={setMetrics} />}
          {showList && <section className={`graph-node-list ${view.display === "list" ? "graph-node-list-full" : ""}`}
            aria-label="Filtered reasoning node explorer">
            <div className="graph-side-heading"><strong>Recorded reasoning nodes</strong>
              {view.display !== "list" && <button type="button" onClick={() => updateGraphView({ showNodes: false })}
                aria-label="Close node explorer">×</button>}</div>
            <ul>{entries.map(node => <li key={node.id}><button type="button"
              className={node.id === selected?.id ? "node-selected" : ""}
              onClick={() => chooseNode(node)} aria-current={node.id === selected?.id ? "true" : undefined}>
              <span>{node.label}</span><small>{node.kind} · {node.state}</small></button></li>)}</ul>
            {result.graph.nodes.length > visibleRows && <button type="button" className="graph-load-more"
              onClick={() => setVisibleRows(v => Math.min(v + LIST_PAGE, result.graph.nodes.length))}>
              Show next {Math.min(LIST_PAGE, result.graph.nodes.length - visibleRows)} nodes
            </button>}
          </section>}
        </>}
      {selected && <aside className="graph-node-detail" aria-label="Selected graph node">
        <div className="graph-side-heading"><strong>Node inspector</strong>
          <button type="button" aria-label="Clear selected graph node"
            onClick={() => updateGraphView({ selectedNodeId: null })}>×</button></div>
        <p>{selected.label}</p>
        <dl><div><dt>State</dt><dd>{selected.state}</dd></div>
          <div><dt>Kind</dt><dd>{selected.kind}</dd></div>
          <div><dt>Node</dt><dd title={selected.id}>{selected.id.slice(0, 18)}</dd></div>
          <div><dt>Parent</dt><dd>{selected.parentId?.slice(0, 18) ?? "Root"}</dd></div>
        </dl>
        {!visibleSelection && <small>Selection is hidden by the current filters.</small>}
        <small>Recorded summary only. Claim tokens and private reasoning are not exposed.</small>
      </aside>}
      {view.showTimeline && <aside className="graph-timeline" aria-label="Reasoning event timeline">
        <div className="graph-side-heading"><strong>Event timeline</strong>
          <button type="button" onClick={() => updateGraphView({ showTimeline: false })} aria-label="Close event timeline">×</button></div>
        {demo ? <p className="subtle">Demo graph has no real event history.</p> : !runId ?
          <p className="subtle">Send a message to start a reasoning run.</p> : !graphTimeline?.events.length ?
          <p className="subtle">No recorded events have been received. {graphState}</p> : <>
            {graphTimeline.gapDetected && <p className="graph-history-gap" role="status">
              Source event history contains a gap. Current graph was refreshed from the checkpoint.</p>}
            <ol>{[...graphTimeline.events].reverse().map(item => <li key={`${runId}:${item.sequence}`}>
              <span className="graph-event-sequence">#{item.sequence}</span>
              <span>{graphEventLabel(item.type)}</span></li>)}</ol>
            <small>Sequence and type only. The backend does not provide event timestamps to this view.</small>
          </>}
      </aside>}
      {view.showDiagnostics && <aside className="graph-perf" aria-label="Graph rendering diagnostics">
        <div className="graph-side-heading"><strong>Rendering diagnostics</strong>
          <button type="button" onClick={() => updateGraphView({ showDiagnostics: false })} aria-label="Close diagnostics">×</button></div>
        {view.display === "list" ? <p>Accessible list mode: WebGL not mounted.</p> : !metrics ?
          <p>Waiting for first 3D draw.</p> : <dl>
            <div><dt>Renderer</dt><dd>{metrics.renderer}</dd></div>
            <div><dt>Draw calls*</dt><dd>{metrics.draws}</dd></div>
            <div><dt>Instances</dt><dd>{metrics.nodeInstances}</dd></div>
            <div><dt>Segments</dt><dd>{metrics.edgeSegments}</dd></div>
            <div><dt>Build CPU</dt><dd>{metrics.buildCpuMs.toFixed(1)} ms</dd></div>
            <div><dt>Render CPU</dt><dd>{metrics.renderCpuMs.toFixed(1)} ms</dd></div>
            <div><dt>Pixel ratio</dt><dd>{metrics.pixelRatio.toFixed(1)}×</dd></div>
            <div><dt>Graph buffers (approx.)</dt><dd>{Math.round(metrics.estimatedGraphBytes / 1024)} KiB</dd></div>
          </dl>}
        <small>*On-demand frames in this WebGL scene; CPU timings exclude GPU completion. Not FPS or peak VRAM.</small>
      </aside>}
    </div>
    <footer className="graph-workbench-status"><span>{demo ? "Preview" : `Run ${runId?.slice(0, 10) ?? "not started"} · ${graphState}`}</span>
      <span>{data ? `Revision ${data.revision} · ${result?.graph.edges.length ?? 0} visible edges` : "No checkpoint"}</span></footer>
  </div>;
}
