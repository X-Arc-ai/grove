// Focused recording-release regressions. No real account, file content, or API calls.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const web = path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(web, name), 'utf8');
const product = read('product.js');
const upload = product.slice(product.indexOf('  function importSource('), product.indexOf('  function requestPendingExport('));

// The public action must navigate before it reads or sends any selected file bytes.
for (const provider of ['claude', 'chatgpt']) {
  const destinations = [];
  const context = {
    window: {location: {assign: url => destinations.push(url)}},
    live: {uploadExport() { throw new Error('The upload action sent a private file'); }},
    canUpload: () => true, liveUpload: () => ({pending: false}), render() {},
    api: {pendingExportContext: () => ({})}
  };
  vm.createContext(context);
  vm.runInContext(upload, context);
  context.importSource('conversations', new Proxy({}, {
    get() { throw new Error('The simulation trigger read the selected file'); }
  }), provider);
  assert.deepEqual(destinations, ['/demo/onboarding?provider=' + provider + '&start=1']);
}

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
    setTimeout: fn => { timers.set(++sequence, fn); return sequence; },
    clearTimeout: id => timers.delete(id),
    addEventListener: (name, fn) => { (events[name] ||= []).push(fn); },
    dispatchEvent: event => (events[event.type] || []).forEach(fn => fn(event)),
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    GROVE_FIXTURE: JSON.parse(read('demo.synthetic.json'))
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(read('demo-onboarding.js'), context);
  // Use exactly the memory boundary emitted by the demo builder.
  vm.runInContext('(function(localStorage){' + read('simulation.js') + '\n})(window.GroveDemo.storage);', context);
  await Promise.resolve();
  ready();
  await Promise.resolve();
  const api = context.GroveSimulation;
  assert.equal(api.isLive(), false);
  assert(markers[0].innerHTML.includes('Your file was not uploaded or saved'));
  assert(markers[0].innerHTML.includes('href="/"'));
  if (expectedProvider) {
    assert.equal(api.getState().onboardingComplete, true);
    assert.equal(api.getState().accountImports[expectedProvider].status, 'importing');
    const before = api.getState().currentNodeCount;
    now += 18000;
    const callbacks = [...timers.values()];
    timers.clear();
    callbacks.forEach(fn => fn());
    assert.equal(api.getState().accountImports[expectedProvider].status, 'ready');
    assert(api.getState().currentNodeCount > before);
    assert(api.getState().currentEdgeCount > 0);
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
  assert.equal(read('demo.synthetic.json'), read('checks/fixture.synthetic.json'));
  const ordinary = read('dist/index.html'), artifact = read('dist/demo-onboarding.html');
  assert(!ordinary.includes('GROVE_FIXTURE'));
  assert(!ordinary.includes('Maya Chen'));
  assert(artifact.includes('window.GROVE_FIXTURE='));
  assert(artifact.includes('})(window.GroveDemo.storage);'));
  assert(!artifact.includes('window.GroveLive ='));
  assert(artifact.includes('Automatic '));
  assert(product.includes("if((live||demo)&&el.dataset.guide==='muse')"));
  console.log('PASS demo upload trigger, memory isolation, synthetic growth, restart and artifact boundaries');
})().catch(error => { console.error(error); process.exitCode = 1; });
