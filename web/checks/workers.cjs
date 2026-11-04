// Worker reads stay tied to the session that requested them.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
function harness(){
  const requests=[],events=[];
  const win={location:{pathname:'/',href:'https://example.test/'},addEventListener(){},
    dispatchEvent:e=>events.push(e.type),crypto:{randomUUID:()=> 'nonce'},
    fetch:(url,init)=>new Promise(resolve=>requests.push({url,init,resolve}))};
  const box={window:win,URL,URLSearchParams,Promise,JSON,Number,CustomEvent:class {constructor(type){this.type=type;}},localStorage:{setItem(){}}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../live.js'),'utf8'),box);
  return {G:win.GroveLive,requests,events,answer:(r,status,body)=>r.resolve({ok:status===200,status,text:()=>Promise.resolve(JSON.stringify(body))})};
}
(async()=>{
  const h=harness();assert.equal(typeof h.G.workers,'function','The live API exposes a guarded worker read');
  const pending=h.G.workers();assert.equal(h.requests[0].url,'/v1/views/workers');
  assert.equal(h.requests[0].init.method,'GET');assert.equal(h.requests[0].init.credentials,'same-origin');
  h.G.invalidateSession();h.answer(h.requests[0],200,{private:'old account'});
  const stale=await pending;assert(stale.stale);assert.equal(stale.body,null);
  const late=h.G.workers();h.G.invalidateSession();h.answer(h.requests[1],401,{error:{code:'unauthenticated'}});
  assert((await late).stale);assert.deepEqual(h.events,[]);
  const current=h.G.workers();h.answer(h.requests[2],401,{error:{code:'unauthenticated'}});
  assert.equal((await current).status,401);assert.deepEqual(h.events,['grove:unauthenticated']);
  const other=harness();const first=other.G.workers();const sign=other.G.session();
  other.answer(other.requests[1],200,{account:'next-account'});await sign;
  other.answer(other.requests[0],200,{private:'old'});assert((await first).stale);
  console.log('worker transport: 4 grouped controls passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
