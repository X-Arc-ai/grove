import asyncio
import copy
from contextlib import contextmanager

import pytest
from jsonschema import Draft202012Validator
from starlette.concurrency import run_in_threadpool

from grove.api.errors import ApiError
from grove.api.packet_release import (
    PacketReleaseMiddleware,
    activate_packet_response,
    retain_packet_lease,
)
from grove.mcp.tools import ToolContext
from grove.reads.mcp_adapter import (
    bind_recall,
    current_recall_schema,
    parse_current_recall,
    recall_handler,
)
from grove.reads.runtime import deadline, request_budget


def context(scopes=frozenset({"grove.read"})):
    return ToolContext(object(), "world", "user", "client", scopes, "grant")


@pytest.mark.parametrize(
    "arguments",
    [
        {"about": "Cedar"},
        {"about": "Cedar", "time": {"mode": "current"}},
        {"about": "Cedar", "kinds": ["project"], "source_types": ["said"]},
        {"about": "Cedar", "hops": {"meanings": ["part of"], "direction": "out", "depth": 3}},
        {"about": "Cedar", "include": ["evidence"], "limit": 1, "cursor": "opaque"},
    ],
)
def test_accepted_first_release_arguments_have_a_matching_schema(arguments):
    original = copy.deepcopy(arguments)
    Draft202012Validator(current_recall_schema()).validate(arguments)
    parsed = parse_current_recall(arguments)
    assert parsed.about == "Cedar"
    assert arguments == original


@pytest.mark.parametrize(
    "arguments,field,rule",
    [
        ({}, "about", "about_required"),
        (
            {"about": "Cedar", "time": {"mode": "as_of", "axis": "world", "date": "2026-11-01"}},
            "time",
            "unsupported_recall_mode",
        ),
        (
            {
                "about": "Cedar",
                "time": {"mode": "between", "start": "2026-11-01", "end": "2026-11-02"},
            },
            "time",
            "unsupported_recall_mode",
        ),
        ({"about": "Cedar", "time": {"mode": "latest"}}, "time", "unsupported_recall_mode"),
        ({"about": "Cedar", "areas": ["private-area"]}, "areas", "unsupported_recall_mode"),
        ({"about": "Cedar", "include": ["history"]}, "include", "unsupported_recall_mode"),
        ({"about": "Cedar", "include": ["ended"]}, "include", "unsupported_recall_mode"),
    ],
)
def test_unavailable_capabilities_are_explicit_errors(arguments, field, rule):
    assert list(Draft202012Validator(current_recall_schema()).iter_errors(arguments))
    with pytest.raises(ApiError) as caught:
        parse_current_recall(arguments)
    assert caught.value.status == 400
    assert caught.value.details == {"field": field, "rule": rule}
    assert "Cedar" not in str(caught.value)
    assert "private-area" not in str(caught.value)


@pytest.mark.parametrize(
    "arguments",
    [
        {"about": " "},
        {"about": "Cedar", "time": {}},
        {"about": "x" * 1025},
        {"about": "Cedar", "limit": True},
        {"about": "Cedar", "include": ["evidence", "evidence"]},
        {"about": "Cedar", "hops": {"depth": 4}},
        {"about": "Cedar", "source_types": ["untrusted-code"]},
        {"about": "Cedar", "world_id": "other"},
    ],
)
def test_existing_strict_parser_still_refuses_invalid_arguments(arguments):
    with pytest.raises(ApiError) as caught:
        parse_current_recall(arguments)
    assert caught.value.status == 400


def test_each_schema_is_a_fresh_container_and_has_no_future_modes():
    first = current_recall_schema()
    first["properties"]["time"]["properties"]["mode"]["const"] = "latest"
    second = current_recall_schema()
    assert second["properties"]["time"]["properties"]["mode"]["const"] == "current"
    assert "areas" not in second["properties"]
    assert second["properties"]["include"]["items"]["enum"] == ["evidence"]


async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def guarded(function):
    async def app(scope, recv, send):
        await function()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"complete"})

    async def send(message):
        pass

    async with request_budget():
        await PacketReleaseMiddleware(app, paths=frozenset({"/mcp"}))(
            {"type": "http", "path": "/mcp"}, receive, send
        )


@pytest.mark.parametrize("activation_required", [False, True])
def test_real_anyio_bridge_uses_owner_loop_and_holds_lease_through_final_send(activation_required):
    async def scenario():
        expected, events = context(), []
        loop = asyncio.get_running_loop()
        handles = []

        @contextmanager
        def lease():
            events.append("lease-enter")
            try:
                yield
            finally:
                events.append("lease-exit")

        async def invoke(query):
            assert asyncio.get_running_loop() is loop
            assert deadline() is handles[0]
            assert query.about == "Cedar" and query.hops.depth == 2
            retain_packet_lease(lease())
            events.append("invoke")
            return {"answer": "found"}

        async def app(scope, recv, send):
            if activation_required:
                await activate_packet_response()
            handles.append(deadline())
            async with bind_recall(expected, invoke):
                result = await run_in_threadpool(
                    recall_handler, expected, {"about": "Cedar", "hops": {"depth": 2}}
                )
            assert result == {"answer": "found"}
            events.append("serialized")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"whole", "more_body": False})

        async def send(message):
            assert "lease-exit" not in events
            if message["type"] == "http.response.body":
                events.append("body")

        async with request_budget():
            await PacketReleaseMiddleware(
                app, paths=frozenset({"/mcp"}), require_activation=activation_required
            )({"type": "http", "path": "/mcp"}, receive, send)
        assert events == ["lease-enter", "invoke", "serialized", "body", "lease-exit"]

    asyncio.run(scenario())


def test_missing_binding_different_context_and_missing_scope_refuse_before_invocation():
    async def scenario():
        calls = []

        async def invoke(query):
            calls.append(query)
            return {}

        expected = context()
        with pytest.raises(ApiError):
            await run_in_threadpool(recall_handler, expected, {"about": "Cedar"})

        async def bound_checks():
            async with bind_recall(expected, invoke):
                with pytest.raises(ApiError):
                    await run_in_threadpool(recall_handler, context(), {"about": "Cedar"})
            no_read = context(frozenset({"grove.write"}))
            async with bind_recall(no_read, invoke):
                with pytest.raises(ApiError) as caught:
                    await run_in_threadpool(recall_handler, no_read, {"about": "Cedar"})
                assert caught.value.status == 403

        await guarded(bound_checks)
        assert calls == []

    asyncio.run(scenario())


def test_late_inherited_context_and_direct_event_loop_call_cannot_invoke():
    async def scenario():
        ready, release = asyncio.Event(), asyncio.Event()
        calls = []
        expected = context()

        async def invoke(query):
            calls.append(query)
            return {}

        async def late():
            ready.set()
            await release.wait()
            with pytest.raises(ApiError):
                await run_in_threadpool(recall_handler, expected, {"about": "Cedar"})

        async def bound_checks():
            async with bind_recall(expected, invoke):
                with pytest.raises(ApiError):
                    recall_handler(expected, {"about": "Cedar"})
                task = asyncio.create_task(late())
                await ready.wait()
            release.set()
            await task

        await guarded(bound_checks)
        assert calls == []

    asyncio.run(scenario())


def test_active_budget_without_response_guard_cannot_bind_a_recall():
    async def scenario():
        async def invoke(query):
            pytest.fail("a callback must not run without a response guard")

        async with request_budget():
            with pytest.raises(ApiError):
                async with bind_recall(context(), invoke):
                    pytest.fail("a budget alone must not permit the binding")

    asyncio.run(scenario())


def test_inactive_response_guard_refuses_binding_before_callback():
    async def scenario():
        output = []

        async def invoke(query):
            pytest.fail("an inactive guard must not permit private work")

        async def app(scope, recv, send):
            with pytest.raises(ApiError):
                async with bind_recall(context(), invoke):
                    pytest.fail("the response needs explicit activation")
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def send(message):
            output.append(message)

        await PacketReleaseMiddleware(app, paths=frozenset({"/mcp"}), require_activation=True)(
            {"type": "http", "path": "/mcp"}, receive, send
        )
        assert output[0]["status"] == 204

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_request", [True, False])
def test_actual_worker_bridge_cancellation_does_not_release_a_successful_packet(cancel_request):
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        output, events = [], []
        expected = context()

        @contextmanager
        def lease():
            try:
                yield
            finally:
                events.append("lease-exit")

        async def invoke(query):
            retain_packet_lease(lease())
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("callback-cancelled")
                cancelled.set()

        async def app(scope, recv, send):
            await activate_packet_response(seconds=0.1)
            async with bind_recall(expected, invoke):
                await run_in_threadpool(recall_handler, expected, {"about": "Cedar"})
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"success"})

        async def send(message):
            output.append(message)

        async def request():
            await PacketReleaseMiddleware(app, paths=frozenset({"/mcp"}), require_activation=True)(
                {"type": "http", "path": "/mcp"}, receive, send
            )

        task = asyncio.create_task(request())
        await asyncio.wait_for(entered.wait(), 1)
        if cancel_request:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        else:
            await asyncio.wait_for(task, 1)
            assert output[0]["status"] == 503
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not any(message.get("body") == b"success" for message in output)
        assert events == ["callback-cancelled", "lease-exit"]

    asyncio.run(scenario())
