"""One leading system message for both inference and local token counting."""
from __future__ import annotations

from copy import deepcopy

from .prompt_loader import load_prompts


def protocol_prompt(config):
    profile = config.get("prompt_profile")
    return load_prompts(profile=profile)["model_protocol"] if profile else ""


def normalize_messages(messages, *, system_prompt=None, protocol=""):
    """Preserve dialogue/tool ordering; merge only trusted system instructions.

    Runner state may append system messages after tool results. Qwen3.5's
    template requires all of these at the beginning. Never alter saved history
    or promote user/tool content to the system role.
    """
    systems = [system_prompt] if system_prompt else []
    dialogue = []
    for original in messages:
        message = deepcopy(original.model_dump() if hasattr(original, "model_dump") else original)
        if message.get("role") == "assistant" and isinstance(message.get("content"), list):
            parts = message["content"]
            thoughts = [part.get("think", "") for part in parts if part.get("type") == "think"]
            if thoughts:
                message["reasoning_content"] = "\n".join(thoughts)
                message["content"] = [part for part in parts if part.get("type") != "think"] or None
        if message.get("role") != "system":
            dialogue.append(message)
            continue
        content = message.get("content")
        if isinstance(content, list):
            if any(part.get("type") != "text" or not isinstance(part.get("text"), str) for part in content):
                raise ValueError("system messages must contain text only")
            content = "\n".join(part["text"] for part in content)
        if not isinstance(content, str):
            raise ValueError("system messages must contain text only")
        if content:
            systems.append(content)
    combined = "\n\n".join(systems)
    if protocol and protocol not in combined:
        combined = "\n\n".join(filter(None, (combined, protocol)))
    return ([{"role": "system", "content": combined}] if combined else []) + dialogue
