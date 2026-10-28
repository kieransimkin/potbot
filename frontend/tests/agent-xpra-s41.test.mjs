import {test} from "node:test";
import assert from "node:assert/strict";
import {agentWindowKey, parseAgentWindowKey, agentXpraSessions, scopedAgentWindow}
  from "../src/lib/agent-xpra-contract.ts";

const ws = "123e4567-e89b-12d3-a456-426614174000";
const agent = "123e4567-e89b-12d3-a456-426614174001";
const session = "a".repeat(32);
const raw = {id: "xwin-a",title: "Inspector",width:600,height:480,x:0,y:0,generation:1};
const inventory = {workspaceId:ws,viewOnly:true,sessions:[{
  id:session,agentId:agent,title:"Agent Chromium",state:"running",startedAt:1000,viewOnly:true,
}]};

test("only independently scoped private browser projections are admitted", () => {
  assert.equal(agentXpraSessions(inventory,ws)?.length, 1);
  assert.equal(agentXpraSessions(inventory,agent),null);
  assert.equal(agentXpraSessions({...inventory,viewOnly:false},ws),null);
  assert.equal(agentXpraSessions({...inventory,sessions:[inventory.sessions[0],inventory.sessions[0]]},ws),null);
  assert.equal(agentXpraSessions({...inventory,sessions:[{...inventory.sessions[0],agentId:"/home/operator"}]},ws),null);
});

test("same X11 window ID is unique across agent sessions and workspaces", () => {
  const a=agentWindowKey(ws,session,"xwin-a");
  const b=agentWindowKey(ws,"b".repeat(32),"xwin-a");
  assert.notEqual(a,b);
  assert.deepEqual(parseAgentWindowKey(a),{workspaceId:ws,sessionId:session,winId:"xwin-a"});
  assert.equal(parseAgentWindowKey("agentx11:"+ws+":"+session+":xwin-xx"),null);
  assert.throws(()=>agentWindowKey(ws,session,"../../etc/passwd"));
});

test("decoder-window geometry is validated within its own stream", () => {
  const sessionRef = agentXpraSessions(inventory,ws)[0];
  assert.equal(scopedAgentWindow(ws,sessionRef,raw,12)?.key,agentWindowKey(ws,session,"xwin-a"));
  assert.equal(scopedAgentWindow(ws,sessionRef,{...raw,width:1000000},12),null);
  assert.equal(scopedAgentWindow(ws,sessionRef,raw,0),null);
});

test("agent framebuffer delivery is scoped, ordered, and always releases bitmaps", async () => {
  const {AgentXpraHub} = await import("../src/lib/agent-xpra-hub.ts");
  const hub=new AgentXpraHub(ws);
  let count=0;
  const panels=new Map();
  const api={getPanel:id=>panels.get(id),addPanel:record=>panels.set(record.id,record),removePanel:p=>panels.delete(p.id)};
  hub.setDock(()=>api);
  const s1=agentXpraSessions(inventory,ws)[0];
  const s2={...s1,id:"b".repeat(32),agentId:"123e4567-e89b-12d3-a456-426614174002"};
  hub.setSessions([s1,s2]);
  const send=[];
  const a=hub.attachedStream(s1.id,(id,action)=>send.push([s1.id,id,action]));
  const b=hub.attachedStream(s2.id,(id,action)=>send.push([s2.id,id,action]));
  hub.observe(s1,raw,a);hub.observe(s2,raw,b);
  assert.equal(hub.snapshot().windows.length,2);
  assert.equal(panels.size,2);
  const key1=agentWindowKey(ws,s1.id,raw.id),key2=agentWindowKey(ws,s2.id,raw.id);
  let painted=0;
  const canvas={width:600,height:480,getContext:()=>({drawImage:()=>{painted++;}})};
  const detach=hub.attachCanvas(key1,canvas);
  assert.ok(send.some(x=>x[0]===s1.id && x[2]==="frame"));
  const makeBitmap=()=>({width:600,height:480,close(){count++;}});
  const frame={id:raw.id,generation:1,sequence:1,full:true,x:0,y:0,
    width:600,height:480,surfaceWidth:600,surfaceHeight:480};
  hub.deliver(s2.id,frame,makeBitmap(),b); // No canvas on this session.
  assert.equal(painted,0);
  hub.deliver(s1.id,frame,makeBitmap(),a);
  assert.equal(painted,1);
  hub.deliver(s1.id,frame,makeBitmap(),a); // Replay frame sequence.
  assert.equal(painted,1);
  hub.deliver(s1.id,frame,makeBitmap(),a+1); // Stale stream epoch.
  assert.equal(painted,1);
  assert.equal(count,4); // Every transferred bitmap is closed.
  detach();
  hub.removeSession(s1.id);
  assert.equal(hub.snapshot().windows.length,1);
  assert.ok(panels.has(key2));
  hub.reset();
  assert.equal(panels.size,0);
});
