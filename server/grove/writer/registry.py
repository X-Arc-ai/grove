"""The registry as the writer reads it inside its transaction: kinds, meanings, aliases, property
names, and the status vocabulary of each kind.

The primitive rows live in world '' and a WorldTx cannot read them, so their fields come from
grove.store.primitives (they never change). A caller may not create an item of the belief kind or
a link of a primitive meaning: the beliefs lane and the composite operations mint those.
"""

import json
from dataclasses import dataclass
from typing import Any

from grove.store.normalise import normalise_phrase
from grove.store.primitives import (
    KIND_BELIEF,
    PRIMITIVE_KIND_BELIEF,
    PRIMITIVE_MEANING_IDS,
    PRIMITIVE_MEANINGS,
)
from grove.writer.errors import Refused
from grove.writer.tx import WorldTx

TERMINAL_ROLES = frozenset({"done", "cancelled", "ended"})
REPORTED_DONE = "reported done"
_UNUSABLE = frozenset({"retired", "split"})
_REGISTRY_TARGETS = ("kind", "meaning", "property_name")


def _usable(row: dict[str, Any]) -> bool:
    return row.get("status") not in _UNUSABLE


@dataclass
class Registry:
    kinds: dict[str, dict[str, Any]]
    meanings: dict[str, dict[str, Any]]
    aliases: list[dict[str, Any]]
    property_names: list[dict[str, Any]]

    @classmethod
    def load(cls, tx: WorldTx) -> "Registry":
        kinds = {row["k"]["id"]: row["k"] for row in tx.run("MATCH (k:Kind) RETURN k")}
        meanings = {row["m"]["id"]: row["m"] for row in tx.run("MATCH (m:Meaning) RETURN m")}
        aliases = [
            row["a"]
            for row in tx.run(
                "MATCH (a:Alias) WHERE a.target_type IN $types RETURN a",
                types=list(_REGISTRY_TARGETS),
            )
        ]
        names = [row["p"] for row in tx.run("MATCH (p:PropertyName) RETURN p")]
        return cls(kinds, meanings, aliases, names)

    # Kinds ------------------------------------------------------------------------------------

    def kind(self, kind_id: str) -> dict[str, Any]:
        if kind_id == KIND_BELIEF:
            return {"id": KIND_BELIEF, **PRIMITIVE_KIND_BELIEF}
        return self.kinds[kind_id]

    def _alias_target(self, target_type: str, phrase: str) -> dict[str, Any] | None:
        wanted = normalise_phrase(phrase)
        for alias in self.aliases:
            if (
                alias.get("target_type") == target_type
                and alias.get("normalised_phrase") == wanted
                and _usable(alias)
            ):
                return alias
        return None

    def resolve_kind(self, ref: str) -> dict[str, Any]:
        wanted = normalise_phrase(ref)
        if ref == KIND_BELIEF or wanted == normalise_phrase(str(PRIMITIVE_KIND_BELIEF["name"])):
            raise Refused(
                "unknown_kind",
                "primitive_kind",
                "a belief is minted by the beliefs operations, not by create_item",
                {"valid": self.kind_names()},
            )
        usable = [row for row in self.kinds.values() if _usable(row)]
        for row in usable:
            if row["id"] == ref or row["name"] == ref:
                return row
        for row in usable:
            if normalise_phrase(row["name"]) == wanted:
                return row
        alias = self._alias_target("kind", ref)
        if alias is not None and alias["target_id"] in self.kinds:
            return self.kinds[alias["target_id"]]
        raise Refused(
            "unknown_kind", "unknown_kind", f"no kind {ref!r}", {"valid": self.kind_names()}
        )

    def kind_names(self) -> list[str]:
        return sorted(row["name"] for row in self.kinds.values() if _usable(row))

    # Meanings ---------------------------------------------------------------------------------

    def meaning(self, meaning_id: str) -> dict[str, Any]:
        if meaning_id in PRIMITIVE_MEANINGS:
            return {"id": meaning_id, **PRIMITIVE_MEANINGS[meaning_id]}
        return self.meanings[meaning_id]

    def meaning_names(self) -> list[str]:
        return sorted(row["name"] for row in self.meanings.values() if _usable(row))

    def resolve_meaning(self, ref: str) -> tuple[dict[str, Any], bool]:
        """(the meaning, whether the phrase was a reversed alias)."""
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
        usable = [row for row in self.meanings.values() if _usable(row)]
        for row in usable:
            if row["id"] == ref or row["name"] == ref:
                return row, False
        for row in usable:
            if normalise_phrase(row["name"]) == wanted:
                return row, False
        alias = self._alias_target("meaning", ref)
        if alias is not None and alias["target_id"] in self.meanings:
            return self.meanings[alias["target_id"]], bool(alias.get("reversed"))
        raise Refused(
            "unknown_meaning",
            "unknown_meaning",
            f"no meaning {ref!r}",
            {"valid": self.meaning_names()},
        )

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
        of_kind = [
            row for row in self.property_names if row.get("kind") == kind_id and _usable(row)
        ]
        for row in of_kind:
            if row.get("name") == name:
                return row
        for row in of_kind:
            if normalise_phrase(row.get("name", "")) == normalise_phrase(name):
                return row
        alias = self._alias_target("property_name", name)
        if alias is not None:
            for row in of_kind:
                if row["id"] == alias["target_id"]:
                    return row
        return None

    def property_names_of(self, kind_id: str) -> list[str]:
        return sorted(
            row["name"]
            for row in self.property_names
            if row.get("kind") == kind_id and _usable(row) and row.get("name")
        )
