"""Write web/checks/fixture.synthetic.json: an invented world for the web checks.

Standard library only; runs under Python 3.9 and 3.12; deterministic (a fixed seed and fixed
dates, no clock). Every name is invented. The shape follows the sim fixture the checks were
written for: elements (nodes and edges in the views' graph shape), stats, time, history,
schema, source_node_ids for the four sources, health, story_date, generated_at, version and
person_id. Node data keys and edge data keys are exactly the ones GET /v1/views/graph serves.

Usage: python3 web/checks/make_fixture.py           (writes the file)
       python3 web/checks/make_fixture.py --check   (exit 1 when the file is not up to date)
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent / "fixture.synthetic.json"
STORY_DATE = date(2026, 10, 28)
FIRST_DAY = date(2026, 8, 3)
GENERATED_AT = "2026-10-28T09:00:00Z"
MASK = (1 << 64) - 1

NODE_KEYS = (
    "id", "label", "type", "summary", "status", "file_path", "valid_at", "invalid_at",
    "created_at", "updated_at", "status_since", "verified_at", "version", "properties",
    "has_changes",
)
EDGE_KEYS = (
    "id", "source", "target", "label", "verb", "cardinality", "canonical", "canonical_of",
    "valid_at", "invalid_at", "note", "source_field",
)
SOURCES = ("conversations", "calendar", "chats", "notes")

FIRST_NAMES = (
    "Maya", "Iris", "Teodor", "Priya", "Jonas", "Aiko", "Samir", "Lena", "Owen", "Noor",
    "Rafael", "Greta", "Kofi", "Elif", "Mateo", "Ingrid", "Tomas", "Yara", "Felix", "Hana",
    "Dario", "Sunniva", "Ravi", "Clara", "Bruno", "Mirela", "Idris", "Wren", "Cosmin", "Lotte",
)
LAST_NAMES = (
    "Chen", "Okafor", "Lindqvist", "Raman", "Ferreira", "Brandt", "Haddad", "Novak", "Mbeki",
    "Valdes", "Sorensen", "Achterberg", "Kowal", "Duarte", "Ishikawa", "Moreau", "Bello",
    "Strand", "Ozturk", "Varga", "Quist", "Pellegrini", "Arnaud", "Delacroix", "Whitlow",
    "Ferrante", "Nakamura", "Oyelaran", "Brightwater", "Castellan",
)
ROLES = ("designer", "editor", "baker", "analyst", "producer", "photographer", "planner",
         "teacher", "engineer", "writer", "buyer", "coach")
CITIES = ("Lisbon", "Leeds", "Tallinn", "Porto", "Ghent", "Bergen", "Lyon", "Utrecht")

COMPANIES = (
    ("Larkspur Studio", "design"), ("Quillfield Labs", "research"),
    ("Tidewater Bakery", "food"), ("Brightmoss Analytics", "data"),
    ("Ferncliff Outfitters", "retail"), ("Copperleaf Press", "publishing"),
    ("Harborline Freight", "logistics"), ("Juniper Row Clinic", "health"),
)
CLIENTS = (
    "Ostrava Tea House", "Pinecrest Library", "Saltmarsh Rowing Club", "Meridian Florist",
    "Kestrel Bike Works", "Alder Hill School",
)
PROJECTS = (
    "Spring catalogue", "Harbor Lights relaunch", "Atlas onboarding guide", "Field notes app",
    "Winter market stall", "Studio website refresh", "Delivery route planner", "Recipe archive",
    "Volunteer rota", "Podcast season two", "Garden workshop series", "Brand photo shoot",
    "Inventory clean-up", "Reading circle",
)
TASK_VERBS = ("Draft", "Review", "Book", "Order", "Write", "Test", "Plan", "Send", "Sort",
              "Print", "Update", "Check")
TASK_OBJECTS = ("the supplier email", "the venue", "the shot list", "the first chapter",
                "the price sheet", "the route map", "the sign-up form", "the newsletter",
                "the sample boxes", "the welcome pack", "the budget sheet", "the poster")
DECISIONS = (
    "Move the launch to March", "Keep the paper menu", "Hire a part-time editor",
    "Use one supplier for flour", "Publish every second Friday", "Close the stall in January",
    "Print the catalogue on recycled stock", "Run workshops on Saturdays",
    "Drop the night delivery slot", "Record the podcast in the studio",
    "Share the rota a month ahead", "Shoot the photos outdoors", "Count stock every Monday",
    "Meet the reading circle online",
)
EVENT_KINDS = ("Kick-off call", "Supplier visit", "Market day", "Quarterly review",
               "Workshop rehearsal", "Planning session", "Photo day", "Team lunch", "Open evening")
OBSERVATIONS = (
    "Deliveries run late on Fridays", "Customers ask for gift cards",
    "The shared drive is hard to search", "Sign-ups rise after each newsletter",
    "Weekend workshops fill first", "Printing costs went up this quarter",
    "Volunteers prefer short shifts", "Listeners finish the shorter episodes",
    "Rainy days cut market sales in half", "New starters ask the same five questions",
    "The route planner saves an hour a day", "Photo shoots overrun after lunch",
    "Stock counts drift by the end of the month", "Readers like a printed reminder",
    "Suppliers answer faster by phone", "Clients reply within a day in the morning",
)
PATTERNS = (
    "Plans slip when the brief is vague", "Small pilots beat big launches",
    "Decisions stick when one person owns them", "Busy weeks follow every market day",
    "Clear checklists cut repeat questions", "Work waits on one late supplier",
    "Short meetings end with clearer owners", "Feedback arrives right after events",
)

STATUS = {
    "person": ("active",), "company": ("active",), "client": ("active",),
    "project": ("active", "active", "active", "paused", "completed"),
    "task": ("open", "open", "done", "blocked"),
    "decision": ("current", "current", "current", "superseded"),
    "event": ("past", "past", "planned"),
    "observation": ("current",), "pattern": ("current",),
}
OPTIONAL_PROPS = {
    "person": ("role", "city"), "company": ("sector", "city"), "client": ("since",),
    "project": ("due", "budget"), "task": ("priority", "due"), "decision": ("why",),
    "event": ("where", "day"), "observation": ("seen",), "pattern": ("strength",),
}


class Rand:
    """A 64-bit linear congruential generator: the same numbers on every Python version."""

    def __init__(self, seed):
        self.state = seed & MASK

    def below(self, n):
        self.state = (self.state * 6364136223846793005 + 1442695040888963407) & MASK
        return (self.state >> 33) % n

    def pick(self, seq):
        return seq[self.below(len(seq))]

    def chance(self, percent):
        return self.below(100) < percent


def instant(day, hour=9):
    return day.isoformat() + "T%02d:00:00Z" % hour


def build():
    rand = Rand(20261028)
    nodes, edges = [], []
    by_id = {}

    def node(prefix, kind, label, summary, props):
        nid = "%s%03d" % (prefix, sum(1 for n in nodes if n["type"] == kind) + 1)
        span = (STORY_DATE - FIRST_DAY).days
        start = FIRST_DAY + timedelta(days=rand.below(span - 7))
        touched = start + timedelta(days=rand.below((STORY_DATE - start).days + 1))
        status = rand.pick(STATUS[kind])
        ended = status in ("completed", "superseded")
        version = 1 + rand.below(3)
        data = {
            "id": nid,
            "label": label,
            "type": kind,
            "summary": summary,
            "status": status,
            "file_path": "",
            "valid_at": instant(start),
            "invalid_at": instant(touched, 17) if ended else None,
            "created_at": instant(start),
            "updated_at": instant(touched, 12),
            "status_since": instant(touched, 12) if status != STATUS[kind][0] else None,
            "verified_at": instant(touched, 15) if rand.chance(40) else None,
            "version": version,
            "properties": props,
            "has_changes": version > 1,
        }
        nodes.append(data)
        by_id[nid] = data
        return nid

    def edge(source, target, verb):
        later = max(by_id[source]["valid_at"], by_id[target]["valid_at"])
        ended = by_id[source]["invalid_at"] or by_id[target]["invalid_at"]
        edges.append({
            "id": "r%04d" % (len(edges) + 1),
            "source": source,
            "target": target,
            "label": verb,
            "verb": verb,
            "cardinality": "many",
            "canonical": False,
            "canonical_of": None,
            "valid_at": later,
            "invalid_at": ended,
            "note": "",
            "source_field": "store",
        })

    people = []
    for i in range(30):
        first, last = FIRST_NAMES[i], LAST_NAMES[(i * 7) % len(LAST_NAMES)]
        role, city = ROLES[i % len(ROLES)], CITIES[(i * 3) % len(CITIES)]
        people.append(node("p", "person", first + " " + last,
                           "%s %s works as a %s in %s." % (first, last, role, city),
                           {"role": role, "city": city}))
    companies = [node("c", "company", name, "%s is a small %s business." % (name, sector),
                      {"sector": sector, "city": CITIES[i % len(CITIES)]})
                 for i, (name, sector) in enumerate(COMPANIES)]
    clients = [node("cl", "client", name, name + " buys from the studio network.",
                    {"since": str(2019 + i)}) for i, name in enumerate(CLIENTS)]
    projects = [node("pr", "project", name, name + " is one of the year's main pieces of work.",
                     {"due": instant(STORY_DATE + timedelta(days=14 * (i + 1)))[:10],
                      "budget": str(1000 * (i % 5 + 1))})
                for i, name in enumerate(PROJECTS)]

    for i, person in enumerate(people):
        edge(person, companies[i % len(companies)], "works at")
    for i in range(24):
        a, b = people[rand.below(len(people))], people[rand.below(len(people))]
        if a != b:
            edge(a, b, "knows")
    for i, project in enumerate(projects):
        edge(project, companies[i % len(companies)], "part of")
        edge(people[(i * 2) % len(people)], project, "leads")
        if i % 2 == 0:
            edge(project, clients[(i // 2) % len(clients)], "made for")
    for i, client in enumerate(clients):
        edge(client, companies[(i * 3) % len(companies)], "client of")

    tasks = []
    for i in range(60):
        project = projects[i % len(projects)]
        label = "%s %s" % (TASK_VERBS[i % len(TASK_VERBS)], TASK_OBJECTS[(i * 5) % len(TASK_OBJECTS)])
        task = node("t", "task", label, label + " for " + by_id[project]["label"] + ".",
                    {"priority": rand.pick(("low", "normal", "high")),
                     "due": instant(STORY_DATE + timedelta(days=rand.below(40)))[:10]})
        tasks.append(task)
        edge(task, project, "part of")
        edge(task, people[rand.below(len(people))], "assigned to")
        same = [t for j, t in enumerate(tasks[:-1]) if j % len(projects) == i % len(projects)]
        if same and rand.chance(30):
            edge(task, rand.pick(same), "depends on")
        elif same and rand.chance(12):
            edge(task, rand.pick(same), "blocks")

    for i in range(28):
        project = projects[i % len(projects)]
        label = DECISIONS[i % len(DECISIONS)] + ("" if i < len(DECISIONS) else " for now")
        decision = node("d", "decision", label,
                        "Agreed while planning " + by_id[project]["label"] + ".",
                        {"why": rand.pick(("cost", "time", "quality", "feedback"))})
        edge(decision, project, "decided for")
        edge(decision, people[rand.below(len(people))], "owned by")

    for i in range(36):
        project = projects[(i * 3) % len(projects)]
        day = FIRST_DAY + timedelta(days=(i * 2) % (STORY_DATE - FIRST_DAY).days)
        label = "%s: %s" % (EVENT_KINDS[i % len(EVENT_KINDS)], by_id[project]["label"])
        event = node("e", "event", label, "Held on " + day.isoformat() + ".",
                     {"where": CITIES[i % len(CITIES)], "day": day.isoformat()})
        edge(event, project, "about")
        for k in range(1 + rand.below(3)):
            edge(event, people[(i + k * 5) % len(people)], "involves")

    for i in range(32):
        where = projects[i % len(projects)] if i % 3 else companies[i % len(companies)]
        label = OBSERVATIONS[i % len(OBSERVATIONS)] + ("" if i < len(OBSERVATIONS) else " again")
        observation = node("o", "observation", label, label + ".",
                           {"seen": rand.pick(("once", "weekly", "often"))})
        edge(observation, where, "noticed in")
        if rand.chance(50):
            edge(observation, people[rand.below(len(people))], "mentions")

    for i, label in enumerate(PATTERNS):
        pattern = node("pt", "pattern", label, label + ".",
                       {"strength": rand.pick(("weak", "clear", "strong"))})
        for k in range(2 + rand.below(2)):
            edge(pattern, projects[(i * 2 + k * 3) % len(projects)], "seen in")

    shares = {
        "conversations": {"project": 100, "company": 100, "decision": 100, "person": 70,
                          "task": 60, "observation": 100, "pattern": 100, "event": 30,
                          "client": 50},
        "calendar": {"event": 100, "person": 60, "project": 50, "decision": 40, "task": 20},
        "chats": {"person": 100, "decision": 70, "event": 60, "task": 50, "client": 100,
                  "project": 40, "observation": 40},
        "notes": {"project": 100, "company": 100, "task": 70, "observation": 70, "pattern": 100,
                  "decision": 50, "client": 100, "person": 20},
    }
    source_node_ids = {
        source: [n["id"] for n in nodes if rand.chance(shares[source].get(n["type"], 0))]
        for source in SOURCES
    }

    by_type = {}
    for n in nodes:
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
    days = sorted({n["created_at"][:10] for n in nodes})
    history, day = [], FIRST_DAY
    while day <= STORY_DATE:
        stamp = day.isoformat()
        history.append({
            "date": stamp,
            "nodes": sum(1 for n in nodes if n["created_at"][:10] <= stamp),
            "edges": sum(1 for e in edges if e["valid_at"][:10] <= stamp),
        })
        day += timedelta(days=7)
    schema = {
        kind: {
            "required": [],
            "optional": list(OPTIONAL_PROPS[kind]),
            "status_field": "status",
            "status_vocab": sorted(set(STATUS[kind])),
        }
        for kind in sorted(STATUS)
    }
    world = {
        "elements": {
            "nodes": [{"data": {key: n[key] for key in NODE_KEYS}} for n in nodes],
            "edges": [{"data": {key: e[key] for key in EDGE_KEYS}} for e in edges],
        },
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "by_type": by_type,
            "embedded": len(nodes),
            "edge_verbs": sorted({e["verb"] for e in edges}),
        },
        "time": {"min": days[0], "max": STORY_DATE.isoformat()},
        "history": history,
        "schema": schema,
        "source_node_ids": source_node_ids,
        "health": {
            "status": "healthy",
            "summary": "Your Grove is ready",
            "generated_at": GENERATED_AT,
            "checks": {"graph": "ready", "sources": "ready"},
            "nodes": len(nodes),
            "edges": len(edges),
            "by_type": by_type,
            "overall": {"state": "healthy"},
        },
        "story_date": STORY_DATE.isoformat(),
        "generated_at": GENERATED_AT,
        "version": 1,
        "person_id": people[0],
    }
    ids = {n["id"] for n in nodes}
    assert len(ids) == len(nodes), "node ids must be unique"
    assert all(e["source"] in ids and e["target"] in ids for e in edges), "edge ends must exist"
    assert all(set(source_node_ids[s]) <= ids and source_node_ids[s] for s in SOURCES)
    return world


def render():
    return json.dumps(build(), ensure_ascii=False, indent=1) + "\n"


def main(argv):
    text = render()
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("fixture.synthetic.json is out of date: run python3 web/checks/make_fixture.py")
            return 1
        print("fixture.synthetic.json is up to date")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print("Wrote web/checks/%s (%d bytes)" % (OUT.name, len(text.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
