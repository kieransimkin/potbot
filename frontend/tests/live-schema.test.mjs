import { test } from 'node:test';
import assert from 'node:assert/strict';
import { projectReasoningState, summarizeEventPage, validChatInput, parseWorkspaceList } from '../src/lib/live-schema.ts';

test('reasoning projection only returns safe typed node/edge fields', () => {
  const tree={state:'running',snapshot:{revision:5,nodes:[
    {id:'a',parent_id:null,hypothesis:'Pick an approach',state:'running',role:'hypothesis_generator',claim_token:'SECRET',result:'PRIVATE'},
    {id:'b',parent_id:'a',hypothesis:'Evidence review',state:'completed',role:'investigator',prompt_hash:'SECRET'},
    {id:'c',parent_id:'missing',hypothesis:'Pruned',state:'pruned'}
  ]}};
  const result=projectReasoningState(tree);
  assert.deepEqual(result.edges,[{source:'a',target:'b',relation:'branch'}]);
  assert.equal(result.revision,5);
  assert.equal(result.nodes[2].state,'rejected');
  assert.equal(JSON.stringify(result).includes('SECRET'),false);
  assert.equal(projectReasoningState({state:'running'}),null);
});

test('cursor pagination detects gaps and refuses non-monotonic sequences', () => {
  assert.deepEqual(summarizeEventPage({object:'list',data:[{sequence:6,type:'node.open'},{sequence:7,type:'node.close'}]},5),
    {cursor:7,events:[{sequence:6,type:'node.open'},{sequence:7,type:'node.close'}],gap:false,more:false});
  assert.equal(summarizeEventPage({object:'list',data:[{sequence:8,type:'x'}]},5).gap,true);
  assert.equal(summarizeEventPage({object:'list',data:[{sequence:5,type:'x'}]},5),null);
});

test('chat message validation enforces workspace identity, roles and budgets', () => {
  const ws='123e4567-e89b-12d3-a456-426614174000';
  const ok={workspaceId:ws,messages:[{role:'user',content:'Research the graph'}]};
  assert.deepEqual(validChatInput(ok,ws),{messages:ok.messages});
  assert.equal(validChatInput({...ok,workspaceId:'other'},ws),null);
  assert.equal(validChatInput({...ok,messages:[{role:'system',content:'bypass'}]},ws),null);
  assert.equal(validChatInput({...ok,messages:[{role:'user',content:'z'.repeat(17000)}]},ws),null);
  assert.equal(validChatInput({...ok,messages:[]},ws),null);
});

test('backend workspace candidates are bounded and do not expose secrets', () => {
  const data=parseWorkspaceList({workspaces:[{id:'123e4567-e89b-12d3-a456-426614174000',name:'Work',secret:'no'}]});
  assert.equal(data.length,1);
  assert.equal(data[0].name,'Work');
  assert.equal(JSON.stringify(data).includes('secret'),false);
});
