/* Paces the real graph of an accepted import. The view sees the nodes the server holds, a few at a
 * time. Nothing here is invented or stored: every node and link came from the server's graph, and
 * only the graph read is answered here, so node detail and every other read stay on the real API.
 * Live mode only: the demo build leaves this file out.
 */
(function () {
  'use strict';
  var TICK_MS = 1500, REFRESH_MS = 5000, SHARE = 0.1, MAX_STEP = 24;
  var run = null;

  // The next count of visible nodes: at least one more, a small share of what waits, never more
  // than the cap. A burst of 300 plays over about a minute; 5,000 takes about five minutes.
  function nextVisibleCount(visible, total) {
    var waiting = total - visible;
    if (waiting <= 0) return visible;
    return visible + Math.min(waiting, MAX_STEP, Math.max(1, Math.ceil(waiting * SHARE)));
  }
  function atOnce() {
    return !!window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }
  function valid(target) {
    return target === run && window.GroveSimulation.isPendingExportContext(target.context);
  }
  function clear() {
    if (run) window.clearTimeout(run.timer);
    run = null;
  }
  function reload() {
    if (typeof window.reloadGrove !== 'function') return;
    try { Promise.resolve(window.reloadGrove()).catch(function () {}); } catch (_) { /* the graph may not have mounted yet */ }
  }
  function reveal(target, count) {
    target.waiting.splice(0, count).forEach(function (id) { target.seen.add(id); target.added.add(id); });
    target.view = null;
  }
  // The visible prefix of the last server graph: only nodes the person may see, and only links
  // whose two ends are among them.
  function visible(target) {
    if (target.view) return target.view;
    var elements = target.body.elements, shown = new Set();
    var nodes = elements.nodes.filter(function (node) {
      if (!target.seen.has(node.data.id)) return false;
      shown.add(node.data.id);
      return true;
    });
    var edges = elements.edges.filter(function (edge) { return shown.has(edge.data.source) && shown.has(edge.data.target); });
    target.view = {nodes: nodes, edges: edges};
    return target.view;
  }
  // The identity node the views add when the server names no self item is not a real item.
  function counted(target) {
    var graph = visible(target), self = target.body.self_item_id != null;
    return {
      nodes: graph.nodes.filter(function (node) { return self || node.data.id !== 'grove-self'; }),
      edges: graph.edges.filter(function (edge) { return self || (edge.data.source !== 'grove-self' && edge.data.target !== 'grove-self'); }),
    };
  }
  // A new read keeps what the person sees and queues what is new, hubs first. A node the server no
  // longer holds leaves the queue and the view.
  function merge(target, body) {
    var ids = new Set(), degree = {};
    body.elements.nodes.forEach(function (node) { ids.add(node.data.id); });
    if (target.seed) { ids.forEach(function (id) { target.seen.add(id); }); target.seed = false; }
    body.elements.edges.forEach(function (edge) {
      degree[edge.data.source] = (degree[edge.data.source] || 0) + 1;
      degree[edge.data.target] = (degree[edge.data.target] || 0) + 1;
    });
    target.waiting = target.waiting.filter(function (id) { return ids.has(id); });
    var queued = new Set(target.waiting), fresh = [];
    ids.forEach(function (id) { if (!target.seen.has(id) && !queued.has(id)) fresh.push(id); });
    fresh.sort(function (a, b) { return (degree[b] || 0) - (degree[a] || 0); });
    target.waiting = target.waiting.concat(fresh);
    target.body = body;
    target.view = null;
    if (atOnce()) reveal(target, target.waiting.length);
  }
  function refresh(target) {
    if (target.fetching) return target.read;
    var settling = !target.moving;
    target.fetching = true;
    target.fetchedAt = Date.now();
    target.read = window.GroveSimulation.readLiveGraph().then(function (res) {
      return res.json().then(function (body) { return {ok: res.ok, body: body}; });
    }).then(function (res) {
      target.fetching = false;
      if (target !== run) return;
      if (!valid(target)) { clear(); return; }
      if (!res.ok || !res.body || !res.body.elements) return;   // the last graph stays
      var before = target.body ? visible(target) : null;
      merge(target, res.body);
      var after = visible(target);
      if (settling) target.settled = true;
      if (!before || before.nodes.length !== after.nodes.length || before.edges.length !== after.edges.length) reload();
    }).catch(function () { target.fetching = false; });
    return target.read;
  }
  function finish() {
    clear();
    reload();   // the normal read takes over, and it shows the same graph
  }
  function tick(target) {
    if (!valid(target)) { if (target === run) clear(); return; }
    var count = target.waiting.length;
    if (count) {
      var done = target.added.size;
      reveal(target, atOnce() ? count : nextVisibleCount(done, done + count) - done);
      reload();
    }
    if (!target.settled && !target.fetching && Date.now() - target.fetchedAt >= REFRESH_MS) refresh(target);
    if (target.settled && !target.fetching && !target.waiting.length) { finish(); return; }
    target.timer = window.setTimeout(function () { tick(target); }, TICK_MS);
  }
  function start() {
    var api = window.GroveSimulation;
    if (!api || !api.isLive() || !api.getState().auth) return;
    var view = window.GroveView && window.GroveView.nodes ? window.GroveView.nodes() : [];
    var target = {context: api.pendingExportContext(), seen: new Set(view.map(function (node) { return node.id; })),
      added: new Set(), waiting: [], body: null, view: null, moving: true, settled: false, fetching: false,
      fetchedAt: 0, read: null, timer: null, seed: !api.getState().graphCheckedAt};
    run = target;
    refresh(target);
    target.timer = window.setTimeout(function () { tick(target); }, TICK_MS);
  }
  // The server accepted an upload: nodes may arrive from now on.
  function accept() {
    if (!run) { start(); return; }
    run.moving = true; run.settled = false;
  }
  // Each progress read says whether any work moves. A page that opens on moving work starts here.
  function observe(moving) {
    if (!run) { if (moving) start(); return; }
    run.moving = !!moving;
    if (moving) run.settled = false;
  }
  function answer(raw) {
    if (!run) return null;
    var url = new URL(raw, window.location.href), target = run;
    if (url.pathname !== '/api/graph' || url.search) return null;
    return Promise.resolve(target.read).then(function () {
      var ready = target === run && target.body, body;
      if (ready) {
        var graph = visible(target);
        body = Object.assign({}, target.body, {elements: graph,
          stats: Object.assign({}, target.body.stats, {total_nodes: graph.nodes.length, total_edges: graph.edges.length})});
      } else body = {error: 'Graph request superseded', code: 'superseded'};
      return {ok: !!ready, status: ready ? 200 : 409, json: function () { return Promise.resolve(JSON.parse(JSON.stringify(body))); }};
    });
  }
  // Counts follow what the person sees, not what waits. Called on every state read, so no call to
  // getState here.
  function project(state) {
    if (!run || !run.body) return state;
    var graph = counted(run), types = {}, added = run.added;
    graph.nodes.forEach(function (node) { types[node.data.type] = (types[node.data.type] || 0) + 1; });
    return Object.assign({}, state, {currentNodeCount: graph.nodes.length, currentEdgeCount: graph.edges.length,
      currentNodeTypes: types, nodeDelta: graph.nodes.filter(function (node) { return added.has(node.data.id); }).length,
      edgeDelta: graph.edges.filter(function (edge) { return added.has(edge.data.source) || added.has(edge.data.target); }).length,
      removedNodes: 0, removedEdges: 0});
  }
  function changes(provider) {
    if (provider || !run || !run.body) return {new_nodes: [], new_edges: []};
    var graph = counted(run), added = run.added;
    return JSON.parse(JSON.stringify({
      new_nodes: graph.nodes.filter(function (node) { return added.has(node.data.id); }).map(function (node) { return node.data; }),
      new_edges: graph.edges.filter(function (edge) { return added.has(edge.data.source) || added.has(edge.data.target); }).map(function (edge) { return edge.data; }),
      removed_nodes: 0, removed_edges: 0}));
  }
  ['grove:unauthenticated', 'grove:session-changed'].forEach(function (name) { window.addEventListener(name, clear); });
  window.GroveImportGrowth = {accept: accept, observe: observe, clear: clear, active: function () { return !!run; },
    answer: answer, project: project, changes: changes, nextVisibleCount: nextVisibleCount};
}());
