from copy import deepcopy

import pytest

from integrations.qq_astrbot.plugin.model_messages import normalize_messages, protocol_prompt
from integrations.qq_astrbot.plugin.prompt_loader import PromptError, load_prompts


def test_late_system_messages_merge_without_changing_dialogue_tools_or_source():
    messages = [{"role": "system", "content": "persona"},
        {"role": "user", "content": "user data"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "type": "function",
            "function": {"name": "weapi", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c", "content": "untrusted result"},
        {"role": "system", "content": [{"type": "text", "text": "current state"}]}]
    original = deepcopy(messages)
    result = normalize_messages(messages, system_prompt="framework", protocol="protocol")
    assert result[0] == {"role": "system", "content": "framework\n\npersona\n\ncurrent state\n\nprotocol"}
    assert result[1:] == original[1:-1]
    assert messages == original
    assert normalize_messages(result, protocol="protocol") == result


@pytest.mark.parametrize("profile", ["qwen3", "qwen35"])
def test_model_profiles_only_change_protocol_guidance(profile):
    shared, selected = load_prompts(), load_prompts(profile=profile)
    assert {k for k in shared if shared[k] != selected[k]} == {"model_protocol"}
    assert protocol_prompt({"prompt_profile": profile}) == selected["model_protocol"]


def test_invalid_profile_and_nontext_system_fail_explicitly():
    with pytest.raises(PromptError):
        load_prompts(profile="missing")
    with pytest.raises(ValueError):
        normalize_messages([{"role": "system", "content": [{"type": "image_url", "image_url": "x"}]}])


def test_thinking_parts_use_protocol_field_for_template_and_provider():
    messages = [{"role": "assistant", "content": [{"type": "think", "think": "scratch"},
        {"type": "text", "text": "tool-stage text"}], "tool_calls": [{"id": "c"}]}]
    result = normalize_messages(messages)
    assert result[0]["reasoning_content"] == "scratch"
    assert result[0]["content"] == [{"type": "text", "text": "tool-stage text"}]
    assert result[0]["tool_calls"] == messages[0]["tool_calls"]
    assert messages[0]["content"][0]["type"] == "think"
