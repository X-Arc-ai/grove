// Kept map positions (web/layout-keep.js): placeNew keeps every stored position exactly, places
// new nodes without overlap near their hub or neighbours, and with nothing stored returns the
// seed (the sim's own layout) as it is.
const assert=require('assert'),path=require('path');
const {placeNew}=require(path.join(__dirname,'..','layout-keep.js'));

// A deterministic world: hubs on a ring, leaves on a spiral around their hub (like seedLayout).
function world(hubCount,leavesPerHub,tag){
  const nodes=[],edges=[],seed={};
  for(let h=0;h<hubCount;h++){
    const id=tag+'hub'+h,a=2*Math.PI*h/hubCount;
    nodes.push({id,hub:true,_primaryHubId:null});seed[id]={x:1000+700*Math.cos(a),y:1000+450*Math.sin(a)};
    let theta=0,r=90;
    for(let l=0;l<leavesPerHub;l++){
      const lid=tag+'leaf'+h+'-'+l;
      if(l){theta+=40/r;r=90+52*theta/(2*Math.PI);}
      nodes.push({id:lid,_primaryHubId:id});seed[lid]={x:seed[id].x+Math.cos(theta)*r,y:seed[id].y+Math.sin(theta)*r};
      edges.push({source:lid,target:id});
      if(l%4===1)edges.push({source:lid,target:tag+'leaf'+h+'-'+(l-1)});
    }
  }
  return {nodes,edges,seed};
}
const room=n=>n.hub?62*1.45+6:10*1.45+6;
const byId=list=>Object.fromEntries(list.map(n=>[n.id,n]));
function noOverlap(positions,nodes,ids,label){
  const map=byId(nodes);
  for(const a of ids)for(const n of nodes){
    if(n.id===a)continue;
    const d=Math.hypot(positions[a].x-positions[n.id].x,positions[a].y-positions[n.id].y);
    assert(d>=room(map[a])+room(n)-1e-9,label+': '+a+' overlaps '+n.id+' ('+d.toFixed(2)+')');
  }
}
function keptExactly(positions,stored,label){
  for(const id of Object.keys(stored)){
    if(!positions[id])continue;
    assert(positions[id].x===stored[id].x&&positions[id].y===stored[id].y,label+': '+id+' moved');
  }
}

// 1. Nothing stored: the result is the seed itself, and every node counts as placed.
const base=world(4,18,'a');
const first=placeNew({},base.nodes,base.edges,base.seed,room);
assert.deepStrictEqual(first.positions,Object.fromEntries(base.nodes.map(n=>[n.id,{x:base.seed[n.id].x,y:base.seed[n.id].y}])));
assert.deepStrictEqual(first.placed,base.nodes.map(n=>n.id));
assert.deepStrictEqual(placeNew(undefined,base.nodes,base.edges,base.seed,room).positions,first.positions,'An absent store is an empty store');

// 2. A reload: the store holds every node (in a frame that is not the seed's); nothing moves, nothing is placed.
const shift=(p,i)=>({x:p.x+400+(i%7)*0.37,y:p.y-250-(i%5)*0.61});
const stored=Object.fromEntries(base.nodes.map((n,i)=>[n.id,shift(base.seed[n.id],i)]));
const reload=placeNew(stored,base.nodes,base.edges,base.seed,room);
keptExactly(reload.positions,stored,'reload');assert.deepStrictEqual(reload.placed,[]);

// 3. New items arrive: leaves on stored hubs, a new hub with its own leaves, a node with only
//    placed neighbours, and a lone node. The seed of the grown graph differs (a new layout).
const grown=world(5,22,'a');
const lone={id:'lone',_primaryHubId:null},linked={id:'linked',_primaryHubId:null};
grown.nodes.push(lone,linked);grown.seed.lone={x:1500,y:1500};grown.seed.linked={x:900,y:300};
grown.edges.push({source:'linked',target:'aleaf0-3'},{source:'linked',target:'aleaf1-5'});
const after=placeNew(stored,grown.nodes,grown.edges,grown.seed,room);
keptExactly(after.positions,stored,'arrival');
const fresh=grown.nodes.filter(n=>!stored[n.id]).map(n=>n.id);
assert.deepStrictEqual([...after.placed].sort(),[...fresh].sort(),'Only the new nodes are placed');
assert(fresh.includes('ahub4')&&fresh.includes('aleaf0-20')&&fresh.length===2+4*4+23,'The arrival adds the expected nodes');
noOverlap(after.positions,grown.nodes,fresh,'arrival');
assert(after.placed.indexOf('ahub4')<after.placed.indexOf('aleaf4-0'),'A new hub is placed before its leaves');
const seedDist=(a,b)=>Math.hypot(grown.seed[a].x-grown.seed[b].x,grown.seed[a].y-grown.seed[b].y);
const dist=(a,b)=>Math.hypot(after.positions[a].x-after.positions[b].x,after.positions[a].y-after.positions[b].y);
for(const id of fresh){
  const n=byId(grown.nodes)[id];
  if(n._primaryHubId)assert(dist(id,n._primaryHubId)<=seedDist(id,n._primaryHubId)+4*(room(n)+62*1.45+6),'A new leaf sits near its hub: '+id);
}
const nb=['aleaf0-3','aleaf1-5'].map(id=>after.positions[id]),centre={x:(nb[0].x+nb[1].x)/2,y:(nb[0].y+nb[1].y)/2};
assert(Math.hypot(after.positions.linked.x-centre.x,after.positions.linked.y-centre.y)<=4*(room(linked)+62*1.45+6),'A node with placed neighbours sits near them');
// the stored frame is the seed frame of the first layout moved by about (+400, -250)
const loneTarget={x:grown.seed.lone.x+400,y:grown.seed.lone.y-250};
assert(Math.hypot(after.positions.lone.x-loneTarget.x,after.positions.lone.y-loneTarget.y)<=40+4*room(lone),'A lone node keeps its seed place in the stored frame');

// 4. Several arrivals, each fed back as stored (reload after each): every earlier node stays
//    exactly where it was placed, and no disc ever overlaps another.
let store={};const rounds=[world(2,6,'a'),world(3,10,'a'),world(4,16,'a'),world(6,24,'a')];
for(const [k,g] of rounds.entries()){
  const out=placeNew(store,g.nodes,g.edges,g.seed,room);
  keptExactly(out.positions,store,'round '+k);
  if(k)noOverlap(out.positions,g.nodes,out.placed,'round '+k);
  out.placed.forEach(id=>assert(!store[id],'round '+k+': a stored node was placed again'));
  store=Object.assign({},store,...out.placed.map(id=>({[id]:out.positions[id]})));
}
assert.equal(Object.keys(store).length,rounds[3].nodes.length);

// 5. Deterministic, ignores stored ids that left the graph, takes a number for the radius.
assert.deepStrictEqual(placeNew(stored,grown.nodes,grown.edges,grown.seed,room),after,'Same input, same layout');
const gone=placeNew(Object.assign({ghost:{x:1,y:2}},stored),grown.nodes,grown.edges,grown.seed,room);
assert(!('ghost' in gone.positions));assert.deepStrictEqual(gone.positions,after.positions);
const flat=placeNew({a:{x:0,y:0}},[{id:'a'},{id:'b'}],[],{a:{x:0,y:0},b:{x:0,y:0}},10);
assert.deepStrictEqual(flat.positions.a,{x:0,y:0});assert(Math.hypot(flat.positions.b.x,flat.positions.b.y)>=20-1e-9,'A numeric radius still keeps discs apart');
console.log('PASS: empty store equals the seed, reload keeps every position exactly, arrivals keep stored positions and place only new nodes, no overlap with placed or newly placed discs, new leaves near their hub, linked nodes near their neighbours, lone nodes in the stored frame, hubs before their leaves, repeated arrivals, determinism and stale ids');
