"""A synthetic MCP tool set for the flow checks: three tools over a belief list per world.

The data is invented: two fictional companies, Kestrel Looms and Marlow Ciderworks, and invented
names. A belief is bi-temporal: valid_from and valid_to say when it was true in the world, and
recorded_at says when Grove learned it. The three tools mirror the synthetic connector's recall,
as-of and retain:

- grove_recall (grove.read): the open beliefs whose topic holds the query, or whose statement holds
  one of its words.
- grove_as_of (grove.read): what was true about a topic on a date, and of that, what Grove knew on
  that date.
- grove_retain (grove.write): a new belief; it closes the open belief on the same topic (valid_to
  becomes the new valid_from), and deletes nothing.

Each handler reads and writes the list of the ToolContext's world only, and the book records the
world of every call, so a test can show that a token of one world never reaches another. The lists
live in process; they are test data, never store rows. TOOLS is the set for a local run
(docs/hosts.md).
"""

import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field

from grove import clock
from grove.api.errors import invalid_arguments
from grove.mcp.tools import Tool, ToolContext

READ = "grove.read"
WRITE = "grove.write"
RECALL = "grove_recall"
AS_OF = "grove_as_of"
RETAIN = "grove_retain"
TOOL_NAMES = (RECALL, AS_OF, RETAIN)
MAX_TEXT = 500
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# The first world: a fictional textile company. The price changed on 2026-10-12, and Grove learned
# it on 2026-10-15, so on 2026-10-13 the new price was true and Grove did not know it yet.
KESTREL_PREFIX = "b"
KESTREL_BELIEFS = (
    {
        "id": "b1",
        "topic": "pricing",
        "statement": "Kestrel Studio costs 40 USD per month.",
        "valid_from": "2026-09-01",
        "valid_to": "2026-10-12",
        "recorded_at": "2026-09-02",
    },
    {
        "id": "b2",
        "topic": "pricing",
        "statement": "Kestrel Studio costs 55 USD per month.",
        "valid_from": "2026-10-12",
        "valid_to": None,
        "recorded_at": "2026-10-15",
    },
    {
        "id": "b3",
        "topic": "launch",
        "statement": "Kestrel Teams launches on 2026-11-20.",
        "valid_from": "2026-10-01",
        "valid_to": None,
        "recorded_at": "2026-10-03",
    },
    {
        "id": "b4",
        "topic": "owner",
        "statement": "Noor Valdane owns the Kestrel pricing page.",
        "valid_from": "2026-08-15",
        "valid_to": None,
        "recorded_at": "2026-08-20",
    },
)

# The second world: a fictional cider maker, with its own pricing and launch.
MARLOW_PREFIX = "m"
MARLOW_BELIEFS = (
    {
        "id": "m1",
        "topic": "pricing",
        "statement": "A Marlow Ciderworks crate costs 18 GBP.",
        "valid_from": "2026-07-01",
        "valid_to": None,
        "recorded_at": "2026-07-02",
    },
    {
        "id": "m2",
        "topic": "launch",
        "statement": "The Marlow pear cider launches on 2026-12-05.",
        "valid_from": "2026-10-20",
        "valid_to": None,
        "recorded_at": "2026-10-21",
    },
)

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
_ADDS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}


@dataclass(frozen=True)
class Call:
    """One tool call as the book saw it: the tool, and the world of the token that made it."""

    tool: str
    world_id: str


@dataclass
class BeliefBook:
    """The belief list of each world, by world id. A world with no seed of its own starts with a
    copy of `default` (prefix, beliefs) at its first use, or with no belief when there is none."""

    default: tuple[str, tuple[dict, ...]] | None = None
    calls: list[Call] = field(default_factory=list)
    _worlds: dict[str, list[dict]] = field(default_factory=dict, init=False)
    _prefixes: dict[str, str] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def seed(self, world_id: str, prefix: str, beliefs: Iterable[dict]) -> None:
        with self._lock:
            self._worlds[world_id] = [dict(belief) for belief in beliefs]
            self._prefixes[world_id] = prefix

    def beliefs(self, world_id: str) -> list[dict]:
        """A copy of the world's list, in the order it was written."""
        with self._lock:
            return [dict(belief) for belief in self._held(world_id)]

    def _held(self, world_id: str) -> list[dict]:
        if world_id not in self._worlds:
            prefix, beliefs = self.default or ("n", ())
            self._worlds[world_id] = [dict(belief) for belief in beliefs]
            self._prefixes[world_id] = prefix
        return self._worlds[world_id]

    def _note(self, tool: str, world_id: str) -> list[dict]:
        self.calls.append(Call(tool, world_id))
        return self._held(world_id)

    def recall(self, world_id: str, query: str) -> dict:
        words = query.lower()
        with self._lock:
            held = self._note(RECALL, world_id)
            hits = [
                dict(belief)
                for belief in held
                if belief["valid_to"] is None
                and (
                    words in belief["topic"]
                    or any(word in belief["statement"].lower() for word in words.split())
                )
            ]
        return {"query": words, "beliefs": hits}

    def as_of(self, world_id: str, topic: str, date: str) -> dict:
        topic = topic.lower()
        with self._lock:
            held = self._note(AS_OF, world_id)
            true_then = [
                dict(belief)
                for belief in held
                if belief["topic"] == topic
                and belief["valid_from"] <= date
                and (belief["valid_to"] is None or date < belief["valid_to"])
            ]
        known_then = [belief for belief in true_then if belief["recorded_at"] <= date]
        return {
            "topic": topic,
            "date": date,
            "true_on_date": true_then,
            "known_by_grove_on_date": known_then,
        }

    def retain(self, world_id: str, topic: str, statement: str, valid_from: str | None) -> dict:
        topic = topic.lower()
        today = clock.now().date().isoformat()
        starts = valid_from or today
        with self._lock:
            held = self._note(RETAIN, world_id)
            closed = []
            for belief in held:
                if belief["topic"] == topic and belief["valid_to"] is None:
                    belief["valid_to"] = starts
                    closed.append(belief["id"])
            new = {
                "id": f"{self._prefixes[world_id]}{len(held) + 1}",
                "topic": topic,
                "statement": statement,
                "valid_from": starts,
                "valid_to": None,
                "recorded_at": today,
            }
            held.append(new)
        return {"stored": dict(new), "closed": closed}


def _text(arguments: dict, name: str, *, required: bool = True) -> str | None:
    value = arguments.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise invalid_arguments(f"{name} must be text of 1 to {MAX_TEXT} characters", name)
    return value.strip()


def _date(arguments: dict, name: str, *, required: bool = True) -> str | None:
    value = _text(arguments, name, required=required)
    if value is not None and _DATE.fullmatch(value) is None:
        raise invalid_arguments(f"{name} must be a date as YYYY-MM-DD", name)
    return value


def _schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def synthetic_tools(book: BeliefBook) -> tuple[Tool, ...]:
    """The three tools, in the order tools/list gives them, over `book`."""

    def recall(context: ToolContext, arguments: dict) -> dict:
        return book.recall(context.world_id, _text(arguments, "query") or "")

    def as_of(context: ToolContext, arguments: dict) -> dict:
        topic = _text(arguments, "topic") or ""
        return book.as_of(context.world_id, topic, _date(arguments, "date") or "")

    def retain(context: ToolContext, arguments: dict) -> dict:
        topic = _text(arguments, "topic") or ""
        statement = _text(arguments, "statement") or ""
        return book.retain(
            context.world_id, topic, statement, _date(arguments, "valid_from", required=False)
        )

    return (
        Tool(
            name=RECALL,
            title="Recall beliefs",
            description=(
                "Search Grove for the current beliefs about a topic or keyword. Call it before "
                "you answer a question about the person's company: its pricing, launches or owners."
            ),
            input_schema=_schema(
                {"query": {"type": "string", "description": "A topic or keywords, e.g. pricing."}},
                ["query"],
            ),
            annotations=dict(_READ_ONLY),
            scope=READ,
            handler=recall,
        ),
        Tool(
            name=AS_OF,
            title="Belief as of a date",
            description=(
                "What was true about a topic on a past date, and what Grove knew on that date."
            ),
            input_schema=_schema(
                {
                    "topic": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                ["topic", "date"],
            ),
            annotations=dict(_READ_ONLY),
            scope=READ,
            handler=as_of,
        ),
        Tool(
            name=RETAIN,
            title="Remember a new belief",
            description=(
                "Store a new fact or decision about the person's company. An open belief on the "
                "same topic is closed, not deleted."
            ),
            input_schema=_schema(
                {
                    "topic": {"type": "string"},
                    "statement": {"type": "string"},
                    "valid_from": {"type": "string", "description": "YYYY-MM-DD; today if absent"},
                },
                ["topic", "statement"],
            ),
            annotations=dict(_ADDS),
            scope=WRITE,
            handler=retain,
        ),
    )


# For a local run with Muse Code (docs/hosts.md): every world starts with the Kestrel beliefs.
TOOLS = synthetic_tools(BeliefBook(default=(KESTREL_PREFIX, KESTREL_BELIEFS)))
