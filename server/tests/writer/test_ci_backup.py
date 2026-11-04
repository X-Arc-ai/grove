"""Pure controls for the physical drill. No Docker or database call belongs in these tests."""

import io
import json
import os
import tarfile
from pathlib import Path

import pytest
import yaml

from tests.writer.physical_backup import (
    DATA_LIMIT,
    DATA_SETTING,
    IMAGE,
    TX_SETTING,
    Drill,
    Identity,
    Refused,
    archive_tree,
    assert_effective_paths,
    config_text,
    inside,
    main,
    tree_size,
    validate_service,
)


def environment(tmp_path):
    return {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "GITHUB_JOB": "validate",
        "GITHUB_REPOSITORY": "X-Arc-ai/grove",
        "GITHUB_EVENT_NAME": "pull_request",
        "GROVE_STORE_EXCLUSIVE": "1",
        "GROVE_SLICE_ID": "ci-forget",
        "GROVE_NEO4J_URI": "bolt://127.0.0.1:57687",
        "NEO4J_AUTH": "none",
        "GROVE_DRILL_SERVICE_ID": "a" * 64,
        "GROVE_DRILL_NETWORK": "job-network",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GROVE_DRILL_HEAD": "b" * 40,
        "RUNNER_TEMP": str(tmp_path),
    }


def service(identity):
    return {
        "id": identity.service,
        "image": IMAGE,
        "image_id": "sha256:" + "c" * 64,
        "networks": {identity.network: {"NetworkID": "d" * 64}},
        "ports": {"7687/tcp": [{"HostIp": "0.0.0.0", "HostPort": "57687"}]},
        "state": {"Running": True},
    }


@pytest.mark.parametrize("name", list(environment(Path("/synthetic"))))
def test_missing_identity_field_refuses_before_any_command(tmp_path, name):
    env = environment(tmp_path)
    del env[name]
    with pytest.raises(Refused):
        Identity.from_env(env)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_EVENT_NAME", "workflow_dispatch"),
        ("GITHUB_REPOSITORY", "another/repo"),
        ("GROVE_DRILL_SERVICE_ID", "grove-dev-neo4j"),
        ("GROVE_DRILL_NETWORK", "bad;network"),
        ("GROVE_NEO4J_URI", "bolt://remote:7687"),
        ("NEO4J_AUTH", "unexpected"),
        ("GITHUB_RUN_ID", "../escape"),
        ("GITHUB_RUN_ATTEMPT", "../escape"),
        ("GROVE_DRILL_HEAD", "HEAD"),
        ("RUNNER_TEMP", "/"),
    ],
)
def test_wrong_identity_field_refuses(tmp_path, name, value):
    env = environment(tmp_path)
    env[name] = value
    with pytest.raises(Refused):
        Identity.from_env(env)


def test_valid_job_identity_has_fixed_private_container_root(tmp_path):
    identity = Identity.from_env(environment(tmp_path))
    assert identity.container_root == "/tmp/grove-d14-123-2"
    assert identity.runner_temp == tmp_path.resolve()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("id", "e" * 64, "service_identity_changed"),
        ("image", "neo4j:latest", "wrong_service_image"),
        ("networks", {"other": {}}, "wrong_service_network"),
        ("networks", {"job-network": {}, "other": {}}, "wrong_service_network"),
        ("ports", {"7687/tcp": [{"HostIp": "0.0.0.0", "HostPort": "7687"}]}, "wrong_service_port"),
    ],
)
def test_service_record_is_bound_to_actions_context(tmp_path, field, value, code):
    identity = Identity.from_env(environment(tmp_path))
    record = service(identity)
    record[field] = value
    with pytest.raises(Refused, match=code):
        validate_service(identity, record)


def test_changed_image_id_refuses(tmp_path):
    identity = Identity.from_env(environment(tmp_path))
    with pytest.raises(Refused, match="service_image_changed"):
        validate_service(identity, service(identity), "sha256:" + "e" * 64)


def test_network_context_may_name_the_network_id(tmp_path):
    env = environment(tmp_path)
    env["GROVE_DRILL_NETWORK"] = "d" * 64
    identity = Identity.from_env(env)
    record = service(identity)
    record["networks"] = {"job-generated-name": {"NetworkID": identity.network}}
    assert validate_service(identity, record) == record["image_id"]


def test_private_path_cannot_escape_or_equal_root(tmp_path):
    for path in (tmp_path, tmp_path / "../outside"):
        with pytest.raises(Refused, match="private_path_escape"):
            inside(tmp_path, path)


def test_symlink_escape_is_refused(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("synthetic")
    (private / "link").symlink_to(outside)
    with pytest.raises(Refused, match="private_path_escape"):
        tree_size(private, private / "link", 100)


def test_copy_ceiling_refuses(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / "data").write_bytes(b"1234")
    with pytest.raises(Refused, match="copy_size_limit"):
        tree_size(tmp_path, folder, 3)


@pytest.mark.parametrize("size", [0, DATA_LIMIT])
def test_remote_size_receipt_contains_counts_without_raw_output(tmp_path, monkeypatch, size):
    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, object())
    monkeypatch.setattr(drill, "docker", lambda *args: f"{size}\tprivate-source-name\n")
    assert drill._remote_size("/data", DATA_LIMIT) == size
    assert drill.source_sizes == [
        {"stage": "preflight", "kind": "data", "bytes": size, "limit": DATA_LIMIT}
    ]
    assert "private-source-name" not in json.dumps(drill.source_sizes)


@pytest.mark.parametrize(
    "output", ["", "unexpected /data", "-1 /data", "1.5 /data", "٢ /data", "9" * 40]
)
def test_remote_size_invalid_output_has_a_distinct_safe_refusal(tmp_path, monkeypatch, output):
    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, object())
    monkeypatch.setattr(drill, "docker", lambda *args: output)
    with pytest.raises(Refused, match="^invalid_copy_size_output$"):
        drill._remote_size("/data", DATA_LIMIT)
    assert drill.source_sizes == []


def test_remote_size_refusal_records_bytes_and_prevents_the_first_stop(tmp_path, monkeypatch):
    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, object())
    order = []

    class Scenario:
        def seed(self):
            order.append("seed")

    monkeypatch.setattr(drill, "preflight", lambda: None)
    monkeypatch.setattr(drill, "docker", lambda *args: f"{DATA_LIMIT + 1}\t/data\n")
    monkeypatch.setattr(drill, "stop", lambda: order.append("stop"))
    with pytest.raises(Refused, match="^copy_size_limit$"):
        drill.execute(Scenario())
    assert order == ["seed"]
    assert drill.dumps == []
    assert drill.source_sizes == [
        {"stage": "seed", "kind": "data", "bytes": DATA_LIMIT + 1, "limit": DATA_LIMIT}
    ]


def test_cli_keeps_numeric_size_evidence_on_refusal(tmp_path, monkeypatch, capsys):
    for key, value in environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("tests.writer.physical_scenario.make_scenario", lambda root: object())
    monkeypatch.setattr(Drill, "docker", lambda *args: f"{DATA_LIMIT + 1}\tprivate-source-name\n")

    def refused(self, scenario):
        self.stage = "seed"
        self._remote_size("/data", DATA_LIMIT)

    monkeypatch.setattr(Drill, "execute", refused)
    assert main() == 1
    output = capsys.readouterr().out
    receipt = json.loads(output)
    assert receipt["refusal"] == "copy_size_limit"
    assert receipt["source_sizes"] == [
        {"stage": "seed", "kind": "data", "bytes": DATA_LIMIT + 1, "limit": DATA_LIMIT}
    ]
    assert receipt["private_files_removed"] is True
    assert receipt["physical_acceptance"] is False
    assert "private-source-name" not in output


def test_inbound_archive_has_only_private_numeric_owned_members(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / "data").write_bytes(b"synthetic")
    with archive_tree(tmp_path, folder, "grove-d14-123-2/restore-1/data", 7474, 7474) as path:
        with tarfile.open(path) as archive:
            members = archive.getmembers()
    assert len(members) == 4
    assert all(m.uid == 7474 and m.gid == 7474 for m in members)
    assert all(m.uname == m.gname == "" for m in members)
    assert all(m.isfile() or m.isdir() for m in members)
    assert all(m.name.startswith("grove-d14-123-2") for m in members)
    assert members[0].mode == 0o700
    assert members[-1].mode == 0o600
    assert all(m.mode == 0o700 for m in members[:-1])


@pytest.mark.parametrize("member", ["/absolute", "../outside", "a/../outside", "."])
def test_inbound_archive_rejects_outside_member_paths(tmp_path, member):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(Refused, match="archive_path_escape"):
        with archive_tree(tmp_path, source, member, 7474, 7474):
            pytest.fail("invalid path accepted")


def test_inbound_archive_rejects_even_internal_links(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"synthetic")
    (source / "link").symlink_to(source / "data")
    with pytest.raises(Refused, match="archive_link"):
        with archive_tree(tmp_path, source, "safe", 7474, 7474):
            pytest.fail("symlink accepted")


@pytest.mark.parametrize("setting", [DATA_SETTING, TX_SETTING])
def test_ignored_configuration_is_not_restore_acceptance(setting):
    found = {DATA_SETTING: "/tmp/new/data", TX_SETTING: "/tmp/new/data/transactions"}
    found[setting] = "/data" if setting == DATA_SETTING else "/data/transactions"
    with pytest.raises(Refused, match="effective_.*_path_mismatch"):
        assert_effective_paths(found, "/tmp/new/data", "/tmp/new/data/transactions")


def test_both_effective_paths_must_be_present():
    with pytest.raises(Refused, match="effective_transaction_path_mismatch"):
        assert_effective_paths(
            {DATA_SETTING: "/tmp/new/data"}, "/tmp/new/data", "/tmp/new/data/transactions"
        )


def test_matching_effective_paths_pass():
    assert_effective_paths(
        {DATA_SETTING: "/tmp/new/data", TX_SETTING: "/tmp/new/data/transactions"},
        "/tmp/new/data",
        "/tmp/new/data/transactions",
    )


def test_start_refuses_ignored_path_before_releasing_scenario(tmp_path, monkeypatch):
    identity = Identity.from_env(environment(tmp_path))

    class FakeCommands:
        deadline = float("inf")

    drill = Drill(identity, tmp_path, FakeCommands())
    states = iter(
        [
            {"state": {"Running": False}},
            {"state": {"Running": True, "Health": {"Status": "healthy"}}},
        ]
    )
    monkeypatch.setattr(drill, "inspect", lambda: next(states))
    monkeypatch.setattr(drill, "docker", lambda *args, **kwargs: "")

    def ignored(data, tx):
        assert_effective_paths({DATA_SETTING: "/data", TX_SETTING: "/data/transactions"}, data, tx)

    monkeypatch.setattr(drill, "check_paths", ignored)
    with pytest.raises(Refused, match="effective_data_path_mismatch"):
        drill.start("/tmp/new/data", "/tmp/new/data/transactions")
    assert drill.current_data == "/data"


def test_admin_config_drops_server_absolute_paths(tmp_path):
    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, object())
    (drill.runtime / "conf").mkdir(parents=True)
    drill.original_config = (
        "server.logs.config=/outside/server.xml\nserver.directories.plugins=/outside/plugins\n"
    )
    drill._admin_config(tmp_path / "copy-data")
    config = (drill.runtime / "conf/neo4j.conf").read_text()
    assert "/outside/" not in config
    for line in config.splitlines():
        key, value = line.split("=", 1)
        if key.startswith("server.directories."):
            assert Path(value).is_relative_to(tmp_path)


def test_preflight_refuses_wrong_checkout_before_docker(tmp_path):
    class WrongCheckout:
        def run(self, tag, args):
            assert args == ["git", "rev-parse", "HEAD"]
            return "0" * 40

    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, WrongCheckout())
    with pytest.raises(Refused, match="checkout_head_mismatch"):
        drill.preflight()


def test_forced_kill_cannot_count_as_clean_stop(tmp_path, monkeypatch):
    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, object())
    states = iter(
        [
            {"state": {"Running": True}},
            {"state": {"Running": False, "Status": "exited", "OOMKilled": False, "ExitCode": 137}},
        ]
    )
    monkeypatch.setattr(drill, "inspect", lambda: next(states))
    monkeypatch.setattr(drill, "docker", lambda *args, **kwargs: "")
    monkeypatch.setattr(drill, "service_checkpoint", lambda: None)
    with pytest.raises(Refused, match="unclean_service_stop"):
        drill.stop()


def test_config_replaces_duplicate_directory_settings_but_keeps_other_settings():
    original = f"{DATA_SETTING}=old\n{DATA_SETTING}=other\nserver.memory.heap.max_size=512m\n"
    result = config_text(
        original, {DATA_SETTING: "/tmp/new/data", TX_SETTING: "/tmp/new/data/transactions"}
    )
    assert result.count(DATA_SETTING + "=") == 1
    assert "server.memory.heap.max_size=512m" in result
    assert "=old" not in result and "=other" not in result


def test_config_rejects_newline_in_path():
    with pytest.raises(Refused, match="invalid_config_value"):
        config_text("", {DATA_SETTING: "/tmp/new\nextra=1"})


def test_cli_refuses_local_use_without_a_docker_call(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("a subprocess must not run")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    assert main() == 1
    assert json.loads(capsys.readouterr().out)["refusal"] == "not_actions"


def test_orchestration_checks_loaded_data_before_acceptance(tmp_path, monkeypatch):
    identity = Identity.from_env(environment(tmp_path))
    order = []

    class FakeScenario:
        def __getattr__(self, name):
            def call(*args):
                order.append((name, *args))
                return {} if name == "summary" else None

            return call

    drill = Drill(identity, tmp_path, object())
    for name in (
        "preflight",
        "_remote_size",
        "stop",
        "copy_stopped_data",
        "dump",
        "load",
        "start",
        "service_checkpoint",
    ):
        monkeypatch.setattr(drill, name, lambda *args, name=name: order.append((name,)))
    monkeypatch.setattr(
        drill, "select_data", lambda local, number: (f"data-{number}", f"tx-{number}")
    )
    assert drill.execute(FakeScenario()) == {}
    assert order.index(("assert_old_dump_contains_canary",)) < order.index(
        ("restore_loaded", "old_dump")
    )
    assert order.index(("restore_loaded", "old_dump")) < order.index(("assert_clean", "old_dump"))
    assert order.index(("reconstruct_clean",)) < order.index(
        ("assert_clean", "clean_reconstruction")
    )
    assert order.index(("restore_loaded", "next_dump")) < order.index(("assert_clean", "next_dump"))


def test_workflow_uses_only_the_existing_service_and_normal_pr_event():
    workflow = Path(__file__).resolve().parents[3] / ".github/workflows/validate.yml"
    data = yaml.safe_load(workflow.read_text())
    job = data["jobs"]["validate"]
    assert set(job["services"]) == {"neo4j"}
    assert job["timeout-minutes"] == 30
    step = job["steps"][-1]
    assert step["name"] == "D14 physical backup drill"
    assert step["if"] == "github.event_name == 'pull_request'"
    assert step["timeout-minutes"] == 10
    assert step["env"]["GROVE_DRILL_SERVICE_ID"] == "${{ job.services.neo4j.id }}"
    assert step["env"]["GROVE_DRILL_NETWORK"] == "${{ job.services.neo4j.network }}"
    assert step["env"]["GROVE_STORE_EXCLUSIVE"] == "1"
    assert step["run"] == "uv run python -m tests.writer.physical_backup"
    assert not step.get("continue-on-error", False)


def test_private_manifest_is_exclusive_and_owner_only(tmp_path):
    from tests.writer.physical_scenario import private_manifest

    private_manifest(tmp_path, "baseline.json", {"id": "synthetic"})
    path = tmp_path / "baseline.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {"id": "synthetic"}
    with pytest.raises(FileExistsError):
        private_manifest(tmp_path, "baseline.json", {})
    with pytest.raises(Refused, match="invalid_manifest_name"):
        private_manifest(tmp_path, "../outside.json", {})


def test_work_deadline_covers_python_and_restores_signal_handler(monkeypatch):
    import signal

    from tests.writer.physical_backup import bounded

    handlers, timers = [], []
    previous = object()
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.append(handler) or previous)
    monkeypatch.setattr(signal, "setitimer", lambda *args: timers.append(args) or (0, 0))
    with pytest.raises(Refused, match="drill_deadline"), bounded(480):
        handlers[0](signal.SIGALRM, None)
    assert handlers[-1] is previous
    assert timers == [(signal.ITIMER_REAL, 480), (signal.ITIMER_REAL, 0, 0)]


@pytest.mark.parametrize("config_dir", ["/var/lib/neo4j/conf", "/conf"])
def test_config_copy_uses_canonical_directory_parent(tmp_path, monkeypatch, config_dir):
    from tests.writer.physical_backup import Capacity

    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, object())
    drill.config_dir, drill.uid, drill.gid = config_dir, 7474, 7474
    drill.last_service_capacity = {"service_tmp": Capacity(2**40, 2**24)}
    monkeypatch.setattr(drill, "runner_checkpoint", lambda: None)
    monkeypatch.setattr(drill, "monitor", lambda: None)
    copied = []
    monkeypatch.setattr(drill, "inspect", lambda: {"state": {"Running": False}})
    monkeypatch.setattr(drill, "docker", lambda *args, **kwargs: copied.append(args))
    local = tmp_path / "loaded"
    local.mkdir()
    drill.select_data(local, 1)
    assert copied[-1][-1] == drill.identity.service + ":" + str(Path(config_dir).parent)


def test_total_deadline_reaps_its_child_group(tmp_path, monkeypatch):
    import signal

    from tests.writer.physical_backup import Commands

    killed = []

    class Child:
        pid = 12345
        returncode = None
        waited = False

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            if not self.waited:
                self.waited = True
                raise Refused("drill_deadline")
            self.returncode = -15

    child = Child()
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr("os.killpg", lambda pid, sig: killed.append((pid, sig)))
    commands = Commands(tmp_path)
    with pytest.raises(Refused, match="drill_deadline"):
        commands.run("synthetic", ["never-executed"])
    assert killed == [(child.pid, signal.SIGTERM)]
    assert commands.receipts == [{"stage": "synthetic", "exit": -15, "interrupted": True}]


def test_amendment_fixed_budgets_and_retained_peak():
    from tests.writer import physical_backup as backup

    assert backup.DATA_LIMIT == 1073741824
    assert backup.RUNTIME_LIMIT == 1073741824
    assert backup.OUTPUT_LIMIT == 1048576
    assert backup.WORK_SECONDS == 476


@pytest.mark.parametrize("size", [832563058, 1073741823, 1073741824])
def test_amended_remote_data_boundary_accepts_measured_input(tmp_path, monkeypatch, size):
    from tests.writer import physical_backup as backup

    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, object())
    monkeypatch.setattr(drill, "docker", lambda *args: f"{size}\t/data\n")
    assert drill._remote_size("/data", backup.DATA_LIMIT) == size


def test_inbound_archive_rejects_hardlink_before_transport(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"synthetic")
    os.link(source / "data", source / "second")
    with pytest.raises(Refused, match="archive_hardlink"):
        with archive_tree(tmp_path, source, "safe", 7474, 7474):
            pytest.fail("hardlink accepted")


def test_manifest_byte_ceiling_refuses_before_excess_write(tmp_path, monkeypatch):
    from tests.writer import physical_scenario as scenario

    monkeypatch.setattr(scenario, "MANIFEST_LIMIT", 16, raising=False)
    with pytest.raises(Refused, match="manifest_size_limit"):
        scenario.private_manifest(tmp_path, "baseline.json", {"text": "x" * 17})
    path = tmp_path / "baseline.json"
    assert not path.exists() or path.stat().st_size <= 16


def test_tree_entry_limit_refuses_small_files(tmp_path, monkeypatch):
    from tests.writer import physical_backup as backup

    source = tmp_path / "source"
    source.mkdir()
    for n in range(4):
        (source / str(n)).write_bytes(b"")
    monkeypatch.setattr(backup, "ENTRY_LIMIT", 4, raising=False)
    with pytest.raises(Refused, match="entry_limit"):
        tree_size(tmp_path, source, DATA_LIMIT)


def test_peak_and_capacity_constants_remain_fixed():
    from tests.writer import physical_backup as b

    assert b.ARCHIVE_LIMIT == b.DATA_LIMIT + 16777216
    assert b.AUX_LIMIT == 268435456
    assert b.AUX_ENTRY_LIMIT == 8192
    assert b.ENTRY_LIMIT == 65536
    assert b.INITIAL_CAPACITY == 17179869184
    assert b.INITIAL_INODES == 1048576
    assert b.FREE_FLOOR == 1073741824
    assert b.INODE_FLOOR == 65536
    retained_peak = b.RUNTIME_LIMIT + 10 * b.DATA_LIMIT + b.ARCHIVE_LIMIT + 2 * b.AUX_LIMIT
    assert retained_peak == 13438550016
    assert retained_peak + 2147483648 + b.FREE_FLOOR <= b.INITIAL_CAPACITY


@pytest.mark.parametrize("initial", [True, False])
def test_capacity_exact_threshold_passes_without_summing_surfaces(initial):
    from tests.writer import physical_backup as b

    charged = b.Usage(1234, 321)
    needed = b.capacity_requirement(charged, initial=initial)
    assert needed.bytes == b.INITIAL_CAPACITY - (0 if initial else charged.bytes)
    assert needed.inodes == b.INITIAL_INODES - (0 if initial else charged.entries)
    assert (
        b.check_capacity({"runner": needed, "service_tmp": needed}, charged, initial=initial)
        == needed
    )


@pytest.mark.parametrize("field", ["bytes", "inodes"])
@pytest.mark.parametrize("initial", [True, False])
def test_capacity_one_below_refuses_each_surface(field, initial):
    from tests.writer import physical_backup as b

    charged = b.Usage(16384, 128)
    needed = b.capacity_requirement(charged, initial=initial)
    low = b.Capacity(needed.bytes - (field == "bytes"), needed.inodes - (field == "inodes"))
    high = b.Capacity(needed.bytes * 2, needed.inodes * 2)
    with pytest.raises(Refused):
        b.check_capacity({"runner": high, "service_tmp": low}, charged, initial=initial)


def test_capacity_retains_byte_and_inode_floors():
    from tests.writer import physical_backup as b

    needed = b.capacity_requirement(b.Usage(b.INITIAL_CAPACITY, b.INITIAL_INODES))
    assert needed == b.Capacity(b.FREE_FLOOR, b.INODE_FLOOR)


@pytest.mark.parametrize("text", ["", "-1 100", "100 -1", "1.5 2", "unknown", "٢ 100", "1 2 3 4"])
def test_malformed_capacity_is_never_guessed(text):
    from tests.writer import physical_backup as b

    with pytest.raises(Refused):
        b.parse_capacity(text)


def test_no_capacity_observation_refuses():
    from tests.writer import physical_backup as b

    with pytest.raises(Refused):
        b.check_capacity({}, b.Usage(0, 0), initial=True)


def test_tar_limit_refuses_before_the_excess_write():
    from tests.writer import physical_backup as b

    target = io.BytesIO()
    writer = b.BoundedWriter(target, limit=5)
    writer.write(b"12345")
    with pytest.raises(Refused, match="archive_size_limit"):
        writer.write(b"6")
    assert target.getvalue() == b"12345"


@pytest.mark.parametrize("size,passes", [(3, True), (4, True), (5, False)])
def test_local_tree_byte_boundary_without_large_allocation(tmp_path, size, passes):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"x" * size)
    if passes:
        assert tree_size(tmp_path, source, 4) == size
    else:
        with pytest.raises(Refused, match="copy_size_limit"):
            tree_size(tmp_path, source, 4)


def test_runtime_budget_is_aggregate_across_components(tmp_path, monkeypatch):
    from tests.writer import physical_backup as b

    monkeypatch.setattr(b, "RUNTIME_LIMIT", 6)
    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, object())
    drill.runtime.mkdir()
    for name in ("bin", "lib", "java"):
        (drill.runtime / name).mkdir()
        (drill.runtime / name / "part").write_bytes(b"xx")
    assert drill.runner_usage().bytes == 6
    (drill.runtime / "conf").write_bytes(b"x")
    with pytest.raises(Refused, match="copy_size_limit"):
        drill.runner_usage()


def test_runner_auxiliary_files_and_entries_cannot_escape_accounting(tmp_path, monkeypatch):
    from tests.writer import physical_backup as b

    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, object())
    monkeypatch.setattr(b, "AUX_LIMIT", 4)
    (tmp_path / "baseline.json").write_bytes(b"1234")
    assert drill.runner_usage().bytes == 4
    (tmp_path / "unknown-output").write_bytes(b"5")
    with pytest.raises(Refused):
        drill.runner_usage()
    (tmp_path / "unknown-output").unlink()
    monkeypatch.setattr(b, "AUX_ENTRY_LIMIT", 1)
    (tmp_path / "second").write_bytes(b"")
    with pytest.raises(Refused):
        drill.runner_usage()


def test_command_output_excess_stops_its_owned_child(tmp_path, monkeypatch):
    import sys

    from tests.writer import physical_backup as b

    monkeypatch.setattr(b, "OUTPUT_LIMIT", 16)
    commands = b.Commands(tmp_path)
    with pytest.raises(Refused, match="command_output_limit"):
        commands.run(
            "bounded_stdout",
            [
                sys.executable,
                "-c",
                "import sys,time;sys.stdout.write('x'*17);sys.stdout.flush();time.sleep(10)",
            ],
            timeout=2,
        )
    assert commands.receipts[-1]["exit"] is not None


def test_command_stderr_excess_is_independently_bounded(tmp_path, monkeypatch):
    import sys

    from tests.writer import physical_backup as b

    monkeypatch.setattr(b, "OUTPUT_LIMIT", 16)
    with pytest.raises(Refused, match="command_output_limit"):
        b.Commands(tmp_path).run(
            "bounded_stderr",
            [
                sys.executable,
                "-c",
                "import sys,time;sys.stderr.write('x'*17);sys.stderr.flush();time.sleep(10)",
            ],
            timeout=2,
        )


def test_command_monitor_rejects_active_output_and_reaps_child(tmp_path):
    import sys

    from tests.writer import physical_backup as b

    output = tmp_path / "active-output"

    def monitor():
        if output.exists():
            raise Refused("synthetic_active_limit")

    commands = b.Commands(tmp_path)
    with pytest.raises(Refused, match="synthetic_active_limit"):
        commands.run(
            "active_tree",
            [
                sys.executable,
                "-c",
                "import pathlib,sys,time;"
                "pathlib.Path(sys.argv[1]).write_bytes(b'x');time.sleep(10)",
                str(output),
            ],
            timeout=2,
            monitor=monitor,
        )
    assert commands.receipts[-1]["exit"] is not None


def test_private_cleanup_failure_prevents_acceptance(tmp_path, monkeypatch, capsys):
    from tests.writer import physical_backup as b

    for key, value in environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("tests.writer.physical_scenario.make_scenario", lambda root: object())
    monkeypatch.setattr(Drill, "execute", lambda self, scenario: {})

    def fail_cleanup(root):
        raise OSError("synthetic refusal")

    monkeypatch.setattr(b.shutil, "rmtree", fail_cleanup)
    assert main() == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["cleanup_error"] == "private_cleanup_failed"
    assert receipt["physical_acceptance"] is False
    assert receipt["private_files_removed"] is False


@pytest.mark.parametrize("surface", ["stdout", "stderr", "tree"])
def test_child_exit_race_still_checks_final_output(tmp_path, monkeypatch, surface):
    from tests.writer import physical_backup as b

    monkeypatch.setattr(b, "OUTPUT_LIMIT", 16)
    active = tmp_path / "active-tree"

    class Child:
        pid = 12345
        returncode = None

        def __init__(self, **kwargs):
            self.output = kwargs.get(surface)

        def poll(self):
            if self.returncode is None:
                if surface == "tree":
                    active.write_bytes(b"x")
                else:
                    self.output.write(b"x" * 17)
                    self.output.flush()
                self.returncode = 0
            return self.returncode

    def monitor():
        if active.exists():
            raise Refused("active_tree_limit")

    monkeypatch.setattr(b.subprocess, "Popen", lambda args, **kwargs: Child(**kwargs))
    expected = "active_tree_limit" if surface == "tree" else "command_output_limit"
    with pytest.raises(Refused, match=expected):
        b.Commands(tmp_path).run("exit_race", ["synthetic"], monitor=monitor)


def test_archive_removed_capacity_is_recomputed(tmp_path):
    from tests.writer import physical_backup as b

    source = tmp_path / "baseline-data"
    source.mkdir()
    (source / "data").write_bytes(b"synthetic")
    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, object())
    before = drill.runner_usage()
    with archive_tree(tmp_path, source, "safe", 7474, 7474) as path:
        during = drill.runner_usage()
        assert during.bytes == before.bytes + path.stat().st_size
        assert during.entries == before.entries + 1
        assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises((FileExistsError, Refused)):
            with archive_tree(tmp_path, source, "safe", 7474, 7474):
                pytest.fail("two archives accepted")
        assert path.exists()
    assert not path.exists()
    assert drill.runner_usage() == before
    assert b.capacity_requirement(before).bytes > b.capacity_requirement(during).bytes


@pytest.mark.parametrize("mode", ["success", "error", "timeout", "cancel"])
def test_file_stdin_and_archive_close_on_every_command_exit(tmp_path, monkeypatch, mode):
    import sys

    from tests.writer import physical_backup as b

    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"synthetic")
    captured = []
    original = b.subprocess.Popen

    def child(*args, **kwargs):
        captured.append(kwargs["stdin"])
        assert not isinstance(kwargs["stdin"], (bytes, int))
        return original(*args, **kwargs)

    def no_whole_file(*args, **kwargs):
        raise AssertionError("whole file read forbidden")

    def monitor():
        if captured and mode == "cancel":
            raise KeyboardInterrupt()

    monkeypatch.setattr(b.subprocess, "Popen", child)
    monkeypatch.setattr(Path, "read_bytes", no_whole_file)
    command = {
        "success": "import sys; sum(len(p) for p in iter(lambda:sys.stdin.buffer.read(4096),b''))",
        "error": "raise SystemExit(3)",
        "timeout": "import time;time.sleep(10)",
        "cancel": "import time;time.sleep(10)",
    }[mode]
    path = tmp_path / b.ARCHIVE_NAME

    def run():
        with archive_tree(tmp_path, source, "safe", 7474, 7474) as archive:
            assert archive == path
            b.Commands(tmp_path).run(
                "file_input",
                [sys.executable, "-c", command],
                file_stdin=archive,
                timeout=0.25,
                monitor=monitor,
            )

    if mode == "success":
        run()
    elif mode == "cancel":
        with pytest.raises(KeyboardInterrupt):
            run()
    else:
        with pytest.raises(
            Refused, match="command_failed" if mode == "error" else "command_timeout"
        ):
            run()
    assert captured and all(handle.closed for handle in captured)
    assert not path.exists()


def test_archive_payload_bound_is_separate_from_tar_bound(tmp_path, monkeypatch):
    from tests.writer import physical_backup as b

    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"12345")
    monkeypatch.setattr(b, "DATA_LIMIT", 4)
    with pytest.raises(Refused, match="copy_size_limit"):
        with archive_tree(tmp_path, source, "safe", 7474, 7474):
            pytest.fail("oversized payload accepted")
    assert not (tmp_path / b.ARCHIVE_NAME).exists()


@pytest.mark.parametrize("operation", ["copy", "dump", "load"])
@pytest.mark.parametrize("size", [3, 4, 5])
def test_data_operation_enforces_complete_tree_boundary(tmp_path, monkeypatch, operation, size):
    from tests.writer import physical_backup as b

    monkeypatch.setattr(b, "DATA_LIMIT", 4)
    destination = (
        tmp_path
        / {"copy": "baseline-data", "dump": "old-dump", "load": "restore-1-data"}[operation]
    )

    class Commands:
        def run(self, *args, **kwargs):
            destination.mkdir(exist_ok=True)
            (destination / ("neo4j.dump" if operation == "dump" else "data")).write_bytes(
                b"x" * size
            )
            return ""

    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, Commands())
    monkeypatch.setattr(drill, "runner_checkpoint", lambda: None)
    monkeypatch.setattr(drill, "inspect", lambda: {"state": {"Running": False}})
    monkeypatch.setattr(drill, "_admin_config", lambda path: path.mkdir(exist_ok=True))

    def action():
        if operation == "copy":
            drill.copy_stopped_data(destination)
        elif operation == "dump":
            drill.dump(tmp_path / "baseline-data", destination)
        else:
            drill.load(tmp_path / "old-dump", destination)

    if size <= 4:
        action()
    else:
        with pytest.raises(Refused, match="copy_size_limit"):
            action()


def test_capacity_loss_prevents_the_next_stop(tmp_path, monkeypatch):
    from tests.writer import physical_backup as b

    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, object())
    commands = []
    monkeypatch.setattr(drill, "inspect", lambda: {"state": {"Running": True}})
    monkeypatch.setattr(drill, "docker", lambda *args, **kwargs: commands.append(args))

    def lost():
        b.check_capacity(
            {"service_data": b.Capacity(b.INITIAL_CAPACITY - 1, b.INITIAL_INODES)}, b.Usage()
        )

    monkeypatch.setattr(drill, "service_checkpoint", lost)
    with pytest.raises(Refused, match="capacity_bytes"):
        drill.stop()
    assert commands == []
