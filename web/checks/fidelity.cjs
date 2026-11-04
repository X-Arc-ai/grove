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
const connectedNodes=[],connectedEdges=[];
for(let group=0;group<5;group++){
  const root='organisation-'+group;
  connectedNodes.push({id:root,type:'organisation'});
  for(let plan=0;plan<4;plan++){
    const id=root+'-plan-'+plan;
    connectedNodes.push({id,type:'plan'});
    connectedEdges.push({source:root,target:id});
    for(let leaf=0;leaf<6;leaf++){
      const key=id+'-'+leaf;
      connectedNodes.push({id:key,type:'task'});
      connectedEdges.push({source:id,target:key},{source:root,target:key});
    }
  }
  if(group)connectedEdges.push({source:'organisation-'+(group-1),target:root});
}
const related=layout.organicView(connectedNodes,connectedEdges);
assert.ok(new Set(Object.values(related.primary)).size<=7,'related plans must share regions rather than each forming an isolated large hub');
console.log('fidelity layout: deterministic, bounded, complete, source-preserving controls passed');
