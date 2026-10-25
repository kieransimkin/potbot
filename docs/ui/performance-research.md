# UI-1/UI-2 rendering choices and performance research (25 October 2026)

## Choice summary

| Concern | Selected | Why | Deferred alternatives |
| --- | --- | --- | --- |
| Workspace orbs | CSS transform/opacity with Motion springs | Compositor-friendly; 4–20 orbs do not require WebGL, texture loading or layout thrash | Pure CSS transitions if Motion weight proves excessive |
| Docking | Dockview React 8.x | Built-in snapping, all-edge docking, floating groups, resizing, layout serialization and keyboard accessibility | Custom drag/split implementation is higher maintenance |
| Panel fullscreen | Single app-owned overlay | Dockview explicitly does not maximize floating groups; overlay can maximize only the graph, never the menu | Dockview native group maximize for docked graph |
| Graph preview | Three.js InstancedMesh + batched lines | Node draw calls scale with materials, not node count; deterministic radial DAG avoids live physics per frame | react-force-graph-3d for small/medium dynamic physics; custom worker-force layout for large graphs |
| Graph animation | Demand-driven render | GPU remains idle when camera/data is still | Continuous 60fps only during an actual simulation/transition |
| Graph visibility | Unmount GL when minimized/fullscreen switch | Avoids offscreen GPU load and duplicate contexts | Retain CPU-side graph state and camera parameters later |
| Chat | Plain React DOM | Readability, accessibility; virtualize only if very long | react-virtuoso after message-history endpoint |

### Research references

- [Dockview floating groups](https://dockview.dev/docs/core/groups/floatingGroups/) — dockable/floating groups; floating groups **cannot be maximized**.
- [Dockview adding panels](https://dockview.dev/docs/core/panels/add/) — `floating` initial positioning, relative docking, optional minimum sizes.
- [Dockview state saving](https://dockview.dev/docs/core/state/save/) — `toJSON`/`fromJSON` and aggregate layout-change subscription.
- [Motion transform/layout performance](https://motion.dev/docs/react-layout-animations) — layout transforms, shared `layoutId` animation.
- [Three.js InstancedMesh](https://threejs.org/docs/pages/InstancedMesh.html) — fewer draw calls when repeating the same geometry/material.
- [React-force-graph 3D](https://github.com/vasturiano/react-force-graph) — useful mature force-simulation alternative, but default continuous ticking would be unnecessary when inspecting a completed DAG.
- [Next.js server/client components](https://nextjs.org/docs/app/getting-started/server-and-client-components) — isolate browser-only GL, dock and Motion behind client components.

## Runtime policies

- Keep the 3D renderer behind `next/dynamic(...,{ssr:false})` in a client component.
- Set WebGL antialiasing `false` and pixel ratio at most 1.5 to cap expensive
  framebuffer allocation; use node/edge instancing and cached coordinates.
- Avoid frequent React `setState` updates for every model delta; UI-3 should
  coalesce graph events once per animation frame, using cursors and backpressure.
- Pause rendering when not visible, dispose GPU resources on graph close.
- Use stable node/edge identifiers and deterministic coordinates. A force
  simulation, if enabled, should run in a Worker and publish settled positions.
- Use `prefers-reduced-motion` to turn off orbit and panel transitions.
- Debounce layout writes; don't persist a Dockview JSON document on every mouse move.
- Target an interactive response under 100ms for a regular workspace switch,
  60fps when actively orbiting, and zero continuous GPU frames when idle.
  These are **targets, not measured performance claims**.

## Initial measurement

A Node 22 `bench-graph.mjs` diagnostic with the pure `graphPositions` algorithm
completed 100, 500 and 1500 node layouts in a few milliseconds on the patch
build container. That is **CPU-only**, not a browser FPS or GPU benchmark. On the
actual target devices, profile frame time, draw calls, WebGL memory, DOM events,
layout shifts, React commits and input-to-render latency at 100/500/1500/5000+
nodes before expanding the hard cap.

## Research-driven next work

For UI-3 compare our on-demand instanced renderer against
`react-force-graph-3d` on the **same graph snapshots**, including CPU, GPU,
first-interaction time, sustained orbit FPS, p95 node-picking time, WebGL VRAM,
initial parse/render and stability after switching workspaces 100 times.
Use actual event payloads from Potbot's graph-projection API once available.
