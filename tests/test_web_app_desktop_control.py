from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_voice_exposes_in_app_screenshot_without_desktop_harness():
    swift = (ROOT / "macos" / "Voice" / "Sources" / "Session" / "VoiceTools.swift").read_text()
    settings = (ROOT / "macos" / "Voice" / "Sources" / "UI" / "SettingsView.swift").read_text()
    assert '"name": "screenshot"' in swift
    assert "control_screen" not in swift
    assert '"enum": ["click", "type", "key", "hotkey", "scroll", "drag", "screenshot"]' not in swift
    assert "Use for explicit visual intent" in swift
    assert "must not use read_article" in swift
    assert "Do not click, scroll, or drive the browser" in swift
    assert "execScreenshot" in swift
    assert "ScreenCapture.mainDisplayPNG" in swift
    assert "sidecarScreenshot" in swift
    assert '"action": "screenshot"' in swift
    assert "modelImageDataURL" in swift
    assert "withTimeout" in swift
    assert "screenshotSession = session(timeout: 12)" in swift
    capture_fn = swift.split("static func mainDisplayPNG", 1)[1].split("static func pngData", 1)[0]
    assert "requestAccess" not in capture_fn
    assert "guard isAllowed else { return nil }" in capture_fn
    assert "title: \"Screenshot\"" in settings
    assert "Desktop Control" not in settings
    assert "desktop-harness" not in settings
