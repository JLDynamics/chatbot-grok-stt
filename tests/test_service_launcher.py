"""Exercise launcher ownership without models, network ports, or personal config."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

VOICE_PORT = "18766"
WEB_PORT = "17860"


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
    # The real launcher recognises its services by command line ("chatbot
    # serve", "server:app"), so the fakes must present the same. The voice
    # child inherits WEB_PORT too, so it explicitly uses its own PORT.
    (binaries / "chatbot").write_text(service.replace("${WEB_PORT:-$PORT}", "$PORT"))
    (tmp_path / "run-openrouter.sh").write_text('#!/bin/bash\nexec "$TEST_STATE/bin/chatbot" serve --port "$PORT"\n')
    (venv / "uvicorn").write_text(service)
    (binaries / "lsof").write_text("""#!/bin/bash
for arg in "$@"; do
  case "$arg" in TCP:*) cat "$TEST_STATE/${arg#TCP:}" 2>/dev/null || true ;; esac
done
""")
    # Stands in for scripts/service_state.py (tested on its own): the health
    # verdict for a port is whatever the test wrote to state-<port>.
    (binaries / "python3").write_text("""#!/bin/bash
url="$2"
port="${url#http://127.0.0.1:}"
cat "$TEST_STATE/state-${port%%/*}" 2>/dev/null || echo unreachable
""")
    for file in [
        tmp_path / "run-openrouter.sh",
        venv / "uvicorn",
        binaries / "lsof",
        binaries / "python3",
        binaries / "chatbot",
    ]:
        file.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{binaries}:/usr/bin:/bin",
        "CHATBOT_ENV": str(tmp_path / "no-personal-config"),
        "TEST_STATE": str(tmp_path),
        "PORT": VOICE_PORT,
        "WEB_PORT": WEB_PORT,
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

    def start_service(port):
        """A Chatbot-looking service nobody's launcher owns (started by hand)."""
        if port == WEB_PORT:
            command = [str(venv / "uvicorn"), "--app-dir", "web_app", "server:app", "--port", port]
        else:
            command = [str(binaries / "chatbot"), "serve", "--port", port]
        process = subprocess.Popen(
            command,
            env={**environment, "PORT": port, "WEB_PORT": port},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        wait_for(lambda: (tmp_path / port).exists())
        return process

    yield tmp_path, start, start_service
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
    directory, start, _ = launcher
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


def test_sidecar_only_starts_web_without_voice(launcher):
    directory, start, _ = launcher
    process = start("--sidecar-only")
    wait_for(lambda: (directory / "17860").exists())
    assert not (directory / "18766").exists()
    process.terminate()
    assert process.wait(timeout=5) == 143
    wait_for(lambda: not (directory / "17860").exists())


def test_sidecar_only_reuses_existing_web(launcher):
    directory, start, _ = launcher
    (directory / "17860").write_text("external-service")
    process = start("--reuse-running", "--sidecar-only")
    assert process.wait(timeout=5) == 0
    assert (directory / "17860").read_text() == "external-service"
    assert not (directory / "18766").exists()


def test_normal_start_refuses_an_occupied_port(launcher):
    directory, start, _ = launcher
    (directory / "18766").write_text("external-service")
    assert start().wait(timeout=5) == 1
    assert (directory / "18766").read_text() == "external-service"
    assert not (directory / "17860").exists()


def test_sidecar_starts_without_waiting_for_voice(launcher):
    directory, start, _ = launcher
    (directory / "run-openrouter.sh").write_text(
        """#!/bin/bash
marker="$TEST_STATE/$PORT"
sleep 1
echo $$ > "$marker"
trap 'rm -f "$marker"; exit 0' TERM INT
while :; do sleep 0.1; done
"""
    )
    (directory / "run-openrouter.sh").chmod(0o755)
    process = start("--reuse-running")
    wait_for(lambda: (directory / "17860").exists())
    assert not (directory / "18766").exists()
    wait_for(lambda: (directory / "18766").exists())
    process.terminate()
    assert process.wait(timeout=5) == 143


def pid_in(marker: Path) -> int:
    return int(marker.read_text().strip())


def test_reuse_keeps_a_current_service(launcher):
    directory, start, start_service = launcher
    voice = start_service(VOICE_PORT)
    (directory / f"state-{VOICE_PORT}").write_text("current\n")
    process = start("--reuse-running")
    wait_for(lambda: (directory / WEB_PORT).exists())
    assert pid_in(directory / VOICE_PORT) == voice.pid
    assert voice.poll() is None
    process.terminate()
    assert process.wait(timeout=5) == 143
    # The launcher only ever stops what it started.
    assert voice.poll() is None
    assert pid_in(directory / VOICE_PORT) == voice.pid


@pytest.mark.parametrize("verdict", ["stale", "unknown", "foreign", None])
def test_reuse_replaces_a_service_that_is_not_running_this_checkouts_code(launcher, verdict):
    """stale = disk changed under it; unknown = predates fingerprints; foreign =
    another worktree; None = listening but never answers (hung)."""
    directory, start, start_service = launcher
    old = start_service(VOICE_PORT)
    if verdict is not None:
        (directory / f"state-{VOICE_PORT}").write_text(f"{verdict}\n")
    process = start("--reuse-running")
    wait_for(lambda: old.poll() is not None)
    wait_for(lambda: (directory / VOICE_PORT).exists() and pid_in(directory / VOICE_PORT) != old.pid)
    wait_for(lambda: (directory / WEB_PORT).exists())
    process.terminate()
    assert process.wait(timeout=5) == 143
    wait_for(lambda: not (directory / VOICE_PORT).exists())


def test_reuse_stops_the_launcher_owning_a_stale_service_and_restarts_its_sibling_too(launcher):
    directory, start, _ = launcher
    first = start("--reuse-running")
    wait_for(lambda: all((directory / port).exists() for port in (VOICE_PORT, WEB_PORT)))
    old_pids = {port: pid_in(directory / port) for port in (VOICE_PORT, WEB_PORT)}
    # The sidecar is fine; the voice backend runs code that has since changed.
    (directory / f"state-{VOICE_PORT}").write_text("stale\n")
    (directory / f"state-{WEB_PORT}").write_text("current\n")
    second = start("--reuse-running")
    # Stopping the first launcher takes both of its services down cleanly...
    assert first.wait(timeout=10) == 143
    # ...and the second launcher brings both back under its own supervision.
    wait_for(
        lambda: all(
            (directory / port).exists() and pid_in(directory / port) != old_pids[port]
            for port in (VOICE_PORT, WEB_PORT)
        )
    )
    second.terminate()
    assert second.wait(timeout=5) == 143
    wait_for(lambda: not any((directory / port).exists() for port in (VOICE_PORT, WEB_PORT)))


def test_sidecar_only_replaces_a_stale_sidecar(launcher):
    directory, start, start_service = launcher
    old = start_service(WEB_PORT)
    (directory / f"state-{WEB_PORT}").write_text("stale\n")
    process = start("--reuse-running", "--sidecar-only")
    wait_for(lambda: old.poll() is not None)
    wait_for(lambda: (directory / WEB_PORT).exists() and pid_in(directory / WEB_PORT) != old.pid)
    assert not (directory / VOICE_PORT).exists()
    process.terminate()
    assert process.wait(timeout=5) == 143


def test_normal_start_still_refuses_a_stale_occupant(launcher):
    directory, start, start_service = launcher
    old = start_service(VOICE_PORT)
    (directory / f"state-{VOICE_PORT}").write_text("stale\n")
    assert start().wait(timeout=5) == 1
    assert old.poll() is None
