import { performance } from 'node:perf_hooks';
import { graphPositions, boundedGraph } from '../src/lib/graph.ts';

// Diagnostic, not a CI pass/fail threshold: CPU and browser GPUs differ.
for (const count of [100, 500, 1500]) {
  const data = { revision:1, nodes:Array.from({length:count}, (_,i)=>({id:`n${i}`,parentId:i?'n0':null,label:'node',kind:'task',state:'pending'})), edges:[] };
  const start = performance.now();
  const projected=graphPositions(boundedGraph(data));
  console.log(JSON.stringify({nodes:count, projected:projected.size, cpu_layout_ms:Number((performance.now()-start).toFixed(2))}));
}
