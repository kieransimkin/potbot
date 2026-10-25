import { test } from 'node:test';
import assert from 'node:assert/strict';
import { workspaceConnections, potbotConfigForRequest } from '../src/lib/server/potbot.ts';
import { projectSession, projectSessionList, conversationEntries } from '../src/lib/chat-sessions.ts';
import { GET as boot } from '../src/app/api/ui/bootstrap/route.ts';
import { GET as list, POST as create } from '../src/app/api/ui/sessions/route.ts';
import { GET as read } from '../src/app/api/ui/sessions/[sessionId]/route.ts';
import { POST as send } from '../src/app/api/ui/sessions/[sessionId]/send/route.ts';
import { sessionCookie, authConfig } from '../src/lib/server/security.ts';

const origin='http://127.0.0.1:3100';
const ids=['11111111-2222-4333-8444-111111111111','22222222-3333-4333-8444-222222222222'];
const firstSession='a1111111-1111-4111-8111-111111111111';
const requestId='b1111111-1111-4111-8111-111111111111';
const turnId='c1111111-1111-4111-8111-111111111111';
const models=[{id:ids[0],name:'Engineering',url:'http://127.0.0.1:8765',token:'TOKEN_A'},
              {id:ids[1],name:'Studio',url:'http://localhost:8766',token:'TOKEN_B'}];
const env={POTBOT_UI_WORKSPACES_JSON:JSON.stringify(models),POTBOT_UI_PASSWORD:'secure-test-password-123',
  POTBOT_UI_COOKIE_SECRET:'Z'.repeat(64),POTBOT_UI_PUBLIC_ORIGIN:origin};
function req(path, init={}) {return new Request(origin+path,init);}
function scope(index=0){return {'x-potbot-workspace':ids[index]};}

// One serial test avoids collisions in global process.env and fetch mocks.
test('operator can switch only among explicitly allowed, individually authenticated workspaces',async t=>{
  const original={...process.env};const oldFetch=globalThis.fetch;
  Object.assign(process.env,env);
  t.after(()=>{globalThis.fetch=oldFetch;process.env=original;});
  const cookie=sessionCookie(authConfig());
  const auth={cookie};
  await t.test('identity isolation and no response-side credential disclosure',async()=>{
    globalThis.fetch=async(input,init)=>{
      const u=new URL(input), idx=u.port==='8766'?1:0;
      assert.equal(init.headers.Authorization,'Bearer '+models[idx].token);
      assert.equal(u.pathname,'/v1/ui/workspace');
      return Response.json({workspace:{id:ids[idx],name:models[idx].name},available:true});
    };
    const response=await boot(req('/api/ui/bootstrap',{headers:auth}));
    assert.equal(response.status,200);
    const out=await response.json();
    assert.deepEqual(out.workspaces.map(x=>x.id),ids);
    assert.equal(out.features.configuredWorkspaceConnections,2);
    assert.ok(!JSON.stringify(out).includes('TOKEN_'));
    assert.equal(potbotConfigForRequest(req('/api/ui/models',{headers:scope(1)})).token,'TOKEN_B');
    assert.equal(potbotConfigForRequest(req('/api/ui/models')),null);
    assert.equal(potbotConfigForRequest(req('/api/ui/models',{headers:{'x-potbot-workspace':'unauthorised'}})),null);
  });
  await t.test('duplicate IDs and external URLs fail the entire configuration',()=>{
    assert.deepEqual(workspaceConnections({...env,POTBOT_UI_WORKSPACES_JSON:JSON.stringify([...models,models[0]])}),[]);
    assert.deepEqual(workspaceConnections({...env,POTBOT_UI_WORKSPACES_JSON:JSON.stringify([{...models[0],url:'http://169.254.169.254:8000'}])}),[]);
  });
  await t.test('listing and projecting only bounded session history',async()=>{
    globalThis.fetch=async(input,init)=>{
      assert.equal(new URL(input).pathname,'/v1/ui/sessions');
      assert.equal(init.headers.Authorization,'Bearer TOKEN_B');
      return Response.json({items:[{id:firstSession,title:'Studio chat',revision:2,updatedAt:'2026-10-25T00:00:00Z'}]});
    };
    const result=await list(req('/api/ui/sessions',{headers:{...auth,...scope(1)}}));
    assert.equal(result.status,200);assert.equal((await result.json()).workspaceId,ids[1]);
    assert.equal((await list(req('/api/ui/sessions',{headers:auth}))).status,404);
    assert.equal(projectSessionList({items:[{id:firstSession,title:'Hi',revision:0,updatedAt:'now'}]}),null);
    const valid={session:{id:firstSession,title:'Hi',revision:1,updatedAt:'2026-10-25T00:00:00Z'},turns:[
      {id:turnId,ordinal:1,user:'hello',assistant:null,state:'indeterminate',runId:null}]};
    assert.match(conversationEntries(projectSession(valid))[1].text,/indeterminate/);
  });
  await t.test('CSRF and workspace mismatch block creation, then admitted sessions stay scoped',async()=>{
    globalThis.fetch=async(input,init)=>{
      assert.equal(new URL(input).pathname,'/v1/ui/sessions');
      assert.equal(init.headers.Authorization,'Bearer TOKEN_A');
      assert.equal(JSON.parse(init.body).id,firstSession);
      return Response.json({id:firstSession,title:'Saved chat',revision:0,updatedAt:'2026-10-25T00:00:00Z',created:true});
    };
    const params={method:'POST',headers:{cookie,origin,'content-type':'application/json',...scope(0)},
      body:JSON.stringify({id:firstSession,title:'Saved chat',workspaceId:ids[0]})};
    assert.equal((await create(req('/api/ui/sessions',{...params,headers:{...params.headers,origin:'https://evil.test'}}))).status,403);
    assert.equal((await create(req('/api/ui/sessions',{...params,body:JSON.stringify({id:firstSession,title:'Saved chat',workspaceId:ids[1]})}))).status,400);
    assert.equal((await create(req('/api/ui/sessions',params))).status,201);
  });
  await t.test('SSE completion is acknowledged in database before [DONE] reaches the browser',async()=>{
    let terminal=null;
    globalThis.fetch=async(input,init)=>{
      const u=new URL(input);
      assert.equal(init.headers.Authorization,'Bearer TOKEN_A');
      if(u.pathname===`/v1/ui/sessions/${firstSession}/turns`){
        assert.equal(JSON.parse(init.body).text,'Write a helper');
        return Response.json({id:turnId,created:true,state:'pending',messages:[{role:'user',content:'Write a helper'}]});
      }
      if(u.pathname==='/v1/chat/completions'){
        assert.deepEqual(JSON.parse(init.body).messages,[{role:'user',content:'Write a helper'}]);
        const s='data: {"choices":[{"delta":{"content":"Helper ready"},"finish_reason":null}]}\n\n'+
          'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n';
        return new Response(s,{headers:{'Content-Type':'text/event-stream','X-Potbot-Run-Id':'c'.repeat(32)}});
      }
      if(u.pathname===`/v1/ui/sessions/${firstSession}/turns/${turnId}/finish`){
        terminal=JSON.parse(init.body);
        assert.equal(terminal.status,'completed');
        assert.equal(terminal.assistant,'Helper ready');
        return Response.json({id:turnId,state:'completed'});
      }
      throw new Error('Unknown service path '+u.pathname);
    };
    const response=await send(req(`/api/ui/sessions/${firstSession}/send`,{method:'POST',headers:{cookie,origin,...scope(0),'content-type':'application/json'},
      body:JSON.stringify({workspaceId:ids[0],requestId,text:'Write a helper'})}),{params:Promise.resolve({sessionId:firstSession})});
    assert.equal(response.status,200);
    assert.equal(response.headers.get('X-Potbot-Session-Id'),firstSession);
    const result=await response.text();
    assert.match(result,/Helper ready/);assert.match(result,/\[DONE\]/);
    assert.ok(terminal);
  });
  await t.test('provider errors become indeterminate, and never send a success terminator',async()=>{
    let terminal;
    globalThis.fetch=async(input,init)=>{
      const u=new URL(input);
      if(u.pathname.endsWith('/turns')) return Response.json({id:turnId,created:true,state:'pending',messages:[{role:'user',content:'test'}]});
      if(u.pathname==='/v1/chat/completions') return new Response('event: error\ndata: {"error":"provider failed"}\n\ndata: [DONE]\n\n',
        {headers:{'Content-Type':'text/event-stream'}});
      if(u.pathname.endsWith('/finish')){ terminal=JSON.parse(init.body);return Response.json({state:'indeterminate'}); }
      throw new Error(u.pathname);
    };
    const result=await send(req(`/api/ui/sessions/${firstSession}/send`,{method:'POST',headers:{cookie,origin,...scope(0)},
      body:JSON.stringify({workspaceId:ids[0],requestId,text:'test'})}),{params:Promise.resolve({sessionId:firstSession})});
    const output=await result.text();
    assert.match(output,/event: error/);assert.doesNotMatch(output,/data: \[DONE\]/);
    assert.equal(terminal.status,'indeterminate');
  });
});
