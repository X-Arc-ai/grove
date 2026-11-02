"""The box scripts against fake commands: what they run, in which order, and what they refuse."""

import json
import os

from grove import clock
from tests.deploy.conftest import BASE, FP, HOST, OTHER_SHA, SHA

# ---- deploy.sh and apply.sh -------------------------------------------------------------------


def test_deploy_refuses_a_short_sha_before_any_git_call(box):
    done = box.run("deploy.sh", SHA[:12])
    assert done.returncode != 0
    assert "full 40-character sha" in done.stderr
    assert not [call for call in box.calls() if call.startswith("git ")]


def test_deploy_refuses_a_sha_that_is_not_on_the_branch(box):
    done = box.run("deploy.sh", SHA, FAKE_ANCESTOR_STATUS="1")
    assert done.returncode != 0
    assert f"{SHA} is not on origin/develop" in done.stderr
    assert not [call for call in box.calls() if call.startswith("git checkout")]
    assert not [call for call in box.calls() if call.startswith("docker")]


def test_deploy_refuses_a_checkout_with_local_changes(box):
    done = box.run("deploy.sh", SHA, FAKE_DIRTY=" M deploy/compose.yml\n")
    assert done.returncode != 0
    assert "local changes" in done.stderr
    assert not [call for call in box.calls() if call.startswith("git checkout")]


def test_deploy_waits_for_the_box_lock_and_stops_when_it_is_held(box):
    done = box.run("deploy.sh", SHA, FAKE_FLOCK_STATUS="1")
    assert done.returncode != 0
    assert "holds" in done.stderr
    assert box.calls()[0].startswith("flock -w 600 9")
    assert not [call for call in box.calls() if call.startswith("git ")]


def test_deploy_checks_the_named_branch(box):
    done = box.run("deploy.sh", SHA, "--ref", "feat/deploy-a-20261102")
    assert done.returncode == 0, done.stderr
    fetch = box.calls()[box.index("git fetch")]
    assert "+refs/heads/feat/deploy-a-20261102:refs/remotes/origin/feat/deploy-a-20261102" in fetch
    assert f"git merge-base --is-ancestor {SHA} origin/feat/deploy-a-20261102" in box.calls()


def test_a_deploy_runs_its_steps_in_order(box):
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr

    steps = [
        "flock -w 600 9",
        "git fetch",
        f"git merge-base --is-ancestor {SHA} origin/develop",
        "git status --porcelain",
        f"git checkout --quiet --detach {SHA}",
        "git rev-parse HEAD",
        "certbot",  # never: the certificate exists, so this step is checked below
    ]
    position = 0
    for step in steps[:-1]:
        position = box.index(step, position)
    assert not [call for call in box.calls() if call.startswith("certbot")]

    build = box.index("compose", position)
    assert "build api runner" in box.calls()[build]
    neo4j_up = box.index("up --detach --wait neo4j", build)
    migrate = box.index("python -m grove.writer.migrate", neo4j_up)
    services_up = box.index("up --detach --wait --remove-orphans api runner", migrate)
    smoke = box.index(f"{BASE}/healthz", services_up)
    box.index("docker image prune", smoke)

    assert (box.state / "deployed.sha").read_text().strip() == SHA
    assert f"deploy {SHA} ref=develop previous=none: served, smoke ok" in box.log_text("deploy.log")
    assert "ok healthz build=" + SHA in done.stdout
    assert "ok /mcp 401" in done.stdout


def test_every_compose_call_names_the_project_the_file_and_the_build(box):
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    compose_calls = [call for call in box.calls() if call.startswith("docker compose")]
    assert compose_calls
    for call in compose_calls:
        assert f"--project-name grove --file {box.src}/deploy/compose.yml" in call


def test_a_deploy_whose_smoke_fails_says_so_and_fails(box):
    done = box.run("deploy.sh", SHA, FAKE_SERVED_BUILD=OTHER_SHA)
    assert done.returncode != 0
    assert "did not answer ok for build" in done.stderr
    assert f"deploy {SHA} ref=develop previous=none: served, smoke FAILED" in box.log_text(
        "deploy.log"
    )


def test_the_smoke_fails_when_the_metadata_names_another_resource(box):
    done = box.run("deploy.sh", SHA, FAKE_RESOURCE="https://elsewhere.test/mcp")
    assert done.returncode != 0
    assert "does not name" in done.stderr


def test_a_deploy_records_the_sha_it_replaces(box):
    box.deployed(OTHER_SHA)
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    assert f"previous={OTHER_SHA}: served, smoke ok" in box.log_text("deploy.log")


def test_apply_refuses_to_run_without_the_lock(box):
    done = box.run("apply.sh", SHA, "develop")
    assert done.returncode != 0
    assert "takes the box lock" in done.stderr
    assert not box.calls()


def test_apply_refuses_when_a_secret_file_is_missing(box):
    (box.etc / "google-oauth.json").unlink()
    done = box.run("deploy.sh", SHA)
    assert done.returncode != 0
    assert "google-oauth.json" in done.stderr
    assert not [call for call in box.calls() if call.startswith("docker")]


def test_apply_refuses_a_secret_file_that_others_can_read(box):
    (box.etc / "neo4j.env").chmod(0o644)
    done = box.run("deploy.sh", SHA)
    assert done.returncode != 0
    assert "readable by group or others" in done.stderr
    assert not [call for call in box.calls() if call.startswith("docker")]


def test_apply_gives_the_oauth_file_to_the_api_user_alone(box):
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    assert f"chown 10001:10001 {box.etc}/google-oauth.json" in box.calls()
    assert oct((box.etc / "google-oauth.json").stat().st_mode & 0o777) == "0o400"


# ---- install-host.sh --------------------------------------------------------------------------


def test_install_host_renders_both_sites_for_the_configured_host(box):
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    for name in ("grove-http.conf", "grove-https.conf"):
        text = (box.nginx / "sites-available" / name).read_text()
        assert f"server_name {HOST};" in text
        assert "__GROVE_HOST__" not in text
        assert (box.nginx / "sites-enabled" / name).resolve() == (
            box.nginx / "sites-available" / name
        )
    https = (box.nginx / "sites-available" / "grove-https.conf").read_text()
    assert f"/etc/letsencrypt/live/{HOST}/fullchain.pem" in https
    timer = box.root / "systemd" / "grove-backup.timer"
    assert timer.is_file()
    assert "systemctl enable --now grove-backup.timer" in box.calls()
    assert box.index("nginx -t") < box.index("systemctl reload nginx")


def test_install_host_asks_for_a_certificate_only_when_there_is_none(box):
    (box.root / "letsencrypt" / "live" / HOST / "fullchain.pem").unlink()
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    certbot = box.index("certbot certonly --webroot")
    assert f"--domain {HOST}" in box.calls()[certbot]
    # The challenge needs the port-80 site live first, and no TLS site that names a missing cert.
    assert box.index("systemctl reload nginx") < certbot


def test_bootstrap_makes_the_runner_user_and_generated_files_and_keeps_existing_ones(box):
    for name in ("neo4j.env", "api.env", "runner.env"):
        (box.etc / name).unlink()
    done = box.run(
        "bootstrap.sh",
        "--base-url",
        BASE,
        FAKE_GETENT_STATUS="2",
        GROVE_RUNNER_HOME=str(box.root / "runner-home"),
    )
    assert done.returncode == 0, done.stderr
    assert any(call.startswith("useradd --system --uid 10002") for call in box.calls())
    auth = (box.etc / "neo4j.env").read_text()
    assert (
        auth.startswith("NEO4J_AUTH=neo4j/") and len(auth.strip()) > len("NEO4J_AUTH=neo4j/") + 40
    )
    assert auth.split("/", 1)[1] not in done.stdout + done.stderr
    for name in ("neo4j.env", "api.env", "runner.env"):
        assert oct((box.etc / name).stat().st_mode & 0o777) == "0o600", name
    assert (box.etc / "api.env").read_text() == f"GROVE_PUBLIC_BASE_URL={BASE}\n"

    again = box.run("bootstrap.sh", "--base-url", BASE, GROVE_RUNNER_HOME=str(box.root / "r"))
    assert again.returncode == 0, again.stderr
    assert (box.etc / "neo4j.env").read_text() == auth

    other = box.run("bootstrap.sh", "--base-url", "https://other.test")
    assert other.returncode != 0
    assert "another base URL" in other.stderr


# ---- backup.sh --------------------------------------------------------------------------------


def _backup_files(box) -> list[str]:
    return sorted(path.name for path in box.backups.iterdir() if not path.name.startswith("."))


def test_a_backup_stops_the_writers_fingerprints_dumps_and_starts_them_again(box):
    box.deployed()
    done = box.run("backup.sh")
    assert done.returncode == 0, done.stderr

    order = [
        "flock -w 1800 9",
        "compose --project-name grove",  # the first compose call, below
        "stop runner api",
        "python -m grove.store.dump_fingerprint",
        "stop neo4j",
        "neo4j-admin database dump neo4j --to-path=/backups",
        "start neo4j",
        f"python -m grove.store.dump_fingerprint --expect {FP}",
        "start api runner",
        "docker network create --internal grove-restorecheck-",
        "neo4j-admin database load neo4j --from-stdin",
        "--env NEO4J_AUTH=none",
        f"python -m grove.store.dump_fingerprint --expect {FP}",
        "docker rm --force grove-restorecheck-",
        "docker volume rm grove-restorecheck-",
        "docker network rm grove-restorecheck-",
    ]
    position = 0
    for step in order:
        position = box.index(step, position)

    files = _backup_files(box)
    dumps = [name for name in files if name.endswith(".dump")]
    assert len(dumps) == 1
    stem = dumps[0][: -len(".dump")]
    assert files == [f"{stem}.dump", f"{stem}.dump.sha256", f"{stem}.fingerprint.json"]
    for name in files:
        assert oct((box.backups / name).stat().st_mode & 0o777) == "0o600", name
    assert json.loads((box.backups / f"{stem}.fingerprint.json").read_text())["fingerprint"] == FP
    log = box.log_text("backup.log")
    assert f"backup {stem}: dumped, fingerprint {FP}" in log
    assert f"restore check {stem}.dump: fingerprint {FP} matches" in log
    assert not [path for path in box.backups.iterdir() if path.name.startswith(".work-")]


def test_a_failed_dump_still_starts_the_store_and_its_writers(box):
    box.deployed()
    done = box.run("backup.sh", FAKE_DOCKER_FAIL="neo4j-admin database dump")
    assert done.returncode != 0
    dump = box.index("neo4j-admin database dump")
    start_store = box.index("start neo4j", dump)
    box.index("start api runner", start_store)
    assert not [name for name in _backup_files(box) if name.endswith(".dump")]
    assert "FAILED" in box.log_text("backup.log")
    assert not [path for path in box.backups.iterdir() if path.name.startswith(".work-")]


def test_a_store_that_changed_across_the_dump_fails_the_backup(box):
    box.deployed()
    done = box.run("backup.sh", FAKE_FP_AFTER_FIRST="b" * 64)
    assert done.returncode != 0
    assert "the store changed across the dump" in done.stderr
    box.index("start api runner")
    assert not [name for name in _backup_files(box) if name.endswith(".dump")]


def test_a_backup_never_starts_while_the_lock_is_held(box):
    box.deployed()
    done = box.run("backup.sh", FAKE_FLOCK_STATUS="1")
    assert done.returncode != 0
    assert not [call for call in box.calls() if call.startswith("docker")]


def test_a_backup_keeps_thirty_days_of_dumps(box):
    box.deployed()
    now = clock.now().timestamp()
    ages = {"grove-20260901T033000Z": 40, "grove-20261020T033000Z": 12}
    for stem, days in ages.items():
        for suffix in (".dump", ".dump.sha256", ".fingerprint.json"):
            path = box.backups / f"{stem}{suffix}"
            path.write_text("old\n")
            os.utime(path, (now - days * 86400, now - days * 86400))
    done = box.run("backup.sh")
    assert done.returncode == 0, done.stderr
    files = _backup_files(box)
    assert not [name for name in files if name.startswith("grove-20260901T033000Z")]
    assert [name for name in files if name.startswith("grove-20261020T033000Z")] == [
        "grove-20261020T033000Z.dump",
        "grove-20261020T033000Z.dump.sha256",
        "grove-20261020T033000Z.fingerprint.json",
    ]
    assert len([name for name in files if name.endswith(".dump")]) == 2
    assert "removed grove-20260901T033000Z.dump, older than 30 days" in box.log_text("backup.log")


# ---- restore-check.sh -------------------------------------------------------------------------


def _planted_dump(box, fingerprint: str = FP) -> str:
    stem = "grove-20261102T033000Z"
    (box.backups / f"{stem}.dump").write_bytes(b"neo4j dump bytes")
    (box.backups / f"{stem}.fingerprint.json").write_text(json.dumps({"fingerprint": fingerprint}))
    return str(box.backups / f"{stem}.dump")


def test_a_restore_check_with_another_fingerprint_fails_and_cleans_up(box):
    box.deployed()
    dump = _planted_dump(box)
    done = box.run("restore-check.sh", dump, FAKE_RESTORE_FP="c" * 64)
    assert done.returncode == 3
    assert "MISMATCH" in box.log_text("backup.log")
    for step in ("docker rm --force", "docker volume rm", "docker network rm"):
        box.index(step)


def test_a_restore_check_runs_on_its_own_network_and_never_on_the_live_store(box):
    box.deployed()
    done = box.run("restore-check.sh", _planted_dump(box))
    assert done.returncode == 0, done.stderr
    load = box.calls()[box.index("neo4j-admin database load")]
    assert "--network none" in load and "grove-neo4j-data" not in load
    check = box.calls()[box.index("grove.store.dump_fingerprint")]
    assert "--network grove-restorecheck-" in check
    assert "bolt://grove-restorecheck-" in check
    assert f"grove-api:{SHA}" in check
    assert not [
        call for call in box.calls() if call.startswith("docker compose") and "stop" in call
    ]


def test_a_restore_check_refuses_a_dump_that_fails_its_checksum(box):
    box.deployed()
    dump = _planted_dump(box)
    (box.backups / "grove-20261102T033000Z.dump.sha256").write_text(
        "0" * 64 + "  grove-20261102T033000Z.dump\n"
    )
    done = box.run("restore-check.sh", dump)
    assert done.returncode != 0
    assert "does not match its checksum" in done.stderr
    assert not [call for call in box.calls() if call.startswith("docker run")]
