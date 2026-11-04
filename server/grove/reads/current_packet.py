"""Pure current-item cards over complete, already authorized rows.

The caller supplies the owner's permitted structural view before this renderer runs. This module
does not classify private data or authenticate a principal. It cannot make raw capture rows safe.
All source reads, temporal inputs and owner explanation outputs must belong to the same capture.
The enclosing service owns ranking, complete page admission, cursor handles and final release.
"""

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from grove.models.verify_judge import JudgeInputError, cut
from grove.reads.capture import encode
from grove.reads.coverage import CoverageStatement, item_coverage_gap
from grove.store.current import SUCCESSOR_MEANINGS, Standing, Successor, link_standing, standing
from grove.store.primitives import (
    KIND_BELIEF,
    MEANING_DESCRIBES,
    PRIMITIVE_KIND_BELIEF,
    PRIMITIVE_MEANINGS,
)
from grove.store.scope import check_world_id
from grove.store.words import ASK_THE_PERSON, clock_value_words, how_words, recheck_words
from grove.strict_json import loads_strict

MAX_CARD_BYTES = 65_536
MAX_PREVIEW_LINKS = 50
LINKS_PER_MEANING = 5
_MIN_TIME = datetime.min.replace(tzinfo=UTC)


class PacketError(ValueError):
    """A fixed code only; errors never embed captured values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise PacketError("invalid_packet_reference")
    return value


def _at(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(UTC)
    except (ValueError, TypeError, OverflowError) as error:
        raise PacketError("invalid_packet_time") from error


def _json(value: Any, expected: type | tuple[type, ...], default: Any) -> Any:
    if value is None:
        return deepcopy(default)
    try:
        result = loads_strict(value.encode("utf-8")) if isinstance(value, str) else deepcopy(value)
    except ValueError as error:
        raise PacketError("invalid_packet_value") from error
    if not isinstance(result, expected):
        raise PacketError("invalid_packet_value")
    return result


@dataclass(frozen=True)
class CurrentRows:
    """Ordinary row input, not a proof of D14 authorization or inventory completeness."""

    world_id: str
    items: Sequence[Mapping] = ()
    kinds: Sequence[Mapping] = ()
    meanings: Sequence[Mapping] = ()
    property_names: Sequence[Mapping] = ()
    links: Sequence[Mapping] = ()
    evidence: Sequence[Mapping] = ()
    sources: Sequence[Mapping] = ()
    episodes: Sequence[Mapping] = ()
    history_values: Sequence[Mapping] = ()
    beliefs: Sequence[Mapping] = ()
    impacts: Sequence[Mapping] = ()
    contradictions: Sequence[Mapping] = ()
    change_entries: Sequence[Mapping] = ()
    verifications: Sequence[Mapping] = ()


@dataclass(frozen=True)
class MarkExplanation:
    """Prepared output from the accepted D6 owner helper, filtered with its dependencies."""

    link_id: str
    text: str
    needs_explanation: bool


@dataclass(frozen=True)
class RenderedItem:
    card: dict
    link_ids: tuple[str, ...]


class CurrentRenderer:
    def __init__(
        self,
        rows: CurrentRows,
        *,
        captured_at: datetime,
        coverage: CoverageStatement,
        complete: bool,
        mark_explanations: Sequence[MarkExplanation] = (),
        recheck_references: Mapping[str, Mapping] | None = None,
    ) -> None:
        if not isinstance(rows, CurrentRows) or complete is not True:
            raise PacketError("incomplete_packet_inputs")
        check_world_id(rows.world_id)
        self.world_id = rows.world_id
        self.captured_at = _at(captured_at)
        if self.captured_at is None or not isinstance(coverage, CoverageStatement):
            raise PacketError("invalid_packet_inputs")
        self.coverage = coverage
        self.recheck_references = deepcopy(dict(recheck_references or {}))
        for name in rows.__dataclass_fields__:
            if name == "world_id":
                continue
            key = {"beliefs": "item_id", "impacts": "link_id", "change_entries": "sequence"}.get(
                name, "id"
            )
            setattr(self, name, self._index(getattr(rows, name), key))
        # Fixed developer definitions are copied. A mutable database primitive is never used.
        self.kinds[KIND_BELIEF] = {"id": KIND_BELIEF, **deepcopy(PRIMITIVE_KIND_BELIEF)}
        self.meanings.update(
            {key: {"id": key, **deepcopy(value)} for key, value in PRIMITIVE_MEANINGS.items()}
        )
        self._explanations = {}
        for value in mark_explanations:
            if not isinstance(value, MarkExplanation) or type(value.needs_explanation) is not bool:
                raise PacketError("invalid_mark_explanation")
            if _text(value.link_id) in self._explanations:
                raise PacketError("duplicate_mark_explanation")
            _text(value.text)
            self._explanations[value.link_id] = value
        self._by_evidence = defaultdict(list)
        self._successors = defaultdict(list)
        self._adjacent = defaultdict(list)
        self._impacts_by_item = defaultdict(list)
        self._contradictions_by_item = defaultdict(list)
        self._verification_times = defaultdict(list)
        for mark in self.impacts.values():
            self._impacts_by_item[mark.get("affected_item")].append(mark)
        for contradiction in self.contradictions.values():
            self._contradictions_by_item[contradiction.get("target")].append(contradiction)
        for verification in self.verifications.values():
            verified_at = _at(verification.get("at"))
            if verified_at is not None:
                self._verification_times[verification.get("claim_id")].append(verified_at)
        for row in (*self.items.values(), *self.links.values()):
            for name in ("became_true_earliest", "stopped_being_true_earliest"):
                row[name] = _at(row.get(name))
        for value in self.evidence.values():
            target = value.get("target_id")
            target_type = value.get("target_type")
            if target_type not in ("item", "belief", "link"):
                raise PacketError("invalid_evidence_target")
            if target_type in ("item", "belief") and target not in self.items:
                raise PacketError("missing_evidence_target")
            if target_type == "link" and target not in self.links:
                raise PacketError("missing_evidence_target")
            if value.get("active") is True:
                self._by_evidence[target].append(value)
        for link in self.links.values():
            if any(link.get(key) not in self.items for key in ("from_item", "to_item")):
                raise PacketError("missing_link_endpoint")
            if link.get("meaning") not in self.meanings:
                raise PacketError("missing_link_meaning")
            if link.get("active_key") and link["meaning"] in SUCCESSOR_MEANINGS:
                self._successors[link["from_item"]].append(
                    Successor(link["to_item"], link["id"], link["meaning"])
                )
        self.standings: dict[str, Standing] = {}
        for item in self.items.values():
            kind = self.kinds.get(item.get("kind"))
            if kind is None:
                raise PacketError("missing_item_kind")
            properties = _json(item.get("properties"), dict, {})
            scheduled = {
                name["scheduled_role"]: properties[name["name"]]
                for name in self.property_names.values()
                if name.get("kind") == item["kind"]
                and name.get("scheduled_role")
                and properties.get(name.get("name")) not in (None, "")
            }
            self.standings[item["id"]] = standing(
                item,
                at=self.captured_at,
                lifecycle_class=kind.get("lifecycle_class"),
                planned_origin=any(
                    e.get("source_type") == "planned" and e.get("polarity") == "supports"
                    for e in self._by_evidence[item["id"]]
                ),
                scheduled=scheduled,
                successors=sorted(
                    self._successors[item["id"]],
                    key=lambda value: (
                        _at(self.links[value.link_id].get("recorded")) or _MIN_TIME,
                        value.link_id,
                    ),
                ),
            )
        self.eligible_ids = frozenset(
            key
            for key, value in self.standings.items()
            if value.state not in ("ended", "not_started") or value.successors
        )
        eligible_link_ids = set()
        for link in self.links.values():
            if (
                link_standing(link, at=self.captured_at).is_current
                and {link["from_item"], link["to_item"]} <= self.eligible_ids
            ):
                eligible_link_ids.add(link["id"])
                self._adjacent[link["from_item"]].append(link)
                if link["to_item"] != link["from_item"]:
                    self._adjacent[link["to_item"]].append(link)
        self.eligible_link_ids = frozenset(eligible_link_ids)

    def _index(self, rows: Sequence[Mapping], key: str) -> dict:
        if not isinstance(rows, (tuple, list)):
            raise PacketError("invalid_packet_rows")
        result = {}
        for row in rows:
            if not isinstance(row, Mapping) or row.get("world_id") != self.world_id:
                raise PacketError("invalid_packet_world")
            identity = row.get(key)
            if key == "sequence":
                if type(identity) is not int or identity < 1:
                    raise PacketError("invalid_packet_reference")
            else:
                _text(identity)
            if identity in result:
                raise PacketError("duplicate_packet_reference")
            result[identity] = deepcopy(dict(row))
        return result

    def _value(self, value_id: Any) -> str | None:
        if value_id is None:
            return None
        row = self.history_values.get(_text(value_id))
        if (
            row is None
            or row.get("forgotten_at") is not None
            or not isinstance(row.get("value"), str)
        ):
            raise PacketError("missing_packet_value")
        return row["value"]

    def _evidence(self, row: Mapping) -> dict:
        source = self.sources.get(row.get("source_id"))
        episode = self.episodes.get(row.get("episode_id"))
        if (
            source is None
            or episode is None
            or source["id"] not in (episode.get("source_ids") or ())
        ):
            raise PacketError("missing_evidence_provenance")
        if source.get("connector_id") != episode.get("connector_id"):
            raise PacketError("invalid_evidence_provenance")
        if not isinstance(source.get("content"), str):
            raise PacketError("stored_excerpt_unavailable")
        locator = _text(row.get("locator"))
        try:
            excerpt = cut(source["content"], locator)
        except (JudgeInputError, ValueError, TypeError) as error:
            raise PacketError("invalid_evidence_locator") from error
        for field_name in ("speaker_item", "original_author"):
            if row.get(field_name) is not None and row[field_name] not in self.items:
                raise PacketError("missing_evidence_speaker")
        source_at = _at(row.get("source_time"))
        if source_at is None:
            raise PacketError("missing_evidence_time")
        return {
            "id": row["id"],
            "source": source["id"],
            "episode": episode["id"],
            "locator": locator,
            "message_key": row.get("message_key"),
            "source_time": source_at,
            "recorded": _at(row.get("recorded")),
            "source_offset_minutes": row.get("source_offset_minutes"),
            "person_timezone": row.get("person_timezone"),
            "source_type": row.get("source_type"),
            "derivation": row.get("derivation"),
            "speaker_kind": row.get("speaker_kind"),
            "speaker_item": row.get("speaker_item"),
            "original_author": row.get("original_author"),
            "modality": row.get("modality"),
            "polarity": row.get("polarity"),
            "excerpt": excerpt,
        }

    def _clocks(self, item: Mapping) -> dict:
        clocks = {key: _at(item.get(key)) for key in ("recorded", "last_confirmed", "recheck_by")}
        for name in ("became_true", "stopped_being_true"):
            low, high = (_at(item.get(f"{name}_{part}")) for part in ("earliest", "latest"))
            clocks[name] = {
                "earliest": low,
                "latest": high,
                "precision": item.get(f"{name}_precision"),
                "words": self._value(item.get(f"{name}_words")),
                "display": clock_value_words(
                    low, high, item.get(f"{name}_precision"), zone=UTC, now=self.captured_at
                ),
            }
        raw_how = item.get("recheck_how")
        how = ASK_THE_PERSON if raw_how == ASK_THE_PERSON else _json(raw_how, (dict, str), {})
        if isinstance(how, str) and how != ASK_THE_PERSON:
            raise PacketError("invalid_recheck_instruction")
        self._check_recheck_sources(item["id"], how)
        clocks["recheck_how"] = how
        clocks["recheck_display"] = recheck_words(
            clocks["recheck_by"], how=how_words(how), zone=UTC, now=self.captured_at
        )
        clocks["confirmed_against"] = item.get("confirmed_against")
        if clocks["confirmed_against"] is not None:
            verification = self.verifications.get(clocks["confirmed_against"])
            if verification is None or verification.get("claim_id") != item["id"]:
                raise PacketError("missing_confirmation")
        return clocks

    def _check_recheck_sources(self, item_id: str, how: Any) -> None:
        if not isinstance(how, dict) or "connector" not in how:
            return
        # D4 stores a connector type here, not an instance ID. The caller resolves the
        # particular instances; matching a type alone must not invent those references.
        source_kind = how["connector"]
        by_id = {row.connector_id: row for row in self.coverage.sources}
        known_kinds = {row.source_kind for row in by_id.values()} | set(self.coverage.not_connected)
        if not isinstance(source_kind, str) or source_kind not in known_kinds:
            raise PacketError("missing_recheck_connector")
        refs = self.recheck_references.get(item_id, {})
        ids = refs.get("connector_ids", ())
        missing = refs.get("missing_kinds", ())
        for values in (ids, missing):
            if not isinstance(values, (list, tuple)) or any(
                not isinstance(value, str) or not value for value in values
            ):
                raise PacketError("invalid_recheck_reference")
        if any(
            identity not in by_id or by_id[identity].source_kind != source_kind for identity in ids
        ):
            raise PacketError("missing_recheck_connector")
        if refs.get("complete") is True and not ids and source_kind not in missing:
            raise PacketError("missing_recheck_connector")

    def _link(self, item_id: str, link: Mapping) -> dict:
        other_id = link["to_item"] if link["from_item"] == item_id else link["from_item"]
        other = self.items[other_id]
        return {
            "id": link["id"],
            "meaning_id": link["meaning"],
            "meaning": self.meanings[link["meaning"]]["name"],
            "direction": "out" if link["from_item"] == item_id else "in",
            "item": other_id,
            "title": other.get("title"),
            "kind": other["kind"],
            "status": other.get("status"),
            "standing": self.standings[other_id].state,
            "recorded": _at(link.get("recorded")),
            "source_type": link.get("derived_source_type"),
        }

    def _marks(self, item_id: str) -> list[dict]:
        result = []
        for mark in sorted(self._impacts_by_item[item_id], key=lambda row: row["link_id"]):
            if mark.get("state") not in ("open", "proposed"):
                continue
            link = self.links.get(mark["link_id"])
            if link is None or link.get("from_item") != item_id:
                raise PacketError("missing_mark_link")
            path = _json(mark.get("path"), list, [])
            if any(part not in self.links for part in path):
                raise PacketError("missing_mark_path")
            text = self._value(mark.get("explanation"))
            prepared = self._explanations.get(mark["link_id"])
            if text is None and prepared is None:
                raise PacketError("mark_explanation_unavailable")
            result.append(
                {
                    "link_id": mark["link_id"],
                    "trigger_change_set": mark.get("trigger_change_set"),
                    "rule": mark.get("rule"),
                    "depth": mark.get("depth"),
                    "path": path,
                    "stakes": mark.get("stakes"),
                    "state": mark.get("state"),
                    "flag": mark.get("flag"),
                    "explanation": text if text is not None else prepared.text,
                    "needs_explanation": False if text is not None else prepared.needs_explanation,
                }
            )
        return result

    def render_link(self, item_id: str, link_id: str) -> dict:
        """Render one continuation link with exactly the preview's eligibility and fields."""
        if item_id not in self.eligible_ids:
            raise PacketError("item_not_current")
        link = self.links.get(link_id)
        if link_id not in self.eligible_link_ids or item_id not in (
            link["from_item"],
            link["to_item"],
        ):
            raise PacketError("link_not_current")
        result = self._link(item_id, link)
        if len(encode(result)) > MAX_CARD_BYTES:
            raise PacketError("item_card_too_large")
        return deepcopy(result)

    def _contradictions(self, item_id: str) -> list[dict]:
        result = []
        for row in sorted(self._contradictions_by_item[item_id], key=lambda value: value["id"]):
            if row.get("status") not in ("open", "with_the_person"):
                continue
            target, sequence = row.get("standing_target_id"), row.get("standing_change_id")
            if target not in self.items or (
                sequence is not None and sequence not in self.change_entries
            ):
                raise PacketError("missing_contradiction_standing")
            if sequence is not None and self.change_entries[sequence].get("entity_id") != target:
                raise PacketError("invalid_contradiction_standing")
            fresh_id = row.get("fresh_evidence_id")
            if fresh_id is not None:
                if fresh_id not in self.evidence:
                    raise PacketError("missing_contradiction_evidence")
                fresh = self._evidence(self.evidence[fresh_id])
            else:
                source = self.sources.get(row.get("fresh_source_id"))
                if source is None or not isinstance(source.get("content"), str):
                    raise PacketError("missing_contradiction_source")
                try:
                    text = cut(source["content"], _text(row.get("fresh_locator")))
                except (JudgeInputError, ValueError, TypeError) as error:
                    raise PacketError("invalid_evidence_locator") from error
                fresh = {"source": source["id"], "locator": row["fresh_locator"], "excerpt": text}
            result.append(
                {
                    "id": row["id"],
                    "status": row["status"],
                    "detected_at": _at(row.get("detected_at")),
                    "standing": {"item": target, "change_sequence": sequence},
                    "fresh": fresh,
                }
            )
        return result

    def _beliefs(self, item_id: str) -> list[dict]:
        result = []
        for link in self._adjacent[item_id]:
            if link["meaning"] != MEANING_DESCRIBES or link["to_item"] != item_id:
                continue
            belief_id = link["from_item"]
            row = self.beliefs.get(belief_id)
            if row is None:
                raise PacketError("missing_belief_state")
            if row.get("state") not in ("tentative", "established", "weakening") or row.get(
                "end_class"
            ):
                continue
            episodes = _json(row.get("supporting_episodes"), list, [])
            if any(episode not in self.episodes for episode in episodes):
                raise PacketError("missing_belief_episode")
            times = self._verification_times[belief_id]
            result.append(
                {
                    "id": belief_id,
                    "title": self.items[belief_id].get("title"),
                    "state": row["state"],
                    "state_since": _at(row.get("state_since")),
                    "supporting_episode_count": len(set(episodes)),
                    "last_retest_at": max((at for at in times if at is not None), default=None),
                }
            )
        return sorted(result, key=lambda value: value["id"])

    def render_item(self, item_id: str, *, include_evidence: bool = False) -> RenderedItem:
        """Optionally expand all active evidence, including contradicting evidence."""
        if type(include_evidence) is not bool:
            raise PacketError("invalid_packet_inputs")
        if item_id not in self.eligible_ids:
            raise PacketError("item_not_current")
        item = self.items[item_id]
        proofs = [self._evidence(value) for value in self._by_evidence[item_id]]
        proofs.sort(
            key=lambda value: (value["source_time"], value["recorded"] or _MIN_TIME, value["id"]),
            reverse=True,
        )
        newest = proofs[0] if proofs else None
        provenance_id = item.get("provenance_evidence_id")
        if provenance_id is not None and provenance_id not in self.evidence:
            raise PacketError("missing_item_provenance")
        if provenance_id is not None and self.evidence[provenance_id].get("target_id") != item_id:
            raise PacketError("invalid_item_provenance")
        provenance = (
            self._evidence(self.evidence[provenance_id]) if provenance_id is not None else None
        )
        clocks = self._clocks(item)
        flags = []
        if clocks["last_confirmed"] is None:
            flags.append("never_confirmed")
        if clocks["recheck_by"] is not None and clocks["recheck_by"] <= self.captured_at:
            flags.append("past_recheck_by")
        status_since = _at(item.get("status_since"))
        if newest is not None and newest["source_time"] > max(
            status_since or _MIN_TIME, clocks["last_confirmed"] or _MIN_TIME
        ):
            flags.append("newer_evidence_than_state")
        links = sorted(self._adjacent[item_id], key=lambda value: value["id"])
        links.sort(key=lambda value: _at(value.get("recorded")) or _MIN_TIME, reverse=True)
        by_meaning, by_kind = Counter(), Counter()
        shown = []
        for link in links:
            meaning = link["meaning"]
            other = link["to_item"] if link["from_item"] == item_id else link["from_item"]
            by_meaning[meaning] += 1
            by_kind[self.items[other]["kind"]] += 1
            if by_meaning[meaning] <= LINKS_PER_MEANING and len(shown) < MAX_PREVIEW_LINKS:
                shown.append(self._link(item_id, link))
        refs = self.recheck_references.get(item_id, {})
        gap = item_coverage_gap(
            self.coverage,
            contributed_connector_ids=tuple(
                sorted({self.sources[value["source"]]["connector_id"] for value in proofs})
            ),
            recheck_connector_ids=refs.get("connector_ids", ()),
            recheck_missing_kinds=refs.get("missing_kinds", ()),
            references_complete=refs.get("complete", item.get("recheck_how") is None),
        )
        card = {
            "id": item_id,
            "kind": item["kind"],
            "kind_name": self.kinds[item["kind"]]["name"],
            "title": item.get("title"),
            "summary": item.get("summary"),
            "properties": _json(item.get("properties"), dict, {}),
            "status": item.get("status"),
            "status_role": item.get("status_role"),
            "end_class": item.get("end_class"),
            "status_since": status_since,
            "standing": self.standings[item_id].state,
            "standing_reason": self.standings[item_id].reason,
            "scheduled": self.standings[item_id].scheduled,
            "clocks": clocks,
            "source_type": item.get("derived_source_type"),
            "derivation": item.get("derivation"),
            "speaker": item.get("speaker"),
            "modality": item.get("modality"),
            "provenance": provenance,
            "provenance_reason": self._value(item.get("provenance_reason")),
            "newest_evidence": newest,
            "flags": flags,
            "successors": [
                self._link(item_id, self.links[value.link_id])
                for value in self.standings[item_id].successors
            ],
            "links": shown,
            "link_counts": {
                "total": len(links),
                "by_meaning": dict(sorted(by_meaning.items())),
                "by_kind": dict(sorted(by_kind.items())),
            },
            "links_remaining": len(links) - len(shown),
            "links_need_cursor": len(links) > len(shown),
            "affected_by": self._marks(item_id),
            "contradictions": self._contradictions(item_id),
            "beliefs": self._beliefs(item_id),
            "coverage_gap": asdict(gap) if gap is not None else None,
        }
        if include_evidence:
            card["evidence"] = proofs
        if len(encode(card)) > MAX_CARD_BYTES:
            raise PacketError("item_card_too_large")
        return RenderedItem(deepcopy(card), tuple(value["id"] for value in links))
