"""The Claude import preview: a sim-sized graph cut from the invented demo corpus.

The reference simulation shows one Claude import as 384 nodes and about 696 edges: six large hubs, a few
people, and many small memories. The demo corpus has 4,440 nodes in 20 unlinked stories, and most
of its small nodes link only to people outside any cut, so links are drawn here: each node of the
cut keeps its real label and type, and hangs from its own story's hub and people. Every value is
invented. Standard library only, Python 3.9, and deterministic for the same corpus.
"""
import hashlib

# The order in which an import reveals types (the sim's SOURCE_TYPE_ORDER for conversations).
REVEAL = ("project", "company", "decision", "person", "task", "observation", "pattern", "event", "client")
STORIES = 6
# One story's 64 nodes: six stories make 384. Its hub is a project or a company, in turn.
PER_STORY = {"decision": 1, "person": 3, "task": 5, "observation": 19, "pattern": 2, "event": 32, "client": 1}
VERBS = {"decision": "decided for", "task": "part of", "observation": "noticed in", "pattern": "seen in",
         "event": "involves", "client": "owned by", "person": "works at"}
# The preview adds one link from the person to each hub, so the total is the sim's 696.
TARGET_EDGES = 690


def rank(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def link(edges, seen, source, target, verb, stamp):
    pair = (source, target) if source < target else (target, source)
    if source == target or pair in seen:
        return
    seen.add(pair)
    edges.append({"data": {"id": "preview-r" + str(len(edges)), "source": source, "target": target,
                           "label": verb, "verb": verb, "cardinality": "many", "canonical": False,
                           "canonical_of": None, "valid_at": stamp, "invalid_at": None, "note": "",
                           "source_field": "store"}})


def cut(corpus):
    """A copy of the corpus with the preview's nodes and links, in reveal order."""
    by_story = {}
    for node in corpus["elements"]["nodes"]:
        by_story.setdefault(node["data"]["id"].split("-")[0], []).append(node)
    nodes, edges, seen, stories = [], [], set(), []
    for index, story in enumerate(sorted(by_story)[:STORIES]):
        rows = sorted(by_story[story], key=lambda row: row["data"]["id"])
        typed = lambda kind: [row for row in rows if row["data"]["type"] == kind]
        hub = typed("company" if index % 2 else "project")[0]
        chosen = {kind: typed(kind)[:count] for kind, count in PER_STORY.items()}
        nodes += [hub] + [row for group in chosen.values() for row in group]
        stories.append((hub, chosen))
    for hub, chosen in stories:
        hub_id, people = hub["data"]["id"], [row["data"]["id"] for row in chosen["person"]]
        for kind, rows in chosen.items():
            for row in rows:
                link(edges, seen, row["data"]["id"], hub_id, VERBS[kind], row["data"]["created_at"])
    # The second link of a small node goes to one of its story's people, in a stable order.
    pending = []
    for hub, chosen in stories:
        people = [row["data"]["id"] for row in chosen["person"]]
        for kind, rows in chosen.items():
            if kind != "person":
                pending += [(rank(row["data"]["id"]), row, people, kind) for row in rows]
    for key, row, people, kind in sorted(pending, key=lambda item: item[0]):
        if len(edges) >= TARGET_EDGES:
            break
        link(edges, seen, row["data"]["id"], people[int(key, 16) % len(people)], VERBS[kind],
             row["data"]["created_at"])
    nodes.sort(key=lambda row: (REVEAL.index(row["data"]["type"]), row["data"]["id"]))
    by_type = {}
    for row in nodes:
        by_type[row["data"]["type"]] = by_type.get(row["data"]["type"], 0) + 1
    verbs = {}
    for row in edges:
        verbs[row["data"]["verb"]] = verbs.get(row["data"]["verb"], 0) + 1
    ids = [row["data"]["id"] for row in nodes]
    value = dict(corpus)
    value["elements"] = {"nodes": nodes, "edges": edges}
    value["source_node_ids"] = {"conversations": ids}
    value["stats"] = {"total_nodes": len(nodes), "total_edges": len(edges), "by_type": by_type,
                      "embedded": len(nodes), "edge_verbs": verbs}
    return value
