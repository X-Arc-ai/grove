"""`grove-brain-shell`: the shell the runner gives OpenCode (R10).

OpenCode 1.18.32 runs each bash tool call as `$SHELL -c "<command>"`, and the runner sets SHELL
to this program. Its bash permission (`brain *` only) already refuses chained, piped, substituted
and backtick forms, but it lets a redirect through (`brain get x > file` wrote a file, and
`brain propose < /etc/hosts` read one; probed on the pinned binary). So this is the second guard,
and it does not depend on OpenCode's matcher:

- No shell ever runs. The command is split here, and `brain` is started directly by its full
  path with the split words, so no expansion, redirect, pipe, list or substitution can happen.
- A command holds one `brain` call with plain words, on one line. Outside single quotes, any of
  `; & | < > ( ) $ ` \\ * ? [ ] { } ~ ! #` and any control character is refused, also inside
  double quotes where bash would expand it. Single-quoted text passes as it is.
- The one other form is `brain propose [--request ID] <<TAG`, a heredoc whose body becomes the
  batch on stdin. Nothing may follow the closing TAG line.
- `brain propose --file` is refused: an agent sends its batch in the heredoc, and a file option
  would let it send any file the runner user can read.
"""

import os
import re
import shlex
import subprocess
import sys

REFUSED_EXIT = 126
MAX_COMMAND_BYTES = 3 * 1024 * 1024
JSON_VERBS = frozenset({"propose", "judge", "retest"})
VERBS = frozenset({"status", "pass", "recall", "get", "finish", "--help", "-h"}) | JSON_VERBS
_SPECIAL = frozenset(";&|<>()$`\\*?[]{}~!#")
_DOUBLE_QUOTE_SPECIAL = frozenset("$`\\")
_HEREDOC = re.compile(
    r"\s*(?P<head>brain\s+(?:propose|judge|retest)(?:\s+[^\s<]+)*)\s*<<(?P<dash>-?)\s*"
    r"(?P<quote>['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)\s*"
)


class Refused(Exception):
    pass


def _check_plain(text: str) -> None:
    """One line of plain words: quotes are the only shell syntax allowed."""
    quote = None
    for char in text:
        if char in "\n\r" or (ord(char) < 32 and char != "\t") or ord(char) == 127:
            raise Refused("a control character or a second line")
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if quote == '"':
            if char == '"':
                quote = None
            elif char in _DOUBLE_QUOTE_SPECIAL:
                raise Refused(f"{char!r} inside double quotes")
            continue
        if char in "'\"":
            quote = char
        elif char in _SPECIAL:
            raise Refused(f"the shell character {char!r}")
    if quote is not None:
        raise Refused("an unclosed quote")


def _words(text: str) -> list[str]:
    _check_plain(text)
    try:
        words = shlex.split(text, posix=True)
    except ValueError:
        raise Refused("the words cannot be split") from None
    if not words or words[0] != "brain":
        raise Refused("only the brain command runs here")
    if len(words) > 1 and words[1] not in VERBS:
        raise Refused(f"brain has no verb {words[1]!r}")
    if (
        len(words) > 1
        and words[1] in JSON_VERBS
        and any(word == "--file" or word.startswith("--file=") for word in words[2:])
    ):
        raise Refused("brain propose takes its batch from a heredoc, not from a file")
    return words


def parse(command: str) -> tuple[list[str], bytes | None]:
    """The argv to run and the stdin to give it, or Refused."""
    if not isinstance(command, str) or not command.strip():
        raise Refused("an empty command")
    if "\x00" in command or len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise Refused("a NUL byte or a command over 3 MiB")
    first, newline, rest = command.partition("\n")
    match = _HEREDOC.fullmatch(first)
    if match is None:
        if newline and rest.strip():
            raise Refused("a second line outside a heredoc")
        return _words(first.strip()), None
    words = _words(match["head"])
    if len(words) < 2 or words[1] not in JSON_VERBS:
        raise Refused("only a JSON brain verb takes a heredoc")
    tag, strip_tabs = match["tag"], match["dash"] == "-"
    lines = rest.split("\n")
    body: list[str] = []
    for index, line in enumerate(lines):
        line = line.lstrip("\t") if strip_tabs else line
        if line.rstrip() == tag:
            if any(after.strip() for after in lines[index + 1 :]):
                raise Refused("text after the end of the heredoc")
            return words, ("\n".join(body) + "\n").encode("utf-8")
        body.append(line)
    raise Refused("the heredoc has no closing line")


def brain_path() -> str:
    explicit = os.environ.get("GROVE_BRAIN_BIN")
    if explicit:
        return explicit
    return os.path.join(os.path.dirname(sys.executable), "brain")


def _refuse(reason: str) -> int:
    sys.stderr.write(
        f"refused by the runner shell: {reason}. Only `brain <verb> <plain words>` runs, and "
        "`brain propose` takes its batch from a quoted heredoc (<<'EOF').\n"
    )
    return REFUSED_EXIT


def run(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "-c":
        return _refuse("the shell takes only -c and one command")
    try:
        words, stdin = parse(argv[1])
    except Refused as refusal:
        return _refuse(str(refusal))
    path = brain_path()
    if not os.path.isabs(path):
        return _refuse("the brain program has no full path")
    done = subprocess.run(
        [path, *words[1:]],
        input=stdin if stdin is not None else b"",
        stdout=sys.stdout.buffer,
        stderr=sys.stderr.buffer,
        check=False,
    )
    return done.returncode


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
