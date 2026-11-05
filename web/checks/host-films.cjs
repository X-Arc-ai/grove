// Connection films and the live Health freshness line, with the real render functions.
// No network or browser. web/checks/host_films.py owns the moving picture and geometry.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = path.join(__dirname, '..');
const read = name => fs.readFileSync(path.join(web, name), 'utf8');
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const screenSource = (() => {
  const box = {};
  vm.createContext(box); box.window = box;
  vm.runInContext(read('app-screens.js'), box);
  return { css: box.GroveAppScreenCSS, screens: box.GroveAppScreens };
})();
const URL_TEXT = 'https://grove.example.test/mcp';

function element() {
  const attrs = {};
  return {
    innerHTML: '', dataset: {}, style: { setProperty() {} }, children: [], hidden: false, textContent: '',
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute(name, value) { attrs[name] = String(value); },
    getAttribute: name => attrs[name] ?? null,
    appendChild(child) { this.children.push(child); return child; },
    addEventListener() {}, focus() {}, closest: () => null, contains: () => false,
    querySelector: () => null, querySelectorAll: () => [],
  };
}

function harness({ reduced = false, screens = true, deferred = null, care = null } = {}) {
  const ids = new Map(), listeners = {};
  const state = {
    auth: { provider: 'email' }, account: 'synthetic-film-account', name: '', onboardingComplete: true,
    onboardingStep: 0, tourComplete: true, sources: [], files: {}, requests: [], accountImports: {},
    pendingExports: {}, connectedHosts: [], mcpClients: [], growth: { focus: '', reading: '', avoid: '', confirm: true },
    currentNodeCount: 0, currentEdgeCount: 0, nodeDelta: 0, edgeDelta: 0,
  };
  const api = { isLive: () => true, getState: () => state, pendingExportContext: () => state.account, isPendingExportContext: () => true, refreshPendingExports: () => Promise.resolve() };
  const live = {
    PAGES: { signup: '/signup', login: '/login', logout: '/logout', home: '/', privacy: '/privacy' },
    ENDPOINTS: { sourceCoverage: '/sources', sourceConnections: '/connections', health: '/health' },
    session: () => new Promise(() => {}), isAuthPage: () => false, authPagePath: page => page,
    mcpUrl: () => URL_TEXT, errorText: (body, fallback) => fallback,
    getJSON: async () => ({ ok: false, status: 503, body: {} }),
  };
  const document = {
    hidden: false, body: element(), documentElement: element(), createElement: element,
    getElementById(id) { if (!ids.has(id)) ids.set(id, element()); return ids.get(id); },
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
  };
  document.getElementById('agents-toggle').closest = () => element();
  const box = {
    console, URL, URLSearchParams, Promise, Date, JSON, Number, document,
    GroveSimulation: api, GroveLive: live, navigator: {}, innerWidth: 1440, innerHeight: 900,
    performance: { now: () => 0 }, location: { pathname: '/', search: '', href: 'https://grove.example.test/' },
    matchMedia: () => ({ matches: reduced }), setTimeout() {}, clearTimeout() {}, setInterval() {}, clearInterval() {},
    requestAnimationFrame() {}, CustomEvent: class { constructor(type) { this.type = type; } },
    addEventListener: (name, callback) => { (listeners[name] ||= []).push(callback); },
    dispatchEvent: event => (listeners[event.type] || []).forEach(callback => callback(event)),
  };
  box.window = box;
  if (screens) { box.GroveAppScreenCSS = screenSource.css; box.GroveAppScreens = screenSource.screens; }
  if (deferred) box.GroveDeferred = deferred;
  if (care) box.GroveCare = care;
  vm.createContext(box);
  vm.runInContext(read('provider-icons.js'), box, { filename: 'provider-icons.js' });
  const marker = '  render(true);maybeTour();', original = read('product.js');
  assert(original.includes(marker), 'The product test entry point exists');
  const hook = '  window.filmTest={ui,handle,hostGuide,guide,health,transferFilm,render,html:()=>modalRoot.innerHTML,root,setData(value){liveData=value;}};';
  vm.runInContext(original.replace(marker, hook), box, { filename: 'product.js' });
  box.filmTest.ui.authStage = null;
  return { box, p: box.filmTest };
}

// The film source, as the browser reads it from the srcdoc attribute.
function filmDoc(html) {
  const match = html.match(/srcdoc="([^"]*)"/);
  assert(match, 'The guide holds a film');
  return match[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}
const framesOf = doc => doc.split('<section class="film-frame"').slice(1);
const delays = doc => [...doc.matchAll(/class="film-frame" style="animation-delay:-([\d.]+)s"/g)].map(match => match[1]);
function hostHtml(h, host) { h.p.ui.host = host; return h.p.hostGuide(); }
const listed = html => [...html.match(/<ol class="gp-section-copy">(.*?)<\/ol>/)[1].matchAll(/<li>(.*?)<\/li>/g)].map(match => match[1]);
const readerSteps = html => [...html.matchAll(/<section data-guide-step="\d+"[^>]*>.*?<\/section>/g)].map(match => match[0]);

let failed = 0;
async function check(name, action) {
  try { await action(); console.log('PASS', name); } catch (error) { failed++; console.error('FAIL', name, '\n', error); }
}

(async () => {
  for (const host of ['chatgpt', 'claude']) {
    const label = host === 'chatgpt' ? 'ChatGPT' : 'Claude';
    await check(host + ' film uses its own app screens, then the Grove cards', () => {
      const html = hostHtml(harness(), host), doc = filmDoc(html), frames = framesOf(doc);
      assert.equal(frames.length, 4);
      assert(doc.includes('data-frame-count="4"'));
      assert(frames[0].includes(screenSource.screens[host + '-home']), 'Frame 1 is the home screen');
      assert(frames[1].includes(screenSource.screens[host + '-menu']), 'Frame 2 is the menu screen');
      const other = host === 'chatgpt' ? 'claude' : 'chatgpt';
      assert(!doc.includes(screenSource.screens[other + '-home']), 'No frame uses the other host');
      assert(!frames[0].includes('film-instruction') && !frames[1].includes('film-instruction'), 'Screen frames are not text cards');
      assert(frames[2].includes('Add the Grove server') && frames[2].includes('<div class="film-server">' + URL_TEXT + '</div>'), 'Frame 3 has the URL box');
      assert(frames[2].includes('03 / 04') && frames[3].includes('04 / 04'));
      assert(frames[3].includes('Review Grove access') && !frames[3].includes('film-server'));
      assert(doc.includes(screenSource.css), 'The film uses the real screen CSS');
      assert.deepEqual(listed(html), ['Open ' + label + ' and click your profile.', 'Open Settings, then the custom connector settings.', 'Add a remote MCP connector with the server URL below.', 'Sign in to Grove and review access. Return to ' + label + ' after approval.']);
    });
  }
  await check('Muse film opens on its transfer frame, then its three cards', () => {
    const h = harness(), html = hostHtml(h, 'muse'), frames = framesOf(filmDoc(html));
    assert.equal(frames.length, 4);
    assert(frames[0].includes(h.p.transferFilm('muse')), 'Frame 1 is the transfer film');
    assert(frames[1].includes('Open a Muse conversation') && frames[1].includes('02 / 04'));
    assert(frames[2].includes('Add the Grove server') && frames[2].includes('film-server'));
    assert(frames[3].includes('Review Grove access'));
    assert.equal(listed(html).length, 4);
  });
  await check('Grok keeps its written steps and has no film', () => {
    const html = hostHtml(harness(), 'grok');
    assert(!html.includes('<iframe') && !html.includes('film-toggle') && !html.includes('data-guide-reader'));
    assert.deepEqual(listed(html), ['Open the app’s custom connector settings.', 'Add a remote MCP connector with the server URL below.', 'Sign in to Grove and review access. Return to Grok after approval.']);
  });
  await check('Frame count, reader and listed steps match, at 3 seconds a frame', () => {
    for (const host of ['muse', 'chatgpt', 'claude']) {
      const html = hostHtml(harness(), host), doc = filmDoc(html);
      assert.deepEqual(delays(doc), ['12.00', '9.00', '6.00', '3.00'], host + ' delays');
      assert(doc.includes('data-frame-seconds="3"') && doc.includes('animation:filmFrame 12s'), host + ' timing');
      const steps = readerSteps(html);
      assert.equal(steps.length, framesOf(doc).length, host + ' reader count');
      assert.equal(listed(html).length, steps.length, host + ' listed count');
      steps.forEach((step, i) => {
        assert(step.includes('Step ' + (i + 1) + ' of 4'));
        assert(step.includes('<p>' + listed(html)[i] + '</p>'), host + ' reader step ' + i + ' matches the list');
      });
    }
  });
  await check('Every host film stays sandboxed, inert and script-free', () => {
    for (const host of ['muse', 'chatgpt', 'claude']) {
      const html = hostHtml(harness(), host), tag = html.match(/<iframe [^>]*?srcdoc=/)[0], doc = filmDoc(html);
      assert(/\bsandbox\b(?!=)/.test(tag) && !tag.includes('allow-'), host + ' sandbox');
      assert(/\binert\b/.test(tag) && tag.includes('tabindex="-1"') && tag.includes('aria-hidden="true"'), host + ' inert');
      assert(!/<script/i.test(doc), host + ' script');
      assert(!/<[a-z][^>]*\son[a-z]+=/i.test(doc), host + ' handler');
    }
  });
  await check('A film opened before the guide asset shows the notice, then the frames', async () => {
    let release, loads = 0;
    const h = harness({ screens: false, deferred: { load(name) { assert.equal(name, 'guides'); loads++; return new Promise(resolve => { release = resolve; }); } } });
    h.p.ui.host = 'chatgpt'; h.p.ui.panel = 'profile'; h.p.ui.settingsTab = 'connections';
    h.p.render(true);
    assert(h.p.html().includes('Loading your guide…'), 'The notice is visible');
    assert(!h.p.html().includes('<iframe'), 'No film without the screens');
    assert(h.p.html().includes('<li>Open ChatGPT and click your profile.</li>'), 'The written steps stay readable');
    assert.equal(loads, 1);
    h.p.render(true); assert.equal(loads, 1, 'One load while one is pending');
    h.box.GroveAppScreenCSS = screenSource.css; h.box.GroveAppScreens = screenSource.screens;
    release(); await settle();
    assert(!h.p.html().includes('Loading your guide…'), 'The asset re-renders the guide');
    assert.equal(framesOf(filmDoc(h.p.html())).length, 4);
    assert(framesOf(filmDoc(h.p.html()))[0].includes(screenSource.screens['chatgpt-home']));
  });
  await check('A failed guide asset offers a retry', async () => {
    const h = harness({ screens: false, deferred: { load: () => Promise.reject(new Error('Synthetic load failure')) } });
    h.p.ui.host = 'claude'; h.p.ui.panel = 'profile'; h.p.ui.settingsTab = 'connections';
    h.p.render(true); await settle();
    assert(h.p.html().includes('This view could not load. Try again.'));
    assert(h.p.html().includes('data-retry-asset="guides"'));
  });
  await check('Normal motion keeps the Pause control', () => {
    const html = hostHtml(harness(), 'chatgpt');
    assert(html.includes('data-action="film-toggle" aria-label="Pause guide">Ⅱ<'));
    assert(!html.includes('Next guide step'));
  });
  await check('Reduced motion reads Next step in host films', () => {
    for (const host of ['muse', 'chatgpt', 'claude']) {
      const html = hostHtml(harness({ reduced: true }), host);
      assert(html.includes('data-action="film-toggle" aria-label="Next guide step">Next step<'), host);
      assert(!html.includes('aria-label="Pause guide"'), host);
    }
  });

  // Reduced motion: a press shows the next frame and the matching reader step.
  function mounted(h, html) {
    const frame = element(), toggle = element(), sections = readerSteps(html).map((_, i) => { const s = element(); s.hidden = i > 0; return s; });
    frame.dataset.base = filmDoc(html); frame.dataset.started = '0';
    const reader = { querySelectorAll: () => sections };
    h.p.root.querySelector = selector => selector === '.gp-app-frame iframe' ? frame : selector === '[data-action=film-toggle]' ? toggle : selector === '[data-guide-reader]' ? reader : null;
    return { frame, toggle, sections };
  }
  const shown = doc => {
    const css = doc.match(/\.film-frame\{display:none!important\}\.film-frame:nth-child\((\d+)\)\{display:block!important;opacity:1!important\}<\/style>/);
    return css ? Number(css[1]) : 0;
  };
  await check('Each reduced-motion press steps a host film and its reader, then wraps', () => {
    const h = harness({ reduced: true }), html = hostHtml(h, 'chatgpt'), film = mounted(h, html);
    for (const expected of [2, 3, 4, 1, 2]) {
      h.p.handle('film-toggle');
      assert.equal(shown(film.frame.getAttribute('srcdoc')), expected, 'Frame ' + expected);
      film.sections.forEach((section, i) => assert.equal(section.hidden, i !== expected - 1, 'Reader follows frame ' + expected));
    }
    assert(!film.frame.dataset.paused, 'A press never pauses or plays');
    assert.notEqual(film.frame.getAttribute('srcdoc'), film.frame.dataset.base);
    assert(film.frame.getAttribute('srcdoc').includes('@media(prefers-reduced-motion:reduce){*{animation:none!important}'), 'Automatic motion stays off');
  });
  await check('Export films step with reader text under reduced motion only', () => {
    const still = harness({ reduced: true });
    still.p.ui.guide = 'chatgpt'; still.p.ui.guideStage = 0;
    const html = still.p.guide();
    assert(html.includes('data-action="film-toggle" aria-label="Next guide step">Next step<'));
    assert.equal(readerSteps(html).length, 4, 'Every export frame has reader text');
    assert(readerSteps(html)[0].includes('<h2>Open ChatGPT and click your profile</h2>'));
    const film = mounted(still, html);
    still.p.handle('film-toggle');
    assert.equal(shown(film.frame.getAttribute('srcdoc')), 2);
    assert.equal(film.sections[1].hidden, false);
    const moving = harness();
    moving.p.ui.guide = 'chatgpt'; moving.p.ui.guideStage = 0;
    const normal = moving.p.guide();
    assert(normal.includes('aria-label="Pause guide">Ⅱ<'));
    assert(!normal.includes('data-guide-reader'), 'Normal motion keeps the export film as it was');
  });
  await check('Normal motion still pauses and plays through the same control', () => {
    const h = harness(), html = hostHtml(h, 'claude'), film = mounted(h, html);
    h.p.handle('film-toggle');
    assert.equal(film.frame.dataset.paused, 'true');
    assert(film.frame.getAttribute('srcdoc').includes('*{animation-play-state:paused!important}'));
    assert.equal(film.toggle.getAttribute('aria-label'), 'Play guide');
    h.p.handle('film-toggle');
    assert.equal(film.frame.dataset.paused, 'false');
    assert.equal(film.toggle.getAttribute('aria-label'), 'Pause guide');
  });

  // The record below has the shape that GroveCare.summary() returns in live mode.
  const summary = freshness => ({ summary: () => ({ updated: null, connections: null, tended: null, fresh: null, areas: null, working: null, enabled: false, running: false, workers: {}, ...freshness }), refresh() {} });
  const healthHtml = freshness => { const h = harness({ care: summary(freshness) }); h.p.setData({ health: { nodes: 7, edges: 3 }, sources: [], checkedAt: '12:00' }); return h.p.health(); };
  await check('Health shows the fallback when the record is absent', () => {
    const html = healthHtml({});
    assert(html.includes('<span>Freshness</span><strong data-live-freshness>Not recorded</strong></div>'));
    assert(!html.includes('gp-freshness-track'));
  });
  await check('Health shows the recorded percent, bar and words', () => {
    const html = healthHtml({ freshness: { percent: 83, text: '5 of 6 items updated in the last 30 days.' } });
    assert(html.includes('<strong data-live-freshness>83%</strong>'));
    assert(html.includes('<div class="gp-freshness-track"><i style="width:83%"></i></div>'));
    assert(html.includes('>5 of 6 items updated in the last 30 days.</small>'));
  });
  await check('Health keeps a zero, and refuses a value outside the record shape', () => {
    assert(healthHtml({ freshness: { percent: 0, text: '0 of 6 items updated in the last 30 days.' } }).includes('<strong data-live-freshness>0%</strong>'));
    for (const percent of [null, 101, -1, 8.5, '83']) {
      const html = healthHtml({ freshness: { percent, text: 'Freshness could not be read.' } });
      assert(html.includes('<strong data-live-freshness>Not recorded</strong>'), String(percent));
      assert(!html.includes('gp-freshness-track'), String(percent));
      assert(html.includes('>Freshness could not be read.</small>'), String(percent));
    }
  });
  await check('Health escapes the record words', () => {
    const html = healthHtml({ freshness: { percent: null, text: '<b>x</b>' } });
    assert(html.includes('>&lt;b&gt;x&lt;/b&gt;</small>') && !html.includes('<b>x</b>'));
  });

  if (failed) { console.error(failed + ' host film checks failed'); process.exitCode = 1; }
  else console.log('PASS: host films build their frames per host, keep the iframe inert, wait for the guide asset, step under reduced motion, and show recorded freshness');
})().catch(error => { console.error(error); process.exitCode = 1; });
