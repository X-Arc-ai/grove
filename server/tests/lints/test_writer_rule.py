import pytest

from tests.lints.conftest import assert_clean, assert_red

ENTRY_POINTS = ("exe~cute_write", "exe~cute_query", "begin~_transaction")

ENTRY_FORMS = [
    "session.{name}(work)\n",
    "{name}(work)\n",
    "call = getattr(session, '{name}')\n",
    "from somewhere import {name}\n",
    "from somewhere import thing as {name}\n",
    "def {name}():\n    pass\n",
]

WRITES = [
    "ME~RGE (n:Item {id: 1})",
    "CRE~ATE (n:Item {id: 1})",
    "MATCH (n:Item) SE~T n.x = 1",
    "MATCH (n:Item) DE~LETE n",
    "MATCH (n:Item) DET~ACH DE~LETE n",
    "MATCH (n:Item) DET~ACH n",
    "MATCH (n:Item) REM~OVE n.x",
    "OPTIONAL MATCH (n:Item) SE~T n.x = 1",
    "UNWIND $rows AS row ME~RGE (n:Item {id: row})",
    "UNWIND $rows\nAS row\nCRE~ATE (n:Item)",
    "CALL { ME~RGE (n:Item) }",
    "FORE~ACH (x IN $rows | SE~T x.y = 1)",
    "LOAD CSV FROM 'x' AS row CRE~ATE (n)",
    "CRE~ATE CONSTRAINT c IF NOT EXISTS FOR (n:Item) REQUIRE n.id IS UNIQUE",
    "CRE~ATE INDEX i IF NOT EXISTS FOR (n:Item) ON (n.id)",
    "CRE~ATE FULLTEXT INDEX i FOR (n:Item) ON EACH [n.title]",
    "CRE~ATE TEXT INDEX i FOR (n:Item) ON (n.title)",
    "CRE~ATE OR REPLACE DATABASE x",
    "DR~OP CONSTRAINT c",
    "DR~OP INDEX i",
    "MATCH (n:Item)\n  SE~T n.x = 1\nRETURN n",
    "MATCH p = (a:Item)-[:LINK]->(b) SE~T b.x = 1",
    "MATCH p=(n:Item {id: $id}) DET~ACH DE~LETE n",
    "CRE~ATE p = (a:Item {id: $a})-[:LINK]->(b:Item {id: $b})",
    "ME~RGE p = (a:Item {id: $a})",
    "MATCH SHORTEST 1 (a:Item)-[:LINK]-+(b) SE~T b.seen = true",
    "MATCH ANY (a)-->(b) DE~LETE b",
    "MATCH REPEATABLE ELEMENTS p = ANY SHORTEST (a)-->(b) DE~LETE b",
    "MATCH DIFFERENT RELATIONSHIPS ALL SHORTEST PATHS (a)-->(b) DE~LETE b",
    "MATCH SHORTEST 2 PATH GROUPS (a)-->(b) DE~LETE b",
]

PROSE = [
    "Set the value",
    "remove the file",
    "create a new item",
    "Delete the row, then merge the two lists and drop the rest.",
    "Detach the sheet and set it aside.",
    "Match the forms, then remove them (all of them).",
    "The matches = (1, 2) set stays.",
]

READS = [
    "MATCH (n:Item) WHERE n.title = 'SE~T x' RETURN n",
    'MATCH (n:Item) WHERE n.title = "DE~LETE it" RETURN n',
    "MATCH (n:Item) RETURN n // SE~T later",
    "MATCH (n:Item) /* ME~RGE */ RETURN n",
    "MATCH (n:Item) WHERE n.created_at > $at RETURN n.`SE~T`",
    "RETURN 'MATCH (n) SE~T n.x'",
]


def module(cypher):
    return f"query = {cypher!r}\n"


def test_clean_tree_is_clean(repo):
    repo.plant("server/grove/fine.py", "value = 1\n")
    assert_clean(repo.lint("writer"))


@pytest.mark.parametrize("name", ENTRY_POINTS)
@pytest.mark.parametrize("form", ENTRY_FORMS)
def test_entry_points_in_a_new_module(repo, name, form):
    assert_clean(repo.lint("writer"))
    repo.plant("server/grove/fresh.py", form.format(name=name))
    assert_red(repo.lint("writer"), "server/grove/fresh.py:1: writer/entry-point")


def test_entry_point_built_by_a_fold_of_constants(repo):
    repo.plant("server/grove/fresh.py", "call = getattr(session, 'execute_' + 'wri~te')\n")
    assert_red(repo.lint("writer"), "server/grove/fresh.py:1: writer/entry-point")


def test_entry_point_named_in_prose_stays_clean(repo):
    repo.plant("server/grove/fresh.py", '"""This module never calls exe~cute_write."""\n')
    assert_clean(repo.lint("writer"))


@pytest.mark.parametrize(
    "source",
    ["import neo4j\n", "import neo4j.time as t\n", "from neo4j import GraphDatabase\n"],
)
@pytest.mark.parametrize("rel", ["server/grove/fresh.py", "server/tests/store/fresh.py", "x.py"])
def test_neo4j_import_in_a_new_module(repo, source, rel):
    assert_clean(repo.lint("writer"))
    repo.plant(rel, source)
    assert_red(repo.lint("writer"), f"{rel}:1: writer/neo4j-import")


@pytest.mark.parametrize(
    ("rel", "source", "line"),
    [
        ("tools/grove-wipe", "#!/usr/bin/env python3\nimport neo4j\n", 2),
        ("tools/x.pyw", "import neo4j\n", 1),
    ],
)
def test_a_python_script_without_the_py_suffix(repo, rel, source, line):
    assert_clean(repo.lint("writer"))
    repo.plant(rel, source)
    assert_red(repo.lint("writer"), f"{rel}:{line}: writer/neo4j-import")


@pytest.mark.parametrize(
    "source",
    [
        "from tests.writer.zz_helper import wipe\n",
        "import tests.writer.zz_helper\n",
        "import tests\n",
        "from tests import writer as helpers\n",
    ],
)
@pytest.mark.parametrize("rel", ["server/grove/zz_api/use.py", "server/grove/writer/fresh.py"])
def test_production_code_may_not_import_the_tests(repo, source, rel):
    assert_clean(repo.lint("writer"))
    repo.plant(rel, source)
    assert_red(repo.lint("writer"), f"{rel}:1: writer/tests-import")


@pytest.mark.parametrize(
    ("rel", "source"),
    [
        ("server/tests/store/test_fresh.py", "from tests.writer.helper import plant\n"),
        ("server/grove/fresh.py", "from .tests import thing\nimport testsuite\n"),
    ],
)
def test_other_imports_of_a_tests_name_stay_clean(repo, rel, source):
    repo.plant(rel, source)
    assert_clean(repo.lint("writer"))


def test_neo4j_import_is_allowed_in_the_store(repo):
    repo.plant("server/grove/store/connection.py", "import neo4j\n")
    assert_clean(repo.lint("writer"))


def test_the_store_may_not_open_a_write(repo):
    repo.plant("server/grove/store/query.py", "session.exe~cute_write(work)\n")
    assert_red(repo.lint("writer"), "server/grove/store/query.py:1: writer/entry-point")


def test_a_relative_module_of_that_name_is_no_driver_import(repo):
    repo.plant("server/grove/fresh.py", "from .neo4j import thing\n")
    assert_clean(repo.lint("writer"))


@pytest.mark.parametrize("cypher", WRITES)
def test_cypher_write_in_a_python_string(repo, cypher):
    assert_clean(repo.lint("writer"))
    repo.plant("server/grove/fresh.py", module(cypher))
    assert_red(repo.lint("writer"), "server/grove/fresh.py")


@pytest.mark.parametrize("cypher", WRITES)
def test_lower_case_cypher_write(repo, cypher):
    assert_clean(repo.lint("writer"))
    repo.plant("server/grove/fresh.py", module(cypher.lower()))
    assert_red(repo.lint("writer"), "server/grove/fresh.py")


@pytest.mark.parametrize(
    "source",
    [
        'name = "Item"\nquery = f"ME~RGE (n:{name} {{id: 1}})"\n',
        'field = "x"\nquery = f"MATCH (n) SE~T n.{field} = 1"\n',
        'query = f"MATCH (n) {clause} SE~T n.x = 1"\n',
        'query = f"""\nMATCH (n)\nDE~LETE n {tail}\n"""\n',
    ],
)
def test_f_string(repo, source):
    assert_clean(repo.lint("writer"))
    repo.plant("server/grove/fresh.py", source)
    assert_red(repo.lint("writer"), "server/grove/fresh.py")


@pytest.mark.parametrize(
    "source",
    [
        'query = "MATCH (n) " + "SE~T n.x = 1"\n',
        'query = "ME" + "RGE (n)"\n',
        'query = "MATCH (n) " + clause + " SE~T n.x = 1"\n',
        'query = "MATCH (n) " + f"SE~T n.{field} = 1"\n',
        'query = ("MATCH (n) "\n    "SE~T n.x = 1")\n',
        'run("MATCH (n) " + "DE~LETE n", 1)\n',
    ],
)
def test_folded_constants(repo, source):
    assert_clean(repo.lint("writer"))
    repo.plant("server/grove/fresh.py", source)
    assert_red(repo.lint("writer"), "server/grove/fresh.py")


def test_cypher_write_in_a_bytes_literal(repo):
    assert_clean(repo.lint("writer"))
    repo.plant("server/grove/fresh.py", 'query = b"MATCH (n) SE~T n.x = 1".decode()\n')
    assert_red(repo.lint("writer"), "server/grove/fresh.py:1: writer/cypher")


def test_a_string_inside_an_f_string_hole_is_walked(repo):
    repo.plant("server/grove/fresh.py", """query = f"{run('ME~RGE (n)')}"\n""")
    assert_red(repo.lint("writer"), "server/grove/fresh.py:1: writer/cypher")


@pytest.mark.parametrize("suffix", ["cypher", "cql"])
def test_cypher_file_outside_the_migrations(repo, suffix):
    assert_clean(repo.lint("writer"))
    rel = f"server/grove/fresh.{suffix}"
    repo.plant(rel, "MATCH (n) RETURN n;\nSE~T n.x = 1;\n")
    assert_red(repo.lint("writer"), f"{rel}:2: writer/cypher")


def test_read_only_cypher_file_stays_clean(repo):
    repo.plant(
        "server/grove/fresh.cypher",
        "// SE~T nothing here\nMATCH (n:Item) WHERE n.title = 'ME~RGE' RETURN n;\n",
    )
    assert_clean(repo.lint("writer"))


@pytest.mark.parametrize("suffix", ["sh", "bash"])
@pytest.mark.parametrize(
    "body",
    [
        'cypher-shell "MATCH (n) SE~T n.x = 1"\n',
        "cypher-shell 'ME~RGE (n:Item)'\n",
        "cypher-shell <<'CYPHER'\nMATCH (n)\nDE~LETE n;\nCYPHER\n",
        "cypher-shell <<-CYPHER\n\tME~RGE (n:Item);\n\tCYPHER\n",
        "cypher-shell \"MATCH (n:Item {id: 'x'}) SE~T n.x = 1\"\n",
        "cypher-shell <<'CY-1'\nMATCH (n:Item {id: 'a'}) SE~T n.y = 1\nCY-1\n",
        "cypher-shell <<-\"q.1\"\n\tMATCH (n:Item {id: 'a'}) SE~T n.y = 1\n\tq.1\n",
    ],
)
def test_shell_file(repo, suffix, body):
    assert_clean(repo.lint("writer"))
    rel = f"deploy/fresh.{suffix}"
    repo.plant(rel, f"#!/bin/bash\nset -euo pipefail\n{body}")
    assert_red(repo.lint("writer"), rel)


@pytest.mark.parametrize("opening", ["<<'CY-1'", "<<\\EOF", '<<-"q.1"', "<< \\'CY.2'"])
def test_heredoc_with_a_delimiter_beyond_word_letters(repo, opening):
    # No other shell line here: a `set -e` line would pair with the query and hide the gap.
    delimiter = opening.lstrip("<- \\").strip("'\"")
    body = f"cypher-shell {opening}\nMATCH (n:Item {{id: 'a'}}) SE~T n.y = 1\n{delimiter}\n"
    assert_clean(repo.lint("writer"))
    repo.plant("deploy/zz/x.sh", body)
    assert_red(repo.lint("writer"), "deploy/zz/x.sh:2: writer/cypher")


@pytest.mark.parametrize(
    "body",
    [
        'cypher-shell "MATCH (n:Item) RETURN n"\n',
        "cypher-shell \"MATCH (n:Item) WHERE n.t = 'SE~T x' RETURN n\"\n",
        "cypher-shell <<'CYPHER'\nMATCH (n:Item) RETURN n;\nCYPHER\nse~t -x\n",
        'echo "remove the file, then set the value"\nrm -f file\n',
        "# ME~RGE (n) lives in a comment\ncypher-shell 'MATCH (n) RETURN n'\n",
        "cypher-shell <<'CY-1'\nMATCH (n:Item) RETURN n;\nCY-1\nse~t -x\n",
        "cypher-shell <<\\EOF\nMATCH (n:Item) RETURN n;\nEOF\nse~t -x\n",
    ],
)
def test_shell_file_with_no_write_stays_clean(repo, body):
    repo.plant("deploy/fresh.sh", f"#!/bin/bash\nset -euo pipefail\n{body}")
    assert_clean(repo.lint("writer"))


@pytest.mark.parametrize(
    "rel",
    [
        "server/grove/writer/tx.py",
        "server/grove/writer/deep/down.py",
        "server/migrations/fresh.py",
        "server/tests/writer/test_fresh.py",
    ],
)
def test_allowed_folders_pass(repo, rel):
    repo.plant(
        rel,
        "import neo4j\nsession.exe~cute_write(work)\nquery = 'MATCH (n) SE~T n.x = 1'\n",
    )
    assert_clean(repo.lint("writer"))


@pytest.mark.parametrize(
    "rel", ["server/migrations/0001_fresh.cypher", "server/grove/writer/fresh.cypher"]
)
def test_allowed_folders_pass_for_cypher_files(repo, rel):
    repo.plant(rel, "ME~RGE (n:Item);\n")
    assert_clean(repo.lint("writer"))


@pytest.mark.parametrize(
    "rel",
    [
        "server/grove/writer2/fresh.py",
        "other/server/grove/writer/fresh.py",
        "server/tests/writer.py",
        "server/migrations2/fresh.py",
        "server/grove/lints/fresh.py",
        "server/tests/lints/fresh.py",
        "server/grove/store/fresh.py",
    ],
)
def test_near_copies_of_the_allowed_folders_are_not_allowed(repo, rel):
    assert_clean(repo.lint("writer"))
    repo.plant(rel, "query = 'MATCH (n) SE~T n.x = 1'\n")
    assert_red(repo.lint("writer"), rel)


@pytest.mark.parametrize("text", PROSE)
def test_prose_stays_clean(repo, text):
    repo.plant("server/grove/fresh.py", f'"""{text}"""\nmessage = {text!r}\n')
    assert_clean(repo.lint("writer"))


@pytest.mark.parametrize("cypher", READS)
def test_read_query_stays_clean(repo, cypher):
    repo.plant("server/grove/fresh.py", module(cypher))
    assert_clean(repo.lint("writer"))


def test_a_file_that_does_not_parse_is_a_finding(repo):
    repo.plant("broken.py", "def (:\n")
    assert_red(repo.lint("writer"), "broken.py:1: writer/unparsable")


def test_output_never_holds_the_matched_text(repo):
    repo.plant("a.py", "secret_context = 'ME~RGE (n:Item)'\n")
    result = repo.lint("writer")
    assert_red(result, "a.py:1: writer/cypher")
    assert "secret_context" not in result.stdout
    assert "Item" not in result.stdout


# D8's mechanical boundary (the Oct 2 drill): inside the writer, only the ontology operations and
# the world seed write the registry labels, and only the judgments writer writes judgments.

REGISTRY_WRITES = {
    "Kind": "CRE~ATE (n:Kind) SE~T n += $row",
    "Meaning": "MATCH (m:Meaning {id: $id}) SE~T m.status = 'canonical'",
    "PropertyName": "ME~RGE (p:PropertyName {id: $id})",
    "OntologyChange": "MATCH (c:OntologyChange) DE~LETE c",
    "Judgment": "CRE~ATE (j:Judgment) SE~T j += $row",
}
OWNERS = {
    "Kind": ("server/grove/writer/ontology.py", "server/grove/writer/worlds.py"),
    "Meaning": ("server/grove/writer/ontology.py", "server/grove/writer/worlds.py"),
    "PropertyName": ("server/grove/writer/ontology.py",),
    "OntologyChange": ("server/grove/writer/ontology.py",),
    "Judgment": ("server/grove/writer/judgments.py",),
}


@pytest.mark.parametrize("label", sorted(REGISTRY_WRITES))
@pytest.mark.parametrize("rel", ["server/grove/writer/sneaky.py", "server/grove/writer/apply.py"])
def test_a_registry_write_outside_its_owner_fails_inside_the_writer(repo, label, rel):
    repo.plant(rel, module(REGISTRY_WRITES[label]))
    assert_red(repo.lint("writer"), f"{rel}:1: writer/registry")


@pytest.mark.parametrize("label", sorted(REGISTRY_WRITES))
def test_a_registry_write_in_its_owner_file_stays_clean(repo, label):
    for rel in OWNERS[label]:
        repo.plant(rel, module(REGISTRY_WRITES[label]))
    assert_clean(repo.lint("writer"))


def test_a_registry_read_in_the_writer_stays_clean(repo):
    repo.plant(
        "server/grove/writer/sneaky.py",
        module("MATCH (m:Meaning) MATCH (j:Judgment) RETURN m.name, j.verdict"),
    )
    assert_clean(repo.lint("writer"))


def test_a_judgment_write_in_the_ontology_module_fails(repo):
    repo.plant("server/grove/writer/ontology.py", module(REGISTRY_WRITES["Judgment"]))
    assert_red(repo.lint("writer"), "server/grove/writer/ontology.py:1: writer/registry")
