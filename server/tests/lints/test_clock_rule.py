import pytest

from tests.lints.conftest import assert_clean, assert_red

BANNED_CALLS = [
    ("import time", f"time.{name}()")
    for name in (
        "time",
        "time_ns",
        "monotonic",
        "monotonic_ns",
        "perf_counter",
        "perf_counter_ns",
        "clock_gettime",
        "clock_gettime_ns",
        "process_time",
        "process_time_ns",
        "localtime",
        "gmtime",
        "ctime",
        "asctime",
    )
] + [
    ("import time", "time.strftime('%Y')"),
    ("import datetime", "datetime.datetime.now()"),
    ("import datetime", "datetime.datetime.utcnow()"),
    ("import datetime", "datetime.datetime.today()"),
    ("import datetime", "datetime.date.today()"),
    ("import uuid", "uuid.uuid1()"),
    ("import uuid", "uuid.uuid6()"),
    ("import uuid", "uuid.uuid7()"),
    ("import asyncio", "asyncio.get_event_loop().time()"),
    ("import asyncio", "asyncio.get_running_loop().time()"),
]

ALIASED = [
    "import datetime as d\nvalue = d.datetime.now()\n",
    "import datetime as d\nvalue = d.date.today()\n",
    "from datetime import datetime\nvalue = datetime.now()\n",
    "from datetime import datetime as dt\nvalue = dt.utcnow()\n",
    "from datetime import date as day\nvalue = day.today()\n",
    "import time as ticks\nvalue = ticks.monotonic()\n",
    "from time import time as t\nvalue = t()\n",
    "from time import time\nvalue = ti~me()\n",
    "from time import strftime as stamp\nvalue = stamp('%Y')\n",
    "from uuid import uuid7 as mint\nvalue = mint()\n",
    "from asyncio import get_running_loop as running\nvalue = running().time()\n",
    "import asyncio\nloop = asyncio.get_running_loop()\nvalue = loop.time()\n",
    "def read():\n    import time as inner\n    return inner.time()\n",
    "import time\nkept = time\nvalue = kept.time()\n",
    "from time import *\n",
]

BARE_REFERENCES = [
    "import time\nf = time.time\n",
    "import time\nf = time.time_ns\n",
    "import time\nvalues = map(time.perf_counter, [])\n",
    "from datetime import datetime\nf = datetime.now\n",
    "import datetime\nf = datetime.date.today\n",
    "import uuid\nf = uuid.uuid1\n",
    "from time import time as t\n",
    "import time\nf = time.strftime\nvalue = f('%Y')\n",
]

ALLOWED_PYTHON = [
    "import time\nvalue = time.strftime('%Y', moment)\n",
    "import time\ntime.sleep(0)\n",
    "import datetime\nspan = datetime.timedelta(days=1)\n",
    "import datetime\nvalue = datetime.datetime(2026, 1, 1)\n",
    "from datetime import datetime\nvalue = datetime.fromisoformat('2026-01-01')\n",
    "import uuid\nvalue = uuid.UUID(int=1)\n",
    "value = thing.time()\n",
    "def now():\n    return 1\nvalue = now()\n",
]

CYPHER_READS = [
    *[f"RETURN {name}~()" for name in ("date", "time", "timestamp")],
    *[f"RETURN {name}~()" for name in ("datetime", "localtime", "localdatetime")],
    *[
        f"RETURN {kind}.~{clock}()"
        for kind in ("date", "datetime", "time", "localtime", "localdatetime")
        for clock in ("transaction", "statement", "realtime")
    ],
    "RETURN DA~TETIME()",
    "RETURN da~tetime( )",
    "RETURN da~tetime.transaction('UTC')",
    "RETURN da~tetime({timezone: 'UTC'})",
]

CYPHER_NOT_READS = [
    "RETURN da~tetime('2026-01-01T00:00:00Z')",
    "RETURN da~te($day)",
    "RETURN upda~te()",
    "RETURN n.da~te()",
]


def test_clean_tree_is_clean(repo):
    repo.plant("server/grove/fine.py", "value = 1\n")
    repo.plant("server/migrations/0001_fine.cypher", "RETURN 1;\n")
    assert_clean(repo.lint("clock"))


@pytest.mark.parametrize(("line", "call"), BANNED_CALLS)
def test_each_banned_call(repo, line, call):
    assert_clean(repo.lint("clock"))
    repo.plant("server/grove/fresh.py", f"{line}\nvalue = {call}\n")
    assert_red(repo.lint("clock"), "server/grove/fresh.py:2: clock/python")


@pytest.mark.parametrize("source", ALIASED)
def test_aliased_imports(repo, source):
    assert_clean(repo.lint("clock"))
    repo.plant("server/tests/fresh.py", source)
    assert_red(repo.lint("clock"), "server/tests/fresh.py")


@pytest.mark.parametrize("source", BARE_REFERENCES)
def test_bare_references(repo, source):
    assert_clean(repo.lint("clock"))
    repo.plant("tools/fresh.py", source)
    assert_red(repo.lint("clock"), "tools/fresh.py")


@pytest.mark.parametrize("source", ALLOWED_PYTHON)
def test_lookalikes_stay_clean(repo, source):
    repo.plant("server/grove/fresh.py", source)
    assert_clean(repo.lint("clock"))


@pytest.mark.parametrize("cypher", CYPHER_READS)
def test_database_clock_in_a_python_string(repo, cypher):
    assert_clean(repo.lint("clock"))
    repo.plant("server/grove/fresh.py", f"query = {cypher!r}\n")
    assert_red(repo.lint("clock"), "server/grove/fresh.py:1: clock/cypher")


@pytest.mark.parametrize("cypher", CYPHER_READS)
@pytest.mark.parametrize("suffix", ["cypher", "cql"])
def test_database_clock_in_a_cypher_file(repo, cypher, suffix):
    assert_clean(repo.lint("clock"))
    repo.plant(f"server/migrations/0001_fresh.{suffix}", f"RETURN 1;\n{cypher};\n")
    assert_red(repo.lint("clock"), f"server/migrations/0001_fresh.{suffix}:2: clock/cypher")


@pytest.mark.parametrize("cypher", CYPHER_NOT_READS)
def test_cypher_that_reads_no_clock(repo, cypher):
    repo.plant("server/grove/fresh.py", f"query = {cypher!r}\n")
    repo.plant("server/migrations/0001_fresh.cypher", f"{cypher};\n")
    assert_clean(repo.lint("clock"))


def test_database_clock_in_a_cypher_comment_or_literal_is_clean(repo):
    repo.plant(
        "server/migrations/0001_fresh.cypher",
        "// da~tetime()\nRETURN 'da~tetime()', 1 /* ti~mestamp() */;\n",
    )
    assert_clean(repo.lint("clock"))


def test_database_clock_in_an_f_string_and_a_fold(repo):
    repo.plant("a.py", 'query = f"RETURN da~tetime() AS {name}"\n')
    assert_red(repo.lint("clock"), "a.py:1: clock/cypher")
    repo.plant("a.py", 'query = "RETURN date" + "ti~me()"\n')
    assert_red(repo.lint("clock"), "a.py:1: clock/cypher")


def test_clock_provider_itself_is_allowed(repo):
    repo.plant("server/grove/clock.py", "import time\nvalue = time.time()\n")
    repo.plant("server/grove/fine.py", "value = 1\n")
    assert_clean(repo.lint("clock"))


@pytest.mark.parametrize(
    "rel",
    [
        "server/grove/clock2.py",
        "server/grove/lints/clock.py",
        "other/server/grove/clock.py",
        "clock.py",
        "server/grove/lints/fresh.py",
        "server/tests/lints/fresh.py",
    ],
)
def test_only_that_one_path_is_allowed(repo, rel):
    assert_clean(repo.lint("clock"))
    repo.plant(rel, "import time\nvalue = time.time()\n")
    assert_red(repo.lint("clock"), rel)


def test_a_file_that_does_not_parse_is_a_finding(repo):
    repo.plant("broken.py", "def (:\n")
    assert_red(repo.lint("clock"), "broken.py:1: clock/unparsable")


def test_output_never_holds_the_matched_text(repo):
    repo.plant("a.py", "import time\nsecret_context = time.time()\n")
    result = repo.lint("clock")
    assert_red(result, "a.py:2: clock/python")
    assert "secret_context" not in result.stdout
