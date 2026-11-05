// The real live.js, layout-keep.js, simulation.js and native-app.js in a browser-shaped sandbox,
// against a fake server. The graph checks use it. The check loop does not run this folder.
const fs=require('fs'),vm=require('vm'),path=require('path');
const web=path.join(__dirname,'..','..');
const read=f=>fs.readFileSync(path.join(web,f),'utf8');
const settle=async()=>{for(let i=0;i<60;i++)await new Promise(r=>setImmediate(r));};

function element(){
  const classes=new Set(),attrs={};
  return {getContext:()=>null,innerHTML:'',textContent:'',hidden:false,style:{setProperty(){},removeProperty(){}},dataset:{},children:[],offsetWidth:100,offsetHeight:100,
    classList:{add:(...k)=>k.forEach(x=>classes.add(x)),remove:(...k)=>k.forEach(x=>classes.delete(x)),toggle(){},contains:k=>classes.has(k)},
    setAttribute(k,v){attrs[k]=String(v);},getAttribute:k=>(k in attrs?attrs[k]:null),hasAttribute:k=>k in attrs,removeAttribute(k){delete attrs[k];},
    addEventListener(){},removeEventListener(){},querySelector:()=>null,querySelectorAll:()=>[],appendChild:c=>c,append(){},remove(){},contains:()=>false,closest:()=>null,focus(){},click(){},
    getBoundingClientRect:()=>({left:0,top:0,width:100,height:100,right:100,bottom:100})};
}
function force(){const f=function(){};['strength','distanceMax','radius','iterations','distance','x','y','id','links'].forEach(k=>{f[k]=()=>f;});return f;}
function fakeGraph(){
  let data={nodes:[],links:[]},zoom=1;const forces={link:force()},kept={};
  const keep=name=>function(fn){if(!arguments.length)return kept[name];kept[name]=fn;return proxy;};
  const g={nodeCanvasObject:keep('nodeCanvasObject'),nodePointerAreaPaint:keep('nodePointerAreaPaint'),linkColor:keep('linkColor'),linkCanvasObjectMode:keep('linkCanvasObjectMode'),onNodeHover:keep('onNodeHover'),onNodeClick:keep('onNodeClick'),
    graphData(d){if(!arguments.length)return data;data=d;return proxy;},d3Force(name,f){if(arguments.length<2)return forces[name];forces[name]=f;return proxy;},
    zoom(v){if(!arguments.length)return zoom;zoom=v;return proxy;},centerAt(){return arguments.length?proxy:{x:0,y:0};},graph2ScreenCoords:(x,y)=>({x,y}),screen2GraphCoords:(x,y)=>({x,y})};
  const proxy=new Proxy(g,{get:(t,k)=>(k in t?t[k]:k==='then'||typeof k!=='string'?undefined:function(){return proxy;})});
  return proxy;
}
// world: {nodes, edges, selfId, generatedAt}. The fake server reads it on each call, so a check can
// change the world between two reads. options: {reducedMotion}. clock.now is the page's time.
function openPage(world,store,options){
  const elements={},listeners={},clock={now:1e6},settings=options||{};
  const document={hidden:false,body:element(),documentElement:element(),createElement:element,getElementById:id=>elements[id]||(elements[id]=element()),querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){},removeEventListener(){}};
  const reply=(status,body)=>Promise.resolve({ok:status>=200&&status<300,status,text:()=>Promise.resolve(JSON.stringify(body))});
  const fetch=url=>{
    const p=new URL(url,'http://grove.test/').pathname;
    if(p==='/v1/views/session')return reply(200,{signed_in:true,self_item_id:world.selfId||null,account:'acct-graph',has_items:true});
    if(p==='/v1/views/graph'){
      const body={elements:{nodes:world.nodes,edges:world.edges},stats:{},time:{min:null,max:null},self_item_id:world.selfId||null};
      if(world.generatedAt)body.generated_at=world.generatedAt;
      return reply(200,body);
    }
    if(p==='/v1/views/positions')return reply(200,{placed:0,kept:0,unknown:0});
    return reply(200,{});
  };
  const box={URL,URLSearchParams,Math,JSON,Date,Promise,MutationObserver:class{observe(){}},console:{log(){},info(){},warn(){},error(){}},
    setTimeout:()=>0,clearTimeout(){},setInterval:()=>0,clearInterval(){},requestAnimationFrame:()=>0,cancelAnimationFrame(){},performance:{now:()=>clock.now},
    innerWidth:1440,innerHeight:900,devicePixelRatio:1,document,navigator:{},matchMedia:q=>({matches:!!settings.reducedMotion&&/reduced-motion/.test(q),addEventListener(){},removeEventListener(){}}),
    getComputedStyle:()=>({getPropertyValue:()=>''}),localStorage:{getItem:k=>(k in store?store[k]:null),setItem:(k,v)=>{store[k]=String(v);},removeItem:k=>{delete store[k];}},
    location:{href:'http://grove.test/',search:'',pathname:'/',assign(){}},fetch,ForceGraph:()=>()=>fakeGraph(),
    d3:{forceManyBody:force,forceCollide:force,forceX:force,forceY:force},CustomEvent:class{constructor(type,init){this.type=type;this.detail=init&&init.detail;}},
    addEventListener:(k,fn)=>{(listeners[k]||(listeners[k]=[])).push(fn);},dispatchEvent:e=>{(listeners[e.type]||[]).forEach(fn=>fn(e));return true;}};
  box.window=box;vm.createContext(box);
  for(const f of ['live.js','layout-keep.js','simulation.js','native-app.js'])vm.runInContext(read(f),box,{filename:f});
  return {box,elements,clock};
}
async function signedIn(world,store,options){
  const page=openPage(world,store||{},options);
  page.box.GroveSimulation.signIn('email','mira@example.com');
  page.box.GroveSimulation.setName('Mira Vale');
  page.box.GroveSimulation.completeOnboarding();
  await page.box.reloadGrove();await settle();
  return page;
}
// What one draw does: the first disc, each text, and whether it drew at all.
function drawn(draw,node,scale){
  const texts=[];let disc=null,alpha=null;
  const ctx=new Proxy({},{get(t,k){
    if(k==='arc')return (x,y,r)=>{if(!disc){disc={x,y,r};alpha=t.globalAlpha;}};
    if(k==='fillText'||k==='strokeText')return (s,x,y)=>{texts.push({s,x,y});};
    if(k==='measureText')return ()=>({width:10});
    return k in t?t[k]:()=>{};
  },set(t,k,v){t[k]=v;return true;}});
  draw(node,ctx,scale);
  return {texts,disc,alpha};
}
const stamp='2026-11-04T09:00:00Z';
function item(id,type,label,recorded){
  const at=recorded===undefined?stamp:recorded;
  return {data:{id,type,label,summary:'',status:'active',file_path:'',valid_at:at,invalid_at:null,created_at:at,updated_at:at,status_since:null,verified_at:at,version:1,properties:{},has_changes:false}};
}
function link(id,source,target,recorded){
  const at=recorded===undefined?stamp:recorded;
  return {data:{id,source,target,label:'relates to',verb:'relates to',valid_at:'2020-01-01T00:00:00Z',invalid_at:null,created_at:at}};
}
module.exports={read,settle,openPage,signedIn,drawn,item,link};
