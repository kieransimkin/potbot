import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionCookie } from '../src/lib/server/security.ts';
import { GET } from '../src/app/api/ui/tools/upgrades/route.ts';
import { POST } from '../src/app/api/ui/tools/upgrades/[id]/[action]/route.ts';

const id = 'c7687924-49a1-4a65-b48e-f1d9d29ed409';
const operation = '1050b1bb-21c2-4188-a053-01498ce7087c';
const record = { id: operation, workspaceId:id, slug:'stemlab', oldCommit:'a'.repeat(40),
  newCommit:'b'.repeat(40), suiteSha256:'c'.repeat(64), evidenceSha256:'d'.repeat(64),
  passedTests:3, tests:[{name:'smoke-test',kind:'smoke',elapsedMs:12,passed:true},{name:'unit-test',kind:'unit',elapsedMs:20,passed:true},{name:'regression-test',kind:'regression',elapsedMs:18,passed:true}], revision:0, state:'awaiting_approval', updatedAt:'2026-10-25T10:17:00Z' };
const auth = { password:'operator-password-very-long', signingKey:'a'.repeat(64), publicOrigin:'http://localhost:3100' };
const cookie = sessionCookie(auth).split(';')[0];
const old = new Map();
for (const [key,value] of Object.entries({ POTBOT_UI_PASSWORD:auth.password, POTBOT_UI_COOKIE_SECRET:auth.signingKey,
  POTBOT_UI_PUBLIC_ORIGIN:auth.publicOrigin, POTBOT_UI_BACKEND_TOKEN:'backend-operator-token-should-never-reflect',
  POTBOT_UI_BACKEND_URL:'http://127.0.0.1:8765/', POTBOT_UI_WORKSPACE_ID:id,
  POTBOT_UI_SETUP_TOKEN:'S'.repeat(64) })) { old.set(key,process.env[key]); process.env[key]=value; }
function restore(){for(const [key,value] of old) {if(value===undefined) delete process.env[key]; else process.env[key]=value;}}
const request = (method, body, origin=true) => new Request('http://localhost:3100/api/ui/tools/upgrades', {
  method, headers:{ Cookie:cookie, ...(origin?{Origin:auth.publicOrigin}:{}),
    'Sec-Fetch-Site':'same-origin', 'Content-Type':'application/json',
    'X-Potbot-Workspace':id }, ...(body?{body:JSON.stringify(body)}:{}) });

test('GET only returns sanitized workspace upgrade projection',async()=>{
  const fetchPrior = globalThis.fetch;
  globalThis.fetch = async (url,opts)=>{
    assert.equal(url,'http://127.0.0.1:8765/v1/ui/tools/upgrades');
    assert.equal(opts.headers.Authorization, `Bearer ${process.env.POTBOT_UI_BACKEND_TOKEN}`);
    return Response.json({schema:'potbot-tool-upgrades-s11/v1',items:[{...record,privatePath:'/home/secret', token:'bad'}]});
  };
  try{const resp=await GET(request('GET'));assert.equal(resp.status,200);
    const payload=await resp.json();assert.equal(payload.items.length,1);
    assert.equal(JSON.stringify(payload).includes('privatePath'),false);
  }finally {globalThis.fetch=fetchPrior;}
});

test('POST sends setup capability only to loopback backend and never returns it',async()=>{
  const fetchPrior = globalThis.fetch;
  globalThis.fetch=async(url,opts)=>{
    assert.equal(url,`http://127.0.0.1:8765/v1/ui/tools/upgrades/${operation}/approve`);
    assert.equal(opts.headers['X-Potbot-Setup-Token'],'S'.repeat(64));
    assert.equal(opts.headers.Origin,auth.publicOrigin);
    return Response.json({...record,revision:1,state:'approved',operator_internal:'secret'});
  };
  try{const resp=await POST(request('POST',{revision:0,evidenceSha256:record.evidenceSha256,confirm:true}),
    {params:Promise.resolve({id:operation,action:'approve'})});
    assert.equal(resp.status,200);const text=await resp.text();assert.equal(text.includes('operator_internal'),false);
    assert.equal(text.includes('SSSSSSSSS'),false);assert.equal(JSON.parse(text).state,'approved');
  }finally {globalThis.fetch=fetchPrior;}
});

test('POST rejects missing origin before touching upstream',async()=>{
 const fetchPrior=globalThis.fetch;
 globalThis.fetch=async()=>{throw new Error('should not fetch');};
 try{const result=await POST(request('POST',{revision:0,evidenceSha256:record.evidenceSha256,confirm:true},false),
  {params:Promise.resolve({id:operation,action:'approve'})});assert.equal(result.status,403);}
 finally{globalThis.fetch=fetchPrior;restore();}
});
