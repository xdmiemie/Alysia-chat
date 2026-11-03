"""Pass request options through the pinned AstrBot OpenAI provider, per request.

The upstream _prepare_chat_payload currently ignores **kwargs. Use its public
custom_extra_body setting on a shallow request copy; never mutate the shared
provider/config or create another client/Agent.
"""
from __future__ import annotations

from copy import copy, deepcopy

from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

from .model_debug import debug_client
from .model_messages import normalize_messages, protocol_prompt, separate_image_parts


REQUEST_OPTIONS = frozenset({"response_format", "max_tokens", "temperature", "top_p",
                             "top_k", "chat_template_kwargs", "reasoning_budget_tokens", "tool_choice"})


PHASE_DEFAULTS = {"agent": True, "selection": True, "followup": False,
                  "music_result": False, "compression": False, "memory": False, "vision": False,
                  "proactive_plan": False, "proactive_reply": False}


def request_config(provider, phase=None):
    config = dict(provider.provider_config)
    if config.get("id") == "astrbot_chat_qwen3":
        from .inference_queue import state_directory
        if (state_directory() / "model_settings.applied.json").exists():
            from .prompt_loader import prompt_path
            import yaml
            values = yaml.safe_load((prompt_path().parents[2] / "configs/runtime/qwen3.yaml").read_text(encoding="utf-8-sig"))
            config["custom_extra_body"] = {**config.get("custom_extra_body", {}),
                **{k: values[k] for k in ("temperature", "top_p", "top_k", "max_tokens", "reasoning_budget_tokens")}}
            config["max_context_tokens"] = values["context_size"]
            config["context_margin_tokens"] = values["context_margin_tokens"]
            config["thinking_phases"] = {name: values["enable_thinking"] and values.get("thinking_" + name, False)
                for name in PHASE_DEFAULTS}
            config["phase_temperatures"] = {name: values["temperature"] if name in {"agent", "selection"} else
                values["result_temperature"] if name in {"followup", "music_result"} else values["utility_temperature"] for name in PHASE_DEFAULTS}
    extra = deepcopy(config.get("custom_extra_body") or {})
    if phase is not None:
        if phase not in PHASE_DEFAULTS:
            raise ValueError("unknown_model_phase")
        enabled = config.get("thinking_phases", {}).get(phase, PHASE_DEFAULTS[phase])
        extra["chat_template_kwargs"] = {**extra.get("chat_template_kwargs", {}), "enable_thinking": enabled}
        extra["reasoning_budget_tokens"] = int(extra.get("reasoning_budget_tokens", 1024)) if enabled else 0
        if phase in config.get("phase_temperatures", {}):
            extra["temperature"] = config["phase_temperatures"][phase]
    config["custom_extra_body"] = extra
    return config


async def request_chat(provider, *, phase=None, **kwargs):
    if isinstance(provider, RequestProvider):
        return await provider.text_chat(**({"phase": phase} if phase else {}), **kwargs)
    config = request_config(provider, phase)
    if phase:
        for name in ("chat_template_kwargs", "reasoning_budget_tokens"):
            kwargs.setdefault(name, config["custom_extra_body"][name])
        if "temperature" in config["custom_extra_body"]:
            kwargs.setdefault("temperature", config["custom_extra_body"]["temperature"])
    tools = kwargs.get("func_tool")
    if tools and not tools.empty():
        # Native tools own their protocol. A content-only JSON grammar would
        # suppress tool calls; discard it from both request and provider options.
        kwargs.pop("response_format", None)
        config["custom_extra_body"].pop("response_format", None)
    if not isinstance(provider, ProviderOpenAIOfficial):
        return await provider.text_chat(**kwargs)
    kwargs["contexts"] = normalize_messages(kwargs.get("contexts") or [],
        system_prompt=kwargs.pop("system_prompt", None), protocol=protocol_prompt(config))
    view = copy(provider)
    async def assemble_context(*args, **options):
        record = await provider.assemble_context(*args, **options)
        record["content"] = separate_image_parts(record.get("content"))
        return record
    view.assemble_context = assemble_context
    extra = config["custom_extra_body"]
    for name in REQUEST_OPTIONS:
        if name not in kwargs:
            continue
        value = kwargs.pop(name)
        if name == "chat_template_kwargs":
            value = {**extra.get(name, {}), **value}
        extra[name] = deepcopy(value)
    view.provider_config = {**config, "custom_extra_body": extra}
    view.client = debug_client(provider.client, phase=phase,
        provider_id=config.get("id"), session_id=kwargs.get("session_id"))
    if config.get("id") == "astrbot_chat_qwen3" or config.get("inference_queue"):
        from .inference_queue import InferenceQueue
        priority = kwargs.pop("queue_priority", 10 if phase in {"memory", "vision"} else 0)
        request_id = kwargs.pop("request_id", None)
        timeout = kwargs.pop("queue_timeout", 180)
        images = len(kwargs.get("image_urls") or [])
        for message in kwargs.get("contexts") or []:
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
            if isinstance(content, list):
                images += sum(isinstance(p, dict) and p.get("type") == "image_url" for p in content)
        async with InferenceQueue().admission(request_id=request_id, priority=priority, phase=phase or "agent",
                scope=kwargs.get("session_id") or "", timeout=timeout, images=images):
            return await view.text_chat(**kwargs)
    return await view.text_chat(**kwargs)


class RequestProvider:
    """Temporary view used only while the native Runner makes its main request."""
    def __init__(self, provider, *, content_only=False, **defaults):
        self.provider, self.defaults, self.content_only = provider, defaults, content_only

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def text_chat(self, **kwargs):
        if self.content_only:
            kwargs["func_tool"] = None
        return await request_chat(self.provider, **{**self.defaults, **kwargs})
