from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_control_schema_exposes_screenshot_without_browser_capture():
    swift = (ROOT / "macos" / "Voice" / "Sources" / "Session" / "VoiceTools.swift").read_text()
    assert '"enum": ["click", "type", "key", "hotkey", "scroll", "drag", "screenshot"]' in swift
    assert "Use screenshot for explicit visual intent" in swift
    assert "text; use read_article" in swift
    assert "must not use read_article" in swift
