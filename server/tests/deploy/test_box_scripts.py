"""The box scripts against fake commands: what they run, in which order, and what they refuse."""

import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time

import pytest
import yaml

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
    assert box.calls()[0].startswith("flock -w 1800 9")
    assert not [call for call in box.calls() if call.startswith("git ")]


def test_deploy_refuses_another_branch_unless_told_it_is_meant(box):
    done = box.run("deploy.sh", SHA, "--ref", "feat/deploy-a-20261102")
    assert done.returncode != 0
    assert "only develop deploys" in done.stderr
    assert not [call for call in box.calls() if call.startswith(("flock", "git"))]


def test_deploy_checks_the_named_branch(box):
    branch = "feat/deploy-a-20261102"
    done = box.run("deploy.sh", SHA, "--ref", branch, GROVE_ALLOW_BRANCH_DEPLOY="1")
    assert done.returncode == 0, done.stderr
    fetch = box.calls()[box.index("git fetch")]
    assert "+refs/heads/feat/deploy-a-20261102:refs/remotes/origin/feat/deploy-a-20261102" in fetch
    assert f"git merge-base --is-ancestor {SHA} origin/feat/deploy-a-20261102" in box.calls()


def test_a_deploy_runs_its_steps_in_order(box):
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr

    steps = [
        "flock -w 1800 9",
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
    services_up = box.index("up --detach --wait --remove-orphans api gateway runner", migrate)
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
    assert box.index("nginx -t") < box.index("systemctl reload-or-restart nginx")


def test_install_host_asks_for_a_certificate_only_when_there_is_none(box):
    (box.root / "letsencrypt" / "live" / HOST / "fullchain.pem").unlink()
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    certbot = box.index("certbot certonly --webroot")
    assert f"--domain {HOST}" in box.calls()[certbot]
    # The challenge needs the port-80 site live first, and no TLS site that names a missing cert.
    assert box.index("systemctl reload-or-restart nginx") < certbot


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
    assert "groupadd --system --gid 10002 grove-runner" in box.calls()
    runner_home = box.root / "runner-home"
    useradd = [call for call in box.calls() if call.startswith("useradd")]
    # Exactly this: no --groups or -G, so no docker group and no sudo.
    assert useradd == [
        f"useradd --system --uid 10002 --gid 10002 --home-dir {runner_home} --no-create-home "
        "--shell /usr/sbin/nologin grove-runner"
    ]
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
        "stop runner gateway api",
        "python -m grove.store.dump_fingerprint",
        "stop neo4j",
        "neo4j-admin database dump neo4j --to-path=/backups",
        "start neo4j",
        f"python -m grove.store.dump_fingerprint --expect {FP}",
        "start api gateway runner",
        "docker network create --internal grove-restorecheck-",
        "neo4j-admin database load neo4j --from-stdin",
        "--env NEO4J_AUTH=none",
        f"python -m grove.store.dump_fingerprint --expect {FP}",
        "docker rm --force --volumes grove-restorecheck-",
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
    box.index("start api gateway runner", start_store)
    assert not [name for name in _backup_files(box) if name.endswith(".dump")]
    assert "FAILED" in box.log_text("backup.log")
    assert not [path for path in box.backups.iterdir() if path.name.startswith(".work-")]


def test_a_store_that_changed_across_the_dump_fails_the_backup(box):
    box.deployed()
    done = box.run("backup.sh", FAKE_FP_AFTER_FIRST="b" * 64)
    assert done.returncode != 0
    assert "the store changed across the dump" in done.stderr
    box.index("start api gateway runner")
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
    data = b"neo4j dump bytes"
    (box.backups / f"{stem}.dump").write_bytes(data)
    (box.backups / f"{stem}.fingerprint.json").write_text(json.dumps({"fingerprint": fingerprint}))
    digest = hashlib.sha256(data).hexdigest()
    (box.backups / f"{stem}.dump.sha256").write_text(f"{digest}  {stem}.dump\n")
    return str(box.backups / f"{stem}.dump")


def test_a_restore_check_with_another_fingerprint_fails_and_cleans_up(box):
    box.deployed()
    dump = _planted_dump(box)
    done = box.run("restore-check.sh", dump, FAKE_RESTORE_FP="c" * 64)
    assert done.returncode == 3
    assert "MISMATCH" in box.log_text("backup.log")
    for step in ("docker rm --force --volumes", "docker volume rm", "docker network rm"):
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


# ---- review round one -------------------------------------------------------------------------


def test_a_deploy_fed_on_stdin_keeps_the_rest_of_its_script(box):
    """ssh host 'bash -s' <<EOF: a one-off container must not read the lines after the deploy."""
    script = f"bash {box.src}/deploy/deploy.sh {SHA}\necho after-the-deploy\n"
    done = subprocess.run(
        ["bash", "-s"], input=script, env=box.env, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert "after-the-deploy" in done.stdout


def test_a_backup_fed_on_stdin_keeps_the_rest_of_its_script(box):
    box.deployed()
    script = f"bash {box.src}/deploy/backup.sh\necho after-the-backup\n"
    done = subprocess.run(
        ["bash", "-s"], input=script, env=box.env, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert "after-the-backup" in done.stdout


def test_the_box_lock_lives_in_a_folder_only_root_can_enter(box):
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    folder = box.root / "lock"
    assert oct(folder.stat().st_mode & 0o777) == "0o700"


def test_a_site_that_nginx_refuses_never_stays_live(box):
    https = box.nginx / "sites-available" / "grove-https.conf"
    https.write_text("the old site\n")
    (box.nginx / "sites-enabled" / "grove-https.conf").symlink_to(https)
    done = box.run("deploy.sh", SHA, FAKE_NGINX_STATUS="1")
    assert done.returncode != 0
    assert "the old ones are back" in done.stderr
    assert https.read_text() == "the old site\n"
    assert (box.nginx / "sites-enabled" / "grove-https.conf").resolve() == https
    assert not (box.nginx / "sites-available" / "grove-http.conf").exists()
    assert not (box.nginx / "sites-enabled" / "grove-http.conf").is_symlink()
    assert not [call for call in box.calls() if call.startswith("systemctl reload")]
    assert not [call for call in box.calls() if call.startswith("docker compose")]


def test_a_deploy_keeps_only_the_images_of_this_build_and_the_last(box):
    box.deployed(OTHER_SHA)
    old = "1" * 40
    images = [f"grove-{kind}:{tag}" for kind in ("api", "runner") for tag in (SHA, OTHER_SHA, old)]
    done = box.run("deploy.sh", SHA, FAKE_IMAGES=" ".join([*images, "neo4j:pinned"]))
    assert done.returncode == 0, done.stderr
    removed = sorted(call.split()[-1] for call in box.calls() if call.startswith("docker image rm"))
    assert removed == [f"grove-api:{old}", f"grove-runner:{old}"]


def test_a_failed_restore_check_keeps_every_old_dump(box):
    box.deployed()
    now = clock.now().timestamp()
    old = box.backups / "grove-20260901T033000Z.dump"
    old.write_text("old\n")
    os.utime(old, (now - 40 * 86400, now - 40 * 86400))
    done = box.run("backup.sh", FAKE_RESTORE_FP="c" * 64)
    assert done.returncode == 3
    assert old.exists()
    assert "removed" not in box.log_text("backup.log")


def test_an_empty_dump_fails_the_backup_and_is_not_kept(box):
    box.deployed()
    done = box.run("backup.sh", FAKE_EMPTY_DUMP="1")
    assert done.returncode != 0
    assert "the dump wrote no file" in done.stderr
    assert not [name for name in _backup_files(box) if name.endswith(".dump")]
    box.index("start api gateway runner")


def test_a_backup_stopped_by_systemd_mid_dump_starts_the_services_again(box):
    box.deployed()
    process = subprocess.Popen(
        ["bash", str(box.src / "deploy" / "backup.sh")],
        env={**box.env, "FAKE_DOCKER_SLOW": "neo4j-admin database dump", "FAKE_DOCKER_SLEEP": "3"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(100):
        if any("neo4j-admin database dump" in call for call in box.calls()):
            break
        time.sleep(0.1)
    process.send_signal(signal.SIGTERM)
    process.communicate(timeout=60)
    assert process.returncode == 143
    dump = box.index("neo4j-admin database dump")
    start = box.index("start neo4j", dump)
    box.index("start api gateway runner", start)
    assert "FAILED (exit 143)" in box.log_text("backup.log")


def test_the_backup_unit_starts_the_services_after_any_run(box):
    box.deployed()
    done = box.run("services-up.sh")
    assert done.returncode == 0, done.stderr
    start = box.index("start neo4j")
    box.index("start api gateway runner", start)
    unit = (box.src / "deploy" / "systemd" / "grove-backup.service").read_text()
    assert "ExecStopPost=/opt/grove/src/deploy/services-up.sh" in unit


def test_the_services_script_does_nothing_before_the_first_deploy(box):
    done = box.run("services-up.sh")
    assert done.returncode == 0, done.stderr
    assert not [call for call in box.calls() if call.startswith("docker")]


def test_a_restore_check_refuses_a_dump_with_no_checksum(box):
    box.deployed()
    dump = _planted_dump(box)
    (box.backups / "grove-20261102T033000Z.dump.sha256").unlink()
    done = box.run("restore-check.sh", dump)
    assert done.returncode != 0
    assert "no checksum" in done.stderr
    assert not [call for call in box.calls() if call.startswith("docker run")]


def test_a_restore_check_first_removes_what_a_killed_check_left(box):
    box.deployed()
    done = box.run("restore-check.sh", _planted_dump(box), FAKE_STALE="leftover")
    assert done.returncode == 0, done.stderr
    sweep = box.index("docker rm --force --volumes leftover")
    box.index("docker volume rm leftover", sweep)
    box.index("docker network rm leftover", sweep)
    assert sweep < box.index("docker network create --internal grove-restorecheck-")


def test_a_restore_check_by_hand_takes_the_box_lock(box):
    box.deployed()
    done = box.run("restore-check.sh", _planted_dump(box), FAKE_FLOCK_STATUS="1")
    assert done.returncode != 0
    assert not [call for call in box.calls() if call.startswith("docker")]


def test_the_smoke_fetches_the_metadata_as_meta_s_fetcher(box):
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    for path in ("oauth-protected-resource", "oauth-protected-resource/mcp"):
        fetched = [
            call
            for call in box.calls()
            if call.startswith("curl")
            and "--user-agent meta-webindexer/1.1" in call
            and call.endswith(f"{BASE}/.well-known/{path}")
        ]
        assert fetched, path


def test_the_smoke_fails_when_the_401_names_another_metadata_url(box):
    elsewhere = "https://elsewhere.test/.well-known/oauth-protected-resource"
    done = box.run("deploy.sh", SHA, FAKE_MCP_METADATA=elsewhere)
    assert done.returncode != 0
    assert "no resource_metadata" in done.stderr


def test_a_backup_removes_the_work_folder_of_a_killed_backup(box):
    box.deployed()
    stale = box.backups / ".work-20261102T011930Z"
    stale.mkdir()
    (stale / "neo4j.dump").write_bytes(b"partial")
    done = box.run("backup.sh")
    assert done.returncode == 0, done.stderr
    assert not stale.exists()
    assert len([name for name in _backup_files(box) if name.endswith(".dump")]) == 1


# ---- review round two -------------------------------------------------------------------------


def test_a_failed_apply_moves_the_checkout_back_to_the_served_sha(box):
    box.deployed(OTHER_SHA)
    (box.etc / "google-oauth.json").unlink()
    done = box.run("deploy.sh", SHA)
    assert done.returncode != 0
    checkouts = [call for call in box.calls() if call.startswith("git checkout")]
    assert checkouts == [
        f"git checkout --quiet --detach {SHA}",
        f"git checkout --quiet --detach {OTHER_SHA}",
    ]
    assert (box.root / "fake" / "HEAD").read_text() == OTHER_SHA
    assert f"host files are back at {OTHER_SHA}" in done.stdout + done.stderr


def test_a_deploy_whose_smoke_fails_keeps_the_checkout_at_the_build_it_serves(box):
    box.deployed(OTHER_SHA)
    done = box.run("deploy.sh", SHA, FAKE_SERVED_BUILD=OTHER_SHA)
    assert done.returncode != 0
    assert (box.state / "deployed.sha").read_text().strip() == SHA
    assert (box.root / "fake" / "HEAD").read_text() == SHA


def test_the_boot_unit_is_enabled_and_runs_the_services_script(box):
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    unit = (box.root / "systemd" / "grove-services.service").read_text()
    assert "ExecStart=/opt/grove/src/deploy/services-up.sh" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "After=docker.service" in unit
    assert "systemctl enable grove-services.service" in box.calls()
    # Started inside a deploy, it would wait for the lock that the deploy holds.
    assert "systemctl enable --now grove-services.service" not in box.calls()


def test_the_services_script_removes_left_one_off_containers_before_the_store_starts(box):
    box.deployed()
    done = box.run("services-up.sh", FAKE_STALE="oneoff-dump")
    assert done.returncode == 0, done.stderr
    listing = box.index("label=com.docker.compose.oneoff=True")
    assert "label=com.docker.compose.project=grove" in box.calls()[listing]
    removed = box.index("docker rm --force --volumes oneoff-dump", listing)
    start = box.index("start neo4j", removed)
    box.index("start api gateway runner", start)
    box.index("ps --quiet api", start)


def test_the_services_script_fails_when_the_api_never_reads_the_store(box):
    box.deployed()
    done = box.run("services-up.sh", FAKE_HEALTH="unhealthy", GROVE_SERVICES_TRIES="2")
    assert done.returncode != 0
    assert "not all healthy" in done.stderr


def test_a_certbot_failure_brings_the_old_sites_back_with_no_tls_site_on(box):
    (box.root / "letsencrypt" / "live" / HOST / "fullchain.pem").unlink()
    https = box.nginx / "sites-available" / "grove-https.conf"
    https.write_text("the old site\n")
    (box.nginx / "sites-enabled" / "grove-https.conf").symlink_to(https)
    done = box.run("deploy.sh", SHA, FAKE_CERTBOT_STATUS="1")
    assert done.returncode != 0
    assert "certbot got no certificate" in done.stderr
    # The file comes back, but no enabled site may name a certificate that does not exist.
    assert https.read_text() == "the old site\n"
    assert not (box.nginx / "sites-enabled" / "grove-https.conf").is_symlink()
    assert not (box.nginx / "sites-available" / "grove-http.conf").exists()


def test_the_certbot_reload_hook_is_installed(box):
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    hook = box.root / "letsencrypt" / "renewal-hooks" / "deploy" / "grove-reload-nginx.sh"
    assert hook.is_file() and os.access(hook, os.X_OK)


def test_a_restore_check_never_names_the_live_volume(box):
    box.deployed()
    done = box.run("restore-check.sh", _planted_dump(box))
    assert done.returncode == 0, done.stderr
    assert not [call for call in box.calls() if "grove-neo4j-data" in call]
    for kind in ("docker ps --all --quiet", "docker volume ls", "docker network ls"):
        listing = box.calls()[box.index(kind)]
        assert "--filter name=grove-restorecheck-" in listing, kind


def test_the_smoke_fails_when_meta_s_fetcher_is_refused(box):
    done = box.run("deploy.sh", SHA, FAKE_REFUSE_AGENT="meta-webindexer/1.1")
    assert done.returncode != 0
    assert "answered 403 to meta-webindexer/1.1" in done.stderr


@pytest.mark.parametrize("mode", [0o640, 0o620, 0o610, 0o604, 0o602, 0o601])
def test_apply_refuses_every_group_or_other_bit(box, mode):
    (box.etc / "neo4j.env").chmod(mode)
    done = box.run("deploy.sh", SHA)
    assert done.returncode != 0
    assert "readable by group or others" in done.stderr


def test_retention_keeps_a_dump_just_under_thirty_days_and_drops_one_just_over(box):
    box.deployed()
    now = clock.now().timestamp()
    ages = {"grove-20261003T120000Z": 29.5, "grove-20261002T000000Z": 30.5}
    for stem, days in ages.items():
        path = box.backups / f"{stem}.dump"
        path.write_text("old\n")
        os.utime(path, (now - days * 86400, now - days * 86400))
    done = box.run("backup.sh")
    assert done.returncode == 0, done.stderr
    assert (box.backups / "grove-20261003T120000Z.dump").exists()
    assert not (box.backups / "grove-20261002T000000Z.dump").exists()


def test_the_runner_home_belongs_to_the_runner_alone(box):
    home = box.root / "runner-home"
    done = box.run("bootstrap.sh", "--base-url", BASE, GROVE_RUNNER_HOME=str(home))
    assert done.returncode == 0, done.stderr
    assert oct(home.stat().st_mode & 0o777) == "0o700"


REAL_FLOCK = shutil.which("flock")


@pytest.mark.skipif(REAL_FLOCK is None, reason="no flock(1) on this machine")
def test_with_a_real_flock_the_backup_reenters_the_lock_for_its_restore_check(box):
    (box.root / "bin" / "flock").unlink()
    box.deployed()
    done = box.run("backup.sh")
    assert done.returncode == 0, done.stderr
    assert "matches" in box.log_text("backup.log")


@pytest.mark.skipif(REAL_FLOCK is None, reason="no flock(1) on this machine")
def test_with_a_real_flock_a_held_lock_stops_a_deploy(box):
    (box.root / "bin" / "flock").unlink()
    lock = box.root / "lock" / "grove-deploy.lock"
    lock.parent.mkdir(mode=0o700)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        done = box.run("deploy.sh", SHA, GROVE_DEPLOY_WAIT_SECONDS="1")
    assert done.returncode != 0
    assert "holds" in done.stderr
    assert not [call for call in box.calls() if call.startswith("git ")]


# ---- review round three -----------------------------------------------------------------------


def test_a_failure_in_the_service_start_keeps_the_checkout_at_the_build_that_now_runs(box):
    box.deployed(OTHER_SHA)
    done = box.run("deploy.sh", SHA, FAKE_DOCKER_FAIL="up --detach --wait --remove-orphans")
    assert done.returncode != 0
    # compose has replaced the containers, so the records name the new build, not the old one.
    assert (box.state / "deployed.sha").read_text().strip() == SHA
    assert (box.root / "fake" / "HEAD").read_text() == SHA
    assert "back at" not in done.stdout + done.stderr
    line = f"deploy {SHA} ref=develop previous={OTHER_SHA}: runs, services NOT healthy"
    assert line in box.log_text("deploy.log")


def test_a_redeploy_of_one_sha_keeps_the_images_of_the_build_before_it(box):
    box.deployed(OTHER_SHA)
    assert box.run("deploy.sh", SHA).returncode == 0
    old = "1" * 40
    images = [f"grove-api:{tag}" for tag in (SHA, OTHER_SHA, old)]
    done = box.run("deploy.sh", SHA, FAKE_IMAGES=" ".join(images))
    assert done.returncode == 0, done.stderr
    removed = [call.split()[-1] for call in box.calls() if call.startswith("docker image rm")]
    assert removed == [f"grove-api:{old}"]
    assert (box.state / "previous.sha").read_text().strip() == OTHER_SHA


def test_the_checkout_check_also_sees_ignored_files(box):
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    assert "git status --porcelain --ignored" in box.calls()


def test_a_first_deploy_prints_no_error_about_a_missing_state_file(box):
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    assert "No such file" not in done.stderr


def test_a_failed_step_after_the_sites_are_written_brings_the_old_sites_back(box):
    https = box.nginx / "sites-available" / "grove-https.conf"
    https.write_text("the old site\n")
    (box.nginx / "sites-enabled" / "grove-https.conf").symlink_to(https)
    done = box.run("deploy.sh", SHA, FAKE_SYSTEMCTL_STATUS="1")
    assert done.returncode != 0
    assert https.read_text() == "the old site\n"
    assert not (box.nginx / "sites-available" / "grove-http.conf").exists()
    assert not [call for call in box.calls() if call.startswith("docker compose")]


def test_certbot_registers_with_the_given_address_or_with_none(box):
    (box.root / "letsencrypt" / "live" / HOST / "fullchain.pem").unlink()
    assert box.run("deploy.sh", SHA).returncode == 0
    assert "--register-unsafely-without-email" in box.calls()[box.index("certbot certonly")]

    box.env["FAKE_LOG"] = str(box.root / "calls-2.log")
    (box.nginx / "sites-enabled" / "grove-https.conf").unlink()
    done = box.run("deploy.sh", SHA, GROVE_ACME_EMAIL="ops@grove.test")
    assert done.returncode == 0, done.stderr
    call = box.calls()[box.index("certbot certonly")]
    assert "--email ops@grove.test" in call
    assert "--register-unsafely-without-email" not in call


def test_the_services_script_starts_the_writers_even_when_the_store_is_slow(box):
    box.deployed()
    done = box.run("services-up.sh", FAKE_UNHEALTHY="neo4j", GROVE_SERVICES_TRIES="2")
    # The API's own check reads the store, so it decides, not the store's port check.
    assert done.returncode == 0, done.stderr
    assert "neo4j is not healthy yet" in done.stdout + done.stderr
    start = box.index("start neo4j")
    box.index("start api gateway runner", start)


def test_the_services_script_fails_when_only_the_api_is_unhealthy(box):
    box.deployed()
    done = box.run("services-up.sh", FAKE_UNHEALTHY="api", GROVE_SERVICES_TRIES="2")
    assert done.returncode != 0
    assert "not all healthy" in done.stderr


def test_a_one_off_container_that_cannot_be_removed_does_not_stop_the_start(box):
    box.deployed()
    done = box.run(
        "services-up.sh", FAKE_STALE="stuck", FAKE_DOCKER_FAIL="rm --force --volumes stuck"
    )
    assert done.returncode == 0, done.stderr
    assert "could not remove one-off container stuck" in done.stdout + done.stderr
    box.index("start api gateway runner", box.index("start neo4j"))


def test_bootstrap_says_which_tool_is_missing(box):
    (box.root / "bin" / "nginx").unlink()
    path = f"{box.root / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin"
    done = box.run("bootstrap.sh", "--base-url", BASE, PATH=path)
    assert done.returncode != 0
    assert "nginx is not installed" in done.stderr


def test_the_boot_unit_keeps_trying(box):
    unit = (box.src / "deploy" / "systemd" / "grove-services.service").read_text()
    assert "Restart=on-failure" in unit
    assert "RestartSec=60" in unit
    assert "StartLimitIntervalSec=0" in unit


# ---- review round four ------------------------------------------------------------------------


def test_a_failed_build_puts_the_host_files_of_the_served_commit_back(box):
    box.deployed(OTHER_SHA)
    done = box.run("deploy.sh", SHA, FAKE_DOCKER_FAIL="build api runner")
    assert done.returncode != 0
    back = box.index(f"git checkout --quiet --detach {OTHER_SHA}")
    # install-host.sh runs again from the restored checkout.
    box.index("systemctl daemon-reload", back)
    box.index("nginx -t", back)
    assert f"the checkout and host files are back at {OTHER_SHA}" in done.stdout + done.stderr


def test_the_services_script_starts_the_writers_again_after_compose_refused_them(box):
    """Real compose refuses `start api` while the store reports unhealthy."""
    box.deployed()
    done = box.run("services-up.sh", FAKE_START_REFUSALS="2", GROVE_SERVICES_TRIES="5")
    assert done.returncode == 0, done.stderr
    starts = [call for call in box.calls() if call.endswith("start api gateway runner")]
    assert len(starts) == 3


def test_the_services_script_gives_up_after_its_tries_and_says_so(box):
    box.deployed()
    done = box.run("services-up.sh", FAKE_START_REFUSALS="99", GROVE_SERVICES_TRIES="3")
    assert done.returncode != 0
    assert "not all healthy" in done.stderr
    assert len([call for call in box.calls() if call.endswith("start api gateway runner")]) == 3


def test_a_failed_backup_hands_over_to_the_unit_that_keeps_trying(box):
    unit = (box.src / "deploy" / "systemd" / "grove-backup.service").read_text()
    assert "OnFailure=grove-services.service" in unit


def test_a_backup_waits_for_the_store_before_its_first_read(box):
    box.deployed()
    done = box.run("backup.sh")
    assert done.returncode == 0, done.stderr
    stop = box.index("stop runner gateway api")
    start = box.index("start neo4j", stop)
    probe = box.index("ps --quiet neo4j", start)
    assert probe < box.index("grove.store.dump_fingerprint", start)


def test_a_backup_makes_no_dump_while_the_store_is_unhealthy(box):
    box.deployed()
    done = box.run("backup.sh", FAKE_UNHEALTHY="neo4j", GROVE_SERVICES_TRIES="2")
    assert done.returncode != 0
    assert "neo4j is not healthy, so no dump" in done.stderr
    assert not [call for call in box.calls() if "neo4j-admin" in call]
    box.index("start api gateway runner")


def test_a_later_failure_on_the_first_certificate_makes_the_old_sites_live_again(box):
    (box.root / "letsencrypt" / "live" / HOST / "fullchain.pem").unlink()
    done = box.run("deploy.sh", SHA, FAKE_FAIL_CALL="systemctl daemon-reload")
    assert done.returncode != 0
    certbot = box.index("certbot certonly")
    assert box.index("systemctl reload-or-restart nginx") < certbot
    box.index("systemctl reload-or-restart nginx", certbot)


def test_a_base_url_with_a_path_stops_the_deploy_before_any_site_is_written(box):
    (box.etc / "api.env").write_text("GROVE_PUBLIC_BASE_URL=https://app.example/\n")
    (box.etc / "api.env").chmod(0o600)
    (box.root / "letsencrypt" / "live" / HOST / "fullchain.pem").unlink()
    done = box.run("deploy.sh", SHA)
    assert done.returncode != 0
    assert "must be https://<host>" in done.stderr
    assert not (box.nginx / "sites-available" / "grove-http.conf").exists()
    assert not [call for call in box.calls() if call.startswith(("certbot", "nginx", "docker"))]


def test_a_failed_write_of_the_state_file_keeps_the_old_record(box):
    box.deployed(OTHER_SHA)
    # A folder in the place of the temporary file makes the whole-file write fail.
    (box.state / "deployed.sha.tmp").mkdir()
    done = box.run("deploy.sh", SHA)
    assert done.returncode != 0
    assert (box.state / "deployed.sha").read_text().strip() == OTHER_SHA
    assert not [call for call in box.calls() if "--remove-orphans api gateway runner" in call]


def test_a_deploy_leaves_no_temporary_state_file(box):
    box.deployed(OTHER_SHA)
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    assert sorted(path.name for path in box.state.iterdir()) == ["deployed.sha", "previous.sha"]


def test_apply_refuses_an_env_file_with_another_owner(box):
    fake_id = box.root / "bin" / "id"
    fake_id.write_text("#!/bin/sh\necho 54321\n")
    fake_id.chmod(0o755)
    done = box.run("deploy.sh", SHA)
    assert done.returncode != 0
    assert "has another owner" in done.stderr
    assert not [call for call in box.calls() if call.startswith("docker")]


def test_apply_refuses_a_secret_folder_open_to_others(box):
    box.etc.chmod(0o750)
    done = box.run("deploy.sh", SHA)
    assert done.returncode != 0
    assert "open to group or others" in done.stderr
    assert not [call for call in box.calls() if call.startswith("docker")]


# ---- review round five ------------------------------------------------------------------------


def test_a_failed_reinstall_says_so_and_never_says_the_host_files_are_back(box):
    box.deployed(OTHER_SHA)
    done = box.run("deploy.sh", SHA, FAKE_DOCKER_FAIL="build api runner", FAKE_SYSTEMCTL_STATUS="1")
    assert done.returncode != 0
    shown = done.stdout + done.stderr
    assert "host files did not install" in shown
    assert "host files are back" not in shown
    assert (box.root / "fake" / "HEAD").read_text() == OTHER_SHA


def test_the_services_script_fails_when_the_runner_does_not_run(box):
    box.deployed()
    done = box.run("services-up.sh", FAKE_NOT_RUNNING="runner", GROVE_SERVICES_TRIES="2")
    assert done.returncode != 0
    assert "not all healthy" in done.stderr
    assert "the store and its writers run" not in done.stdout


def test_the_services_script_shows_what_compose_said_when_it_gives_up(box):
    box.deployed()
    done = box.run(
        "services-up.sh",
        FAKE_START_REFUSALS="99",
        FAKE_START_MESSAGE="dependency failed to start",
        GROVE_SERVICES_TRIES="2",
    )
    assert done.returncode != 0
    assert "compose said: dependency failed to start" in done.stderr


def test_a_failed_restore_check_leaves_a_line_in_the_backup_log(box):
    box.deployed()
    dump = _planted_dump(box)
    (box.backups / "grove-20261102T033000Z.dump.sha256").unlink()
    done = box.run("restore-check.sh", dump)
    assert done.returncode != 0
    line = "restore check grove-20261102T033000Z.dump: FAILED, no checksum"
    assert line in box.log_text("backup.log")


def test_a_scratch_store_that_never_starts_fails_the_check_and_cleans_up(box):
    box.deployed()
    done = box.run("restore-check.sh", _planted_dump(box), FAKE_DOCKER_FAIL="exec ")
    assert done.returncode != 0
    assert "the scratch store did not start" in done.stderr
    assert "FAILED, the scratch store did not start" in box.log_text("backup.log")
    for step in ("docker rm --force --volumes", "docker volume rm", "docker network rm"):
        box.index(step)


@pytest.mark.parametrize("path", ["/", "/signin", "/privacy"])
def test_the_smoke_fails_when_a_page_does_not_answer(box, path):
    done = box.run("deploy.sh", SHA, FAKE_NO_PAGE=path)
    assert done.returncode != 0
    assert f"{path} answered 404 with no page" in done.stderr


def test_the_smoke_asks_for_every_page(box):
    assert box.run("deploy.sh", SHA).returncode == 0
    box.index(f"{BASE}/signin")
    box.index(f"{BASE}/privacy")


# ---- uploads and the screening files ----------------------------------------------------------

SCREENING_FILES = ("rename.json", "denylist.txt")
UPLOADS_503 = "uploads answer 503 until both screening files are placed"
# Synthetic content: invented names only.
SCREENING_TEXT = {
    "rename.json": '{"Invented Person": "Person A"}\n',
    "denylist.txt": "Invented Codename\n",
}
OPEN_MODES = [0o644, 0o640, 0o620, 0o610, 0o604, 0o602, 0o601]


def _place(box, name: str, mode: int = 0o600):
    folder = box.etc / "screening"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / name
    path.write_text(SCREENING_TEXT[name])
    path.chmod(mode)
    return path


def _mode(path) -> int:
    return path.lstat().st_mode & 0o777


def _uploads_lines(done) -> list[str]:
    return [line for line in (done.stdout + done.stderr).splitlines() if UPLOADS_503 in line]


def _chowns(box) -> list[str]:
    return [call for call in box.calls() if call.startswith("chown")]


def _chown_at(box, path) -> int:
    """The position of the chown of exactly this path: the folder's path starts its files' paths."""
    return box.calls().index(f"chown 10001:10001 {path}")


def test_a_deploy_with_no_screening_files_serves_and_says_uploads_answer_503(box):
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    (line,) = _uploads_lines(done)
    assert line.endswith(f"{UPLOADS_503}; absent: rename.json denylist.txt")
    # The file names only: no path of the box.
    assert "/" not in line
    folder = box.etc / "screening"
    assert folder.is_dir() and _mode(folder) == 0o500
    assert _chown_at(box, folder) < box.index("up --detach --wait neo4j")
    box.index("up --detach --wait --remove-orphans api gateway runner")
    assert f"deploy {SHA} ref=develop previous=none: served, smoke ok" in box.log_text("deploy.log")


def test_one_absent_screening_file_still_serves_and_names_only_that_file(box):
    placed = _place(box, "rename.json")
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    (line,) = _uploads_lines(done)
    assert line.endswith(f"{UPLOADS_503}; absent: denylist.txt")
    assert _chown_at(box, placed) < _chown_at(box, box.etc / "screening")
    assert _mode(placed) == 0o400


def test_a_deploy_hands_the_screening_files_to_the_api_user_at_400(box):
    paths = [_place(box, name) for name in SCREENING_FILES]
    folder = box.etc / "screening"
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stderr
    assert not _uploads_lines(done)
    for path in paths:
        assert _mode(path) == 0o400, path.name
        # The fake chown records the call; an unprivileged test cannot change an owner.
        assert _chown_at(box, path) < _chown_at(box, folder)
    assert _mode(folder) == 0o500
    assert _chown_at(box, folder) < box.index("up --detach --wait neo4j")
    shown = done.stdout + done.stderr
    for text in SCREENING_TEXT.values():
        assert text.strip() not in shown

    # The next deploy finds them at 400, which is as private as 600, and goes on.
    box.env["FAKE_LOG"] = str(box.root / "calls-2.log")
    again = box.run("deploy.sh", SHA)
    assert again.returncode == 0, again.stderr
    assert not _uploads_lines(again)
    assert [_mode(path) for path in paths] == [0o400, 0o400]


@pytest.mark.parametrize("name", SCREENING_FILES)
def test_a_screening_file_open_to_others_stops_the_deploy_before_any_chown(box, name):
    for other in SCREENING_FILES:
        _place(box, other, 0o644 if other == name else 0o600)
    done = box.run("deploy.sh", SHA)
    assert done.returncode != 0
    assert f"the screening file {name} is open to group or others: place it at 600" in done.stderr
    # Not one chown, and the mode is as the person placed it: the deploy hides nothing.
    assert not _chowns(box)
    assert _mode(box.etc / "screening" / name) == 0o644
    assert not [call for call in box.calls() if call.startswith("docker")]


@pytest.mark.parametrize("mode", OPEN_MODES[1:], ids=oct)
def test_every_group_or_other_bit_on_a_screening_file_stops_the_deploy(box, mode):
    _place(box, "denylist.txt", mode)
    done = box.run("deploy.sh", SHA)
    assert done.returncode != 0
    assert "the screening file denylist.txt is open to group or others" in done.stderr
    assert not _chowns(box)


@pytest.mark.parametrize("target_exists", [True, False])
def test_a_screening_file_that_is_a_link_stops_the_deploy(box, target_exists):
    """chown and chmod follow a link, so a link could hand any file of the box to the API."""
    target = box.root / "elsewhere.txt"
    if target_exists:
        target.write_text("Invented Codename\n")
        target.chmod(0o600)
    (box.etc / "screening").mkdir(mode=0o700)
    (box.etc / "screening" / "denylist.txt").symlink_to(target)
    done = box.run("deploy.sh", SHA)
    assert done.returncode != 0
    assert "the screening file denylist.txt is not a plain file" in done.stderr
    assert not _chowns(box)
    if target_exists:
        assert _mode(target) == 0o600


def test_a_screening_folder_that_is_a_link_stops_the_deploy(box):
    elsewhere = box.root / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (box.etc / "screening").symlink_to(elsewhere)
    done = box.run("deploy.sh", SHA)
    assert done.returncode != 0
    assert "the screening folder is not a plain folder" in done.stderr
    assert not _chowns(box)
    assert _mode(elsewhere) == 0o700


# ---- a network transition with existing service endpoints -----------------------------------


@pytest.fixture
def network_box(box):
    revisions = box.network_revisions()
    assert box.run("install-host.sh").returncode == 0
    initial = box.network_state()
    credentials = {
        path: (path.read_bytes(), _mode(path)) for path in box.etc.iterdir() if path.is_file()
    }
    yield box, revisions, initial
    final = box.network_state()
    assert final["containers"]["neo4j"] == initial["containers"]["neo4j"]
    assert final["volumes"] == initial["volumes"]
    for field in ("id", "key", "internal", "options"):
        assert final["networks"]["grove_store"][field] == initial["networks"]["grove_store"][field]
    for path, expected in credentials.items():
        assert (path.read_bytes(), _mode(path)) == expected
    assert not any(
        forbidden in call
        for call in box.calls()
        for forbidden in (
            " down",
            "volume rm",
            "volume prune",
            "network disconnect",
            "stop neo4j",
            "rm neo4j",
        )
    )
    assert not any(event[:2] == ["remove-container", "neo4j"] for event in final["events"])
    # These commands are fake. No agent, model gateway, or source upload is part of a deploy.
    assert not any("opencode" in call or "/v1/internal/runner/due" in call for call in box.calls())


def test_runner_network_upgrade_with_active_legacy_endpoints(network_box):
    box, _, initial = network_box
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stdout + done.stderr
    state = box.network_state()
    assert state["networks"]["grove_runner"]["id"] == initial["networks"]["grove_runner"]["id"]
    assert state["networks"]["grove_runner"]["endpoints"] == []
    assert state["networks"]["grove_runner_v2"]["key"] == "runner_v2"
    assert ["migration", SHA, OTHER_SHA] in state["events"]
    migration = state["events"].index(["migration", SHA, OTHER_SHA])
    replacements = [i for i, event in enumerate(state["events"]) if event[0] == "remove-container"]
    assert replacements and min(replacements) > migration
    assert not any(event[0] == "disconnect-for-rename" for event in state["events"])
    assert "ok healthz build=" + SHA in done.stdout


@pytest.mark.parametrize("failure", ["create", "migrate"])
def test_old_entrypoint_preserves_legacy_services_before_target_promotion(network_box, failure):
    box, _, initial = network_box
    done = box.run("deploy.sh", SHA, FAKE_NETWORK_FAILURE=failure)
    assert done.returncode != 0
    assert (
        f"synthetic {'network creation' if failure == 'create' else 'migration'} failure"
        in done.stderr
    )
    state = box.network_state()
    assert state["containers"] == initial["containers"]
    assert state["networks"]["grove_runner"] == initial["networks"]["grove_runner"]
    assert (box.root / "fake" / "HEAD").read_text() == OTHER_SHA
    assert (box.state / "deployed.sha").read_text().strip() == OTHER_SHA
    assert "synthetic legacy revision" in (box.nginx / "sites-enabled/grove-https.conf").read_text()
    assert "host files are back at " + OTHER_SHA in done.stdout
    assert box.run("smoke.sh", BASE, OTHER_SHA).returncode == 0


def test_runner_network_name_only_change_does_not_preserve_legacy_network(network_box):
    box, revisions, _ = network_box
    path = revisions / SHA / "deploy/compose.yml"
    config = yaml.safe_load(path.read_text())
    definition = config["networks"].pop("runner_v2")
    config["networks"]["runner"] = definition
    for service in config["services"].values():
        service["networks"] = [
            "runner" if key == "runner_v2" else key for key in service["networks"]
        ]
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    done = box.run("deploy.sh", SHA, FAKE_NETWORK_FAILURE="migrate")
    assert done.returncode != 0
    state = box.network_state()
    assert any(event[0] == "disconnect-for-rename" for event in state["events"])
    assert "grove_runner" not in state["networks"]
    # A restored marker and checkout cannot make a disconnected legacy API healthy.
    assert (box.state / "deployed.sha").read_text().strip() == OTHER_SHA
    assert box.run("smoke.sh", BASE, OTHER_SHA).returncode != 0


def test_target_service_start_failure_preserves_truthful_build_state(network_box):
    box, _, _ = network_box
    done = box.run("deploy.sh", SHA, FAKE_NETWORK_FAILURE="start")
    assert done.returncode != 0
    assert "runs, services NOT healthy" in box.log_text("deploy.log")
    assert "host files are back" not in done.stdout
    assert (box.root / "fake" / "HEAD").read_text() == SHA
    assert (box.state / "deployed.sha").read_text().strip() == SHA
    assert (box.state / "previous.sha").read_text().strip() == OTHER_SHA
    assert box.run("smoke.sh", BASE, SHA).returncode != 0


def test_same_sha_redeploy_reuses_the_new_runner_network(network_box):
    box, _, _ = network_box
    assert box.run("deploy.sh", SHA).returncode == 0
    identity = box.network_state()["networks"]["grove_runner_v2"]["id"]
    done = box.run("deploy.sh", SHA)
    assert done.returncode == 0, done.stdout + done.stderr
    state = box.network_state()
    assert state["networks"]["grove_runner_v2"]["id"] == identity
    assert state["events"].count(["create-network", "grove_runner_v2"]) == 1
    assert (box.state / "previous.sha").read_text().strip() == OTHER_SHA


@pytest.mark.parametrize("failed_start", [False, True])
def test_rollback_reuses_legacy_network_and_removes_gateway_orphan(network_box, failed_start):
    box, _, initial = network_box
    first = box.run("deploy.sh", SHA, FAKE_NETWORK_FAILURE="start" if failed_start else "")
    assert (first.returncode != 0) == failed_start
    assert "gateway" in box.network_state()["containers"]
    done = box.run("deploy.sh", OTHER_SHA)
    assert done.returncode == 0, done.stdout + done.stderr
    state = box.network_state()
    assert state["networks"]["grove_runner"]["id"] == initial["networks"]["grove_runner"]["id"]
    assert state["networks"]["grove_runner_v2"]["endpoints"] == []
    assert "gateway" not in state["containers"]
    assert ["remove-container", "gateway"] in state["events"]
    assert (box.state / "deployed.sha").read_text().strip() == OTHER_SHA
    assert (box.state / "previous.sha").read_text().strip() == SHA
    assert "ok healthz build=" + OTHER_SHA in done.stdout


def test_network_health_does_not_trust_the_deployed_marker(network_box):
    box, _, _ = network_box
    box.deployed(SHA)
    assert box.run("smoke.sh", BASE, OTHER_SHA).returncode == 0
    state = box.network_state()
    del state["containers"]["api"]
    with open(box.env["FAKE_NETWORK_STATE"], "w") as handle:
        json.dump(state, handle)
    box.deployed(OTHER_SHA)
    assert box.run("smoke.sh", BASE, OTHER_SHA).returncode != 0


def test_network_transition_respects_the_existing_host_lock(network_box):
    box, _, initial = network_box
    done = box.run("deploy.sh", SHA, FAKE_FLOCK_STATUS="1")
    assert done.returncode != 0
    assert "holds" in done.stderr
    assert box.network_state() == initial
    assert not [call for call in box.calls() if call.startswith("docker ")]


# ---- the runner lane: the gateway -------------------------------------------------------------


def test_the_services_script_fails_when_only_the_gateway_is_unhealthy(box):
    box.deployed()
    done = box.run("services-up.sh", FAKE_UNHEALTHY="gateway", GROVE_SERVICES_TRIES="2")
    assert done.returncode != 0
    assert "not all healthy" in done.stderr
    box.index("ps --quiet gateway", box.index("start api gateway runner"))


def test_a_backup_stops_the_gateway_before_its_first_fingerprint(box):
    # The gateway writes a model_calls row for each call: a row written between the two
    # fingerprints fails the backup with "the store changed across the dump".
    box.deployed()
    done = box.run("backup.sh")
    assert done.returncode == 0, done.stderr
    stop = box.index("stop runner gateway api")
    first = box.index("grove.store.dump_fingerprint")
    assert stop < first
    assert not [
        call for call in box.calls()[stop:first] if " start " in f" {call} " and "gateway" in call
    ]
    box.index("start api gateway runner", box.index(f"--expect {FP}", first))
