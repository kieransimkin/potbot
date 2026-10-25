import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readChatStream } from '../src/lib/chat-stream.ts';

function chunks(parts) {
  return new ReadableStream({start(c){for (const x of parts)c.enqueue(new TextEncoder().encode(x));c.close();}});
}

test('SSE parser handles fragmented chunks and completion', async () => {
  const events=[];
  await readChatStream(chunks([
    'data: {"choices":[{"delta":{"content":"Hel',
    'lo"}}]}\r\n\r\n',
    'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
    'data: [DONE]\n\n',
  ]),e=>events.push(e));
  assert.deepEqual(events,[{type:'delta',text:'Hello'},{type:'delta',text:' world'},{type:'complete'}]);
});

test('EOF without DONE is indeterminate and is never a successful completion', async () => {
  const events=[];
  await readChatStream(chunks(['data: {"choices":[{"delta":{"content":"unfinished"}}]}\n\n']),e=>events.push(e));
  assert.equal(events[0].text,'unfinished');
  assert.equal(events.at(-1).type,'error');
  assert.match(events.at(-1).message,/uncertain/);
});

test('provider error is not reinterpreted as success when followed by DONE', async () => {
  const events=[];
  await readChatStream(chunks(['event: error\ndata: {"error":{"message":"secret upstream"}}\n\n','data: [DONE]\n\n']),e=>events.push(e));
  assert.equal(events.length,1);
  assert.equal(events[0].type,'error');
  assert.ok(!JSON.stringify(events).includes('secret upstream'));
});

test('A token-limited finish is not reported as a successful completion', async () => {
  const events=[];
  await readChatStream(chunks(['data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n', 'data: [DONE]\n\n']),e=>events.push(e));
  assert.equal(events.length,1);
  assert.equal(events[0].type,'error');
});
