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
    positions: '/v1/views/positions',
    upload: '/v1/sources/upload',
    pendingExports: '/v1/sources/pending-exports',
    sourceCoverage: '/v1/sources/connectors',
    sourceConnections: '/v1/connectors',
    googleStart: '/v1/auth/google/start?next=/',
    emailStart: '/v1/auth/email/start',
    emailVerify: '/v1/auth/email/verify',
    logout: '/v1/auth/logout',
  };

  // The server's pages the views name: the address after a sign-in, and the sign-in card's link.
  var PAGES = { home: '/', signin: '/signin', privacy: '/privacy' };

  // The way back after a sign-in that the consent page asked for: /signin?next=<path>. Only the
  // consent page's own path passes, in printable ASCII with no backslash, and at most as long as
  // the server keeps for Google (2048). Any other next is ignored and the sign-in goes on as usual.
  var NEXT_PREFIX = '/authorize?', MAX_NEXT = 2048;
  function returnPath() {
    var loc = window.location || {}, next = null;
    if (loc.pathname !== PAGES.signin) return null;
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
  function watchSession(sentIn, res) {
    var code = res.body && res.body.error && res.body.error.code;
    if ((res.status === 401 || code === 'unauthenticated') && !signedOutSent && sentIn === sessionsBegun) {
      signedOutSent = true;
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
  function newSession(res) { if (res.ok) sessionsBegun++; return signedIn(res); }
  function getJSON(path) { return request('GET', path).then(watchSession.bind(null, sessionsBegun)); }
  function postJSON(path, body) { return request('POST', path, body === undefined ? {} : body).then(watchSession.bind(null, sessionsBegun)); }
  function postAuth(path, body) { return request('POST', path, body === undefined ? {} : body); }

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
    KIND_STYLE: KIND_STYLE,
    getJSON: getJSON,
    postJSON: postJSON,
    uploadExport: uploadExport,
    pendingExports: function () { return getJSON(ENDPOINTS.pendingExports); },
    requestExport: function (provider) {
      var codes = { chatgpt: 'chatgpt', claude: 'claude_ai', chats: 'whatsapp', muse: 'muse', grok: 'grok' };
      if (!Object.prototype.hasOwnProperty.call(codes, provider)) return Promise.reject(new Error('Unknown export provider.'));
      return postJSON(ENDPOINTS.pendingExports + '/' + codes[provider], {});
    },
    errorText: errorText,
    savePositions: savePositions,
    // GET the session: 200 {signed_in, self_item_id, has_items, account, method} when signed in, 401 otherwise.
    session: function () { return request('GET', ENDPOINTS.session).then(signedIn); },
    // Google signs in on its own pages and comes back to the start of the app, or to the consent
    // page that sent the person to sign in.
    googleSignIn: function () { window.location.assign(googleStartPath()); },
    returnPath: returnPath,
    returnToNext: returnToNext,
    emailStart: function (email) { return postAuth(ENDPOINTS.emailStart, { email: String(email || '').trim() }); },
    emailVerify: function (email, code) {
      return postAuth(ENDPOINTS.emailVerify, { email: String(email || '').trim(), code: String(code || '').trim() }).then(newSession);
    },
    logout: function () { return postAuth(ENDPOINTS.logout).then(function (res) { if (res.ok) { sessionsBegun++; sessionAccount = null; } return res; }); },
  };
})();
