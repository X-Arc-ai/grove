"""The registry as the writer reads it inside its transaction, or a reader through a WorldQuery:
kinds, meanings, aliases, property names, and the status vocabulary of each kind.

The primitive rows live in world '' and a WorldTx cannot read them, so their fields come from
grove.store.primitives (they never change). A caller may not create an item of the belief kind or
a link of a primitive meaning: the beliefs lane and the composite operations mint those.

The resolution order is grove.writer.resolver's. The methods here are its strict form for the
writer: what does not resolve to one usable word is refused, and an ambiguous phrase is never
picked (rule ambiguous_phrase).
"""

import json
from dataclasses import dataclass
from typing import Any

from grove.store.current import TERMINAL_ROLES, read_rows
from grove.store.normalise import normalise_phrase
from grove.store.primitives import (
    KIND_BELIEF,
    PRIMITIVE_KIND_BELIEF,
    PRIMITIVE_MEANING_IDS,
    PRIMITIVE_MEANINGS,
)
from grove.writer.errors import Refused
from grove.writer.resolver import AMBIGUOUS, Resolution, Source, resolve_word, usable

REPORTED_DONE = "reported done"
_REGISTRY_TARGETS = ("kind", "meaning", "property_name")


def _unresolved(found: Resolution, code: str, word: str, valid: list[str]) -> Refused:
    if found.outcome == AMBIGUOUS:
        return Refused(
            code,
            "ambiguous_phrase",
            f"{found.phrase!r} names more than one {word}: send one of them by name",
            {"candidates": list(found.candidates), "valid": valid},
        )
    return Refused(code, code, f"no {word} {found.phrase!r}", {"valid": valid})


@dataclass
class Registry:
    kinds: dict[str, dict[str, Any]]
    meanings: dict[str, dict[str, Any]]
    aliases: list[dict[str, Any]]
    property_names: list[dict[str, Any]]

    @classmethod
    def load(cls, source: Source) -> "Registry":
        """From a WorldTx inside the batch's transaction, or from a WorldQuery for a reader."""
        kinds = {row["k"]["id"]: row["k"] for row in read_rows(source, "MATCH (k:Kind) RETURN k")}
        meanings = {
            row["m"]["id"]: row["m"] for row in read_rows(source, "MATCH (m:Meaning) RETURN m")
        }
        aliases = [
            row["a"]
            for row in read_rows(
                source,
                "MATCH (a:Alias) WHERE a.target_type IN $types RETURN a",
                types=list(_REGISTRY_TARGETS),
            )
        ]
        names = [row["p"] for row in read_rows(source, "MATCH (p:PropertyName) RETURN p")]
        return cls(kinds, meanings, aliases, names)

    # Kinds ------------------------------------------------------------------------------------

    def kind(self, kind_id: str) -> dict[str, Any]:
        if kind_id == KIND_BELIEF:
            return {"id": KIND_BELIEF, **PRIMITIVE_KIND_BELIEF}
        return self.kinds[kind_id]

    def resolve_kind(self, ref: str) -> dict[str, Any]:
        wanted = normalise_phrase(ref)
        if ref == KIND_BELIEF or wanted == normalise_phrase(str(PRIMITIVE_KIND_BELIEF["name"])):
            raise Refused(
                "unknown_kind",
                "primitive_kind",
                "a belief is minted by the beliefs operations, not by create_item",
                {"valid": self.kind_names()},
            )
        found = resolve_word(self, "kind", ref)
        if found.target is None:
            raise _unresolved(found, "unknown_kind", "kind", self.kind_names())
        return found.target

    def kind_names(self) -> list[str]:
        return sorted(row["name"] for row in self.kinds.values() if usable(row))

    # Meanings ---------------------------------------------------------------------------------

    def meaning(self, meaning_id: str) -> dict[str, Any]:
        if meaning_id in PRIMITIVE_MEANINGS:
            return {"id": meaning_id, **PRIMITIVE_MEANINGS[meaning_id]}
        return self.meanings[meaning_id]

    def meaning_names(self) -> list[str]:
        return sorted(row["name"] for row in self.meanings.values() if usable(row))

    def resolve_meaning(self, ref: str) -> tuple[dict[str, Any], bool]:
        """(the meaning, whether the phrase was a reversed alias: the caller swaps the ends)."""
        wanted = normalise_phrase(ref)
        primitive_names = {
            normalise_phrase(str(row["name"])) for row in PRIMITIVE_MEANINGS.values()
        }
        if ref in PRIMITIVE_MEANING_IDS or wanted in primitive_names:
            raise Refused(
                "unknown_meaning",
                "primitive_meaning",
                "a primitive meaning is written only by the writer's composite operations",
                {"valid": self.meaning_names()},
            )
        found = resolve_word(self, "meaning", ref)
        if found.target is None:
            raise _unresolved(found, "unknown_meaning", "meaning", self.meaning_names())
        return found.target, found.reversed

    def slot_meanings(self, meaning: dict[str, Any]) -> list[str]:
        """The meanings that share one cardinality slot with this one."""
        group = meaning.get("cardinality_group")
        if not group:
            return [meaning["id"]]
        return [
            row["id"] for row in self.meanings.values() if row.get("cardinality_group") == group
        ]

    # Statuses ---------------------------------------------------------------------------------

    def vocabulary(self, kind: dict[str, Any]) -> list[dict[str, str]]:
        words = json.loads(kind.get("status_vocabulary") or "[]")
        if kind.get("lifecycle_class") == "work_with_state" and not any(
            entry["status"] == REPORTED_DONE for entry in words
        ):
            # D5: every work kind inherits a non-terminal "reported done" from its class.
            words.append({"status": REPORTED_DONE, "role": "reported_done"})
        return words

    def resolve_status(self, kind: dict[str, Any], status: str) -> tuple[str, str]:
        words = self.vocabulary(kind)
        valid = [entry["status"] for entry in words]
        for entry in words:
            if entry["status"] == status:
                return entry["status"], entry["role"]
        for entry in words:
            if normalise_phrase(entry["status"]) == normalise_phrase(status):
                return entry["status"], entry["role"]
        rule = "unknown_status" if words else "no_vocabulary"
        raise Refused(
            "unknown_status",
            rule,
            f"{status!r} is not a status of kind {kind['name']!r}",
            {"valid": valid},
        )

    def reopened_status(self, kind: dict[str, Any]) -> tuple[str, str] | None:
        """The status a reopened item takes: the first open one, else the first live one."""
        words = [entry for entry in self.vocabulary(kind) if entry["role"] not in TERMINAL_ROLES]
        for entry in words:
            if entry["role"] == "open":
                return entry["status"], entry["role"]
        return (words[0]["status"], words[0]["role"]) if words else None

    # Property names ---------------------------------------------------------------------------

    def property_name(self, kind_id: str, name: str) -> dict[str, Any] | None:
        """The declared property of this kind, or None. Only the aliases of this kind's property
        names count (line 1649: per kind), and a phrase that reaches two of them is refused."""
        found = resolve_word(self, "property_name", name, kind_id=kind_id)
        if found.outcome == AMBIGUOUS:
            raise Refused(
                "invalid_arguments",
                "ambiguous_phrase",
                f"{name!r} names more than one property of this kind: send one of them by name",
                {"property": name, "candidates": list(found.candidates)},
            )
        return found.target

    def property_names_of(self, kind_id: str) -> list[str]:
        return sorted(
            row["name"]
            for row in self.property_names
            if row.get("kind") == kind_id and usable(row) and row.get("name")
        )
