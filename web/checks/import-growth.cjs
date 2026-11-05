// The import pacer on the real state engine: it shows only the server's graph, a few nodes at a time.
// GROVE_IMPORT_WEB selects a source copy, for the neutered run that must turn this check red.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const web = process.env.GROVE_IMPORT_WEB || path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(web, name), 'utf8');
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const node = id => ({data: {id, type: 'task', label: 'Item ' + id, summary: 'Stored by the server'}});
const link = (source, target) => ({data: {id: source + '>' + target, source, target, verb: 'part of'}});

function world({reduced = false} = {}) {
  let now = Date.parse('2026-11-05T09:00:00Z'), serial = 0;
  const timers = new Map(), storage = {}, events = {}, reads = [], held = [];
  const server = {nodes: [node('me')], edges: [], status: 200, hold: false};
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const reply = () => ({ok: server.status === 200, status: server.status, body: server.status === 200 ? {
    elements: {nodes: server.nodes.map(n => ({data: {...n.data}})), edges: server.edges.map(e => ({data: {...e.data}}))},
    stats: {}, time: {min: null, max: null}, self_item_id: 'me'} : {error: {code: 'unavailable', message: 'Try again'}}});
  const c = {
    console, Date: Clock, Promise, URL, Set, Math, JSON, Number, Object,
    location: {href: 'https://grove.example/'},
    localStorage: {getItem: key => storage[key] || null, setItem: (key, value) => { storage[key] = value; }},
    setTimeout(fn, ms) { timers.set(++serial, {fn, ms}); return serial; }, clearTimeout: id => timers.delete(id),
    matchMedia: () => ({matches: reduced}),
    addEventListener: (name, fn) => (events[name] ||= []).push(fn),
    dispatchEvent: event => (events[event.type] || []).forEach(fn => fn(event)),
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    GroveLive: {ENDPOINTS: {graph: '/graph', node: '/node/', schema: '/schema', health: '/health'}, errorText: () => 'Try again',
      getJSON: async target => {
        reads.push({target, at: now});
        if (target === '/graph' && server.hold) return new Promise(resolve => held.push(() => resolve(reply())));
        if (target === '/graph') return reply();
        return {ok: true, status: 200, body: {node: {id: target.split('/').pop(), title: 'Real detail'}}};
      }},
  };
  c.window = c; vm.createContext(c);
  vm.runInContext(read('simulation.js'), c);
  vm.runInContext(read('import-growth.js'), c);
  const view = {reloads: 0, last: null};
  c.reloadGrove = () => { view.reloads++; return c.groveApi('/api/graph').then(r => r.json()).then(body => { if (!body.error) view.last = body; }); };
  c.GroveView = {nodes: () => view.last ? view.last.elements.nodes.map(n => n.data) : []};
  const ids = () => view.last.elements.nodes.map(n => n.data.id).sort();
  const tick = async () => {
    const pending = [...timers].filter(([, timer]) => timer.ms === 1500);
    assert.equal(pending.length, 1, 'The pacer keeps one timer of 1.5 seconds');
    const [id, timer] = pending[0];
    now += 1500; timers.delete(id); timer.fn(); await settle();
  };
  const signIn = async (account = 'synthetic-owner') => {
    c.GroveSimulation.bindAccount(account); c.GroveSimulation.signIn('email', 'review@example.test');
    await c.reloadGrove(); await settle();
  };
  const graphReads = () => reads.filter(row => row.target === '/graph');
  return {c, server, view, timers, storage, reads, held, ids, tick, signIn, graphReads,
    api: () => c.GroveSimulation, growth: () => c.GroveImportGrowth};
}
const many = (count, from = 0) => Array.from({length: count}, (_, i) => node('n' + (from + i)));
const checks = [];
const check = (name, run) => checks.push([name, run]);

check('The next count is at least one, a small share, and capped', () => {
  const next = world().growth().nextVisibleCount;
  assert.equal(next(0, 1), 1);
  assert.equal(next(0, 5), 1);
  assert.equal(next(0, 100), 10);
  assert.equal(next(0, 100000), 24);
  assert.equal(next(7, 7), 7);
  assert.equal(next(9, 7), 9);
  for (let total = 1; total < 900; total += 7) for (const seen of [0, 1, Math.floor(total / 2), total - 1].filter(value => value < total)) {
    const step = next(seen, total) - seen;
    assert(step >= 1 && step <= 24 && seen + step <= total, 'step of ' + step);
  }
  const seconds = total => { let shown = 0, ticks = 0; while (shown < total) { shown = next(shown, total); ticks++; } return ticks * 1.5; };
  assert(seconds(300) >= 45 && seconds(300) <= 90, 'A burst of 300 plays over about a minute: ' + seconds(300));
  assert(seconds(5000) <= 600, 'A few thousand nodes do not take an hour: ' + seconds(5000));
});

check('An accepted upload plays new real nodes over the baseline, and edges wait for both ends', async () => {
  const t = world(); await t.signIn();
  assert.deepEqual(t.ids(), ['grove-self']);
  t.growth().accept(); await settle();
  assert.equal(t.growth().active(), true);
  t.server.nodes.push(...many(40));
  t.server.edges.push(link('n0', 'n1'), link('n0', 'n39'), link('me', 'n0'));
  for (let i = 0; i < 4; i++) await t.tick();   // 6 seconds: the second read has landed
  await t.tick();
  const shown = t.ids();
  assert(shown.includes('grove-self'), 'The baseline stays');
  assert(shown.length > 1 && shown.length < 41, 'A few of 40 show after one reveal: ' + shown.length);
  assert.equal(shown[0], 'grove-self');
  const seen = new Set(shown);
  for (const edge of t.view.last.elements.edges) assert(seen.has(edge.data.source) && seen.has(edge.data.target), 'Both ends of an edge are visible');
  assert.equal(t.api().getState().currentNodeCount, shown.length, 'The count follows what the person sees');
  assert.equal(t.api().getState().totalNodeCount, 41, 'The real total stays known');
  assert.equal(t.api().getState().nodeDelta, shown.length - 1);
  assert.deepEqual(t.api().getChanges().new_nodes.map(row => row.id).sort(), shown.filter(id => id !== 'grove-self'));
  assert.equal(t.api().getChanges('claude').new_nodes.length, 0, 'No provider split is claimed');
  const before = shown.length;
  await t.tick();
  assert(t.ids().length > before, 'Each tick reveals more');
});

check('Reads of the server graph stay at least 5 seconds apart while work moves', async () => {
  const t = world(); await t.signIn();
  const start = t.graphReads().length;
  t.growth().accept(); await settle();
  for (let i = 0; i < 20; i++) { t.server.nodes.push(node('slow' + i)); await t.tick(); }
  const times = t.graphReads().slice(start).map(row => row.at);
  assert(times.length >= 5, 'The graph is refreshed: ' + times.length);
  for (let i = 1; i < times.length; i++) assert(times[i] - times[i - 1] >= 5000, 'Reads ' + (times[i] - times[i - 1]) + ' ms apart');
});

check('Node detail and other reads go to the real API while the pacer plays', async () => {
  const t = world(); await t.signIn();
  t.server.nodes.push(...many(3)); t.growth().accept(); await settle();
  await t.tick();
  const body = await (await t.c.groveApi('/api/node/n0')).json();
  assert.equal(body.node.title, 'Real detail');
  assert(t.reads.some(row => row.target === '/node/n0'));
  await (await t.c.groveApi('/api/health')).json();
  assert(t.reads.some(row => row.target === '/health'));
  assert.equal(t.api().realId('n0'), 'n0', 'A revealed node keeps its real id');
  assert.equal(t.api().realId('grove-self'), 'me');
});

check('The pacer stops only when progress is terminal and every node is visible', async () => {
  const t = world(); await t.signIn();
  t.growth().accept(); await settle();
  t.server.nodes.push(...many(12));
  await t.tick(); await t.tick(); await t.tick(); await t.tick();
  t.growth().observe(false);
  assert.equal(t.growth().active(), true, 'Terminal progress alone does not stop it');
  t.server.nodes.push(node('last'));
  let guard = 0;
  while (t.growth().active() && guard++ < 60) await t.tick();
  assert.equal(t.growth().active(), false);
  assert.equal(t.timers.size, 0, 'No timer stays');
  await settle();
  assert.equal(t.view.last.elements.nodes.length, 14, 'A node that landed after the last moving read still shows');
  assert.equal(t.api().getState().currentNodeCount, 14);
});

check('New work after a terminal read keeps the pacer running', async () => {
  const t = world(); await t.signIn();
  t.growth().accept(); await settle();
  t.growth().observe(false); t.growth().observe(true);
  for (let i = 0; i < 8; i++) await t.tick();
  assert.equal(t.growth().active(), true);
});

check('A page that opens on moving work keeps what exists and plays only what arrives', async () => {
  const t = world();
  t.server.nodes.push(...many(30));
  t.api().bindAccount('synthetic-owner'); t.api().signIn('email', 'review@example.test');
  t.growth().observe(true); await settle();
  assert.equal(t.growth().active(), true);
  await t.c.reloadGrove(); await settle();
  assert.equal(t.view.last.elements.nodes.length, 31, 'The graph already stored does not replay');
  t.server.nodes.push(...many(5, 30));
  for (let i = 0; i < 5; i++) await t.tick();   // the read at 6 seconds, then one reveal
  assert(t.view.last.elements.nodes.length < 36, 'What arrives later plays');
  assert(t.view.last.elements.nodes.length >= 32);
});

check('An idle progress read starts nothing', async () => {
  const t = world(); await t.signIn();
  t.growth().observe(false); await settle();
  assert.equal(t.growth().active(), false);
  assert.equal(t.timers.size, 0);
});

check('Reduced motion reveals at once', async () => {
  const t = world({reduced: true}); await t.signIn();
  t.server.nodes.push(...many(60));
  t.growth().accept(); await settle();
  await t.c.reloadGrove(); await settle();
  assert.equal(t.view.last.elements.nodes.length, 61);
});

check('A failed read keeps the last graph, and a node the server drops leaves the view', async () => {
  const t = world(); await t.signIn();
  t.server.nodes.push(...many(2));
  t.growth().accept(); await settle();
  await t.tick(); await t.tick();
  const shown = t.ids();
  t.server.status = 503;
  for (let i = 0; i < 4; i++) await t.tick();
  assert.equal(t.growth().active(), true);
  assert.deepEqual(t.ids(), shown);
  t.server.status = 200;
  t.server.nodes = t.server.nodes.filter(row => row.data.id !== 'n0');
  for (let i = 0; i < 5; i++) await t.tick();
  assert(!t.ids().includes('n0'));
});

for (const event of ['grove:unauthenticated', 'grove:session-changed']) {
  check(event + ' clears the pacer and its timer', async () => {
    const t = world(); await t.signIn();
    t.growth().accept(); await settle();
    t.c.dispatchEvent(new t.c.CustomEvent(event));
    assert.equal(t.growth().active(), false);
    assert.equal(t.timers.size, 0);
    assert.equal(t.growth().answer('/api/graph'), null);
  });
}

check('Sign-out clears the pacer, and nothing of the graph enters storage', async () => {
  const t = world(); await t.signIn();
  t.server.nodes.push(...many(5));
  t.growth().accept(); await settle(); await t.tick();
  assert(!JSON.stringify(t.storage).includes('Item n'), 'The graph stays out of browser storage');
  t.api().signOut();
  assert.equal(t.growth().active(), false);
  assert.equal(t.timers.size, 0);
});

check('A late graph answer from an earlier account never reaches the view', async () => {
  const t = world(); await t.signIn('synthetic-owner');
  t.server.nodes.push(...many(4));
  t.server.hold = true;
  t.growth().accept(); await settle();
  const pending = t.c.groveApi('/api/graph').then(r => r.json());
  assert.equal(t.held.length, 1);
  t.api().bindAccount('synthetic-other');
  assert.equal(t.growth().active(), false, 'The account change clears the pacer');
  t.held[0](); await settle();
  const late = await pending;
  assert.equal(late.code, 'superseded', 'A view read of the earlier account answers nothing');
  assert.equal(t.api().getState().currentNodeCount, 0, 'No node of the earlier account stays');
  assert.equal(t.api().getState().account, 'synthetic-other');
});

check('A context change without any event stops the pacer on its next tick', async () => {
  const t = world(); await t.signIn();
  t.growth().accept(); await settle();
  const context = t.api().pendingExportContext();
  t.c.GroveSimulation.isPendingExportContext = other => { assert.deepEqual(JSON.parse(JSON.stringify(other)), JSON.parse(JSON.stringify(context))); return false; };
  const [[id, timer]] = [...t.timers];
  t.timers.delete(id); timer.fn(); await settle();
  assert.equal(t.growth().active(), false);
  assert.equal(t.timers.size, 0);
});

(async () => {
  let failed = 0;
  for (const [name, run] of checks) {
    try { await run(); console.log('PASS: ' + name); }
    catch (error) { failed++; console.error('FAIL: ' + name + '\n' + error.stack); }
  }
  console.log(`Import growth: ${checks.length - failed} passed, ${failed} failed`);
  if (failed) process.exitCode = 1;
})();
