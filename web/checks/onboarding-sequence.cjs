// A conversation upload must not end onboarding, and every provider takes the real upload service.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = process.env.GROVE_ONBOARDING_WEB || path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(web, name), 'utf8');
const settle = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };

async function upload(provider, answer) {
  const source = read('product.js');
  const fn = source.slice(source.indexOf('  function importSource('), source.indexOf('  function requestPendingExport('));
  const calls = [], upload = {pending: false, file: {}};
  const context = {
    window: {location: {assign() { throw new Error('Onboarding navigated away and skipped its remaining steps'); }},
      GroveImportGrowth: {accept() { calls.push('growth'); }}},
    live: {uploadExport: async () => { calls.push('real-upload'); return answer; }, errorText: () => 'Test failure'},
    canUpload: () => true, liveUpload: () => upload, render() {},
    api: {pendingExportContext: () => 1, isPendingExportContext: () => true},
    importProgressState: {values: {}, errors: {}, signatures: {}, tracked: {}},
    ui: {guide: provider}, PROVIDERS: {}, refreshLiveData() {},
  };
  vm.createContext(context); vm.runInContext(fn, context);
  // The product code hands the file to the upload call and reads none of its bytes.
  const file = new Proxy({}, {get() { throw new Error('The page read private file bytes'); }});
  context.importSource('conversations', file, provider);
  await settle();
  return {calls, ui: context.ui, upload};
}

(async () => {
  for (const provider of ['claude', 'chatgpt']) {
    const refused = await upload(provider, {ok: false, status: 503, body: {}});
    assert.deepEqual(refused.calls, ['real-upload']);
    assert.equal(refused.ui.guide, provider, 'A refused upload stays in the guide');
    const taken = await upload(provider, {ok: true, status: 200, body: {state: 'taken_in', failed_chats: 0}});
    assert.deepEqual(taken.calls, ['real-upload', 'growth']);
    assert.equal(taken.ui.guide, null, 'The person returns to the conversations step');
  }
  assert(!read('product.js').includes('GroveConversationPreview'));
  console.log('PASS Claude and ChatGPT use the real upload service and stay in onboarding');
})().catch(error => { console.error(error); process.exitCode = 1; });
