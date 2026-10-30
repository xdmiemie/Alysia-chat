"""Pass request options through the pinned AstrBot OpenAI provider, per request.

The upstream _prepare_chat_payload currently ignores **kwargs. Use its public
custom_extra_body setting on a shallow request copy; never mutate the shared
provider/config or create another client/Agent.
"""
from __future__ import annotations

from copy import copy, deepcopy

from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

from .model_debug import debug_client


REQUEST_OPTIONS = frozenset({"response_format", "max_tokens", "temperature", "top_p",
                             "top_k", "chat_template_kwargs", "reasoning_budget_tokens", "tool_choice"})


PHASE_DEFAULTS = {"agent": True, "selection": True, "followup": False,
                  "music_result": False, "compression": False}


def request_config(provider, phase=None):
    config = dict(provider.provider_config)
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
    view = copy(provider)
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
