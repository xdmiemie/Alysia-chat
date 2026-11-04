"""Per-run extension of AstrBot's native Runner; no second Agent or fake user event."""
from __future__ import annotations

import json
from types import MethodType

from astrbot.core.agent.message import Message, TextPart, ThinkPart
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse

from .agent_state import AgentRun, AgentStore, BudgetExhausted, Superseded
from .prompt_loader import load_prompts
from .reply_content import chat_history, delivered_content, request_view
from .request_provider import RequestProvider, request_chat
from .context_budget import RequestBudget, ContextCapacityError, WholeRoundCompressor

def parse_final_reply(text: str) -> dict | None:
    """Decode delivery metadata locally; ordinary text never needs model repair."""
    raw = text.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = "\n".join(raw.splitlines()[1:-1]).strip()
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        if not raw or raw.startswith(("{", "[")):
            return None
        value = {"reply": raw}
    if not isinstance(value, dict) or not isinstance(value.get("reply"), str):
        return None
    cleaned = chat_history([{"role": "assistant", "content": value["reply"]}])
    reply = cleaned[0]["content"].strip()[:1500] if cleaned else ""
    if not reply:
        return None
    level = value.get("thinking_level", 3)
    if type(level) is not int or not 1 <= level <= 5:
        level = 3
    return {"reply": reply, "thinking_level": level, "voice": value.get("voice") is True}


class SummaryProvider:
    """Keep native summary compression inside this run's request/time budget."""
    def __init__(self, provider, lifecycle):
        self.provider, self.lifecycle = provider, lifecycle

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def text_chat(self, **kwargs):
        lifecycle = self.lifecycle
        store, run = lifecycle.store, lifecycle.run
        store.work(run)
        if run.requests >= run.max_requests - 1:
            raise BudgetExhausted("summary_preserves_close_request")
        store.consume(run, "requests")
        system = load_prompts()["agent_summary"]
        # Native compressor restores original system/persona blocks afterward.
        # They must not impose the chat JSON contract on the summary itself.
        contexts = [m for m in kwargs.pop("contexts", [])
                    if (m.get("role") if isinstance(m, dict) else m.role) != "system"]
        messages = [Message(role="system", content=system)] + [Message.model_validate(m) for m in contexts]
        budget = RequestBudget(self.provider, "compression")
        fitted, tokens, dropped = await store.wait(run, budget.fit(messages), timeout=10)
        store.record(run, "summary_request", {"input_tokens": tokens, "dropped_turns": dropped})
        response = await store.wait(run, request_chat(self.provider, phase="compression",
            system_prompt=system, contexts=[m.model_dump(exclude_none=True) for m in fitted[1:]],
            func_tool=None, request_max_retries=1), timeout=30)
        if response.tools_call_name or not (response.completion_text or "").strip():
            raise ValueError("invalid_summary")
        store.record(run, "summary_generated", {"text": response.completion_text})
        return response


class NativeAgentLifecycle:
    """Keep native tool completion, budgets, executor and real delivery history.

    The pinned Runner has no public completion-veto API. Bind four instance
    methods from on_agent_begin; never patch the shared class/provider or vendor
    sources. Integration tests run this adapter against the pinned real Runner.
    """
    def __init__(self, runner, store: AgentStore, run: AgentRun, event):
        self.runner, self.store, self.run, self.event = runner, store, run, event
        self.initial_messages = [Message.model_validate(m) for m in chat_history(runner.run_context.messages)]
        self.rules_message = None
        if rules := event.get_extra("qq_agent_rules_prompt"):
            self.rules_message = Message(role="system", content=rules)
            index = 1 if self.initial_messages and self.initial_messages[0].role == "system" else 0
            self.initial_messages.insert(index, self.rules_message)
        runner.run_context.messages = list(self.initial_messages)
        self.sent_messages: list[Message] = []
        self.primary_message = None
        self.primary_parts = []
        self.final_content = ""
        self.tool_outputs = []
        self.tool_reasoning = {}
        self.memory_receipts = set()
        existing = runner.req.tool_calls_result
        self.initial_tool_results = len(existing if isinstance(existing, list) else [existing] if existing else [])
        self.finished = False
        self.state_message = None
        self.regeneration_message = None
        self.original_iter = runner._iter_llm_responses
        self.original_complete = runner._complete_with_assistant_response
        self.original_abort = runner._finalize_aborted_step
        self.original_await = runner._await_or_stop
        run.stop = runner.request_stop
        runner.streaming = False
        runner.request_max_retries = 1
        from astrbot.core.agent.context.compressor import TruncateByTurnsCompressor, LLMSummaryCompressor
        if isinstance(runner.request_context_manager.compressor, TruncateByTurnsCompressor):
            runner.request_context_manager.compressor = WholeRoundCompressor()
        elif isinstance(runner.request_context_manager.compressor, LLMSummaryCompressor):
            compressor = runner.request_context_manager.compressor
            compressor.instruction_text = load_prompts()["agent_summary"]
            compressor.provider = SummaryProvider(compressor.provider, self)
        if runner._step_budget_max is not None:
            run.max_requests = min(run.max_requests, max(1, runner._step_budget_max))
        event.set_extra("qq_agent_lifecycle", self)
        self.store.record(run, "request", {"current_request": request_view(runner.req.prompt),
            "tools": runner.req.func_tool.names() if runner.req.func_tool else []})

    def install(self):
        adapter = self

        async def iterate(_runner, **kwargs):
            async for response in adapter.responses(**kwargs):
                yield response

        async def complete(_runner, response):
            try:
                await adapter.complete(response)
            except Superseded:
                adapter.clear_output(response)
                _runner.request_stop()
                await _runner._finalize_aborted_step()

        async def aborted(_runner):
            if adapter.run.closing and adapter.store.current(adapter.run.scope, adapter.run.version):
                from astrbot.core.agent.response import AgentResponse, AgentResponseData
                response = adapter.fallback()
                await adapter.finish(response)
                return AgentResponse(type="llm_result", data=AgentResponseData(
                    chain=MessageChain().message(response.completion_text)))
            result = await adapter.original_abort()
            adapter.restore_history()
            adapter.store.status(adapter.run, "superseded")
            cleanup = adapter.event.get_extra("qq_agent_cleanup")
            if cleanup:
                cleanup()
            return result

        async def bounded_await(_runner, operation):
            try:
                adapter.store.work(adapter.run)
            except BaseException:
                if hasattr(operation, "close"):
                    operation.close()
                adapter.run.closing = not adapter.run.cancel.is_set()
                _runner.request_stop()
                return None
            try:
                return await adapter.store.wait(adapter.run, adapter.original_await(operation))
            except Superseded:
                _runner.request_stop()
                return None
            except (TimeoutError, BudgetExhausted):
                adapter.run.closing = True
                # Do not keep retrying/compressing past the global deadline.
                _runner.request_stop()
                return None

        self.runner._iter_llm_responses = MethodType(iterate, self.runner)
        self.runner._complete_with_assistant_response = MethodType(complete, self.runner)
        self.runner._finalize_aborted_step = MethodType(aborted, self.runner)
        self.runner._await_or_stop = MethodType(bounded_await, self.runner)

    def restore_history(self):
        # Retain native calls/results and the complete protocol content of sends.
        # AstrBot omits only the first system message when saving history.
        # Keep the separate rules in initial_messages for follow-up requests,
        # but do not persist or accumulate them in subsequent conversations.
        self.runner.run_context.messages = [message for message in self.initial_messages
                                           if message is not self.rules_message] + self.tool_history() + self.sent_messages
        callback = self.event.get_extra('qq_protocol_history')
        if callback:
            current = next((m for m in reversed(self.initial_messages) if m.role == 'user'), None)
            callback(([current] if current else []) + self.tool_history() + self.sent_messages)

    def tool_history(self):
        results = self.runner.req.tool_calls_result or []
        results = results if isinstance(results, list) else [results]
        messages = []
        for index, result in enumerate(results[self.initial_tool_results:]):
            if index < len(self.tool_outputs):
                ids, content = self.tool_outputs[index]
                if tuple(call.get('id') if isinstance(call, dict) else call.id
                         for call in result.tool_calls_info.tool_calls or []) == ids:
                    result.tool_calls_info.content = content or None
            messages.extend(Message.model_validate(m) for m in chat_history(result.to_openai_messages()))
        return messages

    def restore_tool_reasoning(self):
        """Current-run scratch context only; never deliver or persist it.

        Thinking-capable templates need reasoning paired with their tool calls
        on subsequent requests. Keep it outside the response sent to the Runner
        UI, and reattach only before budgeting/inference. tool_history() and
        chat_history() still strip it for durable history and follow-ups.
        """
        for message in self.runner.run_context.messages:
            ids = tuple(call.id for call in message.tool_calls or [])
            reasoning = self.tool_reasoning.get(ids)
            if message.role != "assistant" or not reasoning:
                continue
            content = message.content
            parts = [TextPart(text=content)] if isinstance(content, str) and content else (
                [part for part in content if part.type != "think"] if isinstance(content, list) else [])
            message.content = [ThinkPart(think=reasoning).mark_as_temp(), *parts]

    def sent(self, text: str, *, modality: str = "text", model_content: str | None = None,
             primary: bool = False, complete: bool = False):
        if primary:
            self.primary_parts.append(text)
            content = delivered_content(self.final_content, ''.join(self.primary_parts), complete=complete,
                thinking_level=self.event.get_extra('qq_thinking_level', 3),
                voice=self.event.get_extra('qq_voice', False))
            if self.primary_message is None:
                self.primary_message = Message(role='assistant', content=content)
                self.sent_messages.append(self.primary_message)
            else:
                self.primary_message.content = content
        else:
            content = model_content if model_content is not None else delivered_content('', text, complete=False,
                thinking_level=self.event.get_extra('qq_thinking_level', 3), voice=modality == 'voice')
            self.sent_messages.append(Message(role="assistant", content=content))
        if self.finished:
            self.restore_history()
        self.store.record(self.run, "sent", {"text": text, "modality": modality})
        if memory := self.event.get_extra("qq_persistent_memory"):
            memory.record_sent(self.run.scope, text)
        return content

    @staticmethod
    def clear_output(response):
        response.result_chain = None
        response.completion_text = ""
        response.reasoning_content = None

    def fallback(self):
        return LLMResponse("assistant", completion_text=json.dumps({"reply": load_prompts()["agent_budget_reply"],
                           "thinking_level": 1, "voice": False}, ensure_ascii=False))

    def execution_state(self):
        with self.store.connection() as db:
            lookups = db.execute("SELECT COUNT(*) FROM actions WHERE run_id=? AND tool='weapi' AND status != 'rejected'",
                                 (self.run.run_id,)).fetchone()[0]
            rejected = db.execute("SELECT payload FROM audit WHERE run_id=? AND kind='tool_rejected' ORDER BY id DESC LIMIT 1",
                                  (self.run.run_id,)).fetchone()
        last_rejection = json.loads(rejected[0]) if rejected else None
        if last_rejection:
            last_rejection["resolved"] = (last_rejection.get("result", {}).get("error") == "candidate_not_registered"
                and last_rejection.get("arguments", {}).get("candidate") in self.run.candidate_refs)
            if last_rejection["resolved"]:
                last_rejection = None
        return {"source_lookup_attempts": lookups,
            "registered_song_candidates": sorted(self.run.candidate_refs),
            "last_preflight_rejection": last_rejection}

    def execution_state_message(self, prompts):
        return Message(role="system", content=prompts["agent_execution_state"] + "\n" +
            json.dumps(self.execution_state(), ensure_ascii=False, separators=(",", ":")))

    def followup_system_messages(self, prompts):
        # Preserve the complete main system messages, including multipart
        # content, rather than the event's pre-assembly persona snapshot.
        messages = [message.model_copy(deep=True) for message in self.initial_messages
                    if message.role == "system"]
        messages.extend((self.execution_state_message(prompts),
                         Message(role="system", content=prompts["followup_phase"]),
                         Message(role="system", content=prompts["followup_system"])))
        return messages

    def update_execution_state(self):
        messages = self.runner.run_context.messages
        messages[:] = [m for m in messages if m is not self.state_message and m != self.state_message]
        self.state_message = self.execution_state_message(load_prompts())
        messages.append(self.state_message)

    async def responses(self, **kwargs):
        try:
            self.store.work(self.run)
            if self.event.get_extra("qq_search_closed") and self.runner.req.func_tool:
                from .web_search import SEARCH_NAMES
                self.runner.req.func_tool.tools = [tool for tool in self.runner.req.func_tool.tools if tool.name not in SEARCH_NAMES]
            self.tool_history()  # Restore ignored delivery content on the native tool messages.
            native_limit = self.runner._step_budget_max
            if (self.run.requests >= self.run.max_requests or self.run.tool_calls >= self.run.max_tools
                    or (native_limit is not None and self.runner._step_budget_used >= native_limit)):
                self.run.closing = True
                yield self.fallback()
                return
            self.update_execution_state()
            self.restore_tool_reasoning()
            if memory := self.event.get_extra("qq_persistent_memory"):
                for receipt in self.tool_history():
                    if receipt.role == "tool" and receipt.tool_call_id not in self.memory_receipts:
                        memory.record_sent(self.run.scope, str(receipt.content),
                            event_id="tool:" + self.run.run_id + ":" + str(receipt.tool_call_id), role="receipt")
                        self.memory_receipts.add(receipt.tool_call_id)
                def consume_memory_request():
                    if self.run.requests >= self.run.max_requests - 1:
                        raise ValueError("memory_compaction_budget_unavailable")
                    self.store.consume(self.run, "requests")
                self.runner.run_context.messages = await memory.prepare(
                    self.runner.run_context.messages, self.runner.provider, self.run.scope, self.runner.req.func_tool,
                    wait=lambda operation: self.store.wait(self.run, operation, timeout=60),
                    consume=consume_memory_request,
                    record=lambda payload: self.store.record(self.run, "memory_context", payload))
            budget = RequestBudget(self.runner.provider, "agent")
            messages, tokens, dropped = await self.store.wait(self.run,
                budget.fit(self.runner.run_context.messages, self.runner.req.func_tool), timeout=10)
            self.runner.run_context.messages = messages
            if tokens is not None:
                self.store.record(self.run, "context_budget", {"input_tokens": tokens,
                    "context_window": budget.window, "dropped_turns": dropped})
            self.store.consume(self.run, "requests")
            async for response in self.main_responses(**kwargs):
                self.store.ensure_current(self.run)
                if response.is_chunk:
                    continue
                self.store.record(self.run, "generated", {"text": response.completion_text or "",
                                                         "tools": response.tools_call_name})
                if self.regeneration_message is not None:
                    self.runner.run_context.messages = [m for m in self.runner.run_context.messages
                        if m is not self.regeneration_message and m != self.regeneration_message]
                    self.regeneration_message = None
                if response.tools_call_name and response.reasoning_content:
                    self.tool_reasoning[tuple(response.tools_call_ids or [])] = response.reasoning_content
                response.reasoning_content = None
                finish = None
                if response.raw_completion and getattr(response.raw_completion, "choices", None):
                    finish = response.raw_completion.choices[0].finish_reason
                if response.role == "err" or finish in {"length", "content_filter"}:
                    self.run.closing = True
                    yield self.fallback()
                    return
                if response.tools_call_name:
                    import jsonschema
                    for index, name in enumerate(response.tools_call_name):
                        tool = self.runner.req.func_tool.get_tool(name) if self.runner.req.func_tool else None
                        if tool:
                            try:
                                jsonschema.validate(response.tools_call_args[index], tool.parameters)
                            except (jsonschema.ValidationError, IndexError):
                                while len(response.tools_call_args) <= index:
                                    response.tools_call_args.append({})
                                response.tools_call_args[index] = {}
                                self.store.record(self.run, "invalid_tool_arguments", {"tool": name})
                    self.tool_outputs.append((tuple(response.tools_call_ids or []), response.completion_text or ''))
                    # Preserve tool-stage content in protocol history, but never
                    # parse, deliver or regenerate it as a user-facing reply.
                    if response.completion_text:
                        self.store.record(self.run, "tool_content_ignored", {"tools": response.tools_call_name})
                    self.clear_output(response)
                yield response
        except Superseded:
            self.runner.request_stop()
        except (BudgetExhausted, TimeoutError):
            self.run.closing = True
            yield self.fallback()
        except Exception as exc:
            fallbacks = self.runner.fallback_providers
            if fallbacks and self.runner.provider is not fallbacks[-1] and not isinstance(exc, ContextCapacityError):
                raise
            self.store.record(self.run, "model_failed", {"error": type(exc).__name__})
            self.run.closing = True
            yield self.fallback()

    async def main_responses(self, **kwargs):
        provider = self.runner.provider
        self.runner.provider = RequestProvider(provider, phase="agent")
        try:
            async for response in self.original_iter(**kwargs):
                yield response
        finally:
            self.runner.provider = provider

    async def complete(self, response):
        # The native Runner reaches this method only when there are no tool calls.
        # Parse locally. One invalid-output regeneration is shared by the whole run.
        self.store.ensure_current(self.run)
        original_content = response.completion_text or ""
        value = parse_final_reply(original_content)
        if value is None:
            self.store.record(self.run, "invalid_reply", {"text": response.completion_text or ""})
            native_limit = self.runner._step_budget_max
            can_retry = (not self.run.closing and self.run.reply_retries < 1
                and self.run.requests < self.run.max_requests and self.run.tool_calls < self.run.max_tools
                and (native_limit is None or self.runner._step_budget_used + 1 < native_limit))
            if can_retry:
                try:
                    self.store.consume(self.run, "reply_retries")
                except BudgetExhausted:
                    self.run.closing = True
                else:
                    self.regeneration_message = Message(role="system", content=load_prompts()["agent_reply_regenerate"]
                        + "\n" + json.dumps({"invalid_output": (response.completion_text or "")[:1500]}, ensure_ascii=False))
                    self.runner.run_context.messages.append(self.regeneration_message)
                    self.store.record(self.run, "reply_regeneration", {"number": self.run.reply_retries})
                    self.clear_output(response)
                    return
            response.completion_text = self.fallback().completion_text
            original_content = response.completion_text
        else:
            response.completion_text = json.dumps(value, ensure_ascii=False)
        await self.finish(response, content=original_content)

    async def finish(self, response, *, content=None):
        self.store.ensure_current(self.run)
        self.final_content = response.completion_text if content is None else content
        self.event.set_extra('qq_primary_model_content', self.final_content)
        self.event.set_extra("qq_agent_completed", True)
        self.store.record(self.run, "completed", {"text": response.completion_text})
        await self.original_complete(response)
        self.finished = True
        self.restore_history()
        self.store.status(self.run, "completed")
