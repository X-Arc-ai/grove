// The worker record: its transport, the strict validator and the words for each state.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const web=path.join(__dirname,'..');
const RUN={outcome:'ok',started_at:'2026-11-04T10:00:00Z',finished_at:'2026-11-04T10:05:00Z'};
const FRESH={total_items:200,dated_items:190,window_days:30,fresh_items:150,newest_change_at:'2026-11-04T10:05:00Z',oldest_change_at:'2026-09-01T10:00:00Z'};
const trail=(ids,more={})=>({...RUN,changed_items:ids.length,item_ids:ids,has_more:false,...more});
function record(ids=['a1','a2','a3'],freshness=FRESH){
  // A deep copy: the refusal cases change the payload they get.
  return structuredClone({schema:'grove.worker-record.v1',observed_at:'2026-11-04T12:00:00Z',observation_kind:'stored_change_entries',
    agents:[{agent:'learner',last_run:RUN,trail:trail(ids)},{agent:'curator',last_run:RUN,trail:null},{agent:'connector',last_run:null,trail:null}],freshness});
}
function feed(){
  return {schema:'grove.worker-activity.v1',observed_at:'2026-11-04T12:00:00Z',observation_kind:'stored_run_records',service_state:'unknown',service_reason:'no_process_heartbeat',
    recent_limit:20,recent_has_more:false,recent_runs:[],agents:[
      ['learner','limited','legacy_learner_only'],['curator','unavailable','curator_authority_unavailable'],['connector','limited','legacy_connector_only'],
    ].map(([agent,state,reason])=>({agent,policy:{state:'allowed',reason:'ordinary_world_policy'},capability:{state,reason},activity:{state:'idle',running_records:0,stale_records:0}}))};
}
function element(){
  const stub=()=>({addEventListener(){},focus(){},disabled:false,open:false});
  return {innerHTML:'',scrollTop:0,className:'',id:'',style:{},children:[],classList:{add(){},remove(){},toggle(){},contains:()=>false},setAttribute(){},getAttribute:()=>null,hasAttribute:()=>false,
    appendChild(c){this.children.push(c);return c;},addEventListener(){},contains:()=>false,closest:()=>({title:''}),querySelector:stub,querySelectorAll:()=>[],focus(){},checked:false};
}
function panelFor(body,status=200,nodes=['a1','a2','a3']){
  const parts={},events={},document={hidden:false,body:element(),activeElement:null,createElement:element,getElementById:id=>parts[id]||(parts[id]=element()),querySelector:()=>null,addEventListener(){}};
  const state={auth:{provider:'email'},account:'synthetic-account',onboardingComplete:true};
  const box={document,Promise,Date,JSON,Number,CustomEvent:class{constructor(type){this.type=type;}},Event:class{constructor(type){this.type=type;}},setInterval(){return 1;},clearInterval(){},
    GroveSimulation:{isLive:()=>true,getState:()=>state,realId:id=>String(id)},
    GroveView:{graph:()=>({graphData:()=>({nodes:nodes.map(id=>({id}))})})},
    GroveLive:{workers:async()=>({ok:true,status:200,body:feed()}),workerRecord:async()=>({ok:status===200,status,body})},
    addEventListener:(type,fn)=>{(events[type]||(events[type]=[])).push(fn);},dispatchEvent(){}};
  box.window=box;vm.createContext(box);
  vm.runInContext(fs.readFileSync(path.join(web,'agents.js'),'utf8'),box,{filename:'agents.js'});
  return {box,panel:document.body.children.find(n=>n.id==='grove-worker-panel')};
}
async function show(body,status,nodes){const h=panelFor(body,status,nodes);await h.box.GroveCare.refresh();return {html:h.panel.innerHTML,fresh:h.box.GroveCare.summary().freshness};}
const follow=(html,agent)=>html.match(new RegExp('<button[^>]*data-worker-follow="'+agent+'"[^>]*>'))[0];
const enabled=(html,agent)=>!/\bdisabled\b/.test(follow(html,agent));
const READ_FAILED='Recorded work could not be read.',FRESH_FAILED='Freshness could not be read.';
async function refused(name,change){
  const body=record();change(body);const seen=await show(body);
  assert(seen.html.includes(READ_FAILED),name+' must be refused');assert(seen.html.includes(FRESH_FAILED),name+' must lose freshness');
  assert.equal(seen.fresh.percent,null,name+' has no percent');
  assert.equal(enabled(seen.html,'learner'),false,name+' keeps Follow disabled');
  assert(seen.html.includes('data-worker="learner"'),name+' keeps the status feed');
}
const checks=[
  ['Transport mirrors the guarded worker read',async()=>{
    const requests=[],win={location:{pathname:'/',href:'https://example.test/'},addEventListener(){},dispatchEvent(){},crypto:{randomUUID:()=> 'nonce'},fetch:(url,init)=>new Promise(resolve=>requests.push({url,init,resolve}))};
    vm.runInNewContext(fs.readFileSync(path.join(web,'live.js'),'utf8'),{window:win,URL,URLSearchParams,Promise,JSON,Number,CustomEvent:class{constructor(type){this.type=type;}},localStorage:{setItem(){}}});
    const G=win.GroveLive;assert.equal(G.ENDPOINTS.workerRecord,'/v1/views/workers/record');
    const pending=G.workerRecord();assert.equal(requests[0].url,'/v1/views/workers/record');assert.equal(requests[0].init.method,'GET');assert.equal(requests[0].init.credentials,'same-origin');
    G.invalidateSession();requests[0].resolve({ok:true,status:200,text:()=>Promise.resolve('{"private":"old"}')});
    const stale=await pending;assert(stale.stale);assert.equal(stale.body,null);
  }],
  ['A valid record gives Follow and the trail words',async()=>{
    const {html}=await show(record());assert(enabled(html,'learner'));assert(/Last recorded change: 3 items, [^<]+\./.test(html));
    assert(!html.includes('Those items are not on your map.'));
  }],
  ['One item reads as one item',async()=>{const {html}=await show(record(['a1']));assert(/Last recorded change: 1 item, /.test(html));}],
  ['No run and no change stay disabled with plain words',async()=>{
    const {html}=await show(record());
    assert(html.includes('No run recorded yet. Nothing to follow.'));assert(html.includes('Last run recorded no item changes. Nothing to follow.'));
    assert.equal(enabled(html,'curator'),false);assert.equal(enabled(html,'connector'),false);
  }],
  ['A trail off the map stays disabled and says so',async()=>{
    const {html}=await show(record(['gone-1','gone-2']));assert.equal(enabled(html,'learner'),false);assert(html.includes('Those items are not on your map.'));
  }],
  ['A failed read keeps the feed and says so',async()=>{
    const {html,fresh}=await show({error:{code:'unavailable'}},503);
    assert(html.includes(READ_FAILED));assert(html.includes('data-worker="curator"'));assert(html.includes(FRESH_FAILED));assert.deepEqual({...fresh},{percent:null,text:FRESH_FAILED});
  }],
  ['An extra key is refused at every level',async()=>{
    await refused('Root extra key',b=>{b.extra=1;});await refused('Agent extra key',b=>{b.agents[0].title='x';});
    await refused('Trail extra key',b=>{b.agents[0].trail.run_id='x';});await refused('Last run extra key',b=>{b.agents[0].last_run.prompt='x';});await refused('Freshness extra key',b=>{b.freshness.extra=1;});
  }],
  ['A bad schema or shape is refused',async()=>{
    await refused('Bad schema',b=>{b.schema='grove.worker-record.v2';});await refused('Bad kind',b=>{b.observation_kind='guess';});
    await refused('Wrong order',b=>{b.agents.reverse();});await refused('Two agents',b=>{b.agents.pop();});await refused('Bad stamp',b=>{b.observed_at='yesterday';});
    await refused('Unknown outcome',b=>{b.agents[0].last_run.outcome='maybe';});await refused('Trail with no run',b=>{b.agents[0].last_run=null;});
  }],
  ['An id list above 50 is refused, and 50 passes',async()=>{
    const ids=n=>Array.from({length:n},(_,i)=>'a'+i);
    await refused('51 ids',b=>{b.agents[0].trail=trail(ids(51));});await refused('Repeated id',b=>{b.agents[0].trail=trail(['a1','a1']);});await refused('Empty trail',b=>{b.agents[0].trail=trail([]);});
    await refused('Count below ids',b=>{b.agents[0].trail.changed_items=1;});
    const {html}=await show(record(ids(50)),200,ids(50));assert(!html.includes(READ_FAILED));assert(enabled(html,'learner'));
  }],
  ['Impossible freshness is refused',async()=>{
    await refused('Fresh above dated',b=>{b.freshness.fresh_items=191;});await refused('Dated above total',b=>{b.freshness.dated_items=201;});
    await refused('Wrong window',b=>{b.freshness.window_days=7;});await refused('Dated with no stamp',b=>{b.freshness.newest_change_at=null;});
  }],
  ['Freshness words and percent equal the record',async()=>{
    const {html,fresh}=await show(record());assert.equal(fresh.percent,75);
    assert(/^150 of 200 items updated in the last 30 days\. Latest update [^.]+\. 10 items have no update time\.$/.test(fresh.text),fresh.text);
    assert(html.includes('data-worker-freshness'));assert(html.includes('Grove freshness'));assert(html.includes('width:75%'));assert(html.includes('Older than 30 days'));assert(!html.includes('Refreshing soon'));
  }],
  ['No items has no percent, no bar and plain words',async()=>{
    const {html,fresh}=await show(record(['a1'],{...FRESH,total_items:0,dated_items:0,fresh_items:0,newest_change_at:null,oldest_change_at:null}));
    assert.deepEqual({...fresh},{percent:null,text:'No items recorded yet, so freshness has no value.'});assert(!html.includes('gp-freshness-track'));
  }],
  ['Nothing read has no percent',async()=>{const h=panelFor(record());assert.equal(h.box.GroveCare.summary().freshness.percent,null);assert.equal(h.box.GroveCare.summary().fresh,null);}],
];
(async()=>{
  let failed=0;
  for(const [name,run] of checks){try{await run();console.log('PASS: '+name);}catch(error){failed++;console.error('FAIL: '+name+'\n'+error.stack);}}
  console.log(`Worker record: ${checks.length-failed} passed, ${failed} failed`);
  if(failed)process.exitCode=1;
})();
