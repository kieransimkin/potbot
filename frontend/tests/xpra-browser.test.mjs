import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const root = new URL('../src/', import.meta.url);
const proxy = readFileSync(new URL('lib/server/xpra-browser.ts', root), 'utf8');
const route = readFileSync(new URL('app/api/ui/xpra/apps/[sessionId]/browser/route.ts', root), 'utf8');
const inspector = readFileSync(new URL('components/BrowserInspector.tsx', root), 'utf8');

test('browser proxy is operator scoped and same origin for all commands', () => {
  assert.match(proxy, /xpraAppScope\(request, true\)/);
  assert.match(proxy, /appSessionId\(sessionId\)/);
  assert.match(proxy, /boundedJSON\(request, 4096\)/);
  assert.match(proxy, /cache: "no-store"/);
  assert.match(route, /browserProxy\(request, sessionId\)/);
  assert.doesNotMatch(proxy, /Runtime\.evaluate|webSocketDebuggerUrl/);
});

test('native element picker and ARIA exploration are wired into browser pane', () => {
  assert.match(inspector, /"inspect\.start"/);
  assert.match(inspector, /"inspect\.poll"/);
  assert.match(inspector, /"accessibility"/);
  assert.match(inspector, /"interactive"/);
  assert.match(inspector, /"role\.click"/);
  assert.match(inspector, /"sample"/);
  assert.match(inspector, /"x-potbot-workspace": workspace\.id/);
});
