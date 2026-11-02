"""The slot rule (D14): an instruction file holds plain text and no slot of any kind.

A slot is text that a formatter, a template engine, a shell or OpenCode fills in: str.format and
printf forms, string.Template, mustache and Jinja braces, shell expansions, and OpenCode's
{file:...}, {env:...}, $ARGUMENTS, $1, !`command` and @path. The builder never formats an
instruction file. The rule still holds because OpenCode fills {file:...} and {env:...} in its
config and the other forms in its command templates (its docs for 1.18), the agent files sit
beside them, and a slot is the one way a later edit could put stored text where instructions go.

Some prose trips the rule on purpose: braces of any kind (a JSON example too), a dollar before a
letter or a digit, a printf form such as %s, and an @ token that holds a slash or a dot. An
instruction file says these in words instead.

check() walks every file in the instruction folder and, when they exist, every *.md under
runner/agents/ and every prompt, template and description field of runner/opencode.json. It has
the signature of the grove.lints checks. A finding names the file, the line and the form, never
the text.

Out of reach: a file that runner/opencode.json pulls in through its instructions list.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

INSTRUCTIONS = "server/grove/prompts/instructions"
AGENTS = "runner/agents"
OPENCODE = "runner/opencode.json"

# A command's template is a prompt too, and an agent's description is text a model reads to
# pick that agent. OpenCode fills {file:...} and {env:...} in any string of its config.
_PROMPT_KEYS = frozenset({"prompt", "template", "description"})

# One row per form. A text can hold one slot under two forms ({file:x} is a brace too): a
# finding per form keeps the report exact about what was seen.
FORMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("opencode-file", re.compile(r"\{file:[^}]*\}")),
    ("opencode-env", re.compile(r"\{env:[^}]*\}")),
    ("opencode-arguments", re.compile(r"\$ARGUMENTS")),
    ("opencode-shell", re.compile(r"!`[^`]*`")),
    # An email's @ follows a word character, and an agent mention such as @learner holds no
    # slash and no dot, so neither is a path.
    ("opencode-path", re.compile(r"(?<![\w@.+-])@[\w~.-]*[/.][\w~./-]*\w")),
    ("double-brace", re.compile(r"\{\{.*?\}\}", re.DOTALL)),
    ("brace", re.compile(r"\{[^{}]*\}")),
    ("dollar-brace", re.compile(r"\$\{")),
    ("dollar-paren", re.compile(r"\$\(")),
    ("dollar-name", re.compile(r"\$[A-Za-z_]\w*")),
    ("dollar-digit", re.compile(r"\$[0-9]")),
    ("percent-name", re.compile(r"%\([^)]*\)")),
    ("percent", re.compile(r"%[#0+-]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[diouxXeEfFgGcrsab]")),
    ("untrusted-data-tag", re.compile(r"</?\s*untrusted_data", re.IGNORECASE)),
)


@dataclass(frozen=True, order=True)
class Slot:
    offset: int
    form: str
    line: int


@dataclass(frozen=True, order=True)
class SlotFinding:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


@dataclass(frozen=True)
class Walk:
    texts: tuple[tuple[str, str], ...]  # (where, text) for every text the rule covers
    findings: tuple[SlotFinding, ...]  # what the walk could not read as text


def find_slots(text: str) -> list[Slot]:
    slots = [
        Slot(match.start(), form, text.count("\n", 0, match.start()) + 1)
        for form, pattern in FORMS
        for match in pattern.finditer(text)
    ]
    return sorted(slots)


def walk(root: Path) -> Walk:
    texts: list[tuple[str, str]] = []
    findings: list[SlotFinding] = []
    folder = root / INSTRUCTIONS
    if folder.is_dir() or folder.is_symlink():
        _read_tree(root, folder, "", texts, findings)
    else:
        # The builder's own folder: a walk that finds nothing to read is not a pass.
        findings.append(SlotFinding(INSTRUCTIONS, 1, "slots/missing-folder"))
    agents = root / AGENTS
    if agents.is_dir() or agents.is_symlink():
        _read_tree(root, agents, ".md", texts, findings)
    config = root / OPENCODE
    if config.exists() or config.is_symlink():
        _read_config(config, texts, findings)
    return Walk(tuple(texts), tuple(findings))


def check(root: Path, base: str | None = None) -> list[SlotFinding]:
    """`base` is unused: it keeps the signature of the grove.lints checks."""
    done = walk(root)
    findings = list(done.findings)
    for where, text in done.texts:
        findings += [
            SlotFinding(where, slot.line, f"slots/{slot.form}") for slot in find_slots(text)
        ]
    return sorted(set(findings))


def _read_tree(
    root: Path, folder: Path, suffix: str, texts: list, findings: list[SlotFinding]
) -> None:
    # A symlink can point at text outside the reviewed tree, so it is a finding, never followed.
    if folder.is_symlink():
        findings.append(SlotFinding(folder.relative_to(root).as_posix(), 1, "slots/symlink"))
        return
    for top, dirs, names in os.walk(folder):
        dirs.sort()
        for name in sorted(dirs + names):
            path = Path(top, name)
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                findings.append(SlotFinding(rel, 1, "slots/symlink"))
            elif name in names and name.endswith(suffix):
                text = _decode(path, rel, findings)
                if text is not None:
                    texts.append((rel, text))


def _read_config(config: Path, texts: list, findings: list[SlotFinding]) -> None:
    if config.is_symlink():
        findings.append(SlotFinding(OPENCODE, 1, "slots/symlink"))
        return
    text = _decode(config, OPENCODE, findings)
    if text is None:
        return
    try:
        # Pairs, not a dict: a duplicate key would hide the first value from a dict.
        data = json.loads(text, object_pairs_hook=_Pairs)
    except ValueError:
        findings.append(SlotFinding(OPENCODE, 1, "slots/not-json"))
        return
    for pointer, value in _prompt_fields(data, ""):
        where = f"{OPENCODE}#{pointer}"
        if type(value) is str:
            texts.append((where, value))
        else:
            findings.append(SlotFinding(where, 1, "slots/not-text"))


class _Pairs(list):
    """A JSON object as its (key, value) pairs, duplicates kept."""


def _prompt_fields(node: object, pointer: str):
    if isinstance(node, _Pairs):
        for key, value in node:
            here = f"{pointer}/{key.replace('~', '~0').replace('/', '~1')}"
            if key in _PROMPT_KEYS:
                yield here, value
            yield from _prompt_fields(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _prompt_fields(value, f"{pointer}/{index}")


def _decode(path: Path, rel: str, findings: list[SlotFinding]) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        findings.append(SlotFinding(rel, 1, "slots/not-utf8"))
    except OSError:
        findings.append(SlotFinding(rel, 1, "slots/unreadable"))
    return None
