import assert from 'node:assert/strict';
import test from 'node:test';

import { authConfig, sessionCookie } from '../src/lib/server/security.ts';
import { galleryList, galleryPart } from '../src/lib/server/browser-evidence.ts';
import { browserLeaseProxy } from '../src/lib/server/xpra-leases.ts';

const workspace = '00000000-0000-4000-8000-000000000021';
const invocation = '00000000-0000-4000-8000-000000000031';
const session = 'a'.repeat(32);

function configure() {
  Object.assign(process.env, {
    POTBOT_UI_PASSWORD: 'definitely-a-test-password',
    POTBOT_UI_COOKIE_SECRET: 's'.repeat(64),
    POTBOT_UI_PUBLIC_ORIGIN: 'http://localhost:3100',
    POTBOT_UI_WORKSPACE_ID: workspace,
    POTBOT_UI_BACKEND_URL: 'http://127.0.0.1:8760/',
    POTBOT_UI_BACKEND_TOKEN: 'z'.repeat(48),
    POTBOT_UI_XPRA_WORKSPACE_ID: workspace,
    POTBOT_UI_XPRA_BRIDGE_URL: 'http://127.0.0.1:8765/',
    POTBOT_UI_XPRA_BRIDGE_TOKEN: 'a'.repeat(48),
    POTBOT_UI_XPRA_PUBLIC_WS_URL: 'ws://127.0.0.1:8765/v1/xpra/ws',
  });
  return sessionCookie(authConfig()).split(';')[0];
}

function request(url, { cookie = configure(), ws = workspace, method = 'GET', body } = {}) {
  return new Request(`http://localhost:3100${url}`, {
    method,
    headers: {cookie, 'x-potbot-workspace': ws,
      origin: 'http://localhost:3100',
      ...(body ? {'content-type':'application/json'} : {})},
    ...(body ? {body:JSON.stringify(body)} : {}),
  });
}

test('gallery denies absent login, foreign workspace and unsupported queries', async () => {
  configure();
  assert.equal((await galleryList(request('/api/ui/browser/evidence', {cookie:''}))).status, 401);
  assert.equal((await galleryList(request('/api/ui/browser/evidence', {ws:'other'}))).status, 404);
  assert.equal((await galleryList(request('/api/ui/browser/evidence?before=2&before=3'))).status, 400);
  assert.equal((await galleryList(request('/api/ui/browser/evidence?limit=999'))).status, 400);
});

test('gallery BFF only projects scoped bounded upstream evidence', async () => {
  configure();
  const before = globalThis.fetch;
  let target = '';
  globalThis.fetch = async (url) => {
    target = String(url);
    return Response.json({schema_version:1,error:null,data:{schemaVersion:1,items:[{
      id:invocation,sessionId:session,kind:'capture',capturedUnixNs:'12345',
      manifestSha256:'a'.repeat(64),observationSha256:'b'.repeat(64),hasImage:true,
    }],nextCursor:null}});
  };
  try {
    const response = await galleryList(request('/api/ui/browser/evidence?limit=1'));
    assert.equal(response.status, 200);
    assert.equal((await response.json()).items[0].id, invocation);
    assert.ok(target.includes('/v1/browser/evidence?limit=1'));
    assert.equal(response.headers.get('cache-control'), 'private, no-store');
  } finally { globalThis.fetch = before; }
});

test('gallery rejects malformed timestamp and screenshot references', async () => {
  configure();
  const previous = globalThis.fetch;
  globalThis.fetch = async () => Response.json({schema_version:1,error:null,data:{schemaVersion:1,
    items:[{id:invocation,sessionId:session,kind:'capture',capturedUnixNs:'not-a-number',
      manifestSha256:'a'.repeat(64),observationSha256:'b'.repeat(64),hasImage:true}],nextCursor:null}});
  try {
    assert.equal((await galleryList(request('/api/ui/browser/evidence'))).status, 503);
  } finally {globalThis.fetch=previous;}
});

test('evidence binary route checks MIME and image framing', async () => {
  configure();
  const before = globalThis.fetch;
  globalThis.fetch = async () => new Response(new Uint8Array([255,216,255,7,255,217]), {
    headers:{'Content-Type':'image/jpeg'},
  });
  try {
    const response = await galleryPart(request(`/api/ui/browser/evidence/${invocation}/image`), invocation, 'image');
    assert.equal(response.status, 200);
    assert.equal((await response.arrayBuffer()).byteLength, 6);
    assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
    assert.equal((await galleryPart(request(`/api/ui/browser/evidence/${invocation}/html`), invocation,'html')).status, 400);
    globalThis.fetch = async () => new Response('bad', {headers:{'Content-Type':'text/html'}});
    assert.equal((await galleryPart(request(`/api/ui/browser/evidence/${invocation}/image`), invocation,'image')).status, 503);
  } finally { globalThis.fetch = before; }
});

test('browser reservation API never accepts invented token or changes another workspace', async () => {
  configure();
  const url = `/api/ui/xpra/apps/${session}/lease`;
  assert.equal((await browserLeaseProxy(request(url, {ws:'other',method:'POST',body:{action:'acquire'}}),session)).status, 404);
  assert.equal((await browserLeaseProxy(request(url, {method:'POST',body:{action:'renew',token:'short'}}),session)).status, 400);
  const before = globalThis.fetch;
  globalThis.fetch = async () => Response.json({workspaceId:workspace,token:'p'.repeat(43),
    reservation:{sessionId:session,reserved:true,holder:'operator',generation:1,expiresInSeconds:60}});
  try {
    const response = await browserLeaseProxy(request(url, {method:'POST',body:{action:'acquire'}}),session);
    assert.equal(response.status, 200);
    assert.equal((await response.json()).reservation.holder,'operator');
  } finally {globalThis.fetch=before;}
});
