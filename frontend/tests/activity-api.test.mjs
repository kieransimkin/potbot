import { test } from 'node:test';
import assert from 'node:assert/strict';
import { POST as login } from '../src/app/api/ui/auth/login/route.ts';
import { GET as jobsRoute } from '../src/app/api/ui/activity/route.ts';
import { GET as modelsRoute } from '../src/app/api/ui/models/route.ts';

const origin = 'http://127.0.0.1:3100';
const workspaceId = '123e4567-e89b-12d3-a456-426614174000';
const jobId = '123e4567-e89b-12d3-a456-426614174001';
const defaults = {...process.env};
const env = { POTBOT_UI_PASSWORD: 'only-operator-has-this-password',
  POTBOT_UI_COOKIE_SECRET: 'z'.repeat(64), POTBOT_UI_PUBLIC_ORIGIN: origin,
  POTBOT_UI_BACKEND_URL: 'http://127.0.0.1:8000', POTBOT_UI_BACKEND_TOKEN: 'backend-secret',
  POTBOT_UI_WORKSPACE_ID: workspaceId };
const wrap = data => ({ schema_version: 1, request_id: workspaceId, error: null, data });
const request = (path, init) => new Request(origin + path, init);

test('Activity and model routes are authenticated, scoped and refuse raw backend data', async t => {
  Object.assign(process.env, env);
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; for (const k of Object.keys(env)) {
    if (k in defaults) process.env[k] = defaults[k]; else delete process.env[k];
  }});
  assert.equal((await jobsRoute(request('/api/ui/activity'))).status, 401);
  const auth = await login(request('/api/ui/auth/login', {method:'POST',
    headers:{origin,'Content-Type':'application/json'},body:JSON.stringify({password:env.POTBOT_UI_PASSWORD})}));
  assert.equal(auth.status, 200);
  const cookie = auth.headers.get('set-cookie').split(';')[0];
  await t.test('job status and cursor, not raw job input or private event payload', async () => {
    const calls=[];
    globalThis.fetch = async (url, init) => {
      calls.push(new URL(url).pathname + new URL(url).search);
      assert.equal(init.headers.Authorization,'Bearer '+env.POTBOT_UI_BACKEND_TOKEN);
      if (new URL(url).pathname === '/v1/jobs') return Response.json(wrap({items:[{
        id:jobId,episode_id:workspaceId,job_type:'model.generate',state:'running',
        progress:{stage:'generating',completed:2,total:5,secret:'hidden'},
        input:{prompt:'PRIVATE'},result:{credential:'PRIVATE'},
      }]}));
      if (new URL(url).pathname === '/v1/jobs/events') return Response.json(wrap({items:[{
        job_id:jobId,sequence:1,event_type:'job.progress',data:{output:'SECRET'}
      }],next_cursor:1,has_more:false}));
      throw new Error('unexpected path');
    };
    const res = await jobsRoute(request('/api/ui/activity?after=0',{headers:{cookie}}));
    assert.equal(res.status,200);
    const body = await res.json();
    assert.equal(body.workspaceId,workspaceId);
    assert.equal(body.cursor,1);
    assert.equal(body.jobs[0].completed,2);
    assert.equal(body.transferTelemetry,false);
    assert.equal(body.installTelemetry,false);
    assert.ok(!JSON.stringify(body).includes('PRIVATE'));
    assert.ok(!JSON.stringify(body).includes('SECRET'));
    assert.ok(!JSON.stringify(body).includes('backend-secret'));
    assert.deepEqual(calls,['/v1/jobs?limit=50','/v1/jobs/events?after=0&limit=100']);
    assert.equal((await jobsRoute(request('/api/ui/activity?after=foo',{headers:{cookie}}))).status,400);
    assert.equal((await jobsRoute(request('/api/ui/activity?workspace_id=other',{headers:{cookie}}))).status,400);
  });
  await t.test('read-only model catalogue distinguishes qualified from installed', async () => {
    globalThis.fetch = async (url) => {
      assert.equal(new URL(url).pathname,'/v1/models');
      return Response.json(wrap({local:[{
        canonical_model:'Qwen/Qwen3-0.6B', role:'reasoning', qualified:false,
        deployment_configured:false, private_path:'/mnt/private', access_token:'hidden',
      }]}));
    };
    const res=await modelsRoute(request('/api/ui/models',{headers:{cookie}}));
    assert.equal(res.status,200);
    const body=await res.json();
    assert.equal(body.advisory,true);
    assert.equal(body.installTelemetry,false);
    assert.deepEqual(body.models,[{ id:'Qwen/Qwen3-0.6B', role:'reasoning',
      qualified:false, configured:false, responseProtocol:null }]);
    assert.ok(!JSON.stringify(body).includes('/mnt/private'));
    assert.equal((await modelsRoute(request('/api/ui/models?provider=other',{headers:{cookie}}))).status,400);
  });
});
