'use strict';
const assert=require('node:assert/strict');
const layout=require('../layout-keep.js');
const nodes=[],edges=[];
for(let g=0;g<8;g++){
  nodes.push({id:'hub-'+g,type:'plan',x:1000*Math.cos(g),y:1000*Math.sin(g)});
  for(let n=0;n<35;n++){
    nodes.push({id:`node-${g}-${n}`,type:n%5?'observation':'task'});
    edges.push({source:'hub-'+g,target:`node-${g}-${n}`});
    if(n)edges.push({source:`node-${g}-${n-1}`,target:`node-${g}-${n}`});
  }
  if(g)edges.push({source:'hub-'+(g-1),target:'hub-'+g});
}
const original=JSON.stringify({nodes,edges});
assert.equal(typeof layout.organicView,'function','a view-only organic layout must exist');
const first=layout.organicView(nodes,edges);
assert.deepEqual(layout.organicView([...nodes].reverse(),[...edges].reverse()),first,'input order must not alter the view');
assert.equal(JSON.stringify({nodes,edges}),original,'persisted positions and source records stay unchanged');
assert.equal(Object.keys(first.positions).length,nodes.length,'all real nodes stay in the view');
for(const p of Object.values(first.positions))assert.ok(Number.isFinite(p.x)&&Number.isFinite(p.y));
const hubs=nodes.filter(n=>n.type==='plan').map(n=>first.positions[n.id]);
const radius=hubs.map(p=>Math.hypot(p.x,p.y));
assert.ok(Math.max(...radius)-Math.min(...radius)>100,'hubs must not remain on one circular rim');
assert.deepEqual(layout.organicView([],[]).positions,{});
const kept=layout.placeNew({'hub-0':{x:42,y:51}},nodes,edges,first.positions,8);
assert.deepEqual(kept.positions['hub-0'],{x:42,y:51},'the existing saved-map contract remains exact');
console.log('fidelity layout: deterministic, bounded, complete, source-preserving controls passed');
