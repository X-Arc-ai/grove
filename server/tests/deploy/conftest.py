"""A scratch box for the deploy scripts: a copy of deploy/, scratch system folders, and fake
docker, git, flock, nginx, systemctl, certbot, curl, chown and install that log every call.

The scripts run unchanged: every path they touch moves through its GROVE_* variable.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "deploy"
HOST = "grove.test"
BASE = f"https://{HOST}"
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
NEO4J_IMAGE = "neo4j:test-community@sha256:" + "4" * 64
FP = "a" * 64

_FAKE_DOCKER = r"""
import json, os, sys
from pathlib import Path

import time

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("docker " + " ".join(args) + "\n")
joined = " ".join(args)
if os.environ.get("FAKE_NETWORK_STATE"):
    exec(Path(os.environ["FAKE_NETWORK_DRIVER"]).read_text())
    if network_docker(args):
        sys.exit(0)
fail = os.environ.get("FAKE_DOCKER_FAIL")
if fail and fail in joined:
    sys.exit(1)
slow = os.environ.get("FAKE_DOCKER_SLOW")
if slow and slow in joined:
    time.sleep(float(os.environ.get("FAKE_DOCKER_SLEEP", "3")))


def fingerprint(counter_name, default):
    counter = Path(os.environ["FAKE_STATE"]) / counter_name
    calls = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(calls))
    after = os.environ.get("FAKE_FP_AFTER_FIRST")
    value = after if (after and calls > 1) else default
    out = {"fingerprint": value, "nodes": 3}
    if "--expect" in args:
        expected = args[args.index("--expect") + 1]
        out["match"] = value == expected
        print(json.dumps(out))
        sys.exit(0 if out["match"] else 3)
    print(json.dumps(out))
    sys.exit(0)


if args[0] == "compose":
    rest = args[1:]
    while rest and rest[0].startswith("--"):
        rest = rest[2:]
    command = rest[0]
    if command == "start" and "api" in rest and "FAKE_START_REFUSALS" in os.environ:
        # Real compose refuses while the store reports unhealthy, and starts nothing.
        counter = Path(os.environ["FAKE_STATE"]) / "start_refusals"
        refused = int(counter.read_text()) if counter.exists() else 0
        if refused < int(os.environ["FAKE_START_REFUSALS"]):
            counter.write_text(str(refused + 1))
            print(os.environ.get("FAKE_START_MESSAGE", "refused"), file=sys.stderr)
            sys.exit(1)
        Path(os.environ["FAKE_STATE"], "api_started").write_text("yes")
    # docker compose run keeps stdin open, as the real one does with no --interactive=false.
    if command == "run" and not sys.stdin.isatty():
        sys.stdin.read()
    if command == "config" and "--images" in rest:
        print(os.environ["FAKE_NEO4J_IMAGE"])
        print("grove-api:" + os.environ.get("GROVE_BUILD_SHA", "unset"))
    elif command == "ps":
        if rest[-1] != os.environ.get("FAKE_NOT_RUNNING"):
            print("container-" + rest[-1])
    elif command == "run" and "grove.store.dump_fingerprint" in rest:
        fingerprint("live_fingerprints", os.environ.get("FAKE_FP", "a" * 64))
    elif command == "run" and "neo4j-admin" in rest and "dump" in rest:
        mount = rest[rest.index("--volume") + 1]
        host_dir = mount.rsplit(":", 1)[0]
        empty = os.environ.get("FAKE_EMPTY_DUMP") == "1"
        Path(host_dir, "neo4j.dump").write_bytes(b"" if empty else b"neo4j dump bytes")
    elif command == "run" and "grove.writer.migrate" in rest:
        print(json.dumps({"applied": [], "skipped": []}))
elif args[:2] == ["image", "ls"]:
    for image in os.environ.get("FAKE_IMAGES", "").split():
        print(image)
elif args[0] == "ps" or args[1:2] == ["ls"]:
    for stale in os.environ.get("FAKE_STALE", "").split():
        print(stale)
elif args[0] == "inspect":
    unhealthy = "container-" + os.environ.get("FAKE_UNHEALTHY", "none")
    refused = "FAKE_START_REFUSALS" in os.environ and not Path(
        os.environ["FAKE_STATE"], "api_started"
    ).exists()
    if args[-1] == unhealthy or (refused and args[-1] == "container-api"):
        print("unhealthy")
    else:
        print(os.environ.get("FAKE_HEALTH", "healthy"))
elif args[0] == "run" and "grove.store.dump_fingerprint" in args:
    live = os.environ.get("FAKE_FP", "a" * 64)
    fingerprint("restore_fingerprints", os.environ.get("FAKE_RESTORE_FP", live))
elif args[0] == "run" and "load" in args:
    sys.stdin.buffer.read()
"""

_FAKE_GIT = r"""
import os, shutil, sys
from pathlib import Path

args = sys.argv[1:]
if args[:1] == ["-C"]:
    args = args[2:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("git " + " ".join(args) + "\n")
head = Path(os.environ["FAKE_STATE"]) / "HEAD"
command = args[0]
if command == "fetch":
    sys.exit(int(os.environ.get("FAKE_FETCH_STATUS", "0")))
if command == "merge-base":
    sys.exit(int(os.environ.get("FAKE_ANCESTOR_STATUS", "0")))
if command == "status":
    print(os.environ.get("FAKE_DIRTY", ""), end="")
elif command == "checkout":
    if os.environ.get("FAKE_REVISIONS"):
        revision = Path(os.environ["FAKE_REVISIONS"]) / args[-1]
        for source in sorted(revision.rglob("*")):
            if not source.is_file():
                continue
            target = Path(os.environ["GROVE_SRC"]) / source.relative_to(revision)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".checkout")
            shutil.copy2(source, temporary)
            # An open deploy.sh must keep the old inode, as real git checkout does.
            os.replace(temporary, target)
    head.write_text(args[-1])
elif command == "rev-parse":
    print(head.read_text() if head.exists() else "")
"""

_FAKE_CURL = r"""
import json, os, sys
from pathlib import Path
from urllib.parse import urlsplit

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("curl " + " ".join(args) + "\n")
url = args[-1]
out = args[args.index("--output") + 1]
headers = args[args.index("--dump-header") + 1]
method = args[args.index("--request") + 1]
agent = args[args.index("--user-agent") + 1]
parts = urlsplit(url)
base = f"{parts.scheme}://{parts.netloc}"
hdr = ["HTTP/1.1 200 OK"]
if agent == os.environ.get("FAKE_REFUSE_AGENT"):
    status, body = 403, {}
elif (
    parts.path in ("/", "/signin", "/privacy")
    and parts.path not in os.environ.get("FAKE_NO_PAGE", "").split()
):
    status, body = 200, {}
    hdr = ["HTTP/1.1 200 OK", "content-type: text/html"]
elif parts.path == "/healthz":
    state = Path(os.environ["GROVE_STATE"]) / "deployed.sha"
    served = state.read_text().strip() if state.exists() else None
    build = os.environ.get("FAKE_SERVED_BUILD") or served
    status, body = 200, {"status": "ok", "store": "ok", "build": build}
    if os.environ.get("FAKE_NETWORK_STATE"):
        exec(Path(os.environ["FAKE_NETWORK_DRIVER"]).read_text())
        build = network_health()
        status = 200 if build else 503
        body = {"status": "ok" if build else "unavailable", "store": "ok", "build": build}
elif parts.path.startswith("/.well-known/oauth-protected-resource"):
    resource = os.environ.get("FAKE_RESOURCE", base + "/mcp")
    status, body = 200, {"resource": resource, "authorization_servers": [base]}
elif parts.path == "/mcp" and method == "POST":
    status, body = 401, {"error": "unauthorized"}
    metadata = os.environ.get("FAKE_MCP_METADATA", f"{base}/.well-known/oauth-protected-resource")
    hdr = ["HTTP/1.1 401 Unauthorized", f'www-authenticate: Bearer resource_metadata="{metadata}"']
else:
    status, body = 404, {}
Path(out).write_text(json.dumps(body))
Path(headers).write_text("\r\n".join(hdr) + "\r\n\r\n")
print(status, end="")
"""

_FAKE_LOGGED = r"""
import os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(os.path.basename(sys.argv[0]) + " " + " ".join(sys.argv[1:]) + "\n")
failing = os.environ.get("FAKE_FAIL_CALL")
called = os.path.basename(sys.argv[0]) + " " + " ".join(sys.argv[1:])
if failing and failing in called:
    sys.exit(1)
status = os.environ.get("FAKE_" + os.path.basename(sys.argv[0]).upper() + "_STATUS", "0")
sys.exit(int(status))
"""

# install as root sets owners; the tests run unprivileged, so the owner options are dropped.
_FAKE_INSTALL = r"""
import os, sys
args, skip = [], False
for arg in sys.argv[1:]:
    if skip:
        skip = False
    elif arg in ("-o", "-g"):
        skip = True
    else:
        args.append(arg)
os.execv(os.environ["REAL_INSTALL"], ["install", *args])
"""

# Opt-in model of the deployment regression, not a general Compose implementation. It covers
# only the networks reached by up/run, ordinary service replacement, and container orphans.
# Compose v5.6.1 selects networks by their logical label before considering a renamed resource
# (pkg/compose/observed_state.go:selectNetwork). A name-only change must not hide that match.
_NETWORK_DRIVER = r"""
import json, os, sys
from pathlib import Path
import yaml


def network_read():
    return json.loads(Path(os.environ["FAKE_NETWORK_STATE"]).read_text())


def network_save(state):
    Path(os.environ["FAKE_NETWORK_STATE"]).write_text(json.dumps(state))


def network_alive(state, service):
    container = state["containers"].get(service)
    return bool(container and container["running"] and container["healthy"] and all(
        name in state["networks"] and service in state["networks"][name]["endpoints"]
        for name in container["networks"]
    ))


def network_health():
    state = network_read()
    if network_alive(state, "api") and network_alive(state, "neo4j"):
        return state["containers"]["api"]["build"]
    return None


def network_error(state, message):
    network_save(state)
    print(message, file=sys.stderr)
    sys.exit(1)


def network_remove_container(state, service):
    container = state["containers"].pop(service, None)
    if container:
        for name in container["networks"]:
            network = state["networks"].get(name)
            if network and service in network["endpoints"]:
                network["endpoints"].remove(service)
        state["events"].append(["remove-container", service])


def network_ensure(state, config, service):
    names = []
    for key in config["services"][service]["networks"]:
        desired = config["networks"][key] or {}
        name = desired.get("name", "grove_" + key)
        options = desired.get("driver_opts", {})
        matches = [(n, v) for n, v in state["networks"].items() if v["key"] == key]
        matches.sort(key=lambda item: item[0] != name)
        if matches:
            old_name, old = matches[0]
            if old_name != name:
                # Renames in v5.6.1 disconnect observed containers. Preserve this hazard in the
                # name-only control instead of pretending an explicit name ensures isolation.
                state["events"].append(["disconnect-for-rename", old_name, list(old["endpoints"])])
                del state["networks"][old_name]
            elif old["options"] != options or old["internal"] != desired.get("internal", False):
                if old["endpoints"]:
                    network_error(state, "error while removing network: network " + name +
                                  " has active endpoints (" + ", ".join(old["endpoints"]) + ")")
                del state["networks"][name]
        if name not in state["networks"]:
            if os.environ.get("FAKE_NETWORK_FAILURE") == "create" and service == "api":
                network_error(state, "synthetic network creation failure")
            state["serial"] += 1
            state["networks"][name] = {"id": "network-" + str(state["serial"]), "key": key,
                "options": options, "internal": desired.get("internal", False), "endpoints": []}
            state["events"].append(["create-network", name])
        elif state["networks"][name]["key"] != key:
            network_error(state, "network belongs to another logical key")
        names.append(name)
    return names


def network_start(state, config, service, build):
    names = network_ensure(state, config, service)
    if service == "neo4j" and service in state["containers"]:
        return
    network_remove_container(state, service)
    state["containers"][service] = {"build": build, "running": True, "healthy": True,
                                    "networks": names}
    for name in names:
        state["networks"][name]["endpoints"].append(service)
    state["events"].append(["start-container", service, build])


def network_docker(args):
    state = network_read()
    if args[0] == "inspect":
        service = args[-1].removeprefix("container-")
        print("healthy" if network_alive(state, service) else "unhealthy")
        return True
    if args[0] != "compose":
        return False
    config = yaml.safe_load(Path(args[args.index("--file") + 1]).read_text())
    rest = args[1:]
    while rest[0].startswith("--"):
        rest = rest[2:]
    command = rest[0]
    build = os.environ.get("GROVE_BUILD_SHA", "unset")
    if command == "build":
        return True
    if command == "ps":
        if state["containers"].get(rest[-1], {}).get("running"):
            print("container-" + rest[-1])
        return True
    if command == "run" and "grove.writer.migrate" in rest:
        names = network_ensure(state, config, "api")
        live_build = state["containers"]["api"]["build"] if network_alive(state, "api") else None
        state["events"].append(["migration", build, live_build])
        for name in names:
            state["networks"][name]["endpoints"].append("migration")
        for name in names:
            state["networks"][name]["endpoints"].remove("migration")
        if os.environ.get("FAKE_NETWORK_FAILURE") == "migrate":
            network_error(state, "synthetic migration failure")
        state["migrations"].append(build)
        network_save(state)
        print(json.dumps({"applied": [], "skipped": []}))
        return True
    if command == "up":
        if "--remove-orphans" in rest:
            for service in list(state["containers"]):
                if service not in config["services"]:
                    network_remove_container(state, service)
        services = [word for word in rest[1:] if word in config["services"]]
        for service in services:
            network_start(state, config, service, build)
        if "api" in services and os.environ.get("FAKE_NETWORK_FAILURE") == "start":
            state["containers"]["api"]["healthy"] = False
            network_error(state, "synthetic target API unhealthy")
        network_save(state)
        return True
    raise AssertionError("unexpected compose operation in network regression: " + command)
"""


def _write_fake(bin_dir: Path, name: str, source: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


@dataclass
class Box:
    root: Path
    env: dict[str, str] = field(default_factory=dict)

    @property
    def src(self) -> Path:
        return self.root / "src"

    @property
    def etc(self) -> Path:
        return self.root / "etc"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def logs(self) -> Path:
        return self.root / "log"

    @property
    def nginx(self) -> Path:
        return self.root / "nginx"

    def run(self, script: str, *args: str, stdin: str | None = None, **env: str):
        return subprocess.run(
            ["bash", str(self.src / "deploy" / script), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            timeout=60,
        )

    def calls(self) -> list[str]:
        path = Path(self.env["FAKE_LOG"])
        return path.read_text().splitlines() if path.exists() else []

    def index(self, fragment: str, start: int = 0) -> int:
        """The position of the first call at or after start that holds fragment."""
        for position, line in enumerate(self.calls()[start:], start):
            if fragment in line:
                return position
        raise AssertionError(f"no call with {fragment!r}: {self.calls()}")

    def deployed(self, sha: str = SHA) -> None:
        (self.state / "deployed.sha").write_text(sha + "\n")

    def log_text(self, name: str) -> str:
        path = self.logs / name
        return path.read_text() if path.exists() else ""

    def network_state(self) -> dict:
        return json.loads(Path(self.env["FAKE_NETWORK_STATE"]).read_text())

    def network_revisions(self) -> Path:
        """Seed an already-served legacy topology; checkout selects synthetic old/new revisions.

        deploy.sh and lib.sh are unchanged since the observed legacy build. Keep that entrypoint
        open while fake git replaces files atomically; the target apply/compose comes from checkout.
        """
        revisions = self.root / "revisions"
        for sha in (SHA, OTHER_SHA):
            shutil.copytree(self.src / "deploy", revisions / sha / "deploy")
        old = revisions / OTHER_SHA / "deploy"
        config = yaml.safe_load((old / "compose.yml").read_text())
        runner_key = next(key for key in config["networks"] if key.startswith("runner"))
        config["networks"].pop(runner_key)
        config["networks"]["runner"] = {"internal": True}
        config["services"].pop("gateway")
        for service in config["services"].values():
            service["networks"] = [
                "runner" if key == runner_key else key for key in service["networks"]
            ]
            service.get("depends_on", {}).pop("gateway", None)
        (old / "compose.yml").write_text(yaml.safe_dump(config, sort_keys=False))
        apply = old / "apply.sh"
        apply.write_text(apply.read_text().replace("api gateway runner", "api runner"))
        template = old / "nginx" / "grove-https.conf.template"
        template.write_text(template.read_text() + "\n# synthetic legacy revision\n")
        shutil.copytree(old, self.src / "deploy", dirs_exist_ok=True)
        (self.root / "fake" / "HEAD").write_text(OTHER_SHA)
        self.deployed(OTHER_SHA)
        (self.etc / "google-oauth.json").chmod(0o400)
        self.env["FAKE_REVISIONS"] = str(revisions)
        driver = self.root / "fake" / "network_driver.py"
        driver.write_text(_NETWORK_DRIVER)
        self.env["FAKE_NETWORK_DRIVER"] = str(driver)
        state_path = self.root / "fake" / "networks.json"
        self.env["FAKE_NETWORK_STATE"] = str(state_path)
        networks = {}
        for key, definition in config["networks"].items():
            networks["grove_" + key] = {
                "id": "legacy-" + key,
                "key": key,
                "options": {},
                "internal": (definition or {}).get("internal", False),
                "endpoints": [],
            }
        containers = {}
        for service, definition in config["services"].items():
            names = ["grove_" + key for key in definition["networks"]]
            containers[service] = {
                "build": OTHER_SHA,
                "running": True,
                "healthy": True,
                "networks": names,
            }
            for name in names:
                networks[name]["endpoints"].append(service)
        state_path.write_text(
            json.dumps(
                {
                    "networks": networks,
                    "containers": containers,
                    "volumes": {
                        "grove-neo4j-data": "synthetic-data",
                        "grove-neo4j-logs": "synthetic-log",
                    },
                    "serial": 0,
                    "events": [],
                    "migrations": [],
                }
            )
        )
        return revisions


@pytest.fixture
def box(tmp_path: Path) -> Box:
    root = tmp_path / "box"
    shutil.copytree(DEPLOY, root / "src" / "deploy")
    for name in ("etc", "state", "log", "backups", "acme", "systemd", "logrotate", "fake"):
        (root / name).mkdir(parents=True)
    (root / "etc").chmod(0o700)
    for name in ("sites-available", "sites-enabled"):
        (root / "nginx" / name).mkdir(parents=True)
    live = root / "letsencrypt" / "live" / HOST
    live.mkdir(parents=True)
    (live / "fullchain.pem").write_text("certificate\n")

    files = {
        "api.env": f"GROVE_PUBLIC_BASE_URL={BASE}\n",
        "neo4j.env": "NEO4J_AUTH=neo4j/not-a-real-password\n",
        "runner.env": "# no token\n",
        "google-oauth.json": json.dumps({"web": {}}),
    }
    for name, text in files.items():
        path = root / "etc" / name
        path.write_text(text)
        path.chmod(0o600)

    bin_dir = root / "bin"
    bin_dir.mkdir()
    _write_fake(bin_dir, "docker", _FAKE_DOCKER)
    _write_fake(bin_dir, "git", _FAKE_GIT)
    _write_fake(bin_dir, "curl", _FAKE_CURL)
    _write_fake(bin_dir, "install", _FAKE_INSTALL)
    for name in (
        "flock",
        "nginx",
        "systemctl",
        "certbot",
        "chown",
        "getent",
        "useradd",
        "groupadd",
    ):
        _write_fake(bin_dir, name, _FAKE_LOGGED)

    real_install = shutil.which("install")
    assert real_install, "install(1) is missing"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(root),
        "LC_ALL": "C",
        "REAL_INSTALL": real_install,
        "FAKE_LOG": str(root / "calls.log"),
        "FAKE_STATE": str(root / "fake"),
        "FAKE_NEO4J_IMAGE": NEO4J_IMAGE,
        "GROVE_SRC": str(root / "src"),
        "GROVE_ETC": str(root / "etc"),
        "GROVE_STATE": str(root / "state"),
        "GROVE_LOG_DIR": str(root / "log"),
        "GROVE_BACKUP_DIR": str(root / "backups"),
        "GROVE_LOCK_FILE": str(root / "lock" / "grove-deploy.lock"),
        "GROVE_NGINX_DIR": str(root / "nginx"),
        "GROVE_SYSTEMD_DIR": str(root / "systemd"),
        "GROVE_LETSENCRYPT_DIR": str(root / "letsencrypt"),
        "GROVE_ACME_ROOT": str(root / "acme"),
        "GROVE_LOGROTATE_DIR": str(root / "logrotate"),
        "GROVE_POLL_SECONDS": "0",
        "GROVE_SMOKE_TRIES": "2",
        "GROVE_RESTORE_TRIES": "2",
    }
    return Box(root=root, env=env)
