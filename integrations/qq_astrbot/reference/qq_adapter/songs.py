"""Durable song selections and one-at-a-time local cover jobs."""

from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from runtime.qq_adapter.song_audio import SongAudioConfig, SongAudioPipeline, inspect_audio
from runtime.qq_adapter.song_source import NetEaseSource, Song, parse_song_id
from runtime.qq_adapter.voice import send_voice_file
from scripts.install_feiyu_rvc import MODEL_SHA, sha256


class SongJobCancelled(Exception):
    pass


@dataclass(frozen=True)
class SongPolicy:
    enabled: bool = False
    full_song: bool = False
    default_duration_seconds: int = 30
    max_duration_seconds: int = 90
    max_active_jobs: int = 2
    work_dir: str = "storage/temporary/song_jobs"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    separator_executable: str = "audio-separator"
    separator_model: str = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
    separator_model_dir: str = "model_store/cache/audio_separator"
    separator_sha256: str = "5b84f37e8d444c8cb30c79d77f613a41c05868ff9c9ac6c7049c00aefae115aa"
    rvc_python: str = ""
    rvc_bundle: str = "model_store/registry/rvc/feiyu/v1"

    def __post_init__(self) -> None:
        if not 5 <= self.default_duration_seconds <= self.max_duration_seconds <= 600:
            raise ValueError("invalid song duration limits")
        if not 1 <= self.max_active_jobs <= 10:
            raise ValueError("invalid song queue limit")

    def audio_config(self) -> SongAudioConfig:
        import sys
        return SongAudioConfig(self.ffmpeg, self.ffprobe, self.separator_executable,
                               self.separator_model, self.separator_model_dir,
                               self.separator_sha256,
                               self.rvc_python or sys.executable, self.rvc_bundle,
                               self.max_duration_seconds)


class SongService:
    def __init__(self, db: sqlite3.Connection, policy: SongPolicy, *,
                 source: NetEaseSource | None = None,
                 permitted: Callable[[str, int], bool] | None = None) -> None:
        self.db, self.policy = db, policy
        self.source = source or NetEaseSource.from_environment()
        self.permitted = permitted or (lambda _session, _user: False)
        self.pipeline = SongAudioPipeline(policy.audio_config())
        self.wakeup = asyncio.Event()
        self.current_job_id: str | None = None
        self.current_task: asyncio.Task[None] | None = None
        self.db.execute("""CREATE TABLE IF NOT EXISTS song_choices (
            token TEXT PRIMARY KEY, bot_id INTEGER NOT NULL, session_id TEXT NOT NULL,
            user_id INTEGER NOT NULL, song_json TEXT NOT NULL, expires_at REAL NOT NULL,
            selected INTEGER NOT NULL DEFAULT 0)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS song_jobs (
            job_id TEXT PRIMARY KEY, bot_id INTEGER NOT NULL, session_id TEXT NOT NULL,
            user_id INTEGER NOT NULL, origin_message_id TEXT NOT NULL,
            song_json TEXT NOT NULL, start_seconds INTEGER NOT NULL,
            duration_seconds INTEGER NOT NULL, model_id TEXT NOT NULL,
            model_sha256 TEXT NOT NULL, status TEXT NOT NULL,
            artifact_json TEXT NOT NULL DEFAULT '{}', error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, sent_message_id TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(bot_id,origin_message_id))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS song_jobs_status ON song_jobs(status,created_at)")
        self.db.execute("UPDATE song_jobs SET status='queued' WHERE status IN "
                        "('fetching','clipping','separating','converting','mixing','ready_to_send')")
        self.db.execute("UPDATE song_jobs SET status='send_uncertain' WHERE status='sending'")
        self.db.commit()

    def _check(self, session_id: str, user_id: int) -> None:
        if not self.policy.enabled or not self.permitted(session_id, user_id):
            raise PermissionError("song covers are disabled for this session")

    def _choice(self, session_id: str, user_id: int, index: int) -> tuple[str, Song]:
        self._check(session_id, user_id)
        rows = self.db.execute("SELECT token,song_json FROM song_choices WHERE session_id=? "
                               "AND user_id=? AND expires_at>? ORDER BY rowid LIMIT 8",
                               (session_id, user_id, time.time())).fetchall()
        if not 1 <= index <= len(rows):
            raise ValueError("song choice is missing or expired")
        token, raw = rows[index - 1]
        return token, Song(**json.loads(raw))

    async def search(self, bot_id: int, session_id: str, user_id: int,
                     query: str, page: int = 1) -> list[dict[str, Any]]:
        self._check(session_id, user_id)
        song_id = parse_song_id(query)
        songs = ([await asyncio.to_thread(self.source.detail, song_id)] if song_id else
                 await asyncio.to_thread(self.source.search, query, page))
        songs = [song for song in songs if song.available]
        self.db.execute("DELETE FROM song_choices WHERE session_id=? AND user_id=?",
                        (session_id, user_id))
        result = []
        for index, song in enumerate(songs[:8], 1):
            self.db.execute("INSERT INTO song_choices VALUES (?,?,?,?,?,?,0)",
                            (uuid.uuid4().hex, bot_id, session_id, user_id,
                             json.dumps(song.public(), ensure_ascii=False), time.time() + 600))
            result.append({"index": index, **song.public()})
        self.db.commit()
        return result

    def select(self, session_id: str, user_id: int, index: int) -> dict[str, Any]:
        token, song = self._choice(session_id, user_id, index)
        if not song.available:
            raise ValueError("song is unavailable")
        self.db.execute("UPDATE song_choices SET selected=1 WHERE token=?", (token,))
        self.db.commit()
        return {"index": index, **song.public()}

    async def request_cover(self, bot_id: int, session_id: str, user_id: int,
                            origin_id: str, song_query: str) -> str:
        """Resolve the model's song title, then queue its full cover."""
        query = song_query.strip()
        if not query or len(query) > 120:
            raise ValueError("invalid song query")
        songs = await self.search(bot_id, session_id, user_id, query)
        selected = _matching_song(songs, query)
        if selected is None:
            raise ValueError("song unavailable or no exact match")
        self.select(session_id, user_id, selected["index"])
        return self.enqueue(bot_id, session_id, user_id, origin_id)

    def enqueue(self, bot_id: int, session_id: str, user_id: int, origin_id: str,
                *, start: int = 0, duration: int | None = None) -> str:
        self._check(session_id, user_id)
        existing = self.db.execute("SELECT job_id FROM song_jobs WHERE bot_id=? AND origin_message_id=?",
                                   (bot_id, origin_id)).fetchone()
        if existing:
            return existing[0]
        row = self.db.execute("SELECT song_json FROM song_choices WHERE session_id=? AND user_id=? "
                              "AND selected=1 AND expires_at>? ORDER BY rowid DESC LIMIT 1",
                              (session_id, user_id, time.time())).fetchone()
        if not row:
            raise ValueError("select a song first")
        song = Song(**json.loads(row[0]))
        if duration is None:
            duration = (math.ceil(song.duration_seconds - start) if self.policy.full_song
                        else self.policy.default_duration_seconds)
        if not 0 <= start <= 600 or not 5 <= duration <= self.policy.max_duration_seconds:
            raise ValueError("invalid song clip parameters")
        if song.duration_seconds and start + duration > song.duration_seconds + 1:
            raise ValueError("clip exceeds song length")
        duplicate = self.db.execute(
            "SELECT job_id FROM song_jobs WHERE bot_id=? AND session_id=? AND user_id=? "
            "AND song_json=? AND start_seconds=? AND duration_seconds=? AND created_at>? "
            "AND status IN ('queued','fetching','clipping','separating','converting',"
            "'mixing','ready_to_send','sending') ORDER BY created_at DESC LIMIT 1",
            (bot_id, session_id, user_id, row[0], start, duration, time.time() - 120)).fetchone()
        if duplicate:
            return duplicate[0]
        active = self.db.execute("SELECT count(*) FROM song_jobs WHERE status IN "
                                 "('queued','fetching','clipping','separating','converting','mixing','ready_to_send','sending')").fetchone()[0]
        if active >= self.policy.max_active_jobs:
            raise ValueError("song job queue is full")
        job_id = uuid.uuid4().hex
        now = time.time()
        self.db.execute("INSERT INTO song_jobs (job_id,bot_id,session_id,user_id,origin_message_id,"
                        "song_json,start_seconds,duration_seconds,model_id,model_sha256,status,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (job_id, bot_id, session_id, user_id, origin_id,
                         json.dumps(song.public(), ensure_ascii=False), start, duration,
                         "FeiyuVoice-RVC-v1", MODEL_SHA, "queued", now, now))
        self.db.commit()
        self.wakeup.set()
        return job_id

    def status(self, session_id: str, user_id: int, job_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT status,error,song_json,created_at,updated_at FROM song_jobs "
                              "WHERE job_id=? AND session_id=? AND user_id=?",
                              (job_id, session_id, user_id)).fetchone()
        if not row:
            raise ValueError("song job not found")
        return {"job_id": job_id, "status": row[0], "error": row[1],
                "song": json.loads(row[2])["title"], "created_at": row[3],
                "stage_seconds": max(0, round(time.time() - row[4]))}

    def cancel(self, session_id: str, user_id: int, job_id: str) -> bool:
        cursor = self.db.execute("UPDATE song_jobs SET status='cancelled',updated_at=? "
                                 "WHERE job_id=? AND session_id=? AND user_id=? AND status NOT IN "
                                 "('completed','cancelled','failed','sending','send_uncertain')",
                                 (time.time(), job_id, session_id, user_id))
        self.db.commit()
        self.wakeup.set()
        if cursor.rowcount and job_id == self.current_job_id and self.current_task is not None:
            self.current_task.cancel()
        return cursor.rowcount > 0

    def _set(self, job_id: str, status: str, *, error: str | None = None,
             artifacts: dict[str, str] | None = None, sent_id: str | None = None) -> None:
        if artifacts is None:
            row = self.db.execute("SELECT artifact_json FROM song_jobs WHERE job_id=?", (job_id,)).fetchone()
            artifacts = json.loads(row[0]) if row else {}
        self.db.execute("UPDATE song_jobs SET status=?,error=?,artifact_json=?,sent_message_id=?,updated_at=? "
                        "WHERE job_id=?", (status, error, json.dumps(artifacts), sent_id, time.time(), job_id))
        self.db.commit()

    async def worker(self, client: Any, voice_store: Any, voice_limits: Any) -> None:
        while True:
            row = self.db.execute("SELECT job_id FROM song_jobs WHERE status='queued' "
                                  "ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                self.wakeup.clear()
                await self.wakeup.wait()
                continue
            self.current_job_id = row[0]
            self.current_task = asyncio.create_task(self._process(row[0], client, voice_store, voice_limits))
            try:
                await self.current_task
            except asyncio.CancelledError:
                if self.db.execute("SELECT status FROM song_jobs WHERE job_id=?", (row[0],)).fetchone()[0] != "cancelled":
                    raise
            finally:
                self.current_job_id = None
                self.current_task = None

    async def _process(self, job_id: str, client: Any, voice_store: Any, voice_limits: Any) -> None:
        row = self.db.execute("SELECT bot_id,session_id,user_id,song_json,start_seconds,"
                              "duration_seconds,artifact_json FROM song_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return
        bot_id, session_id, user_id, raw_song, start, duration, raw_artifacts = row
        song = Song(**json.loads(raw_song))
        artifacts = json.loads(raw_artifacts)
        directory = Path(self.policy.work_dir) / job_id
        directory.mkdir(parents=True, exist_ok=True)
        try:
            async def valid(name: str) -> bool:
                path = directory / name
                if not path.is_file() or artifacts.get(name) != sha256(path):
                    return False
                await asyncio.to_thread(inspect_audio, path, self.policy.ffprobe,
                                        max_seconds=max(1200, duration + 2))
                return True

            async def checkpoint(status: str, *names: str) -> None:
                if self.db.execute("SELECT status FROM song_jobs WHERE job_id=?", (job_id,)).fetchone()[0] == "cancelled":
                    raise SongJobCancelled
                for name in names:
                    artifacts[name] = sha256(directory / name)
                self._set(job_id, status, artifacts=artifacts)

            audio = directory / "source.mp3"
            if not await valid("source.mp3"):
                await checkpoint("fetching")
                for attempt in range(2):
                    try:
                        await asyncio.to_thread(self.source.download, song.song_id, audio)
                        break
                    except (OSError, TimeoutError, requests.RequestException):
                        if attempt:
                            raise
                        await asyncio.sleep(1)
                await checkpoint("clipping", "source.mp3")
            clip = directory / "clip.wav"
            if not await valid("clip.wav"):
                await checkpoint("clipping")
                await self.pipeline.clip(audio, clip, start, duration)
                await checkpoint("separating", "clip.wav")
            stems_dir = directory / "stems"
            vocals, accompaniment = stems_dir / "vocals.wav", stems_dir / "instrumental.wav"
            if not await valid("stems/vocals.wav") or not await valid("stems/instrumental.wav"):
                await checkpoint("separating")
                first, second = await self.pipeline.separate(clip, stems_dir)
                if first != vocals:
                    first.replace(vocals)
                if second != accompaniment:
                    second.replace(accompaniment)
                await checkpoint("converting", "stems/vocals.wav", "stems/instrumental.wav")
            rvc_dir = directory / "rvc"
            converted = rvc_dir / "rvc_native.wav"
            if not await valid("rvc/rvc_native.wav"):
                await checkpoint("converting")
                attempt_dir = directory / ("rvc-attempt-" + uuid.uuid4().hex)
                result = await self.pipeline.convert(vocals, attempt_dir)
                rvc_dir.mkdir(exist_ok=True)
                shutil.copyfile(result, converted)
                shutil.copyfile(attempt_dir / "cover_result.json", rvc_dir / "cover_result.json")
                await checkpoint("mixing", "rvc/rvc_native.wav")
            final = directory / "cover.wav"
            if not await valid("cover.wav"):
                await checkpoint("mixing")
                await self.pipeline.mix(converted, accompaniment, final)
                await checkpoint("ready_to_send", "cover.wav")
            if not self.permitted(session_id, user_id):
                raise PermissionError("song sender no longer authorized")
            await checkpoint("ready_to_send")
            self._set(job_id, "sending", artifacts=artifacts)
            delivery = await send_voice_file(client, session_id=session_id, audio_path=final,
                                             idempotency_key=f"song:{job_id}", store=voice_store,
                                             limits=voice_limits)
            sent_id = delivery.result.get("message_id") if isinstance(delivery.result, dict) else None
            self._set(job_id, "completed", artifacts=artifacts,
                      sent_id=str(sent_id) if sent_id is not None else "")
        except SongJobCancelled:
            return
        except asyncio.CancelledError:
            state = self.db.execute("SELECT status FROM song_jobs WHERE job_id=?", (job_id,)).fetchone()[0]
            if state == "sending":
                self._set(job_id, "send_uncertain", artifacts=artifacts)
            elif state != "cancelled":
                self._set(job_id, "queued", artifacts=artifacts)
            raise
        except Exception as exc:
            state = self.db.execute("SELECT status FROM song_jobs WHERE job_id=?", (job_id,)).fetchone()[0]
            self._set(job_id, "send_uncertain" if state == "sending" else "failed",
                      error=type(exc).__name__, artifacts=artifacts)
            if state != "sending" and client is not None and self.permitted(session_id, user_id):
                try:
                    message = f"《{song.title}》我刚才没能唱出来，换一首试试好吗？"
                    if session_id.startswith("qq:private:"):
                        await client.send_private_message(user_id, message)
                    else:
                        await client.send_group_message(int(session_id.rsplit(":", 1)[1]), message)
                except Exception:
                    pass


_SING = re.compile(r"^/sing(?:\s+(.+))?$", re.IGNORECASE)
_STATUS = re.compile(r"^/song\s+(status|cancel)\s+([0-9a-f]{32})$", re.IGNORECASE)
def _matching_song(songs: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """Match the model's title/artist and avoid short excerpts when possible."""
    normalized = re.sub(r"[\s《》「」·・,，、_-]", "", query).casefold()
    song_id = parse_song_id(query)
    if song_id:
        return next((song for song in songs if song["song_id"] == song_id), None)
    exact_titles = []
    for song in songs:
        title = re.sub(r"[\s《》「」·・,，、_-]", "", song["title"]).casefold()
        artists = [re.sub(r"[\s《》「」·・,，、_-]", "", name).casefold()
                   for name in song["artists"].split(",")]
        if normalized == title:
            exact_titles.append(song)
        elif title and title in normalized and any(name and name in normalized for name in artists):
            return song
    if not exact_titles:
        return None
    return next((song for song in exact_titles if song.get("duration_seconds", 0) >= 180),
                exact_titles[0])


async def handle_song_command(service: SongService, *, bot_id: int, session_id: str,
                              user_id: int, message_id: str, content: str) -> str | None:
    text = content.strip()
    if not text.startswith("/"):
        return None
    match = _STATUS.fullmatch(text)
    if match:
        if match.group(1).lower() == "cancel":
            return "已取消。" if service.cancel(session_id, user_id, match.group(2)) else "任务无法取消或不存在。"
        state = service.status(session_id, user_id, match.group(2))
        active = state["status"] not in {"completed", "failed", "cancelled", "send_uncertain"}
        elapsed = f"（本阶段已运行 {state['stage_seconds']} 秒）" if active else ""
        return (f"歌曲任务 {state['job_id']}：{state['status']}{elapsed}" +
                (f"（{state['error']}）" if state["error"] else ""))
    match = _SING.fullmatch(text)
    if match is None:
        return None
    if not service.policy.enabled:
        return "点歌翻唱尚未启用。"
    command = (match.group(1) or "").strip()
    if not command:
        return "想听我唱哪首歌？"
    try:
        await service.request_cover(bot_id, session_id, user_id, message_id, command)
        return "好呀，等我一下。"
    except (ValueError, PermissionError):
        return "这首歌现在没法翻唱，换一首试试好吗？"
    except Exception:
        return "音源暂时不可用，请稍后重试。"
