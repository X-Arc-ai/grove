// The synthetic fixture: make_fixture.py output (up to date), in the shape the checks and the
// views read (the graph keys of GET /v1/views/graph), with none of the banned words.
const fs=require('fs'),path=require('path'),assert=require('assert'),cp=require('child_process');
cp.execFileSync('python3',[path.join(__dirname,'make_fixture.py'),'--check'],{encoding:'utf8'});
const text=fs.readFileSync(path.join(__dirname,'fixture.synthetic.json'),'utf8'),w=JSON.parse(text);
assert.deepStrictEqual(Object.keys(w).sort(),['elements','generated_at','health','history','person_id','schema','source_node_ids','stats','story_date','time','version']);
const NODE=['id','label','type','summary','status','file_path','valid_at','invalid_at','created_at','updated_at','status_since','verified_at','version','properties','has_changes'];
const EDGE=['id','source','target','label','verb','cardinality','canonical','canonical_of','valid_at','invalid_at','note','source_field'];
const nodes=w.elements.nodes.map(n=>n.data),edges=w.elements.edges.map(e=>e.data),ids=new Set(nodes.map(n=>n.id));
assert(nodes.length>=150&&edges.length>=300,'A world big enough for the import checks');assert.equal(ids.size,nodes.length,'Unique node ids');
nodes.forEach(n=>{assert.deepStrictEqual(Object.keys(n),NODE);assert.equal(n.has_changes,n.version>1);assert(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(n.valid_at));});
edges.forEach(e=>{assert.deepStrictEqual(Object.keys(e),EDGE);assert(ids.has(e.source)&&ids.has(e.target),'Edge ends exist');assert(/^[a-z]+( [a-z]+)*$/.test(e.verb)&&e.label===e.verb,'Lowercase verb phrases');});
const SIM=new Set(['company','project','person','client','task','decision','event','observation','pattern']);
assert.deepStrictEqual([...new Set(nodes.map(n=>n.type))].sort(),[...SIM].sort(),'The sim visual types, each present');
assert.deepStrictEqual(Object.keys(w.source_node_ids).sort(),['calendar','chats','conversations','notes']);
for(const list of Object.values(w.source_node_ids))assert(list.length>=40&&list.every(id=>ids.has(id)),'Each source names enough real nodes');
assert.equal(w.stats.total_nodes,nodes.length);assert.equal(w.stats.total_edges,edges.length);assert.equal(w.stats.embedded,nodes.length);
assert.deepStrictEqual(w.stats.edge_verbs,[...new Set(edges.map(e=>e.verb))].sort());
assert.equal(w.health.overall.state,'healthy');assert.equal(w.health.nodes,nodes.length);
assert(w.history.length&&w.history.every(p=>/^\d{4}-\d\d-\d\d$/.test(p.date)));assert(nodes.find(n=>n.id===w.person_id).type==='person');
for(const t of SIM)assert(Array.isArray(w.schema[t].optional)&&w.schema[t].status_field==='status');
const banned=new RegExp([['ham','za'],['ham','py'],['aa','tar']].map(p=>p.join('')).join('|'),'i');
assert(!banned.test(text),'None of the banned words');
console.log('PASS: synthetic fixture up to date with make_fixture.py, the views graph keys, valid edge ends, lowercase verbs, the sim visual types, four sources, consistent stats, health, history, schema and no banned words');
