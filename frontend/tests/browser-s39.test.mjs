import { test } from 'node:test';
import assert from 'node:assert/strict';
import { projectAppInventory } from '../src/lib/xpra-apps.ts';
const workspaceId = '00000000-0000-4000-8000-000000000001';
const inventory = {workspaceId, configured:true, choices:[], sessions:[]};
test('S39 readonly Xpra input metadata is projected', () => {
  assert.equal(projectAppInventory({...inventory, xpraReadOnly:true}, workspaceId)?.xpraReadOnly, true);
  assert.equal(projectAppInventory({...inventory, xpraReadOnly:false}, workspaceId)?.xpraReadOnly, false);
});
test('S39 malformed readonly metadata is rejected', () => {
  assert.equal(projectAppInventory({...inventory, xpraReadOnly:'yes'}, workspaceId), null);
});
test('S39 old inventory remains compatible', () => {
  assert.equal(projectAppInventory(inventory, workspaceId)?.xpraReadOnly, undefined);
});
