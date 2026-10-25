import { test } from 'node:test';
import assert from 'node:assert/strict';
import { GET as authStatus } from '../src/app/api/ui/auth/status/route.ts';
import { POST as login } from '../src/app/api/ui/auth/login/route.ts';
import { POST as logout } from '../src/app/api/ui/auth/logout/route.ts';
import { GET as bootstrap } from '../src/app/api/ui/bootstrap/route.ts';
import { POST as chat } from '../src/app/api/ui/chat/route.ts';
import { GET as reasoning } from '../src/app/api/ui/reasoning/[runId]/route.ts';

const origin='http://127.0.0.1:3100';
const workspaceId='123e4567-e89b-12d3-a456-426614174000';
const runId='a'.repeat(32);
function request(path, init={}) {return new Request(origin+path,init);}
const original={...process.env};
const vars={POTBOT_UI_PASSWORD:'long-private-operator-password',POTBOT_UI_COOKIE_SECRET:'s'.repeat(64),
  POTBOT_UI_PUBLIC_ORIGIN:origin,POTBOT_UI_BACKEND_URL:'http://127.0.0.1:8000',
  POTBOT_UI_BACKEND_TOKEN:'private-workspace-token',POTBOT_UI_WORKSPACE_ID:workspaceId};
let cookie;

// Serial subtests avoid mutable process.env/fetch races. No Next or backend server required.
test('authenticated UI routes enforce origin, scope and redact backend metadata', async t=>{
  Object.assign(process.env,vars);
  const realFetch=globalThis.fetch;
  t.after(()=>{globalThis.fetch=realFetch;for(const k of Object.keys(vars)){if(k in original)process.env[k]=original[k];else delete process.env[k];}});
  const initial=await authStatus(request('/api/ui/auth/status'));
  assert.deepEqual(await initial.json(),{configured:true,authenticated:false});
  await t.test('Login rejects wrong origin, accepts correct password, sets private cookie',async()=>{
    const wrong=await login(request('/api/ui/auth/login',{method:'POST',headers:{origin:'https://evil.invalid','Content-Type':'application/json'},body:JSON.stringify({password:vars.POTBOT_UI_PASSWORD})}));
    assert.equal(wrong.status,403);
    const right=await login(request('/api/ui/auth/login',{method:'POST',headers:{origin,'Content-Type':'application/json'},body:JSON.stringify({password:vars.POTBOT_UI_PASSWORD})}));
    assert.equal(right.status,200);
    cookie=right.headers.get('set-cookie').split(';')[0];
    assert.ok(right.headers.get('set-cookie').includes('HttpOnly'));
  });
  await t.test('Bootstrap never exposes token and uses only explicitly configured workspace',async()=>{
    globalThis.fetch=async(input,init)=>{
      assert.equal(new URL(input).pathname,'/v1/ui/workspace');
      assert.equal(init.headers.Authorization,'Bearer '+vars.POTBOT_UI_BACKEND_TOKEN);
      return Response.json({workspace:{id:workspaceId,name:'Potbot'},available:true});
    };
    const response=await bootstrap(request('/api/ui/bootstrap',{headers:{cookie}}));
    assert.equal(response.status,200);
    const data=await response.json();
    assert.equal(data.workspaces.length,1);
    assert.equal(data.workspaces[0].id,workspaceId);
    assert.equal(JSON.stringify(data).includes('private-workspace-token'),false);
    assert.equal(data.features.durableChatSessions,true);
  });
  await t.test('Chat rejects cross-workspace injection before reaching backend',async()=>{
    globalThis.fetch=()=>{throw new Error('no network should occur');};
    const result=await chat(request('/api/ui/chat',{method:'POST',headers:{cookie,origin,'Content-Type':'application/json'},body:JSON.stringify({workspaceId:'different',messages:[{role:'user',content:'secret'}]})}));
    assert.equal(result.status,400);
    const csrf=await chat(request('/api/ui/chat',{method:'POST',headers:{cookie,origin:'https://evil.invalid','Content-Type':'application/json'},body:JSON.stringify({workspaceId,messages:[{role:'user',content:'hello'}]})}));
    assert.equal(csrf.status,403);
  });
  await t.test('Chat bridges real SSE, returns run ID without bearer token to browser',async()=>{
    globalThis.fetch=async(input,init)=>{
      assert.equal(new URL(input).pathname,'/v1/chat/completions');
      const payload=JSON.parse(init.body);
      assert.equal(payload.model,'potbot');
      assert.deepEqual(payload.messages,[{role:'user',content:'hello'}]);
      assert.equal(payload.stream,true);
      return new Response('data: {"choices":[{"delta":{"content":"Hello!"}}]}\n\ndata: [DONE]\n\n',
        {headers:{'Content-Type':'text/event-stream','X-Potbot-Run-Id':runId}});
    };
    const response=await chat(request('/api/ui/chat',{method:'POST',headers:{cookie,origin,'Content-Type':'application/json'},body:JSON.stringify({workspaceId,messages:[{role:'user',content:'hello'}]})}));
    assert.equal(response.status,200);
    assert.equal(response.headers.get('X-Potbot-Run-Id'),runId);
    assert.match(await response.text(),/Hello!/);
    assert.equal([...response.headers].some(([k,v])=>v.includes(vars.POTBOT_UI_BACKEND_TOKEN)),false);
  });
  await t.test('Graph proxy projects safe snapshots and monotonic event cursors',async()=>{
    globalThis.fetch=async(input,init)=>{
      assert.equal(init.headers.Authorization,'Bearer '+vars.POTBOT_UI_BACKEND_TOKEN);
      const path=new URL(input).pathname;
      if(path===`/v1/reasoning/${runId}`) return Response.json({state:'running',snapshot:{revision:7,nodes:[{id:'n1',parent_id:null,hypothesis:'Review choices',state:'running',claim_token:'HIDDEN'}]}});
      if(path===`/v1/reasoning/${runId}/events`) return Response.json({object:'list',data:[{sequence:5,type:'node.claimed',data:{secret:'HIDDEN'}}]});
      throw new Error('unexpected endpoint');
    };
    const response=await reasoning(request(`/api/ui/reasoning/${runId}?after=4`,{headers:{cookie}}),{params:Promise.resolve({runId})});
    assert.equal(response.status,200);
    const view=await response.json();
    assert.equal(view.cursor,5);
    assert.equal(view.graph.revision,7);
    assert.deepEqual(view.events,[{sequence:5,type:'node.claimed'}]);
    assert.ok(!JSON.stringify(view).includes('HIDDEN'));
    const bad=await reasoning(request('/api/ui/reasoning/../../oops',{headers:{cookie}}),{params:Promise.resolve({runId:'../invalid'})});
    assert.equal(bad.status,400);
  });
  await t.test('Sign out clears cookie but cannot be cross-origin induced',async()=>{
    assert.equal((await logout(request('/api/ui/auth/logout',{method:'POST',headers:{origin:'https://evil.invalid',cookie}}))).status,403);
    const response=await logout(request('/api/ui/auth/logout',{method:'POST',headers:{origin,cookie}}));
    assert.equal(response.status,200);
    assert.match(response.headers.get('set-cookie'),/Max-Age=0/);
  });
});
