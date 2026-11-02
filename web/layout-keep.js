/* Grove map positions: keep every placed item where it is, place only the new ones.
 *
 * placeNew(stored, nodes, edges, seed, radius) is pure and deterministic:
 *   stored  {id: {x, y}}  positions the store (or this session) already holds
 *   nodes   [{id, _primaryHubId?, ...}] the current graph, in a stable order
 *   edges   [{source, target}]
 *   seed    {id: {x, y}}  the sim's own layout of the current graph (native-app seedLayout)
 *   radius  function (node) -> the room a node takes, as the collide force sees it (or a number)
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

  var api = { placeNew: placeNew };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.GroveLayoutKeep = api;
})(typeof window !== 'undefined' ? window : null);
