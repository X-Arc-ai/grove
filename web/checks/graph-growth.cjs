// Growth (ruling R4): a later read adds to the map in small steps and moves nothing that is drawn.
// The first read of a session is the map itself, so it draws at once.
const assert=require('assert');
const {signedIn,settle,drawn,item,link}=require('./lib/graph-sandbox.cjs');

const READ='2026-11-04T12:00:00Z';
function world(leaves){
  const nodes=[item('self','person','Mira Vale'),item('hub','organisation','Northwind')],edges=[link('own','self','hub')];
  for(let i=0;i<leaves;i++){nodes.push(item('n'+String(i).padStart(4,'0'),'task','Task '+i));edges.push(link('e'+i,'n'+String(i).padStart(4,'0'),'hub'));}
  return {selfId:'self',generatedAt:READ,nodes,edges};
}
function grow(data,count,tag){
  data.nodes.push(item(tag+'-hub','plan','New plan'));data.edges.push(link(tag+'-link','hub',tag+'-hub'));
  for(let i=0;i<count;i++){data.nodes.push(item(tag+i,'task','Added '+i));data.edges.push(link(tag+'-e'+i,tag+i,tag+'-hub'));}
}
(async()=>{
  const data=world(60);
  const {box,clock}=await signedIn(data,{});
  const graph=box.GroveView.graph(),draw=graph.nodeCanvasObject(),paint=graph.nodePointerAreaPaint(),color=graph.linkColor();
  for(const n of box.GroveView.nodes())assert(!(n.__born>clock.now),'The first read of a session draws at once: '+n.id);
  const before=Object.fromEntries(box.GroveView.nodes().map(n=>[n.id,{x:n.x,y:n.y}]));

  grow(data,300,'g');
  clock.now+=5000;
  await box.reloadGrove();await settle();
  const map=box.GroveView.nodes(),byId=Object.fromEntries(map.map(n=>[n.id,n]));
  assert.equal(map.length,before&&Object.keys(before).length+301,'All new nodes join the map');
  for(const id of Object.keys(before))assert.deepEqual({x:byId[id].x,y:byId[id].y},before[id],'A drawn node stays where it is: '+id);

  const fresh=map.filter(n=>!before[n.id]).sort((a,b)=>a.__born-b.__born);
  assert.equal(fresh[0].id,'g-hub','The hub of a new group arrives first');
  const steps=fresh.slice(1).map((n,i)=>n.__born-fresh[i].__born);
  assert(steps.every(ms=>ms>0&&ms<=140),'Nodes arrive one by one, at the pace of the sim');
  assert(fresh[fresh.length-1].__born-clock.now<=54500,'One arrival ends inside 54 seconds');
  assert(fresh[fresh.length-1].__born-clock.now>=30000,'300 nodes do not arrive at once');

  const late=fresh[fresh.length-1];
  assert.equal(drawn(draw,late,1).disc,null,'A node on its way draws nothing');
  let painted=false;paint(late,'#123',new Proxy({},{get:(t,k)=>k==='arc'?()=>{painted=true;}:k in t?t[k]:()=>{},set(t,k,v){t[k]=v;return true;}}));
  assert(!painted,'A node on its way takes no pointer');
  assert.equal(color({source:late,target:byId.hub}),'transparent','Its links wait for it');
  clock.now=late.__born+800;
  assert(drawn(draw,late,1).disc,'A node draws once it has arrived');
  assert.notEqual(color({source:late,target:byId.hub}),'transparent','Its links draw with it');

  // A read during an arrival keeps the time of each node that is still on its way.
  clock.now=fresh[10].__born-1;const born=late.__born;
  await box.reloadGrove();await settle();
  assert.equal(box.GroveView.nodes().find(n=>n.id===late.id).__born,born,'A later read does not restart an arrival');

  // Reduced motion: no paced arrival.
  const calmData=world(20);
  const calm=await signedIn(calmData,{},{reducedMotion:true});
  grow(calmData,40,'c');await calm.box.reloadGrove();await settle();
  for(const n of calm.box.GroveView.nodes())assert(!(n.__born>calm.clock.now),'Reduced motion shows new nodes at once');

  // A large arrival on a small map stays fast: the page must not freeze before the paced arrival.
  const bigData=world(1);
  const big=await signedIn(bigData,{});
  for(let h=0;h<12;h++)grow(bigData,125,'b'+h+'-');
  bigData.nodes.slice(4).forEach((row,i)=>{if(i%3===0)bigData.edges.push(link('cross'+i,row.data.id,bigData.nodes[4+(i*7)%(bigData.nodes.length-4)].data.id));});
  const placed=Date.now();
  await big.box.reloadGrove();await settle();
  const spent=Date.now()-placed;
  assert.equal(big.box.GroveView.nodes().length,bigData.nodes.length,'All nodes of a large arrival join the map');
  assert(spent<8000,'A read of 1,500 new nodes takes '+spent+' ms');
  const spots=big.box.GroveView.nodes().map(n=>n.x+','+n.y);
  assert.equal(new Set(spots).size,spots.length,'No two nodes of a large arrival share a spot');

  // A new session on the same page draws its first read at once.
  box.GroveView.clear();
  await box.reloadGrove();await settle();
  for(const n of box.GroveView.nodes())assert(!(n.__born>clock.now),'A new session draws its first read at once: '+n.id);
  console.log('graph growth: first read at once, a later read moves nothing, new nodes arrive hub first at the sim pace, reduced motion and a new session draw at once');
})().catch(e=>{console.error(e);process.exitCode=1;});
