/* Grove live mode: the server calls the views make, in one place.
 * Loaded before simulation.js. When no fixture is injected (only the checks inject one),
 * simulation.js routes the native graph's /api/* reads here, and product.js signs in
 * through the auth calls below. Every request carries the session cookie (same origin).
 */
(function () {
  'use strict';

  // Every server path the views call. Nothing else in web/ names a server path.
  var ENDPOINTS = {
    mcp: '/mcp',
    session: '/v1/views/session',
    graph: '/v1/views/graph',
    node: '/v1/views/node/',
    schema: '/v1/views/schema',
    health: '/v1/views/health',
    workers: '/v1/views/workers',
    workerRecord: '/v1/views/workers/record',
    positions: '/v1/views/positions',
    upload: '/v1/sources/upload',
    agentFiles: '/v1/agent-files',
    agentNotes: '/v1/agent-files/notes',
    pendingExports: '/v1/sources/pending-exports',
    importProgress: '/v1/sources/import-progress',
    approvedConnections: '/v1/views/connections',
    sourceCoverage: '/v1/sources/connectors',
    sourceConnections: '/v1/connectors',
    pipedreamAccounts: '/v1/views/pipedream/accounts',
    forgetApprove: '/v1/forget/approve',
    forget: '/v1/forget',
    worldDelete: '/v1/world/delete',
    materials: '/v1/materials',
    materialFiles: '/v1/materials/files',
    materialNotes: '/v1/materials/notes',
    materialDetail: '/v1/materials/{id}',
    materialEdit: '/v1/materials/notes/{id}',
    materialPreview: '/v1/materials/{id}/delete-preview',
    materialConfirmation: '/v1/materials/{id}/delete-confirmations',
    materialDelete: '/v1/materials/{id}/delete',
    googleStart: '/v1/auth/google/start?next=/',
    emailStart: '/v1/auth/email/start',
    emailVerify: '/v1/auth/email/verify',
    logout: '/v1/auth/logout',
  };

  // The server's pages the views name: the address after a sign-in, and the sign-in card's link.
  var PAGES = { home: '/', signin: '/signin', login: '/login', signup: '/signup', logout: '/logout', privacy: '/privacy' };
  function isAuthPage() {
    return [PAGES.signin, PAGES.login, PAGES.signup].indexOf((window.location || {}).pathname) !== -1;
  }

  // The way back after a sign-in that the consent page asked for: /signin?next=<path>. Only the
  // consent page's own path passes, in printable ASCII with no backslash, and at most as long as
  // the server keeps for Google (2048). Any other next is ignored and the sign-in goes on as usual.
  var NEXT_PREFIX = '/authorize?', MAX_NEXT = 2048;
  function returnPath() {
    var loc = window.location || {}, next = null;
    if (!isAuthPage()) return null;
    try { next = new URLSearchParams(loc.search || '').get('next'); } catch (_) { return null; }
    if (typeof next !== 'string' || next.length > MAX_NEXT || next.indexOf(NEXT_PREFIX) !== 0) return null;
    if (!/^[\x21-\x7e]+$/.test(next) || next.indexOf('\\') !== -1) return null;
    return next;
  }
  // Leaves for the consent page when there is a next; says whether it did.
  function returnToNext() {
    var next = returnPath();
    if (!next) return false;
    window.location.assign(next);
    return true;
  }
  function googleStartPath() {
    var next = returnPath();
    return next ? ENDPOINTS.googleStart.split('?')[0] + '?next=' + encodeURIComponent(next) : ENDPOINTS.googleStart;
  }
  function authPagePath(page) {
    var next = returnPath();
    return page + (next ? '?next=' + encodeURIComponent(next) : '');
  }

  // Grove's starter kinds draw with the sim's visual types. The data keeps the real kind
  // name (legend, search, panel); only size, tier, hub role and ring colour are borrowed.
  // person, task, event, observation and decision are sim types already; any other kind
  // draws in the task style under its own name.
  var KIND_STYLE = {
    organisation: 'company',
    plan: 'project',
    place: 'instance',
    problem: 'blocker',
    assumption: 'pattern',
    trip: 'goal',
    period: 'roadmap',
    belief: 'pattern',
  };

  var MAX_POSITIONS = 5000;   // the server takes 1 to 5000 entries per request
  var MAX_COORD = 1e7;        // and refuses a coordinate beyond this

  function read(response) {
    return response.text().then(function (text) {
      var body = null;
      try { body = text ? JSON.parse(text) : null; } catch (_) { body = null; }
      return { ok: !!response.ok, status: response.status, body: body };
    });
  }

  // Resolves {ok, status, body} for any HTTP answer; rejects only when the request fails.
  function request(method, path, body) {
    var init = { method: method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    return window.fetch(path, init).then(read);
  }
  // A views call (graph, node, schema, health, positions) that answers 401 means the session has
  // ended. The page hears it once, as 'grove:unauthenticated' (product.js signs out and clears the
  // graph); a sign-in that the server accepts arms it again. A call sent before that sign-in speaks
  // for the session that ended, so its 401 ends nothing. The auth calls never send it: their answers
  // belong to the sign-in card.
  var signedOutSent = false, sessionsBegun = 0, sessionAccount = null;
  var AUTH_CHANGE_KEY = 'grove-auth-change';
  function notifySessionChange() {
    // No identity, token, or account data crosses this browser signal.
    try { localStorage.setItem(AUTH_CHANGE_KEY, JSON.stringify({nonce: window.crypto.randomUUID()})); } catch (_) {}
  }
  // A deleted Grove: another tab of this browser must drop the old world, not save it back.
  var WORLD_RESET_KEY = 'grove-world-reset';
  function notifyWorldReset() {
    try { localStorage.setItem(WORLD_RESET_KEY, JSON.stringify({nonce: window.crypto.randomUUID()})); } catch (_) {}
  }
  function invalidateSession() { sessionsBegun++; sessionAccount = null; }
  if (window.addEventListener) window.addEventListener('storage', function (event) {
    if (event.key === WORLD_RESET_KEY && event.newValue) { window.dispatchEvent(new CustomEvent('grove:world-reset')); return; }
    if (event.key !== AUTH_CHANGE_KEY || !event.newValue) return;
    invalidateSession();
    window.dispatchEvent(new CustomEvent('grove:session-changed'));
  });
  function watchSession(sentIn, res) {
    var code = res.body && res.body.error && res.body.error.code;
    if ((res.status === 401 || code === 'unauthenticated') && !signedOutSent && sentIn === sessionsBegun) {
      signedOutSent = true;
      if (sessionAccount) notifySessionChange();
      window.dispatchEvent(new CustomEvent('grove:unauthenticated'));
    }
    return res;
  }
  function signedIn(res) {
    if (res.ok) {
      signedOutSent = false;
      var account = res.body && res.body.account;
      if (account && account !== sessionAccount) { sessionAccount = account; sessionsBegun++; }
    }
    return res;
  }
  function newSession(res) { if (res.ok) { sessionsBegun++; notifySessionChange(); } return signedIn(res); }
  function getJSON(path) { return request('GET', path).then(watchSession.bind(null, sessionsBegun)); }
  function postJSON(path, body) { return request('POST', path, body === undefined ? {} : body).then(watchSession.bind(null, sessionsBegun)); }
  function postAuth(path, body) { return request('POST', path, body === undefined ? {} : body); }

  // A worker read speaks only for the session that sent it: a later answer is stale, and its 401 ends nothing.
  function workerRead(path) {
    var sentIn = sessionsBegun;
    return request('GET', path).then(function (res) {
      if (sentIn !== sessionsBegun) return {ok: false, status: 409, stale: true, body: null};
      return watchSession(sentIn, res);
    });
  }

  function materialRequest(action, id, body, key, signal) {
    var routes = {list: ['GET', 'materials'], detail: ['GET', 'materialDetail'],
      file: ['POST', 'materialFiles'], note: ['POST', 'materialNotes'], edit: ['PUT', 'materialEdit'],
      preview: ['GET', 'materialPreview'], confirm: ['POST', 'materialConfirmation'], delete: ['POST', 'materialDelete']};
    var route = routes[action];
    if (!route) return Promise.reject(new Error('Unknown material action.'));
    var path = ENDPOINTS[route[1]];
    if (path.includes('{id}')) {
      if (typeof id !== 'string' || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(id)) return Promise.reject(new Error('Invalid material identifier.'));
      path = path.replace('{id}', id);
    }
    var init = {method: route[0], credentials: 'same-origin', cache: 'no-store',
      signal: signal, headers: {Accept: 'application/json'}};
    if (action === 'list' && body) path += '?cursor=' + encodeURIComponent(body);
    if (route[0] !== 'GET') {
      init.headers['X-Grove-Action'] = 'materials';
      if (key) init.headers['Idempotency-Key'] = key;
      if (action === 'file') {
        var mimes = {txt: 'text/plain', md: 'text/markdown', pdf: 'application/pdf',
          docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
          pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation'};
        var mime = body instanceof File && mimes[body.name.split('.').pop().toLowerCase()];
        if (!mime) return Promise.reject(new Error('Use a TXT, Markdown, PDF, DOCX or PPTX file.'));
        init.headers['Content-Type'] = mime;
        init.headers['X-Grove-Filename'] = encodeURIComponent(body.name);
        init.body = body;
      } else {
        init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body);
      }
    }
    var sentIn = sessionsBegun;
    return window.fetch(path, init).then(read).then(function (res) {
      if (sentIn !== sessionsBegun || (signal && signal.aborted)) return {ok: false, status: 409, stale: true, body: null};
      return watchSession(sentIn, res);
    });
  }

  // Upload the selected file itself. JSON request() would serialize only its metadata.
  // Keep HTTP errors as responses so the upload form can retain the file for a retry.
  function uploadExport(provider, file) {
    var code = { chatgpt: 'chatgpt', claude: 'claude_ai' }[provider];
    if (!code || !(file instanceof File) || !/\.(json|zip)$/i.test(file.name)) {
      return Promise.reject(new Error('Choose a ChatGPT or Claude export in .json or .zip format.'));
    }
    return window.fetch(ENDPOINTS.upload + '?provider=' + code, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { Accept: 'application/json', 'Content-Type': /\.zip$/i.test(file.name) ? 'application/zip' : 'application/json' },
      body: file,
    }).then(read);
  }

  // A typed server error ({"error": {"code", "message", ...}}) as one line of text.
  function errorText(body, fallback) {
    var error = body && body.error;
    if (error && typeof error === 'object' && error.message) return String(error.message);
    if (typeof error === 'string' && error) return error;
    return fallback || 'Grove could not complete that request';
  }

  // Positions are insert-only on the server: an item that already has one keeps it.
  // Batches go one after another; a failed batch rejects, and the caller retries next load.
  function savePositions(list) {
    var clean = (list || []).filter(function (p) {
      return p && typeof p.id === 'string' && p.id &&
        typeof p.x === 'number' && typeof p.y === 'number' && isFinite(p.x) && isFinite(p.y) &&
        Math.abs(p.x) <= MAX_COORD && Math.abs(p.y) <= MAX_COORD;
    }).map(function (p) { return { id: p.id, x: p.x, y: p.y }; });
    var batches = [];
    for (var i = 0; i < clean.length; i += MAX_POSITIONS) batches.push(clean.slice(i, i + MAX_POSITIONS));
    return batches.reduce(function (chain, batch) {
      return chain.then(function (results) {
        return postJSON(ENDPOINTS.positions, { positions: batch }).then(function (res) {
          if (!res.ok) throw new Error(errorText(res.body, 'Positions were not saved (' + res.status + ')'));
          results.push(res.body);
          return results;
        });
      });
    }, Promise.resolve([]));
  }

  window.GroveLive = {
    ENDPOINTS: ENDPOINTS,
    mcpUrl: function () { return new URL(ENDPOINTS.mcp, window.location.href).href; },
    PAGES: PAGES,
    isAuthPage: isAuthPage,
    authPagePath: authPagePath,
    invalidateSession: invalidateSession,
    notifySessionChange: notifySessionChange,
    notifyWorldReset: notifyWorldReset,
    KIND_STYLE: KIND_STYLE,
    getJSON: getJSON,
    postJSON: postJSON,
    materialRequest: materialRequest,
    uploadExport: uploadExport,
    workers: function () { return workerRead(ENDPOINTS.workers); },
    workerRecord: function () { return workerRead(ENDPOINTS.workerRecord); },
    pendingExports: function () { return getJSON(ENDPOINTS.pendingExports); },
    // D14's two steps, for one target the person confirmed in this page.
    forget: function (target) {
      return postJSON(ENDPOINTS.forgetApprove, { target: target, confirm: true }).then(function (res) {
        if (!res.ok) return res;
        if (!res.body || !res.body.approval_token) return { ok: false, status: res.status, body: null };
        return postJSON(ENDPOINTS.forget, { target: target, approval_token: res.body.approval_token });
      });
    },
    deleteWorld: function () { return postJSON(ENDPOINTS.worldDelete, { confirm: 'DELETE' }); },
    approvedConnections: function () {
      var sentIn = sessionsBegun;
      return request('GET', ENDPOINTS.approvedConnections).then(function (res) {
        if (sentIn !== sessionsBegun) return {ok: false, status: 409, stale: true, body: null};
        return watchSession(sentIn, res);
      });
    },
    // One provider's import_progress.v1. An answer sent for an earlier session is stale, and its
    // 401 ends nothing. A Muse read names the approved connection it was chosen under.
    importProgress: function (provider, clientId) {
      var code = {chatgpt: 'chatgpt', claude: 'claude_ai', muse: 'muse'}[provider];
      if (typeof code !== 'string') return Promise.reject(new Error('Unknown import provider.'));
      if (provider === 'muse' ? typeof clientId !== 'string' || !clientId || new TextEncoder().encode(clientId).length > 2048 : clientId != null) {
        return Promise.reject(new Error('Choose an approved connection for Muse only.'));
      }
      var sentIn = sessionsBegun;
      var selection = provider === 'muse' ? '&client_id=' + encodeURIComponent(clientId) : '';
      return request('GET', ENDPOINTS.importProgress + '?provider=' + code + '&limit=1' + selection).then(function (res) {
        if (sentIn !== sessionsBegun) return {ok: false, status: 409, stale: true, body: null};
        return watchSession(sentIn, res);
      });
    },
    pendingExports: function () { return getJSON(ENDPOINTS.pendingExports); },
    disconnectConnection: function (id) { return postJSON(ENDPOINTS.sourceConnections + '/' + encodeURIComponent(id) + '/disconnect', {}); },
    errorText: errorText,
    agentFiles: function () {
      var sentIn = sessionsBegun;
      return request('GET', ENDPOINTS.agentFiles).then(function (res) {
        if (sentIn !== sessionsBegun) return {ok: false, status: 409, stale: true, body: null};
        return watchSession(sentIn, res);
      });
    },
    addAgentNote: function (title, body) { return postJSON(ENDPOINTS.agentNotes, { title: title, body: body }); },
    addAgentFile: function (file) {
      return window.fetch(ENDPOINTS.agentFiles + '?name=' + encodeURIComponent(file.name), {
        method: 'POST',
        credentials: 'same-origin',
        headers: { Accept: 'application/json', 'Content-Type': 'application/octet-stream' },
        body: file,
      }).then(read).then(watchSession.bind(null, sessionsBegun));
    },
    removeAgentFile: function (kind, name) {
      return request('DELETE', ENDPOINTS.agentFiles + '/' + kind + '/' + encodeURIComponent(name)).then(watchSession.bind(null, sessionsBegun));
    },
    requestExport: function (provider) {
      var codes = { chatgpt: 'chatgpt', claude: 'claude_ai', chats: 'whatsapp', muse: 'muse', grok: 'grok' };
      if (!Object.prototype.hasOwnProperty.call(codes, provider)) return Promise.reject(new Error('Unknown export provider.'));
      return postJSON(ENDPOINTS.pendingExports + '/' + codes[provider], {});
    },
    errorText: errorText,
    savePositions: savePositions,
    // GET the session: 200 {signed_in, self_item_id, has_items, account, method} when signed in, 401 otherwise.
    session: function () {
      var sentIn = sessionsBegun;
      return request('GET', ENDPOINTS.session).then(function (res) {
        if (sentIn !== sessionsBegun) return {ok: false, status: 409, stale: true, body: null};
        return signedIn(res);
      });
    },
    // Google signs in on its own pages and comes back to the start of the app, or to the consent
    // page that sent the person to sign in.
    googleSignIn: function () { window.location.assign(googleStartPath()); },
    returnPath: returnPath,
    returnToNext: returnToNext,
    emailStart: function (email) { return postAuth(ENDPOINTS.emailStart, { email: String(email || '').trim() }); },
    emailVerify: function (email, code) {
      var sentIn = sessionsBegun;
      return postAuth(ENDPOINTS.emailVerify, { email: String(email || '').trim(), code: String(code || '').trim() }).then(function (res) {
        if (sentIn !== sessionsBegun) return {ok: false, status: 409, stale: true, body: null};
        return newSession(res);
      });
    },
    logout: function () {
      invalidateSession();
      return postAuth(ENDPOINTS.logout).then(function (res) {
        if (res.ok) { invalidateSession(); notifySessionChange(); }
        return res;
      });
    },
  };
})();
