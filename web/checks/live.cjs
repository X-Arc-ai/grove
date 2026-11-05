// Live mode against a fake server: live.js (paths, fetch helpers, positions batches), the
// simulation.js adapter (routes, self alias, live counts, empty imports), native-app.js map
// positions (saved once, kept after a reload and after new items arrive, retried after a failed
// save), product.js sign-in (session, Google, email code, Apple, logout, the Google error, the limit
// on codes, the address after a sign-in, the account binding, resume), and the built page (no
// fixture, no hook name, none of the banned words).
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
  return {nodes,edges:synthetic.elements.edges.map(w=>Object.assign({},w.data)),selfId:'p001',positions:{},session:true,code:'246810',account:'acct-default',hasItems:true};
}
function makeServer(world){
  const calls=[];
  const reply=(status,body)=>Promise.resolve({ok:status>=200&&status<300,status,text:()=>Promise.resolve(body===undefined?'':JSON.stringify(body))});
  const typed=(status,code,message)=>reply(status,{error:{code,message,details:{},retryable:false}});
  // What the server answers about a session. A world with no account plays an answer with no account.
  const signedInBody=()=>Object.assign({signed_in:true,self_item_id:world.selfId||null},world.account?{account:world.account,method:world.method||null,has_items:!!world.hasItems}:{});
  // When world.holdGraph or world.holdPositions is a list, the answer of that call waits in it: the
  // check calls the entry to send that answer, or passes another answer to send in its place.
  const held=(list,answer)=>list?new Promise(done=>list.push(other=>done(other||answer))):answer;
  function fetch(url,init){
    init=init||{};
    const u=new URL(url,'http://grove.test/'),method=(init.method||'GET').toUpperCase();
    const body=init.body===undefined?undefined:JSON.parse(init.body);
    calls.push({path:u.pathname,search:u.search,method,init,body});
    const ids=new Set(world.nodes.map(n=>n.id));
    if(u.pathname==='/v1/views/session'&&world.sessionThrows)return Promise.reject(new TypeError('Failed to fetch'));
    if(u.pathname==='/v1/views/session'&&world.sessionStatus)return held(world.holdSession,typed(world.sessionStatus,'unavailable','Try again later'));
    if(u.pathname==='/v1/views/session')return held(world.holdSession,world.session?reply(200,signedInBody()):typed(401,'unauthenticated','Sign in to continue'));
    if(u.pathname==='/v1/views/graph'){
      if(world.graphStatus)return typed(world.graphStatus,world.graphStatus===401?'unauthenticated':(world.graphCode||'unavailable'),'Sign in to continue');
      const nodes=world.nodes.map(n=>({data:Object.assign({},n,world.positions[n.id]||{},n.id===world.selfId?{self:true}:{})}));
      return held(world.holdGraph,reply(200,{elements:{nodes,edges:world.edges.filter(e=>ids.has(e.source)&&ids.has(e.target)).map(e=>({data:e}))},stats:{total_nodes:nodes.length},time:{min:null,max:null},generated_at:'2026-11-02T00:00:00Z',as_of:null,known_as_of:null,self_item_id:world.selfId||null}));
    }
    if(u.pathname.indexOf('/v1/views/node/')===0){
      if(world.nodeStatus)return typed(world.nodeStatus,'unauthenticated','Sign in to continue');
      if(world.nodeMissing)return typed(404,'not_found','No such item');
      const n=world.nodes.find(x=>x.id===decodeURIComponent(u.pathname.slice('/v1/views/node/'.length)));
      return held(world.holdNode,n?reply(200,{node:Object.assign({},n,{title:n.label,content:n.summary,status_field_value:n.status}),required_props:[],optional_props:[],required_prop_names:[],status_field:'status'}):typed(404,'not_found','No such item'));
    }
    if(u.pathname==='/v1/views/schema'){if(world.schemaThrows)return Promise.reject(new TypeError('Failed to fetch'));if(world.schemaStatus)return typed(world.schemaStatus,world.schemaStatus===401?'unauthenticated':'unavailable','Sign in to continue');return held(world.holdSchema,reply(200,world.schema||{}));}
    if(u.pathname==='/v1/views/health')return reply(200,{status:'healthy',summary:'Your Grove is ready',checks:{graph:'ready',sources:'ready'},nodes:world.nodes.length,edges:world.edges.length,by_type:{},overall:{state:'healthy'}});
    if(u.pathname==='/v1/sources/pending-exports'&&method==='GET')return reply(200,{account:world.account||null,revision:0,pending_exports:{}});
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
      return held(world.holdPositions,reply(200,{placed,kept,unknown}));
    }
    if(u.pathname==='/v1/auth/email/start'&&world.emailStartThrows)return Promise.reject(new TypeError('Failed to fetch'));
    if(u.pathname==='/v1/auth/email/verify'&&world.emailVerifyThrows)return Promise.reject(new TypeError('Failed to fetch'));
    if(u.pathname==='/v1/auth/email/start'&&method==='POST'&&world.emailStartStatus)return typed(world.emailStartStatus,'rate_limited','Too many codes');
    if(u.pathname==='/v1/auth/email/start'&&method==='POST')return world.emailStartFails?typed(400,'invalid_arguments','That address cannot receive a code'):reply(200,{});
    if(u.pathname==='/v1/auth/email/verify'&&method==='POST'){if(body.code!==world.code)return typed(401,'unauthenticated','The code does not match');world.session=true;return reply(200,signedInBody());}
    if(u.pathname==='/v1/auth/logout'&&method==='POST'){
      if(world.logoutThrows)return Promise.reject(new TypeError('Failed to fetch'));
      if(world.logoutStatus)return typed(world.logoutStatus,'internal','the server failed');
      world.session=false;return reply(200,{});
    }
    return typed(404,'not_found','No such path');
  }
  return {fetch,calls,world,typed,reply};
}

// ---------- a browser-shaped sandbox ----------
function makeElement(tag){
  const classes=new Set(),attrs={};
  const el={tagName:String(tag||'div').toUpperCase(),getContext:()=>null,innerHTML:'',textContent:'',hidden:false,style:{setProperty(){},removeProperty(){}},dataset:{},children:[],offsetWidth:100,offsetHeight:100,scrollTop:0,
    classList:{add:(...k)=>k.forEach(x=>classes.add(x)),remove:(...k)=>k.forEach(x=>classes.delete(x)),toggle(k,on){if(on===undefined)on=!classes.has(k);if(on)classes.add(k);else classes.delete(k);return on;},contains:k=>classes.has(k)},
    setAttribute(k,v){attrs[k]=String(v);},getAttribute:k=>(k in attrs?attrs[k]:null),hasAttribute:k=>k in attrs,removeAttribute(k){delete attrs[k];},
    addEventListener(){},removeEventListener(){},querySelector:()=>null,querySelectorAll:()=>[],appendChild(c){el.children.push(c);return c;},append(){},remove(){},contains:()=>false,closest:()=>null,focus(){},click(){},
    getBoundingClientRect:()=>({left:0,top:0,width:100,height:100,right:100,bottom:100})};
  return el;
}
function chainForce(){const f=function(){};['strength','distanceMax','radius','iterations','distance','x','y','id','links'].forEach(k=>{f[k]=function(){return f;};});return f;}
function makeGraph(){
  let data={nodes:[],links:[]},zoom=1;const forces={link:chainForce()},kept={},calls=[];
  const keep=name=>function(fn){if(!arguments.length)return kept[name];kept[name]=fn;return proxy;};
  const g={calls,nodeCanvasObject:keep('nodeCanvasObject'),onNodeHover:keep('onNodeHover'),onLinkHover:keep('onLinkHover'),graphData(d){if(!arguments.length)return data;data=d;calls.push('graphData');return proxy;},d3Force(name,f){if(arguments.length<2)return forces[name];forces[name]=f;return proxy;},
    zoom(v){if(!arguments.length)return zoom;zoom=v;calls.push('zoom');return proxy;},centerAt(x,y){if(!arguments.length)return {x:0,y:0};calls.push('centerAt');g.center={x,y};return proxy;},graph2ScreenCoords:(x,y)=>({x,y}),screen2GraphCoords:(x,y)=>({x,y})};
  const proxy=new Proxy(g,{get:(t,k)=>(k in t?t[k]:k==='then'||typeof k!=='string'?undefined:function(){calls.push(k);return proxy;})});
  return proxy;
}
// The check's own handle on what the map keeps in its closure, which the page gives no way to read.
const MAP_STATE="  window.__mapState=function(){return {rawNodes,rawEdges,byId,fgNodes,fgLinks,fgNodeById,neighborsOf,focusedNode,hoverNode,hoverLink,hopDist,livePlaced,liveSaving,layoutCache,stableFrame,_evoSig,schemaByType,schemaAsked,hiddenTypes,manualCamera};};\n";
function page(server,store,files,hook,size,prepare){
  const elements={},winListeners={},errors=[],warnings=[],assigned=[],intervals=[],timeouts=[],env={tokens:{},search:'',globals:{}};
  // As index.html starts them: the search and the selection pill are closed, and the box is empty.
  for(const id of ['search-overlay','selection-pill']){elements[id]=makeElement('div');elements[id].setAttribute('hidden','');}
  elements['search-input']=Object.assign(makeElement('input'),{value:''});
  const toggleLabel=makeElement('label');elements['agents-toggle']=Object.assign(makeElement('input'),{closest:()=>toggleLabel});
  if(prepare)prepare(elements,env);
  // The panel agents.js builds finds its own parts in its markup, which this page double does not parse.
  const createElement=tag=>{const el=makeElement(tag);if(tag==='aside')el.querySelector=()=>makeElement('span');return el;};
  const document={hidden:false,body:makeElement('body'),documentElement:makeElement('html'),createElement,getElementById:id=>elements[id]||(elements[id]=makeElement('div')),querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},removeEventListener(){}};
  const sandbox={URL,URLSearchParams,Math,JSON,Date,Promise,MutationObserver:class{observe(){}},
    console:{log(){},info(){},warn:(...a)=>warnings.push(a),error:(...a)=>errors.push(a)},
    setTimeout:(fn,ms)=>{timeouts.push({fn,ms});return 0;},clearTimeout(){},setInterval:(fn,ms)=>intervals.push({fn,ms}),clearInterval(){},requestAnimationFrame:()=>0,cancelAnimationFrame(){},
    performance:{now:()=>1000},innerWidth:(size||[1440,900])[0],innerHeight:(size||[1440,900])[1],devicePixelRatio:1,document,navigator:{},
    matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}}),getComputedStyle:()=>({getPropertyValue:name=>env.tokens[name]||''}),
    localStorage:{getItem:k=>(k in store?store[k]:null),setItem:(k,v)=>{store[k]=String(v);},removeItem:k=>{delete store[k];}},
    location:{href:'http://grove.test/',search:env.search,pathname:env.pathname||'/',assign:u=>assigned.push(u)},
    fetch:server.fetch,ForceGraph:()=>()=>makeGraph(),
    d3:{forceManyBody:chainForce,forceCollide:chainForce,forceX:chainForce,forceY:chainForce},
    CustomEvent:class{constructor(type,init){this.type=type;this.detail=init&&init.detail;}},
    addEventListener:(k,fn)=>{(winListeners[k]||(winListeners[k]=[])).push(fn);},
    dispatchEvent:e=>{(winListeners[e.type]||[]).forEach(fn=>fn(e));return true;}};
  Object.assign(sandbox,env.globals);
  sandbox.window=sandbox;vm.createContext(sandbox);
  for(const f of files){let source=read(f);if(f==='product.js'&&hook)source=source.replace('  render(true);maybeTour();',hook);if(f==='native-app.js')source=source.replace('  window.reloadGrove = function',MAP_STATE+'  window.reloadGrove = function');vm.runInContext(source,sandbox,{filename:f});}
  return {sandbox,elements,errors,warnings,assigned,intervals,timeouts};
}
const GRAPH_FILES=['live.js','layout-keep.js','simulation.js','native-app.js'];
const positionsCalls=server=>server.calls.filter(c=>c.path==='/v1/views/positions');
const fgPositions=pg=>Object.fromEntries(pg.sandbox.GroveView.nodes().map(n=>[n.id,{x:n.x,y:n.y}]));
// The visual projection is independent of the insert-only stored map.
const placedPositions=pg=>JSON.parse(JSON.stringify(pg.sandbox.__mapState().livePlaced));
const assertFiniteMap=(map,ids,label)=>{assert.deepStrictEqual(Object.keys(map).sort(),[...ids].sort(),label+': complete');for(const at of Object.values(map))assert(Number.isFinite(at.x)&&Number.isFinite(at.y),label+': finite');};
// What drawNode really draws for a node: the first disc's radius, its ring colour and ring weight.
function drawnLook(pg,node){
  const look={};
  const ctx=new Proxy({},{get(t,k){if(k==='arc')return (x,y,r)=>{if(look.radius===undefined)look.radius=r;};if(k==='measureText')return ()=>({width:10});return k in t?t[k]:()=>{};},set(t,k,v){if(k==='strokeStyle'&&look.ring===undefined)look.ring=v;if(k==='lineWidth'&&look.width===undefined)look.width=v;t[k]=v;return true;}});
  pg.sandbox.GroveView.graph().nodeCanvasObject()(node,ctx,1);
  return look;
}
const vmStyle=()=>{const box={};vm.createContext(box);box.window=box;vm.runInContext(read('live.js'),box);return JSON.parse(JSON.stringify(box.GroveLive.KIND_STYLE));};
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
    assert.deepStrictEqual(JSON.parse(JSON.stringify(G.ENDPOINTS)),{mcp:'/mcp',session:'/v1/views/session',graph:'/v1/views/graph',node:'/v1/views/node/',schema:'/v1/views/schema',health:'/v1/views/health',workers:'/v1/views/workers',positions:'/v1/views/positions',upload:'/v1/sources/upload',pendingExports:'/v1/sources/pending-exports',sourceCoverage:'/v1/sources/connectors',sourceConnections:'/v1/connectors',materials:'/v1/materials',materialFiles:'/v1/materials/files',materialNotes:'/v1/materials/notes',materialDetail:'/v1/materials/{id}',materialEdit:'/v1/materials/notes/{id}',materialPreview:'/v1/materials/{id}/delete-preview',materialConfirmation:'/v1/materials/{id}/delete-confirmations',materialDelete:'/v1/materials/{id}/delete',agentFiles:'/v1/agent-files',agentNotes:'/v1/agent-files/notes',googleStart:'/v1/auth/google/start?next=/',emailStart:'/v1/auth/email/start',emailVerify:'/v1/auth/email/verify',logout:'/v1/auth/logout'});
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

  // 2. A first visit: the original stored layout saves once under real ids, independently of the complete finite display projection.
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
  const firstPlaced=placedPositions(first);
  assertFiniteMap(firstMap,firstNodes.map(n=>n.id),'Initial display');
  assertFiniteMap(firstPlaced,firstNodes.map(n=>n.id),'Initial stored map');
  for(const p of posts[0].body.positions){const view=p.id==='p001'?'grove-self':p.id;assert(firstPlaced[view].x===p.x&&firstPlaced[view].y===p.y,'The saved position is the placeNew result: '+p.id);}
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

  // 4. New items arrive: old stored positions stay exact; only new ones save; the display can re-project the changed adjacency.
  const fresh=[{id:'n-org',label:'Wrenfield Cooperative',type:'organisation'},{id:'n-t1',label:'Plan the cooperative fair',type:'task'},{id:'n-t2',label:'Order the fair banners',type:'task'},{id:'n-t3',label:'Book the fair hall',type:'task'},{id:'n-t4',label:'Proof the spring catalogue',type:'task'},{id:'n-e1',label:'Open studio day',type:'event'},{id:'n-o1',label:'Noticed a longer queue at the fair',type:'observation'}];
  fresh.forEach(n=>world.nodes.push(Object.assign({},world.nodes[1],n,{summary:''})));
  world.edges.push({id:'rn1',source:'n-t1',target:'n-org',verb:'part of',label:'part of'},{id:'rn2',source:'n-t2',target:'n-org',verb:'part of',label:'part of'},{id:'rn3',source:'n-t3',target:'n-org',verb:'part of',label:'part of'},{id:'rn4',source:'n-t4',target:'pr001',verb:'part of',label:'part of'},{id:'rn5',source:'n-o1',target:'p012',verb:'about',label:'about'});
  const grown=page(server,store,GRAPH_FILES);await settle();
  assert.deepStrictEqual(grown.errors,[]);
  assertFiniteMap(fgPositions(grown),grown.sandbox.GroveView.nodes().map(n=>n.id),'Display after arrival');
  const grownMap=placedPositions(grown);
  for(const id of Object.keys(firstPlaced))assert(grownMap[id].x===firstPlaced[id].x&&grownMap[id].y===firstPlaced[id].y,'An arrival never moves a stored item: '+id);
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
  const failedMap=placedPositions(failing);world2.failPositions=false;
  await failing.sandbox.reloadGrove();await settle();
  const retry=positionsCalls(server2);assert.equal(retry.length,2,'The next load retries the save');
  assert.deepStrictEqual(retry[1].body.positions.map(p=>p.id).sort(),world2.nodes.map(n=>n.id).sort());
  for(const p of retry[1].body.positions)assert(p.x===failedMap[p.id].x&&p.y===failedMap[p.id].y,'The retry saves the original stored placements');
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
  const HOOK='  window.productTest={ui,handle,bind,chooseSignIn,startEmail,verifyEmail,render,modalRoot,tourRoot,sessionNote,html:()=>modalRoot.innerHTML};render(true);';

  // The real setup must never turn a browser-local flag into a connection receipt.
  {
    const failures=[];
    const check=async(name,run)=>{try{await run();console.log('PASS onboarding: '+name);}catch(e){failures.push(name+': '+e.message);console.error('FAIL onboarding: '+name+': '+e.message);}};
    const held={version:3,schema:4,account:'setup-account',auth:{provider:'email',email:'robin@example.test'},onboardingStep:1,onboardingComplete:false,tourComplete:true,pendingExports:{chatgpt:{requestedAt:'2026-11-01T00:00:00Z'}},connectedHosts:['muse'],hostConnected:true,mcpClients:[{id:'cached-app',name:'Cached app'}]};
    const stored={'grove-minimal-codex-v3':JSON.stringify(held)};
    const w=Object.assign(liveWorld(),{account:'setup-account',hasItems:false}),sv=makeServer(w);
    const pg=page(sv,stored,AUTH_FILES,HOOK),p=pg.sandbox.productTest,S=pg.sandbox.GroveSimulation;
    await settle();
    const snapshot=()=>JSON.stringify(['sources','accountImports','pendingExports','connectedHosts','mcpClients','files'].map(k=>S.getState()[k]));
    await check('skip preserves unfinished work',async()=>{
      const before=snapshot(),calls=sv.calls.length;
      for(let step=1;step<=3;step++){
        assert.equal(p.ui.step,step);
        assert(/data-action="skip"[^>]*>Skip for now</.test(p.html()),'visible Skip for now at step '+step);
        assert(!/data-import=|data-integration=|data-file=|data-files-form|data-note-form/.test(p.html()),'unfinished actions are not offered in live setup');
        p.handle('skip');await settle();
        assert.equal(snapshot(),before,'skip changes no source, pending export or connection');
        assert.equal(S.getState().onboardingComplete,false,'skip does not complete onboarding');
      }
      assert.equal(sv.calls.length,calls,'skip sends no request');
    });
    p.ui.step=4;p.render(true);
    await check('same-origin MCP URL and copy',async()=>{
      assert.equal(pg.sandbox.GroveLive.ENDPOINTS.mcp,'/mcp');
      pg.sandbox.location.href='https://app.grove.x-arc.ai/signin?next=x';
      assert.equal(pg.sandbox.GroveLive.mcpUrl(),'https://app.grove.x-arc.ai/mcp');
      pg.sandbox.location.href='http://grove.test/';
      assert(p.html().includes('value="http://grove.test/mcp"'),'the visible URL uses the current origin');
      let copied;pg.sandbox.navigator.clipboard={writeText:text=>{copied=text;return Promise.resolve();}};
      p.handle('copy-mcp');await settle();assert.equal(copied,'http://grove.test/mcp');
    });
    await check('cached host and custom app are not receipts',async()=>{
      assert(!/is-connected|Grove is connected|Cached app|data-mcp-form|data-mcp-remove/.test(p.html()),'cached flags cannot claim a connection');
      const reload=page(sv,stored,AUTH_FILES,HOOK);await settle();reload.sandbox.productTest.ui.step=4;reload.sandbox.productTest.render(true);
      assert(!/is-connected|Grove is connected|Cached app|data-mcp-form/.test(reload.sandbox.productTest.html()),'reload cannot restore false success');
    });
    await check('Muse action opens directions without a local connection',async()=>{
      const before=snapshot(),host=makeElement('button');host.dataset.hostInline='muse';
      p.modalRoot.querySelectorAll=selector=>selector==='[data-host-inline]'?[host]:[];
      p.bind();host.onclick();
      assert(p.html().includes('Muse Desktop'),'Muse directions are visible');
      assert(p.html().includes('Muse opens the Grove authorization page.')&&p.html().includes('Review access in Grove before approval.'),'the real authorization and access review steps are explicit');
      assert(p.html().includes('http://grove.test/mcp'));
      assert(!/data-action="host-connect"|Grove is with you/.test(p.html()));
      assert.equal(snapshot(),before,'selecting Muse changes no connection state');
      p.handle('host-connect');assert.equal(snapshot(),before,'the old action cannot fabricate a live connection');
      p.handle('host-back');assert(p.html().includes('Take Grove with you'));
    });
    await check('Muse source shortcut also opens real directions',async()=>{
      const before=snapshot(),entry=makeElement('button');entry.dataset.guide='muse';
      p.modalRoot.querySelectorAll=selector=>selector==='[data-guide]'?[entry]:[];
      p.bind();entry.onclick();
      assert(p.html().includes('Muse Desktop')&&p.html().includes('Muse opens the Grove authorization page.')&&p.html().includes('Review access in Grove before approval.'));
      assert(!p.html().includes('data-import="muse"'),'the source shortcut offers no simulated Muse import');
      assert.equal(snapshot(),before,'the shortcut creates no import or connection');
      p.handle('host-back');
    });
    await check('simulation still connects locally',async()=>{
      const fixturePage=page(makeServer(liveWorld()),{},['simulation.js','provider-icons.js','app-screens.js','whatsapp-guide.js','product.js'],HOOK,null,(el,env)=>{env.globals.GROVE_FIXTURE=synthetic;});
      const f=fixturePage.sandbox.productTest,A=fixturePage.sandbox.GroveSimulation;
      A.signIn('email','robin@example.test');f.ui.authStage=null;f.ui.step=4;f.render(true);
      const host=makeElement('button');host.dataset.hostInline='muse';f.modalRoot.querySelectorAll=selector=>selector==='[data-host-inline]'?[host]:[];
      f.bind();host.onclick();assert(A.getState().connectedHosts.includes('muse'));assert(f.html().includes('Grove is connected'));assert(f.html().includes('data-mcp-form'));
    });
    assert.deepStrictEqual(failures,[],'truthful setup regressions');
  }

  {
    const w=liveWorld();w.session=false;const sv=makeServer(w),st={},pg=page(sv,st,AUTH_FILES,HOOK),p=pg.sandbox.productTest,A=pg.sandbox.GroveSimulation;
    assert.equal(p.ui.authStage,'checking');assert(p.html().includes('gp-auth-loading'),'The sign-in card waits for the session');
    await settle();assert.equal(sv.calls[0].path,'/v1/views/session');assert.equal(p.ui.authStage,'signin');assert(p.html().includes('Continue with Google'));assert.equal(A.getState().auth,null);
    const n0=sv.calls.length;p.chooseSignIn('google');assert.deepStrictEqual(pg.assigned,['/v1/auth/google/start?next=/']);assert.equal(sv.calls.length,n0);
    p.chooseSignIn('apple');assert(p.html().includes('Continue with Apple'));assert(p.html().includes('data-signin="apple" disabled aria-disabled="true"'));assert(!p.html().includes('Not available yet'));assert.equal(sv.calls.length,n0,'Apple calls nothing');assert.equal(A.getState().auth,null);
    w.emailStartFails=true;await p.startEmail('anas@example.com');assert.equal(p.ui.authStage,'signin');assert(p.ui.error.includes('could not send a code'));
    w.emailStartFails=false;await p.startEmail(' anas@example.com ');let call=sv.calls[sv.calls.length-1];
    assert.equal(call.path,'/v1/auth/email/start');assert.equal(call.method,'POST');assert.deepStrictEqual(call.body,{email:'anas@example.com'});assert.equal(call.init.headers['Content-Type'],'application/json');
    assert.equal(p.ui.authStage,'code');assert(p.html().includes('Check your inbox'));assert(!/482|use-code|gp-email-preview/.test(p.html()),'No code hint');
    await p.verifyEmail('000000');call=sv.calls[sv.calls.length-1];assert.equal(call.path,'/v1/auth/email/verify');assert.deepStrictEqual(call.body,{email:'anas@example.com',code:'000000'});
    assert.equal(A.getState().auth,null);assert(p.ui.error.includes('invalid or expired'));
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
    assert(back.elements['profile-button'].innerHTML.includes('<circle cx="12" cy="8" r="3.5"/>'),'The account button shows the person icon');assert.equal(back.elements['profile-button'].textContent,'','No initial comes from a name that nobody gave');
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
  const signedInStore=()=>({'grove-minimal-codex-v3':JSON.stringify({version:3,schema:4,account:'acct-default',auth:{provider:'email',email:'sam.reyes@example.org'},name:'Sam',onboardingComplete:true,onboardingStep:4,tourComplete:true})});
  async function sessionPage(w,store,prepare){
    const sv=makeServer(w),st=store||signedInStore(),pg=page(sv,st,ALL_FILES,HOOK,null,prepare);let heard=0;
    pg.sandbox.addEventListener('grove:unauthenticated',()=>heard++);await settle();
    return {w,sv,st,pg,p:pg.sandbox.productTest,S:pg.sandbox.GroveSimulation,V:pg.sandbox.GroveView,heard:()=>heard};
  }
  const shownText=t=>Object.values(t.pg.elements).map(e=>e.innerHTML+'|'+e.textContent+'|'+(e.value||'')).join('|')+t.p.html();
  const HIDDEN_KEY='grove-minimal-codex-hidden-types';
  function assertSignedIn(t,label){
    assert(t.S.getState().auth,label+': signed in');assert.equal(t.p.ui.authStage,null,label+': no sign-in card');
    assert(t.V.nodes().length>0&&t.V.graph().graphData().nodes.length>0,label+': the graph shows');
    assert(t.pg.elements['legend-list'].innerHTML.includes('legend-row'),label+': the legend shows');
    assert(/[1-9]\d* nodes/.test(t.pg.elements.stats.innerHTML),label+': the counts show');
  }
  // Nothing of the session stays: not in the map's memory, not on screen, not in local storage.
  const MAP_EMPTY={rawNodes:[],rawEdges:[],byId:{},fgNodes:[],fgLinks:[],fgNodeById:{},neighborsOf:{},focusedNode:null,hoverNode:null,hoverLink:null,hopDist:{},livePlaced:{},liveSaving:{},layoutCache:null,stableFrame:null,_evoSig:null,schemaByType:{},schemaAsked:false,hiddenTypes:{},manualCamera:false};
  function assertEmptied(t,label){
    assert.equal(t.S.getState().auth,null,label+': signed out here');
    assert.equal(JSON.parse(t.st['grove-minimal-codex-v3']).auth,null,label+': the local sign-in leaves local storage');
    assert.equal(t.V.nodes().length,0,label+': no node in memory');assert.equal(t.V.graph()?t.V.graph().graphData().nodes.length:0,0,label+': no node on the map');
    assert.deepStrictEqual(JSON.parse(JSON.stringify(t.pg.sandbox.__mapState())),MAP_EMPTY,label+': the map keeps nothing in memory');
    assert.equal(t.pg.elements['legend-list'].innerHTML,'',label+': the legend is empty');
    assert.equal(t.pg.elements.stats.innerHTML,'<span>0 nodes</span> <span class="gp-stat-divider">·</span> <span>0 connections</span>',label+': the counts are zero and no earlier count stays');
    assert.equal(t.pg.elements['detail-body'].innerHTML,'',label+': the node panel is empty');assert(!t.pg.elements.detail.classList.contains('open'),label+': the node panel is closed');assert.equal(t.pg.elements.detail.getAttribute('aria-hidden'),'true',label+': the node panel is out of reach');
    assert.equal(t.S.getState().currentNodeCount,0,label+': the last payload is gone');assert.equal(t.S.getState().currentEdgeCount,0,label+': the last payload holds no link');
    const E=t.pg.elements;
    assert(E['search-overlay'].hasAttribute('hidden'),label+': the search is closed');assert.equal((E['search-results']||{innerHTML:''}).innerHTML,'',label+': the search shows no result');assert.equal(E['search-input'].value,'',label+': the search box is empty');
    assert(E['selection-pill'].hasAttribute('hidden'),label+': the selection pill is closed');assert.equal(E['selection-pill'].innerHTML,'',label+': the selection pill says nothing');
    assert(!(HIDDEN_KEY in t.st),label+': the hidden kinds leave local storage');
    assert.equal(t.p.tourRoot.innerHTML,'',label+': the tour root is empty');
    assert(t.pg.elements.loading.classList.contains('hidden'),label+': the loading cover does not cover the card');
    const text=shownText(t);assert(!t.w.nodes.some(n=>n.label&&n.label.length>=6&&text.includes(n.label)),label+': no title stays on screen');
  }
  function assertCleared(t,label){
    assertEmptied(t,label);
    assert.equal(t.p.ui.authStage,'signin',label+': the sign-in card shows');assert(t.p.html().includes('Continue with Google'),label+': sign-in card markup');
    const ui=t.p.ui;
    assert.deepStrictEqual([ui.panel,ui.guide,ui.confirm,ui.host],[null,null,null,null],label+': no panel, guide, confirmation or host step stays');
    assert.equal(ui.tour,0,label+': the tour is over');
    assert.deepStrictEqual([ui.error,ui.email,ui.code],['','',''],label+': no error, address or code stays on the card');
    assert.equal(t.p.sessionNote.hidden,true,label+': the offline note is gone');assert.equal(t.p.sessionNote.textContent,'',label+': the offline note says nothing');
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

  // 6g. A graph read is out when the session ends with a 401 from another call, and then answers
  //     with graph data: nothing draws, no store title comes back, and no new read starts. The
  //     graph endpoint keeps answering here, so a read that does start would draw.
  const graphReads=sv=>sv.calls.filter(c=>c.path==='/v1/views/graph').length;
  const endWithNodeDetail=async t=>{t.w.session=false;t.w.nodeStatus=401;t.V.openNode('t002');await settle();};
  const titleKept=(t,w)=>{const kept=shownText(t)+JSON.stringify(t.S.getState())+JSON.stringify(t.st);return w.nodes.map(n=>n.label).find(l=>l&&l.length>=6&&kept.includes(l));};
  for(const [label,start] of [['a late reload',t=>t.pg.sandbox.reloadGrove()],['a late poll',t=>t.pg.intervals.find(i=>i.ms===180000).fn()]]){
    const t=await sessionPage(liveWorld());assertSignedIn(t,label+', before');
    t.w.holdGraph=[];start(t);await settle();
    const out=t.w.holdGraph;t.w.holdGraph=null;assert.equal(out.length,1,label+': the read is out');
    await endWithNodeDetail(t);assertCleared(t,label+', the session ended');assert.equal(t.heard(),1);
    const reads=graphReads(t.sv);
    out[0]();await settle();
    assertCleared(t,label+', after its answer');assert.equal(t.heard(),1,label+': one event for the ended session');
    assert.equal(graphReads(t.sv),reads,label+': its answer starts no new read');
    assert.equal(titleKept(t,t.w),undefined,label+': no store title comes back');
    assert.deepStrictEqual(t.pg.errors,[],label+': no error');
  }

  // 6h. A new session in the same page: the same item ids in another world. The new map is the one
  //     a page of its own draws for that world (its stored places kept, the rest placed fresh, none
  //     of the ended session's places), and its save goes out and lands in that world. What the old
  //     session still has out (a save, a read) neither blocks the new session nor ends it,
  //     whichever way it answers.
  const SECOND_TITLE='Second world item ';
  function secondWorld(){
    const w=liveWorld();
    w.nodes.pop();   // one item fewer, so the counts tell the two worlds apart
    w.nodes.forEach((n,i)=>{n.label=SECOND_TITLE+i;if(i%3===0)w.positions[n.id]={x:4000+60*i,y:-3000};});
    return w;
  }
  const viewId=real=>real==='p001'?'grove-self':real;
  for(const lateAnswer of ['saved','refused']){
    const label='new session, the old save '+lateAnswer;
    const alone=await sessionPage(secondWorld()),expected=fgPositions(alone.pg),expectedSave=positionsCalls(alone.sv)[0].body.positions;
    const w=liveWorld();w.holdPositions=[];
    const t=await sessionPage(w),sv=t.sv;assertSignedIn(t,label+', first person');
    const ended=placedPositions(t.pg),firstWorld={nodes:w.nodes};
    const oldSave=w.holdPositions;w.holdPositions=null;assert.equal(oldSave.length,1,label+': the first save is out');
    w.holdGraph=[];t.pg.sandbox.reloadGrove();await settle();
    const oldRead=w.holdGraph;w.holdGraph=null;assert.equal(oldRead.length,1,label+': a graph read is out');
    await endWithNodeDetail(t);assertCleared(t,label+', the session ended');assert.equal(t.heard(),1);
    Object.assign(w,secondWorld(),{session:false,nodeStatus:0});
    const secondStored=Object.assign({},w.positions),stays=w.nodes.map(n=>n.id).filter(id=>!secondStored[id]);
    assert(stays.length&&stays.every(id=>Math.hypot(ended[viewId(id)].x-placedPositions(alone.pg)[viewId(id)].x,ended[viewId(id)].y-placedPositions(alone.pg)[viewId(id)].y)>1),'The two worlds place the same ids apart');
    const saves=positionsCalls(sv).length;
    await t.p.startEmail('robin.vale@example.org');await t.p.verifyEmail('246810');await settle();
    assertSignedIn(t,label+', second person');assert.equal(t.heard(),1);
    assert.deepStrictEqual(fgPositions(t.pg),expected,label+': the new map is that of a page of its own');
    for(const id of Object.keys(secondStored))assert.deepStrictEqual(placedPositions(t.pg)[viewId(id)],secondStored[id],label+': a stored place is kept');
    assert.equal(positionsCalls(sv).length,saves+1,label+': the new session saves its new items');
    assert.deepStrictEqual(positionsCalls(sv)[saves].body.positions,expectedSave,label+': the save holds the new map');
    for(const p of expectedSave)assert.deepStrictEqual(w.positions[p.id],{x:p.x,y:p.y},label+': the save lands in the new world');
    oldSave[0](lateAnswer==='refused'?sv.typed(401,'unauthenticated','Sign in to continue'):undefined);oldRead[0]();await settle();
    assertSignedIn(t,label+', after the old calls answer');assert.equal(t.heard(),1,label+': an answer of the ended session ends nothing');
    assert.deepStrictEqual(fgPositions(t.pg),expected,label+': nothing moves');
    assert.equal(titleKept(t,firstWorld),undefined,label+': no title of the first world comes back');
    assert.equal(t.S.getState().currentNodeCount,w.nodes.length,label+': the counts stay those of the new world');
    await t.pg.sandbox.reloadGrove();await settle();
    assert.deepStrictEqual(fgPositions(t.pg),expected,label+': a reload keeps the new map');
    assert.equal(positionsCalls(sv).length,saves+1,label+': a reload saves nothing');
    assert.deepStrictEqual(t.pg.errors,[],label+': no error');
  }

  // 6i. The kinds belong to a session. A page that opened signed out reads them after a sign-in in
  //     the same page; the end of a session drops them; a late answer of the ended session is not
  //     kept. The node panel falls back on them when the node read names no kind fields.
  const KINDS={recipe:{required:[{name:'soak_hours',type:'string'}],optional:[],status_field:'status',status_vocab:[]}};
  const schemaCalls=t=>t.sv.calls.filter(c=>c.path==='/v1/views/schema').length;
  const signedOutStore=()=>({'grove-minimal-codex-v3':JSON.stringify({version:3,schema:4,auth:null,name:'Sam',onboardingComplete:true,onboardingStep:4,tourComplete:true})});
  const kindInPanel=async t=>{t.w.nodeMissing=true;t.V.openNode('k001');await settle();t.w.nodeMissing=false;const shown=/soak.hours/i.test(t.pg.elements['detail-body'].innerHTML);t.V.close();await settle();return shown;};
  const signIn=async t=>{await t.p.startEmail('robin.vale@example.org');await t.p.verifyEmail('246810');await settle();assertSignedIn(t,'kinds, signed in');};
  const signedOutPage=async()=>{const w=liveWorld();Object.assign(w,{session:false,graphStatus:401,schemaStatus:401});const t=await sessionPage(w,signedOutStore());Object.assign(w,{graphStatus:0,schemaStatus:0,schema:KINDS});return t;};
  {
    const t=await signedOutPage(),w=t.w;
    assert.equal(schemaCalls(t),1,'A signed-out page asks for the kinds once');
    await signIn(t);
    assert.equal(schemaCalls(t),2,'A sign-in in the same page reads the kinds');
    assert(await kindInPanel(t),'The kinds of this session reach the panel');
    await t.pg.sandbox.reloadGrove();await settle();
    assert.equal(schemaCalls(t),2,'A reload does not read the kinds again');
    await endWithNodeDetail(t);assertCleared(t,'kinds, the session ended');
    Object.assign(w,{nodeStatus:0,schema:{},holdSchema:[]});
    await signIn(t);
    assert.equal(schemaCalls(t),3,'The next session reads the kinds again');
    assert(!await kindInPanel(t),'The kinds of the ended session are gone, with the new read still out');
    w.holdSchema[0]();w.holdSchema=null;await settle();
    assert.deepStrictEqual(t.pg.errors,[],'kinds: no error');
  }
  {
    const t=await signedOutPage(),w=t.w;
    w.holdSchema=[];
    await signIn(t);
    const late=w.holdSchema;w.holdSchema=null;assert.equal(late.length,1,'The read of the kinds is out');
    await endWithNodeDetail(t);assertCleared(t,'late kinds, the session ended');
    Object.assign(w,{nodeStatus:0,schema:{}});
    await signIn(t);
    late[0]();await settle();
    assert(!await kindInPanel(t),'A late answer of the ended session is not kept');
    assert.deepStrictEqual(t.pg.errors,[],'late kinds: no error');
  }

  // 6i-a. The node read answers: the panel shows its content, not the summary the graph gave.
  {
    const t=await sessionPage(liveWorld()),full='The whole text of this item, from the node read';
    t.w.nodes.find(n=>n.id==='t001').summary=full;t.V.openNode('t001');await settle();
    assert(t.pg.elements['detail-body'].innerHTML.includes(full),'The node panel shows the content of the node read');
    t.V.close();await settle();
  }

  for(const fail of [{schemaStatus:503},{schemaThrows:true}]){
    const w=liveWorld();Object.assign(w,fail);
    const t=await sessionPage(w);assertSignedIn(t,'kinds, a failed read');
    Object.assign(w,{schemaStatus:0,schemaThrows:false,schema:KINDS});
    await t.pg.sandbox.reloadGrove();await settle();
    assert(await kindInPanel(t),'The next graph load reads the kinds again after a failed read');
    assertSignedIn(t,'kinds, after the failed read');
  }

  // 6j. Search results are open when the session ends: no title stays behind the sign-in card, and
  //     none shows over the next person's map after the next sign-in.
  const wireSearch=elements=>{const button=elements['search-btn']=makeElement('button');button.addEventListener=(k,fn)=>{if(k==='click')button.click=fn;};};
  const search=(t,query)=>{const input=t.pg.elements['search-input'];input.value=query;input.oninput({target:input});};
  const signInAgain=async(t,label)=>{await t.p.startEmail('robin.vale@example.org');await t.p.verifyEmail('246810');await settle();assertSignedIn(t,label);};
  {
    const w=liveWorld(),t=await sessionPage(w,null,wireSearch),E=t.pg.elements,firstWorld={nodes:w.nodes.slice()};assertSignedIn(t,'search, before');
    E['search-btn'].click();search(t,'a');
    assert(!E['search-overlay'].hasAttribute('hidden'),'The search is open');assert.equal(E['search-input'].value,'a');
    const shown=firstWorld.nodes.map(n=>n.label).filter(l=>E['search-results'].innerHTML.includes(l));assert(shown.length>=5,'The search shows titles');
    goneNow(w);await t.pg.sandbox.reloadGrove();await settle();
    assertCleared(t,'search open at the 401');
    assert(!shown.some(l=>shownText(t).includes(l)),'No result title stays behind the sign-in card');
    Object.assign(w,secondWorld(),{session:false,graphStatus:0});
    await signInAgain(t,'search, the next person');
    assert(E['search-overlay'].hasAttribute('hidden'),'The search stays closed for the next person');assert.equal(E['search-results'].innerHTML,'','The next person sees no result');
    assert.equal(titleKept(t,firstWorld),undefined,'No title of the first person shows over the next map');
    E['search-btn'].click();search(t,'second world');assert(E['search-results'].innerHTML.includes(SECOND_TITLE),'The search works for the next person');
    assert.deepStrictEqual(t.pg.errors,[],'search: no error');
  }
  // 6k. The selection pill is open when the session ends: it closes, and its words go.
  {
    const w=liveWorld(),t=await sessionPage(w,null,el=>{el['selection-pill'].querySelector=()=>makeElement('button');});
    t.V.showChanges('nodes');await settle();
    const pill=t.pg.elements['selection-pill'];assert(!pill.hasAttribute('hidden')&&/new nodes? since \d{4}-\d{2}-\d{2}/.test(pill.innerHTML),'The selection pill is open');
    goneNow(w);await t.pg.sandbox.reloadGrove();await settle();assertCleared(t,'selection pill open at the 401');
  }
  // 6l. Kind names are store text: the hidden kinds leave the page and local storage with the
  //     session, so the next person sees every kind. A page opening signed out drops an older list.
  {
    const w=liveWorld(),st=signedInStore(),t=await sessionPage(w,st,legendRows),list=t.pg.elements['legend-list'],mapped=()=>t.V.graph().graphData().nodes;
    list.rows().find(r=>r.getAttribute('data-type')==='recipe').click();
    assert.deepStrictEqual(JSON.parse(st[HIDDEN_KEY]),{recipe:1},'The hidden kind is saved');assert(!mapped().some(n=>n.type==='recipe'),'The kind is hidden');
    assert(/legend-row-disabled" data-type="recipe"/.test(list.innerHTML),'The legend shows it hidden');
    goneNow(w);await t.pg.sandbox.reloadGrove();await settle();assertCleared(t,'hidden kinds');
    w.graphStatus=0;await signInAgain(t,'hidden kinds, the next session');
    assert(mapped().some(n=>n.type==='recipe'),'The next session shows the kind the last one hid');
    assert(!/legend-row-disabled/.test(list.innerHTML),'The legend hides no kind for the next session');assert(!(HIDDEN_KEY in st),'The next session starts with no hidden kinds');
    const out=liveWorld();Object.assign(out,{session:false,graphStatus:401});const older={[HIDDEN_KEY]:'{"recipe":1}'};
    await sessionPage(out,older);assert(!(HIDDEN_KEY in older),'A signed-out page drops an older list of hidden kinds');
  }
  // 6m. A failed save, then new items, then the retry: the retry preserves the original stored placements,
  //     not a fresh layout of the bigger graph, and a later page load moves nothing.
  {
    const w=liveWorld();w.failPositions=true;
    const sv=makeServer(w),st=signedInStore(),pg=page(sv,st,GRAPH_FILES);await settle();
    const drawn=placedPositions(pg);assert.equal(positionsCalls(sv).length,1,'The first save goes out');
    const newcomers=[];for(let i=0;i<40;i++){const id='late-'+i;newcomers.push(id);w.nodes.push(Object.assign({},w.nodes[1],{id,label:'Late arrival '+i,type:'task',summary:''}));w.edges.push({id:'late-e'+i,source:id,target:'c001',verb:'part of',label:'part of'});}
    w.failPositions=false;await pg.sandbox.reloadGrove();await settle();
    const retry=positionsCalls(sv);assert.equal(retry.length,2,'The next load saves again');
    const saved=Object.fromEntries(retry[1].body.positions.map(q=>[q.id,q]));
    assert.equal(Object.keys(saved).length,w.nodes.length,'The retry saves every item');
    for(const [id,at] of Object.entries(drawn)){const q=saved[id==='grove-self'?'p001':id];assert(q&&q.x===at.x&&q.y===at.y,'The retry saves the original stored place of '+id);}
    // The display is a projection of the current graph, so a refresh keeps old places and a fresh page may not.
    const seen=placedPositions(pg),later=page(sv,st,GRAPH_FILES);await settle();const again=page(sv,st,GRAPH_FILES);await settle();
    assert.deepStrictEqual(placedPositions(later),seen,'A later page load keeps the stored map');
    assert.deepStrictEqual(fgPositions(again),fgPositions(later),'A later page load moves nothing');
    assert.equal(positionsCalls(sv).length,2,'A later page load saves nothing');
    assert.deepStrictEqual(later.errors,[],'failed save: no error');
  }
  // 6n. A node read rejects after the session ended: its fallback panel stays out.
  {
    const t=await sessionPage(liveWorld());t.w.holdNode=[];t.V.openNode('t001');await settle();
    const out=t.w.holdNode;t.w.holdNode=null;assert.equal(out.length,1,'The node read is out');
    goneNow(t.w);await t.pg.sandbox.reloadGrove();await settle();assertCleared(t,'a node read is out');
    out[0](Promise.reject(new TypeError('Failed to fetch')));await settle();
    assertCleared(t,'a node read rejected late');assert.deepStrictEqual(t.pg.errors,[],'late rejected read: no error');
  }
  // 6o. A late 401 after a logout: the person at the code step keeps the step and the typed email.
  {
    const t=await sessionPage(liveWorld()),w=t.w;w.holdGraph=[];t.pg.sandbox.reloadGrove();await settle();
    const late=w.holdGraph;w.holdGraph=null;assert.equal(late.length,1,'A graph read is out');
    t.p.handle('logout');await settle();assertCleared(t,'logout with a read out');assert.equal(t.heard(),0);
    await t.p.startEmail('robin.vale@example.org');assert.equal(t.p.ui.authStage,'code','The code step shows');
    late[0](t.sv.typed(401,'unauthenticated','Sign in to continue'));await settle();
    assert.equal(t.heard(),0,'The old session 401 is ignored');assert.equal(t.p.ui.authStage,'code','The late 401 keeps the code step');assert.equal(t.p.ui.email,'robin.vale@example.org','The late 401 keeps the typed email');
    assert(t.p.html().includes('Check your inbox')&&t.p.html().includes('robin.vale@example.org'),'The card still asks for the code');
  }
  // 6p. A session that ends with a node open or under the pointer: the poll of the next session
  //     still reads, because "never mid-interaction" is about the person at the screen now.
  for(const held of ['a node detail','a hovered node']){
    const t=await sessionPage(liveWorld()),w=t.w,poll=t.pg.intervals.find(i=>i.ms===180000);assertSignedIn(t,held+', before');
    if(held==='a hovered node')t.V.graph().onNodeHover()(t.V.nodes().find(n=>n.id==='t001'));
    await endWithNodeDetail(t);assertCleared(t,held+', the session ended');
    w.nodeStatus=0;
    await signInAgain(t,held+', the next session');
    assert(!t.pg.elements.detail.classList.contains('open'),held+': no panel is open for the next person');
    w.nodes.push(Object.assign({},w.nodes[1],{id:'poll-new',label:'Found by the poll',type:'task',summary:''}));
    const reads=graphReads(t.sv);poll.fn();await settle();
    assert(graphReads(t.sv)>reads,held+': the poll of the next session reads the graph');
    assert(t.V.nodes().some(n=>n.id==='poll-new'),held+': the poll brings the new item to the map');
  }
  // 6q. The network is fully down: a logout stays signed in, with the error on screen.
  {
    const t=await sessionPage(liveWorld()),w=t.w;
    t.p.ui.panel='profile';t.p.ui.settingsTab='account';t.p.render(true);
    Object.assign(w,{logoutThrows:true,sessionThrows:true});t.p.handle('logout');await settle();
    assertSignedIn(t,'logout, network down');assert.equal(t.p.ui.panel,'profile','The account panel stays');
    assert(t.p.html().includes('Grove could not sign you out. Try again.'),'The error shows');
    assert.deepStrictEqual(t.sv.calls.slice(-2).map(c=>c.method+' '+c.path),['POST /v1/auth/logout','GET /v1/views/session'],'Both calls were tried');
    assert.equal(t.heard(),0,'No call ended the session');
  }
  // 6r. Offline at each sign-in step: the card shows the offline text and stays on its step.
  {
    const OFFLINE='Grove could not be reached. Try again in a moment.';
    const w=liveWorld();Object.assign(w,{session:false,sessionThrows:true});
    const pg=page(makeServer(w),{},AUTH_FILES,HOOK),p=pg.sandbox.productTest,A=pg.sandbox.GroveSimulation;
    // render() appends the error line to the card's form, as it does in the browser.
    const form=makeElement('div');p.modalRoot.querySelector=sel=>(sel==='.gp-auth-form'?form:null);
    const alertNow=()=>{const kids=form.children.filter(c=>c.getAttribute('role')==='alert').map(c=>c.textContent);form.children.length=0;return kids;};
    assert.equal(p.ui.authStage,'checking','The card waits for the session');
    await settle();assert.equal(p.ui.authStage,'signin','An offline session check leaves the waiting step');assert(p.html().includes('Continue with Google'),'The sign-in options show');assert.deepStrictEqual(alertNow(),[OFFLINE],'An offline session check shows the offline text');
    Object.assign(w,{sessionThrows:false,emailStartThrows:true});
    await p.startEmail('robin.vale@example.org');assert.equal(p.ui.authStage,'signin','An offline email start stays on its step');assert.deepStrictEqual(alertNow(),[OFFLINE],'An offline email start shows the offline text');
    w.emailStartThrows=false;await p.startEmail('robin.vale@example.org');assert.equal(p.ui.authStage,'code');assert.deepStrictEqual(alertNow(),[],'The offline text goes when the call works');
    w.emailVerifyThrows=true;await p.verifyEmail('246810');assert.equal(p.ui.authStage,'code','An offline email verify stays on its step');assert.deepStrictEqual(alertNow(),[OFFLINE],'An offline email verify shows the offline text');assert.equal(A.getState().auth,null,'An offline verify signs nobody in');
  }
  // 6s. The look of the kinds, read from the page's own tokens in both themes: each starter kind
  //     takes the ring of the sim style it aliases, in the legend and on the map; an unknown kind
  //     takes the task style, in ring and in size, under its own name.
  {
    const css=read('style.css'),themes=[0,1].map(i=>{const t={};for(const m of css.matchAll(/(--ring-[a-z_]+):\s*(#[0-9a-fA-F]{3,6})/g)){const seen=[...css.matchAll(new RegExp(m[1]+':\\s*(#[0-9a-fA-F]{3,6})','g'))];t[m[1]]=seen[i][1];}for(const m of css.matchAll(/(--ring-[a-z_]+):\s*var\((--ring-[a-z_]+)\)/g))t[m[1]]=t[m[2]];return t;});
    for(const tokens of themes){
      const w=liveWorld(),STYLE=vmStyle();
      for(const kind of Object.keys(STYLE))w.nodes.push(Object.assign({},w.nodes[1],{id:'kind-'+kind,label:'A '+kind,type:kind,status:'',summary:''}));
      const pg=page(makeServer(w),{},GRAPH_FILES,null,null,(el,env)=>{env.tokens=tokens;});await settle();
      const legend=pg.elements['legend-list'].innerHTML,swatch=kind=>(legend.match(new RegExp('data-type="'+kind+'"[^>]*><span class="legend-swatch" style="border-color:(#[0-9a-fA-F]+)"'))||[])[1];
      assert(tokens['--ring-task']&&tokens['--ring-company']!==tokens['--ring-task'],'The tokens are read');
      const look=id=>{const n=pg.sandbox.GroveView.nodes().find(x=>x.id===id);return drawnLook(pg,Object.assign({},n,{__born:0}));};
      for(const [kind,style] of Object.entries(STYLE)){
        assert.equal(swatch(kind),tokens['--ring-'+style],kind+' takes the legend colour of '+style);
        assert.notEqual(swatch(kind),tokens['--ring-task'],kind+' does not fall to the task colour');
        assert.equal(look('kind-'+kind).ring,tokens['--ring-'+style],kind+' draws the ring of '+style);
      }
      assert.equal(swatch('recipe'),tokens['--ring-task'],'An unknown kind takes the legend colour of a task');
      const task=look('t001'),unknown=look('k001');
      assert.equal(task.radius,6,'A task draws at the task size');assert.equal(unknown.radius,6,'An unknown kind takes the task size');
      assert.equal(unknown.ring,tokens['--ring-task'],'An unknown kind takes the ring colour of a task');assert.equal(unknown.width,task.width,'An unknown kind takes the ring weight of a task');
    }
  }
  // 6t. A new leaf keeps its seed offset from its hub, where that spot is free: not only "near" it.
  {
    const w=liveWorld(),sv=makeServer(w),st=signedInStore();
    page(sv,st,GRAPH_FILES);await settle();
    const hubId='c001',leaves=[];
    for(let i=0;i<24;i++){const id='leaf-'+i;leaves.push(id);w.nodes.push(Object.assign({},w.nodes[1],{id,label:'New leaf '+i,type:'task',summary:''}));w.edges.push({id:'leaf-e'+i,source:id,target:hubId,verb:'part of',label:'part of'});}
    const asked=[],pg=page(sv,st,GRAPH_FILES),place=pg.sandbox.GroveLayoutKeep.placeNew;
    pg.sandbox.GroveLayoutKeep.placeNew=(...a)=>{const out=place(...a);asked.push({items:a[1],seed:a[3],room:a[4],out});return out;};await settle();
    assert.equal(asked.length,1,'One layout');const {items,seed,room,out}=asked[0],at=out.positions;
    const discs=items.filter(n=>!out.placed.includes(n.id)).map(n=>({x:at[n.id].x,y:at[n.id].y,r:room(n)}));
    let exact=0;
    for(const id of out.placed){
      const n=items.find(x=>x.id===id);
      if(leaves.includes(id)){
        assert.equal(n._primaryHubId,hubId,id+' anchors on its hub');
        const target={x:at[hubId].x+(seed[id].x-seed[hubId].x),y:at[hubId].y+(seed[id].y-seed[hubId].y)};
        if(discs.every(d=>Math.hypot(d.x-target.x,d.y-target.y)>=d.r+room(n))){exact++;assert(at[id].x===target.x&&at[id].y===target.y,id+' keeps its seed offset from its hub, as that spot is free');}
        else assert(Math.hypot(at[id].x-target.x,at[id].y-target.y)<=250,id+' takes a free spot near its seed offset');
      }
      discs.push({x:at[id].x,y:at[id].y,r:room(n)});
    }
    assert(exact>=4,'Some new leaves find their spot free: '+exact);
  }
  // 6u. The insert-only map reserves the original hub and identity discs even
  //     when the display uses a separate compact projection.
  {
    const w=liveWorld(),sv=makeServer(w),st=signedInStore();
    const capture=async()=>{const pg=page(sv,st,GRAPH_FILES),calls=[],place=pg.sandbox.GroveLayoutKeep.placeNew;pg.sandbox.GroveLayoutKeep.placeNew=(...a)=>{const out=place(...a);calls.push({items:a[1],room:a[4],out});return out;};await settle();assert.equal(calls.length,1);return {pg,...calls[0]};};
    const first=await capture(),base=first.out.positions;
    const ended=first.items.find(n=>n._demoted);assert(ended,'The stored seed has an ended hub');
    const leafIds=first.items.filter(n=>!/organisation|plan/.test(n.type)&&n.id!=='grove-self').map(n=>n.id);
    let pair=null,best=Infinity;
    for(const a of leafIds)for(const b of leafIds){if(a>=b)continue;const d=Math.hypot((base[a].x+base[b].x)/2-base[ended.id].x,(base[a].y+base[b].y)/2-base[ended.id].y);if(d<best){best=d;pair=[a,b];}}
    const addLeaf=(id,type,targets)=>{w.nodes.push(Object.assign({},w.nodes[1],{id,label:'New '+id,type,status:'',summary:''}));targets.forEach((target,i)=>w.edges.push({id:id+'-e'+i,source:id,target,verb:'related to',label:'related to'}));};
    addLeaf('aimed-at-hub','observation',pair);addLeaf('aimed-at-self','observation',['p001']);
    const arrived=await capture();
    const insideAny=(snapshot,ids,roomOf=snapshot.room)=>{const out=[];for(const id of ids){const q=snapshot.out.positions[id];for(const h of snapshot.items){if(!/organisation|plan/.test(h.type))continue;const at=snapshot.out.positions[h.id],r=roomOf(h),d=Math.hypot(q.x-at.x,q.y-at.y);if(d<r)out.push(id+' in '+h.id+' ('+d.toFixed(1)+' < '+r+')');}}return out;};
    assert.deepStrictEqual(insideAny(arrived,['aimed-at-hub','aimed-at-self']),[],'No new stored home enters a reserved hub disc');
    const self=arrived.out.positions['grove-self'],q=arrived.out.positions['aimed-at-self'];
    assert(Math.hypot(q.x-self.x,q.y-self.y)>=90,'A stored home stays out of the reserved identity disc');
    const row=w.nodes.find(n=>n.id===ended.id);row.status='';row.invalid_at=null;
    for(let i=0;i<300;i++)addLeaf('mass-'+i,'observation',[ended.id]);
    const grownBig=await capture(),back=grownBig.items.find(n=>n.id===ended.id);
    assert.equal(back._hubR,62,'The original stored seed grows to its largest hub size');
    assert.deepStrictEqual({...grownBig.out.positions[ended.id]},{...base[ended.id]},'The hub keeps its stored place');
    // A placed item never moves, so the reservation is the largest drawn disc, without the padding a new item gets.
    assert.deepStrictEqual(insideAny(grownBig,['aimed-at-hub','aimed-at-self'],()=>back._hubR),[],'A stored item stays out of the largest drawn disc after growth');
    assert.deepStrictEqual(insideAny(grownBig,w.nodes.filter(n=>n.id.startsWith('mass-')).map(n=>n.id)),[],'New stored homes stay out of reserved hub discs');
    assertFiniteMap(fgPositions(grownBig.pg),grownBig.items.map(n=>n.id),'The grown display remains finite and complete');
  }

  // 6v. The workers panel on the real page: a live account shows stored run records, so no loop runs
  //     and a stored view choice opens nothing. The end of a session empties the panel (native-app
  //     tells agents.js), and the next session reads its own records.
  {
    const st=signedInStore();st['grove-minimal-codex-agents']='1';
    const w=liveWorld(),sv=makeServer(w),pg=page(sv,st,[...ALL_FILES,'agents.js'],HOOK);
    await settle();
    const t={w,sv,st,pg,p:pg.sandbox.productTest,S:pg.sandbox.GroveSimulation,V:pg.sandbox.GroveView};
    const agents=['learner','curator','connector'].map((agent,i)=>({agent,policy:{state:i?'disabled':'limited',reason:i?'demo_autonomous_disabled':'demo_learner_only'},capability:{state:i===1?'unavailable':'limited',reason:['legacy_learner_only','curator_authority_unavailable','legacy_connector_only'][i]},activity:{state:i?'idle':'recorded_running',running_records:i?0:1,stale_records:0}}));
    pg.sandbox.GroveLive.workers=async()=>({ok:true,status:200,body:{schema:'grove.worker-activity.v1',observed_at:'2026-11-04T12:00:00Z',observation_kind:'stored_run_records',service_state:'unknown',service_reason:'no_process_heartbeat',recent_limit:20,recent_has_more:false,agents,recent_runs:[]}});
    const care=()=>pg.sandbox.GroveCare.summary(),learner=()=>care().workers.learner;
    assert.equal(care().enabled,false,'A stored choice opens no workers view');assert.equal(care().running,false,'The display starts no work');
    assert.equal(learner(),'No activity loaded','Nothing shows before a read');
    await pg.sandbox.GroveCare.refresh();
    assert.equal(learner(),'Run recorded as active','The panel shows the stored run record');
    goneNow(w);await pg.sandbox.reloadGrove();await settle();
    assertCleared(t,'workers on');
    assert.equal(learner(),'No activity loaded','The panel drops the ended session');
    assert.deepStrictEqual([...pg.sandbox.__hood.turns()],[],'No turn of the ended session stays');
    w.graphStatus=0;await t.p.startEmail('robin.vale@example.org');await t.p.verifyEmail('246810');await settle();
    assert.equal(learner(),'No activity loaded','The next session starts with no record of the last one');
    await pg.sandbox.GroveCare.refresh();
    assert.equal(learner(),'Run recorded as active','The next session reads its own records');
    assert.deepStrictEqual(pg.errors,[],'workers on: no error');
  }

  // 6w. The session check fails at load, with anything but a 401. Only a 401 ends the local sign-in.
  const OFFLINE='Grove could not be reached. Try again in a moment.';
  const cardAlerts=form=>form.children.filter(c=>c.getAttribute('role')==='alert').map(c=>c.textContent);
  // A page whose sign-in card can take the error line, as it does in the browser.
  function offlinePage(w,store){
    const sv=makeServer(w),st=store||{},pg=page(sv,st,ALL_FILES,HOOK),p=pg.sandbox.productTest,form=makeElement('div');
    p.modalRoot.querySelector=sel=>(sel==='.gp-auth-form'?form:null);
    return {w,sv,st,pg,p,form,S:pg.sandbox.GroveSimulation,V:pg.sandbox.GroveView};
  }
  {
    // A good check shows no note. The note is an alert of its own, outside #grove-product, whose
    // stacking level the loading cover passes.
    const t=await sessionPage(liveWorld()),note=t.p.sessionNote;assertSignedIn(t,'a good check');
    assert.equal(note.hidden,true,'A good check shows no note');assert.equal(note.textContent,'','A good check puts no text in the note');
    assert.equal(note.id,'gp-session-note');assert.equal(note.getAttribute('role'),'alert');
    assert(t.pg.sandbox.document.body.children.includes(note),'The note sits on the body');
    assert(/#gp-session-note\s*\{[^}]*z-index:101/.test(read('product.css')),'The note stands over the loading cover');
  }
  for(const [label,fail] of [['a 503',{sessionStatus:503}],['a 500',{sessionStatus:500}],['a failed request',{sessionThrows:true}]]){
    // Cached sign-in: retain its state but cover the map until the server verifies it.
    const w=liveWorld();Object.assign(w,fail);
    const t=await sessionPage(w);
    assert(t.S.getState().auth,label+': cached identity is not a new verification');assert.equal(t.p.ui.authStage,'checking',label+': the verification cover stays');
    assert.equal(t.S.getState().auth.email,'sam.reyes@example.org',label+': the local sign-in stays');assert.equal(JSON.parse(t.st['grove-minimal-codex-v3']).auth.email,'sam.reyes@example.org',label+': local storage keeps the sign-in');
    assert.equal(t.S.getState().name,'Sam',label+': the local name stays');
    assert.equal(t.p.sessionNote.hidden,false,label+': the note shows');assert.equal(t.p.sessionNote.textContent,OFFLINE,label+': the note holds the offline text');
    assert.equal(t.heard(),0,label+': nothing ended the session');assert.deepStrictEqual(t.pg.errors,[],label+': no error');
    // A later 401 is an answer about the session: it ends it, and the note goes with it.
    goneNow(w);await t.pg.sandbox.reloadGrove();await settle();assertCleared(t,label+', then a 401');
  }
  for(const [label,fail] of [['a 503',{sessionStatus:503}],['a failed request',{sessionThrows:true}]]){
    // Signed out here, while the graph read of a still valid cookie answers: the card says what
    // failed, and that graph stays neither behind the card nor in memory.
    const w=liveWorld();Object.assign(w,fail);
    const st=signedOutStore();st[HIDDEN_KEY]='{"recipe":1}';
    const t=offlinePage(w,st);await settle();
    assertEmptied(t,label+' at load, signed out here');
    assert.equal(t.p.ui.authStage,'signin',label+': the card leaves the waiting step');assert(t.p.html().includes('Continue with Google'),label+': the sign-in options show');
    assert.deepStrictEqual(cardAlerts(t.form),[OFFLINE],label+': the card shows the offline text');
    assert.equal(t.p.sessionNote.hidden,true,label+': the card holds the text, not the note');
    assert.equal(t.S.getState().name,'',label+': anonymous state keeps no name');assert.deepStrictEqual(t.pg.errors,[],label+': no error');
    // The service comes back and the 3-minute poll fires: with nobody signed in here it reads
    // nothing, so no graph goes behind the card and no position is saved.
    Object.assign(w,{sessionStatus:0,sessionThrows:false});
    const readsBefore=t.sv.calls.length;
    t.pg.intervals.find(i=>i.ms===180000).fn();await settle();
    assert.equal(t.sv.calls.length,readsBefore,label+': the poll asks nothing while nobody is signed in here');
    assertEmptied(t,label+' at load, after the poll');assert.deepStrictEqual(cardAlerts(t.form),[OFFLINE],label+': the card keeps the offline text');
    // The next sign-in on that card draws its own map.
    await t.p.startEmail('robin.vale@example.org');await t.p.verifyEmail('246810');await settle();
    assert.equal(t.S.getState().auth.email,'robin.vale@example.org');assert(t.V.nodes().length>0,label+': the next sign-in draws the map');
  }
  {
    // The graph read is still out when the check fails: its late answer draws nothing, and the
    // loading cover does not stay over the card.
    const w=liveWorld();Object.assign(w,{sessionStatus:503,holdGraph:[]});
    const t=offlinePage(w,signedOutStore());await settle();
    const late=w.holdGraph;w.holdGraph=null;assert.equal(late.length,1,'The graph read is out');
    assert.deepStrictEqual(cardAlerts(t.form),[OFFLINE]);
    late[0]();await settle();
    assertEmptied(t,'a late graph answer after the failed check');assert.deepStrictEqual(t.pg.errors,[],'late graph: no error');
  }
  {
    // All of Grove is down, signed out here: the card shows, not the failure text of the map.
    const w=liveWorld();Object.assign(w,{sessionStatus:503,graphStatus:503});
    const t=offlinePage(w,signedOutStore());await settle();
    const cover=t.pg.elements.loading;
    assert(cover.classList.contains('hidden'),'The loading cover steps aside for the card');assert(!/Unable to open/.test(cover.textContent),'No map failure text over the card');
    assert.deepStrictEqual(cardAlerts(t.form),[OFFLINE],'The card shows the offline text');assert.equal(t.p.ui.authStage,'signin');
    assert.deepStrictEqual(t.pg.errors,[],'A failed read of a view that was cleared changes and reports nothing');
    // Signed in here: the map cannot draw, so its own failure text shows, with the note over it.
    const w2=liveWorld();Object.assign(w2,{sessionStatus:503,graphStatus:503});
    const t2=await sessionPage(w2);const cover2=t2.pg.elements.loading;
    assert(!cover2.classList.contains('hidden'),'The cover stays when the map cannot draw');assert(/Unable to open/.test(cover2.textContent),'The map says it failed');
    assert.equal(t2.p.sessionNote.textContent,OFFLINE,'The note says what failed');assert.equal(t2.S.getState().auth.email,'sam.reyes@example.org');
  }
  {
    // A check still out when a 401 of another call ended the session: the person has moved on to
    // the code step, and its late failure leaves that step and that address alone.
    const w=liveWorld();w.holdSession=[];
    const t=await sessionPage(w);
    const late=w.holdSession;w.holdSession=null;assert.equal(late.length,1,'The session check is out');
    goneNow(w);await t.pg.sandbox.reloadGrove();await settle();assertCleared(t,'a check out at the 401');
    await t.p.startEmail('robin.vale@example.org');assert.equal(t.p.ui.authStage,'code');
    const calls=t.V.graph().calls.length;
    late[0](t.sv.typed(503,'unavailable','Try again later'));await settle();
    assert.equal(t.p.ui.authStage,'code','The late failure keeps the code step');assert.equal(t.p.ui.email,'robin.vale@example.org');assert.equal(t.p.ui.error,'','The late failure says nothing on the card');
    assert.equal(t.V.graph().calls.length,calls,'The late failure clears nothing');
  }

  // 6x. The session ends with something of the first person open: each is gone from the page.
  {
    // The tour, with a store title in its spotlight.
    const w=liveWorld();w.holdGraph=[];
    const st=JSON.parse(signedInStore()['grove-minimal-codex-v3']);st.tourComplete=false;
    const t=await sessionPage(w,{'grove-minimal-codex-v3':JSON.stringify(st)}),tour=t.p.tourRoot,parts={};
    const part=sel=>{const el=parts[sel]||(parts[sel]=makeElement('div'));el.querySelector=()=>makeElement('button');return el;};
    tour.querySelector=sel=>(tour.innerHTML?part(sel):null);
    const held=w.holdGraph;w.holdGraph=null;held[0]();await settle();
    assert.equal(t.p.ui.tour,1,'The tour is open');assert(tour.innerHTML.includes('gp-tour-card')&&tour.innerHTML.includes('gp-node-spotlight'),'The tour card and spotlight show');
    assert.equal(parts['.gp-node-spotlight'].getAttribute('aria-label'),'Open highlighted '+w.nodes[0].label+' node','The spotlight names a store title');
    goneNow(w);await t.pg.sandbox.reloadGrove();await settle();
    assertCleared(t,'the tour open at the 401');
    assert.equal(tour.querySelector('.gp-tour-card'),null,'The tour card is gone');assert.equal(tour.querySelector('.gp-node-spotlight'),null,'The spotlight is gone');
    assert(!t.w.nodes.some(n=>n.label&&tour.innerHTML.includes(n.label)),'No title stays in the tour root');
    assert(!t.pg.sandbox.document.body.classList.contains('gp-tour-open'),'The page is out of the tour');
    assert.equal(t.S.getState().tourComplete,true,'The tour counts as over');
  }
  {
    // Sign in by email in the page, with a code in the form: neither stays for the next person.
    const w=liveWorld();Object.assign(w,{session:false,graphStatus:401});
    const t=await sessionPage(w,signedOutStore());w.graphStatus=0;
    await t.p.startEmail('sam.reyes@example.org');await t.p.verifyEmail('246810');await settle();
    assertSignedIn(t,'email, signed in');assert.equal(t.p.ui.email,'sam.reyes@example.org');
    t.p.ui.code='246810';   // the code form's oninput keeps what is typed here
    w.session=false;w.graphStatus=401;await t.pg.sandbox.reloadGrove();await settle();
    assertCleared(t,'email, the session ended');
    assert.equal(t.p.ui.email,'','The address is gone');assert.equal(t.p.ui.code,'','The code is gone');
    assert(!t.p.html().includes('sam.reyes@example.org'),'The form holds no address');assert(/name="email"[^>]*value=""/.test(t.p.html()),'The email field is empty');
  }
  {
    // A confirmation open: the card shows no confirmation dialog.
    const t=await sessionPage(liveWorld());
    t.p.ui.panel='profile';t.p.ui.settingsTab='sources';t.p.ui.confirm={type:'file',id:'file-1'};t.p.render(true);
    assert(t.p.html().includes('gp-confirm-dialog'),'The confirmation shows');
    goneNow(t.w);await t.pg.sandbox.reloadGrove();await settle();
    assertCleared(t,'a confirmation open at the 401');
    assert.equal(t.p.ui.confirm,null,'The confirmation is closed');assert(!/gp-confirm/.test(t.p.html()),'The card shows no confirmation dialog');
  }
  {
    // A host step and an error of the first person: neither reaches the next person.
    const t=await sessionPage(liveWorld());
    t.p.ui.host='claude';t.p.ui.error='That source could not be added. Please try again.';t.p.render(true);
    goneNow(t.w);await t.pg.sandbox.reloadGrove();await settle();
    assertCleared(t,'a host step and an error at the 401');
    assert.equal(t.p.ui.host,null,'The host step is gone');assert.equal(t.p.ui.error,'','The error is gone');
  }
  {
    // A guide open: the card takes the screen and the guide stays closed for the next person.
    const t=await sessionPage(liveWorld());
    t.p.ui.guide='chatgpt';t.p.render(true);
    goneNow(t.w);await t.pg.sandbox.reloadGrove();await settle();
    assertCleared(t,'a guide open at the 401');assert.equal(t.p.ui.guide,null,'The guide is closed');
  }
  {
    // A link under the pointer: that link stays out of memory.
    const t=await sessionPage(liveWorld());
    t.V.graph().onLinkHover()(t.V.graph().graphData().links[0]);
    assert(t.pg.sandbox.__mapState().hoverLink,'A link is under the pointer');
    goneNow(t.w);await t.pg.sandbox.reloadGrove();await settle();assertCleared(t,'a link under the pointer');
  }
  {
    // The first person moves the camera, a 401, the next person signs in: the next map frames itself.
    let down=null;
    const t=await sessionPage(liveWorld(),null,el=>{el.canvas=makeElement('main');el.canvas.addEventListener=(k,fn)=>{if(k==='pointerdown')down=fn;};});
    assert(down,'The canvas listens for the pointer');down();
    const moved=t.V.graph().calls.length;await t.pg.sandbox.reloadGrove();await settle();
    assert(!t.V.graph().calls.slice(moved).includes('centerAt'),'The first person keeps the camera they moved');
    goneNow(t.w);await t.pg.sandbox.reloadGrove();await settle();assertCleared(t,'the camera moved');
    Object.assign(t.w,secondWorld(),{session:false,graphStatus:0});
    const before=t.V.graph().calls.length;
    await signInAgain(t,'the camera, the next person');
    const G=t.V.graph(),after=G.calls.slice(before);
    assert(after.includes('centerAt')&&after.includes('zoom'),'The next map frames itself');
    const xs=t.V.nodes().map(n=>n.x);assert(Math.abs(G.center.x-(Math.min(...xs)+Math.max(...xs))/2)<1,'The frame is centred on the next map');
  }
  {
    // A poll ran before the end: the graph it read leaves with the session.
    const t=await sessionPage(liveWorld()),poll=t.pg.intervals.find(i=>i.ms===180000),reads=()=>t.sv.calls.filter(c=>c.path==='/v1/views/graph').length,before=reads();
    poll.fn();await settle();assert.equal(reads(),before+1,'A poll reads the graph');
    goneNow(t.w);await t.pg.sandbox.reloadGrove();await settle();assertCleared(t,'a poll ran');
  }
  {
    // Any answer that says the session is over ends it: a plain 401, and a typed error of that code.
    const plain=await sessionPage(liveWorld()),w=plain.w;w.holdGraph=[];plain.pg.sandbox.reloadGrove();await settle();
    const out=w.holdGraph;w.holdGraph=null;out[0](plain.sv.reply(401));await settle();
    assertCleared(plain,'a 401 with no typed body');assert.equal(plain.heard(),1);
    const typed=await sessionPage(liveWorld());Object.assign(typed.w,{session:false,graphStatus:403,graphCode:'unauthenticated'});
    await typed.pg.sandbox.reloadGrove();await settle();assertCleared(typed,'the typed code on another status');assert.equal(typed.heard(),1);
    const other=await sessionPage(liveWorld());other.w.graphStatus=503;
    await other.pg.sandbox.reloadGrove();await settle();assertSignedIn(other,'another failure');assert.equal(other.heard(),0,'Another failure ends nothing');
    // A failed sign-in does not count as a sign-in: a call sent before it still speaks for this page.
    const armed=await sessionPage(liveWorld());armed.w.code='111111';armed.w.holdGraph=[];armed.pg.sandbox.reloadGrove();await settle();
    const before=armed.w.holdGraph;armed.w.holdGraph=null;
    const answer=await armed.pg.sandbox.GroveLive.emailVerify('sam.reyes@example.org','000000');assert.equal(answer.ok,false);
    before[0](armed.sv.typed(401,'unauthenticated','Sign in to continue'));await settle();assert.equal(armed.heard(),1,'A refused code starts no session: the 401 still counts');
    // A refused session check does not arm the event again: a second 401 of that session sends no second event.
    const once=await sessionPage(liveWorld());goneNow(once.w);await once.pg.sandbox.reloadGrove();await settle();assert.equal(once.heard(),1);
    await once.pg.sandbox.GroveLive.session();await once.pg.sandbox.reloadGrove();await settle();assert.equal(once.heard(),1,'A refused check arms nothing');
    // A session check that answers yes arms it again, in the same session.
    once.w.session=true;await once.pg.sandbox.GroveLive.session();await once.pg.sandbox.reloadGrove();await settle();assert.equal(once.heard(),2,'A good check arms the event again');
    // The callers read the state signOut hands back.
    assert.equal(once.S.signOut().auth,null,'signOut answers with the state it leaves');
  }

  // 6y. A legend click only hides and shows, in live mode: every home and pin stays at its display place
  //     to the last bit, the links come back, and no relayout, reheat, hold-off or re-fit runs.
  const legendPage=(w,st,search)=>{const sv=makeServer(w);return {sv,pg:page(sv,st,GRAPH_FILES,null,null,(el,env)=>{legendRows(el);env.search=search||'';})};};
  const clickKind=(pg,kind)=>pg.elements['legend-list'].rows().find(r=>r.getAttribute('data-type')===kind).click();
  {
    const w=liveWorld(),st=signedInStore(),{sv,pg}=legendPage(w,st);await settle();
    const V=pg.sandbox.GroveView,G=V.graph(),drawn=()=>G.graphData().nodes.map(n=>n.id).sort(),linked=()=>G.graphData().links.map(l=>l.source+'>'+l.target+'>'+l.verb).sort();
    const pinned=new Set(V.nodes().filter(n=>n.fx!=null).map(n=>n.id));
    assert(pinned.has('grove-self')&&V.nodes().some(n=>n.type==='organisation'&&pinned.has(n.id)),'Hubs and the identity are pinned');
    const stored=id=>w.positions[id==='grove-self'?'p001':id];
    let display=fgPositions(pg),storedBefore=JSON.stringify(w.positions);
    const atStored=label=>V.nodes().forEach(n=>{
      const at=display[n.id],saved=stored(n.id);assert(saved&&at,label+': '+n.id+' has both maps');
      assert(Number.isFinite(saved.x)&&Number.isFinite(saved.y)&&Number.isFinite(at.x)&&Number.isFinite(at.y),label+': finite maps');
      assert(n.__home[0]===at.x&&n.__home[1]===at.y,label+': the display home stays exact for '+n.id);
      assert(n.x===at.x&&n.y===at.y,label+': the display coordinate stays exact for '+n.id);
      if(pinned.has(n.id))assert(n.fx===at.x&&n.fy===at.y,label+': the display pin stays exact for '+n.id);else assert(n.fx==null&&n.fy==null,label+': '+n.id+' takes no pin');
      assert.deepStrictEqual(placedPositions(pg)[n.id],saved,label+': the stored map stays independent');
    });
    atStored('first visit');
    const allNodes=drawn(),allLinks=linked(),wholeLinks=G.graphData().links.map(l=>({source:l.source,target:l.target,key:l.source+'>'+l.target+'>'+l.verb})),sizes=V.nodes().map(n=>n._hubR),saves=positionsCalls(sv).length;
    assert(allLinks.length>100&&new Set(allLinks).size>100,'The map has links to hide');
    G.calls.length=0;
    for(const kind of ['organisation','plan','person','task','recipe']){
      const members=V.nodes().filter(n=>n.type===kind);assert(members.length,kind+' is on the map');
      clickKind(pg,kind);
      assert(!G.graphData().nodes.some(n=>n.type===kind),kind+' is hidden');
      const memberIds=new Set(members.map(n=>n.id));
      assert.deepStrictEqual(linked(),wholeLinks.filter(l=>!memberIds.has(l.source)&&!memberIds.has(l.target)).map(l=>l.key).sort(),kind+': its links are hidden with it, and only those');
      const shownIds=new Set(drawn());assert(G.graphData().links.every(l=>shownIds.has(l.source)&&shownIds.has(l.target)),kind+': no link hangs in the air');
      atStored(kind+' hidden');
      clickKind(pg,kind);
      assert.deepStrictEqual(drawn(),allNodes,kind+': its items come back');assert.deepStrictEqual(linked(),allLinks,kind+': its links come back');
      atStored(kind+' shown again');
    }
    assert.deepStrictEqual(G.calls.filter(c=>c!=='graphData'),[],'A legend click reheats nothing and moves no camera');
    assert.equal(G.calls.length,10,'Each click hands the map its visible nodes and links');
    assert.deepStrictEqual(pg.timeouts.filter(x=>x.ms===3500),[],'No re-fit is waiting');assert.equal(pg.sandbox.__brainHoldOff,false,'The hold stays on');
    assert.deepStrictEqual(V.nodes().map(n=>n._hubR),sizes,'No hub changes size');
    assert.equal(positionsCalls(sv).length,saves,'A legend click saves nothing');
    assert.equal(JSON.stringify(w.positions),storedBefore,'A legend click changes no stored coordinate');
    // A kind hidden while new items arrive, then shown: every old home stays exact, in that page.
    clickKind(pg,'observation');
    const olds=V.nodes().map(n=>n.id);
    w.nodes.push(Object.assign({},w.nodes[1],{id:'kind-hub',label:'Wrenfield Cooperative',type:'organisation',summary:''}),Object.assign({},w.nodes[1],{id:'kind-task',label:'Plan the cooperative fair',type:'task',summary:''}),Object.assign({},w.nodes[1],{id:'kind-seen',label:'Noticed a longer queue',type:'observation',summary:''}));
    w.edges.push({id:'kind-e1',source:'kind-task',target:'kind-hub',verb:'part of',label:'part of'},{id:'kind-e2',source:'kind-seen',target:'kind-task',verb:'about',label:'about'});
    await pg.sandbox.reloadGrove();await settle();
    for(const [id,at] of Object.entries(JSON.parse(storedBefore)))assert.deepStrictEqual(w.positions[id],at,'An arrival never overwrites an old stored position');
    display=fgPositions(pg);storedBefore=JSON.stringify(w.positions);
    pinned.add('kind-hub');atStored('after new items, in that page');
    assert(olds.every(id=>V.nodes().some(n=>n.id===id)),'Every old item stays');
    assert(!drawn().includes('kind-seen'),'A new item of a hidden kind stays hidden');assert(drawn().includes('kind-task'),'A new item of a shown kind shows');
    clickKind(pg,'observation');
    assert(drawn().includes('kind-seen'),'It comes back with its kind');assert(linked().includes('kind-seen>kind-task>about'),'It comes back with its link');
    atStored('the hidden kind shown again');
    // A later page load draws the same map as any other fresh page: the display is a projection of the current graph.
    const later=page(sv,st,GRAPH_FILES);await settle();const again=page(sv,st,GRAPH_FILES);await settle();const fresh=fgPositions(again);
    for(const n of later.sandbox.GroveView.nodes()){const at=fresh[n.id];assert(n.__home[0]===at.x&&n.__home[1]===at.y,'A later load keeps the display place of '+n.id);}
    assert.equal(JSON.stringify(w.positions),storedBefore,'A later load changes no stored coordinate');
    assert.deepStrictEqual(pg.errors,[],'legend: no error');
  }
  // 6z. Fixture mode (the sim engine of the checks) keeps the sim: its legend click lays the map out
  //     again and heats it, and its URL settings change the layout.
  {
    const fixtureStore=()=>({'grove-minimal-codex-v3':JSON.stringify({version:3,schema:4,auth:{provider:'email',email:'sam.reyes@example.org'},name:'Sam',onboardingComplete:true,onboardingStep:4,tourComplete:true,accountImports:{chatgpt:{id:'chatgpt',source:'conversations',status:'ready',progress:100,kind:'account'}}})});
    const fixturePage=async search=>{const pg=page(makeServer(liveWorld()),fixtureStore(),GRAPH_FILES,null,null,(el,env)=>{legendRows(el);env.search=search;env.globals.GROVE_FIXTURE=JSON.parse(JSON.stringify(synthetic));});await settle();return pg;};
    // A map that cannot draw keeps the sim's failure text there, with or without a local sign-in.
    const broken=page(makeServer(liveWorld()),{'grove-minimal-codex-v3':JSON.stringify({version:3,schema:4,auth:null,onboardingComplete:true,onboardingStep:4,tourComplete:true})},GRAPH_FILES,null,null,(el,env)=>{env.globals.GROVE_FIXTURE=Object.assign(JSON.parse(JSON.stringify(synthetic)),{stats:undefined});});await settle();
    assert(/Unable to open/.test(broken.elements.loading.textContent),'Fixture mode keeps the failure text of the sim');assert(!broken.elements.loading.classList.contains('hidden'));
    const plain=await fixturePage('');assert.equal(plain.sandbox.GroveSimulation.isLive(),false,'This page runs on the fixture');assert(plain.sandbox.GroveView.nodes().length>10,'The fixture draws');
    const layout=pg=>JSON.stringify(fgPositions(pg));
    for(const search of ['?classic=1','?dense=0','?orbit=0'])assert.notEqual(layout(await fixturePage(search)),layout(plain),search+' changes the layout in fixture mode');
    const hubs=()=>plain.sandbox.GroveView.nodes().filter(n=>n.type==='company').map(n=>[n.id,n.fx,n.fy].join());
    const before=hubs(),G=plain.sandbox.GroveView.graph();G.calls.length=0;
    clickKind(plain,'project');
    assert.notDeepStrictEqual(hubs(),before,'A legend click lays the hubs out again');assert(G.calls.includes('d3ReheatSimulation'),'A legend click heats the map');
    clickKind(plain,'project');   // the first click after a load keeps the frame; the next one lets go
    assert.equal(plain.sandbox.__brainHoldOff,true,'The hold lets go');assert(plain.timeouts.some(x=>x.ms===3500),'A re-fit waits');
  }
  // 6aa. Live mode has no layout settings in its URL: a first visit with one saves exactly what a
  //      plain first visit saves, and draws the same sizes. ?hood is a view setting and saves the same.
  {
    const firstVisit=async search=>{const sv=makeServer(liveWorld()),pg=page(sv,signedInStore(),[...GRAPH_FILES,'agents.js'],null,null,(el,env)=>{env.search=search;});await settle();return {pg,save:positionsCalls(sv)[0].body.positions,look:pg.sandbox.GroveView.nodes().map(n=>drawnLook(pg,Object.assign({},n,{__born:0})).radius)};};
    const plain=await firstVisit('');assert(plain.save.length>100,'A first visit saves the map');
    for(const search of ['?classic=1','?dense=0','?orbit=0','?classic=1&dense=0&orbit=0','?x=1&classic=1','?hood=1','?hood=0']){
      const seen=await firstVisit(search);
      assert.deepStrictEqual(seen.save,plain.save,search+' saves what a plain first visit saves');assert.deepStrictEqual(seen.look,plain.look,search+' draws what a plain first visit draws');
    }
  }

  // 6ab. The sign-in slice on the page: what the server's answers and the address decide.
  const COPY={google:'Google sign-in did not finish. Try again.',tooMany:'Please wait a few minutes before you request another code.'};
  const storedPerson=extra=>({'grove-minimal-codex-v3':JSON.stringify(Object.assign({version:3,schema:4,auth:null,name:'Sam',growth:{focus:'Sam likes long walks',reading:'',avoid:'',confirm:true},files:{notes:[{name:'sam-notes.txt'}]},onboardingComplete:true,onboardingStep:4,tourComplete:true},extra))});
  // A page at an address: env.search and env.pathname, with a history that records each new address.
  function signinPage(w,st,where){
    const sv=makeServer(w),addresses=[];
    let pg;pg=page(sv,st,ALL_FILES,HOOK,null,(el,env)=>{Object.assign(env,{search:'',pathname:'/signin'},where);if(!(where&&where.noHistory))env.globals.history=where&&where.badHistory?{replaceState(){throw new Error('denied');}}:{replaceState:(s,t,url)=>{addresses.push(url);if(pg){const to=new URL(url,'http://grove.test/');Object.assign(pg.sandbox.location,{pathname:to.pathname,search:to.search});}}};});
    return {w,sv,st,pg,addresses,p:pg.sandbox.productTest,S:pg.sandbox.GroveSimulation,graphReads:()=>sv.calls.filter(c=>c.path==='/v1/views/graph').length};
  }
  const signedOutWorld=()=>Object.assign(liveWorld(),{session:false,graphStatus:401});
  {
    // The Google error: shown on the card, and the query leaves the address.
    const t=signinPage(signedOutWorld(),{},{search:'?signin_error=google'});
    assert.equal(t.p.ui.authStage,'checking');await settle();
    assert.equal(t.p.ui.authStage,'signin');assert.equal(t.p.ui.error,COPY.google,'The card shows the Google error');
    assert.deepStrictEqual(t.addresses,['/signin'],'The query leaves the address, once');
    assert.equal(t.S.getState().auth,null);assert.deepStrictEqual(t.pg.errors,[]);
    t.p.handle('auth-back');await settle();assert.equal(t.p.ui.error,'','The error goes with the next step');
    // A stale local sign-in hears the 401 of the graph read first: the error still reaches the card.
    const staleWorld=Object.assign(signedOutWorld(),{holdSession:[]}),stale=signinPage(staleWorld,signedInStore(),{search:'?signin_error=google'});await settle();
    assert.equal(staleWorld.holdSession.length,1,'The session answer is still out');
    assert.equal(stale.p.ui.authStage,'signin');assert.equal(stale.p.ui.error,COPY.google,'A stale local sign-in keeps the Google error');assert.equal(stale.S.getState().auth,null);
    staleWorld.holdSession[0]();await settle();assert.equal(stale.p.ui.error,COPY.google,'The session answer keeps it');
    // A session that answers ok drops it; a page with no history, or one that refuses, still works.
    const ok=signinPage(liveWorld(),{},{search:'?signin_error=google'});await settle();
    assert.equal(ok.p.ui.authStage,null);assert.equal(ok.p.ui.error,'','A session in hand shows no error');
    for(const where of [{search:'?signin_error=google',noHistory:true},{search:'?signin_error=google',badHistory:true}]){
      const t2=signinPage(signedOutWorld(),{},where);await settle();
      assert.equal(t2.p.ui.error,COPY.google,'The error shows with no usable history');assert.deepStrictEqual(t2.pg.errors,[]);
    }
    // Another value names no known failure: nothing shows, and the address is still clean. No query: nothing moves.
    const other=signinPage(signedOutWorld(),{},{search:'?signin_error=other'});await settle();
    assert.equal(other.p.ui.error,'');assert.deepStrictEqual(other.addresses,['/signin']);
    const plain=signinPage(signedOutWorld(),{});await settle();
    assert.equal(plain.p.ui.error,'');assert.deepStrictEqual(plain.addresses,[]);
    // The card links the privacy page once it shows, in live mode.
    assert(plain.p.html().includes('href="/privacy"'),'The card links the privacy page');
    const waiting=signinPage(signedOutWorld(),{});assert(!waiting.p.html().includes('href="/privacy"'),'The waiting card has no link yet');await settle();
  }
  {
    // A 429 on the email start says so; any other failure keeps the address text.
    const t=signinPage(signedOutWorld(),{});await settle();
    t.w.emailStartStatus=429;await t.p.startEmail('sam.reyes@example.org');
    assert.equal(t.p.ui.error,COPY.tooMany,'A 429 names the limit');assert.equal(t.p.ui.authStage,'signin');
    t.w.emailStartStatus=503;await t.p.startEmail('sam.reyes@example.org');
    assert(t.p.ui.error.includes('could not send a code'),'Another failure keeps its text');
  }
  {
    // After a live sign-in on /signin, the address is / . At / it does not move.
    const t=signinPage(signedOutWorld(),{});await settle();
    assert.deepStrictEqual(t.addresses,[],'The card keeps its address until the sign-in');
    t.w.graphStatus=0;await t.p.startEmail('sam.reyes@example.org');assert.deepStrictEqual(t.addresses,[],'A sent code moves nothing');
    await t.p.verifyEmail('000000');assert.deepStrictEqual(t.addresses,[],'A wrong code moves nothing');
    await t.p.verifyEmail('246810');assert.deepStrictEqual(t.addresses,['/'],'The email code ends at /');
    const back=signinPage(liveWorld(),{});await settle();assert.deepStrictEqual(back.addresses,['/'],'The way back from a session ends at /');
    const local=signinPage(liveWorld(),signedInStore());await settle();assert.deepStrictEqual(local.addresses,['/'],'A signed-in visit to /signin ends at /');
    const home=signinPage(liveWorld(),{},{pathname:'/'});await settle();assert.deepStrictEqual(home.addresses,[],'At / the address stays');
  }
  {
    // The way back from a session names the method; with none, it is Google as before.
    for(const [method,provider] of [['google','google'],['email','email'],[null,'google']]){
      const w=Object.assign(liveWorld(),{account:'acct-a',method});
      const t=signinPage(w,{},{pathname:'/'});await settle();
      assert.equal(t.S.getState().auth.provider,provider,'The way back uses the method '+method);assert.equal(t.S.getState().auth.email,'');
    }
  }
  const noTraceOfSam=(t,label)=>{
    const S=t.S.getState();
    assert.equal(S.name,'',label+': the name is gone');assert.equal(S.growth.focus,'',label+': the guidance is gone');assert.deepStrictEqual(JSON.parse(JSON.stringify(S.files)),{},label+': the file list is gone');
    assert(!t.st['grove-minimal-codex-v3'].includes('Sam')&&!t.st['grove-minimal-codex-v3'].includes('sam-notes'),label+': local storage holds none of it');
  };
  {
    // Another account: the earlier person's local state goes before the sign-in completes.
    const w=Object.assign(signedOutWorld(),{account:'acct-b',method:'email',graphStatus:0,hasItems:false});w.session=false;
    const t=signinPage(w,storedPerson({account:'acct-a'}));await settle();
    assert.equal(t.S.getState().account,null,'Anonymous state has no current account');assert.equal(t.S.getState().name,'');
    t.p.ui.nameDraft='Sam';t.p.ui.growthDraft={focus:'Sam likes long walks'};t.p.ui.noteDraft={title:'Sam',body:'Sam'};
    await t.p.startEmail('robin.vale@example.org');await t.p.verifyEmail('246810');
    assert.equal(t.S.getState().account,'acct-b','The new account binds');noTraceOfSam(t,'email, another account');
    assert.equal(t.S.getState().auth.email,'robin.vale@example.org','The typed address survives the reset');assert.equal(t.S.getState().onboardingComplete,false);
    assert(t.p.html().includes('Get Started'),'The next person gets the first-run welcome');
    assert.equal(t.p.ui.nameDraft,'');assert.equal(t.p.ui.growthDraft,null);assert.equal(t.p.ui.noteDraft.body,'');
    assert.deepStrictEqual(t.pg.errors,[]);
    // The same in one page: the first person logs out, the next signs in.
    const onePage=signinPage(Object.assign(liveWorld(),{account:'acct-a',method:'email'}),storedPerson({account:'acct-a',auth:{provider:'email',email:'sam.reyes@example.org'}}),{pathname:'/'});await settle();
    assert.equal(onePage.S.getState().name,'Sam','The same account keeps its local state');assert.equal(onePage.p.ui.authStage,null);
    onePage.p.handle('logout');await settle();assert.equal(onePage.p.ui.authStage,'signin');assert.equal(onePage.S.getState().account,null,'Logout clears the current account');assert.equal(onePage.S.getState().name,'');
    onePage.w.account='acct-b';await onePage.p.startEmail('robin.vale@example.org');await onePage.p.verifyEmail('246810');
    noTraceOfSam(onePage,'one page, the next person');assert.equal(onePage.S.getState().account,'acct-b');
    // The way back on load (a session of another person, with a local sign-in of the first): reset too.
    const load=signinPage(Object.assign(liveWorld(),{account:'acct-b',method:'google'}),storedPerson({account:'acct-a',auth:{provider:'email',email:'sam.reyes@example.org'}}),{pathname:'/'});await settle();
    noTraceOfSam(load,'on load');assert.equal(load.S.getState().auth.provider,'google','The new session signs in');assert.equal(load.S.getState().auth.email,'','The first address is gone');assert.equal(load.S.getState().account,'acct-b');
    // The same account after logout keeps no private content.
    const again=signinPage(Object.assign(signedOutWorld(),{account:'acct-a',graphStatus:0}),storedPerson({account:'acct-a'}));await settle();
    again.w.session=false;await again.p.startEmail('sam.reyes@example.org');await again.p.verifyEmail('246810');
    noTraceOfSam(again,'same account after logout');
    // No stored account (an older page): unbound content is cleared.
    const older=signinPage(Object.assign(liveWorld(),{account:'acct-a'}),storedPerson({auth:{provider:'email',email:'sam.reyes@example.org'}}),{pathname:'/'});await settle();
    assert.equal(older.S.getState().account,'acct-a','An absent stored account binds');assert.equal(older.S.getState().name,'','An absent stored account cannot own cached content');
    assert.equal(JSON.parse(older.st['grove-minimal-codex-v3']).account,'acct-a','The account is in local storage');
    // A different verified default account clears the old account.
    const none=signinPage(liveWorld(),storedPerson({account:'acct-a',auth:{provider:'email',email:'sam.reyes@example.org'}}),{pathname:'/'});await settle();
    assert.equal(none.S.getState().account,'acct-default');assert.equal(none.S.getState().name,'');
  }
  {
    // Resume: a world with items on a browser with nothing in progress opens the map.
    const w=Object.assign(liveWorld(),{account:'acct-a',method:'google',hasItems:true});
    const t=signinPage(w,{},{pathname:'/'});await settle();
    let s=t.S.getState();assert(s.onboardingComplete&&s.tourComplete,'A world with items skips the first run');assert.equal(s.auth.provider,'google');
    assert(!t.p.html().includes('Get Started'),'No welcome');assert.equal(t.p.ui.authStage,null);assert.equal(t.p.ui.tour,0,'No tour');
    assert(t.pg.sandbox.GroveView.nodes().length>0,'The map shows the items');
    assert.deepStrictEqual(t.pg.errors,[]);
    // No items: the first-run welcome.
    const first=signinPage(Object.assign(liveWorld(),{account:'acct-a',hasItems:false}),{},{pathname:'/'});await settle();
    assert.equal(first.S.getState().onboardingComplete,false);assert(first.p.html().includes('Get Started'),'An empty world gets the welcome');
    // Onboarding in progress here stays in progress, with items or not.
    const going=signinPage(Object.assign(liveWorld(),{account:'acct-a',hasItems:true}),storedPerson({account:'acct-a',onboardingComplete:false,onboardingStep:2,tourComplete:false}),{pathname:'/'});await settle();
    assert.equal(going.S.getState().onboardingComplete,false,'An onboarding in progress is kept');assert.equal(going.S.getState().onboardingStep,2);
    // The email code resumes too, and a reload of the graph opens it.
    const mail=signinPage(Object.assign(signedOutWorld(),{account:'acct-a',method:'email',hasItems:true,graphStatus:0}),{});await settle();
    const before=mail.graphReads();await mail.p.startEmail('sam.reyes@example.org');await mail.p.verifyEmail('246810');await settle();
    assert(mail.S.getState().onboardingComplete,'The email code resumes');assert.equal(mail.S.getState().auth.email,'sam.reyes@example.org');assert(mail.graphReads()>before,'The map reads the graph');
    assert(!mail.p.html().includes('Get Started'));assert.equal(mail.p.ui.authStage,null);
    // A local sign-in still on the welcome, whose world has since gained items, moves on.
    const later=signinPage(Object.assign(liveWorld(),{account:'acct-a',hasItems:true}),storedPerson({account:'acct-a',auth:{provider:'google',email:''},name:'',onboardingComplete:false,onboardingStep:0,tourComplete:false}),{pathname:'/'});await settle();
    assert(later.S.getState().onboardingComplete,'A person on the welcome moves on');assert(!later.p.html().includes('Get Started'));
    // Another account with items: the reset first, then the map; none of the first person's state.
    const next=signinPage(Object.assign(liveWorld(),{account:'acct-b',hasItems:true}),storedPerson({account:'acct-a',auth:{provider:'email',email:'sam.reyes@example.org'}}),{pathname:'/'});await settle();
    noTraceOfSam(next,'another account with items');assert(next.S.getState().onboardingComplete,'The next person resumes their own world');assert(next.pg.sandbox.GroveView.nodes().length>0,'Their map shows');
    assert.deepStrictEqual(next.pg.errors,[]);
  }

  // 6ad. The way back to the consent page: /signin?next=<the /authorize path and query>. Only a
  //      next that starts with /authorize? passes, on the sign-in page, at most 2048 characters.
  {
    const next='/authorize?response_type=code&client_id=c-1&redirect_uri=http%3A%2F%2F127.0.0.1%3A43117%2Fcallback&state=s-1&scope=grove.read+grove.write';
    const at=n=>({search:'?next='+encodeURIComponent(n)});
    const outWorld=()=>Object.assign(liveWorld(),{session:false});
    // Google carries the next to the server, encoded once.
    const g=signinPage(outWorld(),{},at(next));await settle();
    assert.equal(g.p.ui.authStage,'signin');g.p.chooseSignIn('google');
    assert.deepStrictEqual(g.pg.assigned,['/v1/auth/google/start?next='+encodeURIComponent(next)],'Google starts with the next');
    // An email-code sign-in goes back to it, and signs in nothing on this page on the way.
    const e=signinPage(outWorld(),{},at(next));await settle();
    await e.p.startEmail('robin.vale@example.org');await e.p.verifyEmail('246810');
    assert.deepStrictEqual(e.pg.assigned,[next],'The email code returns to the consent page');assert.equal(e.S.getState().auth,null);
    // A wrong code stays on the card.
    const w=signinPage(outWorld(),{},at(next));await settle();
    await w.p.startEmail('robin.vale@example.org');await w.p.verifyEmail('000000');
    assert.deepStrictEqual(w.pg.assigned,[],'A wrong code goes nowhere');assert.equal(w.p.ui.authStage,'code');
    // A page that is signed in already goes straight back.
    const s=signinPage(liveWorld(),{},at(next));await settle();
    assert.deepStrictEqual(s.pg.assigned,[next],'A signed-in sign-in page returns to the consent page');
    // Any other next is ignored: Google keeps its default, and the email code signs in here.
    for(const bad of ['/','/v1/views/graph','//evil.example/authorize?x=1','https://evil.example/authorize?x=1','/authorize','/authorizex?a=1','/authorize?a=\\b','/authorize?a= b','/authorize?a='+'x'.repeat(2048)]){
      const b=signinPage(outWorld(),{},at(bad));await settle();
      b.p.chooseSignIn('google');assert.deepStrictEqual(b.pg.assigned,['/v1/auth/google/start?next=/'],'Ignored next: '+bad.slice(0,40));
      const m=signinPage(outWorld(),{},at(bad));await settle();await m.p.startEmail('robin.vale@example.org');await m.p.verifyEmail('246810');
      assert.deepStrictEqual(m.pg.assigned,[],'No return for: '+bad.slice(0,40));assert.equal(m.S.getState().auth.provider,'email','The email code signs in here: '+bad.slice(0,40));
    }
    // The longest next the server keeps passes whole; off the sign-in page a next is ignored.
    const longest='/authorize?state='+'s'.repeat(2048-'/authorize?state='.length);
    const l=signinPage(outWorld(),{},at(longest));await settle();l.p.chooseSignIn('google');
    assert.deepStrictEqual(l.pg.assigned,['/v1/auth/google/start?next='+encodeURIComponent(longest)],'A next of 2048 characters passes');
    const home=signinPage(outWorld(),{},Object.assign(at(next),{pathname:'/'}));await settle();home.p.chooseSignIn('google');
    assert.deepStrictEqual(home.pg.assigned,['/v1/auth/google/start?next=/'],'Off the sign-in page, a next is ignored');
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
  // build.py writes each vendored font to dist/assets under the hash of its bytes, and the page
  // links it on the same origin, so every font the page names holds its exact vendored bytes.
  const fontFiles=[...new Set([...read('vendor/fonts.css').matchAll(/url\((fonts\/[^)]+)\)/g)].map(m=>m[1]))];
  const fontBytes=fontFiles.map(f=>fs.readFileSync(path.join(web,'vendor',f)));
  const built=[...new Set([...dist.matchAll(/url\(\/assets\/grove\/([a-f0-9]{64})\.woff2\)/g)].map(m=>m[1]))];
  assert.equal(built.length,fontFiles.length,'Every vendored font is built');
  for(const hash of built){const bytes=fs.readFileSync(path.join(web,'dist','assets',hash+'.woff2'));assert(fontBytes.some(b=>b.equals(bytes)),'A built font holds its vendored bytes');assert.equal(require('crypto').createHash('sha256').update(bytes).digest('hex'),hash,'A built font is named by its bytes');}
  console.log('PASS: the way back to the consent page after Google, the email code or a session in hand, and no other next; the Google error on the card, the limit on codes, the address after a sign-in, the account binding and resume; one endpoint map with same-origin calls and batched positions, live routes and the self alias, live counts and empty imports, first-visit save under real ids, kept positions after a reload and after new items arrive, only new items saved, failed saves retried with the same positions, starter kinds as hubs and legend names, local identity node, typed errors, session check, Google start, Apple not available, email code start and verify with errors, logout, item text that never runs in the node panel or behind Show more, kind names as text with a working legend toggle, a 401 mid-session (reload, poll, node detail, position save) that ends on the sign-in card with the graph cleared, a read out at that moment (reload, poll) that draws nothing and brings no title back, a new session in the same page that keeps none of the old places and saves its own, unblocked and not ended by what the old session had out, search results, the selection pill and hidden kinds that end with the session, a retry after a failed save that keeps the drawn places, a rejected node read after the end that draws nothing, a late 401 that keeps the code step, a next session whose poll works, logout only when the server confirms it (network down too), offline text at each sign-in step, the look of starter and unknown kinds from the style tokens of the page, new leaves at their seed offset, no new home in a drawn hub disc or the identity disc, even after a hub grows, no local questions from store titles, no example identity in live mode, and a session check that fails with anything but a 401 and keeps the local state with the offline text, a session end that leaves no tour, address, code, confirmation, guide, host step, error or moved camera and nothing in the memory of the map, a legend click that only hides and shows, fixture mode that keeps the relayout and URL settings of the sim, a live first visit that no URL setting changes, and a built page with no fixture');
})().catch(e=>{console.error(e);process.exitCode=1;});
