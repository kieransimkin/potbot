import {test} from 'node:test';
import assert from 'node:assert/strict';
import {authConfig,sessionCookie,authenticated,verifyPassword,validOrigin} from '../src/lib/server/security.ts';
import {potbotConfig,upstream,configuredWorkspace} from '../src/lib/server/potbot.ts';
const env={POTBOT_UI_PASSWORD:'correct horse battery staple',POTBOT_UI_COOKIE_SECRET:'s'.repeat(64),POTBOT_UI_PUBLIC_ORIGIN:'http://127.0.0.1:3100'};
const backend={POTBOT_UI_BACKEND_TOKEN:'scoped-secret',POTBOT_UI_WORKSPACE_ID:'123e4567-e89b-12d3-a456-426614174000',POTBOT_UI_BACKEND_URL:'http://127.0.0.1:9000'};

test('server secrets never enter workspace bootstrap, sessions expire and are MAC verified',()=>{
  const cfg=authConfig(env);assert.ok(cfg);
  assert.equal(verifyPassword('wrong',cfg),false);
  assert.equal(verifyPassword(env.POTBOT_UI_PASSWORD,cfg),true);
  const cookie=sessionCookie(cfg,Date.UTC(2026,9,25,12));
  assert.ok(cookie.includes('HttpOnly'));
  const parsed=cookie.split(';')[0];
  assert.ok(authenticated(parsed,cfg,Date.UTC(2026,9,25,13)));
  assert.equal(authenticated(parsed.replace(/.$/,'x'),cfg,Date.UTC(2026,9,25,13)),false);
  assert.equal(authenticated(parsed,cfg,Date.UTC(2026,9,26,3)),false);
  const ws=configuredWorkspace(potbotConfig(backend));
  assert.equal(JSON.stringify(ws).includes('scoped-secret'),false);
});

test('origin check fails closed and backend URL cannot redirect to external servers', async()=>{
  const cfg=authConfig(env);
  assert.ok(validOrigin(new Request('http://127.0.0.1:3100/api', {method:'POST',headers:{origin:cfg.publicOrigin}}),cfg));
  assert.equal(validOrigin(new Request('http://127.0.0.1:3100/api',{method:'POST',headers:{origin:'https://attacker.test'}}),cfg),false);
  assert.equal(potbotConfig({...backend,POTBOT_UI_BACKEND_URL:'https://example.com'}),null);
  assert.equal(potbotConfig({...backend,POTBOT_UI_BACKEND_URL:'http://127.0.0.1:9000/sneaky'}),null);
});
