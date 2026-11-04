'use strict';
// Focused auth regressions.
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
  'arrive', 'finishSignIn', 'startEmail', 'verifyEmail', 'holdEmailCard', 'dropEmailCard',
  'heldEmailCard', 'showCard', 'showSigninError',
  'sessionUnknown', 'checkSession', 'endSession', 'logout', 'handle',
].map(sourceFunction).join('\n');
// Use the real event handlers, including their invalidation and recheck order.
const lifecycle = between("    window.addEventListener('grove:session-changed'", '    readSigninError();');
const unauthenticated = between("  window.addEventListener('grove:unauthenticated'", "  window.addEventListener('grove:change'");
const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
const signedIn = account => ({ signed_in: true, account, method: 'email', has_items: false });
const MAIN = 'grove-minimal-codex-v3';
const HELD = 'grove-email-code';
const CODE_LIFE_MS = 10 * 60 * 1000;
const checks = [];
const check = (name, run) => checks.push([name, run]);

// `tab` is this tab's session storage. Pass it on to open a new page load in the same tab.
function page(initialStore = {}, tab = {}) {
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
    sessionStorage: {
      getItem: key => (key in tab ? tab[key] : null),
      setItem: (key, value) => { tab[key] = String(value); },
      removeItem: key => { delete tab[key]; },
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
  return { box, counts, store, tab, classes, requests, take,
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
// A new page load in the same tab, after a reload or a trip to another page.
async function reopen(p) {
  const next = page(p.store, p.tab), pending = next.box.checkSession();
  next.take('/v1/views/session').reply(401);
  await pending;
  await settle();
  return next;
}
function held(email, age) { return JSON.stringify({ email, sent: Date.now() - age }); }
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
  assert.equal(p.box.ui.authStage, 'code', 'The cleanup removes private state, not the held card');
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
  assert.equal(p.box.ui.authStage, 'code');
  assertAnonymous(p);
});

check('A new page load in the same tab brings back the card, and its code signs in', async () => {
  const p = await anonymous();
  await send(p, 'held@example.test');
  const next = await reopen(p);
  assert.equal(next.box.ui.authStage, 'code');
  assert.equal(next.box.ui.email, 'held@example.test');
  assert.match(next.box.html, /data-code-form/);
  assert.match(next.box.html, /held@example\.test/);
  assertAnonymous(next);
  const pending = next.box.verifyEmail('123456'), verify = next.take('/v1/auth/email/verify');
  assert.equal(JSON.parse(verify.init.body).email, 'held@example.test');
  verify.reply(200, signedIn('synthetic-account-a'));
  await pending;
  assert.equal(next.box.api.getState().account, 'synthetic-account-a');
  assert.equal(next.box.ui.authStage, null);
  assert.deepEqual(next.tab, {}, 'A finished sign-in holds no card');
});

check('A graph 401 during a page load cannot lose the held card', async () => {
  // The order a real page load gives: a views call answers first and ends the session check.
  const p = await anonymous();
  await send(p, 'held@example.test');
  const next = page(p.store, p.tab), pending = next.box.checkSession();
  next.fire('grove:unauthenticated');
  next.take('/v1/views/session').reply(401);
  await pending;
  await settle();
  assert.equal(next.box.ui.authStage, 'code');
  assert.equal(next.box.ui.email, 'held@example.test');
  assert.match(next.box.html, /data-code-form/);
});

check('A session check that fails during a page load keeps the held card', async () => {
  const p = page({}, { [HELD]: held('held@example.test', 0) }), pending = p.box.checkSession();
  p.take('/v1/views/session').reply(503);
  await pending;
  await settle();
  assert.equal(p.box.ui.authStage, 'code');
  assert.equal(p.box.ui.email, 'held@example.test');
  assert.equal(p.box.ui.error, 'Grove could not be reached. Try again in a moment.');
});

check('The held card keeps the address and its time, never a code', async () => {
  const p = await anonymous();
  await send(p, 'held@example.test');
  p.box.ui.code = '654321';
  const pending = p.box.verifyEmail('654321');
  p.take('/v1/auth/email/verify').reply(401, { error: { code: 'unauthenticated' } });
  await pending;
  assert.equal(p.box.ui.authStage, 'code');
  assert.deepEqual(Object.keys(p.tab), [HELD]);
  assert.deepEqual(Object.keys(JSON.parse(p.tab[HELD])).sort(), ['email', 'sent']);
  assert.doesNotMatch(p.tab[HELD], /654321/);
  assert.doesNotMatch(p.store[MAIN] || '', /held@example|654321/);
});

check('Use another email drops the held card', async () => {
  const p = await anonymous();
  await send(p);
  p.box.handle('auth-back');
  assert.deepEqual(p.tab, {});
  const next = await reopen(p);
  assert.equal(next.box.ui.authStage, 'signin');
  assert.match(next.box.html, /data-email-form/);
});

check('Closing the card drops the held card', async () => {
  const p = await anonymous();
  await send(p);
  p.box.handle('auth-close');
  assert.deepEqual(p.tab, {});
});

check('The card comes back for the life of a code and no longer', async () => {
  const fresh = page({}, { [HELD]: held('late@example.test', CODE_LIFE_MS - 5000) });
  let pending = fresh.box.checkSession();
  fresh.take('/v1/views/session').reply(401);
  await pending;
  await settle();
  assert.equal(fresh.box.ui.authStage, 'code');
  const old = page({}, { [HELD]: held('late@example.test', CODE_LIFE_MS) });
  pending = old.box.checkSession();
  old.take('/v1/views/session').reply(401);
  await pending;
  await settle();
  assert.equal(old.box.ui.authStage, 'signin');
  assert.deepEqual(old.tab, {});
});

check('A held value that makes no card is dropped', async () => {
  const values = ['not json', 'null', '7', '{}', '{"email":5,"sent":1}',
    JSON.stringify({ email: '', sent: Date.now() }), held('future@example.test', -60000),
    JSON.stringify({ email: 'nosent@example.test' })];
  for (const value of values) {
    const p = page({}, { [HELD]: value }), pending = p.box.checkSession();
    p.take('/v1/views/session').reply(401);
    await pending;
    await settle();
    assert.equal(p.box.ui.authStage, 'signin', value);
    assert.deepEqual(p.tab, {}, value);
  }
});

check('A send that fails holds no card and drops an older one', async () => {
  const p = await anonymous();
  await send(p, 'first@example.test');
  p.box.handle('auth-back');
  p.tab[HELD] = held('first@example.test', 0);
  for (const status of [429, 503]) {
    const pending = p.box.startEmail('second@example.test');
    p.take('/v1/auth/email/start').reply(status);
    await pending;
    assert.equal(p.box.ui.authStage, 'signin');
    assert.deepEqual(p.tab, {});
  }
});

check('A cancelled send holds no card', async () => {
  const p = await anonymous(), pending = p.box.startEmail('old@example.test');
  const request = p.take('/v1/auth/email/start');
  p.box.handle('auth-back');
  request.reply(200);
  await pending;
  assert.deepEqual(p.tab, {});
});

check('A session found on load drops the held card', async () => {
  const p = page({}, { [HELD]: held('held@example.test', 0) }), pending = p.box.checkSession();
  p.take('/v1/views/session').reply(200, signedIn('synthetic-account-a'));
  await pending;
  await settle();
  assert.equal(p.box.api.getState().account, 'synthetic-account-a');
  assert.deepEqual(p.tab, {});
});

check('A blocked session storage leaves the card for the life of the page', async () => {
  const p = await anonymous();
  const blocked = () => { throw new Error('Synthetic blocked storage'); };
  p.box.sessionStorage = { getItem: blocked, setItem: blocked, removeItem: blocked };
  await send(p);
  await focus401(p);
  assert.equal(p.box.ui.authStage, 'code');
  p.fire('pageshow', { persisted: true });
  p.take('/v1/views/session').reply(401);
  await settle();
  assert.equal(p.box.ui.authStage, 'signin');
});

check('A return to the consent page drops the held card before it leaves', async () => {
  const p = await anonymous();
  await send(p);
  p.box.live.returnToNext = () => true;
  const pending = p.box.verifyEmail('123456');
  p.take('/v1/auth/email/verify').reply(200, signedIn('synthetic-account-a'));
  await pending;
  assert.deepEqual(p.tab, {});
});

check('Browser cache restoration brings back the code card', async () => {
  const p = await anonymous();
  await send(p, 'held@example.test');
  p.fire('pagehide');
  p.fire('pageshow', { persisted: true });
  assert.equal(p.box.ui.authStage, 'checking');
  p.take('/v1/views/session').reply(401);
  await settle();
  assert.equal(p.box.ui.authStage, 'code');
  assert.equal(p.box.ui.email, 'held@example.test');
  assertAnonymous(p);
});

check('A cross-tab signal keeps an anonymous code card', async () => {
  const p = await anonymous();
  await send(p, 'held@example.test');
  p.fire('storage', { key: 'grove-auth-change', newValue: '{"nonce":"synthetic-other-tab"}' });
  assert.equal(p.box.ui.authStage, 'checking');
  p.take('/v1/views/session').reply(401);
  await settle();
  assert.equal(p.box.ui.authStage, 'code');
  assert.equal(p.box.ui.email, 'held@example.test');
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
