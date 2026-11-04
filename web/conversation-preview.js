/* Only the Claude import illustration uses this page-memory graph.
 * Authentication, provider consent, files and AI access stay on their live services. */
(function () {
  'use strict';
  var data = null, context = null, epoch = 0, timer = null, step = 0, ids = [], history = [];
  var tick = 3000, steps = 18;
  function clear() {
    epoch++; window.clearTimeout(timer); timer = null;
    data = null; context = null; step = 0; ids = []; history = [];
  }
  function active() { return !!data; }
  function valid(ticket) {
    return ticket === epoch && window.GroveSimulation.isPendingExportContext(context);
  }
  function elements(identity) {
    if (!data) return {nodes: [], edges: []};
    var base = Math.min(10, ids.length);
    var count = step === 0 ? 1 : step >= steps ? ids.length :
      Math.round(base + (ids.length - base) * Math.pow((step - 1) / (steps - 1), 1.55));
    var visible = new Set(ids.slice(0, count));
    var nodes = data.elements.nodes.filter(function (node) { return visible.has(node.data.id); });
    var edges = data.elements.edges.filter(function (edge) {
      return visible.has(edge.data.source) && visible.has(edge.data.target);
    });
    var name = identity || window.GroveSimulation.getState().name || 'You';
    nodes = nodes.concat([{data: {id: 'grove-self', type: 'person', label: name,
      status: 'active', summary: 'Your Grove', created_at: '2026-10-28T00:00:00Z'}}]);
    nodes.filter(function (node) { return ['company', 'project'].includes(node.data.type); }).slice(0, 16).forEach(function (node) {
      edges.push({data: {id: 'self-' + node.data.id, source: 'grove-self', target: node.data.id,
        verb: 'CONNECTS TO', label: 'CONNECTS TO'}});
    });
    return {nodes: nodes, edges: edges};
  }
  function counts(graph) {
    var types = {};
    graph.nodes.forEach(function (node) { types[node.data.type] = (types[node.data.type] || 0) + 1; });
    return {nodes: graph.nodes.length, edges: graph.edges.length, types: types};
  }
  // No call to getState here: the live state engine uses this projection.
  function view() {
    if (!data) return null;
    return {provider: 'claude', label: 'Claude example', progress: Math.round(step / steps * 100),
      status: step < steps ? 'importing' : 'ready', step: step};
  }
  function announce() {
    window.dispatchEvent(new CustomEvent('grove:change'));
    if (window.reloadGrove) Promise.resolve(window.reloadGrove()).catch(function () {});
  }
  function advance(ticket) {
    if (!valid(ticket)) { clear(); return; }
    step++;
    var total = counts(elements());
    history.push({at: new Date().toISOString(), nodes: total.nodes, edges: total.edges});
    announce();
    if (step < steps) timer = window.setTimeout(function () { advance(ticket); }, tick);
  }
  async function start() {
    var api = window.GroveSimulation;
    if (!api.isLive() || !api.getState().auth) throw new Error('Sign in before you start the Claude example.');
    clear();
    context = api.pendingExportContext();
    var ticket = epoch;
    await window.GroveDeferred.load('claude-preview');
    if (!valid(ticket)) throw new Error('Your account changed. Start the example again.');
    var candidate = window.GroveClaudePreviewData;
    if (!candidate || !candidate.elements || !Array.isArray(candidate.elements.nodes) ||
        !Array.isArray(candidate.elements.edges) || !candidate.source_node_ids) {
      throw new Error('The Claude example could not load. Try again.');
    }
    data = candidate;
    var allowed = new Set(data.source_node_ids.conversations || []);
    ids = data.elements.nodes.filter(function (node) { return allowed.has(node.data.id); }).map(function (node) { return node.data.id; });
    if (window.GroveView && window.GroveView.clear) window.GroveView.clear();
    announce();
    timer = window.setTimeout(function () { advance(ticket); }, tick);
  }
  function graph() {
    var value = elements(), total = counts(value);
    return {elements: value, stats: {total_nodes: total.nodes, total_edges: total.edges,
      by_type: total.types, embedded: total.nodes, edge_verbs: data.stats.edge_verbs},
      time: data.time, generated_at: data.generated_at};
  }
  function answer(raw) {
    if (!data) return null;
    var url = new URL(raw, window.location.href), path = url.pathname, body;
    if (path === '/api/graph') body = graph();
    else if (path === '/api/schema') body = data.schema || {};
    else if (path === '/api/history') body = history;
    else if (path === '/api/health' || path === '/health') {
      var total = counts(elements());
      body = {nodes: total.nodes, edges: total.edges, by_type: total.types, overall: {state: 'unmeasured'}};
    } else if (path.indexOf('/api/node/') === 0) {
      var id = decodeURIComponent(path.slice('/api/node/'.length));
      var node = elements().nodes.find(function (row) { return row.data.id === id; });
      body = node ? {node: Object.assign({}, node.data, {title: node.data.label, content: node.data.summary}),
        required_props: [], optional_props: [], required_prop_names: [], status_field: 'status'} : {error: 'No such visible node'};
    } else body = {error: 'No example endpoint for this request'};
    return Promise.resolve({ok: !body.error, status: body.error ? 404 : 200,
      json: function () { return Promise.resolve(JSON.parse(JSON.stringify(body))); }});
  }
  function project(state) {
    if (!data) return state;
    var total = counts(elements(state.name || 'You'));
    return Object.assign({}, state, {conversationPreview: view(), currentNodeCount: total.nodes,
      currentEdgeCount: total.edges, currentNodeTypes: total.types, nodeDelta: total.nodes - 1, edgeDelta: total.edges,
      growthHistory: history.slice(), removedNodes: 0, removedEdges: 0});
  }

  function changes() {
    var current = elements();
    return {new_nodes: current.nodes.map(function (n) { return n.data; }),
      new_edges: current.edges.map(function (e) { return e.data; }), removed_nodes: 0, removed_edges: 0};
  }
  ['grove:unauthenticated', 'grove:session-changed'].forEach(function (name) { window.addEventListener(name, clear); });
  window.GroveConversationPreview = {start: start, clear: clear, active: active, view: view,
    project: project, answer: answer, changes: changes};
}());
