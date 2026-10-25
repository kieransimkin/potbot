import { test } from 'node:test';
import assert from 'node:assert/strict';
import { bytesLabel, projectPreferences, projectTransfers, validateArtifact, validateSettings } from '../src/lib/ui-resources.ts';
import { GET as getPrefs, PUT as putPrefs } from '../src/app/api/ui/preferences/route.ts';
import { GET as getTransfers, POST as queue } from '../src/app/api/ui/downloads/route.ts';
import { POST as cancel } from '../src/app/api/ui/downloads/[id]/cancel/route.ts';
import { POST as login } from '../src/app/api/ui/auth/login/route.ts';

const origin='http://127.0.0.1:3100';
const workspaceId='123e4567-e89b-12d3-a456-426614174000';
const transferId='123e4567-e89b-12d3-a456-426614174002';
const original = {...process.env};
const env={ POTBOT_UI_PASSWORD:'test-secret-operator-password', POTBOT_UI_COOKIE_SECRET:'k'.repeat(64),
 POTBOT_UI_PUBLIC_ORIGIN:origin, POTBOT_UI_BACKEND_URL:'http://127.0.0.1:8765',
 POTBOT_UI_BACKEND_TOKEN:'DO-NOT-SHOW', POTBOT_UI_WORKSPACE_ID:workspaceId };
const request = (path,options={}) => new Request(origin+path, options);
const artifact = { model_id:'Qwen/Qwen3-0.6B',artifact_repository:'Qwen/Qwen3-0.6B',revision:'a'.repeat(40),
 filename:'model.Q4_K_M.gguf',quantization:'Q4_K_M',sha256:'b'.repeat(64),size_bytes:1024 };

test('strict schema rejects credentials, unpinned models and cross-workspace progress',()=>{
 assert.deepEqual(validateSettings({theme:'dark',xpraMaxFps:30}),{theme:'dark',xpraMaxFps:30});
 assert.equal(validateSettings({apiKey:'SHOULD-NOT-APPEAR'}),null);
 assert.equal(validateSettings({reducedMotion:1}),null);
 assert.equal(validateArtifact({...artifact,revision:'main'}),null);
 assert.equal(validateArtifact({...artifact,filename:'../target'}),null);
 assert.ok(validateArtifact(artifact));
 assert.equal(projectPreferences({workspaceId,revision:0,settings:{}},workspaceId)?.revision,0);
 assert.equal(projectPreferences({workspaceId:'other',revision:0,settings:{}},workspaceId),null);
 const feed={workspaceId,transferTelemetry:true,installTelemetry:true,items:[{ id:transferId, modelId:artifact.model_id,
 revision:artifact.revision,filename:artifact.filename,quantization:artifact.quantization,state:'downloading',
 expectedBytes:1024,downloadedBytes:512,verifiedBytes:0,cancelRequested:false,errorCode:null,
 updatedAt:'2026-10-25T04:00:00+00:00',password:'PRIVATE'}]};
 assert.equal(projectTransfers(feed,workspaceId)?.items[0].downloadedBytes,512);
 assert.ok(!JSON.stringify(projectTransfers(feed,workspaceId)).includes('PRIVATE'));
 assert.equal(projectTransfers({...feed,workspaceId:'different'},workspaceId),null);
 assert.equal(projectTransfers({...feed,items:[{...feed.items[0],verifiedBytes:700}]},workspaceId),null);
 assert.equal(bytesLabel(1024),'1.0 KiB');
});

test('authenticated preferences/download routes enforce origin, scope and whitelisting',async t=>{
 Object.assign(process.env,env);
 const oldFetch=globalThis.fetch;
 t.after(()=>{globalThis.fetch=oldFetch;for(const k of Object.keys(env)){if(k in original)process.env[k]=original[k];else delete process.env[k];}});
 assert.equal((await getPrefs(request('/api/ui/preferences'))).status,401);
 const auth=await login(request('/api/ui/auth/login',{method:'POST',headers:{origin,'Content-Type':'application/json'},body:JSON.stringify({password:env.POTBOT_UI_PASSWORD})}));
 const cookie=auth.headers.get('set-cookie').split(';')[0];
 await t.test('preferences read never expose backend secrets',async()=>{
  globalThis.fetch=async(url,init)=>{
   assert.equal(new URL(url).pathname,'/v1/ui/preferences');assert.equal(init.headers.Authorization,'Bearer DO-NOT-SHOW');
   return Response.json({revision:3,settings:{theme:'dark',password:'UNSAFE'}});
  };
  assert.equal((await getPrefs(request('/api/ui/preferences',{headers:{cookie}}))).status,502);
  globalThis.fetch=async()=>Response.json(["not an object"]);
  assert.equal((await getPrefs(request('/api/ui/preferences',{headers:{cookie}}))).status,502);
  globalThis.fetch=async()=>Response.json({revision:3,settings:{theme:'dark'}});
  assert.deepEqual(await (await getPrefs(request('/api/ui/preferences',{headers:{cookie}}))).json(),
    {workspaceId,revision:3,settings:{theme:'dark'}});
 });
 await t.test('write rejects invalid settings and cross-origin requests before network',async()=>{
  globalThis.fetch=()=>{throw new Error('must not call backend')};
  const payload={revision:3,settings:{theme:'dark'}};
  assert.equal((await putPrefs(request('/api/ui/preferences',{method:'PUT',headers:{cookie,origin:'https://evil.test'},body:JSON.stringify(payload)}))).status,403);
  assert.equal((await putPrefs(request('/api/ui/preferences',{method:'PUT',headers:{cookie,origin},body:JSON.stringify({revision:3,settings:{password:'oops'}})}))).status,400);
  globalThis.fetch=async(url,init)=>{
   assert.equal(new URL(url).pathname,'/v1/ui/preferences');
   assert.deepEqual(JSON.parse(init.body),payload);
   return Response.json({revision:4,settings:{theme:'dark'}});
  };
  assert.equal((await putPrefs(request('/api/ui/preferences',{method:'PUT',headers:{cookie,origin},body:JSON.stringify(payload)}))).status,200);
 });
 await t.test('real transfer progress hides source credentials',async()=>{
  globalThis.fetch=async()=>Response.json({items:[{id:transferId,modelId:artifact.model_id,
   revision:artifact.revision,filename:artifact.filename,quantization:artifact.quantization,
   state:'downloading',expectedBytes:1024,downloadedBytes:512,verifiedBytes:0,
   cancelRequested:false,errorCode:null,updatedAt:'2026-10-25T04:00:00+00:00',api_key:'PRIVATE'}],
   transferTelemetry:true,installTelemetry:true,source_url:'PRIVATE'});
  const res=await getTransfers(request('/api/ui/downloads',{headers:{cookie}}));
  assert.equal(res.status,200);
  const body=await res.json();
  assert.equal(body.items[0].downloadedBytes,512);
  assert.ok(!JSON.stringify(body).includes('PRIVATE'));
 });
 await t.test('queue/cancel demand explicit manifest and CSRF',async()=>{
  globalThis.fetch=()=>{throw new Error('should not be dispatched')};
  assert.equal((await queue(request('/api/ui/downloads',{method:'POST',headers:{cookie,origin:'https://wrong.test'},body:JSON.stringify({artifact,confirm:true,requestId:transferId})}))).status,403);
  assert.equal((await queue(request('/api/ui/downloads',{method:'POST',headers:{cookie,origin},body:JSON.stringify({artifact:{...artifact,revision:'main'},confirm:true,requestId:transferId})}))).status,400);
  globalThis.fetch=async(url,init)=>{assert.equal(new URL(url).pathname,'/v1/ui/downloads');assert.equal(JSON.parse(init.body).confirm,true);return Response.json({id:transferId,state:'queued',created:true});};
  const queued=await queue(request('/api/ui/downloads',{method:'POST',headers:{cookie,origin},body:JSON.stringify({artifact,confirm:true,requestId:transferId})}));
  assert.equal(queued.status,202);
  globalThis.fetch=async(url,init)=>{assert.equal(new URL(url).pathname,`/v1/ui/downloads/${transferId}/cancel`);return Response.json({id:transferId,state:'cancelled'});};
  const cancelled=await cancel(request(`/api/ui/downloads/${transferId}/cancel`,{method:'POST',headers:{cookie,origin}}),{params:Promise.resolve({id:transferId})});
  assert.equal(cancelled.status,202);
 });
});
