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
 *   a near spot where its disc overlaps no placed disc (radius + radius apart).
 * Hubs and nodes without a hub are placed before the leaves that anchor on them.
 *
 * Used by native-app.js in live mode (window.GroveLayoutKeep) and by web/checks (require).
 */
(function (root) {
  'use strict';

  var LARGE_ARRIVAL = 300;   // above this many new nodes, speed comes before the nearest spot

  function has(map, id) { return Object.prototype.hasOwnProperty.call(map, id) && !!map[id]; }
  function finite(p) { return !!p && typeof p.x === 'number' && typeof p.y === 'number' && isFinite(p.x) && isFinite(p.y); }

  // A uniform grid of placed discs, so a spot check reads only the nearby cells. A disc sits in each
  // cell its box touches, so that one wide disc does not force wide cells on all the small ones.
  function Discs(cell) {
    this.cell = cell;
    this.cells = new Map();
  }
  Discs.prototype.key = function (cx, cy) { return cx * 4194301 + cy; };
  Discs.prototype.add = function (x, y, r) {
    var disc = { x: x, y: y, r: r }, c = this.cell;
    for (var i = Math.floor((x - r) / c); i <= Math.floor((x + r) / c); i++) {
      for (var j = Math.floor((y - r) / c); j <= Math.floor((y + r) / c); j++) {
        var k = this.key(i, j), list = this.cells.get(k);
        if (list) list.push(disc); else this.cells.set(k, [disc]);
      }
    }
  };
  // Two overlapping discs share a point, and so a cell of both boxes: this box scan misses none.
  Discs.prototype.free = function (x, y, r) {
    var c = this.cell;
    for (var i = Math.floor((x - r) / c); i <= Math.floor((x + r) / c); i++) {
      for (var j = Math.floor((y - r) / c); j <= Math.floor((y + r) / c); j++) {
        var list = this.cells.get(this.key(i, j));
        if (!list) continue;
        for (var n = 0; n < list.length; n++) {
          var d = list[n];
          if (Math.hypot(d.x - x, d.y - y) < d.r + r) return false;
        }
      }
    }
    return true;
  };

  // A near free spot: the target itself, else the first free point on a spiral around it, at about
  // two points for each disc width. A large arrival passes `reached`: placed discs never leave, so a
  // search in the same area resumes near the point where the last one stopped. That costs a node
  // a little distance from its target, and it keeps the page from freezing.
  function freeSpot(discs, target, r, reached) {
    if (discs.free(target.x, target.y, r)) return { x: target.x, y: target.y };
    var step = Math.max(1, r * 0.5), wide = step * 12;
    var area = reached ? discs.key(Math.floor(target.x / wide), Math.floor(target.y / wide)) : 0;
    var first = reached ? Math.max(1, Math.floor((reached.get(area) || 0) * 0.85)) : 1;
    for (var k = first; k <= 2000000; k++) {
      var reach = step * Math.sqrt(k), angle = k * 2.399963229728653;
      var x = target.x + reach * Math.cos(angle), y = target.y + reach * Math.sin(angle);
      if (discs.free(x, y, r)) {
        if (reached) reached.set(area, k);
        return { x: x, y: y };
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
    var room = {};
    ids.forEach(function (id) {
      var r = Number(roomOf(byId[id]));
      room[id] = isFinite(r) && r > 0 ? r : 0;
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

    var rooms = ids.map(function (id) { return room[id]; }).sort(function (a, b) { return a - b; });
    var discs = new Discs(Math.max(8, 2 * (rooms[rooms.length >> 1] || 1)));
    kept.forEach(function (id) { discs.add(out[id].x, out[id].y, room[id]); });

    // Hubs (anything a node names as its primary hub) first, then nodes without a hub, then leaves.
    var anchor = {};
    ids.forEach(function (id) { var h = byId[id]._primaryHubId; if (h != null) anchor[h] = 1; });
    var order = {};
    ids.forEach(function (id, i) { order[id] = i; });
    function group(id) { return anchor[id] ? 0 : byId[id]._primaryHubId == null ? 1 : 2; }
    var fresh = ids.filter(function (id) { return !out[id]; });
    fresh.sort(function (a, b) { return group(a) - group(b) || order[a] - order[b]; });
    var reached = fresh.length > LARGE_ARRIVAL ? new Map() : null;

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
      var spot = freeSpot(discs, target, room[id], reached);
      out[id] = spot;
      discs.add(spot.x, spot.y, room[id]);
      placed.push(id);
    });
    return { positions: out, placed: placed };
  }

  // The display map: lobes of small dots, in the look of a real brain of about 5,000 nodes.
  // Graph-derived groups and local links decide it. No identifier prefix, saved coordinate or
  // fixture-specific name affects it, and the same graph gives the same map in any input order.
  var GOLDEN = 2.399963229728653;
  var SPIRAL = 10.5;      // the step of a lobe's spiral: neighbouring dots sit about 20 apart
  var LOBE_GAP = 5;       // the space two lobes of one clump keep: their edges meet
  var CLUMP_GAP = 26;     // the open space between clumps, which the hairline edges cross
  var OWNER_ROOM = 92;    // the clear disc the owner node and its name take at the centre
  var MIN_LOBE = 6;       // a smaller group joins a neighbouring lobe
  var CHUNK = 24;         // the spiral slots that share out by direction, in one pass
  var FULL_TURN = 2 * Math.PI;

  function compareIds(a, b) { return a < b ? -1 : a > b ? 1 : 0; }
  // A stable value in 0..1 from a text: the small variety of a lobe stays the same on every load.
  function unitHash(text) {
    var h = 2166136261;
    for (var i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0) / 4294967296;
  }
  function turnOf(angle) { return ((angle % FULL_TURN) + FULL_TURN) % FULL_TURN; }

  // One level of modularity moves: each node joins the neighbouring group it gains most from.
  function moveToGroups(ids, local) {
    var label = Object.create(null), mass = Object.create(null), total = 0;
    ids.forEach(function (id) { label[id] = id; mass[id] = local[id].length; total += local[id].length; });
    var whole = Math.max(1, total);
    for (var pass = 0; pass < 18; pass++) {
      var moved = 0;
      ids.forEach(function (id) {
        var degree = local[id].length, old = label[id], weights = Object.create(null);
        if (!degree) return;
        mass[old] -= degree;
        local[id].forEach(function (other) { weights[label[other]] = (weights[label[other]] || 0) + 1; });
        var best = old, score = (weights[old] || 0) - degree * (mass[old] || 0) / whole;
        Object.keys(weights).sort().forEach(function (group) {
          var next = weights[group] - degree * (mass[group] || 0) / whole;
          if (next > score + 1e-8) { best = group; score = next; }
        });
        label[id] = best;
        mass[best] = (mass[best] || 0) + degree;
        if (best !== old) moved++;
      });
      if (!moved) break;
    }
    return { label: label, mass: mass, whole: whole };
  }

  // Joins neighbouring groups while the join gains modularity, so that a whole connected plan can
  // move even when one leaf alone would not. A group stops at the limit: one lobe never takes all.
  function joinGroups(ids, local, found, limit) {
    var label = found.label, mass = found.mass, between = Object.create(null), size = Object.create(null);
    ids.forEach(function (id) {
      var a = label[id];
      size[a] = (size[a] || 0) + 1;
      local[id].forEach(function (other) {
        var b = label[other];
        if (a === b) return;
        var row = between[a] || (between[a] = Object.create(null));
        row[b] = (row[b] || 0) + 1;
      });
    });
    var into = Object.create(null);
    for (var round = 0; round < 240; round++) {
      var keep = null, drop = null, gain = 1e-8;
      Object.keys(between).sort().forEach(function (a) {
        Object.keys(between[a]).sort().forEach(function (b) {
          if (a >= b || size[a] + size[b] > limit) return;
          var score = between[a][b] - (mass[a] || 0) * (mass[b] || 0) / found.whole;
          if (score > gain) { gain = score; keep = a; drop = b; }
        });
      });
      if (keep === null) break;
      Object.keys(between[drop]).forEach(function (other) {
        var weight = between[drop][other];
        delete between[other][drop];
        if (other === keep) return;
        between[keep][other] = (between[keep][other] || 0) + weight;
        between[other][keep] = (between[other][keep] || 0) + weight;
      });
      delete between[drop];
      delete between[keep][drop];
      mass[keep] = (mass[keep] || 0) + (mass[drop] || 0);
      size[keep] += size[drop];
      into[drop] = keep;
    }
    function root(id) { while (into[id]) id = into[id]; return id; }
    ids.forEach(function (id) { label[id] = root(label[id]); });
  }

  // A group over the limit opens up around its best-linked member, so one huge hub gives several
  // lobes when its leaves link to each other. A plain star stays one spiral.
  function splitGroup(members, near, limit, depth) {
    if (members.length <= limit || depth > 2) return [members];
    var inside = Object.create(null);
    members.forEach(function (id) { inside[id] = 1; });
    var core = members.slice().sort(function (a, b) { return near[b].length - near[a].length || compareIds(a, b); })[0];
    var rest = members.filter(function (id) { return id !== core; }), local = Object.create(null);
    rest.forEach(function (id) { local[id] = near[id].filter(function (other) { return inside[other] && other !== core; }); });
    var found = moveToGroups(rest, local);
    joinGroups(rest, local, found, Math.max(limit * 0.7, MIN_LOBE * 4));
    var parts = Object.create(null);
    rest.forEach(function (id) { var g = found.label[id]; (parts[g] || (parts[g] = [])).push(id); });
    var lobes = Object.keys(parts).sort().map(function (g) { return parts[g]; })
      .filter(function (part) { return part.length >= MIN_LOBE * 2; })
      .sort(function (a, b) { return b.length - a.length || compareIds(a[0], b[0]); });
    if (lobes.length < 2) return [members];
    var inLobe = Object.create(null);
    lobes.forEach(function (lobe, index) { lobe.forEach(function (id) { inLobe[id] = index; }); });
    rest.forEach(function (id) {
      if (inLobe[id] !== undefined) return;
      var votes = Object.create(null), best = 0, top = 0;
      local[id].forEach(function (other) { var at = inLobe[other]; if (at !== undefined) votes[at] = (votes[at] || 0) + 1; });
      Object.keys(votes).sort().forEach(function (at) { if (votes[at] > top) { top = votes[at]; best = Number(at); } });
      lobes[best].push(id);
    });
    lobes[0].push(core);
    return lobes.reduce(function (all, lobe) { return all.concat(splitGroup(lobe, near, limit, depth + 1)); }, []);
  }

  // A small group joins the lobe it links to most. With no link it joins the loose lobe, in which
  // the members of one group stay side by side.
  function foldSmallGroups(groups, near) {
    var big = groups.filter(function (g) { return g.length >= MIN_LOBE; }), small = groups.filter(function (g) { return g.length < MIN_LOBE; });
    if (!big.length) big = [[]];
    var at = Object.create(null);
    big.forEach(function (g, index) { g.forEach(function (id) { at[id] = index; }); });
    var loose = [];
    small.forEach(function (group) {
      var votes = Object.create(null), best = -1, top = 0;
      group.forEach(function (id) { near[id].forEach(function (other) { if (at[other] !== undefined) votes[at[other]] = (votes[at[other]] || 0) + 1; }); });
      Object.keys(votes).sort().forEach(function (index) { if (votes[index] > top) { top = votes[index]; best = Number(index); } });
      if (best < 0) { loose.push.apply(loose, group); return; }
      big[best].push.apply(big[best], group);
      group.forEach(function (id) { at[id] = best; });
    });
    if (loose.length) big.push(loose);
    return big.filter(function (g) { return g.length; });
  }

  // Lobes pack as touching discs: linked lobes pull together, none overlaps, and the mass takes
  // the shape of the screen (aspect is its width over its height).
  function packLobes(lobes, bridges, aspect, owner, gap) {
    var wide = Math.max(0.5, Math.min(1.7, aspect || 1.45)), used = owner ? OWNER_ROOM * OWNER_ROOM : 0;
    var ordered = lobes.slice().sort(function (a, b) { return b.r - a.r || compareIds(a.id, b.id); });
    ordered.forEach(function (lobe, index) {
      used += lobe.r * lobe.r;
      var angle = index * GOLDEN, reach = Math.sqrt(used);
      lobe.x = Math.cos(angle) * reach * Math.sqrt(wide);
      lobe.y = Math.sin(angle) * reach / Math.sqrt(wide);
    });
    var count = ordered.length, steps = Math.min(320, Math.max(24, Math.floor(1600000 / Math.max(1, count * count))));
    function apart(strength) {
      for (var a = 0; a < count; a++) {
        var one = ordered[a];
        if (owner) {
          var od = Math.max(1, Math.hypot(one.x, one.y)), oroom = OWNER_ROOM + one.r + gap;
          if (od < oroom) { one.dx += one.x / od * (oroom - od) * strength * 2; one.dy += one.y / od * (oroom - od) * strength * 2; }
        }
        for (var b = a + 1; b < count; b++) {
          var two = ordered[b], x = two.x - one.x, y = two.y - one.y, d = Math.max(1, Math.hypot(x, y)), room = one.r + two.r + gap;
          if (d >= room) continue;
          var push = (room - d) * strength;
          one.dx -= x / d * push; one.dy -= y / d * push;
          two.dx += x / d * push; two.dy += y / d * push;
        }
      }
    }
    for (var step = 0; step < steps; step++) {
      ordered.forEach(function (lobe) { lobe.dx = -lobe.x * 0.012 / wide; lobe.dy = -lobe.y * 0.012 * wide; });
      apart(0.35);
      bridges.forEach(function (edge) {
        var x = edge.b.x - edge.a.x, y = edge.b.y - edge.a.y, d = Math.max(1, Math.hypot(x, y)), target = edge.a.r + edge.b.r + gap;
        var pull = (d - target) * Math.min(0.012, 0.0025 * Math.sqrt(edge.w));
        edge.a.dx += x / d * pull; edge.a.dy += y / d * pull;
        edge.b.dx -= x / d * pull; edge.b.dy -= y / d * pull;
      });
      ordered.forEach(function (lobe) {
        lobe.x += Math.max(-24, Math.min(24, lobe.dx));
        lobe.y += Math.max(-24, Math.min(24, lobe.dy));
      });
    }
    // Links pull in every direction, so the mass ends round: open it to the shape of the screen,
    // then let it close up again along both axes. Three short rounds reach a tall phone shape too.
    var closing = Math.min(60, Math.max(6, Math.floor(1600000 / Math.max(1, count * count))));
    for (var round = 0; round < 3; round++) {
      var x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      ordered.forEach(function (lobe) {
        x0 = Math.min(x0, lobe.x - lobe.r); x1 = Math.max(x1, lobe.x + lobe.r);
        y0 = Math.min(y0, lobe.y - lobe.r); y1 = Math.max(y1, lobe.y + lobe.r);
      });
      var open = Math.sqrt(wide * (y1 - y0) / Math.max(1, x1 - x0));
      ordered.forEach(function (lobe) { lobe.x *= open; lobe.y /= open; });
      for (var close = 0; close < closing; close++) {
        ordered.forEach(function (lobe) { lobe.dx = -lobe.x * 0.01 / wide; lobe.dy = -lobe.y * 0.01 * wide; });
        apart(0.4);
        ordered.forEach(function (lobe) {
          lobe.x += Math.max(-18, Math.min(18, lobe.dx));
          lobe.y += Math.max(-18, Math.min(18, lobe.dy));
        });
      }
    }
    for (var settle = 0; settle < closing; settle++) {
      ordered.forEach(function (lobe) { lobe.dx = 0; lobe.dy = 0; });
      apart(0.5);
      ordered.forEach(function (lobe) { lobe.x += lobe.dx; lobe.y += lobe.dy; });
    }
  }

  // Lobes gather into a few clumps of uneven size: the most linked pairs join first, and no clump
  // takes more than a quarter of the map. Linked lobes touch inside a clump, and open space lies
  // between clumps, as in a real brain of thousands of nodes.
  function gatherClumps(lobes, bridges, total) {
    var wanted = Math.max(1, Math.min(14, Math.round(Math.sqrt(total) / 5.5))), most = Math.max(total / 4, 120);
    var into = new Map(), size = new Map();
    lobes.forEach(function (lobe) { size.set(lobe, lobe.members.length); });
    function root(lobe) { while (into.has(lobe)) lobe = into.get(lobe); return lobe; }
    var strongest = bridges.slice().sort(function (a, b) {
      var one = a.w / Math.sqrt(a.a.members.length * a.b.members.length), two = b.w / Math.sqrt(b.a.members.length * b.b.members.length);
      return two - one || compareIds(a.a.id + a.b.id, b.a.id + b.b.id);
    });
    var left = lobes.length;
    strongest.forEach(function (edge) {
      if (left <= wanted) return;
      var a = root(edge.a), b = root(edge.b);
      if (a === b || size.get(a) + size.get(b) > most) return;
      into.set(b, a);
      size.set(a, size.get(a) + size.get(b));
      left--;
    });
    var byRoot = new Map();
    lobes.forEach(function (lobe) {
      var r = root(lobe);
      if (!byRoot.has(r)) byRoot.set(r, { id: r.id, lobes: [], x: 0, y: 0, dx: 0, dy: 0, r: 0 });
      byRoot.get(r).lobes.push(lobe);
    });
    return Array.from(byRoot.values()).sort(function (a, b) { return compareIds(a.id, b.id); });
  }

  // Each clump packs its own lobes, then the clumps pack as wide discs around the owner.
  function packClumps(lobes, bridges, total, aspect, owner) {
    var clumps = gatherClumps(lobes, bridges, total), clumpOf = new Map();
    clumps.forEach(function (clump) {
      clump.lobes.forEach(function (lobe) { clumpOf.set(lobe, clump); });
      var inside = bridges.filter(function (edge) { return clumpOf.get(edge.a) === clump && clumpOf.get(edge.b) === clump; });
      packLobes(clump.lobes, inside, 1, false, LOBE_GAP);
      var cx = 0, cy = 0, mass = 0;
      clump.lobes.forEach(function (lobe) { cx += lobe.x * lobe.r; cy += lobe.y * lobe.r; mass += lobe.r; });
      cx /= mass; cy /= mass;
      var area = 0;
      clump.lobes.forEach(function (lobe) { lobe.x -= cx; lobe.y -= cy; area += lobe.r * lobe.r; });
      clump.r = Math.sqrt(area) * 1.12;   // by area, not reach: a long clump leaves room beside it
    });
    var joined = new Map();
    bridges.forEach(function (edge) {
      var a = clumpOf.get(edge.a), b = clumpOf.get(edge.b);
      if (a === b) return;
      var key = a.id < b.id ? a.id + '\0' + b.id : b.id + '\0' + a.id;
      if (!joined.has(key)) joined.set(key, { a: a, b: b, w: 0 });
      joined.get(key).w += edge.w;
    });
    var between = Array.from(joined.keys()).sort().map(function (key) { return joined.get(key); });
    packLobes(clumps, between, aspect, owner, CLUMP_GAP);
    clumps.forEach(function (clump) {
      clump.lobes.forEach(function (lobe) { lobe.x += clump.x; lobe.y += clump.y; });
    });
    // Clumps packed by area can touch at their ends: the lobes themselves settle apart.
    var count = lobes.length, rounds = Math.min(40, Math.max(4, Math.floor(800000 / Math.max(1, count * count))));
    for (var round = 0; round < rounds; round++) {
      for (var a = 0; a < count; a++) {
        for (var b = a + 1; b < count; b++) {
          var one = lobes[a], two = lobes[b], x = two.x - one.x, y = two.y - one.y, d = Math.max(1, Math.hypot(x, y));
          var push = (one.r + two.r + LOBE_GAP - d) * 0.5;
          if (push <= 0) continue;
          one.x -= x / d * push; one.y -= y / d * push;
          two.x += x / d * push; two.y += y / d * push;
        }
      }
      if (owner) {
        lobes.forEach(function (lobe) {
          var d = Math.max(1, Math.hypot(lobe.x, lobe.y)), room = OWNER_ROOM + lobe.r + LOBE_GAP;
          if (d < room) { lobe.x += lobe.x / d * (room - d); lobe.y += lobe.y / d * (room - d); }
        });
      }
    }
  }

  // A lobe is a spiral of dots, the best-linked at its centre. Inside each ring a dot takes the
  // side that faces its links to other lobes, so connections fan between lobes instead of crossing
  // them. A small stretch and jitter keep it from reading as a drawn circle.
  function placeLobe(lobe, index, lobeOf, near, radii, positions) {
    var members = lobe.members, phase = unitHash(lobe.id + '/phase') * FULL_TURN;
    var stretch = 1.06 + unitHash(lobe.id + '/stretch') * 0.2, axis = lobe.axis, count = members.length;
    var slots = members.map(function (_, i) {
      var reach = Math.sqrt(lobe.core * lobe.core * (i ? 1 : 0) + SPIRAL * SPIRAL * i * 1.04);
      return { angle: i * GOLDEN + phase, reach: reach };
    });
    var want = Object.create(null);
    members.forEach(function (id) {
      var x = 0, y = 0;
      near[id].forEach(function (other) {
        var there = lobeOf[other];
        if (!there || there === lobe) return;
        var dx = there.x - lobe.x, dy = there.y - lobe.y, d = Math.max(1, Math.hypot(dx, dy));
        x += dx / d; y += dy / d;
      });
      want[id] = x || y ? turnOf(Math.atan2(y, x)) : unitHash(id + '/want') * FULL_TURN;
    });
    var taken = Object.create(null);
    if (count) taken[members[0]] = slots[0];
    for (var start = 1; start < count; start += CHUNK) {
      var part = members.slice(start, start + CHUNK), seats = slots.slice(start, start + CHUNK);
      part.sort(function (a, b) { return want[a] - want[b] || compareIds(a, b); });
      seats.sort(function (a, b) { return turnOf(a.angle) - turnOf(b.angle) || a.reach - b.reach; });
      part.forEach(function (id, i) { taken[id] = seats[i]; });
    }
    // The outline is a lump, not a circle, and the hub sits off the middle.
    var cos = Math.cos(axis), sin = Math.sin(axis), lumpA = unitHash(lobe.id + '/a') * FULL_TURN, lumpB = unitHash(lobe.id + '/b') * FULL_TURN;
    var drift = unitHash(lobe.id + '/drift') * FULL_TURN, driftX = Math.cos(drift) * lobe.r * 0.24, driftY = Math.sin(drift) * lobe.r * 0.24;
    members.forEach(function (id) {
      var seat = taken[id], shape = 1 + 0.17 * Math.sin(3 * seat.angle + lumpA) + 0.09 * Math.sin(5 * seat.angle + lumpB);
      var near0 = Math.max(0, 1 - seat.reach / lobe.r), toward = near0 * near0;
      var x = Math.cos(seat.angle) * seat.reach * shape + driftX * toward, y = Math.sin(seat.angle) * seat.reach * shape + driftY * toward;
      var along = (x * cos + y * sin) * stretch, across = (y * cos - x * sin) / stretch;
      var loose = seat.reach ? SPIRAL * 0.34 : 0;
      positions[id] = {
        x: lobe.x + along * cos - across * sin + (unitHash(id + '/x') - 0.5) * loose,
        y: lobe.y + along * sin + across * cos + (unitHash(id + '/y') - 0.5) * loose,
      };
    });
  }

  function organicView(nodes, edges, aspect) {
    var list = (nodes || []).filter(function (n) { return n && typeof n.id === 'string'; }).slice().sort(function (a, b) { return compareIds(a.id, b.id); });
    var byId = Object.create(null), near = Object.create(null), positions = {}, primary = {}, radii = {};
    list.forEach(function (n) { byId[n.id] = n; near[n.id] = []; });
    (edges || []).forEach(function (e) {
      if (e && byId[e.source] && byId[e.target] && e.source !== e.target) { near[e.source].push(e.target); near[e.target].push(e.source); }
    });
    Object.keys(near).forEach(function (id) { near[id] = Array.from(new Set(near[id])).sort(); });
    if (!list.length) return { positions: positions, primary: primary, radii: radii };

    var content = list.filter(function (n) { return n.id !== 'grove-self'; }).map(function (n) { return n.id; });
    var owner = !!byId['grove-self'], local = Object.create(null);
    content.forEach(function (id) { local[id] = near[id].filter(function (other) { return other !== 'grove-self'; }); });
    var limit = Math.max(90, Math.ceil(content.length / 7));
    var found = moveToGroups(content, local);
    joinGroups(content, local, found, limit);
    var buckets = Object.create(null);
    content.forEach(function (id) { var g = found.label[id]; (buckets[g] || (buckets[g] = [])).push(id); });
    var groups = Object.keys(buckets).sort().reduce(function (all, g) { return all.concat(splitGroup(buckets[g], local, limit, 0)); }, []);
    groups = foldSmallGroups(groups, local);

    var lobeOf = Object.create(null);
    var lobes = groups.map(function (group) {
      var members = group.slice().sort(function (a, b) { return local[b].length - local[a].length || compareIds(a, b); });
      var lobe = { id: members[0], members: members, x: 0, y: 0, dx: 0, dy: 0, axis: 0, core: 0 };
      members.forEach(function (id) { lobeOf[id] = lobe; });
      return lobe;
    }).sort(function (a, b) { return compareIds(a.id, b.id); });

    lobes.forEach(function (lobe) {
      lobe.members.forEach(function (id, rank) {
        var n = byId[id], degree = near[id].length, semantic = ['company', 'project', 'organisation', 'plan'].indexOf(n.type) !== -1;
        var r = semantic ? Math.min(46, 7 + Math.sqrt(degree) * 1.25)
          : ['person', 'instance', 'place'].indexOf(n.type) !== -1 ? 10
          : ['event', 'observation'].indexOf(n.type) !== -1 ? 5 : 6;
        if (rank === 0 && degree >= 20) r = Math.max(r, Math.min(18, 6 + Math.sqrt(degree)));
        radii[id] = r;
        primary[id] = lobe.id;
      });
      lobe.core = radii[lobe.id] + 8;
      lobe.r = Math.sqrt(lobe.core * lobe.core + SPIRAL * SPIRAL * lobe.members.length * 1.04);
    });

    var joined = Object.create(null);
    content.forEach(function (id) {
      local[id].forEach(function (other) {
        var a = lobeOf[id], b = lobeOf[other];
        if (a === b || a.id > b.id) return;
        var key = a.id + '\0' + b.id;
        (joined[key] || (joined[key] = { a: a, b: b, w: 0 })).w++;
      });
    });
    var bridges = Object.keys(joined).sort().map(function (key) { return joined[key]; });
    packClumps(lobes, bridges, content.length, aspect, owner);
    lobes.forEach(function (lobe) {
      var x = 0, y = 0;
      bridges.forEach(function (edge) {
        if (edge.a !== lobe && edge.b !== lobe) return;
        var there = edge.a === lobe ? edge.b : edge.a, dx = there.x - lobe.x, dy = there.y - lobe.y, d = Math.max(1, Math.hypot(dx, dy));
        x += dx / d * edge.w; y += dy / d * edge.w;
      });
      lobe.axis = x || y ? Math.atan2(y, x) : unitHash(lobe.id + '/axis') * FULL_TURN;
    });
    lobes.forEach(function (lobe, index) { placeLobe(lobe, index, lobeOf, near, radii, positions); });

    if (owner) { positions['grove-self'] = { x: 0, y: 0 }; primary['grove-self'] = null; radii['grove-self'] = 24; }
    // One slow wave over the whole map bends the lobes out of their plain discs.
    Object.keys(positions).forEach(function (id) {
      var p = positions[id], x = p.x, y = p.y;
      p.x = x + 22 * Math.sin(y / 170 + 0.7) + 11 * Math.sin(y / 67 + 2.1);
      p.y = y + 22 * Math.sin(x / 190 + 1.9) + 11 * Math.sin(x / 73 + 0.3);
    });
    return { positions: positions, primary: primary, radii: radii };
  }

  var api = { placeNew: placeNew, organicView: organicView };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.GroveLayoutKeep = api;
})(typeof window !== 'undefined' ? window : null);
