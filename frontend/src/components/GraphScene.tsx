"use client";

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { boundedGraph, graphPositions, graphStateColour } from "@/lib/graph";
import type { GraphNode, GraphProjection } from "@/lib/model";

export type GraphRenderMetrics = {
  renderer: "WebGL";
  nodeInstances: number;
  edgeSegments: number;
  draws: number;
  buildCpuMs: number;
  renderCpuMs: number;
  pixelRatio: number;
  estimatedGraphBytes: number;
};

type SceneController = { select: (nodeId: string | null) => void };

/** Two draw calls (nodes + segments), stable geometry and demand-only frames.
 * Selection changes update instance data and camera without rebuilding the scene.
 */
export default function GraphScene({ graph, onInspect, selectedNodeId = null, onMetrics }: {
  graph: GraphProjection;
  onInspect: (node: GraphNode) => void;
  selectedNodeId?: string | null;
  onMetrics?: (metrics: GraphRenderMetrics) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const inspect = useRef(onInspect);
  const report = useRef(onMetrics);
  const wantedSelection = useRef(selectedNodeId);
  const sceneController = useRef<SceneController | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => { inspect.current = onInspect; }, [onInspect]);
  useEffect(() => { report.current = onMetrics; }, [onMetrics]);
  useEffect(() => { wantedSelection.current = selectedNodeId; sceneController.current?.select(selectedNodeId); }, [selectedNodeId]);
  useEffect(() => {
    const root = container.current;
    if (!root) return;
    let renderer: THREE.WebGLRenderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: false, alpha: true, powerPreference: "high-performance" }); }
    catch { setFailed(true); return; }
    const buildStart = performance.now();
    const data = boundedGraph(graph);
    const positions = graphPositions(data);
    const lookup = new Map(data.nodes.map((node, index) => [node.id, index]));
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(53, 1, 1, 5000);
    camera.position.set(175, 115, 220);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = false; // No endless physics/damping animation loop.
    controls.minDistance = 35;
    controls.maxDistance = 1800;
    const pixelRatio = Math.min(Math.max(window.devicePixelRatio || 1, 1), 1.5);
    renderer.setPixelRatio(pixelRatio);
    const geometry = new THREE.IcosahedronGeometry(5, 1);
    const material = new THREE.MeshBasicMaterial({ vertexColors: false });
    const nodes = new THREE.InstancedMesh(geometry, material, data.nodes.length);
    nodes.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    const transform = new THREE.Object3D();
    function setNode(index: number, highlighted: boolean) {
      const node = data.nodes[index];
      const [x, y, z] = positions.get(node.id) ?? [0, 0, 0];
      transform.position.set(x, y, z);
      const scale = (node.kind === "objective" ? 2 : node.kind === "evidence" ? .75 : 1) *
        (highlighted ? 1.55 : 1);
      transform.scale.setScalar(scale);
      transform.updateMatrix();
      nodes.setMatrixAt(index, transform.matrix);
      nodes.setColorAt(index, new THREE.Color(highlighted ? 0xffffff : graphStateColour(node.state)));
    }
    data.nodes.forEach((_, index) => setNode(index, false));
    nodes.instanceMatrix.needsUpdate = true;
    if (nodes.instanceColor) nodes.instanceColor.needsUpdate = true;
    scene.add(nodes);
    const vertices: number[] = [];
    data.edges.forEach(edge => {
      const a = positions.get(edge.source), b = positions.get(edge.target);
      if (a && b) vertices.push(...a, ...b);
    });
    const lineGeometry = new THREE.BufferGeometry();
    lineGeometry.setAttribute("position", new THREE.Float32BufferAttribute(vertices, 3));
    const lines = new THREE.LineSegments(lineGeometry,
      new THREE.LineBasicMaterial({ color: 0x64899b, transparent: true, opacity: .5 }));
    scene.add(lines);
    const raycaster = new THREE.Raycaster();
    const mouse = new THREE.Vector2();
    const metrics: GraphRenderMetrics = {
      renderer: "WebGL", nodeInstances: data.nodes.length, edgeSegments: vertices.length / 6,
      draws: 0, buildCpuMs: 0, renderCpuMs: 0, pixelRatio,
      // Approximate graph instance transforms, instance colors and line vertices;
      // deliberately NOT a measured GPU allocation or full texture/driver usage.
      estimatedGraphBytes: data.nodes.length * (64 + 12) + vertices.length * 4,
    };
    let raf = 0;
    let visible = true;
    let ready = true;
    let lastReport = -Infinity;
    const draw = () => {
      if (!visible || !ready || raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        if (!ready || !visible) return;
        const started = performance.now();
        renderer.render(scene, camera);
        metrics.draws += 1;
        metrics.renderCpuMs = performance.now() - started;
        if (performance.now() - lastReport >= 1200) {
          lastReport = performance.now();
          report.current?.({ ...metrics });
        }
      });
    };
    const resize = () => {
      const width = Math.max(root.clientWidth, 1), height = Math.max(root.clientHeight, 1);
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      draw();
    };
    let highlighted: string | null = null;
    const select = (nodeId: string | null) => {
      const next = nodeId && lookup.has(nodeId) ? nodeId : null;
      if (highlighted === next) return;
      if (highlighted) setNode(lookup.get(highlighted)!, false);
      if (next) setNode(lookup.get(next)!, true);
      nodes.instanceMatrix.needsUpdate = true;
      if (nodes.instanceColor) nodes.instanceColor.needsUpdate = true;
      highlighted = next;
      if (next) {
        const point = positions.get(next);
        if (point) {
          const target = new THREE.Vector3(...point);
          camera.position.add(target.clone().sub(controls.target));
          controls.target.copy(target);
          controls.update();
        }
      }
      draw();
    };
    sceneController.current = { select };
    root.appendChild(renderer.domElement);
    renderer.domElement.className = "graph-canvas";
    renderer.domElement.setAttribute("aria-label", "Interactive three-dimensional reasoning nodes. Use the Nodes list for keyboard selection.");
    const observer = new ResizeObserver(resize);
    observer.observe(root);
    const intersection = new IntersectionObserver(entries => {
      visible = entries.some(entry => entry.isIntersecting);
      draw();
    });
    intersection.observe(root);
    controls.addEventListener("change", draw);
    let pointerOrigin: { x: number; y: number } | null = null;
    const pointerDown = (event: PointerEvent) => {
      pointerOrigin = { x: event.clientX, y: event.clientY };
    };
    const click = (event: PointerEvent) => {
      const start = pointerOrigin;
      pointerOrigin = null;
      if (!start || Math.hypot(event.clientX - start.x, event.clientY - start.y) > 5) return;
      const rect = renderer.domElement.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      mouse.set(((event.clientX - rect.left) / rect.width) * 2 - 1,
        -((event.clientY - rect.top) / rect.height) * 2 + 1);
      raycaster.setFromCamera(mouse, camera);
      const hit = raycaster.intersectObject(nodes, false)[0];
      if (hit && hit.instanceId !== undefined) inspect.current(data.nodes[hit.instanceId]);
    };
    const contextLost = (event: Event) => { event.preventDefault(); setFailed(true); };
    renderer.domElement.addEventListener("pointerdown", pointerDown);
    renderer.domElement.addEventListener("pointerup", click);
    renderer.domElement.addEventListener("webglcontextlost", contextLost);
    metrics.buildCpuMs = performance.now() - buildStart;
    select(wantedSelection.current);
    resize();
    return () => {
      ready = false;
      sceneController.current = null;
      cancelAnimationFrame(raf);
      observer.disconnect(); intersection.disconnect();
      controls.removeEventListener("change", draw); controls.dispose();
      renderer.domElement.removeEventListener("pointerdown", pointerDown);
      renderer.domElement.removeEventListener("pointerup", click);
      renderer.domElement.removeEventListener("webglcontextlost", contextLost);
      nodes.geometry.dispose(); material.dispose(); lineGeometry.dispose();
      (lines.material as THREE.Material).dispose();
      renderer.dispose(); renderer.domElement.remove();
    };
  }, [graph, failed]);
  return failed ? <div className="graph-fallback graph-fallback-list" role="region" aria-label="WebGL not available">
    <strong>WebGL unavailable</strong><p>Switch to the accessible List view to inspect every recorded node.</p>
    <ul>{graph.nodes.slice(0, 80).map(node => <li key={node.id}><button type="button" onClick={() => onInspect(node)}>
      {node.label} <small>{node.state}</small>
    </button></li>)}</ul>
  </div> : <div ref={container} className="graph-render-surface" aria-label="Reasoning graph 3D viewport" />;
}
