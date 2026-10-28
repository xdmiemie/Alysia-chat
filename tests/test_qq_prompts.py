from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from integrations.qq_astrbot.plugin.prompt_loader import (
    PromptError,
    apply_tool_descriptions,
    load_prompts,
    prompt_path,
    replace_datetime_reminders,
    replace_framework_prompts,
)


def test_editable_bundle_reloads_and_yaml_comments_are_not_model_input(tmp_path, monkeypatch):
    path = tmp_path / "prompts.yaml"
    values = load_prompts()
    path.write_text("# LOCAL COMMENT ONLY\n" + yaml.safe_dump(values, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("ASTRBOT_CHAT_PROMPTS_FILE", str(path))
    assert prompt_path() == path
    assert "LOCAL COMMENT ONLY" not in "\n".join(load_prompts().values())
    values["qq_context"] = "EDITED NEXT REQUEST"
    path.write_text(yaml.safe_dump(values, allow_unicode=True), encoding="utf-8")
    assert load_prompts()["qq_context"] == "EDITED NEXT REQUEST"


@pytest.mark.parametrize("content", ["[invalid", "- not-a-mapping", "qq_context: ''"])
def test_broken_prompt_file_is_rejected_instead_of_silently_using_other_prompts(tmp_path, content):
    path = tmp_path / "prompts.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(PromptError):
        load_prompts(path)


def test_framework_replacement_retains_persona_and_respects_inactive_features():
    prompts = load_prompts()
    prompts["astrbot_safety"] = "FILE SAFETY"
    prompts["astrbot_tool_call"] = "FILE TOOL"
    sources = {"astrbot_safety": "ORIGINAL SAFETY", "astrbot_tool_call": "ORIGINAL TOOL"}
    assert replace_framework_prompts("ORIGINAL SAFETY\nPERSONA\nORIGINAL TOOL", prompts, sources) == (
        "FILE SAFETY\nPERSONA\nFILE TOOL")
    assert replace_framework_prompts("PERSONA", prompts, sources) == "PERSONA"


def test_datetime_template_and_tool_schema_use_bundle_without_mutating_shared_schema():
    prompts = load_prompts()
    prompts["datetime_reminder"] = "日期 $current_time；星期 $weekday；文本 $100 {JSON}"
    parts = [SimpleNamespace(text="<system_reminder>Current datetime: 2026-10-28 18:30 (HKT), Weekday: Wednesday</system_reminder>"),
             SimpleNamespace(text="ordinary user data")]
    replace_datetime_reminders(parts, prompts)
    assert parts[0].text == "日期 2026-10-28 18:30 (HKT)；星期 Wednesday；文本 $100 {JSON}"
    assert parts[1].text == "ordinary user data"
    shared = {"type": "object", "properties": {"value": {"type": "string", "description": "original"}}}
    tool = SimpleNamespace(name="q2_echo", description="original", parameters=shared)
    prompts["echo_description"], prompts["echo_value_description"] = "FILE TOOL", "FILE PARAMETER"
    apply_tool_descriptions(SimpleNamespace(tools=[tool]), prompts)
    assert tool.description == "FILE TOOL"
    assert tool.parameters["properties"]["value"]["description"] == "FILE PARAMETER"
    assert shared["properties"]["value"]["description"] == "original"
