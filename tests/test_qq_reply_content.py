from __future__ import annotations

from copy import deepcopy

import pytest

from integrations.qq_astrbot.plugin.reply_content import chat_history, dialogue_view, has_control_markup, speech_parts, spoken_text


@pytest.mark.parametrize("text", ["sticker:开心", "自然回话~ sticker:开心", "[已发送《歌曲》原版演唱] 回话"])
def test_transport_control_markup_cannot_be_spoken_as_dialogue(text):
    assert has_control_markup(text)


def test_legacy_reasoning_is_not_read_as_seen_dialogue_and_original_record_is_preserved():
    rows = [{"role": "system", "content": "persona"}, {"role": "assistant", "content": [
        {"type": "think", "think": "private and obsolete capability judgment"}, {"type": "text", "text": "actually said"}]},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call"}]},
        {"role": "tool", "content": "tool receipt", "tool_call_id": "call"}]
    original = deepcopy(rows)
    clean = chat_history(rows)
    assert rows == original
    assert clean[1]["content"] == [{"type": "text", "text": "actually said"}]
    assert clean[2]["tool_calls"] == [{"id": "call"}] and clean[3]["tool_call_id"] == "call"
    assert dialogue_view(rows) == [{"role": "assistant", "content": "actually said"}]


@pytest.mark.parametrize('face', ['(๑•̀ㅂ•́)و✧', '(T_T)', '(0_0)', '😊', '❤️'])
def test_faces_remain_text_and_never_become_spoken_digits(face):
    assert speech_parts(face) == [(face, False)]
    assert spoken_text(face) == ''
    assert speech_parts('准备中哦~ ' + face) == [('准备中哦~', True), (face, False)]
    assert spoken_text('准备中哦~ ' + face) == '准备中哦~'


def test_parenthetical_words_and_numbers_are_still_spoken():
    assert spoken_text('唱雨爱（司凤版），音量（1.5）。') == '唱雨爱（司凤版），音量（1.5）。'


def test_history_removes_internal_receipts_but_preserves_real_words_and_user_input():
    rows = [
        {'role': 'assistant', 'content': '[已发送《雨爱》整首翻唱] 唱好啦'},
        {'role': 'assistant', 'content': '[已发送《歌曲》原唱] '},
        {'role': 'assistant', 'content': '[{"type":"text","text":"[已发送《歌曲》原唱] 听听看"}]'},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '[已发送《歌曲》原唱] 回话'}]},
        {'role': 'user', 'content': '你怎么发了[已发送《雨爱》整首翻唱]'},
    ]
    original = deepcopy(rows)
    assert chat_history(rows) == [
        {'role': 'assistant', 'content': '唱好啦'},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '听听看'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '回话'}]},
        original[-1],
    ]
    assert rows == original


def test_checker_dialogue_uses_actual_user_text_without_transport_envelope():
    import json
    rows = [{'role': 'user', 'content': [
        {'type': 'text', 'text': json.dumps({'current_message': {'text': '我想听你唱雨爱', 'message_id': 'private-id'}})},
        {'type': 'text', 'text': '<system_reminder>Current datetime: old</system_reminder>'}]}]
    assert dialogue_view(rows) == [{'role': 'user', 'content': '我想听你唱雨爱'}]


def test_protocol_content_and_native_tool_pairs_are_preserved_while_dialogue_view_extracts_words():
    raw = '{\n"reply":"保留 sticker:标签 与字段", "thinking_level":4, "voice":true\n}'
    rows = [{'role': 'assistant', 'content': 'ignored sticker:tool-stage', 'tool_calls': [
        {'id': 'call1', 'type': 'function', 'function': {'name': 'weapi', 'arguments': '{"query":"song"}'}}]},
        {'role': 'tool', 'tool_call_id': 'call1', 'content': '{"ok":true}'},
        {'role': 'assistant', 'content': raw},
        {'role': 'assistant', 'content': '{"follow_up":"补发一句","thinking_level":2}'}]
    assert chat_history(rows) == rows
    assert dialogue_view(rows) == [{'role': 'assistant', 'content': '保留 sticker:标签 与字段'},
                                   {'role': 'assistant', 'content': '补发一句'}]


def test_protocol_view_bounds_whole_turns_without_truncating_json_or_orphaning_tools():
    from integrations.qq_astrbot.plugin.reply_content import protocol_view
    rows = [{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'old answer'},
        {'role': 'user', 'content': 'current'}, {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call', 'type': 'function', 'function': {'name': 'weapi', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call', 'content': '{"data":"' + 'x' * 1400 + '"}'},
        {'role': 'assistant', 'content': '{"reply":"ready","thinking_level":2,"voice":true}'}]
    assert protocol_view(rows, max_turns=1) == rows[2:]
