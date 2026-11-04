"""Adapt background AstrBot requests to the shared search-only tool loop."""
from __future__ import annotations

from astrbot.core.agent.message import Message
from astrbot.core.agent.tool import FunctionTool, ToolSet

from .context_budget import RequestBudget, ContextCapacityError
from .search_loop import complete_with_search, SearchContextOverflow
from .web_search import SearchService


async def request_search_chat(provider, *, phase=None, contexts=None, response_format=None,
                              before_request=None, search_service=None, **kwargs):
    from .request_provider import request_chat
    kwargs.pop("func_tool", None)
    search_service = search_service or SearchService()
    if not search_service.configured():
        if before_request:
            before_request()
        return await request_chat(provider, phase=phase, contexts=contexts, func_tool=None,
                                  response_format=response_format, **kwargs)
    messages = [m.model_dump(exclude_none=True) if hasattr(m, "model_dump") else m for m in contexts or []]
    # Utility callers supply contexts; keep compatibility with simple prompt calls.
    if system := kwargs.pop("system_prompt", None):
        messages.insert(0, {"role": "system", "content": system})
    if prompt := kwargs.pop("prompt", None):
        messages.append({"role": "user", "content": prompt})
    budget = RequestBudget(provider, phase)
    budget.output_tokens = kwargs.get("max_tokens") or budget.output_tokens

    async def call(current, schemas, fmt):
        tools = ToolSet([FunctionTool(**item["function"]) for item in schemas]) if schemas else None
        typed = [Message.model_validate(m) for m in current]
        try:
            fitted, _, _ = await budget.fit(typed, tools)
        except ContextCapacityError as exc:
            if str(exc) == "current_turn_exceeds_context_capacity":
                raise SearchContextOverflow(str(exc)) from exc
            raise
        if before_request:
            before_request()
        response = await request_chat(provider, phase=phase, contexts=fitted, func_tool=tools,
                                      response_format=fmt, **kwargs)
        calls = [call.model_dump(exclude_none=True) for call in response.to_openai_tool_calls_model()] if response.tools_call_name else []
        message = {"role": "assistant", "content": response.completion_text}
        if calls:
            message["tool_calls"] = calls
        if getattr(response, "reasoning_content", None):
            message["reasoning_content"] = response.reasoning_content
        raw = getattr(response, "raw_completion", None)
        choices = getattr(raw, "choices", None)
        finish = choices[0].finish_reason if choices else "tool_calls" if calls else "stop"
        return {"choices": [{"message": message, "finish_reason": finish}], "native_response": response}

    result = await complete_with_search(call, messages, response_format, service=search_service)
    return result["native_response"]
