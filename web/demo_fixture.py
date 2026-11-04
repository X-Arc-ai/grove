"""Assemble the public invented corpus without changing the small check fixture.

Run from a source checkout: python3 web/demo_fixture.py. The generated JSON lives
under web/ so the API image can build without the excluded web/checks directory.
The vocabulary and semantic relationships come only from the invented generator.
"""
import copy
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
RECORDING_DATE = date(2026, 11, 4)
GENERATED_AT = "2026-11-04T21:00:00Z"
GROUPS = 20


def source():
    spec = importlib.util.spec_from_file_location("invented_vocabulary", ROOT / "checks/make_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shift_dates(value, delta):
    if isinstance(value, dict):
        return {key: shift_dates(child, delta) for key, child in value.items()}
    if isinstance(value, list):
        return [shift_dates(child, delta) for child in value]
    if isinstance(value, str):
        return re.sub(r"\b2026-\d{2}-\d{2}\b", lambda match: (date.fromisoformat(match[0]) + delta).isoformat(), value)
    return value


def provider_plan(ids, provider):
    """The existing simulation chooses a deterministic 64 percent per account."""
    selected = set()
    for node_id in ids:
        value = 2166136261
        for char in provider + "|" + node_id:
            value = ((value ^ ord(char)) * 16777619) & 0xFFFFFFFF
        if value % 100 < 64:
            selected.add(node_id)
    return selected


def build():
    vocabulary = source()
    original = vocabulary.build()
    template = shift_dates(original, RECORDING_DATE - date.fromisoformat(original["story_date"]))
    world = copy.deepcopy(template)
    nodes, edges = [], []
    source_ids = {name: [] for name in template["source_node_ids"]}
    # Separate programmes retain their own people, projects, tasks and decisions.
    # No cross-programme links turn these meaningful groups into one dense hub.
    for group in range(GROUPS):
        programme = vocabulary.CITIES[group % len(vocabulary.CITIES)] + " " + vocabulary.PROJECTS[group % len(vocabulary.PROJECTS)]
        prefix = "demo%02d-" % group
        for wrapper in template["elements"]["nodes"]:
            node = copy.deepcopy(wrapper["data"])
            node["id"] = prefix + node["id"]
            node["label"] += " · " + programme
            node["summary"] = "Within the " + programme + " programme: " + node["summary"]
            node["properties"]["demo_group"] = programme
            nodes.append({"data": node})
        for wrapper in template["elements"]["edges"]:
            edge = copy.deepcopy(wrapper["data"])
            for key in ("id", "source", "target"):
                edge[key] = prefix + edge[key]
            edges.append({"data": edge})
        for name, ids in template["source_node_ids"].items():
            source_ids[name].extend(prefix + node_id for node_id in ids)
    # Each conversation demonstration can reveal a rich, complete example world.
    # The existing engine still selects its own provider subset and times growth.
    source_ids["conversations"] = [wrapper["data"]["id"] for wrapper in nodes]
    world["elements"] = {"nodes": nodes, "edges": edges}
    world["source_node_ids"] = source_ids
    world["person_id"] = "demo00-" + template["person_id"]
    world["story_date"] = RECORDING_DATE.isoformat()
    world["generated_at"] = GENERATED_AT
    by_type = {kind: count * GROUPS for kind, count in template["stats"]["by_type"].items()}
    world["stats"].update(total_nodes=len(nodes), total_edges=len(edges), embedded=len(nodes), by_type=by_type)
    world["health"].update(nodes=len(nodes), edges=len(edges), by_type=by_type, generated_at=GENERATED_AT)
    for point in world["history"]:
        point["nodes"] *= GROUPS
        point["edges"] *= GROUPS
    for schema in world["schema"].values():
        schema["optional"].append("demo_group")
    return world


if __name__ == "__main__":
    world = build()
    data = (json.dumps(world, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    (ROOT / "demo.synthetic.json").write_bytes(data)
    plans = {}
    for provider in ("claude", "chatgpt"):
        ids = provider_plan(world["source_node_ids"]["conversations"], provider)
        plans[provider] = {
            "nodes": len(ids),
            "edges": sum(edge["data"]["source"] in ids and edge["data"]["target"] in ids for edge in world["elements"]["edges"]),
        }
    print(json.dumps({"artifact": "web/demo.synthetic.json", "bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest(), "groups": GROUPS,
                      "nodes": world["stats"]["total_nodes"], "edges": world["stats"]["total_edges"],
                      "provider_plans": plans, "recording_date": world["story_date"]}, indent=2))
