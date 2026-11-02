"""The one place that assembles a prompt for a model or an agent run (D14).

The instruction part is the bytes of the data rules, a blank line, and the bytes of one named
instruction file. Only the operator writes those files, through review, and nothing here formats
them. No parameter takes caller text for that part: stored and ingested text reaches a model only
in the data part, as escaped blocks labelled with validated codes and ids.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import InitVar, dataclass, field
from importlib.resources import files
from pathlib import Path

from grove.prompts.slots import find_slots
from grove.store.enums import ENUMS

DATA_RULES = "grove.data-rules.v1"
TAG = "untrusted_data"
BLOCK_KINDS = ("source", "item", "belief", "question", "receipt")
NAME_FORM = r"[a-z0-9.-]+\.v[0-9]+"

# The data rules end with one newline, so this separator leaves exactly one blank line.
_SEPARATOR = "\n"
_NAME = re.compile(r"(?P<base>[a-z0-9.-]+)\.v(?P<version>[0-9]+)")
# The text form of grove.ids.new_id: lowercase UUID, version 7, the RFC variant.
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
# NFKC folds these six to &, < or > (small and full-width forms). A model may read them as the
# ASCII ones, so they become character references too and no look-alike can pass for a tag.
LOOKALIKES = "\ufe60\ufe64\ufe65\uff06\uff1c\uff1e"
_ESCAPE = re.compile("[&<>" + LOOKALIKES + "]")
_BUILT = object()


class PromptError(ValueError):
    """A prompt cannot be built. The message names the field or the file, never a value."""


@dataclass(frozen=True)
class DataBlock:
    """One labelled piece of data. Every field but `text` is a validated code or id."""

    kind: str
    speaker: str
    source_type: str | None
    ref: str | None
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        _check_block(self)


@dataclass(frozen=True)
class Prompt:
    """Only build_prompt makes one, so a Prompt's instruction part always comes from files.

    For the name grove.extract.v1, instruction_name is grove.extract and instruction_version 1.
    """

    instruction_name: str
    instruction_version: int
    instructions: str = field(repr=False)
    data: str = field(repr=False)
    # An InitVar left out takes its default, so dataclasses.replace cannot swap the parts either.
    _built: InitVar[object] = None

    def __post_init__(self, _built: object) -> None:
        if _built is not _BUILT:
            raise TypeError("a Prompt comes only from build_prompt")

    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.instructions},
            {"role": "user", "content": self.data},
        ]


def build_prompt(name: str, blocks: Sequence[DataBlock], *, root: Path | None = None) -> Prompt:
    """`root` is the folder that holds the named file (tests pass a temporary one). The data
    rules always come from the package, so no root can leave them out."""
    match = _NAME.fullmatch(name) if type(name) is str else None
    if match is None:
        raise PromptError(f"name must match {NAME_FORM}")
    rules = _read(instructions_root(), DATA_RULES)
    named = _read(instructions_root() if root is None else root, name)
    return Prompt(
        match["base"],
        int(match["version"]),
        rules + _SEPARATOR + named,
        render_blocks(blocks),
        _BUILT,
    )


def render_blocks(blocks: Iterable[DataBlock]) -> str:
    # Each error below is raised outside its except clause, so it carries no __context__, and
    # the OS error (which names the path) or a bad value goes nowhere.
    try:
        items: tuple | None = tuple(blocks)
    except TypeError:
        items = None
    if items is None:
        raise PromptError("blocks must be a sequence of DataBlock")
    return "\n".join(_render(n, block) for n, block in enumerate(items, start=1))


def escape(text: str) -> str:
    return _ESCAPE.sub(lambda m: _ESCAPES.get(m.group(), f"&#x{ord(m.group()):X};"), text)


def instructions_root() -> Path:
    root = files("grove.prompts") / "instructions"
    if not isinstance(root, Path):
        raise PromptError("the instruction files are not in a folder on disk")
    return root


def _render(n: int, block: object) -> str:
    # Checked again here: the codes go into attributes unescaped, so a block changed after it
    # was made (object.__setattr__) must not reach the text.
    if type(block) is not DataBlock:
        raise PromptError(f"blocks item {n} is not a DataBlock")
    _check_block(block)
    attrs = [f'n="{n}"', f'kind="{block.kind}"', f'speaker="{block.speaker}"']
    if block.source_type is not None:
        attrs.append(f'source_type="{block.source_type}"')
    if block.ref is not None:
        attrs.append(f'ref="{block.ref}"')
    return f"<{TAG} {' '.join(attrs)}>\n{escape(block.text)}\n</{TAG}>"


def _check_block(block: DataBlock) -> None:
    # type() and not isinstance(): a str subclass can change == or its text after the check.
    _check_code("kind", block.kind, BLOCK_KINDS, "block kind")
    _check_code("speaker", block.speaker, ENUMS["speaker_kind"], "speaker_kind")
    if block.source_type is not None:
        _check_code("source_type", block.source_type, ENUMS["source_type"], "source_type")
    if block.ref is not None and not (type(block.ref) is str and _ID.fullmatch(block.ref)):
        raise PromptError("DataBlock.ref is not an id")
    if type(block.text) is not str:
        raise PromptError("DataBlock.text is not text")


def _check_code(name: str, value: object, allowed: tuple[str, ...], list_name: str) -> None:
    if type(value) is not str or value not in allowed:
        raise PromptError(f"DataBlock.{name} is not a {list_name} code")


def _read(folder: Path, name: str) -> str:
    if not isinstance(folder, Path):
        raise PromptError("root must be a Path")
    try:
        top: Path | None = folder.resolve(strict=True)
        path = (folder / f"{name}.md").resolve(strict=True)
    except (OSError, RuntimeError):  # RuntimeError: a symlink loop on Python 3.12
        top = None
    if top is None:
        raise PromptError("no instruction file has that name")
    if not path.is_relative_to(top) or not path.is_file():
        raise PromptError("the name does not resolve to a file inside root")
    try:
        text: str | None = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None:
        raise PromptError(f"instruction file {name} is not UTF-8")
    slots = find_slots(text)
    if slots:
        first = slots[0]
        raise PromptError(f"instruction file {name} holds a slot ({first.form}, line {first.line})")
    return text
