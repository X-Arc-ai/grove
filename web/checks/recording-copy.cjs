// Synthetic live-mode rendering and action regression. No network, browser login, or build.
// GROVE_RECORDING_WEB can select an unchanged source tree for the preserved red run.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = process.env.GROVE_RECORDING_WEB || path.join(__dirname, '..');
const read = name => fs.readFileSync(path.join(web, name), 'utf8');
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const unwanted = /not available|unavailable|not applied|limited import|limited connection|off in this demo|does not start|does not connect|not imported/i;

// Minimal DOM for the real render functions. This verifies markup and action guards,
// while the independent browser review owns geometry, visibility, and focus behavior.
function element() {
  const attrs = {}, events = {}, classes = new Set();
  let markup = '', cache = new Map();
  const node = {
    dataset: {}, style: { setProperty() {} }, children: [], isConnected: true,
    checked: false, hidden: false, textContent: '', scrollTop: 0,
    classList: {
      add: (...names) => names.forEach(name => classes.add(name)),
      remove: (...names) => names.forEach(name => classes.delete(name)),
      contains: name => classes.has(name),
      toggle(name, on) { if (on === undefined) on = !classes.has(name); on ? classes.add(name) : classes.delete(name); },
    },
    setAttribute(name, value) { attrs[name] = String(value); },
    getAttribute: name => attrs[name] ?? null,
    hasAttribute: name => Object.hasOwn(attrs, name),
    appendChild(child) { node.children.push(child); return child; },
    contains: () => false, focus() {}, closest: () => null,
    addEventListener(name, callback) { (events[name] ||= []).push(callback); },
    dispatchEvent(event) { (events[event.type] || []).forEach(callback => callback.call(node, event)); },
    click() { if (!node.disabled) { if (node.onclick) node.onclick(); node.dispatchEvent({ type: 'click' }); } },
    querySelectorAll(selector) {
      const match = selector.match(/^\[([\w-]+)(?:="?([^"\]]+)"?)?\]$/);
      if (!match) return [];
      const found = [];
      for (const tag of markup.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)) {
        const values = {};
        for (const attr of tag[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) values[attr[1]] = attr[2] ?? '';
        if (!Object.hasOwn(values, match[1]) || (match[2] !== undefined && values[match[1]] !== match[2])) continue;
        const key = tag.index + ':' + tag[0];
        let child = cache.get(key);
        if (!child) {
          child = element();
          for (const [name, value] of Object.entries(values)) {
            child.setAttribute(name, value);
            if (name.startsWith('data-')) child.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
          }
          child.disabled = Object.hasOwn(values, 'disabled');
          cache.set(key, child);
        }
        found.push(child);
      }
      return found;
    },
    querySelector(selector) { return node.querySelectorAll(selector)[0] || null; },
  };
  Object.defineProperty(node, 'innerHTML', { get: () => markup, set(value) { markup = value; cache = new Map(); } });
  return node;
}

function harness() {
  const ids = new Map(), listeners = {}, calls = [];
  const state = {
    auth: { provider: 'email' }, account: 'synthetic-recording-account', name: '',
    onboardingComplete: true, onboardingStep: 0, tourComplete: true,
    sources: [], files: {}, requests: [], accountImports: {}, pendingExports: {},
    connectedHosts: [], mcpClients: [], growth: { focus: '', reading: '', avoid: '', confirm: true },
    currentNodeCount: 0, currentEdgeCount: 0, nodeDelta: 0, edgeDelta: 0,
  };
  const api = {
    isLive: () => true, getState: () => state,
    pendingExportContext: () => state.account,
    isPendingExportContext: context => context === state.account,
    refreshPendingExports: () => Promise.resolve(),
    saveGrowth: value => { state.growth = value; calls.push('browser-guidance'); },
  };
  for (const name of ['importSource', 'addFile', 'removeFile', 'disconnectSource', 'connectGoogle', 'connectHost', 'answerRequest', 'addMcpClient']) {
    api[name] = () => { calls.push(name); throw new Error('Unexpected mutation: ' + name); };
  }
  const live = {
    PAGES: { signup: '/signup', login: '/login', logout: '/logout', home: '/', privacy: '/privacy' },
    ENDPOINTS: { sourceCoverage: '/sources', sourceConnections: '/connections', health: '/health' },
    session: () => new Promise(() => {}), isAuthPage: () => false, authPagePath: page => page,
    mcpUrl: () => 'https://grove.example.test/mcp',
    errorText: (body, fallback) => body?.error?.message || fallback,
    uploadExport: async () => { calls.push('upload-request'); return { ok: false, status: 503, body: { error: { message: 'Synthetic upload failed. Try again.' } } }; },
    workers: async () => ({ ok: false, status: 503 }),
    getJSON: async () => ({ ok: false, status: 503, body: { error: { message: 'Synthetic read failed. Try again.' } } }),
    postJSON: async () => { calls.push('provider-request'); return { ok: false, status: 403, body: { error: { code: 'scope_denied' } } }; },
  };
  const document = {
    hidden: false, body: element(), documentElement: element(), createElement: element,
    getElementById(id) { if (!ids.has(id)) ids.set(id, element()); return ids.get(id); },
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
  };
  const toggle = document.getElementById('agents-toggle'), label = element();
  toggle.closest = () => label;
  const box = {
    console, URL, URLSearchParams, Promise, Date, JSON, Number, document,
    GroveSimulation: api, GroveLive: live, navigator: {},
    innerWidth: 1440, innerHeight: 900, performance: { now: () => 0 },
    location: { pathname: '/', search: '', href: 'https://grove.example.test/' },
    matchMedia: () => ({ matches: true }),
    setTimeout() {}, clearTimeout() {}, setInterval() {}, clearInterval() {}, requestAnimationFrame() {},
    CustomEvent: class { constructor(type) { this.type = type; } },
    addEventListener: (name, callback) => { (listeners[name] ||= []).push(callback); },
    dispatchEvent: event => (listeners[event.type] || []).forEach(callback => callback(event)),
    open: () => ({ closed: false, opener: null, location: {}, close() { this.closed = true; } }),
  };
  box.window = box;
  vm.createContext(box);
  const original = read('product.js');
  const marker = '  render(true);maybeTour();';
  assert(original.includes(marker), 'The product test entry point exists');
  const hook = '  window.recordingTest={ui,handle,bind,modalRoot,signIn,filesComposer,confirmation,growthForm,requestsPanel,sourceRow,health,hostGuide,exportPicker,importSource,liveUpload,uploadResult,setData(value){liveData=value;}};';
  vm.runInContext(original.replace(marker, hook), box, { filename: 'product.js' });
  box.recordingTest.ui.authStage = null;
  return { box, api, live, state, calls, document, toggle, p: box.recordingTest,
    load(name) { vm.runInContext(read(name), box, { filename: name }); } };
}

function clean(markup) { assert(!unwanted.test(markup), 'Unwanted visible capability copy: ' + markup.match(unwanted)?.[0]); }
function disabled(markup, attribute) { assert(new RegExp('<button[^>]*' + attribute + '[^>]*\\bdisabled\\b').test(markup), attribute + ' stays present and disabled'); }
function workerPayload() {
  return {
    schema: 'grove.worker-activity.v1', observed_at: '2026-11-04T12:00:00Z',
    observation_kind: 'stored_run_records', service_state: 'unknown', service_reason: 'no_process_heartbeat',
    recent_limit: 20, recent_has_more: false,
    agents: ['learner', 'curator', 'connector'].map((agent, index) => ({
      agent, policy: { state: index ? 'disabled' : 'limited', reason: index ? 'demo_autonomous_disabled' : 'demo_learner_only' },
      capability: { state: index === 1 ? 'unavailable' : 'limited', reason: ['legacy_learner_only', 'curator_authority_unavailable', 'legacy_connector_only'][index] },
      activity: { state: index ? 'idle' : 'recorded_running', running_records: index ? 0 : 1, stale_records: 0 },
    })),
    recent_runs: [{ agent: 'learner', outcome: 'failed', started_at: '2026-11-04T11:00:00Z', finished_at: '2026-11-04T11:01:00Z' }],
  };
}

const checks = [];
function check(name, run) { checks.push([name, run]); }
check('Apple remains disabled without an unavailable label', () => {
  const h = harness(), html = h.p.signIn();
  clean(html); disabled(html, 'data-signin="apple"');
  assert(html.includes('Continue with Apple')); assert(html.includes('Continue with Google'));
});
check('Files and notes remain visible without simulated success', () => {
  const h = harness(), html = h.p.filesComposer();
  clean(html); assert(html.includes('data-materials="sources"'), 'Live files and notes mount their durable service');
  h.p.handle('choose-files'); h.p.handle('write-note');
  assert.equal(h.p.ui.filePicker, false); assert.equal(h.p.ui.noteEditor, false);
  assert.deepEqual(h.calls, []); assert.deepEqual(h.state.files, {});
});
check('Questions use a neutral empty description', () => {
  const html = harness().p.requestsPanel(); clean(html);
  assert(html.includes('Questions from your Grove appear here.')); assert(!/caught up/i.test(html));
});
check('Unsupported upload pickers stay disabled with neutral descriptions', () => {
  const h = harness();
  for (const provider of ['notes', 'chats', 'grok']) {
    const html = h.p.exportPicker(provider); clean(html);
    disabled(html, 'data-action="pick-export"'); assert(!html.includes('data-pick-export='));
  }
  assert(h.p.exportPicker('chatgpt').includes('data-pick-export="chatgpt"'));
});
check('Muse directions and the account icon remain present', () => {
  const h = harness(); h.p.ui.host = 'muse'; const html = h.p.hostGuide(); clean(html);
  assert(html.includes('Automatic Muse Desktop connection guide')); assert(html.includes('Review access in Grove before approval.'));
  assert(html.includes('data-action="copy-mcp"')); assert(!html.includes('data-action="host-connect"'));
  h.p.handle('close');
  assert(h.document.getElementById('profile-button').innerHTML.includes('<circle cx="12" cy="8" r="3.5"/>'));
  assert.deepEqual(h.calls, []);
});
check('Delete brain keeps its disabled confirmation and cannot mutate', () => {
  const h = harness(); h.p.ui.confirm = { type: 'brain' };
  const html = h.p.confirmation(); clean(html);
  assert(html.includes('Delete your brain?')); assert(html.includes('data-action="cancel-confirm"'));
  assert(/<button[^>]*disabled[^>]*>Delete brain<\/button>/.test(html));
  const before = JSON.stringify(h.state); h.p.handle('confirm-delete');
  assert.equal(JSON.stringify(h.state), before); assert.deepEqual(h.calls, []);
});
check('Unknown source counts use a dash and retain failed import status', () => {
  const html = harness().p.sourceRow({ live: true, id: 'chatgpt', label: 'ChatGPT', outcome: 'failed', readCount: null });
  clean(html); assert(html.includes('Import failed')); assert(html.includes('>—</span>'));
});
check('Health retains real counts, neutral unknown metrics, and retry errors', () => {
  const h = harness(); h.p.setData({ health: { nodes: 7, edges: 3 }, sources: [], checkedAt: '12:00' });
  let html = h.p.health(); clean(html);
  assert(html.includes('data-live-count="nodes">7')); assert(html.includes('data-live-count="edges">3'));
  assert(html.includes('<span>Freshness</span><strong>—</strong>'));
  h.p.setData({ health: null, sources: null, pending: false }); html = h.p.health(); clean(html);
  assert(html.includes('data-live-count="nodes">—')); assert(html.includes('could not be read. Try again.'));
  assert(html.includes('data-action="refresh-live-data"'));
});
check('Guidance names its existing browser-only save', () => {
  const h = harness(), html = h.p.growthForm(h.state); clean(html);
  assert(html.includes('Save browser guidance')); assert(html.includes('saved on this browser'));
  const form = element(); form.elements = { focus: { value: 'Synthetic focus' }, reading: { value: '' }, avoid: { value: '' }, confirm: { checked: true } };
  h.p.modalRoot.querySelector = selector => selector === '[data-growth-form]' ? form : null;
  h.p.bind(); form.onsubmit({ preventDefault() {} });
  assert.equal(h.p.ui.notice, 'Saved on this browser'); assert.deepEqual(h.calls, ['browser-guidance']);
});
check('Upload failure stays an error with no added source or graph changes', async () => {
  const h = harness(), before = JSON.stringify(h.state);
  h.p.importSource('conversations', { name: 'synthetic-export.zip' }, 'chatgpt'); await settle();
  assert.deepEqual(h.calls, ['upload-request']); assert.equal(JSON.stringify(h.state), before);
  assert.match(h.p.liveUpload('chatgpt').error, /HTTP 503: Synthetic upload failed/);
  assert.equal(h.p.liveUpload('chatgpt').result, null);
  h.p.importSource('notes', { name: 'synthetic.txt' }, 'notes'); assert.deepEqual(h.calls, ['upload-request']);
});
check('Accepted upload retains exact counts and its actual graph outcome', () => {
  const html = harness().p.uploadResult({ state: 'taken_in', chats: 2, chats_new: 1, sources_added: 1, episodes_added: 3, messages_read: 8, messages_kept: 6, failed_chats: 0 });
  assert(html.includes('Import accepted')); assert(html.includes('Messages read: 8')); assert(html.includes('Messages kept: 6'));
  assert(html.includes('Your graph has not changed from this upload.'));
});
check('Worker rows retain actual recorded activity and disabled Follow', async () => {
  const h = harness(); h.load('agents.js'); h.live.workers = async () => ({ ok: true, body: workerPayload() });
  await h.box.GroveCare.refresh();
  const panel = h.document.body.children.find(node => node.id === 'grove-worker-panel');
  clean(panel.innerHTML); assert.equal((panel.innerHTML.match(/data-worker="/g) || []).length, 3);
  assert.equal((panel.innerHTML.match(/<button[^>]*disabled[^>]*>Follow<\/button>/g) || []).length, 3);
  assert(panel.innerHTML.includes('Run recorded as active')); assert(panel.innerHTML.includes('Learner · Failed'));
  assert(!panel.innerHTML.includes('hood-live-capability')); assert(!panel.innerHTML.includes('hood-live-policy'));
  assert.equal(h.box.GroveCare.summary().running, false); assert.equal(h.box.__hood.turns().length, 0);
  assert.equal(h.box.GroveCare.summary().workers.learner, 'Run recorded as active');
});
check('Worker errors retain Retry and malformed payloads stay refused', async () => {
  const h = harness(); h.load('agents.js');
  const payload = workerPayload(); payload.agents[0].policy.state = 'allowed';
  h.live.workers = async () => ({ ok: true, body: payload }); await h.box.GroveCare.refresh();
  const panel = h.document.body.children.find(node => node.id === 'grove-worker-panel');
  assert(panel.innerHTML.includes('could not be read. Try again.')); assert(panel.innerHTML.includes('>Retry</button>'));
  assert(!panel.innerHTML.includes('data-worker="learner"'));
});
check('Late worker response cannot cross an account change', async () => {
  const h = harness(); h.load('agents.js'); let resolve;
  h.live.workers = () => new Promise(done => { resolve = done; }); const pending = h.box.GroveCare.refresh();
  h.state.account = 'synthetic-next-account'; resolve({ ok: true, body: workerPayload() }); await pending;
  const panel = h.document.body.children.find(node => node.id === 'grove-worker-panel');
  assert(!panel.innerHTML.includes('Run recorded as active'));
});

function mountConnections(h, configured = true) {
  const root = element(), scope = { querySelectorAll: () => [root] };
  h.live.getJSON = async url => ({ ok: true, body: url.includes('/apps?') ? {
    account: h.state.account, configured, apps: [{ provider: 'synthetic', name: 'Synthetic app', auth_type: 'oauth', connectable: true }], next_cursor: null, total: 1,
  } : { account: h.state.account, configured, providers: [{ provider: 'synthetic', status: 'not_connected' }] } });
  h.load('pipedream.js'); h.box.GrovePipedream.mount(scope); return root;
}
check('Connections use neutral copy and keep disabled configuration guards', async () => {
  const h = harness(), root = mountConnections(h, false); await settle();
  clean(root.innerHTML); assert(root.innerHTML.includes('Connect your accounts with Pipedream.'));
  root.querySelector('[data-pipedream-select]').click(); await settle();
  disabled(root.innerHTML, 'data-pipedream-connect="synthetic"');
  root.querySelector('[data-pipedream-connect]').onclick(); await settle(); assert.deepEqual(h.calls, []);
});
check('Denied provider action remains an error without connection success', async () => {
  const h = harness(), root = mountConnections(h); await settle();
  root.querySelector('[data-pipedream-select]').click(); await settle();
  root.querySelector('[data-pipedream-connect]').click(); await settle();
  assert.deepEqual(h.calls, ['provider-request']);
  assert(root.innerHTML.includes('Connections are disabled for this prepared demo account'));
  assert(root.innerHTML.includes('Not connected')); assert(!root.innerHTML.includes('is-connected'));
});
check('A failed Gmail status read does not block a real authorization attempt', async () => {
  const h = harness(), root = mountConnections(h); await settle();
  h.live.getJSON = async () => ({ok:false,status:503,body:{error:{message:'Status read failed'}}});
  root.querySelector('[data-pipedream-select]').click(); await settle();
  assert.equal(root.querySelector('[data-pipedream-connect]').disabled, false);
  let opened;
  h.box.open = () => (opened = {closed:false,location:{},close(){this.closed=true;}});
  h.live.postJSON = async url => {
    h.calls.push(url);
    return {ok:true,body:{account:h.state.account,provider:'synthetic',
      connect_url:'https://pipedream.com/_static/connect.html?token=synthetic'}};
  };
  root.querySelector('[data-pipedream-connect]').click(); await settle();
  assert.deepEqual(h.calls, ['/v1/views/pipedream/synthetic/connect']);
  assert.equal(opened.location.href,'https://pipedream.com/_static/connect.html?token=synthetic');
  assert(!root.innerHTML.includes('is-connected'));
  h.live.getJSON = async () => ({ok:true,body:{account:h.state.account,configured:true,
    providers:[{provider:'synthetic',status:'connected'}]}});
  h.box.dispatchEvent({type:'focus'}); await settle();
  assert(root.innerHTML.includes('is-connected'));
});
check('Late connection response cannot cross an account change', async () => {
  const h = harness(), root = mountConnections(h); await settle(); let resolve;
  h.live.getJSON = () => new Promise(done => { resolve = done; });
  root.querySelector('[data-pipedream-refresh]').click();
  h.state.account = 'synthetic-next-account';
  h.box.dispatchEvent({ type: 'grove:change' });
  resolve({ ok: true, body: { account: 'synthetic-recording-account', configured: true, providers: [{ provider: 'synthetic', status: 'connected' }] } });
  await settle(); assert(!root.innerHTML.includes('is-connected')); assert(root.innerHTML.includes('Your account changed. Reload Grove.'));
});

(async () => {
  let failures = 0;
  for (const [name, run] of checks) {
    try { await run(); console.log('PASS: ' + name); }
    catch (error) { failures++; console.error('FAIL: ' + name + '\n' + error.stack); }
  }
  console.log(`Recording copy: ${checks.length - failures} passed, ${failures} failed`);
  if (failures) process.exitCode = 1;
})();
