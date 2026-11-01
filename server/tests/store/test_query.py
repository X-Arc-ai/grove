"""The read side, live. Test data comes through plant_node and plant_link only. Write snippets
hold a `~` that plain() drops, so this file stays clean under the writer lint."""

from datetime import UTC, date, datetime

import pytest

from grove.ids import new_id
from grove.store.connection import DATABASE
from grove.store.errors import Neo4jError, ScopeError, WorldLeak
from grove.store.query import MAX_SEARCH_TERMS, PlatformQuery, WorldQuery, guard, lucene_terms
from grove.writer.testing import plant_link, plant_node

ACCESS_MODE = "Neo.ClientError.Statement.AccessMode"
ITEMS = "grove_items_lexical"


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
    WorldQuery(recording, "w1", timeout=7.5).read("MATCH (i:Item) RETURN i")
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
