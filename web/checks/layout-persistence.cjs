'use strict';
// Reuse the strict live HTTP/browser harness without running its legacy visual-coordinate
// assertions. Those assertions remain unchanged in live.cjs. This contract separates the
// existing insert-only stored map from the new adjacency-based visual projection.
const fs=require('node:fs'),path=require('node:path');
const harness=fs.readFileSync(path.join(__dirname,'live.cjs'),'utf8').split('(async()=>{')[0];
const checks=String.raw`
return (async()=>{
  const world=liveWorld(),server=makeServer(world),pg=page(server,{},GRAPH_FILES);
  const place=pg.sandbox.GroveLayoutKeep.placeNew,placements=[];
  pg.sandbox.GroveLayoutKeep.placeNew=(...args)=>{const value=place(...args);placements.push(value);return value;};
  await settle();
  assert.deepStrictEqual(pg.errors,[]);
  let posts=positionsCalls(server);
  assert.equal(posts.length,1,'Initial positions still save once');
  assert.equal(placements.length,1,'The initial stored map still uses placeNew');
  assert.deepStrictEqual(posts[0].body.positions.map(p=>p.id).sort(),world.nodes.map(n=>n.id).sort());
  for(const p of posts[0].body.positions){const view=p.id===world.selfId?'grove-self':p.id;assert.equal(p.x,placements[0].positions[view].x);assert.equal(p.y,placements[0].positions[view].y);}
  const stored=JSON.stringify(world.positions),drawn=fgPositions(pg);
  assert(posts[0].body.positions.some(p=>{const v=drawn[p.id===world.selfId?'grove-self':p.id];return v.x!==p.x||v.y!==p.y;}),'Visual projection is independent of stored coordinates');
  await pg.sandbox.reloadGrove();await settle();
  assert.equal(positionsCalls(server).length,1,'A refresh never rewrites stored coordinates');
  assert.equal(JSON.stringify(world.positions),stored);
  assert.deepStrictEqual(fgPositions(pg),drawn,'The same graph has the same visual projection');
  world.nodes.push({id:'new-note',type:'observation',label:'A new source-backed observation'});
  world.edges.push({id:'new-edge',source:'new-note',target:'p012',verb:'about'});
  await pg.sandbox.reloadGrove();await settle();
  posts=positionsCalls(server);
  assert.equal(posts.length,2);
  assert.deepStrictEqual(posts[1].body.positions.map(p=>p.id),['new-note'],'Only an unplaced item is sent');
  for(const [id,p] of Object.entries(JSON.parse(stored)))assert.deepStrictEqual(world.positions[id],p,'No existing stored coordinate changes');
  const recent=placements[placements.length-1].positions['new-note'];
  assert.deepStrictEqual(world.positions['new-note'],{x:recent.x,y:recent.y});

  const failed=liveWorld();failed.failPositions=true;
  const server2=makeServer(failed),pg2=page(server2,{},GRAPH_FILES);await settle();
  assert.equal(positionsCalls(server2).length,1);assert(pg2.warnings.length);
  const first=JSON.stringify(positionsCalls(server2)[0].body.positions);
  failed.failPositions=false;await pg2.sandbox.reloadGrove();await settle();
  assert.equal(positionsCalls(server2).length,2);
  assert.equal(JSON.stringify(positionsCalls(server2)[1].body.positions),first,'A failed save retries the same stored placements');
  console.log('PASS: initial insert-only placement, placeNew arrivals, self identity, no stored overwrites, independent visual projection, failed-save retry');
})();`;
new Function('require','__dirname',harness+checks)(require,__dirname).catch(error=>{console.error(error);process.exitCode=1;});
