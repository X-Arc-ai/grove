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
      if(world.nodeStatus)return typed(world.nodeStatus,'unauthenticated','Sign in to continue');
      const n=world.nodes.find(x=>x.id===decodeURIComponent(u.pathname.slice('/v1/views/node/'.length)));
      return n?reply(200,{node:Object.assign({},n,{title:n.label,content:n.summary,status_field_value:n.status}),required_props:[],optional_props:[],required_prop_names:[],status_field:'status'}):typed(404,'not_found','No such item');
    }
    if(u.pathname==='/v1/views/schema')return reply(200,{});
    if(u.pathname==='/v1/views/health')return reply(200,{status:'healthy',summary:'Your Grove is ready',checks:{graph:'ready',sources:'ready'},nodes:world.nodes.length,edges:world.edges.length,by_type:{},overall:{state:'healthy'}});
    if(u.pathname==='/v1/views/positions'&&method==='POST'){
      assert.equal(init.headers['Content-Type'],'application/json');
      assert(Array.isArray(body.positions)&&body.positions.length>=1&&body.positions.length<=5000,'1 to 5000 positions per request');
      if(world.positionsStatus)return typed(world.positionsStatus,'unauthenticated','Sign in to continue');
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
    if(u.pathname==='/v1/auth/logout'&&method==='POST'){
      if(world.logoutThrows)return Promise.reject(new TypeError('Failed to fetch'));
      if(world.logoutStatus)return typed(world.logoutStatus,'internal','the server failed');
      world.session=false;return reply(200,{});
    }
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
function page(server,store,files,hook,size,prepare){
  const elements={},winListeners={},errors=[],warnings=[],assigned=[],intervals=[];
  if(prepare)prepare(elements);
  const document={hidden:false,body:makeElement('body'),documentElement:makeElement('html'),createElement:makeElement,getElementById:id=>elements[id]||(elements[id]=makeElement('div')),querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},removeEventListener(){}};
  const sandbox={URL,URLSearchParams,Math,JSON,Date,Promise,
    console:{log(){},info(){},warn:(...a)=>warnings.push(a),error:(...a)=>errors.push(a)},
    setTimeout:()=>0,clearTimeout(){},setInterval:(fn,ms)=>intervals.push({fn,ms}),clearInterval(){},requestAnimationFrame:()=>0,cancelAnimationFrame(){},
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
  return {sandbox,elements,errors,warnings,assigned,intervals};
}
const GRAPH_FILES=['live.js','layout-keep.js','simulation.js','native-app.js'];
const positionsCalls=server=>server.calls.filter(c=>c.path==='/v1/views/positions');
const fgPositions=pg=>Object.fromEntries(pg.sandbox.GroveView.nodes().map(n=>[n.id,{x:n.x,y:n.y}]));
const decodeHtml=s=>s.replace(/&#x([0-9a-f]+);/gi,(_,h)=>String.fromCodePoint(parseInt(h,16))).replace(/&#(\d+);/g,(_,d)=>String.fromCodePoint(+d))
  .replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
// A legend list whose rows behave as the browser's do: each row reads its data-type back decoded,
// and a click runs the row's own handler.
function legendRows(elements){
  const el=makeElement('div');let rows=[];
  el.querySelectorAll=sel=>{
    if(sel!=='.legend-row')return [];
    rows=[...el.innerHTML.matchAll(/<div class="legend-row[^"]*" data-type="([^"]*)"/g)].map(m=>{let onclick=null;return {getAttribute:k=>(k==='data-type'?decodeHtml(m[1]):null),addEventListener:(k,fn)=>{if(k==='click')onclick=fn;},click:()=>onclick&&onclick()};});
    return rows;
  };
  el.rows=()=>rows;elements['legend-list']=el;
}

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
  const fresh=[{id:'n-org',label:'Wrenfield Cooperative',type:'organisation'},{id:'n-t1',label:'Plan the cooperative fair',type:'task'},{id:'n-t2',label:'Order the fair banners',type:'task'},{id:'n-t3',label:'Book the fair hall',type:'task'},{id:'n-t4',label:'Proof the spring catalogue',type:'task'},{id:'n-e1',label:'Open studio day',type:'event'},{id:'n-o1',label:'Noticed a longer queue at the fair',type:'observation'}];
  fresh.forEach(n=>world.nodes.push(Object.assign({},world.nodes[1],n,{summary:''})));
  world.edges.push({id:'rn1',source:'n-t1',target:'n-org',verb:'part of',label:'part of'},{id:'rn2',source:'n-t2',target:'n-org',verb:'part of',label:'part of'},{id:'rn3',source:'n-t3',target:'n-org',verb:'part of',label:'part of'},{id:'rn4',source:'n-t4',target:'pr001',verb:'part of',label:'part of'},{id:'rn5',source:'n-o1',target:'p012',verb:'about',label:'about'});
  const grown=page(server,store,GRAPH_FILES);await settle();
  assert.deepStrictEqual(grown.errors,[]);
  const grownMap=fgPositions(grown);
  for(const id of Object.keys(firstMap))assert(grownMap[id].x===firstMap[id].x&&grownMap[id].y===firstMap[id].y,'An arrival never moves a placed item: '+id);
  posts=positionsCalls(server);assert.equal(posts.length,2);
  assert.deepStrictEqual(posts[1].body.positions.map(p=>p.id).sort(),fresh.map(n=>n.id).sort(),'Only the new items are saved');
  const tFair=grownMap['n-t1'],hub=grownMap['n-org'];assert(Math.hypot(tFair.x-hub.x,tFair.y-hub.y)<600,'A new task sits near its new hub');
  // A new leaf whose only link is to a leaf (no hub of its own) sits by that leaf, not by a hub
  // one hop away whose seed is elsewhere (the browser test saw such arrivals land 700 px off).
  const seen=grownMap['n-o1'],friend=grownMap['p012'];assert(Math.hypot(seen.x-friend.x,seen.y-friend.y)<150,'A new leaf sits near the leaf it links to');

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
  // With no name known, the identity node says You (the sim's example name is fixture mode only).
  const S2=failing.sandbox.GroveSimulation;S2.setStep(1);await failing.sandbox.reloadGrove();await settle();
  assert.equal((await (await failing.sandbox.groveApi('/api/node/grove-self')).json()).node.title,'You','The identity node says You');
  assert.equal(failing.sandbox.GroveView.nodes().find(n=>n.id==='grove-self').title,'You','The map labels the identity node You');
  S2.setName('Sam');await failing.sandbox.reloadGrove();await settle();
  const ids2=failing.sandbox.GroveView.nodes().map(n=>n.id);assert(ids2.includes('grove-self'));
  const selfLinks=failing.sandbox.GroveView.graph().graphData().links.filter(l=>l.source==='grove-self');
  assert(selfLinks.length>0&&selfLinks.length<=16,'The identity node links to hubs, as in the sim');
  const before2=server2.calls.length,selfDetail=await (await failing.sandbox.groveApi('/api/node/grove-self')).json();
  assert.equal(server2.calls.length,before2,'The identity node detail stays local');assert.equal(selfDetail.node.title,'Sam');
  assert(!positionsCalls(server2).some(c=>c.body.positions.some(p=>p.id==='grove-self')),'The identity node is never saved');
  assert.equal(S2.getState().currentNodeCount,world2.nodes.length,'Counts are real items only');
  // A signed-out graph read answers a typed error as text.
  world2.graphStatus=401;const denied=await failing.sandbox.groveApi('/api/graph');assert.equal(denied.ok,false);assert.equal(denied.status,401);assert.equal((await denied.json()).error,'Sign in to continue');
  // A signed-out first load: the graph read is refused and the loading cover steps aside, so the
  // sign-in card shows (the browser test saw "Unable to open this brain" cover the card).
  const outWorld=liveWorld();outWorld.session=false;outWorld.graphStatus=401;
  const signedOut=page(makeServer(outWorld),{},GRAPH_FILES);await settle();
  assert(signedOut.elements.loading.classList.contains('hidden'),'A signed-out load hides the loading cover');
  assert(!/Unable to open/.test(signedOut.elements.loading.textContent),'A signed-out load shows no failure text');
  assert.deepStrictEqual(signedOut.errors,[],'A signed-out load logs no error');

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
    // The email code keeps the address the person typed, and Account shows it.
    p.ui.panel=null;
    // Back from Google: a session with no local sign-in signs in, with no made-up identity.
    const done=()=>({'grove-minimal-codex-v3':JSON.stringify({version:3,schema:4,onboardingComplete:true,onboardingStep:4,tourComplete:true})});
    w.session=true;const back=page(sv,done(),AUTH_FILES,HOOK);await settle();
    const B=back.sandbox.GroveSimulation,bp=back.sandbox.productTest;
    assert.equal(B.getState().auth.provider,'google');assert.equal(bp.ui.authStage,null);
    assert.equal(B.getState().auth.email,'','Google gives the page no address, and none is made up');assert.equal(B.getState().name,'','No example name in live mode');
    bp.ui.panel='profile';bp.ui.settingsTab='account';bp.render(true);
    assert(!/anas@example\.com|Anas/.test(bp.html()),'Account shows no example identity');
    assert(bp.html().includes('Signed in with Google')&&bp.html().includes('<small>You</small>'),'Account shows the sign-in method and a neutral name');
    assert.equal(back.elements['profile-button'].textContent,'Y');
    // An email sign-in shows the typed address.
    const typed=page(sv,done(),AUTH_FILES,HOOK);await settle();const tp=typed.sandbox.productTest;w.session=false;
    typed.sandbox.GroveSimulation.signOut();tp.ui.authStage='signin';await tp.startEmail('sam.reyes@example.org');await tp.verifyEmail('246810');
    assert.equal(typed.sandbox.GroveSimulation.getState().auth.email,'sam.reyes@example.org');
    tp.ui.panel='profile';tp.ui.settingsTab='account';tp.render(true);assert(tp.html().includes('sam.reyes@example.org')&&!tp.html().includes('anas@example.com'));
    // A stale local sign-in with no server session goes back to the sign-in screen.
    w.session=false;const stale=page(sv,st,AUTH_FILES,HOOK);stale.sandbox.GroveSimulation.signIn('email','anas@example.com');await settle();
    assert.equal(stale.sandbox.GroveSimulation.getState().auth,null);assert.equal(stale.sandbox.productTest.ui.authStage,'signin');
  }

  // 6b. Item text in the node panel never runs: not in the summary, not in its snippet, not in
  //     the full text that "Show more" puts in place (data-full-html), whatever marked would make.
  {
    const w=liveWorld();
    const hostile='Notes <img src=x onerror=alert(1)> and <script>alert(2)</script> <a href="javascript:alert(3)">raw</a> [md](javascript:alert(4)) ![pic](javascript:alert(5)) <javascript:alert(6)> <https://example.com/ok>';
    const long=hostile+'\n\n'+'Plain words to pass the fold of the panel. '.repeat(20)+'\n\n'+hostile.replace(/alert\(/g,'confirm(');
    w.nodes.push(Object.assign({},w.nodes[1],{id:'x-short',label:'Short note',summary:hostile}),Object.assign({},w.nodes[1],{id:'x-long',label:'Long note',summary:long}));
    const pg=page(makeServer(w),{},['vendor/marked.min.js','markup.js',...GRAPH_FILES]);await settle();
    assert(pg.sandbox.marked&&pg.sandbox.GroveMarkup,'The panel renders with the vendored marked through markup.js');
    for(const id of ['x-short','x-long']){
      pg.sandbox.GroveView.openNode(id);await settle();
      const html=pg.elements['detail-body'].innerHTML,full=(html.match(/ data-full-html="([^"]*)"/)||[])[1];
      assert(html.includes('<h6>summary</h6>'),id+': the summary shows');
      if(id==='x-long'){assert(full,'A long summary keeps its full text for Show more');assert(decodeHtml(full).includes('&lt;img src=x onerror=confirm(1)&gt;'),'The text past the fold is escaped too');}
      for(const [part,label] of [[html.replace(/ data-full-html="[^"]*"/,''),id+' panel'],[full?decodeHtml(full):'',id+' show more']]){
        assert(!/<(img|script|svg|iframe)\b/i.test(part),label+': no image or script element');
        const tags=[...part.matchAll(/<[^>]*>/g)].map(m=>m[0]);
        assert(!tags.some(t=>/\son\w+\s*=/i.test(t)),label+': no event handler');
        assert(!tags.some(t=>/javascript:/i.test(decodeHtml(t))),label+': no javascript: address in an attribute');
      }
      assert(html.includes('<a href="https://example.com/ok">'),id+': a safe link stays a link');
    }
  }

  // 6c. A kind name is store text: the legend shows it as text, and its toggle still hides it.
  {
    const w=liveWorld(),odd='<img src=x onerror=alert(1)>"q" & co',st={};
    w.nodes.push(Object.assign({},w.nodes[1],{id:'odd-1',label:'Odd kind item',type:odd}));
    const pg=page(makeServer(w),st,GRAPH_FILES,null,null,legendRows);await settle();
    const list=pg.elements['legend-list'],view=()=>pg.sandbox.GroveView.graph().graphData().nodes;
    assert(!/<img/i.test(list.innerHTML),'No markup from a kind name reaches the legend');
    assert(list.innerHTML.includes('data-type="&lt;img src=x onerror=alert(1)&gt;&quot;q&quot; &amp; co"'),'The attribute holds the escaped kind name');
    assert(list.innerHTML.includes('<span class="legend-name">&lt;img src=x onerror=alert(1)&gt;&quot;q&quot; &amp; co</span>'),'The label shows the kind name as text');
    let row=list.rows().find(r=>r.getAttribute('data-type')===odd);assert(row,'The row reads back the exact kind name');
    assert(view().some(n=>n.type===odd));row.click();
    assert(!view().some(n=>n.type===odd),'A click hides that kind');assert.deepStrictEqual(JSON.parse(st['grove-minimal-codex-hidden-types']),{[odd]:1});
    row=list.rows().find(r=>r.getAttribute('data-type')===odd);row.click();
    assert(view().some(n=>n.type===odd),'A second click shows it again');
  }

  // 6d. The session turns 401 mid-session: one 'grove:unauthenticated', the local sign-in ends,
  //     panels close, the sign-in card shows, and the graph leaves memory and the screen. Each live
  //     call can be the one: a graph reload, the 3-minute poll, a node detail, a position save.
  const ALL_FILES=['live.js','layout-keep.js','simulation.js','native-app.js','provider-icons.js','app-screens.js','whatsapp-guide.js','product.js'];
  const signedInStore=()=>({'grove-minimal-codex-v3':JSON.stringify({version:3,schema:4,auth:{provider:'email',email:'sam.reyes@example.org'},name:'Sam',onboardingComplete:true,onboardingStep:4,tourComplete:true})});
  async function sessionPage(w,store){
    const sv=makeServer(w),st=store||signedInStore(),pg=page(sv,st,ALL_FILES,HOOK);let heard=0;
    pg.sandbox.addEventListener('grove:unauthenticated',()=>heard++);await settle();
    return {w,sv,st,pg,p:pg.sandbox.productTest,S:pg.sandbox.GroveSimulation,V:pg.sandbox.GroveView,heard:()=>heard};
  }
  const shownText=t=>Object.values(t.pg.elements).map(e=>e.innerHTML+'|'+e.textContent).join('|')+t.p.html();
  function assertSignedIn(t,label){
    assert(t.S.getState().auth,label+': signed in');assert.equal(t.p.ui.authStage,null,label+': no sign-in card');
    assert(t.V.nodes().length>0&&t.V.graph().graphData().nodes.length>0,label+': the graph shows');
    assert(t.pg.elements['legend-list'].innerHTML.includes('legend-row'),label+': the legend shows');
    assert(/[1-9]\d* nodes/.test(t.pg.elements.stats.innerHTML),label+': the counts show');
  }
  function assertCleared(t,label){
    assert.equal(t.S.getState().auth,null,label+': signed out here');
    assert.equal(t.p.ui.authStage,'signin',label+': the sign-in card shows');assert(t.p.html().includes('Continue with Google'),label+': sign-in card markup');
    assert.equal(t.p.ui.panel,null,label+': panels are closed');
    assert.equal(t.V.nodes().length,0,label+': no node in memory');assert.equal(t.V.graph().graphData().nodes.length,0,label+': no node on the map');
    assert.equal(t.pg.elements['legend-list'].innerHTML,'',label+': the legend is empty');
    assert(/^<span>0 nodes<\/span>/.test(t.pg.elements.stats.innerHTML),label+': the counts are zero');
    assert.equal(t.pg.elements['detail-body'].innerHTML,'',label+': the node panel is empty');assert(!t.pg.elements.detail.classList.contains('open'),label+': the node panel is closed');
    assert.equal(t.S.getState().currentNodeCount,0,label+': the last payload is gone');
    const text=shownText(t);assert(!t.w.nodes.some(n=>n.label&&n.label.length>=6&&text.includes(n.label)),label+': no title stays on screen');
  }
  const goneNow=w=>{w.session=false;w.graphStatus=401;};
  {
    // A graph reload.
    const t=await sessionPage(liveWorld());assertSignedIn(t,'reload, before');
    t.p.ui.panel='profile';t.p.ui.settingsTab='account';t.p.render(true);
    goneNow(t.w);await t.pg.sandbox.reloadGrove();await settle();
    assertCleared(t,'reload');assert.equal(t.heard(),1,'One event for the ended session');
    // A sign-in the server accepts arms it again: the graph comes back, and a later 401 ends it again.
    t.w.graphStatus=0;await t.p.startEmail('sam.reyes@example.org');await t.p.verifyEmail('246810');await settle();
    assertSignedIn(t,'signed in again');
    goneNow(t.w);await t.pg.sandbox.reloadGrove();await settle();assertCleared(t,'reload, second session');assert.equal(t.heard(),2);
    assert.deepStrictEqual(t.pg.errors,[],'The ended sessions log no error');
  }
  {
    // The 3-minute poll.
    const t=await sessionPage(liveWorld());assertSignedIn(t,'poll, before');
    const poll=t.pg.intervals.find(i=>i.ms===180000);assert(poll,'The graph polls every 3 minutes');
    goneNow(t.w);poll.fn();await settle();assertCleared(t,'poll');assert.equal(t.heard(),1);
    poll.fn();await settle();assert.equal(t.heard(),1,'A second 401 sends no second event');
  }
  {
    // A node detail, with another node's panel open.
    const t=await sessionPage(liveWorld());assertSignedIn(t,'detail, before');
    t.V.openNode('t001');await settle();assert(t.pg.elements['detail-body'].innerHTML.includes('detail-title'),'A node panel shows');assert(t.pg.elements.detail.classList.contains('open'));
    t.w.session=false;t.w.nodeStatus=401;
    t.V.openNode('t002');await settle();assertCleared(t,'node detail');assert.equal(t.heard(),1);
  }
  {
    // A position save (a first visit: nothing is placed yet).
    const w=liveWorld();w.positionsStatus=401;
    const t=await sessionPage(w);assert.equal(positionsCalls(t.sv).length,1,'The first visit saves');
    assertCleared(t,'position save');assert.equal(t.heard(),1);
  }
  {
    // A signed-out first load: the card keeps its step, and nothing is drawn.
    const w=liveWorld();w.session=false;w.graphStatus=401;
    const t=await sessionPage(w,{});assert.equal(t.p.ui.authStage,'signin');assert.equal(t.V.nodes().length,0);assert.deepStrictEqual(t.pg.errors,[]);
  }

  // 6e. Logout signs out here only when the server confirms it, or the session is already gone.
  {
    const w=liveWorld();w.logoutStatus=500;const t=await sessionPage(w);
    t.p.ui.panel='profile';t.p.ui.settingsTab='account';t.p.render(true);
    t.p.handle('logout');await settle();
    assertSignedIn(t,'failed logout');assert.equal(t.p.ui.panel,'profile','The account panel stays');assert.equal(t.p.ui.settingsTab,'account');
    assert(t.p.html().includes('<p class="gp-footnote" role="alert">Grove could not sign you out. Try again.</p>'),'The error shows in the sim\'s error style');
    assert.deepStrictEqual(t.sv.calls.slice(-2).map(c=>c.method+' '+c.path),['POST /v1/auth/logout','GET /v1/views/session'],'A failed logout asks whether the session is gone');
    w.logoutStatus=0;w.logoutThrows=true;t.p.handle('logout');await settle();
    assertSignedIn(t,'logout offline');assert(t.p.html().includes('Grove could not sign you out. Try again.'));
    // The logout fails, but the session is already gone: signed out here.
    w.logoutThrows=false;w.logoutStatus=500;w.session=false;t.p.handle('logout');await settle();
    assertCleared(t,'logout of a gone session');
  }
  {
    const w=liveWorld(),t=await sessionPage(w);
    t.p.ui.panel='profile';t.p.ui.settingsTab='account';t.p.render(true);t.V.openNode('t001');await settle();
    t.p.handle('logout');await settle();
    assertCleared(t,'confirmed logout');assert.equal(t.sv.calls[t.sv.calls.length-1].path,'/v1/auth/logout','A confirmed logout asks nothing more');
    assert(!t.p.html().includes('could not sign you out'));
  }

  // 6f. Live mode makes no local questions from store titles, and drops the ones an older page saved.
  for(const older of [false,true]){
    const kept={version:3,schema:4,auth:{provider:'email',email:'sam.reyes@example.org'},name:'Sam',onboardingStep:4};
    if(older)Object.assign(kept,{requestsSeeded:true,requests:[{id:'focus',targetLabel:'Saved by an older page',title:'Should Saved by an older page stay?',status:'open'}]});
    const w=liveWorld(),st={'grove-minimal-codex-v3':JSON.stringify(kept)},label=older?'older questions':'a first onboarding';
    const pg=page(makeServer(w),st,GRAPH_FILES);await settle();const S=pg.sandbox.GroveSimulation;
    await S.importSource('conversations',{provider:'chatgpt',name:'chatgpt-export.zip'});S.completeOnboarding();await settle();
    assert(S.getState().onboardingComplete&&Object.keys(S.getState().accountImports).length,label+': onboarding is done, with an import');
    assert.deepStrictEqual(S.getState().requests,[],label+': no local question in live mode');
    const saved=st['grove-minimal-codex-v3'];assert(!saved.includes('Saved by an older page'),label+': the older questions are dropped');
    const titles=w.nodes.map(n=>n.label).filter(l=>l&&l.length>=6);assert(!titles.some(l=>saved.includes(l)),label+': no store title reaches local storage');
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
  console.log('PASS: one endpoint map with same-origin calls and batched positions, live routes and the self alias, live counts and empty imports, first-visit save under real ids, kept positions after a reload and after new items arrive, only new items saved, failed saves retried with the same positions, starter kinds as hubs and legend names, local identity node, typed errors, session check, Google start, Apple not available, email code start and verify with errors, logout, item text that never runs in the node panel or behind Show more, kind names as text with a working legend toggle, a 401 mid-session (reload, poll, node detail, position save) that ends on the sign-in card with the graph cleared, logout only when the server confirms it, no local questions from store titles, no example identity in live mode, and a built page with no fixture');
})().catch(e=>{console.error(e);process.exitCode=1;});
