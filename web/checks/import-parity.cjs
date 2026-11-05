// Real import progress without a browser or network: accepted uploads, the ring, partial failures.
// GROVE_IMPORT_WEB selects a source copy, for the neutered run that must turn this check red.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = process.env.GROVE_IMPORT_WEB || path.join(__dirname, '..');
const source = fs.readFileSync(path.join(web, 'product.js'), 'utf8');
function sourceFunction(name) {
  const start = source.indexOf('  function ' + name + '(');
  assert(start >= 0, 'Missing function: ' + name);
  const end = source.indexOf('\n  function ', start + 1);
  assert(end > start, 'Missing function boundary: ' + name);
  return source.slice(start, end);
}
const NAMES = ['esc', 'plural', 'chatFailure', 'progressFailure', 'importNote', 'importNoteSlot', 'importView', 'importMark',
  'importPanelMarkup', 'uploadResult', 'canUpload', 'resetImportProgress', 'parsedImportProgress', 'importMoving', 'importShare',
  'refreshImportProgress', 'importSource'];
function progressBody(provider, patch = {}) {
  return { contract: 'import_progress.v1', provider, state: 'idle', sources_total: 0,
    requests_total: 0, queued_count: 0, processing_count: 0, retry_wait_count: 0,
    extracted_count: 0, held_nothing_count: 0, held_count: 0, failed_count: 0,
    graph: { items_created: 0, items_changed: 0, links_created: 0, links_changed: 0 }, ...patch };
}
function harness({ owner = 'owner-a', complete = false, response = null, promise = null, accounts = [] } = {}) {
  const state = { account: owner, auth: true, onboardingComplete: complete, pendingExports: {} };
  const uploads = {};
  const ui = { guide: 'chatgpt', guideStage: 1, step: 1 };
  const calls = { refreshed: 0, graph: 0, reminder: 0, sent: [], accepted: 0, observed: [], reads: [], timers: [] };
  const responses = {};
  const growth = { accept() { calls.accepted++; }, observe(moving) { calls.observed.push(moving); }, active: () => false, clear() {} };
  const context = vm.createContext({
    ui, liveSessionReady: true, document: { hidden: false }, console,
    live: {
      uploadExport(provider, file) { calls.sent.push([provider, file]); return promise || Promise.resolve(response); },
      errorText: (_, fallback) => fallback || 'Upload failed',
      importProgress(id) { calls.reads.push(id); return responses[id] || Promise.resolve({ ok: true, body: progressBody({ claude: 'claude_ai' }[id] || id) }); },
    },
    api: { getState: () => state, pendingExportContext: () => owner, isPendingExportContext: id => state.account === id,
      applyPendingExports() { calls.reminder++; } },
    liveUpload: id => uploads[id] ||= { file: null, pending: false, result: null, error: '' },
    liveSourceAccounts: () => accounts,
    PROVIDERS: { chatgpt: { label: 'ChatGPT' }, claude: { label: 'Claude' } },
    icon: () => '<svg/>', render() {}, updateLive() {},
    refreshLiveData() { calls.refreshed++; },
    setTimeout(fn, delay) { calls.timers.push(delay); return calls.timers.length; }, clearTimeout() {},
    window: { reloadGrove() { calls.graph++; }, GroveImportGrowth: growth, GroveMotion: { source() { throw Error('Fabricated graph feedback'); } } },
  });
  vm.runInContext("var IMPORT_CODES={chatgpt:'chatgpt',claude:'claude_ai'};var importProgressState={owner:'"+owner+"',values:{},errors:{},signatures:{},tracked:{},request:0,pending:false};var importProgressTimer=null;" +
    NAMES.map(sourceFunction).join('\n'), context);
  const evaluate = code => vm.runInContext(code, context);
  return { context, state, ui, uploads, calls, responses, growth, evaluate,
    upload: id => context.liveUpload(id),
    send: async (provider = 'chatgpt', file = { name: 'invented.json' }) => {
      ui.guide = provider;
      const upload = context.liveUpload(provider);
      upload.file = file;
      context.__file = file; evaluate(`importSource('conversations',__file,${JSON.stringify(provider)})`);
      await new Promise(setImmediate);
    },
    refresh: () => evaluate('refreshImportProgress(true)'),
    view: id => JSON.parse(JSON.stringify(evaluate(`importView(${JSON.stringify(id)})`))),
    mark: id => evaluate(`importMark(${JSON.stringify(id)})`),
    note: id => evaluate(`importNote(${JSON.stringify(id)})`),
    panel: () => evaluate('importPanelMarkup()'),
  };
}
const accepted = (patch = {}) => ({ ok: true, status: 200, body: { state: 'taken_in', chats: 2, chats_new: 2, sources_added: 2, episodes_added: 2,
  messages_read: 9, messages_kept: 8, failed_chats: 0, screened: {}, processing: { requests_added: 2, held_sources: 0 }, ...patch } });
const checks = [];
const check = (name, run) => checks.push([name, run]);

for (const provider of ['chatgpt', 'claude']) {
  check(provider + ': the check shows when the server accepts the upload, before any growth', async () => {
    const test = harness({ response: accepted() });
    await test.send(provider);
    assert.equal(test.calls.sent.length, 1, 'The file takes the real upload path');
    assert.equal(test.calls.sent[0][0], provider);
    const view = test.view(provider);
    assert.equal(view.added, true); assert.equal(view.label, 'Added'); assert.equal(view.ring, 0); assert.equal(view.warned, false);
    assert.equal(test.ui.guide, null, 'A clean answer closes the guide');
    assert.equal(test.upload(provider).file, null);
    assert.equal(test.calls.accepted, 1, 'The pacer starts at acceptance');
    assert.equal(test.calls.refreshed, 1);
    assert.equal(test.calls.graph, 0, 'The upload itself adds no graph');
  });
}
check('The check stays while the ring runs on the real counts', async () => {
  const test = harness({ response: accepted() }); await test.send('claude');
  test.responses.claude = Promise.resolve({ ok: true, body: progressBody('claude_ai', { state: 'processing', sources_total: 8, requests_total: 8,
    extracted_count: 2, held_nothing_count: 1, held_count: 0, failed_count: 0, queued_count: 4, processing_count: 1 }) });
  await test.refresh();
  assert.equal(test.view('claude').ring, 3 / 8);
  assert.equal(test.view('claude').added, true);
  assert.match(test.mark('claude'), /--progress:0\.375/);
  assert.match(test.mark('claude'), /aria-label="Claude import, 38 percent"/);
  assert.doesNotMatch(test.mark('claude'), /gp-complete-check/);
  assert.deepEqual(test.calls.observed.at(-1), true);
  assert.equal(test.calls.timers.at(-1), 4000);
});
check('The ring counts failed and held parts as ended, and never passes the whole', async () => {
  const test = harness({ response: accepted() }); await test.send('chatgpt');
  test.responses.chatgpt = Promise.resolve({ ok: true, body: progressBody('chatgpt', { state: 'partial', sources_total: 5, requests_total: 4,
    extracted_count: 1, held_nothing_count: 1, failed_count: 1, held_count: 2 }) });
  await test.refresh();
  assert.equal(test.view('chatgpt').ring, 1);
});
check('A clean end shows the complete mark, and no progress leaves the ring empty', async () => {
  const test = harness({ response: accepted() }); await test.send('claude');
  assert.equal(test.mark('claude'), '', 'No mark before the first read');
  test.responses.claude = Promise.resolve({ ok: true, body: progressBody('claude_ai', { state: 'complete', sources_total: 2, requests_total: 2, extracted_count: 1, held_nothing_count: 1,
    graph: { items_created: 3, items_changed: 0, links_created: 1, links_changed: 0 } }) });
  await test.refresh();
  assert.match(test.mark('claude'), /gp-complete-check/);
  assert.equal(test.view('claude').label, 'Added');
  assert.deepEqual(test.calls.observed.at(-1), false);
});
check('An import finished on an earlier visit shows no mark, and a stored read keeps its check', async () => {
  const test = harness({ accounts: [{ id: 'chatgpt', readCount: 2 }] });
  test.responses.chatgpt = Promise.resolve({ ok: true, body: progressBody('chatgpt', { state: 'complete', sources_total: 1, requests_total: 1, extracted_count: 1 }) });
  await test.refresh();
  assert.equal(test.mark('chatgpt'), '');
  assert.equal(test.view('chatgpt').added, true); assert.equal(test.view('chatgpt').ring, 1);
  assert.equal(test.panel(), '', 'A finished import needs no line');
});
check('A connector with no tracked progress keeps its full ring', () => {
  const test = harness({ accounts: [{ id: 'claude', readCount: 1 }] });
  assert.equal(test.view('claude').added, true); assert.equal(test.view('claude').ring, 1);
});
check('A failed upload gives no check and keeps the file for a retry', async () => {
  for (const response of [{ ok: false, status: 503, body: {} }, accepted({ state: 'failed', failed_chats: 2, messages_kept: 0, sources_added: 0 })]) {
    const file = { name: 'invented.json' }, test = harness({ response });
    await test.send('claude', file);
    assert.equal(test.view('claude').added, false);
    assert.notEqual(test.view('claude').label, 'Added');
    assert.equal(test.upload('claude').file, file);
    assert.equal(test.ui.guide, 'claude');
    assert.equal(test.calls.accepted, 0);
    assert.equal(test.calls.refreshed, 0);
  }
});
check('kept_out adds no check and starts no growth', async () => {
  const test = harness({ response: accepted({ state: 'kept_out', messages_kept: 0 }) }); await test.send('chatgpt');
  assert.equal(test.view('chatgpt').added, false); assert.equal(test.calls.accepted, 0);
  assert.equal(test.ui.guide, 'chatgpt');
});
check('A repeat upload says No new messages and adds no growth', async () => {
  const test = harness({ response: accepted({ state: 'nothing_new', chats_new: 0, sources_added: 0, messages_kept: 0 }) });
  await test.send('claude');
  assert.equal(test.calls.accepted, 0, 'No growth');
  assert.equal(test.view('claude').added, false, 'The repeat alone proves no new item');
  assert.equal(test.ui.guide, null);
  assert.match(test.panel(), /No new messages/);
  assert.doesNotMatch(test.panel(), /accepted/);
});
check('A partial upload stays in the guide with its file and is never a clean success', async () => {
  const file = { name: 'invented.json' }, test = harness({ response: accepted({ failed_chats: 1 }) });
  await test.send('chatgpt', file);
  assert.equal(test.ui.guide, 'chatgpt'); assert.equal(test.upload('chatgpt').file, file);
  assert.equal(test.calls.reminder, 0);
  const view = test.view('chatgpt');
  assert.equal(view.warned, true); assert.equal(view.label, 'Partial import');
  assert.match(test.note('chatgpt'), /Some chats failed: 1\./);
  assert.match(test.panel(), /Some chats failed: 1\./);
  test.responses.chatgpt = Promise.resolve({ ok: true, body: progressBody('chatgpt', { state: 'complete', sources_total: 1, requests_total: 1, extracted_count: 1 }) });
  await test.refresh();
  assert.doesNotMatch(test.mark('chatgpt'), /gp-complete-check/, 'A later complete read does not hide the failed chat');
  assert.match(test.note('chatgpt'), /Some chats failed/);
});
for (const [state, patch, label, line] of [
  ['partial', { extracted_count: 1, failed_count: 1, held_count: 1, requests_total: 3 }, 'Partial import', /Not all of this import finished: 1 part failed, 1 part held\./],
  ['failed', { failed_count: 2, requests_total: 2 }, 'Import failed', /2 parts failed/],
  ['held', { held_count: 1, requests_total: 1 }, 'Needs attention', /1 part held/],
]) {
  check('Progress state ' + state + ' shows a persistent line and no clean success', async () => {
    const test = harness({ response: accepted() }); await test.send('claude');
    test.responses.claude = Promise.resolve({ ok: true, body: progressBody('claude_ai', { state, sources_total: 3, ...patch }) });
    await test.refresh();
    assert.equal(test.view('claude').label, label);
    assert.equal(test.view('claude').warned, true);
    assert.match(test.note('claude'), line);
    assert.match(test.evaluate("importNoteSlot('small','claude')"), line);
    assert.doesNotMatch(test.evaluate("importNoteSlot('small','claude')"), /hidden/);
    assert.match(test.panel(), line);
    assert.doesNotMatch(test.mark('claude'), /gp-complete-check/);
  });
}
check('A provider with no import has no note slot', () => {
  assert.equal(harness().evaluate("importNoteSlot('small','notion')"), '');
  assert.equal(harness().evaluate("importNote('notion')"), '');
});
check('Text states counts only: no percent and no old copy', () => {
  const test = harness();
  const html = test.evaluate("uploadResult({state:'taken_in',chats:2,chats_new:1,sources_added:1,episodes_added:1,messages_read:2,messages_kept:1,failed_chats:0,processing:{requests_added:1,held_sources:1}})");
  assert.match(html, /<details><summary>Import details<\/summary>/);
  assert(html.indexOf('<details>') < html.indexOf('Messages read:'));
  assert.match(html, /Grove accepted 1 message from 1 new chat\. 1 part added to the queue\. 1 part held\./);
  assert.doesNotMatch(html, /Your graph has not changed|%|percent/);
});
check('canUpload holds ChatGPT and Claude, and one entry adds a provider', () => {
  const test = harness();
  test.context.live = {};
  vm.runInContext("var live={}", test.context);
  assert.equal(test.evaluate("canUpload('chatgpt')"), true);
  assert.equal(test.evaluate("canUpload('claude')"), true);
  assert.equal(test.evaluate("canUpload('chats')"), false);
  assert.equal(test.evaluate("canUpload('grok')"), false);
  test.evaluate("IMPORT_CODES.chats='whatsapp'");
  assert.equal(test.evaluate("canUpload('chats')"), true);
});
check('Missing totals, mismatched providers, and unsupported completion claims stay out', async () => {
  for (const patch of [{ requests_total: undefined }, { provider: 'muse' }, { state: 'complete', requests_total: 1 }, { extracted_count: 2, requests_total: 1 }]) {
    const test = harness();
    test.responses.chatgpt = Promise.resolve({ ok: true, body: progressBody('chatgpt', patch) });
    await test.refresh();
    assert.equal(test.context.importProgressState.values.chatgpt, undefined);
    assert.equal(test.calls.graph, 0);
  }
});
check('A failed progress read cannot end the growth', async () => {
  const test = harness({ response: accepted() }); await test.send('chatgpt');
  test.responses.chatgpt = Promise.reject(new Error('offline'));
  await test.refresh();
  assert.deepEqual(test.calls.observed, []);
  assert.match(test.panel(), /could not read the import status/);
});
check('Only stored changes reload the graph, and the first read adds no second load', async () => {
  const test = harness();
  test.responses.chatgpt = Promise.resolve({ ok: true, body: progressBody('chatgpt', { state: 'queued', sources_total: 1, requests_total: 1, queued_count: 1 }) });
  await test.refresh(); assert.equal(test.calls.graph, 0);
  test.responses.chatgpt = Promise.resolve({ ok: true, body: progressBody('chatgpt', { state: 'complete', sources_total: 1, requests_total: 1, extracted_count: 1,
    graph: { items_created: 1, items_changed: 0, links_created: 0, links_changed: 0 } }) });
  await test.refresh(); assert.equal(test.calls.graph, 1);
  await test.refresh(); assert.equal(test.calls.graph, 1);
  test.growth.active = () => true;
  test.responses.chatgpt = Promise.resolve({ ok: true, body: progressBody('chatgpt', { state: 'complete', sources_total: 1, requests_total: 1, extracted_count: 1,
    graph: { items_created: 2, items_changed: 0, links_created: 0, links_changed: 0 } }) });
  await test.refresh(); assert.equal(test.calls.graph, 1, 'While the pacer plays, the pacer owns the graph');
});
check('Page load on moving work tells the pacer, and idle work does not', async () => {
  const test = harness();
  test.responses.claude = Promise.resolve({ ok: true, body: progressBody('claude_ai', { state: 'retry_wait', sources_total: 1, requests_total: 1, retry_wait_count: 1 }) });
  await test.refresh();
  assert.deepEqual(test.calls.observed, [true]);
  const idle = harness(); await idle.refresh();
  assert.deepEqual(idle.calls.observed, [false]);
  assert.equal(idle.calls.timers.at(-1), 15000);
});
check('An old account progress response cannot enter current state', async () => {
  const test = harness(); let resolve;
  test.responses.chatgpt = new Promise(done => { resolve = done; });
  const pending = test.refresh(); test.state.account = 'owner-b';
  resolve({ ok: true, body: progressBody('chatgpt', { state: 'complete', sources_total: 1, requests_total: 1, extracted_count: 1 }) });
  await pending;
  assert.equal(test.calls.graph, 0); assert.deepEqual(test.calls.observed, []);
  assert.equal(test.context.importProgressState.values.chatgpt, undefined);
});
check('An upload response for an old account cannot update the next account', async () => {
  let resolve; const promise = new Promise(done => { resolve = done; });
  const test = harness({ promise }); await test.send('chatgpt');
  test.state.account = 'owner-b';
  resolve(accepted()); await new Promise(setImmediate);
  assert.equal(test.upload('chatgpt').result, null); assert.equal(test.calls.refreshed, 0); assert.equal(test.calls.accepted, 0);
});
check('A reset clears the progress and the pacer', async () => {
  const test = harness({ response: accepted() }); let cleared = 0; test.growth.clear = () => { cleared++; };
  await test.send('claude');
  test.evaluate('resetImportProgress()');
  assert.equal(cleared, 1);
  assert.equal(Object.keys(test.context.importProgressState.tracked).length, 0);
});
check('Progress and connection reads use only the owned endpoints and reject stale session replies', async () => {
  const calls = [], events = [];
  let release;
  const context = vm.createContext({ URL, URLSearchParams, TextEncoder, CustomEvent: class { constructor(type) { this.type = type; } },
    window: { addEventListener() {}, dispatchEvent: event => events.push(event.type), location: { pathname: '/' },
      fetch: (url, init) => { calls.push({ url, init }); return new Promise(done => { release = done; }); } } });
  vm.runInContext(fs.readFileSync(path.join(web, 'live.js'), 'utf8'), context);
  const live = context.window.GroveLive;
  const pending = live.importProgress('claude');
  assert.equal(calls[0].url, '/v1/sources/import-progress?provider=claude_ai&limit=1');
  assert.equal(calls[0].init.credentials, 'same-origin');
  assert.equal(calls[0].init.method, 'GET');
  assert.equal(calls[0].init.headers.Authorization, undefined);
  live.invalidateSession();
  release({ ok: false, status: 401, text: () => Promise.resolve('{"error":{"code":"unauthenticated"}}') });
  assert.equal((await pending).stale, true); assert.deepEqual(events, []);
  await assert.rejects(live.importProgress('__proto__'), /Unknown import provider/);
  await assert.rejects(live.importProgress('muse'), /approved connection/);
  assert.equal(calls.length, 1);
  const connections = live.approvedConnections();
  assert.equal(calls[1].url, '/v1/views/connections');
  live.invalidateSession();
  release({ ok: true, status: 200, text: () => Promise.resolve('{"connections":[]}') });
  assert.equal((await connections).stale, true);
});
(async () => {
  let failed = 0;
  for (const [name, run] of checks) {
    try { await run(); console.log('PASS: ' + name); }
    catch (error) { failed++; console.error('FAIL: ' + name + '\n' + error.stack); }
  }
  console.log(`Import controls: ${checks.length - failed} passed, ${failed} failed`);
  if (failed) process.exitCode = 1;
})();
