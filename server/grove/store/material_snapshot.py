"""Validate material metadata and content references before snapshot acceptance."""

import json
import re
from collections import defaultdict
from datetime import datetime
from uuid import UUID

from grove.store.catalogue import MATERIAL_REVISIONS
from grove.store.scope import check_world_id

_HASH = re.compile(r"[0-9a-f]{64}")
_FIELDS = {p for c in MATERIAL_REVISIONS.columns for p in c.props}


def _id(value):
    try:
        return type(value) is str and str(UUID(value)) == value and UUID(value).version == 7
    except (ValueError, AttributeError):
        return False


def material_counts(row, sources):
    """Counts describe retained body messages, not the original upload or title."""
    parts = characters = 0
    for sid in row["source_ids"]:
        source = sources[sid]
        try:
            messages = json.loads(source["content"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid material source content") from None
        if not isinstance(messages, list) or any(
            not isinstance(message, dict) or not isinstance(message.get("text"), str)
            for message in messages
        ):
            raise ValueError("invalid material source content")
        if source.get("forget_state") == "forgotten":
            if messages:
                raise ValueError("forgotten material source retains content")
            continue
        parts += len(messages)
        characters += sum(len(message["text"]) for message in messages)
    return {
        "part_count": parts,
        "retained_characters": characters,
        "held_count": parts if row["kind"] == "file" else 0,
    }


def validate_material_snapshot(tables):
    def rows(name):
        value = tables.get(name, [])
        return list(value.values()) if isinstance(value, dict) else value

    sources = {r["id"]: r for r in rows("sources")}
    requests = {r["id"]: r for r in rows("requests")}
    episodes = {r["id"]: r for r in rows("episodes")}
    families = defaultdict(list)
    keys = set()
    identities = set()
    for row in rows("material_revisions"):

        def refuse():
            raise ValueError("invalid material snapshot")

        if set(row) - _FIELDS or not (_FIELDS - {"request_id", "episode_id"}) <= set(row):
            refuse()
        check_world_id(row["world_id"])
        for key in ("created_at", "touched_at"):
            if not isinstance(row[key], datetime) or row[key].utcoffset() is None:
                refuse()
        if any(not _id(row[k]) for k in ("id", "material_id", "title_source_id")):
            refuse()
        if row["id"] in identities or row["request_key_hash"] in keys:
            refuse()
        identities.add(row["id"])
        keys.add(row["request_key_hash"])
        if row["kind"] not in {"file", "note"} or row["format"] not in {
            "text",
            "markdown",
            "pdf",
            "docx",
            "pptx",
            "note",
        }:
            refuse()
        if (row["kind"] == "note") != (row["format"] == "note") or row["state"] not in {
            "current",
            "superseded",
            "deleted",
        }:
            refuse()
        for key in ("request_key_hash", "input_digest"):
            if type(row[key]) is not str or not _HASH.fullmatch(row[key]):
                refuse()
        for key in (
            "revision",
            "version",
            "input_bytes",
            "retained_characters",
            "part_count",
            "held_count",
        ):
            minimum = 1 if key in {"revision", "version"} else 0
            maximum = 2**31 - 1 if key in {"revision", "version"} else 16 * 1024 * 1024
            if type(row[key]) is not int or not minimum <= row[key] <= maximum:
                refuse()
        ids = row["source_ids"]
        if (
            type(ids) is not list
            or len(ids) > 64
            or len(set(ids)) != len(ids)
            or any(not _id(s) for s in ids)
        ):
            refuse()
        if row["part_count"] > 1000 or row["held_count"] > row["part_count"]:
            refuse()
        if row["state"] != "deleted":
            for sid in [row["title_source_id"], *ids]:
                if (
                    sid not in sources
                    or sources[sid].get("world_id") != row["world_id"]
                    or (
                        sources[sid].get("forget_state") == "forgotten"
                        and sources[sid].get("content") != "[]"
                    )
                ):
                    refuse()
            if sources[row["title_source_id"]].get("parser_version") != "material.title.v1":
                refuse()
            title_source = sources[row["title_source_id"]]
            expected_parsers = {
                "note": "material.note.v1",
                "text": "document.text.v1",
                "markdown": "document.text.v1",
                "pdf": "document.pdf.v1",
                "docx": "document.docx.v1",
                "pptx": "document.pptx.v1",
            }
            if any(
                sources[sid].get("connector_id") != title_source.get("connector_id")
                or sources[sid].get("parser_version") != expected_parsers[row["format"]]
                for sid in ids
            ):
                refuse()
            if any(row[key] != value for key, value in material_counts(row, sources).items()):
                refuse()
            if row["kind"] == "note" and (
                row["part_count"] or row.get("request_id") or row.get("episode_id")
            ):
                request, episode = (
                    requests.get(row.get("request_id")),
                    episodes.get(row.get("episode_id")),
                )
                if (
                    not request
                    or not episode
                    or request.get("source_id") not in ids
                    or episode.get("source_ids") != ids
                    or any(r.get("world_id") != row["world_id"] for r in (request, episode))
                ):
                    refuse()
            elif row.get("request_id") or row.get("episode_id"):
                refuse()
        elif any(row[key] for key in ("part_count", "retained_characters", "held_count")):
            refuse()
        families[(row["world_id"], row["material_id"])].append(row)
    for family in families.values():
        ordered = sorted(family, key=lambda r: r["revision"])
        if [r["revision"] for r in ordered] != list(range(1, len(ordered) + 1)):
            raise ValueError("invalid material revision order")
        states = [r["state"] for r in ordered]
        if states != ["deleted"] * len(ordered) and states != ["superseded"] * (
            len(ordered) - 1
        ) + ["current"]:
            raise ValueError("invalid material revision states")
