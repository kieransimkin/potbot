import { test } from 'node:test';
import assert from 'node:assert/strict';
import { graphCounts, graphEventLabel, mergeGraphTimeline, searchGraph, sanitizedGraphView } from '../src/lib/graph-workbench.ts';
const graph={revision:3,nodes:[
  {id:'root',parentId:null,label:'Research',state:'running',kind:'objective'},
  {id:'a',parentId:'root',label:'Check evidence',state:'completed',kind:'evidence'},
  {id:'b',parentId:'a',label:'Score KeywordMoves',state:'completed',kind:'result'},
  {id:'c',parentId:'root',label:'Failing branch',state:'rejected',kind:'hypothesis'},
  {id:'d',parentId:'root',label:'Improve coding',state:'pending',kind:'task'},
 ],edges:[{source:'root',target:'a',relation:'branch'},{source:'a',target:'b',relation:'branch'},
  {source:'root',target:'c',relation:'branch'},{source:'root',target:'d',relation:'branch'}]};
test('search retains ancestors only as visual context and never invents edges',()=>{
 const result=searchGraph(graph,{query:'keyword',state:'all',kind:'all'});
 assert.equal(result.matches,1); assert.equal(result.contextNodes,2);
 assert.deepEqual(result.graph.nodes.map(n=>n.id),['root','a','b']);
 assert.deepEqual(result.graph.edges.map(e=>e.target),['a','b']);
 assert.equal(result.graph.revision,3);
 assert.equal(searchGraph(graph,{query:'',state:'completed',kind:'evidence'}).graph.nodes.length,1);
 assert.equal(searchGraph(graph,{query:'',state:'completed',kind:'evidence'}).graph.edges.length,0);
});
test('view parsing refuses unsupported filters and oversized persisted selection',()=>{
 assert.deepEqual(sanitizedGraphView({display:'list',state:'forbidden',kind:'task',query:'a'.repeat(200)}),{
  query:'a'.repeat(120),state:'all',kind:'task',display:'list',showNodes:false,
  showTimeline:false,showDiagnostics:false,selectedNodeId:null
 });
 assert.deepEqual(graphCounts(graph.nodes),{pending:1,running:1,completed:2,rejected:1,indeterminate:0});
});
test('timeline merges pages, truncates bounded history and retains gaps',()=>{
 const run='a'.repeat(32);
 const first=mergeGraphTimeline(null,run,[{sequence:1,type:'node.add'},{sequence:2,type:'node.done'}]);
 assert.equal(first.cursor,2);assert.equal(first.gapDetected,false);
 const next=mergeGraphTimeline(first,run,[{sequence:3,type:'node.more'}]);
 assert.deepEqual(next.events.map(x=>x.sequence),[1,2,3]);
 const gap=mergeGraphTimeline(next,run,[{sequence:6,type:'node.recovered'}]);
 assert.equal(gap.gapDetected,true);assert.deepEqual(gap.events.map(x=>x.sequence),[6]);
 const replay=mergeGraphTimeline(gap,run,[{sequence:7,type:'node.next'}],false,1);
 assert.equal(replay.events.length,1); assert.equal(replay.events[0].sequence,7);
 assert.equal(mergeGraphTimeline(null,run,[{sequence:2,type:'secret<script>'}]),null);
 assert.equal(mergeGraphTimeline(next,run,[{sequence:7,type:'node.a'},{sequence:6,type:'node.b'}]),null);
 assert.equal(mergeGraphTimeline(next,'wrong',[]),null);
 assert.equal(graphEventLabel('node.claimed'),'Node Claimed');
});
