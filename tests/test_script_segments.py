"""Mixed-script replies are cut so each Kokoro pipeline only sees its own script."""

from __future__ import annotations

from chatbot.TTS.script_segments import JAPANESE, UNSUPPORTED, Segment, contains_han, segment_by_script


def test_plain_english_is_one_base_segment():
    assert segment_by_script("Hello there, how are you?", "b") == [Segment("b", "Hello there, how are you?")]


def test_chinese_name_inside_english_goes_to_the_mandarin_pipeline():
    segments = segment_by_script("Huawei, or 华为, is a company.", "b")
    assert segments == [
        Segment("b", "Huawei, or "),
        Segment(UNSUPPORTED, "华为, "),
        Segment("b", "is a company."),
    ]


def test_neutral_characters_stay_with_the_run_they_follow():
    segments = segment_by_script("小墨同学 (Xiao Mo) launched in 2024年.", "b")
    assert [s.lang_code for s in segments] == [UNSUPPORTED, "b", UNSUPPORTED]
    assert segments[0].text == "小墨同学 ("
    assert segments[1].text == "Xiao Mo) launched in 2024"
    assert segments[2].text == "年."


def test_cjk_punctuation_belongs_to_the_chinese_run():
    segments = segment_by_script("He said 「你好」 to me.", "b")
    assert segments == [Segment("b", "He said "), Segment(UNSUPPORTED, "「你好」 "), Segment("b", "to me.")]


def test_whole_chinese_reply_in_chinese_mode_is_one_segment():
    text = "华为是一家中国公司。它成立于1987年。"
    assert segment_by_script(text, UNSUPPORTED) == [Segment(UNSUPPORTED, text)]


def test_latin_inside_a_mandarin_turn_can_be_routed_to_an_english_pipeline():
    # The Mandarin front end passes raw Latin letters through as phonemes, so
    # a Chinese turn hands alphabetic runs to the English pipeline it is given.
    assert segment_by_script("我用 OpenAI 的模型。", UNSUPPORTED, letters_lang_code="b") == [
        Segment(UNSUPPORTED, "我用 "),
        Segment("b", "OpenAI "),
        Segment(UNSUPPORTED, "的模型。"),
    ]
    # Without one, everything stays in the turn's pipeline.
    assert segment_by_script("我用 OpenAI 的模型。", UNSUPPORTED) == [Segment(UNSUPPORTED, "我用 OpenAI 的模型。")]


def test_kanji_next_to_kana_is_japanese_not_mandarin():
    segments = segment_by_script("Tokyo is 東京は日本の首都です in Japanese.", "b")
    assert segments == [
        Segment("b", "Tokyo is "),
        Segment(JAPANESE, "東京は日本の首都です "),
        Segment("b", "in Japanese."),
    ]


def test_han_separated_from_kana_by_latin_stays_mandarin():
    segments = segment_by_script("华为 versus トヨタ", "b")
    assert [s.lang_code for s in segments] == [UNSUPPORTED, "b", JAPANESE]


def test_hangul_is_marked_unsupported_rather_than_spelled_out():
    segments = segment_by_script("Samsung, or 삼성, is Korean.", "b")
    assert segments == [
        Segment("b", "Samsung, or "),
        Segment(UNSUPPORTED, "삼성, "),
        Segment("b", "is Korean."),
    ]


def test_adjacent_runs_for_the_same_pipeline_merge():
    segments = segment_by_script("北京 和 上海", "b")
    assert segments == [Segment(UNSUPPORTED, "北京 和 上海")]


def test_leading_neutral_characters_join_the_first_run():
    assert segment_by_script("  ...华为", "b") == [Segment(UNSUPPORTED, "  ...华为")]
    assert segment_by_script("  ", "b") == [Segment("b", "  ")]
    assert segment_by_script("", "b") == []


def test_accented_latin_and_cyrillic_stay_with_the_base_pipeline():
    assert segment_by_script("Café Zürich München", "b") == [Segment("b", "Café Zürich München")]
    assert segment_by_script("Москва is Moscow", "b") == [Segment("b", "Москва is Moscow")]


def test_contains_han():
    assert contains_han("a 华 b")
    assert not contains_han("plain ascii and ひらがな")
