"""Keep complete model HTTP bodies in a bounded, readable JSON archive."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import inspect
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid


logger = logging.getLogger(__name__)
_LOCK = threading.Lock()
_LOG_NAME = re.compile(r"model-(\d+)\.json")
_LEGACY_NAME = re.compile(r"model-(\d+)\.jsonl")
RECORDS_PER_FILE = 50
MAX_FILES = 5


def debug_directory() -> Path:
    if explicit := os.environ.get("ASTRBOT_CHAT_MODEL_DEBUG_DIR"):
        return Path(explicit).resolve()
    if state := os.environ.get("ASTRBOT_CHAT_STATE_DIR"):
        return Path(state).resolve() / "logs/model_debug"
    for parent in Path(__file__).resolve().parents:
        if (parent / "configs/qq_companion/prompts.yaml").is_file():
            return parent / "storage/qq_companion/logs/model_debug"
    raise RuntimeError("Cannot find model debug log directory")


@contextmanager
def _archive_lock(directory):
    # The lock is outside the archive so it contains at most five log files.
    with _LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory.parent / ("." + directory.name + ".lock")).open("a+b") as handle:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _archive_files(directory):
    return sorted((path for path in directory.iterdir()
                   if path.is_file() and _LOG_NAME.fullmatch(path.name)),
                  key=lambda path: int(_LOG_NAME.fullmatch(path.name)[1]))


def _read_records(path):
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ValueError(f"Invalid model debug archive: {path.name}")
    return records


def _write_records(path, records):
    # Replace the whole array atomically; an interrupted write leaves the old
    # valid JSON intact. Keep the temporary file outside the archive directory.
    temporary = path.parent.parent / f".{path.parent.name}-{path.name}.tmp"
    try:
        with temporary.open("wb") as handle:
            handle.write((json.dumps(records, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _migrate_legacy(directory):
    for path in sorted(directory.glob("model-*.jsonl")):
        if not _LEGACY_NAME.fullmatch(path.name):
            continue
        records = []
        with path.open("rb") as handle:
            while line := handle.readline():
                try:
                    records.append(json.loads(line))
                except (ValueError, UnicodeError):
                    if not line.endswith(b"\n"):
                        break  # Old append format could leave an incomplete tail.
                    raise
        target = path.with_suffix(".json")
        if target.exists():
            # Resume a migration interrupted between replacement and deletion.
            if _read_records(target)[:len(records)] != records:
                raise ValueError(f"Conflicting model debug archives: {path.name}")
        else:
            _write_records(target, records)
        path.unlink()


def _prune(directory):
    for obsolete in _archive_files(directory)[:-MAX_FILES]:
        obsolete.unlink()


def migrate_archive(directory=None):
    directory = Path(directory) if directory is not None else debug_directory()
    with _archive_lock(directory):
        _migrate_legacy(directory)
        _prune(directory)


def append_exchange(record, directory=None):
    directory = Path(directory) if directory is not None else debug_directory()
    # The durable counter survives archive rotation; UUID prevents double counting.
    from .control_store import ControlStore
    if record.get("request_id"):
        ControlStore(directory.parent.parent).record_usage(record)
        from .control_store import public_exchange
        from .prompt_loader import load_prompts
        try:
            record = {**record, "prompt_names": public_exchange(record, load_prompts())["prompt_names"]}
        except (ValueError, OSError, AttributeError):
            pass
    record = memory_safe_exchange(record)
    database = directory.parent.parent / "memories.sqlite3"
    if database.exists():
        import sqlite3
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='sm_governance'").fetchone():
                scopes = {r[0] for r in db.execute("SELECT scope FROM sm_governance WHERE retention!='normal' UNION "
                    "SELECT scope FROM sm_subject_rules WHERE retention!='normal'")}
                raw = json.dumps(record.get("request"), ensure_ascii=False)
                if any(scope and scope in raw for scope in scopes) or (scopes and record.get("phase") in {"vision", "proactive_plan", "proactive_reply"}):
                    record["request"] = {"messages": [], "redacted": "retention_policy"}
                    response = record.get("response")
                    record["response"] = {"usage": response.get("usage")} if isinstance(response, dict) else None
    with _archive_lock(directory):
        _migrate_legacy(directory)
        files = _archive_files(directory)
        current = files[-1] if files else None
        records = _read_records(current) if current else []
        if current is None or len(records) >= RECORDS_PER_FILE:
            sequence = int(_LOG_NAME.fullmatch(current.name)[1]) + 1 if current else 1
            current = directory / f"model-{sequence:06d}.json"
            records = []
        records.append(record)
        _write_records(current, records)
        _prune(directory)


def memory_safe_exchange(record):
    """Memory bodies are not a second debug archive or a replay source."""
    record = deepcopy(record)
    if record.get("phase") == "memory":
        record["request"] = {"messages": [], "redacted": "private_memory_task"}
        response = record.get("response")
        record["response"] = {"usage": response.get("usage")} if isinstance(response, dict) else None
        return record
    def scrub(value):
        if isinstance(value, dict):
            return {key: ("[memory view omitted]" if key in {"relevant_memories", "shared_memory", "memory_view"} else scrub(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                return value
            if isinstance(parsed, (dict, list)):
                return json.dumps(scrub(parsed), ensure_ascii=False)
        return value
    if "request" in record:
        record["request"] = scrub(record["request"])
    return record


def purge_memory_debug(directory, scopes=()):
    """Remove governed scope captures and old memory tasks, keeping numeric telemetry."""
    directory = Path(directory)
    if not directory.exists():
        return
    with _archive_lock(directory):
        _migrate_legacy(directory)
        for path in _archive_files(directory):
            records = []
            for record in _read_records(path):
                raw = json.dumps(record.get("request"), ensure_ascii=False)
                if record.get("phase") == "memory" or any(scope and scope in raw for scope in scopes) or (scopes and record.get("phase") in {"vision", "proactive_plan", "proactive_reply"}):
                    record["request"] = {"messages": [], "redacted": "memory_governance"}
                    response = record.get("response")
                    record["response"] = {"usage": response.get("usage")} if isinstance(response, dict) else None
                records.append(memory_safe_exchange(record))
            _write_records(path, records)


def _body(content):
    try:
        return json.loads(content)
    except (ValueError, UnicodeError):
        return content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content


def _intended_body(arguments):
    # Only used if cancellation/connection failure leaves no HTTP request object.
    # Preserve all intended model inputs, and explicitly label this source.
    body = {key: value for key, value in arguments.items()
            if key not in {"extra_body", "extra_headers", "extra_query", "timeout"}}
    body.update(arguments.get("extra_body") or {})
    return body


async def _save(record):
    try:
        await asyncio.to_thread(append_exchange, record)
    except Exception:
        # A disk problem must not turn a successful model response into fallback.
        logger.exception("Could not write complete model debug exchange")


class DebugCompletions:
    """Observe each SDK call before SDK/AstrBot parsing, including API retries."""
    def __init__(self, completions, *, phase, provider_id, session_id):
        self.completions = completions
        self.metadata = {"phase": phase, "provider_id": provider_id, "session_id": session_id}

    def __getattr__(self, name):
        return getattr(self.completions, name)

    async def create(self, **arguments):
        started = time.monotonic()
        record = {"request_id": uuid.uuid4().hex, **self.metadata,
                  "started_at": datetime.now(timezone.utc).isoformat(),
                  "request": _intended_body(arguments), "request_source": "sdk_arguments",
                  "response": None, "http_status": None, "status": "pending"}
        http_response = None
        try:
            raw = await self.completions.with_raw_response.create(**arguments)
            http_response = raw.http_response
            record["request"] = _body(http_response.request.content)
            record["request_source"] = "http_body"
            record["response"] = _body(http_response.content)
            record["http_status"] = http_response.status_code
            parsed = raw.parse()
            if inspect.isawaitable(parsed):
                parsed = await parsed
            record["status"] = "received"
            return parsed
        except BaseException as exc:
            if http_response is None:
                http_response = getattr(exc, "response", None)
                request = getattr(exc, "request", None)
                if request is not None:
                    record["request"] = _body(request.content)
                    record["request_source"] = "http_body"
                if http_response is not None:
                    record["response"] = _body(http_response.content)
                    record["http_status"] = http_response.status_code
            record["status"] = "canceled" if isinstance(exc, asyncio.CancelledError) else "error"
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            await _save(record)


def debug_client(client, *, phase, provider_id, session_id):
    # Public SDK copy shares its connection pool; do not close that shared pool
    # or replace resources on the provider's long-lived client.
    view = client.copy()
    view.chat.completions = DebugCompletions(view.chat.completions,
        phase=phase, provider_id=provider_id, session_id=session_id)
    return view
