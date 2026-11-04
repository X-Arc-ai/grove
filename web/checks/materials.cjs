// Synthetic transport controls for the real material controller. No approval is logged.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {webcrypto} = require('node:crypto');
const {File} = require('node:buffer');
const checks = [];
const test = (name, run) => checks.push({name, run});
const clone = value => JSON.parse(JSON.stringify(value));
const tick = async () => { for (let i=0;i<12;i++) await new Promise(resolve=>setImmediate(resolve)); };
const source = fs.readFileSync(path.join(__dirname, '../materials.js'), 'utf8');
const box = {console, File, AbortController, TextEncoder, crypto:webcrypto, setTimeout, clearTimeout};
box.window=box;
vm.runInNewContext(source,box);
const item = (extra={}) => Object.assign({id:'11111111-1111-4111-8111-111111111111',kind:'note',title:'Cedar plan',revision:1,source_state:'retained',graph_state:'queued',counts:{parts:1,held:0,retained_characters:12},screening_counts:{},parts:[{key:'note',text:'A cedar plan'}],request:{status:'queued',changed_ids:[]}},extra);
function setup() {
  const calls=[], timers=new Map(); let seq=0, owner='owner-a', respond;
  const store=box.GroveMaterials.createStore({
    account:()=>owner,
    request:(action,id,body,key,signal)=>new Promise((resolve,reject)=>calls.push({action,id,body,key,signal,resolve,reject})),
    changed:()=>{}, later:(fn)=>{timers.set(++seq,fn);return seq;},cancel:id=>timers.delete(id)
  });
  const answer=(call,body,status=200)=>call.resolve({ok:status<400,status,body:Object.assign({account:owner,generation:0},body)});
  return {store,calls,timers,answer,setOwner:value=>owner=value};
}
async function ready(env) {
  env.store.mount('onboarding');
  env.answer(env.calls.at(-1),{materials:[],next_cursor:null}); await tick();
}
test('list, detail, reload and current account',async()=>{
  const e=setup(); await ready(e);
  const pending=e.store.detail(item().id); e.answer(e.calls.at(-1),{material:item()}); await pending;
  assert.equal(e.store.state.material.title,'Cedar plan');
  assert.equal(e.store.state.material.parts[0].text,'A cedar plan');
  e.store.reload(); assert.equal(e.calls.at(-1).action,'detail');
  e.answer(e.calls.at(-1),{material:item({revision:2})});await tick();
  assert.equal(e.store.state.material.revision,2);
});
test('same submission retries keep a key; changed content gets a new key',async()=>{
  const e=setup();await ready(e);e.store.newNote();e.store.draft('Cedar','Plan');
  let pending=e.store.saveNote(), first=e.calls.at(-1);first.reject(new Error('offline'));await pending;
  pending=e.store.saveNote();let retry=e.calls.at(-1);assert.equal(retry.key,first.key);
  retry.reject(new Error('offline'));await pending;e.store.draft('Cedar','Changed');
  pending=e.store.saveNote();let changed=e.calls.at(-1);assert.notEqual(changed.key,first.key);
  e.answer(changed,{material:item()},201);await pending;assert.equal(e.store.state.view,'detail');
});
test('revision conflict preserves draft until explicit reload',async()=>{
  const e=setup();await ready(e);let p=e.store.detail(item().id);e.answer(e.calls.at(-1),{material:item()});await p;
  e.store.edit();e.store.draft('Changed','New body');p=e.store.saveNote();
  assert.equal(e.calls.at(-1).body.expected_revision,1);
  e.answer(e.calls.at(-1),{error:{code:'revision_conflict'}},409);await p;
  assert(e.store.state.conflict);assert.equal(e.store.state.draft.body,'New body');
  p=e.store.reload();e.answer(e.calls.at(-1),{material:item({revision:2})});await p;
  assert.equal(e.store.state.material.revision,2);assert.equal(e.store.state.view,'detail');
});
test('preview and cancel never mint an approval; confirmation consumes once',async()=>{
  const e=setup();await ready(e);let p=e.store.detail(item().id);e.answer(e.calls.at(-1),{material:item()});await p;
  p=e.store.preview();e.answer(e.calls.at(-1),{id:item().id,revision:1,revisions:3,sources:6,scope:'all_revisions_and_derived_copies',downloaded_exports:'out_of_reach',backup_days:30});await p;
  assert.equal(e.calls.filter(c=>c.action==='confirm').length,0);
  p=e.store.back();e.answer(e.calls.at(-1),{material:item()});await p;
  p=e.store.preview();e.answer(e.calls.at(-1),{id:item().id,revision:1,revisions:3,sources:6,scope:'all_revisions_and_derived_copies',downloaded_exports:'out_of_reach',backup_days:30});await p;
  p=e.store.confirmDelete();const approval=e.calls.at(-1);assert.equal(approval.action,'confirm');
  assert.deepEqual(clone(approval.body),{confirm:true,expected_revision:1});
  e.answer(approval,{approval_token:'synthetic-once',one_use:true});await tick();
  const deletion=e.calls.at(-1);assert.equal(deletion.action,'delete');
  assert(!JSON.stringify(e.store.state).includes('synthetic-once'));
  assert.equal(deletion.body.expected_revision,1);
  e.answer(deletion,{deleted:true,receipt:{}});await p;
  assert.equal(e.store.state.material,null);assert.match(e.store.state.notice,/deleted/i);
});
test('account and route changes discard drafts, late reads and late approval',async()=>{
  const e=setup();await ready(e);e.store.newNote();e.store.draft('Private','Private body');
  const p=e.store.saveNote(),old=e.calls.at(-1);e.setOwner('owner-b');e.store.mount('onboarding');
  e.answer(old,{account:'owner-a',material:item()});await p;
  assert(!JSON.stringify(e.store.state).includes('Private'));assert.equal(e.store.state.material,null);
  const read=e.calls.at(-1);e.store.mount(null);e.answer(read,{materials:[item()]});await tick();
  assert.equal(e.store.state.items.length,0);assert(read.signal.aborted);
});
test('mismatched accounts and old generations never disclose data',async()=>{
  const e=setup();e.store.mount('settings');
  e.answer(e.calls.at(-1),{account:'other',materials:[item()]});await tick();assert.equal(e.store.state.items.length,0);
  let p=e.store.list();e.answer(e.calls.at(-1),{generation:4,materials:[item()],next_cursor:null});await p;
  p=e.store.detail(item().id);e.answer(e.calls.at(-1),{generation:3,material:item({title:'Stale secret'})});await p;
  assert(!JSON.stringify(e.store.state).includes('Stale secret'));
});
test('all supported files use bytes; unsupported formats refuse locally',async()=>{
  for(const ext of ['txt','md','pdf','docx','pptx']) {
    const e=setup();await ready(e);const f=new File(['Cedar bytes'],'Café %20.'+ext);
    e.store.newFile();e.store.file(f);const p=e.store.saveFile();await tick();const call=e.calls.at(-1);
    assert.equal(call.action,'file');assert.equal(call.body,f);assert(call.key);
    e.answer(call,{material:item({kind:'file',graph_state:'held_provenance',request:null})},201);await p;
    assert.equal(e.store.state.material.graph_state,'held_provenance');
  }
  const e=setup();await ready(e);e.store.newFile();e.store.file(new File(['x'],'bad.zip'));
  await e.store.saveFile();assert.equal(e.calls.length,1);assert.match(e.store.state.error,/TXT|text/i);
});
test('polling is read-only, bounded and cancelled on closure',async()=>{
  const e=setup();await ready(e);let p=e.store.detail(item().id);e.answer(e.calls.at(-1),{material:item()});await p;
  for(let i=0;i<15&&e.timers.size;i++) {
    const [key,fn]=e.timers.entries().next().value;e.timers.delete(key);fn();
    assert.equal(e.calls.at(-1).action,'detail');e.answer(e.calls.at(-1),{material:item()});await tick();
  }
  assert.equal(e.timers.size,0);assert(e.calls.length<=14);
  e.store.mount(null);assert.equal(e.timers.size,0);
});
test('status separates held files, screened content and saved empty effects',()=>{
  const status=box.GroveMaterials.status;
  assert.match(status(item({kind:'file',graph_state:'held_provenance'})),/source details/);
  assert.match(status(item({source_state:'screened_out'})),/screen/i);
  assert(!status(item({request:{status:'saved',changed_ids:[]}})).includes('Added to your brain'));
  assert.match(status(item({request:{status:'saved',changed_ids:['actual-id']}})),/Added to your brain/);
});
test('late approval after closure never sends delete',async()=>{
  const e=setup();await ready(e);let p=e.store.detail(item().id);e.answer(e.calls.at(-1),{material:item()});await p;
  p=e.store.preview();e.answer(e.calls.at(-1),{id:item().id,revision:1,revisions:1,sources:2,scope:'all_revisions_and_derived_copies'});await p;
  p=e.store.confirmDelete();const approval=e.calls.at(-1);e.store.mount(null);
  e.answer(approval,{approval_token:'synthetic-once',one_use:true});await p;
  assert.equal(e.calls.filter(c=>c.action==='delete').length,0);
});
test('transport uses exact MIME, filename encoding, session and mutation headers',async()=>{
  const calls=[];
  const env={File,URL,URLSearchParams,Promise,crypto:webcrypto,CustomEvent:class{},
    dispatchEvent(){},addEventListener(){},fetch:(url,init)=>{calls.push({url,init});return Promise.resolve({ok:true,status:200,text:()=>Promise.resolve('{}')});}};
  env.window=env;vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../live.js'),'utf8'),env);
  const types={txt:'text/plain',md:'text/markdown',pdf:'application/pdf',docx:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',pptx:'application/vnd.openxmlformats-officedocument.presentationml.presentation'};
  for(const [ext,mime] of Object.entries(types)){
    const file=new File(['bytes'],'Café %20.'+ext);await env.GroveLive.materialRequest('file',null,file,'synthetic-key');
    const {init}=calls.at(-1);assert.equal(init.body,file);assert.equal(init.headers['Content-Type'],mime);
    assert.equal(init.headers['X-Grove-Filename'],encodeURIComponent(file.name));
    assert.equal(init.headers['X-Grove-Action'],'materials');assert.equal(init.credentials,'same-origin');
    assert.equal(init.cache,'no-store');assert.equal(init.headers['Idempotency-Key'],'synthetic-key');
    assert(!('Authorization' in init.headers));
  }
  await env.GroveLive.materialRequest('edit',item().id,{title:'T',body:'B',expected_revision:3},'key');
  assert.equal(calls.at(-1).init.method,'PUT');assert.equal(JSON.parse(calls.at(-1).init.body).expected_revision,3);
});
(async()=>{let failed=0;for(const c of checks){try{await c.run();console.log('PASS '+c.name);}catch(error){failed++;console.error('FAIL '+c.name+': '+error.message);}}console.log(`${checks.length-failed} passed, ${failed} failed`);if(failed)process.exitCode=1;})();
