// Files and notes of setup, with synthetic data only. The same-tab logout walks the real live.js,
// simulation.js, product.js and agent-files.js against a fake server. The slow list reads drive the
// real agent-files.js with answers the check sends by hand. No network, no browser, no build.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = path.join(__dirname, '..');
const read = name => fs.readFileSync(path.join(web, name), 'utf8');
const settle = async () => { for (let i = 0; i < 40; i++) await new Promise(done => setImmediate(done)); };
const ACCOUNT_A = 'synthetic-account-a', ACCOUNT_B = 'synthetic-account-b';
const PLAN = 'a-private-plan.pdf';

// A minimal DOM: attribute selectors find the tags of the current markup, as in recording-copy.cjs.
function element() {
  const attrs = {}, classes = new Set();
  let markup = '', cache = new Map();
  const node = {
    dataset: {}, style: { setProperty() {}, removeProperty() {} }, children: [], isConnected: true,
    hidden: false, textContent: '', scrollTop: 0, offsetWidth: 100, offsetHeight: 100, getContext: () => null,
    classList: {
      add: (...names) => names.forEach(name => classes.add(name)),
      remove: (...names) => names.forEach(name => classes.delete(name)),
      contains: name => classes.has(name),
      toggle(name, on) { if (on === undefined) on = !classes.has(name); on ? classes.add(name) : classes.delete(name); return on; },
    },
    setAttribute(name, value) { attrs[name] = String(value); },
    getAttribute: name => attrs[name] ?? null,
    hasAttribute: name => Object.hasOwn(attrs, name),
    removeAttribute(name) { delete attrs[name]; },
    appendChild(child) { node.children.push(child); return child; },
    addEventListener() {}, removeEventListener() {}, append() {}, remove() {},
    contains: () => false, focus() {}, click() {}, closest: () => null,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100 }),
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

// ---------- the real page: live.js, simulation.js, agent-files.js and product.js ----------
function makeServer() {
  const world = { session: false, signsInAs: ACCOUNT_A, account: null, lists: {}, holdList: null, failList: false };
  const reply = (status, body) => Promise.resolve({ ok: status >= 200 && status < 300, status, text: () => Promise.resolve(JSON.stringify(body)) });
  const typed = (status, code, message) => reply(status, { error: { code, message, details: {}, retryable: false } });
  const signedIn = () => ({ signed_in: true, self_item_id: null, has_items: false, account: world.account, method: 'email' });
  const calls = [];
  function fetch(url, init) {
    const u = new URL(url, 'http://grove.test/'), method = ((init && init.method) || 'GET').toUpperCase();
    calls.push(method + ' ' + u.pathname);
    if (u.pathname === '/v1/views/session') return world.session ? reply(200, signedIn()) : typed(401, 'unauthenticated', 'Sign in to continue');
    if (u.pathname === '/v1/auth/email/start') return reply(200, {});
    if (u.pathname === '/v1/auth/email/verify') { world.session = true; world.account = world.signsInAs; return reply(200, signedIn()); }
    if (u.pathname === '/v1/auth/logout') { world.session = false; world.account = null; return reply(200, {}); }
    if (u.pathname === '/v1/agent-files' && method === 'GET') {
      if (world.failList) return typed(503, 'unavailable', 'Synthetic list failure. Try again.');
      const answer = reply(200, { account: world.account, files: world.lists[world.account] || [] });
      return world.holdList ? new Promise(done => world.holdList.push(() => done(answer))) : answer;
    }
    return typed(404, 'not_found', 'No such path');
  }
  return { fetch, world, calls };
}
function realPage(server) {
  const created = [], listeners = {}, store = {};
  const createElement = () => { const node = element(); created.push(node); return node; };
  const ids = {};
  const document = {
    hidden: false, body: createElement(), documentElement: createElement(), createElement,
    getElementById: id => ids[id] || (ids[id] = createElement()),
    querySelector: () => null,
    querySelectorAll: selector => created.flatMap(node => node.querySelectorAll(selector)),
    addEventListener() {}, removeEventListener() {},
  };
  const box = {
    URL, URLSearchParams, Math, JSON, Date, Promise, MutationObserver: class { observe() {} },
    console: { log() {}, info() {}, warn() {}, error() {} },
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {}, requestAnimationFrame: () => 0, cancelAnimationFrame() {},
    performance: { now: () => 1000 }, innerWidth: 1440, innerHeight: 900, devicePixelRatio: 1, document, navigator: {},
    crypto: { randomUUID: () => 'synthetic-nonce' },
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    localStorage: { getItem: key => (key in store ? store[key] : null), setItem(key, value) { store[key] = String(value); }, removeItem(key) { delete store[key]; } },
    location: { href: 'http://grove.test/', search: '', pathname: '/', assign() {} },
    history: { replaceState() {} }, fetch: server.fetch,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
    dispatchEvent(event) { (listeners[event.type] || []).forEach(callback => callback(event)); return true; },
  };
  box.window = box; vm.createContext(box);
  const hook = '  window.productTest={ui,handle,verifyEmail,startEmail,render};render(true);';
  for (const name of ['live.js', 'simulation.js', 'provider-icons.js', 'app-screens.js', 'whatsapp-guide.js', 'agent-files.js', 'product.js']) {
    let source = read(name);
    if (name === 'product.js') { assert(source.includes('  render(true);maybeTour();')); source = source.replace('  render(true);maybeTour();', hook); }
    vm.runInContext(source, box, { filename: name });
  }
  return { box, p: box.productTest, document };
}
const painted = page => page.document.querySelectorAll('[data-agent-files]').map(root => root.innerHTML).join('');
const errorLine = page => page.document.querySelectorAll('[data-agent-error]').map(line => line.textContent).join('');
async function signIn(page, server, account) {
  server.world.signsInAs = account;
  page.p.startEmail('robin@example.test'); await settle();
  page.p.verifyEmail('246810'); await settle();
  page.p.ui.step = 3; page.p.render(true); await settle();
}
async function accountAListed() {
  const server = makeServer(), page = realPage(server);
  server.world.lists[ACCOUNT_A] = [{ name: PLAN, kind: 'file', bytes: 7 }];
  await settle();
  await signIn(page, server, ACCOUNT_A);
  assert(painted(page).includes(PLAN), 'account A sees its own file first');
  return { server, page };
}
const logOut = async page => { page.p.handle('logout'); await settle(); };

// ---------- the real agent-files.js with answers sent by hand ----------
function held() {
  const list = [];
  const call = args => new Promise((resolve, reject) => list.push({ args, resolve, reject }));
  return { list, call };
}
function files(account = ACCOUNT_A) {
  const listeners = {}, state = { auth: { provider: 'email' }, account };
  const fileRoot = element(), noteRoot = element(), errorRow = element();
  fileRoot.dataset.agentFiles = 'file'; noteRoot.dataset.agentFiles = 'note';
  const document = { querySelectorAll: selector => selector === '[data-agent-files]' ? [fileRoot, noteRoot] : selector === '[data-agent-error]' ? [errorRow] : [] };
  const lists = held(), adds = held(), notes = held(), removes = held();
  const box = {
    console, Promise, document, GroveIcons: {}, GroveSimulation: { getState: () => state },
    GroveLive: {
      agentFiles: () => lists.call([]), addAgentFile: file => adds.call([file]), addAgentNote: (title, body) => notes.call([title, body]),
      removeAgentFile: (kind, name) => removes.call([kind, name]), errorText: (body, fallback) => fallback,
    },
    addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
    dispatchEvent(event) { (listeners[event.type] || []).forEach(callback => callback(event)); },
  };
  box.window = box; vm.runInNewContext(read('agent-files.js'), box);
  const scope = { querySelector: selector => selector === '[data-agent-files]' ? fileRoot : null };
  return {
    box, state, lists, adds, notes, removes, api: box.GroveAgentFiles, errorRow,
    mount: () => box.GroveAgentFiles.mount(scope),
    html: () => fileRoot.innerHTML + noteRoot.innerHTML,
    change: () => box.dispatchEvent({ type: 'grove:change' }),
    answer: async (call, body) => { call.resolve({ ok: true, status: 200, body }); await settle(); },
    list: (rows, owner = state.account) => ({ account: owner, files: rows }),
  };
}
const row = (name, kind = 'file') => ({ name, kind, bytes: 7 });
async function listed(h, rows) { h.mount(); await h.answer(h.lists.list.at(-1), h.list(rows)); }

const checks = [];
const check = (name, run) => checks.push([name, run]);

check('Same-tab logout: no row of account A stays when the list read of account B fails', async () => {
  const { server, page } = await accountAListed();
  await logOut(page);
  server.world.failList = true;
  await signIn(page, server, ACCOUNT_B);
  assert(server.calls.filter(call => call === 'GET /v1/agent-files').length >= 2, 'account B read its list');
  assert.equal(page.box.GroveSimulation.getState().account, ACCOUNT_B);
  assert(!painted(page).includes(PLAN), 'a row of account A stayed for account B');
  assert.match(errorLine(page), /Synthetic list failure/);
});
check('Same-tab logout: a late list answer of account A cannot repaint for account B', async () => {
  const { server, page } = await accountAListed();
  server.world.holdList = [];
  page.p.render(true); await settle();
  const late = server.world.holdList.slice();
  assert(late.length >= 1, 'a list read of account A is on its way');
  await logOut(page);
  server.world.holdList = null;
  server.world.failList = true;
  await signIn(page, server, ACCOUNT_B);
  late.forEach(send => send()); await settle();
  assert(!painted(page).includes(PLAN), 'a late answer of account A repainted its row');
  assert.match(errorLine(page), /Synthetic list failure/);
});
check('Same-tab logout: account B reads and shows only its own rows', async () => {
  const { server, page } = await accountAListed();
  await logOut(page);
  assert(!painted(page).includes(PLAN), 'the row left at the logout');
  server.world.lists[ACCOUNT_B] = [{ name: 'b-garden-notes.pdf', kind: 'file', bytes: 7 }];
  await signIn(page, server, ACCOUNT_B);
  assert(painted(page).includes('b-garden-notes.pdf'));
  assert(!painted(page).includes(PLAN));
});
check('product.js resets the files at each place where it resets the materials', async () => {
  const { server, page } = await accountAListed();
  let resets = 0;
  const reset = page.box.GroveAgentFiles.reset;
  assert.equal(typeof reset, 'function', 'GroveAgentFiles.reset exists');
  page.box.GroveAgentFiles.reset = () => { resets++; reset(); };
  await logOut(page);
  assert.equal(resets, 2, 'logout and endSession each reset the files');
  resets = 0;
  await signIn(page, server, ACCOUNT_B);
  assert.equal(resets, 1, 'a sign-in as another account resets the files');
  resets = 0;
  page.box.dispatchEvent({ type: 'pagehide' });
  assert.equal(resets, 1, 'pagehide resets the files');
  assert(!painted(page).includes(PLAN));
});
check('pagehide clears the rows, so a page kept by the browser shows none', async () => {
  const { page } = await accountAListed();
  page.box.dispatchEvent({ type: 'pagehide' });
  assert(!painted(page).includes(PLAN), 'a row stayed after pagehide');
});
check('live.js marks a list read as stale when the session changed on its way', async () => {
  const server = makeServer(), page = realPage(server);
  server.world.session = true; server.world.account = ACCOUNT_A;
  await settle();
  server.world.holdList = [];
  const pending = page.box.GroveLive.agentFiles();
  await settle();
  page.box.GroveLive.invalidateSession();
  server.world.holdList.forEach(send => send());
  const res = await pending;
  assert.equal(res.stale, true);
  assert.equal(res.ok, false);
  assert.equal(res.body, null);
});
check('reset clears every row and an answer on its way stays out', async () => {
  const h = files();
  await listed(h, [row(PLAN)]);
  h.mount();
  h.api.add([{ name: 'late-upload.pdf', size: 7 }]); await settle();
  assert(h.html().includes('late-upload.pdf'));
  h.api.reset();
  assert.equal(h.html().includes(PLAN) || h.html().includes('late-upload.pdf'), false, 'reset cleared the painted rows');
  await h.answer(h.lists.list.at(-1), h.list([row(PLAN)]));
  await h.answer(h.adds.list.at(-1), { account: ACCOUNT_A, file: row('late-upload.pdf') });
  assert(!h.html().includes(PLAN) && !h.html().includes('late-upload.pdf'));
});
check('reset frees the queue, so a held upload of one account holds up no upload of the next', async () => {
  const h = files();
  await listed(h, [row(PLAN)]);
  h.api.add([{ name: 'slow-upload.pdf', size: 7 }]); await settle();
  assert.equal(h.adds.list.length, 1, 'the first upload is on its way');
  h.api.reset();
  h.state.account = ACCOUNT_B;
  h.api.add([{ name: 'first-of-b.pdf', size: 7 }]); await settle();
  assert.equal(h.adds.list.length, 2, 'the upload of the next account starts at once');
  assert.equal(h.adds.list[1].args[0].name, 'first-of-b.pdf');
});
check('An account change drops the rows with no reset call', async () => {
  const h = files();
  await listed(h, [row(PLAN)]);
  assert(h.html().includes(PLAN));
  h.state.account = ACCOUNT_B; h.change();
  assert(!h.html().includes(PLAN), 'rows of the old account stayed');
});
check('A mount for another account starts with no rows, with no event and no reset call', async () => {
  const h = files();
  await listed(h, [row(PLAN)]);
  h.state.account = ACCOUNT_B;
  h.mount();
  assert(!h.html().includes(PLAN));
  h.lists.list.at(-1).resolve({ ok: false, status: 503, body: null }); await settle();
  assert(!h.html().includes(PLAN));
  assert.equal(h.errorRow.textContent, 'Your files could not load. Try again.');
});
check('A late list answer cannot cross an account change with no event', async () => {
  const h = files();
  h.mount();
  h.state.account = ACCOUNT_B;
  await h.answer(h.lists.list.at(-1), h.list([row(PLAN)], ACCOUNT_A));
  assert(!h.html().includes(PLAN));
});
check('A list answer for another account is refused', async () => {
  const h = files();
  await listed(h, [row('own-file.pdf')]);
  h.mount();
  await h.answer(h.lists.list.at(-1), h.list([row(PLAN)], ACCOUNT_B));
  assert(!h.html().includes(PLAN), 'a row of another account was shown');
  assert(h.html().includes('own-file.pdf'));
  assert.equal(h.errorRow.textContent, 'Your files could not load. Try again.');
});
check('A write answer for another account is refused', async () => {
  const h = files();
  await listed(h, [row('own-file.pdf')]);
  h.api.add([{ name: PLAN, size: 7 }]); await settle();
  await h.answer(h.adds.list.at(-1), { account: ACCOUNT_B, file: row(PLAN) });
  assert(!h.html().includes(PLAN));
  const note = h.api.addNote('Plan', 'Body');
  await h.answer(h.notes.list.at(-1), { account: ACCOUNT_B, file: row('Plan.md', 'note') });
  assert.equal(await note, false);
  assert(!h.html().includes('Plan.md'));
  h.api.remove('file', 'own-file.pdf');
  await h.answer(h.removes.list.at(-1), { account: ACCOUNT_B, removed: 'own-file.pdf' });
  assert(h.html().includes('own-file.pdf'), 'a remove answer of another account took a row away');
});
check('A nobody-signed-in mount reads nothing', async () => {
  const h = files();
  h.state.auth = null;
  h.mount(); await settle();
  assert.equal(h.lists.list.length, 0);
});
check('Slow list: a file added during the read stays, and the list is read again', async () => {
  const h = files();
  h.mount();
  const slow = h.lists.list.at(-1);
  h.api.add([{ name: 'new-upload.pdf', size: 7 }]); await settle();
  await h.answer(h.adds.list.at(-1), { account: ACCOUNT_A, file: row('new-upload.pdf') });
  assert(h.html().includes('new-upload.pdf'));
  await h.answer(slow, h.list([]));
  assert(h.html().includes('new-upload.pdf'), 'the slow list erased the new row');
  assert.equal(h.lists.list.length, 2, 'the list is read again');
  await h.answer(h.lists.list.at(-1), h.list([row('new-upload.pdf')]));
  assert.equal((h.html().match(/new-upload\.pdf<\/strong>/g) || []).length, 1);
});
check('Slow list: a note added during the read stays', async () => {
  const h = files();
  h.mount();
  const slow = h.lists.list.at(-1);
  const note = h.api.addNote('New note', 'Body');
  await h.answer(h.notes.list.at(-1), { account: ACCOUNT_A, file: row('New note.md', 'note') });
  assert.equal(await note, true);
  await h.answer(slow, h.list([]));
  assert(h.html().includes('New note.md'), 'the slow list erased the new note');
});
check('Slow list: a file removed during the read does not come back', async () => {
  const h = files();
  await listed(h, [row('old-file.pdf')]);
  h.mount();
  const slow = h.lists.list.at(-1);
  h.api.remove('file', 'old-file.pdf');
  await h.answer(h.removes.list.at(-1), { account: ACCOUNT_A, removed: 'old-file.pdf' });
  await h.answer(slow, h.list([row('old-file.pdf')]));
  assert(!h.html().includes('old-file.pdf'), 'the slow list brought a removed row back');
  await h.answer(h.lists.list.at(-1), h.list([]));
  assert(!h.html().includes('old-file.pdf'));
});
check('Slow list: an add in flight stays visible when the read lands', async () => {
  const h = files();
  h.mount();
  const slow = h.lists.list.at(-1);
  h.api.add([{ name: 'on-its-way.pdf', size: 7 }]); await settle();
  await h.answer(slow, h.list([row('old-file.pdf')]));
  assert(h.html().includes('old-file.pdf'));
  assert(h.html().includes('on-its-way.pdf') && h.html().includes('Adding'), 'the add in flight left the screen');
  await h.answer(h.adds.list.at(-1), { account: ACCOUNT_A, file: row('on-its-way.pdf') });
  assert(h.html().includes('on-its-way.pdf') && !h.html().includes('Adding'));
});
check('Slow list: an older read cannot overwrite a newer one', async () => {
  const h = files();
  h.mount();
  const older = h.lists.list.at(-1);
  h.mount();
  await h.answer(h.lists.list.at(-1), h.list([row('newer-file.pdf')]));
  await h.answer(older, h.list([row('older-file.pdf')]));
  assert(h.html().includes('newer-file.pdf'));
  assert(!h.html().includes('older-file.pdf'), 'an older read overwrote a newer one');
});
check('Slow list: a read that live.js calls stale reads again and shows no error', async () => {
  const h = files();
  h.mount();
  h.lists.list.at(-1).resolve({ ok: false, status: 409, stale: true, body: null }); await settle();
  assert.equal(h.errorRow.textContent, '');
  assert.equal(h.lists.list.length, 2);
  await h.answer(h.lists.list.at(-1), h.list([row('own-file.pdf')]));
  assert(h.html().includes('own-file.pdf'));
});

(async () => {
  let failures = 0;
  for (const [name, run] of checks) {
    try { await run(); console.log('PASS: ' + name); }
    catch (error) { failures++; console.error('FAIL: ' + name + '\n' + error.stack); }
  }
  if (failures) { console.error(failures + ' of ' + checks.length + ' agent file checks failed'); process.exit(1); }
  console.log('All ' + checks.length + ' agent file checks passed');
})();
