import { test } from 'node:test';
import assert from 'node:assert/strict';
import { projectAppInventory, projectAppSession, validAppKind, appSessionId } from '../src/lib/xpra-apps.ts';
import { GET, POST } from '../src/app/api/ui/xpra/apps/route.ts';
import { POST as mutate } from '../src/app/api/ui/xpra/apps/[sessionId]/[operation]/route.ts';

const origin = 'http://127.0.0.1:3100';
const workspaceId = '123e4567-e89b-12d3-a456-426614174000';
const id = '1'.repeat(32);
const token = 't'.repeat(50);
const env = {
  POTBOT_UI_PASSWORD: 'xpra-tests-password', POTBOT_UI_COOKIE_SECRET: 'q'.repeat(64),
  POTBOT_UI_PUBLIC_ORIGIN: origin,
  POTBOT_UI_BACKEND_URL: 'http://127.0.0.1:8000',
  POTBOT_UI_BACKEND_TOKEN: 'backend-only',
  POTBOT_UI_WORKSPACE_ID: workspaceId,
  POTBOT_UI_XPRA_BRIDGE_URL: 'http://127.0.0.1:8765/',
  POTBOT_UI_XPRA_BRIDGE_TOKEN: token,
  POTBOT_UI_XPRA_PUBLIC_WS_URL: 'ws://127.0.0.1:8765/v1/xpra/ws',
};
const sample = {id,kind:'terminal',title:'Shell',state:'running',detached:false,startedAt:1700000000,exitCode:null};

test('session projections omit paths, argv, Xauthority and host PIDs',()=>{
  assert.ok(validAppKind('terminal')); assert.ok(!validAppKind('../evil'));
  assert.ok(appSessionId(id)); assert.ok(!appSessionId('../../etc/passwd'));
  assert.deepEqual(projectAppSession({...sample, argv:['/host/secret'], pid:99}), sample);
  assert.equal(projectAppSession({...sample, state:'starting'}), null);
  assert.equal(projectAppSession({...sample,id:'bad'}), null);
  assert.equal(projectAppSession({...sample,startedAt:Infinity}),null);
  assert.deepEqual(projectAppInventory({workspaceId,configured:true,choices:[{kind:'terminal',title:'Shell'}],
    sessions:[{...sample, socket:'/tmp/fake', authority:'SECRET'}]},workspaceId),
  {workspaceId,configured:true,choices:[{kind:'terminal',title:'Shell'}],sessions:[sample]});
  assert.equal(projectAppInventory({workspaceId:'wrong', configured:true,choices:[],sessions:[]},workspaceId),null);
  assert.equal(projectAppInventory({workspaceId,configured:false,choices:[{kind:'terminal',title:'Shell'}],sessions:[]},workspaceId),null);
});

test('authenticated BFF routes enforce workspace, origin, kinds and owned session IDs',async t=>{
  const old={};for(const [key,value] of Object.entries(env)){old[key]=process.env[key];process.env[key]=value;}
  const original=globalThis.fetch;
  t.after(()=>{globalThis.fetch=original;for(const [k,v] of Object.entries(old)){if(v===undefined) delete process.env[k]; else process.env[k]=v;}});
  const { POST: login }=await import('../src/app/api/ui/auth/login/route.ts');
  const auth=await login(new Request(origin+'/api/ui/auth/login',{method:'POST',headers:{origin,'Content-Type':'application/json'},body:JSON.stringify({password:env.POTBOT_UI_PASSWORD})}));
  assert.equal(auth.status,200);
  const cookie=auth.headers.get('set-cookie').split(';')[0];
  const baseHeaders={cookie,'x-potbot-workspace':workspaceId, origin};
  let called=0;
  globalThis.fetch=async (url,opts)=>{
    called++;
    assert.equal(opts.headers.Authorization,`Bearer ${token}`);
    assert.ok(url.startsWith('http://127.0.0.1:8765/v1/xpra/apps'));
    if(opts.method==='GET') return Response.json({workspaceId,configured:true,choices:[{kind:'terminal',title:'Shell'}],sessions:[sample]});
    if(url.endsWith('/terminate')) return Response.json({workspaceId,session:{...sample,state:'exited',exitCode:0}});
    assert.equal(JSON.parse(opts.body).kind,'terminal');
    return Response.json({workspaceId,session:sample},{status:201});
  };
  const denied=await POST(new Request(origin+'/api/ui/xpra/apps',{method:'POST',headers:{...baseHeaders,origin:'https://evil.test'},body:'{"kind":"terminal"}'}));
  assert.equal(denied.status,403);assert.equal(called,0);
  assert.equal((await GET(new Request(origin+'/api/ui/xpra/apps',{headers:{...baseHeaders,'x-potbot-workspace':'other'}}))).status,404);
  assert.equal(called,0);
  const invalid=await POST(new Request(origin+'/api/ui/xpra/apps',{method:'POST',headers:baseHeaders,body:JSON.stringify({kind:'terminal',argv:['/bin/bash']})}));
  assert.equal(invalid.status,400);assert.equal(called,0);
  const listed=await GET(new Request(origin+'/api/ui/xpra/apps',{headers:baseHeaders}));
  assert.equal(listed.status,200);assert.equal((await listed.json()).sessions[0].id,id);
  const created=await POST(new Request(origin+'/api/ui/xpra/apps',{method:'POST',headers:baseHeaders,body:'{"kind":"terminal"}'}));
  assert.equal(created.status,201);assert.equal((await created.json()).session.id,id);
  const stopped=await mutate(new Request(origin+`/api/ui/xpra/apps/${id}/terminate`,{method:'POST',headers:baseHeaders}),
    {params:Promise.resolve({sessionId:id,operation:'terminate'})});
  assert.equal(stopped.status,200);assert.equal((await stopped.json()).session.state,'exited');
  assert.equal(called,3);
  assert.equal((await mutate(new Request(origin+'/api/ui/xpra/apps/bad/terminate',{method:'POST',headers:baseHeaders}),
    {params:Promise.resolve({sessionId:'bad',operation:'terminate'})})).status,400);
});

test('different operator-authorized workspaces select their own private Xpra relay', async()=>{
  const { xpraBridgeConfigForWorkspace }=await import('../src/lib/server/xpra.ts');
  const other='123e4567-e89b-12d3-a456-426614174999';
  const multi={...env, POTBOT_UI_WORKSPACES_JSON: JSON.stringify([
    {id:workspaceId,name:'One',url:'http://127.0.0.1:8000',token:'backend-one'},
    {id:other,name:'Two',url:'http://127.0.0.1:8001',token:'backend-two'},
  ]), POTBOT_UI_XPRA_BRIDGES_JSON:JSON.stringify([
    {workspaceId,bridgeUrl:'http://127.0.0.1:8765/',bridgeToken:token,publicWsUrl:'ws://127.0.0.1:8765/v1/xpra/ws'},
    {workspaceId:other,bridgeUrl:'http://127.0.0.1:8766/',bridgeToken:'b'.repeat(48),publicWsUrl:'ws://127.0.0.1:8766/v1/xpra/ws'},
  ])};
  const a=xpraBridgeConfigForWorkspace(workspaceId,multi);
  const b=xpraBridgeConfigForWorkspace(other,multi);
  assert.equal(a?.base,'http://127.0.0.1:8765');
  assert.equal(b?.base,'http://127.0.0.1:8766');
  assert.notEqual(a?.secret,b?.secret);
  assert.equal(xpraBridgeConfigForWorkspace('unauthorized',multi),null);
  const bad={...multi, POTBOT_UI_XPRA_BRIDGES_JSON:JSON.stringify([
    {workspaceId,bridgeUrl:'http://evil.test/',bridgeToken:token,publicWsUrl:'ws://evil.test/v1/xpra/ws'},
  ])};
  assert.equal(xpraBridgeConfigForWorkspace(workspaceId,bad),null);
});
