"""What goes out to a provider and what comes back, in one place.

The readers never raise. A provider's answer can have any shape, and the call has already run, so
an odd field reads as absent and the call still reaches the meter. When an answer states no token
counts, the row carries an estimate, so such a call is not free against the ceiling.

The cleaners keep only what a provider needs and refuse the rest. A provider must never fetch a
URL or reach out for a caller: that would pass the egress allowlist from the far side.
"""

import math
import re
from dataclasses import dataclass
from typing import Any

from grove.models.errors import ModelRefused

_CHARS_PER_TOKEN = 4
_ROLES = frozenset({"system", "developer", "user", "assistant", "tool"})
_MESSAGE_KEYS = ("role", "content", "name", "tool_calls", "tool_call_id")
_FUNCTION_KEYS = ("name", "description", "parameters", "strict")
_RESPONSE_FORMATS = frozenset({"text", "json_object", "json_schema"})
# Raster formats only: an SVG can name a remote resource.
_DATA_IMAGES = tuple(f"data:image/{kind}" for kind in ("png", "jpeg", "jpg", "webp", "gif"))
_BEARER = re.compile(r"(?i)bearer\s+\S+")
# Under this length a part of a key could match ordinary words.
_MIN_SECRET_PART = 8


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def estimate_usage(input_chars: int, output_chars: int = 0) -> Usage:
    """Tokens for a call the provider ran or may have run but did not report: four characters a
    token. The budget then sees a call whose real usage never came."""
    return Usage(
        input_tokens=math.ceil(input_chars / _CHARS_PER_TOKEN),
        output_tokens=math.ceil(output_chars / _CHARS_PER_TOKEN),
    )


def usage_or_estimate(usage: Usage, input_chars: int, output_chars: int = 0) -> Usage:
    if usage.input_tokens or usage.output_tokens:
        return usage
    return estimate_usage(input_chars, output_chars)


def responses_usage(doc: dict) -> Usage:
    usage = _object(doc.get("usage"))
    return Usage(
        input_tokens=_count(usage.get("input_tokens")),
        cached_tokens=_count(_object(usage.get("input_tokens_details")).get("cached_tokens")),
        output_tokens=_count(usage.get("output_tokens")),
        reasoning_tokens=_count(
            _object(usage.get("output_tokens_details")).get("reasoning_tokens")
        ),
    )


def chat_usage(usage: object) -> Usage:
    usage = _object(usage)
    return Usage(
        input_tokens=_count(usage.get("prompt_tokens")),
        cached_tokens=_count(_object(usage.get("prompt_tokens_details")).get("cached_tokens")),
        output_tokens=_count(usage.get("completion_tokens")),
        reasoning_tokens=_count(
            _object(usage.get("completion_tokens_details")).get("reasoning_tokens")
        ),
    )


def responses_text(doc: dict) -> str:
    """The answer of a Responses document: its message parts, never its reasoning items."""
    parts = []
    for item in _list(doc.get("output")):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in _list(item.get("content")):
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                parts.append(text if isinstance(text, str) else "")
    return "".join(parts)


def chat_answer(doc: dict) -> tuple[str, tuple[dict, ...], str | None]:
    """The text, the tool calls and the finish reason of a Chat Completions document."""
    choices = _list(doc.get("choices"))
    choice = _object(choices[0]) if choices else {}
    message = _object(choice.get("message"))
    content = message.get("content")
    if isinstance(content, list):
        pieces = (_object(part).get("text") for part in content)
        text = "".join(piece for piece in pieces if isinstance(piece, str))
    else:
        text = content if isinstance(content, str) else ""
    calls = tuple(call for call in _list(message.get("tool_calls")) if isinstance(call, dict))
    finish = choice.get("finish_reason")
    return text, calls, finish if isinstance(finish, str) else None


def may_have_run(status: int) -> bool:
    """A 5xx other than 503 says nothing about the work behind it. A 503 and a 4xx say it did
    not run: an overload or a Modal cold start, and a refusal."""
    return status >= 500 and status != 503


def redact(text: str, secrets: tuple[str | None, ...]) -> str:
    """The text with each key, each dot-separated part of a key, and any bearer value removed."""
    for secret in secrets:
        if not secret:
            continue
        for part in (secret, *secret.split(".")):
            if len(part) >= _MIN_SECRET_PART:
                text = text.replace(part, "<redacted>")
    return _BEARER.sub("Bearer <redacted>", text)


def _data_image(url: object) -> bool:
    return isinstance(url, str) and url.startswith(_DATA_IMAGES)


def _chat_part(part: object) -> dict:
    if not isinstance(part, dict):
        raise ModelRefused("a content part must be an object")
    if part.get("type") == "text" and isinstance(part.get("text"), str):
        return {"type": "text", "text": part["text"]}
    if part.get("type") == "image_url":
        image = part.get("image_url")
        url = image.get("url") if isinstance(image, dict) else image
        if _data_image(url):
            return {"type": "image_url", "image_url": {"url": url}}
    raise ModelRefused(
        "only text parts and raster images as data URLs are taken: a provider never fetches a URL"
    )


def _tool_call(call: object) -> dict:
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(call, dict) or call.get("type") != "function":
        raise ModelRefused("only function tool calls are taken")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        raise ModelRefused("a function tool call needs a function with a name")
    kept = {"name": function["name"]}
    if "arguments" in function:
        kept["arguments"] = function["arguments"]
    out = {"type": "function", "function": kept}
    return {"id": call["id"], **out} if "id" in call else out


def clean_message(message: object) -> dict:
    if not isinstance(message, dict) or message.get("role") not in _ROLES:
        raise ModelRefused("a message must be an object with a known role")
    out = {key: message[key] for key in _MESSAGE_KEYS if key in message}
    content = out.get("content")
    if isinstance(content, list):
        out["content"] = [_chat_part(part) for part in content]
    elif content is not None and not isinstance(content, str):
        raise ModelRefused("message content must be text or a list of parts")
    if "tool_calls" in out:
        if not isinstance(out["tool_calls"], list):
            raise ModelRefused("tool_calls must be a list")
        out["tool_calls"] = [_tool_call(call) for call in out["tool_calls"]]
    return out


def clean_messages(messages: object) -> list[dict]:
    if not isinstance(messages, list) or not messages:
        raise ModelRefused("messages must be a list with at least one message")
    return [clean_message(message) for message in messages]


def refuse_remote_refs(node: Any) -> None:
    """A schema may refer inside itself. A `$ref` that names anything else is a URL to fetch."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if ref is not None and not (isinstance(ref, str) and ref.startswith("#")):
            raise ModelRefused("a schema may refer only inside itself")
        for value in node.values():
            refuse_remote_refs(value)
    elif isinstance(node, list):
        for value in node:
            refuse_remote_refs(value)


def clean_tools(tools: object) -> list[dict]:
    """Function tools only, with only their known keys. Another type, such as a web search or a
    remote MCP server, would make the provider reach out for the agent, past the allowlist."""
    if not isinstance(tools, list):
        raise ModelRefused("tools must be a list")
    clean = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ModelRefused("only function tools are taken")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ModelRefused("a function tool needs a function with a name")
        refuse_remote_refs(function.get("parameters"))
        kept = {key: function[key] for key in _FUNCTION_KEYS if key in function}
        clean.append({"type": "function", "function": kept})
    return clean


def check_response_format(form: object) -> None:
    if form is None:
        return
    if not isinstance(form, dict) or form.get("type") not in _RESPONSE_FORMATS:
        raise ModelRefused("response_format must be text, json_object or json_schema")
    refuse_remote_refs(form)


def check_responses_input(value: object) -> None:
    """The Responses API's input: text, or messages of text and images as data URLs."""
    if isinstance(value, str):
        return
    if not isinstance(value, list) or not value:
        raise ModelRefused("input must be text or a list of messages")
    for message in value:
        if not isinstance(message, dict) or message.get("role") not in _ROLES:
            raise ModelRefused("an input message must be an object with a known role")
        content = message.get("content")
        if isinstance(content, str):
            continue
        for part in _list(content):
            kind = part.get("type") if isinstance(part, dict) else None
            if kind == "input_text" and isinstance(part.get("text"), str):
                continue
            if kind == "input_image" and _data_image(part.get("image_url")):
                continue
            raise ModelRefused(
                "only text parts and raster images as data URLs are taken: "
                "a provider never fetches a URL"
            )
        if not isinstance(content, list):
            raise ModelRefused("input content must be text or a list of parts")
