"""Read the editable QQ prompt bundle at request time; comments stay local."""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import re

import yaml


REQUIRED_PROMPTS = frozenset({
    "qq_context", "reply_output", "model_protocol",
    "astrbot_safety", "astrbot_tool_call", "datetime_reminder",
    "echo_description", "echo_value_description",
    "music_result", "music_selection", "music_clarification",
    "agent_policy", "agent_reply_regenerate", "followup_system", "followup_user", "followup_phase",
    "agent_budget_reply", "tool_emotion", "tool_musiccover", "tool_music",
    "tool_sticker", "tool_weapi", "tool_speech_text", "tool_emotion_value", "tool_song_id",
    "tool_sticker_tag", "tool_weapi_action", "tool_music_query", "tool_music_page", "tool_song_action",
    "tool_candidate", "agent_summary", "agent_execution_state", "music_reselection",
    "memory_use", "memory_extract", "memory_verify", "memory_compress", "memory_shared_topics", "diary_generate", "diary_public",
    "qzone_daily", "qzone_event", "qzone_interaction",
    "vision_analyze", "vision_merge", "vision_context", "vision_reply",
    "douyin_interest",
    "proactive_plan", "proactive_review", "proactive_reply",
})


class PromptError(ValueError):
    pass


def prompt_path() -> Path:
    if explicit := os.environ.get("ASTRBOT_CHAT_PROMPTS_FILE"):
        return Path(explicit).resolve()
    # Works both in the source tree and in external/AstrBot/data/plugins/… .
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "configs/qq_companion/prompts.yaml"
        if candidate.is_file():
            return candidate
    raise PromptError("Cannot find configs/qq_companion/prompts.yaml")


def load_prompts(path: Path | None = None, *, profile: str | None = None) -> dict[str, str]:
    path = path or prompt_path()
    try:
        values = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise PromptError(f"Cannot read QQ prompts: {path}") from exc
    if not isinstance(values, dict):
        raise PromptError(f"QQ prompts must be a YAML mapping: {path}")
    if profile:
        profiles = values.get("model_profiles", {})
        override = profiles.get(profile) if isinstance(profiles, dict) else None
        if not isinstance(override, dict) or set(override) != {"model_protocol"}:
            raise PromptError(f"Unknown or invalid model prompt profile: {profile}")
        values = {**values, **override}
    invalid = sorted(key for key in REQUIRED_PROMPTS if not isinstance(values.get(key), str) or not values[key].strip())
    if invalid:
        raise PromptError(f"Missing or empty QQ prompt keys in {path}: {', '.join(invalid)}")
    return {key: values[key].strip() for key in REQUIRED_PROMPTS}


def replace_framework_prompts(base: str, prompts: dict[str, str], sources: dict[str, str]) -> str:
    for key, original in sources.items():
        # Keep the framework's enable/disable decisions; only rewrite present blocks.
        if original:
            base = base.replace(original, prompts[key])
    return base


def replace_datetime_reminders(parts, prompts: dict[str, str]) -> None:
    pattern = r"<system_reminder>Current datetime: ([^\n<]+), Weekday: ([A-Za-z]+)</system_reminder>"
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str):
            part.text = re.sub(pattern, lambda match: prompts["datetime_reminder"].replace(
                "$current_time", match[1]).replace("$weekday", match[2]), text)


def apply_tool_descriptions(tool_set, prompts: dict[str, str]) -> None:
    if tool_set is None:
        return
    for tool in tool_set.tools:
        if tool.name in {"emotion", "musiccover", "music", "sticker", "weapi"}:
            from .agent_tools import schemas
            tool.description = prompts["tool_" + tool.name]
            tool.parameters = schemas(prompts)[tool.name]
        if tool.name == "q2_echo":
            tool.description = prompts["echo_description"]
            tool.parameters = deepcopy(tool.parameters)
            value = tool.parameters.get("properties", {}).get("value")
            if isinstance(value, dict):
                value["description"] = prompts["echo_value_description"]
