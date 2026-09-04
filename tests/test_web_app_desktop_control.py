from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_control_schema_exposes_screenshot_without_browser_capture():
    swift = (ROOT / "macos" / "Voice" / "Sources" / "Session" / "VoiceTools.swift").read_text()
    assert '"enum": ["click", "type", "key", "hotkey", "scroll", "drag", "screenshot"]' in swift
    assert "Use screenshot for explicit visual intent" in swift
    # Visual requests still never route to the article reader.
    assert "must not use read_article" in swift
    # Reaching the screen for page text is a last resort, and read-only:
    # scrolling to see more is fine, dismissing a wall for the user is not.
    assert "reach this tool only once web_fetch and read_article have both failed" in swift
    assert "use action screenshot and action scroll only, never click, type, drag, or key" in swift
