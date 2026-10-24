"""Self-contained queue dashboard assets, with no third-party scripts or storage."""

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Potbot · Jobs</title><link rel="stylesheet" href="/assets/dashboard.css">
<script src="/assets/dashboard.js" defer></script></head>
<body><header><div><p class="eyebrow">POTBOT</p><h1>Jobs &amp; models</h1></div>
<div id="connection" role="status">Disconnected</div></header>
<main><section class="connection card"><h2>Connect to your workspace</h2>
<form id="connect-form"><label>Access token<input id="token" type="password" autocomplete="off" required></label>
<button type="submit">Connect</button><button type="button" id="disconnect">Disconnect</button></form>
<p class="muted">The token stays in this tab's memory. Closing or disconnecting clears it.</p>
<p id="notice" role="status" aria-live="polite"></p></section>
<section class="card"><div class="section-heading"><h2>Queue</h2><button id="refresh">Refresh</button></div>
<div id="queue" class="metrics"><span class="muted">Connect to see the current queue.</span></div></section>
<div class="columns"><section class="card"><div class="section-heading"><h2>Jobs</h2>
<label>State<select id="state"><option value="">All states</option><option>queued</option><option>running</option>
<option>cancelling</option><option>succeeded</option><option>failed</option><option>cancelled</option><option>indeterminate</option></select></label></div>
<div class="table-scroll"><table><thead><tr><th>Job</th><th>State</th><th>Progress</th><th>Updated</th></tr></thead>
<tbody id="jobs"></tbody></table></div><p id="empty" class="muted">No jobs loaded.</p>
<div class="pagination"><button id="previous" disabled>Previous</button><button id="next" disabled>Next</button></div></section>
<section class="card"><div class="section-heading"><h2>Job detail</h2><button id="cancel" disabled>Request cancellation</button></div>
<p id="selected-label" class="muted">Select a job to inspect its state and events.</p>
<pre id="detail" tabindex="0"></pre><h3>Live events</h3><ol id="events" class="events"></ol>
<button id="events-more" disabled>Load more event history</button></section></div>
<section class="card"><div class="section-heading"><h2>Submit a model job</h2><span class="muted">Requires an existing active task and configured provider.</span></div>
<form id="submit-form" novalidate><div class="form-grid">
<label>Task episode ID<input id="episode" required placeholder="00000000-0000-0000-0000-000000000000"></label>
<label>Provider<select id="provider" required><option value="">Select a provider</option></select></label>
<label>Model ID<input id="model" list="model-options" required placeholder="Exact provider model ID"><datalist id="model-options"></datalist></label>
<label>Role<select id="role"><option value="reasoning">Reasoning</option><option value="coding">Coding</option></select></label>
<label>Maximum output tokens<input id="output-limit" type="number" min="1" max="131072" value="2048" required></label>
</div><label>Prompt<textarea id="prompt" rows="5" required></textarea></label>
<details><summary>Advanced: submit a complete job payload</summary>
<p class="muted">Optional JSON replaces the fields above. Use selected_models for multiple reasoning or coding models.</p>
<textarea id="advanced" rows="8" spellcheck="false" aria-label="Complete job payload JSON"></textarea></details>
<button type="submit" id="submit">Add to queue</button></form></section>
<section class="card"><div class="section-heading"><h2>Model catalogue</h2><button id="discover">Discover selected provider's models</button></div>
<p class="muted">Discovery lists the provider's available catalogue. Text capabilities and server policy determine which models can run reasoning or coding jobs.</p>
<div class="table-scroll"><table><thead><tr><th>Provider</th><th>Model</th><th>Capabilities / roles</th><th>Availability</th></tr></thead><tbody id="models"></tbody></table></div></section>
</main><footer>Potbot · Durable jobs · <a href="/openapi.json">REST API schema</a></footer></body></html>
"""

DASHBOARD_CSS = """
:root{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,sans-serif;background:#0e1420;color:#e2e9f3;font-size:15px;line-height:1.5}
*{box-sizing:border-box}body{margin:0}header,main,footer{max-width:1440px;margin:auto;padding:24px}header{display:flex;align-items:center;justify-content:space-between;gap:24px;padding-top:32px}
h1,h2,h3,p{margin-top:0}h1{font-size:28px;margin-bottom:0;font-weight:650;letter-spacing:-.03em}h2{font-size:18px;margin-bottom:16px}h3{font-size:15px}.eyebrow{font-size:11px;letter-spacing:.2em;color:#7ce6c4;margin-bottom:8px}.muted,footer{color:#98a7bb;font-size:13px}#connection{padding:7px 12px;border:1px solid #34465c;border-radius:20px;font-size:13px}main{display:grid;gap:20px;padding-top:0}.card{background:#172130;border:1px solid #2c3b50;border-radius:14px;padding:22px;min-width:0}.columns{display:grid;grid-template-columns:1.15fr 1fr;gap:20px}.section-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}.section-heading h2{margin-bottom:12px}form{display:grid;gap:16px}.connection form{grid-template-columns:minmax(180px,1fr) auto auto;align-items:end;max-width:820px}.connection .muted{margin:10px 0 0}label{display:grid;gap:6px;font-size:13px;color:#b9c9dc}input,select,textarea,button{font:inherit}input,select,textarea{width:100%;border:1px solid #41516a;background:#101925;color:#e2e9f3;border-radius:7px;padding:10px;min-width:0}textarea{resize:vertical;font-family:ui-monospace,monospace}button{border:1px solid #477767;background:#21483e;color:#d6fff0;border-radius:7px;padding:9px 13px;cursor:pointer;white-space:nowrap}button:hover{background:#2b6052}button:disabled{opacity:.4;cursor:default}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid #7ce6c4;outline-offset:3px}.metrics{display:flex;gap:12px;flex-wrap:wrap}.metric{background:#101925;border-radius:9px;padding:12px 18px;min-width:110px}.metric strong{display:block;font-size:24px}.metric span{text-transform:capitalize;font-size:12px;color:#a1b2c9}.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px;text-align:left}th{font-weight:500;color:#98a7bb}td,th{padding:12px 9px;border-bottom:1px solid #2c3b50;vertical-align:top}td button{font:inherit;background:none;border:none;padding:0;text-decoration:underline;color:#94e5d1;max-width:180px;overflow-wrap:anywhere;white-space:normal}.pagination{display:flex;justify-content:space-between;margin-top:16px}.form-grid{display:grid;grid-template-columns:2fr 1fr 1.5fr 1fr 1fr;gap:14px}pre{background:#101925;border-radius:8px;padding:12px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:360px;overflow:auto;font-size:12px}pre:empty{display:none}.events{padding-left:20px;max-height:350px;overflow:auto;font-family:ui-monospace,monospace;font-size:12px;overflow-wrap:anywhere}.events li{padding:8px 0;border-bottom:1px solid #2c3b50}details{border-top:1px solid #2c3b50;padding-top:12px}summary{cursor:pointer;font-size:13px}details p{margin-top:12px}#submit{justify-self:start}#notice:not(:empty){margin:12px 0 0;color:#ffd79e}footer{padding-top:0}a{color:#94e5d1}@media(max-width:980px){.columns{grid-template-columns:1fr}.form-grid{grid-template-columns:1fr 1fr}}@media(max-width:600px){header,main,footer{padding-left:14px;padding-right:14px}.card{padding:16px}.connection form,.form-grid{grid-template-columns:1fr}.section-heading{align-items:flex-start}h1{font-size:24px}}
"""

DASHBOARD_JS = """'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let token = '', socket = null, cursor = 0, reconnectTimer = null, refreshTimer = null;
  let selected = null, eventCursor = 0, pageCursor = null, nextCursor = null, pages = [], models = [];
  let connected = false, refreshing = false, refreshAgain = false, generation = 0;
  const states = ['queued','running','cancelling','succeeded','failed','cancelled','indeterminate'];
  const notice = message => { $('notice').textContent = message; };
  const envelope = payload => ({schema_version:1, request_id:crypto.randomUUID(), idempotency_key:crypto.randomUUID(), payload});
  const asText = value => typeof value === 'string' ? value : JSON.stringify(value);
  const node = (tag, text, className) => {const item=document.createElement(tag); if(text!==undefined)item.textContent=text; if(className)item.className=className; return item;};
  async function api(path, options={}) {
    if (!token) throw new Error('Connect to your workspace first.');
    const response = await fetch(path, {...options, cache:'no-store', headers:{'Authorization':'Bearer '+token, ...(options.body?{'Content-Type':'application/json'}:{}), ...options.headers}});
    const value = await response.json();
    if (!response.ok || value.error) throw new Error(value.error?.message || 'The request could not be completed.');
    return value.data;
  }
  function disconnect() {
    connected=false; generation++; token=''; cursor=0; selected=null; eventCursor=0; pageCursor=null; nextCursor=null; pages=[]; models=[];
    clearTimeout(reconnectTimer); clearTimeout(refreshTimer); reconnectTimer=null; refreshTimer=null;
    if(socket){const old=socket; socket=null; old.close();}
    $('token').value=''; $('connection').textContent='Disconnected'; $('jobs').replaceChildren(); $('models').replaceChildren(); $('queue').replaceChildren(); $('events').replaceChildren(); $('detail').textContent=''; $('selected-label').textContent='Select a job to inspect its state and events.'; $('cancel').disabled=true; $('next').disabled=true; $('previous').disabled=true; $('events-more').disabled=true;
  }
  function queueView(data) {
    const counts = data.counts || data.states || data;
    $('queue').replaceChildren(...states.map(state=>{const el=node('div',undefined,'metric');el.append(node('strong',String(counts[state]??0)),node('span',state));return el;}));
  }
  function progressText(job) {
    const p=job.progress||{}; return [p.stage,p.completed!==undefined ? String(p.completed)+(p.total!==null&&p.total!==undefined?'/'+p.total:''):''].filter(Boolean).join(' · ') || '—';
  }
  async function selectJob(id) {
    selected=id; eventCursor=0; $('events').replaceChildren(); $('selected-label').textContent=id;
    const current=generation;
    try { const [job, events]=await Promise.all([api('/v1/jobs/'+encodeURIComponent(id)),api('/v1/jobs/'+encodeURIComponent(id)+'/events?limit=200')]);
      if(!connected || current!==generation || selected!==id)return;
      detailView(job); events.items.forEach(appendEvent); eventCursor=events.next_cursor; $('events-more').disabled=!events.has_more;
    } catch(error){notice(error.message);}
  }
  function detailView(job) {
    $('detail').textContent=JSON.stringify(job,null,2);
    $('cancel').disabled=!['queued','running'].includes(job.state);
  }
  function appendEvent(event) {
    if(!selected || event.job_id!==selected)return;
    const id=String(event.sequence);
    if([...$('events').children].some(el=>el.dataset.sequence===id))return;
    const item=node('li', '#'+id+' '+event.event_type+' '+JSON.stringify(event.data||{}));item.dataset.sequence=id;
    $('events').append(item);while($('events').children.length>500)$('events').firstChild.remove();
    $('events').scrollTop=$('events').scrollHeight;
  }
  async function refresh() {
    if(!connected)return;
    if(refreshing){refreshAgain=true;return;}
    refreshing=true; const current=generation;
    try {
      const query=new URLSearchParams({limit:'30'}); if($('state').value)query.set('state',$('state').value);if(pageCursor)query.set('after',pageCursor);
      const [queue,jobs]=await Promise.all([api('/v1/queue'),api('/v1/jobs?'+query)]);
      if(!connected || current!==generation)return;
      if(socket===null && cursor===0 && Number.isSafeInteger(queue.event_cursor) && Number.isSafeInteger(jobs.event_cursor))cursor=Math.min(queue.event_cursor,jobs.event_cursor);
      queueView(queue);$('jobs').replaceChildren(...jobs.items.map(job=>{
        const row=node('tr'), id=node('td'), button=node('button',job.id);button.addEventListener('click',()=>selectJob(job.id));id.append(button);
        row.append(id,node('td',job.state),node('td',progressText(job)),node('td',job.updated_at||job.created_at||'—'));return row;
      }));
      $('empty').textContent=jobs.items.length?'':'No jobs match this view.';nextCursor=jobs.next_cursor;$('next').disabled=!nextCursor;$('previous').disabled=!pages.length;
      if(selected){const job=await api('/v1/jobs/'+encodeURIComponent(selected));if(current===generation&&job.id===selected)detailView(job);}
    }catch(error){notice(error.message);}finally{refreshing=false;if(refreshAgain){refreshAgain=false;queueRefresh();}}
  }
  function queueRefresh(){if(!refreshTimer)refreshTimer=setTimeout(()=>{refreshTimer=null;refresh();},150);}
  function stream() {
    if(!connected)return;
    const current=generation;
    const target=new URL('/v1/jobs/stream',location.href);target.protocol=location.protocol==='https:'?'wss:':'ws:';
    const ws=new WebSocket(target);socket=ws;
    ws.onopen=()=>ws.send(JSON.stringify({type:'authenticate',token,after:cursor}));
    ws.onmessage=message=>{if(current!==generation)return;try{const value=JSON.parse(message.data);
      if(value.type==='ready'){$('connection').textContent='Live';notice('');}
      else if(value.type==='event'){if(value.cursor>cursor){cursor=value.cursor;appendEvent(value.event);queueRefresh();}}
      else if(value.type==='error')notice(value.error?.message||'The update stream is unavailable.');
    }catch(_error){notice('Received an unsupported update.');}};
    ws.onclose=event=>{if(current!==generation||!connected)return;socket=null;
      if(event.code===1008){$('connection').textContent='Authentication or scope changed';notice('Reconnect with a valid workspace token.');return;}
      $('connection').textContent='Reconnecting…';reconnectTimer=setTimeout(stream,2000);
    };
    ws.onerror=()=>{if(current===generation)$('connection').textContent='Connection interrupted';};
  }
  function catalogueView(data) {
    const providers=data.providers||[];
    const previous=$('provider').value;
    $('provider').replaceChildren(node('option','Select a provider'));
    $('provider').firstChild.value='';
    for(const provider of providers){const option=node('option',provider.label||provider.id);option.value=provider.id;if(provider.configured===false)option.textContent+=' · not configured';$('provider').append(option);}
    if(providers.some(p=>p.id===previous))$('provider').value=previous;
    models=[...(data.models||[])];
    for(const local of data.local_models||data.local||[])models.push({...local,provider:'local',model:local.canonical_model});
    modelsView();
  }
  function modelsView() {
    $('models').replaceChildren(...models.map(model=>{
      const row=node('tr'),local=model.provider==='local';
      const capability=model.text_generation===false?'non-text model':model.text_generation===true?'text generation':local?asText(model.roles||model.role||'local profile'):'text capability unverified';
      const eligibility=model.qualified===true?'qualified':local?'local profile; qualification required':model.text_generation===false?'unavailable for text jobs':model.generation_enabled===false?'not enabled by configuration':model.text_generation!==true?'capability verification required':'enabled; qualification required';
      row.append(node('td',model.provider||'—'),node('td',model.model||model.id||model.canonical_model||'—'),node('td',capability),node('td',eligibility));return row;
    }));
    const provider=$('provider').value;
    $('model-options').replaceChildren(...models.filter(m=>m.provider===provider&&m.text_generation!==false&&m.generation_enabled!==false).map(model=>{const option=node('option');option.value=model.model||model.id||model.canonical_model;return option;}));
  }
  $('connect-form').addEventListener('submit',async event=>{event.preventDefault();const supplied=$('token').value;disconnect();token=supplied;
    try{const data=await api('/v1/potbot/models');connected=true;$('token').value='';$('connection').textContent='Connecting…';catalogueView(data);await refresh();stream();}catch(error){disconnect();notice(error.message);}});
  $('disconnect').addEventListener('click',()=>{disconnect();notice('');});
  $('refresh').addEventListener('click',refresh);
  $('state').addEventListener('change',()=>{pages=[];pageCursor=null;refresh();});
  $('next').addEventListener('click',()=>{if(nextCursor){pages.push(pageCursor);pageCursor=nextCursor;refresh();}});
  $('previous').addEventListener('click',()=>{if(pages.length){pageCursor=pages.pop();refresh();}});
  $('provider').addEventListener('change',modelsView);
  $('events-more').addEventListener('click',async()=>{if(!selected)return;const id=selected;try{const data=await api('/v1/jobs/'+encodeURIComponent(id)+'/events?limit=200&after='+eventCursor);if(id===selected){data.items.forEach(appendEvent);eventCursor=data.next_cursor;$('events-more').disabled=!data.has_more;}}catch(error){notice(error.message);}});
  $('cancel').addEventListener('click',async()=>{if(!selected)return;try{await api('/v1/jobs/'+encodeURIComponent(selected)+'/cancel',{method:'POST',body:JSON.stringify(envelope({}))});notice('Cancellation requested. A running call may need to finish reconciling its outcome.');refresh();}catch(error){notice(error.message);}});
  $('discover').addEventListener('click',async()=>{const provider=$('provider').value;if(!provider){notice('Select a provider first.');return;}$('discover').disabled=true;
    try{const data=await api('/v1/providers/'+encodeURIComponent(provider)+'/models/discover',{method:'POST',body:JSON.stringify(envelope({}))});models=models.filter(m=>m.provider!==provider).concat((data.models||[]).map(m=>({...m,provider})));modelsView();notice('Catalogue refreshed.');}catch(error){notice(error.message);}finally{$('discover').disabled=false;}});
  $('submit-form').addEventListener('submit',async event=>{event.preventDefault();const custom=$('advanced').value.trim();if(!custom&&!$('submit-form').reportValidity())return;$('submit').disabled=true;
    try{const payload=custom?JSON.parse(custom):{job_type:'model.generate',episode_id:$('episode').value.trim(),selected_models:[{provider:$('provider').value,model:$('model').value.trim(),role:$('role').value}],input:{prompt:$('prompt').value,max_output_tokens:Number($('output-limit').value)}};
      const job=await api('/v1/jobs',{method:'POST',body:JSON.stringify(envelope(payload))});pageCursor=null;pages=[];$('state').value='';notice('Job added to the queue.');await refresh();await selectJob(job.id);
    }catch(error){notice(error.message);}finally{$('submit').disabled=false;}});
  window.addEventListener('pagehide',disconnect);
})();
"""
