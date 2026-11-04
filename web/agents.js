/* Grove workers: native nodes plus a regional maintenance heatmap.
 * Regions carry freshness; nodes retain Grove's type colors. Agent markers show
 * the current work and can be followed without changing the graph's meaning.
 * All activity and timestamps are local simulation fixtures.
 */
(function () {
  'use strict';

  // Live accounts show stored run metadata only. The display never starts work.
  if (window.GroveSimulation && window.GroveSimulation.isLive && window.GroveSimulation.isLive()) {
    var liveApi=window.GroveLive, liveState=window.GroveSimulation;
    var livePanel=document.createElement('aside'), liveToggle=document.getElementById('agents-toggle');
    var liveOwner=null, liveRequest=0, liveStatus='unavailable', liveSnapshot=null;
    var workerNames={learner:'Learner',curator:'Curator',connector:'Connector',unknown:'Unknown worker'};
    var activityText={recorded_running:'Run recorded as active',stale:'Last run needs a status update',idle:'No active run recorded',unknown:'Activity is unknown'};
    var policyText={ordinary_world_policy:'Allowed for this account',demo_learner_only:'Demo requests only',demo_autonomous_disabled:'Off in this demo'};
    var capabilityText={legacy_learner_only:'Limited import support',curator_authority_unavailable:'Not available yet',legacy_connector_only:'Limited connection support'};
    var outcomeText={running:'Recorded as active',ok:'Completed',failed:'Failed',killed:'Stopped',timeout:'Time limit reached',stopped:'Stopped',preempted:'Interrupted',budget:'Work limit reached',abandoned:'No completion recorded',unknown:'Unknown outcome'};
    livePanel.className='hood-score hood-live';livePanel.id='grove-worker-panel';
    livePanel.setAttribute('aria-label','Your Grove workers');
    document.body.appendChild(livePanel);
    liveToggle.setAttribute('aria-controls',livePanel.id);
    liveToggle.setAttribute('aria-label','Show worker activity');
    liveToggle.closest('label').title='Show recorded worker activity';
    liveToggle.checked=false;document.body.classList.remove('hood-on');

    function safeText(value){return String(value).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
    function ownerNow(){var state=liveState.getState();return state.auth&&state.onboardingComplete&&state.account||null;}
    function keys(value,names){return !!value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).sort().join(',')===names.slice().sort().join(',');}
    function member(value,choices){return typeof value==='string'&&Object.prototype.hasOwnProperty.call(choices,value);}
    function timestamp(value){return typeof value==='string'&&value.length<=40&&/^\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:\d{2})$/.test(value)&&Number.isFinite(Date.parse(value));}
    function count(value){return Number.isInteger(value)&&value>=0&&value<=2147483647;}
    function validPayload(body){
      if(!keys(body,['schema','observed_at','observation_kind','service_state','service_reason','agents','recent_runs','recent_limit','recent_has_more'])||
        body.schema!=='grove.worker-activity.v1'||body.observation_kind!=='stored_run_records'||body.service_state!=='unknown'||body.service_reason!=='no_process_heartbeat'||
        !timestamp(body.observed_at)||body.recent_limit!==20||typeof body.recent_has_more!=='boolean'||!Array.isArray(body.agents)||body.agents.length!==3||
        !Array.isArray(body.recent_runs)||body.recent_runs.length>20||JSON.stringify(body).length>32768)return false;
      var order=['learner','curator','connector'];
      return body.agents.every(function(row,i){
        return keys(row,['agent','policy','capability','activity'])&&row.agent===order[i]&&
          keys(row.policy,['state','reason'])&&member(row.policy.reason,policyText)&&
          ({ordinary_world_policy:'allowed',demo_learner_only:'limited',demo_autonomous_disabled:'disabled'})[row.policy.reason]===row.policy.state&&
          keys(row.capability,['state','reason'])&&member(row.capability.reason,capabilityText)&&
          ({legacy_learner_only:'limited',curator_authority_unavailable:'unavailable',legacy_connector_only:'limited'})[row.capability.reason]===row.capability.state&&
          keys(row.activity,['state','running_records','stale_records'])&&member(row.activity.state,activityText)&&count(row.activity.running_records)&&count(row.activity.stale_records);
      })&&body.recent_runs.every(function(row){return keys(row,['agent','outcome','started_at','finished_at'])&&member(row.agent,workerNames)&&member(row.outcome,outcomeText)&&
        (row.started_at===null||timestamp(row.started_at))&&(row.finished_at===null||timestamp(row.finished_at));});
    }
    function displayTime(value){return value===null?'Time unavailable':new Date(value).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});}
    function summaries(){
      var values={};
      ['learner','curator','connector'].forEach(function(name,i){
        var row=liveSnapshot&&liveSnapshot.agents[i];
        values[name]=row?activityText[row.activity.state]+' · '+(row.policy.state==='disabled'?policyText[row.policy.reason]:capabilityText[row.capability.reason]):liveStatus==='loading'?'Checking status…':'Activity unavailable';
      });
      return values;
    }
    function renderLive(){
      liveToggle.setAttribute('aria-expanded',String(liveToggle.checked));
      var buttonText=liveStatus==='loading'?'Refresh again':liveStatus==='error'?'Retry':'Refresh';
      var content='<div class="hood-panel-heading"><h2>Your Grove workers</h2><button type="button" class="hood-live-refresh" data-worker-refresh>'+buttonText+'</button><button type="button" class="hood-live-close" data-worker-close aria-label="Close worker activity">×</button></div>';
      content+='<p class="hood-live-intro">Stored activity. This view does not start workers.</p>';
      if(liveStatus==='loading')content+='<p role="status" class="hood-live-message">Checking worker status…</p>';
      else if(liveStatus==='error'||!liveSnapshot)content+='<p role="status" class="hood-live-message">Worker status is unavailable. Try again.</p>';
      else{
        content+='<p class="hood-live-observed" role="status">Last checked '+safeText(displayTime(liveSnapshot.observed_at))+'</p>';
        content+=liveSnapshot.agents.map(function(row){return '<section class="hood-live-worker" data-worker="'+row.agent+'"><div class="hood-live-worker-head"><span class="hood-score-dot gp-worker-'+row.agent+'" aria-hidden="true"></span><h3>'+workerNames[row.agent]+'</h3><button type="button" disabled title="Following a worker is not available">Follow</button></div><p class="hood-live-activity">'+activityText[row.activity.state]+'</p><p class="hood-live-policy">'+policyText[row.policy.reason]+'</p><p class="hood-live-capability">'+capabilityText[row.capability.reason]+'</p></section>';}).join('');
        content+='<details class="hood-live-recent"><summary>Recent activity'+(liveSnapshot.recent_has_more?' · latest 20':'')+'</summary>';
        content+=liveSnapshot.recent_runs.length?'<ol>'+liveSnapshot.recent_runs.map(function(row){return '<li><strong>'+workerNames[row.agent]+' · '+outcomeText[row.outcome]+'</strong><span>Started '+safeText(displayTime(row.started_at))+'</span><span>'+(row.finished_at===null?'No finish time recorded':'Finished '+safeText(displayTime(row.finished_at)))+'</span></li>';}).join('')+'</ol>':'<p>No recent activity recorded.</p>';
        content+='</details><p class="hood-live-note">Process status is unavailable. A stored run does not confirm that a worker is still active.</p>';
      }
      var focused=livePanel.contains(document.activeElement)&&document.activeElement.hasAttribute('data-worker-refresh');
      livePanel.innerHTML=content;
      livePanel.querySelector('[data-worker-refresh]').addEventListener('click',refreshWorkers);
      livePanel.querySelector('[data-worker-close]').addEventListener('click',function(){liveToggle.checked=false;liveToggle.dispatchEvent(new Event('change',{bubbles:true}));liveToggle.focus();});
      if(focused)livePanel.querySelector('[data-worker-refresh]').focus();
      window.dispatchEvent(new CustomEvent('grove:worker-status'));
    }
    function clearWorkers(nextOwner){
      liveRequest++;liveOwner=nextOwner||null;liveSnapshot=null;liveStatus='unavailable';
      liveToggle.checked=false;document.body.classList.remove('hood-on');renderLive();
    }
    function refreshWorkers(){
      var account=ownerNow();
      if(!account){clearWorkers(null);return Promise.resolve();}
      if(document.hidden)return Promise.resolve();
      if(liveOwner!==account){liveRequest++;liveOwner=account;liveSnapshot=null;}
      var request=++liveRequest;liveStatus='loading';liveSnapshot=null;renderLive();
      function current(){return request===liveRequest&&account===liveOwner&&account===ownerNow()&&!document.hidden;}
      return liveApi.workers().then(function(res){
        if(!current()||res.stale)return;
        if(!res.ok||!validPayload(res.body)){liveStatus='error';liveSnapshot=null;}else{liveStatus='ready';liveSnapshot=res.body;}
        renderLive();
      }).catch(function(){if(current()){liveStatus='error';liveSnapshot=null;renderLive();}});
    }
    liveToggle.addEventListener('change',function(){
      document.body.classList.toggle('hood-on',liveToggle.checked);
      liveToggle.setAttribute('aria-expanded',String(liveToggle.checked));
      if(liveToggle.checked)refreshWorkers();else{liveRequest++;if(liveStatus==='loading'){liveStatus='unavailable';renderLive();}}
    });
    window.addEventListener('grove:change',function(){var account=ownerNow();if(account!==liveOwner)clearWorkers(account);});
    ['grove:unauthenticated','grove:session-changed','grove:graph-cleared'].forEach(function(name){window.addEventListener(name,function(){clearWorkers(null);});});
    document.addEventListener('visibilitychange',function(){if(document.hidden){liveRequest++;if(liveStatus==='loading'){liveStatus='unavailable';renderLive();}}});
    window.GroveCare={refresh:refreshWorkers,summary:function(){return {updated:null,connections:null,tended:null,fresh:null,areas:null,working:null,enabled:liveToggle.checked,running:false,workers:summaries()};}};
    window.__hood={agents:function(){return {};},at:function(){return {t:0,total:0,turn:null,cycle:0,ready:false};},turns:function(){return [];},score:function(){return {};},following:function(){return null;}};
    renderLive();
    return;
  }

  var AGENTS = {
    U: { name: 'Learner', role: 'Brings in new context' },
    L: { name: 'Curator', role: 'Keeps memories useful' },
    O: { name: 'Connector', role: 'Finds links across your world' },
  };
  var ORDER = ['U', 'L', 'O'];
  var PALETTE = {
    light: { U: '#16a34a', L: '#8e24aa', O: '#d4a52a', ink: '#1a1a1a', halo: 'rgba(247, 246, 241, 0.92)', surface: '#ffffff',
      bg: '#f7f6f1', clean: '#d9d2c4', dirty: '#2e2c28', tint: 0.1, zone: 0.14 },
    dark: { U: '#22c55e', L: '#c06ad8', O: '#e0b43a', ink: '#f0f0f0', halo: 'rgba(10, 10, 11, 0.88)', surface: '#111113',
      bg: '#0a0a0b', clean: '#f2efe4', dirty: '#57524a', tint: 0.15, zone: 0.2 },
  };
  var STEPS = 32;                // ring shades between clean and a full round
  var GROUP_LINK = 150;          // things closer than this on the map (layout units) share one container
  var PAD = 22;                  // layout units between a thing's disk and its container's edge
  var CELL = 8;                  // layout units per grid cell when tracing a container's outline
  var RETRACE = 36;              // frames between re-tracing a container as the brain breathes (it is held in place)
  var ZONE_PAD = 30;             // a cleaned section's zone reaches a little past its things
  var ZONE_RETRACE = 150;        // frames between re-tracing a zone, staggered so they never all trace at once
  var TURN_IN = 600, TURN_OUT = 700, TRAVEL = 600, CLEAN_MS = 600;
  var PICK_MS = 1400;            // CCCo shows what it is about to connect before it writes
  var WRITE_MS = { add: 1300, update: 1100, link: 1250, retire: 1000, unlink: 1000 };
  var MOVE_MS = 450;             // an agent's hop to the next thing it writes
  var PULSE_MS = 700;            // the ring of a write landing
  var PLUS_MS = 950;             // a +1 rising
  var CLOSE_MS = 750;            // a section closing
  var READY_MS = 2600;           // the page has opened and settled; containers are read from the map
  var START = -Math.PI / 2;

  // Mirrors of app.js drawing constants (DENSE default), so a node is drawn exactly as the page draws it.
  var SIZES = { company: 42, project: 38, person: 24, instance: 22, product: 17, client: 16, goal: 13, decision: 13,
    blocker: 13, task: 10, event: 9, status_change: 8, observation: 9, roadmap: 13, pattern: 15 };
  var TIER = { company: 1, project: 1, person: 2, instance: 2, product: 3, client: 3, pattern: 3, goal: 4,
    decision: 4, blocker: 4, roadmap: 4, task: 5, event: 6, status_change: 6, observation: 6 };
  var RING_W = [0, 5.0, 3.5, 2.5, 2.0, 1.4, 1.2];
  var HUB = { company: 1, project: 1 };
  var LOW_SIGNAL = { event: 1, observation: 1, status_change: 1, task: 1, decision: 1 };
  var HUB_TEXT = '#1a1a1a';
  // Grove's starter kinds borrow the sim's styles here too, as in native-app.js (map in web/live.js).
  var KIND_STYLE = (window.GroveLive && window.GroveLive.KIND_STYLE) || {};
  Object.keys(KIND_STYLE).forEach(function (kind) {
    var style = KIND_STYLE[kind];
    if (SIZES[kind] == null && SIZES[style] != null) SIZES[kind] = SIZES[style];
    if (TIER[kind] == null && TIER[style] != null) TIER[kind] = TIER[style];
    if (HUB[style]) HUB[kind] = 1;
  });

  var params = new URLSearchParams(window.location.search);
  var on = false;
  try { on = localStorage.getItem('grove-minimal-codex-agents') === '1'; } catch (_) {}
  if (params.get('hood') === '1') on = true;
  if (params.get('hood') === '0') on = false;

  var D = null;                  // data.json
  var idIndex = {};              // node id -> index into D arrays
  var secOf;                     // node index -> its section (-1 if none)
  var since0 = [];               // per section: cleanup cycles since it was last cleaned, at the start (Infinity: never)
  var cleanedIn = [];            // per section: the CCCl turn of the sim that cleans it (-1 if none)
  var ROUND = 1, WEEK = 168, inSections = 1;
  var nodeTouch;                 // sim time CCCl passes each node (Infinity if it does not)
  var hiddenUntil;               // sim time a node CCU adds appears (0: always there)
  var turns = [], total = 1, nextBeyond = {}, cleanTurns = [], roundEnd = [];
  var writes = [];               // every write CCU and CCCo make in the loop: {p, k, n, at, end, src, turn}
  var marks = {};                // node index -> the writes on it
  var zones = [];                // sections that show as CCCl's zone at some point of the loop
  // The playback bar is gone, and with it every way to pause, seek or speed up the loop: no key, no
  // URL setting and no page handle changes these. ?hood=0|1 only forces the view off or on.
  var simT = 0, playing = true, speed = 1;
  var lastTick = 0, installedAt = 0, ready = false;
  var Graph = null, fgById = {}, fgNodesRef = null, nbrsOf = {};
  var col = PALETTE.light, shades = [], theme = null, cream = '#ffffff', accent = '#16a34a';
  var F = { turn: null, cycle: 0, hopRef: null, focusId: null, hoverId: null, selection: false, scale: 1, frame: 0 };
  var following=null, followLast=0, followPoint=null;
  var personas = {};             // agent -> {x, y, node} on screen, for clicks
  var mixCache = {};

  // ---------------- data ----------------

  // Local fixture adapter: preserve the current Grove renderer; never read live agent runs.
  function sampleAgentData() {
    var nodes = window.GroveView.nodes(), ids = nodes.map(function(n){return n.id;}), ix = {};
    ids.forEach(function(id,i){ix[id]=i;});
    var edges = window.GroveView.graph().graphData().links.map(function(l){return [ix[typeof l.source==='object'?l.source.id:l.source],ix[typeof l.target==='object'?l.target.id:l.target]];}).filter(function(e){return e[0]!==undefined&&e[1]!==undefined;});
    var groups = {}, sections=[];
    nodes.forEach(function(n,i){var k=n._primaryHubId||n.type;(groups[k]||(groups[k]=[])).push(i);});
    Object.keys(groups).forEach(function(k){groups[k].sort(function(a,b){return nodes[a].x-nodes[b].x||nodes[a].y-nodes[b].y;});for(var i=0;i<groups[k].length;i+=32) sections.push({n:groups[k].slice(i,i+32),since:sections.length*12});});
    var turns=[];
    for(var r=0;r<3;r++) {
      var sec=r%sections.length, core=sections[sec].n, pair=edges[r%Math.max(1,edges.length)];
      turns.push({p:'U',round:r,real:false,steps:core.slice(0,3).map(function(n){return {k:'update',n:[n],src:'your sources'};})});
      turns.push({p:'L',round:r,real:false,section:sec,steps:core.map(function(n){return {k:'update',n:[n]};})});
      turns.push({p:'O',round:r,real:false,steps:pair?[{k:'link',n:pair}]:[{k:'update',n:[core[0]]}]});
    }
    return {ids:ids,sections:{list:sections},meta:{rounds:3,cycles_per_day:24},turns:turns};
  }
  new Promise(function(resolve){
    function check(){var s=window.GroveSimulation.getState();if(s.onboardingComplete&&window.GroveView&&window.GroveView.nodes().length){resolve(sampleAgentData());}else setTimeout(check,400);}
    check();
  }).then(function(d){if(!D)loadData(d);}).catch(function (err) { console.error('agents: no data', err); });
  function refreshAgentData(){
    if(!window.GroveSimulation.getState().onboardingComplete||!window.GroveView||!window.GroveView.nodes().length)return;
    var next=sampleAgentData();
    if(!D){loadData(next);return;}   // the first graph of a session starts the loop from its start
    if(next.ids.join('|')===D.ids.join('|'))return;
    var phase=total?simT/total:0;loadData(next);simT=phase*total;
  }
  window.addEventListener('grove:graph-rendered',refreshAgentData);
  // The session ended (native-app clearGraph): nothing of it stays here. No item id, no title and
  // no count of that session is kept for the next person, on the panel or in memory.
  window.addEventListener('grove:graph-cleared',function(){
    D=null;idIndex={};secOf=null;since0=[];cleanedIn=[];nextBeyond={};zones=[];zoneKey='';
    turns=[];total=1;cleanTurns=[];roundEnd=[];writes=[];marks={};
    simT=0;ready=false;following=null;followPoint=null;personas={};noteHTML='';
    fgById={};fgNodesRef=null;nbrsOf={};
    F.turn=null;F.cycle=0;F.focusId=null;F.hoverId=null;F.hopRef=null;
    scoreVals={};chromeState='';paintScore();paintFollowing();renderNote();
  });
  var completedBefore=window.GroveSimulation.getState().onboardingComplete;
  window.addEventListener('grove:change',function(){var complete=window.GroveSimulation.getState().onboardingComplete;if(complete&&!completedBefore)refreshAgentData();completedBefore=complete;});
  function loadData(d) {
    D = d;zoneKey='';idIndex={};since0=[];cleanedIn=[];nextBeyond={};zones=[];simT=0;ready=false;installedAt=performance.now();
    D.ids.forEach(function (id, i) { idIndex[id] = i; });
    var S = D.sections;
    ROUND = Math.max(1, S.list.length);
    WEEK = Math.max(1, (D.meta.cycles_per_day || 24) * 7);
    secOf = new Int32Array(D.ids.length).fill(-1);
    inSections = 0;
    S.list.forEach(function (s, k) {
      s.n.forEach(function (i) { secOf[i] = k; });
      inSections += s.n.length;
      since0[k] = s.since === null || s.since === undefined ? Infinity : s.since;
      cleanedIn[k] = -1;
    });
    inSections = Math.max(1, inSections);
    buildSchedule();
    setTheme();
    if(barEl)barEl.remove();segEls=[];buildBar();
    if(!scoreEl)buildScore();
    buildLegend();install();
    chromeState='';updateChrome();paintFollowing();
  }

  // Turn lengths come from the data alone, so the loop and the play bar never depend on where
  // things land on the map.
  function buildSchedule() {
    var t = 0;
    turns = []; cleanTurns = []; writes = []; marks = {}; roundEnd = [];
    hiddenUntil = new Float64Array(D.ids.length);
    D.turns.forEach(function (tn) {
      var tr = { p: tn.p, round: tn.round, real: !!tn.real, at: tn.at, section: tn.section, steps: tn.steps, groups: null,
        touchAt: {}, writes: [], picks: [] };
      if (tn.round >= D.meta.rounds) { nextBeyond[tn.p] = tr; return; }
      tr.t0 = t;
      tr.w0 = t + TURN_IN;
      if (tr.p === 'L') {
        tr.core = tn.steps.map(function (s) { return s.n[0]; });
        tr.w1 = tr.w0 + Math.max(4500, 380 * tr.core.length);
        tr.cleanIndex = cleanTurns.length;
        cleanedIn[tr.section] = cleanTurns.length;
        cleanTurns.push(tr);
      } else {
        // CCCo first shows what it is about to connect; CCU just goes to what it writes
        var w = tr.w0 + (tr.p === 'O' ? PICK_MS : 0);
        if (tr.p === 'O') tn.steps.forEach(function (s) { s.n.forEach(function (i) { if (tr.picks.indexOf(i) < 0) tr.picks.push(i); }); });
        tn.steps.forEach(function (s) {
          var d = WRITE_MS[s.k] || 1100;
          var wr = { p: tr.p, k: s.k, n: s.n, t0: w, at: w + MOVE_MS, src: s.src, turn: tr, round: tn.round };
          tr.writes.push(wr);
          writes.push(wr);
          s.n.forEach(function (i) { (marks[i] = marks[i] || []).push(wr); });
          w += d;
        });
        tr.w1 = Math.max(w, tr.w0 + 1500);
      }
      tr.t1 = tr.w1 + TURN_OUT;
      t = tr.t1;
      roundEnd[tn.round] = tr.t1;
      turns.push(tr);
    });
    total = Math.max(1, t);
    // what a write holds until: the end of its round
    writes.forEach(function (wr) {
      wr.end = roundEnd[wr.round];
      if (wr.p === 'U' && wr.k === 'add') wr.n.forEach(function (i) { if (!hiddenUntil[i] || wr.at < hiddenUntil[i]) hiddenUntil[i] = wr.at; });
    });
    nodeTouch = new Float64Array(D.ids.length).fill(Infinity);
  }

  // ---------------- CCCl's containers and zones ----------------

  function ensureGroups(tr) {
    if (tr.groups) return tr.groups;
    var pts = [];
    tr.core.forEach(function (i) { var n = fg(i); if (n) pts.push({ i: i, n: n }); });
    var parent = pts.map(function (_, k) { return k; });
    function find(k) { while (parent[k] !== k) { parent[k] = parent[parent[k]]; k = parent[k]; } return k; }
    for (var a = 0; a < pts.length; a++) {
      for (var b = a + 1; b < pts.length; b++) {
        var dx = pts[a].n.x - pts[b].n.x, dy = pts[a].n.y - pts[b].n.y;
        if (dx * dx + dy * dy < GROUP_LINK * GROUP_LINK) parent[find(a)] = find(b);
      }
    }
    var byRoot = {}, raw = [];
    pts.forEach(function (p, k) {
      var r = find(k);
      if (!byRoot[r]) { byRoot[r] = { nodes: [] }; raw.push(byRoot[r]); }
      byRoot[r].nodes.push(p.i);
    });
    // visit order: the first group, then always the nearest one not visited yet
    var out = [], left = raw.slice();
    while (left.length) {
      var pick = 0;
      if (out.length) {
        var c0 = centre(out[out.length - 1].nodes), bd = Infinity;
        left.forEach(function (g, k) {
          var c = centre(g.nodes), d = (c.x - c0.x) * (c.x - c0.x) + (c.y - c0.y) * (c.y - c0.y);
          if (d < bd) { bd = d; pick = k; }
        });
      }
      out.push(left.splice(pick, 1)[0]);
    }
    out.forEach(function (g) { g.tree = spanning(g.nodes); });
    tr.groups = out;
    if (tr.w0 !== undefined && tr.p === 'L') timeGroups(tr);
    return tr.groups;
  }

  // The shortest links that join a group's things (a spanning tree on the map): the container
  // follows them, so it holds the group's things and the way between them, nothing else.
  function spanning(nodes) {
    var n = nodes.length, inTree = [true], best = [], from = [], edges = [];
    if (n < 2) return edges;
    var P = nodes.map(function (i) { return fg(i); });
    for (var k = 1; k < n; k++) { inTree[k] = false; best[k] = dist2(P[0], P[k]); from[k] = 0; }
    for (var step = 1; step < n; step++) {
      var m = -1;
      for (var j = 1; j < n; j++) if (!inTree[j] && (m < 0 || best[j] < best[m])) m = j;
      inTree[m] = true;
      edges.push([from[m], m]);
      for (var q = 1; q < n; q++) {
        if (inTree[q]) continue;
        var d = dist2(P[m], P[q]);
        if (d < best[q]) { best[q] = d; from[q] = m; }
      }
    }
    return edges;
  }
  function dist2(a, b) { return (a.x - b.x) * (a.x - b.x) + (a.y - b.y) * (a.y - b.y); }

  // Each container gets a share of the turn by its size: a short hop in, then once around it; each
  // node is passed when CCCl reaches the part of the outline nearest to it.
  function timeGroups(tr) {
    var n = 0;
    tr.groups.forEach(function (g) { n += g.nodes.length; });
    var t = tr.w0, span = tr.w1 - tr.w0;
    tr.groups.forEach(function (g, k) {
      var d = n ? span * g.nodes.length / n : span;
      g.t0 = t;
      g.o0 = t + (k ? Math.min(TRAVEL, d * 0.3) : 0);
      g.t1 = t + d;
      t += d;
      var sh = shape(g, true);
      g.nodes.forEach(function (i) {
        var nd = fg(i), f = sh ? sh.fractionNear(nd.x, nd.y) : 0;
        var at = g.o0 + f * (g.t1 - g.o0) * 0.94;
        tr.touchAt[i] = at;
        if (at < nodeTouch[i]) nodeTouch[i] = at;
      });
    });
  }

  // Once the page has laid the brain out, read CCCl's containers and zones from the map.
  function prepare() {
    nodeTouch.fill(Infinity);
    turns.forEach(function (tr) { if (tr.p === 'L') { tr.groups = null; ensureGroups(tr); } });
    if (nextBeyond.L) { nextBeyond.L.core = nextBeyond.L.steps.map(function (s) { return s.n[0]; }); nextBeyond.L.groups = null; ensureGroups(nextBeyond.L); }
    zones = [];
    D.sections.list.forEach(function (sec, k) {
      var groups = ensureGroups({ core: sec.n.slice() });
      groups.forEach(function (g, q) {
        g.pad = ZONE_PAD;
        g.every = ZONE_RETRACE;
        g.stagger = (k * 37 + q * 11) % ZONE_RETRACE;
      });
      zones.push({ k: k, groups: groups });
    });
    ready = true;
  }

  function centre(nodes) {
    var x = 0, y = 0, m = 0;
    nodes.forEach(function (i) { var n = fg(i); if (n) { x += n.x; y += n.y; m++; } });
    return m ? { x: x / m, y: y / m } : { x: 0, y: 0 };
  }

  // A container's outline where its things are now: every point within its pad of one of its
  // things or of the tree between them, traced as a contour on a grid and smoothed. Re-traced every
  // few frames as the brain breathes.
  function shape(g, force) {
    if (!force && g.sh && F.frame - g.shFrame < (g.every || RETRACE)) return g.sh;
    var P = [], R = [], pad = g.pad || PAD;
    g.nodes.forEach(function (i) {
      var n = fg(i);
      if (!n) return;
      P.push(n);
      R.push(nodeR(n) + pad);
    });
    if (!P.length) return null;
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity, rmax = 0;
    P.forEach(function (n, k) {
      x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y);
      rmax = Math.max(rmax, R[k]);
    });
    var cell = Math.max(CELL, Math.max(x1 - x0, y1 - y0) / 64);
    x0 -= rmax + 2 * cell; y0 -= rmax + 2 * cell; x1 += rmax + 2 * cell; y1 += rmax + 2 * cell;
    var nx = Math.ceil((x1 - x0) / cell) + 1, ny = Math.ceil((y1 - y0) / cell) + 1;
    var tree = (g.tree || []).filter(function (e) { return P[e[0]] && P[e[1]]; });
    var v = new Float32Array(nx * ny);
    for (var j = 0; j < ny; j++) {
      var y = y0 + j * cell;
      for (var i = 0; i < nx; i++) {
        var x = x0 + i * cell, m = Infinity;
        for (var k = 0; k < P.length; k++) {
          var d = Math.sqrt((x - P[k].x) * (x - P[k].x) + (y - P[k].y) * (y - P[k].y)) - R[k];
          if (d < m) m = d;
        }
        for (var e = 0; e < tree.length; e++) {
          var a = P[tree[e][0]], b = P[tree[e][1]], ra = R[tree[e][0]], rb = R[tree[e][1]];
          var ex = b.x - a.x, ey = b.y - a.y, L2 = ex * ex + ey * ey;
          var t = L2 ? Math.max(0, Math.min(1, ((x - a.x) * ex + (y - a.y) * ey) / L2)) : 0;
          var px = a.x + ex * t - x, py = a.y + ey * t - y;
          var dd = Math.sqrt(px * px + py * py) - Math.min(ra, rb);
          if (dd < m) m = dd;
        }
        v[j * nx + i] = m;
      }
    }
    var loops = march(v, nx, ny, x0, y0, cell).map(function (l) { return smooth(smooth(smooth(l))); });
    if (!loops.length) return null;
    var outer = loops.reduce(function (a, b) { return Math.abs(area(b)) > Math.abs(area(a)) ? b : a; });
    // slivers where two arms of the container nearly meet are tracing noise, not holes
    var floor = Math.max(pad * pad * 2, Math.abs(area(outer)) * 0.03);
    loops = loops.filter(function (l) { return l === outer || Math.abs(area(l)) >= floor; });
    // the outline runs clockwise on screen and any hole inside it the other way, so groups that
    // touch are filled as one shape, never tinted twice
    loops.forEach(function (l) { if ((l === outer) !== (area(l) > 0)) l.reverse(); });
    var top = 0;
    outer.forEach(function (p, k) { if (p.y < outer[top].y) top = k; });
    outer = outer.slice(top).concat(outer.slice(0, top));   // it starts at the top
    var cum = [0];
    for (var q = 1; q <= outer.length; q++) {
      var p0 = outer[q - 1], p1 = outer[q % outer.length];
      cum.push(cum[q - 1] + Math.sqrt(dist2(p0, p1)));
    }
    var sh = {
      loops: loops, outer: outer, cum: cum, len: cum[cum.length - 1],
      at: function (f) {
        var L = ((f % 1) + 1) % 1 * this.len, lo = 0, hi = this.outer.length;
        while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (this.cum[mid] <= L) lo = mid; else hi = mid; }
        var a = this.outer[lo], b = this.outer[(lo + 1) % this.outer.length], seg = this.cum[lo + 1] - this.cum[lo];
        var u = seg ? (L - this.cum[lo]) / seg : 0;
        return { x: a.x + (b.x - a.x) * u, y: a.y + (b.y - a.y) * u };
      },
      fractionNear: function (x, y) {
        var best = 0, bd = Infinity;
        this.outer.forEach(function (p, k) { var d = (p.x - x) * (p.x - x) + (p.y - y) * (p.y - y); if (d < bd) { bd = d; best = k; } });
        return this.len ? this.cum[best] / this.len : 0;
      },
    };
    // the outline as a ready path, so drawing it each frame costs one call, not thousands
    sh.path = new Path2D();
    loops.forEach(function (l) {
      l.forEach(function (p, k) { if (k) sh.path.lineTo(p.x, p.y); else sh.path.moveTo(p.x, p.y); });
      sh.path.closePath();
    });
    g.sh = sh;
    g.shFrame = F.frame - (g.stagger || 0);
    return sh;
  }
  // Marching squares over the grid: the closed lines where the field crosses zero.
  function march(v, nx, ny, x0, y0, cell) {
    var pts = {}, adj = {};
    function val(i, j) { return v[j * nx + i]; }
    function point(o, i, j) {
      var k = o + i + ',' + j;
      if (pts[k]) return k;
      var a, b, ax, ay, bx, by;
      if (o === 'h') { a = val(i, j); b = val(i + 1, j); ax = i; ay = j; bx = i + 1; by = j; }
      else { a = val(i, j); b = val(i, j + 1); ax = i; ay = j; bx = i; by = j + 1; }
      var t = a / (a - b);
      pts[k] = { x: x0 + (ax + (bx - ax) * t) * cell, y: y0 + (ay + (by - ay) * t) * cell };
      return k;
    }
    function link(p, q) { (adj[p] = adj[p] || []).push(q); (adj[q] = adj[q] || []).push(p); }
    for (var j = 0; j < ny - 1; j++) {
      for (var i = 0; i < nx - 1; i++) {
        var a = val(i, j) < 0, b = val(i + 1, j) < 0, c = val(i + 1, j + 1) < 0, d = val(i, j + 1) < 0;
        var idx = (a ? 8 : 0) | (b ? 4 : 0) | (c ? 2 : 0) | (d ? 1 : 0);
        if (idx === 0 || idx === 15) continue;
        var T = point('h', i, j), B = point('h', i, j + 1), Lf = point('v', i, j), Rt = point('v', i + 1, j);
        switch (idx) {
          case 1: case 14: link(Lf, B); break;
          case 2: case 13: link(B, Rt); break;
          case 3: case 12: link(Lf, Rt); break;
          case 4: case 11: link(T, Rt); break;
          case 6: case 9: link(T, B); break;
          case 7: case 8: link(Lf, T); break;
          case 5: case 10:
            var mid = (val(i, j) + val(i + 1, j) + val(i + 1, j + 1) + val(i, j + 1)) / 4 < 0;
            if ((idx === 5) === mid) { link(Lf, T); link(B, Rt); } else { link(Lf, B); link(T, Rt); }
            break;
        }
      }
    }
    var seen = {}, loops = [];
    Object.keys(adj).forEach(function (k0) {
      if (seen[k0]) return;
      var loop = [], prev = null, cur = k0;
      while (cur && !seen[cur]) {
        seen[cur] = true;
        loop.push(pts[cur]);
        var nb = adj[cur], nxt = nb[0] === prev ? nb[1] : nb[0];
        prev = cur;
        cur = nxt;
      }
      if (loop.length > 3) loops.push(loop);
    });
    return loops;
  }
  function smooth(l) {
    var out = [];
    for (var k = 0; k < l.length; k++) {
      var p = l[k], q = l[(k + 1) % l.length];
      out.push({ x: p.x * 0.75 + q.x * 0.25, y: p.y * 0.75 + q.y * 0.25 }, { x: p.x * 0.25 + q.x * 0.75, y: p.y * 0.25 + q.y * 0.75 });
    }
    return out;
  }
  function area(l) {
    var a = 0;
    for (var k = 0; k < l.length; k++) { var p = l[k], q = l[(k + 1) % l.length]; a += p.x * q.y - q.x * p.y; }
    return a / 2;
  }
  function onShape(g, f) {
    var sh = shape(g);
    var c = centre(g.nodes);
    if (!sh) return { x: c.x, y: c.y, a: 0, g: g };
    var p = sh.at(f);
    return { x: p.x, y: p.y, a: Math.atan2(p.y - c.y, p.x - c.x), g: g };
  }

  // ---------------- sim clock and where the agents are ----------------

  function tick(now) {
    var dt = lastTick ? Math.min(100, now - lastTick) : 0;
    lastTick = now;
    if (playing && D && ready && window.GroveSimulation.getState().onboardingComplete) {
      simT += dt * speed;
      if (simT >= total) simT = 0;   // loop: the brain goes back to how it is now
    }
    F.turn = null;
    for (var i = 0; i < turns.length; i++) {
      if (simT >= turns[i].t0 && simT < turns[i].t1) { F.turn = turns[i]; break; }
    }
    // cleanup cycles that have begun in the sim: one with each of CCCl's turns
    F.cycle = 0;
    cleanTurns.forEach(function (tr) { if (simT >= tr.t0) F.cycle++; });
  }

  // The next turn of an agent after a time: in the loop, or the one just beyond it.
  function nextTurnOf(p, after) {
    for (var i = 0; i < turns.length; i++) {
      if (turns[i].p === p && turns[i].t0 >= after && turns[i] !== F.turn) return turns[i];
    }
    return nextBeyond[p] || null;
  }
  function lastTurnOf(p) {
    var last = null;
    for (var i = 0; i < turns.length; i++) if (turns[i].p === p && turns[i].t1 <= simT) last = turns[i];
    return last;
  }
  function currentGroup(tr) {
    if (!tr || !tr.groups || !tr.groups.length) return null;
    for (var k = 0; k < tr.groups.length; k++) if (simT < tr.groups[k].t1) return tr.groups[k];
    return tr.groups[tr.groups.length - 1];
  }
  function between(p, q, k) { return { x: p.x + (q.x - p.x) * k, y: p.y + (q.y - p.y) * k, a: q.a, g: q.g, node: q.node }; }
  function ease(x) { x = Math.max(0, Math.min(1, x)); return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; }

  // Beside a node, up and to the right, so the agent never hides what it is writing.
  function beside(i) {
    var n = fg(i);
    if (!n) return null;
    var r = nodeR(n) + 9 / F.scale;
    return { x: n.x + r * 0.71, y: n.y - r * 0.71, a: -Math.PI / 4, node: i };
  }
  function writeSpot(wr) {
    if (wr.k === 'link' || wr.k === 'unlink') {
      var a = fg(wr.n[0]), b = fg(wr.n[1]);
      if (a && b) {
        var k = ease((simT - wr.at) / 400);
        if (simT < wr.at) return beside(wr.n[0]);
        // it rides the link it draws, from one end to the other
        var s = beside(wr.n[0]), e = beside(wr.n[1]);
        return s && e ? between(s, e, k) : s;
      }
    }
    return beside(wr.n[0]);
  }

  function spotOf(p) {
    var tr = F.turn && F.turn.p === p ? F.turn : null;
    if (p === 'L') {
      if (tr && tr.groups && tr.groups.length) {
        var gs = tr.groups, s;
        if (simT < tr.w0) s = onShape(gs[0], 0);
        else if (simT >= tr.w1) {
          var from = onShape(gs[gs.length - 1], 0);
          var nx = nextTurnOf('L', tr.t1), nxg = nx && ensureGroups(nx);
          s = nxg && nxg.length ? between(from, onShape(nxg[0], 0), ease((simT - tr.w1) / TURN_OUT)) : from;
        } else {
          for (var k = 0; k < gs.length; k++) {
            var g = gs[k];
            if (simT >= g.t1 && k < gs.length - 1) continue;
            if (simT < g.o0) { s = between(onShape(gs[k - 1], 0), onShape(g, 0), ease((simT - g.t0) / (g.o0 - g.t0))); break; }
            s = onShape(g, Math.min(1, (simT - g.o0) / ((g.t1 - g.o0) * 0.94)));
            break;
          }
        }
        if (s) { s.working = true; return s; }
      }
      var nl = nextTurnOf('L', simT), gl = nl && ensureGroups(nl);
      if (gl && gl.length) { var w = onShape(gl[0], 0); w.working = false; return w; }
      return null;
    }
    // CCU and CCCo go from one thing they write to the next
    if (tr && tr.writes.length) {
      var cur = null, prev = null;
      tr.writes.forEach(function (wr) { if (wr.t0 <= simT) { prev = cur; cur = wr; } });
      var spot;
      if (!cur) {
        // CCCo looks over what it picked; CCU waits a beat at its first thing
        spot = p === 'O' && tr.picks.length ? beside(tr.picks[Math.min(tr.picks.length - 1, Math.floor((simT - tr.w0) / 450) % tr.picks.length)]) : beside(tr.writes[0].n[0]);
      } else {
        var to = writeSpot(cur);
        var fromSpot = prev ? writeSpot(prev) : beside(cur.n[0]);
        spot = simT < cur.at && fromSpot && to ? between(fromSpot, beside(cur.n[0]), ease((simT - cur.t0) / MOVE_MS)) : to;
      }
      if (spot) { spot.working = true; return spot; }
    }
    var nt = nextTurnOf(p, simT);
    var first = nt && (p === 'O' && nt.picks && nt.picks.length ? nt.picks[0] : nt.writes && nt.writes.length ? nt.writes[0].n[0] : null);
    if (first !== null && first !== undefined) { var wt = beside(first); if (wt) { wt.working = false; return wt; } }
    var lt = lastTurnOf(p);
    if (lt && lt.writes.length) { var lw = beside(lt.writes[lt.writes.length - 1].n[0]); if (lw) { lw.working = false; return lw; } }
    return null;
  }
  function nearestThing(s) {
    if (!s) return null;
    if (s.node !== undefined && s.node !== null) return s.node;
    if (!s.g) return null;
    var best = null, bd = Infinity;
    s.g.nodes.forEach(function (i) {
      var n = fg(i);
      if (!n) return;
      var d = (n.x - s.x) * (n.x - s.x) + (n.y - s.y) * (n.y - s.y);
      if (d < bd) { bd = d; best = i; }
    });
    return best;
  }

  // ---------------- last cleaned ----------------

  // Cleanup cycles since a section was last cleaned, at this point of the sim (Infinity: never).
  function sectionSince(k) {
    var j = cleanedIn[k];
    if (j >= 0 && F.cycle > j && simT >= cleanTurns[j].t1) return F.cycle - (j + 1);
    return since0[k] + F.cycle;
  }
  function thingSince(i) {
    var k = secOf[i];
    if (k < 0) return Infinity;
    if (nodeTouch[i] <= simT) {
      var j = cleanedIn[k];
      return j >= 0 ? Math.max(0, F.cycle - (j + 1)) : 0;
    }
    return sectionSince(k);
  }
  // light for this cycle to dark for a full round or never; the square root keeps recent cycles apart
  function shadeAt(c) { return c === Infinity ? 1 : Math.min(1, Math.sqrt(c / ROUND)); }
  function shadeOf(i) {
    var c = thingSince(i), f = shadeAt(c), tt = nodeTouch[i];
    if (tt <= simT && simT - tt < CLEAN_MS) {
      var before = shadeAt(since0[secOf[i]] + F.cycle - 1);
      f = before * (1 - ease((simT - tt) / CLEAN_MS));
    }
    return shades[Math.round(f * (STEPS - 1))];
  }
  function mix(a, b, f) {
    function rgb(c) {
      if (c.charAt(0) === '#') {
        var h = c.slice(1);
        if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
        return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
      }
      var m = /rgba?\(([^)]+)\)/.exec(c);
      return m ? m[1].split(',').slice(0, 3).map(function (x) { return parseFloat(x); }) : [255, 255, 255];
    }
    var key = a + b + f;
    if (mixCache[key]) return mixCache[key];
    var x = rgb(a), y = rgb(b);
    return (mixCache[key] = 'rgb(' + x.map(function (v, i) { return Math.round(v + (y[i] - v) * f); }).join(',') + ')');
  }

  // What an agent has done to a node at this moment of the loop: CCU's green holds for the rest of
  // the round; CCCo's write flashes its color as it lands.
  function markOf(i) {
    var ms = marks[i];
    if (!ms) return null;
    var out = null;
    for (var k = 0; k < ms.length; k++) {
      var wr = ms[k];
      if (simT < wr.at || simT >= wr.end) continue;
      if (wr.p === 'U') {
        if (wr.k === 'add') return { ring: col.U, fill: mix(cream, col.U, 0.26) };
        if (wr.k === 'update' || wr.k === 'retire') out = { ring: col.U };
      } else if (simT < wr.at + PULSE_MS * 1.6 && wr.k !== 'link') {
        out = { ring: col.O };
      }
    }
    return out;
  }

  // ---------------- hooks into the page's graph ----------------

  function install() {
    var bv = window.__brainViz;
    if (!bv || !bv.Graph) return setTimeout(install, 300);
    if (bv.Graph === Graph) return;
    Graph = bv.Graph;
    fgNodesRef = null;
    installedAt = performance.now();
    ready = false;

    var drawNode = Graph.nodeCanvasObject();
    Graph.nodeCanvasObject(function (node, ctx, scale) {
      if (on && D && hidden(node)) return;
      return drawNode(node, ctx, scale);
    });
    // A node CCU has not added yet in the loop is not there to point at, and nor are its links.
    // Only the few nodes CCU adds can be hidden, so each node and link learns once whether it can.
    var hitArea = Graph.nodePointerAreaPaint();
    Graph.nodePointerAreaPaint(function (node) {
      if (on && D && canHide(node) && hidden(node)) return;
      return hitArea.apply(this, arguments);
    });
    var linkColor = Graph.linkColor();
    Graph.linkColor(function (l) {
      if (on && D && !F.selection && linkCanHide(l) && (hidden(l.source) || hidden(l.target))) return 'rgba(0,0,0,0)';
      return linkColor(l);
    });
    var labelMode = Graph.linkCanvasObjectMode();
    Graph.linkCanvasObjectMode(function (l) {
      if (on && D && !F.selection && linkCanHide(l) && (hidden(l.source) || hidden(l.target))) return null;
      return labelMode(l);
    });

    var onHover = Graph.onNodeHover();
    Graph.onNodeHover(function (node) {
      F.hoverId = null;
      if (onHover) onHover.apply(this, arguments);
    });

    // A click on an agent opens the node it is at, wherever the pointer lands on its circle.
    var onNodeClick = Graph.onNodeClick();
    Graph.onNodeClick(function (node, ev) {
      var hit = on ? agentAt(ev) : null;
      if (hit !== null) return followAgent(hit);
      return onNodeClick.apply(this, arguments);
    });
    // under an agent there is often a link, which the page does nothing with; the agent takes the click
    var onLinkClick = Graph.onLinkClick();
    Graph.onLinkClick(function (link, ev) {
      var hit = on ? agentAt(ev) : null;
      if (hit !== null) return followAgent(hit);
      if (onLinkClick) return onLinkClick.apply(this, arguments);
    });
    var onBackgroundClick = Graph.onBackgroundClick();
    Graph.onBackgroundClick(function (ev) {
      var hit = on ? agentAt(ev) : null;
      if (hit !== null) return followAgent(hit);
      if (onBackgroundClick) return onBackgroundClick.apply(this, arguments);
    });

    Graph.onRenderFramePre(function (ctx, scale) {
      F.scale = scale;
      F.frame++;
      frameState();
      tick(performance.now());
      updateChrome();
      if (on && D && ready && !F.selection) { drawZones(ctx); }
    });
    Graph.onRenderFramePost(function (ctx, scale) {
      if (on && D && ready && !F.selection) {drawOverlay(ctx, scale);followCamera();}
    });
  }
  // The page builds a new graph when the brain changes; hook the new one too.
  setInterval(function () { if (D && window.__brainViz && window.__brainViz.Graph !== Graph) install(); }, 1000);

  function hidden(node) {
    var id = typeof node === 'object' ? node.id : node;
    var i = idIndex[id];
    return i !== undefined && hiddenUntil[i] > simT;
  }
  function canHide(node) {
    if (node.__ch === undefined) { var i = idIndex[node.id]; node.__ch = i !== undefined && hiddenUntil[i] > 0; }
    return node.__ch;
  }
  function linkCanHide(l) {
    if (l.__ch === undefined) {
      var a = idIndex[typeof l.source === 'object' ? l.source.id : l.source], b = idIndex[typeof l.target === 'object' ? l.target.id : l.target];
      l.__ch = (a !== undefined && hiddenUntil[a] > 0) || (b !== undefined && hiddenUntil[b] > 0);
    }
    return l.__ch;
  }

  function frameState() {
    var gd = Graph.graphData();
    if (gd.nodes !== fgNodesRef) {
      fgNodesRef = gd.nodes;
      fgById = {};
      nbrsOf = {};
      gd.nodes.forEach(function (n) { fgById[n.id] = n; });
      gd.links.forEach(function (l) {
        var s = typeof l.source === 'object' ? l.source.id : l.source;
        var t = typeof l.target === 'object' ? l.target.id : l.target;
        (nbrsOf[s] = nbrsOf[s] || {})[t] = 1;
        (nbrsOf[t] = nbrsOf[t] || {})[s] = 1;
      });
      if (ready) prepare();   // what is on the map changed: read the containers again
    }
    if (!ready && D && performance.now() - installedAt > READY_MS) prepare();
    var hop = window.__brainViz && window.__brainViz.hopDist ? window.__brainViz.hopDist() : null;
    if (hop !== F.hopRef) {
      F.hopRef = hop;
      F.focusId = null;
      if (hop) for (var id in hop) { if (hop[id] === 0) { F.focusId = id; break; } }
    }
    var pill = document.getElementById('selection-pill');
    F.selection = !!pill && !pill.hidden;   // the page's own "+N new" highlight takes over
    setTheme();
  }

  function setTheme() {
    var th = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
    if (th === theme) return;
    theme = th;
    col = PALETTE[th];
    var cs = getComputedStyle(document.documentElement);
    cream = cs.getPropertyValue('--cream').trim() || '#ffffff';
    accent = cs.getPropertyValue('--accent').trim() || col.U;
    shades = [];
    for (var k = 0; k < STEPS; k++) shades.push(mix(col.clean, col.dirty, k / (STEPS - 1)));
    buildLegend();
  }

  // Same shade the page gives a node (inactive, focus and hover dimming).
  function baseAlpha(node) {
    var a = (!HUB[node.type] && (node.ended || LOW_SIGNAL[node.type])) ? 0.92 : 1;
    if (F.focusId) {
      var d = F.hopRef[node.id];
      a *= (d === 0 || d === 1) ? 1 : d === 2 ? 0.6 : d === 3 ? 0.3 : 0.08;
    } else if (F.hoverId && F.hoverId !== node.id) {
      if (!(nbrsOf[F.hoverId] && nbrsOf[F.hoverId][node.id])) a *= 0.08;
    }
    return a;
  }

  function fg(i) { return i === null || i === undefined ? null : fgById[D.ids[i]] || null; }
  function nodeR(n) { return n._demoted ? 18 : (n._hubR || SIZES[n.type] || 16); }

  // ---------------- drawing ----------------

  // A node with agents on: exactly the page's node (size, disk, ring weight, glyphs, hub names,
  // focus ring), in the agents' palette: its ring shaded by when its section was last cleaned, or
  // in the color of the agent that just wrote it.
  function drawThing(node, ctx, scale) {
    var i = node.__hi;
    if (i === undefined) i = node.__hi = idIndex[node.id] === undefined ? -1 : idIndex[node.id];
    if (i >= 0 && hiddenUntil[i] > simT) return;
    var sz = nodeR(node), tier = TIER[node.type] || 5;
    var alpha = baseAlpha(node);
    var mk = i >= 0 ? markOf(i) : null;
    var ring = mk ? mk.ring : (i < 0 ? shades[0] : shadeOf(i));
    ctx.globalAlpha = alpha;
    ctx.fillStyle = mk && mk.fill ? mk.fill : cream;
    ctx.beginPath();
    ctx.arc(node.x, node.y, sz, 0, 2 * Math.PI);
    ctx.fill();
    ctx.lineWidth = RING_W[tier] / Math.max(scale, 0.85);
    ctx.strokeStyle = ring;
    ctx.stroke();
    if (F.focusId === node.id) {
      ctx.beginPath();
      ctx.arc(node.x, node.y, sz + 5, 0, 2 * Math.PI);
      ctx.lineWidth = 2.4 / scale;
      ctx.strokeStyle = accent;
      ctx.globalAlpha = 1;
      ctx.stroke();
    }
    if (node.type === 'person' && sz * scale >= 7.5) {
      ctx.globalAlpha = alpha * 0.72;
      personGlyph(ctx, node.x, node.y, sz, ring);
    }
    if (node.type === 'instance' && sz * scale >= 7.5) {
      ctx.globalAlpha = alpha * 0.78;
      robotGlyph(ctx, node.x, node.y, sz, ring);
    }
    if (HUB[node.type] && scale >= 0.4) {
      ctx.globalAlpha = alpha;
      hubLabel(ctx, node, sz);
    }
    ctx.globalAlpha = 1;
  }

  // The page's own glyphs and hub names (app.js drawPersonGlyph, drawRobotGlyph, drawHubLabel).
  function personGlyph(ctx, cx, cy, sz, color) {
    ctx.fillStyle = color;
    var headR = sz * 0.28, headY = cy - sz * 0.18;
    ctx.beginPath();
    ctx.arc(cx, headY, headR, 0, 2 * Math.PI);
    ctx.fill();
    var bodyW = sz * 0.62, bodyTop = headY + headR * 0.7, bodyBot = cy + sz * 0.5;
    ctx.beginPath();
    ctx.moveTo(cx - bodyW * 0.3, bodyTop);
    ctx.quadraticCurveTo(cx - bodyW * 0.55, bodyBot, cx, bodyBot + sz * 0.05);
    ctx.quadraticCurveTo(cx + bodyW * 0.55, bodyBot, cx + bodyW * 0.3, bodyTop);
    ctx.closePath();
    ctx.fill();
  }
  function robotGlyph(ctx, cx, cy, sz, color) {
    ctx.fillStyle = color;
    var antH = sz * 0.18, antOff = sz * 0.18, antTop = cy - sz * 0.62;
    ctx.lineWidth = sz * 0.09;
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.moveTo(cx - antOff, antTop + antH); ctx.lineTo(cx - antOff, antTop);
    ctx.moveTo(cx + antOff, antTop + antH); ctx.lineTo(cx + antOff, antTop);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(cx - antOff, antTop, sz * 0.06, 0, 2 * Math.PI);
    ctx.arc(cx + antOff, antTop, sz * 0.06, 0, 2 * Math.PI);
    ctx.fill();
    var headW = sz * 0.66, headH = sz * 0.5, headX = cx - headW / 2, headY = cy - sz * 0.42, rr = sz * 0.08;
    rounded(ctx, headX, headY, headW, headH, rr);
    ctx.fill();
    ctx.fillStyle = cream;
    var eyeR = sz * 0.07, eyeY = headY + headH * 0.45;
    ctx.beginPath();
    ctx.arc(cx - headW * 0.22, eyeY, eyeR, 0, 2 * Math.PI);
    ctx.arc(cx + headW * 0.22, eyeY, eyeR, 0, 2 * Math.PI);
    ctx.fill();
    ctx.fillStyle = color;
    var bodyW = sz * 0.78, bodyH = sz * 0.36;
    rounded(ctx, cx - bodyW / 2, headY + headH + sz * 0.04, bodyW, bodyH, rr * 0.7);
    ctx.fill();
  }
  function rounded(ctx, x, y, w, h, r) {
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
  function hubLabel(ctx, node, sz) {
    var title = (node.title || node.id).replace(/\s*\(.*?\)\s*/g, '').trim().toUpperCase();
    var words = title.split(/\s+/);
    var lines = words.length <= 1 ? [title] : words.length === 2 ? words : words.length === 3
      ? [words[0], words.slice(1).join(' ')] : [words.slice(0, 2).join(' '), words.slice(2).join(' ')];
    var longest = Math.max.apply(null, lines.map(function (l) { return l.length; }));
    var size = Math.max(8, Math.min(sz * 1.7 / (longest * 0.6), sz * 1.5 / lines.length / 1.2, sz * 0.32));
    ctx.font = '700 ' + size.toFixed(1) + 'px "JetBrains Mono", ui-monospace, monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = HUB_TEXT;
    var lh = size * 1.18, y0 = node.y - lh * lines.length / 2 + lh / 2;
    lines.forEach(function (line, k) { ctx.fillText(line, node.x, y0 + k * lh); });
  }

  // Under everything: CCCl's zones. Each section it cleaned is tinted in its color, strongest the
  // cycle it was cleaned and gone after about a week of cycles; while CCCl goes through one, its
  // zone fills in as it goes.
  function zoneStrength(k) {
    var j = cleanedIn[k];
    if (j >= 0 && F.turn === cleanTurns[j]) {
      var n = 0, done = 0;
      D.sections.list[k].n.forEach(function (i) { n++; if (nodeTouch[i] <= simT) done++; });
      return n ? done / n : 0;
    }
    var c = sectionSince(k);
    return c < WEEK ? 1 - Math.sqrt(c / WEEK) : 0;   // same square-root clock as the shades
  }
  // The zones are painted once into their own layer and reused frame after frame; the layer is
  // repainted only when the view moves, a cycle passes, CCCl passes another node, or every few
  // seconds as the brain breathes.
  var zoneCanvas = document.createElement('canvas'), zoneCtx = zoneCanvas.getContext('2d'), zoneKey = '';
  function drawZones(ctx) {
    // a flat tint loses nothing at half resolution, and the layer costs a quarter to lay down
    var m = ctx.getTransform(), cv = ctx.canvas, hw = Math.ceil(cv.width / 2), hh = Math.ceil(cv.height / 2);
    if (zoneCanvas.width !== hw || zoneCanvas.height !== hh) { zoneCanvas.width = hw; zoneCanvas.height = hh; zoneKey = ''; }
    var passed = 0;
    if (F.turn && F.turn.p === 'L') F.turn.core.forEach(function (i) { if (nodeTouch[i] <= simT) passed++; });
    var key = [m.a, m.d, m.e, m.f].map(function (v) { return v.toFixed(1); }).join(',') + '|' + theme + '|' + F.cycle + '|' +
      (F.turn ? F.turn.t0 : -1) + '|' + passed + '|' + Math.floor(F.frame / ZONE_RETRACE);
    if (key !== zoneKey) {
      zoneKey = key;
      zoneCtx.setTransform(1, 0, 0, 1, 0, 0);
      zoneCtx.clearRect(0, 0, zoneCanvas.width, zoneCanvas.height);
      zoneCtx.setTransform(m.a / 2, m.b / 2, m.c / 2, m.d / 2, m.e / 2, m.f / 2);
      zones.forEach(function (z) {
        var freshness = zoneStrength(z.k), active = F.turn && F.turn.p === 'L' && F.turn.section === z.k;
        var fresh = theme === 'dark' ? '#6cb3a0' : '#6f9e8e';
        var dusty = theme === 'dark' ? '#bd9a61' : '#bca475';
        var color = active ? mix(col.L, fresh, freshness) : mix(dusty, fresh, freshness);
        z.groups.forEach(function (g) {
          var sh = shape(g); if (!sh) return;
          zoneCtx.save();
          zoneCtx.fillStyle = color;
          zoneCtx.globalAlpha = active ? .22 : .10 + (1-freshness)*.06;
          zoneCtx.fill(sh.path, 'nonzero');
          // Fine, stationary grain belongs to a region, never to an individual node.
          // It clears as that region is tended.
          if (freshness < .95) {
            var xs=sh.outer.map(function(p){return p.x;}), ys=sh.outer.map(function(p){return p.y;});
            var x0=Math.min.apply(null,xs),x1=Math.max.apply(null,xs),y0=Math.min.apply(null,ys),y1=Math.max.apply(null,ys);
            zoneCtx.clip(sh.path);zoneCtx.fillStyle=dusty;zoneCtx.globalAlpha=.24*(1-freshness);
            for(var y=Math.floor(y0/18)*18;y<y1;y+=18)for(var x=Math.floor(x0/18)*18;x<x1;x+=18){
              var jitter=Math.sin(x*.13+y*.31+z.k)*5;
              zoneCtx.beginPath();zoneCtx.arc(x+jitter,y-jitter,.8,0,Math.PI*2);zoneCtx.fill();
            }
          }
          zoneCtx.restore();
        });
      });
    }
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.drawImage(zoneCanvas, 0, 0, cv.width, cv.height);
    ctx.restore();
  }

  // Under the nodes: the section CCCl is cleaning, tinted a little more in its color.
  function drawFill(ctx) {
    var tr = F.turn;
    if (!tr || tr.p !== 'L' || !tr.groups) return;
    ctx.globalAlpha = col.tint;
    ctx.fillStyle = col.L;
    tr.groups.forEach(function (g) { var sh = shape(g); if (sh) ctx.fill(sh.path, 'nonzero'); });
    ctx.globalAlpha = 1;
  }

  function outlineOf(ctx, scale, g, color, alpha, dashed, grow) {
    var sh = shape(g);
    if (!sh) return;
    ctx.save();
    if (grow) {
      var c = centre(g.nodes);
      ctx.translate(c.x, c.y);
      ctx.scale(grow, grow);
      ctx.translate(-c.x, -c.y);
    }
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth = (dashed ? 1.1 : 1.3) / scale / (grow || 1);
    ctx.lineJoin = 'round';
    ctx.setLineDash(dashed ? [4 / scale, 4 / scale] : []);
    ctx.stroke(sh.path);
    ctx.restore();
  }

  function ringAround(ctx, scale, i, color, alpha, extra, width) {
    var n = fg(i);
    if (!n) return;
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth = width / scale;
    ctx.beginPath();
    ctx.arc(n.x, n.y, nodeR(n) + extra / scale, 0, 2 * Math.PI);
    ctx.stroke();
  }

  function plusOne(ctx, scale, x, y, color, k) {
    ctx.globalAlpha = (1 - k) * 0.95;
    ctx.font = '700 ' + (11 / scale) + 'px "JetBrains Mono", ui-monospace, monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    var yy = y - (10 + 16 * ease(k)) / scale;
    ctx.lineJoin = 'round';
    ctx.lineWidth = 3 / scale;
    ctx.strokeStyle = col.halo;
    ctx.strokeText('+1', x, yy);
    ctx.fillStyle = color;
    ctx.fillText('+1', x, yy);
  }

  function drawOverlay(ctx, scale) {
    var now = performance.now();
    var tr = F.turn;
    // CCCl: its next section dashed while it waits, the one it cleans in its color
    if (!(tr && tr.p === 'L')) {
      var nl = nextTurnOf('L', simT), gl = nl && ensureGroups(nl);
      if (gl) gl.forEach(function (g, k) { outlineOf(ctx, scale, g, col.L, k ? 0.45 : 0.75, true); });
    }
    if (tr && tr.p === 'L' && tr.groups) {
      var cur = currentGroup(tr);
      tr.groups.forEach(function (g) { outlineOf(ctx, scale, g, col.L, g === cur && simT >= tr.w0 ? 0.8 : 0.4, false); });
    }
    // a section closing: one quick ring out from its container as CCCl finishes it
    cleanTurns.forEach(function (ct) {
      var k = (simT - ct.w1) / CLOSE_MS;
      if (k < 0 || k >= 1 || !ct.groups) return;
      ct.groups.forEach(function (g) { outlineOf(ctx, scale, g, col.L, 0.7 * (1 - k), false, 1 + 0.08 * ease(k)); });
    });
    // CCCo rings the things it is about to connect, until each is written
    if (tr && tr.p === 'O' && simT >= tr.w0) {
      tr.picks.forEach(function (i) {
        var done = tr.writes.some(function (wr) { return wr.n.indexOf(i) >= 0 && wr.at <= simT; });
        if (done) return;
        ctx.setLineDash([3 / scale, 3 / scale]);
        ringAround(ctx, scale, i, col.O, 0.9, 6, 1.4);
        ctx.setLineDash([]);
      });
    }
    // the links CCU and CCCo made, drawn as they are made, held for the rest of the round
    writes.forEach(function (wr) {
      if ((wr.k !== 'link' && wr.k !== 'unlink') || simT < wr.at || simT >= wr.end) return;
      var a = fg(wr.n[0]), b = fg(wr.n[1]);
      if (!a || !b || hidden(a) || hidden(b)) return;
      var k = ease((simT - wr.at) / 400);
      ctx.globalAlpha = 0.95;
      ctx.strokeStyle = col[wr.p];
      ctx.lineWidth = 2 / scale;
      ctx.setLineDash(wr.k === 'unlink' ? [4 / scale, 4 / scale] : []);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(a.x + (b.x - a.x) * k, a.y + (b.y - a.y) * k);
      ctx.stroke();
      ctx.setLineDash([]);
    });
    // a write landing: one ring out from the node; +1 where CCU adds or CCCo links
    writes.forEach(function (wr) {
      var k = (simT - wr.at) / PULSE_MS;
      if (k < 0 || k >= 1) return;
      wr.n.forEach(function (i) { if (!(wr.k === 'link' && wr.p === 'U' && i !== wr.n[0])) ringAround(ctx, scale, i, col[wr.p], 0.8 * (1 - k), 3 + 12 * ease(k), 1.5); });
    });
    writes.forEach(function (wr) {
      var k = (simT - wr.at) / PLUS_MS;
      if (k < 0 || k >= 1) return;
      if (wr.p === 'U' && wr.k === 'add') { var n = fg(wr.n[0]); if (n) plusOne(ctx, scale, n.x, n.y - nodeR(n), col.U, k); }
      if (wr.p === 'O' && wr.k === 'link') {
        var a = fg(wr.n[0]), b = fg(wr.n[1]);
        if (a && b) plusOne(ctx, scale, (a.x + b.x) / 2, (a.y + b.y) / 2, col.O, k);
      }
    });
    ctx.globalAlpha = 1;
    // the agents: the waiting ones first, the working one on top
    var spots = {};
    ORDER.forEach(function (p) { spots[p] = spotOf(p); });
    ORDER.forEach(function (p) {   // two agents on one spot: the waiting one steps aside
      var s = spots[p];
      if (!s || s.working) return;
      ORDER.forEach(function (q) {
        var o = spots[q];
        if (q === p || !o || (!o.working && ORDER.indexOf(q) > ORDER.indexOf(p))) return;
        var dx = (s.x - o.x) * scale, dy = (s.y - o.y) * scale;
        if (dx * dx + dy * dy < 18 * 18) { s.x -= 16 / scale; s.y -= 12 / scale; }
      });
    });
    personas = {};
    ORDER.filter(function (p) { return spots[p] && !spots[p].working; })
      .concat(ORDER.filter(function (p) { return spots[p] && spots[p].working; }))
      .forEach(function (p) {
        var s = spots[p];
        drawAgent(ctx, scale, p, s, now);
        var sc = Graph.graph2ScreenCoords(s.x, s.y);
        personas[p] = { x: sc.x, y: sc.y, node: nearestThing(s) };
      });
  }

  function drawAgent(ctx, scale, p, s, now) {
    var c = col[p], r = (s.working ? 7 : 5.5) / scale;
    if (s.working) {
      var ph = (now % 1600) / 1600;
      ctx.globalAlpha = 0.5 * (1 - ph);
      ctx.strokeStyle = c;
      ctx.lineWidth = 1.5 / scale;
      ctx.beginPath();
      ctx.arc(s.x, s.y, r + (12 * ph) / scale, 0, 2 * Math.PI);
      ctx.stroke();
    }
    ctx.globalAlpha = s.working ? 1 : 0.8;
    ctx.fillStyle = c;
    ctx.beginPath();
    ctx.arc(s.x, s.y, r, 0, 2 * Math.PI);
    ctx.fill();
    ctx.lineWidth = 2 / scale;
    ctx.strokeStyle = col.surface;
    ctx.stroke();
    var left = Math.cos(s.a || 0) < -0.2;
    ctx.font = '600 ' + (11 / scale) + 'px "JetBrains Mono", ui-monospace, monospace';
    var tx = left ? s.x - r - 5 / scale : s.x + r + 5 / scale;
    ctx.textAlign = left ? 'right' : 'left';
    ctx.textBaseline = 'middle';
    ctx.lineJoin = 'round';
    ctx.lineWidth = 3 / scale;
    ctx.strokeStyle = col.halo;
    ctx.strokeText(AGENTS[p].name, tx, s.y);
    ctx.fillStyle = c;
    ctx.fillText(AGENTS[p].name, tx, s.y);
    ctx.globalAlpha = 1;
  }

  function agentAt(ev) {
    if (!ev || !D) return null;
    var x = ev.clientX, y = ev.clientY;   // the page's canvas fills the window from its top left
    var best = null, bestD = 13 * 13;
    ORDER.forEach(function (p) {
      var s = personas[p];
      if (!s || s.node === null || s.node === undefined) return;
      var d = (s.x - x) * (s.x - x) + (s.y - y) * (s.y - y);
      if (d < bestD) { bestD = d; best = p; }
    });
    return best;
  }

  // ---------------- chrome: switch, play bar, legend, score, panel line ----------------

  var barEl = null, segEls = [], legendEl = null, speedEl = null, playEl = null, scoreEl = null, scoreVals = {};

  (function buildSwitch() {
    var box=document.getElementById('agents-toggle');
    box.checked=on;
    box.addEventListener('change',function(){setOn(box.checked);});
    document.body.classList.toggle('hood-on',on);
    window.addEventListener('grove:change',function(){if(!window.GroveSimulation.getState().onboardingComplete){box.checked=false;setOn(false);}});
  })();

  function setOn(v) {
    if(v===on&&!v)return;
    on = v;
    if(!v)stopFollowing(true);
    try { localStorage.setItem('grove-minimal-codex-agents', on ? '1' : '0'); } catch (_) {}
    document.body.classList.toggle('hood-on', on);
    renderNote();
  }

  // The simulation's bottom playback bar (play, pause, speed, seek) is not part of the product:
  // nothing is built, barEl stays null, and updateChrome paints only the score.
  function buildBar() {}

  // A small score per agent: what it has done so far in the loop, ticking as it changes.
  var SCORE = [
    { p: 'U', parts: [['updated', 'memories updated']] },
    { p: 'L', parts: [['sections', 'areas tended']] },
    { p: 'O', parts: [['links', 'connections found']] },
  ];
  function buildScore() {
    scoreEl=document.createElement('aside');scoreEl.className='hood-score';scoreEl.setAttribute('aria-label','Your Grove workers');
    scoreEl.innerHTML='<div class="hood-panel-heading"><span>Your Grove workers</span><button class="hood-stop" aria-label="Stop following" hidden>Overview ↗</button></div>'+SCORE.map(function(r){return '<button class="hood-score-row" data-a="'+r.p+'" aria-pressed="false"><span class="hood-score-dot"></span><span class="hood-score-description"><strong>'+AGENTS[r.p].name+' <span class="gp-info" tabindex="0" role="note" aria-label="'+AGENTS[r.p].role+'">i<span class="gp-tooltip" role="tooltip">'+AGENTS[r.p].role+'</span></span></strong><span class="hood-score-counts">'+r.parts.map(function(pt){return '<span class="hood-score-v" data-k="'+r.p+pt[0]+'">0</span> '+pt[1];}).join(' · ')+'</span></span><span class="hood-follow-label">Follow</span></button>';}).join('')+'<div class="hood-legend-slot"></div>';
    document.body.appendChild(scoreEl);
    scoreEl.querySelectorAll('[data-a]').forEach(function(el){el.onclick=function(){var p=el.dataset.a;if(following===p)stopFollowing(true);else followAgent(p);};});
    scoreEl.querySelectorAll('.gp-info').forEach(function(el){el.onclick=function(e){e.stopPropagation();};});
    scoreEl.querySelector('.hood-stop').onclick=function(){stopFollowing(true);};
    if(legendEl)scoreEl.querySelector('.hood-legend-slot').appendChild(legendEl);
  }
  function followAgent(p){
    if(!on||!Graph)return;following=p;followPoint=null;followLast=0;
    if(window.GroveView)window.GroveView.close();
    Graph.zoom(Math.min(1.05,Math.max(.68,Graph.zoom())),850);paintFollowing();
  }
  function stopFollowing(overview){following=null;followPoint=null;paintFollowing();if(overview&&window.GroveView)window.GroveView.fit(800);}
  function paintFollowing(){if(!scoreEl)return;scoreEl.querySelector('.hood-stop').hidden=!following;scoreEl.querySelectorAll('[data-a]').forEach(function(el){var yes=el.dataset.a===following;el.classList.toggle('is-following',yes);el.setAttribute('aria-pressed',String(yes));el.setAttribute('aria-label',(yes?'Stop following ':'Follow ')+AGENTS[el.dataset.a].name);el.querySelector('.hood-follow-label').textContent=yes?'Following':'Follow';});}
  function followCamera(){
    if(!following||!on||!ready||document.querySelector('.gp-dialog')||document.querySelector('.gp-tour-card'))return;
    var now=performance.now(),dt=Math.min(40,now-(followLast||now));followLast=now;var target=spotOf(following);if(!target)return;
    if(!followPoint){var c=Graph.centerAt();followPoint={x:c.x-target.x,y:c.y-target.y};}
    var decay=Math.exp(-dt/120);followPoint.x*=decay;followPoint.y*=decay;
    if(Math.abs(followPoint.x)+Math.abs(followPoint.y)<.05)followPoint={x:0,y:0};
    Graph.centerAt(target.x+followPoint.x,target.y+followPoint.y,0);
  }
  document.getElementById('canvas').addEventListener('pointerdown',function(){if(following)stopFollowing(false);});
  document.getElementById('canvas').addEventListener('wheel',function(){if(following)stopFollowing(false);},{passive:true});
  function scoreNow() {
    var v = { Uadded: 0, Uupdated: 0, Ulinks: 0, Olinks: 0, Lsections: 0 };
    writes.forEach(function (wr) {
      if (wr.at > simT) return;
      if (wr.p === 'U' && wr.k === 'add') v.Uadded++;
      if (wr.p === 'U' && wr.k === 'update') v.Uupdated++;
      if (wr.p === 'U' && wr.k === 'link') v.Ulinks++;
      if (wr.p === 'O' && wr.k === 'link') v.Olinks++;
    });
    cleanTurns.forEach(function (ct) { if (ct.w1 <= simT) v.Lsections++; });
    var clean = 0;
    D.sections.list.forEach(function (s, k) { if (sectionSince(k) < WEEK) clean += s.n.length; });
    v.Lclean = (Math.round(clean / inSections * 1000) / 10).toFixed(1) + '%';   // a section is a fraction of a percent
    return v;
  }
  function paintScore() {
    if (!scoreEl) return;
    var v = D ? scoreNow() : { Uadded: 0, Uupdated: 0, Ulinks: 0, Olinks: 0, Lsections: 0, Lclean: '0.0%' };
    var working = F.turn ? F.turn.p : null;
    Object.keys(v).forEach(function (k) {
      var el = scoreEl.querySelector('[data-k="' + k + '"]');
      if (!el) return;
      var s = String(v[k]);
      if (scoreVals[k] !== undefined && scoreVals[k] !== s) {
        el.classList.remove('tick');
        void el.offsetWidth;   // restart the tick
        el.classList.add('tick');
      }
      scoreVals[k] = s;
      el.textContent = s;
    });
    var rows = scoreEl.querySelectorAll('.hood-score-row');
    for (var i = 0; i < rows.length; i++) {
      var p = rows[i].getAttribute('data-a');
      rows[i].classList.toggle('working', p === working);
      rows[i].querySelector('.hood-score-dot').style.background = col[p];
    }
  }

  var PLAY = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M8 5.5v13l10.5-6.5z"/></svg>';
  var PAUSE = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><rect x="7" y="5.5" width="3.4" height="13" rx="0.8"/><rect x="13.6" y="5.5" width="3.4" height="13" rx="0.8"/></svg>';
  var chromeState = '';
  function updateChrome() {
    if (!barEl && !scoreEl) return;   // the bar is gone; the score panel still updates
    var key = theme + '|' + playing + '|' + speed + '|' + Math.round(simT / 120);
    if (key === chromeState) return;
    chromeState = key;
    if (barEl) {
      playEl.innerHTML = playing ? PAUSE : PLAY;
      playEl.setAttribute('aria-label', playing ? 'pause' : 'play');
      speedEl.textContent = speed + '×';
      turns.forEach(function (tn, j) {
        var f = simT <= tn.t0 ? 0 : simT >= tn.t1 ? 1 : (simT - tn.t0) / (tn.t1 - tn.t0);
        var seg = segEls[j];
        seg.firstChild.style.background = col[tn.p];
        seg.lastChild.style.background = col[tn.p];
        seg.lastChild.style.width = (f * 100) + '%';
      });
    }
    paintScore();
    window.dispatchEvent(new CustomEvent('grove:care'));
    renderNote();
  }

  // Last cleaned, in order: a ring light this cycle, on CCCl's zone, to dark for a full round or never.
  function buildLegend() {
    if (!D) return;
    if (!legendEl) {
      legendEl = document.createElement('aside');
      legendEl.className = 'hood-legend';
      (scoreEl?scoreEl.querySelector('.hood-legend-slot'):document.body).appendChild(legendEl);
    }
    legendEl.innerHTML = '<div class="hood-legend-h">GROVE FRESHNESS <span class="gp-info" tabindex="0" role="note" aria-label="Care starts with dusty areas">i<span class="gp-tooltip" role="tooltip">Dust marks context due for a refresh. Your Grove workers revisit these areas automatically.</span></span></div><div class="hood-freshness-scale" aria-hidden="true"></div><div class="hood-freshness-labels"><span>Fresh</span><span>Refreshing soon</span></div>';

  }

  // One line on top of the node panel: which agent is on this node, and why, in a few words.
  function cleanedWords(c) {
    if (c === Infinity) return 'Not cleaned yet';
    if (c <= 0) return 'Cleaned this cycle';
    return 'Cleaned ' + c + (c === 1 ? ' cycle' : ' cycles') + ' ago';
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  // An item title as HTML text for the panel line: titles are store text, never markup.
  function label(i) {
    var n = fg(i), t = String((n && n.title) || D.ids[i]);
    return esc(t.length > 42 ? t.slice(0, 41).replace(/[\s,;:.-]+$/, '') + '…' : t);
  }
  var DONE = { add: 'added this', update: 'updated this', retire: 'retired this', link: 'linked this', unlink: 'unlinked this' };
  var COMING = { add: 'is adding this', update: 'is about to update this', retire: 'is about to retire this', link: 'is about to link this', unlink: 'is about to unlink this' };
  function noteFor(id) {
    var i = idIndex[id];
    if (i === undefined) return null;
    var tr = F.turn, k = secOf[i];
    if (tr && tr.p === 'L' && k >= 0 && k === tr.section) {
      if (simT < tr.w1) {
        var before = since0[k] + F.cycle - 1;
        return { p: 'L', head: 'is cleaning this section', sub: before === Infinity ? 'Its first clean' : 'Last cleaned ' + before + (before === 1 ? ' cycle' : ' cycles') + ' ago' };
      }
      return { p: null, head: cleanedWords(0) };
    }
    // what CCU or CCCo did, or is about to do, to this node in this round
    var ms = (marks[i] || []).filter(function (wr) { return wr.turn === tr || (wr.at <= simT && simT < wr.end); });
    if (ms.length) {
      // the latest write that has landed on it, or else the next one coming
      var landed = ms.filter(function (w) { return w.at <= simT; });
      var wr = landed.length ? landed[landed.length - 1] : ms[0];
      var other = wr.n.length > 1 ? (wr.n[0] === i ? wr.n[1] : wr.n[0]) : null;
      var done = wr.at <= simT, link = wr.k === 'link' || wr.k === 'unlink';
      var head = done ? DONE[wr.k] + (link && other !== null ? ' to ' + label(other) : '') : (wr.p === 'O' ? 'is about to connect this' : COMING[wr.k]);
      var sub = wr.p === 'U' ? (wr.src ? 'From ' + esc(wr.src) : 'From what came in')
        : !done ? (other !== null ? 'With ' + label(other) : 'While connecting the brain')
        : link ? 'A new link this round' : 'Updated while connecting the brain';
      return { p: wr.p, head: head, sub: sub };
    }
    var nl = nextTurnOf('L', simT);
    if (k >= 0 && nl && nl.section === k && !(tr && tr.p === 'L')) {
      return { p: 'L', head: 'cleans this section next', sub: cleanedWords(thingSince(i)) };
    }
    return { p: null, head: cleanedWords(thingSince(i)) };
  }
  var noteHTML = '';
  function renderNote() {
    var body = document.getElementById('detail-body');
    var panel = document.getElementById('detail');
    if (!body || !panel) return;
    var old = body.querySelector('.hood-note');
    if (!on || !D || !ready || !panel.classList.contains('open') || !F.focusId) {
      if (old) old.remove();
      noteHTML = '';
      return;
    }
    var n = noteFor(F.focusId);
    if (!n) { if (old) old.remove(); return; }
    var html = n.p
      ? '<div class="hood-note-head"><span class="hood-note-dot" style="background:' + col[n.p] + '"></span>' +
        '<span class="hood-note-name" style="color:' + col[n.p] + '">' + AGENTS[n.p].name + '</span> ' + n.head + '</div>' +
        '<div class="hood-note-sub">' + n.sub + '</div>'
      : '<div class="hood-note-sub">' + n.head + '</div>';
    if (old && html === noteHTML) return;
    noteHTML = html;
    if (!old) {
      old = document.createElement('div');
      old.className = 'detail-section hood-note';
      body.appendChild(old);
    }
    old.innerHTML = '<h6>Last refreshed</h6>'+html;
  }
  // The panel re-renders on every click; put the line back each time.
  (function watchPanel() {
    var body = document.getElementById('detail-body');
    if (!body) return setTimeout(watchPanel, 200);
    new MutationObserver(function () {
      if (!body.querySelector('.hood-note')) { noteHTML = ''; renderNote(); }
    }).observe(body, { childList: true });
  })();

  // The pointer becomes a hand over an agent.
  document.addEventListener('mousemove', function (e) {
    if (!on || !D) return;
    var hit = ORDER.some(function (p) {
      var s = personas[p];
      return s && (s.x - e.clientX) * (s.x - e.clientX) + (s.y - e.clientY) * (s.y - e.clientY) < 13 * 13;
    });
    var cv = document.querySelector('#canvas canvas');
    if (cv && hit) cv.style.cursor = 'pointer';
  });

  window.GroveCare={summary:function(){
    var v=D&&inSections?scoreNow():{Uupdated:0,Ulinks:0,Olinks:0,Lsections:0,Lclean:'0%'};
    return {updated:v.Uupdated,connections:v.Ulinks+v.Olinks,tended:v.Lsections,fresh:parseFloat(v.Lclean)||0,areas:D?D.sections.list.length:0,working:F.turn&&F.turn.p,enabled:on,running:!!(D&&ready&&playing&&window.GroveSimulation.getState().onboardingComplete)};
  }};

  // Debug handle, like the page's own __brainViz (for verification scripting only). It only reads.
  window.__hood = {
    agents: function () { return personas; },
    at: function () { return { t: simT, total: total, turn: F.turn && F.turn.p, cycle: F.cycle, ready: ready, focus: F.focusId }; },
    turns: function () { return turns.map(function (t) { return { p: t.p, t0: t.t0, w0: t.w0, w1: t.w1, t1: t.t1, section: t.section, writes: t.writes.map(function (w) { return { k: w.k, at: w.at, n: w.n }; }) }; }); },
    score: function () { return scoreVals; },
    following: function(){return following;},
  };

  // Escape stops following an agent. The playback keys left with the playback bar.
  document.addEventListener('keydown', function (e) {
    if (!on || !D || document.querySelector('.gp-dialog') || /input|textarea/i.test((e.target && e.target.tagName) || '')) return;
    if (e.key === 'Escape' && following) stopFollowing(true);
  });
})();
