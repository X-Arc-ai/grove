"""Fixed invented stories, not a general-purpose import format.

The operator accepts no replacement corpus. The digest pins every word and link;
changing this version requires an explicit new reviewed fixture identity.
"""

import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from hashlib import sha256

VERSION = "rich-demo-v1"
PARSER = "synthetic-demo-1"
ACTOR = "synthetic-demo:rich-demo-v1"
SOURCE_TIME = "2026-10-20T12:00:00+00:00"
RECHECK_TIME = "2027-01-20T12:00:00+00:00"


@dataclass(frozen=True)
class Item:
    key: str
    kind: str
    title: str
    summary: str
    status: str | None = None


@dataclass(frozen=True)
class Link:
    source: str
    meaning: str
    target: str


@dataclass(frozen=True)
class Story:
    key: str
    topic: str
    items: tuple[Item, ...]
    links: tuple[Link, ...]

    def messages(self) -> tuple[dict, ...]:
        titles = {i.key: i.title for s in stories() for i in s.items}
        return tuple(
            {
                "key": item.key,
                "sender": None,
                "at": SOURCE_TIME,
                "text": item.summary
                + " Connections: "
                + "; ".join(
                    f"{link.meaning} {titles[link.target]}"
                    for link in self.links
                    if link.source == item.key
                ),
            }
            for item in self.items
        )

    def content(self) -> str:
        return json.dumps(self.messages(), ensure_ascii=False, sort_keys=True)

    def digest(self) -> str:
        return sha256(canonical({"story": asdict(self), "messages": self.messages()})).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


# These places, organisations and people are invented. The domains provide
# distinct problems and evidence, rather than a sequence of numbered nodes.
_PLACES = ("Amber Quay", "Bramble Reach", "Cobalt Vale", "Dapple Cove", "Ember Hollow")
_DOMAINS = (
    ("Lantern", "night library", "book lockers", "readers", "loan slips", "quiet access"),
    ("Moss", "seed exchange", "seed trays", "gardeners", "germination cards", "dry storage"),
    ("Tern", "harbour repair", "tool benches", "boat crews", "repair logs", "safe access"),
    ("Willow", "community kitchen", "meal crates", "cooks", "menu cards", "allergy labels"),
    ("Flint", "repair cafe", "spare parts", "repairers", "fault sheets", "tool availability"),
    (
        "Saffron",
        "oral history",
        "audio booths",
        "storytellers",
        "consent cards",
        "clear recordings",
    ),
    ("Pebble", "walking trail", "route markers", "walkers", "survey maps", "step-free routes"),
    ("Velvet", "textile studio", "fabric rolls", "weavers", "pattern cards", "material reuse"),
)
_PEOPLE = ("Mira Venn", "Oren Pell", "Tavi Noll", "Lysa Fen")
_WORKSTREAMS = ("access", "supplies", "training", "evaluation")


@lru_cache(maxsize=1)
def stories() -> tuple[Story, ...]:
    result = []
    for domain, activity, material, audience, records, priority in _DOMAINS:
        for place in _PLACES:
            topic = f"{domain} {place}"
            key = topic.lower().replace(" ", "-")
            items: list[Item] = []

            def add(
                suffix,
                kind,
                title,
                detail,
                status=None,
                key=key,
                items=items,
                activity=activity,
                place=place,
            ):
                item_key = f"{key}:{suffix}"
                items.append(
                    Item(
                        item_key,
                        kind,
                        title,
                        f"Synthetic demo data. {title}. In the invented {activity} program at "
                        f"{place}, {detail}",
                        status,
                    )
                )
                return item_key

            people = [
                add(
                    f"person-{j}",
                    "person",
                    f"{name} of {topic}",
                    f"{name} coordinates {_WORKSTREAMS[j]} for {audience}.",
                )
                for j, name in enumerate(_PEOPLE)
            ]
            orgs = [
                add(
                    "cooperative",
                    "organisation",
                    f"{topic} Cooperative",
                    f"the cooperative maintains {material} and supports {audience}.",
                    "active",
                ),
                add(
                    "circle",
                    "organisation",
                    f"{topic} Review Circle",
                    f"the circle checks {records} and protects {priority}.",
                    "active",
                ),
            ]
            plans = [
                add(
                    f"plan-{j}",
                    "plan",
                    f"{topic} {stream} plan",
                    f"the {stream} plan opens a small {activity} pilot on 12 November 2026; "
                    f"the review uses {records} and a limit of twelve participants.",
                    "active",
                )
                for j, stream in enumerate(_WORKSTREAMS)
            ]
            actions = (
                ("Map", f"the entrances for {audience}"),
                ("Inspect", f"the first delivery of {material}"),
                ("Draft", f"the induction guide for {audience}"),
                ("Compare", f"the baseline {records}"),
                ("Label", f"the shared {material}"),
                ("Test", f"the route to the {activity} desk"),
                ("Arrange", f"a practice session with {audience}"),
                ("Review", f"the missing fields on {records}"),
                ("Measure", f"the shelf space for {material}"),
                ("Book", f"a second orientation for {audience}"),
                ("Translate", f"the signs for the {activity}"),
                ("Prepare", f"a summary from the first {records}"),
                ("Check", f"the reserve stock of {material}"),
                ("Collect", f"access notes from {audience}"),
                ("Rehearse", f"the welcome at the {activity} desk"),
                ("Publish", f"the blank template for {records}"),
                ("Repair", f"the carrying cases for {material}"),
                ("Confirm", f"the quiet-hour request from {audience}"),
                ("Design", f"the handover for the {activity} pilot"),
                ("Audit", f"the archived {records}"),
            )
            tasks = [
                add(
                    f"task-{j}",
                    "task",
                    f"{topic}: {verb} {thing}",
                    f"the team must {verb.lower()} {thing} before 10 November 2026. "
                    f"The acceptance note must mention {priority}.",
                    "blocked" if j in (1, 8) else "in progress" if j % 3 == 0 else "open",
                )
                for j, (verb, thing) in enumerate(actions)
            ]
            choices = (
                "Use a twelve-place pilot before expansion",
                "Reserve the first hour for new participants",
                "Keep a paper copy beside the digital record",
                "Require a second check before public display",
                "Reuse the existing shelves before procurement",
                "Review access notes at the weekly meeting",
            )
            decisions = [
                add(
                    f"decision-{j}",
                    "decision",
                    f"{topic}: {choice}",
                    f"the fictional team chose this rule on {14 + j} October 2026 "
                    f"to support {priority}.",
                )
                for j, choice in enumerate(choices)
            ]
            occasions = (
                "site visit",
                "materials review",
                "access workshop",
                "guide rehearsal",
                "feedback circle",
                "pilot briefing",
            )
            events = [
                add(
                    f"event-{j}",
                    "event",
                    f"{topic} {occasion}",
                    f"a fictional {occasion} took place on {10 + j} October 2026 at 10:00 UTC. "
                    f"Participants compared {records} and agreed to preserve {priority}.",
                )
                for j, occasion in enumerate(occasions)
            ]
            findings = (
                "The entrance sign is hard to read from the footpath",
                f"Two containers of {material} need clearer labels",
                f"New {audience} prefer a short guided introduction",
                f"The current {records} omit the handover date",
                "The spare shelf can hold the pilot stock",
            )
            observations = [
                add(
                    f"observation-{j}",
                    "observation",
                    f"{topic}: {finding}",
                    f"the fictional site notes dated 18 October 2026 record this observation. "
                    f"It informs the {activity} pilot.",
                )
                for j, finding in enumerate(findings)
            ]
            assumptions = [
                add(
                    f"assumption-{j}",
                    "assumption",
                    f"{topic}: {claim}",
                    f"the team treats this as an unconfirmed working assumption for {activity}. "
                    f"The next review must compare {records} with participant feedback.",
                    "holds",
                )
                for j, claim in enumerate(
                    (
                        "Twelve places are enough for the first trial",
                        "The spare shelf stays available through November",
                        "A short induction removes most access questions",
                        "Paper records remain useful during the pilot",
                        "A weekly review can detect supply gaps",
                    )
                )
            ]
            links = []

            def connect(src, triples, links=links):
                links.extend(Link(src, meaning, target) for meaning, target in triples)

            for j, src in enumerate(people):
                connect(
                    src, (("works at", orgs[0]), ("member of", orgs[1]), ("related to", plans[j]))
                )
            for j, src in enumerate(orgs):
                connect(
                    src,
                    (
                        ("related to", orgs[1 - j]),
                        ("related to", plans[0]),
                        ("related to", plans[1]),
                    ),
                )
            for j, src in enumerate(plans):
                connect(
                    src,
                    (
                        ("owned by", orgs[0]),
                        ("assigned to", people[j]),
                        ("depends on", assumptions[j]),
                    ),
                )
            for j, src in enumerate(tasks):
                connect(
                    src,
                    (
                        ("assigned to", people[j % 4]),
                        ("step of", plans[j % 4]),
                        ("depends on", assumptions[j % 5]),
                    ),
                )
            for j, src in enumerate(decisions + events + observations):
                connect(
                    src,
                    (
                        ("involves", people[j % 4]),
                        ("part of", plans[j % 4]),
                        ("related to", tasks[j % 20]),
                    ),
                )
            for j, src in enumerate(assumptions):
                connect(
                    src,
                    (
                        ("owned by", orgs[0]),
                        ("related to", plans[j % 4]),
                        ("related to", observations[j]),
                    ),
                )
            if result:
                links.append(Link(plans[0], "related to", result[-1].items[6].key))
            result.append(Story(key, topic, tuple(items), tuple(links)))
    return tuple(result)


def digest() -> str:
    return sha256(canonical([story.digest() for story in stories()])).hexdigest()


def manifest() -> dict:
    corpus = stories()
    return {
        "version": VERSION,
        "sha256": digest(),
        "stories": len(corpus),
        "items": sum(len(s.items) for s in corpus),
        "links": sum(len(s.links) for s in corpus),
        "synthetic": True,
    }


def request_identity(world_id: str, story_key: str) -> str:
    from grove.demo_policy import DemoDenied

    if not any(story.key == story_key for story in stories()):
        raise DemoDenied()
    return VERSION + ":" + sha256(canonical([world_id, story_key])).hexdigest()


def content_identity(story_key: str) -> str:
    from grove.demo_policy import DemoDenied, require_corpus_identity

    story = next((story for story in stories() if story.key == story_key), None)
    if story is None:
        raise DemoDenied()
    return sha256(
        canonical([VERSION, require_corpus_identity(), story.key, story.digest()])
    ).hexdigest()


def topic_items(about: str | None, maximum: int = 32) -> tuple[str, ...]:
    """Rank the fixed vocabulary locally; database capture uses only these exact titles.

    No private title or entire graph crosses the connection to rank a query.
    """
    words = set(re.findall(r"[a-z0-9]+", (about or "").lower()))
    stop = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "in",
        "of",
        "for",
        "to",
        "what",
        "which",
        "show",
        "me",
        "tell",
        "about",
        "and",
        "demo",
        "synthetic",
        "data",
        "saved",
    }
    words -= stop
    kind_words = {
        "tasks": "task",
        "plans": "plan",
        "projects": "plan",
        "assumptions": "assumption",
        "observations": "observation",
        "decisions": "decision",
        "decide": "decision",
        "events": "event",
        "people": "person",
        "organizations": "organisation",
        "organisations": "organisation",
    }
    requested_kinds = {kind_words.get(word, word) for word in words}
    if not words:
        return ()
    ranked = []
    for story in stories():
        topic = set(re.findall(r"[a-z0-9]+", story.topic.lower()))
        for item in story.items:
            title = set(re.findall(r"[a-z0-9]+", item.title.lower()))
            score = 10 * len(words & topic) + len(words & title)
            if item.kind in requested_kinds and score:
                score += 6
            if score:
                ranked.append((-score, item.key, item.title))
    return tuple(row[2] for row in sorted(ranked)[:maximum])
