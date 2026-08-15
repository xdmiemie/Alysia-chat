from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from uuid import uuid4


@dataclass(frozen=True)
class CallRecord:
    call_id: str
    path: Path
    character_id: str
    conversation_id: str
    qwen_model: str
    tts_model: str
    save_audio: bool
    started_at: str


class CallStore:
    """Durable, append-only Stage 14 call history."""

    def __init__(self, root: str | Path = "storage/calls") -> None:
        self.root = Path(root)
        self._lock = Lock()
        self._recover_interrupted_calls()

    def start(
        self,
        *,
        character_id: str,
        conversation_id: str,
        qwen_model: str,
        tts_model: str,
        save_audio: bool = True,
    ) -> CallRecord:
        now = datetime.now(timezone.utc)
        call_id = f"{now.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:10]}"
        path = self.root / call_id
        (path / "user").mkdir(parents=True, exist_ok=False)
        (path / "assistant").mkdir()
        record = CallRecord(
            call_id=call_id,
            path=path,
            character_id=character_id,
            conversation_id=conversation_id,
            qwen_model=qwen_model,
            tts_model=tts_model,
            save_audio=bool(save_audio),
            started_at=now.isoformat(),
        )
        self._write_metadata(
            record,
            status="active",
            ended_at="",
            turn_count=0,
            error="",
        )
        (path / "turns.jsonl").touch()
        return record

    def append_turn(self, record: CallRecord, turn: object) -> None:
        payload = asdict(turn) if hasattr(turn, "__dataclass_fields__") else dict(turn)  # type: ignore[arg-type]
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with (record.path / "turns.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")

    def finish(
        self,
        record: CallRecord,
        *,
        status: str,
        turn_count: int,
        error: str = "",
    ) -> None:
        self._write_metadata(
            record,
            status=status,
            ended_at=datetime.now(timezone.utc).isoformat(),
            turn_count=max(0, int(turn_count)),
            error=error,
        )

    def _write_metadata(self, record: CallRecord, **state: object) -> None:
        data = {
            "call_id": record.call_id,
            "character_id": record.character_id,
            "conversation_id": record.conversation_id,
            "models": {"qwen3": record.qwen_model, "gpt_sovits": record.tts_model},
            "save_audio": record.save_audio,
            "started_at": record.started_at,
            **state,
        }
        temporary = record.path / "call.json.tmp"
        target = record.path / "call.json"
        with self._lock:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(target)

    def _recover_interrupted_calls(self) -> None:
        if not self.root.is_dir():
            return
        for target in self.root.glob("*/call.json"):
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("status") != "active":
                continue
            data["status"] = "interrupted"
            data["ended_at"] = datetime.now(timezone.utc).isoformat()
            data["error"] = "应用在通话完成前退出"
            temporary = target.with_suffix(".json.recovery")
            try:
                temporary.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temporary.replace(target)
            except OSError:
                temporary.unlink(missing_ok=True)
