"""Exercise the durable cover job with a local source and a fake QQ sender."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.qq_adapter import songs as song_module
from runtime.qq_adapter.song_audio import inspect_audio
from runtime.qq_adapter.song_source import Song
from runtime.qq_adapter.songs import SongPolicy, SongService


async def probe(source: Path, output_dir: Path, *, start: int, duration: int,
                separator_executable: str, rvc_python: str) -> dict:
    source = source.resolve(strict=True)
    if output_dir.exists():
        raise ValueError("probe output directory already exists")
    output_dir.mkdir(parents=True)
    source_info = inspect_audio(source, "ffprobe", max_seconds=1200)

    class LocalSource:
        def search(self, _query: str, _page: int = 1) -> list[Song]:
            return [Song("local_probe", 1, source.stem, "probe", source_info["seconds"], True,
                         "local-probe-v1")]

        def download(self, _song_id: int, destination: Path) -> str:
            shutil.copyfile(source, destination)
            return source_info["sha256"]

    db = sqlite3.connect(output_dir / "song_jobs.sqlite3")
    policy = SongPolicy(enabled=True, work_dir=str(output_dir / "jobs"),
                        separator_executable=separator_executable, rvc_python=rvc_python)
    service = SongService(db, policy, source=LocalSource(), permitted=lambda _session, _user: True)
    session = "qq:private:1:1"
    await service.search(1, session, 1, source.stem)
    service.select(session, 1, 1)
    job_id = service.enqueue(1, session, 1, "local-probe", start=start, duration=duration)
    sends: list[dict] = []

    async def fake_send(_client, **kwargs):
        sends.append({"session_id": kwargs["session_id"],
                      "audio_path": str(kwargs["audio_path"]),
                      "idempotency_key": kwargs["idempotency_key"]})
        return type("Delivery", (), {"result": {"message_id": "dry-run"}})()

    original = song_module.send_voice_file
    song_module.send_voice_file = fake_send
    try:
        await service._process(job_id, None, None, None)
    finally:
        song_module.send_voice_file = original
    state = service.status(session, 1, job_id)
    if state["status"] != "completed" or len(sends) != 1:
        raise RuntimeError(f"cover job failed: {state}")
    result = {"job_id": job_id, "status": state["status"], "fake_send": sends[0],
              "output": inspect_audio(output_dir / "jobs" / job_id / "cover.wav", "ffprobe")}
    (output_dir / "probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")
    db.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--duration", type=int, default=8)
    parser.add_argument("--separator-executable", default=str(ROOT / "envs/song/Scripts/audio-separator.exe"))
    parser.add_argument("--rvc-python", default=str(ROOT / "envs/song/Scripts/python.exe"))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args.source, args.output_dir, start=args.start,
                                        duration=args.duration,
                                        separator_executable=args.separator_executable,
                                        rvc_python=args.rvc_python)), ensure_ascii=False, indent=2))
