"""Bounded, code-owned inputs to the inactive D10 extraction component.

These records are not admission credentials. An integration must construct them from an
authorized retained source and its exact message metadata, never from request JSON.
Typed source admission is deliberately unavailable until its persistence owner supplies it.
"""

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from grove.store.enums import ENUMS
from grove.writer.clocks import ClockError, parse_instant

MAX_MESSAGES = 128
MAX_TEXT = 16_384
MAX_BATCH_TEXT = 65_536
MAX_IDENTIFIER = 1_024
MAX_REGISTRY = 256


class ExtractionRefused(ValueError):
    """A stable reason code, with no source text in the exception."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def bounded_text(value: object, maximum: int = MAX_IDENTIFIER) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ExtractionRefused("input_text_bounds")
    return value


@dataclass(frozen=True)
class SourceMessage:
    key: str
    locator: str
    text: str
    source_time: str
    speaker_kind: str
    modality: str = "text"
    speaker_item: str | None = None
    original_author: str | None = None
    timezone: str | None = None
    eligibility: str = "eligible"


@dataclass(frozen=True)
class SourceBatch:
    world_id: str
    run_id: str
    episode_id: str
    source_id: str
    connector_type: str
    messages: tuple[SourceMessage, ...]
    source_version: int
    episode_version: int
    screened: bool = False
    provenance_mode: str = "legacy"


@dataclass(frozen=True)
class RegistrySnapshot:
    """A bounded authorized grounding snapshot, not model-selected registry values.

    Kinds map to their allowed statuses. Versions map existing item/link IDs to the
    snapshot version. Scheduled fields map (kind, role) to a declared property name.
    """

    kinds: dict[str, tuple[str, ...]]
    meanings: tuple[str, ...] = ()
    properties: dict[str, tuple[str, ...]] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)
    scheduled_fields: dict[str, dict[str, str]] = field(default_factory=dict)


def validate_batch(batch: SourceBatch) -> None:
    if not isinstance(batch, SourceBatch):
        raise ExtractionRefused("source_batch_required")
    # A claimed admitted flag can never release typed input through this component.
    if batch.provenance_mode != "legacy":
        raise ExtractionRefused("typed_admission_unavailable")
    if batch.screened is not True:
        raise ExtractionRefused("screened_source_required")
    if batch.connector_type not in ENUMS["connector_type"]:
        raise ExtractionRefused("unknown_connector")
    for value in (batch.world_id, batch.run_id, batch.episode_id, batch.source_id):
        bounded_text(value)
    for value in (batch.source_version, batch.episode_version):
        if type(value) is not int or value < 1:
            raise ExtractionRefused("source_version_required")
    if not isinstance(batch.messages, tuple) or not 1 <= len(batch.messages) <= MAX_MESSAGES:
        raise ExtractionRefused("message_count")
    keys, locators = set(), set()
    total = 0
    previous = None
    for message in batch.messages:
        if not isinstance(message, SourceMessage) or message.eligibility != "eligible":
            raise ExtractionRefused("ineligible_message")
        bounded_text(message.key)
        bounded_text(message.locator)
        bounded_text(message.text, MAX_TEXT)
        total += len(message.text.encode("utf-8"))
        if total > MAX_BATCH_TEXT:
            raise ExtractionRefused("batch_text_bounds")
        if message.key in keys or message.locator in locators:
            raise ExtractionRefused("duplicate_message")
        keys.add(message.key)
        locators.add(message.locator)
        if message.speaker_kind not in ENUMS["speaker_kind"]:
            raise ExtractionRefused("speaker_required")
        if message.modality not in ENUMS["modality"]:
            raise ExtractionRefused("modality_required")
        for value in (message.speaker_item, message.original_author):
            if value is not None:
                bounded_text(value)
        if message.speaker_kind == "forwarded" and message.original_author is None:
            raise ExtractionRefused("original_author_required")
        bounded_text(message.source_time, 64)
        try:
            instant = parse_instant(message.source_time)
        except ClockError:
            raise ExtractionRefused("exact_source_time_required") from None
        if previous is not None and instant < previous:
            raise ExtractionRefused("messages_not_in_time_order")
        previous = instant
        if message.timezone is not None:
            bounded_text(message.timezone, 128)
            try:
                ZoneInfo(message.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ExtractionRefused("source_timezone_unknown") from None


def validate_registry(registry: RegistrySnapshot) -> None:
    if not isinstance(registry, RegistrySnapshot) or not registry.kinds:
        raise ExtractionRefused("registry_required")
    if not isinstance(registry.meanings, tuple) or len(registry.meanings) > MAX_REGISTRY:
        raise ExtractionRefused("registry_bounds")
    for collection in (
        registry.kinds,
        registry.properties,
        registry.versions,
        registry.scheduled_fields,
    ):
        if not isinstance(collection, dict) or len(collection) > MAX_REGISTRY:
            raise ExtractionRefused("registry_bounds")
    for kind, statuses in registry.kinds.items():
        bounded_text(kind)
        if not isinstance(statuses, tuple) or len(statuses) > 32:
            raise ExtractionRefused("status_bounds")
        for status in statuses:
            bounded_text(status)
    for meaning in registry.meanings:
        bounded_text(meaning)
    for kind, names in registry.properties.items():
        if kind not in registry.kinds or not isinstance(names, tuple) or len(names) > 64:
            raise ExtractionRefused("property_registry")
        for name in names:
            bounded_text(name)
    for reference, version in registry.versions.items():
        bounded_text(reference)
        if reference.startswith("$") or type(version) is not int or version < 1:
            raise ExtractionRefused("item_version_required")
    for kind, roles in registry.scheduled_fields.items():
        if kind not in registry.kinds or not isinstance(roles, dict) or len(roles) > 3:
            raise ExtractionRefused("scheduled_registry")
        for role, name in roles.items():
            if role not in ENUMS["scheduled_role"] or name not in registry.properties.get(kind, ()):
                raise ExtractionRefused("scheduled_registry")
