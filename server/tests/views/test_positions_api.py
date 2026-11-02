"""POST /v1/views/positions: insert-only, and a placed item keeps its place in the graph."""

import json

import pytest

from grove.store.query import WorldQuery
from grove.writer.testing import plant_node

POSITIONS = "/v1/views/positions"


def placed(payload):
    return {
        w["data"]["id"]: (w["data"]["x"], w["data"]["y"])
        for w in payload["elements"]["nodes"]
        if "x" in w["data"] or "y" in w["data"]
    }


def entries(*triples):
    return {"positions": [{"id": i, "x": x, "y": y} for i, x, y in triples]}


@pytest.fixture
def items(driver, world):
    return [plant_node(driver, world, "Item", title=f"Item {n}")["id"] for n in range(3)]


def test_first_post_places_and_a_second_post_keeps_the_first_values(world, client_for, items):
    client = client_for(world)
    a, b, _ = items
    first = client.post(POSITIONS, json=entries((a, 12.345678901234, -0.5), (b, 1e7, -1e7)))
    assert first.status_code == 200
    assert first.json() == {"placed": 2, "kept": 0, "unknown": 0}
    assert first.headers["cache-control"] == "no-store"

    second = client.post(POSITIONS, json=entries((a, 999.0, 999.0), (b, 0.0, 0.0)))
    assert second.json() == {"placed": 0, "kept": 2, "unknown": 0}

    graph = client.get("/v1/views/graph").json()
    assert placed(graph) == {a: (12.345678901234, -0.5), b: (1e7, -1e7)}
    detail = client.get(f"/v1/views/node/{a}").json()
    assert (detail["node"]["x"], detail["node"]["y"]) == (12.345678901234, -0.5)


def test_positions_survive_new_items_arriving(driver, world, client_for, items):
    client = client_for(world)
    a, b, c = items
    client.post(POSITIONS, json=entries((a, 10.0, 20.0), (b, -30.0, 40.5)))
    before = placed(client.get("/v1/views/graph").json())
    assert before == {a: (10.0, 20.0), b: (-30.0, 40.5)}

    newcomers = [plant_node(driver, world, "Item", title=f"New {n}")["id"] for n in range(5)]
    after = client.get("/v1/views/graph").json()
    assert placed(after) == before
    assert after["stats"]["total_nodes"] == 8
    unplaced = [w["data"] for w in after["elements"]["nodes"] if w["data"]["id"] in newcomers]
    assert len(unplaced) == 5 and all("x" not in d and "y" not in d for d in unplaced)

    # The map places the newcomers; the old items stay where they were, to the last bit.
    body = entries((a, 0.0, 0.0), (c, 5.0, 6.0), *((n, 1.5, 2.5) for n in newcomers))
    assert client.post(POSITIONS, json=body).json() == {"placed": 6, "kept": 1, "unknown": 0}
    final = placed(client.get("/v1/views/graph").json())
    assert final[a] == (10.0, 20.0) and final[b] == (-30.0, 40.5)
    assert final[c] == (5.0, 6.0)
    assert all(final[n] == (1.5, 2.5) for n in newcomers)


def test_unknown_ids_and_a_repeat_inside_one_call(driver, world, client_for, items):
    client = client_for(world)
    a, b, _ = items
    body = entries((a, 1.0, 1.0), (a, 2.0, 2.0), ("no-such-item", 3.0, 3.0), (b, 4, -4))
    assert client.post(POSITIONS, json=body).json() == {"placed": 2, "kept": 1, "unknown": 1}
    assert placed(client.get("/v1/views/graph").json()) == {a: (1.0, 1.0), b: (4.0, -4.0)}
    count = WorldQuery(driver, world).read("MATCH (p:MapPosition) RETURN count(p) AS n")
    assert count[0]["n"] == 2


# Each bad entry goes after a valid one: a refused call writes nothing, not even the valid entry.
BAD_ENTRIES = [
    ("x is text", {"id": "x", "x": "1", "y": 1.0}),
    ("y is bool", {"id": "x", "x": 1.0, "y": True}),
    ("x is null", {"id": "x", "x": None, "y": 1.0}),
    ("x too large", {"id": "x", "x": 1.00001e7, "y": 0.0}),
    ("y too small", {"id": "x", "x": 0.0, "y": -2e7}),
    ("id is a number", {"id": 7, "x": 0.0, "y": 0.0}),
    ("id is empty", {"id": "", "x": 0.0, "y": 0.0}),
    ("id too long", {"id": "i" * 129, "x": 0.0, "y": 0.0}),
    ("missing y", {"id": "x", "x": 1.0}),
    ("extra key", {"id": "x", "x": 1.0, "y": 1.0, "z": 1.0}),
    ("entry not an object", ["x", 1.0, 1.0]),
]
BAD_SHAPES = [
    ("empty list", {"positions": []}),
    ("too many", entries(*(("x", 0.0, 0.0),) * 5001)),
    ("not a list", {"positions": {"id": "x", "x": 1, "y": 1}}),
    ("a string", {"positions": "x"}),
    ("extra top key", {"positions": [{"id": "x", "x": 1.0, "y": 1.0}], "more": 1}),
    ("no positions key", {"items": []}),
    ("body is a list", [{"id": "x", "x": 1.0, "y": 1.0}]),
]


def assert_refused(driver, world, response, name):
    assert response.status_code == 400, name
    error = response.json()["error"]
    assert error["code"] == "invalid_arguments" and error["retryable"] is False
    assert set(error["details"]) == {"field"}
    count = WorldQuery(driver, world).read("MATCH (p:MapPosition) RETURN count(p) AS n")
    assert count[0]["n"] == 0


@pytest.mark.parametrize(("name", "entry"), BAD_ENTRIES, ids=[name for name, _ in BAD_ENTRIES])
def test_an_invalid_entry_is_a_typed_400_and_writes_nothing(
    driver, world, client_for, items, name, entry
):
    body = {"positions": [{"id": items[0], "x": 1.0, "y": 1.0}, entry]}
    response = client_for(world).post(POSITIONS, json=body)
    assert_refused(driver, world, response, name)
    assert response.json()["error"]["details"]["field"].startswith("positions[1]")


@pytest.mark.parametrize(("name", "body"), BAD_SHAPES, ids=[name for name, _ in BAD_SHAPES])
def test_an_invalid_body_is_a_typed_400_and_writes_nothing(driver, world, client_for, name, body):
    assert_refused(driver, world, client_for(world).post(POSITIONS, json=body), name)


RAW_BODIES = [
    ("not json", b"{positions: nope", "application/json"),
    ("nan", b'{"positions": [{"id": "x", "x": NaN, "y": 1.0}]}', "application/json"),
    ("infinity", b'{"positions": [{"id": "x", "x": Infinity, "y": 1.0}]}', "application/json"),
    ("minus infinity", b'{"positions": [{"id": "x", "x": 1, "y": -Infinity}]}', "application/json"),
    ("huge number", b'{"positions": [{"id": "x", "x": 1e400, "y": 1.0}]}', "application/json"),
    ("not utf-8", b'{"positions": "\xff"}', "application/json"),
    ("form type", b'{"positions": [{"id": "x", "x": 1.0, "y": 1.0}]}', "text/plain"),
    (
        "form encoded",
        b"positions=1",
        "application/x-www-form-urlencoded",
    ),
    ("no type", b'{"positions": [{"id": "x", "x": 1.0, "y": 1.0}]}', ""),
    # Deeper than the parser's recursion limit, yet far under the 2 MiB cap.
    (
        "deeply nested",
        b'{"positions": ' + b"[" * 100_000 + b"]" * 100_000 + b"}",
        "application/json",
    ),
]


@pytest.mark.parametrize(("name", "raw", "media"), RAW_BODIES, ids=[n for n, _, _ in RAW_BODIES])
def test_a_raw_body_that_is_not_plain_json_is_a_typed_400(world, client_for, name, raw, media):
    client = client_for(world)
    headers = {"content-type": media} if media else {}
    response = client.post(POSITIONS, content=raw, headers=headers)
    assert response.status_code == 400, name
    assert response.json()["error"]["code"] == "invalid_arguments"
    if name == "deeply nested":
        assert response.json()["error"]["details"] == {"field": "body"}


def test_json_with_a_charset_is_accepted(world, client_for, items):
    client = client_for(world)
    raw = json.dumps(entries((items[0], 1.0, 2.0))).encode()
    response = client.post(
        POSITIONS, content=raw, headers={"content-type": "application/json; charset=utf-8"}
    )
    assert response.json() == {"placed": 1, "kept": 0, "unknown": 0}


def test_a_body_over_the_cap_is_a_typed_400(world, client_for):
    client = client_for(world)
    raw = b'{"positions": [' + b" " * (2 * 1024 * 1024) + b"]}"
    response = client.post(POSITIONS, content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["details"] == {"field": "body"}


def chunks(*parts):
    """A body with no declared length: httpx sends a generator as Transfer-Encoding: chunked."""

    def stream():
        yield from parts

    return stream()


def test_a_chunked_body_over_the_cap_is_a_typed_400(world, client_for):
    client = client_for(world)
    filler = b" " * (1024 * 1024)
    response = client.post(
        POSITIONS,
        content=chunks(b'{"positions": [', filler, filler, filler, b"]}"),
        headers={"content-type": "application/json"},
    )
    assert "content-length" not in response.request.headers
    assert response.request.headers["transfer-encoding"] == "chunked"
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_arguments"
    assert error["details"] == {"field": "body"}
    assert error["message"] == "the body is too large"


def test_a_chunked_body_under_the_cap_is_read(world, client_for, items):
    client = client_for(world)
    raw = json.dumps(entries((items[0], 1.0, 2.0))).encode()
    response = client.post(
        POSITIONS,
        content=chunks(raw[:9], raw[9:]),
        headers={"content-type": "application/json"},
    )
    assert "content-length" not in response.request.headers
    assert response.json() == {"placed": 1, "kept": 0, "unknown": 0}


def test_5000_entries_are_accepted(driver, world, client_for, items):
    client = client_for(world)
    body = entries(*((f"missing-{n}", float(n), float(-n)) for n in range(4999)), (items[0], 1, 2))
    assert client.post(POSITIONS, json=body).json() == {"placed": 1, "kept": 0, "unknown": 4999}
