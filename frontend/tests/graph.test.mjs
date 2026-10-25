import { test } from 'node:test';
import assert from 'node:assert/strict';
import { boundedGraph, graphPositions, graphStateColour } from '../src/lib/graph.ts';

test('invalid and duplicate links are removed; nodes remain stable', () => {
  const data = {revision:1,nodes:[{id:'root',parentId:null,label:'Start',kind:'objective',state:'running'},{id:'child',parentId:'root',label:'Step',kind:'task',state:'completed'}, {id:'root',parentId:null,label:'Duplicate',kind:'objective',state:'running'}],edges:[{source:'root',target:'child',relation:'supports'},{source:'not-present',target:'root',relation:'false'}]};
  const bounded = boundedGraph(data,10,10);
  assert.equal(bounded.nodes.length,2);
  assert.equal(bounded.edges.length,1);
  assert.deepEqual([...graphPositions(data).entries()], [...graphPositions(data).entries()]);
  assert.notEqual(graphStateColour('completed'),graphStateColour('rejected'));
});

test('graph bounding keeps large events out of the WebGL scene', () => {
  const nodes = Array.from({length:2400},(_,i)=>({id:`node${i}`, parentId:i?'node0':null, label:'Node', kind:'task',state:'pending'}));
  const bounded = boundedGraph({revision:5,nodes,edges:[]});
  assert.equal(bounded.nodes.length,1500);
  assert.equal(graphPositions(bounded).size,1500);
});
