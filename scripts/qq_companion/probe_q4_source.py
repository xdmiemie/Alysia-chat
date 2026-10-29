"""Bounded, read-only weapi acceptance and whole-source download; no QQ/GPU."""

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.media_api.song_source import NetEaseSource
from services.media_api.songs import select_song
from services.media_api.song_audio import inspect_audio


def run(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    source = NetEaseSource.from_environment()
    report = {"searches": [], "not_found": [], "downloads": [], "qq_connections": 0}
    for title in ("雨爱", "年轮", "小幸运", "童话", "稻香", "晴天", "七里香", "后来", "成都", "起风了"):
        try:
            song = select_song(source, {"query": title, "artist": ""})
            report["searches"].append({"query": title, "ok": True, "available": song["available"]})
            if song["available"] and 180 <= song["duration_seconds"] <= 600 and len(report["downloads"]) < 3:
                path = directory / (str(song["song_id"]) + ".audio")
                source.download(song["song_id"], path)
                info = inspect_audio(path, str(ROOT / "tools/ffmpeg/bin/ffprobe.exe"))
                if abs(info["seconds"] - song["duration_seconds"]) > 2:
                    raise ValueError("Source is a trial or incomplete song")
                report["downloads"].append({"song": song, "file": path.name, "audio": info})
            print(f"search {len(report['searches'])}/10; whole sources {len(report['downloads'])}/3", flush=True)
        except Exception as exc:
            report["searches"].append({"query": title, "ok": False, "error": type(exc).__name__})
            print(f"source check unavailable: {type(exc).__name__}", flush=True)
            # Avoid hammering an inaccessible service or repeating a blocked
            # network operation across ten titles.
            if len(report["searches"]) == 1:
                break
        time.sleep(0.3)
    if len(report["searches"]) == 10:
        for index in range(5):
            try:
                select_song(source, {"query": f"astrbot-q4-不存在的歌曲-9fc027-{index}", "artist": ""})
            except ValueError as exc:
                report["not_found"].append(str(exc) == "song_not_found")
            except Exception:
                report["not_found"].append(False)
            else:
                report["not_found"].append(False)
            time.sleep(0.3)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = (len(report["searches"]) == 10 and all(item["ok"] for item in report["searches"])
              and len(report["not_found"]) == 5 and all(report["not_found"]) and len(report["downloads"]) == 3)
    print("Q4_SOURCE_" + ("PASS" if passed else "INCOMPLETE"), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=ROOT / "storage/temporary/q4-source-acceptance")
    raise SystemExit(run(parser.parse_args().directory))
