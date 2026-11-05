"""Pure whole-body checks before any MCP handler or general packet activation."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from grove.api.errors import ApiError, invalid_arguments
from grove.mcp import transport
from grove.mcp.tools import DEFAULT_TOOLS, Tool, ToolContext
from tests.hosts.test_mcp_transport import bearer, issue, make_app

READ = "grove.read"
WRITE = "grove.write"
SENSITIVE = "grove.sensitive_write"
MIXED_ERROR = {
    "error": {
        "code": "invalid_arguments",
        "message": "recall and write tools cannot share a batch",
        "details": {"rule": "mixed_read_write_batch"},
        "retryable": False,
    }
}


def call(name, message_id=1, **params):
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "tools/call",
        "params": {"name": name, **params},
    }


def protocol(method="ping"):
    return {"jsonrpc": "2.0", "id": "protocol", "method": method}


@pytest.fixture
def context():
    return ToolContext(
        object(), "world", "person", "host", frozenset({READ, WRITE, SENSITIVE}), "grant"
    )


@pytest.fixture
def tools():
    def tool(name, scope):
        return Tool(
            name,
            name,
            name,
            {"type": "object"},
            # Deliberately false hints: only the required scope is authority.
            {"readOnlyHint": scope != READ},
            scope,
            Mock(return_value={"tool": name}),
        )

    return tuple(
        tool(name, scope)
        for name, scope in (
            ("recall", READ),
            ("store", WRITE),
            ("erase", SENSITIVE),
            ("inspect", READ),
        )
    )


def assert_no_handlers(tools, respond):
    respond.assert_not_called()
    for tool in tools:
        tool.handler.assert_not_called()


@pytest.mark.parametrize("write", ["store", "erase"])
@pytest.mark.parametrize("reverse", [False, True])
def test_direct_answer_refuses_mixed_batch_before_every_handler(
    tools, context, monkeypatch, write, reverse
):
    payload = [call("recall", arguments={"query": "private-input"}), call(write, 2)]
    if reverse:
        payload.reverse()
    payload.insert(0, protocol("initialize"))
    respond = Mock(wraps=transport._respond)
    monkeypatch.setattr(transport, "_respond", respond)
    result = transport.answer(payload, tools, context)
    assert result.status == 400
    assert result.body == MIXED_ERROR
    assert result.session_id is None
    assert_no_handlers(tools, respond)


@pytest.mark.parametrize("reverse", [False, True])
def test_preflight_refuses_mixed_batch_before_activation(tools, context, reverse):
    payload = [call("recall"), call("store", 2, arguments={"invalid": "private-input"})]
    if reverse:
        payload.reverse()
    activation = Mock()
    with pytest.raises(ApiError) as caught:
        result = transport.preflight(payload, tools, context)
        if result.has_executable_recall:
            activation()
    assert caught.value.status == 400
    assert caught.value.body() == MIXED_ERROR
    activation.assert_not_called()
    for tool in tools:
        tool.handler.assert_not_called()


@pytest.mark.parametrize("arguments", [{}, None, [], "text", 7])
@pytest.mark.parametrize("entry", ["answer", "preflight"])
def test_scope_refusal_precedes_mixed_and_malformed_arguments(
    tools, context, monkeypatch, arguments, entry
):
    context = replace(context, scopes=frozenset({READ, WRITE}))
    # The final known target must refuse even after an otherwise mixed pair.
    payload = [protocol(), call("recall"), call("store", 2), call("erase", 3, arguments=arguments)]
    respond = Mock(wraps=transport._respond)
    monkeypatch.setattr(transport, "_respond", respond)
    with pytest.raises(transport.ScopeDenied) as caught:
        getattr(transport, entry)(payload, tools, context)
    assert caught.value.scope == SENSITIVE
    assert caught.value.tool == "erase"
    assert_no_handlers(tools, respond)


@pytest.mark.parametrize("arguments", [None, [], "text", 7, False])
@pytest.mark.parametrize("malformed_name", ["recall", "store"])
def test_non_object_arguments_are_not_executable(tools, context, arguments, malformed_name):
    other = "store" if malformed_name == "recall" else "recall"
    payload = [call(malformed_name, arguments=arguments), call(other, 2)]
    result = transport.preflight(payload, tools, context)
    assert result.has_executable_recall is (other == "recall")
    answered = transport.answer(payload, tools, context)
    assert answered.status == 200
    assert answered.body[0]["error"]["code"] == transport.INVALID_PARAMS
    assert answered.body[1]["result"]["structuredContent"] == {"tool": other}
    next(tool for tool in tools if tool.name == malformed_name).handler.assert_not_called()


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "recall"}},
        {"jsonrpc": "2.0", "id": 1, "result": {"name": "recall"}},
        {"jsonrpc": "2.0", "id": 1, "error": {"name": "recall"}},
        {"jsonrpc": "1.0", "id": 1, "method": "tools/call", "params": {"name": "recall"}},
        {"jsonrpc": "2.0", "id": None, "method": "tools/call", "params": {"name": "recall"}},
        {"jsonrpc": "2.0", "id": True, "method": "tools/call", "params": {"name": "recall"}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["recall"]},
        {"jsonrpc": "2.0", "id": 1, "method": "recall"},
        call("unknown", arguments={"name": "recall"}),
        call(["recall"]),
        "recall",
    ],
)
def test_non_executable_recall_shapes_do_not_make_a_mixed_batch(tools, context, message):
    payload = [message, call("store", 2)]
    assert transport.preflight(payload, tools, context).has_executable_recall is False
    assert transport.answer(payload, tools, context).status == 200
    next(tool for tool in tools if tool.name == "recall").handler.assert_not_called()
    next(tool for tool in tools if tool.name == "store").handler.assert_called_once()


def test_write_notification_neither_requires_scope_nor_counts_as_a_write(tools, context):
    notification = call("erase")
    del notification["id"]
    context = replace(context, scopes=frozenset({READ}))
    payload = [call("recall"), notification]
    assert transport.preflight(payload, tools, context).has_executable_recall is True
    assert len(transport.answer(payload, tools, context).body) == 1
    next(tool for tool in tools if tool.name == "erase").handler.assert_not_called()


@pytest.mark.parametrize(
    "payload,expected",
    [
        (call("recall"), True),
        (call("recall", arguments={}), True),
        (call("recall", arguments={"invalid": "still-dispatched"}), True),
        ([call("recall"), protocol(), call("recall", 2)], True),
        (call("store"), False),
        ([call("store"), call("erase", 2)], False),
        ([call("inspect"), call("store", 2)], False),
        (protocol("initialize"), False),
        ([protocol(), protocol("tools/list")], False),
        ([], False),
        (None, False),
    ],
)
def test_preflight_is_pure_repeatable_and_immutable(tools, context, monkeypatch, payload, expected):
    before = deepcopy(payload)
    respond = Mock(wraps=transport._respond)
    monkeypatch.setattr(transport, "_respond", respond)
    first = transport.preflight(payload, tools, context)
    assert isinstance(first, transport.Preflight)
    assert first.has_executable_recall is expected
    assert transport.preflight(payload, tools, context) == first
    with pytest.raises(FrozenInstanceError):
        first.has_executable_recall = not expected
    assert payload == before
    assert_no_handlers(tools, respond)


def test_an_unregistered_recall_is_not_executable(context):
    payload = call("recall")
    assert transport.preflight(payload, (), context).has_executable_recall is False
    assert transport.answer(payload, (), context).body["error"]["code"] == transport.INVALID_PARAMS


def test_the_default_registry_has_an_executable_recall_and_refuses_a_mixed_batch(context):
    assert transport.preflight(call("recall"), DEFAULT_TOOLS, context).has_executable_recall is True
    batch = [call("store", message_id=1), call("recall", message_id=2)]
    with pytest.raises(ApiError) as refused:
        transport.preflight(batch, DEFAULT_TOOLS, context)
    assert refused.value.details["rule"] == "mixed_read_write_batch"


def test_single_call_keeps_arguments_context_and_tool_error(tools, context):
    recall = tools[0]
    recall.handler.side_effect = invalid_arguments("query is required", "query")
    payload = call("recall", arguments={"invalid": "value"})
    assert transport.preflight(payload, tools, context).has_executable_recall is True
    answered = transport.answer(payload, tools, context)
    assert answered.status == 200
    assert answered.body["result"]["isError"] is True
    assert answered.body["result"]["structuredContent"]["error"]["details"] == {"field": "query"}
    assert recall.handler.call_args.args[0] is context
    assert recall.handler.call_args.args[1] is payload["params"]["arguments"]


@pytest.mark.parametrize("payload,status", [(None, 400), ([], 400), (protocol(), 200)])
def test_direct_answer_preserves_protocol_status(tools, context, payload, status):
    assert transport.answer(payload, tools, context).status == status


@pytest.mark.parametrize("reverse", [False, True])
def test_current_http_adapter_refuses_mixed_batch_without_any_handler(
    driver, hosts_plant, tools, monkeypatch, reverse
):
    person = hosts_plant.person(seeded=False)
    token = issue(driver, person, hosts_plant.client())
    payload = [call("recall"), call("store", 2)]
    if reverse:
        payload.reverse()
    payload.insert(0, protocol("initialize"))
    respond = Mock(wraps=transport._respond)
    monkeypatch.setattr(transport, "_respond", respond)
    with TestClient(make_app(driver, tools)) as client:
        response = client.post("/mcp", json=payload, headers=bearer(token.access))
    assert response.status_code == 400
    assert response.json() == MIXED_ERROR
    assert response.headers["cache-control"] == "no-store"
    assert "mcp-session-id" not in response.headers
    assert_no_handlers(tools, respond)


@pytest.mark.parametrize("arguments", [{}, None])
def test_current_http_adapter_preserves_scope_challenge_before_mixed_check(
    driver, hosts_plant, tools, monkeypatch, arguments
):
    person = hosts_plant.person(seeded=False)
    token = issue(driver, person, hosts_plant.client(), scopes=(READ,))
    payload = [protocol(), call("recall"), call("store", 2, arguments=arguments)]
    respond = Mock(wraps=transport._respond)
    monkeypatch.setattr(transport, "_respond", respond)
    with TestClient(make_app(driver, tools)) as client:
        response = client.post("/mcp", json=payload, headers=bearer(token.access))
    assert response.status_code == 403
    assert response.json()["error"]["details"] == {"tool": "store", "scope": WRITE}
    assert 'error="insufficient_scope", scope="grove.write"' in response.headers["www-authenticate"]
    assert_no_handlers(tools, respond)
