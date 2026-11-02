// Live mode against a fake server: live.js (paths, fetch helpers, positions batches), the
// simulation.js adapter (routes, self alias, live counts, empty imports), native-app.js map
// positions (saved once, kept after a reload and after new items arrive, retried after a failed
// save), product.js sign-in (session, Google, email code, Apple, logout), and the built page
// (no fixture, no hook name, none of the banned words).
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path'),cp=require('child_process');
const web=path.join(__dirname,'..');
const read=f=>fs.readFileSync(path.join(web,f),'utf8');
// Build the page from the current sources: the dist checks below read what build.py makes now.
cp.execFileSync('python3',[path.join(web,'build.py')],{stdio:'ignore'});
const settle=async(n=40)=>{for(let i=0;i<n;i++)await new Promise(r=>setImmediate(r));};

// ---------- a fake server with the HTTP contract the views read ----------
const synthetic=JSON.parse(read('checks/fixture.synthetic.json'));
const KIND={company:'organisation',project:'plan',client:'place',pattern:'assumption'};
function liveWorld(){
  const nodes=synthetic.elements.nodes.map(w=>Object.assign({},w.data,{type:KIND[w.data.type]||w.data.type}));
  nodes.push(Object.assign({},nodes[0],{id:'k001',label:'Sourdough method',type:'recipe',summary:'A recipe kind the sim does not know'}));
  return {nodes,edges:synthetic.elements.edges.map(w=>Object.assign({},w.data)),selfId:'p001',positions:{},session:true,code:'246810'};
}
function makeServer(world){
  const calls=[];
  const reply=(status,body)=>Promise.resolve({ok:status>=200&&status<300,status,text:()=>Promise.resolve(body===undefined?'':JSON.stringify(body))});
  const typed=(status,code,message)=>reply(status,{error:{code,message,details:{},retryable:false}});
  function fetch(url,init){
    init=init||{};
    const u=new URL(url,'http://grove.test/'),method=(init.method||'GET').toUpperCase();
    const body=init.body===undefined?undefined:JSON.parse(init.body);
    calls.push({path:u.pathname,search:u.search,method,init,body});
    const ids=new Set(world.nodes.map(n=>n.id));
    if(u.pathname==='/v1/views/session')return world.session?reply(200,{signed_in:true,self_item_id:world.selfId||null}):typed(401,'unauthenticated','Sign in to continue');
    if(u.pathname==='/v1/views/graph'){
      if(world.graphStatus)return typed(world.graphStatus,'unauthenticated','Sign in to continue');
      const nodes=world.nodes.map(n=>({data:Object.assign({},n,world.positions[n.id]||{},n.id===world.selfId?{self:true}:{})}));
      return reply(200,{elements:{nodes,edges:world.edges.filter(e=>ids.has(e.source)&&ids.has(e.target)).map(e=>({data:e}))},stats:{total_nodes:nodes.length},time:{min:null,max:null},generated_at:'2026-11-02T00:00:00Z',as_of:null,known_as_of:null,self_item_id:world.selfId||null});
    }
    if(u.pathname.indexOf('/v1/views/node/')===0){
      const n=world.nodes.find(x=>x.id===decodeURIComponent(u.pathname.slice('/v1/views/node/'.length)));
      return n?reply(200,{node:Object.assign({},n,{title:n.label,content:n.summary,status_field_value:n.status}),required_props:[],optional_props:[],required_prop_names:[],status_field:'status'}):typed(404,'not_found','No such item');
    }
    if(u.pathname==='/v1/views/schema')return reply(200,{});
    if(u.pathname==='/v1/views/health')return reply(200,{status:'healthy',summary:'Your Grove is ready',checks:{graph:'ready',sources:'ready'},nodes:world.nodes.length,edges:world.edges.length,by_type:{},overall:{state:'healthy'}});
    if(u.pathname==='/v1/views/positions'&&method==='POST'){
      assert.equal(init.headers['Content-Type'],'application/json');
      assert(Array.isArray(body.positions)&&body.positions.length>=1&&body.positions.length<=5000,'1 to 5000 positions per request');
      if(world.failPositions)return typed(503,'unavailable','Try again later');
      let placed=0,kept=0,unknown=0;
      for(const p of body.positions){
        assert(Number.isFinite(p.x)&&Number.isFinite(p.y)&&Math.abs(p.x)<=1e7&&Math.abs(p.y)<=1e7,'finite coordinates');
        if(!ids.has(p.id))unknown++;else if(world.positions[p.id])kept++;else{world.positions[p.id]={x:p.x,y:p.y};placed++;}
      }
      return reply(200,{placed,kept,unknown});
    }
    if(u.pathname==='/v1/auth/email/start'&&method==='POST')return world.emailStartFails?typed(400,'invalid_arguments','That address cannot receive a code'):reply(200,{});
    if(u.pathname==='/v1/auth/email/verify'&&method==='POST'){if(body.code!==world.code)return typed(401,'unauthenticated','The code does not match');world.session=true;return reply(200,{signed_in:true});}
    if(u.pathname==='/v1/auth/logout'&&method==='POST'){world.session=false;return reply(200,{});}
    return typed(404,'not_found','No such path');
  }
  return {fetch,calls,world};
}

// ---------- a browser-shaped sandbox ----------
function makeElement(tag){
  const classes=new Set(),attrs={};
  const el={tagName:String(tag||'div').toUpperCase(),innerHTML:'',textContent:'',hidden:false,style:{setProperty(){},removeProperty(){}},dataset:{},children:[],offsetWidth:100,offsetHeight:100,scrollTop:0,
    classList:{add:(...k)=>k.forEach(x=>classes.add(x)),remove:(...k)=>k.forEach(x=>classes.delete(x)),toggle(k,on){if(on===undefined)on=!classes.has(k);if(on)classes.add(k);else classes.delete(k);return on;},contains:k=>classes.has(k)},
    setAttribute(k,v){attrs[k]=String(v);},getAttribute:k=>(k in attrs?attrs[k]:null),hasAttribute:k=>k in attrs,removeAttribute(k){delete attrs[k];},
    addEventListener(){},removeEventListener(){},querySelector:()=>null,querySelectorAll:()=>[],appendChild(c){el.children.push(c);return c;},append(){},remove(){},contains:()=>false,closest:()=>null,focus(){},click(){},
    getBoundingClientRect:()=>({left:0,top:0,width:100,height:100,right:100,bottom:100})};
  return el;
}
function chainForce(){const f=function(){};['strength','distanceMax','radius','iterations','distance','x','y','id','links'].forEach(k=>{f[k]=function(){return f;};});return f;}
function makeGraph(){
  let data={nodes:[],links:[]},zoom=1;const forces={link:chainForce()};
  const g={graphData(d){if(!arguments.length)return data;data=d;return proxy;},d3Force(name,f){if(arguments.length<2)return forces[name];forces[name]=f;return proxy;},
    zoom(v){if(!arguments.length)return zoom;zoom=v;return proxy;},centerAt(){return arguments.length?proxy:{x:0,y:0};},graph2ScreenCoords:(x,y)=>({x,y}),screen2GraphCoords:(x,y)=>({x,y})};
  const proxy=new Proxy(g,{get:(t,k)=>(k in t?t[k]:k==='then'||typeof k!=='string'?undefined:function(){return proxy;})});
  return proxy;
}
function page(server,store,files,hook,size){
  const elements={},winListeners={},errors=[],warnings=[],assigned=[];
  const document={hidden:false,body:makeElement('body'),documentElement:makeElement('html'),createElement:makeElement,getElementById:id=>elements[id]||(elements[id]=makeElement('div')),querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},removeEventListener(){}};
  const sandbox={URL,URLSearchParams,Math,JSON,Date,Promise,
    console:{log(){},info(){},warn:(...a)=>warnings.push(a),error:(...a)=>errors.push(a)},
    setTimeout:()=>0,clearTimeout(){},setInterval:()=>0,clearInterval(){},requestAnimationFrame:()=>0,cancelAnimationFrame(){},
    performance:{now:()=>1000},innerWidth:(size||[1440,900])[0],innerHeight:(size||[1440,900])[1],devicePixelRatio:1,document,navigator:{},
    matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}}),getComputedStyle:()=>({getPropertyValue:()=>''}),
    localStorage:{getItem:k=>(k in store?store[k]:null),setItem:(k,v)=>{store[k]=String(v);},removeItem:k=>{delete store[k];}},
    location:{href:'http://grove.test/',search:'',pathname:'/',assign:u=>assigned.push(u)},
    fetch:server.fetch,ForceGraph:()=>()=>makeGraph(),
    d3:{forceManyBody:chainForce,forceCollide:chainForce,forceX:chainForce,forceY:chainForce},
    CustomEvent:class{constructor(type,init){this.type=type;this.detail=init&&init.detail;}},
    addEventListener:(k,fn)=>{(winListeners[k]||(winListeners[k]=[])).push(fn);},
    dispatchEvent:e=>{(winListeners[e.type]||[]).forEach(fn=>fn(e));return true;}};
  sandbox.window=sandbox;vm.createContext(sandbox);
  for(const f of files){let source=read(f);if(f==='product.js'&&hook)source=source.replace('  render(true);maybeTour();',hook);vm.runInContext(source,sandbox,{filename:f});}
  return {sandbox,elements,errors,warnings,assigned};
}
const GRAPH_FILES=['live.js','layout-keep.js','simulation.js','native-app.js'];
const positionsCalls=server=>server.calls.filter(c=>c.path==='/v1/views/positions');
const fgPositions=pg=>Object.fromEntries(pg.sandbox.GroveView.nodes().map(n=>[n.id,{x:n.x,y:n.y}]));

(async()=>{
  // 1. live.js: one endpoint map, same-origin fetch helpers, positions in batches of 5000.
  {
    const server=makeServer(liveWorld()),pg=page(server,{},['live.js']),G=pg.sandbox.GroveLive;
    assert.deepStrictEqual(JSON.parse(JSON.stringify(G.ENDPOINTS)),{session:'/v1/views/session',graph:'/v1/views/graph',node:'/v1/views/node/',schema:'/v1/views/schema',health:'/v1/views/health',positions:'/v1/views/positions',googleStart:'/v1/auth/google/start?next=/',emailStart:'/v1/auth/email/start',emailVerify:'/v1/auth/email/verify',logout:'/v1/auth/logout'});
    const res=await G.getJSON('/v1/views/health');assert(res.ok&&res.body.overall.state==='healthy');
    assert.equal(server.calls[0].method,'GET');assert.equal(server.calls[0].init.credentials,'same-origin');
    const many=[];for(let i=0;i<12000;i++)many.push({id:i<11990?synthetic.elements.nodes[i%222].data.id:'x'+i,x:i,y:-i});
    many.push({id:'bad',x:NaN,y:0},{id:'far',x:2e7,y:0},{id:7,x:1,y:1},{id:'inf',x:Infinity,y:1});
    const saved=await G.savePositions(many);const posts=positionsCalls(server);
    assert.deepStrictEqual(posts.map(c=>c.body.positions.length),[5000,5000,2000],'Positions go in batches of at most 5000');
    assert(posts.every(c=>c.init.credentials==='same-origin'&&c.method==='POST'));assert.equal(saved.length,3);
    assert(!posts.some(c=>c.body.positions.some(p=>['bad','far','inf'].includes(p.id)||typeof p.id!=='string')),'Unusable positions are dropped');
    server.world.failPositions=true;await assert.rejects(G.savePositions([{id:'p002',x:1,y:2}]));
    assert.equal(G.errorText({error:{code:'not_found',message:'No such item'}}),'No such item');
  }

  // 2. A first visit: the map is the sim's layout of the graph, saved once, the self item under its real id.
  const world=liveWorld(),server=makeServer(world),store={};
  const first=page(server,store,GRAPH_FILES);await settle();
  assert.deepStrictEqual(first.errors,[],'The live page renders without errors');
  const S=first.sandbox.GroveSimulation;assert.equal(S.isLive(),true);
  assert(server.calls.some(c=>c.path==='/v1/views/graph')&&server.calls.some(c=>c.path==='/v1/views/schema'));
  assert(server.calls.every(c=>c.init.credentials==='same-origin'),'Every call carries the session cookie');
  const firstNodes=first.sandbox.GroveView.nodes();
  assert.equal(firstNodes.length,world.nodes.length);
  assert(firstNodes.some(n=>n.id==='grove-self')&&!firstNodes.some(n=>n.id==='p001'),'The self item takes the identity node id');
  const links=first.sandbox.GroveView.graph().graphData().links;
  assert(links.some(l=>l.source==='grove-self'||l.target==='grove-self')&&!links.some(l=>l.source==='p001'||l.target==='p001'),'Edges follow the self alias');
  let posts=positionsCalls(server);assert.equal(posts.length,1,'One save on the first visit');
  const sentIds=posts[0].body.positions.map(p=>p.id);
  assert.deepStrictEqual([...sentIds].sort(),world.nodes.map(n=>n.id).sort(),'Every item is saved under its real id');
  assert(!sentIds.includes('grove-self'));
  const firstMap=fgPositions(first);
  for(const p of posts[0].body.positions){const view=p.id==='p001'?'grove-self':p.id;assert(firstMap[view].x===p.x&&firstMap[view].y===p.y,'The saved position is the drawn one: '+p.id);}
  // Kinds: organisation and plan are hubs (pinned, mass-sized); the legend lists every kind by its own name.
  const org=firstNodes.find(n=>n.type==='organisation'),plan=firstNodes.find(n=>n.type==='plan'),place=firstNodes.find(n=>n.type==='place');
  assert(org.fx!=null&&org._hubR&&plan.fx!=null&&plan._hubR,'organisation and plan are hubs');assert(place.fx==null,'place is not a hub');
  const legend=first.elements['legend-list'].innerHTML;
  for(const kind of ['organisation','plan','place','assumption','person','task','decision','event','observation','recipe'])assert(legend.includes('data-type="'+kind+'"'),'Legend lists '+kind);
  assert(!/data-type="(company|project|client|pattern)"/.test(legend),'The legend shows the real kind names');
  assert(legend.indexOf('data-type="organisation"')<legend.indexOf('data-type="person"'),'Hub kinds lead the legend');
  // Adapter routes and live counts.
  const api=first.sandbox.groveApi,before=server.calls.length;
  let detail=await (await api('/api/node/grove-self')).json();assert.equal(server.calls[server.calls.length-1].path,'/v1/views/node/p001');assert.equal(detail.node.id,'grove-self');assert.equal(detail.node.title,world.nodes[0].label);
  await api('/api/node/t001');assert.equal(server.calls[server.calls.length-1].path,'/v1/views/node/t001');
  await api('/api/graph?as_of=2026-10-01T00:00:00Z');assert.equal(server.calls[server.calls.length-1].path+server.calls[server.calls.length-1].search,'/v1/views/graph?as_of=2026-10-01T00:00:00Z');
  assert.equal((await (await api('/api/health')).json()).overall.state,'healthy');assert.equal(server.calls[server.calls.length-1].path,'/v1/views/health');
  await api('/api/schema');assert.equal(server.calls[server.calls.length-1].path,'/v1/views/schema');
  const callsNow=server.calls.length,history=await api('/api/history');assert.equal(history.ok,false);assert.equal(history.status,404);assert((await history.json()).error);assert.equal(server.calls.length,callsNow,'Unknown paths never reach the server');
  assert.equal(server.calls.length-before,5);
  assert.equal(S.getState().currentNodeCount,world.nodes.length);assert.equal(S.getState().totalNodeCount,world.nodes.length);
  assert.equal(S.getState().currentEdgeCount,world.edges.length);assert.equal(S.getState().currentNodeTypes.organisation,8);
  assert.equal(S.realId('grove-self'),'p001');assert.equal(S.realId('t001'),'t001');
  await S.importSource('conversations',{provider:'chatgpt',name:'chatgpt-export.zip'});assert.equal(S.getState().accountImports.chatgpt.status,'importing');assert.equal(S.getState().currentNodeCount,world.nodes.length,'Imports add nothing in live mode');

  // 3. A reload in a phone-sized window (a different seed layout): every item comes back
  //    exactly where it was; nothing is saved again.
  const reload=page(server,store,GRAPH_FILES,null,[393,852]);await settle();
  assert.deepStrictEqual(reload.errors,[]);assert.equal(positionsCalls(server).length,1,'A reload saves nothing');
  assert.deepStrictEqual(fgPositions(reload),firstMap,'A reload keeps every position');

  // 4. New items arrive: the old ones keep their positions; only the new ones are saved.
  const fresh=[{id:'n-org',label:'Wrenfield Cooperative',type:'organisation'},{id:'n-t1',label:'Plan the cooperative fair',type:'task'},{id:'n-t2',label:'Order the fair banners',type:'task'},{id:'n-t3',label:'Book the fair hall',type:'task'},{id:'n-t4',label:'Proof the spring catalogue',type:'task'},{id:'n-e1',label:'Open studio day',type:'event'}];
  fresh.forEach(n=>world.nodes.push(Object.assign({},world.nodes[1],n,{summary:''})));
  world.edges.push({id:'rn1',source:'n-t1',target:'n-org',verb:'part of',label:'part of'},{id:'rn2',source:'n-t2',target:'n-org',verb:'part of',label:'part of'},{id:'rn3',source:'n-t3',target:'n-org',verb:'part of',label:'part of'},{id:'rn4',source:'n-t4',target:'pr001',verb:'part of',label:'part of'});
  const grown=page(server,store,GRAPH_FILES);await settle();
  assert.deepStrictEqual(grown.errors,[]);
  const grownMap=fgPositions(grown);
  for(const id of Object.keys(firstMap))assert(grownMap[id].x===firstMap[id].x&&grownMap[id].y===firstMap[id].y,'An arrival never moves a placed item: '+id);
  posts=positionsCalls(server);assert.equal(posts.length,2);
  assert.deepStrictEqual(posts[1].body.positions.map(p=>p.id).sort(),fresh.map(n=>n.id).sort(),'Only the new items are saved');
  const tFair=grownMap['n-t1'],hub=grownMap['n-org'];assert(Math.hypot(tFair.x-hub.x,tFair.y-hub.y)<600,'A new task sits near its new hub');

  // 5. A failed save keeps the map; the next load retries with the same positions.
  const world2=liveWorld();world2.selfId=null;world2.failPositions=true;
  const server2=makeServer(world2),store2={};
  const failing=page(server2,store2,GRAPH_FILES);await settle();
  assert.deepStrictEqual(failing.errors,[],'A failed save does not break rendering');
  assert.equal(failing.sandbox.GroveView.nodes().length,world2.nodes.length);assert(failing.elements.loading.classList.contains('hidden'),'The map finished loading');
  assert.equal(positionsCalls(server2).length,1);assert(failing.warnings.length>=1,'The failed save is reported');
  const failedMap=fgPositions(failing);world2.failPositions=false;
  await failing.sandbox.reloadGrove();await settle();
  const retry=positionsCalls(server2);assert.equal(retry.length,2,'The next load retries the save');
  assert.deepStrictEqual(retry[1].body.positions.map(p=>p.id).sort(),world2.nodes.map(n=>n.id).sort());
  for(const p of retry[1].body.positions)assert(p.x===failedMap[p.id].x&&p.y===failedMap[p.id].y,'The retry saves the positions on screen');
  // No self item: the sim's identity node joins once onboarding starts, and is never saved.
  const S2=failing.sandbox.GroveSimulation;S2.setName('Sam');S2.setStep(1);await failing.sandbox.reloadGrove();await settle();
  const ids2=failing.sandbox.GroveView.nodes().map(n=>n.id);assert(ids2.includes('grove-self'));
  const selfLinks=failing.sandbox.GroveView.graph().graphData().links.filter(l=>l.source==='grove-self');
  assert(selfLinks.length>0&&selfLinks.length<=16,'The identity node links to hubs, as in the sim');
  const before2=server2.calls.length,selfDetail=await (await failing.sandbox.groveApi('/api/node/grove-self')).json();
  assert.equal(server2.calls.length,before2,'The identity node detail stays local');assert.equal(selfDetail.node.title,'Sam');
  assert(!positionsCalls(server2).some(c=>c.body.positions.some(p=>p.id==='grove-self')),'The identity node is never saved');
  assert.equal(S2.getState().currentNodeCount,world2.nodes.length,'Counts are real items only');
  // A signed-out graph read answers a typed error as text.
  world2.graphStatus=401;const denied=await failing.sandbox.groveApi('/api/graph');assert.equal(denied.ok,false);assert.equal(denied.status,401);assert.equal((await denied.json()).error,'Sign in to continue');

  // 6. Sign-in in live mode (product.js): session check, Google, Apple, email code, logout.
  const AUTH_FILES=['live.js','simulation.js','provider-icons.js','app-screens.js','whatsapp-guide.js','product.js'];
  const HOOK='  window.productTest={ui,handle,chooseSignIn,startEmail,verifyEmail,render,html:()=>modalRoot.innerHTML};render(true);';
  {
    const w=liveWorld();w.session=false;const sv=makeServer(w),st={},pg=page(sv,st,AUTH_FILES,HOOK),p=pg.sandbox.productTest,A=pg.sandbox.GroveSimulation;
    assert.equal(p.ui.authStage,'checking');assert(p.html().includes('gp-auth-loading'),'The sign-in card waits for the session');
    await settle();assert.equal(sv.calls[0].path,'/v1/views/session');assert.equal(p.ui.authStage,'signin');assert(p.html().includes('Continue with Google'));assert.equal(A.getState().auth,null);
    const n0=sv.calls.length;p.chooseSignIn('google');assert.deepStrictEqual(pg.assigned,['/v1/auth/google/start?next=/']);assert.equal(sv.calls.length,n0);
    p.chooseSignIn('apple');assert(p.html().includes('Not available yet'));assert.equal(sv.calls.length,n0,'Apple calls nothing');assert.equal(A.getState().auth,null);
    w.emailStartFails=true;await p.startEmail('anas@example.com');assert.equal(p.ui.authStage,'signin');assert(p.ui.error.includes('could not send a code'));
    w.emailStartFails=false;await p.startEmail(' anas@example.com ');let call=sv.calls[sv.calls.length-1];
    assert.equal(call.path,'/v1/auth/email/start');assert.equal(call.method,'POST');assert.deepStrictEqual(call.body,{email:'anas@example.com'});assert.equal(call.init.headers['Content-Type'],'application/json');
    assert.equal(p.ui.authStage,'code');assert(p.html().includes('Check your inbox'));assert(!/482|use-code|gp-email-preview/.test(p.html()),'No code hint');
    await p.verifyEmail('000000');call=sv.calls[sv.calls.length-1];assert.equal(call.path,'/v1/auth/email/verify');assert.deepStrictEqual(call.body,{email:'anas@example.com',code:'000000'});
    assert.equal(A.getState().auth,null);assert(p.ui.error.includes('code doesn’t match'));
    await p.verifyEmail('246810');assert.equal(A.getState().auth.provider,'email');assert.equal(A.getState().auth.email,'anas@example.com');assert.equal(p.ui.authStage,null);
    p.handle('logout');await settle();call=sv.calls[sv.calls.length-1];assert.equal(call.path,'/v1/auth/logout');assert.equal(call.method,'POST');
    assert.equal(A.getState().auth,null);assert.equal(p.ui.authStage,'signin');assert(sv.calls.every(c=>c.init.credentials==='same-origin'));
    // Back from Google: a session with no local sign-in signs in.
    w.session=true;const back=page(sv,{},AUTH_FILES,HOOK);await settle();
    assert.equal(back.sandbox.GroveSimulation.getState().auth.provider,'google');assert.equal(back.sandbox.productTest.ui.authStage,null);
    // A stale local sign-in with no server session goes back to the sign-in screen.
    w.session=false;const stale=page(sv,st,AUTH_FILES,HOOK);stale.sandbox.GroveSimulation.signIn('email','anas@example.com');await settle();
    assert.equal(stale.sandbox.GroveSimulation.getState().auth,null);assert.equal(stale.sandbox.productTest.ui.authStage,'signin');
  }

  // 7. The production page: live scripts first, and no fixture anywhere in the built file.
  const index=read('index.html');
  assert(!/src="[^"]*fixture|fixture\.js/.test(index),'index.html loads no fixture');assert(index.includes('<title>Grove</title>'));
  assert(index.indexOf('./live.js')<index.indexOf('./simulation.js')&&index.indexOf('./layout-keep.js')<index.indexOf('./native-app.js'));
  const dist=read('dist/index.html');
  assert(!dist.includes('GROVE_FIXTURE'),'The bundle names no fixture hook');
  assert(!dist.includes('fixture.js')&&!dist.includes('fixture.json'),'The bundle references no fixture file');
  assert(!/482910|data-action="use-code"|Delete my Grove/.test(dist),'Simulation-only sign-in and resets are gone');
  const banned=new RegExp([['ham','za'],['ham','py'],['aa','tar']].map(p=>p.join('')).join('|'),'i');
  assert(!banned.test(dist),'The bundle holds none of the banned words');
  assert(dist.includes('window.GroveLive')&&dist.includes('GroveLayoutKeep')&&dist.includes('<title>Grove</title>'));
  // build.py percent-encodes A and a inside data: URLs; a data: URL is percent-decoded before
  // its base64 is read, so every vendored font still decodes to its exact bytes.
  const fontFiles=[...new Set([...read('vendor/fonts.css').matchAll(/url\((fonts\/[^)]+)\)/g)].map(m=>m[1]))];
  const fontBytes=fontFiles.map(f=>fs.readFileSync(path.join(web,'vendor',f)));
  const encoded=[...new Set([...dist.matchAll(/url\((data:font\/woff2;base64,[^)]*%(?:41|61)[^)]*)\)/g)].map(m=>m[1]))];
  assert.equal(encoded.length,fontFiles.length,'Every vendored font is embedded, encoded');
  for(const uri of encoded){const bytes=Buffer.from(await (await fetch(uri)).arrayBuffer());assert(fontBytes.some(b=>b.equals(bytes)),'An encoded font decodes to its vendored bytes');}
  console.log('PASS: one endpoint map with same-origin calls and batched positions, live routes and the self alias, live counts and empty imports, first-visit save under real ids, kept positions after a reload and after new items arrive, only new items saved, failed saves retried with the same positions, starter kinds as hubs and legend names, local identity node, typed errors, session check, Google start, Apple not available, email code start and verify with errors, logout, and a built page with no fixture');
})().catch(e=>{console.error(e);process.exitCode=1;});
