/* Session-only material controls shared by setup and Settings. */
(function () {
  'use strict';
  var ACCEPT = '.txt,.md,.pdf,.docx,.pptx', MAX_FILE = 16 * 1024 * 1024;
  function status(m) {
    if (m.source_state === 'screened_out') return 'Screening retained no content. Nothing was added to your brain.';
    if (m.kind === 'file') return 'File saved. Brain extraction needs source details.';
    var r = m.request;
    if (!r) return 'Note saved. No brain request is available.';
    if (r.status === 'saved') return r.changed_ids && r.changed_ids.length ? 'Note saved. Added to your brain.' : 'Note saved. The request saved no graph changes.';
    return 'Note saved. Request status: ' + r.status + '.';
  }
  function blank() {
    return {items: [], cursor: null, material: null, preview: null, view: 'list',
      draft: {title: '', body: ''}, file: null, busy: false, error: '', notice: '', conflict: false};
  }
  function createStore(deps) {
    var state = blank(), owner = null, route = null, epoch = 0, controller = null;
    var generation = -1, submission = null, timer = null, polls = 0;
    var later = deps.later || setTimeout, cancel = deps.cancel || clearTimeout;
    function emit() { deps.changed(); }
    function stop() {
      epoch++;
      if (controller) controller.abort();
      controller = null;
      if (timer !== null) cancel(timer);
      timer = null;
    }
    function clear() {
      stop(); owner = null; route = null; generation = -1; submission = null; polls = 0;
      Object.assign(state, blank()); emit();
    }
    function current(ctx) { return !!route && owner === deps.account() && ctx.owner === owner && ctx.epoch === epoch; }
    function begin() {
      stop(); controller = new AbortController();
      return {owner: owner, epoch: epoch, signal: controller.signal};
    }
    function enter(view) {
      stop(); state.view = view; state.busy = false; state.error = ''; state.notice = '';
      state.preview = null; state.conflict = false; polls = 0;
    }
    function failure(res) {
      var messages = {
        401: 'Your session ended. Sign in again.',
        403: 'This account cannot perform this material action.',
        404: 'This material is no longer available. Reload the list.',
        409: 'This material changed. Reload the latest revision before you try again.',
        413: 'This file exceeds 16 MiB, or the text exceeds its limit.',
        415: 'Use a TXT, Markdown, PDF, DOCX or PPTX file.',
        422: 'This material is unsupported, unreadable, or exceeds a content limit. Use a supported text document.',
        429: 'Another upload is active. Try again after it finishes.',
        503: 'Materials are temporarily unavailable. Try again later.'
      };
      return messages[res.status] || 'Grove could not complete this action. Try again.';
    }
    async function request(ctx, action, id, body, key) {
      var res = await deps.request(action, id, body, key, ctx.signal);
      if (!current(ctx) || res.stale) return null;
      if (!res.ok) {
        state.conflict = res.status === 409;
        throw new Error(failure(res));
      }
      if (!res.body || res.body.account !== owner) throw new Error('The account changed. Reload materials.');
      if (action !== 'confirm' && action !== 'delete') {
        var next = res.body.generation;
        if (!Number.isSafeInteger(next) || next < generation) throw new Error('Materials changed during this request. Reload materials.');
        if (next > generation) { state.items = []; state.cursor = null; }
        generation = next;
      }
      return res.body;
    }
    async function run(work) {
      if (!route || owner !== deps.account()) { clear(); return; }
      var ctx = begin(); state.busy = true; state.error = ''; emit();
      try { await work(ctx); }
      catch (_) { if (current(ctx)) state.error = _.message === 'Failed to fetch' || _.name === 'TypeError' ? 'Grove could not be reached. Try again.' : _.message; }
      finally { if (current(ctx)) { state.busy = false; emit(); } }
    }
    function keep(material) {
      if (!material || !material.id || !Number.isSafeInteger(material.revision)) throw new Error('Grove returned an incomplete material. Reload materials.');
      state.material = material;
      state.items = state.items.filter(function (row) { return row.id !== material.id; }).concat([material]);
    }
    function poll() {
      var m = state.material, r = m && m.request;
      if (state.view !== 'detail' || !r || !['queued', 'applying'].includes(r.status) || polls >= 12) return;
      var ctx = {owner: owner, epoch: epoch}, id = m.id;
      timer = later(function () {
        timer = null;
        if (!current(ctx) || state.view !== 'detail' || !state.material || state.material.id !== id) return;
        polls++; detail(id, true);
      }, 5000);
    }
    function list(more) {
      var cursor = more ? state.cursor : null;
      enter('list'); state.material = null; state.draft = {title: '', body: ''}; state.file = null; submission = null;
      if (!more) { state.items = []; state.cursor = null; }
      return run(async function (ctx) {
        var body = await request(ctx, 'list', null, cursor);
        if (!body) return;
        if (!Array.isArray(body.materials)) throw new Error('Grove returned an incomplete list. Reload materials.');
        var rows = new Map(state.items.map(function (m) { return [m.id, m]; }));
        body.materials.forEach(function (m) { rows.set(m.id, m); });
        state.items = Array.from(rows.values()); state.cursor = body.next_cursor || null;
      });
    }
    function detail(id, polling) {
      if (!polling) { enter('detail'); state.material = null; state.draft = {title: '', body: ''}; submission = null; }
      return run(async function (ctx) {
        var body = await request(ctx, 'detail', id);
        if (!body) return;
        if (!body.material || body.material.id !== id) throw new Error('Grove returned a different material. Reload the list.');
        keep(body.material); poll();
      });
    }
    function draft(title, body) { state.draft = {title: title, body: body}; }
    function newNote() { enter('note'); state.material = null; state.draft = {title: '', body: ''}; state.file = null; submission = null; emit(); }
    function edit() {
      if (!state.material || state.material.kind !== 'note' || state.busy) return;
      enter('note'); state.draft = {title: state.material.title, body: (state.material.parts || []).map(function (p) { return p.text; }).join('\n\n')};
      submission = null; emit();
    }
    function keyFor(fingerprint) {
      if (!submission || submission.fingerprint !== fingerprint) submission = {fingerprint: fingerprint, key: window.crypto.randomUUID()};
      return submission.key;
    }
    function saveNote() {
      if (state.busy || state.conflict) return Promise.resolve();
      var body = {title: state.draft.title, body: state.draft.body}, m = state.material;
      if (!body.title.trim() || !body.body.trim() || body.title.length > 512 || body.body.length > 16000) {
        state.error = 'Enter a title up to 512 characters and a note up to 16,000 characters.'; emit(); return Promise.resolve();
      }
      if (m) body.expected_revision = m.revision;
      var action = m ? 'edit' : 'note', id = m && m.id;
      var key = keyFor(JSON.stringify([action, id, body]));
      return run(async function (ctx) {
        var result = await request(ctx, action, id, body, key);
        if (!result) return;
        keep(result.material); submission = null; state.draft = {title: '', body: ''}; state.view = 'detail'; poll();
      });
    }
    function newFile() { enter('file'); state.material = null; state.file = null; submission = null; emit(); }
    function file(value) { if (!state.busy) { state.file = value; state.error = ''; emit(); } }
    function saveFile() {
      if (state.busy) return Promise.resolve();
      var value = state.file;
      if (!(value instanceof File) || !/\.(txt|md|pdf|docx|pptx)$/i.test(value.name) || !value.size || value.size > MAX_FILE) {
        state.error = 'Choose a nonempty TXT, Markdown, PDF, DOCX or PPTX file up to 16 MiB.'; emit(); return Promise.resolve();
      }
      return run(async function (ctx) {
        var hash = await window.crypto.subtle.digest('SHA-256', await value.arrayBuffer());
        if (!current(ctx)) return;
        var digest = Array.from(new Uint8Array(hash)).map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
        var key = keyFor(JSON.stringify(['file', value.name, digest]));
        var body = await request(ctx, 'file', null, value, key);
        if (!body) return;
        keep(body.material); state.file = null; submission = null; state.view = 'detail';
      });
    }
    function preview() {
      if (!state.material || state.busy) return Promise.resolve();
      var id = state.material.id; enter('delete');
      return run(async function (ctx) {
        var body = await request(ctx, 'preview', id);
        if (!body) return;
        if (body.id !== id || body.scope !== 'all_revisions_and_derived_copies' || !Number.isSafeInteger(body.revision)) throw new Error('Grove returned an incomplete deletion preview. Reload the material.');
        state.preview = body;
      });
    }
    function confirmDelete() {
      if (state.view !== 'delete' || !state.preview || state.busy) return Promise.resolve();
      var preview = state.preview; state.preview = null;
      return run(async function (ctx) {
        // Only this explicit confirmation action obtains approval. Never retain it in state.
        var approval = await request(ctx, 'confirm', preview.id, {confirm: true, expected_revision: preview.revision});
        if (!approval || !current(ctx)) return;
        if (!approval.one_use || typeof approval.approval_token !== 'string') throw new Error('Deletion approval is unavailable. Reload the preview and confirm again.');
        var body = await request(ctx, 'delete', preview.id, {approval_token: approval.approval_token, expected_revision: preview.revision});
        approval = null;
        if (!body) return;
        if (body.deleted !== true || !body.receipt) throw new Error('Deletion is not confirmed. Reload the list before another attempt.');
        state.items = state.items.filter(function (m) { return m.id !== preview.id; });
        state.material = null; state.view = 'list'; state.draft = {title: '', body: ''}; submission = null;
        state.notice = 'Material deleted. All revisions and their derived copies were removed.';
      });
    }
    function back() { if (state.material) return detail(state.material.id); return list(); }
    function reload() { if (state.material) return detail(state.material.id); return list(); }
    function mount(nextRoute) {
      var nextOwner = deps.account();
      if (!nextRoute || !nextOwner) { if (route || owner) clear(); return; }
      if (nextOwner === owner && route === nextRoute) return;
      clear(); owner = nextOwner; route = nextRoute; list();
    }
    return {state: state, mount: mount, clear: clear, list: list, detail: detail, reload: reload,
      newNote: newNote, draft: draft, edit: edit, saveNote: saveNote, newFile: newFile, file: file,
      saveFile: saveFile, preview: preview, confirmDelete: confirmDelete, back: back};
  }
  function esc(value) { return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) { return {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]; }); }
  var root = null, store = null;
  function button(action, label, disabled, extra) { return '<button type="button" class="gp-small" data-material-action="' + action + '"' + (disabled ? ' disabled' : '') + (extra || '') + '>' + label + '</button>'; }
  function paint() {
    if (!root || !store) return;
    var s = store.state, m = s.material, disabled = s.busy, html = '';
    if (s.view === 'list') {
      html = '<div class="gp-material-tools">' + button('reload', 'Reload materials', disabled) + '</div>';
      [['file','Your files','Add files +'],['note','Your notes','Add note +']].forEach(function (group) {
        var rows = s.items.filter(function (row) { return row.kind === group[0]; });
        html += '<section class="gp-material-section"><header><div><h2>' + group[1] + '</h2><p>' + (group[0] === 'file' ? 'TXT, Markdown, PDF, DOCX and PPTX' : 'Your own words, directly to Grove') + '</p></div>' + button(group[0], group[2], disabled) + '</header>';
        html += rows.length ? '<ul class="gp-material-list">' + rows.map(function (row) {
          return '<li>' + button('detail', esc(row.title || 'Untitled material'), disabled, ' data-material-id="' + esc(row.id) + '"') + '<p>' + esc(status(row)) + '</p></li>';
        }).join('') + '</ul>' : '<p class="gp-material-empty">' + (disabled ? 'Materials check in progress' : 'No ' + (group[0] === 'file' ? 'files' : 'notes') + ' in this page') + '</p>';
        html += '</section>';
      });
      if (s.cursor) html += button('more', 'Load more', disabled);
    } else {
      html = '<div class="gp-material-tools">' + button('list', 'All materials', false) + '</div>';
      if (s.view === 'note') {
        html += '<form class="gp-note-form" data-material-note><h2>' + (m ? 'Edit note' : 'Add note') + '</h2>' + (m ? '<p>This update adds a new note to your brain. Earlier statements remain until you delete this note.</p>' : '') + '<label for="gp-material-title">Title</label><input id="gp-material-title" name="title" maxlength="512" required value="' + esc(s.draft.title) + '"' + (disabled ? ' disabled' : '') + '><label for="gp-material-body">Note</label><textarea id="gp-material-body" name="body" rows="6" maxlength="16000" required' + (disabled ? ' disabled' : '') + '>' + esc(s.draft.body) + '</textarea><footer>' + button('back', 'Cancel', false) + '<button class="gp-small"' + (disabled || s.conflict ? ' disabled' : '') + '>' + (m ? 'Save revision' : 'Save note') + '</button></footer></form>';
      } else if (s.view === 'file') {
        html += '<h2>Add file</h2><p>Choose a text document up to 16 MiB. Image-only PDFs and unsupported embedded content cannot be read.</p><label class="gp-material-file">File<input type="file" accept="' + ACCEPT + '" data-material-file' + (disabled ? ' disabled' : '') + '></label><p>' + esc(s.file ? s.file.name : 'No file selected') + '</p>' + button('upload', 'Upload file', disabled || !s.file) + button('back', 'Cancel', false);
      } else if (s.view === 'delete') {
        var p = s.preview;
        html += '<section class="gp-material-delete" role="group" aria-labelledby="gp-material-delete-title"><h2 id="gp-material-delete-title" tabindex="-1">Delete this material?</h2>';
        if (p) html += '<p>Delete all ' + esc(p.revisions) + ' revisions and their derived copies (' + esc(p.sources) + ' sources).</p><p>Downloaded copies are out of reach. Backups can retain data for up to ' + esc(p.backup_days) + ' days.</p>';
        html += button('back', 'Cancel', false) + button('confirm', 'Delete all revisions', disabled || !p) + '</section>';
      } else if (m) {
        html += '<article class="gp-material-detail"><h2>' + esc(m.title || 'Untitled material') + '</h2><p data-material-status>' + esc(status(m)) + '</p><p>Revision ' + esc(m.revision) + ' · Received ' + esc(m.received_at || '') + '</p><p>Retained parts: ' + esc(m.counts.parts) + ' · Retained characters: ' + esc(m.counts.retained_characters) + ' · Held: ' + esc(m.counts.held) + '</p>';
        html += Object.keys(m.screening_counts || {}).map(function (key) { return '<p>Screened ' + esc(key) + ': ' + esc(m.screening_counts[key]) + '</p>'; }).join('');
        html += '<div class="gp-material-parts">' + (m.parts || []).map(function (part) { return '<p>' + esc(part.text) + '</p>'; }).join('') + '</div><div class="gp-material-tools">' + button('reload', 'Check status', disabled) + (m.kind === 'note' ? button('edit', 'Edit note', disabled) : '') + button('preview', 'Delete material', disabled) + '</div></article>';
      }
    }
    if (s.busy) html += '<p role="status">Request in progress</p>';
    if (s.notice) html += '<p role="status">' + esc(s.notice) + '</p>';
    if (s.error) html += '<p role="alert">' + esc(s.error) + '</p>' + button('reload', s.conflict ? 'Discard draft and reload latest' : 'Reload materials', disabled);
    root.innerHTML = html;
    root.querySelectorAll('[data-material-action]').forEach(function (el) {
      el.onclick = function () {
        var action = el.dataset.materialAction;
        var actions = {reload: store.reload, list: store.list, note: store.newNote, file: store.newFile,
          upload: store.saveFile, edit: store.edit, preview: store.preview, confirm: store.confirmDelete, back: store.back};
        if (action === 'detail') store.detail(el.dataset.materialId);
        else if (action === 'more') store.list(true);
        else actions[action]();
        if (action === 'note' || action === 'edit') { var input = root.querySelector('#gp-material-title'); if (input) input.focus(); }
      };
    });
    var form = root.querySelector('[data-material-note]');
    if (form) {
      form.oninput = function () { store.draft(form.elements.title.value, form.elements.body.value); };
      form.onsubmit = function (event) { event.preventDefault(); store.saveNote(); };
    }
    var picker = root.querySelector('[data-material-file]');
    if (picker) picker.onchange = function () { store.file(picker.files[0] || null); };
  }
  function mount(scope) {
    var api = window.GroveSimulation;
    if (!api || !api.isLive() || !window.GroveLive) return;
    root = scope.querySelector('[data-materials]');
    if (!store) store = createStore({account: function () { var s = api.getState(); return s.auth && s.account; }, request: window.GroveLive.materialRequest, changed: paint});
    store.mount(root && root.dataset.materials); paint();
  }
  function reset() { root = null; if (store) store.clear(); }
  window.GroveMaterials = {mount: mount, reset: reset, createStore: createStore, status: status};
})();
