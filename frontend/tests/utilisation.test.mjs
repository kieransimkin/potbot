import test from 'node:test';
import assert from 'node:assert/strict';
import { projectUtilisation } from '../src/lib/utilisation.ts';

const ID = '11111111-1111-4111-8111-111111111111';
const example = () => ({
  schema: 1, available: true,
  system: { load3m: .44, load2s: .80, memoryUsed: .33, logicalCpus: 8,
    pressure: { cpu: { avg10: 1.2 }, io: {}, memory: { avg10: 5.4 } } },
  agents: [{ agentId: ID, heat: .08 }],
});

test('accepts bounded current operator telemetry', () => {
  const result = projectUtilisation(example());
  assert.equal(result?.system.load3m, .44);
  assert.deepEqual(result?.agents, [{ agentId: ID, heat: .08 }]);
});

test('never forwards raw paths, PIDs or unrecognized process fields', () => {
  const payload = example();
  payload.secret_path = '/etc/shadow';
  payload.workers = [{ pid: 10, private: '/home/agent/passwords' }];
  const result = projectUtilisation(payload);
  assert.ok(result);
  assert.equal(JSON.stringify(result).includes('/etc/shadow'), false);
  assert.equal(JSON.stringify(result).includes('passwords'), false);
});

test('rejects bad heat shares, duplicate agents, invalid system readings', () => {
  const negative = example();
  negative.agents[0].heat = -1;
  assert.equal(projectUtilisation(negative), null);
  const duplicate = example();
  duplicate.agents.push({ ...duplicate.agents[0] });
  assert.equal(projectUtilisation(duplicate), null);
  const bad = example();
  bad.system.load3m = Infinity;
  assert.equal(projectUtilisation(bad), null);
});
