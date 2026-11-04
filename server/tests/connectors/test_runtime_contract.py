from pathlib import Path


def test_api_image_installs_git_for_the_local_clone_reader():
    dockerfile = Path(__file__).resolve().parents[3] / "deploy" / "api.Dockerfile"
    text = dockerfile.read_text()
    assert "apt-get install -y --no-install-recommends git" in text
    assert text.index("apt-get install") < text.index("USER grove-api")
