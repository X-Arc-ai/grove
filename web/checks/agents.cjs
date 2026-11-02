// Exercise the real agent schedule, region geometry and camera hooks without a browser.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
let now=0,nativeDraws=0,fitCalls=0,camera={x:0,y:0},zoom=1;
const geometry=[];
function finite(args){for(const n of args)if(typeof n==='number')assert(Number.isFinite(n),'Non-finite canvas coordinate');}
class Path {moveTo(...a){finite(a);geometry.push(a);}lineTo(...a){finite(a);geometry.push(a);}closePath(){}}
function context(){const ctx={canvas:{width:1200,height:800},getTransform:()=>({a:1,b:0,c:0,d:1,e:0,f:0}),measureText:t=>({width:t.length*7})};for(const k of ['save','restore','setTransform','clearRect','beginPath','moveTo','lineTo','arc','fill','stroke','closePath','clip','translate','scale','drawImage','setLineDash','strokeText','fillText'])ctx[k]=(...a)=>finite(a);return ctx;}
const classes={toggle(){},add(){},remove(){},contains(){return false;}};
const element=()=>({hidden:true,classList:classes,addEventListener(){},getAttribute(){return null;},querySelector(){return null;},querySelectorAll(){return [];},appendChild(){},setAttribute(){},style:{}});
const doc={body:element(),documentElement:{...element(),getAttribute:()=> 'light'},createElement:tag=>tag==='canvas'?{width:0,height:0,getContext:()=>context()}:element(),getElementById:()=>element(),querySelector:()=>null,addEventListener(){}};
const nodes=Array.from({length:75},(_,i)=>({id:i?'n'+i:'grove-self',title:i?'Memory '+i:'Anas',type:i%6===0?'person':'observation',x:(i%15)*24-170,y:Math.floor(i/15)*30-70,_primaryHubId:Math.floor(i/25)}));
const links=nodes.slice(1).map((n,i)=>({source:nodes[i],target:n}));
const methods={nodeCanvasObject:()=>{nativeDraws++;},nodePointerAreaPaint(){},linkColor:()=> '#aaa',linkCanvasObjectMode:()=> 'after',onNodeHover(){},onNodeClick(){},onLinkClick(){},onBackgroundClick(){}};
const graph={graphData:()=>({nodes,links}),centerAt:(x,y)=>{if(x===undefined)return camera;finite([x,y]);camera={x,y};return graph;},zoom:v=>{if(v===undefined)return zoom;finite([v]);zoom=v;return graph;},graph2ScreenCoords:(x,y)=>({x,y})};
for(const k of [...Object.keys(methods),'onRenderFramePre','onRenderFramePost'])graph[k]=function(fn){if(!arguments.length)return methods[k];methods[k]=fn;return graph;};
const sandbox={console,URLSearchParams,Float64Array,Float32Array,Int32Array,Path2D:Path,Math,Promise,performance:{now:()=>now},setInterval(){},setTimeout(){},clearTimeout(){},getComputedStyle:()=>({getPropertyValue:()=>''}),MutationObserver:class{observe(){}},document:doc,localStorage:{getItem:()=>null,setItem(){}},innerWidth:1200,location:{search:''}};
sandbox.window=sandbox;sandbox.addEventListener=()=>{};sandbox.GroveSimulation={getState:()=>({onboardingComplete:true})};sandbox.GroveView={nodes:()=>nodes,graph:()=>graph,close(){},fit(){fitCalls++;}};sandbox.__brainViz={Graph:graph,hopDist:()=>null};
let source=fs.readFileSync(path.join(__dirname,'../agents.js'),'utf8');
source=source.replace('  (function buildSwitch() {','  buildBar=function(){}; buildScore=function(){}; buildLegend=function(){};\n  (function buildSwitch() {');
source=source.replace(/\}\)\(\);\s*$/,'window.__agentTest={sample:sampleAgentData,load:loadData,setOn,follow:followAgent,stop:stopFollowing,spot:spotOf,strength:zoneStrength,regions:()=>zones};})();');
vm.createContext(sandbox);vm.runInContext(source,sandbox);
const test=sandbox.__agentTest,data=test.sample();test.load(data);test.setOn(true);now=5000;
const ctx=context();function frame(){methods.onRenderFramePre(ctx,1);methods.onRenderFramePost(ctx,1);}
frame();assert(sandbox.__hood.at().ready);assert(test.regions().length>1);assert(geometry.length>10);
for(const n of nodes)methods.nodeCanvasObject(n,ctx,1);assert.equal(nativeDraws,nodes.length,'Activity view must retain native node rendering');
const turns=sandbox.__hood.turns(),total=sandbox.__hood.at().total;
const cleaner=turns.find(t=>t.p==='L');
sandbox.__hood.seek((cleaner.w0+1)/total);frame();const before=test.strength(cleaner.section);
sandbox.__hood.seek((cleaner.w1-1)/total);frame();assert(test.strength(cleaner.section)>before,'The tended region must become fresher');
for(const agent of ['U','L','O']){
 test.follow(agent);assert.equal(sandbox.__hood.following(),agent);
 for(const turn of turns){sandbox.__hood.seek(((turn.t0+turn.t1)/2)/total);now+=20;frame();}
 assert(Number.isFinite(camera.x)&&Number.isFinite(camera.y));
 for(let i=0;i<100;i++){now+=30;frame();}
 const target=test.spot(agent);assert(Math.abs(camera.x-target.x)<.1&&Math.abs(camera.y-target.y)<.1,'Followed agent must be at the viewport centre');
}
test.stop(true);assert.equal(sandbox.__hood.following(),null);assert(fitCalls>0);
const still=camera;now+=20;frame();assert.deepEqual(camera,still,'Camera must stop following');
test.setOn(false);const time=sandbox.__hood.at().t;now+=50;frame();assert(sandbox.__hood.at().t>time,'Work must continue when its view is hidden');const progressed=sandbox.__hood.at().t;test.setOn(true);assert.equal(sandbox.__hood.at().t,progressed,'Showing work must not restart it');
console.log('PASS: regional geometry, native node colors, progressive area freshness, all agent follow paths, overview and hidden activity continuity');
