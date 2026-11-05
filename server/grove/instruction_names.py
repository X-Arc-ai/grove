"""The file names OpenCode acts on, and the one rule that keeps them out of a world's folder.

A person's file is data, and OpenCode runs in that person's folder (grove.runner.loop.pass_folder).
The API (grove.api.agent_files) refuses these names. The runner refuses a folder that holds one at
any depth, which also catches a name that an earlier build let in. A step that copies a person's
material into a world's folder uses the same function. This module needs only the standard
library, so that the API and the runner both import it.

What OpenCode 1.18.32 does under the runner's flags (tests/runner/test_opencode.py):
- AGENTS.md and CONTEXT.md in a folder below the working folder: its read tool adds them to the
  read of any other file there, as "Instructions from:" text. No flag stops this.
- .opencode in the working folder or a parent: it runs the folder's plugins as code. No flag stops
  this. A plain file of that name stops OpenCode when it starts.
- CLAUDE.md, the same two names at the top of the working folder, opencode.json and opencode.jsonc:
  only loop.OPENCODE_FLAGS keeps them out, so one changed flag would let them in.
- tui.json and tui.jsonc: config of its terminal interface, which `opencode run` does not read.
- tsconfig.json in the working folder: OpenCode is a Bun binary, and Bun maps a bare import through
  compilerOptions.paths to a person's file and runs it when OpenCode starts, before any permission.
  jsconfig.json is the same setting. bunfig.toml, package.json, node_modules and the bun lock files
  are Bun's other startup names. Bun's .env files and .bunfig.toml fall under the hidden name rule.

The API never makes a hidden name, so any other one is out of place too: .claude, .agents, .git.
The one exception is the temporary file of an upload that is running.
"""

import unicodedata

TEMP_PREFIX = ".upload-"
NAMES = frozenset(
    {
        "agents.md",
        "bun.lock",
        "bun.lockb",
        "bunfig.toml",
        "claude.md",
        "context.md",
        "jsconfig.json",
        "node_modules",
        "opencode.json",
        "opencode.jsonc",
        "package.json",
        "tsconfig.json",
        "tui.json",
        "tui.jsonc",
    }
)


def is_instruction_name(name: str) -> bool:
    """True when OpenCode could act on a file or a folder of this name, in any case."""
    folded = unicodedata.normalize("NFC", name).casefold()
    if folded.startswith("."):
        return not folded.startswith(TEMP_PREFIX)
    return folded in NAMES
