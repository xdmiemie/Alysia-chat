"""Q3 local acceptance against pinned AstrBot classes. No QQ connection or model.

Run with envs/astrbot/Scripts/python.exe. Transport and inference are local fakes;
the event, message chain, provider request and tool-execution classes are real.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "external/AstrBot")]


class TimerSelector:
    def select(self, timeout=None):
        if timeout:
            time.sleep(min(timeout, 0.01))
        return []


class LocalLoop(asyncio.BaseEventLoop):
    """Timer/task scheduler with no socket transports, also usable in a sandbox."""
    def __init__(self):
        super().__init__()
        self._selector = TimerSelector()

    def _process_events(self, events):
        pass

    def _write_to_self(self):
        pass


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="q3-local-", dir=ROOT / "storage/temporary") as directory:
        state = Path(directory)
        os.environ["ASTRBOT_ROOT"] = str(state)
        os.environ["ASTRBOT_CHAT_STATE_DIR"] = str(state / "plugin")

        from astrbot.core.agent.message import Message, TextPart
        from astrbot.core.agent.tool import FunctionTool, ToolSet
        from astrbot.core.astr_agent_tool_exec import call_local_llm_tool
        from astrbot.core.astr_main_agent_resources import LLM_SAFETY_MODE_SYSTEM_PROMPT, TOOL_CALL_PROMPT
        from astrbot.core.config.astrbot_config import AstrBotConfig
        from astrbot.core.message.components import At, Plain, Reply
        from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
        from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        from astrbot.core.provider.entities import LLMResponse, ProviderRequest
        from integrations.qq_astrbot.plugin.main import Q1Guard
        from integrations.qq_astrbot.plugin.message_router import Incoming
        from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
        import yaml

        class Bot:
            def __init__(self):
                self.sent = []
                self.info_calls = 0

            async def call_action(self, action, **kwargs):
                assert action == "get_group_info", "Unexpected OneBot API"
                self.info_calls += 1
                return {"member_count": 100}

            async def send_group_msg(self, *, group_id, message, **kwargs):
                self.sent.append(("group", str(group_id), message))

            async def send_private_msg(self, *, user_id, message, **kwargs):
                self.sent.append(("dm", str(user_id), message))

        class Event(AiocqhttpMessageEvent):
            async def send(self, chain):
                # Actual adapter serialization/dispatch with a local Bot. Avoid
                # base-event telemetry, which is unrelated to QQ routing.
                await self.send_message(self.bot, chain, is_group=bool(self.get_group_id()),
                                        session_id=self.get_group_id() or self.get_sender_id())
                self._has_send_oper = True

        bot = Bot()
        counter = 0

        def event(sender="101", group="", text="你好", *, at=False, quote=False, role="member",
                  post_type="message", members=100):
            nonlocal counter
            counter += 1
            obj = AstrBotMessage()
            obj.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
            obj.self_id, obj.message_id = "999", str(counter)
            obj.sender = MessageMember(sender, "同名用户")
            obj.group = Group(group, member_count=members) if group else None
            obj.raw_message = {"post_type": post_type, "sender": {"role": role}}
            obj.message = [Plain(text)]
            if at:
                obj.message.insert(0, At(qq="999"))
            if quote:
                obj.message.insert(0, Reply(id="9001", sender_id="999"))
            obj.message_str = text
            # Original adapter group session may contain sender_group. Q3 must
            # replace it before AstrBot can read a conversation.
            return Event(text.lstrip("/"), obj, PlatformMetadata("aiocqhttp", "local fake", "snowluma"),
                         sender + "_" + group if group else sender, bot)

        class Context:
            def __init__(self):
                self.calls = []

            async def llm_generate(self, **kwargs):
                assert kwargs["chat_provider_id"] == "astrbot_chat_qwen3"
                self.calls.append(kwargs)
                return LLMResponse(role="assistant", completion_text='{"follow_up":null,"thinking_level":1}')

        async def run():
            schema = json.loads((ROOT / "integrations/qq_astrbot/plugin/_conf_schema.json").read_text(encoding="utf-8"))
            config = AstrBotConfig(config_path=str(state / "plugin-config.json"), schema=schema)
            assert config["auto_probability"] == 0 and config["context_messages"] == 20
            config["merge_seconds"], config["context_token_budget"] = 0.03, 8192
            plugin = Q1Guard(Context(), config)
            try:
                # Two users interleave ten messages each. Each private burst
                # produces one admitted request, preserving only that user's text.
                private = [event(str(101 + i % 2), text=f"secret-{101 + i % 2}-{i}") for i in range(20)]
                await asyncio.gather(*(plugin.filter_inbound(e) for e in private))
                admitted = [e for e in private if not e.is_stopped()]
                assert len(admitted) == 2
                for e in admitted:
                    req = ProviderRequest(system_prompt="persona")
                    await plugin.request_paced_reply(e, req)
                    other = "102" if e.get_sender_id() == "101" else "101"
                    assert f"secret-{other}" not in req.prompt
                    assert e.session_id == f"qq:999:dm:{e.get_sender_id()}"
                    assert req.session_id == e.session_id

                # Three groups, two same-name participants, ten interleaved messages.
                groups = [event(str(101 + i % 2), group, f"group-{group}-{i}", at=True)
                          for i in range(10) for group in ("201", "202", "203")]
                await asyncio.gather(*(plugin.filter_inbound(e) for e in groups))
                assert all(not e.is_stopped() for e in groups)
                for e in groups[-3:]:
                    req = ProviderRequest(system_prompt="persona", contexts=[{"role": "user", "content": "legacy private secret"}])
                    await plugin.request_paced_reply(e, req)
                    assert req.contexts == [] and "secret-" not in req.prompt
                    rows = json.loads(req.prompt)["recent_messages"]
                    assert len(rows) == 10
                    assert {r["sender_uin"] for r in rows} == {"101", "102"}
                    assert all(f"group-{e.get_group_id()}-" in r["text"] for r in rows)
                    assert e.session_id == f"qq:999:group:{e.get_group_id()}"

                # The actual OneBot adapter sends to the numeric recipient,
                # despite canonical session keys containing colons.
                for e in [admitted[0], groups[-1]]:
                    await e.send(MessageEventResult().message("local only"))
                    assert bot.sent[-1][1] == (e.get_group_id() or e.get_sender_id())

                passive = event(group="201", text="普通群聊")
                await plugin.filter_inbound(passive)
                assert passive.is_stopped()
                quoted = event(group="201", quote=True)
                await asyncio.wait_for(plugin.filter_inbound(quoted), timeout=0.1)
                assert quoted.get_extra("qq_route").trigger == "reply"
                duplicate = event(group="201", at=True)
                duplicate.message_obj.message_id = groups[0].message_obj.message_id
                await plugin.filter_inbound(duplicate)
                assert duplicate.is_stopped()
                for e in (event(sender="999", at=True), event(post_type="notice"),
                          event(group="301", at=True, members=2001)):
                    await plugin.filter_inbound(e)
                    assert e.is_stopped()

                # Stop a group after inference but before decoration/send.
                pending = event(group="401", at=True)
                await plugin.filter_inbound(pending)
                pending.set_extra("qq_thinking_level", 1)
                pending.set_result(MessageEventResult().message("应被取消").set_result_content_type(ResultContentType.LLM_RESULT))
                command = event(group="401", text="/q3 off", role="admin")
                await plugin.filter_inbound(command)
                await plugin.delay_paced_reply(pending)
                assert pending.is_stopped() and not pending.get_result().chain
                command_on = event(group="401", text="/q3 on", role="admin")
                await plugin.filter_inbound(command_on)
                assert not plugin._event_active(pending), "Re-enabling revived a canceled reply"
                allowed = event(group="402", at=True)
                await plugin.filter_inbound(allowed)
                assert not allowed.is_stopped()

                # Existing sentence pacing keeps all text, bounded to three
                # outgoing messages in a group and without a second model call.
                allowed.set_extra("qq_reply_started_at", time.monotonic() - 100)
                raw = '{"reply":"一。二。三。四。五。","thinking_level":1}'
                response = LLMResponse(role="assistant", completion_text=raw)
                history = SimpleNamespace(messages=[Message(role="assistant", content=[TextPart(text=raw)])])
                await plugin.unpack_paced_reply(allowed, history, response)
                allowed.set_result(MessageEventResult().message(response.completion_text).set_result_content_type(ResultContentType.LLM_RESULT))
                await plugin.delay_paced_reply(allowed)
                before = len(bot.sent)
                await allowed.send(allowed.get_result())
                await plugin.finish_paced_reply(allowed)
                assert len(bot.sent) - before == 3
                assert history.messages[-1].content[0].text == "一。二。三。四。五。"

                # Exercise the real AstrBot tool-call convention: event is
                # positional, schema arguments are keyword arguments.
                request = ProviderRequest(func_tool=ToolSet([
                    FunctionTool(name="q2_echo", description="echo", parameters={"type": "object"}, handler=plugin.q2_echo),
                    FunctionTool(name="astrbot_execute_shell", description="blocked", parameters={"type": "object"}, handler=plugin.q2_echo),
                ]))
                await plugin.request_paced_reply(allowed, request)
                assert [t.name for t in request.func_tool.tools] == ["q2_echo"]
                tool = request.func_tool.tools[0]
                context = SimpleNamespace(context=SimpleNamespace(event=allowed))
                results = [r async for r in call_local_llm_tool(context, tool.handler, "decorator_handler", value="q3-local")]
                assert results == ["character_id=elysia; echo=q3-local"]
                plugin.router.control(Incoming("999", "101", "control", "/q3 off", "402", is_group_admin=True))
                try:
                    await tool.handler(allowed, value="blocked")
                except PermissionError:
                    pass
                else:
                    raise AssertionError("Tool executed after group was disabled")

                # Unknown group sizes are queried once, then cached; no member-list request.
                unknown = event(group="501", text="unaddressed", members=None)
                await plugin.filter_inbound(unknown)
                unknown2 = event(group="501", text="unaddressed", members=None)
                await plugin.filter_inbound(unknown2)
                assert bot.info_calls == 1

                # Saved edits must affect the actual next main/follow-up payload,
                # including upstream blocks, while preserving the persona.
                prompts = load_prompts()
                for key in ("qq_context", "followup_system", "followup_user", "astrbot_safety",
                            "astrbot_tool_call", "datetime_reminder", "echo_description", "echo_value_description"):
                    prompts[key] = "FILE " + key
                prompt_file = state / "editable-prompts.yaml"
                prompt_file.write_text(yaml.safe_dump(prompts), encoding="utf-8")
                os.environ["ASTRBOT_CHAT_PROMPTS_FILE"] = str(prompt_file)
                edited = event(text="local prompt edit")
                await plugin.filter_inbound(edited)
                req = ProviderRequest(
                    system_prompt=LLM_SAFETY_MODE_SYSTEM_PROMPT + "\nPERSONA UNCHANGED\n" + TOOL_CALL_PROMPT,
                    extra_user_content_parts=[TextPart(text="<system_reminder>Current datetime: 2026-10-28 18:30 (HKT), Weekday: Wednesday</system_reminder>")],
                    func_tool=ToolSet([FunctionTool(name="q2_echo", description="old description",
                        parameters={"type": "object", "properties": {"value": {"type": "string"}}}, handler=plugin.q2_echo)]))
                await plugin.request_paced_reply(edited, req)
                assert "PERSONA UNCHANGED" in req.system_prompt
                for key in ("qq_context", "astrbot_safety", "astrbot_tool_call"):
                    assert "FILE " + key in req.system_prompt
                assert req.extra_user_content_parts[0].text == "FILE datetime_reminder"
                assert req.func_tool.tools[0].description == "FILE echo_description"
                assert req.func_tool.tools[0].parameters["properties"]["value"]["description"] == "FILE echo_value_description"
                edited.set_extra("qq_run_context", SimpleNamespace(messages=[Message(role="assistant", content="已回复")]))
                await plugin._maybe_followup(edited)
                assert plugin.context.calls[-1]["contexts"][-1]["content"] == "FILE followup_user"
                assert "FILE followup_system" in plugin.context.calls[-1]["system_prompt"]
                assert "PERSONA UNCHANGED" in plugin.context.calls[-1]["system_prompt"]
                prompt_file.write_text("invalid: [", encoding="utf-8")
                broken = event(group="601", at=True)
                await plugin.filter_inbound(broken)
                await plugin.request_paced_reply(broken, ProviderRequest())
                assert broken.is_stopped(), "Broken prompt configuration did not stop inference"
                print("Q3_LOCAL_PASS: private interleave, three groups, canonical sessions, real adapter dispatch, "
                      "TTL context, triggers, dedup, admin stop, bounded pacing, real tool execution, schema, cache and editable prompts.")
                print("Transport: local fake only. QQ connections: 0. Model calls: 0.")
            finally:
                await plugin.terminate()

        with asyncio.Runner(loop_factory=LocalLoop) as runner:
            runner.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
