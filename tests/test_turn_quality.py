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
        ("ah", "filler_only"),
        ("aah", "filler_only"),
        ("oh", "filler_only"),
        ("yeah", "filler_only"),
        ("Yep.", "filler_only"),
        ("ya", "filler_only"),
        ("yeah yeah", "filler_only"),
        ("huh", "filler_only"),
        ("mm-hmm", "filler_only"),
        ("uh-huh", "filler_only"),
        ("[noise]", "non_speech"),
        ("(yawning)", "non_speech"),
        ("<blank audio>", "non_speech"),
        ("*coughing*", "non_speech"),
        ("four", "asr_noise"),
        ("Eight.", "asr_noise"),
        ("six", "asr_noise"),
        ("6", "asr_noise"),
        ("six a", "asr_noise"),
        ("six and a", "asr_noise"),
        ("oh four", "asr_noise"),
        ("yeah six", "asr_noise"),
        ("I", "too_short"),
        ("sí", "too_short"),
        ("music", "too_short"),
        ("noise", "too_short"),
        ("later", "too_short"),
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
    [
        "what?",
        "hello",
        "hi",
        "hey",
        "thanks",
        "sorry",
        "um, stop",
        "ah, stop",
        "there is background noise",
        "four issues",
        "chapter six",
        "hello how are you",
        "can you hear me",
    ],
)
def test_policy_allows_commands_greetings_and_solid_speech(transcript: str) -> None:
    assert TurnQualityPolicy().evaluate(transcript).should_respond is True


def test_short_noise_blip_stays_ignored_even_with_padded_looking_duration() -> None:
    decision = TurnQualityPolicy().evaluate("four", active_speech_ms=416)
    assert decision.should_respond is False
    assert decision.reason == "asr_noise"

    long_noise = TurnQualityPolicy().evaluate("six a", active_speech_ms=2000)
    assert long_noise.should_respond is False
    assert long_noise.reason == "asr_noise"


def test_one_word_needs_held_active_speech() -> None:
    policy = TurnQualityPolicy()

    short_blip = policy.evaluate("music", active_speech_ms=416)
    assert short_blip.should_respond is False
    assert short_blip.reason == "too_short"

    held_word = policy.evaluate("music", active_speech_ms=700)
    assert held_word.should_respond is True
    assert held_word.reason == "meaningful"

    held_i = policy.evaluate("I", active_speech_ms=800)
    assert held_i.should_respond is True
    assert held_i.reason == "meaningful"


def test_greeting_is_allowed_at_idle_vad_floor() -> None:
    decision = TurnQualityPolicy().evaluate("hello", active_speech_ms=448)
    assert decision.should_respond is True
    assert decision.reason == "meaningful"


def test_short_blip_with_padded_multiword_transcript_is_ignored() -> None:
    policy = TurnQualityPolicy()
    blip = policy.evaluate("can you hear", active_speech_ms=416)
    assert blip.should_respond is False
    assert blip.reason == "too_short"

    padded_phrase = policy.evaluate("four issues", active_speech_ms=416)
    assert padded_phrase.should_respond is False
    assert padded_phrase.reason == "too_short"

    real_talk = policy.evaluate("can you hear me", active_speech_ms=896)
    assert real_talk.should_respond is True
    assert real_talk.reason == "meaningful"

    barge_in = policy.evaluate("um, stop", active_speech_ms=416)
    assert barge_in.should_respond is True
    assert barge_in.reason == "protected_short_command"


def test_hide_from_client_keeps_real_sentences_visible() -> None:
    policy = TurnQualityPolicy()
    assert policy.evaluate("later", active_speech_ms=416).hide_from_client() is True
    assert policy.evaluate("four").hide_from_client() is True
    assert policy.evaluate("um").hide_from_client() is True
    assert policy.evaluate("can you hear", active_speech_ms=416).hide_from_client() is False
    assert policy.evaluate("a lot of work to do", active_speech_ms=384).hide_from_client() is False


def test_disabled_policy_allows_filler() -> None:
    decision = TurnQualityPolicy(enabled=False).evaluate("um")

    assert decision.should_respond is True
    assert decision.reason == "disabled"
