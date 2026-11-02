"""The box's static files: the compose services, the images, nginx and the backup timer."""

import re

import yaml

from tests.deploy.conftest import DEPLOY, REPO

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
    assert runner["networks"] == ["runner"]
    assert COMPOSE["networks"]["runner"]["internal"] is True
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
    }
    (mount,) = SERVICES["api"]["volumes"]
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
    # The two locations differ only in the body limit: every path reaches the API.
    assert re.findall(r"location (\S+) \{", https) == ["/", "/v1/sources/"]
    assert https.count("proxy_pass http://127.0.0.1:8000;") == 2
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
}


def test_the_runner_service_has_no_key_beyond_its_list():
    assert set(SERVICES["runner"]) == RUNNER_KEYS


def test_the_runner_mounts_its_home_and_nothing_else():
    runner = SERVICES["runner"]
    assert runner["volumes"] == ["/var/lib/grove-runner:/var/lib/grove-runner"]
    assert runner["cap_drop"] == ["ALL"]
    assert runner["security_opt"] == ["no-new-privileges:true"]
    assert runner["environment"] == {"GROVE_ENV": "production", "GROVE_API_URL": "http://api:8000"}


def test_the_api_drops_every_capability_and_mounts_one_file():
    api = SERVICES["api"]
    assert api["cap_drop"] == ["ALL"]
    assert api["security_opt"] == ["no-new-privileges:true"]
    assert [mount["source"] for mount in api["volumes"]] == ["/etc/grove/google-oauth.json"]
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


def test_only_the_upload_path_takes_a_large_body():
    https = (DEPLOY / "nginx" / "grove-https.conf.template").read_text(encoding="utf-8")
    assert re.findall(r"client_max_body_size (\S+);", https) == ["10m", "256m"]
    upload = https[https.index("location /v1/sources/") :]
    assert "client_max_body_size 256m;" in upload
    assert "client_max_body_size 256m;" not in https[: https.index("location /v1/sources/")]
