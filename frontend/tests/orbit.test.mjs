import { test } from 'node:test';
import assert from 'node:assert/strict';
import { nextWorkspace, orbPosition, relativeOrbitIndex } from '../src/lib/orbit.ts';

test('orbit wraps in both directions and centres the chosen workspace', () => {
  assert.equal(nextWorkspace(0, -1, 4), 3);
  assert.equal(nextWorkspace(3, 1, 4), 0);
  assert.equal(relativeOrbitIndex(3, 0, 4), -1);
  for (let selected = 0; selected < 4; selected++) {
    const pos = orbPosition(selected, selected, 4, false);
    assert.deepEqual([pos.x, pos.y, pos.scale, pos.opacity], [0, 0, 1, 1]);
  }
});

test('other workspace orbs leave above the viewport when collapsed', () => {
  for (let index = 1; index < 8; index++) {
    const position = orbPosition(index, 0, 8, false);
    assert.equal(position.opacity, 0);
    assert.ok(position.y < -100);
    assert.ok(orbPosition(index, 0, 8, true).opacity > 0);
  }
});
