"""Store transport over the published screened-source, queue and receipt contracts.

No model call, writer algorithm, authentication or default registration lives here. The caller
supplies an authenticated ToolContext. Public activation still needs the common D14 release guard.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime

from grove import clock
from grove.api.errors import ApiError, invalid_arguments, not_found, unavailable
from grove.config import ConfigError
from grove.ids import new_id
from grove.mcp.tools import ToolContext
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.errors import STORE_UNAVAILABLE, Neo4jError
from grove.store.query import WorldQuery
from grove.writer import Caller, enqueue_request, request_state
from grove.writer.clocks import ClockError, parse_instant
from grove.writer.errors import (
    HTTP_STATUS,
    BatchDeclined,
    InvalidRecord,
    ReceiptUnavailable,
    WriteFailed,
)
from grove.writer.ingest import ensure_connector
from grove.writer.receipts import render_receipt
from grove.writer.requests import RequestState
from grove.writer.sources import write_screened_source

MAX_TEXT = 32_768
MAX_KEY = 1_024
MAX_ABOUT = 50
_ITEM_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_CLIENT_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,48}")
_RETRY_MESSAGE = "the request could not be confirmed; retry with the same arguments and key"
_FIELDS = frozenset(
    {"mode", "text", "verbatim", "host_message_id", "said_at", "about", "idempotency_key"}
)
STORE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "verbatim", "idempotency_key"],
    "properties": {
        "mode": {"type": "string", "enum": ["remember"], "default": "remember"},
        "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT},
        "verbatim": {"type": "boolean"},
        "host_message_id": {"type": "string", "minLength": 1, "maxLength": MAX_KEY},
        "said_at": {"type": "string", "format": "date-time", "maxLength": 64},
        "about": {
            "type": "array",
            "maxItems": MAX_ABOUT,
            "uniqueItems": True,
            "items": {"type": "string", "pattern": _ITEM_ID.pattern},
        },
        "idempotency_key": {"type": "string", "minLength": 1, "maxLength": MAX_KEY},
    },
    "allOf": [
        {
            "if": {"properties": {"verbatim": {"const": True}}},
            "then": {"required": ["host_message_id"]},
        }
    ],
}


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise invalid_arguments("the field must be bounded non-empty text", field)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise invalid_arguments("the field must be valid UTF-8 text", field) from None
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _client_prefix(client_id: str) -> str:
    # Metadata-document clients may use a URL, so only machine codes enter the public request ID.
    code = client_id if _CLIENT_CODE.fullmatch(client_id) else _digest(client_id)[:32]
    return f"{code}:"


def request_identity(context: ToolContext, key: str) -> str:
    """Bound before hashing; bind a retry to its world and authenticated client, not its token."""
    key = _text(key, "idempotency_key", MAX_KEY)
    return _client_prefix(context.client_id) + _digest([context.world_id, context.client_id, key])


def _scope(context: ToolContext, scope: str) -> None:
    if scope not in context.scopes:
        raise ApiError(
            403, "scope_denied", "the credential lacks the required scope", details={"scope": scope}
        )


def _arguments(arguments: dict) -> tuple[dict, datetime | None]:
    if not isinstance(arguments, dict) or set(arguments) - _FIELDS:
        raise invalid_arguments("the request contains unsupported fields", "arguments")
    if arguments.get("mode", "remember") != "remember":
        raise invalid_arguments("this component supports remember only", "mode")
    checked = {
        "mode": "remember",
        "text": _text(arguments.get("text"), "text", MAX_TEXT),
        "idempotency_key": _text(arguments.get("idempotency_key"), "idempotency_key", MAX_KEY),
    }
    verbatim = arguments.get("verbatim")
    if type(verbatim) is not bool:
        raise invalid_arguments("verbatim must be true or false", "verbatim")
    checked["verbatim"] = verbatim
    message = arguments.get("host_message_id")
    if verbatim and message is None:
        raise invalid_arguments("verbatim requires host_message_id", "host_message_id")
    checked["host_message_id"] = (
        _text(message, "host_message_id", MAX_KEY) if "host_message_id" in arguments else None
    )
    when = None
    if "said_at" in arguments:
        raw = _text(arguments["said_at"], "said_at", 64)
        try:
            when = parse_instant(raw).astimezone(UTC)
        except (ClockError, ValueError, OverflowError):
            raise invalid_arguments("said_at must be an aware timestamp", "said_at") from None
    checked["said_at"] = when.isoformat() if when else None
    about = arguments.get("about", [])
    if (
        not isinstance(about, list)
        or len(about) > MAX_ABOUT
        or any(not isinstance(item, str) or not _ITEM_ID.fullmatch(item) for item in about)
        or len(set(about)) != len(about)
    ):
        raise invalid_arguments("about must contain distinct bounded item IDs", "about")
    checked["about"] = sorted(about)
    return checked, when


class StoreHandler:
    """The component seam for MCP and a later REST remember adapter.

    A screener override and shorter wait support isolated component checks. Production construction
    uses the environment configuration, whose required private files fail closed when absent.
    """

    def __init__(
        self,
        *,
        screener: Screener | None = None,
        wait_seconds: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, int | float)
            or not math.isfinite(wait_seconds)
            or not 0 <= wait_seconds <= 5
        ):
            raise ValueError("wait_seconds must be between zero and five")
        self.screener = screener
        self.wait_seconds = wait_seconds
        self.sleep = sleep

    def __call__(self, context: ToolContext, arguments: dict) -> dict:
        _scope(context, "grove.write")
        checked, said_at = _arguments(arguments)
        request_id = request_identity(context, checked["idempotency_key"])
        at = clock.now(context.world_id)
        if said_at is not None and said_at > at:
            raise invalid_arguments("said_at cannot follow receipt of the request", "said_at")
        try:
            return self._enqueue(context, checked, said_at, at, request_id)
        except ConfigError:
            raise unavailable("input screening is not configured") from None
        except BatchDeclined as error:
            refusal = error.refusals[0]
            raise ApiError(
                HTTP_STATUS.get(refusal.code, 422),
                refusal.code,
                "the request was refused",
                details={"rule": refusal.rule},
                retryable=error.retryable,
            ) from None
        except InvalidRecord as error:
            raise ApiError(
                HTTP_STATUS.get(error.code, 422),
                error.code,
                "the source was refused",
                details={"rule": error.rule},
            ) from None
        except (WriteFailed, ReceiptUnavailable, *STORE_UNAVAILABLE):
            raise unavailable(_RETRY_MESSAGE) from None
        except Neo4jError as error:
            if error.is_retryable():
                raise unavailable(_RETRY_MESSAGE) from None
            raise ApiError(500, "internal", "the store request failed") from None

    def _enqueue(
        self,
        context: ToolContext,
        checked: dict,
        said_at: datetime | None,
        at: datetime,
        request_id: str,
    ) -> dict:
        # Check references before any connector or request write; another world's ID is just absent.
        about = checked["about"]
        if about:
            found = WorldQuery(context.driver, context.world_id).read(
                "MATCH (i:Item) WHERE i.id IN $ids RETURN i.id AS id", ids=about
            )
            if {row["id"] for row in found} != set(about):
                raise not_found("an about item is not available in this world")
        screener = self.screener or Screener(ScreeningConfig.from_environment())
        message_key = "m_" + _digest(checked["host_message_id"] or request_id)
        screened = screener.screen_source(
            "", [Message(key=message_key, sender=None, text=checked["text"], at=said_at or at)]
        )
        if not screened.messages or not any(message.text.strip() for message in screened.messages):
            raise ApiError(422, "contamination", "input screening retained no message")
        connector = ensure_connector(
            context.driver,
            context.world_id,
            "ai_host",
            "host:" + _digest(context.client_id),
            batch_rule="host_request",
            auth_reference="oauth",
        )

        def write_source(tx) -> str:
            source_id = new_id(tx.world_id)
            write_screened_source(
                tx,
                screened,
                source_id=source_id,
                connector_id=connector,
                parser_version="mcp-store-1",
            )
            return source_id

        caller = Caller("ai_host", context.scopes, "host:" + _digest(context.client_id))
        state = enqueue_request(
            context.driver,
            context.world_id,
            caller,
            request_id,
            write_source=write_source,
            content_key=_digest(checked),
            verbatim=checked["verbatim"],
            host_message_id=checked["host_message_id"],
            said_at=said_at,
            about=about or None,
        )
        deadline = clock.monotonic() + self.wait_seconds
        # The iteration cap also bounds a frozen or faulty test clock.
        for _ in range(math.ceil(self.wait_seconds / 0.1)):
            if state.api_state != "saving":
                break
            remaining = deadline - clock.monotonic()
            if remaining <= 0:
                break
            self.sleep(min(0.1, remaining))
            latest = request_state(context.driver, context.world_id, request_id)
            if latest is None:
                raise unavailable("the queued request is not available")
            state = latest
        return self._response(context, state)

    @staticmethod
    def _response(context: ToolContext, state: RequestState) -> dict:
        if state.api_state == "failed":
            raise unavailable("the request failed; inspect its status before another attempt")
        result = state.to_api()
        if state.api_state == "saved":
            result["receipt_text"] = render_receipt(context.driver, context.world_id, state.receipt)
        return result
