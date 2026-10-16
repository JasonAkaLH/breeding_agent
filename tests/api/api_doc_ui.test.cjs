const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const {execFileSync} = require('node:child_process');
const vm = require('node:vm');
const {JSDOM,VirtualConsole} = require('jsdom');
const ts = require('typescript');

const root = resolve(__dirname, '../..');
const html = readFileSync(resolve(root,'docs/api/api-doc.html'),'utf8');
const sample = readFileSync(resolve(root,'docs/api/frontend-example.ts'),'utf8');

function loadExample(fetcher = () => {throw new Error('Unexpected fetch');}) {
  const module={exports:{}};
  const output=ts.transpileModule(sample,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
  vm.runInNewContext(output,{module,exports:module.exports,fetch:fetcher,Headers,FormData,TextDecoder,Uint8Array,crypto:require('node:crypto').webcrypto});
  return module.exports;
}

function stream(text, chunkSize=1, onCancel=()=>{}) {
  const bytes=new TextEncoder().encode(text);let offset=0;
  return new ReadableStream({pull(controller){if(offset===bytes.length){controller.close();return;}controller.enqueue(bytes.slice(offset,offset+chunkSize));offset+=Math.min(chunkSize,bytes.length-offset);},cancel:onCancel});
}
const event=(type='agent.run.completed',id='evt1')=>({event_id:id,event_type:type,task_id:'task1',conversation_id:'conv1',node_id:null,payload:{},created_at:null});

async function page(t,url='http://localhost:8000/api-doc') {
  const errors=[],copied=[],network=[];const vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(e));
  const dom=new JSDOM(html,{url,runScripts:'dangerously',pretendToBeVisual:true,virtualConsole:vc,beforeParse(w){
    w.HTMLElement.prototype.scrollIntoView=()=>{};
    Object.defineProperty(w,'isSecureContext',{value:true});
    Object.defineProperty(w.navigator,'clipboard',{value:{writeText:async text=>copied.push(text)}});
    w.fetch=async url=>{network.push(url);throw new Error('Document must not fetch business APIs');};
  }});
  t.after(()=>dom.window.close());await new Promise(setImmediate);assert.deepEqual(errors,[]);
  return {window:dom.window,document:dom.window.document,copied,network,input(selector,value,event='input'){const e=dom.window.document.querySelector(selector);e.value=value;e.dispatchEvent(new dom.window.Event(event,{bubbles:true}));}};
}

for(const [url,base] of [['http://host.example:8000/api-doc','http://host.example:8000'],['https://host.example/seedpilot/api-doc','https://host.example/seedpilot'],['https://host.example/seedpilot/api-doc/','https://host.example/seedpilot'],['file:///tmp/api-doc.html','http://127.0.0.1:8000']]) {
  test('document hydrates and resolves its own links at '+url,async t=>{
    const p=await page(t,url);assert.equal(p.document.querySelectorAll('.endpoint').length,57);assert.equal(p.document.querySelectorAll('.schema').length,70);
    assert.equal(p.document.querySelector('#base-url').value,base);
    for(const a of p.document.querySelectorAll('[data-doc-link]'))assert.equal(a.href,new URL(base+'/'+a.dataset.docLink).href);
    assert.ok(p.document.querySelector('#request-op-post-api-v1-auth-login').textContent.includes(base+'/api/v1/auth/login'));
    assert.deepEqual(p.network,[]);
  });
}
test('editing base and language updates requests without changing resource links',async t=>{
  const p=await page(t);p.input('#base-url','https://gateway.example/seedpilot///');
  p.input('#code-language','curl','change');
  const snippets=[...p.document.querySelectorAll('[id^="request-op-"]')];
  for(const code of snippets){assert.ok(code.textContent.includes('https://gateway.example/seedpilot/api/v1/'));execFileSync('/bin/bash',['-n'],{input:code.textContent});}
  p.document.querySelector('[data-copy="request-op-post-api-v1-auth-login"]').click();assert.equal(p.copied.at(-1),snippets[0].textContent);
  assert.equal(p.document.querySelector('[data-doc-link="openapi.json"]').href,'http://localhost:8000/openapi.json');
  p.input('#base-url','javascript:alert(1)');assert.equal(p.document.querySelector('[data-copy="request-op-post-api-v1-auth-login"]').disabled,true);
  p.input('#base-url','https://safe.example');assert.equal(p.document.querySelector('[data-copy="request-op-post-api-v1-auth-login"]').disabled,false);
});
test('search, category, schema navigation and deep links reveal complete details',async t=>{
  const p=await page(t);const visible=()=>[...p.document.querySelectorAll('.endpoint')].filter(x=>!x.hidden);
  p.input('#endpoint-search','SSE');assert.equal(visible()[0].id,'op-get-api-v1-tasks-task-id-events');
  p.input('#endpoint-search','POST routing_mode');assert.equal(visible().length,1);assert.ok(visible()[0].textContent.includes('提交消息'));
  p.input('#group-filter','auth','change');assert.equal(visible().length,0);assert.equal(p.document.querySelector('#empty-results').hidden,false);
  p.document.querySelector('a[href="#op-post-api-v1-auth-login"]').click();assert.equal(p.document.querySelector('#op-post-api-v1-auth-login').open,true);assert.equal(visible().length,57);
  p.document.querySelector('#collapse-all').click();assert.equal(p.document.querySelectorAll('.endpoint[open]').length,0);
  p.input('#schema-search','missing-schema');
  p.document.querySelector('a[href="#schema-AuthTokenResponse"]').click();assert.equal(p.document.querySelector('#schema-AuthTokenResponse').hidden,false);assert.equal(p.document.querySelector('#schema-AuthTokenResponse').open,true);
});
test('every generated fetch example parses as TypeScript',async t=>{
  const p=await page(t);
  for(const code of p.document.querySelectorAll('[id^="request-op-"]')){
    const result=ts.transpileModule(code.textContent,{reportDiagnostics:true,compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}});
    assert.equal(result.diagnostics.length,0,code.id);
  }
  assert.ok(p.document.querySelector('#op-get-api-v1-tasks-task-id-events').textContent.includes('SSE 事件流'));
  assert.ok(p.document.querySelector('#op-delete-api-v1-mcp-servers-server-id').textContent.includes('UserMCPDeletePendingResponse'));
});
test('knowledge examples preserve multipart fields and read Markdown as text',async t=>{
  const p=await page(t);
  p.input('#group-filter','knowledge','change');
  assert.equal([...p.document.querySelectorAll('.endpoint')].filter(x=>!x.hidden).length,24);
  const upload=p.document.querySelector('#request-op-post-api-v1-knowledge-libraries-library-id-imports').textContent;
  assert.ok(upload.includes("form.append('files', file)"));
  assert.ok(upload.includes('replacement_document_id'));
  assert.ok(!upload.includes("'Content-Type':"));
  const content=p.document.querySelector('#request-op-get-api-v1-knowledge-libraries-library-id-documents-document-id-content').textContent;
  assert.ok(content.includes('/library_example/documents/document_example/content?download=false'));
  assert.ok(content.includes('await response.text()'));
  assert.ok(!content.includes('response.json()'));
  const preview=p.document.querySelector('#op-get-api-v1-knowledge-libraries-library-id-imports-import-id-content').textContent;
  assert.ok(preview.includes('Markdown 文本流'));
  p.input('#code-language','curl','change');
  const curl=p.document.querySelector('#request-op-post-api-v1-knowledge-libraries-library-id-imports').textContent;
  assert.ok(curl.includes("-F 'files=@/path/to/paper.md'"));
  assert.ok(!curl.includes('conversation_id='));
  for(const code of p.document.querySelectorAll('[id^="request-op-"]'))assert.ok(!code.textContent.includes('/undefined'));
});
test('SSE decoder handles UTF-8 and CRLF across single-byte boundaries and multiline data',async()=>{
  const api=loadExample();const expected=event('agent.reasoning_delta');expected.payload={delta:'中文🌾',ordinal:0,sample_id:'sample1'};
  const text=': heartbeat\r\n\r\nid: ignored\r\nevent: ignored\r\ndata: '+JSON.stringify(expected).replace(',"payload"',',\r\ndata: "payload"')+'\r\n\r\n';
  const actual=[];for await(const e of api.decodeEvents(stream(text)))actual.push(JSON.parse(JSON.stringify(e)));
  assert.deepEqual(actual,[expected]);
});
test('SSE decoder rejects malformed envelopes and discards incomplete final frames',async()=>{
  const api=loadExample();await assert.rejects(async()=>{for await(const _e of api.decodeEvents(stream('data: {"task_id":"task1"}\n\n'))){}},/Invalid SSE/);
  const actual=[];for await(const e of api.decodeEvents(stream('data: '+JSON.stringify(event()))))actual.push(e);assert.equal(actual.length,0);
});
test('leaving a stream cancels its reader and auth invalidation clears the token',async()=>{
  let cancelled=false;const api=loadExample();const body=new ReadableStream({start(c){c.enqueue(new TextEncoder().encode('data: '+JSON.stringify(event())+'\n\n'));},cancel(){cancelled=true;}});
  for await(const _e of api.decodeEvents(body))break;assert.equal(cancelled,true);
  const requests=[];const client=loadExample(async(url,init)=>{requests.push(init);return url.endsWith('/events')?new Response(stream('data: '+JSON.stringify(event('auth.invalidated'))+'\n\n'),{headers:{'Content-Type':'text/event-stream'}}):new Response('{}',{headers:{'Content-Type':'application/json'}});}).createApi('http://localhost:8000');
  client.setToken('test-token');await assert.rejects(async()=>{for await(const _e of client.events('task1')){}},e=>e.status===401);
  await client.request('/api/v1/auth/me');assert.equal(requests.at(-1).headers.has('Authorization'),false);
});
test('request helper sends Bearer, preserves multipart boundaries and accepts 204',async()=>{
  const calls=[];const api=loadExample(async(url,init)=>{calls.push({url,init});return new Response(null,{status:204});}).createApi('https://api.example/seedpilot/');api.setToken('test-token');
  const form=new FormData();form.append('conversation_id','conv1');
  const response=await api.request('/api/v1/conversations/uploads',{method:'POST',body:form});assert.equal(response,undefined);
  assert.equal(calls[0].url,'https://api.example/seedpilot/api/v1/conversations/uploads');assert.equal(calls[0].init.headers.get('Authorization'),'Bearer test-token');assert.equal(calls[0].init.headers.has('Content-Type'),false);
});
test('request helper preserves structured errors and tolerates text proxy errors',async()=>{
  for(const [body,expected] of [['{"detail":{"code":"message_id_conflict"}}','message_id_conflict'],['upstream unavailable','upstream unavailable']]){
    const api=loadExample(async()=>new Response(body,{status:409})).createApi('https://api.example');
    await assert.rejects(()=>api.request('/api/v1/conversations'),e=>e.status===409&&e.message.includes(expected));
  }
});
test('aborting fetch propagates and never resubmits a message',async()=>{
  let calls=0;const api=loadExample(async(_url,init)=>{calls++;init.signal.throwIfAborted();}).createApi('https://api.example');
  const c=new AbortController();c.abort();await assert.rejects(async()=>{for await(const _e of api.events('task1',c.signal)){}},e=>e.name==='AbortError');assert.equal(calls,1);
});

test('complete first-conversation example deduplicates terminal events and retrieves final history',async()=>{
  const calls=[],received=[];
  const sampleApi=loadExample(async(url,init={})=>{
    calls.push({url,init});
    if(url.endsWith('/auth/login'))return Response.json({access_token:'test-token',user:{username:'frontend_demo'}});
    if(url.endsWith('/chat-messages'))return Response.json({task_id:'task1'},{status:202});
    if(url.endsWith('/events'))return new Response(stream('data: '+JSON.stringify(event('task.accepted','first'))+'\n\ndata: '+JSON.stringify(event('task.accepted','first'))+'\n\ndata: '+JSON.stringify(event())+'\n\n'),{headers:{'Content-Type':'text/event-stream'}});
    if(url.endsWith('/tasks/task1'))return Response.json({status:'completed'});
    if(url.endsWith('/messages'))return Response.json({messages:[{role:'assistant',content:'你好',task_id:'task1',stream_status:'complete'}]});
    if(url.endsWith('/artifacts'))return Response.json({artifacts:[]});
    throw new Error('Unexpected URL '+url);
  });
  const result=await sampleApi.firstConversation('http://localhost:8000','frontend_demo',e=>received.push(e.event_id));
  assert.deepEqual(received,['first','evt1']);assert.equal(result.history.messages[0].content,'你好');
  assert.equal(calls.filter(x=>x.url.endsWith('/chat-messages')).length,1);
  const sent=JSON.parse(calls.find(x=>x.url.endsWith('/chat-messages')).init.body);
  assert.equal(sent.conversation_id,result.conversationId);assert.equal(sent.client_message_id.length,32);
});

test('an early EOF queries task state and reports recovery without resubmitting',async()=>{
  let submissions=0;
  const sampleApi=loadExample(async(url)=>{
    if(url.endsWith('/auth/login'))return Response.json({access_token:'test-token',user:{username:'frontend_demo'}});
    if(url.endsWith('/chat-messages')){submissions++;return Response.json({task_id:'task1'},{status:202});}
    if(url.endsWith('/events'))return new Response(stream(''),{headers:{'Content-Type':'text/event-stream'}});
    if(url.endsWith('/tasks/task1'))return Response.json({status:'running'});
    throw new Error('Unexpected URL '+url);
  });
  await assert.rejects(()=>sampleApi.firstConversation('http://localhost:8000','frontend_demo',()=>{}),/task_id=task1/);
  assert.equal(submissions,1);
});
