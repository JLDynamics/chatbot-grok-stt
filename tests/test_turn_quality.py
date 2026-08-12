import pytest

from chatbot.pipeline.turn_quality import TurnQualityPolicy


@pytest.mark.parametrize(
    ("transcript", "reason"),
    [
        ("", "no_text"),
        ("   ... ", "no_text"),
        ("um", "filler_only"),
        ("Uh...", "filler_only"),
        ("ummm, er, hmmm", "filler_only"),
        ("[noise]", "non_speech"),
        ("(yawning)", "non_speech"),
        ("<blank audio>", "non_speech"),
        ("*coughing*", "non_speech"),
    ],
)
def test_non_meaningful_transcripts_are_suppressed(transcript: str, reason: str) -> None:
    decision = TurnQualityPolicy().evaluate(transcript)

    assert decision.should_respond is False
    assert decision.reason == reason


@pytest.mark.parametrize("command", ["yes", "stop!", "wait", "no.", "help?"])
def test_important_short_commands_are_always_allowed(command: str) -> None:
    decision = TurnQualityPolicy().evaluate(command)

    assert decision.should_respond is True
    assert decision.reason == "protected_short_command"


@pytest.mark.parametrize(
    "transcript",
    ["what?", "I", "sí", "music", "noise", "um, stop", "there is background noise"],
)
def test_policy_allows_other_short_or_meaningful_speech(transcript: str) -> None:
    assert TurnQualityPolicy().evaluate(transcript).should_respond is True


def test_disabled_policy_allows_filler() -> None:
    decision = TurnQualityPolicy(enabled=False).evaluate("um")

    assert decision.should_respond is True
    assert decision.reason == "disabled"
