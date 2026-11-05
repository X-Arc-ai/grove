// The look of the live map on a brain-shaped graph (the target is 5,416 real nodes in lobes of small
// dots). All data here is invented. A graph is deterministic: a small seeded generator makes it.
const assert=require('assert'),path=require('path');
const {organicView}=require(path.join(__dirname,'..','layout-keep.js'));

function brain(count,seed){
  let state=seed;const rand=()=>{state=(state*1664525+1013904223)>>>0;return state/4294967296;};
  const hubs=Math.max(6,Math.floor(count/130)),nodes=[{id:'grove-self',type:'person'}],edges=[],weights=[],members=[];
  for(let h=0;h<hubs;h++){nodes.push({id:'hub'+h,type:h%3?'plan':'organisation'});weights.push(1/Math.pow(h+1,.75));members.push([]);}
  const total=weights.reduce((a,b)=>a+b,0),pick=()=>{let r=rand()*total;for(let h=0;h<hubs;h++){r-=weights[h];if(r<=0)return h;}return hubs-1;};
  const kinds=['event','event','event','task','task','observation','observation','decision','person','assumption'];
  for(let i=hubs;i<count;i++){const h=pick(),id='n'+i;nodes.push({id,type:kinds[Math.floor(rand()*kinds.length)]});edges.push({source:id,target:'hub'+h});members[h].push(id);}
  for(let k=0;k<count*1.85;k++){const h=pick(),m=members[h];if(m.length<2)continue;const a=m[Math.floor(rand()*m.length)],r=rand();const b=r<.72?m[Math.floor(rand()*m.length)]:r<.88?'hub'+pick():nodes[1+Math.floor(rand()*(nodes.length-1))].id;if(a!==b)edges.push({source:a,target:b});}
  return {nodes,edges};
}
function nearest(points){
  const cell=40,grid=new Map(),key=(x,y)=>x+','+y;
  points.forEach(p=>{const k=key(Math.floor(p.x/cell),Math.floor(p.y/cell));(grid.get(k)||grid.set(k,[]).get(k)).push(p);});
  return points.map(p=>{let best=Infinity;const cx=Math.floor(p.x/cell),cy=Math.floor(p.y/cell);
    for(let i=-1;i<=1;i++)for(let j=-1;j<=1;j++)for(const q of grid.get(key(cx+i,cy+j))||[])if(q!==p)best=Math.min(best,Math.hypot(p.x-q.x,p.y-q.y));
    return best;}).sort((a,b)=>a-b);
}
const at=(list,share)=>list[Math.floor(list.length*share)];
function check(count,aspect){
  const g=brain(count,count+7),started=Date.now(),view=organicView(g.nodes,g.edges,aspect),spent=Date.now()-started,label=count+' nodes: ';
  assert.equal(Object.keys(view.positions).length,g.nodes.length,label+'every node has a place');
  for(const p of Object.values(view.positions))assert(Number.isFinite(p.x)&&Number.isFinite(p.y),label+'finite');
  assert(spent<6000,label+'the layout takes '+spent+' ms');
  const content=g.nodes.filter(n=>n.id!=='grove-self'),points=content.map(n=>view.positions[n.id]);
  // Small dots with room between them, never a fog of overlapping points.
  const gaps=nearest(points);
  assert(at(gaps,.5)>=12&&at(gaps,.5)<=30,label+'the median gap to the nearest dot is '+at(gaps,.5).toFixed(1));
  assert(at(gaps,.02)>=4,label+'dots do not pile up: the 2% gap is '+at(gaps,.02).toFixed(1));
  const smallOnes=content.filter(n=>!['plan','organisation','person'].includes(n.type)).map(n=>view.radii[n.id]);
  assert(Math.max(...smallOnes)<=18,label+'leaves stay small dots');
  // Lobes: several, none takes the map, and a lobe stays together around its hub.
  const lobes={};content.forEach(n=>{(lobes[view.primary[n.id]]||(lobes[view.primary[n.id]]=[])).push(n.id);});
  const sizes=Object.values(lobes).map(m=>m.length).sort((a,b)=>b-a);
  assert(sizes.length>=Math.min(5,count/60)&&sizes.length<=count/12,label+'lobes: '+sizes.length);
  assert(sizes[0]<=count*.3,label+'the largest lobe holds '+sizes[0]);
  // A few large hubs, not many of one size: the target shows about three that stand out.
  const hubSizes=g.nodes.filter(n=>['plan','organisation'].includes(n.type)).map(n=>view.radii[n.id]).sort((a,b)=>b-a);
  if(count>=5000)assert(hubSizes[0]>=hubSizes[Math.floor(hubSizes.length/2)]*1.6,label+'hub sizes stay uneven: '+hubSizes.slice(0,3).join(',')+' over a median of '+hubSizes[Math.floor(hubSizes.length/2)]);
  const xs=points.map(p=>p.x),ys=points.map(p=>p.y),width=Math.max(...xs)-Math.min(...xs),height=Math.max(...ys)-Math.min(...ys);
  for(const [hub,ids] of Object.entries(lobes)){
    if(ids.length<30)continue;
    const cx=ids.reduce((s,id)=>s+view.positions[id].x,0)/ids.length,cy=ids.reduce((s,id)=>s+view.positions[id].y,0)/ids.length;
    const reach=ids.map(id=>Math.hypot(view.positions[id].x-cx,view.positions[id].y-cy)).sort((a,b)=>a-b);
    assert(at(reach,.9)<=11*Math.sqrt(ids.length)*1.6+40,label+'lobe '+hub+' stays together: '+at(reach,.9).toFixed(0));
  }
  // Clumps: lobes gather into a few masses with open space between them, not one even field.
  const cells=new Map(),size=40;
  points.forEach(p=>{const k=Math.floor(p.x/size)+','+Math.floor(p.y/size);cells.set(k,(cells.get(k)||0)+1);});
  const filled=cells.size*size*size/(width*height);
  if(count>=5000)assert(filled<.62,label+'open space stays between clumps: '+filled.toFixed(2)+' of the map holds dots');
  // The mass takes the shape of the screen, with room for the owner at its middle.
  assert(width/height>aspect*.62&&width/height<aspect*1.6,label+'the map is '+(width/height).toFixed(2)+' wide for a screen of '+aspect);
  const owner=view.positions['grove-self'],room=Math.min(...points.map(p=>Math.hypot(p.x-owner.x,p.y-owner.y)));
  assert(room>=50,label+'the owner keeps clear room: '+room.toFixed(0));
  return {view,g,gaps:at(gaps,.5),lobes:sizes.length,width,height,spent};
}
const results=[[400,1.6],[1000,1.6],[5000,1.6],[5000,.46]].map(([n,a])=>Object.assign({n,a},check(n,a)));
// The same graph gives the same map in any input order.
const small=brain(400,11),one=organicView(small.nodes,small.edges,1.6),two=organicView([...small.nodes].reverse(),[...small.edges].reverse(),1.6);
assert.deepEqual(two,one,'Input order does not change the map');
// A plain star, an empty world, a world of unlinked items, and many small groups all draw.
const star={nodes:[{id:'hub',type:'plan'}],edges:[]};for(let i=0;i<1500;i++){star.nodes.push({id:'s'+i,type:'task'});star.edges.push({source:'s'+i,target:'hub'});}
assert.equal(new Set(Object.values(organicView(star.nodes,star.edges).primary)).size,1,'A plain star stays one spiral');
assert.deepEqual(organicView([],[]).positions,{});
const lone=Array.from({length:300},(_,i)=>({id:'l'+i,type:'task'}));
assert.equal(Object.keys(organicView(lone,[]).positions).length,300,'Unlinked items each have a place');
const pairs={nodes:[],edges:[]};for(let i=0;i<2000;i++){pairs.nodes.push({id:'a'+i,type:'task'},{id:'b'+i,type:'task'});pairs.edges.push({source:'a'+i,target:'b'+i});}
const started=Date.now();organicView(pairs.nodes,pairs.edges);
assert(Date.now()-started<6000,'Thousands of small groups stay fast');
console.log('graph look: '+results.map(r=>r.n+' nodes at '+r.a+': '+r.lobes+' lobes, median gap '+r.gaps.toFixed(1)+', '+Math.round(r.width)+'x'+Math.round(r.height)+', '+r.spent+' ms').join('; '));
