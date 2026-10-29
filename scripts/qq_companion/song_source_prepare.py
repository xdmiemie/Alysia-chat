"""Download/verify one selected source before reserving the GPU for conversion."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from services.media_api.song_audio import inspect_audio
from services.media_api.song_source import NetEaseSource, SongSourceError, classify_search_error

ROOT = Path(__file__).resolve().parents[2]


def ready_source(store, job_id):
    job = store.get(job_id)
    path = store.output(job_id).parent / "source.audio"
    manifest = path.with_suffix(".receipt.json")
    try:
        receipt = json.loads(manifest.read_text(encoding="utf-8"))
        if (receipt["song_id"] == job["song"]["song_id"] and path.stat().st_size == receipt["audio"]["bytes"]
                and hashlib.sha256(path.read_bytes()).hexdigest() == receipt["audio"]["sha256"]):
            return path
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def prepare_source(store, job_id, *, source=None):
    cached = ready_source(store, job_id)
    if cached:
        return cached
    job = store.get(job_id)
    song = job["song"]
    song_id = song["song_id"]
    store.start_download(job_id, song_id)
    path = store.output(job_id).parent / "source.audio"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        (source or NetEaseSource.from_environment()).download(song_id, path)
        info = inspect_audio(path, str(ROOT / "tools/ffmpeg/bin/ffprobe.exe"))
        if abs(info["seconds"] - song["duration_seconds"]) > 2:
            raise SongSourceError("source_is_not_full_song")
        current = store.get(job_id)
        if current["status"] not in {"waiting_gpu", "running"} or current["song"]["song_id"] != song_id:
            raise SongSourceError("song_selection_changed")
        path.with_suffix(".receipt.json").write_text(json.dumps({"song_id": song_id, "audio": info}), encoding="utf-8")
        store.downloaded(job_id, song_id)
        return path
    except Exception as exc:
        error = classify_search_error(exc)
        if error and error.code == "source_http_403":
            store.reject_download(job_id, song_id)
            raise SongSourceError("source_http_403") from None
        if error:
            raise error from None
        raise
