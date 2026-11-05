"""The box's static files: the compose services, the images, nginx and the backup timer."""

import re

import yaml

from grove.api.agent_files import MAX_FILE_BYTES
from grove.api.app import create_app
from grove.api.uploads import (
    DEFAULT_MAX_BYTES,
    MAX_BYTES_VAR,
    MAX_PARALLEL_UPLOADS,
    PREFIX,
    router,
    upload_cap,
)
from grove.config import DENYLIST_VAR, RENAME_MAP_VAR
from grove.oauth import MAX_REGISTRATIONS_PER_WINDOW
from tests.deploy.conftest import DEPLOY, REPO

UPLOAD_PATH = PREFIX + "/upload"
SCREENING_MOUNT = "/run/grove/screening"

COMPOSE = yaml.safe_load((DEPLOY / "compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]
DEV_COMPOSE = yaml.safe_load((REPO / "docker-compose.dev.yml").read_text(encoding="utf-8"))
WORKFLOW = yaml.safe_load((REPO / ".github" / "workflows" / "validate.yml").read_text())


def _env_files(service: dict) -> list[str]:
    return [entry if isinstance(entry, str) else entry["path"] for entry in service["env_file"]]


def test_the_box_runs_the_same_neo4j_as_dev_and_ci():
    pin = SERVICES["neo4j"]["image"]
    assert re.fullmatch(r"neo4j:[0-9.]+-community@sha256:[0-9a-f]{64}", pin)
    assert pin == DEV_COMPOSE["services"]["neo4j"]["image"]
    assert pin == WORKFLOW["jobs"]["validate"]["services"]["neo4j"]["image"]


def test_neo4j_memory_is_set_to_the_box_sizes():
    env = SERVICES["neo4j"]["environment"]
    assert env["NEO4J_server_memory_heap_initial__size"] == "1g"
    assert env["NEO4J_server_memory_heap_max__size"] == "1g"
    assert env["NEO4J_server_memory_pagecache_size"] == "512m"
    assert env["NEO4J_db_query_default__language"] == "CYPHER_25"


def test_the_store_is_on_an_internal_network_with_no_published_port():
    assert "ports" not in SERVICES["neo4j"]
    assert SERVICES["neo4j"]["networks"] == ["store"]
    assert COMPOSE["networks"]["store"]["internal"] is True
    assert COMPOSE["volumes"]["neo4j-data"]["name"] == "grove-neo4j-data"


def test_the_api_listens_on_loopback_only_and_runs_as_production():
    assert SERVICES["api"]["ports"] == ["127.0.0.1:8000:8000"]
    env = SERVICES["api"]["environment"]
    # Without it the guard that refuses a test clock does not run (store-map open point).
    assert env["GROVE_ENV"] == "production"
    assert env["GROVE_NEO4J_URI"] == "bolt://neo4j:7687"
    assert SERVICES["api"]["read_only"] is True


def test_the_runner_holds_no_store_credential_and_no_model_key():
    runner = SERVICES["runner"]
    assert _env_files(runner) == ["/etc/grove/runner.env"]
    assert runner["networks"] == ["runner_v2"]
    assert COMPOSE["networks"]["runner_v2"]["internal"] is True
    assert "store" not in runner["networks"]
    assert runner["user"] == "10002:10002"
    assert runner["environment"]["GROVE_ENV"] == "production"
    assert "ports" not in runner


def test_the_api_reads_the_secret_files_of_the_map():
    files = _env_files(SERVICES["api"])
    assert files[:2] == ["/etc/grove/api.env", "/etc/grove/neo4j.env"]
    optional = {e["path"] for e in SERVICES["api"]["env_file"] if isinstance(e, dict)}
    assert optional == {
        "/etc/grove/model-api.env",
        "/etc/grove/modal.env",
        "/etc/grove/resend.env",
        "/etc/grove/gateway.env",
        "/etc/grove/runner-auth.env",
        "/etc/grove/glimmer.env",
    }
    assert all(e["required"] is False for e in SERVICES["api"]["env_file"] if isinstance(e, dict))
    (mount,) = [m for m in SERVICES["api"]["volumes"] if m["target"].endswith(".json")]
    assert mount["source"] == "/etc/grove/google-oauth.json"
    assert mount["read_only"] is True
    assert mount["bind"]["create_host_path"] is False


def test_every_base_image_is_pinned_by_digest():
    for name in ("api.Dockerfile", "runner.Dockerfile"):
        text = (DEPLOY / name).read_text(encoding="utf-8")
        refs = re.findall(r"^FROM (\S+)|--from=(\S+)", text, re.MULTILINE)
        images = [ref for pair in refs for ref in pair if ref]
        assert images, name
        for image in images:
            assert re.search(r"@sha256:[0-9a-f]{64}$", image), (name, image)


def test_the_runner_image_pins_opencode_and_keeps_claude_files_out():
    text = (DEPLOY / "runner.Dockerfile").read_text(encoding="utf-8")
    assert "ARG OPENCODE_VERSION=1.18.32\n" in text
    assert re.search(r"^ARG OPENCODE_SHA256=[0-9a-f]{64}$", text, re.MULTILINE)
    assert "sha256sum --check --strict" in text
    assert 'test "$(/usr/local/bin/opencode --version)" = "${OPENCODE_VERSION}"' in text
    assert "OPENCODE_DISABLE_CLAUDE_CODE=1" in text
    assert "HOME=/var/lib/grove-runner" in text
    assert "USER grove-runner" in text


def test_the_api_image_keeps_the_migrations_beside_an_editable_install():
    text = (DEPLOY / "api.Dockerfile").read_text(encoding="utf-8")
    assert "COPY server/ ./" in text
    assert "--no-editable" not in text
    assert "WORKDIR /app/server" in text
    assert "USER grove-api" in text


def test_nginx_passes_everything_to_the_api_and_reads_no_user_agent():
    https = (DEPLOY / "nginx" / "grove-https.conf.template").read_text(encoding="utf-8")
    http = (DEPLOY / "nginx" / "grove-http.conf.template").read_text(encoding="utf-8")
    for text in (https, http):
        assert "server_name __GROVE_HOST__;" in text
        assert "$http_user_agent" not in text
        assert "deny " not in text
    assert "location / {" in https
    assert https.count("proxy_pass http://127.0.0.1:8000;") == 13
    assert "root /var/www/grove-acme;" in http
    assert "return 301 https://$host$request_uri;" in http


def test_the_backup_runs_daily_and_catches_up_after_downtime():
    timer = (DEPLOY / "systemd" / "grove-backup.timer").read_text(encoding="utf-8")
    service = (DEPLOY / "systemd" / "grove-backup.service").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 03:30:00 UTC" in timer
    assert "Persistent=true" in timer
    assert "ExecStart=/opt/grove/src/deploy/backup.sh" in service


def test_no_address_or_operator_key_path_is_in_the_deploy_files():
    doc = REPO / "docs" / "deploy.md"
    assert doc.is_file()
    loopback = {"127.0.0.1", "0.0.0.0"}
    for path in [*DEPLOY.rglob("*"), doc]:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        addresses = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)) - loopback
        assert not addresses, path.name
        # The host name is enough to reach the box; the operator's own key stays on the operator's
        # machine. The box's deploy key lives under /root/.ssh/ on the box.
        assert "~/.ssh/" not in text, path.name
        assert "IdentityFile" not in text, path.name
        assert "sslip.io" not in text, path.name


# ---- review round one -------------------------------------------------------------------------

# Every key the runner service may have. A new key (privileged, devices, pid, a socket mount) is a
# change of its isolation, so it must come with a change of this list.
RUNNER_KEYS = {
    "build",
    "image",
    "restart",
    "init",
    "user",
    "env_file",
    "environment",
    "volumes",
    "networks",
    "cap_drop",
    "security_opt",
    "depends_on",
    "logging",
    "stop_grace_period",
}


def test_the_runner_service_has_no_key_beyond_its_list():
    assert set(SERVICES["runner"]) == RUNNER_KEYS


def test_the_runner_gets_the_time_to_end_its_passes_on_a_stop():
    """The loop's own stop takes up to 15 s per pass (SIGTERM 10 s, SIGKILL 5 s), then it
    removes the pass folder and ends the pass. Docker's default of 10 s cuts that short."""
    assert SERVICES["runner"]["stop_grace_period"] == "40s"


def test_the_runner_mounts_its_home_and_nothing_else():
    runner = SERVICES["runner"]
    assert runner["volumes"] == ["/var/lib/grove-runner:/var/lib/grove-runner"]
    assert runner["cap_drop"] == ["ALL"]
    assert runner["security_opt"] == ["no-new-privileges:true"]
    assert runner["environment"] == {
        "GROVE_ENV": "production",
        "GROVE_API_URL": "http://api:8000",
        "GROVE_GATEWAY_URL": "http://gateway:18300/v1",
        "GROVE_RUNNER_CONFIG": "/opt/grove/runner",
        "GROVE_OPENCODE_BIN": "/usr/local/bin/opencode",
    }


def test_the_api_drops_every_capability_and_mounts_the_oauth_file_and_the_screening_folder():
    api = SERVICES["api"]
    assert api["cap_drop"] == ["ALL"]
    assert api["security_opt"] == ["no-new-privileges:true"]
    assert [mount["source"] for mount in api["volumes"]] == [
        "/var/lib/grove-runner/worlds",
        "${GROVE_FORGET_LEDGER_HOST_DIR:?set the existing private journal directory}",
        "/etc/grove/google-oauth.json",
        "/etc/grove/screening",
        "/etc/grove/connectors",
    ]
    assert "privileged" not in api and "devices" not in api


def test_nginx_needs_no_file_from_the_box_beyond_the_certificate():
    https = (DEPLOY / "nginx" / "grove-https.conf.template").read_text(encoding="utf-8")
    assert "include " not in https
    assert "ssl_dhparam" not in https
    paths = set(re.findall(r"\s(/etc/\S+);", https))
    assert paths == {
        "/etc/letsencrypt/live/__GROVE_HOST__/fullchain.pem",
        "/etc/letsencrypt/live/__GROVE_HOST__/privkey.pem",
    }
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in https
    # No DHE cipher, so no dhparam file is needed.
    ciphers = re.search(r"ssl_ciphers (\S+);", https).group(1).split(":")
    assert ciphers and all(cipher.startswith("ECDHE-") for cipher in ciphers)


def test_the_operator_lock_frees_itself_after_one_hour_by_default():
    text = (DEPLOY / "with-deploy-lock.sh").read_text(encoding="utf-8")
    assert "cap=${GROVE_LOCK_CAP_SECONDS:-3600}" in text
    assert 'lock="${GROVE_LOCK_DIR:-$HOME/.grove/locks}/deploy.lock.d"' in text


def test_the_box_lock_is_shared_by_deploy_backup_and_restore_check():
    lib = (DEPLOY / "lib.sh").read_text(encoding="utf-8")
    assert "GROVE_LOCK_FILE=${GROVE_LOCK_FILE:-/run/grove/deploy.lock}" in lib
    for name in ("deploy.sh", "backup.sh", "restore-check.sh", "services-up.sh"):
        assert "take_lock" in (DEPLOY / name).read_text(encoding="utf-8"), name


def test_no_box_script_runs_a_one_off_container_with_stdin_open():
    for path in DEPLOY.glob("*.sh"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "compose run" in line and not line.lstrip().startswith("#"):
                assert path.name == "lib.sh" and line.rstrip().endswith("</dev/null"), path.name


def test_only_explicit_upload_routes_take_large_bodies():
    https = (DEPLOY / "nginx" / "grove-https.conf.template").read_text(encoding="utf-8")
    assert re.findall(r"client_max_body_size (\S+);", https) == [
        "10m",
        "16m",
        "70k",
        "70k",
        "4k",
        "4k",
        "32m",
        "256m",
    ]
    assert "client_max_body_size 256m;" in _location(https, f"location = {UPLOAD_PATH} {{")
    assert "client_max_body_size 16m;" in _location(https, "location = /v1/materials/files {")
    assert "client_max_body_size 32m;" in _location(https, "location = /v1/agent-files {")


# ---- review round five ------------------------------------------------------------------------


def test_neo4j_takes_its_password_from_the_env_file_only():
    neo4j = SERVICES["neo4j"]
    assert neo4j["env_file"] == ["/etc/grove/neo4j.env"]
    assert "NEO4J_AUTH" not in neo4j["environment"]
    # A clean shutdown before the nightly dump: the default of 10 s can kill the server.
    assert neo4j["stop_grace_period"] == "2m"


def test_the_images_follow_the_build_sha():
    assert SERVICES["api"]["image"].startswith("grove-api:${GROVE_BUILD_SHA")
    assert SERVICES["runner"]["image"].startswith("grove-runner:${GROVE_BUILD_SHA")
    assert SERVICES["gateway"]["image"] == SERVICES["api"]["image"]
    assert SERVICES["api"]["environment"]["GROVE_BUILD_SHA"] == "${GROVE_BUILD_SHA}"


def test_the_runner_image_gives_its_user_no_way_up():
    text = (DEPLOY / "runner.Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"(?<![A-Z_])HOME=/var/lib/grove-runner(\s|$)", text, re.MULTILINE)
    assert not re.search(r"(?<![A-Z_])HOME=(?!/var/lib/grove-runner(\s|$))", text, re.MULTILINE)
    assert re.findall(r"^USER (\S+)$", text, re.MULTILINE) == ["grove-runner"]
    assert "--shell /usr/sbin/nologin" in text
    for word in ("sudo", "docker", "--groups", " -G ", "apt-get"):
        assert word not in text.replace("runner.Dockerfile", ""), word


def _location(https: str, head: str) -> str:
    block = https[https.index(head) :]
    return block[: block.index("}") + 1]


def test_nginx_limits_the_email_code_path_and_the_four_oauth_paths_only():
    https = (DEPLOY / "nginx" / "grove-https.conf.template").read_text(encoding="utf-8")
    zone = "limit_req_zone $binary_remote_addr zone=grove_email:1m rate=10r/m;"
    assert https.index(zone) < https.index("server {")
    assert https.count("limit_req ") == 5
    block = _location(https, "location /v1/auth/email/ {")
    for line in (
        "limit_req zone=grove_email burst=10 nodelay;",
        "limit_req_status 429;",
        "proxy_pass http://127.0.0.1:8000;",
    ):
        assert line in block, line


def test_nginx_limits_tokens_and_the_authorization_request_per_address():
    https = (DEPLOY / "nginx" / "grove-https.conf.template").read_text(encoding="utf-8")
    zone = "limit_req_zone $binary_remote_addr zone=grove_oauth:1m rate=30r/m;"
    assert https.index(zone) < https.index("server {")
    for path in ("/token", "/authorize", "/authorize/decision"):
        block = _location(https, f"location = {path} {{")
        for line in (
            "limit_req zone=grove_oauth burst=20 nodelay;",
            "limit_req_status 429;",
            "proxy_pass http://127.0.0.1:8000;",
        ):
            assert line in block, (path, line)
    # The other OAuth paths go through location / with no limit of nginx.
    for path in ("/revoke", "/.well-known/"):
        assert f"location = {path}" not in https and f"location {path}" not in https


def test_nginx_limits_the_decision_as_it_limits_the_token_path():
    """Each decision can reach a document fetch, so a session cannot send them with no limit."""
    https = (DEPLOY / "nginx" / "grove-https.conf.template").read_text(encoding="utf-8")
    decision = _location(https, "location = /authorize/decision {")
    token = _location(https, "location = /token {")
    assert decision.replace("/authorize/decision", "/token") == token
    assert "zone=grove_oauth" in decision


def test_nginx_gives_registration_its_own_zone_that_one_address_cannot_fill_the_cap_with():
    https = (DEPLOY / "nginx" / "grove-https.conf.template").read_text(encoding="utf-8")
    zone = "limit_req_zone $binary_remote_addr zone=grove_register:1m rate=4r/m;"
    assert https.index(zone) < https.index("server {")
    block = _location(https, "location = /register {")
    for line in (
        "limit_req zone=grove_register burst=20 nodelay;",
        "limit_req_status 429;",
        "proxy_pass http://127.0.0.1:8000;",
    ):
        assert line in block, line
    per_minute = int(re.search(r"zone=grove_register:1m rate=(\d+)r/m;", https).group(1))
    burst = int(re.search(r"zone=grove_register burst=(\d+) nodelay;", https).group(1))
    assert per_minute * 60 + burst < MAX_REGISTRATIONS_PER_WINDOW
    assert MAX_REGISTRATIONS_PER_WINDOW == 1000


def test_nginx_has_exact_locations_and_no_rule_that_can_answer_for_the_api():
    https = (DEPLOY / "nginx" / "grove-https.conf.template").read_text(encoding="utf-8")
    assert re.findall(r"^[ \t]*location\b.*$", https, re.MULTILINE) == [
        "    location / {",
        "    location /v1/internal/ {",
        "    location /v1/auth/email/ {",
        "    location = /register {",
        "    location = /authorize {",
        "    location = /authorize/decision {",
        "    location = /token {",
        "    location = /v1/materials/files {",
        "    location = /v1/materials/notes {",
        "    location ^~ /v1/materials/notes/ {",
        "    location = /v1/materials {",
        "    location /v1/materials/ {",
        "    location = /v1/agent-files {",
        "    location = /v1/sources/upload {",
    ]
    for word in (
        "auth_basic",
        "auth_request",
        "limit_conn",
        "proxy_hide_header",
        "return ",
        "rewrite ",
        "if (",
        "deny ",
    ):
        assert word not in https.replace(_INTERNAL_BLOCK, ""), word
    # The one block nginx answers itself: the internal routes, which are not the public API.
    assert https.count(_INTERNAL_BLOCK) == 1


_INTERNAL_BLOCK = "    location /v1/internal/ {\n        return 404;\n    }\n"


def test_nginx_passes_what_the_api_and_a_stream_need():
    https = (DEPLOY / "nginx" / "grove-https.conf.template").read_text(encoding="utf-8")
    for line in (
        "proxy_http_version 1.1;",
        "proxy_set_header Host $host;",
        "proxy_set_header X-Forwarded-Proto $scheme;",
        "proxy_buffering off;",
        "proxy_read_timeout 300s;",
        'add_header Strict-Transport-Security "max-age=31536000" always;',
        "ssl_session_tickets off;",
    ):
        assert line in https, line


def test_the_units_give_each_step_the_time_it_needs():
    backup = (DEPLOY / "systemd" / "grove-backup.service").read_text(encoding="utf-8")
    services = (DEPLOY / "systemd" / "grove-services.service").read_text(encoding="utf-8")
    for line in (
        "Type=oneshot",
        "Requires=docker.service",
        "After=docker.service",
        "TimeoutStartSec=1h",
        # services-up.sh may wait for the lock, then for the store and the API.
        "TimeoutStopSec=30min",
    ):
        assert line in backup.splitlines(), line
    for line in ("Type=oneshot", "Requires=docker.service", "TimeoutStartSec=30min"):
        assert line in services.splitlines(), line


def test_the_api_image_builds_the_page_and_the_api_serves_it():
    text = (DEPLOY / "api.Dockerfile").read_text(encoding="utf-8")
    assert "COPY web/ /app/web/" in text
    assert "RUN python /app/web/build.py" in text
    assert "ENV GROVE_WEB_DIST=/app/web/dist" in text
    ignored = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "web" not in ignored
    assert "web/dist" in ignored


def test_the_image_gets_the_data_flow_file_for_the_privacy_page_and_no_other_doc():
    text = (DEPLOY / "api.Dockerfile").read_text(encoding="utf-8")
    copied = text.index("COPY docs/data-flow.md /app/docs/data-flow.md")
    assert copied < text.index("RUN python /app/web/build.py")
    ignored = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "docs/*" in ignored
    assert "docs" not in ignored
    # A later line wins in .dockerignore, so the exception must follow the rule it opens.
    assert ignored.index("!docs/data-flow.md") > ignored.index("docs/*")


# ---- uploads and the screening files ----------------------------------------------------------

_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _size_bytes(text: str) -> int:
    """A size as nginx and the kernel's tmpfs read it: a whole number with k, m or g."""
    match = re.fullmatch(r"(\d+)([kmg]?)", text.lower())
    assert match, text
    return int(match.group(1)) * _UNITS[match.group(2)]


def _https() -> str:
    return (DEPLOY / "nginx" / "grove-https.conf.template").read_text(encoding="utf-8")


def _without_comments(text: str) -> str:
    return re.sub(r"#[^\n]*", "", text)


def _location_blocks(https: str) -> dict[str, str]:
    """Each location's match, such as '= /token', with the text inside its braces."""
    pattern = r"^[ \t]*location\s+([^{]+?)\s*\{([^}]*)\}"
    return dict(re.findall(pattern, _without_comments(https), re.MULTILINE))


def test_every_api_route_at_a_prefix_location_has_its_exact_location():
    """nginx answers a path with no slash itself, with a 301 to the slash form, when only the
    prefix location proxies it. So the route never reaches the API: the person sees the 404 of the
    slash form. GET /v1/materials failed like this on the first deploy."""
    blocks = _location_blocks(_https())
    routes = set(create_app(driver=None).openapi()["paths"])
    prefixes = [
        match for match, body in blocks.items() if match.endswith("/") and "proxy_pass" in body
    ]
    assert "/v1/materials/" in prefixes
    for prefix in prefixes:
        path = prefix.split()[-1].rstrip("/")
        if path in routes:
            assert f"= {path}" in blocks, path


def test_the_exact_materials_route_keeps_the_limits_of_its_prefix():
    blocks = _location_blocks(_https())
    assert blocks["= /v1/materials"] == blocks["/v1/materials/"]


def _screening_mount() -> dict:
    (mount,) = [m for m in SERVICES["api"]["volumes"] if m["target"] == SCREENING_MOUNT]
    return mount


def test_the_api_names_both_screening_files_inside_the_screening_mount():
    env = SERVICES["api"]["environment"]
    assert env[RENAME_MAP_VAR] == f"{SCREENING_MOUNT}/rename.json"
    assert env[DENYLIST_VAR] == f"{SCREENING_MOUNT}/denylist.txt"
    # apply.sh hands these two names to the API user; any other name would stay root's at 600.
    apply = (DEPLOY / "apply.sh").read_text(encoding="utf-8")
    assert apply.count("for name in rename.json denylist.txt; do") == 2


def test_the_screening_folder_is_mounted_read_only_and_never_made_by_docker():
    mount = _screening_mount()
    assert mount["type"] == "bind"
    # The folder, not a file: a file mount would fail every deploy while that file is absent.
    assert mount["source"] == "/etc/grove/screening"
    assert mount["read_only"] is True
    assert mount["bind"]["create_host_path"] is False
    targets = [m["target"] for m in SERVICES["api"]["volumes"]]
    assert not [target for target in targets if target.startswith(SCREENING_MOUNT + "/")]


def test_no_other_service_gets_a_screening_setting_or_the_upload_cap():
    for name in ("runner", "gateway", "neo4j"):
        service = SERVICES[name]
        env = service.get("environment", {})
        for variable in (RENAME_MAP_VAR, DENYLIST_VAR, MAX_BYTES_VAR):
            assert variable not in env, (name, variable)
        assert "screening" not in str(service.get("volumes", [])), name


def test_the_api_takes_the_code_default_as_its_upload_cap(monkeypatch):
    value = SERVICES["api"]["environment"][MAX_BYTES_VAR]
    assert value == "268435456" == str(DEFAULT_MAX_BYTES)
    monkeypatch.setenv(MAX_BYTES_VAR, value)
    assert upload_cap() == DEFAULT_MAX_BYTES == 256 * 1024 * 1024


def test_the_api_tmp_holds_every_upload_spool_at_the_cap():
    """A large body spools to a file in /tmp, the one place the read-only root lets it go, and
    MAX_PARALLEL_UPLOADS of them can run at once."""
    (entry,) = SERVICES["api"]["tmpfs"]
    target, _, options = entry.partition(":")
    assert target == "/tmp"
    sizes = [option.removeprefix("size=") for option in options.split(",") if "size=" in option]
    assert len(sizes) == 1
    size = _size_bytes(sizes[0])
    assert size > MAX_PARALLEL_UPLOADS * DEFAULT_MAX_BYTES
    # No room for one more: the tmpfs is inside the api's memory limit.
    assert size < (MAX_PARALLEL_UPLOADS + 1) * DEFAULT_MAX_BYTES
    # A tmpfs is memory: with no size it may take half of the box's 8 GB.
    assert size <= 1024**3
    # tempfile writes to /tmp only while no TMPDIR points elsewhere.
    assert "TMPDIR" not in SERVICES["api"]["environment"]
    assert "TMPDIR" not in (DEPLOY / "api.Dockerfile").read_text(encoding="utf-8")


def test_the_upload_location_takes_the_api_cap_and_streams_the_body():
    block = _location_blocks(_https())[f"= {UPLOAD_PATH}"]
    directives = [line.strip() for line in block.splitlines() if line.strip()]
    assert directives == [
        "client_max_body_size 256m;",
        "proxy_request_buffering off;",
        "proxy_read_timeout 600s;",
        "proxy_send_timeout 600s;",
        "proxy_pass http://127.0.0.1:8000;",
    ]
    limit = re.search(r"client_max_body_size (\S+);", block).group(1)
    assert _size_bytes(limit) == DEFAULT_MAX_BYTES == 268435456


def test_the_upload_location_is_the_route_the_api_serves():
    served = {(route.path, method) for route in router.routes for method in route.methods}
    assert (UPLOAD_PATH, "POST") in served
    assert UPLOAD_PATH == "/v1/sources/upload"
    # An exact match, so /v1/sources/connectors and every other path keep the 10m limit.
    assert f"location = {UPLOAD_PATH} {{" in _https()


def test_every_other_path_keeps_the_10m_limit_and_request_buffering():
    https = _https()
    blocks = _location_blocks(https)
    assert len(blocks) == 14
    materials = {
        "= /v1/materials/files": "16m",
        "= /v1/materials/notes": "70k",
        "^~ /v1/materials/notes/": "70k",
        "= /v1/materials": "4k",
        "/v1/materials/": "4k",
    }
    for match, limit in materials.items():
        assert [line.strip() for line in blocks[match].splitlines() if line.strip()] == [
            f"client_max_body_size {limit};",
            "proxy_request_buffering off;",
            "proxy_pass http://127.0.0.1:8000;",
        ]
    agent_files = "= /v1/agent-files"
    assert [line.strip() for line in blocks[agent_files].splitlines() if line.strip()] == [
        "client_max_body_size 32m;",
        "proxy_request_buffering off;",
        "proxy_read_timeout 120s;",
        "proxy_send_timeout 120s;",
        "proxy_pass http://127.0.0.1:8000;",
    ]
    assert _size_bytes("32m") == MAX_FILE_BYTES
    for match, block in blocks.items():
        if match in (f"= {UPLOAD_PATH}", agent_files) or match in materials:
            continue
        for directive in ("client_max_body_size", "proxy_request_buffering", "proxy_send_timeout"):
            assert directive not in block, (match, directive)
    server_level = re.sub(r"location\s+[^{]+\{[^}]*\}", "", _without_comments(https))
    assert re.findall(r"client_max_body_size (\S+);", server_level) == ["10m"]
    assert _size_bytes("10m") == 10 * 1024 * 1024
    assert "proxy_request_buffering" not in server_level
    assert "proxy_send_timeout" not in server_level


def test_the_api_has_a_memory_limit_with_no_swap_that_holds_one_upload():
    """One upload at the caps of grove.ingest holds about 2.5 GiB. The limit keeps a worst case
    inside the api container, and leaves the store and the 8 GB box their memory."""
    api = SERVICES["api"]
    assert api["mem_limit"] == "4g"
    assert api["memswap_limit"] == api["mem_limit"]
    assert MAX_PARALLEL_UPLOADS == 1
    neo4j = SERVICES["neo4j"]["environment"]
    store = _size_bytes(neo4j["NEO4J_server_memory_heap_max__size"]) + _size_bytes(
        neo4j["NEO4J_server_memory_pagecache_size"]
    )
    assert store + _size_bytes(api["mem_limit"]) <= 6 * 1024**3
    assert api["restart"] == "unless-stopped"


# ---- the runner lane: the gateway and the runner loop -----------------------------------------

# Every key the gateway service may have: it holds the model keys and the gateway secret.
GATEWAY_KEYS = {
    "image",
    "command",
    "restart",
    "init",
    "env_file",
    "environment",
    "networks",
    "read_only",
    "tmpfs",
    "cap_drop",
    "security_opt",
    "depends_on",
    "healthcheck",
    "logging",
}


def test_the_gateway_runs_from_the_api_image_with_no_build_and_no_port():
    gateway = SERVICES["gateway"]
    assert set(gateway) == GATEWAY_KEYS
    assert gateway["image"].startswith("grove-api:${GROVE_BUILD_SHA")
    # No build key: `compose build api runner` builds the image it runs.
    assert "build" not in gateway and "ports" not in gateway
    assert gateway["command"] == ["python", "-m", "grove.models.gateway"]
    assert gateway["networks"] == ["store", "runner_v2", "egress"]
    assert gateway["read_only"] is True
    assert gateway["tmpfs"] == ["/tmp"]
    assert gateway["cap_drop"] == ["ALL"]
    assert gateway["security_opt"] == ["no-new-privileges:true"]
    assert gateway["depends_on"] == {"neo4j": {"condition": "service_healthy"}}
    assert "http://127.0.0.1:18300/health" in " ".join(gateway["healthcheck"]["test"])


def test_the_gateway_reads_the_store_file_and_its_optional_secret_files():
    env_file = SERVICES["gateway"]["env_file"]
    assert env_file == [
        "/etc/grove/neo4j.env",
        {"path": "/etc/grove/gateway.env", "required": False},
        {"path": "/etc/grove/model-api.env", "required": False},
        {"path": "/etc/grove/glimmer.env", "required": False},
    ]
    # The API mints the tokens that the gateway checks, so both read the one secret file.
    assert "/etc/grove/gateway.env" in _env_files(SERVICES["api"])
    assert "/etc/grove/gateway.env" not in _env_files(SERVICES["runner"])


def test_the_gateway_listens_where_the_runner_can_reach_it():
    env = SERVICES["gateway"]["environment"]
    assert env == {
        "GROVE_ENV": "production",
        "GROVE_NEO4J_URI": "bolt://neo4j:7687",
        "GROVE_GATEWAY_HOST": "0.0.0.0",
        "GROVE_GATEWAY_PORT": "18300",
    }
    url = SERVICES["runner"]["environment"]["GROVE_GATEWAY_URL"]
    assert url == f"http://gateway:{env['GROVE_GATEWAY_PORT']}/v1"
    assert set(SERVICES["runner"]["networks"]) <= set(SERVICES["gateway"]["networks"])
    assert SERVICES["runner"]["depends_on"] == {
        "api": {"condition": "service_healthy"},
        "gateway": {"condition": "service_started"},
    }


def test_the_runner_network_has_a_stable_bridge_name_and_no_route_out():
    runner = COMPOSE["networks"]["runner_v2"]
    assert runner["internal"] is True
    name = runner["driver_opts"]["com.docker.network.bridge.name"]
    assert name == "grove-runner"
    # Linux caps an interface name at 15 characters.
    assert len(name) <= 15


def test_the_runner_image_runs_the_loop_with_the_repo_config():
    text = (DEPLOY / "runner.Dockerfile").read_text(encoding="utf-8")
    assert re.findall(r"^CMD (.+)$", text, re.MULTILINE) == ['["python", "-m", "grove.runner"]']
    assert re.search(r"^COPY runner/ /opt/grove/runner/?$", text, re.MULTILINE)
    assert "ENV PATH=/app/server/.venv/bin:$PATH" in text
    assert "GROVE_RUNNER_CONFIG=/opt/grove/runner" in text
    assert "GROVE_OPENCODE_BIN=/usr/local/bin/opencode" in text
    assert "RUN uv sync --frozen --no-dev --no-install-project" in text
    assert "RUN uv sync --frozen --no-dev\n" in text
    assert "placeholder" not in text
    assert not (DEPLOY / "runner-placeholder.sh").exists()
    # The loop copies these into HOME before each pass, and .dockerignore must let them through.
    assert (REPO / "runner" / "opencode.json").is_file()
    assert sorted(path.name for path in (REPO / "runner" / "agents").glob("*.md"))
    ignored = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert not [line for line in ignored if line.strip("/").split("/")[0] == "runner"]


def test_the_runner_image_pins_uv_as_the_api_image_does():
    def uv_pin(name: str) -> str:
        text = (DEPLOY / name).read_text(encoding="utf-8")
        (pin,) = re.findall(r"--from=(ghcr\.io/astral-sh/uv:\S+)", text)
        return pin

    assert uv_pin("runner.Dockerfile") == uv_pin("api.Dockerfile")


def test_the_brain_and_its_shell_are_console_scripts_next_to_the_venv_python():
    pyproject = (REPO / "server" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'brain = "grove.cli.main:main"' in pyproject
    assert 'grove-brain-shell = "grove.runner.shell:main"' in pyproject


def test_nginx_answers_no_internal_route_from_outside_the_box():
    """The runner and the brain CLI reach /v1/internal on the runner network, never through
    nginx: a request from outside meets a 404 before any token is read."""
    https = (DEPLOY / "nginx" / "grove-https.conf.template").read_text(encoding="utf-8")
    block = _location(https, "location /v1/internal/ {")
    assert "return 404;" in block and "proxy_pass" not in block
    http = (DEPLOY / "nginx" / "grove-http.conf.template").read_text(encoding="utf-8")
    assert "proxy_pass" not in http


def test_the_runner_transition_uses_a_new_logical_key_and_docker_name():
    assert "runner" not in COMPOSE["networks"]
    assert COMPOSE["networks"]["runner_v2"]["name"] == "grove_runner_v2"
    assert SERVICES["api"]["networks"] == ["store", "runner_v2", "egress"]
    assert SERVICES["gateway"]["networks"] == ["store", "runner_v2", "egress"]
    assert SERVICES["runner"]["networks"] == ["runner_v2"]
