"""One leading system message for both inference and local token counting."""
from __future__ import annotations

from copy import deepcopy

from .prompt_loader import load_prompts


def separate_image_parts(content):
    """Prevent llama.cpp from treating adjacent images as merged video frames."""
    if not isinstance(content, list):
        return content
    parts, index = [], 0
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            if parts and isinstance(parts[-1], dict) and parts[-1].get("type") == "image_url":
                parts.append({"type": "text", "text": '{"image_index":' + str(index) + '}'})
            index += 1
        parts.append(part)
    return parts


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
        if "content" in message:
            message["content"] = separate_image_parts(message["content"])
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
