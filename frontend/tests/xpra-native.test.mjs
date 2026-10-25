import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { XWINDOW_ID, windowPanelId, validXpraWindow, xpraFrameURL,
  normalizedPointerPosition, validXpraControl } from '../src/lib/xpra-contract.ts';
import { xpraBridgeConfig } from '../src/lib/server/xpra.ts';
import { POST as issueTicket } from '../src/app/api/ui/xpra/ticket/route.ts';

const origin = 'http://127.0.0.1:3100';
const workspaceId = '123e4567-e89b-12d3-a456-426614174000';
const token = 'a'.repeat(48);
const ticket = 'B'.repeat(43);
const channel = 'a'.repeat(32);
const environment = {
  POTBOT_UI_PASSWORD: 'private-Potbot-password',
  POTBOT_UI_COOKIE_SECRET: 'b'.repeat(64),
  POTBOT_UI_PUBLIC_ORIGIN: origin,
  POTBOT_UI_BACKEND_URL: 'http://127.0.0.1:8000',
  POTBOT_UI_BACKEND_TOKEN: 'backend-only',
  POTBOT_UI_WORKSPACE_ID: workspaceId,
  POTBOT_UI_XPRA_BRIDGE_URL: 'http://127.0.0.1:8765/',
  POTBOT_UI_XPRA_BRIDGE_TOKEN: token,
  POTBOT_UI_XPRA_PUBLIC_WS_URL: 'ws://127.0.0.1:8765/v1/xpra/ws',
};
const savedEnv = Object.fromEntries(Object.keys(environment).map(k => [k, process.env[k]]));

test('native client URLs reject external redirects and carry only short-lived tickets', () => {
  const ws = `ws://127.0.0.1:8765/v1/xpra/ws?ticket=${ticket}`;
  const path = xpraFrameURL(ws, channel);
  const parsed = new URL(path, origin);
  assert.equal(parsed.searchParams.get('potbot_channel'), channel);
  assert.equal(parsed.searchParams.get('path'), `/v1/xpra/ws?ticket=${ticket}`);
  assert.equal(parsed.searchParams.get('server'), '127.0.0.1');
  assert.throws(() => xpraFrameURL(`ws://evil.test/v1/xpra/ws?ticket=${ticket}`, channel));
  assert.throws(() => xpraFrameURL('ws://localhost:8765/v1/xpra/ws?ticket=short', channel));
  assert.throws(() => xpraFrameURL(ws, 'invalid-channel'));
  assert.equal(windowPanelId('xwin-a2'), 'x11:xwin-a2');
  assert.ok(XWINDOW_ID.test('xwin-abcdef'));
  assert.equal(validXpraWindow({id:'xwin-a2', title:'XBill',width:640,height:480,x:1,y:2})?.title,'XBill');
  assert.equal(validXpraWindow({id:'xwin-a2',title:'XBill',width:100000,height:480,x:1,y:2}),null);
  assert.ok(validXpraControl({ action:'blur' }));
  assert.ok(!validXpraControl({ action:'resize',width:99999,height:20 }));
  const p = normalizedPointerPosition(50, 50,{left:0,top:0,width:100,height:100},200,100);
  assert.deepEqual(p,{x:100,y:50});
});

test('BFF issues only operator-scoped single-use capabilities with origin validation', async t => {
  for (const [k,v] of Object.entries(environment)) process.env[k]=v;
  const originalFetch=globalThis.fetch;
  t.after(()=>{globalThis.fetch=originalFetch; for(const [k,v] of Object.entries(savedEnv)){if(v===undefined)delete process.env[k];else process.env[k]=v;}});
  assert.equal(xpraBridgeConfig()?.workspaceId,workspaceId);
  assert.equal(xpraBridgeConfig({...environment,POTBOT_UI_XPRA_PUBLIC_WS_URL:'ws://evil.test/v1/xpra/ws'}),null);
  // Authenticate via the UI session service, not a browser-visible API token.
  const { POST:login }=await import('../src/app/api/ui/auth/login/route.ts');
  const auth = await login(new Request(origin+'/api/ui/auth/login',{method:'POST',headers:{origin,'Content-Type':'application/json'},body:JSON.stringify({password:environment.POTBOT_UI_PASSWORD})}));
  assert.equal(auth.status,200);
  const cookie=auth.headers.get('set-cookie').split(';')[0];
  let called=0;
  globalThis.fetch=async(_url,opts)=>{called++;assert.equal(opts.headers.Authorization,`Bearer ${token}`);return Response.json({workspaceId,ticket,expiresInSeconds:30});};
  const bad=await issueTicket(new Request(origin+'/api/ui/xpra/ticket',{method:'POST',headers:{origin:'https://malicious.example',cookie}}));
  assert.equal(bad.status,403);assert.equal(called,0);
  const good=await issueTicket(new Request(origin+'/api/ui/xpra/ticket',{method:'POST',headers:{origin,cookie}}));
  assert.equal(good.status,200);
  const data=await good.json();
  assert.equal(data.transport,'xpra-native-binary');
  assert.equal(data.workspaceId,workspaceId);
  assert.equal(new URL(data.wsUrl).searchParams.get('ticket'),ticket);
  assert.ok(!JSON.stringify(data).includes(token));
  assert.equal(called,1);
});

test('window-only Xpra JS adapter forwards decoded frames and routes input to upstream methods',async()=>{
  const source=fs.readFileSync(new URL('../vendor/xpra-html5/potbot-window-bridge.js',import.meta.url),'utf8');
  const messages=[];const forwarded=[];const handlers=new Map();
  const parent={postMessage:(data,origin,transfer)=>messages.push({data,origin,transfer})};
  const xpraWindow={wid:42,title:'XBill',w:480,h:320,x:1,y:2,
    canvas:{ getBoundingClientRect:()=>({left:0,top:0}) },resize_counter:0,
    resize(w,h){this.w=w;this.h=h;},};
  class XpraClient {
    id_to_window={};connected=true;
    _new_window(wid){xpraWindow.client=this;this.id_to_window[wid]=xpraWindow;return 'created';}
    _process_lost_window(packet){delete this.id_to_window[packet[1]];}
    _process_window_metadata(_packet){return 'metadata';}
    set_focus(w){forwarded.push(['focus',w.wid]);}
    release_keys(){forwarded.push(['blur']);}
    _keyb_onkeydown(e){forwarded.push(['keydown',e.key]);}
    _keyb_onkeyup(e){forwarded.push(['keyup',e.key]);}
    on_mousemove(_e,w){forwarded.push(['pointer',w.wid]);}
    on_mousedown(_e,w){forwarded.push(['button-down',w.wid]);}
    on_mouseup(_e,w){forwarded.push(['button-up',w.wid]);}
    on_mousescroll(_e,w){forwarded.push(['wheel',w.wid]);}
    send_configure_window(w){forwarded.push(['resize',w.w,w.h]);}
  }
  class XpraWindow {draw(){return 1;}}
  const mockBitmap={width:480,height:320,close:()=>{}};
  const sandbox={ URLSearchParams,location:{search:'?potbot_channel='+channel,origin},
    window:{parent,addEventListener:(name,fn)=>handlers.set(name,fn)},
    performance:{now:()=>1000},Map,Set,Number,Math,
    XpraClient,XpraWindow,createImageBitmap:async()=>mockBitmap,
    KeyboardEvent:class{constructor(type,options){Object.assign(this,options);this.type=type;}},
    MouseEvent:class{constructor(type,options){Object.assign(this,options);this.type=type;}},
    WheelEvent:class{constructor(type,options){Object.assign(this,options);this.type=type;}} };
  sandbox.window.parent=parent;
  sandbox.window.XpraClient=XpraClient;
  vm.runInNewContext(source,sandbox);
  const client=new XpraClient();
  sandbox.window.PotbotXpraBridge.bindClient(client);
  client._new_window(42);
  assert.equal(messages.at(-1).data.id,'xwin-2a');
  assert.equal(messages.at(-1).data.action,'open');
  xpraWindow.client=client;
  const renderer=new XpraWindow();Object.assign(renderer,xpraWindow);
  client.id_to_window[42]=renderer;
  renderer.draw();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(messages.at(-1).data.action,'frame');
  assert.equal(messages.at(-1).data.bitmap,mockBitmap);
  const dispatch=(id,action,extra={})=>handlers.get('message')({source:parent,origin,data:{type:'potbot:xpra-control',channel,id,action,...extra}});
  dispatch('xwin-2a','focus'); dispatch('xwin-2a','key',{key:'x',code:'KeyX',pressed:true});
  dispatch('xwin-2a','key',{key:'x',code:'KeyX',pressed:false});
  dispatch('xwin-2a','pointer',{x:10,y:20});
  dispatch('xwin-2a','button',{x:10,y:20,button:1,pressed:true});
  dispatch('xwin-2a','wheel',{x:10,y:20,deltaY:45});
  dispatch('xwin-2a','resize',{width:600,height:410});
  dispatch('xwin-2a','blur');
  assert.deepEqual(forwarded.map(x=>x[0]),['focus','focus','keydown','focus','keyup','pointer','button-down','wheel','resize','blur']);
  assert.deepEqual(forwarded.find(x=>x[0]==='resize'),['resize',600,410]);
  handlers.get('message')({source:parent,origin:'https://evil.test',data:{type:'potbot:xpra-control',channel,id:'xwin-2a',action:'focus'}});
  assert.equal(forwarded.length,10);
  client._process_lost_window(['lost',42]);
  assert.equal(messages.at(-1).data.action,'close');
});
