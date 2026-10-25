import { test } from 'node:test';
import assert from 'node:assert/strict';
import { layoutStorageKey, decodeSavedLayout, shouldMinimizePanel, PANEL_TITLES } from '../src/lib/layout.ts';

test('layout persistence is versioned and strictly workspace-specific', () => {
  const raw = JSON.stringify({ version: 1, workspaceId: 'workspace_a', dock: { grid: {} }, graphMinimized: true, savedAt: 1700000000 });
  assert.equal(decodeSavedLayout(raw, 'workspace_a')?.graphMinimized, true);
  assert.equal(decodeSavedLayout(raw, 'workspace_b'), null);
  assert.equal(decodeSavedLayout(raw.replace('"version":1', '"version":9'), 'workspace_a'), null);
  assert.equal(decodeSavedLayout('{not json', 'workspace_a'), null);
  assert.notEqual(layoutStorageKey('workspace_a'), layoutStorageKey('workspace_b'));
  assert.throws(() => layoutStorageKey('../escape'));
});

test('chat is permanent and other panels are restorable', () => {
  assert.ok(shouldMinimizePanel('graph'));
  assert.ok(shouldMinimizePanel('menu'));
  assert.ok(shouldMinimizePanel('terminal'));
  assert.ok(!shouldMinimizePanel('chat'));
  assert.equal(PANEL_TITLES.graph, 'Thought graph');
});
