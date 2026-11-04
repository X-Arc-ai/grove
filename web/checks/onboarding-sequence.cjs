// The Claude example must not end onboarding or replace real provider services.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = process.env.GROVE_ONBOARDING_WEB || path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(web, name), 'utf8');
const settle = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };

async function upload(provider) {
  const source = read('product.js');
  const fn = source.slice(source.indexOf('  function importSource('), source.indexOf('  function requestPendingExport('));
  const calls = [], upload = {pending: false};
  const context = {
    window: {location: {assign() { throw new Error('Onboarding navigated away and skipped its remaining steps'); }},
      GroveConversationPreview: {start: async () => calls.push('claude-preview')}},
    live: {uploadExport: async () => { calls.push('real-upload'); return {ok:false,status:503,body:{}}; }, errorText: () => 'Test failure'},
    canUpload: () => true, liveUpload: () => upload, render() {},
    api: {pendingExportContext: () => 1, isPendingExportContext: () => true},
    ui: {guide: provider}, PROVIDERS: {}, refreshLiveData() {},
  };
  vm.createContext(context); vm.runInContext(fn, context);
  const file = new Proxy({}, {get() { throw new Error('The Claude preview read private file bytes'); }});
  context.importSource('conversations', file, provider);
  await settle();
  assert.deepEqual(calls, [provider === 'claude' ? 'claude-preview' : 'real-upload']);
  if (provider === 'claude') assert.equal(context.ui.guide, null);
}

(async () => {
  await upload('claude');
  await upload('chatgpt');
  console.log('PASS Claude remains in onboarding; ChatGPT uses the real upload service');
})().catch(error => { console.error(error); process.exitCode = 1; });
