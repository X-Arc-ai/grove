/* brain visualization · constellation
 *
 * Architecture:
 *   1. Fetch /api/graph -> node + edge arrays.
 *   2. Seed hub-cluster positions; pin hubs (fx/fy). Leaves stay live.
 *   3. A continuously-warm d3-force sim spreads leaves and keeps the layout in a
 *      gentle perpetual drift (alpha floored at a low target, never frozen).
 *   4. Custom nodeCanvasObject draws cream disk + colored ring + (for hubs) name inside.
 *   5. Custom linkCanvasObject draws hairline + verb label on load-bearing edges.
 *   6. Click node -> green ring + slide-in detail panel; hover dims rest.
 *
 * Performance:
 *   - No ctx.filter (kills perf at this node count) and no getComputedStyle in
 *     the draw hot path (CSS vars resolved ONCE per theme into THEME).
 *   - The sim runs continuously by design (cooldowns Infinity, low alphaTarget);
 *     Barnes-Hut charge + collide stay within d3-force's comfortable range here.
 *   - Hub label rendering only at scale >= 0.4 to skip when fully zoomed out.
 */

(function () {
  'use strict';
  var prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  // File previews and private browsing can refuse storage. The product still opens.
  var storage = {
    getItem: function (key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
    setItem: function (key, value) { try { localStorage.setItem(key, value); } catch (_) {} },
    removeItem: function (key) { try { localStorage.removeItem(key); } catch (_) {} }
  };

  // ---------------- constants ----------------

  // DENSE + ORBIT are the DEFAULT view (Anas approved 2026-07-28).
  //
  // Dense — re-weighting so the graph's real mass (2,100+ nodes, ~5,800
  // edges) reads visually: (1) intelligence-layer records render
  // small-but-CRISP instead of alpha-fog, (2) resting edge ink steps up so
  // the connective tissue shows, (3) tighter geometry + softer long-range
  // repulsion close the inter-cluster void.
  //
  // Orbit — hub placement from the data instead of the fixed even-slot ring
  // (where a 1-member archived hub held the same territory as a 265-member
  // company): biggest clusters form a CORE chain ordered by shared tissue,
  // mid hubs ORBIT at the angle of their strongest partner, small ones tuck
  // into the RIM, archived hubs DEMOTE to free-floating nodes, hub disk area
  // ∝ cluster mass. Deterministic; evolves with the graph (ambient refresh).
  //
  // Escape hatches: ?classic=1 restores the pre-2026-07-27 look (ring + fog);
  // ?dense=0 / ?orbit=0 disable one lever for tuning. Old ?dense=1&orbit=1
  // URLs keep working (redundant now).
  // The production page (live mode, no fixture) has none of the three: a first visit writes its
  // layout to the store for good, so no URL may change that layout.
  var URL_SETTINGS = window.GROVE_FIXTURE ? window.location.search : '';
  var CLASSIC = /[?&]classic=1/.test(URL_SETTINGS);
  var DENSE = !CLASSIC && !/[?&]dense=0/.test(URL_SETTINGS);
  var ORBIT = !CLASSIC && !/[?&]orbit=0/.test(URL_SETTINGS);

  var HUB_TYPES = { company: 1, project: 1 };

  // Node radii — 6-tier hierarchy that reads at first glance, even at zoom 0.3.
  // Pushed harder so hubs DOMINATE the eye (7-10x leaf size, was 3x).
  // Tier 1 hubs (company/project) — anchor the eye, biggest disks.
  // Tier 2 (person/instance) clearly subordinate but visible.
  // Tier 3 (product/client) supporting structure.
  // Tier 4 (goal/decision/blocker) operational mid-tier.
  // Tier 5 (task) leaf-level work — small.
  // Tier 6 (event/observation/status_change) temporal records — TRULY small,
  //   recede into the back-layer, don't compete with structural nodes.
  var SIZES = {
    company: 42, project: 38,
    person: 24, instance: 22,
    product: 17, client: 16,
    goal: 13, decision: 13, blocker: 13,
    task: 10, event: 7, status_change: 6, observation: 6,
    roadmap: 13, pattern: 15,
  };
  // Dense lever 1a: the intelligence layer (81% of all nodes) gets a slightly
  // larger floor so a thousand records read as countable points, not dust.
  // Hubs untouched — hierarchy survives at 4x+.
  if (DENSE) {
    SIZES.observation = 9; SIZES.event = 9; SIZES.status_change = 8;
  }

  // ----- FORCE-DIRECTED LAYOUT (Apr 26 rebuild #3 — real physics) -----
  //
  // Hubs (company/project) are pinned in a ring around canvas center via fx/fy.
  // Every other node is seeded near its primary hub with random jitter and then
  // released to a real d3-force simulation:
  //   - charge: hubs strongly repel (-1500), leaves mildly (-200), max range 600
  //   - link: spring distance 120, mid-soft strength so neighbors stay close
  //   - collide: radius = nodeRadius * 1.5 + 8 — overlap is mathematically
  //     impossible at convergence
  //   - center: disabled (the hub ring already centers the layout)
  //
  // Once the engine stops (cooldownTicks 400, alpha decay 0.025), all leaves
  // are pinned to their final position via fx/fy so subsequent renders are
  // pure draw — no per-tick physics work, hover/focus stay snappy.

  // Visible draw radius per type (mirrors SIZES; used by collide force so the
  // physics sees the same shapes the user sees).
  function nodeRadius(d) {
    if (d._demoted) return 18;   // orbit mode: archived hubs lose pillar size
    // Mirror drawNode: mass-proportional hub disks in orbit mode, so the
    // collide force sees exactly the shapes the user sees.
    return d._hubR || SIZES[d.type] || 12;
  }
  function isHub(d) {
    return !!HUB_TYPES[d.type];
  }

  // Tier classification used by ring-weight, label rules, and hover behavior.
  // Tier 6 carved out for purely temporal records so they get the thinnest ring
  // and the smallest visual weight — they should fade into the back-layer.
  var TIER = {
    company: 1, project: 1,
    person: 2, instance: 2,
    product: 3, client: 3,
    goal: 4, decision: 4, blocker: 4, roadmap: 4,
    task: 5,
    event: 6, status_change: 6, observation: 6,
    pattern: 3,   // structural insight — reads as meaningful structure (ring-weight 2.5)
  };

  // Grove's starter kinds draw with the sim's styles (map in web/live.js): the data keeps the
  // real kind name; a kind borrows the size, tier, hub role and ring colour of its sim type.
  // organisation and plan are hubs, as company and project are. Live mode gives any other
  // kind the task style (adoptKinds); the legend lists every kind under its own name.
  var SIM_KINDS = Object.keys(SIZES);
  var KIND_STYLE = (window.GroveLive && window.GroveLive.KIND_STYLE) || {};
  Object.keys(KIND_STYLE).forEach(function (kind) {
    var style = KIND_STYLE[kind];
    if (SIZES[kind] == null && SIZES[style] != null) SIZES[kind] = SIZES[style];
    if (TIER[kind] == null && TIER[style] != null) TIER[kind] = TIER[style];
    if (HUB_TYPES[style]) HUB_TYPES[kind] = 1;
  });
  function adoptKinds(nodes) {
    nodes.forEach(function (n) {
      var kind = n.type;
      if (typeof kind !== 'string' || !kind || kind === '__proto__' || Object.prototype.hasOwnProperty.call(SIZES, kind)) return;
      SIZES[kind] = SIZES.task;
      TIER[kind] = TIER.task;
    });
  }

  // Verbs that ALWAYS show their label on the canvas — small set for visual signal.
  var ALWAYS_LABELED_VERBS = {
    'CEO of': 1, 'CTO of': 1, 'Director of': 1,
    'founded': 1, 'leads': 1, 'manages': 1,
    'depends on': 1, 'blocks': 1,
  };

  // Verbs labeled at moderate zoom (1.4+)
  var ZOOMED_LABELED_VERBS = {
    'owned by': 1, 'managed by': 1, 'blocked by': 1,
    'goal for': 1, 'product of': 1, 'core engine of': 1,
    'deployed to serve': 1,
  };

  // Statuses that mean "not actively being worked / not currently engaged".
  // Hidden in active-only mode. Includes terminal states (archived, completed,
  // sunset, etc.) AND paused-but-not-dead states (parked, paused, on_leave,
  // prospective) — these entities exist but aren't current operational work.
  var ENDED_STATUSES = {
    // terminal
    archived: 1, cancelled: 1, completed: 1, sunset: 1,
    departed: 1, resolved: 1, superseded: 1, reversed: 1, backlog: 1,
    // paused / not-yet-active
    parked: 1, paused: 1, on_leave: 1, prospective: 1, planned: 1,
  };

  var STATUS_DOT_CLASS = {
    blocked: 'dot-blocked', stalled: 'dot-blocked',
    pending: 'dot-pending', backlog: 'dot-pending',
  };

  // ---------------- state ----------------

  var canvasEl = document.getElementById('canvas');
  var detailEl = document.getElementById('detail');
  var detailBodyEl = document.getElementById('detail-body');
  var loadingEl = document.getElementById('loading');
  var statsEl = document.getElementById('stats');

  var Graph;
  var rawNodes = [], rawEdges = [], byId = {};
  var fgNodes = [], fgLinks = [], fgNodeById = {};
  // Counts the cleared sessions: a read that started before the last clear draws nothing.
  var viewEpoch = 0;
  var neighborsOf = {};
  var focusedNode = null;
  var hoverNode = null;
  var hoverLink = null;
  var activeOnly = false;
  // Per-type schema {type: {required: [...], optional: [...], status_field, status_vocab}}
  // Fetched once at boot from /api/schema. Drives the REQUIRED/OPTIONAL split
  // in the detail panel. Empty {} until the fetch resolves; rendering code
  // tolerates a missing entry by falling back to the legacy flat list.
  var schemaByType = {};
  var schemaAsked = false;   // false again after a failed read or the end of a session: the next graph load reads the kinds
  // Hop-distance from focusedNode (rebuilt on every click).
  // hopDist[id] = 0 (focused), 1, 2, 3, or undefined (>3 / not reachable).
  var hopDist = {};
  // Date string (YYYY-MM-DD) of the last daily backup we're comparing against.
  // Set in renderStats(); used by selectChangesOnCanvas() to query /api/diff.
  var prevSnapshotDate = null;
  // Multi-node "highlight set" — populated when the user clicks a +N delta in
  // the stats bar to visualize new nodes/edges since the last backup.
  // Mutually exclusive with focusedNode (which is single-node BFS). When
  // active, drawNode/getLinkColor brighten the set + dim everything else.
  var highlightedIds = {};         // {nodeId: 1}
  var highlightedEdgeKeys = {};    // {"src||verb||tgt": 1}
  var highlightedNodeCount = 0;    // cached cardinality for hot-path checks
  var highlightedEdgeCount = 0;
  function hasSelection() {
    return highlightedNodeCount > 0 || highlightedEdgeCount > 0;
  }
  function edgeKey(l) {
    var s = typeof l.source === 'object' ? l.source.id : l.source;
    var t = typeof l.target === 'object' ? l.target.id : l.target;
    return s + '||' + (l.verb || l.label || '') + '||' + t;
  }

  // Cached CSS values — rebuilt on theme change.
  var THEME = {};
  // Desaturate a hex color toward its own perceived-luminance gray. amount 0 =
  // unchanged, 1 = fully gray. Lightness stays ~constant, so the result reads as
  // "muted", not "darkened" — exactly what an inactive ring should be.
  function desaturateColor(hex, amount) {
    var m = String(hex).trim().match(/^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/);
    if (!m) return hex;
    var h = m[1];
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    var r = parseInt(h.slice(0, 2), 16),
        g = parseInt(h.slice(2, 4), 16),
        b = parseInt(h.slice(4, 6), 16);
    var gray = 0.299 * r + 0.587 * g + 0.114 * b;
    r = Math.round(r + (gray - r) * amount);
    g = Math.round(g + (gray - g) * amount);
    b = Math.round(b + (gray - b) * amount);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }

  // Inactive nodes desaturate their ring by this much (0–1). The single signal
  // separating active from inactive is shade: dimmer alpha (drawNode) + this
  // muted ring. Tunable in the visual loop.
  var INACTIVE_DESAT = 0.7;

  function rebuildThemeCache() {
    var s = getComputedStyle(document.documentElement);
    function v(name) { return s.getPropertyValue(name).trim(); }
    var ring = {
      company: v('--ring-company'),
      project: v('--ring-project'),
      person: v('--ring-person'),
      instance: v('--ring-instance'),
      product: v('--ring-product'),
      client: v('--ring-client'),
      goal: v('--ring-goal'),
      task: v('--ring-task'),
      decision: v('--ring-decision'),
      blocker: v('--ring-blocker'),
      event: v('--ring-event'),
      status_change: v('--ring-status_change'),
      observation: v('--ring-observation'),
      roadmap: v('--ring-roadmap'),
      pattern: v('--ring-pattern'),
    };
    // Starter kinds read their alias tokens (style.css), which resolve to the sim's colours.
    Object.keys(KIND_STYLE).forEach(function (kind) { ring[kind] = v('--ring-' + kind) || ring[KIND_STYLE[kind]]; });
    // Precompute the inactive (desaturated) ring per type so the draw hot path
    // is a map lookup, never a color computation — it runs every frame now that
    // the simulation never freezes.
    var ringInactive = {};
    Object.keys(ring).forEach(function (k) {
      ringInactive[k] = desaturateColor(ring[k], INACTIVE_DESAT);
    });
    THEME = {
      bg: v('--bg'),
      cream: v('--cream'),
      accent: v('--accent'),
      edge: v('--edge'),
      liveEdge: v('--edge-live'),
      edgeBright: v('--edge-bright'),
      edgeFaded: v('--edge-faded'),
      edgeLabel: v('--edge-label'),
      labelHalo: v('--label-halo'),
      hubText: '#1a1a1a',   // dark ink reads on the cream hub disk in both themes
      ownerText: v('--text'),
      ring: ring,
      ringInactive: ringInactive,
    };
    // Dense lever 2 (edge ink). edgeRest is what resting inactive-touching
    // edges draw with; off-flag it aliases edgeFaded so default rendering is
    // unchanged. Dense also lifts the base edge a step. Values live in CSS
    // (--edge-dense / --edge-rest-dense) so both themes stay tuned in one place.
    THEME.edgeRest = THEME.edgeFaded;
    if (DENSE) {
      THEME.edge = v('--edge-dense') || THEME.edge;
      THEME.edgeRest = v('--edge-rest-dense') || THEME.edgeFaded;
    }
  }

  // ---------------- boot ----------------

  graphRequest=0;
  rebuildThemeCache();
  loadSchema();
  loadGraph(false);
  // Legend depends on LEGEND_TYPES + hiddenTypes which are declared later in
  // the IIFE. var hoisting makes them visible but not initialized — defer to
  // microtask so the assignments run first.
  Promise.resolve().then(renderLegend);

  function loadSchema() {
    var epoch = viewEpoch;
    schemaAsked = true;
    window.groveApi('/api/schema').then(function (r) { return r.json(); }).then(function (s) {
      if (epoch !== viewEpoch || request!==graphRequest) return;   // stale reads cannot change the view
      if (s && !s.error) schemaByType = s; else schemaAsked = false;
    }).catch(function () { /* schema is optional; panel falls back */ if (epoch === viewEpoch) schemaAsked = false; });
  }

  document.getElementById('refresh-data').addEventListener('click', function () { loadGraph(true); });
  document.getElementById('theme-toggle').addEventListener('click', toggleTheme);
  document.getElementById('active-only').addEventListener('change',function(){activeOnly=this.checked;clearSelection({silent:true});applyVisibility();scheduleRedraw();});
  document.getElementById('search-btn').addEventListener('click', toggleSearch);
  document.getElementById('detail-close').addEventListener('click', closePanel);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (!document.getElementById('search-overlay').hasAttribute('hidden')) toggleSearch();
      else if (hasSelection()) clearSelection();
      else closePanel();
    } else if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
      e.preventDefault();
      toggleSearch();
    }
  });

  // Light is the HTML default now; only a returning user who explicitly chose
  // dark gets flipped back. rebuildThemeCache() already ran at boot against the
  // light default, so first paint is correct without this block.
  var savedTheme = storage.getItem('grove-minimal-codex-theme');
  if (savedTheme === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
    rebuildThemeCache();
  }

  document.addEventListener('visibilitychange', function () {
    if (!Graph) return;
    if (document.hidden) Graph.pauseAnimation(); else Graph.resumeAnimation();
  });

  window.addEventListener('resize', function () {
    if (!Graph) return;
    Graph.width(window.innerWidth).height(window.innerHeight);
    if (!focusedNode) frameGrove(300);
  });

  // ---------------- data load ----------------

  // Local YYYY-MM-DD (matches heartbeat backup dir naming, which uses local time).
  function todayLocalDate() {
    var d = new Date();
    return d.getFullYear() + '-' +
           String(d.getMonth() + 1).padStart(2, '0') + '-' +
           String(d.getDate()).padStart(2, '0');
  }

  // Render a delta as plain text "(+5)" or, when positive and a comparison
  // date exists, as a clickable button that highlights the new items on the
  // canvas via /api/diff?since=...
  function fmtDeltaHTML(n, kind) {
    if (n === 0 || n == null || isNaN(n)) return '';
    var sign = n > 0 ? '+' : '';
    if (n > 0 && prevSnapshotDate) {
      return ' (<button type="button" class="stats-delta" data-kind="' + kind +
        '" title="highlight new ' + kind + ' since ' + esc(prevSnapshotDate) + '">' +
        sign + n + '</button>)';
    }
    return ' (' + sign + n + ')';
  }

  // Pick the most recent history entry strictly before today — that's "yesterday"
  // (or whichever last day had a backup). Returns null if no prior day exists.
  function previousDayEntry(history) {
    var today = todayLocalDate();
    for (var i = history.length - 1; i >= 0; i--) {
      if (history[i].date < today) return history[i];
    }
    return null;
  }

  var layoutCache = null, stableFrame = null;
  var lastNodeCount = null, lastEdgeCount = null, manualCamera = false;
  function renderStats() {
    var state=window.GroveSimulation.getState(),live=window.GroveSimulation.isLive();
    function delta(kind,count,removed){return count?' <button class="stats-delta gp-count-delta'+(removed?' is-removed':'')+'" data-kind="'+kind+'" aria-label="Show '+count+' '+(removed?'removed ':'new ')+(kind.includes('nodes')?'nodes':'connections')+'">('+(removed?'−':'+')+count+')</button>':'';}
    statsEl.innerHTML='<span>'+(live?state.currentNodeCount:rawNodes.length)+' nodes</span>'+delta('nodes',state.nodeDelta)+delta('removed-nodes',state.removedNodes,true)+' <span class="gp-stat-divider">·</span> <span>'+(live?state.currentEdgeCount:rawEdges.length)+' connections</span>'+delta('edges',state.edgeDelta)+delta('removed-edges',state.removedEdges,true)+(live?'<span class="stats-baseline"> · '+(state.graphBaselineAt?'Since previous graph check':'First graph check is the baseline')+'</span>':'');
    statsEl.querySelectorAll('[data-kind]').forEach(function(el){el.onclick=function(){selectChangesOnCanvas(el.dataset.kind);};});
    lastNodeCount=rawNodes.length;lastEdgeCount=rawEdges.length;
  }

  // The sim's own layout of a node set: seedLayout, opened out, with room for the identity at
  // the centre. Returns {cache: {id: node}, frame}; fixture and live mode share it.
  function simLayout(ns, es) {
    seedLayout(ns, es);
    var cache = {};
    ns.forEach(function (n) {
      if (n.__open) { n.x = n.__open[2]; n.y = n.__open[3]; delete n.__open; }
      cache[n.id] = n;
    });
    // An empty world (live mode only): the identity sits at the centre of the empty layout.
    if (!ns.length) { var g0 = computeLayoutGeometry(0); cache['grove-self'] = { x: g0.cx, y: g0.cy }; return { cache: cache, frame: { x0: g0.cx - 65, x1: g0.cx + 65, y0: g0.cy - 65, y1: g0.cy + 65 } }; }
    var xs=ns.map(function(n){return n.x;}),ys=ns.map(function(n){return n.y;});
    var selfX=(Math.min.apply(null,xs)+Math.max.apply(null,xs))/2,selfY=(Math.min.apply(null,ys)+Math.max.apply(null,ys))/2;
    // Reserve breathing room for the identity without stacking it over a hub.
    ns.forEach(function(n,i){var dx=n.x-selfX,dy=n.y-selfY,r=Math.hypot(dx,dy),a=r?Math.atan2(dy,dx):i*2.39996,space=r+180*Math.exp(-r/600);n.x=selfX+Math.cos(a)*space;n.y=selfY+Math.sin(a)*space;});
    xs=ns.map(function(n){return n.x;});ys=ns.map(function(n){return n.y;});
    var frame={x0:Math.min.apply(null,xs)-65,x1:Math.max.apply(null,xs)+65,y0:Math.min.apply(null,ys)-65,y1:Math.max.apply(null,ys)+65};
    cache['grove-self']={x:selfX,y:selfY};
    return { cache: cache, frame: frame };
  }

  function prepareLayout(payload) {
    var fixture = window.GROVE_FIXTURE;
    if (!fixture) return prepareLiveLayout(payload);
    // Fixture mode (the checks): the FULL fixture is laid out once, so nothing moves as imports reveal it.
    if (layoutCache) return;
    var ns = JSON.parse(JSON.stringify(fixture.elements.nodes)).map(function (w) { return w.data; });
    var es = fixture.elements.edges.map(function (w) { return w.data; });
    var laid = simLayout(ns, es);
    layoutCache = laid.cache;
    stableFrame = laid.frame;
  }

  // Live layout is a projection of current adjacency, separate from the stored map.
  // Never send these display coordinates to the positions endpoint.
  var livePlaced = {}, liveSaving = {};
  var SELF_R_FAR = 90;
  function prepareLiveLayout(payload) {
    keepStoredLayout(payload);
    var el=payload.elements,all=el.nodes.map(function(w){return Object.assign({},w.data);});
    adoptKinds(all);
    var es=el.edges.map(function(w){return w.data;});
    var metadata=simLayout(all.filter(function(n){return n.id!=='grove-self';}),es);
    var view=window.GroveLayoutKeep.organicView(all,es);
    layoutCache={};
    var x0=Infinity,x1=-Infinity,y0=Infinity,y1=-Infinity;
    all.forEach(function(n){var p=view.positions[n.id];layoutCache[n.id]=Object.assign({},metadata.cache[n.id]||{},p,{_primaryHubId:view.primary[n.id]===n.id?null:view.primary[n.id],_hubR:view.radii[n.id],_demoted:false});x0=Math.min(x0,p.x);x1=Math.max(x1,p.x);y0=Math.min(y0,p.y);y1=Math.max(y1,p.y);});
    stableFrame=all.length?{x0:x0-65,x1:x1+65,y0:y0-65,y1:y1+65}:metadata.frame;
  }
  // Retain the original insert-only map independently of the visual projection.
  function keepStoredLayout(payload) {
    var el = (payload && payload.elements) || { nodes: [], edges: [] };
    var serverStored = {};
    el.nodes.forEach(function (w) {
      var d = w.data;
      if (typeof d.x === 'number' && typeof d.y === 'number' && isFinite(d.x) && isFinite(d.y)) serverStored[d.id] = { x: d.x, y: d.y };
    });
    var all = JSON.parse(JSON.stringify(el.nodes)).map(function (w) { return w.data; });
    adoptKinds(all);
    var ns = all.filter(function (n) { return n.id !== 'grove-self'; });
    var es = el.edges.map(function (w) { return w.data; }).filter(function (e) { return e.source !== 'grove-self' && e.target !== 'grove-self'; });
    var laid = simLayout(ns, es);
    var seed = {}, items = [];
    all.forEach(function (n) {
      var c = laid.cache[n.id] || {};
      seed[n.id] = { x: c.x, y: c.y };
      // A new leaf re-anchors on the hub its seed sits around. The primary hub can be one hop
      // away while the seed sits at the centre, and its offset would throw the leaf far off.
      items.push({ id: n.id, type: n.type, _primaryHubId: c._seedHubId || null, _hubR: c._hubR || null, _demoted: !!c._demoted });
    });
    var edges = el.edges.map(function (w) { return { source: w.data.source, target: w.data.target }; });
    // The room a node takes here is the collide force's radius, and never less than the largest
    // disc the node can draw. A placed item never moves, while a hub's disc grows with its mass
    // (and from 18 to its full size when an ended hub comes back), and the identity node's disc
    // keeps its size on screen, so it is largest on the map when the camera is far out.
    var room = function (n) {
      var r = nodeRadius(n) * (DENSE ? 1.45 : 1.6) + (DENSE ? 6 : 8);
      if (n.id === 'grove-self') return Math.max(r, SELF_R_FAR);
      return isHub(n) ? Math.max(r, HUB_R_MAX) : r;
    };
    var kept = window.GroveLayoutKeep.placeNew(Object.assign({}, livePlaced, serverStored), items, edges, seed, room);
    all.forEach(function (n) {
      var p = kept.positions[n.id];
      livePlaced[n.id] = { x: p.x, y: p.y };
    });
    saveNewPositions(all.filter(function (n) { return !serverStored[n.id]; }).map(function (n) { return n.id; }), kept.positions);
  }
  function saveNewPositions(ids, positions) {
    var S = window.GroveSimulation, G = window.GroveLive;
    if (!S || !S.realId || !G) return;
    var list = [], sent = [];
    ids.forEach(function (id) {
      var real = S.realId(id), p = positions[id];   // the identity node saves under the self item's id
      if (!real || !p || liveSaving[real]) return;
      liveSaving[real] = 1; sent.push(real);
      list.push({ id: real, x: p.x, y: p.y });
    });
    if (!list.length) return;
    G.savePositions(list).catch(function (err) {
      // Not saved: the map still renders, and the next load saves these items again.
      sent.forEach(function (real) { delete liveSaving[real]; });
      console.warn('Grove: map positions were not saved', err);
    });
  }

  function frameGrove(duration) {
    if (!Graph || !fgNodes.length) return;
    var ns = fgNodes.filter(function (n) { return !hiddenTypes[n.type]; });
    if (!ns.length) return;
    var x0=Infinity, y0=Infinity, x1=-Infinity, y1=-Infinity;
    ns.forEach(function (n) { var r=nodeRadius(n)+24; x0=Math.min(x0,n.x-r); x1=Math.max(x1,n.x+r); y0=Math.min(y0,n.y-r); y1=Math.max(y1,n.y+r); });
    var w=window.innerWidth,h=window.innerHeight;
    var setup = window.GroveSimulation && !window.GroveSimulation.getState().onboardingComplete;
    if(stableFrame){x0=stableFrame.x0;x1=stableFrame.x1;y0=stableFrame.y0;y1=stableFrame.y1;}
    var side = setup && w > 820;
    var areaLeft=side?Math.min(w*.58,Math.max(64,(w-1220)/2)+440+64):(w>820?235:22);
    var areaW=w-areaLeft-30;
    var phoneSetup=setup&&w<=820;
    var areaH=phoneSetup?Math.min(210,h*.24):h-175;
    var z = Math.min(1.15, 0.86*Math.min(areaW/Math.max(220,x1-x0), areaH/Math.max(220,y1-y0)));
    var screenX = setup ? (areaLeft+w-30)/2 : w/2;
    var screenY = phoneSetup?Math.min(160,h*.17):h/2+12;
    Graph.centerAt((x0+x1)/2-(screenX-w/2)/z, (y0+y1)/2-(screenY-h/2)/z, duration||0);
    Graph.zoom(z,duration||0);
  }

  window.reloadGrove = function () { return loadGraph(true); };
  window.GroveView = {
    showChanges:selectChangesOnCanvas,
    fit: function (duration) { manualCamera=false; frameGrove(prefersReducedMotion.matches?0:(duration===undefined?600:duration)); },
    close: function () { closePanel(); },
    openNode: function (id) { if (fgNodeById[id]) onNodeClick(fgNodeById[id]); },
    tourNode: function () {
      if (!Graph || !fgNodes.length) return null;
      var n = fgNodes.find(function(n){return n.id==='grove-self';}) || fgNodes.find(function (n) { return isHub(n) && !n.ended && !hiddenTypes[n.type]; }) || fgNodes.find(function(n){return n.type==='person';}) || fgNodes[0];
      var point = Graph.graph2ScreenCoords(n.x,n.y);
      return {id:n.id,title:n.title,x:point.x,y:point.y,r:n.id==='grove-self'?Math.max(14,Math.min(27,90*Graph.zoom())):Math.max(20,nodeRadius(n)*Graph.zoom())};
    },
    nodes: function () { return fgNodes; },
    graph: function () { return Graph; },
    clear: function () { clearGraph(); }
  };

  // Live mode, the session has ended: no item of it stays in memory or on screen (the map, the
  // legend, the counts, the node panel, the search, the selection pill), no kind name of it stays
  // in local storage, and no placement of it is kept for the next person. The workers panel
  // hears 'grove:graph-cleared' and drops its own copy.
  function clearGraph() {
    viewEpoch++;graphRequest++;setGraphStatus('','');
    rawNodes = []; rawEdges = []; byId = {};
    fgNodes = []; fgLinks = []; fgNodeById = {}; neighborsOf = {};
    focusedNode = null; hoverNode = null; hoverLink = null; hopDist = {};
    livePlaced = {}; liveSaving = {}; layoutCache = null; stableFrame = null; _evoSig = null;
    schemaByType = {}; schemaAsked = false;
    hiddenTypes = {}; storage.removeItem('grove-minimal-codex-hidden-types');
    manualCamera = false;activeOnly=false;document.getElementById('active-only').checked=false;
    clearSelection({ silent: true });
    document.getElementById('selection-pill').innerHTML = '';
    document.getElementById('search-overlay').setAttribute('hidden', '');
    document.getElementById('search-input').value = '';
    document.getElementById('search-results').innerHTML = '';
    if (Graph) Graph.graphData({ nodes: [], links: [] });
    detailEl.classList.remove('open');
    detailEl.setAttribute('aria-hidden', 'true');
    detailBodyEl.innerHTML = '';
    renderLegend();
    renderStats();
    scheduleRedraw();
    loadingEl.classList.add('hidden');   // a read still out draws nothing, so nothing else lifts the cover
    window.dispatchEvent(new CustomEvent('grove:graph-cleared'));
  }
  canvasEl.addEventListener('pointerdown', function () { manualCamera=true; });

  var graphRequest;
  function placeToolbarControls(){var bar=document.querySelector('.topbar');if(bar)document.documentElement.style.setProperty('--grove-toolbar-bottom',bar.getBoundingClientRect().bottom+'px');}
  window.addEventListener('resize',placeToolbarControls);
  function setGraphStatus(state,text){var el=document.getElementById('graph-status');if(el){el.dataset.state=state;el.textContent=text;}var button=document.getElementById('refresh-data');if(button)button.setAttribute('aria-busy',String(state==='pending'));requestAnimationFrame(placeToolbarControls);}
  function loadGraph(refresh) {
    var epoch = viewEpoch,request=++graphRequest;
    setGraphStatus('pending',refresh?'Refresh in progress':'Load in progress');
    return window.groveApi('/api/graph').then(function(r){return r.json();}).then(function(payload){
      if (payload.error) { var failed = new Error(payload.error); failed.code = payload.code; throw failed; }
      if (epoch !== viewEpoch || request!==graphRequest) return;   // stale reads cannot change the view
      if (!schemaAsked) loadSchema();    // a sign-in in the same page: the first read answered 401
      prepareLayout(payload);
      var previous = fgNodeById || {};
      rawNodes = payload.elements.nodes.map(function(w){
        var n=w.data, seed=layoutCache && layoutCache[n.id];
        if(seed) ['x','y','_depth','_primaryHubId','_demoted','_orbitTier','_hubR'].forEach(function(k){n[k]=seed[k];});
        return n;
      });
      rawEdges = payload.elements.edges.map(function(w){return w.data;});
      byId={}; rawNodes.forEach(function(n){byId[n.id]=n;});
      if (!rawNodes.length) { hiddenTypes={}; try { storage.setItem('grove-minimal-codex-hidden-types','{}'); } catch (_) {} }
      buildAdjacency(); renderStats(); buildFgPayload();
      fgNodes=fgNodes.map(function(n){
        var old=previous[n.id];
        if(old) { var x=old.x,y=old.y,fx=old.fx,fy=old.fy;Object.assign(old,n);old.x=x;old.y=y;old.fx=fx;old.fy=fy;return old; }
        n.__home=[n.x,n.y]; n.__born=performance.now(); return n;
      });
      fgNodeById={}; fgNodes.forEach(function(n){fgNodeById[n.id]=n;});
      fgNodes.forEach(function(n){n._primaryHub=n._primaryHubId?fgNodeById[n._primaryHubId]||null:null;});
      if (!Graph) initGraph();
      else {
        var visible=fgNodes.filter(function(n){return !hiddenTypes[n.type];});
        var ids={};visible.forEach(function(n){ids[n.id]=1;});
        Graph.graphData({nodes:visible,links:fgLinks.filter(function(l){return ids[l.source]&&ids[l.target];})});
      }
      applyVisibility();
      if(focusedNode&&!fgNodeById[focusedNode.id]){focusedNode=null;closePanel();}
      clearSelection({silent:true});
      renderLegend();
      if (!focusedNode && !manualCamera && (!refresh || !previous["grove-self"])) frameGrove(0);
      if (!rawNodes.length) { focusedNode=null; manualCamera=false; detailEl.classList.remove('open'); }
      loadingEl.classList.add('hidden');
      setGraphStatus('ready','Graph updated · '+new Date().toLocaleTimeString());
      window.dispatchEvent(new CustomEvent('grove:graph-rendered'));
      window.dispatchEvent(new CustomEvent('grove:change'));
    }).catch(function(err){
      if(epoch!==viewEpoch||request!==graphRequest||err&&err.code==='superseded')return;
      setGraphStatus('error','Refresh failed. Your last graph stays visible. Try again.');
      // Live mode, signed out: the sign-in card takes the screen, so the loading cover steps aside.
      if (err && err.code === 'unauthenticated') { loadingEl.classList.add('hidden'); return; }
      // Live mode with no local sign-in: the sign-in card takes the screen, and it says what failed.
      if (!window.GROVE_FIXTURE && !window.GroveSimulation.getState().auth) { loadingEl.classList.add('hidden'); console.error(err); return; }
      loadingEl.textContent='Unable to open this brain';console.error(err);
    });
  }

  // ---------------- live evolution (ambient auto-refresh) ----------------
  // The layout is a pure function of the data, so evolution = re-fetch +
  // re-derive: masses re-count, tiers re-promote, satellites re-migrate,
  // disks re-size. Poll every 3 min (the heartbeat writes on a ~5-min floor);
  // re-init ONLY when the graph actually changed and the user is idle (no
  // focus/selection/hover) so we never yank an interaction. A page left open
  // on a screen quietly morphs as the brain learns.
  var _evoSig = null;
  function _graphSignature(ns, es) {
    var newest = '';
    for (var i = 0; i < ns.length; i++) {
      var u = ns[i].updated_at || '';
      if (u > newest) newest = u;
    }
    return ns.length + '|' + es.length + '|' + newest;
  }
  setInterval(function () {
    if (document.hidden) return;
    // Live mode with no local sign-in: the sign-in card has the screen, and no graph goes behind it.
    if (!window.GROVE_FIXTURE && !window.GroveSimulation.getState().auth) return;
    if (hasSelection() || focusedNode || hoverNode) return;   // never mid-interaction
    loadGraph(false);   // One accepted snapshot updates both the map and its comparison.

  }, 180000);

  function buildAdjacency() {
    neighborsOf = {};
    rawNodes.forEach(function (n) { neighborsOf[n.id] = new Set(); });
    rawEdges.forEach(function (e) {
      if (!neighborsOf[e.source] || !neighborsOf[e.target]) return;
      neighborsOf[e.source].add(e.target);
      neighborsOf[e.target].add(e.source);
    });
  }

  // ---------------- seed layout (hubs pinned, leaves loose) ----------------
  //
  // Hard-place hubs in a uniform ring around canvas center and pin them with
  // fx/fy. Every other node is positioned NEAR its primary hub (or canvas
  // center if it has no hub neighbor) with random jitter — these are starting
  // positions only; the d3-force simulation in initGraph() will refine them.

  // Single source of truth for layout geometry. The working area is sized to
  // the NODE COUNT (not the viewport): ~1,500 disks need far more than a
  // screen's worth of space to settle without overlap. Area grows linearly with
  // n (so side ∝ sqrt(n)) which keeps packing density constant as the graph
  // grows. `unit` sets that density — tuned so the forceCollide padded-disk area
  // packs to ~40% of the layout area, leaving collide enough room to fully
  // separate every node (the no-overlap guarantee). Hubs sit on an ellipse
  // (Rx/Ry) widened to the viewport aspect so they fill a 16:9 frame instead of
  // collapsing into a tight central circle.
  function computeLayoutGeometry(nodeCount) {
    // Dense lever 3a: smaller unit → tighter area (area ∝ unit²; 58/68 ≈ 0.73x)
    // and a narrower hub ring, so galaxy edges touch instead of floating as
    // islands. Collide pad shrinks alongside (see forces) to keep the
    // no-overlap guarantee's packing headroom.
    var unit = DENSE ? 58 : 68;                      // px per sqrt-node (tunable)
    var ringF = DENSE ? 0.37 : 0.42;
    var side = Math.max(1400, Math.sqrt(nodeCount) * unit);
    var vw = window.innerWidth || 1440, vh = window.innerHeight || 900;
    var aspect = vw / vh;                            // spread hubs wider on 16:9
    var k = Math.min(Math.sqrt(aspect) * 1.25, 1.7);
    return {
      W: side, H: side, cx: side / 2, cy: side / 2,
      Rx: side * ringF * k,                          // elliptical hub ring
      Ry: side * ringF / k,
      k: k,                                          // viewport aspect factor
    };
  }

  // Orbit mode: continuous mass-proportional hub disk. Radius ∝ sqrt of the
  // hub's share of the biggest cluster, so AREA ∝ relative mass (honest size
  // encoding); clamped so the smallest hub still carries its label.
  var HUB_R_MIN = 24, HUB_R_MAX = 62;
  function hubRadiusFromMass(mass, maxMass) {
    return Math.round(HUB_R_MIN + (HUB_R_MAX - HUB_R_MIN) * Math.sqrt(mass / Math.max(maxMass, 1)));
  }

  // ---------------- orbit placement (ORBIT mode) ----------------
  // items: [{id, mass, demoted}] · tissue: {"a|b": sharedEdgeCount} (a<b)
  // Returns {id: {x, y, demoted, tier}}. Pure function of the data — same
  // layout every load.
  function orbitLayout(items, tissue, geo) {
    var cx = geo.cx, cy = geo.cy;
    function T(a, b) { return tissue[a < b ? a + '|' + b : b + '|' + a] || 0; }
    function rC(m) { return 26 + 15 * Math.sqrt(Math.max(m, 1)); }  // cluster visual radius

    var live = items.filter(function (h) { return !h.demoted; })
                    .sort(function (a, b) { return b.mass - a.mass; });
    var demoted = items.filter(function (h) { return h.demoted; });
    var out = {};
    if (!live.length && !demoted.length) return out;
    var maxM = live.length ? live[0].mass : 1;

    // Tiers: relative to the biggest cluster, capped core of 4.
    var core = [], orbit = [], rim = [];
    live.forEach(function (h) {
      if (core.length < 4 && h.mass >= 0.25 * maxM) core.push(h);
      else if (h.mass >= 0.06 * maxM) orbit.push(h);
      else rim.push(h);
    });
    if (!core.length && live.length) core.push(live[0]);

    // Core chain order: permutation maximizing adjacent shared tissue, so the
    // hub that bridges two others sits BETWEEN them (one hub between two hubs).
    function bestOrder(arr) {
      if (arr.length <= 2) return arr;
      var best = arr, bestScore = -1;
      function permute(rest, acc) {
        if (!rest.length) {
          var s = 0;
          for (var i = 0; i + 1 < acc.length; i++) s += T(acc[i].id, acc[i + 1].id);
          if (s > bestScore) { bestScore = s; best = acc.slice(); }
          return;
        }
        for (var j = 0; j < rest.length; j++) {
          permute(rest.slice(0, j).concat(rest.slice(j + 1)), acc.concat([rest[j]]));
        }
      }
      permute(arr, []);
      return best;
    }
    var chain = bestOrder(core);

    // Horizontal core chain centered on (cx, cy); gap ∝ the two cluster radii.
    var xs = [0], i;
    for (i = 1; i < chain.length; i++) {
      xs.push(xs[i - 1] + 0.75 * (rC(chain[i - 1].mass) + rC(chain[i].mass)));
    }
    var half = xs[xs.length - 1] / 2;
    var coreMaxRc = 0;
    chain.forEach(function (h, idx) {
      out[h.id] = { x: cx + xs[idx] - half, y: cy, demoted: false, tier: 'core' };
      if (rC(h.mass) > coreMaxRc) coreMaxRc = rC(h.mass);
    });

    // Orbit/rim/demoted: desired angle = toward the strongest-tissue partner
    // among already-placed hubs; ties/center-partners alternate top/bottom.
    var ring = orbit.map(function (h) { return { h: h, tier: 'orbit' }; })
      .concat(rim.map(function (h) { return { h: h, tier: 'rim' }; }))
      .concat(demoted.map(function (h) { return { h: h, tier: 'demoted' }; }));
    var flip = 1;
    ring.forEach(function (it) {
      var bestId = null, bestT = -1;
      Object.keys(out).forEach(function (pid) {
        var t = T(it.h.id, pid);
        if (t > bestT) { bestT = t; bestId = pid; }
      });
      var a;
      if (bestId && Math.abs(out[bestId].x - cx) > 40) {
        a = Math.atan2(0, out[bestId].x - cx);          // 0 (right) or π (left)
        a += flip * (Math.PI / 5);                      // lift off the axis
      } else {
        a = flip * Math.PI / 2;                         // center partner → top/bottom
      }
      flip = -flip;
      it.angle = a;
    });

    // Angular relaxation (wrap-aware): push overlapping neighbors apart so
    // every satellite gets arc proportional to its cluster radius.
    var Rx0 = half + 120, Ry0 = coreMaxRc * 0.62 + 90;
    ring.forEach(function (it) {
      // Demoted hubs EMBED inside the cloud (0.55) — they hold no territory
      // and must not stretch the frame from the rim.
      var f = it.tier === 'orbit' ? 1.0 : (it.tier === 'rim' ? 1.14 : 0.55);
      it.Rx = (Rx0 + rC(it.h.mass)) * f;
      it.Ry = (Ry0 + rC(it.h.mass) * 0.8) * f;
      it.halfArc = Math.min(0.9, (rC(it.h.mass) + 40) / ((it.Rx + it.Ry) / 2));
    });
    ring.sort(function (a, b) { return a.angle - b.angle; });
    for (var pass = 0; pass < 40; pass++) {
      var moved = false;
      for (i = 0; i < ring.length; i++) {
        var A = ring[i], B = ring[(i + 1) % ring.length];
        var gap = (i + 1 < ring.length) ? B.angle - A.angle
                                        : (B.angle + 2 * Math.PI) - A.angle;
        var need = A.halfArc + B.halfArc;
        if (gap < need) {
          var push = (need - gap) / 2;
          A.angle -= push; B.angle += push;
          moved = true;
        }
      }
      ring.sort(function (a, b) { return a.angle - b.angle; });
      if (!moved) break;
    }
    ring.forEach(function (it) {
      out[it.h.id] = {
        x: cx + it.Rx * Math.cos(it.angle),
        y: cy + it.Ry * Math.sin(it.angle),
        demoted: it.tier === 'demoted',
        tier: it.tier,
      };
    });

    // Aspect-fill: stretch every x-offset by the viewport factor so the blob
    // fills 16:9 instead of sitting as a circle with dead side margins. The
    // core chain widens with it — each core hub gains its own territory.
    var kx = geo.k || 1;
    Object.keys(out).forEach(function (id) {
      out[id].x = cx + (out[id].x - cx) * kx;
    });
    return out;
  }

  function seedLayout(nodes, edges) {
    // Build adjacency on raw nodes (drives primary-hub picking + visibleDegree).
    var nByid = {};
    nodes.forEach(function (n) { nByid[n.id] = n; n._neighbors = []; });
    edges.forEach(function (e) {
      var a = nByid[e.source], b = nByid[e.target];
      if (!a || !b) return;
      a._neighbors.push(b); b._neighbors.push(a);
    });

    var hubs = nodes.filter(function (n) { return HUB_TYPES[n.type]; });
    var nonHubs = nodes.filter(function (n) { return !HUB_TYPES[n.type]; });

    // Hub ordering: visible (non-temporal, non-ended) degree DESC.
    var ENDED = { archived: 1, cancelled: 1, completed: 1, sunset: 1,
                  departed: 1, resolved: 1, superseded: 1 };
    var TEMPORAL_OR_TASK = { event: 1, observation: 1, status_change: 1, task: 1 };
    var visibleDegreeOf = {};
    function visibleDegree(hub) {
      if (visibleDegreeOf[hub.id] !== undefined) return visibleDegreeOf[hub.id];
      var d = 0;
      hub._neighbors.forEach(function (nb) {
        if (ENDED[nb.status || ''] || nb.invalid_at) return;
        if (TEMPORAL_OR_TASK[nb.type]) return;
        d++;
      });
      return (visibleDegreeOf[hub.id] = d);
    }
    hubs.sort(function (a, b) { return visibleDegree(b) - visibleDegree(a); });

    // Working area + hub-ring geometry sized to the node count (see
    // computeLayoutGeometry). The d3 simulation does the heavy lifting; this is
    // the comfortable area it spreads into.
    var geo = computeLayoutGeometry(nodes.length);
    var W = geo.W, H = geo.H, cx = geo.cx, cy = geo.cy;

    // 1. Hard-place hubs. Ring mode: even slots on the ellipse, all pinned.
    //    Orbit mode: placement happens AFTER primary-hub assignment below —
    //    it needs cluster masses + cross-tissue, which need the primaries.
    if (!ORBIT) {
      hubs.forEach(function (hub, i) {
        var angle = (2 * Math.PI * i / Math.max(hubs.length, 1)) - Math.PI / 2;
        hub.x = cx + geo.Rx * Math.cos(angle);
        hub.y = cy + geo.Ry * Math.sin(angle);
        hub.fx = hub.x;
        hub.fy = hub.y;
      });
    }

    // 2. Pick primary hub for each non-hub (most-connected hub neighbor;
    //    one-hop fallback if no direct hub neighbor; null for orphans).
    function primaryHubFor(node) {
      var hubNeighbors = node._neighbors.filter(function (n) { return HUB_TYPES[n.type]; });
      if (hubNeighbors.length === 0) {
        // Walk one hop.
        var seen = {};
        node._neighbors.forEach(function (nb) {
          (nb._neighbors || []).forEach(function (nn) {
            if (HUB_TYPES[nn.type] && !seen[nn.id]) {
              seen[nn.id] = 1;
              hubNeighbors.push(nn);
            }
          });
        });
      }
      if (hubNeighbors.length === 0) return null;
      // Pick hub with the most edges to this node, tie-break visibleDegree.
      var best = hubNeighbors[0], bestCount = 0;
      hubNeighbors.forEach(function (h) {
        var cnt = node._neighbors.filter(function (nn) { return nn.id === h.id; }).length;
        if (cnt > bestCount || (cnt === bestCount && visibleDegree(h) > visibleDegree(best))) {
          best = h; bestCount = cnt;
        }
      });
      return best;
    }

    // 3a. Assign primaries first (pure adjacency — no positions needed).
    //     Stamp the primary hub id on the raw node so buildFgPayload can
    //     propagate it onto the simulation node (where forces read it).
    nonHubs.forEach(function (n) {
      var hub = primaryHubFor(n);
      n._primaryHubId = hub ? hub.id : null;
      n.__ph = hub;
    });

    // 3b. Orbit mode: place hubs from what the data says — cluster mass +
    //     cross-cluster tissue. Archived hubs demote (no pin, no pillar size).
    if (ORBIT) {
      var massOf = {};
      hubs.forEach(function (h) { massOf[h.id] = 1; });
      nonHubs.forEach(function (n) {
        if (n._primaryHubId) massOf[n._primaryHubId] = (massOf[n._primaryHubId] || 0) + 1;
      });
      var clusterOf = {};
      nodes.forEach(function (n) {
        clusterOf[n.id] = HUB_TYPES[n.type] ? n.id : n._primaryHubId;
      });
      var tissue = {};
      edges.forEach(function (e) {
        var a = clusterOf[e.source], b = clusterOf[e.target];
        if (!a || !b || a === b) return;
        var k = a < b ? a + '|' + b : b + '|' + a;
        tissue[k] = (tissue[k] || 0) + 1;
      });
      var items = hubs.map(function (h) {
        return { id: h.id, mass: massOf[h.id] || 1,
                 demoted: !!h.invalid_at || isEnded(h) };
      });
      var placed = orbitLayout(items, tissue, geo);
      var maxMass = 1;
      items.forEach(function (it) { if (!it.demoted && it.mass > maxMass) maxMass = it.mass; });
      hubs.forEach(function (h) {
        var p = placed[h.id];
        if (!p) { h.x = cx; h.y = cy; h.fx = h.x; h.fy = h.y; return; }
        h.x = p.x; h.y = p.y;
        h._orbitTier = p.tier;
        if (p.demoted) { h._demoted = true; }
        else {
          h.fx = h.x; h.fy = h.y;
          // Disk AREA ∝ cluster mass (radius ∝ sqrt): a company's size IS how
          // much of the brain lives around it. Evolves with the data.
          h._hubR = hubRadiusFromMass(massOf[h.id] || 1, maxMass);
        }
      });
    }

    // 3c. Place leaves around the hub the forces will pull them to (the first
    //     hub they link to, as applyVisibility assigns; the centre for leaves
    //     with none), on an even spiral in link order, so linked leaves start
    //     side by side and nothing starts on top of anything. Leaves start at
    //     SEED_START of their settled spread and the brain opens out from
    //     there. Deterministic: the same brain loads the same way every time.
    //     The settled spread is kept as the camera's first frame.
    var SEED_GAP = { hub: 52, centre: 42 };   // between spiral arms: the settled spacing, measured
    var SEED_START = 0.6;
    // the room each leaf takes, as the collide force sees it, so neighbours start just apart
    function seedRoom(n) { return (SIZES[n.type] || 12) * (DENSE ? 1.45 : 1.6) + (DENSE ? 6 : 8); }
    // the middle mass sits wider than tall, like the hub ring around it
    var seedWide = Math.pow(geo.k || 1, 1 / 3);
    var firstHub = {};
    edges.forEach(function (e) {
      var a = nByid[e.source], b = nByid[e.target];
      if (!a || !b) return;
      if (HUB_TYPES[b.type] && !HUB_TYPES[a.type] && !firstHub[a.id]) firstHub[a.id] = b;
      if (HUB_TYPES[a.type] && !HUB_TYPES[b.type] && !firstHub[b.id]) firstHub[b.id] = a;
    });
    var seedGroups = {}, seedOrder = [];
    nonHubs.forEach(function (n) {
      var key = firstHub[n.id] ? firstHub[n.id].id : '';
      n._seedHubId = key || null;   // the hub this leaf's seed sits around (none: the centre)
      if (!seedGroups[key]) { seedGroups[key] = []; seedOrder.push(key); }
      seedGroups[key].push(n);
    });
    var seedFrame = { x0: Infinity, x1: -Infinity, y0: Infinity, y1: -Infinity };
    function frameAdd(x, y, pad) {
      seedFrame.x0 = Math.min(seedFrame.x0, x - pad); seedFrame.x1 = Math.max(seedFrame.x1, x + pad);
      seedFrame.y0 = Math.min(seedFrame.y0, y - pad); seedFrame.y1 = Math.max(seedFrame.y1, y + pad);
    }
    hubs.forEach(function (h) { frameAdd(h.x, h.y, h._hubR || 40); });
    seedOrder.forEach(function (key) {
      var group = seedGroups[key], hub = key ? firstHub[group[0].id] : null;
      var hx = hub ? (hub.fx != null ? hub.fx : hub.x) : cx;
      var hy = hub ? (hub.fy != null ? hub.fy : hub.y) : cy;
      var gap = hub ? SEED_GAP.hub : SEED_GAP.centre;
      var core = hub ? (hub._hubR || 30) : 0;
      // link order: breadth-first through the group's own links, busiest leaf first
      var inGroup = {}, deg = {};
      group.forEach(function (n) { inGroup[n.id] = 1; });
      group.forEach(function (n) {
        deg[n.id] = 0;
        n._neighbors.forEach(function (m) { if (inGroup[m.id]) deg[n.id]++; });
      });
      var placed = {}, ordered = [];
      group.slice().sort(function (a, b) { return deg[b.id] - deg[a.id] || (a.id < b.id ? -1 : 1); })
        .forEach(function (root) {
          if (placed[root.id]) return;
          var queue = [root];
          placed[root.id] = 1;
          for (var q = 0; q < queue.length; q++) {
            ordered.push(queue[q]);
            queue[q]._neighbors.forEach(function (m) {
              if (inGroup[m.id] && !placed[m.id]) { placed[m.id] = 1; queue.push(m); }
            });
          }
        });
      // Section order, when the agents layer serves the sections (window.__hoodSections):
      // each section's things one after another in the planner's order, and the sections
      // breadth-first over the links between them from the one holding the busiest leaf, so a
      // section is one stretch of the spiral and linked sections sit side by side. Leaves in no
      // section keep link order, after them.
      var HS = window.__hoodSections;
      if (HS) {
        var secNodes = {}, secOf = {}, secKeys = [];
        group.forEach(function (n) {
          var m = HS[n.id];
          if (!m) return;
          secOf[n.id] = m[0];
          if (!secNodes[m[0]]) { secNodes[m[0]] = []; secKeys.push(m[0]); }
          secNodes[m[0]].push(n);
        });
        if (secKeys.length) {
          secKeys.forEach(function (k) { secNodes[k].sort(function (a, b) { return HS[a.id][1] - HS[b.id][1]; }); });
          var sLinks = {};
          group.forEach(function (n) {
            var a = secOf[n.id];
            if (a === undefined) return;
            n._neighbors.forEach(function (m) {
              var b = secOf[m.id];
              if (b === undefined || b === a) return;
              var row = sLinks[a] || (sLinks[a] = {});
              row[b] = (row[b] || 0) + 1;
            });
          });
          var busiest = null;
          group.forEach(function (n) {
            if (secOf[n.id] !== undefined && (!busiest || deg[n.id] > deg[busiest.id])) busiest = n;
          });
          var seenS = {}, secOrder = [];
          [secOf[busiest.id]].concat(secKeys.slice().sort(function (a, b) {
            return secNodes[b].length - secNodes[a].length || a - b;
          })).forEach(function (root) {
            if (seenS[root]) return;
            seenS[root] = 1;
            var line = [root];
            for (var li = 0; li < line.length; li++) {
              secOrder.push(line[li]);
              var row = sLinks[line[li]] || {};
              Object.keys(row).map(Number).sort(function (a, b) { return row[b] - row[a] || a - b; }).forEach(function (t) {
                if (!seenS[t]) { seenS[t] = 1; line.push(t); }
              });
            }
          });
          // the planner's own sweep order, when it exports one, wins over this breadth-first one
          var HSO = window.__hoodSectionOrder;
          if (HSO && HSO.length) {
            var rank = {};
            HSO.forEach(function (k, r) { rank[k] = r; });
            secOrder = secOrder.filter(function (k) { return rank[k] !== undefined; })
              .sort(function (a, b) { return rank[a] - rank[b]; })
              .concat(secOrder.filter(function (k) { return rank[k] === undefined; }));
          }
          var inSections = [];
          secOrder.forEach(function (k) { inSections = inSections.concat(secNodes[k]); });
          ordered = inSections.concat(ordered.filter(function (n) { return secOf[n.id] === undefined; }));
        }
      }
      // even spiral: neighbours along the arm just touch, arms one gap apart
      var r0 = core + gap * (hub ? 1 : 0.5), theta = 0, r = r0, prev = null;
      var sx = hub ? 1 : seedWide, sy = hub ? 1 : 1 / seedWide;
      ordered.forEach(function (n) {
        if (prev) {
          theta += (seedRoom(prev) + seedRoom(n) + 2) / r;
          r = r0 + gap * theta / (2 * Math.PI);
        }
        prev = n;
        var ux = Math.cos(theta) * sx, uy = Math.sin(theta) * sy;
        frameAdd(hx + ux * r, hy + uy * r, 20);
        var start = core + (r - core) * SEED_START;
        n.x = hx + ux * start;
        n.y = hy + uy * start;
        n.__open = [n.x, n.y, hx + ux * r, hy + uy * r];   // where it glides to as the brain opens
      });
    });
    window.__brainSeedFrame = seedFrame;

    // Back-layer parallax for temporal/ended nodes (alpha-only, doesn't move them).
    var TEMPORAL = { event: 1, observation: 1, status_change: 1 };
    nodes.forEach(function (n) {
      if (HUB_TYPES[n.type]) return;
      if (TEMPORAL[n.type] || isEnded(n)) n._depth = 1;
    });

    // Diagnostics for verification scripts.
    window.__brainVizCanvas = { w: W, h: H, cx: cx, cy: cy, Rx: geo.Rx, Ry: geo.Ry, R_HUB: Math.max(geo.Rx, geo.Ry), hubCount: hubs.length };
  }


  function buildFgPayload() {
    fgNodes = rawNodes.map(function (n) {
      var hub = HUB_TYPES[n.type];
      var node = {
        id: n.id,
        title: n.label,
        type: n.type,
        status: n.status,
        ended: isEnded(n),
        depth: n._depth || 0,
        x: n.x, y: n.y,
        __r: SIZES[n.type] || 12,  // visible radius — used by overlap counter
        _primaryHubId: n._primaryHubId || null,  // for per-leaf hubX/hubY gravity
        _demoted: !!n._demoted,    // orbit mode: archived hub, floats unpinned
        _orbitTier: n._orbitTier || null,        // orbit mode: core|orbit|rim
        _hubR: n._hubR || null,    // orbit mode: mass-proportional disk radius
      };
      // Pin hubs in place. Leaves stay loose so the simulation can move them.
      // Demoted hubs (orbit mode) float free — they hold no frame territory.
      if (n.id==='grove-self'||hub && !n._demoted) { node.fx = n.x; node.fy = n.y; }
      return node;
    });
    fgNodeById = {};
    fgNodes.forEach(function (n) { fgNodeById[n.id] = n; });
    // Resolve _primaryHubId -> direct reference now that fgNodeById is built.
    fgNodes.forEach(function (n) {
      n._primaryHub = n._primaryHubId ? (fgNodeById[n._primaryHubId] || null) : null;
    });
    fgLinks = rawEdges.filter(function (e) {
      return fgNodeById[e.source] && fgNodeById[e.target];
    }).map(function (e) {
      return {
        source: e.source,
        target: e.target,
        verb: e.verb,
        ended: !!e.invalid_at,
        // Precompute "touches an inactive node" so the per-frame link-color
        // lookup stays a property read (mirrors drawNode's inactive rule).
        inactive: linkTouchesInactive(fgNodeById[e.source], fgNodeById[e.target]),
      };
    });
  }

  // ---------------- force-graph init ----------------

  // Gentle perpetual "breathing" drift. force-graph 1.43.5 does NOT expose
  // d3AlphaTarget, so we can't floor the simulation's alpha to keep it warm.
  // Instead a custom d3 force injects a tiny, ALPHA-INDEPENDENT oscillating
  // velocity into every unpinned node each tick. Because the structural forces
  // (charge/link/hubX/hubY) fade as alpha decays to ~0, the layout settles and
  // then only this drift + forceCollide (also alpha-independent) stay active —
  // so nodes bob gently around their settled positions, collide still prevents
  // overlap every tick, and nothing wanders (sin/cos average to 0 → bounded).
  // Per-node golden-angle phases decorrelate neighbors so it reads as organic
  // breathing, not a synchronized pulse. Sub-pixel amplitudes; velocityDecay
  // smooths them into a slow drift. Both constants tuned in the visual loop.
  var DRIFT_AMP = 0.05;     // per-tick velocity injection
  var DRIFT_FREQ = 0.025;   // oscillation speed
  function makeDriftForce() {
    var nodes = [];
    var t = 0;
    function force() {
      t += 1;
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        if (n.fx != null || n.fy != null) continue;   // pinned hubs don't drift
        var ph = n._driftPhase || 0;
        n.vx += DRIFT_AMP * Math.sin(t * DRIFT_FREQ + ph);
        n.vy += DRIFT_AMP * Math.cos(t * DRIFT_FREQ + ph * 1.3);
      }
    }
    force.initialize = function (n) {
      nodes = n;
      for (var i = 0; i < nodes.length; i++) {
        nodes[i]._driftPhase = (i * 2.399963) % (Math.PI * 2);   // golden angle
      }
    };
    return force;
  }

  function initGraph() {
    if (Graph && Graph._destructor) Graph._destructor();
    Graph = ForceGraph()(canvasEl)
      .width(window.innerWidth)
      .height(window.innerHeight)
      .backgroundColor('rgba(0,0,0,0)')
      .graphData({ nodes: fgNodes, links: fgLinks })
      .nodeId('id')
      .nodeRelSize(1)
      .nodeLabel(function () { return ''; })
      .linkSource('source')
      .linkTarget('target')
      .linkColor(getLinkColor)
      .linkWidth(getLinkWidth)
      .linkCurvature(0)
      .nodeCanvasObject(drawNode)
      .nodePointerAreaPaint(paintNodeHitArea)
      .linkCanvasObjectMode(function (l) {
        if (ALWAYS_LABELED_VERBS[l.verb]) return 'after';
        if (Graph && Graph.zoom() >= 1.4 && ZOOMED_LABELED_VERBS[l.verb]) return 'after';
        var hovered = hoverNode && (
          (typeof l.source === 'object' ? l.source.id : l.source) === hoverNode.id ||
          (typeof l.target === 'object' ? l.target.id : l.target) === hoverNode.id
        );
        if (hovered || hoverLink === l) return 'after';
        return null;
      })
      .linkCanvasObject(drawLinkLabel)
      .onNodeClick(onNodeClick)
      .onNodeHover(onNodeHover)
      .onLinkHover(function (l) { hoverLink = l; scheduleRedraw(); })
      .onBackgroundClick(onBackgroundClick)
      // Real d3-force physics. Hubs pinned in ring; leaves released to settle.
      // Once the engine stops we freeze every leaf's fx/fy so subsequent
      // renders are pure draw.
      .autoPauseRedraw(false)        // paint every frame so the perpetual drift is visible
      .enableNodeDrag(true)          // user CAN drag a node to reposition it
      // Never freeze. force-graph stops AND freezes the layout the moment
      // cooldownTicks OR cooldownTime is hit, regardless of alpha — so both must
      // be Infinity for continuous motion (confirmed against the force-graph
      // continuous-motion example). The custom 'drift' force (registered after
      // the force block) supplies the gentle perpetual motion. No onEngineStop
      // freeze: leaves stay live, hubs stay pinned via buildFgPayload.
      .cooldownTicks(Infinity)
      .cooldownTime(Infinity)
      .warmupTicks(0)
      .d3AlphaDecay(0.035)
      .d3VelocityDecay(0.4);

    // Cluster physics — each leaf has STRONG positional gravity toward its
    // primary hub. Intra-cluster links are short + strong; cross-cluster
    // links are long + weak so they don't drag leaves out of their territory.
    // Result: visually distinct clumps around each pinned hub, not a unified
    // galaxy. Pattern mirrors the legacy COSE-bilkent compound-graph layout.
    Graph
      .d3Force('center', null)
      .d3Force('charge', d3.forceManyBody()
        // Dense lever 3b: softer, shorter-range repulsion — clusters may touch.
        .strength(function (d) { return isHub(d) ? (DENSE ? -1600 : -2500) : (DENSE ? -230 : -300); })
        .distanceMax(DENSE ? 450 : 700))
      .d3Force('collide', d3.forceCollide()
        // Dense: pad shrinks with the tighter area so packing headroom holds
        // (~45% padded-disk coverage; no-overlap guarantee intact).
        .radius(function (d) { return nodeRadius(d) * (DENSE ? 1.45 : 1.6) + (DENSE ? 6 : 8); })
        .strength(1.0).iterations(3))
      .d3Force('hubX', d3.forceX(function (d) {
        if (isHub(d)) return d.fx != null ? d.fx : d.x;
        // Demoted (unpinned) primary hubs have no fx — gravitate toward their
        // seed x instead (accessor is evaluated at init; the seed is set).
        if (d._primaryHub) return d._primaryHub.fx != null ? d._primaryHub.fx : d._primaryHub.x;
        return (window.__brainVizCanvas && window.__brainVizCanvas.cx) || 0;
      }).strength(function (d) { return isHub(d) ? 0 : 0.12; }))
      .d3Force('hubY', d3.forceY(function (d) {
        if (isHub(d)) return d.fy != null ? d.fy : d.y;
        if (d._primaryHub) return d._primaryHub.fy != null ? d._primaryHub.fy : d._primaryHub.y;
        return (window.__brainVizCanvas && window.__brainVizCanvas.cy) || 0;
      }).strength(function (d) { return isHub(d) ? 0 : 0.12; }));

    // Tune the existing link force — short tight intra-cluster, long loose
    // cross-cluster. Don't replace it (replacing causes a stale links/nodes
    // mismatch on first init).
    var linkForce = Graph.d3Force('link');
    if (linkForce) {
      linkForce
        .distance(function (l) {
          var sHub = isHub(l.source) ? l.source : l.source._primaryHub;
          var tHub = isHub(l.target) ? l.target : l.target._primaryHub;
          return (sHub && tHub && sHub === tHub) ? (DENSE ? 80 : 90) : (DENSE ? 180 : 250);
        })
        .strength(function (l) {
          var sHub = isHub(l.source) ? l.source : l.source._primaryHub;
          var tHub = isHub(l.target) ? l.target : l.target._primaryHub;
          return (sHub && tHub && sHub === tHub) ? 0.7 : 0.05;
        });
    }

    // Let alpha fully decay (d3AlphaMin 0) so the structural forces settle the
    // layout, then register the perpetual drift. Engine never stops (cooldowns
    // Infinity above), so the drift keeps the settled layout gently breathing.
    Graph.d3AlphaMin(0);
    Graph.d3Force('drift', makeDriftForce());

    // Held where it was laid out: once the brain has opened, each leaf is pulled
    // gently back to its place on the spiral, regardless of heat (like the
    // drift), so the layout stays as laid out, section by section, while
    // links, charge and collide still polish it and the drift keeps it
    // breathing. A later filter change lays the brain out from the forces
    // again, so the hold lets go then.
    var HOLD = 0.05;
    window.__brainHoldOff = false;
    Graph.d3Force('hold', (function () {
      var held = [];
      function force() {
        if (window.__brainHoldOff) return;
        for (var i = 0; i < held.length; i++) {
          var n = held[i];
          if (!n.__home || n.fx != null) continue;
          n.vx += (n.__home[0] - n.x) * HOLD;
          n.vy += (n.__home[1] - n.y) * HOLD;
          n.x=Math.max(n.__home[0]-5,Math.min(n.__home[0]+5,n.x));
          n.y=Math.max(n.__home[1]-5,Math.min(n.__home[1]+5,n.y));
          n.vx=Math.max(-.35,Math.min(.35,n.vx));n.vy=Math.max(-.35,Math.min(.35,n.vy));
        }
      }
      force.initialize = function (ns) { held = ns; };
      return force;
    })());

    // Calm start: for the first CALM_MS after load, charge, links and hub
    // gravity act as if the layout runs at ALPHA_START instead of full heat:
    // the seed is already where the brain settles, so the forces only polish
    // it instead of collapsing it towards the hubs and letting it spring
    // back. Later reheats (filters) run at full heat as before.
    var ALPHA_START = 0.01, CALM_MS = 12000, calmUntil = performance.now() + CALM_MS;
    ['charge', 'link', 'hubX', 'hubY'].forEach(function (name) {
      var f = Graph.d3Force(name);
      if (!f) return;
      var calm = function (alpha) { return f(performance.now() < calmUntil ? Math.min(alpha, ALPHA_START) : alpha); };
      Object.keys(f).forEach(function (k) {
        if (typeof f[k] !== 'function') return;
        calm[k] = function () { var r = f[k].apply(f, arguments); return r === f ? calm : r; };
      });
      Graph.d3Force(name, calm);
    });

    // Camera starts ON the mass, frame one. Layout coordinates live in an
    // all-positive quadrant (center ≈ side/2), but force-graph boots its
    // camera at (0,0) — so the graph appeared bottom-right and then JUMPED to
    // center at the 1.5s fit. Instant (0ms) fit over the seeded positions
    // anchors the camera on the cloud immediately; the warmup expansion then
    // plays out centered, growing in place.
    // The seed frame is the settled spread, so the camera is right from frame
    // one and the brain opens out inside it, with a little headroom for the
    // slow outward drift of the next minute; no re-fit chases it.
    var sf = window.__brainSeedFrame;
    if (sf && isFinite(sf.x0)) {
      Graph.centerAt((sf.x0 + sf.x1) / 2, (sf.y0 + sf.y1) / 2, 0);
      Graph.zoom(0.92 * Math.min((window.innerWidth - 100) / (sf.x1 - sf.x0), (window.innerHeight - 100) / (sf.y1 - sf.y0)), 0);
      window.__brainSeedFramed = true;
    } else {
      Graph.zoomToFit(0, 80);
    }
    // Preserve the source layout while progressive imports add nodes.

    // The brain opens out: each leaf glides from its start to its place over
    // OPEN_MS, held on its path by fx/fy, then the forces take over from a
    // settled layout. Smooth and the same every load, where a squeezed start
    // left to the collide force would pop open in a frame or two.
    var OPEN_MS = 1400, openFrom = performance.now();
    var opening = fgNodes.filter(function (n) { return byId[n.id] && byId[n.id].__open; });
    (function openStep() {
      var k = Math.min(1, (performance.now() - openFrom) / OPEN_MS);
      var e = 1 - Math.pow(1 - k, 3);
      opening.forEach(function (n) {
        var o = byId[n.id].__open;
        n.fx = o[0] + (o[2] - o[0]) * e;
        n.fy = o[1] + (o[3] - o[1]) * e;
      });
      if (k < 1) return requestAnimationFrame(openStep);
      opening.forEach(function (n) {
        n.x = n.fx; n.y = n.fy; n.vx = 0; n.vy = 0;
        n.__home = [n.fx, n.fy];   // its place as laid out, which the hold keeps it near
        n.fx = null; n.fy = null;
        delete byId[n.id].__open;
      });
    })();

    // Debug handle (for verification scripting only)
    window.__brainViz = {
      Graph: Graph,
      hover: function (id) {
        var n = fgNodeById[id];
        if (n) { hoverNode = n; scheduleRedraw(); }
      },
      focus: function (id) {
        var n = fgNodeById[id];
        if (n) { focusedNode = n; rebuildHopDist(id); scheduleRedraw(); }
      },
      click: function (id) {
        var n = fgNodeById[id];
        if (n) onNodeClick(n);
      },
      hopDist: function () { return hopDist; },
      reset: function () { hoverNode = null; focusedNode = null; hopDist = {}; scheduleRedraw(); },
    };
  }

  // Low-signal types are hidden in active-only mode.
  // Tasks: leaf-level work items (roll up to their parent goal).
  // Events/observations/status_changes: immutable temporal records, no ongoing state.
  // Decisions: historical intelligence records. validity_state='current' does NOT
  //   mean ongoing work — a decision is not "active" in the operational sense.
  //   Decisions are shown in full-graph mode but hidden in active-only mode.
  var LOW_SIGNAL_TYPES = { event: 1, observation: 1, status_change: 1, task: 1, decision: 1 };

  // A node is "inactive" when it's ended OR a low-signal/temporal type — the
  // same set the active-only filter hides. Hubs are never inactive. This single
  // rule drives shade-based depth (drawNode) and edge calming (getLinkColor);
  // both callers share it so they can't drift apart.
  function nodeIsInactive(n) {
    return !!n && !HUB_TYPES[n.type] && (n.ended || !!LOW_SIGNAL_TYPES[n.type]);
  }
  function linkTouchesInactive(s, t) {
    return nodeIsInactive(s) || nodeIsInactive(t);
  }

  // Per-type legend filter — click a row to hide that type. Persisted to
  // localStorage so the user's preference survives reload.
  var hiddenTypes = {};
  try {
    var saved = JSON.parse(storage.getItem('grove-minimal-codex-hidden-types') || '{}');
    if (saved && typeof saved === 'object') hiddenTypes = saved;
  } catch (_) { hiddenTypes = {}; }

  function applyVisibility() {
    if (!Graph) return;
    var visibleNodes = fgNodes.filter(function (n) {
      if (hiddenTypes[n.type]) return false;
      if (activeOnly && (n.ended || LOW_SIGNAL_TYPES[n.type])) return false;
      return true;
    });
    var visibleSet = {};
    visibleNodes.forEach(function (n) { visibleSet[n.id] = 1; });
    var visibleLinks = fgLinks.filter(function (l) {
      var s = typeof l.source === 'object' ? l.source.id : l.source;
      var t = typeof l.target === 'object' ? l.target.id : l.target;
      return visibleSet[s] && visibleSet[t];
    });

    // Live mode: a legend click only hides and shows. Preserve the current display positions, so
    // nothing below runs: no hub moves, no pin is dropped (a hidden hub keeps its own), and the
    // camera stays. Fixture mode keeps the sim's relayout.
    if (!window.GROVE_FIXTURE) { Graph.graphData({ nodes: visibleNodes, links: visibleLinks }); return; }

    // Dynamic relayout: recompute hub ring from visible hubs only so we don't
    // get empty slots when archived hubs hide. Leaves get fresh primary-hub
    // assignments and unfreeze so they re-settle into the new clusters.
    // Recompute geometry from the VISIBLE node count so a filtered (smaller)
    // graph packs tighter, and refresh the shared diagnostics handle so the
    // orphan-gravity fallback + verification scripts see current geometry.
    var geo = computeLayoutGeometry(visibleNodes.length);
    window.__brainVizCanvas = {
      w: geo.W, h: geo.H, cx: geo.cx, cy: geo.cy,
      Rx: geo.Rx, Ry: geo.Ry, R_HUB: Math.max(geo.Rx, geo.Ry),
      hubCount: visibleNodes.filter(isHub).length,
    };
    var visibleHubs = visibleNodes
      .filter(isHub)
      .map(function (h) {
        var deg = (visibleLinks.filter(function (l) {
          var s = typeof l.source === 'object' ? l.source.id : l.source;
          var t = typeof l.target === 'object' ? l.target.id : l.target;
          return s === h.id || t === h.id;
        }).length);
        return { node: h, degree: deg };
      })
      .sort(function (a, b) { return b.degree - a.degree; });

    if (ORBIT) {
      // Orbit re-placement over the visible set: masses + tissue recomputed
      // from what's on screen, same deterministic engine as the seed.
      var vMass = {};
      visibleHubs.forEach(function (it) { vMass[it.node.id] = 1; });
      visibleNodes.forEach(function (n) {
        if (!isHub(n) && n._primaryHubId && vMass[n._primaryHubId] != null) vMass[n._primaryHubId]++;
      });
      var vCluster = {};
      visibleNodes.forEach(function (n) {
        vCluster[n.id] = isHub(n) ? n.id : n._primaryHubId;
      });
      var vTissue = {};
      visibleLinks.forEach(function (l) {
        var s = typeof l.source === 'object' ? l.source.id : l.source;
        var t = typeof l.target === 'object' ? l.target.id : l.target;
        var a = vCluster[s], b = vCluster[t];
        if (!a || !b || a === b) return;
        var k = a < b ? a + '|' + b : b + '|' + a;
        vTissue[k] = (vTissue[k] || 0) + 1;
      });
      var vItems = visibleHubs.map(function (it) {
        return { id: it.node.id, mass: vMass[it.node.id] || 1, demoted: !!it.node._demoted };
      });
      var vPlaced = orbitLayout(vItems, vTissue, geo);
      var vMaxMass = 1;
      vItems.forEach(function (it) { if (!it.demoted && it.mass > vMaxMass) vMaxMass = it.mass; });
      visibleHubs.forEach(function (it) {
        var p = vPlaced[it.node.id];
        if (!p) return;
        it.node._orbitTier = p.tier;
        if (p.demoted) { it.node.fx = null; it.node.fy = null; it.node.x = p.x; it.node.y = p.y; }
        else {
          it.node.fx = p.x; it.node.fy = p.y;
          it.node._hubR = hubRadiusFromMass(vMass[it.node.id] || 1, vMaxMass);
        }
      });
    } else {
      visibleHubs.forEach(function (item, i) {
        var angle = (2 * Math.PI * i / visibleHubs.length) - Math.PI / 2;
        item.node.fx = geo.cx + geo.Rx * Math.cos(angle);
        item.node.fy = geo.cy + geo.Ry * Math.sin(angle);
      });
    }

    // Hidden hubs: clear pin so they don't take screen space.
    fgNodes.filter(isHub).forEach(function (h) {
      if (!visibleSet[h.id]) { h.fx = null; h.fy = null; }
    });

    // Recompute primary hub for each visible leaf using only currently-visible hubs.
    var visHubSet = {};
    visibleHubs.forEach(function (it) { visHubSet[it.node.id] = it.node; });
    var firstVisHub = {};
    visibleLinks.forEach(function (l) {
      var s = typeof l.source === 'object' ? l.source.id : l.source;
      var t = typeof l.target === 'object' ? l.target.id : l.target;
      if (visHubSet[t] && !(s in firstVisHub)) firstVisHub[s] = visHubSet[t];
      if (visHubSet[s] && !(t in firstVisHub)) firstVisHub[t] = visHubSet[s];
    });
    visibleNodes.forEach(function (n) {
      if (isHub(n)) return;
      n._primaryHub = firstVisHub[n.id] || null;
      // Unfreeze so the leaf can re-settle into its new cluster.
      n.fx = null; n.fy = null;
    });

    Graph.graphData({ nodes: visibleNodes, links: visibleLinks });

    // Re-settle: kick alpha up to re-spread into the new clusters, then return
    // to the perpetual SIMMER floor (not 0) so motion never freezes. Leaves are
    // already unfrozen above; that drift is now the steady state.
    try {
      Graph.d3VelocityDecay(0.4);
      Graph.d3AlphaMin(0);
      Graph.d3ReheatSimulation();   // alpha→1: re-spread into the new clusters; the drift force keeps motion alive after it settles
    } catch (e) {}

    // At load the camera already frames the settled spread (no late jump);
    // filter changes re-fit once the brain has re-settled.
    if (window.__brainSeedFramed) window.__brainSeedFramed = false;
    else { window.__brainHoldOff = true; setTimeout(function () { Graph.zoomToFit(600, 50); }, 3500); }
  }

  // ---------------- legend ----------------

  // Render order — hubs first, then people/instances, structural, operational,
  // temporal. status_change collapsed under its visual group.
  var LEGEND_TYPES = [
    'company', 'project',
    'person', 'instance',
    'product', 'client',
    'goal', 'task', 'blocker', 'decision', 'pattern',
    'event', 'roadmap', 'observation',
  ];

  function renderLegend() {
    var listEl = document.getElementById('legend-list');
    if (!listEl) return;
    var present={}; rawNodes.forEach(function(n){present[n.type]=1;});
    var types=LEGEND_TYPES.filter(function(t){return present[t];});
    // Kinds the sim does not know (Grove's starter kinds, any new kind) join under their own
    // name, next to the sim type they draw as; unknown kinds last, by name.
    var rank=function(t){var i=LEGEND_TYPES.indexOf(KIND_STYLE[t]);return i<0?LEGEND_TYPES.length:i;};
    var extra=Object.keys(present).filter(function(t){return SIM_KINDS.indexOf(t)<0;});
    if(extra.length){types=types.concat(extra).sort(function(a,b){var ra=LEGEND_TYPES.indexOf(a),rb=LEGEND_TYPES.indexOf(b);ra=ra<0?rank(a)+0.5:ra;rb=rb<0?rank(b)+0.5:rb;return ra-rb||(a<b?-1:a>b?1:0);});}
    document.getElementById('legend').hidden=!types.length;
    listEl.innerHTML = types.map(function (t) {
      var color = THEME.ring[t] || THEME.ring.task;
      var disabled = hiddenTypes[t] ? ' legend-row-disabled' : '';
      var glyph;
      if (t === 'person') {
        glyph = '<svg class="legend-glyph" viewBox="0 0 14 14"><circle cx="7" cy="4.5" r="2.2" fill="' + color + '"/><path d="M2.5 13 C 3 9.5, 11 9.5, 11.5 13 Z" fill="' + color + '"/></svg>';
      } else if (t === 'instance') {
        glyph = '<svg class="legend-glyph" viewBox="0 0 14 14"><rect x="3.5" y="3.2" width="7" height="5" rx="0.8" fill="' + color + '"/><line x1="5" y1="2.2" x2="5" y2="3.2" stroke="' + color + '" stroke-width="0.9"/><line x1="9" y1="2.2" x2="9" y2="3.2" stroke="' + color + '" stroke-width="0.9"/><circle cx="5" cy="2" r="0.55" fill="' + color + '"/><circle cx="9" cy="2" r="0.55" fill="' + color + '"/><rect x="2.6" y="8.6" width="8.8" height="3.4" rx="0.7" fill="' + color + '"/><circle cx="5.5" cy="5.6" r="0.6" fill="var(--cream)"/><circle cx="8.5" cy="5.6" r="0.6" fill="var(--cream)"/></svg>';
      } else {
        glyph = '<span class="legend-swatch" style="border-color:' + color + '"></span>';
      }
      // A kind name is store text (agents can mint kinds): escaped in the attribute and the label.
      return '<div class="legend-row' + disabled + '" data-type="' + esc(t) + '" title="click to toggle visibility">' +
        glyph +
        '<span class="legend-name">' + esc(t) + '</span>' +
        '</div>';
    }).join('');
    listEl.querySelectorAll('.legend-row').forEach(function (el) {
      el.addEventListener('click', function () {
        var t = el.getAttribute('data-type');
        if (hiddenTypes[t]) delete hiddenTypes[t];
        else hiddenTypes[t] = 1;
        try { storage.setItem('grove-minimal-codex-hidden-types', JSON.stringify(hiddenTypes)); } catch (_) {}
        renderLegend();
        applyVisibility();
      });
    });
  }

  // ---------------- canvas drawing ----------------

  function drawNode(node, ctx, scale) {
    if(node.id==='grove-self'){
      var r=Math.max(14,Math.min(27,90*scale))/scale;
      ctx.globalAlpha=1;ctx.fillStyle=THEME.cream;ctx.beginPath();ctx.arc(node.x,node.y,r,0,Math.PI*2);ctx.fill();
      ctx.strokeStyle=THEME.ring.person;ctx.lineWidth=2/scale;ctx.stroke();
      ctx.globalAlpha=.8;drawPersonGlyph(ctx,node.x,node.y,r,THEME.ring.person);
      ctx.globalAlpha=1;ctx.fillStyle=THEME.ownerText;ctx.font='500 '+(10/scale)+'px "JetBrains Mono",monospace';ctx.textAlign='center';ctx.textBaseline='top';
      ctx.lineWidth=4/scale;ctx.strokeStyle=THEME.cream;ctx.strokeText(node.title,node.x,node.y+r+8/scale);ctx.fillText(node.title,node.x,node.y+r+8/scale);return;
    }
    // Orbit mode: hub disks size continuously by cluster mass (_hubR, area ∝
    // mass); demoted hubs stay small; everything else keeps its type size.
    var baseSz = node._demoted ? 18 : (node._hubR || SIZES[node.type] || 16);
    var isHL = !!highlightedIds[node.id];
    // Multi-select highlighted nodes scale up aggressively so even tier-5/6
    // nodes (event/observation/task at sz 6–10) are unmistakable at
    // fit-everything zooms.
    var sz = isHL ? Math.max(baseSz * 2, 22) : baseSz;
    if (node.__born && !prefersReducedMotion.matches) { var bornT=Math.min(1,(performance.now()-node.__born)/1000); sz*=.65+.35*(1-Math.pow(1-bornT,3)); }
    var tier = TIER[node.type] || 5;
    var isHub = HUB_TYPES[node.type];
    // Active vs inactive is conveyed by SHADE only (the toggle is OFF by default
    // so everything renders). Inactive disks stay crisp and full-size for their
    // tier, but dimmer with a desaturated ring, so the active structure reads in
    // front. Hubs and highlighted (+N) nodes are never inactive.
    var isInactive = !isHL && nodeIsInactive(node);
    var ringColor = isInactive
      ? (THEME.ringInactive[node.type] || THEME.ringInactive.task)
      : (THEME.ring[node.type] || THEME.ring.task);
    var creamColor = THEME.cream;

    // Base alpha — inactive recedes to a dimmer shade (still crisp, not a ghost).
    // Dense lever 1b: full-opacity ink — depth is carried by the desaturated
    // ring + smaller size alone, so the mass stays countable instead of foggy.
    var alpha = isInactive ? (DENSE ? 0.92 : 0.6) : 1;
    if(node.__born) alpha *= Math.min(1,(performance.now()-node.__born)/700);

    // Multi-hop expansion (when a node is focused/clicked).
    // 1-hop full, 2-hop 60%, 3-hop 30%, everything else fades to 8%.
    if (hasSelection()) {
      // Multi-select highlight from a stats-bar +N click. Highlighted nodes
      // (and any node touching a highlighted edge) get full alpha; their
      // direct neighbors stay readable at 0.4; everything else fades.
      if (highlightedIds[node.id]) alpha *= 1.0;
      else {
        var touchesSelectedNode = false;
        if (neighborsOf[node.id]) {
          neighborsOf[node.id].forEach(function (nbId) {
            if (highlightedIds[nbId]) touchesSelectedNode = true;
          });
        }
        alpha *= touchesSelectedNode ? 0.4 : 0.08;
      }
    } else if (focusedNode) {
      var d = hopDist[node.id];
      if (d === 0) alpha *= 1.0;          // focused itself
      else if (d === 1) alpha *= 1.0;     // direct neighbor
      else if (d === 2) alpha *= 0.6;
      else if (d === 3) alpha *= 0.3;
      else alpha *= 0.08;
    } else if (hoverNode && hoverNode !== node) {
      // No focus, but hovering — Obsidian-style dim non-neighbors.
      var isNeighbor = neighborsOf[hoverNode.id] && neighborsOf[hoverNode.id].has(node.id);
      if (!isNeighbor) alpha *= 0.08;
    }

    // Disk
    ctx.globalAlpha = alpha;
    ctx.fillStyle = creamColor;
    ctx.beginPath();
    ctx.arc(node.x, node.y, sz, 0, 2 * Math.PI);
    ctx.fill();

    // Ring — heavier weight for higher-tier nodes so the hierarchy reads
    // structurally, not just by size. At zoom 0.3, hubs should anchor the eye.
    // Pushed wider on T1/T2 to amplify the dominance of structural nodes.
    var ringW;
    if (tier === 1) ringW = 5.0;
    else if (tier === 2) ringW = 3.5;
    else if (tier === 3) ringW = 2.5;
    else if (tier === 4) ringW = 2.0;
    else if (tier === 5) ringW = 1.4;
    else ringW = 1.2; // tier 6 — event/observation/status_change
    ctx.lineWidth = (window.GROVE_FIXTURE ? ringW : ringW*.48) / Math.max(scale, 0.85);
    ctx.strokeStyle = ringColor;
    ctx.stroke();

    // Focus ring — single focusedNode keeps the green accent (clear "you
    // clicked this"), but multi-select highlights use the node TYPE color so
    // the palette stays informative (decision = orange glow, event =
    // blue-gray glow, etc.) instead of a wall of green.
    var isFocused = focusedNode && focusedNode.id === node.id;
    if (isFocused || isHL) {
      var hlColor = isFocused ? THEME.accent : ringColor;
      if (isHL && !isFocused) {
        ctx.shadowColor = hlColor;
        ctx.shadowBlur = 18;
      }
      ctx.beginPath();
      ctx.arc(node.x, node.y, sz + 5, 0, 2 * Math.PI);
      ctx.lineWidth = (isHL && !isFocused ? 4.5 : 2.4) / scale;
      ctx.strokeStyle = hlColor;
      ctx.globalAlpha = 1;
      ctx.stroke();
      if (isHL && !isFocused) {
        ctx.shadowBlur = 0;
        ctx.shadowColor = 'transparent';
      }
    }

    // Person glyph (silhouette inside the disk). Gate on the ON-SCREEN disk
    // radius (sz·scale), not raw zoom, so the glyph shows whenever the disk is
    // big enough to render it cleanly — including the zoomed-out demo-open view —
    // and self-adjusts as the graph (and thus fit-zoom) changes. ~7.5px ≈ a 15px
    // disk, the point below which the silhouette becomes sub-pixel noise.
    var GLYPH_MIN_PX = 7.5;
    if (node.type === 'person' && sz * scale >= GLYPH_MIN_PX) {
      ctx.globalAlpha = alpha * 0.72;
      drawPersonGlyph(ctx, node.x, node.y, sz, ringColor);
    }

    // Robot glyph — same on-screen-size gate, scaled to instance disk size.
    if (node.type === 'instance' && sz * scale >= GLYPH_MIN_PX) {
      ctx.globalAlpha = alpha * 0.78;
      drawRobotGlyph(ctx, node.x, node.y, sz, ringColor);
    }

    // Hub label (UPPERCASE inside the disk) — tier 1 always shows name when scale allows
    if (isHub && scale >= 0.4) {
      ctx.globalAlpha = alpha;
      drawHubLabel(ctx, node, sz, scale);
    }

    // Tier-2 (person/instance) — show single initial letter inside the disk on
    // hover OR when this node is the focus / its 1-hop neighbor. Adds a layer
    // of identity without cluttering the default view.
    var showInitial = (tier === 2) && scale >= 0.6 && (
      hoverNode === node ||
      (focusedNode && (focusedNode.id === node.id || hopDist[node.id] === 1))
    );
    if (showInitial && node.type !== 'instance') {
      // Instance keeps its robot glyph even on hover/focus — letter would
      // overwrite the body. Person uses early-return guard already.
      ctx.globalAlpha = alpha;
      drawInitialLetter(ctx, node, sz, ringColor);
    }

    ctx.globalAlpha = 1;
  }

  function drawInitialLetter(ctx, node, sz, color) {
    var title = (node.title || node.id || '').trim();
    if (!title) return;
    var letter = title.charAt(0).toUpperCase();
    var fontSize = sz * 0.95;
    ctx.font = '700 ' + fontSize.toFixed(1) + 'px "JetBrains Mono", ui-monospace, monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = THEME.hubText;
    // Person nodes already render a glyph — don't overwrite it.
    if (node.type === 'person') return;
    ctx.fillText(letter, node.x, node.y);
  }

  function drawPersonGlyph(ctx, cx, cy, sz, color) {
    ctx.fillStyle = color;
    var headR = sz * 0.28;
    var headY = cy - sz * 0.18;
    ctx.beginPath();
    ctx.arc(cx, headY, headR, 0, 2 * Math.PI);
    ctx.fill();
    var bodyW = sz * 0.62;
    var bodyTop = headY + headR * 0.7;
    var bodyBot = cy + sz * 0.5;
    ctx.beginPath();
    ctx.moveTo(cx - bodyW * 0.3, bodyTop);
    ctx.quadraticCurveTo(cx - bodyW * 0.55, bodyBot, cx, bodyBot + sz * 0.05);
    ctx.quadraticCurveTo(cx + bodyW * 0.55, bodyBot, cx + bodyW * 0.3, bodyTop);
    ctx.closePath();
    ctx.fill();
  }

  // Robot glyph — square head with two tiny antennas, rectangle body. Matches
  // the visual weight of drawPersonGlyph (silhouette in ring color, fits inside
  // the disk with ~0.65 of disk diameter used).
  function drawRobotGlyph(ctx, cx, cy, sz, color) {
    ctx.fillStyle = color;
    // Antennas — two short vertical strokes from the top of the head
    var antH = sz * 0.18;
    var antOff = sz * 0.18;
    var antTop = cy - sz * 0.62;
    ctx.lineWidth = sz * 0.09;
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.moveTo(cx - antOff, antTop + antH);
    ctx.lineTo(cx - antOff, antTop);
    ctx.moveTo(cx + antOff, antTop + antH);
    ctx.lineTo(cx + antOff, antTop);
    ctx.stroke();
    // Antenna tips (small dots)
    ctx.beginPath();
    ctx.arc(cx - antOff, antTop, sz * 0.06, 0, 2 * Math.PI);
    ctx.arc(cx + antOff, antTop, sz * 0.06, 0, 2 * Math.PI);
    ctx.fill();
    // Head (square with rounded corners)
    var headW = sz * 0.66;
    var headH = sz * 0.5;
    var headX = cx - headW / 2;
    var headY = cy - sz * 0.42;
    var rr = sz * 0.08;
    roundedRect(ctx, headX, headY, headW, headH, rr);
    ctx.fill();
    // Eyes — two small disks punched out using the disk cream so they read
    // against the colored head silhouette.
    ctx.fillStyle = THEME.cream;
    var eyeR = sz * 0.07;
    var eyeY = headY + headH * 0.45;
    ctx.beginPath();
    ctx.arc(cx - headW * 0.22, eyeY, eyeR, 0, 2 * Math.PI);
    ctx.arc(cx + headW * 0.22, eyeY, eyeR, 0, 2 * Math.PI);
    ctx.fill();
    // Body (rectangle below head)
    ctx.fillStyle = color;
    var bodyW = sz * 0.78;
    var bodyH = sz * 0.36;
    var bodyX = cx - bodyW / 2;
    var bodyY = headY + headH + sz * 0.04;
    roundedRect(ctx, bodyX, bodyY, bodyW, bodyH, rr * 0.7);
    ctx.fill();
  }

  function roundedRect(ctx, x, y, w, h, r) {
    if (r > w / 2) r = w / 2;
    if (r > h / 2) r = h / 2;
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function drawHubLabel(ctx, node, sz, scale) {
    // Strip parenthetical aliases like "BPOP (3ed)" -> "BPOP"
    var title = (node.title || node.id).replace(/\s*\(.*?\)\s*/g, '').trim().toUpperCase();
    var words = title.split(/\s+/);
    var lines;
    if (words.length <= 1) lines = [title];
    else if (words.length === 2) lines = words;
    else if (words.length === 3) lines = [words[0], words.slice(1).join(' ')];
    else lines = [words.slice(0, 2).join(' '), words.slice(2).join(' ')];

    // Font sized so longest line fits inside disk diameter (sz * 2 minus ring/padding)
    var maxLineLen = Math.max.apply(null, lines.map(function (l) { return l.length; }));
    var availableWidth = sz * 1.7; // disk diameter minus padding
    // Approx: char width in JBM ~ 0.55x font size at 700 weight
    var fontByWidth = availableWidth / (maxLineLen * 0.6);
    var fontByHeight = (sz * 1.5) / lines.length / 1.2;
    var fontSize = Math.max(8, Math.min(fontByWidth, fontByHeight, sz * 0.32));

    ctx.font = '700 ' + fontSize.toFixed(1) + 'px "JetBrains Mono", ui-monospace, monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = THEME.hubText;
    var lineH = fontSize * 1.18;
    var totalH = lineH * lines.length;
    var startY = node.y - totalH / 2 + lineH / 2;
    lines.forEach(function (line, i) {
      ctx.fillText(line, node.x, startY + i * lineH);
    });
  }

  function drawLinkLabel(link, ctx, scale) {
    if (scale < 0.55) return;
    var s = typeof link.source === 'object' ? link.source : fgNodeById[link.source];
    var t = typeof link.target === 'object' ? link.target : fgNodeById[link.target];
    if (!s || !t || s.x === undefined) return;

    var midX = (s.x + t.x) / 2;
    var midY = (s.y + t.y) / 2;
    var label = (link.verb || '').toUpperCase();
    if (!label) return;

    var fontSize = Math.max(7.5, 9 / Math.max(scale, 0.85));
    ctx.font = '500 ' + fontSize + 'px "JetBrains Mono", monospace';

    var dx = t.x - s.x, dy = t.y - s.y;
    var angle = Math.atan2(dy, dx);
    if (angle > Math.PI / 2) angle -= Math.PI;
    if (angle < -Math.PI / 2) angle += Math.PI;

    ctx.save();
    ctx.translate(midX, midY);
    ctx.rotate(angle);

    var w = ctx.measureText(label).width + 6;
    ctx.fillStyle = THEME.labelHalo;
    ctx.fillRect(-w / 2, -fontSize / 2 - 1, w, fontSize + 2);

    ctx.fillStyle = THEME.edgeLabel;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, 0, 0);
    ctx.restore();
  }

  function paintNodeHitArea(node, color, ctx) {
    var baseSz = node.id==='grove-self' ? Math.max(14,Math.min(27,90*Graph.zoom()))/Graph.zoom() : (SIZES[node.type] || 16);
    // Highlighted nodes draw at 2x size; the hit area must match so clicks
    // on the visible disk actually register (otherwise clicks on the scaled
    // edge fall outside the original-size hit zone and silently no-op).
    var sz = (highlightedIds[node.id] ? Math.max(baseSz * 2, 22) : baseSz) + 4;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(node.x, node.y, sz, 0, 2 * Math.PI);
    ctx.fill();
  }

  // Edges follow the same hop-distance gradient as nodes when something is
  // focused. Edge "hop level" = min(hopDist[source], hopDist[target]) when
  // both endpoints are reachable, treated as the edge's distance from focus.
  function edgeHopLevel(l) {
    var s = typeof l.source === 'object' ? l.source.id : l.source;
    var t = typeof l.target === 'object' ? l.target.id : l.target;
    var ds = hopDist[s], dt = hopDist[t];
    if (ds === undefined && dt === undefined) return Infinity;
    if (ds === undefined) return dt;
    if (dt === undefined) return ds;
    return Math.min(ds, dt);
  }

  function getLinkColor(l) {
    if (l.ended) return THEME.edgeFaded;

    if (hasSelection()) {
      // Edge mode (highlightedEdgeKeys populated): ONLY the explicit edge
      // keys read bright — otherwise old edges between the same node pairs
      // would also light up, drowning the signal.
      // Node mode (only highlightedIds): edges between two highlighted
      // nodes brighten so intra-selection structure reads clearly.
      if (highlightedEdgeKeys[edgeKey(l)]) return THEME.edgeBright;
      var ss = typeof l.source === 'object' ? l.source.id : l.source;
      var tt = typeof l.target === 'object' ? l.target.id : l.target;
      if (highlightedEdgeCount === 0 && highlightedIds[ss] && highlightedIds[tt]) {
        return THEME.edgeBright;
      }
      if (highlightedIds[ss] || highlightedIds[tt]) return THEME.edge;
      return THEME.edgeFaded;
    }

    if (focusedNode) {
      var lvl = edgeHopLevel(l);
      // 0–1 hop = bright, 2 = mid, 3 = faded, beyond = barely there
      if (lvl <= 1) return THEME.edgeBright;
      if (lvl === 2) return THEME.edge;
      if (lvl === 3) return THEME.edgeFaded;
      return THEME.edgeFaded;
    }

    if (hoverNode) {
      var hs = typeof l.source === 'object' ? l.source.id : l.source;
      var ht = typeof l.target === 'object' ? l.target.id : l.target;
      if (hs === hoverNode.id || ht === hoverNode.id) return THEME.edgeBright;
      return THEME.edgeFaded;
    }

    // Resting state (nothing focused/hovered/selected): edges touching an
    // inactive node fade so the active structure's web stays readable at full
    // density. Dense lever 2: edgeRest sits above edgeFaded so the connective
    // tissue is visible at rest (edgeRest == edgeFaded when the flag is off;
    // interaction dimming states above keep edgeFaded either way).
    if (!window.GROVE_FIXTURE) return THEME.liveEdge;
    if (l.inactive) return THEME.edgeRest;
    return THEME.edge;
  }

  function getLinkWidth(l) {
    var s = typeof l.source === 'object' ? l.source.id : l.source;
    var t = typeof l.target === 'object' ? l.target.id : l.target;

    if (focusedNode) {
      var lvl = edgeHopLevel(l);
      if (lvl <= 1) return 1.6;
      if (lvl === 2) return 1.0;
      if (lvl === 3) return 0.6;
      return 0.4;
    }

    if (hoverNode && (hoverNode.id === s || hoverNode.id === t)) return 1.4;
    return window.GROVE_FIXTURE ? (ALWAYS_LABELED_VERBS[l.verb] ? 0.9 : 0.5) : 0.55;
  }

  // ---------------- interaction ----------------

  function onNodeClick(node) {
    // Clicking a node OUTSIDE the current multi-select means the user is
    // moving on — drop the selection. Clicking INSIDE the selection (one of
    // the highlighted "new" nodes) keeps it so you can inspect a single new
    // node without losing the surrounding context of what else is new.
    if (hasSelection() && !highlightedIds[node.id]) {
      clearSelection({ silent: true });
    }
    focusedNode = node;
    rebuildHopDist(node.id);
    if(node.id!=='grove-self'){Graph.centerAt(node.x, node.y, 600);Graph.zoom(Math.max(Graph.zoom(), 1.5), 600);}
    openPanel(node);
    window.dispatchEvent(new CustomEvent('grove:node-opened',{detail:{id:node.id}}));
    scheduleRedraw();
  }

  // BFS up to 3 hops from `id` over the full undirected graph (respects
  // active-only filtering implicitly because `neighborsOf` is built from
  // rawEdges and we don't traverse hidden nodes — but since fgNodeById holds
  // every node, the BFS still computes correct distances; opacity is what
  // controls visibility). Stores result in hopDist (replaced on every call).
  function rebuildHopDist(startId) {
    hopDist = {};
    hopDist[startId] = 0;
    var frontier = [startId];
    for (var hop = 1; hop <= 3; hop++) {
      var next = [];
      for (var i = 0; i < frontier.length; i++) {
        var cur = frontier[i];
        var nbrs = neighborsOf[cur];
        if (!nbrs) continue;
        nbrs.forEach(function (nbId) {
          if (hopDist[nbId] === undefined) {
            hopDist[nbId] = hop;
            next.push(nbId);
          }
        });
      }
      frontier = next;
      if (!frontier.length) break;
    }
  }

  // rAF-throttled hover so the cursor never queues more than one redraw per frame
  var hoverPending = false;
  function onNodeHover(node) {
    hoverNode = node;
    canvasEl.style.cursor = node ? 'pointer' : '';
    scheduleRedraw();
  }

  // Repaint the canvas WITHOUT touching the physics engine. We disabled
  // autoPauseRedraw at init, so rAF naturally redraws every frame. To force a
  // paint after a state change (hover/focus opacity), nudge any cheap accessor
  // — flipping linkColor reference triggers force-graph's internal "needs
  // redraw" flag. Crucially we never call d3ReheatSimulation: that would
  // restart the layout and make all nodes drift while the user just wants to
  // see their hover dim.
  var redrawScheduled = false;
  function scheduleRedraw() {
    if (redrawScheduled) return;
    redrawScheduled = true;
    requestAnimationFrame(function () {
      redrawScheduled = false;
      // With autoPauseRedraw=false the canvas paints every frame; this rAF
      // simply ensures the state mutation is visible at the next tick.
      // No reheat, no fx/fy mutation, no physics work.
    });
  }

  function onBackgroundClick() {
    if (hasSelection()) clearSelection();
    closePanel();
  }

  // ---------------- detail panel ----------------

  // Base-table fields that live on the node row itself (not in `properties`).
  // Order matters: the sidebar shows these alongside `properties.*` after the
  // schema-required ones, so users see operational metadata in one place.
  var BASE_FIELDS = [
    'id', 'type', 'status_field_value', 'valid_at', 'invalid_at',
    'version', 'created_at', 'updated_at', 'verified_at',
    'source', 'confidence', 'file_path',
  ];

  function openPanel(node) {
    var raw = byId[node.id] || {};
    var localProps = raw.properties || {};
    var status = raw.status || 'unknown';
    var dotClass = STATUS_DOT_CLASS[status] || (node.ended ? 'dot-ended' : '');

    var epoch = viewEpoch;
    window.groveApi('/api/node/' + encodeURIComponent(node.id)).then(function (r) { return r.json(); }).then(function (extra) {
      if (epoch !== viewEpoch || request!==graphRequest) return;   // stale reads cannot change the view
      var apiNode = (extra && extra.node) || {};
      var fullSummary = apiNode.content || apiNode.summary || raw.summary || '';
      var apiProps = apiNode.properties || localProps;
      // Server now augments the response with required_props / optional_props
      // pulled from seed/node_types.json so the sidebar can flag missing slots
      // even if loadSchema() hasn't completed.
      var requiredProps = extra && extra.required_props ? extra.required_props : null;
      var optionalProps = extra && extra.optional_props ? extra.optional_props : null;
      renderPanel({
        id: node.id,
        title: raw.label || apiNode.title || node.id,
        type: raw.type || apiNode.type || node.type,
        status: apiNode.status_field_value || status,
        dotClass: dotClass,
        properties: apiProps,
        baseFields: extractBaseFields(apiNode),
        summary: fullSummary,
        relations: collectRelations(node.id),
        requiredProps: requiredProps,
        optionalProps: optionalProps,
      });
    }).catch(function () {
      if (epoch !== viewEpoch) return;
      renderPanel({
        id: node.id,
        title: raw.label || node.id,
        type: raw.type || node.type,
        status: status,
        dotClass: dotClass,
        properties: localProps,
        baseFields: extractBaseFields(raw),
        summary: raw.summary || '',
        relations: collectRelations(node.id),
        requiredProps: null,
        optionalProps: null,
      });
    });

    detailEl.classList.add('open');
    detailEl.setAttribute('aria-hidden', 'false');
  }

  function extractBaseFields(src) {
    var out = {};
    BASE_FIELDS.forEach(function (k) {
      if (src && Object.prototype.hasOwnProperty.call(src, k)) out[k] = src[k];
    });
    return out;
  }

  function closePanel() {
    detailEl.classList.remove('open');
    detailEl.setAttribute('aria-hidden', 'true');
    focusedNode = null;
    hopDist = {};
    manualCamera=false;
    if (Graph) frameGrove(500);
    scheduleRedraw();
  }

  function renderPanel(d) {
    // Order (Anas-specified Apr 26):
    //   1. Type pill + title (top)
    //   2. Status row (small dot + status text)
    //   3. Summary block (markdown; collapsible if > 600 chars)
    //   4. Properties block — combined required + optional, NO section split,
    //      sorted REQUIRED FIRST then optional. Includes base-table fields
    //      (id, valid_at, file_path, etc.) alongside properties.*. Required
    //      props that are MISSING get a red MISSING flag.
    //   5. Relations / change_log at the bottom (legacy ordering).
    var html = '';
    html += '<div class="detail-type">' + esc(d.type) + '</div>';
    html += '<div class="detail-title">' + esc(d.title) + '</div>';
    html += '<div class="detail-status"><span class="dot ' + d.dotClass + '"></span><span>' + esc(d.status) + '</span></div>';

    if (d.summary && d.summary.trim()) {
      html += renderSummaryBlock(d.summary);
    }

    html += renderUnifiedProps(d);

    if (d.relations && d.relations.length) {
      html += '<div class="detail-section"><h6>relations</h6><div class="relations-list">';
      d.relations.forEach(function (r) {
        html += '<div class="rel-row">' +
          '<span class="rel-arrow">' + (r.dir === 'out' ? '→' : '←') + '</span>' +
          '<span class="rel-verb">' + esc(r.verb) + '</span>' +
          '<a class="rel-target" data-id="' + esc(r.otherId) + '">' + esc(r.otherTitle) + '</a>' +
          '</div>';
      });
      html += '</div></div>';
    }

    detailBodyEl.innerHTML = html;

    detailBodyEl.querySelectorAll('.rel-target').forEach(function (el) {
      el.addEventListener('click', function () {
        var id = el.getAttribute('data-id');
        var n = fgNodeById[id];
        if (n) onNodeClick(n);
      });
    });

    // Wire up the summary "show more" toggle (single button per panel).
    var sm = detailBodyEl.querySelector('.summary-show-more');
    if (sm) {
      sm.addEventListener('click', function () {
        var box = detailBodyEl.querySelector('.summary-md');
        if (!box) return;
        box.classList.add('summary-md-expanded');
        box.innerHTML = box.getAttribute('data-full-html') || box.innerHTML;
        sm.remove();
      });
    }
  }

  // ---------- summary block (collapsible) ----------

  function renderSummaryBlock(summary) {
    var raw = String(summary || '').trim();
    if (!raw) return '';
    var THRESH = 600;
    // Store text: markup.js renders it so raw HTML shows as text and only safe links stay.
    var fullMd = window.GroveMarkup ? window.GroveMarkup.render(raw) : esc(raw);

    if (raw.length <= THRESH) {
      return '<div class="detail-section"><h6>summary</h6><div class="summary-md">' + fullMd + '</div></div>';
    }
    // Collapsed view: render markdown of the first 600 chars only. Try to
    // truncate at a paragraph boundary so the snippet doesn't end mid-sentence.
    var snippetEnd = THRESH;
    var lastPara = raw.lastIndexOf('\n\n', THRESH);
    if (lastPara > THRESH * 0.5) snippetEnd = lastPara;
    var snippet = raw.slice(0, snippetEnd) + (snippetEnd < raw.length ? ' …' : '');
    var snippetMd = window.GroveMarkup ? window.GroveMarkup.render(snippet) : esc(snippet);

    // Stash the full HTML on a data attr so the toggle can swap it in without
    // re-parsing. esc() the JSON-string-form so embedded quotes survive.
    var fullEsc = fullMd.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
    return '<div class="detail-section"><h6>summary</h6>' +
      '<div class="summary-md summary-md-collapsed" data-full-html="' + fullEsc + '">' +
      snippetMd +
      '</div>' +
      '<button type="button" class="summary-show-more">Show more ▾</button>' +
      '</div>';
  }

  // ---------- unified properties block ----------

  // Render REQUIRED-first, then optional, then base-table fields. No section
  // header split. Each row is one prop. Missing required props get a red dot
  // and the literal string MISSING in JBM mono after the prop name.
  function renderUnifiedProps(d) {
    var nodeType = d.type;
    var properties = d.properties || {};
    var baseFields = d.baseFields || {};

    // 1. Resolve required + optional spec. Prefer per-node response from the
    //    server (extra.required_props / extra.optional_props); fall back to
    //    the cached schemaByType from /api/schema; then to legacy flat list.
    var requiredSpec = d.requiredProps || (schemaByType[nodeType] && schemaByType[nodeType].required) || [];
    var optionalNames = d.optionalProps || (schemaByType[nodeType] && schemaByType[nodeType].optional) || [];

    var requiredNames = {};
    requiredSpec.forEach(function (r) { requiredNames[r.name] = 1; });

    var nodeEdges = collectEdgesFor(d.id);

    // 2. Build rows in order: REQUIRED (each, satisfied or MISSING) ->
    //    OPTIONAL (those present, in spec order) -> EXTRA properties not in
    //    schema (alphabetical) -> BASE fields (in BASE_FIELDS order).
    var rows = [];

    // 2a. Required
    requiredSpec.forEach(function (rspec) {
      var row = { name: rspec.name, kind: 'required', spec: rspec };
      if (rspec.type === 'edge' || rspec.type === 'list[edge]') {
        var match = matchEdgeForRequiredProp(nodeEdges, rspec);
        row.isEdge = true;
        row.satisfied = match.length > 0;
        row.matches = match;
        row.expectedVerbs = rspec.verb || '';
      } else {
        var hasVal = Object.prototype.hasOwnProperty.call(properties, rspec.name)
          && properties[rspec.name] !== null
          && properties[rspec.name] !== undefined
          && properties[rspec.name] !== '';
        row.isEdge = false;
        row.present = hasVal;
        row.value = hasVal ? properties[rspec.name] : null;
        row.satisfied = hasVal;
      }
      rows.push(row);
    });

    // 2b. Optional (in spec order, only if present)
    var seenOpt = {};
    optionalNames.forEach(function (name) {
      if (requiredNames[name]) return;
      if (Object.prototype.hasOwnProperty.call(properties, name)) {
        rows.push({ name: name, kind: 'optional', present: true, value: properties[name] });
        seenOpt[name] = 1;
      }
    });

    // 2c. Extra props present but not declared in schema. Sorted alpha so the
    //     ordering is deterministic.
    var extras = Object.keys(properties).filter(function (k) {
      return !requiredNames[k] && !seenOpt[k];
    }).sort();
    extras.forEach(function (k) {
      rows.push({ name: k, kind: 'optional', present: true, value: properties[k] });
    });

    // 2d. Base-table fields (operational metadata). Always shown last so they
    //     never crowd out the schema-significant slots. Only shown if present
    //     (id is always present, so this section never collapses to zero).
    BASE_FIELDS.forEach(function (k) {
      if (baseFields[k] === null || baseFields[k] === undefined || baseFields[k] === '') return;
      rows.push({ name: k, kind: 'base', present: true, value: baseFields[k] });
    });

    if (!rows.length) return '';

    // Render as one flat table — no section split (per Apr 26 spec).
    var html = '<div class="detail-section"><h6>properties</h6><div class="props-table">';
    rows.forEach(function (row) {
      html += renderPropRow(row);
    });
    html += '</div></div>';
    return html;
  }

  // ---------- per-row renderer ----------
  // Three row kinds: 'required' (red MISSING flag if absent), 'optional'
  // (light styling), 'base' (operational metadata). Required props ALWAYS
  // render -- if missing, a red dot precedes the prop name and the value cell
  // shows MISSING in JBM mono. The user wants to SEE the gaps.
  function renderPropRow(row) {
    var keyClass = 'prop-key';
    if (row.kind === 'required') keyClass += ' prop-key-required';
    else if (row.kind === 'base') keyClass += ' prop-key-base';
    else keyClass += ' prop-key-optional';

    // Red dot prefix on required rows whose slot is empty -- visual gap flag.
    var unsatisfied = (row.kind === 'required') && !row.satisfied;
    var prefix = unsatisfied ? '<span class="prop-missing-dot" title="required slot is empty"></span>' : '';
    var keyHtml = '<div class="' + keyClass + '">' + prefix + esc(prettifyKey(row.name)) + '</div>';
    var valHtml;

    if (row.kind === 'required' && row.isEdge) {
      if (row.satisfied) {
        if (row.matches.length === 1) {
          var m = row.matches[0];
          valHtml = '<div class="prop-val prop-edge-ok">' +
            '<a class="rel-target" data-id="' + esc(m.targetId) + '">' + esc(m.targetTitle) + '</a>' +
            ' <span class="prop-edge-via">via ' + esc(m.verb) + '</span>' +
            '</div>';
        } else {
          valHtml = '<div class="prop-val prop-edge-ok">' +
            row.matches.map(function (m) {
              return '<a class="rel-target" data-id="' + esc(m.targetId) + '">' + esc(m.targetTitle) +
                '</a> <span class="prop-edge-via">via ' + esc(m.verb) + '</span>';
            }).join('<br>') +
            '</div>';
        }
      } else {
        valHtml = '<div class="prop-val prop-missing">MISSING' +
          ' <span class="prop-edge-via">(expected verb: ' + esc(row.expectedVerbs) + ')</span>' +
          '</div>';
      }
    } else if (row.kind === 'required' && !row.isEdge && !row.present) {
      valHtml = '<div class="prop-val prop-missing">MISSING</div>';
    } else {
      valHtml = '<div class="prop-val">' + prettifyVal(row.value) + '</div>';
    }

    return keyHtml + valHtml;
  }

  // Build (verb, targetId) list of OUTGOING edges from this node, plus
  // INCOMING edges where the verb pattern could be satisfied. Required edge
  // specs in the schema are written from the node's perspective (e.g. "decided
  // by", "owned by") which always means OUTGOING from the node. We restrict
  // to outgoing to avoid false positives.
  function collectEdgesFor(nodeId) {
    var out = [];
    rawEdges.forEach(function (e) {
      if (e.source === nodeId) {
        out.push({
          verb: e.verb,
          targetId: e.target,
          targetTitle: (byId[e.target] || {}).label || e.target,
        });
      }
    });
    return out;
  }

  // rspec.verb may be a single verb ("decided by") or a pipe-alternation
  // ("owned by|assigned to"). Match any node edge whose verb appears in the
  // alternation set.
  function matchEdgeForRequiredProp(nodeEdges, rspec) {
    var verbs = (rspec.verb || '').split('|').map(function (v) { return v.trim(); }).filter(Boolean);
    if (!verbs.length) return [];
    var verbSet = {};
    verbs.forEach(function (v) { verbSet[v] = 1; });
    return nodeEdges.filter(function (e) { return verbSet[e.verb]; });
  }

  // camelCase / snake_case -> "Title Case"
  function prettifyKey(k) {
    if (!k) return '';
    var s = String(k)
      .replace(/_/g, ' ')
      .replace(/([a-z])([A-Z])/g, '$1 $2')
      .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2');
    return s.split(' ').map(function (w) {
      if (!w) return w;
      return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
    }).join(' ');
  }

  // Render a property value to HTML — booleans as check/cross, dates readable,
  // arrays as bullets, objects as key:value lines, strings/numbers plain.
  function prettifyVal(v) {
    if (v === null || v === undefined || v === '') return '<span class="prop-empty">—</span>';
    if (typeof v === 'boolean') {
      return v
        ? '<span class="prop-bool prop-true">✓</span>'
        : '<span class="prop-bool prop-false">✗</span>';
    }
    if (typeof v === 'number') return esc(String(v));
    if (Array.isArray(v)) {
      if (v.length === 0) return '<span class="prop-empty">—</span>';
      return '<ul class="prop-list">' + v.map(function (item) {
        return '<li>' + prettifyVal(item) + '</li>';
      }).join('') + '</ul>';
    }
    if (typeof v === 'object') {
      var keys = Object.keys(v);
      if (!keys.length) return '<span class="prop-empty">—</span>';
      return '<div class="prop-object">' + keys.map(function (k) {
        return '<div class="prop-object-row"><span class="prop-object-key">' + esc(prettifyKey(k)) + ':</span> ' + prettifyVal(v[k]) + '</div>';
      }).join('') + '</div>';
    }
    var s = String(v);
    // ISO date / datetime detection
    var dateMatch = s.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$/);
    if (dateMatch) {
      var dt = new Date(s);
      if (!isNaN(dt.getTime())) {
        var hasTime = !!dateMatch[4];
        var opts = hasTime
          ? { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
          : { year: 'numeric', month: 'short', day: 'numeric' };
        return '<span class="prop-date">' + esc(dt.toLocaleString('en-US', opts)) + '</span>';
      }
    }
    return esc(s);
  }

  function collectRelations(id) {
    var rels = [];
    var seen = {};  // dedup: same (dir + verb + otherId) only counts once
    rawEdges.forEach(function (e) {
      // Only show ACTIVE edges. Archived edges (invalid_at set) are historical
      // and would otherwise show as ghost duplicates.
      if (e.invalid_at) return;
      var dir = e.source === id ? 'out' : (e.target === id ? 'in' : null);
      if (!dir) return;
      var otherId = dir === 'out' ? e.target : e.source;
      var key = dir + ':' + e.verb + ':' + otherId;
      if (seen[key]) return;
      seen[key] = 1;
      rels.push({
        dir: dir, verb: e.verb, otherId: otherId,
        otherTitle: (byId[otherId] || {}).label || otherId,
      });
    });
    return rels.slice(0, 50);
  }

  // ---------------- search ----------------

  function toggleSearch() {
    var ov = document.getElementById('search-overlay');
    if (ov.hasAttribute('hidden')) {
      ov.removeAttribute('hidden');
      var inp = document.getElementById('search-input');
      inp.value = '';
      document.getElementById('search-results').innerHTML = '';
      setTimeout(function () { inp.focus(); }, 30);
      inp.oninput = doSearch;
    } else {
      ov.setAttribute('hidden', '');
    }
  }

  function doSearch(e) {
    var q = (e.target.value || '').toLowerCase().trim();
    var resEl = document.getElementById('search-results');
    if (!q) { resEl.innerHTML = ''; return; }
    var hits = rawNodes.filter(function (n) {
      return (n.id || '').toLowerCase().indexOf(q) >= 0 ||
             (n.label || '').toLowerCase().indexOf(q) >= 0;
    }).slice(0, 12);
    if (!hits.length) {
      resEl.innerHTML = '<div class="search-result"><div class="search-result-meta">no matches</div></div>';
      return;
    }
    resEl.innerHTML = hits.map(function (h) {
      return '<div class="search-result" data-id="' + esc(h.id) + '">' +
        '<div class="search-result-title">' + esc(h.label || h.id) + '</div>' +
        '<div class="search-result-meta">' + esc(h.type) + '  ·  ' + esc(h.id) + '</div>' +
        '</div>';
    }).join('');
    resEl.querySelectorAll('.search-result').forEach(function (el) {
      el.addEventListener('click', function () {
        var id = el.getAttribute('data-id');
        var n = fgNodeById[id];
        if (n) {
          toggleSearch();
          onNodeClick(n);
        }
      });
    });
  }

  // ---------------- canvas selection (clicked from stats +N deltas) ----------------
  //
  // Click +N nodes / +N edges in the topbar to highlight what's new since the
  // last daily backup directly on the graph. Other nodes/edges fade so the
  // selection stands out. Esc, background-click, or the floating "clear" pill
  // all dismiss the selection.

  function selectChangesOnCanvas(kind, provider) {
    Promise.resolve(window.GroveSimulation.getChanges(provider)).then(function(data){
      if (!data || data.error) return;
      if(kind==='removed-nodes'||kind==='removed-edges'){clearSelection({silent:true});var nodes=kind==='removed-nodes',count=nodes?data.removed_nodes:data.removed_edges;showSelectionPill(count+' '+(nodes?'node':'connection')+(count===1?'':'s')+' removed since the previous graph check. Removed details are not retained.');scheduleRedraw();return;}
      // Reset any previous selection or single-node focus.
      clearSelection({ silent: true });
      focusedNode = null;
      hopDist = {};

      var newNodes = data.new_nodes || [];
      var newEdges = data.new_edges || [];
      var label;

      if (kind === 'nodes') {
        newNodes.forEach(function (n) {
          if (fgNodeById[n.id] && !highlightedIds[n.id]) {
            highlightedIds[n.id] = 1;
            highlightedNodeCount++;
          }
        });
        label = newNodes.length + ' '+(provider?'':'new ')+'node'+(newNodes.length===1?'':'s')+(provider?' from this source':' added to your Grove');
      } else {
        newEdges.forEach(function (e) {
          var key = e.source + '||' + (e.verb || '') + '||' + e.target;
          if (!highlightedEdgeKeys[key]) {
            highlightedEdgeKeys[key] = 1;
            highlightedEdgeCount++;
          }
          // Endpoints stay visible so the edge has something to anchor to.
          if (fgNodeById[e.source] && !highlightedIds[e.source]) {
            highlightedIds[e.source] = 1;
            highlightedNodeCount++;
          }
          if (fgNodeById[e.target] && !highlightedIds[e.target]) {
            highlightedIds[e.target] = 1;
            highlightedNodeCount++;
          }
        });
        label = newEdges.length+' new connection'+(newEdges.length===1?'':'s')+' added to your Grove';
      }

      // Frame the highlighted nodes. We don't use Graph.zoomToFit() — it picks
      // an unbounded max zoom (observed 280x in testing, drowning the canvas
      // in a single disk). Compute the bbox manually, center on it, and cap
      // the zoom to a readable range.
      if (Graph && highlightedNodeCount) {
        var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, n_count = 0;
        for (var i = 0; i < fgNodes.length; i++) {
          var fn = fgNodes[i];
          if (!highlightedIds[fn.id]) continue;
          if (typeof fn.x !== 'number' || typeof fn.y !== 'number') continue;
          if (fn.x < minX) minX = fn.x; if (fn.x > maxX) maxX = fn.x;
          if (fn.y < minY) minY = fn.y; if (fn.y > maxY) maxY = fn.y;
          n_count++;
        }
        if (n_count) {
          var cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
          var w = Math.max(maxX - minX, 80);
          var h = Math.max(maxY - minY, 80);
          var vw = window.innerWidth * 0.65;
          var vh = window.innerHeight * 0.65;
          var z = Math.min(vw / w, vh / h, 2.5);
          z = Math.max(z, 0.7);
          Graph.centerAt(cx, cy, 600);
          Graph.zoom(z, 600);
        }
      }

      showSelectionPill(label);
      scheduleRedraw();
    }).catch(function (err) {
      console.error('selectChangesOnCanvas:', err);
    });
  }

  function showSelectionPill(text) {
    var pill = document.getElementById('selection-pill');
    if (!pill) return;
    pill.innerHTML = '<span>' + esc(text) + '</span>' +
      '<button type="button" class="selection-clear" aria-label="clear selection">clear · esc</button>';
    pill.removeAttribute('hidden');
    pill.querySelector('.selection-clear').addEventListener('click', function (e) {
      e.stopPropagation();
      clearSelection();
    });
  }

  function clearSelection(opts) {
    highlightedIds = {};
    highlightedEdgeKeys = {};
    highlightedNodeCount = 0;
    highlightedEdgeCount = 0;
    var pill = document.getElementById('selection-pill');
    if (pill) pill.setAttribute('hidden', '');
    if (!(opts && opts.silent)) scheduleRedraw();
  }

  // ---------------- theme ----------------

  function toggleTheme() {
    var cur = document.documentElement.getAttribute('data-theme') || 'dark';
    var next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    storage.setItem('grove-minimal-codex-theme', next);
    window.dispatchEvent(new CustomEvent('grove:theme',{detail:{theme:next}}));
    rebuildThemeCache();
    renderLegend();
    scheduleRedraw();
  }

  // ---------------- helpers ----------------

  function isEnded(n) {
    var s = n.status || '';
    return ENDED_STATUSES[s] === 1 || !!n.invalid_at;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }
})();
