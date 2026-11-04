'use strict';
// Focused auth regressions. Run only when tests are authorized.
// Real product auth functions, live transport and simulation state; synthetic HTTP only.
// No browser, build, third-party dependency or real email/account action.
// GROVE_EMAIL_CODE_WEB can select an unchanged source tree for a baseline control.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const web = process.env.GROVE_EMAIL_CODE_WEB || path.join(__dirname, '..');
const read = name => fs.readFileSync(path.join(web, name), 'utf8');
const source = read('product.js');
function between(startText, endText) {
  const start = source.indexOf(startText);
  const end = source.indexOf(endText, start + startText.length);
  assert(start >= 0 && end > start, 'Missing product source boundaries: ' + startText);
  return source.slice(start, end);
}
function sourceFunction(name) {
  return between('  function ' + name + '(', '\n  function ');
}
const authSource = [
  'accountMode', 'wordmark', 'esc', 'icon', 'signIn', 'signInEmail', 'setAddress',
  'arrive', 'finishSignIn', 'startEmail', 'verifyEmail', 'showSigninError',
  'sessionUnknown', 'checkSession', 'endSession', 'logout', 'handle',
].map(sourceFunction).join('\n');
// Use the real event handlers, including their invalidation and recheck order.
const lifecycle = between("    window.addEventListener('grove:session-changed'", '    readSigninError();');
const unauthenticated = between("  window.addEventListener('grove:unauthenticated'", "  window.addEventListener('grove:change'");
const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
const signedIn = account => ({ signed_in: true, account, method: 'email', has_items: false });
const MAIN = 'grove-minimal-codex-v3';
const checks = [];
const check = (name, run) => checks.push([name, run]);

function page(initialStore = {}) {
  const store = { ...initialStore }, listeners = {}, requests = [], classes = new Set();
  const counts = { clears: 0, renders: 0, invalidations: 0, notifications: 0 };
  const box = {
    console, URL, URLSearchParams, Date, Math, Promise,
    clearTimeout() {},
    setTimeout() { throw new Error('This auth regression must not start a timer'); },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    location: { pathname: '/login', href: 'https://grove.example.test/login', search: '' },
    history: { replaceState(_state, _title, url) { box.location.pathname = url; } },
    crypto: { randomUUID: () => 'synthetic-session-change' },
    localStorage: {
      getItem: key => store[key] || null,
      setItem: (key, value) => { store[key] = String(value); },
    },
    document: { body: { classList: {
      add: name => classes.add(name), remove: name => classes.delete(name),
    } } },
    addEventListener: (type, fn) => { (listeners[type] || (listeners[type] = [])).push(fn); },
    dispatchEvent: event => { for (const fn of listeners[event.type] || []) fn(event); },
    GroveView: { clear() { counts.clears++; } },
    fetch(url, init) {
      assert(['/v1/views/session', '/v1/auth/email/start', '/v1/auth/email/verify',
        '/v1/auth/logout', '/v1/views/health'].includes(url), 'Unexpected request: ' + url);
      assert.equal(init.credentials, 'same-origin');
      return new Promise((resolve, reject) => requests.push({
        url, init, reject,
        reply(status, body = {}) {
          resolve({ ok: status >= 200 && status < 300, status, text: () => Promise.resolve(JSON.stringify(body)) });
        },
      }));
    },
    ui: { authStage: 'checking', email: '', code: '', error: '', offline: false, step: 0 },
    icons: {}, authEpoch: 0, signinError: '', liveSessionReady: false,
    liveData: { owner: null }, liveUploads: {},
    endTour() {},
    setStep(step) { box.ui.step = step; box.render(true); },
    render() { counts.renders++; box.html = box.ui.authStage ? box.signIn() : ''; },
  };
  box.window = box;
  vm.createContext(box);
  vm.runInContext(read('live.js'), box, { filename: 'live.js', timeout: 1000 });
  vm.runInContext(read('simulation.js'), box, { filename: 'simulation.js', timeout: 1000 });
  box.api = box.GroveSimulation;
  box.live = box.GroveLive;
  for (const [name, counter] of [['invalidateSession', 'invalidations'], ['notifySessionChange', 'notifications']]) {
    const original = box.live[name];
    box.live[name] = (...args) => { counts[counter]++; return original(...args); };
  }
  vm.runInContext(between('  var SIGNIN_COPY =', '  var icons =') + authSource + '\n' + lifecycle + unauthenticated,
    box, { filename: 'product-auth.js', timeout: 1000 });
  function take(url) {
    const index = requests.findIndex(request => request.url === url);
    assert(index >= 0, 'Expected pending request: ' + url);
    return requests.splice(index, 1)[0];
  }
  return { box, counts, store, classes, requests, take,
    fire: (type, extra = {}) => box.dispatchEvent({ type, ...extra }),
  };
}

async function anonymous(store) {
  const p = page(store);
  const pending = p.box.checkSession();
  p.take('/v1/views/session').reply(401);
  await pending;
  await settle();
  assert.equal(p.box.ui.authStage, 'signin');
  return p;
}
async function send(p, email = 'synthetic@example.test') {
  const pending = p.box.startEmail(email);
  p.take('/v1/auth/email/start').reply(200);
  await pending;
  assert.equal(p.box.ui.authStage, 'code');
}
async function login(p, account = 'synthetic-account-a') {
  await send(p);
  const pending = p.box.verifyEmail('123456');
  p.take('/v1/auth/email/verify').reply(200, signedIn(account));
  await pending;
  assert.equal(p.box.api.getState().account, account);
  assert.equal(p.box.ui.authStage, null);
}
async function focus401(p) {
  p.fire('focus');
  p.take('/v1/views/session').reply(401);
  await settle();
}
function assertAnonymous(p) {
  assert.equal(p.box.api.getState().auth, null);
  assert.equal(p.box.api.getState().account, null);
}

check('Initial anonymous check still removes cached private state', async () => {
  const p = await anonymous({ [MAIN]: JSON.stringify({ version: 3, schema: 4,
    account: 'old-account', auth: null, name: 'Private name', growth: { focus: 'Private guidance' } }) });
  assertAnonymous(p);
  assert.equal(p.box.api.getState().name, '');
  assert.equal(p.box.api.getState().growth.focus, '');
  assert.doesNotMatch(p.store[MAIN], /Private/);
  assert(p.counts.clears > 0);
});

check('Focus 401 retains the code card, typed code and pending-request epochs', async () => {
  const p = await anonymous();
  await send(p);
  p.box.ui.code = '123';
  p.box.render();
  const before = { ...p.counts }, epoch = p.box.authEpoch, html = p.box.html;
  await focus401(p);
  await focus401(p);
  assert.equal(p.box.ui.authStage, 'code');
  assert.equal(p.box.ui.email, 'synthetic@example.test');
  assert.equal(p.box.ui.code, '123');
  assert.equal(p.box.html, html, 'No replacement of the active form');
  assert.match(p.box.html, /data-code-form/);
  assert.doesNotMatch(p.box.html, /data-email-form/);
  assert.equal(p.box.authEpoch, epoch);
  assert.deepEqual(p.counts, before, 'Anonymous focus does not clear, rerender or invalidate');
  assertAnonymous(p);
});

check('Focus 401 after email start begins does not cancel its success', async () => {
  const p = await anonymous(), pending = p.box.startEmail('pending@example.test');
  const request = p.take('/v1/auth/email/start'), epoch = p.box.authEpoch;
  await focus401(p);
  assert.equal(p.box.authEpoch, epoch);
  request.reply(200);
  await pending;
  assert.equal(p.box.ui.authStage, 'code');
  assert.equal(p.box.ui.email, 'pending@example.test');
});

check('Focus 401 after verification begins preserves the live transport response', async () => {
  const p = await anonymous();
  await send(p);
  const pending = p.box.verifyEmail('123456'), request = p.take('/v1/auth/email/verify');
  await focus401(p);
  request.reply(200, signedIn('synthetic-account-a'));
  await pending;
  assert.equal(p.box.api.getState().account, 'synthetic-account-a');
  assert.equal(p.box.api.getState().auth.provider, 'email');
  assert.equal(p.box.ui.authStage, null);
});

check('A late focus 401 cannot end a completed verification', async () => {
  const p = await anonymous();
  await send(p);
  const pending = p.box.verifyEmail('123456'), verify = p.take('/v1/auth/email/verify');
  p.fire('focus');
  const focus = p.take('/v1/views/session');
  verify.reply(200, signedIn('synthetic-account-a'));
  await pending;
  focus.reply(401);
  await settle();
  assert.equal(p.box.api.getState().account, 'synthetic-account-a');
  assert.equal(p.box.ui.authStage, null);
});

check('A session response from before email start cannot replace its code card', async () => {
  const p = await anonymous();
  p.fire('focus');
  const old = p.take('/v1/views/session');
  await send(p);
  old.reply(200, signedIn('superseded-account'));
  await settle();
  assert.equal(p.box.ui.authStage, 'code');
  assertAnonymous(p);
});

check('Explicit back cancels a pending email start', async () => {
  const p = await anonymous(), pending = p.box.startEmail('old@example.test');
  const request = p.take('/v1/auth/email/start');
  p.box.handle('auth-back');
  request.reply(200);
  await pending;
  assert.equal(p.box.ui.authStage, 'signin');
  assert.equal(p.box.ui.code, '');
  assert.match(p.box.html, /data-email-form/);
  assertAnonymous(p);
});

check('Explicit back cancels a pending verification in the UI', async () => {
  const p = await anonymous();
  await send(p);
  const pending = p.box.verifyEmail('123456'), request = p.take('/v1/auth/email/verify');
  p.box.handle('auth-back');
  request.reply(200, signedIn('superseded-account'));
  await pending;
  assert.equal(p.box.ui.authStage, 'signin');
  assertAnonymous(p);
});

check('A confirmed signed-in expiry clears account data and notifies other tabs', async () => {
  const p = await anonymous();
  await login(p);
  p.box.api.setName('Private name');
  p.box.api.saveGrowth({ focus: 'Private guidance' });
  p.box.ui.noteDraft = { title: 'Private', body: 'Private body' };
  const before = { ...p.counts };
  await focus401(p);
  assertAnonymous(p);
  assert.equal(p.box.ui.authStage, 'signin');
  assert.equal(p.box.api.getState().name, '');
  assert.equal(p.box.api.getState().growth.focus, '');
  assert.equal(p.box.ui.noteDraft.body, '');
  assert.doesNotMatch(p.store[MAIN], /Private/);
  assert(p.counts.invalidations > before.invalidations);
  assert(p.counts.notifications > before.notifications);
  assert(p.counts.clears > before.clears);
});

check('An anonymous card with a locally bound account still receives expiry cleanup', async () => {
  const p = await anonymous();
  await send(p);
  p.box.api.bindAccount('cached-account');
  p.box.api.setName('Cached private name');
  await focus401(p);
  assertAnonymous(p);
  assert.equal(p.box.api.getState().name, '');
  assert.equal(p.box.ui.authStage, 'signin');
});

check('A 503 or rejected session request does not expire a signed-in account', async () => {
  const p = await anonymous();
  await login(p);
  for (const rejected of [false, true]) {
    p.fire('focus');
    const request = p.take('/v1/views/session');
    if (rejected) request.reject(new Error('Synthetic offline response'));
    else request.reply(503);
    await settle();
    assert.equal(p.box.api.getState().account, 'synthetic-account-a');
    assert.equal(p.box.ui.offline, true);
  }
});

check('Logout keeps the account until confirmed, then clears it before the next account', async () => {
  const p = await anonymous();
  await login(p);
  p.box.api.setName('First private name');
  const pending = p.box.logout(), request = p.take('/v1/auth/logout');
  assert.equal(p.box.api.getState().account, 'synthetic-account-a');
  request.reply(200);
  await pending;
  assertAnonymous(p);
  await login(p, 'synthetic-account-b');
  assert.equal(p.box.api.getState().name, '');
  assert.doesNotMatch(p.store[MAIN], /First private name/);
});

check('Cross-tab change invalidates pending verification and checks the new account', async () => {
  const p = await anonymous();
  await send(p);
  const pending = p.box.verifyEmail('123456'), verify = p.take('/v1/auth/email/verify');
  p.fire('storage', { key: 'grove-auth-change', newValue: '{"nonce":"synthetic-other-tab"}' });
  assert.equal(p.box.ui.authStage, 'checking');
  const session = p.take('/v1/views/session');
  verify.reply(200, signedIn('superseded-account'));
  await pending;
  assertAnonymous(p);
  session.reply(200, signedIn('synthetic-account-b'));
  await settle();
  assert.equal(p.box.api.getState().account, 'synthetic-account-b');
  assert.equal(p.box.ui.authStage, null);
});

check('A live unauthenticated event still cancels pending email start', async () => {
  const p = await anonymous(), pending = p.box.startEmail('pending@example.test');
  const start = p.take('/v1/auth/email/start');
  p.fire('grove:unauthenticated');
  start.reply(200);
  await pending;
  assert.equal(p.box.ui.authStage, 'signin');
  assertAnonymous(p);
});

check('Browser cache restoration clears private state and requires a fresh session', async () => {
  const p = await anonymous();
  await login(p);
  p.box.api.setName('Private cached name');
  p.fire('pagehide');
  assert(p.classes.has('gp-session-hidden'));
  p.fire('pageshow', { persisted: true });
  assertAnonymous(p);
  assert.equal(p.box.ui.authStage, 'checking');
  assert.equal(p.box.api.getState().name, '');
  assert(!p.classes.has('gp-session-hidden'));
  p.take('/v1/views/session').reply(401);
  await settle();
  assert.equal(p.box.ui.authStage, 'signin');
});

check('Pagehide invalidates a pending verification before browser cache restoration', async () => {
  const p = await anonymous();
  await send(p);
  const pending = p.box.verifyEmail('123456'), verify = p.take('/v1/auth/email/verify');
  p.fire('pagehide');
  verify.reply(200, signedIn('superseded-account'));
  await pending;
  assertAnonymous(p);
  p.fire('pageshow', { persisted: true });
  p.take('/v1/views/session').reply(401);
  await settle();
  assert.equal(p.box.ui.authStage, 'signin');
});

(async () => {
  let failures = 0;
  for (const [name, run] of checks) {
    try { await run(); console.log('PASS: ' + name); }
    catch (error) { failures++; console.error('FAIL: ' + name + '\n' + error.stack); }
  }
  console.log(`Email code focus: ${checks.length - failures} passed, ${failures} failed`);
  if (failures) process.exitCode = 1;
})().catch(error => { console.error(error); process.exitCode = 1; });
