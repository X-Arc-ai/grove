"""D14 screening on arrival: every source item is screened in code before the sources table.

Order per message (fixed):
1. Denylist on the raw sender and text: a hit drops the message.
2. One-time codes, then reset and magic links: a strong signal drops the message, and a weak
   candidate in a message that stays is masked as [code] or [link].
3. Rename map on the sender and text.
4. Credentials, 5. payment cards, 6. third-party contact identifiers, 7. pay amounts (opt-in).
The weak candidates and steps 3 to 7 return spans on the same text. Overlapping spans merge, and
the earlier step wins the class, so a rename can never be undone by a later mask and no part of a
value survives.
Every step reads the text as written and as read (names.Normalised.views), so chat markup or an
escape glued to a value hides nothing.

The locator goes through steps 1, 3, 4, 5 and 6 as one string. A denylisted term there keeps
the whole source out. It is a file name, so "_" also reads as a space for the names and phones
(SC20). The message key goes through steps 3 to 6, so all four fields are screened.

No step calls a model. Nothing here logs, and no repr or error holds a value.
"""

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType

from grove.config import DENYLIST_VAR, RENAME_MAP_VAR, load_screening_settings
from grove.screening import detectors
from grove.screening.classes import (
    CLASSES,
    CONTACT_PHONE,
    CONTACT_WORDS,
    DENYLISTED,
    KEPT_OUT,
    MASKS,
    ONE_TIME_CODE,
    OPT_IN_CLASSES,
    PAY_AMOUNT,
    RENAMED,
    RESET_LINK,
)
from grove.screening.detectors import Hit, OwnIdentifiers
from grove.screening.names import (
    Denylist,
    Normalised,
    RenameMap,
    file_name_view,
    load_denylist,
    load_rename_map,
)
from grove.source_facts import TextSourceFacts, check_text_transport
from grove.store.enums import ENUMS

_SPEAKER_KINDS = frozenset(ENUMS["speaker_kind"])
_MODALITIES = frozenset(ENUMS["modality"])

# The step number of each span decides who wins an overlap: the lower step.
STEP_DENYLIST = 1
STEP_WEAK_CODE_OR_LINK = 2
STEP_RENAME = 3
STEP_CREDENTIAL = 4
STEP_CARD = 5
STEP_CONTACT = 6
STEP_PAY = 7

LOCATOR_STEPS = frozenset({STEP_DENYLIST, STEP_RENAME, STEP_CREDENTIAL, STEP_CARD, STEP_CONTACT})
KEY_STEPS = frozenset({STEP_RENAME, STEP_CREDENTIAL, STEP_CARD, STEP_CONTACT})
SENDER_STEPS = frozenset({STEP_RENAME, STEP_CREDENTIAL, STEP_CARD, STEP_CONTACT, STEP_PAY})
TEXT_STEPS = SENDER_STEPS | {STEP_WEAK_CODE_OR_LINK}


def _length(value: str | None) -> int | None:
    return None if value is None else len(value)


@dataclass(frozen=True, repr=False)
class Message:
    """One message as a connector read it. Its repr shows lengths only."""

    key: str
    sender: str | None
    text: str
    at: datetime | None = None
    # Who spoke and in what form, from the source's metadata (D5). Closed codes: screening passes
    # them through as they are, and a connector that knows neither leaves them None.
    speaker_kind: str | None = None
    modality: str | None = None
    source_facts: TextSourceFacts | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str):
            raise TypeError("Message.key must be a str")
        if self.sender is not None and not isinstance(self.sender, str):
            raise TypeError("Message.sender must be a str or None")
        if not isinstance(self.text, str):
            raise TypeError("Message.text must be a str")
        if self.at is not None and (
            not isinstance(self.at, datetime) or self.at.utcoffset() is None
        ):
            raise ValueError("Message.at must be an aware datetime or None")
        # type() and not isinstance(): a str subclass could carry text past the closed list.
        if self.speaker_kind is not None and (
            type(self.speaker_kind) is not str or self.speaker_kind not in _SPEAKER_KINDS
        ):
            raise ValueError("Message.speaker_kind must be a speaker_kind code or None")
        if self.modality is not None and (
            type(self.modality) is not str or self.modality not in _MODALITIES
        ):
            raise ValueError("Message.modality must be a modality code or None")
        if self.source_facts is not None:
            _check_message_facts(self.source_facts, self.at, self.speaker_kind, self.modality)

    def __repr__(self) -> str:
        return (
            f"Message(key_length={len(self.key)}, sender_length={_length(self.sender)}, "
            f"text_length={len(self.text)}, at={'set' if self.at else None})"
        )


@dataclass(frozen=True, repr=False)
class ScreenedMessage:
    key: str
    sender: str | None
    text: str
    at: datetime | None = None
    speaker_kind: str | None = None
    modality: str | None = None
    source_facts: TextSourceFacts | None = None

    def __post_init__(self) -> None:
        if self.source_facts is not None:
            _check_message_facts(self.source_facts, self.at, self.speaker_kind, self.modality)

    def as_row(self) -> dict:
        row = {
            "key": self.key,
            "sender": self.sender,
            "text": self.text,
            "at": None if self.at is None else self.at.isoformat(),
        }
        # Only when a connector set them, so a source without them keeps its four fields.
        if self.speaker_kind is not None:
            row["speaker_kind"] = self.speaker_kind
        if self.modality is not None:
            row["modality"] = self.modality
        if self.source_facts is not None:
            _check_message_facts(self.source_facts, self.at, self.speaker_kind, self.modality)
            row["source_facts"] = self.source_facts.as_row()
            row["speaker_kind"] = self.speaker_kind
        return row

    def __repr__(self) -> str:
        return (
            f"ScreenedMessage(key_length={len(self.key)}, sender_length={_length(self.sender)}, "
            f"text_length={len(self.text)}, at={'set' if self.at else None})"
        )


def _check_message_facts(facts, at, speaker_kind, modality) -> None:
    if type(facts) is not TextSourceFacts:
        raise ValueError("source facts must be typed")
    facts.check_at(at)
    if type(modality) is not str or modality != "text":
        raise ValueError("text source facts require text modality")
    if speaker_kind is not None and (
        type(speaker_kind) is not str or speaker_kind not in _SPEAKER_KINDS
    ):
        raise ValueError("text source speaker must be a closed code or None")


# Only this module holds the token, so only the Screener makes a ScreenedSource.
_MADE_BY_SCREENER = object()


class ScreenedSource:
    """A source after screening. The writer op for sources takes only this type."""

    __slots__ = ("locator", "messages", "counts", "dropped_whole")

    locator: str
    messages: tuple[ScreenedMessage, ...]
    counts: Mapping[str, int]
    dropped_whole: bool

    def __init__(
        self,
        *,
        locator: str,
        messages: tuple[ScreenedMessage, ...],
        counts: Mapping[str, int],
        dropped_whole: bool,
        _token: object = None,
    ) -> None:
        if _token is not _MADE_BY_SCREENER:
            raise TypeError("a ScreenedSource is made by Screener only")
        object.__setattr__(self, "locator", locator)
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "counts", counts)
        object.__setattr__(self, "dropped_whole", dropped_whole)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("a ScreenedSource is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("a ScreenedSource is read-only")

    def __reduce__(self) -> object:
        # A copy or a pickle would rebuild the object outside the screener.
        raise TypeError("a ScreenedSource cannot be copied or pickled")

    def content(self) -> str:
        """The text the sources table stores: a JSON array of the kept messages."""
        rows = [message.as_row() for message in self.messages]
        return json.dumps(rows, sort_keys=True, ensure_ascii=False)

    def __repr__(self) -> str:
        return (
            f"ScreenedSource(locator_length={len(self.locator)}, messages={len(self.messages)}, "
            f"counts={dict(self.counts)}, dropped_whole={self.dropped_whole})"
        )


@dataclass(frozen=True, repr=False)
class ScreeningConfig:
    rename: RenameMap = field(default_factory=RenameMap)
    denylist: Denylist = field(default_factory=Denylist)
    own_identifiers: frozenset[str] = frozenset()
    sensitive_classes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.rename, RenameMap):
            raise TypeError("ScreeningConfig.rename must be a RenameMap")
        if not isinstance(self.denylist, Denylist):
            raise TypeError("ScreeningConfig.denylist must be a Denylist")
        if isinstance(self.own_identifiers, str) or isinstance(self.sensitive_classes, str):
            raise TypeError("own_identifiers and sensitive_classes are sets, not one string")
        object.__setattr__(self, "own_identifiers", frozenset(self.own_identifiers))
        object.__setattr__(self, "sensitive_classes", frozenset(self.sensitive_classes))
        if not all(isinstance(value, str) for value in self.own_identifiers):
            raise TypeError("ScreeningConfig.own_identifiers must hold strings")
        if not self.sensitive_classes <= OPT_IN_CLASSES:
            raise ValueError("ScreeningConfig.sensitive_classes holds a class this build lacks")

    @classmethod
    def from_environment(
        cls, *, own_identifiers: Iterable[str] = (), sensitive_classes: Iterable[str] = ()
    ) -> "ScreeningConfig":
        """GROVE_RENAME_MAP and GROVE_DENYLIST name the files. Unset is empty outside production
        and a ConfigError with GROVE_ENV=production; any fault in a set file is a ConfigError, so
        the screener never runs half-configured."""
        settings = load_screening_settings()
        return cls(
            rename=load_rename_map(settings.rename_map, RENAME_MAP_VAR),
            denylist=load_denylist(settings.denylist, DENYLIST_VAR),
            own_identifiers=frozenset(own_identifiers),
            sensitive_classes=frozenset(sensitive_classes),
        )

    def __repr__(self) -> str:
        return (
            f"ScreeningConfig(rename={self.rename!r}, denylist={self.denylist!r}, "
            f"own_identifiers={len(self.own_identifiers)}, "
            f"sensitive_classes={sorted(self.sensitive_classes)})"
        )


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    step: int
    cls: str
    replacement: str = field(default="", repr=False)
    key: str = field(default="", repr=False)


def merge_spans(spans: Iterable[_Span]) -> list[_Span]:
    """Overlapping spans become one span over their union; the lowest step gives the class."""
    merged: list[_Span] = []
    for span in sorted(spans, key=lambda s: (s.start, s.step, -s.end)):
        if merged and span.start < merged[-1].end:
            top = merged[-1]
            winner = top if top.step <= span.step else span
            merged[-1] = _Span(
                top.start,
                max(top.end, span.end),
                winner.step,
                winner.cls,
                winner.replacement,
                winner.key,
            )
        else:
            merged.append(span)
    return merged


class _Run:
    """The state of one screened source: its counts and the numbers of distinct contacts."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.numbers: dict[tuple[str, str], int] = {}
        self.next_number: Counter[str] = Counter()

    def contact_mask(self, cls: str, key: str) -> str:
        number = self.numbers.get((cls, key))
        if number is None:
            self.next_number[cls] += 1
            number = self.numbers[(cls, key)] = self.next_number[cls]
        return f"[{CONTACT_WORDS[cls]} {number}]"


class Screener:
    def __init__(self, config: ScreeningConfig) -> None:
        if not isinstance(config, ScreeningConfig):
            raise TypeError("Screener takes a ScreeningConfig")
        self._config = config
        self._own = OwnIdentifiers.parse(config.own_identifiers)
        self._pay = PAY_AMOUNT in config.sensitive_classes

    def __repr__(self) -> str:
        return f"Screener({self._config!r})"

    def screen_text(
        self, text: str, *, sender: str | None = None, key: str = "text"
    ) -> ScreenedSource:
        """One message with no locator: a remember request or a note."""
        return self.screen_source("", [Message(key=key, sender=sender, text=text)])

    def screen_source(
        self,
        locator: str,
        messages: Iterable[Message],
        *,
        source_identity: tuple[str, str] | None = None,
    ) -> ScreenedSource:
        if not isinstance(locator, str):
            raise TypeError("the locator must be a str")
        items = list(messages)
        if not all(isinstance(message, Message) for message in items):
            raise TypeError("screen_source takes Message objects")
        if any(message.source_facts is not None for message in items):
            for message in items:
                check_text_transport(source_identity, message.source_facts, message.at)
                _check_message_facts(
                    message.source_facts, message.at, message.speaker_kind, message.modality
                )
        run = _Run()

        place = _Text(locator, file_name=True)
        dropped_whole = bool(self._config.denylist.find(place.folded))
        screened_locator = self._screen(run, place, LOCATOR_STEPS)

        kept: list[ScreenedMessage] = []
        if dropped_whole:
            run.counts[DENYLISTED] += len(items)
        else:
            for message in items:
                screened = self._screen_message(run, message)
                if screened is not None:
                    if message.source_facts is not None:
                        screened = replace(screened, source_facts=message.source_facts)
                    kept.append(screened)

        counts = {cls: run.counts[cls] for cls in CLASSES if run.counts[cls] > 0}
        return ScreenedSource(
            locator=screened_locator,
            messages=tuple(kept),
            counts=MappingProxyType(counts),
            dropped_whole=dropped_whole,
            _token=_MADE_BY_SCREENER,
        )

    def _screen_message(self, run: _Run, message: Message) -> ScreenedMessage | None:
        sender = None if message.sender is None else _Text(message.sender)
        text = _Text(message.text)
        denylist = self._config.denylist
        if denylist.holds(None if sender is None else sender.folded) or denylist.holds(text.folded):
            run.counts[DENYLISTED] += 1
            return None
        dropped = self._drop_class(sender, text)
        if dropped is not None:
            run.counts[dropped] += 1
            return None
        # Sender, then text, then key: contact numbers follow first appearance in that order.
        screened_sender = None if sender is None else self._screen(run, sender, SENDER_STEPS)
        screened_text = self._screen(run, text, TEXT_STEPS, _said_by(sender))
        screened_key = self._screen(run, _Text(message.key), KEY_STEPS)
        return ScreenedMessage(
            key=screened_key,
            sender=screened_sender,
            text=screened_text,
            at=message.at,
            speaker_kind=message.speaker_kind,
            modality=message.modality,
        )

    @staticmethod
    def _drop_class(sender: "_Text | None", text: "_Text") -> str | None:
        """Step 2, on the detector copy of the text: a one-time code first, then a link."""
        views = text.copy.views
        said_by = _said_by(sender)
        if any(detectors.one_time_codes(view, said_by) for view in views):
            return ONE_TIME_CODE
        if any(detectors.reset_links(view) for view in views):
            return RESET_LINK
        return None

    def _spans(
        self, text: "_Text", steps: frozenset[int], said_by: str | None = None
    ) -> list[_Span]:
        spans: list[_Span] = []
        if STEP_DENYLIST in steps:
            for start, end in self._config.denylist.find(text.folded):
                spans.append(_Span(start, end, STEP_DENYLIST, DENYLISTED, KEPT_OUT))
        if STEP_RENAME in steps:
            for start, end, label in self._config.rename.find(text.folded):
                spans.append(_Span(start, end, STEP_RENAME, RENAMED, label))

        copy = text.copy
        found: list[tuple[int, Hit]] = []
        for view in copy.views:
            if STEP_WEAK_CODE_OR_LINK in steps:
                found += [
                    (STEP_WEAK_CODE_OR_LINK, hit) for hit in detectors.weak_codes(view, said_by)
                ]
                found += [(STEP_WEAK_CODE_OR_LINK, hit) for hit in detectors.weak_links(view)]
            if STEP_CREDENTIAL in steps:
                found += [(STEP_CREDENTIAL, hit) for hit in detectors.credentials(view)]
            if STEP_CARD in steps:
                found += [(STEP_CARD, hit) for hit in detectors.cards(view)]
            if STEP_CONTACT in steps:
                found += [(STEP_CONTACT, hit) for hit in detectors.contacts(view, self._own)]
            if STEP_PAY in steps and self._pay:
                found += [(STEP_PAY, hit) for hit in detectors.pay_amounts(view)]
        # Phones only: an own address or handle holds "_", and the view would cut it. The rename
        # map and the denylist read this view through the folded copy.
        if text.file_name and "_" in copy.text:
            spaced = file_name_view(copy.views[-1])
            found += [
                (STEP_CONTACT, hit)
                for hit in detectors.contacts(spaced, self._own)
                if hit.cls == CONTACT_PHONE
            ]
        for step, hit in found:
            start, end = copy.span(hit.start, hit.end)
            spans.append(_Span(start, end, step, hit.cls, MASKS.get(hit.cls, ""), hit.key))
        return spans

    def _screen(
        self, run: _Run, text: "_Text", steps: frozenset[int], said_by: str | None = None
    ) -> str:
        merged = merge_spans(self._spans(text, steps, said_by))
        # Numbers follow first appearance, so they are given left to right before the splice.
        replacements = []
        for span in merged:
            if span.cls in CONTACT_WORDS:
                replacement = run.contact_mask(span.cls, span.key)
            else:
                replacement = span.replacement
            # A denylisted term in a locator counts through its messages, not here.
            if span.step != STEP_DENYLIST:
                run.counts[span.cls] += 1
            replacements.append(replacement)
        # Right to left over the original offsets, joined once.
        pieces = []
        cursor = len(text.raw)
        for span, replacement in reversed(list(zip(merged, replacements, strict=True))):
            pieces += [text.raw[span.end : cursor], replacement]
            cursor = span.start
        pieces.append(text.raw[:cursor])
        return "".join(reversed(pieces))


class _Text:
    """One string and its two comparison copies, each built once and only when asked for."""

    __slots__ = ("raw", "file_name", "_folded", "_copy")

    def __init__(self, raw: str, *, file_name: bool = False) -> None:
        self.raw = raw
        self.file_name = file_name
        self._folded: Normalised | None = None
        self._copy: Normalised | None = None

    @property
    def folded(self) -> Normalised:
        """The copy names match on: NFKC and case folded."""
        if self._folded is None:
            self._folded = Normalised(self.raw, fold_case=True, file_name=self.file_name)
        return self._folded

    @property
    def copy(self) -> Normalised:
        """The copy the detectors read: compatibility forms and invisible characters folded away,
        case kept, so a zero-width space cannot split a key or a card number."""
        if self._copy is None:
            self._copy = Normalised(self.raw, fold_case=False)
        return self._copy

    def __repr__(self) -> str:
        return f"_Text(length={len(self.raw)})"


def _said_by(sender: _Text | None) -> str | None:
    """The sender rules need no form as written, so the read view alone serves them."""
    return None if sender is None else sender.copy.views[-1]
