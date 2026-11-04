/* Grove map positions: keep every placed item where it is, place only the new ones.
 *
 * placeNew(stored, nodes, edges, seed, radius) is pure and deterministic:
 *   stored  {id: {x, y}}  positions the store (or this session) already holds
 *   nodes   [{id, _primaryHubId?, ...}] the current graph, in a stable order; _primaryHubId is
 *           the hub the node's seed sits around (native-app passes seedLayout's group hub)
 *   edges   [{source, target}]
 *   seed    {id: {x, y}}  the sim's own layout of the current graph (native-app seedLayout)
 *   radius  function (node) -> the room a node takes, as the collide force sees it and at least
 *           the largest disc it can draw, since a placed node never moves (or a number)
 * It returns {positions: {id: {x, y}}, placed: [ids it placed]}.
 *
 * - Every stored position is kept exactly.
 * - With nothing stored, the result is the seed itself: the sim's own layout.
 * - A new node goes to its seed position re-anchored on its primary hub's position; with no
 *   placed hub, at the centre of its placed neighbours; with neither, at its seed position
 *   moved by the mean offset between the seed frame and the stored frame. From there it takes
 *   the nearest spot where its disc overlaps no placed disc (radius + radius apart).
 * Hubs and nodes without a hub are placed before the leaves that anchor on them.
 *
 * Used by native-app.js in live mode (window.GroveLayoutKeep) and by web/checks (require).
 */
(function (root) {
  'use strict';

  function has(map, id) { return Object.prototype.hasOwnProperty.call(map, id) && !!map[id]; }
  function finite(p) { return !!p && typeof p.x === 'number' && typeof p.y === 'number' && isFinite(p.x) && isFinite(p.y); }

  // A uniform grid of placed discs, so a spot check reads only the nearby cells.
  function Discs(cell) {
    this.cell = cell;
    this.cells = {};
  }
  Discs.prototype.key = function (cx, cy) { return cx + ',' + cy; };
  Discs.prototype.add = function (x, y, r) {
    var k = this.key(Math.floor(x / this.cell), Math.floor(y / this.cell));
    (this.cells[k] || (this.cells[k] = [])).push({ x: x, y: y, r: r });
  };
  Discs.prototype.free = function (x, y, r) {
    var cx = Math.floor(x / this.cell), cy = Math.floor(y / this.cell);
    for (var i = cx - 1; i <= cx + 1; i++) {
      for (var j = cy - 1; j <= cy + 1; j++) {
        var list = this.cells[this.key(i, j)];
        if (!list) continue;
        for (var n = 0; n < list.length; n++) {
          var d = list[n];
          if (Math.hypot(d.x - x, d.y - y) < d.r + r) return false;
        }
      }
    }
    return true;
  };

  // The nearest free spot: the target itself, else the first free point on rings of growing
  // radius around it (each ring a little wider than the last, points evenly spaced on it).
  function freeSpot(discs, target, r) {
    if (discs.free(target.x, target.y, r)) return { x: target.x, y: target.y };
    var step = Math.max(1, r / 2);
    for (var ring = 1; ring <= 20000; ring++) {
      var R = ring * step, count = Math.max(8, Math.ceil(2 * Math.PI * R / step));
      for (var i = 0; i < count; i++) {
        var a = 2 * Math.PI * i / count, x = target.x + R * Math.cos(a), y = target.y + R * Math.sin(a);
        if (discs.free(x, y, r)) return { x: x, y: y };
      }
    }
    return { x: target.x, y: target.y };
  }

  function placeNew(stored, nodes, edges, seed, radius) {
    stored = stored || {};
    seed = seed || {};
    nodes = nodes || [];
    edges = edges || [];
    var roomOf = typeof radius === 'function' ? radius : function () { return Number(radius) || 0; };
    var byId = {}, ids = [];
    nodes.forEach(function (n) {
      if (!n || n.id == null || byId[n.id]) return;
      byId[n.id] = n;
      ids.push(n.id);
    });
    var room = {}, maxRoom = 1;
    ids.forEach(function (id) {
      var r = Number(roomOf(byId[id]));
      room[id] = isFinite(r) && r > 0 ? r : 0;
      if (room[id] > maxRoom) maxRoom = room[id];
    });

    var out = {}, placed = [];
    var kept = ids.filter(function (id) { return has(stored, id) && finite(stored[id]); });
    kept.forEach(function (id) { out[id] = { x: stored[id].x, y: stored[id].y }; });

    // Nothing stored: the sim's own layout, as it is.
    if (!kept.length) {
      ids.forEach(function (id) {
        var s = finite(seed[id]) ? seed[id] : { x: 0, y: 0 };
        out[id] = { x: s.x, y: s.y };
        placed.push(id);
      });
      return { positions: out, placed: placed };
    }

    // The centre of the stored frame, and the mean offset between the seed and stored frames
    // (from the centres when no stored node has a seed).
    var cx = 0, cy = 0, ox = 0, oy = 0, both = 0;
    kept.forEach(function (id) { cx += stored[id].x; cy += stored[id].y; });
    cx /= kept.length; cy /= kept.length;
    kept.forEach(function (id) {
      if (!finite(seed[id])) return;
      ox += stored[id].x - seed[id].x;
      oy += stored[id].y - seed[id].y;
      both++;
    });
    if (both) { ox /= both; oy /= both; }
    else {
      var seeded = ids.filter(function (id) { return finite(seed[id]); });
      if (seeded.length) {
        var sx0 = 0, sy0 = 0;
        seeded.forEach(function (id) { sx0 += seed[id].x; sy0 += seed[id].y; });
        ox = cx - sx0 / seeded.length;
        oy = cy - sy0 / seeded.length;
      }
    }

    var near = {};
    edges.forEach(function (e) {
      var a = e && e.source, b = e && e.target;
      if (a == null || b == null || a === b || !byId[a] || !byId[b]) return;
      (near[a] || (near[a] = [])).push(b);
      (near[b] || (near[b] = [])).push(a);
    });

    var discs = new Discs(2 * maxRoom);
    kept.forEach(function (id) { discs.add(out[id].x, out[id].y, room[id]); });

    // Hubs (anything a node names as its primary hub) first, then nodes without a hub, then leaves.
    var anchor = {};
    ids.forEach(function (id) { var h = byId[id]._primaryHubId; if (h != null) anchor[h] = 1; });
    var order = {};
    ids.forEach(function (id, i) { order[id] = i; });
    function group(id) { return anchor[id] ? 0 : byId[id]._primaryHubId == null ? 1 : 2; }
    var fresh = ids.filter(function (id) { return !out[id]; });
    fresh.sort(function (a, b) { return group(a) - group(b) || order[a] - order[b]; });

    fresh.forEach(function (id) {
      var s = finite(seed[id]) ? seed[id] : null, hub = byId[id]._primaryHubId, target = null;
      if (s && hub != null && out[hub] && finite(seed[hub])) {
        target = { x: out[hub].x + (s.x - seed[hub].x), y: out[hub].y + (s.y - seed[hub].y) };
      } else {
        var placedNear = (near[id] || []).filter(function (m) { return !!out[m]; });
        if (placedNear.length) {
          var sx = 0, sy = 0;
          placedNear.forEach(function (m) { sx += out[m].x; sy += out[m].y; });
          target = { x: sx / placedNear.length, y: sy / placedNear.length };
        } else if (s) {
          target = { x: s.x + ox, y: s.y + oy };
        } else {
          target = { x: cx, y: cy };
        }
      }
      var spot = freeSpot(discs, target, room[id]);
      out[id] = spot;
      discs.add(spot.x, spot.y, room[id]);
      placed.push(id);
    });
    return { positions: out, placed: placed };
  }

  // A display-only map derived from actual adjacency. It never reads or writes stored
  // coordinates. Semantic hubs seed communities; their connected records fill unequal
  // regions through the centre instead of forming one rim around an empty centre.
  function organicView(nodes, edges) {
    var list=(nodes||[]).filter(function(n){return n&&typeof n.id==='string';}).slice().sort(function(a,b){return a.id.localeCompare(b.id);});
    var byId=Object.create(null),near=Object.create(null),groups=Object.create(null),positions={},primary={};
    list.forEach(function(n){byId[n.id]=n;near[n.id]=[];});
    (edges||[]).forEach(function(e){if(e&&byId[e.source]&&byId[e.target]&&e.source!==e.target){near[e.source].push(e.target);near[e.target].push(e.source);}});
    Object.keys(near).forEach(function(id){near[id]=Array.from(new Set(near[id])).sort();});
    if(!list.length)return {positions:positions,primary:primary};
    function rank(a,b){return near[b.id].length-near[a.id].length||a.id.localeCompare(b.id);}
    var hubs=list.filter(function(n){return n.id!=='grove-self'&&['company','project','organisation','plan'].includes(n.type);}).sort(rank);
    if(!hubs.length)hubs=list.filter(function(n){return n.id!=='grove-self';}).sort(rank).slice(0,Math.max(1,Math.min(8,Math.round(Math.sqrt(list.length)/3))));
    if(!hubs.length)hubs=list.slice(0,1);
    var queue=[];
    hubs.forEach(function(n){primary[n.id]=n.id;groups[n.id]=[];queue.push(n.id);});
    for(var q=0;q<queue.length;q++)near[queue[q]].forEach(function(id){if(id==='grove-self'||primary[id])return;primary[id]=primary[queue[q]];queue.push(id);});
    list.forEach(function(n){if(n.id==='grove-self')return;if(!primary[n.id]){primary[n.id]=n.id;groups[n.id]=[];hubs.push(n);}groups[primary[n.id]].push(n);});
    hubs.sort(function(a,b){return groups[b.id].length-groups[a.id].length||rank(a,b);});
    var centers={},golden=2.399963229728653;
    hubs.forEach(function(h,i){var angle=i*golden,radial=170*Math.sqrt(i)*(0.92+(i%3)*0.07);centers[h.id]={x:Math.cos(angle)*radial*1.25,y:Math.sin(angle)*radial*0.88};});
    // Actual inter-community edges pull related regions together, with a bounded shift.
    var start=JSON.parse(JSON.stringify(centers));
    hubs.forEach(function(h){var links=Object.create(null);groups[h.id].forEach(function(n){near[n.id].forEach(function(id){var other=primary[id];if(other&&other!==h.id)links[other]=(links[other]||0)+1;});});var total=0,x=0,y=0;Object.keys(links).sort().forEach(function(id){if(!start[id])return;var w=Math.sqrt(links[id]);total+=w;x+=start[id].x*w;y+=start[id].y*w;});if(total){centers[h.id].x=start[h.id].x*.82+x/total*.18;centers[h.id].y=start[h.id].y*.82+y/total*.18;}});
    function hash(id){var h=2166136261;for(var i=0;i<id.length;i++){h^=id.charCodeAt(i);h=Math.imul(h,16777619);}return h>>>0;}
    var discs=new Discs(150);
    hubs.forEach(function(h,index){
      var c=centers[h.id],members=groups[h.id].slice().sort(function(a,b){return (a.id===h.id?-1:b.id===h.id?1:rank(a,b));});
      members.forEach(function(n,i){var phase=(hash(n.id)%1000)/1000,angle=i*golden+index*.71,r=i?22*Math.sqrt(i)*(0.8+phase*.4):0;
        var target={x:c.x+Math.cos(angle)*r*(1.0+(index%3)*.15),y:c.y+Math.sin(angle)*r*(.82+(index%2)*.15)};
        var room=n.id===h.id?45:['person','instance','place'].includes(n.type)?22:9;
        var spot=freeSpot(discs,target,room);positions[n.id]=spot;discs.add(spot.x,spot.y,room);
      });
    });
    if(byId['grove-self']){var self=freeSpot(discs,{x:0,y:0},65);positions['grove-self']=self;primary['grove-self']=null;}
    return {positions:positions,primary:primary};
  }

  var api = { placeNew: placeNew, organicView: organicView };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.GroveLayoutKeep = api;
})(typeof window !== 'undefined' ? window : null);
