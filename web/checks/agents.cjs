// Exercise the real agent schedule, region geometry and camera hooks without a browser.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
let now=0,nativeDraws=0,fitCalls=0,camera={x:0,y:0},zoom=1;
const geometry=[];
function finite(args){for(const n of args)if(typeof n==='number')assert(Number.isFinite(n),'Non-finite canvas coordinate');}
class Path {moveTo(...a){finite(a);geometry.push(a);}lineTo(...a){finite(a);geometry.push(a);}closePath(){}}
function context(){const ctx={canvas:{width:1200,height:800},getTransform:()=>({a:1,b:0,c:0,d:1,e:0,f:0}),measureText:t=>({width:t.length*7})};for(const k of ['save','restore','setTransform','clearRect','beginPath','moveTo','lineTo','arc','fill','stroke','closePath','clip','translate','scale','drawImage','setLineDash','strokeText','fillText'])ctx[k]=(...a)=>finite(a);return ctx;}
// The playback bar is gone: the real buildBar runs below, and the moment any element takes one of
// the bar's classes (as its class or in its markup), this check fails with that name.
const BAR=/\bhood-(bar|play|track|seg|speed)\b/;
function noBar(value){assert(!BAR.test(String(value)),'The playback bar must not be built: '+String(value).slice(0,80));}
const classes={toggle(k){noBar(k);},add(...k){k.forEach(noBar);},remove(){},contains(){return false;}};
const element=()=>({hidden:true,classList:classes,addEventListener(){},getAttribute(){return null;},querySelector(){return null;},querySelectorAll(){return [];},appendChild(){},setAttribute(){},style:{}});
function watched(el){
  const kept={className:'',innerHTML:''},set=el.setAttribute;
  for(const k of Object.keys(kept))Object.defineProperty(el,k,{enumerable:true,get:()=>kept[k],set(v){noBar(v);kept[k]=v;}});
  el.setAttribute=(k,v)=>{if(k==='class')noBar(v);set.call(el,k,v);};
  return el;
}
// The node panel the workers line goes into, and the page's keydown listeners.
const panelBody={...element(),appended:[],appendChild(c){this.appended.push(c);return c;}},panel={...element(),classList:{...classes,contains:k=>k==='open'}},keys=[];
const doc={body:element(),documentElement:{...element(),getAttribute:()=> 'light'},createElement:tag=>tag==='canvas'?{width:0,height:0,getContext:()=>context()}:watched(element()),getElementById:id=>id==='detail-body'?panelBody:id==='detail'?panel:element(),querySelector:()=>null,addEventListener(type,fn){if(type==='keydown')keys.push(fn);}};
const nodes=Array.from({length:75},(_,i)=>({id:i?'n'+i:'grove-self',title:i?'Memory '+i:'Anas',type:i%6===0?'person':'observation',x:(i%15)*24-170,y:Math.floor(i/15)*30-70,_primaryHubId:Math.floor(i/25)}));
const links=nodes.slice(1).map((n,i)=>({source:nodes[i],target:n}));
const methods={nodeCanvasObject:()=>{nativeDraws++;},nodePointerAreaPaint(){},linkColor:()=> '#aaa',linkCanvasObjectMode:()=> 'after',onNodeHover(){},onNodeClick(){},onLinkClick(){},onBackgroundClick(){}};
const graph={graphData:()=>({nodes,links}),centerAt:(x,y)=>{if(x===undefined)return camera;finite([x,y]);camera={x,y};return graph;},zoom:v=>{if(v===undefined)return zoom;finite([v]);zoom=v;return graph;},graph2ScreenCoords:(x,y)=>({x,y})};
for(const k of [...Object.keys(methods),'onRenderFramePre','onRenderFramePost'])graph[k]=function(fn){if(!arguments.length)return methods[k];methods[k]=fn;return graph;};
let hop=null;
// One load of agents.js; search is the page's query string, fakeScore the workers panel, if any.
function boot(search,fakeScore){
  const heard={};
  const box={console,URLSearchParams,Float64Array,Float32Array,Int32Array,Path2D:Path,Math,Promise,CustomEvent:class{constructor(type){this.type=type;}},dispatchEvent(e){(heard[e.type]||[]).forEach(fn=>fn(e));return true;},performance:{now:()=>now},setInterval(){},setTimeout(){},clearTimeout(){},getComputedStyle:()=>({getPropertyValue:()=>''}),MutationObserver:class{observe(){}},document:doc,localStorage:{getItem:()=>null,setItem(){}},innerWidth:1200,location:{search}};
  box.window=box;box.addEventListener=(type,fn)=>{(heard[type]||(heard[type]=[])).push(fn);};box.GroveSimulation={getState:()=>({onboardingComplete:true})};box.GroveView={nodes:()=>graph.graphData().nodes,graph:()=>graph,close(){},fit(){fitCalls++;}};box.__brainViz={Graph:graph,hopDist:()=>hop};box.__fakeScore=fakeScore||null;
  let source=fs.readFileSync(path.join(__dirname,'../agents.js'),'utf8');
  source=source.replace('  (function buildSwitch() {','  buildScore=function(){scoreEl=window.__fakeScore;}; buildLegend=function(){};\n  (function buildSwitch() {');
  // The check's own handle reads and sets what the page gives no way to reach: the time of the loop, among others.
  source=source.replace(/\}\)\(\);\s*$/,'window.__agentTest={sample:sampleAgentData,load:loadData,setOn,follow:followAgent,stop:stopFollowing,spot:spotOf,strength:zoneStrength,regions:()=>zones,note:renderNote,bar:()=>barEl,seek(f){simT=f*total;},play(v){playing=v;},nodeR,draw:drawThing,kept:()=>JSON.stringify({ids:D&&D.ids,idIndex,fgById,nbrsOf,personas,noteHTML,turns,writes,marks,simT}),state:()=>({D,idIndex,secOf,since0,cleanedIn,nextBeyond,zones,zoneKey,turns,total,cleanTurns,roundEnd,writes,marks,simT,ready,following,followPoint,personas,noteHTML,fgById,fgNodesRef,nbrsOf,turn:F.turn,cycle:F.cycle,focusId:F.focusId,hoverId:F.hoverId,hopRef:F.hopRef,scoreVals,chromeState})};})();');
  vm.createContext(box);vm.runInContext(fs.readFileSync(path.join(__dirname,'../live.js'),'utf8'),box);vm.runInContext(source,box);return box;
}
const sandbox=boot('');
const test=sandbox.__agentTest,data=test.sample();test.load(data);test.setOn(true);now=5000;
const ctx=context();function frame(){methods.onRenderFramePre(ctx,1);methods.onRenderFramePost(ctx,1);}
frame();assert(sandbox.__hood.at().ready);assert(test.regions().length>1);assert(geometry.length>10);
assert.equal(test.bar(),null,'With the workers toggle on, no playback bar is built');
for(const n of nodes)methods.nodeCanvasObject(n,ctx,1);assert.equal(nativeDraws,nodes.length,'Activity view must retain native node rendering');
const turns=sandbox.__hood.turns(),total=sandbox.__hood.at().total;
const cleaner=turns.find(t=>t.p==='L');
test.seek((cleaner.w0+1)/total);frame();const before=test.strength(cleaner.section);
test.seek((cleaner.w1-1)/total);frame();assert(test.strength(cleaner.section)>before,'The tended region must become fresher');
for(const agent of ['U','L','O']){
 test.follow(agent);assert.equal(sandbox.__hood.following(),agent);
 for(const turn of turns){test.seek(((turn.t0+turn.t1)/2)/total);now+=20;frame();}
 assert(Number.isFinite(camera.x)&&Number.isFinite(camera.y));
 for(let i=0;i<100;i++){now+=30;frame();}
 const target=test.spot(agent);assert(Math.abs(camera.x-target.x)<.1&&Math.abs(camera.y-target.y)<.1,'Followed agent must be at the viewport centre');
}
test.stop(true);assert.equal(sandbox.__hood.following(),null);assert(fitCalls>0);
const still=camera;now+=20;frame();assert.deepEqual(camera,still,'Camera must stop following');
test.setOn(false);const time=sandbox.__hood.at().t;now+=50;frame();assert(sandbox.__hood.at().t>time,'Work must continue when its view is hidden');const progressed=sandbox.__hood.at().t;test.setOn(true);assert.equal(sandbox.__hood.at().t,progressed,'Showing work must not restart it');

// The workers line on the node panel: an item title in it stays text.
nodes[1].title='<img src=x onerror=alert(1)> "Lantern" &';
const linking=turns.find(t=>t.p==='O'&&t.writes.some(w=>w.k==='link'&&w.n.includes(0)&&w.n.includes(1)));
assert(linking,'A connector turn links the first two nodes');
const linkWrite=linking.writes.find(w=>w.k==='link'&&w.n.includes(0)&&w.n.includes(1));
hop={'grove-self':0};test.play(false);test.seek((linkWrite.at+1)/total);now+=20;frame();
assert.equal(sandbox.__hood.at().focus,'grove-self');assert.equal(sandbox.__hood.at().turn,'O');
test.note();const note=panelBody.appended[panelBody.appended.length-1];
assert(note&&note.innerHTML.includes('Last refreshed'),'The workers line is on the panel');
assert(note.innerHTML.includes('to &lt;img src=x onerror=alert(1)&gt; &quot;Lantern&quot; &amp;</div>'),'The linked title shows as text');
assert(!/<img|onerror=alert\(1\)>/.test(note.innerHTML.replace(/&lt;img src=x onerror=alert\(1\)&gt;/g,'')),'No markup from a title reaches the panel');
hop=null;now+=20;frame();test.play(true);

// The playback bar is gone, and its keys with it: Space, ArrowLeft and ArrowRight change nothing.
assert(keys.length>=1,'The page listens for keys');
const press=key=>keys.forEach(fn=>fn({key,preventDefault(){},target:{tagName:'CANVAS'}}));
test.seek((turns[2].t0+500)/total);
for(const key of [' ','ArrowRight','ArrowLeft','ArrowRight']){
  const at=sandbox.__hood.at().t;press(key);
  assert.equal(sandbox.__hood.at().t,at,key+' must not move the loop');
  assert.equal(sandbox.GroveCare.summary().running,true,key+' must not pause the loop');
}
test.follow('U');assert.equal(sandbox.__hood.following(),'U');press('Escape');assert.equal(sandbox.__hood.following(),null,'Escape still stops following');

// Nor do the bar's URL parameters: ?pause=1, ?speed=4 and ?t=0.9 change nothing. The loop runs at
// its one speed from its start, and the workers panel counts what it counts with no parameter.
const SEEN=[];
for(const search of ['','?pause=1&speed=4','?t=0.9','?pause=1&speed=4&t=0.9']){
  keys.length=0;const page=boot(search),t2=page.__agentTest;t2.load(t2.sample());t2.setOn(true);now+=5000;
  methods.onRenderFramePre(ctx,1);const start=page.__hood.at().t;now+=50;methods.onRenderFramePre(ctx,1);
  assert.equal(page.GroveCare.summary().running,true,'The loop runs with '+(search||'no parameters'));
  assert.equal(t2.bar(),null,'No playback bar with '+(search||'no parameters'));
  const {updated,connections,tended}=page.GroveCare.summary();
  SEEN.push({search,start,step:page.__hood.at().t-start,updated,connections,tended});
}
assert(SEEN[0].step>0,'The loop runs with no parameters');
for(const seen of SEEN)assert.deepEqual({...seen,search:''},{...SEEN[0],search:''},'A URL parameter changes the loop: '+seen.search);
// The page itself has no handle that moves the loop. ?hood=0|1 stays: it forces the view off or on.
assert.equal(sandbox.__hood.seek,undefined,'The page has no seek handle');assert.equal(sandbox.__hood.play,undefined,'The page has no play handle');
for(const [search,enabled] of [['?hood=1',true],['?hood=0',false]]){keys.length=0;assert.equal(boot(search).GroveCare.summary().enabled,enabled,search+' forces the view');}

// The starter kinds draw here as the sim type they alias (web/live.js): the size the agents' rings
// and spots read, and the disc, the ring weight and, for a hub, the name inside the disc.
function drawn(page,type,scale){
  const calls=[],draw=context();
  draw.arc=(...a)=>calls.push(['arc',...a]);draw.arcTo=()=>{};draw.fillText=(...a)=>calls.push(['text',...a]);
  Object.defineProperty(draw,'lineWidth',{set(v){calls.push(['ring',v]);}});
  page.__agentTest.draw({id:'kind-probe',title:'Wrenfield Cooperative',type,x:0,y:0},draw,scale);
  // A glyph belongs to the sim type's own name: a kind borrows the disc, the ring and the hub name.
  return {disc:calls.find(c=>c[0]==='arc'),ring:calls.find(c=>c[0]==='ring'),text:calls.filter(c=>c[0]==='text')};
}
{
  keys.length=0;hop=null;const page=boot(''),t3=page.__agentTest;t3.load(t3.sample());t3.setOn(true);
  const STYLE=page.GroveLive.KIND_STYLE;
  assert(Object.keys(STYLE).includes('organisation')&&Object.keys(STYLE).includes('plan')&&Object.keys(STYLE).length===8,'The starter kinds are known');
  for(const [kind,style] of Object.entries(STYLE)){
    assert.equal(t3.nodeR({type:kind}),t3.nodeR({type:style}),kind+' takes the size of '+style);
    assert.deepEqual(drawn(page,kind,1),drawn(page,style,1),kind+' draws as '+style);
  }
  for(const hub of ['organisation','plan']){
    assert(drawn(page,hub,1).text.length>0,hub+' draws its name in the disc, in hub style');
    assert(t3.nodeR({type:hub})>=38,hub+' takes a hub size');
  }
  assert.equal(drawn(page,'place',1).text.length,0,'A kind that is no hub draws no name in its disc');
}

// The session ends: the workers panel keeps no count, the loop no item, and the next session starts
// its own loop from the start. On the map this comes with an empty graph (native-app clearGraph).
{
  const painted={},row=k=>{if(!painted[k]){const r={textContent:'',ticks:0,classList:{add(){r.ticks++;},remove(){}},get offsetWidth(){return 0;}};painted[k]=r;}return painted[k];};
  const stop={hidden:true};
  const fakeScore={querySelector:sel=>{const m=sel.match(/data-k="(\w+)"/);return m?row(m[1]):sel==='.hood-stop'?stop:null;},querySelectorAll:()=>[]};
  const box=boot('',fakeScore),t4=box.__agentTest;keys.length=0;hop=null;
  // A turn past the last round, as a longer loop of the sim has: its slot goes with the session too.
  const data4=t4.sample();data4.turns.push({p:'U',round:data4.meta.rounds,real:false,steps:[]});
  t4.load(data4);t4.setOn(true);
  now+=5000;frame();t4.seek(.9);now+=200;frame();
  assert(box.GroveCare.summary().updated>0,'The panel counts before the session ends');
  assert(painted.Uupdated&&Number(painted.Uupdated.textContent)>0,'The panel shows the count');
  t4.follow('U');assert.equal(box.__hood.following(),'U');assert.equal(stop.hidden,false,'The stop button shows');
  hop={n3:0,n4:1,n5:2};now+=200;frame();hop=null;   // a node is open, as the node panel leaves it
  methods.onNodeHover(nodes[6]);
  const nodesNow=graph.graphData;graph.graphData=()=>({nodes:[],links:[]});
  // Every piece of memory the end of a session empties, each one full before it, or this proves nothing.
  const EMPTIED={D:null,idIndex:{},secOf:null,since0:[],cleanedIn:[],nextBeyond:{},zones:[],zoneKey:'',turns:[],total:1,cleanTurns:[],roundEnd:[],writes:[],marks:{},simT:0,ready:false,following:null,followPoint:null,personas:{},noteHTML:'',fgById:{},fgNodesRef:null,nbrsOf:{},turn:null,cycle:0,focusId:null,hoverId:null,hopRef:null,scoreVals:{Uadded:'0',Uupdated:'0',Ulinks:'0',Olinks:'0',Lsections:'0',Lclean:'0.0%'},chromeState:''};
  const plain=value=>{const seen=new WeakSet();return JSON.parse(JSON.stringify(value,(k,v)=>{if(v&&typeof v==='object'){if(seen.has(v))return 'seen';seen.add(v);}return v;}));};
  const full=plain(t4.state());
  assert.deepEqual(Object.keys(EMPTIED).filter(k=>JSON.stringify(full[k])===JSON.stringify(EMPTIED[k])),['hoverId'],'Every piece is full before the end, but for the one no line ever sets');
  const note={removed:false,remove(){this.removed=true;}},lookup=panelBody.querySelector;panelBody.querySelector=()=>note;
  for(const r of Object.values(painted))r.ticks=0;
  box.dispatchEvent(new box.CustomEvent('grove:graph-cleared'));
  panelBody.querySelector=lookup;
  assert.deepEqual(plain(t4.state()),EMPTIED,'Nothing of the ended session stays in the memory of the loop');
  assert.equal(stop.hidden,true,'The stop button goes with the follow');
  assert.equal(note.removed,true,'The workers line leaves the node panel');
  assert.deepEqual(Object.values(painted).map(r=>r.ticks).filter(Boolean),[],'The counts go back to zero with no tick');
  // At once, with no frame to wait for (a hidden tab draws none), and still after the next frame.
  for(const when of ['at once','after a frame']){
    for(const [k,el] of Object.entries(painted))assert(/^0(\.0%)?$/.test(el.textContent),'The panel shows no count of the ended session, '+when+': '+k+'='+el.textContent);
    const after=box.GroveCare.summary();
    assert.deepEqual([after.updated,after.connections,after.tended,after.areas,after.working,after.running],[0,0,0,0,null,false],'No count stays, '+when);
    now+=50;frame();
  }
  assert.equal(box.__hood.following(),null,'The follow ends');assert.equal(box.__hood.at().t,0,'The loop is back at its start');
  assert.deepEqual([...box.__hood.turns()],[],'No turn stays');
  assert(!/Memory \d|"n\d|Lantern/.test(t4.kept()),'No id or title of the ended session stays in the loop: '+t4.kept().slice(0,120));
  // The next person: their first graph starts a loop of their own, from its start, not from where the first one was.
  graph.graphData=nodesNow;now+=1;
  box.dispatchEvent(new box.CustomEvent('grove:graph-rendered'));
  assert.equal(box.GroveCare.summary().areas>0,true,'The next session starts its own loop');assert.equal(box.__hood.at().t,0,'It starts from its start');
  now+=5000;frame();assert.equal(box.GroveCare.summary().running,true,'The loop of the next session runs');
}
console.log('PASS: regional geometry, native node colors, progressive area freshness, all agent follow paths, overview and hidden activity continuity, item titles as text in the workers line, no playback bar, keys, URL settings or seek handle, ?hood forces the view, starter kinds in the style of their sim type, and a session end that leaves no count or item in the workers loop');
