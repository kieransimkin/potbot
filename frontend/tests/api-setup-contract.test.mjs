import test from 'node:test';
import assert from 'node:assert/strict';
import { projectApiSetup, projectSetupProvider, setupCapability } from '../src/lib/server/api-setup.ts';

const labels = {openai:'OpenAI',anthropic:'Anthropic (Claude)',gemini:'Google Gemini',xai:'xAI (Grok)',evolink:'EvoLink'};
const vars = {openai:'OPENAI_API_KEY',anthropic:'ANTHROPIC_API_KEY',gemini:'GEMINI_API_KEY',xai:'XAI_API_KEY',evolink:'EVOLINK_API_KEY'};
function entry(id) { return { id,label:labels[id], configured:false,status:'missing',source:null,editable:true,environmentVariable:vars[id] }; }
function sample() { return {schema:'potbot-api-setup/v1',scope:'operator_credential_profile',profile:'default',storage:'native_os_credential_vault',secretsExposed:false,providers:Object.keys(labels).map(entry)}; }
const workspaceId = '11111111-1111-4111-8111-111111111111';

test('strict status projection permits exact five providers, never raw extra fields',()=>{
  const data = sample(); data.providers[0].privateKey = 'do-not-return';
  const projected = projectApiSetup(data,workspaceId);
  assert.equal(projected?.providers.length,5);
  assert.equal(JSON.stringify(projected).includes('do-not-return'),false);
  assert.equal(projected?.workspaceId,workspaceId);
});

test('status projection rejects duplicated/missing providers and fake storage',()=>{
  const data = sample(); data.providers[1]=data.providers[0];
  assert.equal(projectApiSetup(data,workspaceId),null);
  data.providers=Object.keys(labels).map(entry); data.storage='insecure_file';
  assert.equal(projectApiSetup(data,workspaceId),null);
});

test('provider projection refuses unknown provider ID and altered environment mapping',()=>{
  assert.equal(projectSetupProvider({...entry('openai'),id:'malicious'}),null);
  assert.equal(projectSetupProvider({...entry('openai'),environmentVariable:'POTBOT_API_SETUP_TOKEN'}),null);
  assert.equal(projectSetupProvider({...entry('openai'),source:'secret'}),null);
});

test('server-side setup capability validation rejects unsafe values',()=>{
  assert.equal(setupCapability({POTBOT_UI_SETUP_TOKEN:'private-'+ 'x'.repeat(50)}),'private-'+ 'x'.repeat(50));
  for(const key of ['', 'short', 'line\nbreak', 'unsafe key', 'x'.repeat(4097)])
    assert.equal(setupCapability({POTBOT_UI_SETUP_TOKEN:key}),null);
});
