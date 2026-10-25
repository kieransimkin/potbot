/** CPU-only diagnostic for search/ancestry and timeline ingestion, not GPU FPS. */
import { performance } from 'node:perf_hooks';
import { searchGraph, mergeGraphTimeline } from '../src/lib/graph-workbench.ts';
const nodes = Array.from({length: 1500}, (_, index) => ({
  id:`node-${index}`, parentId:index?`node-${Math.floor((index-1)/3)}`:null,
  label:`KeywordMoves evidence study ${index}`,
  state: index%3===0?'completed':index%3===1?'running':'pending',
  kind:index%4===0?'evidence':'hypothesis'
}));
const edges = nodes.slice(1).map(n=>({source:n.parentId,target:n.id,relation:'branch'}));
const graph={revision:10,nodes,edges};
let result; const start=performance.now();
for(let i=0;i<40;i++)result=searchGraph(graph,{query:'evidence study 9',state:'all',kind:'all'});
const searchMs=(performance.now()-start)/40;
let timeline=null;const run='d'.repeat(32);const page=Array.from({length:200},(_,i)=>({sequence:i+1,type:'node.completed'}));
const t=performance.now();timeline=mergeGraphTimeline(timeline,run,page);
const timelineMs=performance.now()-t;
console.log(JSON.stringify({kind:'cpu-only',nodeCount:graph.nodes.length,edgeCount:graph.edges.length,
  matched:result.matches,searchAverageMs:Number(searchMs.toFixed(3)),timelinePageMs:Number(timelineMs.toFixed(3)),
  notes:'No WebGL FPS, GPU memory or browser paint timings are represented.'},null,2));
