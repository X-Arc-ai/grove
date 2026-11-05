"""The real import chain on the real model, with short invented text. Never in CI.

A synthetic export in the claude.ai format goes through every real part: the file, the HTTP
upload with a session of an ordinary world, the ordinary upload policy, the Request, the runner
loop, OpenCode in an isolated HOME, the gateway, the Meta model, the writer and the graph view.
Then the same file goes again and adds nothing, and a held part and a failed run stay visible.

Run in a lane, from server/. The store's env comes from ~/.grove/neo4j-dev.env, the Meta key
from MODEL_API_KEY, else from ~/.meta/model-api.env:

    GROVE_SLICE_ID=real-import GROVE_REAL_CHAIN_EVIDENCE=<dir> GROVE_REAL_CHAIN_HOME=<dir> \
        uv run pytest tests/runner/test_real_import_live.py -m live -s

GROVE_REAL_CHAIN_EVIDENCE takes the answers of each step (ids, counts, codes and invented text;
no key and no token). GROVE_REAL_CHAIN_HOME is OpenCode's HOME and every XDG folder. Both default
to the test's own temporary folder. The API takes port 18471 and the gateway 18472, or
GROVE_REAL_CHAIN_API_PORT and GROVE_REAL_CHAIN_GATEWAY_PORT.

The dev store is shared. The runner's API lists only this test's world (PassSettings.only_worlds),
and a second world of this test holds due work to prove it.
"""

import io
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import zipfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from grove import clock
from grove.api.app import create_app
from grove.demo_policy import is_demo_world
from grove.ids import new_id
from grove.models.config import load_catalog, load_model_settings
from grove.models.gateway import Gateway
from grove.models.gateway import create_app as gateway_app
from grove.runner.loop import LoopSettings, Runner, config_home, opencode_env, prepare_home
from grove.runner.passes import PassSettings, RunnerAuth
from grove.runner.tokens import runner_token_hash
from grove.store.query import WorldQuery
from grove.views.session import SESSION_COOKIE
from grove.writer.testing import plant_session, seed_test_world
from tests.runner.helpers import queue_store

pytestmark = pytest.mark.live

REPO = Path(__file__).resolve().parents[3]
BIN = Path(sys.executable).parent
MODEL = "muse-spark-1.3"
IMPORT_TIME_CAP = 600
MAX_PASSES = 3
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
ACCOUNT = "7c1f0e52-0000-4000-8000-00000000ac01"
FAIR = "7c1f0e52-0000-4000-8000-00000000f001"
LONG = "7c1f0e52-0000-4000-8000-00000000f002"
LATE = "7c1f0e52-0000-4000-8000-00000000f003"
LAST = "7c1f0e52-0000-4000-8000-00000000f004"
# Invented people, places and plans only (R3).
SAID = (
    "Yara Soltren and I agreed today that the Harrow Lane seed library opens at the Millbrook "
    "community barn. Tobin Ekwe will run its first workshop."
)
ANSWERED = "Noted. Yara Soltren and Tobin Ekwe are on the Harrow Lane seed library plan."
LATE_SAID = "Tobin Ekwe asked me to find a second room at the Millbrook community barn."
LAST_SAID = "Yara Soltren keeps the only key to the north cupboard at the Millbrook barn."
# One message above the 32768-byte part limit: admission holds it, intact, and never claims it.
OVERSIZED = "The Harrow Lane seed library keeps its pear seeds in the north cupboard. " * 600
_TOKEN = re.compile(r"\b[gp]1\.[A-Za-z0-9_-]{16,}")


def _iso(seconds: int) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _message(uuid: str, sender: str, seconds: int, text: str, parent: str) -> dict[str, Any]:
    """One chat_messages row in the shape of a claude.ai export."""
    return {
        "uuid": uuid,
        "text": text,
        "content": [
            {
                "start_timestamp": _iso(seconds),
                "stop_timestamp": _iso(seconds),
                "type": "text",
                "text": text,
                "citations": [],
            }
        ],
        "sender": sender,
        "created_at": _iso(seconds),
        "updated_at": _iso(seconds),
        "attachments": [],
        "files": [],
        "parent_message_uuid": parent,
    }


def _conversation(uuid: str, name: str, turns: list[tuple[str, str]], start: int) -> dict[str, Any]:
    root = "00000000-0000-4000-8000-000000000000"
    rows, parent = [], root
    for number, (sender, text) in enumerate(turns):
        row_id = f"{uuid[:-4]}{number + 1:04x}"[:36]
        rows.append(_message(row_id, sender, start + number * 60, text, parent))
        parent = row_id
    return {
        "uuid": uuid,
        "name": name,
        "summary": "",
        "created_at": _iso(start),
        "updated_at": _iso(start + 3600),
        "account": {"uuid": ACCOUNT},
        "chat_messages": rows,
    }


def export_zip(conversations: list[dict[str, Any]]) -> bytes:
    """The export archive: conversations.json, and the files beside it the parser never opens."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, doc in (
            ("conversations.json", conversations),
            ("users.json", [{"uuid": ACCOUNT, "full_name": "Invented Person"}]),
            ("projects.json", []),
        ):
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 21, 8, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, json.dumps(doc, ensure_ascii=False, indent=1))
    return buffer.getvalue()


def fair() -> dict[str, Any]:
    return _conversation(FAIR, "Seed library", [("human", SAID), ("assistant", ANSWERED)], 0)


def _meta_key() -> dict[str, str]:
    """The key's env name and value, from the environment or the chmod-600 file. Never printed."""
    found = os.environ.get("MODEL_API_KEY")
    if not found:
        path = Path.home() / ".meta" / "model-api.env"
        for line in path.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.strip().removeprefix("export ").partition("=")
            if separator and name == "MODEL_API_KEY":
                found = value.strip().strip("'\"")
    if not found:
        pytest.fail("MODEL_API_KEY is not set: this test calls the real model")
    return {"MODEL_API_KEY": found}


class Fixed:
    """An ASGI app under uvicorn on a named loopback port, in a thread of this process."""

    def __init__(self, app: Any, port: int, health: str) -> None:
        self.url = f"http://127.0.0.1:{port}"
        self.health = health
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", access_log=False
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "Fixed":
        self.thread.start()
        with httpx.Client(trust_env=False) as client:
            for _ in range(400):
                try:
                    client.get(self.url + self.health, timeout=1)
                    return self
                except httpx.TransportError:
                    threading.Event().wait(0.05)
        raise RuntimeError("the app did not start")

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=30)


class Evidence:
    """Each step's answer, as one JSON file. The test reads nothing back from here."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def save(self, name: str, value: Any) -> Any:
        text = json.dumps(value, indent=1, default=str, ensure_ascii=False)
        (self.folder / f"{name}.json").write_text(text + "\n", encoding="utf-8")
        return value

    def scan(self, key: str) -> dict[str, int]:
        """No key and no pass token in any file here. Counts only."""
        files = hits = 0
        for path in self.folder.rglob("*"):
            if path.is_file():
                files += 1
                text = path.read_bytes().decode("utf-8", "replace")
                hits += text.count(key) + len(_TOKEN.findall(text))
        return {"files": files, "secret_hits": hits}


class Chain:
    def __init__(self, driver, world, cookie, api_url, runner_token, evidence) -> None:
        self.driver = driver
        self.world = world
        self.query = WorldQuery(driver, world)
        self.evidence = evidence
        self.user = httpx.Client(
            base_url=api_url,
            cookies={SESSION_COOKIE: cookie},
            timeout=httpx.Timeout(60.0, connect=5.0),
            trust_env=False,
        )
        self.service = httpx.Client(
            base_url=api_url,
            headers={"Authorization": f"Bearer {runner_token}"},
            timeout=httpx.Timeout(60.0, connect=5.0),
            trust_env=False,
        )
        self.progress_log: list[dict[str, Any]] = []

    def close(self) -> None:
        self.user.close()
        self.service.close()

    def upload(self, data: bytes) -> dict[str, Any]:
        response = self.user.post(
            "/v1/sources/upload?provider=claude_ai",
            content=data,
            headers={"content-type": "application/zip"},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def progress(self, label: str) -> dict[str, Any]:
        response = self.user.get("/v1/sources/import-progress?provider=claude_ai")
        assert response.status_code == 200, response.text
        answer = response.json()
        self.progress_log.append({"label": label, "answer": answer})
        self.evidence.save("f-import-progress", self.progress_log)
        return answer

    def due(self) -> list[dict[str, Any]]:
        response = self.service.get("/v1/internal/runner/due")
        assert response.status_code == 200, response.text
        return response.json()["due"]

    def graph(self) -> dict[str, Any]:
        response = self.user.get("/v1/views/graph")
        assert response.status_code == 200, response.text
        return response.json()

    def node(self, item_id: str) -> dict[str, Any]:
        response = self.user.get(f"/v1/views/node/{item_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def requests(self) -> list[dict[str, Any]]:
        return self.query.read(
            "MATCH (r:Request) RETURN r.id AS id, r.status AS status, r.source_id AS source_id, "
            "r.attempts AS attempts, r.last_error_code AS last_error_code, r.run_id AS run_id "
            "ORDER BY r.id"
        )

    def runs(self) -> list[dict[str, Any]]:
        rows = self.query.read("MATCH (r:RunRecord) RETURN r ORDER BY r.started, r.id")
        out = []
        for row in rows:
            run = dict(row["r"])
            started, finished = run.get("started"), run.get("finished")
            seconds = (finished - started).total_seconds() if started and finished else None
            out.append(
                {
                    "id": run.get("id"),
                    "agent": run.get("agent"),
                    "trigger": run.get("trigger"),
                    "outcome": run.get("outcome"),
                    "input_ids": run.get("input_ids"),
                    "tokens_in": run.get("tokens_in"),
                    "tokens_cached": run.get("tokens_cached"),
                    "tokens_out": run.get("tokens_out"),
                    "token_cap": run.get("token_cap"),
                    "time_cap": run.get("time_cap"),
                    "started": started,
                    "finished": finished,
                    "duration_seconds": seconds,
                    "model": run.get("model"),
                    "summary": run.get("summary"),
                }
            )
        return out

    def model_calls(self) -> list[dict[str, Any]]:
        return self.query.read(
            "MATCH (c:ModelCall) RETURN c.run_id AS run_id, c.job AS job, c.model AS model, "
            "c.outcome AS outcome, c.input_tokens AS input_tokens, "
            "c.cached_tokens AS cached_tokens, c.output_tokens AS output_tokens, "
            "c.cost AS cost ORDER BY c.at, c.id"
        )

    def counts(self) -> dict[str, int]:
        counted = {}
        for name, label in (
            ("sources", "Source"),
            ("episodes", "Episode"),
            ("requests", "Request"),
            ("runs", "RunRecord"),
            ("model_calls", "ModelCall"),
            ("change_entries", "ChangeEntry"),
        ):
            counted[name] = self.query.read(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]
        return counted

    def written(self) -> dict[str, Any]:
        """What the import's run wrote: its item and link rows and the evidence that ties them to
        the source."""
        changes = self.query.read(
            "MATCH (c:ChangeEntry) WHERE c.request_id STARTS WITH 'import:' "
            "RETURN DISTINCT c.entity_type AS type, c.entity_id AS id, c.request_id AS request_id, "
            "c.run_id AS run_id ORDER BY type, id"
        )
        item_ids = [row["id"] for row in changes if row["type"] == "item"]
        link_ids = [row["id"] for row in changes if row["type"] == "link"]
        items = self.query.read(
            "MATCH (i:Item) WHERE i.id IN $ids "
            "RETURN i.id AS id, i.kind AS kind, i.title AS title, i.summary AS summary "
            "ORDER BY i.id",
            ids=item_ids,
        )
        links = self.query.read(
            "MATCH (a:Item)-[l:LINK]->(b:Item) WHERE l.id IN $ids "
            "RETURN l.id AS id, a.title AS from_title, l.meaning AS meaning, b.title AS to_title "
            "ORDER BY l.id",
            ids=link_ids,
        )
        evidence = self.query.read(
            "MATCH (e:Evidence) WHERE e.target_id IN $ids "
            "RETURN e.id AS id, e.target_id AS target_id, e.source_id AS source_id, "
            "e.source_type AS source_type, e.statement_class AS statement_class, "
            "e.locator AS locator, e.active AS active ORDER BY e.id",
            ids=item_ids + link_ids,
        )
        return {"changes": changes, "items": items, "links": links, "evidence": evidence}

    def run_one_pass(self, runner: Runner, label: str) -> None:
        """One tick of the real loop, polling the progress route while the pass runs."""
        started = runner.tick()
        assert started == 1, f"the runner started {started} passes for {label}"
        seen = False
        deadline = clock.monotonic() + IMPORT_TIME_CAP + 60
        while runner.running():
            if clock.monotonic() > deadline:
                pytest.fail(f"the pass {label} outlived its watchdog")
            if not seen:
                seen = self.progress(f"during {label}")["processing_count"] > 0
            threading.Event().wait(1.0)

    def wait_learner_due(self, seconds: int) -> bool:
        deadline = clock.monotonic() + seconds
        while clock.monotonic() < deadline:
            if any(row.get("agent") == "learner" for row in self.due()):
                return True
            threading.Event().wait(2.0)
        return False


def _pass_trace(home: Path, evidence: Evidence, seen: set[str]) -> None:
    """Each pass folder's tool calls, from OpenCode's own record, read after the process ended."""
    for work in sorted((home / "passes").glob("*")):
        if work.name in seen or not work.is_dir():
            continue
        seen.add(work.name)
        calls = []
        database = work / "data" / "opencode" / "opencode.db"
        if database.is_file():
            with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as conn:
                rows = conn.execute(
                    "SELECT part.data, message.data FROM part "
                    "JOIN message ON message.id = part.message_id "
                    "ORDER BY part.time_created, part.id"
                ).fetchall()
            for part_data, message_data in rows:
                part = json.loads(part_data)
                if part.get("type") != "tool":
                    continue
                state = part.get("state") or {}
                command = (state.get("input") or {}).get("command")
                calls.append(
                    {
                        "agent": json.loads(message_data).get("agent"),
                        "tool": part.get("tool"),
                        "status": state.get("status"),
                        "command": command[:240] if isinstance(command, str) else None,
                        "output_head": str(state.get("output") or state.get("error") or "")[:400],
                    }
                )
        evidence.save(f"pass-trace-{work.name}", calls)


def test_real_chain(driver, make_world, tmp_path):
    folder = os.environ.get("GROVE_REAL_CHAIN_EVIDENCE")
    evidence = Evidence(Path(folder) if folder else tmp_path / "evidence")
    home = Path(os.environ.get("GROVE_REAL_CHAIN_HOME") or tmp_path / "home")
    api_port = int(os.environ.get("GROVE_REAL_CHAIN_API_PORT") or 18471)
    gateway_port = int(os.environ.get("GROVE_REAL_CHAIN_GATEWAY_PORT") or 18472)
    opencode = os.environ.get("GROVE_OPENCODE_BIN") or str(
        Path.home() / ".opencode" / "bin" / "opencode"
    )
    key = _meta_key()

    world, decoy = make_world(), make_world()
    seed_test_world(driver, world, owner_user_id=new_id())
    seed_test_world(driver, decoy, owner_user_id=new_id())
    assert not is_demo_world(driver, world)
    cookie = plant_session(driver, world)
    # Due work in a world outside only_worlds: the runner must never list it.
    queue_store(driver, decoy)

    runner_token = secrets.token_urlsafe(32)
    gateway_secret = secrets.token_hex(32)
    auth = RunnerAuth(
        gateway_secret=gateway_secret.encode("utf-8"),
        runner_token_sha256=runner_token_hash(runner_token),
    )
    # Production's caps, so start_pass applies the import caps itself.
    pass_settings = PassSettings(only_worlds=frozenset({world}))
    api = create_app(driver=driver, runner_auth=auth, pass_settings=pass_settings)
    model_settings = load_model_settings({**key, "GROVE_GATEWAY_SECRET": gateway_secret})
    gateway = Gateway(driver, settings=model_settings, catalog=load_catalog())

    with (
        Fixed(api, api_port, "/healthz") as served,
        Fixed(gateway_app(gateway), gateway_port, "/health") as model_gateway,
    ):
        loop = LoopSettings(
            api_url=served.url,
            runner_token=runner_token,
            gateway_url=f"{model_gateway.url}/v1",
            home=home,
            config_dir=REPO / "runner",
            opencode_bin=opencode,
            brain_bin=str(BIN / "brain"),
            shell_bin=str(BIN / "grove-brain-shell"),
            poll_seconds=2.0,
            max_passes=1,
            keep_pass_logs=True,
        )
        chain = Chain(driver, world, cookie, served.url, runner_token, evidence)
        try:
            _walk(chain, loop, evidence, world, decoy, home)
        finally:
            chain.close()
            _pass_trace(home, evidence, set())
    scan = evidence.save("secret-scan", evidence.scan(key["MODEL_API_KEY"]))
    assert scan["secret_hits"] == 0


def _prove_isolation(loop, evidence, world, decoy, chain, home) -> None:
    prepare_home(home, loop.config_dir)
    grant = {
        "pass_token": "placeholder",
        "gateway_token": "placeholder",
        "agent": "learner",
        "run_id": "isolation-proof",
        "world_id": world,
        "time_cap_seconds": IMPORT_TIME_CAP,
    }
    env = opencode_env(loop, grant, home / "passes" / "isolation-proof")
    folders = {
        name: env[name]
        for name in (
            "HOME",
            "TMPDIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
        )
    }
    under_home = all(Path(value).is_relative_to(home) for value in folders.values())
    config = config_home(home)
    shipped = sorted(p.name for p in (loop.config_dir / "agents").glob("*.md"))
    placed = sorted(p.name for p in (config / "agents").glob("*.md"))
    same_config = (config / "opencode.json").read_bytes() == (
        loop.config_dir / "opencode.json"
    ).read_bytes()
    only_files = sorted(p.name for p in config.iterdir())
    due = chain.due()
    evidence.save(
        "isolation",
        {
            "world": world,
            "decoy_world_with_queued_store": decoy,
            "world_is_demo": False,
            "folders": folders,
            "all_under_home": under_home,
            "opencode_disable_claude_code": env["OPENCODE_DISABLE_CLAUDE_CODE"],
            "config_folder": only_files,
            "config_is_repo_runner_config": same_config,
            "agents": placed,
            "gateway_url": env["GROVE_GATEWAY_URL"],
            "due_before_upload": due,
        },
    )
    assert under_home and same_config and placed == shipped
    assert only_files == ["agents", "opencode.json"]
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert env["GROVE_GATEWAY_URL"].startswith("http://127.0.0.1:")
    assert due == [], "the runner lists a world outside only_worlds, or work before any upload"


def _walk(chain: Chain, loop, evidence, world, decoy, home) -> None:
    _prove_isolation(loop, evidence, world, decoy, chain, home)
    runner = Runner(loop)

    # a, b: the file through the real route and the ordinary policy.
    first = export_zip([fair()])
    (evidence.folder / "a-export.zip").write_bytes(first)
    idle = chain.progress("before upload")
    assert idle["state"] == "idle" and idle["requests_total"] == 0
    answer = evidence.save("b-upload-answer", chain.upload(first))
    assert answer["state"] == "taken_in"
    assert answer["processing"]["requests_added"] > 0
    assert answer["processing"]["held_sources"] == 0
    queued = chain.progress("after upload")
    assert queued["state"] == "queued"
    assert not any(queued["graph"].values())

    # c, d: the Request, the Learner pass and the real model.
    requests = evidence.save("c-requests-after-upload", chain.requests())
    import_requests = [row for row in requests if row["id"].startswith("import:")]
    assert len(import_requests) == answer["processing"]["requests_added"]
    assert [row["status"] for row in import_requests] == ["queued"] * len(import_requests)
    due = evidence.save("c-due-after-upload", chain.due())
    assert due == [{"world_id": world, "agent": "learner"}]

    for attempt in range(1, MAX_PASSES + 1):
        chain.run_one_pass(runner, f"pass {attempt}")
        chain.progress(f"after pass {attempt}")
        states = {row["id"]: row["status"] for row in chain.requests()}
        if all(states[row["id"]] == "saved" for row in import_requests):
            break
        if attempt < MAX_PASSES and not chain.wait_learner_due(240):
            break
    requests = evidence.save("c-requests-after-pass", chain.requests())
    runs = evidence.save("c-run-records", chain.runs())
    calls = evidence.save("d-model-calls", chain.model_calls())
    outcomes: dict[str, int] = {}
    for row in calls:
        outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
    last = runs[-1]
    used = (last["tokens_in"] or 0) + (last["tokens_out"] or 0)
    evidence.save(
        "d-model-call-counts",
        {
            "rows": len(calls),
            "by_outcome": outcomes,
            "models": sorted({row["model"] for row in calls}),
            "jobs": sorted({row["job"] for row in calls}),
            "runs": sorted({row["run_id"] for row in calls}),
            "input_tokens": sum(row["input_tokens"] or 0 for row in calls),
            "cached_tokens": sum(row["cached_tokens"] or 0 for row in calls),
            "output_tokens": sum(row["output_tokens"] or 0 for row in calls),
            "cost_usd": sum(row["cost"] or 0 for row in calls),
        },
    )
    evidence.save(
        "c-caps",
        {
            "passes": len(runs),
            "last_pass_tokens_in_plus_out": used,
            "token_cap": last["token_cap"],
            "last_pass_seconds": last["duration_seconds"],
            "time_cap": last["time_cap"],
            "fits_time_cap": (last["duration_seconds"] or 0) < IMPORT_TIME_CAP,
        },
    )
    assert all(run["token_cap"] is None for run in runs if run["agent"] == "learner")
    assert all(run["time_cap"] == IMPORT_TIME_CAP for run in runs if run["agent"] == "learner")
    assert outcomes.get("ok", 0) > 0, "no model call succeeded through the gateway"
    assert {row["model"] for row in calls} == {MODEL}
    assert last["outcome"] == "ok", "the Learner pass did not end ok"
    assert [r["status"] for r in requests if r["id"].startswith("import:")] == ["saved"] * len(
        import_requests
    ), "the import Request is not saved"

    # e: items, links and the session's graph.
    written = evidence.save("e-items-links-evidence", chain.written())
    assert written["items"], "the Learner wrote no item"
    graph = evidence.save("e-graph", chain.graph())
    nodes = {node["data"]["id"] for node in graph["elements"]["nodes"]}
    edges = {edge["data"]["id"] for edge in graph["elements"]["edges"]}
    assert {item["id"] for item in written["items"]} <= nodes
    assert {link["id"] for link in written["links"]} <= edges
    evidence.save("e-nodes", [chain.node(item["id"]) for item in written["items"]])
    sources = {row["source_id"] for row in import_requests}
    cited = {row["target_id"] for row in written["evidence"] if row["source_id"] in sources}
    assert cited == {row["id"] for row in written["items"] + written["links"]}

    # f: the finished state.
    after = chain.progress("after chain")
    assert after["state"] == "complete" and after["extracted_count"] == len(import_requests)
    assert after["graph"]["items_created"] == len(written["items"])
    assert any(
        entry["label"].startswith("during") and entry["answer"]["processing_count"] > 0
        for entry in chain.progress_log
    ), "no read during the pass showed it as processing"

    # g: the same bytes again.
    before = chain.counts()
    graph_before = (nodes, edges)
    again = evidence.save("g-repeat-upload-answer", chain.upload(first))
    now = chain.counts()
    graph_now = chain.graph()
    nodes_now = {node["data"]["id"] for node in graph_now["elements"]["nodes"]}
    edges_now = {edge["data"]["id"] for edge in graph_now["elements"]["edges"]}
    due = chain.due()
    evidence.save(
        "g-repeat-effect",
        {
            "counts_before": before,
            "counts_after": now,
            "graph_nodes_same": graph_before[0] == nodes_now,
            "graph_edges_same": graph_before[1] == edges_now,
            "due_after": due,
        },
    )
    assert again["state"] == "nothing_new"
    assert again["sources_added"] == 0 and again["processing"]["requests_added"] == 0
    assert now == before
    assert graph_before == (nodes_now, edges_now)
    assert not any(row.get("agent") == "learner" for row in due)
    assert chain.progress("after repeat upload")["state"] == "complete"

    # h: a held part, then a run that fails: partial, never complete.
    held_file = export_zip(
        [fair(), _conversation(LONG, "Cupboard notes", [("human", OVERSIZED)], 7200)]
    )
    (evidence.folder / "h-export-held.zip").write_bytes(held_file)
    held = evidence.save("h-held-upload-answer", chain.upload(held_file))
    assert held["state"] == "taken_in"
    assert held["processing"]["requests_added"] == held["processing"]["held_sources"] == 1
    partial = chain.progress("after held upload")
    assert partial["state"] == "partial" and partial["held_count"] == 1
    assert partial["extracted_count"] == len(import_requests)
    assert [r["reason_code"] for r in partial["sources"] if r["state"] == "held"] == [
        "input_too_large"
    ]
    assert not any(row.get("agent") == "learner" for row in chain.due())

    # A second real pass, in a world that now holds items: recall runs, so it costs more. Its
    # numbers go on record for the caps; only the held part's state is asserted here.
    grown = [
        fair(),
        _conversation(LONG, "Cupboard notes", [("human", OVERSIZED)], 7200),
        _conversation(LATE, "Second room", [("human", LATE_SAID)], 10800),
    ]
    second = evidence.save("c-second-upload-answer", chain.upload(export_zip(grown)))
    assert second["processing"]["requests_added"] == 1
    chain.run_one_pass(runner, "second real pass")
    evidence.save("c-second-pass-run-records", chain.runs())
    during_held = chain.progress("after second real pass")
    assert during_held["state"] != "complete" and during_held["held_count"] == 1
    moved = during_held["extracted_count"] + during_held["held_nothing_count"]

    failing = export_zip(
        [*grown, _conversation(LAST, "Key cupboard", [("human", LAST_SAID)], 14400)]
    )
    (evidence.folder / "h-export-failing.zip").write_bytes(failing)
    late = evidence.save("h-failing-upload-answer", chain.upload(failing))
    assert late["processing"]["requests_added"] == 1 and late["processing"]["held_sources"] == 0
    # The same loop with no gateway behind its URL: the run cannot reach a model.
    dead = LoopSettings(**{**loop.__dict__, "gateway_url": "http://127.0.0.1:9/v1"})
    chain.run_one_pass(Runner(dead), "failing pass")
    final = chain.progress("after failing pass")
    evidence.save("h-requests", chain.requests())
    evidence.save("h-run-records", chain.runs())
    evidence.save("h-model-calls", chain.model_calls())
    assert final["state"] == "partial" and final["state"] != "complete"
    assert final["held_count"] == 1
    assert final["extracted_count"] + final["held_nothing_count"] == moved
    assert final["queued_count"] + final["retry_wait_count"] + final["failed_count"] >= 1
