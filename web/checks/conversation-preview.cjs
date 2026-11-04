// Live session + memory-only Claude graph: no provider/account/position writes.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const web = path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(web, name), 'utf8');
const settle = async () => { for(let i=0;i<10;i++) await Promise.resolve(); };
const timers = new Map(), storage = {}, events = {}, requests = [];
let serial=0, defer=null;
const c = {
  console, Date, Promise, URL, Set, Math, JSON, Number,
  location:{href:'https://grove.example/'},
  localStorage:{getItem:key=>storage[key]||null,setItem:(key,value)=>storage[key]=value},
  setTimeout(fn,ms){timers.set(++serial,{fn,ms});return serial;},clearTimeout:id=>timers.delete(id),
  addEventListener:(name,fn)=>(events[name] ||= []).push(fn),
  dispatchEvent:event=>(events[event.type]||[]).forEach(fn=>fn(event)),
  CustomEvent:class{constructor(type,init){this.type=type;this.detail=init?.detail;}},
  GroveDeferred:{load:async name=>{assert.equal(name,'claude-preview');if(defer)await defer;}},
  GroveClaudePreviewData:JSON.parse(read('checks/fixture.synthetic.json')),
  GroveLive:{ENDPOINTS:{graph:'/graph'},getJSON:async target=>{requests.push(target);return {ok:true,status:200,body:{elements:{nodes:[],edges:[]}}};}},
};
c.window=c;vm.createContext(c);
vm.runInContext(read('simulation.js'),c);
vm.runInContext(read('conversation-preview.js'),c);
(async()=>{
  await settle();
  const api=c.GroveSimulation, preview=c.GroveConversationPreview;
  api.bindAccount('synthetic-owner');api.signIn('email','review@example.test');api.setStep(1);
  const storedBefore=JSON.stringify(storage);
  await preview.start();
  assert.equal(api.isLive(),true);
  assert.equal(api.getState().onboardingComplete,false);
  assert.equal(api.getState().tourComplete,false);
  assert.equal(api.getState().onboardingStep,1);
  assert.equal(api.getState().account,'synthetic-owner');
  const before=api.getState().currentNodeCount;
  for(let step=0;step<18;step++){
    const [id,timer]=[...timers][0];assert.equal(timer.ms,3000);timers.delete(id);timer.fn();
  }
  assert.equal(api.getState().conversationPreview.status,'ready');
  assert(api.getState().currentNodeCount>before);
  const graph=await (await c.groveApi('/api/graph')).json();
  assert(graph.elements.nodes.length>1);
  assert.equal(api.realId(graph.elements.nodes[0].data.id),null);
  assert.deepEqual(requests,[]);
  assert.equal(JSON.stringify(storage),storedBefore,'Preview graph and progress stay out of account storage');
  for(const step of [2,3,4]){api.setStep(step);assert.equal(api.getState().onboardingComplete,false);}
  api.completeOnboarding();assert.equal(api.getState().tourComplete,false);
  assert(!JSON.stringify(storage).includes('conversationPreview'));
  assert(!JSON.stringify(storage).includes(graph.elements.nodes[0].data.id));
  api.signOut();assert.equal(preview.active(),false);assert.equal(timers.size,0);
  api.bindAccount('synthetic-owner');api.signIn('email');
  let release;defer=new Promise(resolve=>release=resolve);
  const pending=preview.start();api.bindAccount('synthetic-other');release();
  await assert.rejects(pending,/account changed/);
  assert.equal(preview.active(),false);assert.equal(timers.size,0);
  assert.equal(api.getState().account,'synthetic-other');
  console.log('PASS full onboarding sequence, live session, graph growth, no writes, logout and delayed account switch');
})().catch(error=>{console.error(error);process.exitCode=1;});
