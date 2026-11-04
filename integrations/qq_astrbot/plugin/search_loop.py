"""Bounded read-only tool loop shared by native and direct HTTP model callers."""
from __future__ import annotations

from copy import deepcopy
import json

import jsonschema

from .prompt_loader import load_prompts
from .web_search import SearchService, rejected, tool_schemas


class SearchContextOverflow(ValueError):
    pass


def valid_final(message, response_format):
    if not response_format:
        return True
    try:
        value = json.loads(message.get("content") or "")
        if response_format.get("type") == "json_schema":
            jsonschema.validate(value, response_format["json_schema"]["schema"])
        return isinstance(value, dict)
    except (ValueError, TypeError, KeyError, jsonschema.ValidationError):
        return False


async def complete_with_search(call, messages, response_format=None, *, service=None):
    """call(messages, schemas_or_none, format) returns an OpenAI completion.

    The caller keeps ownership of inference admission, cancellation, token
    counting and logs. Search happens after each inference lease is released.
    """
    service = service or SearchService()
    original = deepcopy(messages)
    if not service.configured():
        return await call(original, None, response_format)
    working = [{"role": "system", "content": load_prompts()["web_search_policy"]}, *deepcopy(original)]
    schemas, executed = tool_schemas(), 0
    try:
        for _ in range(2):
            result = await call(working, schemas, None)
            choice = result["choices"][0]
            message = choice["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                if choice.get("finish_reason") != "stop" or valid_final(message, response_format):
                    return result
                break  # Reapply the module's output grammar, with no tools.
            if choice.get("finish_reason") not in {"tool_calls", "stop"} or len(calls) > 8:
                break  # Never execute a truncated/oversized model request.
            assistant = {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
            if message.get("reasoning_content"):
                assistant["reasoning_content"] = message["reasoning_content"]
            working.append(assistant)
            closed = False
            for item in calls:
                if closed or executed >= 3:
                    receipt = rejected("search_turn_limit")
                else:
                    try:
                        arguments = json.loads(item["function"]["arguments"])
                    except (ValueError, TypeError, KeyError):
                        receipt = rejected("invalid_search_arguments")
                    else:
                        executed += 1
                        receipt = await service.execute(item["function"]["name"], arguments)
                working.append({"role": "tool", "tool_call_id": item["id"],
                                "content": json.dumps(receipt, ensure_ascii=False)})
                closed = closed or receipt.get("continue_without_search", False)
            if closed or executed >= 3:
                break
        return await call(working, None, response_format)
    except SearchContextOverflow:
        # Optional web context must never evict required memory/video material
        # or prevent the original task from completing within its old budget.
        return await call(original, None, response_format)


class ToolCallStream:
    """Assemble streamed function argument fragments without executing deltas."""
    def __init__(self):
        self.calls = {}

    def add(self, deltas):
        for delta in deltas:
            index = delta["index"]
            if type(index) is not int or not 0 <= index < 8:
                raise ValueError("too_many_search_tool_calls")
            target = self.calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if delta.get("id"):
                target["id"] = delta["id"]
            function = delta.get("function") or {}
            for key in ("name", "arguments"):
                target["function"][key] += function.get(key) or ""

    def message(self, content):
        result = {"role": "assistant", "content": content}
        if self.calls:
            result["tool_calls"] = [self.calls[index] for index in sorted(self.calls)]
        return result
