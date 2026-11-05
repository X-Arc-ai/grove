// The +X counts and their highlight read real record times. A count survives a later read and a page
// reload, a row with no record time counts for nothing, and a click highlights exactly those rows.
const assert=require('assert');
const {signedIn,openPage,settle,drawn,item,link}=require('./lib/graph-sandbox.cjs');

const READ='2026-11-04T12:00:00Z',OLD='2026-10-20T09:00:00Z',NEW='2026-11-04T08:00:00Z',EDGE_OF_DAY='2026-11-03T12:00:00Z';
const world=()=>({selfId:'self',generatedAt:READ,
  nodes:[item('self','person','Mira Vale',NEW),item('hub','organisation','Northwind',OLD),item('old','task','Old task',OLD),item('edge-of-day','task','Exactly one day old',EDGE_OF_DAY),
    item('new-a','task','New task',NEW),item('new-b','observation','New note',NEW),item('new-c','event','New event',NEW),item('no-time','task','No record time',null)],
  edges:[link('e-old','old','hub',OLD),link('e-new-a','new-a','hub',NEW),link('e-new-b','new-b','hub',NEW),link('e-no-time','no-time','hub',null),link('e-self','self','hub',NEW)]});
const ids=rows=>rows.map(r=>r.id).sort();

(async()=>{
  const data=world(),store={};
  const first=await signedIn(data,store);
  let s=first.box.GroveSimulation.getState();
  assert.equal(s.nodeDelta,3,'The first read of a page shows what the store recorded in the last day');
  assert.equal(s.edgeDelta,3,'Links count from their own record time, not from their event time');
  const stats=first.elements.stats.innerHTML;
  assert(stats.includes('(+3)'),'The header shows the node count');
  assert(!/graph check|baseline/i.test(stats),'The header holds no baseline words: '+stats);

  // Rows: the owner never counts, the row exactly one day old is out, a row with no time is out.
  const changes=first.box.GroveSimulation.getChanges();
  assert.deepEqual(ids(changes.new_nodes),['new-a','new-b','new-c']);
  assert.deepEqual(ids(changes.new_edges),['e-new-a','e-new-b','e-self']);

  // A second read with no change keeps the count: the 3-minute poll must not clear it.
  await first.box.reloadGrove();await settle();
  s=first.box.GroveSimulation.getState();
  assert.equal(s.nodeDelta,3,'A later read keeps the count');assert.equal(s.edgeDelta,3);

  // A page reload keeps the count: the source is the store, not page memory.
  const again=await signedIn(data,{});
  s=again.box.GroveSimulation.getState();
  assert.equal(s.nodeDelta,3,'A page reload keeps the count');assert.equal(s.edgeDelta,3);

  // The day moves on: the same rows age out of the count, with no action from the person.
  const later=Object.assign(world(),{generatedAt:'2026-11-06T12:00:00Z'});
  const aged=await signedIn(later,{});
  assert.equal(aged.box.GroveSimulation.getState().nodeDelta,0,'A row older than the day leaves the count');
  assert(!aged.elements.stats.innerHTML.includes('(+'),'No count shows when nothing is new');

  // No read time: no count, never an invented number.
  const blind=await signedIn(Object.assign(world(),{generatedAt:null}),{});
  s=blind.box.GroveSimulation.getState();
  assert.equal(s.nodeDelta,0,'A read with no read time shows no count');assert.equal(s.edgeDelta,0);

  // A click on +X highlights exactly those nodes, keeps the map in view and names the date.
  const {box,elements,clock}=await signedIn(world(),{});
  clock.now+=2000;   // past the entrance of the first draw
  const graph=box.GroveView.graph(),draw=graph.nodeCanvasObject(),map=box.GroveView.nodes(),byId=Object.fromEntries(map.map(n=>[n.id,n]));
  const restR=drawn(draw,byId['new-a'],1).disc.r;
  graph.zoom(.3);box.GroveView.showChanges('nodes');await settle();
  assert(elements['selection-pill'].innerHTML.includes('3 new nodes since 2026-11-03'),'The chip names the count and the date: '+elements['selection-pill'].innerHTML);
  assert(drawn(draw,byId['new-a'],1).disc.r>=22&&drawn(draw,byId['new-a'],1).disc.r>restR,'A new node draws large');
  assert(drawn(draw,byId.old,1).alpha<=.41,'An old node fades');
  box.GroveView.showChanges('edges');await settle();
  assert(elements['selection-pill'].innerHTML.includes('3 new connections since 2026-11-03'),'The chip counts connections');
  assert(drawn(draw,byId.hub,1).disc.r>=22,'A new connection highlights its ends');

  // New rows across a large map: the camera keeps the whole map in view, as the target does.
  const wide={selfId:'self',generatedAt:READ,nodes:[item('self','person','Mira Vale',OLD)],edges:[]};
  for(let h=0;h<20;h++){
    wide.nodes.push(item('hub'+h,'organisation','Hub '+h,OLD));
    for(let i=0;i<99;i++){const id='w'+h+'-'+i;wide.nodes.push(item(id,'task','Item',i%10?OLD:NEW));wide.edges.push(link('l'+h+'-'+i,id,'hub'+h,OLD));}
    if(h)wide.edges.push(link('bridge'+h,'hub'+h,'hub'+(h-1),OLD));
  }
  const far=await signedIn(wide,{});
  far.box.GroveView.graph().zoom(.3);far.box.GroveView.showChanges('nodes');await settle();
  assert.equal(far.box.GroveSimulation.getState().nodeDelta,200,'Every tenth item is new');
  assert(far.box.GroveView.graph().zoom()<.7,'A wide selection does not crop the map: zoom '+far.box.GroveView.graph().zoom());
  console.log('graph changes: +X from real record times, kept over a read and a reload, nothing with no time, the click highlights those rows');
})().catch(e=>{console.error(e);process.exitCode=1;});
