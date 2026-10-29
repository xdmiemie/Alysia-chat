"""Q4 voice, stickers and asynchronous song delivery on the current session."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, build_opener

from astrbot.core.message.components import Image, Plain, Record
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core import logger

from .api_client import MediaClient
from .media_decision import parse_song_reply, parse_song_selection
from .prompt_loader import load_prompts
from .reply_pacing import split_sentences


class SongReplyError(RuntimeError):
    """Audio stays pending when its model-generated reply is unavailable."""


class MediaRouter:
    def __init__(self, context, directory: Path, *, character_id="elysia"):
        self.context, self.client = context, MediaClient(directory)
        self.character_id = character_id
        self.tasks = set()
        self.resumed = False
        self.sticker_last = {}
        self.active_group = lambda scope, generation: True
        self.record_group_result = lambda scope, key, text: None

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def resume(self):
        if not self.resumed:
            self.resumed = True
            for key, payload in self.client.state.recover():
                self.spawn(self.deliver_song(key, payload))

    async def input_voice(self, event, record):
        # Only the trusted OneBot get_record response can supply a path/URL.
        info = await asyncio.wait_for(event.bot.call_action("get_record", file=record.file,
                                                           out_format="wav"), timeout=20)
        if isinstance(info, dict) and isinstance(info.get("data"), dict):
            info = info["data"]
        if not isinstance(info, dict):
            raise ValueError("Invalid get_record response")
        location = info.get("file") or info.get("url")
        if not isinstance(location, str):
            raise ValueError("get_record did not return media")

        def read():
            if location.startswith("base64://"):
                if len(location) > 17 * 1024 * 1024:
                    raise ValueError("Audio too large")
                return base64.b64decode(location[9:], validate=True)
            parsed = urlparse(location)
            if parsed.scheme in {"https", "http"}:
                import ipaddress
                import socket
                host = parsed.hostname or ""
                local = host == "127.0.0.1" and parsed.port == 3000
                remote = parsed.scheme == "https" and host.endswith((".qq.com", ".qpic.cn"))
                if not local and not remote:
                    raise ValueError("Untrusted voice host")
                if remote and any(not ipaddress.ip_address(item[4][0]).is_global for item in
                                  socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)):
                    raise ValueError("Private voice address")
                opener = build_opener(ProxyHandler({}))
                # Refuse redirects, including a valid QQ host redirecting to localhost.
                from urllib.request import HTTPRedirectHandler
                class NoRedirect(HTTPRedirectHandler):
                    def redirect_request(self, *args, **kwargs):
                        return None
                opener.add_handler(NoRedirect())
                with opener.open(location, timeout=20) as response:
                    return response.read(12 * 1024 * 1024 + 1)
            path = Path(location)
            if not path.is_absolute() or not path.is_file() or path.stat().st_size > 12 * 1024 * 1024:
                raise ValueError("Invalid trusted voice file")
            return path.read_bytes()

        result = await self.client.transcribe(await asyncio.to_thread(read))
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("No speech recognized")
        return text.strip()[:2000]

    async def voice_chains(self, text: str, emotion: str, *, group: bool):
        if len(text) > (180 if group else 1500):
            return []  # Long answers remain readable, including after TTS loss.
        chunks = []
        for sentence in split_sentences(text):
            while sentence:
                chunks.append(sentence[:120])
                sentence = sentence[120:]
        chains = []
        limit = 3 if group else 6
        if len(chunks) > limit:
            chunks = chunks[:limit - 1] + ["".join(chunks[limit - 1:])]
        for chunk in chunks:
            try:
                data = await self.client.speech(chunk, emotion, self.character_id)
                audio = self.client.save_audio(data)
                chains.append(MessageChain(chain=[Record.fromFileSystem(str(audio))]))
            except Exception:
                chains.append(MessageChain(chain=[Plain(chunk)]))
        return chains

    async def sticker(self, event, tag):
        if tag is None:
            return
        scope = event.get_extra("qq_scope")
        last = self.sticker_last.get(scope, 0)
        if time.monotonic() - last < 60:
            return
        path = self.client.directory / "stickers" / (tag + ".png")
        if path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
            await event.send(MessageChain(chain=[Image.fromFileSystem(str(path))]))
            self.sticker_last[scope] = time.monotonic()
            if len(self.sticker_last) > 512:
                self.sticker_last.pop(next(iter(self.sticker_last)))

    def start_song(self, event, decision):
        if not decision.music:
            return
        incoming = event.get_extra("qq_incoming")
        key = hashlib.sha256(f"{incoming.scope}:{incoming.message_id}".encode()).hexdigest()[:32]
        payload = {"scope": incoming.scope, "music": decision.music,
            "user_request": incoming.text[:2000], "request_context": event.get_extra("qq_music_context", []),
            "origin": event.get_extra("qq_transport_origin"), "group": incoming.group_id,
            "generation": event.get_extra("qq_control_generation"),
            "persona": event.get_extra("qq_persona_prompt", ""),
            "history_origin": event.unified_msg_origin}
        if self.client.state.remember(key, payload):
            self.spawn(self.deliver_song(key, payload))

    async def _send(self, payload, chain):
        if payload["group"] and not self.active_group(payload["scope"], payload["generation"]):
            raise PermissionError("Group stopped before media delivery")
        if not await self.context.send_message(payload["origin"], chain):
            raise RuntimeError("Original message transport is unavailable")

    async def deliver_song(self, key, payload, attempt=0):
        state = self.client.state
        claimed = False
        try:
            await self.client.request("/v1/songs", {"request_id": key, "scope": payload["scope"], "music": payload["music"],
                "user_request": payload.get("user_request", ""), "request_context": payload.get("request_context", [])})
            deadline = time.monotonic() + 2400
            query = urlencode({"scope": payload["scope"]})
            while time.monotonic() < deadline:
                if payload["group"] and not self.active_group(payload["scope"], payload["generation"]):
                    await self.client.request(f"/v1/songs/{key}/cancel?{query}", {})
                    state.mark(key, "canceled")
                    return
                job = await self.client.request(f"/v1/songs/{key}?{query}")
                if job["status"] == "awaiting_selection":
                    async with self.client.admission():
                        song_id = await self.select_song(key, payload, job["search_results"])
                    job = await self.client.request(f"/v1/songs/{key}/select?{query}", {"song_id": song_id})
                if job["status"] in {"completed", "failed", "canceled", "needs_clarification"}:
                    break
                await asyncio.sleep(1)
            else:
                await self.client.request(f"/v1/songs/{key}/cancel?{query}", {})
                job = {"status": "failed", "error": "timeout", "has_audio": False}
            if job["status"] == "canceled":
                state.mark(key, "canceled")
                return
            # Recovery failure keeps the delivery pending until models recover.
            while state.mode() != "ready":
                await asyncio.sleep(0.25)
            audio = None
            if job.get("has_audio"):
                data = await self.client.request(f"/v1/songs/{key}/audio?{query}", binary=True)
                audio = self.client.save_audio(data)
            # Hold one lease through generation and both sends. A subsequent
            # cover must wait, and no music is sent before its reply is ready.
            async with self.client.admission():
                line = await self.result_line(payload, job, key=key)
                if not state.claim_send(key):
                    return
                claimed = True
                if audio:
                    # Do not retry a potentially delivered record as a second
                    # media message; a transport exception is an uncertain send.
                    await self._send(payload, MessageChain(chain=[Record.fromFileSystem(str(audio))]))
                elif job.get("song", {}).get("link"):
                    await self._send(payload, MessageChain(chain=[Plain(job["song"]["link"])]))
                await self._send(payload, MessageChain(chain=[Plain(line)]))
                state.mark(key, "sent")
            await self.remember_result(key, payload, job, line)
        except asyncio.CancelledError:
            # Pending jobs resume on the next plugin startup. Interrupted sends
            # stay uncertain and are never automatically duplicated.
            if claimed:
                state.mark(key, "uncertain")
            raise
        except SongReplyError:
            # Never replace a model's rejected output with a canned success
            # line. No transport was attempted; startup can resume this job.
            logger.error(f"Song delivery pending: model reply unavailable for job {key}")
        except Exception:
            if claimed:
                state.mark(key, "uncertain")
            else:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    return await self.deliver_song(key, payload, attempt + 1)
                # Report bounded failure, never leave the user waiting forever.
                try:
                    async with self.client.admission():
                        if state.claim_send(key):
                            await self._send(payload, MessageChain(chain=[Plain("这次没能把歌准备好，晚点再试试吧。")]))
                            state.mark(key, "sent")
                except Exception:
                    state.mark(key, "uncertain")

    async def remember_result(self, key, payload, job, line):
        title = job.get("song", {}).get("title", payload["music"]["query"])
        kind = "整首翻唱" if payload["music"]["mode"] == "cover" else "原唱"
        receipt = f"[已发送《{title}》{kind}] {line}" if job.get("has_audio") else line
        if payload["group"]:
            self.record_group_result(payload["scope"], key, receipt)
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return
        # Use the same lock as AstrBot's Runner; never overwrite a newer turn.
        from astrbot.core.utils.session_lock import session_lock_manager
        origin = payload["history_origin"]
        async with session_lock_manager.acquire_lock(origin):
            cid = await manager.get_curr_conversation_id(origin)
            if cid:
                conversation = await manager.get_conversation(origin, cid)
                if conversation:
                    history = json.loads(conversation.history)
                    history.append({"role": "assistant", "content": receipt})
                    await manager.update_conversation(origin, cid, history=history)

    async def result_line(self, payload, job, *, key):
        # Caller holds the GPU lease until generation and delivery finish.
        for attempt in range(1, 4):
            raw = ""
            try:
                task = load_prompts()["music_clarification" if job["status"] == "needs_clarification" else "music_result"]
                result_view = {name: value for name, value in job.items() if name not in {"search_results", "search_history"}}
                result = await asyncio.wait_for(self.context.llm_generate(chat_provider_id="astrbot_chat_qwen3",
                    system_prompt=payload["persona"] + "\n" + task,
                    # The last turn states the current task explicitly, rather
                    # than leaving only data after a long persona/skills block.
                    prompt=task + "\n" + json.dumps({"request": payload["music"],
                        "user_request": payload.get("user_request", ""), "result": result_view}, ensure_ascii=False),
                    max_tokens=160, temperature=0.5, request_max_retries=1), timeout=45)
                raw = result.completion_text or ""
                line = parse_song_reply(raw)
                if line:
                    self.client.state.record_song_reply(key, attempt, status="generated", raw=raw, reply=line)
                    return line
                error = "invalid_model_output"
            except Exception as exc:
                error = type(exc).__name__
            self.client.state.record_song_reply(key, attempt, status="rejected", raw=raw, error=error)
            logger.warning(f"Song result reply rejected: job={key}, attempt={attempt}/3, error={error}")
            if attempt < 3:
                await asyncio.sleep(attempt)
        raise SongReplyError("Model result reply failed after three attempts")

    async def select_song(self, key, payload, candidates):
        for attempt in range(1, 4):
            raw, reasoning_chars = "", 0
            try:
                task = load_prompts()["music_selection"]
                data = {"candidates": candidates}
                data.update(music=payload["music"], request_context=payload.get("request_context", []),
                            user_request=payload.get("user_request", ""))
                result = await asyncio.wait_for(self.context.llm_generate(chat_provider_id="astrbot_chat_qwen3",
                    system_prompt=task,
                    prompt=task + "\n" + json.dumps(data, ensure_ascii=False), request_max_retries=1), timeout=45)
                raw = result.completion_text or ""
                reasoning_chars = len(getattr(result, "reasoning_content", "") or "")
                valid, song_id = parse_song_selection(raw, candidates)
                if valid:
                    self.client.state.record_song_selection(key, attempt, status="selected", raw=raw,
                        song_id=song_id, reasoning_chars=reasoning_chars)
                    return song_id
                error = "invalid_model_selection"
            except Exception as exc:
                error = type(exc).__name__
            self.client.state.record_song_selection(key, attempt, status="rejected", raw=raw,
                error=error, reasoning_chars=reasoning_chars)
            logger.warning(f"Song selection rejected: job={key}, attempt={attempt}/3, error={error}")
            if attempt < 3:
                await asyncio.sleep(attempt)
        raise SongReplyError("Model selection failed after three attempts")

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.client.state.forget_process()
