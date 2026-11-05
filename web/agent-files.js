/* Files and notes of setup: kept on the server, in the working folder of the account's agent.
 * Adding one writes no memory. The rows here show only what the server listed or accepted. */
(function () {
  'use strict';
  var MAX_FILE = 32 * 1024 * 1024;
  var state = blank(), owner = null, epoch = 0, reads = 0, changes = 0, queue = Promise.resolve();
  function blank() { return {items: [], adding: [], error: ''}; }
  function live() { return window.GroveLive; }
  function signedIn() { var s = window.GroveSimulation.getState(); return s.auth ? s.account : null; }
  function esc(v) { return String(v).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function mark() { return (window.GroveIcons || {}).notes || ''; }
  function message(res, fallback) { return live().errorText ? live().errorText(res.body, fallback) : fallback; }
  function row(item) {
    var remove = item.adding ? '' : '<button type="button" class="gp-file-remove" data-agent-remove="' + esc(item.name) + '" data-agent-kind="' + item.kind + '" aria-label="Remove ' + esc(item.name) + '">×</button>';
    return '<div><span>' + mark() + '</span><strong>' + esc(item.name) + '</strong><small class="gp-status">' + (item.adding ? 'Adding…' : '') + '</small>' + remove + '</div>';
  }
  function section(kind, empty) {
    var rows = state.items.concat(state.adding).filter(function (item) { return item.kind === kind; });
    return rows.length ? '<div class="gp-file-added">' + rows.map(row).join('') + '</div>' : '<p class="gp-material-empty">' + empty + '</p>';
  }
  function paint() {
    document.querySelectorAll('[data-agent-files]').forEach(function (root) {
      root.innerHTML = root.dataset.agentFiles === 'note' ? section('note', 'An idea, a preference, or something worth knowing') : section('file', 'Bring the things you return to');
      root.querySelectorAll('[data-agent-remove]').forEach(function (button) {
        button.onclick = function () { remove(button.dataset.agentKind, button.dataset.agentRemove); };
      });
    });
    document.querySelectorAll('[data-agent-error]').forEach(function (line) { line.textContent = state.error; line.hidden = !state.error; });
  }
  // A same-tab logout sends no event: product.js calls this wherever it resets the materials.
  function reset() { epoch++; owner = null; state = blank(); queue = Promise.resolve(); paint(); }
  // The rows belong to one account. Another account drops them, and every answer still on its way.
  function bind() { var now = signedIn(); if (now !== owner) { reset(); owner = now; } return epoch; }
  function keep(file) {
    changes++;
    state.items = [file].concat(state.items.filter(function (item) { return item.kind !== file.kind || item.name !== file.name; }));
  }
  function load() {
    var ticket = bind(), read = ++reads, seen = changes;
    if (!owner) return Promise.resolve();
    return live().agentFiles().then(function (res) {
      if (bind() !== ticket || read !== reads) return;
      // A read that began before an add or a remove ended may not show it, so it reads again.
      if (res.stale || seen !== changes) return load();
      if (!res.ok || !res.body || res.body.account !== owner || !Array.isArray(res.body.files)) { state.error = message(res, 'Your files could not load. Try again.'); paint(); return; }
      state.items = res.body.files; state.error = ''; paint();
    }).catch(function () { if (bind() === ticket && read === reads) { state.error = 'Your files could not load. Try again.'; paint(); } });
  }
  function upload(file, ticket) {
    var row = {name: file.name, kind: 'file', adding: true};
    state.adding.push(row); paint();
    function done() { state.adding = state.adding.filter(function (item) { return item !== row; }); }
    return live().addAgentFile(file).then(function (res) {
      if (bind() !== ticket) return;
      done();
      if (!res.ok || !res.body || res.body.account !== owner || !res.body.file) { state.error = message(res, file.name + ' could not be added. Try again.'); paint(); return; }
      keep(res.body.file); paint();
      if (window.GroveMotion) window.GroveMotion.source('notes');
    }).catch(function () { if (bind() === ticket) { done(); state.error = file.name + ' could not be added. Try again.'; paint(); } });
  }
  // One file at a time, in the order chosen.
  function add(files) {
    var ticket = bind(); state.error = '';
    Array.from(files).forEach(function (file) {
      if (file.size > MAX_FILE) { state.error = file.name + ' is larger than 32 MiB.'; return; }
      queue = queue.then(function () { return bind() === ticket ? upload(file, ticket) : null; });
    });
    paint();
    return queue;
  }
  function addNote(title, body) {
    var ticket = bind(); state.error = ''; paint();
    return live().addAgentNote(title, body).then(function (res) {
      if (bind() !== ticket) return false;
      if (!res.ok || !res.body || res.body.account !== owner || !res.body.file) { state.error = message(res, 'Your note could not be saved. Try again.'); paint(); return false; }
      keep(res.body.file); paint();
      return true;
    }).catch(function () { if (bind() === ticket) { state.error = 'Your note could not be saved. Try again.'; paint(); } return false; });
  }
  function remove(kind, name) {
    var ticket = bind(); state.error = '';
    return live().removeAgentFile(kind, name).then(function (res) {
      if (bind() !== ticket) return;
      if (!res.ok || !res.body || res.body.account !== owner) { state.error = message(res, name + ' could not be removed. Try again.'); paint(); return; }
      changes++;
      state.items = state.items.filter(function (item) { return item.kind !== kind || item.name !== name; }); paint();
    }).catch(function () { if (bind() === ticket) { state.error = name + ' could not be removed. Try again.'; paint(); } });
  }
  function mount(scope) {
    if (!scope.querySelector('[data-agent-files]')) return;
    var input = scope.querySelector('[data-agent-file-input]');
    if (input) input.onchange = function () { var files = Array.from(input.files || []); input.value = ''; add(files); };
    bind(); paint(); load();
  }
  ['grove:unauthenticated', 'grove:session-changed'].forEach(function (name) { window.addEventListener(name, reset); });
  window.addEventListener('grove:change', function () { if (owner) bind(); });
  window.GroveAgentFiles = {mount: mount, reset: reset, add: add, addNote: addNote, remove: remove};
}());
