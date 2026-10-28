import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AgentXpraHub } from '../src/lib/agent-xpra-hub.ts';
import { agentWindowKey, agentXpraSessions } from '../src/lib/agent-xpra-contract.ts';

const workspace = '123e4567-e89b-12d3-a456-426614174000';
const agent = '123e4567-e89b-12d3-a456-426614174001';
const session = 'e'.repeat(32);
const source = {workspaceId: workspace, viewOnly: true, sessions: [
  {id: session, agentId: agent, title: 'Browser', state: 'running', viewOnly: true, startedAt: 500},
]};
const entry = agentXpraSessions(source, workspace)[0];
const window = {id:'xwin-a',title:'Browser',width:480,height:320,x:0,y:0,generation:1};

function hubWithDock() {
  const hub = new AgentXpraHub(workspace);
  const panels = new Map();
  hub.setDock(() => ({getPanel: id => panels.get(id), addPanel: item => panels.set(item.id,item),
    removePanel: item => panels.delete(item.id)}));
  hub.setSessions([entry]);
  return {hub,panels};
}

test('reconnection epochs are monotonic even without a window', () => {
  const {hub} = hubWithDock();
  const first = hub.attachedStream(session,()=>{});
  hub.disconnected(session,first);
  const second = hub.attachedStream(session,()=>{});
  assert.ok(second > first, 'new stream must advance without any observed X11 windows');
  hub.disconnected(session,first);
  hub.observe(entry, window, first);
  assert.equal(hub.snapshot().windows.length,0);
  hub.observe(entry, window, second);
  assert.equal(hub.snapshot().windows.length,1);
  assert.equal(hub.snapshot().windows[0].connected,true);
  hub.reset();
});

test('a stale canvas cleanup never hides a newer canvas', () => {
  const {hub} = hubWithDock();
  const messages=[];
  const epoch = hub.attachedStream(session,(id,action,visible)=>messages.push({id,action,visible}));
  hub.observe(entry,window,epoch);
  const key = agentWindowKey(workspace,session,window.id);
  const canvas = () => ({width:480,height:320,getContext:()=>({drawImage(){}})});
  const firstUnmount=hub.attachCanvas(key, canvas());
  const secondUnmount=hub.attachCanvas(key, canvas());
  const baseline=messages.length;
  firstUnmount();
  assert.equal(messages.length,baseline,'stale cleanup must not hide second canvas');
  secondUnmount();
  assert.equal(messages.at(-1).action,'visibility');
  assert.equal(messages.at(-1).visible,false);
});

test('removed browser cannot reappear from a late decoder event', () => {
  const {hub,panels} = hubWithDock();
  const epoch=hub.attachedStream(session,()=>{});
  hub.removeSession(session);
  hub.observe(entry,window,epoch);
  assert.equal(hub.snapshot().windows.length,0);
  assert.equal(panels.size,0);
});

test('generation and epoch both fence stale bitmaps after reconnection', () => {
  const {hub} = hubWithDock();
  const epoch=hub.attachedStream(session,()=>{});
  hub.observe(entry,window,epoch);
  const key=agentWindowKey(workspace,session,window.id);
  let paints=0,closes=0;
  hub.attachCanvas(key,{width:480,height:320,getContext:()=>({drawImage(){paints++;}})});
  const frame = {id:window.id,sequence:1,generation:1,full:true,x:0,y:0,width:480,height:320,
    surfaceWidth:480,surfaceHeight:320};
  const bitmap=()=>({width:480,height:320,close(){closes++;}});
  hub.disconnected(session,epoch);
  const next=hub.attachedStream(session,()=>{});
  assert.ok(next>epoch);
  hub.deliver(session,frame,bitmap(),epoch);
  hub.deliver(session,frame,bitmap(),next);
  assert.equal(paints,0,'old canvas is never painted by new or old stream');
  assert.equal(closes,2,'every transferred image buffer is released');
});
