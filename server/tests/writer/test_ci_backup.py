"""Pure controls for the physical drill. No Docker or database call belongs in these tests."""

import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from tests.writer.physical_backup import (
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


def test_inbound_archive_has_only_private_numeric_owned_members(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / "data").write_bytes(b"synthetic")
    value = archive_tree(tmp_path, folder, "grove-d14-123-2/restore-1/data", 7474, 7474)
    with tarfile.open(fileobj=io.BytesIO(value)) as archive:
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
        archive_tree(tmp_path, source, member, 7474, 7474)


def test_inbound_archive_rejects_even_internal_links(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"synthetic")
    (source / "link").symlink_to(source / "data")
    with pytest.raises(Refused, match="archive_link"):
        archive_tree(tmp_path, source, "safe", 7474, 7474)


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
    for name in ("preflight", "_remote_size", "stop", "copy_stopped_data", "dump", "load", "start"):
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
    drill = Drill(Identity.from_env(environment(tmp_path)), tmp_path, object())
    drill.config_dir, drill.uid, drill.gid = config_dir, 7474, 7474
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

        def communicate(self, data, timeout):
            raise Refused("drill_deadline")

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            self.returncode = -15

    child = Child()
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr("os.killpg", lambda pid, sig: killed.append((pid, sig)))
    commands = Commands(tmp_path)
    with pytest.raises(Refused, match="drill_deadline"):
        commands.run("synthetic", ["never-executed"])
    assert killed == [(child.pid, signal.SIGTERM)]
    assert commands.receipts == [{"stage": "synthetic", "exit": -15, "interrupted": True}]
