"""The rename map and the denylist (slice row 4), and the comparison copy they match on.

Both match on whole words, case-insensitively, after Unicode NFKC normalisation and case folding,
so a full-width or compatibility form cannot dodge a match. The output keeps the original text
except for the replaced spans, so a match in the comparison copy maps back to original offsets.

File formats (fixed, never extended):
- rename.json: one JSON object; keys are name forms, values are the label.
- denylist.txt: UTF-8, one term per line; `#` starts a comment anywhere on the line.

No error, repr or log names a form, a term or a label: the files are kept off the repo because
their content is the names. An error names the variable and a line or an entry position.
"""

import json
import re
import unicodedata
from collections.abc import Iterable, Iterator
from pathlib import Path

from grove.config import ConfigError, read_private_file

_WORD = re.compile(r"\w")
_ASCII_RUN = re.compile(r"[\x00-\x7f]+")


def _is_mark(ch: str) -> bool:
    return unicodedata.category(ch).startswith("M")


def _clusters(text: str, start: int, stop: int) -> Iterator[tuple[int, int]]:
    """A base character with the marks after it. NFKC composes inside a cluster, not across."""
    begin = start
    for index in range(start + 1, stop):
        if not _is_mark(text[index]):
            yield begin, index
            begin = index
    if stop > start:
        yield begin, stop


def _normalise(piece: str, fold_case: bool) -> str:
    out = unicodedata.normalize("NFKC", piece)
    if fold_case:
        out = unicodedata.normalize("NFKC", out.casefold())
    # Format characters (zero-width space, soft hyphen, joiners) are invisible: dropping them
    # from the copy stops "Or<ZWSP>la" from dodging a match.
    return "".join(ch for ch in out if unicodedata.category(ch) != "Cf")


class Normalised:
    """A comparison copy of a text, with the original span of every copy character.

    fold_case=True is the copy names match on. fold_case=False keeps case for the detectors,
    whose key formats are case-sensitive.
    """

    __slots__ = ("text", "_starts", "_ends")

    def __init__(self, original: str, *, fold_case: bool) -> None:
        self._starts: list[int] | None
        self._ends: list[int] | None
        if original.isascii():
            # ASCII has no format characters and NFKC leaves it alone: offsets are the identity.
            self.text = original.lower() if fold_case else original
            self._starts = self._ends = None
            return
        pieces: list[str] = []
        starts: list[int] = []
        ends: list[int] = []

        def add_clusters(begin: int, stop: int) -> None:
            for start, end in _clusters(original, begin, stop):
                piece = _normalise(original[start:end], fold_case)
                pieces.append(piece)
                starts.extend([start] * len(piece))
                ends.extend([end] * len(piece))

        # ASCII runs map one to one, so only the other characters go cluster by cluster.
        position = 0
        for run in _ASCII_RUN.finditer(original):
            begin, stop = run.span()
            if begin > position:
                add_clusters(position, begin)
            # A mark after the run composes with its last character, so that character goes
            # with the cluster that follows.
            if stop < len(original) and _is_mark(original[stop]):
                stop -= 1
            if stop > begin:
                chunk = original[begin:stop]
                pieces.append(chunk.lower() if fold_case else chunk)
                starts.extend(range(begin, stop))
                ends.extend(range(begin + 1, stop + 1))
            position = max(position, stop)
        add_clusters(position, len(original))
        self.text = "".join(pieces)
        self._starts, self._ends = starts, ends

    def span(self, start: int, end: int) -> tuple[int, int]:
        """The original span of copy[start:end]; end > start."""
        if self._starts is None or self._ends is None:
            return start, end
        return self._starts[start], self._ends[end - 1]

    def __repr__(self) -> str:
        return f"Normalised(length={len(self.text)})"


def name_key(text: str) -> str:
    """The comparison form of a name: folded, with each whitespace run as one space."""
    return " ".join(Normalised(text, fold_case=True).text.split())


def _compile(keys: Iterable[str]) -> re.Pattern[str] | None:
    # Longest first: at one position the regex takes the first alternative that matches.
    ordered = sorted(set(keys), key=lambda key: (-len(key), key))
    if not ordered:
        return None
    # Whitespace in a form matches any run of whitespace: an export may hold a non-breaking or a
    # double space between two words of one name.
    body = "|".join(r"\s+".join(map(re.escape, key.split(" "))) for key in ordered)
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)")


def _check_key(key: str, where: str, source: str) -> None:
    if not key:
        raise ConfigError(f"{source}: {where} is empty")
    if not _WORD.search(key):
        raise ConfigError(f"{source}: {where} holds no letter or digit")


class RenameMap:
    """Name forms to labels. One pass: a label put in is never matched again."""

    __slots__ = ("_labels", "_pattern")

    def __init__(
        self, entries: Iterable[tuple[object, object]] = (), *, source: str = "rename map"
    ):
        found: dict[str, tuple[int, str]] = {}
        for position, (form, label) in enumerate(entries, start=1):
            where = f"entry {position}"
            if not isinstance(form, str) or not isinstance(label, str):
                raise ConfigError(f"{source}: {where} must map a string to a string")
            key = name_key(form)
            _check_key(key, f"the form of {where}", source)
            label = label.strip()
            if not label:
                raise ConfigError(f"{source}: the label of {where} is empty")
            seen = found.get(key)
            if seen is None:
                found[key] = (position, label)
            elif seen[1] != label:
                raise ConfigError(
                    f"{source}: entries {seen[0]} and {position} give one form two labels"
                )
        self._labels = {key: label for key, (_, label) in found.items()}
        self._pattern = _compile(self._labels)

    @classmethod
    def from_mapping(cls, mapping: dict[str, str], *, source: str = "rename map") -> "RenameMap":
        return cls(mapping.items(), source=source)

    @property
    def form_count(self) -> int:
        return len(self._labels)

    @property
    def label_count(self) -> int:
        return len(set(self._labels.values()))

    def find(self, folded: Normalised) -> list[tuple[int, int, str]]:
        """Original spans and their labels, left to right, never overlapping."""
        if self._pattern is None:
            return []
        found = []
        for match in self._pattern.finditer(folded.text):
            start, end = folded.span(match.start(), match.end())
            found.append((start, end, self._labels[" ".join(match.group().split())]))
        return found

    def __repr__(self) -> str:
        return f"RenameMap(forms={self.form_count}, labels={self.label_count})"


class Denylist:
    """Terms whose message is dropped and counted."""

    __slots__ = ("_count", "_pattern")

    def __init__(self, terms: Iterable[str] = (), *, source: str = "denylist") -> None:
        keys = set()
        for position, term in enumerate(terms, start=1):
            if not isinstance(term, str):
                raise ConfigError(f"{source}: term {position} must be a string")
            key = name_key(term)
            _check_key(key, f"term {position}", source)
            keys.add(key)
        self._count = len(keys)
        self._pattern = _compile(keys)

    @property
    def term_count(self) -> int:
        return self._count

    def find(self, folded: Normalised) -> list[tuple[int, int]]:
        if self._pattern is None:
            return []
        return [folded.span(m.start(), m.end()) for m in self._pattern.finditer(folded.text)]

    def holds(self, folded: Normalised | None) -> bool:
        if folded is None or self._pattern is None:
            return False
        return self._pattern.search(folded.text) is not None

    def __repr__(self) -> str:
        return f"Denylist(terms={self.term_count})"


class _Pairs(list):
    """A JSON object as its pairs in file order, so a duplicate key is seen and not lost."""


def _parse_json(text: str) -> tuple[object, str | None]:
    """The parsed value, or why it failed. The error object holds the whole text, so it must not
    travel as the context of the ConfigError."""
    try:
        return json.loads(text, object_pairs_hook=_Pairs), None
    except json.JSONDecodeError as error:
        return None, f"not valid JSON at line {error.lineno} column {error.colno}"
    except (ValueError, RecursionError):
        return None, "not valid JSON: nested too deep or a number too long"


def parse_rename_map(text: str, source: str) -> RenameMap:
    top, error = _parse_json(text)
    if error is not None:
        raise ConfigError(f"{source}: {error}")
    if not isinstance(top, _Pairs):
        raise ConfigError(f"{source}: the file must hold one JSON object")
    return RenameMap(top, source=source)


def parse_denylist(text: str, source: str) -> Denylist:
    terms = []
    # Split on newlines only, so the line numbers in errors are the ones an editor shows.
    for number, line in enumerate(text.split("\n"), start=1):
        # A trailing comment left in the term would make it never match, and its message pass.
        term = line.partition("#")[0].strip()
        if not term:
            continue
        _check_key(name_key(term), f"line {number}", source)
        terms.append(term)
    return Denylist(terms, source=source)


def load_rename_map(path: Path | None, variable: str) -> RenameMap:
    """Unset is empty. Set means the file must exist, be private and parse: fail closed."""
    if path is None:
        return RenameMap()
    return parse_rename_map(read_private_file(path, variable), variable)


def load_denylist(path: Path | None, variable: str) -> Denylist:
    if path is None:
        return Denylist()
    return parse_denylist(read_private_file(path, variable), variable)
