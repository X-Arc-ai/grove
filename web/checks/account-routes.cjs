// Focused account transport and destination controls. No server or real identity.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../live.js'), 'utf8');
function page(route, search = '') {
  const listeners = {}, calls = [], signals = [], assigned = [];
  const box = {URL, URLSearchParams, console, Promise, JSON,
    location: {pathname: route, search, href: 'https://grove.example' + route + search, assign: p => assigned.push(p)},
    crypto: {randomUUID: () => 'synthetic-nonce'},
    localStorage: {setItem: (key, value) => signals.push([key, value])},
    addEventListener: (name, fn) => {listeners[name] = fn;},
    dispatchEvent: event => {signals.push(event.type);},
    CustomEvent: class {constructor(type) {this.type = type;}},
    fetch: (url, init) => new Promise(resolve => calls.push({url, init, resolve})),
  };
  box.window = box; vm.createContext(box); vm.runInContext(source, box);
  const answer = (index, status, body) => calls[index].resolve({ok: status === 200, status, text: async () => JSON.stringify(body)});
  return {box, live: box.GroveLive, calls, signals, assigned, listeners, answer};
}
(async () => {
  const next = '/authorize?client_id=synthetic&scope=grove.read';
  for (const route of ['/login', '/signup', '/signin']) {
    const p = page(route, '?next=' + encodeURIComponent(next));
    assert.equal(p.live.returnPath(), next);
    assert.equal(p.live.authPagePath('/signup'), '/signup?next=' + encodeURIComponent(next));
    p.live.googleSignIn();
    assert.equal(p.assigned[0], '/v1/auth/google/start?next=' + encodeURIComponent(next));
  }
  for (const next of ['//outside.example', '/\\outside.example', '/authorize?x=\n', '/other', '/authorize?' + 'a'.repeat(2048)]) {
    assert.equal(page('/signup', '?next=' + encodeURIComponent(next)).live.returnPath(), null);
  }
  assert.equal(page('/logout', '?next=' + encodeURIComponent(next)).live.returnPath(), null);
  {
    const p = page('/login');
    const pending = p.live.session(); p.live.invalidateSession();
    p.answer(0, 200, {signed_in: true, account: 'old-account'});
    assert.equal((await pending).stale, true, 'old session success must not restore a logged-out account');
  }
  {
    const p = page('/signup');
    const pending = p.live.emailVerify('maya@example.test', '123456'); p.live.invalidateSession();
    p.answer(0, 200, {signed_in: true, account: 'old-account'});
    assert.equal((await pending).stale, true, 'late verification must not restore an old UI session');
    assert.equal(p.signals.length, 0);
  }
  {
    const p = page('/logout'); const pending = p.live.logout();
    assert.equal(p.calls[0].url, '/v1/auth/logout');
    assert.equal(p.calls[0].init.method, 'POST');
    assert.equal(p.calls[0].init.credentials, 'same-origin');
    p.answer(0, 200, {}); await pending;
    assert.equal(p.signals[0][0], 'grove-auth-change');
    assert.deepEqual(JSON.parse(p.signals[0][1]), {nonce: 'synthetic-nonce'});
    p.listeners.storage({key: 'grove-auth-change', newValue: '{}'});
    assert.equal(p.signals[1], 'grove:session-changed');
  }
  console.log('PASS account routes: direct destinations, hostile next values, stale success, POST logout, private cross-tab signal');
})().catch(error => {console.error(error); process.exitCode = 1;});
