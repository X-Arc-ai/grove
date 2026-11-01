"""The read side, live. Test data comes through plant_node and plant_link only. Write snippets
hold a `~` that plain() drops, so this file stays clean under the writer lint."""

from datetime import UTC, date, datetime

import pytest

from grove.ids import new_id
from grove.store.connection import DATABASE
from grove.store.errors import Neo4jError, ScopeError, WorldLeak
from grove.store.query import (
    MAX_SEARCH_TERM,
    MAX_SEARCH_TERMS,
    MAX_SEARCH_TEXT,
    PlatformQuery,
    WorldQuery,
    guard,
    lucene_terms,
    plain,
)
from grove.writer.testing import plant_link, plant_node
from tests.store.test_scope import (
    AS_REPROS,
    PLATFORM_KEYWORD_REPROS,
    STARTS_REPROS,
    spellings,
)

ACCESS_MODE = "Neo.ClientError.Statement.AccessMode"
ITEMS = "grove_items_lexical"
# A well-formed id (S36) for tests that never reach the store.
ANY_WORLD = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def plain_text(cypher):
    return cypher.replace("~", "")


@pytest.fixture
def worlds(make_world):
    return make_world(), make_world()


def test_read_returns_only_the_own_row_of_a_shared_id(driver, worlds):
    a, b = worlds
    # Item ids are unique across the store; a request id is the caller's key, unique per world.
    plant_node(driver, a, "Request", id="same-request", note="from a")
    plant_node(driver, b, "Request", id="same-request", note="from b")

    rows = WorldQuery(driver, a).read("MATCH (r:Request {id: $id}) RETURN r", id="same-request")

    assert [row["r"]["note"] for row in rows] == ["from a"]


def test_two_worlds_with_the_same_content_stay_apart(driver, worlds):
    a, b = worlds
    plant_node(driver, a, "Item", title="same title", kind="k1")
    plant_node(driver, a, "Item", title="same title", kind="k1")
    plant_node(driver, b, "Item", title="same title", kind="k1")

    query = "MATCH (i:Item {title: $t, kind: $k}) RETURN count(i) AS n"
    assert WorldQuery(driver, a).read(query, t="same title", k="k1") == [{"n": 2}]
    assert WorldQuery(driver, b).read(query, t="same title", k="k1") == [{"n": 1}]


def test_a_world_with_no_rows_sees_nothing(driver, worlds):
    a, b = worlds
    plant_node(driver, a, "Item", title="only in a")
    assert WorldQuery(driver, b).read("MATCH (i:Item) RETURN i") == []


def _chain(driver, world, length=3):
    items = [plant_node(driver, world, "Item", title=f"step {n}")["id"] for n in range(length)]
    for start, end in zip(items, items[1:], strict=False):
        plant_link(driver, world, start, end)
    return items


def test_a_variable_length_walk_stays_in_its_world(driver, worlds):
    a, b = worlds
    a_items, b_items = _chain(driver, a), _chain(driver, b)
    # A link stamped with world b that ends in world a: a walk in a must not step on it.
    plant_link(driver, b, b_items[0], a_items[0], to_world=a)

    walk = "MATCH (s:Item {id: $id})-[:LINK*1..5]-(x) RETURN DISTINCT x.id AS id"
    rows = WorldQuery(driver, a).read(walk, id=a_items[0])

    assert {row["id"] for row in rows} == set(a_items[1:])


def test_a_quantified_walk_stays_in_its_world(driver, worlds):
    a, b = worlds
    a_items, b_items = _chain(driver, a), _chain(driver, b)
    plant_link(driver, b, b_items[0], a_items[0], to_world=a)

    walk = "MATCH (s:Item {id: $id})-[:LINK]-{1,5}(x) RETURN DISTINCT x.id AS id"
    rows = WorldQuery(driver, a).read(walk, id=a_items[0])

    assert {row["id"] for row in rows} == set(a_items[1:])


def test_a_walk_from_another_worlds_id_finds_nothing(driver, worlds):
    a, b = worlds
    b_items = _chain(driver, b)
    walk = "MATCH (s:Item {id: $id})-[:LINK*1..5]->(x) RETURN x"
    assert WorldQuery(driver, a).read(walk, id=b_items[0]) == []


def test_exists_and_comprehension_stay_in_their_world(driver, worlds):
    a, b = worlds
    a_items, b_items = _chain(driver, a, 2), _chain(driver, b, 2)
    plant_link(driver, b, b_items[0], a_items[0], to_world=a)

    rows = WorldQuery(driver, a).read(
        "MATCH (i:Item {id: $id}) "
        "RETURN EXISTS { (i)<-[:LINK]-() } AS incoming, "
        "[(i)-[:LINK]->(x) | x.id] AS out, COUNT { (i)-[:LINK]->() } AS out_count",
        id=a_items[0],
    )

    assert rows == [{"incoming": False, "out": [a_items[1]], "out_count": 1}]


def test_a_cross_world_element_in_a_result_raises_world_leak(driver, worlds):
    a, b = worlds
    start = plant_node(driver, a, "Item", title="start")["id"]
    end = plant_node(driver, b, "Item", title="other world")["id"]
    plant_link(driver, a, start, end, to_world=b)

    with pytest.raises(WorldLeak):
        WorldQuery(driver, a).read("MATCH (s:Item)-[:LINK]->(x) RETURN x")


def _raw_row(driver, a, b):
    start = plant_node(driver, a, "Item", title="start")["id"]
    end = plant_node(driver, a, "Item", title="end")["id"]
    plant_link(driver, a, start, end)
    with driver.session(database=DATABASE) as session:
        return session.run(
            "MATCH p = (s:Item {id: $id})-[r:LINK]->(e) RETURN s, r, e, p", id=start
        ).single()


def test_guard_passes_a_result_of_the_scope(driver, worlds):
    a, b = worlds
    row = _raw_row(driver, a, b)
    guard({"s": row["s"], "r": row["r"], "p": row["p"], "l": [row["e"]], "n": 1}, a)


@pytest.mark.parametrize(
    "shape",
    [
        lambda row: row["s"],
        lambda row: row["r"],
        lambda row: row["p"],
        lambda row: [row["s"]],
        lambda row: {"deep": [{"inner": row["r"]}]},
        lambda row: (1, row["p"]),
    ],
)
def test_guard_fires_on_every_element_shape(driver, worlds, shape):
    a, b = worlds
    row = _raw_row(driver, a, b)
    with pytest.raises(WorldLeak):
        guard(shape(row), b)


def test_guard_fires_on_an_element_with_no_world_id(driver, worlds):
    a, _ = worlds
    plant_node(driver, a, "World", next_sequence=0)
    with driver.session(database=DATABASE) as session:
        row = session.run("MATCH (w:World {id: $id}) RETURN w", id=a).single()
    with pytest.raises(WorldLeak):
        guard(row["w"], a)


def test_guard_fires_on_a_map_that_names_another_world():
    with pytest.raises(WorldLeak):
        guard({"m": {"world_id": "other", "id": "x"}}, "mine")


def test_guard_does_not_see_plain_scalars():
    guard({"a": "text", "b": 1, "c": [1, 2], "d": None}, "mine")


def test_nodes_links_paths_and_times_come_back_plain(driver, world):
    at = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    day = date(2026, 3, 4)
    start = plant_node(driver, world, "Item", title="start", recorded=at, day=day)["id"]
    end = plant_node(driver, world, "Item", title="end")["id"]
    link = plant_link(driver, world, start, end, recorded=at)

    [row] = WorldQuery(driver, world).read(
        "MATCH p = (s:Item {id: $id})-[l:LINK]->(e) RETURN s, l, p, [s.recorded] AS times, "
        "{day: s.day} AS map",
        id=start,
    )

    assert type(row["s"]) is dict
    assert row["s"]["title"] == "start"
    assert row["s"]["recorded"] == at
    assert isinstance(row["s"]["recorded"], datetime)
    assert row["s"]["day"] == day
    assert type(row["s"]["day"]) is date
    assert type(row["l"]) is dict
    assert row["l"]["id"] == link["id"]
    assert [node["title"] for node in row["p"]["nodes"]] == ["start", "end"]
    assert [one["id"] for one in row["p"]["relationships"]] == [link["id"]]
    assert row["times"] == [at]
    assert row["map"] == {"day": day}


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (i:Item {}) RETURN i",
        "MATCH (i:Item {id: $id} WHERE i.title = 'one') RETURN i",
        "MATCH (i:Item WHERE i.title = 'one')-[l:LINK WHERE l.id <> 'x']->(x) RETURN x",
        "MATCH (a:Item|Alias) RETURN a",
        "MATCH (n:HistoryValue&PastValue) RETURN n",
        "MATCH (n:HistoryValue:PastValue) RETURN n",
        "MATCH (a:Item)-[:LINK*]->(b) RETURN b",
        "MATCH (a:Item)-[:LINK*1..3]-(b) RETURN b",
        "MATCH (a:Item)-[:LINK*..4]->(b) RETURN b",
        "MATCH (a:Item)-[r:LINK*1.. {id: 'x'}]->(b) RETURN b",
        "MATCH (a:Item)-[:LINK]->{1,3}(b) RETURN b",
        "MATCH (a:Item)-[:LINK]->+(b) RETURN b",
        "MATCH (a:Item) ((x)-[:LINK]->(y)){1,3} (b) RETURN b",
        "MATCH SHORTEST 1 (a:Item)-[:LINK]->+(b) RETURN b",
        "MATCH ANY (a:Item)-[:LINK]->+(b) RETURN b",
        "MATCH p = (a:Item)-[:LINK*1..2]->(b) RETURN p",
        "MATCH (a:Item) WHERE EXISTS { MATCH (a)-[:LINK]->(b:Item) } RETURN a",
        "MATCH (a:Item) WHERE EXISTS { (a)-[:LINK]->(b:Item) WHERE b.x = 1 } RETURN a",
        "MATCH (a:Item) RETURN COUNT { (a)-[:LINK]->() } AS c",
        "MATCH (a:Item) RETURN COLLECT { MATCH (a)-[:LINK]->(b:Item) RETURN b.id } AS ids",
        "MATCH (a:Item) CALL (a) { MATCH (a)-[:LINK]->(b:Item) RETURN b } RETURN b",
        "CALL { MATCH (b:Item) RETURN b } MATCH (b) RETURN b",
        "OPTIONAL CALL () { MATCH (b:Item) RETURN b } RETURN b",
        "MATCH (a:Item) WITH DISTINCT a ORDER BY a.x LIMIT 1 MATCH (a) RETURN a",
        "MATCH (a:Item) RETURN a.id AS id UNION ALL MATCH (b:Alias) RETURN b.id AS id",
        "MATCH (n:Item) RETURN n {.id, .*, x: 1} AS m",
        "MATCH (n:Item) RETURN (n {.id}) AS m",
        "MATCH (a:Item) RETURN [(a)-[:LINK]->(b:Item) WHERE b.x = 1 | b.y] AS l",
        "MATCH (a:Item) RETURN [p = (a)-[:LINK]->() | p] AS l",
        "MATCH (a:Item) WHERE NOT (a)-[:LINK]->(:Item) RETURN a",
        "MATCH (a:Item) RETURN size([(a)-[:LINK]->(b) | b]) AS n",
        "MATCH (a:Item) RETURN exists((a)-[:LINK]->()) AS e",
        "MATCH (n:Item|Alias) WHERE (n:Item OR n:Alias) AND n:Item RETURN n",
        "MATCH (n:Item) WHERE n.title IS :: STRING RETURN n.title :: STRING AS t",
    ],
)
def test_every_rewritten_form_runs_on_the_server(driver, world, cypher):
    """The rewrite must be Cypher the server takes, not only text that test_scope.py expects."""
    start = plant_node(driver, world, "Item", title="one")["id"]
    end = plant_node(driver, world, "Item", title="two")["id"]
    plant_link(driver, world, start, end)
    params = {"id": start} if "$id" in cypher else {}
    assert isinstance(WorldQuery(driver, world).read(cypher, **params), list)


def test_a_scan_by_a_comprehension_variable_is_refused_before_the_store(world):
    """This form returns every node of the store if it reaches the server unscoped."""
    cypher = "MATCH (a:Item) WHERE size([(a)-[:LINK]->(z) | z]) >= 0 MATCH (z) RETURN count(z) AS c"
    with pytest.raises(ScopeError):
        WorldQuery(object(), world).read(cypher)


def test_a_caller_world_id_is_refused(driver, world):
    with pytest.raises(ScopeError):
        WorldQuery(driver, world).read("MATCH (i:Item {id: $id}) RETURN i", id=1, world_id=world)


def test_a_parameter_may_be_named_like_the_argument(driver, world):
    row = plant_node(driver, world, "Item", title="one")
    query = "MATCH (i:Item {id: $cypher}) RETURN i.title AS t"
    assert WorldQuery(driver, world).read(query, cypher=row["id"]) == [{"t": "one"}]


def test_a_refused_query_reaches_no_store(world):
    with pytest.raises(ScopeError):
        WorldQuery(object(), world).read("MATCH (n) RETURN n")


@pytest.mark.parametrize("bad", ["", "a b", "x" * 81, "a'b"])
def test_a_bad_world_id_is_refused_at_the_door(driver, bad):
    with pytest.raises(ScopeError):
        WorldQuery(driver, bad)


class _RecordingSession:
    """Stands in for a session: it keeps the unit of work and does not call it."""

    def __init__(self):
        self.work = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute_read(self, work):
        self.work = work
        return []


class _RecordingDriver:
    def __init__(self):
        self.opened = _RecordingSession()

    def session(self, **_):
        return self.opened


def test_the_timeout_rides_on_the_unit_of_work():
    # Not a live test: the server checks timeouts every 2 s by default, so a short one is a race.
    # tests/writer/test_tx.py proves a live timeout through a blocked lock wait.
    recording = _RecordingDriver()
    WorldQuery(recording, ANY_WORLD, timeout=7.5).read("MATCH (i:Item) RETURN i")
    assert recording.opened.work.timeout == 7.5

    recording = _RecordingDriver()
    PlatformQuery(recording, timeout=3.5).read("MATCH (u:User) RETURN u")
    assert recording.opened.work.timeout == 3.5


# Search.


def test_search_returns_a_rare_worlds_hit_under_a_flood_from_another_world(driver, worlds):
    """A true top k: the flood outscores the rare row, so a filter after LIMIT would return none."""
    rare, flood = worlds
    limit = 5
    term = "zebrafinch"
    for _ in range(limit * 6):
        plant_node(driver, flood, "Item", title=f"{term} {term} {term}")
    wanted = plant_node(
        driver, rare, "Item", title="ordinary heading", body=f"a long body with {term} once in it"
    )

    rows = WorldQuery(driver, rare).search(ITEMS, term, limit=limit)

    assert [row["node"]["id"] for row in rows] == [wanted["id"]]
    assert len(WorldQuery(driver, flood).search(ITEMS, term, limit=limit)) == limit


def test_search_cannot_read_the_other_world(driver, worlds):
    a, b = worlds
    plant_node(driver, b, "Item", title="kestrelwood")
    assert WorldQuery(driver, a).search(ITEMS, "kestrelwood", limit=10) == []


def test_search_shape_and_order(driver, world):
    weak = plant_node(driver, world, "Item", body="kestrelwood and many other plain words here")
    strong = plant_node(driver, world, "Item", title="kestrelwood kestrelwood")

    rows = WorldQuery(driver, world).search(ITEMS, "kestrelwood", limit=10)

    assert [row["node"]["id"] for row in rows] == [strong["id"], weak["id"]]
    assert rows[0]["labels"] == ["Item"]
    assert isinstance(rows[0]["score"], float)
    assert rows[0]["score"] >= rows[1]["score"]
    assert type(rows[0]["node"]) is dict


def test_search_folds_accents_and_case(driver, world):
    plant_node(driver, world, "Item", title="Crème Brûlée")
    assert len(WorldQuery(driver, world).search(ITEMS, "creme brulee", limit=5)) == 1
    assert len(WorldQuery(driver, world).search(ITEMS, "CRÈME", limit=5)) == 1


def test_fuzzy_match_works(driver, world):
    plant_node(driver, world, "Item", title="Marrowgate")
    query = WorldQuery(driver, world)
    assert query.search(ITEMS, "marowgate", limit=5) == []
    assert len(query.search(ITEMS, "marowgate", limit=5, fuzzy=True)) == 1


def test_search_on_the_other_indexes(driver, world):
    plant_node(
        driver, world, "Alias", phrase="Old Quarry Road", normalised_phrase="old quarry road"
    )
    plant_node(driver, world, "HistoryValue:PastValue", value="retired name quillfeather")
    plant_node(driver, world, "HistoryValue", value="current name brackenfold")
    query = WorldQuery(driver, world)

    assert query.search("grove_aliases_lexical", "quarry", limit=5)[0]["labels"] == ["Alias"]
    past = query.search("grove_past_values", "quillfeather", limit=5)
    assert past[0]["labels"] == ["HistoryValue", "PastValue"]
    assert query.search("grove_past_values", "brackenfold", limit=5) == []
    assert len(query.search("grove_value_index", "brackenfold", limit=5)) == 1


def test_lucene_text_stays_inside_the_terms():
    text = 'apple) OR world_id:"other'
    assert lucene_terms(text, fuzzy=False) == [r"apple\)", r"world_id\:\"other"]
    assert lucene_terms(text, fuzzy=True) == [r"apple\)~", "or~", r"world_id\:\"other~"]
    assert lucene_terms("x AND y NOT z", fuzzy=True) == ["x~", "and~", "y~", "not~", "z~"]
    assert lucene_terms(r"a\b+c*d?", fuzzy=False) == [r"a\\b\+c\*d\?"]


@pytest.mark.parametrize("template", ['apple) OR world_id:"{other}', 'apple" OR "{other}'])
def test_lucene_text_cannot_widen_to_another_world(driver, worlds, template):
    mine, other = worlds
    plant_node(driver, mine, "Item", title="apple")
    plant_node(driver, other, "Item", title="apple banana")
    planted = plant_node(driver, other, "Item", title="banana")

    rows = WorldQuery(driver, mine).search(ITEMS, template.format(other=other), limit=50)

    assert [row["node"]["title"] for row in rows] == ["apple"]
    assert planted["id"] not in {row["node"]["id"] for row in rows}


def test_lucene_text_cannot_empty_the_world_filter(driver, worlds):
    mine, other = worlds
    plant_node(driver, other, "Item", title="pomegranate")
    for text in ["pomegranate) OR (*:*", "*:*", 'world_id:"' + other + '"', "+world_id:*"]:
        assert WorldQuery(driver, mine).search(ITEMS, text, limit=50) == []


@pytest.mark.parametrize(
    "text",
    [
        "foo(",
        ")",
        '"',
        '"unbalanced',
        "\\",
        "trailing\\",
        "a:b",
        "[x TO y]",
        "{x",
        "~",
        "~2",
        "*",
        "?",
        "&&",
        "||",
        "!",
        "^2",
        "/regex/",
        "AND",
        "OR",
        "NOT",
        "a AND",
        "OR b",
        "+",
        "-",
        "- -",
        "<",
        "=",
        "x^",
    ],
)
@pytest.mark.parametrize("fuzzy", [False, True])
def test_lucene_special_text_never_breaks_the_query(driver, world, text, fuzzy):
    # Long words on purpose: a fuzzy `or~` is a legal term and matches a short word such as `row`.
    plant_node(driver, world, "Item", title="xylophone quartermaster")
    assert WorldQuery(driver, world).search(ITEMS, text, limit=5, fuzzy=fuzzy) == []


@pytest.mark.parametrize("text", ["", "   ", "()", "-", "the", "THE", "the of and", "the,", "___"])
def test_text_with_no_searchable_term_returns_nothing(driver, world, text):
    plant_node(driver, world, "Item", title="row that must not come back")
    assert WorldQuery(driver, world).search(ITEMS, text, limit=5) == []


def test_a_stop_word_next_to_a_real_term_does_not_hurt(driver, world):
    plant_node(driver, world, "Item", title="the sandpiper")
    assert len(WorldQuery(driver, world).search(ITEMS, "the sandpiper", limit=5)) == 1


def test_search_does_not_match_the_world_id_itself(driver, world):
    plant_node(driver, world, "Item", id=new_id(), title="alder")
    query = WorldQuery(driver, world)
    assert query.search(ITEMS, world, limit=5) == []
    assert query.search(ITEMS, world.rsplit("-", 1)[-1], limit=5) == []
    assert len(query.search(ITEMS, "alder", limit=5)) == 1


def test_search_refuses_more_terms_than_the_cap(driver, world):
    query = WorldQuery(driver, world)
    assert query.search(ITEMS, " ".join(["larch"] * MAX_SEARCH_TERMS), limit=5) == []
    with pytest.raises(ScopeError):
        query.search(ITEMS, " ".join(["larch"] * (MAX_SEARCH_TERMS + 1)), limit=5)


def test_search_at_the_cap_stays_under_the_lucene_clause_limit(driver, world):
    words = " ".join(f"word{number}" for number in range(MAX_SEARCH_TERMS))
    assert WorldQuery(driver, world).search("grove_value_index", words, limit=5, fuzzy=True) == []


def test_search_refuses_an_unknown_index(driver, world):
    with pytest.raises(ScopeError):
        WorldQuery(driver, world).search("no_such_index", "x", limit=1)
    with pytest.raises(ScopeError):
        WorldQuery(driver, world).search("grove_items_lexical) MATCH (n", "x", limit=1)


@pytest.mark.parametrize("limit", [0, -1, True, "5", 1.5])
def test_search_refuses_a_bad_limit(driver, world, limit):
    with pytest.raises(ScopeError):
        WorldQuery(driver, world).search(ITEMS, "x", limit=limit)


# Primitive rows.


def test_primitives_read_only_primitive_rows(driver, worlds):
    a, _ = worlds
    primitive = plant_node(driver, a, "Meaning", primitive=True, name=f"p {a}")
    world_row = plant_node(driver, a, "Meaning", name=f"w {a}")
    query = "MATCH (m:Meaning {id: $id}) RETURN m"

    query_primitives = WorldQuery.primitives(driver)
    assert query_primitives.read(query, id=primitive["id"])[0]["m"]["world_id"] == ""
    assert query_primitives.read(query, id=world_row["id"]) == []
    assert WorldQuery(driver, a).read(query, id=primitive["id"]) == []
    assert len(WorldQuery(driver, a).read(query, id=world_row["id"])) == 1


def test_primitives_refuse_a_write(driver, world):
    name = f"must not exist {world}"
    with pytest.raises(Neo4jError) as refused:
        WorldQuery.primitives(driver).read(plain_text("CRE~ATE (:Meaning {name: $n})"), n=name)
    assert refused.value.code == ACCESS_MODE
    rows = WorldQuery.primitives(driver).read("MATCH (m:Meaning {name: $n}) RETURN m", n=name)
    assert rows == []


def test_primitives_refuse_search(driver):
    with pytest.raises(ScopeError):
        WorldQuery.primitives(driver).search("grove_value_index", "x", limit=1)


# The platform side.


def test_platform_query_reads_platform_rows(driver, world):
    plant_node(driver, world, "World", next_sequence=7)
    rows = PlatformQuery(driver).read("MATCH (w:World {id: $id}) RETURN w", id=world)
    assert rows[0]["w"]["next_sequence"] == 7


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (i:Item) RETURN i",
        "MATCH (w:World) WHERE w:Item RETURN w",
        "MATCH (w:World)-[:LINK]->(x) RETURN x",
        "MATCH (n) RETURN n",
    ],
)
def test_platform_query_refuses_world_labels(driver, cypher):
    with pytest.raises(ScopeError):
        PlatformQuery(driver).read(cypher)


def test_world_query_refuses_platform_labels(driver, world):
    with pytest.raises(ScopeError):
        WorldQuery(driver, world).read("MATCH (w:World) RETURN w")


# Fix round 2: isolation.


@pytest.fixture
def planted(driver, worlds):
    """Two worlds with one titled item each; returns (a, b, a item, b item)."""
    a, b = worlds
    mine = plant_node(driver, a, "Item", title="own heron", n=1)
    theirs = plant_node(driver, b, "Item", title="secret heron", n=1)
    return a, b, mine, theirs


def _params(mine, theirs):
    return {"id": mine["id"], "t": theirs["title"], "bid": theirs["id"]}


@pytest.mark.parametrize(
    "cypher", [form for repro in AS_REPROS + STARTS_REPROS for form in spellings(repro)]
)
def test_keyword_name_repros_never_return_the_other_world(driver, planted, cypher):
    a, _, mine, theirs = planted
    with pytest.raises(ScopeError, match="keyword"):
        WorldQuery(driver, a).read(cypher, **_params(mine, theirs))


@pytest.mark.parametrize(
    "cypher", [form for repro in PLATFORM_KEYWORD_REPROS for form in spellings(repro)]
)
def test_keyword_name_repros_are_refused_on_the_platform_side(driver, planted, cypher):
    _, _, mine, theirs = planted
    with pytest.raises(ScopeError):
        PlatformQuery(driver).read(cypher, **_params(mine, theirs))


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("MATCH (i:Item) WHERE i.title STARTS WITH $p RETURN i.title AS t", ["own heron"]),
        ("match (i:Item) where i.title ends with 'heron' return i.title as t", ["own heron"]),
        ("MATCH (i:Item) RETURN i.title As t", ["own heron"]),
        ("MATCH (i:Item) WITH i, i.n AS m WITH m MATCH (j:Item {n: m}) RETURN j.title AS t", None),
        (
            "MATCH (i:Item {id:$id}) RETURN i.title AS t // note\rUNION MATCH (n:Item {title:$t}) "
            "RETURN n.title AS t",
            ["own heron"],
        ),
        ("MATCH (i:Item {id:$id})\r\n// note\r\nRETURN i.title AS t", ["own heron"]),
        ("MATCH (i:Item {id:$id}) // c\rMATCH (j:Item) RETURN j.title AS t", ["own heron"]),
    ],
)
def test_the_keywords_and_line_ends_run_live_in_the_own_world(driver, planted, cypher, expected):
    a, _, mine, theirs = planted
    rows = WorldQuery(driver, a).read(cypher, p="own", **_params(mine, theirs))
    assert [row["t"] for row in rows] == (expected or ["own heron"])


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (i:Item {id:$id}) RETURN i.title AS t // note\rUNION MATCH (n {title:$t}) "
        "RETURN n.title AS t",
        "MATCH (i:Item {id:$id}) // c\rMATCH (n {title:$t}) RETURN n.title AS t //\n",
    ],
)
def test_the_carriage_return_repros_are_refused(driver, planted, cypher):
    a, _, mine, theirs = planted
    with pytest.raises(ScopeError):
        WorldQuery(driver, a).read(cypher, **_params(mine, theirs))


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("MATCH (a:Item {id:$id}) RETURN a.n - [1][0] AS t", 0),
        ("MATCH (a:Item {id:$id}) RETURN (a.n)-[1][0] AS t", 0),
        (
            "MATCH (a:Item {id:$id}) WITH collect(a) AS l RETURN [x IN l | x.n][0] - [1,2][1] AS t",
            -1,
        ),
    ],
)
def test_subtraction_of_a_list_element_runs_live(driver, planted, cypher, expected):
    a, _, mine, theirs = planted
    assert WorldQuery(driver, a).read(cypher, id=mine["id"]) == [{"t": expected}]


def test_a_link_with_no_type_stays_refused_live(driver, planted):
    a, *_ = planted
    with pytest.raises(ScopeError, match="no type"):
        WorldQuery(driver, a).read("MATCH (a)-[]->(b) RETURN b")


def test_a_non_integer_bound_is_a_scope_error(driver, planted):
    a, _, mine, _ = planted
    for cypher in (
        "MATCH (i:Item {id:$id})-[:LINK*1e0..2]->(j) RETURN j.title AS t",
        "MATCH (i:Item {id:$id})-[:LINK]->{1.5,2}(j) RETURN j.title AS t",
    ):
        with pytest.raises(ScopeError):
            WorldQuery(driver, a).read(cypher, id=mine["id"])


def test_for_after_a_projection_is_refused(driver, planted):
    a, _, mine, _ = planted
    with pytest.raises(ScopeError, match="FOR"):
        WorldQuery(driver, a).read(
            "MATCH (i:Item {id:$id}) WITH i FOR x IN [1] RETURN x", id=mine["id"]
        )


@pytest.mark.parametrize(
    "timeout", [0, 0.0, -1, -0.5, True, False, None, "5", float("nan"), float("inf")]
)
def test_a_timeout_that_is_not_a_positive_number_is_refused(timeout):
    with pytest.raises(ScopeError, match="timeout"):
        WorldQuery(object(), ANY_WORLD, timeout=timeout)
    with pytest.raises(ScopeError, match="timeout"):
        WorldQuery.primitives(object(), timeout=timeout)
    with pytest.raises(ScopeError, match="timeout"):
        PlatformQuery(object(), timeout=timeout)


def test_a_positive_timeout_passes():
    assert WorldQuery(object(), ANY_WORLD, timeout=1)._timeout == 1
    assert PlatformQuery(object(), timeout=0.5)._timeout == 0.5


# Search caps: none of these may reach the server. A 1 MB fuzzy word once filled its heap.


@pytest.mark.parametrize("fuzzy", [False, True])
@pytest.mark.parametrize(
    "text",
    [
        "a" * 1_000_000,
        "a" * 40_000,
        "-".join(f"w{i}" for i in range(3000)),
        "x" * (MAX_SEARCH_TEXT + 1),
        "y" * (MAX_SEARCH_TERM + 1),
        "short " + "z" * (MAX_SEARCH_TERM + 1),
        # Every word is under the term cap, but the analyzer splits each into ten tokens.
        " ".join(["a-b-c-d-e-f-g-h-i-j"] * 7),
        "-".join(f"w{i}" for i in range(13)) + " " + " ".join(f"v{i}" for i in range(52)),
    ],
)
def test_search_caps_refuse_before_any_query(text, fuzzy):
    with pytest.raises(ScopeError):
        WorldQuery(object(), ANY_WORLD).search(ITEMS, text, limit=5, fuzzy=fuzzy)


def test_search_caps_let_text_at_the_limits_through(driver, world):
    plant_node(driver, world, "Item", title="osprey")
    query = WorldQuery(driver, world)
    assert len(query.search(ITEMS, "osprey " + "q" * (MAX_SEARCH_TERM - 7), limit=5)) == 1
    padded = "osprey" + " " * (MAX_SEARCH_TEXT - 6)
    assert len(query.search(ITEMS, padded, limit=5)) == 1
    hyphens = " ".join(["a1-b2-c3-d4"] * 16)
    assert query.search(ITEMS, hyphens, limit=5, fuzzy=True) == []


def test_lucene_terms_count_analyzer_tokens():
    """`a-b` is one term to a split on spaces and two tokens to the analyzer."""
    at_cap = " ".join(["a-b"] * (MAX_SEARCH_TERMS // 2))
    assert len(lucene_terms(at_cap, fuzzy=False)) == MAX_SEARCH_TERMS // 2
    with pytest.raises(ScopeError):
        lucene_terms(at_cap + " c", fuzzy=False)
    with pytest.raises(ScopeError):
        lucene_terms(at_cap + " c", fuzzy=True)


# Text the analyzer folds to stop words or drops returned every row of the world.

COMBINING_ACUTE = chr(0x301)
FOLDED_AWAY = [
    "thé",
    "ＴＨＥ",
    "ｔｈｅ",
    "ÀN",
    "the²",
    "²",
    "½",
    "⅓",
    "①",
    COMBINING_ACUTE,
    "²³ ½",
    "the" + COMBINING_ACUTE,
]


@pytest.mark.parametrize("text", FOLDED_AWAY)
def test_text_that_folds_to_nothing_returns_nothing(driver, world, text):
    plant_node(driver, world, "Item", title="row that must not come back")
    plant_node(driver, world, "Item", title="another row")
    assert lucene_terms(text, fuzzy=False) == []
    assert WorldQuery(driver, world).search(ITEMS, text, limit=5) == []


@pytest.mark.parametrize("text", ["²", "½", "⅓", "①", COMBINING_ACUTE])
def test_fuzzy_text_with_no_letter_returns_nothing(driver, world, text):
    plant_node(driver, world, "Item", title="row that must not come back")
    assert lucene_terms(text, fuzzy=True) == []
    assert WorldQuery(driver, world).search(ITEMS, text, limit=5, fuzzy=True) == []


@pytest.mark.parametrize(
    ("title", "text"),
    [("مكتبة المدينة القديمة", "مكتبة"), ("東京タワー", "東京"), ("Thé Garden", "garden")],
)
def test_arabic_and_cjk_words_still_search(driver, world, title, text):
    planted_row = plant_node(driver, world, "Item", title=title)
    rows = WorldQuery(driver, world).search(ITEMS, text, limit=5)
    assert [row["node"]["id"] for row in rows] == [planted_row["id"]]


# Values the driver gives as tuples or as neo4j time types (S16, DESIGN of the read side).


def test_plain_turns_every_driver_value_into_python(driver):
    """Raw driver values, read with no layer, so the test needs no import of the driver here."""
    from datetime import time as native_time
    from datetime import timedelta, timezone

    with driver.session(database=DATABASE) as session:
        raw = session.run(
            "RETURN duration('P1M2DT3H') AS d, point({latitude: 1, longitude: 2}) AS p, "
            "point({x: 1, y: 2, z: 3}) AS p3, time('12:00:00+01:00') AS t, "
            "localtime('12:00:00') AS lt, date('2026-01-02') AS day, "
            "datetime('2026-01-02T03:04:05Z') AS dt, [duration('P1D'), point({x: 1, y: 2})] AS l"
        ).single()

    assert isinstance(raw["d"], tuple) and isinstance(raw["p"], tuple)
    assert plain(raw["d"]) == "P1M2DT3H"
    assert plain(raw["p"]) == {"srid": 4326, "x": 2.0, "y": 1.0}
    assert plain(raw["p3"]) == {"srid": 9157, "x": 1.0, "y": 2.0, "z": 3.0}
    assert plain(raw["t"]) == native_time(12, 0, tzinfo=timezone(timedelta(hours=1)))
    assert type(plain(raw["lt"])) is native_time
    assert type(plain(raw["day"])) is date
    assert plain(raw["dt"]) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert plain(raw["l"]) == ["P1D", {"srid": 7203, "x": 1.0, "y": 2.0}]


def test_every_temporal_and_spatial_value_reads_back_plain(driver, world):
    import json
    from datetime import time as native_time
    from datetime import timedelta, timezone

    [row] = WorldQuery(driver, world).read(
        "RETURN time('12:00:00+01:00') AS t, localtime('12:00:00') AS lt, "
        "duration('P1M2DT3H') AS d, point({latitude: 1, longitude: 2}) AS p, "
        "point({x: 1, y: 2, z: 3}) AS p3, date('2026-01-02') AS day, "
        "localdatetime('2026-01-02T03:04:05') AS ldt, "
        "datetime('2026-01-02T03:04:05Z') AS dt, [duration('P1D')] AS ds"
    )
    assert row["t"] == native_time(12, 0, tzinfo=timezone(timedelta(hours=1)))
    assert row["lt"] == native_time(12, 0)
    assert row["d"] == "P1M2DT3H"
    assert row["p"] == {"srid": 4326, "x": 2.0, "y": 1.0}
    assert row["p3"] == {"srid": 9157, "x": 1.0, "y": 2.0, "z": 3.0}
    assert row["day"] == date(2026, 1, 2)
    assert row["ldt"] == datetime(2026, 1, 2, 3, 4, 5)
    assert row["dt"] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert row["ds"] == ["P1D"]
    json.dumps({key: value for key, value in row.items() if key in ("d", "p", "p3", "ds")})
