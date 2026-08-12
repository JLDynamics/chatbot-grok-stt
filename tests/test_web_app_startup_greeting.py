import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("greeting,expected", [("  Say hello.  ", 2), ("   ", 0)])
def test_websocket_startup_greeting_is_sent_once(greeting, expected):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    script = f"""
globalThis.localStorage = {{ getItem() {{ return null; }} }};
const {{ S2sWsRealtimeClient }} = await import("./web_app/ws/s2s-ws-client.js");
const client = new S2sWsRealtimeClient({{
  voice: "Ryan", instructions: "Be helpful.", directUrl: "ws://unused",
  startupGreeting: {greeting!r},
}});
const sent = [];
client._send = (event) => sent.push(event);
client.requestResponse = () => sent.push({{ type: "response.create" }});
client._sendStartupGreeting();
client._sendStartupGreeting();
if (sent.length !== {expected}) throw new Error(`unexpected count ${{sent.length}}`);
"""
    subprocess.run([node, "--input-type=module", "-e", script], cwd=ROOT, check=True)
