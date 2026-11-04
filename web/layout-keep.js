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

  // Graph-derived communities and local links determine this display-only map.
  // No identifier prefix, saved coordinate or fixture-specific name affects grouping.
  function organicView(nodes, edges) {
    var list=(nodes||[]).filter(function(n){return n&&typeof n.id==='string';}).slice().sort(function(a,b){return a.id.localeCompare(b.id);});
    var byId=Object.create(null),near=Object.create(null),positions={},primary={},radii={};
    list.forEach(function(n){byId[n.id]=n;near[n.id]=[];});
    (edges||[]).forEach(function(e){if(e&&byId[e.source]&&byId[e.target]&&e.source!==e.target){near[e.source].push(e.target);near[e.target].push(e.source);}});
    Object.keys(near).forEach(function(id){near[id]=Array.from(new Set(near[id])).sort();});
    if(!list.length)return {positions:positions,primary:primary,radii:radii};
    var content=list.filter(function(n){return n.id!=='grove-self';}),label=Object.create(null),mass=Object.create(null),total=0;
    content.forEach(function(n){label[n.id]=n.id;mass[n.id]=near[n.id].length;total+=near[n.id].length;});
    // A bounded modularity pass keeps densely linked records together, instead of
    // treating every plan as an independent pillar and scattering its links.
    for(var pass=0;pass<18;pass++){
      var moved=0;
      content.forEach(function(n){
        var degree=near[n.id].length,old=label[n.id],weights=Object.create(null);
        if(!degree)return;
        mass[old]-=degree;
        near[n.id].forEach(function(id){if(label[id])weights[label[id]]=(weights[label[id]]||0)+1;});
        var best=old,score=(weights[old]||0)-degree*(mass[old]||0)/Math.max(1,total);
        Object.keys(weights).sort().forEach(function(group){var next=weights[group]-degree*(mass[group]||0)/Math.max(1,total);if(next>score+1e-8){best=group;score=next;}});
        label[n.id]=best;mass[best]=(mass[best]||0)+degree;if(best!==old)moved++;
      });
      if(!moved)break;
    }
    // Aggregate profitable neighbouring communities. A whole connected plan
    // can move even when moving one leaf alone would lower modularity.
    for(var round=0;round<96;round++){
      var between=Object.create(null),winner=null,gain=1e-8;
      content.forEach(function(n){near[n.id].forEach(function(id){var a=label[n.id],b=label[id];if(a&&b&&a<b){var key=a+'\0'+b;between[key]=(between[key]||0)+1;}});});
      Object.keys(between).sort().forEach(function(key){var pair=key.split('\0'),score=between[key]-(mass[pair[0]]||0)*(mass[pair[1]]||0)/Math.max(1,total);if(score>gain){gain=score;winner=pair;}});
      if(!winner)break;
      content.forEach(function(n){if(label[n.id]===winner[1])label[n.id]=winner[0];});
      mass[winner[0]]+=mass[winner[1]];mass[winner[1]]=0;
    }
    var groups=Object.create(null);
    content.forEach(function(n){(groups[label[n.id]]||(groups[label[n.id]]=[])).push(n);});
    // Stable group identity uses the best-connected member, never retained text.
    var hubs=Object.keys(groups).map(function(id){var members=groups[id];members.sort(function(a,b){return near[b.id].length-near[a.id].length||a.id.localeCompare(b.id);});return {id:members[0].id,members:members,r:Math.max(55,20*Math.sqrt(members.length))};}).sort(function(a,b){return a.id.localeCompare(b.id);});
    hubs.forEach(function(h){h.members.forEach(function(n){primary[n.id]=h.id;var semantic=['company','project','organisation','plan'].includes(n.type);radii[n.id]=semantic?Math.min(21,8+Math.sqrt(near[n.id].length)*2):['person','instance','place'].includes(n.type)?10:['event','observation'].includes(n.type)?4.5:6;});});
    var hubMap=Object.create(null),links=Object.create(null),golden=2.399963229728653;
    hubs.forEach(function(h,i){var a=i*golden,r=190*Math.sqrt(i);h.x=Math.cos(a)*r;h.y=Math.sin(a)*r;hubMap[h.id]=h;});
    content.forEach(function(n){near[n.id].forEach(function(id){var a=primary[n.id],b=primary[id];if(a&&b&&a<b){var k=a+'\0'+b;links[k]=(links[k]||0)+1;}});});
    var bridges=Object.keys(links).sort().map(function(k){var pair=k.split('\0');return {a:hubMap[pair[0]],b:hubMap[pair[1]],w:links[k]};});
    var centerSteps=Math.min(420,Math.max(2,Math.floor(2000000/Math.max(1,hubs.length*hubs.length))));
    for(var step=0;step<centerSteps;step++){
      hubs.forEach(function(h){h.dx=-h.x*.0001;h.dy=-h.y*.0001;});
      for(var a=0;a<hubs.length;a++)for(var b=a+1;b<hubs.length;b++){
        var one=hubs[a],two=hubs[b],dx=two.x-one.x,dy=two.y-one.y,d=Math.max(1,Math.hypot(dx,dy)),space=one.r+two.r+45;
        var push=d<space?(space-d)*.075:Math.min(1.8,800/(d*d));
        one.dx-=dx/d*push;one.dy-=dy/d*push;two.dx+=dx/d*push;two.dy+=dy/d*push;
      }
      bridges.forEach(function(edge){var dx=edge.b.x-edge.a.x,dy=edge.b.y-edge.a.y,d=Math.max(1,Math.hypot(dx,dy)),target=edge.a.r+edge.b.r+55;var pull=(d-target)*Math.min(.055,.012*Math.sqrt(edge.w));edge.a.dx+=dx/d*pull;edge.a.dy+=dy/d*pull;edge.b.dx-=dx/d*pull;edge.b.dy-=dy/d*pull;});
      hubs.forEach(function(h){h.x+=Math.max(-9,Math.min(9,h.dx));h.y+=Math.max(-9,Math.min(9,h.dy));});
    }
    hubs.forEach(function(h,index){
      var local=Object.create(null),members=h.members;
      members.forEach(function(n,i){var angle=i*golden+index*.67,r=19*Math.sqrt(i);local[n.id]={x:Math.cos(angle)*r,y:Math.sin(angle)*r};});
      var localSteps=Math.min(75,Math.max(2,Math.floor(2000000/Math.max(1,members.length*members.length))));
      for(var iteration=0;iteration<localSteps;iteration++){
        members.forEach(function(n){var p=local[n.id];p.dx=-p.x*.014;p.dy=-p.y*.014;});
        for(var a=0;a<members.length;a++)for(var b=a+1;b<members.length;b++){
          var na=members[a],nb=members[b],pa=local[na.id],pb=local[nb.id],dx=pb.x-pa.x,dy=pb.y-pa.y,d=Math.max(.1,Math.hypot(dx,dy)),min=radii[na.id]+radii[nb.id]+8;
          var push=d<min?(min-d)*.35:Math.min(.5,40/(d*d));pa.dx-=dx/d*push;pa.dy-=dy/d*push;pb.dx+=dx/d*push;pb.dy+=dy/d*push;
        }
        members.forEach(function(n){var p=local[n.id];near[n.id].forEach(function(id){if(n.id>=id||!local[id])return;var q=local[id],dx=q.x-p.x,dy=q.y-p.y,d=Math.max(.1,Math.hypot(dx,dy)),pull=(d-42)*.018;p.dx+=dx/d*pull;p.dy+=dy/d*pull;q.dx-=dx/d*pull;q.dy-=dy/d*pull;});});
        members.forEach(function(n){var p=local[n.id];p.x+=Math.max(-4,Math.min(4,p.dx));p.y+=Math.max(-4,Math.min(4,p.dy));});
      }
      // Expand each local network into its reserved territory. Its outer
      // records meet neighbouring networks instead of shrinking into islands.
      var angle=index*golden,cos=Math.cos(angle),sin=Math.sin(angle);
      members.forEach(function(n){var p=local[n.id];positions[n.id]={x:h.x+2.4*(p.x*cos-p.y*sin),y:h.y+2.4*(p.x*sin+p.y*cos)};});
    });
    if(byId['grove-self']){var x=0,y=0;hubs.forEach(function(h){x+=h.x;y+=h.y;});positions['grove-self']={x:x/Math.max(1,hubs.length),y:y/Math.max(1,hubs.length)};primary['grove-self']=null;radii['grove-self']=24;}
    return {positions:positions,primary:primary,radii:radii};
  }

  var api = { placeNew: placeNew, organicView: organicView };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.GroveLayoutKeep = api;
})(typeof window !== 'undefined' ? window : null);
