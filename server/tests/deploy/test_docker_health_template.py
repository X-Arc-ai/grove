"""Evaluate the real inspect projection with synthetic metadata, without Docker."""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[3] / "deploy/cutover.py"
spec = importlib.util.spec_from_file_location("health_template_cutover", MODULE)
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)

EVALUATOR = r'''
package main

import (
    "encoding/json"
    "os"
    "text/template"
)

func main() {
    var input struct {
        Template string
        Data map[string]any
    }
    if err := json.NewDecoder(os.Stdin).Decode(&input); err != nil { panic(err) }
    funcs := template.FuncMap{"json": func(value any) (string, error) {
        data, err := json.Marshal(value)
        return string(data), err
    }}
    t, err := template.New("inspect").Option("missingkey=error").Funcs(funcs).Parse(input.Template)
    if err != nil { panic(err) }
    if err := t.Execute(os.Stdout, input.Data); err != nil { panic(err) }
}
'''


@pytest.fixture(scope="module")
def evaluate_template(tmp_path_factory):
    go = shutil.which("go")
    if go is None:
        pytest.skip("Go is required for actual inspect template evaluation")
    root = tmp_path_factory.mktemp("health-template")
    source = root / "main.go"
    source.write_text(EVALUATOR)
    binary = root / "evaluate"
    env = {
        **os.environ,
        "GOMAXPROCS": "2",
        "GOCACHE": str(root / "cache"),
        "GOPATH": str(root / "gopath"),
        "GOTOOLCHAIN": "local",
        "GOENV": "off",
    }
    subprocess.run(
        [go, "build", "-p", "2", "-o", str(binary), str(source)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    def evaluate(template, data):
        return subprocess.run(
            [str(binary)],
            input=json.dumps({"Template": template, "Data": data}),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout

    return evaluate


@pytest.mark.parametrize("health", [None, "healthy"], ids=["absent-health", "healthy"])
def test_inspect_optional_health_preserves_safe_projection(evaluate_template, health):
    metadata = {
        "Id": "synthetic-runner",
        "Image": "sha256:" + "1" * 64,
        "Config": {
            "Labels": {
                "com.docker.compose.project": "grove",
                "com.docker.compose.service": "runner",
                "com.docker.compose.oneoff": "False",
            },
            "Env": ["SYNTHETIC_PRIVATE_VALUE=never-project-this"],
        },
        "State": {"Running": True, "Status": "running"},
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}},
        "NetworkSettings": {"Networks": {"grove_runner_v2": {"NetworkID": "synthetic"}}},
    }
    if health is not None:
        metadata["State"]["Health"] = {"Status": health}

    def run(*args):
        assert args[:3] == ("docker", "inspect", "--format")
        assert args[4:] == ("synthetic-runner",)
        assert ".Config.Env" not in args[3]
        return evaluate_template(args[3], metadata)

    guard = cutover.Cutover.__new__(cutover.Cutover)
    guard.run = run
    assert guard.inspect("synthetic-runner") == {
        "id": metadata["Id"],
        "image": metadata["Image"],
        "project": "grove",
        "service": "runner",
        "oneoff": "False",
        "running": True,
        "status": "running",
        "health": health,
        "restart": metadata["HostConfig"]["RestartPolicy"],
        "networks": metadata["NetworkSettings"]["Networks"],
    }


def test_strict_evaluator_rejects_original_missing_health_traversal(evaluate_template):
    original = '{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}'
    with pytest.raises(subprocess.CalledProcessError) as failure:
        evaluate_template(original, {"State": {"Running": True, "Status": "running"}})
    assert 'map has no entry for key "Health"' in failure.value.stderr
