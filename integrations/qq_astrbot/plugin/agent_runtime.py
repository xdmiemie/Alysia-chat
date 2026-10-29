"""Per-run extension of AstrBot's native Runner; no second Agent or fake user event."""
from __future__ import annotations

import json
import re
from collections import Counter
from types import MethodType

from astrbot.core.agent.message import Message
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse

from .agent_state import AgentRun, AgentStore, BudgetExhausted, Superseded
from .prompt_loader import load_prompts
from .reply_content import chat_history, dialogue_view, has_control_markup, request_view
from .request_provider import RequestProvider, request_chat
from .context_budget import RequestBudget, ContextCapacityError, WholeRoundCompressor, action_receipts

CHECK_FORMAT = {"type": "json_schema", "json_schema": {"name": "completion_check", "strict": True, "schema": {
    "type": "object", "properties": {
    "claims_music_started": {"type": "boolean"},
    "claims_music_completed": {"type": "boolean"}, "claims_link_sent": {"type": "boolean"},
    "claims_audio_sent": {"type": "boolean"},
    "decision": {"type": "string", "enum": ["finish", "continue"]},
    "instruction": {"type": "string", "maxLength": 800}}, "required": [
        "claims_music_started", "claims_music_completed", "claims_link_sent", "claims_audio_sent", "decision", "instruction"], "additionalProperties": False}}}
CLOSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "restricted_close", "strict": True, "schema": {
    "type": "object", "properties": {"reply": {"type": "string", "minLength": 1, "maxLength": 1500},
    "thinking_level": {"type": "integer", "const": 1}, "voice": {"type": "boolean", "const": False}},
    "required": ["reply", "thinking_level", "voice"], "additionalProperties": False}}}
REPLY_FORMAT = {"type": "json_schema", "json_schema": {"name": "qq_reply", "strict": True, "schema": {
    "type": "object", "properties": {"reply": {"type": "string", "minLength": 1, "maxLength": 1500},
    "thinking_level": {"type": "integer", "enum": [1, 2, 3, 4, 5]}, "voice": {"type": "boolean"}},
    "required": ["reply", "thinking_level", "voice"], "additionalProperties": False}}}


def parse_candidate(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    if (not isinstance(value, dict) or set(value) != {"reply", "thinking_level", "voice"}
            or not isinstance(value["reply"], str) or not value["reply"].strip()
            or len(value["reply"]) > 1500 or type(value["thinking_level"]) is not int
            or not 1 <= value["thinking_level"] <= 5 or type(value["voice"]) is not bool
            or has_control_markup(value["reply"])):
        return None
    return value


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
    """Keep AstrBot's protocol, executor, tool messages, fallback and compression.

    The pinned Runner has no public completion-veto API. Bind four instance
    methods from on_agent_begin; never patch the shared class/provider or vendor
    sources. Integration tests run this adapter against the pinned real Runner.
    """
    def __init__(self, runner, store: AgentStore, run: AgentRun, event):
        self.runner, self.store, self.run, self.event = runner, store, run, event
        self.initial_messages = [Message.model_validate(m) for m in chat_history(runner.run_context.messages)]
        runner.run_context.messages = list(self.initial_messages)
        self.sent_messages: list[Message] = []
        self.last_candidate: dict | None = None
        self.finished = False
        self.continuations = []
        self.invalid_candidates = 0
        self.invalid_outputs = Counter()
        self.format_repair = False
        self.tool_repair = None
        self.state_message = None
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
                await adapter.candidate(response)
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
        # Only actual incoming messages and confirmed sends become dialogue.
        self.runner.run_context.messages = self.initial_messages + self.sent_messages

    def sent(self, text: str, *, modality: str = "text"):
        self.sent_messages.append(Message(role="assistant", content=text))
        if self.finished:
            self.restore_history()
        self.store.record(self.run, "sent", {"text": text, "modality": modality})

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

    def update_execution_state(self):
        messages = self.runner.run_context.messages
        messages[:] = [m for m in messages if m is not self.state_message and m != self.state_message]
        self.state_message = Message(role="system", content=load_prompts()["agent_execution_state"] + "\n" +
            json.dumps(self.execution_state(), ensure_ascii=False, separators=(",", ":")))
        messages.append(self.state_message)

    async def responses(self, **kwargs):
        try:
            self.store.work(self.run)
            # Reserve the final request for a bounded, tool-free close.
            if self.run.requests >= self.run.max_requests - 1 or self.run.tool_calls >= self.run.max_tools:
                self.run.closing = True
            if self.run.closing:
                yield await self.restricted_close()
                return
            self.update_execution_state()
            budget = RequestBudget(self.runner.provider, "repair" if self.format_repair else "agent")
            messages, tokens, dropped = await self.store.wait(self.run,
                budget.fit(self.runner.run_context.messages, None if self.format_repair else self.runner.req.func_tool), timeout=10)
            self.runner.run_context.messages = messages
            if tokens is not None:
                self.store.record(self.run, "context_budget", {"input_tokens": tokens,
                    "context_window": budget.window, "dropped_turns": dropped})
            self.store.consume(self.run, "requests")
            async for response in self.main_responses(**kwargs):
                self.store.ensure_current(self.run)
                if response.is_chunk:
                    continue
                raw = response.completion_text or ""
                self.store.record(self.run, "generated", {"text": raw, "tools": response.tools_call_name})
                response.reasoning_content = None
                if response.role == "err":
                    self.run.closing = True
                    yield self.fallback()
                    return
                finish = None
                if response.raw_completion and getattr(response.raw_completion, "choices", None):
                    finish = response.raw_completion.choices[0].finish_reason
                if finish in {"length", "content_filter"}:
                    # Never execute a truncated call or semantically approve a partial draft.
                    response.tools_call_name = []
                    response.tools_call_args = []
                    response.tools_call_ids = []
                    self.clear_output(response)
                elif response.tools_call_name:
                    if self.format_repair:
                        # Formatting is a content-only internal regeneration;
                        # a provider ignoring that restriction cannot run tools.
                        response.tools_call_name = []
                        response.tools_call_args = []
                        response.tools_call_ids = []
                        self.clear_output(response)
                        yield response
                        continue
                    self.tool_repair = None
                    import jsonschema
                    for index, name in enumerate(response.tools_call_name):
                        tool = self.runner.req.func_tool.get_tool(name) if self.runner.req.func_tool else None
                        if tool:
                            try:
                                jsonschema.validate(response.tools_call_args[index], tool.parameters)
                            except (jsonschema.ValidationError, IndexError):
                                # Preserve a real call/result round-trip, but no malformed
                                # original argument can become executable after filtering.
                                while len(response.tools_call_args) <= index:
                                    response.tools_call_args.append({})
                                response.tools_call_args[index] = {}
                                self.store.record(self.run, "invalid_tool_arguments", {"tool": name})
                    # Tool-step prose is an internal draft, never a QQ reply.
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
                raise  # Let AstrBot try the next provider with the same run budgets.
            # Provider errors are not dialogue. A bounded terminal response is
            # still version-checked and audited by the normal completion path.
            self.store.record(self.run, "model_failed", {"error": type(exc).__name__})
            self.run.closing = True
            yield self.fallback()

    async def main_responses(self, **kwargs):
        # Preserve native request construction, tool executor and fallback
        # identities. Only this Runner sees the temporary request-options view.
        provider = self.runner.provider
        options = {}
        if self.tool_repair and not self.format_repair:
            # Regenerate a native call for the registered tool selected by the
            # model. Plain-text arguments never become executable.
            options["tool_choice"] = {"type": "function", "function": {"name": self.tool_repair}}
        self.runner.provider = RequestProvider(provider, phase="repair" if self.format_repair else "agent",
            content_only=self.format_repair, response_format=REPLY_FORMAT, **options)
        try:
            async for response in self.original_iter(**kwargs):
                yield response
        finally:
            self.runner.provider = provider

    def completion_input(self, candidate=None):
        with self.store.connection() as db:
            actions = [dict(row) for row in db.execute(
                "SELECT tool,arguments,status,result FROM actions WHERE run_id=?", (self.run.run_id,))]
            sent = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM audit WHERE run_id=? AND kind='sent' ORDER BY id", (self.run.run_id,))]
            rejections = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM audit WHERE run_id=? AND kind='tool_rejected' ORDER BY id DESC LIMIT 5", (self.run.run_id,))]
        for action in actions:
            for key in ("arguments", "result"):
                action[key] = json.loads(action[key]) if action[key] else None
        value = {"check_number": self.run.checks,
            "current_request": request_view(self.runner.req.prompt),
            "dialogue": dialogue_view(self.initial_messages + self.sent_messages), "sent_this_run": sent,
            "delivery_facts": {"any_message_sent": bool(sent),
                "link_sent": any(re.search(r"https?://", message.get("text", "")) for message in sent),
                "audio_sent": any(message.get("modality") in {"voice", "audio"} for message in sent)},
            "background_tasks": [{"tool": action["tool"], "job_id": action["result"]["job_id"],
                "song": action_receipts([action], self.run)[0]["result"].get("song"),
                "state": "accepted_waiting_for_foreground_release"}
                for action in actions if action["tool"] in {"music", "musiccover"}
                and isinstance(action.get("result"), dict) and action["result"].get("ok") is True
                and action["result"].get("status") == "queued" and action["result"].get("job_id")],
            "reply_delivery": {"state": "after_approval", "automatic_tts": True},
            "available_tools": [{"name": t.name, "description": t.description} for t in
                (self.runner.req.func_tool.tools if self.runner.req.func_tool else [])],
            "actions": action_receipts(actions, self.run)}
        for rejection in rejections:
            rejection["resolved"] = (rejection.get("result", {}).get("error") == "candidate_not_registered"
                and rejection.get("arguments", {}).get("candidate") in self.run.candidate_refs)
        # Resolved failures remain in the audit/native tool history, but they
        # are not current blockers. Repeating them misleads non-reasoning checks.
        value["preflight_rejections"] = [row for row in reversed(rejections) if not row["resolved"]]
        # Keep the current evidence and candidate after historical dialogue.
        # A non-reasoning judge must not confuse an earlier failure with this
        # run's accepted task or treat a pending reply as a failed delivery.
        value["current_task_facts"] = {"accepted_music": bool(value["background_tasks"]),
            "music_completed": any(action["tool"] in {"music", "musiccover"}
                and isinstance(action.get("result"), dict) and action["result"].get("completed") is True for action in actions),
            "accepted_songs": [task["song"] for task in value["background_tasks"]],
            "waiting_for_this_reply": bool(value["background_tasks"]),
            "candidate_not_sent_yet": True, **self.execution_state(),
            "known_songs": [{"candidate": ref, **{key: self.run.candidates[song_id].get(key)
                for key in ("title", "artists", "aliases", "available")}}
                for ref, song_id in list(self.run.candidate_refs.items())[-10:]]}
        value["draft_reply"] = candidate
        return value

    async def restricted_close(self):
        self.run.closing = True
        self.runner.req.func_tool = None
        prompts = load_prompts()
        persona = self.event.get_extra("qq_persona_prompt", "")
        system = "\n".join(part for part in (persona, prompts["agent_restricted_close"]) if part)
        budget = RequestBudget(self.runner.provider, "close")
        try:
            self.store.consume(self.run, "requests")
            close_input = self.completion_input()
            # Rejected drafts and old repair instructions are not evidence.
            close_input.pop("draft_reply")
            close_input.pop("available_tools")
            # Persona supplies style; old conversation failures are not facts
            # about this run. Closing only needs the current request/receipts.
            close_input.pop("dialogue")
            close_input["invalid_candidates"] = self.invalid_candidates
            messages, _, _ = await self.store.wait(self.run, budget.fit([
                Message(role="system", content=system),
                Message(role="user", content=json.dumps(close_input, ensure_ascii=False))]), timeout=10)
            response = await self.store.wait(self.run, request_chat(self.runner.provider, phase="close",
                contexts=self.runner._sanitize_contexts_for_provider(messages[1:]),
                system_prompt=system, func_tool=None,
                response_format=CLOSE_FORMAT, max_tokens=budget.output_tokens, request_max_retries=1), timeout=30)
            self.store.record(self.run, "restricted_close", {"text": response.completion_text or ""})
            value = parse_candidate(response.completion_text or "")
            if (not response.tools_call_name and value and value["thinking_level"] == 1
                    and value["voice"] is False):
                return response
        except Superseded:
            raise
        except Exception as exc:
            self.store.record(self.run, "close_failed", {"error": type(exc).__name__})
        return self.fallback()

    async def candidate(self, response):
        self.store.ensure_current(self.run)
        raw = response.completion_text or ""
        value = parse_candidate(raw)
        if self.run.closing:
            if value is None or value["thinking_level"] != 1 or value["voice"] is not False:
                response.result_chain = None
                response.completion_text = self.fallback().completion_text
            await self.finish(response)
            return
        if value is None:
            self.invalid_candidates += 1
            self.tool_repair = None
            try:
                decoded = json.loads(raw)
                fingerprint = json.dumps(decoded, ensure_ascii=False, sort_keys=True)
                error = "control_markup" if isinstance(decoded, dict) and has_control_markup(str(decoded.get("reply", ""))) else (
                    "tool_in_content" if isinstance(decoded, dict) and "name" in decoded and "arguments" in decoded else "invalid_fields")
                tools = self.runner.req.func_tool
                if (error == "tool_in_content" and isinstance(decoded.get("name"), str)
                        and isinstance(decoded.get("arguments"), dict) and tools and tools.get_tool(decoded["name"])):
                    self.tool_repair = decoded["name"]
            except (ValueError, TypeError):
                fingerprint, error = raw.strip(), "invalid_json"
            self.invalid_outputs[fingerprint] += 1
            self.store.record(self.run, "invalid_candidate", {"text": raw, "error": error,
                "number": self.invalid_candidates, "repeated": self.invalid_outputs[fingerprint]})
            if self.invalid_candidates >= 3 or self.invalid_outputs[fingerprint] >= 2:
                self.store.record(self.run, "repair_exhausted", {"number": self.invalid_candidates})
                await self.close_candidate(response)
            else:
                self.format_repair = error != "tool_in_content"
                if self.tool_repair:
                    self.store.record(self.run, "tool_protocol_repair", {"tool": self.tool_repair})
                self.continue_run(response, load_prompts()["agent_repair"].replace("$error", error), retain_candidate=False)
            return
        self.format_repair = False
        self.tool_repair = None
        self.last_candidate = value
        try:
            # Reserve before I/O: malformed responses and timeouts cost a check.
            self.store.consume(self.run, "checks")
            self.store.consume(self.run, "requests")
            prompts = load_prompts()
            check_input = self.completion_input(value)
            while True:
                try:
                    await self.store.wait(self.run, RequestBudget(self.runner.provider, "check").fit([
                        Message(role="system", content=prompts["agent_completion_check"]),
                        Message(role="user", content=json.dumps(check_input, ensure_ascii=False, separators=(",", ":")))]), timeout=10)
                    break
                except ContextCapacityError:
                    if not check_input["dialogue"]:
                        raise
                    check_input["dialogue"].pop(0)
            self.store.record(self.run, "check_input", check_input)
            result = await self.store.wait(self.run, request_chat(self.runner.provider, phase="check",
                system_prompt=prompts["agent_completion_check"],
                prompt=json.dumps(check_input, ensure_ascii=False, separators=(",", ":")), func_tool=None,
                response_format=CHECK_FORMAT, max_tokens=RequestBudget(self.runner.provider, "check").output_tokens, request_max_retries=1), timeout=25)
            verdict = json.loads(result.completion_text or "")
            claims = {"claims_music_started": check_input["current_task_facts"]["accepted_music"],
                "claims_music_completed": check_input["current_task_facts"]["music_completed"],
                "claims_link_sent": check_input["delivery_facts"]["link_sent"],
                "claims_audio_sent": check_input["delivery_facts"]["audio_sent"]}
            if (result.tools_call_name or not isinstance(verdict, dict) or set(verdict) != {"decision", "instruction", *claims}
                    or verdict["decision"] not in {"finish", "continue"}
                    or any(type(verdict[key]) is not bool for key in claims)
                    or not isinstance(verdict["instruction"], str) or len(verdict["instruction"]) > 800):
                raise ValueError("invalid_completion_verdict")
            if any(verdict[key] and not evidence for key, evidence in claims.items()):
                # Extraction and verdict are one semantic request. Acceptance
                # cannot override an extracted claim lacking actual evidence.
                verdict.update(decision="continue", instruction=prompts["agent_unverified_claim"])
            self.store.record(self.run, "semantic_check", {"number": self.run.checks, **verdict})
        except Superseded:
            raise
        except Exception as exc:
            self.store.record(self.run, "check_failed", {"number": self.run.checks, "error": type(exc).__name__})
            verdict = {"decision": "continue", "instruction": load_prompts()["agent_check_failed"]}
        self.store.ensure_current(self.run)
        if verdict["decision"] == "finish":
            await self.finish(response)
        elif self.run.checks >= 3:
            await self.close_candidate(response)
        else:
            self.continue_run(response, verdict["instruction"])

    async def close_candidate(self, response):
        closed = await self.restricted_close()
        response.result_chain = closed.result_chain
        response.completion_text = closed.completion_text
        response.reasoning_content = None
        await self.finish(response)

    def continue_run(self, response, instruction, *, retain_candidate=False):
        # Old rejected drafts are audited separately. Retain the latest repair
        # instruction while keeping all native call/result pairs in this run.
        self.runner.run_context.messages = [m for m in self.runner.run_context.messages
            if not any(m is previous for previous in self.continuations)]
        self.continuations = []
        if response.completion_text and retain_candidate:
            self.continuations.append(Message(role="assistant", content=response.completion_text))
        self.continuations.append(Message(role="system", content=
            load_prompts()["agent_continue"].replace("$instruction", instruction)))
        self.runner.run_context.messages.extend(self.continuations)
        self.clear_output(response)

    async def finish(self, response):
        self.store.ensure_current(self.run)
        self.event.set_extra("qq_agent_approved", True)
        self.store.record(self.run, "approved", {"text": response.completion_text})
        await self.original_complete(response)
        self.finished = True
        self.restore_history()
        self.store.status(self.run, "approved")
