"""Exercise launcher ownership without models, network ports, or personal config."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


@pytest.fixture
def launcher(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copy(root / "run-browser.sh", tmp_path)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    venv = tmp_path / ".venv/bin"
    venv.mkdir(parents=True)
    service = """#!/bin/bash
marker="$TEST_STATE/${WEB_PORT:-$PORT}"
echo $$ > "$marker"
trap 'rm -f "$marker"; exit 0' TERM INT
while :; do sleep 0.1; done
"""
    # The voice child inherits WEB_PORT too, so explicitly use its own PORT.
    (tmp_path / "run-openrouter.sh").write_text(service.replace("${WEB_PORT:-$PORT}", "$PORT"))
    (venv / "uvicorn").write_text(service)
    (binaries / "lsof").write_text("""#!/bin/bash
for arg in "$@"; do
  case "$arg" in TCP:*) cat "$TEST_STATE/${arg#TCP:}" 2>/dev/null || true ;; esac
done
""")
    for file in [tmp_path / "run-openrouter.sh", venv / "uvicorn", binaries / "lsof"]:
        file.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{binaries}:/usr/bin:/bin",
        "CHATBOT_ENV": str(tmp_path / "no-personal-config"),
        "TEST_STATE": str(tmp_path),
        "PORT": "18766",
        "WEB_PORT": "17860",
        "SERVER_LOG": str(tmp_path / "server.log"),
        "WEB_LOG": str(tmp_path / "web.log"),
    }
    processes = []

    def start(*args):
        process = subprocess.Popen(
            ["/bin/bash", str(tmp_path / "run-browser.sh"), *args],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        return process

    yield tmp_path, start
    for process in processes:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate()


@pytest.mark.parametrize("existing", [(), ("18766",), ("17860",), ("18766", "17860")])
def test_reuse_starts_only_missing_services_and_preserves_external(launcher, existing):
    directory, start = launcher
    for port in existing:
        (directory / port).write_text("external-service")
    process = start("--reuse-running")
    wait_for(lambda: all((directory / port).exists() for port in ("18766", "17860")))
    if len(existing) == 2:
        assert process.wait(timeout=5) == 0
    else:
        process.terminate()
        assert process.wait(timeout=5) == 143
    for port in ("18766", "17860"):
        marker = directory / port
        if port in existing:
            assert marker.read_text() == "external-service"
        else:
            wait_for(lambda: not marker.exists())


def test_normal_start_refuses_an_occupied_port(launcher):
    directory, start = launcher
    (directory / "18766").write_text("external-service")
    assert start().wait(timeout=5) == 1
    assert (directory / "18766").read_text() == "external-service"
    assert not (directory / "17860").exists()
