import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import { attachXpraCanvas, deliverXpraFrame, xpraFrameStats } from '../src/lib/xpra-frames.ts';
import { validXpraFrame, validXpraControl, validXpraWindow } from '../src/lib/xpra-contract.ts';

function bitmap(width, height) {
  const value = { width, height, closeCount: 0, close() { this.closeCount++; } };
  return value;
}
function frame(overrides = {}) {
  return { id: 'xwin-2a', generation: 1, sequence: 1, full: true,
    x: 0, y: 0, width: 100, height: 70, surfaceWidth: 100, surfaceHeight: 70,
    ...overrides };
}
function mockCanvas() {
  const draws = [];
  const context = { drawImage: (...args) => draws.push(args) };
  return { width: 640, height: 480, draws, getContext: () => context };
}

test('strict frame contract rejects out-of-bounds, mismatched bitmap and stale generations', () => {
  assert.ok(validXpraWindow({id:'xwin-2a',title:'XBill',width:100,height:70,x:0,y:0,generation:1}));
  assert.equal(validXpraWindow({id:'xwin-2a',title:'XBill',width:100,height:70,x:0,y:0}),null);
  assert.ok(validXpraFrame(frame(), bitmap(100,70)));
  assert.equal(validXpraFrame(frame({x:2}),bitmap(100,70)),null);
  assert.equal(validXpraFrame(frame({width:101}),bitmap(101,70)),null);
  assert.equal(validXpraFrame(frame({generation:0}),bitmap(100,70)),null);
  assert.equal(validXpraFrame(frame({surfaceWidth:1921}),bitmap(100,70)),null);
  assert.equal(validXpraFrame(frame({full:false,width:20,height:10,x:95}),bitmap(20,10)),null);
  assert.ok(validXpraControl({action:'visibility',visible:false}));
  assert.ok(!validXpraControl({action:'visibility',visible:1}));
});

test('frame consumer applies cropped damage and resyncs only after a full baseline', () => {
  const canvas = mockCanvas();
  const detach = attachXpraCanvas('xwin-2a', canvas, 1);
  const partial = frame({full:false,x:3,y:4,width:20,height:10,sequence:1});
  const ahead = bitmap(20,10);
  assert.equal(deliverXpraFrame('xwin-2a',ahead,partial),'resync');
  assert.equal(ahead.closeCount,1);
  const fullBitmap=bitmap(100,70);
  assert.equal(deliverXpraFrame('xwin-2a',fullBitmap,frame()),'applied');
  assert.equal(canvas.width,100);assert.equal(canvas.height,70);
  assert.deepEqual(canvas.draws[0],[fullBitmap,0,0]);
  const patch=bitmap(20,10);
  assert.equal(deliverXpraFrame('xwin-2a',patch,{...partial,sequence:2}),'applied');
  assert.deepEqual(canvas.draws[1],[patch,3,4]);
  const stale=bitmap(20,10);
  assert.equal(deliverXpraFrame('xwin-2a',stale,{...partial,sequence:1}),'stale');
  assert.equal(stale.closeCount,1);
  const wrongGeneration=bitmap(100,70);
  assert.equal(deliverXpraFrame('xwin-2a',wrongGeneration,frame({generation:3,sequence:40})),'stale');
  assert.equal(canvas.draws.length,2);
  const statistics=xpraFrameStats('xwin-2a');
  assert.equal(statistics?.displayed,2);
  assert.equal(statistics?.incremental,1);
  assert.equal(statistics?.estimatedDecodedPixelBytes,(100*70+20*10)*4);
  detach();
  assert.equal(deliverXpraFrame('xwin-2a',bitmap(100,70),frame({sequence:3})),'hidden');
});

test('reopening a minimized canvas demands a fresh full frame before damage', () => {
  const first=mockCanvas(), close=attachXpraCanvas('xwin-2a',first,1);
  assert.equal(deliverXpraFrame('xwin-2a',bitmap(100,70),frame()),'applied');
  close();
  const second=mockCanvas(), closeSecond=attachXpraCanvas('xwin-2a',second,1);
  assert.equal(deliverXpraFrame('xwin-2a',bitmap(20,10),frame({full:false,x:2,y:2,width:20,height:10,sequence:2})),'resync');
  assert.equal(second.draws.length,0);
  assert.equal(deliverXpraFrame('xwin-2a',bitmap(100,70),frame({sequence:3})),'applied');
  closeSecond();
});

test('pinned Xpra adapter sends only cropped windows, throttles correctly and controls remain native',async()=>{
  const source = fs.readFileSync(new URL('../vendor/xpra-html5/potbot-window-bridge.js',import.meta.url),'utf8');
  const origin='http://127.0.0.1:3100', channel='a'.repeat(32);
  let clock=1000;
  const captured=[],messages=[],forwarded=[],handlers=new Map();
  const parent={postMessage:(m,domain)=>{assert.equal(domain,origin);messages.push(m);}};
  class Window {
    resize(w,h){this.w=w;this.h=h;this.canvas.width=w;this.canvas.height=h;}
    do_paint(_packet, callback){callback();}
    draw(){return true;}
  }
  const createWindow=wid=>Object.assign(new Window(),{wid,title:'XBill',w:100,h:70,x:0,y:0,
    canvas:{width:100,height:70,getBoundingClientRect:()=>({left:0,top:0})},
    resize_counter:0});
  let win=createWindow(42);
  class Client {
    id_to_window={};connected=true;server_is_desktop=false;
    _new_window(wid){win.client=this;this.id_to_window[wid]=win;}
    _process_lost_window(p){delete this.id_to_window[p[1]];}
    _process_window_metadata(){}
    set_focus(w){forwarded.push(['focus',w.wid]);}
    release_keys(){forwarded.push(['blur']);}
    _keyb_onkeydown(e){forwarded.push(['key-down',e.key]);}
    _keyb_onkeyup(e){forwarded.push(['key-up',e.key]);}
    on_mousemove(_e,w){forwarded.push(['pointer',w.wid]);}
    on_mousedown(_e,w){forwarded.push(['button',w.wid]);}
    on_mouseup(_e,w){forwarded.push(['release',w.wid]);}
    on_mousescroll(_e,w){forwarded.push(['wheel',w.wid]);}
    send_configure_window(w){forwarded.push(['resize',w.w,w.h]);}
  }
  const timers=new Map();let nextTimer=0;
  const fakeCreate=async (_canvas,x,y,w,h) => {
    captured.push([x,y,w,h]);
    return bitmap(w,h);
  };
  const sandbox={URLSearchParams,location:{search:`?potbot_channel=${channel}`,origin},
    window:{parent,addEventListener:(name,fn)=>handlers.set(name,fn)},performance:{now:()=>clock},
    Number,Math,Map,Set,Promise,Utilities:{s:x=>x},XpraClient:Client,XpraWindow:Window,
    createImageBitmap:fakeCreate,
    setTimeout:(fn,_ms)=>{timers.set(++nextTimer,fn);return nextTimer;},
    clearTimeout:(id)=>timers.delete(id),
    KeyboardEvent:class{constructor(type,props){Object.assign(this,props);this.type=type;}},
    MouseEvent:class{constructor(type,props){Object.assign(this,props);this.type=type;}},
    WheelEvent:class{constructor(type,props){Object.assign(this,props);this.type=type;}},
  };
  vm.runInNewContext(source,sandbox);
  const client=new Client();sandbox.window.PotbotXpraBridge.bindClient(client);
  client._new_window(42);
  assert.equal(messages.at(-1).generation,1);
  const send=(action,extras={},id='xwin-2a')=>handlers.get('message')({source:parent,origin,
    data:{type:'potbot:xpra-control',channel,id,action,...extras}});
  const flush=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};
  send('visibility',{visible:true});await flush();
  assert.equal(messages.at(-1).action,'frame');
  assert.equal(messages.at(-1).full,true);
  assert.deepEqual(captured.at(-1),[0,0,100,70]);
  clock+=45;
  win.do_paint(['draw',42,5,7,20,10,'rgb32'],()=>{});
  win.do_paint(['draw',42,18,12,10,12,'rgb32'],()=>{});
  win.draw();await flush();
  assert.deepEqual(captured.at(-1),[5,7,23,17], 'adjacent damage is coalesced');
  assert.equal(messages.at(-1).full,false);
  assert.equal(messages.at(-1).sequence,2);
  clock += 45;
  win.do_paint(['draw',42,1,2,5,5,'scroll'],()=>{});
  win.draw();await flush();
  assert.deepEqual(captured.at(-1),[0,0,100,70], 'scroll invalidates full window');
  send('key',{key:'a',code:'KeyA',pressed:true});
  send('pointer',{x:4,y:3});
  assert.ok(forwarded.some(x=>x[0]==='key-down'));
  assert.ok(forwarded.some(x=>x[0]==='pointer'));
  send('visibility',{visible:false});
  clock+=45;
  win.do_paint(['draw',42,10,10,8,8,'rgb32'],()=>{});win.draw();await flush();
  assert.equal(captured.length,3,'minimized window must not copy decoded frames');
  send('visibility',{visible:true});await flush();
  assert.deepEqual(captured.at(-1),[0,0,100,70],'restoring uses complete frame');
  client._process_lost_window(['lost',42]);
  assert.equal(messages.at(-1).action,'close');
  assert.equal(messages.at(-1).generation,1);
  assert.equal(timers.size,0);
  // Numeric window ID reuse increments generation and rejects delayed old data.
  win=createWindow(42);
  client._new_window(42);
  assert.equal(messages.at(-1).generation,2);
  const before=forwarded.length;
  handlers.get('message')({source:parent,origin:'https://evil.test',data:{type:'potbot:xpra-control',channel,id:'xwin-2a',action:'focus'}});
  assert.equal(forwarded.length,before);
});

test('lost incremental frame forces full resync rather than corrupting the canvas', () => {
  const canvas=mockCanvas();
  const release=attachXpraCanvas('xwin-2a',canvas,7);
  try {
    assert.equal(deliverXpraFrame('xwin-2a',bitmap(100,70),frame({generation:7,sequence:10})),'applied');
    const dropped=bitmap(20,10);
    assert.equal(deliverXpraFrame('xwin-2a',dropped,frame({generation:7,sequence:12,
      full:false,x:3,y:4,width:20,height:10})),'resync');
    assert.equal(dropped.closeCount,1);
    assert.equal(canvas.draws.length,1);
    assert.equal(deliverXpraFrame('xwin-2a',bitmap(100,70),frame({generation:7,sequence:13})),'applied');
    assert.equal(deliverXpraFrame('xwin-2a',bitmap(20,10),frame({generation:7,sequence:14,
      full:false,x:3,y:4,width:20,height:10})),'applied');
  } finally {release();}
});

test('frame pacing and input-to-next-frame stats stay bounded and are not inferred as acknowledgements',async()=>{
  const {markXpraInput}=await import('../src/lib/xpra-frames.ts');
  const canvas=mockCanvas();
  const release=attachXpraCanvas('xwin-2a',canvas,8);
  const native=globalThis.performance;
  let time=1000;
  Object.defineProperty(globalThis,'performance',{value:{now:()=>time},configurable:true});
  try {
    assert.equal(deliverXpraFrame('xwin-2a',bitmap(100,70),frame({generation:8,sequence:1})),'applied');
    time+=15;
    markXpraInput('xwin-2a');
    time+=27;
    assert.equal(deliverXpraFrame('xwin-2a',bitmap(20,10),frame({generation:8,sequence:2,
      full:false,x:5,y:6,width:20,height:10})),'applied');
    const stats=xpraFrameStats('xwin-2a');
    assert.equal(stats?.p95FrameIntervalMs,42);
    assert.equal(stats?.medianInputToNextFrameMs,27);
    assert.equal(stats?.inputSamples,1);
    time+=3000;
    assert.equal(xpraFrameStats('xwin-2a')?.fps,0);
    assert.equal(xpraFrameStats('xwin-2a')?.lastFrameAgeMs,3000);
  } finally {release();Object.defineProperty(globalThis,'performance',{value:native,configurable:true});}
});
