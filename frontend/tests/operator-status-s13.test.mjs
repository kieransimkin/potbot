import assert from 'node:assert/strict';
import test from 'node:test';
import { projectOperatorStatus } from '../src/lib/operator-status.ts';
const ws = '55d15cd6-34fd-44d2-92e4-2e9331879994';
const record = {schema:'potbot-ui-operator/v1',workspaceId:ws,configured:true,healthy:true,
 journal:'healthy',releaseGate:'observed',pendingReleases:0,activeRuns:2,epoch:'a'.repeat(32)};
test('operator status projection retains only bounded public fields',()=>{
 assert.deepEqual(projectOperatorStatus({...record,secret:'should-not-show'},ws), record);
 assert.equal(projectOperatorStatus({...record,workspaceId:'other'},ws),null);
 assert.equal(projectOperatorStatus({...record,healthy:true,journal:'unavailable'},ws),null);
 assert.equal(projectOperatorStatus({...record,activeRuns:1e50},ws),null);
 assert.equal(projectOperatorStatus({...record,epoch:'bad'},ws),null);
});
