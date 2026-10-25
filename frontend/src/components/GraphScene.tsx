"use client";

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { boundedGraph, graphPositions, graphStateColour } from "@/lib/graph";
import type { GraphNode, GraphProjection } from "@/lib/model";

/** Instanced geometry + one line batch. Only render when data, controls or size changes. */
export default function GraphScene({ graph, onInspect }: { graph: GraphProjection; onInspect: (node: GraphNode) => void }) {
  const container = useRef<HTMLDivElement>(null);
  const inspect = useRef(onInspect);
  const [failed, setFailed] = useState(false);
  useEffect(() => { inspect.current = onInspect; }, [onInspect]);
  useEffect(() => {
    const root = container.current;
    if (!root) return;
    let renderer: THREE.WebGLRenderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: false, alpha: true, powerPreference: "high-performance" }); }
    catch { setFailed(true); return; }
    const data = boundedGraph(graph);
    const positions = graphPositions(data);
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(53, 1, 1, 5000);
    camera.position.set(175, 115, 220);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = false; // Demand rendering; no continuous RAF loop.
    controls.minDistance = 35;
    controls.maxDistance = 1800;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    const geometry = new THREE.IcosahedronGeometry(5, 1);
    const material = new THREE.MeshBasicMaterial({ vertexColors: false });
    const nodes = new THREE.InstancedMesh(geometry, material, data.nodes.length);
    nodes.instanceMatrix.setUsage(THREE.StaticDrawUsage);
    const transform = new THREE.Object3D();
    data.nodes.forEach((node, index) => {
      const [x, y, z] = positions.get(node.id) ?? [0, 0, 0];
      transform.position.set(x, y, z);
      const scale = node.kind === "objective" ? 2 : node.kind === "evidence" ? .75 : 1;
      transform.scale.setScalar(scale);
      transform.updateMatrix();
      nodes.setMatrixAt(index, transform.matrix);
      nodes.setColorAt(index, new THREE.Color(graphStateColour(node.state)));
    });
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
    const lines = new THREE.LineSegments(lineGeometry, new THREE.LineBasicMaterial({ color: 0x64899b, transparent: true, opacity: .5 }));
    scene.add(lines);
    const raycaster = new THREE.Raycaster();
    const mouse = new THREE.Vector2();
    let raf = 0;
    let visible = true;
    let ready = true;
    const draw = () => {
      if (!visible || !ready || raf) return;
      raf = requestAnimationFrame(() => { raf = 0; if (visible && ready) renderer.render(scene, camera); });
    };
    const resize = () => {
      const w = Math.max(root.clientWidth, 1), h = Math.max(root.clientHeight, 1);
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      draw();
    };
    root.appendChild(renderer.domElement);
    renderer.domElement.className = "graph-canvas";
    const observer = new ResizeObserver(resize);
    observer.observe(root);
    const intersection = new IntersectionObserver(entries => {
      visible = entries.some(entry => entry.isIntersecting);
      draw();
    });
    intersection.observe(root);
    controls.addEventListener("change", draw);
    const click = (event: PointerEvent) => {
      const rect = renderer.domElement.getBoundingClientRect();
      mouse.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
      raycaster.setFromCamera(mouse, camera);
      const found = raycaster.intersectObject(nodes)[0];
      if (found && found.instanceId !== undefined) inspect.current(data.nodes[found.instanceId]);
    };
    renderer.domElement.addEventListener("click", click);
    resize();
    return () => {
      ready = false;
      cancelAnimationFrame(raf);
      observer.disconnect(); intersection.disconnect();
      controls.removeEventListener("change", draw); controls.dispose();
      renderer.domElement.removeEventListener("click", click);
      nodes.geometry.dispose(); material.dispose(); lineGeometry.dispose();
      (lines.material as THREE.Material).dispose();
      renderer.dispose(); renderer.domElement.remove();
    };
  }, [graph]);
  return failed ? <div className="graph-fallback graph-fallback-list" role="region" aria-label="Accessible reasoning graph nodes">
    <strong>WebGL unavailable — node list</strong>
    <ul>{graph.nodes.slice(0, 120).map(node => <li key={node.id}><button type="button" onClick={() => onInspect(node)}>
      {node.label} <small>{node.state}</small>
    </button></li>)}</ul>
    {graph.nodes.length > 120 && <p>Showing first 120 of {graph.nodes.length} nodes.</p>}
  </div> : <div ref={container} className="graph-render-surface" aria-label="Interactive three-dimensional reasoning graph" />;
}
