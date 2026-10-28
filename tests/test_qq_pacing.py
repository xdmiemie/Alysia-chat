from __future__ import annotations

from integrations.qq_astrbot.plugin.reply_pacing import (
    DEFAULT_LEVEL,
    THINKING_SECONDS,
    parse_followup,
    parse_reply,
    remaining_seconds,
    split_sentences,
    typing_seconds,
)


def test_structured_level_and_visible_reply():
    assert parse_reply('{"reply":"我刚吃过，大米饭太好吃了！","thinking_level":1}') == (
        "我刚吃过，大米饭太好吃了！", 1, True)
    assert parse_reply('{"reply":"在呀","thinking_level":true}') == ("在呀", DEFAULT_LEVEL, False)
    assert parse_reply("在呀") == ("在呀", DEFAULT_LEVEL, False)


def test_cjk_ideographs_and_other_characters_have_distinct_rates():
    assert typing_seconds("中文，A!") == 2 * 0.4 + 3 * 0.12


def test_elapsed_model_time_is_subtracted_and_never_goes_negative():
    reply = "中文，A!"
    target = THINKING_SECONDS[1] + typing_seconds(reply)
    assert abs(remaining_seconds(reply, 1, 100.0, now=100.7) - (target - 0.7)) < 1e-9
    assert remaining_seconds(reply, 1, 100.0, now=100.0 + target + 1) == 0


def test_user_example_uses_written_non_chinese_rate():
    reply = "我刚吃过，大米饭太好吃了！"
    assert abs(remaining_seconds(reply, 1, 100.0, now=100.7) - 4.44) < 1e-9


def test_sentence_split_preserves_punctuation_and_order():
    assert split_sentences("我好像不太会唱呢。\n你想听什么？  我可以试试！") == [
        "我好像不太会唱呢。", "你想听什么？", "我可以试试！"]
    assert split_sentences("价格是3.14元。真的！") == ["价格是3.14元。", "真的！"]
    assert split_sentences("“早上好！”你醒了吗？") == ["“早上好！”", "你醒了吗？"]


def test_second_inference_decision_is_nullable_and_one_sentence_only():
    assert parse_followup('{"follow_up":null,"thinking_level":1}', "睡得很香") == (None, 1, True)
    assert parse_followup('{"follow_up":"你呢，睡得好吗？我想听听。","thinking_level":2}', "睡得很香") == (
        "你呢，睡得好吗？", 2, True)
    assert parse_followup('{"follow_up":"睡得很香","thinking_level":2}', "睡得很香") == (None, 2, True)
    assert parse_followup("你呢？", "睡得很香") == (None, DEFAULT_LEVEL, False)


def test_cumulative_typing_targets_for_multiple_messages():
    first, second = split_sentences("吃过啦。你呢？")
    first_target = THINKING_SECONDS[1] + typing_seconds(first)
    second_target = THINKING_SECONDS[1] + typing_seconds(first + second)
    assert second_target > first_target
    assert remaining_seconds(first + second, 1, 100, now=100 + second_target + 1) == 0
