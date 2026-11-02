"""The instruction files ship inside the package, so a wheel install finds them.

The editable install of the dev tree reads the source folder, which proves nothing about a wheel.
So one test builds the wheel, unpacks it, and loads the files through importlib.resources in a
fresh interpreter that sees only the unpacked copy of the package.
"""

import os
import shutil
import subprocess
import sys
import zipfile
from importlib.resources import files

from grove.prompts import DATA_RULES, build_prompt
from grove.prompts.slots import INSTRUCTIONS
from tests.prompts.conftest import REPO, RULES_BYTES

SERVER = REPO / "server"

_PROBE = """
import sys
from pathlib import Path

site, source = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(site))

from importlib.resources import files

import grove.prompts
from grove.prompts import DATA_RULES, build_prompt

assert Path(grove.prompts.__file__).is_relative_to(site), "the probe loaded the dev tree"
for path in sorted(source.rglob("*")):
    if path.is_file():
        shipped = files("grove.prompts").joinpath("instructions", *path.relative_to(source).parts)
        assert shipped.read_bytes() == path.read_bytes(), "a shipped file differs"
prompt = build_prompt(DATA_RULES, [])
rules = (source / (DATA_RULES + ".md")).read_text(encoding="utf-8")
assert prompt.instructions == rules + "\\n" + rules, "the shipped builder read other rules"
print("shipped")
"""


def test_the_rules_load_through_importlib_resources():
    shipped = files("grove.prompts").joinpath("instructions", f"{DATA_RULES}.md")
    assert shipped.is_file()
    assert shipped.read_bytes() == (REPO / INSTRUCTIONS / f"{DATA_RULES}.md").read_bytes()
    assert build_prompt(DATA_RULES, []).instructions.encode("utf-8").startswith(RULES_BYTES)


def test_the_wheel_ships_every_instruction_file(tmp_path):
    uv = os.environ.get("UV") or shutil.which("uv")
    assert uv, "uv must be on PATH: the wheel test builds with it"
    built = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path / "dist"), str(SERVER)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert built.returncode == 0, built.stderr[-2000:]
    (wheel,) = (tmp_path / "dist").glob("grove-*.whl")

    source = REPO / INSTRUCTIONS
    expected = {
        f"grove/prompts/instructions/{path.relative_to(source).as_posix()}"
        for path in source.rglob("*")
        if path.is_file()
    }
    with zipfile.ZipFile(wheel) as archive:
        assert expected and expected <= set(archive.namelist())
        archive.extractall(tmp_path / "site")

    probe = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(tmp_path / "site"), str(source)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert probe.returncode == 0, probe.stderr[-2000:]
    assert probe.stdout.strip() == "shipped"
