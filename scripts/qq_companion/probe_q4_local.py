"""Q4 integration against pinned AstrBot classes, with zero QQ/model connections."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import wave
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "external/AstrBot")]
from scripts.qq_companion.probe_q3_local import LocalLoop


def main():
    with tempfile.TemporaryDirectory(prefix="q4-local-", dir=ROOT / "storage/temporary") as temp:
        directory = Path(temp)
        os.environ["ASTRBOT_ROOT"] = str(directory)
        os.environ["ASTRBOT_CHAT_STATE_DIR"] = str(directory)
        os.environ.pop("ASTRBOT_CHAT_PROMPTS_FILE", None)
        from astrbot.core.agent.message import Message, TextPart
        from astrbot.core.message.components import At, Plain, Record
        from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
        from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        from astrbot.core.provider.entities import LLMResponse, ProviderRequest
        from integrations.qq_astrbot.plugin.main import Q1Guard
        from scripts.qq_companion.init_stickers import initialize

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 16000)
        audio = buffer.getvalue()
        events, requests = [], []
        song_order = []

        class Bot:
            async def call_action(self, action, **kwargs):
                assert action == "get_record"
                assert kwargs["file"] == "opaque-record-id"
                return {"file": "base64://" + base64.b64encode(audio).decode()}

        class Context:
            async def send_message(self, origin, chain):
                assert origin == "snowluma:FriendMessage:101"
                song_order.append("send")
                events.append(("background", [part.type.value.lower() for part in chain.chain]))
                return True

            async def llm_generate(self, **kwargs):
                assert kwargs["chat_provider_id"] == "astrbot_chat_qwen3"
                song_order.append("generate")
                return LLMResponse(role="assistant", completion_text='{"follow_up":"唱得怎么样呀？","thinking_level":2}')

        class Event(AiocqhttpMessageEvent):
            async def send(self, chain):
                events.append(("event", [part.type.value.lower() for part in chain.chain]))
                self._has_send_oper = True

        counter = 0
        def event(*, voice=False, group="", text="你好"):
            nonlocal counter
            counter += 1
            obj = AstrBotMessage()
            obj.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
            obj.self_id, obj.message_id = "999", str(counter)
            obj.sender = MessageMember("101", "本地用户")
            obj.group = Group(group, member_count=100) if group else None
            obj.raw_message = {"post_type": "message", "sender": {"role": "member"}}
            obj.message = [Record(file="opaque-record-id")] if voice else [Plain(text)]
            if group:
                obj.message.insert(0, At(qq="999"))
            obj.message_str = "[record]" if voice else text
            return Event(obj.message_str, obj, PlatformMetadata("aiocqhttp", "fake", "snowluma"), group or "101", Bot())

        async def run():
            plugin = Q1Guard(Context(), {"merge_seconds": 0})
            initialize(directory / "stickers")
            job_counter = 0

            async def request(path, body=None, **kwargs):
                nonlocal job_counter
                requests.append(path)
                if path == "/v1/audio/transcriptions":
                    assert b"RIFF" in body
                    return {"text": "本地语音转写", "language": "zh", "duration_ms": 1000}
                if path == "/v1/audio/speech":
                    assert body["character_id"] == "elysia"
                    return audio
                if path == "/v1/songs":
                    assert events[-1][0] == "event", "Song started before acknowledgement"
                    job_counter += 1
                    return {"status": "queued"}
                if "/audio?" in path:
                    return audio
                return {"status": "completed", "has_audio": True, "mode": "cover", "song": {"title": "测试歌"}}

            plugin.media.client.request = request
            try:
                for _ in range(20):
                    e = event(voice=True)
                    await plugin.filter_inbound(e)
                    assert not e.is_stopped() and e.message_str == "本地语音转写"
                    req = ProviderRequest(system_prompt="PERSONA")
                    await plugin.request_paced_reply(e, req)
                    assert json.loads(req.prompt)["input_modality"] == "voice"
                    assert "media_decisions" not in req.system_prompt  # Only body text reaches AI.
                    raw = json.dumps({"reply": "我听到啦。", "thinking_level": 1, "voice": True,
                                      "emotion": "happy", "music": None, "sticker": None}, ensure_ascii=False)
                    history = SimpleNamespace(messages=[Message(role="assistant", content=[TextPart(text=raw)])])
                    response = LLMResponse(role="assistant", completion_text=raw)
                    await plugin.unpack_paced_reply(e, history, response)
                    e.set_result(MessageEventResult().message(response.completion_text).set_result_content_type(ResultContentType.LLM_RESULT))
                    await plugin.delay_paced_reply(e)
                    assert isinstance(e.get_result().chain[0], Record)
                    assert history.messages[-1].content[0].text == "我听到啦。"
                    assert plugin.media.client.state.active() == 0

                # Q4 never routes current images to the text model.
                e = event()
                await plugin.filter_inbound(e)
                req = ProviderRequest(image_urls=["file:///not-a-real-image"])
                await plugin.request_paced_reply(e, req)
                assert req.image_urls == [] and not json.loads(req.prompt)["image_understanding_available"]
                plugin.media.client.state.leave(e.get_extra("qq_gpu_lease"))

                # TTS failure retains every text segment; long group answers do
                # not turn into a long recording or more than three messages.
                async def fail(*args):
                    raise RuntimeError("injected TTS failure")
                original = plugin.media.client.speech
                plugin.media.client.speech = fail
                chains = await plugin.media.voice_chains("一。二。三。四。", "sad", group=True)
                assert len(chains) == 3 and all(isinstance(c.chain[0], Plain) for c in chains)
                assert "".join(c.chain[0].text for c in chains) == "一。二。三。四。"
                assert await plugin.media.voice_chains("长" * 181, "neutral", group=True) == []
                plugin.media.client.speech = original

                for i in range(20):
                    e = event(group=str(200 + i))
                    await plugin.filter_inbound(e)
                    await plugin.media.sticker(e, "happy")
                    count = len(events)
                    await plugin.media.sticker(e, "happy")
                    assert len(events) == count, "Sticker cooldown failed"
                assert sum("image" in kinds for _, kinds in events) == 20

                e = event(text="明确请你唱完整测试歌")
                await plugin.filter_inbound(e)
                req = ProviderRequest(system_prompt="PERSONA")
                await plugin.request_paced_reply(e, req)
                raw = json.dumps({"reply": "等我一会呀～", "thinking_level": 1, "voice": False,
                    "music": {"mode": "cover", "query": "测试歌", "artist": ""}}, ensure_ascii=False)
                history = SimpleNamespace(messages=[Message(role="assistant", content=[TextPart(text=raw)])])
                response = LLMResponse(role="assistant", completion_text=raw)
                await plugin.unpack_paced_reply(e, history, response)
                e.set_extra("qq_reply_started_at", time.monotonic() - 100)
                e.set_result(MessageEventResult().message(response.completion_text).set_result_content_type(ResultContentType.LLM_RESULT))
                await plugin.delay_paced_reply(e)
                before = len(events)
                await e.send(e.get_result())
                await plugin.finish_paced_reply(e)
                await asyncio.gather(*list(plugin.media.tasks))
                assert events[before:] == [("event", ["plain"]), ("background", ["record"]), ("background", ["plain"])]
                assert song_order == ["generate", "send", "send"]
                assert job_counter == 1
                plugin.media.start_song(e, e.get_extra("qq_media_decision"))
                assert not plugin.media.tasks, "Duplicate message created a second song task"

                # Completed audio must wait through restoration and recovery
                # failure, then wait for generation before either message.
                payload = {"scope": "qq:999:dm:101", "music": {"mode": "cover", "query": "测试歌", "artist": ""},
                    "origin": "snowluma:FriendMessage:101", "group": "", "generation": None,
                    "persona": "PERSONA", "history_origin": "unused-local-history"}
                for mode in ("restoring", "recovering"):
                    key = ("a" if mode == "restoring" else "b") * 32
                    state = plugin.media.client.state
                    assert state.remember(key, payload)
                    terminal, generating, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
                    before, prior = len(events), len(song_order)

                    async def completed(path, body=None, **kwargs):
                        if path == "/v1/songs":
                            return {"status": "completed"}
                        if "/audio?" in path:
                            return audio
                        state.set_mode(mode)
                        terminal.set()
                        return {"status": "completed", "has_audio": True, "song": {"title": "测试歌"}}

                    async def delayed_reply(**kwargs):
                        assert state.active() == 1, "Generation has no GPU lease"
                        song_order.append("generate")
                        generating.set()
                        await release.wait()
                        return LLMResponse(role="assistant", completion_text='{"follow_up":"这首送给你。","thinking_level":2}')

                    plugin.media.client.request = completed
                    plugin.media.context.llm_generate = delayed_reply
                    task = asyncio.create_task(plugin.media.deliver_song(key, payload))
                    await terminal.wait()
                    await asyncio.sleep(0.01)
                    assert len(events) == before and len(song_order) == prior
                    if mode == "recovering":
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        assert (key, payload) in state.recover(), "Waiting delivery lost on shutdown"
                        task = asyncio.create_task(plugin.media.deliver_song(key, payload))
                        await asyncio.sleep(0.01)
                        assert len(events) == before and len(song_order) == prior
                    state.set_mode("ready")
                    await asyncio.wait_for(generating.wait(), timeout=2)
                    assert len(events) == before, "Music sent before reply generation finished"
                    # Another cover can close admission but must drain our
                    # existing lease, including both sends, before unloading.
                    state.set_mode("draining")
                    assert state.active() == 1
                    release.set()
                    await asyncio.wait_for(task, timeout=2)
                    assert song_order[prior:] == ["generate", "send", "send"]
                    assert events[before:] == [("background", ["record"]), ("background", ["plain"])]
                    assert state.active() == 0
                    with state.connection() as db:
                        assert db.execute("SELECT state FROM delivery WHERE id=?", (key,)).fetchone()[0] == "sent"
                    state.set_mode("ready")

                async def completed_ready(path, body=None, **kwargs):
                    if "/audio?" in path:
                        return audio
                    return {"status": "completed", "has_audio": True, "song": {"title": "测试歌"}}

                plugin.media.client.request = completed_ready
                key = "c" * 32
                assert state.remember(key, payload)
                model_outputs = ['普通聊天正文\nsticker:开心', '{"follow_up":null,"thinking_level":2}',
                                 '{"follow_up":"这首送给你。","thinking_level":2}']
                calls = 0

                async def invalid_then_valid(**kwargs):
                    nonlocal calls
                    assert kwargs["request_max_retries"] == 1
                    assert "follow_up" in kwargs["prompt"]
                    raw = model_outputs[calls]
                    calls += 1
                    return LLMResponse(role="assistant", completion_text=raw)

                plugin.media.context.llm_generate = invalid_then_valid
                before = len(events)
                await plugin.media.deliver_song(key, payload)
                assert calls == 3 and len(events) == before + 2
                with state.connection() as db:
                    audit = db.execute("SELECT status,raw,reply,error FROM song_reply_attempts WHERE job_id=? ORDER BY id", (key,)).fetchall()
                assert [row[0] for row in audit] == ["rejected", "rejected", "generated"]
                assert [row[1] for row in audit] == model_outputs
                assert audit[-1][2] == "这首送给你。"

                key = "d" * 32
                assert state.remember(key, payload)
                calls = 0

                async def unavailable_reply(**kwargs):
                    nonlocal calls
                    calls += 1
                    raise TimeoutError("injected model error")

                plugin.media.context.llm_generate = unavailable_reply
                before = len(events)
                await plugin.media.deliver_song(key, payload)
                assert calls == 3 and len(events) == before, "Canned success sent after reply failure"
                assert (key, payload) in state.recover()
                with state.connection() as db:
                    audit = db.execute("SELECT error FROM song_reply_attempts WHERE job_id=?", (key,)).fetchall()
                assert audit == [("TimeoutError",)] * 3
                assert state.active() == 0

                # Recovery reuses the completed song and sends only once after
                # a valid model line is finally available.
                async def recovered_reply(**kwargs):
                    return LLMResponse(role="assistant", completion_text=model_outputs[-1])

                plugin.media.context.llm_generate = recovered_reply
                await plugin.media.deliver_song(key, payload)
                assert len(events) == before + 2
                await plugin.media.deliver_song(key, payload)
                assert len(events) == before + 2, "Recovered result sent twice"

                # Exercise the production two-stage store/API/dispatcher flow
                # with local source, inference, worker and transport stand-ins.
                from services.media_api.app import create_app, SongPayload, SongSelectionPayload
                from services.media_api.songs import SongStore
                from services.media_api.song_source import Song
                from scripts.qq_companion.cover_dispatcher import CoverDispatcher
                endpoints = {r.path: r.endpoint for r in create_app().routes}
                jobs = SongStore(directory)
                candidates = [Song("netease", 1, "测试歌", "歌手", 220, True),
                              Song("netease", 3, "测试歌指定版", "指定歌手", 250, True)]
                for suffix, choice, empty in (("e", 3, False), ("f", None, False), ("1", None, True)):
                    key, flow = suffix * 32, []
                    current_payload = {**payload, "user_request": "翻唱测试歌的指定版",
                                       "request_context": [{"text": "指定版"}]}
                    assert state.remember(key, current_payload)

                    class Source:
                        def search(self, query):
                            assert query == "测试歌"
                            flow.append("search")
                            return [] if empty else candidates

                    def stop(names):
                        assert state.active() == 0 and flow[-1] == "select"
                        names.append("local-model")
                        flow.append("stop")

                    def work(job_key):
                        assert jobs.get(job_key)["song"]["song_id"] == 3
                        flow.append("worker")
                        jobs.output(job_key).parent.mkdir(parents=True, exist_ok=True)
                        jobs.output(job_key).write_bytes(audio)

                    dispatcher = CoverDispatcher(directory, broker=SimpleNamespace(file=object()), source=Source(),
                        stop_models=stop, restore_models=lambda names: flow.append("restore"),
                        verify_gpu=lambda: None, run_worker=work)

                    async def local_api(path, body=None, **kwargs):
                        if path == "/v1/songs":
                            return endpoints[path](SongPayload(**body))
                        if "/select?" in path:
                            return endpoints["/v1/songs/{job_id}/select"](key, current_payload["scope"], SongSelectionPayload(**body))
                        if "/audio?" in path:
                            return jobs.output(key).read_bytes()
                        if jobs.get(key)["status"] in ("queued", "waiting_gpu"):
                            dispatcher.step()
                        return jobs.public(key, current_payload["scope"])

                    async def local_model(**kwargs):
                        data = json.loads(kwargs["prompt"].rsplit("\n", 1)[1])
                        if "candidates" in data:
                            assert data["user_request"] == "翻唱测试歌的指定版"
                            assert state.active() == 1 and jobs.get(key)["status"] == "awaiting_selection"
                            flow.append("select")
                            return LLMResponse(role="assistant", completion_text=json.dumps({"song_id": choice}),
                                               reasoning_content="local simulated reasoning")
                        flow.append("result")
                        status = data["result"]["status"]
                        assert status == ("needs_clarification" if choice is None else "completed")
                        line = "没有找到这个版本，具体叫什么呀？" if choice is None else "嗯…这首送给你。"
                        return LLMResponse(role="assistant", completion_text=json.dumps({"follow_up": line, "thinking_level": 2}))

                    plugin.media.client.request = local_api
                    plugin.media.context.llm_generate = local_model
                    before = len(events)
                    await plugin.media.deliver_song(key, current_payload)
                    expected = ["search", "select", "stop", "worker", "restore", "result"] if choice else (
                        ["search", "result"] if empty else ["search", "select", "result"])
                    assert flow == expected, flow
                    assert events[before:] == ([("background", ["plain"])] if choice is None else [
                        ("background", ["record"]), ("background", ["plain"])])
                    with state.connection() as db:
                        expected_line = "没有找到这个版本，具体叫什么呀？" if choice is None else "嗯…这首送给你。"
                        assert db.execute("SELECT reply FROM song_reply_attempts WHERE job_id=? AND status='generated'", (key,)).fetchone()[0] == expected_line
                print("Q4_LOCAL_PASS: voice input 20, voice output 20, stickers 20, fallback, long text, "
                      "acknowledgement before background job, generation before result delivery, "
                      "restore/recovery wait, pending restart, GPU lease through both sends, same recipient, dedup, "
                      "model reply retry/audit, no canned success, pending reply recovery, "
                      "search/AI choice/worker/result order and clarification.")
                print("QQ connections: 0. Model calls: 0. Transport and inference: local fakes.")
            finally:
                await plugin.terminate()

        with asyncio.Runner(loop_factory=LocalLoop) as runner:
            runner.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
