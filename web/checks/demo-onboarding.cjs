// Focused recording-release regressions. No real account, file content, or API calls.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const web = path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(web, name), 'utf8');
const product = read('product.js');
const upload = product.slice(product.indexOf('  function importSource('), product.indexOf('  function requestPendingExport('));

async function demo(search, expectedProvider) {
  let now = Date.parse('2026-10-28T09:00:00Z');
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const timers = new Map(), events = {}, addresses = [], markers = [];
  let sequence = 0, ready;
  const context = {
    console, Date: Clock, URL, URLSearchParams, Promise, Math,
    location: {href: 'https://example.test/demo/onboarding' + search, search},
    history: {replaceState: (_, __, url) => addresses.push(url)},
    document: {
      addEventListener: (_, fn) => { ready = fn; },
      createElement: () => ({setAttribute() {}, innerHTML: ''}),
      body: {appendChild: el => markers.push(el)}
    },
    localStorage: {getItem() { throw new Error('Live storage read'); }, setItem() { throw new Error('Live storage write'); }},
    fetch() { throw new Error('Real API request'); },
    setTimeout: (fn, delay) => { timers.set(++sequence, {fn, delay, at: now + delay}); return sequence; },
    clearTimeout: id => timers.delete(id),
    addEventListener: (name, fn) => { (events[name] ||= []).push(fn); },
    dispatchEvent: event => (events[event.type] || []).forEach(fn => fn(event)),
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    GROVE_FIXTURE: JSON.parse(read('demo.synthetic.json'))
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(read('demo-onboarding.js'), context);
  assert.equal(context.__GROVE_SIM_TICK_MS__, 3000);
  // Use exactly the memory boundary emitted by the demo builder.
  vm.runInContext('(function(localStorage){' + read('simulation.js') + '\n})(window.GroveDemo.storage);', context);
  await Promise.resolve();
  ready();
  await Promise.resolve();
  const api = context.GroveSimulation;
  assert.equal(api.isLive(), false);
  assert.equal(context.document.title, 'Grove');
  assert.equal(markers.length, 0);
  if (expectedProvider) {
    assert.equal(api.getState().onboardingComplete, false);
    assert.equal(api.getState().onboardingStep, 1);
    assert.equal(api.getState().tourComplete, false);
    assert.equal(api.getState().accountImports[expectedProvider].status, 'importing');
    const before = api.getState().currentNodeCount;
    const started = now;
    function advance(ms) {
      now += ms;
      for (const [id, timer] of [...timers]) {
        if (timer.at <= now) {
          timers.delete(id);
          timer.fn();
        }
      }
    }
    for (let step = 1; step <= 18; step++) {
      assert.equal(timers.size, 1);
      assert.equal([...timers.values()][0].delay, 3000);
      const prior = api.getState().currentNodeCount;
      advance(2999);
      assert.equal(api.getState().currentNodeCount, prior);
      assert.equal(api.getState().accountImports[expectedProvider].status, 'importing');
      advance(1);
      if (step < 18) assert.equal(api.getState().accountImports[expectedProvider].status, 'importing');
    }
    assert.equal(now - started, 54000);
    assert.equal(timers.size, 0);
    assert.equal(api.getState().accountImports[expectedProvider].status, 'ready');
    assert(api.getState().currentNodeCount > before);
    assert(api.getState().currentNodeCount >= 2000);
    assert(api.getState().currentEdgeCount >= 3000);
  } else {
    assert.equal(api.getState().onboardingComplete, false);
    assert.equal(api.getState().onboardingStep, 0);
    assert.equal(Object.keys(api.getState().accountImports).length, 0);
    assert.equal(addresses[0], '/demo/onboarding');
  }
  return api.getState();
}

(async () => {
  await demo('?provider=claude&start=1', 'claude');
  await demo('?provider=chatgpt&start=1', 'chatgpt');
  await demo('?provider=private-file.zip&start=1&account=private', null);
  await demo('', null); // A fresh page restarts without the previous import.
  assert.equal(JSON.parse(read('checks/fixture.synthetic.json')).elements.nodes.length, 222);
  const ordinary = read('dist/index.html'), artifact = read('dist/demo-onboarding.html');
  assert(!ordinary.includes('GROVE_FIXTURE'));
  assert(!ordinary.includes('GroveClaudePreviewData'));
  assert(!ordinary.includes('claude-preview'));
  assert(!ordinary.includes('GroveConversationPreview'));
  assert(ordinary.includes('window.GroveImportGrowth ='), 'The live page paces the real graph');
  assert(!artifact.includes('window.GroveImportGrowth ='), 'The demo build leaves the live-only pacer out');
  for (const name of fs.readdirSync(path.join(web, 'dist/assets'))) {
    if (name === 'manifest.json') continue;
    const asset = fs.readFileSync(path.join(web, 'dist/assets', name));
    assert(!asset.includes('GroveClaudePreviewData') && !asset.includes('"source_node_ids"'), 'No public asset carries invented data: ' + name);
  }
  assert(!ordinary.includes('"source_node_ids": {'));
  assert(artifact.includes('window.GROVE_FIXTURE='));
  assert(artifact.includes('})(window.GroveDemo.storage);'));
  assert(!artifact.includes('window.GroveLive ='));
  assert(artifact.includes('Automatic '));
  assert(!read('style.css').includes('#grove-demo-marker'));
  assert(!artifact.includes('grove-demo-marker'));
  assert(!artifact.includes('Simulation · Grove'));
  assert(!ordinary.includes('window.__GROVE_SIM_TICK_MS__ = 3000;'));
  const demoTick = artifact.indexOf('window.__GROVE_SIM_TICK_MS__ = 3000;');
  assert(demoTick >= 0 && demoTick < artifact.indexOf('var TICK_MS ='));
  assert(product.includes("if((live||demo)&&el.dataset.guide==='muse')"));
  console.log('PASS standalone preview retains onboarding, memory isolation, 54-second growth, restart and artifact boundaries');
})().catch(error => { console.error(error); process.exitCode = 1; });
