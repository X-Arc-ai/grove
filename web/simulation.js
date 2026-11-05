/* Grove's local progressive simulation engine.
 * The native graph reads the same API shapes as the real visualizer, while
 * onboarding and background imports remain browser-local and fixture-only.
 * Live mode (no injected fixture; only the checks inject one): the native graph's
 * /api/* reads go to the server through web/live.js, the self item takes the identity
 * node's id, counts come from the last graph payload, and imports add nothing (empty plan).
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'grove-minimal-codex-v3';
  var VERSION = 3;
  var SOURCE_ORDER = ['conversations', 'calendar', 'chats', 'notes'];
  var SOURCE_COPY = {
    conversations: { label: 'AI conversations', description: 'The conversations that shaped your work' },
    calendar: { label: 'Calendar', description: 'Plans, meetings and moments over time' },
    chats: { label: 'Chats', description: 'The people and decisions in your messages' },
    notes: { label: 'Notes and files', description: 'The documents where your work lives' },
  };
  var HOSTS = { claude: 1, chatgpt: 1, muse: 1, grok: 1 };
  var SOURCE_TYPE_ORDER = {
    conversations: ['project', 'company', 'decision', 'person', 'task', 'observation', 'pattern', 'event', 'client'],
    calendar: ['event', 'person', 'project', 'decision', 'task', 'observation', 'company', 'client', 'pattern'],
    chats: ['person', 'decision', 'event', 'project', 'task', 'observation', 'company', 'client', 'pattern'],
    notes: ['project', 'company', 'task', 'observation', 'pattern', 'decision', 'person', 'client', 'event'],
  };
  var TICK_MS = Math.max(20, Number(window.__GROVE_SIM_TICK_MS__) || 1000);
  var JOB_STEPS = 18;
  var fixture = window.GROVE_FIXTURE || null;
  // No fixture: live mode. The page never fetches a fixture; the engine runs on an empty one.
  var LIVE = !fixture;
  var EMPTY = { elements: { nodes: [], edges: [] }, source_node_ids: {}, stats: { edge_verbs: [] }, schema: {}, history: [], health: { status: 'healthy' } };
  function normalizeFixture(data){data.elements.edges.forEach(function(e,i){if(!e.data.id)e.data.id="fixture-edge-"+i;});return data;}
  if(fixture)normalizeFixture(fixture);
  var fixturePromise = Promise.resolve(fixture || EMPTY);
  var jobs = {};
  var importEpoch = 0;
  var orderedCache = {};
  var nodeByIdCache = null;

  function blankSource(id) {
    return {
      id: id, status: 'idle', progress: 0, loadedCount: 0,
      name: null, size: null, provider: null, providers: [],
      startedAt: null, importedAt: null, error: null,
    };
  }

  function freshState() {
    var sources = {};
    SOURCE_ORDER.forEach(function (id) { sources[id] = blankSource(id); });
    return {
      schema: 4, growthHistory: [], requests: [], requestsSeeded: false, growth: { focus: '', reading: '', avoid: '', confirm: true }, files: {}, recentNodes: [], recentEdges: [], version: VERSION, auth: null, account: null, name: '', onboardingComplete: false, onboardingStep: 0, tourComplete: false,
      mcpClients: [], hostConnected: false, connectedHosts: [], pendingExports: {}, accountImports: {}, googleConnected: false, nodeDelta: 0, edgeDelta: 0, updatedAt: null, sources: sources,
    };
  }

  function readState() {
    var value = freshState();
    try {
      var stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
      if (!stored || stored.version !== VERSION) return value;
      value.auth = Object.prototype.hasOwnProperty.call(stored,'auth') ? stored.auth : (stored.onboardingComplete && !LIVE ? {provider:'email',email:'anas@example.com'} : null);
      value.account = typeof stored.account === 'string' && stored.account ? stored.account : null;
      value.growthHistory = Array.isArray(stored.growthHistory) ? stored.growthHistory.filter(function(p){return p&&Number.isFinite(p.nodes)&&Number.isFinite(p.edges)&&p.nodes>=0&&p.edges>=0;}).slice(-240) : [];
      // Live mode keeps no local questions (views-b brings them from the server); an older page
      // saved some with store titles in them, so they are dropped here.
      value.requests = !LIVE && Array.isArray(stored.requests) ? stored.requests : [];
      value.requestsSeeded = !!stored.requestsSeeded;
      value.mcpClients = Array.isArray(stored.mcpClients) ? stored.mcpClients.filter(function(c){return c&&typeof c.id==='string'&&typeof c.name==='string';}) : [];
      value.pendingExports = !LIVE && stored.pendingExports && typeof stored.pendingExports === 'object' ? stored.pendingExports : {};
      value.googleConnected = !!stored.googleConnected;
      value.accountImports = stored.accountImports || {};
      value.name = typeof stored.name === 'string' ? stored.name : '';
      value.onboardingComplete = !!stored.onboardingComplete;
      value.onboardingStep = Math.max(0, Math.min(3, Number(stored.onboardingStep) || 0));
      value.growth = Object.assign(value.growth, stored.growth || {});
      value.files = stored.files || {};
      value.recentNodes = stored.recentNodes || []; value.recentEdges = stored.recentEdges || [];
      value.tourComplete = !!stored.tourComplete;
      value.connectedHosts = Array.isArray(stored.connectedHosts) ? stored.connectedHosts.filter(function (id) { return HOSTS[id]; }) : [];
      value.hostConnected = value.connectedHosts.length > 0 || !!stored.hostConnected;
      value.nodeDelta = Number(stored.nodeDelta) || 0;
      value.edgeDelta = Number(stored.edgeDelta) || 0;
      value.updatedAt = stored.updatedAt || null;
      SOURCE_ORDER.forEach(function (id) {
        var saved = stored.sources && stored.sources[id];
        if (!saved) return;
        value.sources[id] = Object.assign(blankSource(id), saved, { id: id });
        value.sources[id].loadedCount = Math.max(0, Number(value.sources[id].loadedCount) || 0);
        value.sources[id].providers = Array.isArray(value.sources[id].providers) ? value.sources[id].providers.slice() : [];
        if (!['idle', 'importing', 'ready', 'paused'].includes(value.sources[id].status)) value.sources[id].status = 'idle';
      });
      SOURCE_ORDER.forEach(function(id){var so=value.sources[id];so.providers.forEach(function(provider){if(!value.accountImports[provider])value.accountImports[provider]={id:provider,source:id,status:so.status,progress:so.progress,startedAt:so.startedAt};});});
    } catch (_) { /* unavailable or corrupt storage leaves a working in-memory simulation */ }
    return value;
  }

  var state = readState();
  var reminderEpoch = 0, reminderRevision = -1;
  // Only bindAccount, called with a successful server session, sets this identity.
  // Browser state never establishes who owns a preference.
  var verifiedAccount = null;
  var ONBOARDING_KEY = 'grove-onboarding-v1', MAX_ONBOARDING_ACCOUNTS = 8;
  function onboardingPreferences() {
    try {
      var raw = localStorage.getItem(ONBOARDING_KEY) || '[]';
      if (raw.length > 4096) return [];
      var rows = JSON.parse(raw);
      if (!Array.isArray(rows) || rows.length > MAX_ONBOARDING_ACCOUNTS) return [];
      return rows.filter(function (row) {
        return row && typeof row.account === 'string' && row.account.length > 0 && row.account.length <= 128 &&
          Number.isInteger(row.step) && row.step >= 0 && row.step <= 4 &&
          typeof row.complete === 'boolean' && typeof row.tour === 'boolean' &&
          Object.keys(row).sort().join(',') === 'account,complete,step,tour';
      });
    } catch (_) { return []; }
  }
  function saveOnboardingPreference() {
    if (!LIVE || !verifiedAccount || state.account !== verifiedAccount) return;
    var rows = onboardingPreferences().filter(function (row) { return row.account !== verifiedAccount; });
    rows.push({account: verifiedAccount, step: Math.max(0, Math.min(3, Math.round(state.onboardingStep || 0))),
      complete: !!state.onboardingComplete, tour: !!state.tourComplete});
    try { localStorage.setItem(ONBOARDING_KEY, JSON.stringify(rows.slice(-MAX_ONBOARDING_ACCOUNTS))); } catch (_) {}
  }
  function restoreOnboardingPreference(account) {
    var row = onboardingPreferences().find(function (value) { return value.account === account; });
    if (!row) return;
    state.onboardingStep = Math.min(3, row.step); state.onboardingComplete = row.complete; state.tourComplete = row.tour;
  }

  function pendingExportContext() { return { account: state.account, epoch: reminderEpoch }; }
  function isPendingExportContext(context) {
    return !!context && !!state.auth && context.account === state.account && context.epoch === reminderEpoch;
  }
  function applyPendingExports(body, context) {
    if (!LIVE || !isPendingExportContext(context)) return false;
    var aliases = { chatgpt: 'chatgpt', claude_ai: 'claude', whatsapp: 'chats', muse: 'muse', grok: 'grok' };
    var values = body && body.pending_exports;
    if (!body || body.account !== state.account || !Number.isSafeInteger(body.revision) || body.revision < 0 ||
        !values || typeof values !== 'object' || Array.isArray(values) || Object.keys(values).length > 5 ||
        Object.keys(body).some(function (key) { return !['account', 'revision', 'pending_exports'].includes(key); }) ||
        Object.keys(values).some(function (key) {
          return !Object.prototype.hasOwnProperty.call(aliases, key) || typeof values[key] !== 'string' ||
            !/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(values[key]);
        })) throw new Error('Grove returned invalid export reminders. Try again.');
    if (body.revision < reminderRevision) return false;
    var next = {};
    Object.keys(values).sort().forEach(function (key) { next[aliases[key]] = values[key]; });
    if (body.revision === reminderRevision && JSON.stringify(next) !== JSON.stringify(state.pendingExports)) {
      throw new Error('Grove returned inconsistent export reminders. Try again.');
    }
    reminderRevision = body.revision;
    state.pendingExports = next;
    save();
    return true;
  }
  function refreshPendingExports() {
    if (!LIVE) return Promise.resolve(true);
    var context = pendingExportContext();
    return window.GroveLive.pendingExports().then(function (res) {
      if (!isPendingExportContext(context)) return false;
      if (!res.ok) throw new Error('Grove could not load your export reminders. Try again.');
      return applyPendingExports(res.body, context);
    });
  }

  function clone(value) { return JSON.parse(JSON.stringify(value)); }

  function allNodeById(data) {
    if (nodeByIdCache) return nodeByIdCache;
    var map = {};
    data.elements.nodes.forEach(function (n) { map[n.data.id] = n; });
    nodeByIdCache = map;
    return nodeByIdCache;
  }

  function orderedSourceIds(data, id) {
    if (orderedCache[id]) return orderedCache[id];
    var byId = allNodeById(data);
    var rank = {};
    (SOURCE_TYPE_ORDER[id] || []).forEach(function (type, i) { rank[type] = i; });
    var ids = (data.source_node_ids[id] || []).filter(function (nodeId) { return !!byId[nodeId]; }).slice();
    ids.sort(function (a, b) {
      var at = byId[a].data.type, bt = byId[b].data.type;
      return (rank[at] == null ? 99 : rank[at]) - (rank[bt] == null ? 99 : rank[bt]) || (a < b ? -1 : a > b ? 1 : 0);
    });
    orderedCache[id] = ids;
    return ids;
  }

  function nodeCount(id) { return fixture ? orderedSourceIds(fixture, id).length : 0; }
  function publicSource(id) { return Object.assign({}, SOURCE_COPY[id], state.sources[id], { nodeCount: nodeCount(id) }); }
  function getSources() { return SOURCE_ORDER.map(publicSource); }

  function legacyVisibleIdMap(data) {
    var ids = {};
    SOURCE_ORDER.forEach(function (id) {
      var list = orderedSourceIds(data, id);
      list.slice(0, Math.min(state.sources[id].loadedCount, list.length)).forEach(function (nodeId) { ids[nodeId] = 1; });
    });
    return ids;
  }

  // Every source owns a stable fixture subset. Overlap is intentional: one
  // memory can be supported by several sources, without duplicating the graph.
  function hash(text) { var h=2166136261; for(var i=0;i<text.length;i++) h=Math.imul(h^text.charCodeAt(i),16777619); return h>>>0; }
  function planFor(data, account) {
    if(account.nodeIds)return account.nodeIds;
    var ids=orderedSourceIds(data,account.source||'notes');
    var chosen=ids.filter(function(id){return hash(account.id+'|'+id)%100<64;});
    if(account.kind==='file') chosen=chosen.slice(0,36+hash(account.id)%32);
    account.nodeIds=chosen;return chosen;
  }
  function visibleIdMap(data) {
    var ids={};
    Object.keys(state.accountImports).forEach(function(id){var a=state.accountImports[id];planFor(data,a).slice(0,a.loadedCount||0).forEach(function(n){ids[n]=1;});});
    return ids;
  }
  function refreshSources() {
    SOURCE_ORDER.forEach(function(id){
      var previous=state.sources[id], list=Object.values(state.accountImports).filter(function(a){return a.source===id;});
      var ids={};list.forEach(function(a){(a.nodeIds||[]).slice(0,a.loadedCount||0).forEach(function(n){ids[n]=1;});});
      var importing=list.filter(function(a){return a.status==='importing';});
      state.sources[id]=Object.assign(blankSource(id),{name:previous.name,provider:previous.provider,providers:list.map(function(a){return a.id;}),loadedCount:Object.keys(ids).length,
        status:importing.length?'importing':list.some(function(a){return a.status==='ready';})?'ready':list.length?'paused':'idle',
        progress:importing.length?Math.round(importing.reduce(function(sum,a){return sum+a.progress;},0)/importing.length):list.length?100:0});
    });
  }
  function sourceStats(account) {
    var ids={};(account.nodeIds||[]).slice(0,account.loadedCount||0).forEach(function(n){ids[n]=1;});
    return {nodes:Object.keys(ids).length,edges:fixture?fixture.elements.edges.filter(function(e){return ids[e.data.source]&&ids[e.data.target];}).length:0};
  }
  function recordGrowth(before) {
    var after=visibleElements(fixture), bn={},be={};
    before.nodes.forEach(function(n){bn[n.data.id]=1;});before.edges.forEach(function(e){be[e.data.id]=1;});
    var ns=after.nodes.filter(function(n){return !bn[n.data.id];}).map(function(n){return n.data.id;});
    var es=after.edges.filter(function(e){return !be[e.data.id];}).map(function(e){return e.data.id;});
    state.recentNodes=Array.from(new Set(state.recentNodes.concat(ns)));
    state.recentEdges=Array.from(new Set(state.recentEdges.concat(es)));
    state.nodeDelta=state.recentNodes.length;state.edgeDelta=state.recentEdges.length;
    return ns.length+es.length>0;
  }
  // Live mode: the items a source holds evidence for, read from /v1/sources/connectors.
  var liveSourceItems={};
  function sourceChanges(connector){var ids=new Set(liveSourceItems[connector]||[]);return {new_nodes:clone(live.nodes.filter(function(n){return ids.has(n.data.id);}).map(function(n){return n.data;})),new_edges:[]};}
  function getChanges(provider) {
    if(LIVE&&window.GroveConversationPreview&&window.GroveConversationPreview.active())return provider&&provider!=='claude'?{new_nodes:[],new_edges:[]}:window.GroveConversationPreview.changes();
    if(LIVE)return provider?sourceChanges(provider):{new_nodes:clone(live.nodes.filter(function(n){return liveDelta.nodes.has(n.data.id);}).map(function(n){return n.data;})),new_edges:clone(live.edges.filter(function(e){return liveDelta.edges.has(liveEdgeId(e.data));}).map(function(e){return e.data;})),removed_nodes:liveDelta.removedNodes,removed_edges:liveDelta.removedEdges};
    if(!fixture)return {new_nodes:[],new_edges:[]};
    var graph=visibleElements(fixture),ids,edges;
    if(provider){var a=state.accountImports[provider];ids=a?(a.nodeIds||[]).slice(0,a.loadedCount||0):[];edges=graph.edges.filter(function(e){return ids.includes(e.data.source)&&ids.includes(e.data.target);}).map(function(e){return e.data.id;});}
    else {ids=state.recentNodes;edges=state.recentEdges;}
    return {new_nodes:graph.nodes.filter(function(n){return ids.includes(n.data.id);}).map(function(n){return n.data;}),new_edges:graph.edges.filter(function(e){return edges.includes(e.data.id);}).map(function(e){return e.data;})};
  }
  // The sim's example name is fixture mode only; live mode with no name known shows 'You'.
  function selfNode(){return {data:{id:'grove-self',type:'person',label:state.name||(LIVE?'You':'Anas'),status:'active',summary:'Your world, connected through the sources you bring to Grove',created_at:'2026-10-28T00:00:00Z'}};}
  // Live mode: the last graph payload, with the self item renamed to the identity node's id.
  var live = { selfId: null, nodes: [], edges: [] };
  // Grows when the session ends: a graph read still out at that moment keeps nothing here.
  var liveSession = 0, liveGraphRequest = 0;
  function emptyLiveDelta(){return {nodes:new Set(),edges:new Set(),removedNodes:0,removedEdges:0,checkedAt:null,baselineAt:null};}
  var liveDelta=emptyLiveDelta(), liveSnapshot=null;
  function liveEdgeId(e){return e.id || JSON.stringify([e.source,e.verb||e.label||'',e.target]);}
  function clearLiveSnapshot(){liveGraphRequest++;liveSnapshot=null;liveDelta=emptyLiveDelta();}
  // +X counts what the store recorded in the day before this read, from each row's own record time.
  // So it survives a later read, a page reload and a new device. A row with no record time, or a read
  // with no read time, counts for nothing: no number comes from any other source.
  var DELTA_WINDOW_MS=86400000;
  function recordedAfter(row,from){var at=Date.parse(row.created_at||'');return isFinite(at)&&at>from;}
  function compareLiveGraph(nodes,edges,readAt){
    var next={nodes:new Set(nodes.map(function(w){return w.data.id;})),edges:new Set(edges.map(function(w){return liveEdgeId(w.data);})),at:new Date().toISOString()};
    var before=liveSnapshot;liveDelta=emptyLiveDelta();liveDelta.checkedAt=next.at;
    var read=Date.parse(readAt||'');
    if(isFinite(read)){
      var from=read-DELTA_WINDOW_MS;
      liveDelta.baselineAt=new Date(from).toISOString();
      nodes.forEach(function(w){if(w.data.id!=='grove-self'&&recordedAfter(w.data,from))liveDelta.nodes.add(w.data.id);});
      edges.forEach(function(w){if(recordedAfter(w.data,from))liveDelta.edges.add(liveEdgeId(w.data));});
    }
    if(before){
      before.nodes.forEach(function(id){if(!next.nodes.has(id))liveDelta.removedNodes++;});
      before.edges.forEach(function(id){if(!next.edges.has(id))liveDelta.removedEdges++;});
    }
    liveSnapshot=next;
  }
  function isHubKind(type) { var style = ((window.GroveLive && window.GroveLive.KIND_STYLE) || {})[type] || type; return style === 'company' || style === 'project'; }
  // The views show the payload; when it names no self item, the sim's identity node joins it, as in the sim.
  function liveElements() {
    var nodes = live.nodes.slice(), edges = live.edges.slice();
    if (!live.selfId && (state.onboardingStep > 0 || state.onboardingComplete)) {
      nodes = nodes.concat([selfNode()]);
      nodes.filter(function (n) { return isHubKind(n.data.type); }).slice(0, 16).forEach(function (n) { edges.push({ data: { id: 'self-' + n.data.id, source: 'grove-self', target: n.data.id, verb: 'CONNECTS TO', label: 'CONNECTS TO' } }); });
    }
    return { nodes: nodes, edges: edges };
  }
  function visibleElements(data) {
    if (LIVE) return liveElements();
    var selected = visibleIdMap(data);
    var nodes = data.elements.nodes.filter(function (n) { return !!selected[n.data.id]; });
    var edges = data.elements.edges.filter(function (e) { return !!selected[e.data.source] && !!selected[e.data.target]; });
    if(state.onboardingStep>0||state.onboardingComplete){
      nodes=nodes.concat([selfNode()]);
      nodes.filter(function(n){return n.data.type==='company'||n.data.type==='project';}).slice(0,16).forEach(function(n){edges.push({data:{id:'self-'+n.data.id,source:'grove-self',target:n.data.id,verb:'CONNECTS TO',label:'CONNECTS TO'}});});
    }
    return { nodes: nodes, edges: edges };
  }

  function currentCounts() {
    // Live mode counts the real items of the last payload (never the sim's identity node).
    if (LIVE) { var liveTypes = {}; live.nodes.forEach(function (n) { liveTypes[n.data.type] = (liveTypes[n.data.type] || 0) + 1; }); return { nodes: live.nodes.length, edges: live.edges.length, types: liveTypes }; }
    if (!fixture) return { nodes: 0, edges: 0, types: {} };
    var elements = visibleElements(fixture), types = {};
    elements.nodes.forEach(function (n) { types[n.data.type] = (types[n.data.type] || 0) + 1; });
    return { nodes: elements.nodes.length, edges: elements.edges.length, types: types };
  }

  function getState() {
    var sources = getSources(), counts = currentCounts();
    var all = LIVE ? live : fixture && fixture.elements ? fixture.elements : { nodes: [], edges: [] };
    var imported = sources.filter(function (s) { return s.status === 'ready'; }).length;
    var result = {
      version: VERSION, auth: clone(state.auth), account: state.account, name: state.name, onboardingComplete: state.onboardingComplete,
      onboardingStep: state.onboardingStep, tourComplete: state.tourComplete,
      mcpClients: clone(state.mcpClients), hostConnected: state.hostConnected, connectedHosts: state.connectedHosts.slice(),
      requests: clone(state.requests), growth: clone(state.growth), files: clone(state.files), pendingExports: clone(state.pendingExports), accountImports: Object.fromEntries(Object.entries(state.accountImports).map(function(entry){return [entry[0],Object.assign({},entry[1],{contribution:sourceStats(entry[1])})];})), googleConnected: state.googleConnected,
      importedCount: imported, hasImports: sources.some(function (s) { return s.status !== 'idle' || s.loadedCount > 0; }),
      currentNodeCount: counts.nodes, currentEdgeCount: counts.edges, currentNodeTypes: counts.types,
      growthHistory: clone(state.growthHistory), nodeDelta: LIVE?liveDelta.nodes.size:state.nodeDelta, edgeDelta: LIVE?liveDelta.edges.size:state.edgeDelta,
      removedNodes:LIVE?liveDelta.removedNodes:0,removedEdges:LIVE?liveDelta.removedEdges:0,graphCheckedAt:LIVE?liveDelta.checkedAt:null,graphBaselineAt:LIVE?liveDelta.baselineAt:null,
      totalNodeCount: all.nodes.length, totalEdgeCount: all.edges.length,
      updatedAt: state.updatedAt, sources: sources,
    };
    return LIVE&&window.GroveConversationPreview?window.GroveConversationPreview.project(result):result;
  }

  function announce() { window.dispatchEvent(new CustomEvent('grove:change', { detail: getState() })); }
  function requestReload() {
    if (typeof window.reloadGrove !== 'function') return;
    try {
      var result = window.reloadGrove();
      if (result && typeof result.catch === 'function') result.catch(function () {});
    } catch (_) { /* the graph may not have mounted yet */ }
  }
  function save(options) {
    saveOnboardingPreference();
    seedRequests();
    var count=currentCounts(),last=state.growthHistory[state.growthHistory.length-1];
    if(!last||last.nodes!==count.nodes||last.edges!==count.edges){
      state.growthHistory.push({at:new Date().toISOString(),nodes:count.nodes,edges:count.edges});
      if(state.growthHistory.length>240)state.growthHistory=state.growthHistory.filter(function(p,i){return i===0||i===state.growthHistory.length-1||i%2===0;});
    }
    state.updatedAt = new Date().toISOString();
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(LIVE ? Object.assign({}, state, {pendingExports:{}}) : state)); } catch (_) { /* continue in memory */ }
    announce();
    if (options && options.reload) requestReload();
  }

  function setName(name) {
    state.name = String(name == null ? '' : name).trim().slice(0, 80);
    save({reload:true}); return getState();
  }
  function setStep(step) {
    state.onboardingStep = Math.max(0, Math.min(3, Math.round(Number(step) || 0)));
    save({reload:true}); return getState();
  }

  function targetAtStep(total, step) {
    if (!total) return 0;
    if (step <= 0) return 1;
    if (step === 1) return Math.min(10, total);
    if (step >= JOB_STEPS) return total;
    var base = Math.min(10, total), ratio = (step - 1) / (JOB_STEPS - 1);
    return Math.min(total, Math.max(base, Math.round(base + (total - base) * Math.pow(ratio, 1.55))));
  }

  function stopJob(id) { if(jobs[id])window.clearTimeout(jobs[id]); delete jobs[id]; }
  function scheduleAccount(provider) {
    var a=state.accountImports[provider],epoch=importEpoch;
    stopJob(provider);if(!a||a.status!=='importing')return;
    var run=function(){
      if(epoch!==importEpoch||!state.accountImports[provider]||a.status!=='importing')return;
      var step=Math.min(JOB_STEPS,Math.floor((Date.now()-Date.parse(a.startedAt))/TICK_MS));
      var before=visibleElements(fixture),ids=planFor(fixture,a);
      a.loadedCount=Math.max(a.loadedCount||0,targetAtStep(ids.length,step));
      a.progress=Math.max(1,Math.round(step/JOB_STEPS*100));
      if(step>=JOB_STEPS){a.status='ready';a.progress=100;a.importedAt=new Date().toISOString();a.updating=false;}
      refreshSources();var changed=recordGrowth(before);save({reload:changed});
      if(a.status==='importing')jobs[provider]=window.setTimeout(run,TICK_MS);
    };
    jobs[provider]=window.setTimeout(run,TICK_MS);
  }
  function importSource(id, meta) {
    if(!SOURCE_COPY[id])return Promise.reject(new Error('Unknown Grove source: '+id));
    meta=meta||{};var provider=String(meta.provider||id);
    return fixturePromise.then(function(data){
      fixture=data;var existing=state.accountImports[provider];
      if(existing&&existing.status!=='disconnected'&&!meta.update)return publicSource(id);
      var before=visibleElements(data);
      var a=Object.assign({},existing||{id:provider,source:id,loadedCount:0}, {label:meta.label||provider,kind:meta.kind||existing&&existing.kind||'account',status:'importing',progress:1,startedAt:new Date().toISOString(),name:meta.name||provider,updating:!!meta.update});
      state.accountImports[provider]=a;
      var ids=planFor(data,a);a.loadedCount=Math.max(a.loadedCount||0,Math.min(1,ids.length));
      delete state.pendingExports[provider];refreshSources();var changed=recordGrowth(before);save({reload:changed});scheduleAccount(provider);return publicSource(id);
    });
  }
  function addFile(name, content, kind) {
    var clean=String(name||'Untitled note.md').trim().slice(0,160);
    var id='file-'+hash(clean+'|'+String(content||''));
    if(state.files[id])return Promise.resolve(getState());
    state.files[id]={id:id,name:clean,kind:kind==='note'?'note':'file',addedAt:new Date().toISOString(),text:String(content||'').slice(0,10000)};
    return importSource('notes',{provider:id,label:clean,name:clean,kind:'file'}).then(getState);
  }
  function removeFile(id) {
    if(!state.files[id])return getState();
    stopJob(id);delete state.files[id];delete state.accountImports[id];refreshSources();
    var changes=getChanges();state.recentNodes=changes.new_nodes.map(function(n){return n.id;});state.recentEdges=changes.new_edges.map(function(e){return e.id;});state.nodeDelta=state.recentNodes.length;state.edgeDelta=state.recentEdges.length;
    save({reload:true});return getState();
  }
  function updateSource(id) {var a=state.accountImports[id];if(!a)return Promise.resolve(getState());return importSource(a.source,{provider:id,label:a.label,name:a.name,kind:a.kind,update:true});}
  function acknowledgeChanges(){state.recentNodes=[];state.recentEdges=[];state.nodeDelta=0;state.edgeDelta=0;save();return getState();}
  function cancelExport(id){if(LIVE)return getState();delete state.pendingExports[id];save();return getState();}
  function seedRequests(){
    // Live mode: the questions come from the server (views-b); none are made from store titles.
    if(LIVE||!state.onboardingComplete||!fixture||!Object.keys(state.accountImports).length)return;
    if(state.requestsSeeded&&state.requests.every(function(r){return r.targetLabel||r.status==='answered';}))return;
    var graph=visibleElements(fixture),nodes=graph.nodes.map(function(n){return n.data;}),map={};nodes.forEach(function(n){map[n.id]=n;});
    var node=nodes.find(function(n){return n.type==='project';})||nodes.find(function(n){return n.id!=='grove-self';});
    if(!node)return;
    var edge=graph.edges.find(function(e){return e.data.source!=='grove-self'&&e.data.target!=='grove-self';})||graph.edges[0];
    var relation=edge&&edge.data,a=relation&&map[relation.source],b=relation&&map[relation.target];
    var next=[
      {id:'focus',agent:'Learner',targetId:node.id,targetLabel:node.label,kind:'node',title:'Should '+node.label+' stay a current priority?',context:'Before giving this more weight in your Grove',placeholder:'Tell Grove what is current, or what has changed',status:'open',answer:''},
      {id:'plans',agent:'Connector',targetId:relation?relation.id:node.id,kind:relation?'connection':'node',targetLabel:relation?a.label+' → '+(relation.verb||relation.label||'connected to').toLowerCase()+' → '+b.label:node.label,title:relation?'Does this connection still hold?':'Is this still part of your world?',context:'Before keeping this relationship current',placeholder:'Confirm it, or describe the connection as it is now',status:'open',answer:''}
    ];
    state.requests=next.map(function(r){return state.requests.find(function(old){return old.id===r.id&&old.status==='answered';})||r;});
    state.requestsSeeded=true;
  }
  function addMcpClient(name){
    name=String(name||'').trim().slice(0,60);if(!name)return getState();
    if(!state.mcpClients.some(function(c){return c.name.toLowerCase()===name.toLowerCase();}))state.mcpClients.push({id:'mcp-'+Date.now().toString(36)+'-'+state.mcpClients.length,name:name});
    save();return getState();
  }
  function removeMcpClient(id){state.mcpClients=state.mcpClients.filter(function(c){return c.id!==id;});save();return getState();}
  function answerRequest(id,answer){var r=state.requests.find(function(r){return r.id===id;});var text=String(answer||'').trim().slice(0,2000);if(!r||!text)return getState();r.answer=text;r.status='answered';r.answeredAt=new Date().toISOString();save();return getState();}
  // Live mode: the last graph payload goes too, so no item of the session stays in memory.
  function signOut(){
    if(window.GroveConversationPreview)window.GroveConversationPreview.clear();
    if(LIVE){
      saveOnboardingPreference();verifiedAccount=null;
      reminderEpoch++;reminderRevision=-1;importEpoch++;
      Object.keys(jobs).forEach(stopJob);state=freshState();
      liveSession++;clearLiveSnapshot();live={selfId:null,nodes:[],edges:[]};
    }else state.auth=null;
    save();return getState();
  }
  function saveGrowth(value){['focus','reading','avoid'].forEach(function(k){state.growth[k]=String(value[k]||'').trim().slice(0,1500);});state.growth.confirm=!!value.confirm;save();return getState();}
  function clearGrove(){var auth=state.auth,account=state.account,name=state.name,growth=state.growth;resetOnboarding();state.auth=auth;state.account=account;state.name=name;state.growth=growth;state.onboardingStep=1;save({reload:true});return getState();}

  function requestExport(id) {
    if (LIVE) {
      var context = pendingExportContext();
      if (!isPendingExportContext(context)) return Promise.reject(new Error('Sign in to save an export reminder.'));
      return window.GroveLive.requestExport(id).then(function (res) {
        if (!isPendingExportContext(context)) return false;
        if (!res.ok) throw new Error('Grove could not save your export reminder. Try again.');
        return applyPendingExports(res.body, context);
      });
    }
    if (!HOSTS[id] && id!=='chats') return getState();
    state.pendingExports[id] = { requestedAt: new Date().toISOString() };
    save(); return getState();
  }
  function connectGoogle() {
    state.googleConnected = true; save();
    return Promise.all([importSource('conversations',{provider:'gmail',name:'Gmail'}),importSource('notes',{provider:'drive',name:'Google Drive'}),importSource('calendar',{provider:'calendar',name:'Google Calendar'})]);
  }

  function signIn(provider,email) {
    // The example identity is fixture mode only: live mode keeps the email the sign-in knows ('' if none).
    state.auth={provider:provider,email:String(email||(LIVE?'':'anas@example.com')).trim()};
    if(!state.name&&!LIVE)state.name='Anas';
    save();return getState();
  }
  function disconnectSource(provider) {
    var a=state.accountImports[provider];if(!a)return getState();stopJob(provider);a.status='disconnected';refreshSources();
    state.googleConnected=['gmail','drive','calendar'].every(function(p){return state.accountImports[p]&&state.accountImports[p].status!=='disconnected';});
    save();return getState();
  }
  function disconnectHost(provider) {
    state.connectedHosts=state.connectedHosts.filter(function(id){return id!==provider;});state.hostConnected=state.connectedHosts.length>0;save();return getState();
  }
  function completeOnboarding() { state.onboardingComplete = true; save(); return getState(); }
  function completeTour() { state.tourComplete = true; save(); return getState(); }
  function resetOnboarding() {
    if(window.GroveConversationPreview)window.GroveConversationPreview.clear();
    var before = currentCounts();
    verifiedAccount = null;
    if(LIVE){liveSession++;clearLiveSnapshot();live={selfId:null,nodes:[],edges:[]};}
    reminderEpoch++; reminderRevision = -1;
    importEpoch += 1; Object.keys(jobs).forEach(stopJob); state = freshState();
    state.nodeDelta = 0; state.edgeDelta = 0;
    save({ reload: before.nodes > 0 || before.edges > 0 }); return getState();
  }
  // Live mode: this browser's local state belongs to one account. A sign-in that brings another
  // account resets it first, so nothing of one person shows to the next. Says whether it reset.
  function bindAccount(account) {
    if (!LIVE || typeof account !== 'string' || !account || account.length > 128) return false;
    var other = state.account !== account;
    if (other) resetOnboarding();
    if (state.account !== account) { reminderEpoch++; reminderRevision = -1; state.pendingExports = {}; }
    var firstBinding = verifiedAccount !== account;
    verifiedAccount = account;
    state.account = account;
    if (firstBinding) restoreOnboardingPreference(account);
    save();
    return other;
  }
  // Live mode: a person whose world already holds items skips the first-run steps on a new browser.
  function resumeOnboarding() { state.onboardingStep = 4; state.onboardingComplete = true; state.tourComplete = true; save(); return getState(); }
  function connectHost(providerId) {
    var id = String(providerId || 'claude').toLowerCase();
    if (!HOSTS[id]) throw new Error('Unknown AI host: ' + id);
    if (state.connectedHosts.indexOf(id) < 0) state.connectedHosts.push(id);
    state.hostConnected = state.connectedHosts.length > 0;
    save(); return getState();
  }

  function graphPayload(data) {
    var elements = visibleElements(data), byType = {};
    elements.nodes.forEach(function (n) { byType[n.data.type] = (byType[n.data.type] || 0) + 1; });
    return clone({
      elements: elements,
      stats: { total_nodes: elements.nodes.length, total_edges: elements.edges.length, by_type: byType, embedded: elements.nodes.length, edge_verbs: data.stats.edge_verbs },
      time: data.time, generated_at: data.generated_at,
    });
  }
  function historyPayload(data) {
    var elements = visibleElements(data);
    return (data.history || []).map(function (point) {
      var day = point.date;
      return {
        date: day,
        nodes: elements.nodes.filter(function (n) { return String(n.data.created_at || '').slice(0, 10) <= day; }).length,
        edges: elements.edges.filter(function (e) { return String(e.data.valid_at || '').slice(0, 10) <= day; }).length,
      };
    }).filter(function (point) { return point.nodes || point.edges; });
  }
  function selfDetail(){var self=selfNode().data;return {node:Object.assign({},self,{title:self.label,content:self.summary,status_field_value:'active'}),required_props:[],optional_props:[],required_prop_names:[],status_field:'status'};}
  function nodeDetail(data, id) {
    if(id==='grove-self'&&(state.onboardingStep>0||state.onboardingComplete))return selfDetail();
    if (!visibleIdMap(data)[id]) return { error: 'No such visible node' };
    var wrapper = allNodeById(data)[id];
    if (!wrapper) return { error: 'No such node' };
    var n = clone(wrapper.data);
    return {
      node: Object.assign({}, n, { title: n.label, content: n.summary, status_field_value: n.status }),
      required_props: (data.schema[n.type] && data.schema[n.type].required) || [],
      optional_props: (data.schema[n.type] && data.schema[n.type].optional) || [],
      required_prop_names: [], status_field: 'status',
    };
  }
  function diffPayload(data, since) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(since || '')) return { error: 'since must be YYYY-MM-DD' };
    var graph = graphPayload(data);
    return {
      since: since,
      new_nodes: graph.elements.nodes.map(function (n) { return n.data; }).filter(function (n) { return String(n.created_at || '').slice(0, 10) > since; }).map(function (n) { return { id: n.id, label: n.label, type: n.type }; }),
      new_edges: graph.elements.edges.map(function (e) { return e.data; }).filter(function (e) { return String(e.valid_at || '').slice(0, 10) > since; }).map(function (e) { return { source: e.source, target: e.target, verb: e.verb || e.label }; }),
    };
  }
  function route(data, rawUrl) {
    var url = new URL(rawUrl, window.location.href), path = url.pathname;
    if (path === '/api/graph') return graphPayload(data);
    if (path === '/api/history') return historyPayload(data);
    if (path === '/api/schema') return clone(data.schema || {});
    if (path.indexOf('/api/node/') === 0) return nodeDetail(data, decodeURIComponent(path.slice('/api/node/'.length)));
    if (path === '/api/diff') return diffPayload(data, url.searchParams.get('since') || '');
    if (path === '/api/health' || path === '/health') {
      var counts = currentCounts();
      return Object.assign(clone(data.health || { status: 'healthy' }), { nodes: counts.nodes, edges: counts.edges, by_type: counts.types });
    }
    return { error: 'No simulated endpoint for ' + path };
  }

  // Live mode: the same reads, answered by the server. Other paths answer a 404-shaped error.
  function liveAnswer(status, payload) { return { ok: status >= 200 && status < 300 && !payload.error, status: status, json: function () { return Promise.resolve(payload); } }; }
  function acceptGraph(body) {
    var el = body && body.elements;
    if(!el||!Array.isArray(el.nodes)||!Array.isArray(el.edges)||el.nodes.some(function(w){return !w||!w.data||typeof w.data.id!=='string';})||el.edges.some(function(w){return !w||!w.data||typeof w.data.source!=='string'||typeof w.data.target!=='string';}))throw new Error('Invalid graph response');
    var nodes=el.nodes,edges=el.edges;
    var selfId = body && body.self_item_id != null ? String(body.self_item_id) : null;
    if (selfId && !nodes.some(function (w) { return w && w.data && w.data.id === selfId; })) selfId = null;
    function alias(id) { return selfId && id === selfId ? 'grove-self' : id; }
    live = {
      selfId: selfId,
      nodes: nodes.map(function (w) { var d = Object.assign({}, w.data); d.id = alias(d.id); return { data: d }; }),
      edges: edges.map(function (w) { var d = Object.assign({}, w.data); d.source = alias(d.source); d.target = alias(d.target); return { data: d }; }),
    };
    compareLiveGraph(live.nodes,live.edges,body.generated_at);
    return Object.assign({}, body, { elements: clone(liveElements()) });
  }
  function aliasDetail(body) {
    if (!body || !body.node || !live.selfId || body.node.id !== live.selfId) return body;
    return Object.assign({}, body, { node: Object.assign({}, body.node, { id: 'grove-self' }) });
  }
  function liveApi(rawUrl) {
    var G = window.GroveLive, url = new URL(rawUrl, window.location.href), path = url.pathname, target = null, kind = '';
    if (!G) return Promise.resolve(liveAnswer(404, { error: 'Grove is not connected' }));
    if (path === '/api/graph') { target = G.ENDPOINTS.graph + url.search; kind = 'graph'; }
    else if (path.indexOf('/api/node/') === 0) {
      var id = decodeURIComponent(path.slice('/api/node/'.length));
      if (id === 'grove-self') {
        if (!live.selfId) return Promise.resolve(state.onboardingStep > 0 || state.onboardingComplete ? liveAnswer(200, selfDetail()) : liveAnswer(404, { error: 'No such visible node' }));
        id = live.selfId;
      }
      target = G.ENDPOINTS.node + encodeURIComponent(id) + url.search; kind = 'node';
    }
    else if (path === '/api/schema') target = G.ENDPOINTS.schema;
    else if (path === '/api/health' || path === '/health') target = G.ENDPOINTS.health;
    else return Promise.resolve(liveAnswer(404, { error: 'No live endpoint for ' + path }));
    var session = liveSession,request=kind==='graph'?++liveGraphRequest:0;
    return G.getJSON(target).then(function (res) {
      var body = res.body || {};
      if (!res.ok) return liveAnswer(res.status || 500, { error: G.errorText(body), code: (body.error && body.error.code) || null });
      if (kind === 'graph') { if(session!==liveSession||request!==liveGraphRequest)return liveAnswer(409,{error:'Graph request superseded',code:'superseded'});body = acceptGraph(body); }   // native-app drops a late answer
      else if (kind === 'node') body = aliasDetail(body);
      return liveAnswer(res.status, body);
    });
  }

  window.groveApi = function (url) {
    if(LIVE&&window.GroveConversationPreview&&window.GroveConversationPreview.active())return window.GroveConversationPreview.answer(url);
    if (LIVE) return liveApi(url);
    return fixturePromise.then(function (data) {
      fixture = data;
      var payload = route(data, url);
      return { ok: !payload.error, status: payload.error ? 404 : 200, json: function () { return Promise.resolve(payload); } };
    });
  };

  window.GroveSimulation = {
    pendingExportContext: pendingExportContext, isPendingExportContext: isPendingExportContext,
    applyPendingExports: applyPendingExports, refreshPendingExports: refreshPendingExports,
    addMcpClient:addMcpClient, removeMcpClient:removeMcpClient, acknowledgeChanges:acknowledgeChanges, addFile:addFile, removeFile:removeFile, updateSource:updateSource, cancelExport:cancelExport, saveGrowth:saveGrowth, clearGrove:clearGrove, getChanges:getChanges, setSourceItems:function(items){liveSourceItems=items||{};}, getState: getState, getSources: getSources, setName: setName, setStep: setStep,
    importSource: importSource, completeOnboarding: completeOnboarding, completeTour: completeTour,
    signIn: signIn, signOut:signOut, bindAccount: bindAccount, resumeOnboarding: resumeOnboarding, answerRequest:answerRequest, disconnectSource: disconnectSource, disconnectHost: disconnectHost, resetOnboarding: resetOnboarding, connectHost: connectHost, requestExport: requestExport, connectGoogle: connectGoogle,
    // Live mode: true when no fixture is injected; realId maps a view id back to the item id
    // (the identity node to the self item, null when the identity node is the sim's own).
    isLive: function () { return LIVE; },
    realId: function (id) { if (!LIVE || id == null || (window.GroveConversationPreview&&window.GroveConversationPreview.active())) return null; return id === 'grove-self' ? live.selfId : String(id); },
  };

  fixturePromise.then(function(data){
    fixture=data;
    Object.keys(state.accountImports).forEach(function(id){
      var a=state.accountImports[id];a.id=id;
      if(!a.source)a.source=SOURCE_ORDER.find(function(k){return state.sources[k].providers.includes(id);})||'notes';
      var ids=planFor(data,a);
      if(a.loadedCount==null)a.loadedCount=Math.round(ids.length*(a.progress||100)/100);
      if(a.status==='ready')a.loadedCount=ids.length;
      if(id==='notes'&&!a.kind){a.kind='file';a.label=state.sources.notes.name||'Imported notes';state.files.notes={id:'notes',name:a.label};}
      if(a.status==='importing')scheduleAccount(id);
    });refreshSources();seedRequests();announce();if(!LIVE)requestReload(); // live: native-app's boot load reads the graph
  }).catch(function(error){console.error(error);});
})();
