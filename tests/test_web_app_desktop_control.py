import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_control_schema_exposes_screenshot_and_removes_old_browser_capture_node():
    main = (ROOT / "web_app" / "main.js").read_text()
    html = (ROOT / "web_app" / "index.html").read_text()
    assert 'enum: ["click", "type", "key", "hotkey", "scroll", "drag", "screenshot"]' in main
    assert "Use screenshot " in main
    assert "for explicit visual intent" in main
    assert "text; use read_article" in main
    assert 'id="screen-video"' not in html
    assert 'id="cam-video"' in html


def test_desktop_control_toggle_registers_and_removes_tool_from_fresh_state():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    script = """
import {
  addDesktopControlTool,
  bindDesktopControlToggle,
  readDesktopControlPreference,
} from "./web_app/ui/desktop-control.js";

class FakeCheckbox extends EventTarget {
  checked = false;
  disabled = false;
}
const row = { classList: { toggle() {} } };
const hint = { textContent: "" };
const input = new FakeCheckbox();
const storage = new Map();
let preferences = {
  desktop_control: readDesktopControlPreference({}),
};
let serverAvailable = true;

const activeNames = () => {
  const definitions = [];
  addDesktopControlTool(
    definitions,
    { name: "control_screen" },
    preferences.desktop_control,
    serverAvailable,
  );
  return definitions.map((tool) => tool.name);
};

const binding = bindDesktopControlToggle({
  input,
  row,
  hint,
  getPreferred: () => preferences.desktop_control,
  getServerAvailable: () => serverAvailable,
  onPreferenceChange(enabled) {
    preferences.desktop_control = enabled;
    storage.set("s2s.ws.tools", JSON.stringify(preferences));
  },
});

if (input.checked || activeNames().length) throw new Error("fresh state must start disabled");
input.checked = true;
input.dispatchEvent(new Event("change"));
if (!input.checked) throw new Error("checkbox did not remain enabled");
if (JSON.stringify(activeNames()) !== '["control_screen"]') throw new Error("tool was not registered");
if (!JSON.parse(storage.get("s2s.ws.tools")).desktop_control) throw new Error("enabled state was not persisted");

input.checked = false;
input.dispatchEvent(new Event("change"));
if (activeNames().length) throw new Error("tool was not removed");
if (JSON.parse(storage.get("s2s.ws.tools")).desktop_control) throw new Error("disabled state was not persisted");

if (!readDesktopControlPreference({ read_screen: true })) throw new Error("legacy preference was not migrated");
if (readDesktopControlPreference({ desktop_control: false, read_screen: true })) {
  throw new Error("modern preference must override the legacy value");
}

preferences.desktop_control = true;
serverAvailable = false;
binding.sync();
if (!input.disabled || input.checked) throw new Error("unavailable server must disable and clear the visible toggle");
input.checked = true;
input.dispatchEvent(new Event("change"));
if (activeNames().length) throw new Error("server-disabled tool must not register");
"""
    subprocess.run([node, "--input-type=module", "-e", script], cwd=ROOT, check=True)
