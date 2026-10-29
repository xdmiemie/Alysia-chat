"""Native AstrBot tools with explicit receipts and durable duplicate suppression."""
from __future__ import annotations

import hashlib
import json

import jsonschema
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain

from .agent_state import BudgetExhausted, Superseded
from .prompt_loader import load_prompts
from .reply_content import has_control_markup, spoken_text
from .context_budget import tool_result_view

EMOTIONS = ["neutral", "happy", "sad", "surprised", "gentle", "serious"]
STICKERS = ["happy", "sad", "surprised", "gentle", "serious"]


class FeiyuFunctionTool(FunctionTool):
    """Retain this plugin's module ownership for native unload/reload handling."""


def schemas(prompts):
    def obj(properties, required):
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    def prop(kind, key, **constraints):
        return {"type": kind, "description": prompts[key], **constraints}

    song = obj({"candidate": prop("integer", "tool_candidate", minimum=1),
                "action": prop("string", "tool_song_action", enum=["start", "cancel", "replace"])}, [])
    return {
        "emotion": obj({"text": prop("string", "tool_speech_text", minLength=1, maxLength=500),
                        "emotion": prop("string", "tool_emotion_value", enum=EMOTIONS)}, ["text", "emotion"]),
        "musiccover": song,
        "music": song,
        "sticker": obj({"tag": prop("string", "tool_sticker_tag", enum=STICKERS)}, ["tag"]),
        "weapi": obj({"action": prop("string", "tool_weapi_action", enum=["search", "resolve"]),
                      "query": prop("string", "tool_music_query", maxLength=120),
                      "song_id": prop("integer", "tool_song_id", minimum=1, maximum=999999999999),
                      "candidate": prop("integer", "tool_candidate", minimum=1),
                      "page": prop("integer", "tool_music_page", minimum=1, maximum=5)}, ["action"]),
    }


class AgentTools:
    def __init__(self, owner):
        self.owner = owner

    def registered(self):
        prompts = load_prompts()
        return [FeiyuFunctionTool(name=name, description=prompts["tool_" + name], parameters=schema,
                             handler=self.handler(name)) for name, schema in schemas(prompts).items()]

    def handler(self, name):
        async def invoke(event, **arguments):
            return json.dumps(tool_result_view(await self.execute(event, name, arguments), event.get_extra("qq_agent_run")),
                              ensure_ascii=False, separators=(",", ":"))
        return invoke

    async def execute(self, event, name, arguments):
        store = self.owner.agent_store
        run = event.get_extra("qq_agent_run")
        key = None
        if run is None:
            return {"ok": False, "status": "rejected", "error": "no_active_run"}
        try:
            # This guard runs inside the executor, not in a non-vetoable hook.
            store.work(run)
            if run.closing:
                raise PermissionError("restricted_close_has_no_tools")
            if not self.owner._event_active(event) or name not in schemas(load_prompts()):
                raise PermissionError("tool_not_authorized")
            store.consume(run, "tool_calls")
            jsonschema.validate(arguments, schemas(load_prompts())[name])
            # Check a reference before reserving the action: a premature call
            # must not permanently poison this number after search fills it.
            if arguments.get("candidate") is not None:
                run.song_id(arguments["candidate"])
            arguments = dict(arguments)
            if name in {"music", "musiccover"}:
                # Omitting the optional action means start. Both spellings
                # must share a receipt rather than submit two identical jobs.
                arguments.setdefault("action", "start")
            key, replay = store.reserve_action(run, name, arguments)
            if replay is not None:
                return replay
            result = await store.wait(run, self.dispatch(event, name, arguments, key), timeout=60)
            result = {"tool": name, "action_id": key, **result}
            store.action_result(run, key, result)
            return result
        except Superseded:
            result = {"ok": False, "status": "superseded", "error": "conversation_changed"}
        except (BudgetExhausted, PermissionError, jsonschema.ValidationError, ValueError, KeyError) as exc:
            result = {"ok": False, "status": "rejected", "error": type(exc).__name__,
                      "detail": (exc.message if isinstance(exc, jsonschema.ValidationError) else str(exc))[:240]}
            if str(exc) == "unknown_candidate_use_weapi_first":
                result.update(error="candidate_not_registered", executed=False,
                              required_next_step="weapi_search")
        except Exception as exc:
            # A timed-out side effect may already have happened: never auto-replay.
            result = {"ok": False, "status": "uncertain", "error": type(exc).__name__}
        if key:
            store.action_result(run, key, {"tool": name, "action_id": key, **result})
        else:
            # Preflight failures are evidence too, but must not reserve an
            # action key: search may register this candidate on the next step.
            result["executed"] = False
            store.record(run, "tool_rejected", {"tool": name, "arguments": arguments, "result": result})
        return {"tool": name, **result}

    async def dispatch(self, event, name, args, key):
        owner, run = self.owner, event.get_extra("qq_agent_run")
        media = owner.media
        if name == "emotion":
            if has_control_markup(args["text"]):
                raise ValueError("speech_contains_control_markup")
            text = spoken_text(args['text'])
            if not text:
                return {'ok': False, 'status': 'skipped', 'error': 'no_spoken_text', 'sent': False}
            data = await media.client.speech(text, args["emotion"], media.character_id)
            owner.agent_store.ensure_current(run)
            audio = media.client.save_audio(data)
            run.speech[args["text"]] = audio
            owner.agent_store.record(run, "speech_prepared", {"text": text,
                "path": str(audio), "emotion": args["emotion"], "source": "emotion"})
            return {"ok": True, "status": "prepared", "artifact_id": key, "text": text,
                    "emotion": args["emotion"], "sent": False}
        if name == "weapi":
            if args["action"] == "search" and not args.get("query", "").strip():
                raise ValueError("search_requires_query")
            query = {**args, "scope": run.scope}
            if args["action"] == "resolve":
                if args.get("candidate") and args.get("song_id"):
                    raise ValueError("resolve_requires_one_selector")
                if args.get("candidate"):
                    query["song_id"] = run.song_id(args["candidate"])
                if not query.get("song_id"):
                    raise ValueError("resolve_requires_candidate_or_user_song_id")
            query.pop("candidate", None)
            result = await media.client.request("/v1/music/weapi", query)
            for song in result.get("songs", []):
                run.remember_song(song)
            return result
        if name == "sticker":
            path = media.client.directory / "stickers" / (args["tag"] + ".png")
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                return {"ok": False, "status": "unavailable", "error": "sticker_asset_missing"}
            import time
            if time.monotonic() - media.sticker_last.get(run.scope, 0) < 60:
                return {"ok": False, "status": "skipped", "error": "sticker_cooldown"}
            owner.agent_store.ensure_current(run)
            await event.send(MessageChain(chain=[Image.fromFileSystem(str(path))]))
            media.sticker_last[run.scope] = time.monotonic()
            lifecycle = event.get_extra("qq_agent_lifecycle")
            if lifecycle:
                lifecycle.sent("[sticker:" + args["tag"] + "]", modality="sticker")
            return {"ok": True, "status": "sent", "tag": args["tag"]}
        if name in {"music", "musiccover"}:
            incoming = event.get_extra("qq_incoming")
            mode = "cover" if name == "musiccover" else "original"
            action = args.get('action', 'start')
            if action == 'cancel':
                rows = media.song_tasks.cancel(run.scope, owner=incoming.sender_uin, mode=mode)
                results = await media.cancel_song_tasks(rows)
                return {"ok": bool(rows), "status": "canceled" if rows else "not_found", "tasks": results,
                        "scope": "current_requester", "sent": False}
            if not args.get('candidate'):
                raise ValueError('start_or_replace_requires_candidate')
            song_id = run.song_id(args["candidate"])
            song = run.candidates.get(song_id)
            if song is None:
                return {"ok": False, "status": "rejected", "error": "use_weapi_before_song_action"}
            if name == "musiccover" and song.get("available") is False:
                return {"ok": False, "status": "unavailable", "error": "selected_audio_unavailable",
                        "song": song, "can_cover": False, "completed": False, "sent": False}
            job_id = hashlib.sha256(f"{run.run_id}:{key}".encode()).hexdigest()[:32]
            task = media.song_tasks.register(job_id, run.scope, incoming.sender_uin, mode, run.run_id, song['title'])
            music = {"mode": mode, "query": song["title"][:120], "artist": song.get("artists", "")[:100]}
            payload = {"scope": run.scope, "music": music, "selected_song_id": song_id,
                       "version": run.version, "run_id": run.run_id,
                       "user_request": incoming.text[:2000], "request_context": event.get_extra("qq_music_context", []),
                       "origin": event.get_extra("qq_transport_origin"), "group": incoming.group_id,
                       "generation": event.get_extra("qq_control_generation"),
                       "task_id": job_id, "task_revision": task['revision'], "owner": incoming.sender_uin,
                       "persona": event.get_extra("qq_persona_prompt", ""), "history_origin": event.unified_msg_origin}
            # Persist recovery metadata before the idempotent submission, so a
            # lost HTTP response cannot lead to a second song/synthesis job.
            media.client.state.remember(job_id, payload)
            try:
                result = await media.client.request("/v1/songs/selected", {"request_id": job_id, "scope": run.scope,
                    "music": music, "song_id": song_id, "source_receipt": song.get("source_receipt", ""),
                    "candidate_receipts": [item["source_receipt"] for item in run.candidates.values() if item.get("source_receipt")],
                    "user_request": payload["user_request"], "request_context": payload["request_context"]})
                if result.get('status') not in {'awaiting_commit', 'waiting_gpu', 'running', 'completed'}:
                    raise ValueError('new_song_not_accepted')
            except BaseException:
                media.song_tasks.finish(job_id, 'uncertain')
                media.client.state.mark(job_id, 'uncertain')
                raise
            # Recover an accepted submission even if its foreground request
            # became obsolete while waiting for the HTTP response. Replacement
            # cancellation is a separate side effect and still needs a guard.
            media.spawn(media.deliver_song(job_id, payload))
            owner.agent_store.ensure_current(run)
            replaced = []
            if action == 'replace':
                rows = media.song_tasks.cancel(run.scope, owner=incoming.sender_uin, except_id=job_id, reason='explicit_replace')
                replaced = await media.cancel_song_tasks(rows)
            owner.agent_store.ensure_current(run)
            return {"ok": True, "status": "queued", "job_id": job_id, "song": result.get("song"),
                    "mode": mode, "replaced": replaced, "completed": False, "sent": False}
        raise PermissionError("unknown_tool")
