"""scope_cypher, with no server. Write snippets hold a `~` that plain() drops, so this file stays
clean under the writer lint with no exemption for tests."""

import pytest

from grove.store.errors import ScopeError
from grove.store.scope import (
    check_world_id,
    literal_spans,
    mask,
    refuse_world_param,
    scope_cypher,
)

W = "{world_id: $world_id}"
WC = "{world_id: $world_id, "


def plain(cypher):
    return cypher.replace("~", "")


def r(cypher):
    return scope_cypher(plain(cypher), "read")


def w(cypher):
    return scope_cypher(plain(cypher), "write")


def p(cypher):
    return scope_cypher(cypher, "platform")


def refused(scoper, cypher, match=None):
    with pytest.raises(ScopeError, match=match):
        scoper(cypher)


# The mask.


def test_mask_keeps_length_and_line_breaks():
    text = "RETURN 'a\nb' AS x // note\n/* c\nd */ RETURN 1"
    masked = mask(text)
    assert len(masked) == len(text)
    assert "note" not in masked
    assert "RETURN" in masked
    assert masked.count("\n") >= 2


def test_mask_handles_an_escaped_quote():
    assert mask(r"RETURN 'it\'s' AS x").endswith(" AS x")


def test_literal_spans_name_each_kind():
    kinds = [kind for _, _, kind in literal_spans("'a' // b\n`c` /* d */")]
    assert kinds == ["string", "comment", "name", "comment"]


@pytest.mark.parametrize("text", ["RETURN 'a", 'RETURN "a', "RETURN /* a", "RETURN `a"])
def test_a_literal_with_no_end_is_refused(text):
    refused(r, text)


# String literals and comments that hold fake patterns.


def test_a_string_with_a_fake_pattern_stays_as_it_was():
    cypher = "MATCH (i:Item) WHERE i.title = '(n:User)-[]->()' RETURN i"
    assert r(cypher) == f"MATCH (i:Item {W}) WHERE i.title = '(n:User)-[]->()' RETURN i"


def test_a_double_quoted_string_with_a_fake_pattern_stays():
    cypher = 'MATCH (i:Item) WHERE i.title = "(n)" RETURN i'
    assert r(cypher) == f'MATCH (i:Item {W}) WHERE i.title = "(n)" RETURN i'


def test_a_string_with_a_quote_and_a_fake_pattern_stays():
    cypher = r"MATCH (i:Item {title: 'it\'s (n:User)'}) RETURN i"
    assert r(cypher) == r"MATCH (i:Item {world_id: $world_id, title: 'it\'s (n:User)'}) RETURN i"


def test_a_comment_with_a_fake_pattern_stays():
    cypher = "// MATCH (n) USE x\nMATCH (i:Item) /* CALL db.labels() (u:User) */ RETURN i"
    assert r(cypher) == (
        f"// MATCH (n) USE x\nMATCH (i:Item {W}) /* CALL db.labels() (u:User) */ RETURN i"
    )


def test_offsets_after_a_multibyte_literal_stay_exact():
    cypher = "MATCH (a:Item {title: 'çafé ☕'})-[:LINK]->(b:Item) RETURN b"
    assert r(cypher) == (
        "MATCH (a:Item {world_id: $world_id, title: 'çafé ☕'})"
        f"-[:LINK {W}]->(b:Item {W}) RETURN b"
    )


# Rewrites.


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("MATCH (i:Item {id: $id}) RETURN i", f"MATCH (i:Item {WC}id: $id}}) RETURN i"),
        ("MATCH (i:Item) RETURN i", f"MATCH (i:Item {W}) RETURN i"),
        ("MATCH (:Item) RETURN 1", f"MATCH (:Item {W}) RETURN 1"),
        ("MATCH (i:Item {}) RETURN i", f"MATCH (i:Item {W}) RETURN i"),
        ("match (i:Item) return i", f"match (i:Item {W}) return i"),
        (
            "OPTIONAL MATCH (i:Item) RETURN i",
            f"OPTIONAL MATCH (i:Item {W}) RETURN i",
        ),
        ("MATCH (a:Alias), (b:Item) RETURN a, b", f"MATCH (a:Alias {W}), (b:Item {W}) RETURN a, b"),
        (
            "MATCH (i:Item)-[l:LINK {x: 1}]->(x) RETURN x",
            f"MATCH (i:Item {W})-[l:LINK {WC}x: 1}}]->(x) RETURN x",
        ),
        (
            "MATCH (i:Item)<-[:LINK]-(x) RETURN x",
            f"MATCH (i:Item {W})<-[:LINK {W}]-(x) RETURN x",
        ),
        (
            "MATCH (i:Item)-[:LINK]-(x) RETURN x",
            f"MATCH (i:Item {W})-[:LINK {W}]-(x) RETURN x",
        ),
        (
            "MATCH (i:Item {id: $id})-[:LINK]->(x) RETURN x",
            f"MATCH (i:Item {WC}id: $id}})-[:LINK {W}]->(x) RETURN x",
        ),
        (
            "MATCH (i:Item WHERE i.a = 1)-[l:LINK WHERE l.b = 2]->(x) RETURN x",
            f"MATCH (i:Item {W} WHERE i.a = 1)-[l:LINK {W} WHERE l.b = 2]->(x) RETURN x",
        ),
        (
            "MATCH (i:Item {a: 1} WHERE i.b = 2) RETURN i",
            f"MATCH (i:Item {WC}a: 1}} WHERE i.b = 2) RETURN i",
        ),
    ],
)
def test_rewrites(cypher, expected):
    assert r(cypher) == expected


def test_write_mode_stamps_created_nodes_and_links():
    assert w("CRE~ATE (i:Item {id: $id})") == plain(f"CRE~ATE (i:Item {WC}id: $id}})")
    assert w("MER~GE (a:Item {id: $id})") == plain(f"MER~GE (a:Item {WC}id: $id}})")
    assert w("MATCH (a:Item), (b:Item) CRE~ATE (a)-[:LINK {id: $id}]->(b)") == (
        plain(f"MATCH (a:Item {W}), (b:Item {W}) CRE~ATE (a)-[:LINK {WC}id: $id}}]->(b)")
    )


def test_merge_on_create_and_on_match_stay_readable():
    assert w("MER~GE (a:Item {id: 1}) ON CRE~ATE S~ET a.x = 1 ON MATCH S~ET a.y = 2") == (
        plain(f"MER~GE (a:Item {WC}id: 1}}) ON CRE~ATE S~ET a.x = 1 ON MATCH S~ET a.y = 2")
    )


def test_foreach_body_is_scoped():
    assert w("FOR~EACH (x IN $rows | CRE~ATE (:Item {id: x}))") == (
        plain(f"FOR~EACH (x IN $rows | CRE~ATE (:Item {WC}id: x}}))")
    )


# Label expressions.


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("MATCH (n:Item|Alias) RETURN n", f"MATCH (n:Item|Alias {W}) RETURN n"),
        (
            "MATCH (n:HistoryValue&PastValue) RETURN n",
            f"MATCH (n:HistoryValue&PastValue {W}) RETURN n",
        ),
        (
            "MATCH (n:HistoryValue:PastValue) RETURN n",
            f"MATCH (n:HistoryValue:PastValue {W}) RETURN n",
        ),
        ("MATCH (n:Item|Alias {id: 1}) RETURN n", f"MATCH (n:Item|Alias {WC}id: 1}}) RETURN n"),
    ],
)
def test_label_expressions(cypher, expected):
    assert r(cypher) == expected


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (n:Item|Nope) RETURN n",
        "MATCH (n:Item|User) RETURN n",
        "MATCH (n:!Item) RETURN n",
        "MATCH (n:%) RETURN n",
        "MATCH (n:(Item|Alias)) RETURN n",
        "MATCH (n:$($label)) RETURN n",
        "MATCH (n IS Item) RETURN n",
        "MATCH (n:`Item`) RETURN n",
    ],
)
def test_label_expressions_refused(cypher):
    refused(r, cypher)


def test_a_label_test_in_where_is_checked_and_not_rewritten():
    cypher = "MATCH (n:Item|Alias) WHERE n:Item RETURN n"
    assert r(cypher) == f"MATCH (n:Item|Alias {W}) WHERE n:Item RETURN n"
    cypher = "MATCH (n:Item|Alias) WHERE (n:Item) RETURN n"
    assert r(cypher) == f"MATCH (n:Item|Alias {W}) WHERE (n:Item) RETURN n"
    cypher = "MATCH (n:Item|Alias) WHERE (n:Item OR n:Alias) RETURN n"
    assert r(cypher) == f"MATCH (n:Item|Alias {W}) WHERE (n:Item OR n:Alias) RETURN n"
    refused(r, "MATCH (n:Item) WHERE n:User RETURN n")
    refused(r, "MATCH (n:Item) WHERE (n:User) RETURN n")
    refused(r, "MATCH (n:Item) RETURN n:Nope")


def test_a_type_predicate_is_not_a_label_test():
    cypher = "MATCH (n:Item) WHERE n.a IS :: STRING RETURN n.a :: INTEGER"
    assert r(cypher) == f"MATCH (n:Item {W}) WHERE n.a IS :: STRING RETURN n.a :: INTEGER"


# Variable-length links.


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        (
            "MATCH (a:Item)-[:LINK*1..3]->(b) RETURN b",
            f"MATCH (a:Item {W})-[:LINK*1..3 {W}]->(b) RETURN b",
        ),
        ("MATCH (a:Item)-[:LINK*]->(b) RETURN b", f"MATCH (a:Item {W})-[:LINK* {W}]->(b) RETURN b"),
        (
            "MATCH (a:Item)-[:LINK*2]->(b) RETURN b",
            f"MATCH (a:Item {W})-[:LINK*2 {W}]->(b) RETURN b",
        ),
        (
            "MATCH (a:Item)-[:LINK*..4]->(b) RETURN b",
            f"MATCH (a:Item {W})-[:LINK*..4 {W}]->(b) RETURN b",
        ),
        (
            "MATCH (a:Item)-[r:LINK*1.. {x: 1}]->(b) RETURN b",
            f"MATCH (a:Item {W})-[r:LINK*1.. {WC}x: 1}}]->(b) RETURN b",
        ),
        (
            "MATCH (a:Item)-[:LINK]->{1,3}(b) RETURN b",
            f"MATCH (a:Item {W})-[:LINK {W}]->{{1,3}}(b) RETURN b",
        ),
        (
            "MATCH (a:Item)-[:LINK]->+(b) RETURN b",
            f"MATCH (a:Item {W})-[:LINK {W}]->+(b) RETURN b",
        ),
        (
            "MATCH (a:Item) ((x)-[:LINK]->(y)){1,3} (b) RETURN b",
            f"MATCH (a:Item {W}) ((x)-[:LINK {W}]->(y)){{1,3}} (b) RETURN b",
        ),
        (
            "MATCH SHORTEST 1 (a:Item)-[:LINK]->+(b) RETURN b",
            f"MATCH SHORTEST 1 (a:Item {W})-[:LINK {W}]->+(b) RETURN b",
        ),
        (
            "MATCH p = (a:Item)-[:LINK*1..2]->(b) RETURN p",
            f"MATCH p = (a:Item {W})-[:LINK*1..2 {W}]->(b) RETURN p",
        ),
    ],
)
def test_variable_length_links(cypher, expected):
    assert r(cypher) == expected


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (a)-[:LINK*0..3]->(b) RETURN b",
        "MATCH (a)-[:LINK*0]->(b) RETURN b",
        "MATCH (a)-[:LINK*0..]->(b) RETURN b",
        "MATCH (a)-[:LINK]->*(b) RETURN b",
        "MATCH (a)-[:LINK]->{0,3}(b) RETURN b",
        "MATCH (a)-[:LINK]->{,3}(b) RETURN b",
        "MATCH (a) ((x)-[:LINK]->(y))* (b) RETURN b",
        "MATCH (a) ((x)-[:LINK]->(y)){0,2} (b) RETURN b",
    ],
)
def test_a_zero_lower_bound_is_refused(cypher):
    """A zero-length match binds a node with no link, so it would reach every world."""
    refused(r, cypher, "zero lower bound")


def test_shortest_path_function_is_refused_with_the_way_out():
    refused(r, "MATCH p = shortestPath((a:Item)-[:LINK*]-(b:Item)) RETURN p", "SHORTEST")
    refused(r, "MATCH (a:Item), (b:Item) RETURN allShortestPaths((a)-[:LINK*]-(b))", "SHORTEST")


# Subqueries.


def test_exists_subquery_with_match():
    cypher = "MATCH (a:Item) WHERE EXISTS { MATCH (a)-[:LINK]->(b:Item) } RETURN a"
    assert r(cypher) == (
        f"MATCH (a:Item {W}) WHERE EXISTS {{ MATCH (a)-[:LINK {W}]->(b:Item {W}) }} RETURN a"
    )


def test_exists_subquery_with_a_bare_pattern_and_where():
    cypher = "MATCH (a:Item) WHERE EXISTS { (a)-[:LINK]->(b:Item) WHERE b.x = 1 } RETURN a"
    assert r(cypher) == (
        f"MATCH (a:Item {W}) WHERE EXISTS {{ (a)-[:LINK {W}]->(b:Item {W}) WHERE b.x = 1 }} "
        "RETURN a"
    )


def test_count_and_collect_subqueries():
    cypher = (
        "MATCH (a:Item) RETURN COUNT { (a)-[:LINK]->() } AS c, "
        "COLLECT { MATCH (a)-[:LINK]->(b:Item) RETURN b.id } AS ids"
    )
    assert r(cypher) == (
        f"MATCH (a:Item {W}) RETURN COUNT {{ (a)-[:LINK {W}]->() }} AS c, "
        f"COLLECT {{ MATCH (a)-[:LINK {W}]->(b:Item {W}) RETURN b.id }} AS ids"
    )


def test_call_subquery_with_import_list():
    cypher = "MATCH (a:Item) CALL (a) { MATCH (a)-[:LINK]->(b:Item) RETURN b } RETURN b"
    assert r(cypher) == (
        f"MATCH (a:Item {W}) CALL (a) {{ MATCH (a)-[:LINK {W}]->(b:Item {W}) RETURN b }} RETURN b"
    )


def test_call_subquery_with_no_import():
    cypher = "CALL { MATCH (b:Item) RETURN b } RETURN b"
    assert r(cypher) == f"CALL {{ MATCH (b:Item {W}) RETURN b }} RETURN b"


def test_optional_call_subquery():
    assert r("OPTIONAL CALL () { MATCH (b:Item) RETURN b } RETURN b") == (
        f"OPTIONAL CALL () {{ MATCH (b:Item {W}) RETURN b }} RETURN b"
    )


def test_a_refusal_inside_a_subquery_still_fires():
    refused(r, "MATCH (a:Item) WHERE EXISTS { MATCH (u:User) } RETURN a", "other scope")
    refused(r, "CALL { MATCH (n) RETURN n } RETURN n")
    refused(r, "MATCH (a:Item) RETURN COUNT { MATCH (a)-[r]->() } AS c")


def test_a_subquery_inside_a_map_value_is_scanned():
    refused(r, "RETURN {a: EXISTS { MATCH (n) }} AS m")
    refused(r, "MATCH (i:Item {a: EXISTS { MATCH (u:User) }}) RETURN i")


def test_call_returned_node_is_bound_afterwards():
    cypher = "CALL { MATCH (b:Item) RETURN b } MATCH (b) RETURN b"
    assert r(cypher) == f"CALL {{ MATCH (b:Item {W}) RETURN b }} MATCH (b) RETURN b"


def test_union_arms_are_each_scoped():
    cypher = "MATCH (a:Item) RETURN a.id AS id UNION ALL MATCH (b:Alias) RETURN b.id AS id"
    assert r(cypher) == (
        f"MATCH (a:Item {W}) RETURN a.id AS id UNION ALL MATCH (b:Alias {W}) RETURN b.id AS id"
    )


# A variable that left scope is a fresh variable to the server, which then scans every world.


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (a:Item) WITH a.id AS id MATCH (a) RETURN a",
        "MATCH (a:Item) WITH 1 AS a MATCH (a) RETURN a",
        "MATCH (a:Item) RETURN a UNION MATCH (a) RETURN a",
        "MATCH (a:Item) CALL { MATCH (a) RETURN a AS b } RETURN b",
        "MATCH (a:Item) CALL () { MATCH (a) RETURN a AS b } RETURN b",
        "MATCH (a:Item) WITH a.id AS id WHERE EXISTS { MATCH (a) } RETURN id",
        "UNWIND $xs AS a MATCH (a) RETURN a",
        "MATCH (a:Item) WHERE size([(a)-[:LINK]->(z) | z]) > 0 MATCH (z) RETURN count(z)",
        "MATCH (a:Item) WHERE EXISTS { (a)-[:LINK]->(z) } MATCH (z) RETURN z",
        "MATCH (a:Item) RETURN [(a)-[:LINK]->(z) | z] AS l UNION MATCH (z) RETURN z AS l",
        "MATCH (a:Item {x: [(a)-[:LINK]->(z) | z]}) MATCH (z) RETURN z",
    ],
)
def test_a_stale_variable_does_not_count_as_bound(cypher):
    refused(r, cypher, "no label and no link")


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("MATCH (a:Item) MATCH (a) RETURN a", f"MATCH (a:Item {W}) MATCH (a) RETURN a"),
        (
            "MATCH (a:Item) WITH a MATCH (a) RETURN a",
            f"MATCH (a:Item {W}) WITH a MATCH (a) RETURN a",
        ),
        (
            "MATCH (a:Item) WITH a AS b MATCH (b) RETURN b",
            f"MATCH (a:Item {W}) WITH a AS b MATCH (b) RETURN b",
        ),
        (
            "MATCH (a:Item) WITH * MATCH (a) RETURN a",
            f"MATCH (a:Item {W}) WITH * MATCH (a) RETURN a",
        ),
        (
            "MATCH (a:Item) WITH DISTINCT a ORDER BY a.x LIMIT 1 MATCH (a) RETURN a",
            f"MATCH (a:Item {W}) WITH DISTINCT a ORDER BY a.x LIMIT 1 MATCH (a) RETURN a",
        ),
        (
            "MATCH (a:Item) CALL (a) { MATCH (a) RETURN a AS b } RETURN b",
            f"MATCH (a:Item {W}) CALL (a) {{ MATCH (a) RETURN a AS b }} RETURN b",
        ),
        (
            "MATCH (a:Item) CALL (*) { MATCH (a) RETURN a AS b } RETURN b",
            f"MATCH (a:Item {W}) CALL (*) {{ MATCH (a) RETURN a AS b }} RETURN b",
        ),
    ],
)
def test_a_live_variable_counts_as_bound(cypher, expected):
    assert r(cypher) == expected


# Expressions that look like patterns.


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("MATCH (n:Item) RETURN n {.id, .title}", f"MATCH (n:Item {W}) RETURN n {{.id, .title}}"),
        (
            "MATCH (n:Item) RETURN n {.*, x: 1} AS m",
            f"MATCH (n:Item {W}) RETURN n {{.*, x: 1}} AS m",
        ),
        ("MATCH (n:Item) RETURN (n {.id}) AS m", f"MATCH (n:Item {W}) RETURN (n {{.id}}) AS m"),
        ("MATCH (n:Item) RETURN n {.id, k: n.x}", f"MATCH (n:Item {W}) RETURN n {{.id, k: n.x}}"),
        (
            "MATCH (n:Item) RETURN [x IN n.tags WHERE x <> 'a' | x + 1] AS l",
            f"MATCH (n:Item {W}) RETURN [x IN n.tags WHERE x <> 'a' | x + 1] AS l",
        ),
        ("RETURN [x IN range(1, 3) | (x)] AS l", "RETURN [x IN range(1, 3) | (x)] AS l"),
        ("RETURN (1 + 2) * (3) AS x", "RETURN (1 + 2) * (3) AS x"),
        ("RETURN ($a + 1) AS x, ($b) AS y", "RETURN ($a + 1) AS x, ($b) AS y"),
        (
            "MATCH (n:Item) WHERE (n.a - 1) < (n.b) RETURN n",
            f"MATCH (n:Item {W}) WHERE (n.a - 1) < (n.b) RETURN n",
        ),
        (
            "MATCH (n:Item) WHERE n.a STARTS WITH 'x' AND n.b ENDS WITH 'y' RETURN n",
            f"MATCH (n:Item {W}) WHERE n.a STARTS WITH 'x' AND n.b ENDS WITH 'y' RETURN n",
        ),
        (
            "MATCH (n:Item) RETURN CASE WHEN n.a THEN (n.b) ELSE count(n) END AS c",
            f"MATCH (n:Item {W}) RETURN CASE WHEN n.a THEN (n.b) ELSE count(n) END AS c",
        ),
        (
            "MATCH (n:Item) RETURN n.s~et AS order_of, n.match AS m",
            f"MATCH (n:Item {W}) RETURN n.s~et AS order_of, n.match AS m",
        ),
        (
            "MATCH (n:Item) RETURN {match: 1, s~et: 2} AS m",
            f"MATCH (n:Item {W}) RETURN {{match: 1, s~et: 2}} AS m",
        ),
    ],
)
def test_expressions_stay_as_they_were(cypher, expected):
    assert r(cypher) == plain(expected)


def test_pattern_comprehension():
    cypher = "MATCH (a:Item) RETURN [(a)-[:LINK]->(b:Item) WHERE b.x = 1 | b.y] AS l"
    assert r(cypher) == (
        f"MATCH (a:Item {W}) RETURN [(a)-[:LINK {W}]->(b:Item {W}) WHERE b.x = 1 | b.y] AS l"
    )


def test_pattern_comprehension_with_a_path_variable():
    cypher = "MATCH (a:Item) RETURN [p = (a)-[:LINK]->() | p] AS l"
    assert r(cypher) == f"MATCH (a:Item {W}) RETURN [p = (a)-[:LINK {W}]->() | p] AS l"


def test_a_link_predicate_in_where():
    cypher = "MATCH (a:Item) WHERE NOT (a)-[:LINK]->(:Item) RETURN a"
    assert r(cypher) == f"MATCH (a:Item {W}) WHERE NOT (a)-[:LINK {W}]->(:Item {W}) RETURN a"


def test_a_pattern_inside_a_function_call():
    cypher = "MATCH (a:Item) RETURN size([(a)-[:LINK]->(b) | b]) AS n, exists((a)-[:LINK]->()) AS e"
    assert r(cypher) == (
        f"MATCH (a:Item {W}) RETURN size([(a)-[:LINK {W}]->(b) | b]) AS n, "
        f"exists((a)-[:LINK {W}]->()) AS e"
    )


def test_refusals_inside_comprehensions_fire():
    refused(r, "MATCH (a:Item) RETURN [(a)-[]->(b) | b] AS l", "no type")
    refused(r, "MATCH (a:Item) RETURN [(a)-[:LINK]->(u:User) | u] AS l", "other scope")
    refused(r, "MATCH (a:Item) RETURN [(a)-[:NOPE]->(b) | b] AS l")


# Label-less nodes.


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (n) RETURN n",
        "MATCH (n {id: $id}) RETURN n",
        "MATCH () RETURN 1",
        "MATCH p = (n) RETURN p",
        "MATCH (a:Item), (n) RETURN n",
        "MATCH ((n)) RETURN n",
        "OPTIONAL MATCH (n) RETURN n",
        "MATCH ANY (n) RETURN n",
        "MATCH (n WHERE n.id = 1) RETURN n",
    ],
)
def test_an_unbound_lone_node_is_refused(cypher):
    refused(r, cypher, "no label and no link")


def test_bound_and_linked_label_less_nodes_pass():
    assert r("MATCH (i:Item) MATCH (i {id: 1}) RETURN i") == (
        f"MATCH (i:Item {W}) MATCH (i {{id: 1}}) RETURN i"
    )
    assert r("MATCH (a)-[:LINK]->(b) RETURN b") == f"MATCH (a)-[:LINK {W}]->(b) RETURN b"
    assert r("MATCH (:Item)-[:LINK]->()-[:LINK]->(b) RETURN b") == (
        f"MATCH (:Item {W})-[:LINK {W}]->()-[:LINK {W}]->(b) RETURN b"
    )


# The refusals of the design, one by one.


@pytest.mark.parametrize(
    ("cypher", "match"),
    [
        ("MATCH (n:Nope) RETURN n", "catalogue"),
        ("MATCH (n:item) RETURN n", "catalogue"),
        ("MATCH (a:Item)-[:NOPE]->(b) RETURN b", "not in this scope"),
        ("MATCH (a:Item)-[:LINK|NOPE]->(b) RETURN b", "not in this scope"),
        ("MATCH (n:User) RETURN n", "other scope"),
        ("MATCH (n:World) RETURN n", "other scope"),
        ("MATCH (n:Migration) RETURN n", "other scope"),
        ("MATCH (i:Item {world_id: 'x'}) RETURN i", "states world_id"),
        ("MATCH (i:Item {id: 1, world_id: $w}) RETURN i", "states world_id"),
        ("MATCH (i:Item WHERE i.world_id = 'x') RETURN i", "states world_id"),
        ("MATCH (a)-[l:LINK {world_id: 'x'}]->(b) RETURN b", "states world_id"),
        ("MATCH (n {world_id: 'x'})-[:LINK]->(b) RETURN b", "states world_id"),
        ("MATCH (i:Item {id: $world_id}) RETURN i", "bound by the layer"),
        ("MATCH (a:Item)-[r]-(b) RETURN b", "no type"),
        ("MATCH (a:Item)-[r]->(b) RETURN b", "no type"),
        ("MATCH (a:Item)<-[r]-(b) RETURN b", "no type"),
        ("MATCH (a:Item)-[]->(b) RETURN b", "no type"),
        ("MATCH (a:Item)-[r {id: 1}]->(b) RETURN b", "no type"),
        ("MATCH (a:Item)-->(b) RETURN b", "no type"),
        ("MATCH (a:Item)<--(b) RETURN b", "no type"),
        ("MATCH (a:Item)--(b) RETURN b", "no type"),
        ("MATCH (a:Item)<-->(b) RETURN b", "no type"),
        ("CALL db.labels() YIELD label RETURN label", "procedure"),
        ("CALL db.index.fulltext.queryNodes('x', 'y') YIELD node RETURN node", "procedure"),
        ("MATCH (a:Item) CALL apoc.x(a) RETURN a", "procedure"),
        ("USE neo4j MATCH (i:Item) RETURN i", "USE"),
        ("CALL { USE neo4j MATCH (i:Item) RETURN i } RETURN i", "USE"),
        ("LOAD CSV FROM 'file:///x' AS row RETURN row", "LOAD CSV"),
        ("MATCH (i:Item) LOAD CSV FROM 'file:///x' AS row RETURN row", "LOAD CSV"),
    ],
)
def test_design_refusals(cypher, match):
    refused(r, cypher, match)


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (a:Item)-[r:!LINK]->(b) RETURN b",
        "MATCH (a:Item)-[r:%]->(b) RETURN b",
        "MATCH (a:Item)-[r IS LINK]->(b) RETURN b",
        "MATCH (a:Item)-[r:$($t)]->(b) RETURN b",
        "MATCH (i:Item $props) RETURN i",
        "MATCH (a)-[l:LINK $props]->(b) RETURN b",
        "MATCH (`i`:Item) RETURN `i`",
        "RETURN \\u0028 AS x",
        "RETURN 'a\\u0027' AS x",
        "CYPHER 5 MATCH (i:Item) RETURN i",
        "EXPLAIN MATCH (i:Item) RETURN i",
        "RETURN 1; MATCH (n) RETURN n",
        "RETURN 1 AS x NEXT MATCH (i:Item) RETURN i",
        "LET x = 1 RETURN x",
        "MATCH (i:Item) FILTER i.a = 1 RETURN i",
        "MATCH (i:Item) SEARCH i IN (FULLTEXT INDEX grove_items_lexical FOR 'x' LIMIT 1) RETURN i",
        "SHOW INDEXES",
        "TERMINATE TRANSACTIONS 'x'",
        "WHEN true THEN { RETURN 1 AS x } RETURN x",
        "MATCH (i:Item) RETURN (i",
        "MATCH (i:Item)) RETURN i",
    ],
)
def test_forms_the_scoper_does_not_read_are_refused(cypher):
    refused(r, cypher)


# Write mode.


@pytest.mark.parametrize(
    ("cypher", "match"),
    [
        ("MATCH (i:Item) S~ET i = {id: 1}", "whole-map"),
        ("MATCH (i:Item) S~ET i = $row", "whole-map"),
        ("MATCH (i:Item) S~ET i.world_id = 'x'", "world_id"),
        ("MATCH (i:Item) S~ET i.a = 1, i.world_id = 'x'", "world_id"),
        ("MATCH (i:Item) S~ET i += {world_id: 'x'}", "world_id"),
        ("WITH {world_id: 'x'} AS m MATCH (i:Item) S~ET i += m", "states world_id"),
        ("MATCH (i:Item) WITH i, {a: {world_id: 'x'}} AS m S~ET i += m.a", "world_id"),
        ("MATCH (i:Item) REM~OVE i.world_id", "world_id"),
        ("MATCH (i:Item) REM~OVE i.a, i.world_id", "world_id"),
        ("MER~GE (i:Item {id: 1}) ON CRE~ATE S~ET i.world_id = 'x'", "world_id"),
        ("MER~GE (i:Item {id: 1}) ON MATCH S~ET i = $row", "whole-map"),
        ("MATCH (i:Item) WITH i AS j S~ET j.world_id = 'x'", "world_id"),
        ("MATCH (i:Item) FOR~EACH (x IN [1] | S~ET i.world_id = 'x')", "world_id"),
        ("CALL { MATCH (i:Item) S~ET i = {} RETURN i } RETURN i", "whole-map"),
        ("MATCH (i:Item) S~ET i[$key] = 1", "write form"),
        ("MATCH (i:Item) REM~OVE i[$key]", "write form"),
        ("MATCH (i:Item) S~ET (CASE WHEN true THEN i END).a = 1", "write form"),
        ("MATCH (i:Item) S~ET i:User", "other scope"),
        ("MATCH (i:Item) S~ET i:Nope", "catalogue"),
        ("MATCH (i:Item) REM~OVE i:User", "other scope"),
        ("MATCH (i:Item) S~ET i:$($label)", "does not read"),
        ("CRE~ATE (n)", "no label and no link"),
        ("CRE~ATE (n {id: 1})", "no label and no link"),
        ("CRE~ATE (a:Item)-[:LINK]->(b)", "needs a label"),
        ("MER~GE (a)-[:LINK]->(b)", "needs a label"),
        ("CRE~ATE (i:Item $props)", "map parameter"),
        ("CRE~ATE (u:User {id: 1})", "other scope"),
        ("CRE~ATE (a:Item)-[:NOPE]->(b:Item)", "not in this scope"),
        ("CRE~ATE CONSTRAINT x FOR (n:Item) REQUIRE n.a IS UNIQUE", "pattern"),
        ("CRE~ATE INDEX x FOR (n:Item) ON (n.a)", "pattern"),
        ("CRE~ATE (i:Item {world_id: 'x'})", "states world_id"),
        ("INSERT (i:Item)", "INSERT"),
    ],
)
def test_write_refusals(cypher, match):
    refused(w, cypher, match)


def test_write_forms_that_pass():
    assert w("MATCH (i:Item {id: $id}) S~ET i.a = 1, i.b = $b, i += {c: 2}, i:PastValue") == (
        plain(f"MATCH (i:Item {WC}id: $id}}) S~ET i.a = 1, i.b = $b, i += {{c: 2}}, i:PastValue")
    )
    assert w("MATCH (i:Item) REM~OVE i.a, i:PastValue") == (
        plain(f"MATCH (i:Item {W}) REM~OVE i.a, i:PastValue")
    )
    assert w("MATCH (i:Item) DET~ACH DEL~ETE i") == plain(f"MATCH (i:Item {W}) DET~ACH DEL~ETE i")
    assert w("MATCH (a:Item), (b:Item) MER~GE (a)-[l:LINK {id: 1}]->(b) RETURN l") == (
        plain(f"MATCH (a:Item {W}), (b:Item {W}) MER~GE (a)-[l:LINK {WC}id: 1}}]->(b) RETURN l")
    )


def test_read_mode_leaves_a_write_clause_to_the_read_transaction():
    # The store refuses these; the read transaction test proves it. Labels are still checked.
    cypher = plain("MATCH (i:Item) S~ET i = {}")
    assert r(cypher) == cypher.replace("(i:Item)", f"(i:Item {W})")
    refused(r, "MATCH (i:Item) S~ET i:User", "other scope")


# Platform mode.


def test_platform_mode_adds_nothing():
    cypher = "MATCH (u:User {id: $id}) RETURN u"
    assert p(cypher) == cypher


def test_platform_mode_allows_world_id_on_a_platform_row():
    cypher = "MATCH (t:OAuthToken {world_id: $world_id}) RETURN t"
    assert p(cypher) == cypher


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (i:Item) RETURN i",
        "MATCH (u:User) WHERE u:Item RETURN u",
        "MATCH (u:User|Item) RETURN u",
        "MATCH (u:User)-[:LINK]->(x) RETURN x",
        "MATCH (n) RETURN n",
        "MATCH (a:User)-[r]->(b) RETURN b",
        "CALL db.labels() YIELD label RETURN label",
    ],
)
def test_platform_refusals(cypher):
    refused(p, cypher)


def test_unknown_mode_is_refused():
    with pytest.raises(ScopeError):
        scope_cypher("RETURN 1", "admin")


# World ids and parameters.


@pytest.mark.parametrize("world_id", ["a", "A-b_9", "x" * 80])
def test_good_world_ids(world_id):
    assert check_world_id(world_id) == world_id


@pytest.mark.parametrize("world_id", ["", "x" * 81, "a b", "a'b", 'a"b', "a\n", None, 7])
def test_bad_world_ids(world_id):
    with pytest.raises(ScopeError):
        check_world_id(world_id)


def test_a_world_id_parameter_is_refused_also_when_nested():
    refuse_world_param({"id": 1, "rows": [{"a": 1}]})
    for params in (
        {"world_id": "x"},
        {"row": {"world_id": "x"}},
        {"rows": [{"a": 1}, {"world_id": "x"}]},
    ):
        with pytest.raises(ScopeError):
            refuse_world_param(params)
