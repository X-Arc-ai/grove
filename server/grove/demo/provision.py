"""Idempotent demo provision through normal account, screened queue and receipt APIs.

No address, raw text, credential or private path is emitted. A queued request is not ready.
The separately running production Learner supplies the graph commit and actual saved receipt.
"""

import argparse
import os
import stat
import time
from collections.abc import Callable
from typing import Any

from grove import clock
from grove.demo_policy import (
    DEMO_INPUTS,
    DEMO_SEED_CLIENT,
    DEMO_VERSION,
    DemoDenied,
    configured_email_hash,
    require_designation,
)
from grove.mcp.store import StoreHandler
from grove.mcp.tools import ToolContext
from grove.store.connection import open_driver
from grove.writer.accounts import default_world_id, email_hash, ensure_demo_account


def private_email_hash() -> str:
    """Open the private file once; validate the same descriptor before a bounded read."""
    path = os.environ.get("GROVE_DEMO_EMAIL_FILE")
    if not path:
        raise DemoDenied()
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_size > 512
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise DemoDenied()
        raw = os.read(descriptor, 513)
        if len(raw) > 512:
            raise DemoDenied()
        address = raw.decode("utf-8").strip()
        if not address or len(address) > 254 or address.count("@") != 1:
            raise DemoDenied()
        if any(character.isspace() for character in address):
            raise DemoDenied()
        hashed = email_hash(address)
        if hashed != configured_email_hash():
            raise DemoDenied()
        return hashed
    except (OSError, UnicodeError, ValueError):
        raise DemoDenied() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def provision(
    driver: Any,
    hashed_address: str,
    *,
    wait_seconds: int = 300,
    world_id_for: Callable[[str], str] = default_world_id,
    handler: StoreHandler | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int | str]:
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 1800:
        raise DemoDenied()
    account = ensure_demo_account(driver, hashed_address, world_id_for=world_id_for)
    require_designation(driver, account.world_id, account.user_id)
    context = ToolContext(
        driver,
        account.world_id,
        account.user_id,
        DEMO_SEED_CLIENT,
        frozenset({"grove.read", "grove.write"}),
        "operator-demo-provision",
    )
    store = handler or StoreHandler(wait_seconds=0)
    arguments = [
        {
            "text": text,
            "verbatim": True,
            "host_message_id": f"{DEMO_VERSION}-{index}",
            "idempotency_key": f"{DEMO_VERSION}-{index}",
        }
        for index, text in enumerate(DEMO_INPUTS)
    ]
    deadline = clock.monotonic() + wait_seconds
    # Bound iterations too, including under a fixed or defective clock.
    for _ in range(wait_seconds + 1):
        saved = 0
        for argument in arguments:
            result = store(context, argument)
            if result.get("state") == "saved" and result.get("receipt", {}).get("state") == "saved":
                saved += 1
            elif result.get("state") != "saving":
                raise DemoDenied()
        if saved == len(arguments):
            # A saved receipt can represent an empty pass. The actual guarded read must find
            # retained facts before the operator receives a ready result.
            from grove.reads.demo_current import DemoRecallHandler

            recalled = DemoRecallHandler()(context, {"about": "Cedar"})
            if recalled.get("items"):
                return {"state": "ready", "saved_requests": saved}
            return {"state": "saved", "saved_requests": saved}
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            break
        sleep(min(1.0, remaining))
    return {"state": "saving", "saved_requests": saved}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the restricted invented-data demo.")
    parser.add_argument("--wait-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        hashed = private_email_hash()
        with open_driver() as driver:
            result = provision(driver, hashed, wait_seconds=args.wait_seconds)
        print(f"demo {result['state']}; saved requests: {result['saved_requests']}")
        return 0 if result["state"] == "ready" else 2
    except Exception:
        # Backend exception text can contain queries, paths or credentials.
        print("demo preparation refused or unavailable")
        return 1
