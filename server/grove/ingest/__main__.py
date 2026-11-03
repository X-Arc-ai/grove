"""`python -m grove.ingest token mint|revoke|list`: upload-only tokens, run on the server.

mint prints the raw token once, on stdout, and nowhere else: only its sha256 is stored. list
prints ids and times, never a hash or a token. Each command prints JSON lines.

Exit 0 on success. Exit 2 when an argument, the world or the settings are refused; the one line
on stderr then names the rule, never a value, because a pasted token must not echo into a log.
Exit 1 when the store cannot be reached.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Any, NoReturn, TextIO

from grove.config import ConfigError
from grove.store.connection import Driver, open_driver
from grove.store.errors import STORE_UNAVAILABLE, ScopeError
from grove.store.query import PlatformQuery
from grove.store.scope import check_world_id
from grove.writer.upload_tokens import (
    DEFAULT_MINUTES,
    MAX_MINUTES,
    TOKEN_PREFIX,
    UnknownWorld,
    mint_upload_token,
    revoke_upload_token,
)

PROG = "python -m grove.ingest"
MAX_ID_LENGTH = 128

_FIND_WORLD = "MATCH (w:World {id: $world}) RETURN w.id AS id"
_LIST_TOKENS = (
    "MATCH (t:UploadToken {world_id: $world}) "
    "RETURN t.id AS id, t.created_at AS created_at, t.expires_at AS expires_at, "
    "t.revoked_at AS revoked_at ORDER BY t.created_at, t.id"
)


class Refused(ValueError):
    """An argument this command refuses. The message names the rule, never the value."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's own text quotes what was typed, and that can be a pasted token.
        del message
        self.exit(2, f"{PROG}: the arguments are not valid; see --help\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG)
    groups = parser.add_subparsers(dest="group", required=True)
    token = groups.add_parser("token", help="upload-only tokens for POST /v1/sources/upload")
    actions = token.add_subparsers(dest="action", required=True)
    mint = actions.add_parser("mint", help="mint a token for one world and print it once")
    mint.add_argument("--world", required=True, help="the world id")
    mint.add_argument(
        "--minutes",
        default=str(DEFAULT_MINUTES),
        help=f"how long the token lives, 1 to {MAX_MINUTES}; default {DEFAULT_MINUTES}",
    )
    revoke = actions.add_parser("revoke", help="revoke a token by the id that mint printed")
    revoke.add_argument("--id", required=True, dest="token_id", help="the token id")
    listing = actions.add_parser("list", help="list a world's tokens: ids and times only")
    listing.add_argument("--world", required=True, help="the world id")
    return parser


def _minutes(text: str) -> int:
    # Read here, not by argparse's type=int, whose error would quote the value.
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= MAX_MINUTES:
        raise Refused(f"--minutes must be a whole number from 1 to {MAX_MINUTES}")
    return int(text)


def _token_id(text: str) -> str:
    if text.startswith(TOKEN_PREFIX):
        # The raw token itself: refuse it, so it is neither echoed back nor sent to the store.
        raise Refused("--id takes the id that mint printed, not the token")
    if not text or len(text) > MAX_ID_LENGTH or not text.isprintable():
        raise Refused("--id is not a token id")
    return text


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _checked(args: argparse.Namespace) -> dict[str, Any]:
    """Every argument checked before the store is opened."""
    if args.action == "mint":
        return {"world": check_world_id(args.world), "minutes": _minutes(args.minutes)}
    if args.action == "revoke":
        return {"token_id": _token_id(args.token_id)}
    return {"world": check_world_id(args.world)}


def _run(driver: Driver, action: str, checked: dict[str, Any]) -> list[dict[str, Any]]:
    if action == "mint":
        minted = mint_upload_token(driver, checked["world"], minutes=checked["minutes"])
        return [
            {
                "id": minted.id,
                "token": minted.token,
                "world_id": minted.world_id,
                "expires_at": minted.expires_at.isoformat(),
            }
        ]
    if action == "revoke":
        revoked = revoke_upload_token(driver, checked["token_id"])
        return [{"id": checked["token_id"], "revoked": revoked}]
    platform = PlatformQuery(driver)
    if not platform.read(_FIND_WORLD, world=checked["world"]):
        raise UnknownWorld("no world has this id")
    return [
        {
            "id": row["id"],
            "created_at": _iso(row["created_at"]),
            "expires_at": _iso(row["expires_at"]),
            "revoked_at": _iso(row["revoked_at"]),
        }
        for row in platform.read(_LIST_TOKENS, world=checked["world"])
    ]


def _fail(message: str, code: int = 2) -> int:
    print(f"{PROG}: {message}", file=sys.stderr)
    return code


def main(
    argv: Sequence[str] | None = None, *, driver: Driver | None = None, out: TextIO | None = None
) -> int:
    """With a driver, the caller owns it; with none, the command opens one from the environment."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 2
    try:
        checked = _checked(args)
        if driver is not None:
            lines = _run(driver, args.action, checked)
        else:
            with open_driver() as opened:
                lines = _run(opened, args.action, checked)
    except Refused as refused:
        return _fail(str(refused))
    except ScopeError:
        return _fail("--world is not a world id")
    except UnknownWorld:
        return _fail("no world has this id")
    except ConfigError as error:
        # A ConfigError names the variable, never its value.
        return _fail(str(error))
    except ValueError:
        return _fail("an argument or a setting is not valid")
    except STORE_UNAVAILABLE:
        return _fail("the store cannot be reached", code=1)
    target = out if out is not None else sys.stdout
    for line in lines:
        print(json.dumps(line), file=target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
