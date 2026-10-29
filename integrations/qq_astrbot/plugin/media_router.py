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
from types import SimpleNamespace

from astrbot.core.message.components import Image, Plain, Record
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core import logger

from .api_client import MediaClient
from .media_decision import parse_song_reply
from .prompt_loader import load_prompts
from .reply_pacing import split_sentences
from .reply_content import chat_history, has_control_markup, speech_parts
from .song_tasks import SongTasks, SongCanceled, SongHeld, SongFinished, SongDeliveryUncertain, SongDeliveryBot
from .request_provider import request_chat
from .context_budget import tool_result_view

RESELECT_FORMAT = {"type": "json_schema", "json_schema": {"name": "song_reselection", "strict": True,
    "schema": {"type": "object", "properties": {"candidate": {"type": ["integer", "null"]}},
               "required": ["candidate"], "additionalProperties": False}}}


class SongReplyError(RuntimeError):
    """Audio stays pending when its model-generated reply is unavailable."""


class MediaRouter:
    def __init__(self, context, directory: Path, *, character_id="elysia"):
        self.context, self.client = context, MediaClient(directory)
        self.character_id = character_id
        self.song_tasks = SongTasks(directory / "song_tasks.sqlite3")
        self.tasks = set()
        self.resumed = False
        self.sticker_last = {}
        self.active_group = lambda scope, generation: True
        self.active_version = lambda scope, version: True
        self.agent_store = None
        self.run_status = lambda run_id: None
        self.run_active = lambda run_id: False
        self.record_delivery = lambda run_id, kind, payload: None
        self.record_group_result = lambda scope, key, text: None

    async def model_request(self, phase, **kwargs):
        provider = await self.context.provider_manager.get_provider_by_id("astrbot_chat_qwen3")
        if provider is None:
            raise RuntimeError("local_chat_provider_unavailable")
        return await request_chat(provider, phase=phase, **kwargs)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def resume(self):
        if not self.resumed:
            self.resumed = True
            self.song_tasks.reset_holds()
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

    @staticmethod
    def speech_segments(text: str, *, group: bool):
        if has_control_markup(text):
            raise ValueError("speech_contains_control_markup")
        chunks = []
        for content, voice in speech_parts(text):
            for sentence in split_sentences(content) if voice else [content]:
                while sentence:
                    chunks.append((sentence[:120], voice))
                    sentence = sentence[120:]
        limit = 3 if group else 6
        if len(chunks) > limit:
            tail = chunks[limit - 1:]
            chunks = chunks[:limit - 1] + [(''.join(content for content, _ in tail), all(voice for _, voice in tail))]
        return chunks

    @staticmethod
    def speech_texts(text: str, *, group: bool):
        return [content for content, _ in MediaRouter.speech_segments(text, group=group)]

    async def voice_chains(self, text: str, emotion: str, *, group: bool):
        if len(text) > (180 if group else 1500):
            return []
        chains = []
        for chunk, voice in self.speech_segments(text, group=group):
            if not voice:
                chains.append(MessageChain(chain=[Plain(chunk)]))
                continue
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

    def _guard_song_task(self, payload, *, ready=True):
        self.song_tasks.guard(payload, ready=ready)
        if payload["group"] and not self.active_group(payload["scope"], payload["generation"]):
            self.song_tasks.finish(payload["task_id"], "canceled")
            raise SongCanceled("group_control_changed")

    async def cancel_song_tasks(self, rows):
        results = []
        for row in rows:
            try:
                await self.client.request(f"/v1/songs/{row['id']}/cancel?" + urlencode({"scope": row['scope']}), {})
                self.client.state.mark(row['id'], "canceled")
                results.append({"job_id": row['id'], "status": "canceled", "audio_sent": self.song_tasks.part_sent(row['id'], "audio")})
            except Exception as exc:
                # The durable task guard already forbids delivery even if the
                # worker cancellation request could not reach the media API.
                results.append({"job_id": row['id'], "status": "delivery_canceled", "worker_stop": "uncertain", "error": type(exc).__name__})
        return results

    async def _send(self, payload, chain, *, part=None):
        task = bool(payload.get("task_id"))
        if task:
            self._guard_song_task(payload)
            if self.song_tasks.part_sent(payload['task_id'], part):
                return False
        elif "version" in payload and not self.active_version(payload["scope"], payload["version"]):
            raise PermissionError("Conversation changed before media delivery")
        if payload["group"] and not self.active_group(payload["scope"], payload["generation"]):
            raise PermissionError("Group stopped before media delivery")
        if task or ("version" in payload and self.agent_store is not None):
            # Use AstrBot's native serializer with a per-send bot proxy. The
            # final revision check must happen after async audio/image encoding.
            from astrbot.core.platform.message_session import MessageSesion
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            from .agent_state import VersionedBot
            session = MessageSesion.from_str(payload["origin"])
            platform = self.context.get_platform_inst(session.platform_name)
            if platform is None or platform.meta().name != "aiocqhttp":
                raise RuntimeError("Original OneBot transport is unavailable")
            bot = (SongDeliveryBot(platform.bot, self.song_tasks, payload, part, self._guard_song_task) if task else
                   VersionedBot(platform.bot, self.agent_store, payload["scope"], payload["version"]))
            await AiocqhttpMessageEvent.send_message(bot, chain, is_group=bool(payload["group"]),
                                                   session_id=payload["scope"].split(":")[-1])
        elif not await self.context.send_message(payload["origin"], chain):
            raise RuntimeError("Original message transport is unavailable")
        if payload.get("run_id"):
            self.record_delivery(payload["run_id"], "sent", {"modality": "media",
                                 "text": chain.get_plain_text(), "version": payload["version"],
                                 "task_id": payload.get("task_id"), "part": part})
        return True

    async def deliver_song(self, key, payload, attempt=0):
        if payload.get("task_id"):
            return await self.deliver_task_song(key, payload)
        state = self.client.state
        claimed = False
        try:
            if not payload.get("selected_song_id"):
                await self.client.request("/v1/songs", {"request_id": key, "scope": payload["scope"], "music": payload["music"],
                    "user_request": payload.get("user_request", ""), "request_context": payload.get("request_context", [])})
            deadline = time.monotonic() + 2400
            query = urlencode({"scope": payload["scope"]})
            while time.monotonic() < deadline:
                if ((payload["group"] and not self.active_group(payload["scope"], payload["generation"])) or
                        ("version" in payload and not self.active_version(payload["scope"], payload["version"]))):
                    await self.client.request(f"/v1/songs/{key}/cancel?{query}", {})
                    state.mark(key, "canceled")
                    return
                job = await self.client.request(f"/v1/songs/{key}?{query}")
                if job["status"] == "awaiting_reselection":
                    if state.mode() != "ready":
                        await asyncio.sleep(0.25)
                        continue
                    await self.reselect_source(key, payload, job)
                    continue
                if job["status"] == "awaiting_commit" and payload.get("run_id"):
                    if self.run_status(payload["run_id"]) == "sent":
                        job = await self.client.request(f"/v1/songs/{key}/commit?{query}", {})
                    elif not self.run_active(payload["run_id"]):
                        await self.client.request(f"/v1/songs/{key}/cancel?{query}", {})
                        state.mark(key, "canceled")
                        return
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
                if "version" in payload and not self.active_version(payload["scope"], payload["version"]):
                    state.mark(key, "canceled")
                    return
                await asyncio.sleep(0.25)
            audio = None
            if job.get("has_audio"):
                data = await self.client.request(f"/v1/songs/{key}/audio?{query}", binary=True)
                audio = self.client.save_audio(data)
            # Hold one lease through generation and both sends. A subsequent
            # cover must wait, and no music is sent before its reply is ready.
            async with self.client.admission():
                line = await self.result_line(payload, job, key=key)
                if "version" in payload and not self.active_version(payload["scope"], payload["version"]):
                    state.mark(key, "canceled")
                    return
                if not state.claim_send(key):
                    return
                claimed = True
                if audio:
                    # Do not retry a potentially delivered record as a second
                    # media message; a transport exception is an uncertain send.
                    await self._send(payload, MessageChain(chain=[Record.fromFileSystem(str(audio))]))
                    await self.remember_result(key, payload, job, "")
                elif job.get("song", {}).get("link"):
                    await self._send(payload, MessageChain(chain=[Plain(job["song"]["link"])]))
                    await self.remember_result(key, payload, {**job, "has_audio": False}, job["song"]["link"])
                await self._send(payload, MessageChain(chain=[Plain(line)]))
                state.mark(key, "sent")
            await self.remember_result(key, payload, {**job, "has_audio": False}, line)
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
            if "version" in payload and not self.active_version(payload["scope"], payload["version"]):
                state.mark(key, "uncertain" if claimed else "canceled")
                return
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

    async def commit_run(self, run_id):
        with self.client.state.connection() as db:
            rows = db.execute("SELECT id,payload FROM delivery WHERE state='pending'").fetchall()
        for key, data in rows:
            payload = json.loads(data)
            if payload.get("run_id") != run_id:
                continue
            if payload.get("task_id"):
                try:
                    self._guard_song_task(payload, ready=False)
                except SongCanceled:
                    continue
            elif not self.active_version(payload["scope"], payload["version"]):
                continue
            await self.client.request(f"/v1/songs/{key}/commit?" + urlencode({"scope": payload["scope"]}), {})

    async def deliver_task_song(self, key, payload):
        """Accepted music survives unrelated dialogue; only its own token can end it."""
        state, claimed = self.client.state, False
        query = urlencode({"scope": payload['scope']})
        deadline = time.monotonic() + 2400
        try:
            while time.monotonic() < deadline:
                self._guard_song_task(payload, ready=False)
                job = await self.client.request(f"/v1/songs/{key}?{query}")
                if job['status'] == 'awaiting_reselection':
                    if state.mode() != 'ready':
                        await asyncio.sleep(0.25)
                        continue
                    try:
                        await self.reselect_source(key, payload, job)
                    except SongHeld:
                        await asyncio.sleep(0.25)
                    continue
                if job['status'] == 'awaiting_commit':
                    if self.run_status(payload['run_id']) == 'sent' or not self.run_active(payload['run_id']):
                        try:
                            self._guard_song_task(payload)
                        except SongHeld:
                            await asyncio.sleep(0.25)
                            continue
                        # The tool accepted this task. An obsolete foreground
                        # acknowledgement is not an instruction to cancel it.
                        job = await self.client.request(f"/v1/songs/{key}/commit?{query}", {})
                if job['status'] == 'canceled':
                    raise SongCanceled('worker_canceled')
                if job['status'] in {'completed', 'failed', 'needs_clarification'}:
                    break
                await asyncio.sleep(1)
            else:
                raise SongCanceled('song_task_deadline')
            audio, line = None, None
            if job.get('has_audio'):
                data = await self.client.request(f"/v1/songs/{key}/audio?{query}", binary=True)
                audio = self.client.save_audio(data)
            while time.monotonic() < deadline:
                try:
                    self._guard_song_task(payload)
                except SongHeld:
                    await asyncio.sleep(0.25)
                    continue
                if state.mode() != 'ready':
                    await asyncio.sleep(0.25)
                    continue
                try:
                    async with self.client.admission():
                        self._guard_song_task(payload)
                        if line is None:
                            line = await self.result_line(payload, job, key=key)
                        self._guard_song_task(payload)
                        if not state.claim_send(key):
                            return
                        claimed = True
                        if audio:
                            if await self._send(payload, MessageChain(chain=[Record.fromFileSystem(str(audio))]), part='audio'):
                                await self.remember_result(key, payload, job, '')
                        elif job.get('song', {}).get('link'):
                            if await self._send(payload, MessageChain(chain=[Plain(job['song']['link'])]), part='link'):
                                await self.remember_result(key, payload, {**job, 'has_audio': False}, job['song']['link'])
                        if await self._send(payload, MessageChain(chain=[Plain(line)]), part='reply'):
                            await self.remember_result(key, payload, {**job, 'has_audio': False}, line)
                        state.mark(key, 'sent')
                        self.song_tasks.finish(key, 'failed' if job['status'] == 'failed' else 'sent')
                        self.record_delivery(payload['run_id'], 'song_outcome', {'job_id': key,
                            'status': job['status'], 'has_audio': bool(audio), 'error': job.get('error', '')})
                        return
                except SongHeld:
                    # Keep this deadline and prepared prose. Each transport
                    # part has its own receipt, so audio is never replayed.
                    if claimed:
                        state.mark(key, 'pending')
                        claimed = False
                    await asyncio.sleep(0.25)
            raise SongCanceled('song_task_deadline')
        except SongFinished:
            state.mark(key, 'sent')
        except SongCanceled as exc:
            self.song_tasks.finish(key, 'canceled')
            state.mark(key, 'canceled')
            self.record_delivery(payload['run_id'], 'song_canceled', {'job_id': key, 'reason': str(exc)})
            try:
                await self.client.request(f"/v1/songs/{key}/cancel?{query}", {})
            except Exception:
                pass
        except asyncio.CancelledError:
            if claimed:
                state.mark(key, 'uncertain')
            raise
        except SongDeliveryUncertain:
            state.mark(key, 'uncertain')
            self.song_tasks.finish(key, 'uncertain')
        except Exception as exc:
            if claimed:
                state.mark(key, 'uncertain')
                self.song_tasks.finish(key, 'uncertain')
            self.record_delivery(payload['run_id'], 'song_delivery_failed', {'job_id': key, 'error': type(exc).__name__})
            logger.error(f"Song delivery pending: job={key}, error={type(exc).__name__}")

    async def remember_result(self, key, payload, job, line):
        # Audio delivery facts live in the task/part audit. Chat contains only
        # the actual words sent, never a fabricated assistant receipt prefix.
        receipt = line.strip()
        if not receipt:
            return
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
                    history = chat_history(json.loads(conversation.history))
                    history.append({"role": "assistant", "content": receipt})
                    await manager.update_conversation(origin, cid, history=history)

    async def reselect_source(self, key, payload, job):
        """One existing task; model only sees untried candidates from its snapshot."""
        def guard():
            if payload.get('task_id'):
                self._guard_song_task(payload)
            elif 'version' in payload and not self.active_version(payload['scope'], payload['version']):
                raise SongCanceled('song_request_changed')
        guard()
        pool = job['search_results']
        mapping = {i + 1: song['song_id'] for i, song in enumerate(pool)}
        remaining_ids = {song['song_id'] for song in job['remaining_candidates']}
        selected = job['song']
        equivalent = [ref for ref, song_id in mapping.items() if song_id in remaining_ids and any(
            song['song_id'] == song_id and song.get('title') == selected.get('title') and
            song.get('artists') == selected.get('artists') and
            abs(song.get('duration_seconds', 0) - selected.get('duration_seconds', 0)) <= 2
            for song in job['remaining_candidates'])]
        # Same recording metadata remains: constrain the native JSON grammar so
        # the model cannot give up before trying another matching source.
        allowed = [ref for ref, song_id in mapping.items() if song_id in remaining_ids]
        response_format = RESELECT_FORMAT
        if equivalent:
            response_format = {'type': 'json_schema', 'json_schema': {'name': 'song_reselection', 'strict': True,
                'schema': {'type': 'object', 'properties': {'candidate': {'type': 'integer', 'enum': allowed}},
                           'required': ['candidate'], 'additionalProperties': False}}}
        view = tool_result_view({'songs': job['remaining_candidates']}, SimpleNamespace(candidate_refs=mapping))
        data = {'user_request': payload.get('user_request', ''), 'request_context': payload.get('request_context', []),
            'music': payload['music'], 'failed_count': sum(item['status'] == 'forbidden' for item in job['download_attempts']),
            'equivalent_candidates': equivalent, **view}
        revision = job['retry_revision']
        audit_key = f'{key}:retry:{revision}'
        with self.client.state.connection() as db:
            used = db.execute('SELECT COALESCE(MAX(attempt),0) FROM song_selection_attempts WHERE job_id=?', (audit_key,)).fetchone()[0]
        selection, selection_error = None, True
        async with self.client.admission(timeout=180):
            for attempt in range(used + 1, 4):
                guard()
                # Reserve before network I/O; restart/timeouts cannot reset it.
                self.client.state.record_song_selection(audit_key, attempt, status='started')
                raw = ''
                try:
                    result = await asyncio.wait_for(self.model_request('selection',
                        system_prompt=load_prompts()['music_reselection'],
                        prompt=json.dumps(data, ensure_ascii=False, separators=(',', ':')),
                        response_format=response_format, func_tool=None, request_max_retries=1), timeout=45)
                    raw = result.completion_text or ''
                    value = json.loads(raw)
                    if not isinstance(value, dict) or set(value) != {'candidate'} or getattr(result, 'tools_call_name', None):
                        raise ValueError('invalid_reselection')
                    candidate = value['candidate']
                    if candidate is None and equivalent:
                        raise ValueError('matching_sources_still_untried')
                    if candidate is not None and (type(candidate) is not int or mapping.get(candidate) not in remaining_ids):
                        raise ValueError('eliminated_or_unknown_candidate')
                    selection = mapping[candidate] if candidate is not None else None
                    selection_error = False
                    self.client.state.record_song_selection(audit_key, attempt, status='selected', raw=raw, song_id=selection)
                    break
                except (SongCanceled, SongHeld):
                    raise
                except Exception as exc:
                    self.client.state.record_song_selection(audit_key, attempt, status='rejected', raw=raw, error=type(exc).__name__)
            guard()
            return await self.client.request(f'/v1/songs/{key}/select?' + urlencode({'scope': payload['scope']}),
                {'song_id': selection, 'retry_revision': revision, 'selection_error': selection_error})

    async def result_line(self, payload, job, *, key):
        # Caller holds the GPU lease until generation and delivery finish.
        for attempt in range(1, 4):
            raw = ""
            try:
                task = load_prompts()["music_clarification" if job["status"] == "needs_clarification" else "music_result"]
                result_view = tool_result_view({name: value for name, value in job.items() if name not in {
                    "search_results", "search_history", "remaining_candidates", "download_attempts"}})
                result = await asyncio.wait_for(self.model_request("music_result",
                    system_prompt=payload["persona"] + "\n" + task,
                    # The last turn states the current task explicitly, rather
                    # than leaving only data after a long persona/skills block.
                    prompt=task + "\n" + json.dumps({"request": payload["music"],
                        "user_request": payload.get("user_request", ""), "result": result_view}, ensure_ascii=False),
                    request_max_retries=1), timeout=45)
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
                candidates = candidates[:10]
                data = {"candidates": tool_result_view({"songs": candidates})}
                data.update(music=payload["music"], request_context=payload.get("request_context", []),
                            user_request=payload.get("user_request", ""))
                result = await asyncio.wait_for(self.model_request("selection",
                    system_prompt=task,
                    prompt=task + "\n" + json.dumps(data, ensure_ascii=False), request_max_retries=1), timeout=45)
                raw = result.completion_text or ""
                reasoning_chars = len(getattr(result, "reasoning_content", "") or "")
                value = json.loads(raw)
                ref = value.get("candidate") if isinstance(value, dict) and set(value) == {"candidate"} else False
                valid = ref is None or type(ref) is int and 1 <= ref <= len(candidates)
                song_id = candidates[ref - 1]["song_id"] if valid and ref is not None else None
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
