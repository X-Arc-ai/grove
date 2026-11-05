// A search reads the status of what it found, so a connected app shows as connected and can reach
// Disconnect. The real pipedream.js runs in a vm with answers the check sends by hand. No network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../pipedream.js'), 'utf8');
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(done => setImmediate(done)); };
const ACCOUNT_A = 'synthetic-account-a', ACCOUNT_B = 'synthetic-account-b';
const app = (provider, name) => ({ provider, name, auth_type: 'oauth', connectable: true, category: 'Synthetic' });
const AIRTABLE = app('airtable_oauth', 'Airtable');
const TRELLO = app('trello', 'Trello');
const statusUrl = provider => '/v1/views/pipedream/accounts?provider=' + provider;

// A minimal DOM: attribute selectors find the tags of the current markup, as in recording-copy.cjs.
function element() {
  const attrs = {};
  let markup = '', cache = new Map();
  const node = {
    dataset: {}, isConnected: true, disabled: false, focused: 0,
    setAttribute(name, value) { attrs[name] = String(value); },
    getAttribute: name => attrs[name] ?? null,
    hasAttribute: name => Object.hasOwn(attrs, name),
    contains: () => false, addEventListener() {}, setSelectionRange() {},
    focus() { node.focused++; },
    click() { if (!node.disabled && node.onclick) node.onclick(); },
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

function harness({ refresh = 'answer' } = {}) {
  const listeners = {}, state = { auth: { provider: 'email' }, account: ACCOUNT_A };
  const reads = [], posts = [], tabs = [], timers = [];
  const hold = url => new Promise((resolve, reject) => reads.push({ url, resolve, reject }));
  const box = {
    console, Promise, URL, document: { activeElement: null }, GroveIcons: {}, GroveSimulation: { getState: () => state },
    GroveLive: {
      getJSON(url) {
        if (url === '/v1/views/pipedream/accounts') {
          if (refresh === 'fail') return Promise.resolve({ ok: false, status: 503, body: null });
          return Promise.resolve({ ok: true, status: 200, body: { account: state.account, configured: true, providers: [] } });
        }
        return hold(url);
      },
      postJSON(url, body) { return new Promise((resolve, reject) => posts.push({ url, body, resolve, reject })); },
    },
    setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout() {},
    open() { const tab = { closed: false, opener: 'page', location: {}, close() { this.closed = true; } }; tabs.push(tab); return box.blockTabs ? null : tab; },
    addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
    dispatchEvent(event) { (listeners[event.type] || []).forEach(callback => callback(event)); },
  };
  box.window = box; vm.runInNewContext(source, box);
  const root = element();
  root.dataset.pipedreamApps = 'true';
  box.GrovePipedream.mount({ querySelectorAll: () => [root] });
  const h = {
    box, state, root, reads, posts, tabs, timers,
    rowReads: () => reads.filter(read => read.url.includes('/accounts?provider=')),
    pick: provider => root.querySelector('[data-pipedream-select="' + provider + '"]'),
    status: (provider, value, owner = state.account) => ({ ok: true, status: 200, body: { account: owner, configured: true, providers: [{ provider, name: provider, status: value }] } }),
    async answer(url, res) {
      const index = reads.findIndex(read => read.url === url);
      assert.notEqual(index, -1, 'a read of ' + url + ' is on its way');
      reads.splice(index, 1)[0].resolve(res); await settle();
    },
    // A search as the input sends it, up to the answer of the catalog.
    async search(text, apps) {
      root.querySelector('[data-pipedream-search]').oninput({ target: { value: text } });
      timers.pop()(); await settle();
      await h.answer('/v1/views/pipedream/apps?q=' + encodeURIComponent(text), { ok: true, status: 200, body: { account: state.account, configured: true, apps, total: apps.length, next_cursor: null } });
    },
    change() { box.dispatchEvent({ type: 'grove:change' }); },
    async connectAnswer(index, provider) {
      posts[index].resolve({ ok: true, status: 200, body: { account: state.account, provider, connect_url: 'https://pipedream.com/_static/connect.html?token=synthetic' } }); await settle();
    },
  };
  return h;
}
const connects = h => h.posts.filter(post => post.url.endsWith('/connect'));
const shownConnected = (h, provider) => h.pick(provider).getAttribute('class').includes('is-connected');

const checks = [];
const check = (name, run) => checks.push([name, run]);

check('A search reads the status of each found app, at most 8, and the reads do not cancel each other', async () => {
  const h = harness(); await settle();
  const apps = Array.from({ length: 10 }, (_, index) => app('synthetic_app_' + index, 'Synthetic app ' + index));
  await h.search('synthetic', apps);
  assert.equal(h.rowReads().length, 8, 'one status read for each of the 8 rows, all on their way together');
  for (let index = 7; index >= 0; index--) await h.answer(statusUrl('synthetic_app_' + index), h.status('synthetic_app_' + index, index % 2 ? 'connected' : 'not_connected'));
  for (let index = 0; index < 8; index++) assert.equal(shownConnected(h, 'synthetic_app_' + index), index % 2 === 1, 'row ' + index);
});
check('A connected result shows connected, opens Disconnect, and starts no connect', async () => {
  const h = harness(); await settle();
  await h.search('airtable', [AIRTABLE]);
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected'));
  const button = h.pick('airtable_oauth');
  assert(shownConnected(h, 'airtable_oauth'));
  assert.equal(button.getAttribute('aria-label'), 'Connected to Airtable');
  assert(h.root.innerHTML.includes('✓'));
  button.click(); await settle();
  assert.equal(h.tabs.length, 0, 'no tab opens for a connected app');
  assert.equal(h.posts.length, 0, 'no connect starts for a connected app');
  assert(h.root.innerHTML.includes('Disconnect Airtable?'));
  h.root.querySelector('[data-pipedream-confirm]').click(); await settle();
  assert.equal(h.posts[0].url, '/v1/views/pipedream/airtable_oauth/disconnect');
  assert.equal(JSON.stringify(h.posts[0].body), '{"confirm":true}');
  h.posts[0].resolve({ ok: true, status: 200, body: { account: ACCOUNT_A, provider: 'airtable_oauth', status: 'not_connected' } }); await settle();
  assert(!shownConnected(h, 'airtable_oauth'));
});
check('A click before the status arrives reads it first and starts no second connect', async () => {
  const h = harness(); await settle();
  await h.search('airtable', [AIRTABLE]);
  h.pick('airtable_oauth').click(); await settle();
  assert.equal(connects(h).length, 0, 'no connect starts while the status is unknown');
  assert.equal(h.tabs.length, 1, 'the tab opens inside the click');
  assert.equal(h.rowReads().length, 2, 'the click reads the status itself');
  assert(h.pick('airtable_oauth').disabled, 'the rows wait for the read');
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected'));
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected'));
  assert.equal(connects(h).length, 0, 'a second connect started for a connected app');
  assert.equal(h.tabs[0].closed, true, 'the tab closes again');
  assert(h.root.innerHTML.includes('Disconnect Airtable?'));
  const cancel = h.root.querySelector('[data-pipedream-cancel]');
  assert(cancel.focused >= 1, 'the focus moves to the dialog');
  h.box.dispatchEvent({ type: 'focus' }); await settle();
  assert.equal(h.root.querySelector('[data-pipedream-cancel]'), cancel, 'the closed tab gave the focus back and the dialog lost it');
});
check('A click before the status arrives connects an app that is not connected, in the same tab', async () => {
  const h = harness(); await settle();
  await h.search('trello', [TRELLO]);
  h.pick('trello').click(); await settle();
  assert.equal(connects(h).length, 0);
  while (h.rowReads().length) await h.answer(statusUrl('trello'), h.status('trello', 'not_connected'));
  assert.equal(connects(h).length, 1);
  assert.equal(h.tabs.length, 1, 'one tab only');
  await h.connectAnswer(0, 'trello');
  assert.equal(h.tabs[0].location.href, 'https://pipedream.com/_static/connect.html?token=synthetic');
  assert.equal(h.tabs[0].closed, false);
  assert.equal(h.tabs[0].opener, null);
});
check('A second click while the status is on its way opens no second tab', async () => {
  const h = harness(); await settle();
  await h.search('trello', [TRELLO]);
  const button = h.pick('trello');
  button.click(); button.onclick(); await settle();
  assert.equal(h.tabs.length, 1);
  assert.equal(h.rowReads().length, 2, 'the search read and one click read');
});
check('A failed status read does not block connect', async () => {
  for (const fail of [read => read.resolve({ ok: false, status: 503, body: null }), read => read.reject(new Error('offline'))]) {
    const h = harness(); await settle();
    await h.search('trello', [TRELLO]);
    fail(h.reads.shift()); await settle();
    assert(!shownConnected(h, 'trello'));
    assert(!h.pick('trello').disabled, 'the row stays usable');
    h.pick('trello').click(); await settle();
    assert.equal(h.rowReads().length, 1, 'a row of unknown status reads it again on the click');
    fail(h.reads.shift()); await settle();
    assert.equal(connects(h).length, 1, 'connect must start after a failed read');
    await h.connectAnswer(0, 'trello');
    assert.equal(h.tabs[0].location.href, 'https://pipedream.com/_static/connect.html?token=synthetic');
  }
});
check('A status read of a fixed row that failed is read again on the click', async () => {
  const h = harness({ refresh: 'fail' }); await settle();
  h.pick('gmail').click(); await settle();
  assert.equal(connects(h).length, 0);
  await h.answer(statusUrl('gmail'), h.status('gmail', 'connected'));
  assert.equal(connects(h).length, 0, 'a second connect started for a connected fixed row');
  assert.equal(h.tabs[0].closed, true);
  assert(h.root.innerHTML.includes('Disconnect Gmail?'));
});
check('A blocked tab asks for popups and reads or starts nothing', async () => {
  const h = harness(); await settle();
  await h.search('trello', [TRELLO]);
  await h.answer(statusUrl('trello'), { ok: false, status: 503, body: null });
  h.box.blockTabs = true;
  h.pick('trello').click(); await settle();
  assert(h.root.innerHTML.includes('Allow popups for Grove, then try again.'));
  assert.equal(h.rowReads().length, 0);
  assert.equal(h.posts.length, 0);
  assert(!h.pick('trello').disabled, 'the rows stay usable');
});
check('A late status answer cannot cross an account change', async () => {
  const h = harness(); await settle();
  await h.search('airtable', [AIRTABLE]);
  h.state.account = ACCOUNT_B; h.change();
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected', ACCOUNT_A));
  assert(!h.root.innerHTML.includes('is-connected'));
  assert(h.root.innerHTML.includes('Your account changed. Reload Grove.'));
});
check('A click that waits for a status ends at an account change', async () => {
  const h = harness(); await settle();
  await h.search('trello', [TRELLO]);
  h.pick('trello').click(); await settle();
  h.state.account = ACCOUNT_B; h.change();
  while (h.rowReads().length) await h.answer(statusUrl('trello'), h.status('trello', 'not_connected', ACCOUNT_A));
  assert.equal(h.posts.length, 0, 'a connect started for the wrong account');
  assert.equal(h.tabs[0].closed, true);
});
check('A click that waits for a status ends at an account change with no event', async () => {
  for (const answer of ['not_connected', 'connected']) {
    const h = harness(); await settle();
    await h.search('trello', [TRELLO]);
    h.pick('trello').click(); await settle();
    h.state.account = ACCOUNT_B;
    while (h.rowReads().length) await h.answer(statusUrl('trello'), h.status('trello', answer, ACCOUNT_A));
    assert.equal(h.posts.length, 0, 'a connect started for the wrong account');
    assert.equal(h.tabs[0].closed, true, 'the tab of the old account stayed open');
    assert(!h.root.innerHTML.includes('Disconnect Trello?'));
  }
});
check('A late status answer cannot cross a newer search', async () => {
  const h = harness(); await settle();
  await h.search('air', [AIRTABLE]);
  await h.search('airt', [AIRTABLE]);
  assert.equal(h.rowReads().length, 2);
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected'));
  assert(!shownConnected(h, 'airtable_oauth'));
  await h.answer(statusUrl('airtable_oauth'), { ok: false, status: 503, body: null });
  assert(!shownConnected(h, 'airtable_oauth'), 'an answer of the older search crossed the newer one');
});
check('The status answer of the newest search counts', async () => {
  const h = harness(); await settle();
  await h.search('air', [AIRTABLE]);
  await h.search('airt', [AIRTABLE]);
  h.reads.shift().resolve({ ok: false, status: 503, body: null }); await settle();
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected'));
  assert(shownConnected(h, 'airtable_oauth'));
});
check('Text typed after a search ends its status reads at once', async () => {
  const h = harness(); await settle();
  await h.search('air', [AIRTABLE]);
  h.root.querySelector('[data-pipedream-search]').oninput({ target: { value: 'airt' } });
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected'));
  assert(!h.root.innerHTML.includes('is-connected'));
});
check('A status answer for another account is refused', async () => {
  const h = harness(); await settle();
  await h.search('airtable', [AIRTABLE]);
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected', ACCOUNT_B));
  assert(!shownConnected(h, 'airtable_oauth'));
});
check('A status read from before a disconnect cannot mark the row connected again', async () => {
  const h = harness(); await settle();
  await h.search('air', [AIRTABLE]);
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected'));
  await h.search('airt', [AIRTABLE]);
  h.pick('airtable_oauth').click(); await settle();
  h.root.querySelector('[data-pipedream-confirm]').click(); await settle();
  h.posts[0].resolve({ ok: true, status: 200, body: { account: ACCOUNT_A, provider: 'airtable_oauth', status: 'not_connected' } }); await settle();
  assert(!shownConnected(h, 'airtable_oauth'));
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected'));
  assert(!shownConnected(h, 'airtable_oauth'), 'a read from before the disconnect undid it');
  h.pick('airtable_oauth').click(); await settle();
  assert(!h.root.innerHTML.includes('Disconnect Airtable?'), 'the row still acts as connected');
  assert.equal(connects(h).length, 1);
});
check('A status read from before a connect cannot undo the read after it', async () => {
  const h = harness(); await settle();
  await h.search('air', [AIRTABLE]);
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'not_connected'));
  await h.search('airt', [AIRTABLE]);
  h.pick('airtable_oauth').click(); await settle();
  await h.connectAnswer(0, 'airtable_oauth');
  h.box.dispatchEvent({ type: 'focus' }); await settle();
  const late = h.reads.shift();
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected'));
  assert(shownConnected(h, 'airtable_oauth'));
  late.resolve(h.status('airtable_oauth', 'not_connected')); await settle();
  assert(shownConnected(h, 'airtable_oauth'), 'a read from before the connect showed + again');
});
check('A search answer that lands during a disconnect starts no status read', async () => {
  const h = harness(); await settle();
  await h.search('air', [AIRTABLE]);
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected'));
  h.root.querySelector('[data-pipedream-search]').oninput({ target: { value: 'airt' } });
  h.timers.pop()(); await settle();
  h.pick('airtable_oauth').click(); await settle();
  h.root.querySelector('[data-pipedream-confirm]').click(); await settle();
  await h.answer('/v1/views/pipedream/apps?q=airt', { ok: true, status: 200, body: { account: ACCOUNT_A, configured: true, apps: [AIRTABLE], total: 1, next_cursor: null } });
  assert.equal(h.rowReads().length, 0, 'a status read began during the disconnect and can outlive it');
  h.posts[0].resolve({ ok: true, status: 200, body: { account: ACCOUNT_A, provider: 'airtable_oauth', status: 'not_connected' } }); await settle();
  assert(!shownConnected(h, 'airtable_oauth'));
});
check('A status answer leaves an open Disconnect dialog as it is', async () => {
  const h = harness(); await settle();
  await h.search('air', [AIRTABLE, TRELLO]);
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected'));
  h.pick('airtable_oauth').click(); await settle();
  const cancel = h.root.querySelector('[data-pipedream-cancel]');
  await h.answer(statusUrl('trello'), h.status('trello', 'connected'));
  assert.equal(h.root.querySelector('[data-pipedream-cancel]'), cancel, 'the dialog was painted again and lost its focus');
  cancel.onclick(); await settle();
  assert(shownConnected(h, 'trello'), 'the answer shows when the dialog closes');
});
check('A status answer keeps the focus on the row that holds it', async () => {
  const h = harness(); await settle();
  await h.search('air', [AIRTABLE, TRELLO]);
  h.box.document.activeElement = h.pick('trello');
  h.root.contains = () => true;
  await h.answer(statusUrl('airtable_oauth'), h.status('airtable_oauth', 'connected'));
  assert.equal(h.pick('trello').focused, 1, 'the row lost the focus');
});

(async () => {
  let failures = 0;
  for (const [name, run] of checks) {
    try { await run(); console.log('PASS: ' + name); }
    catch (error) { failures++; console.error('FAIL: ' + name + '\n' + error.stack); }
  }
  if (failures) { console.error(failures + ' of ' + checks.length + ' search status checks failed'); process.exit(1); }
  console.log('All ' + checks.length + ' search status checks passed');
})();
