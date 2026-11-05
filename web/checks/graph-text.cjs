// No text inside a node, at any zoom (ruling R5). The real native-app.js draws every kind of node
// at every scale, at rest and under a hover. Nothing may write text inside a disc: only the owner's
// name, below the owner's disc. agents.js draws the same map when the workers are on, so its source
// may hold no hub name either. web/checks/graph_parity.py proves the same in a real browser.
const assert=require('assert');
const {read,signedIn,drawn,item,link}=require('./lib/graph-sandbox.cjs');

const nodes=[item('self','person','Mira Vale'),item('org','organisation','Northwind Harbour Trading Company'),item('plan','plan','Spring catalogue relaunch'),
  item('person','person','Tomas Reed'),item('robot','instance','Field assistant'),item('place','place','Lisbon studio'),item('task','task','Send the brief'),
  item('note','observation','Noticed a delay'),item('moment','event','Kickoff'),item('choice','decision','Pick the printer'),item('kind','recipe','Sourdough method')];
const edges=nodes.slice(1).map((n,i)=>link('e'+i,n.data.id,i%2?'org':'plan')).filter(e=>e.data.source!==e.data.target);
edges.push(link('own','self','org'));

(async()=>{
  const {box}=await signedIn({nodes,edges,selfId:'self',generatedAt:'2026-11-04T12:00:00Z'});
  const graph=box.GroveView.graph(),draw=graph.nodeCanvasObject(),hover=graph.onNodeHover();
  assert.equal(typeof draw,'function','The map gives the graph its node draw');
  const map=box.GroveView.nodes();
  assert.equal(map.length,nodes.length,'Every node of the world is on the map');
  assert(map.some(n=>n.id==='grove-self'),'The owner is on the map');
  const scales=[.02,.05,.1,.2,.3,.39,.4,.41,.5,.55,.59,.6,.61,.8,1,1.15,1.5,2,3,4,8,16];
  let checked=0;
  function sweep(state){
    for(const node of map)for(const scale of scales){
      const {texts,disc}=drawn(draw,node,scale);
      checked++;
      assert(disc,state+': '+node.id+' draws a disc');
      if(node.id==='grove-self'){
        assert.equal(new Set(texts.map(t=>t.s)).size,1,state+': the owner shows one name, at scale '+scale);
        for(const t of texts)assert(t.y>=disc.y+disc.r,state+': the owner name sits below the disc at scale '+scale);
      }else assert.deepEqual(texts.map(t=>t.s),[],state+': '+node.id+' draws no text at scale '+scale);
    }
  }
  sweep('rest');
  for(const n of map){hover(n);sweep('hover on '+n.id);}
  hover(null);
  // At rest a live map holds no verb either, at any zoom: only the link under the pointer shows one.
  const labelled=graph.linkCanvasObjectMode();
  for(const zoom of [.3,.6,1,1.5,3]){graph.zoom(zoom);for(const verb of ['DEPENDS ON','depends on','blocks','relates to'])assert.equal(labelled({source:'org',target:'plan',verb}),null,'A link at rest has no text at zoom '+zoom);}
  for(const file of ['native-app.js','agents.js']){
    const source=read(file);
    assert(!/drawHubLabel|drawInitialLetter|hubLabel\(/.test(source.replace(/^\s*\/\/.*$/gm,'')),file+' has no text path inside a node');
  }
  console.log('graph text: '+checked+' draws, no text inside any node at any zoom, the owner name stays below the owner disc');
})().catch(e=>{console.error(e);process.exitCode=1;});
