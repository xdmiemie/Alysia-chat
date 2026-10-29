"""Read-only weapi candidate export; no downloads, model or QQ calls."""
import json
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.media_api.song_source import NetEaseSource, classify_search_error
import time


def run():
    source = NetEaseSource.from_environment()
    output = ROOT / "storage/temporary/song-search-results"
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for query in ("雨爱", "奇迹再现", "年轮", "叹云兮"):
        for attempt in range(1, 4):
            try:
                songs = source.search(query)
                rows.append({"query": query, "page": 1, "limit": 20,
                    "results": [{**song.public(), "link": f"https://music.163.com/song?id={song.song_id}"} for song in songs]})
                print(f"Search completed: {len(rows)}/4; {len(songs)} candidates", flush=True)
                break
            except Exception as exc:
                failure = classify_search_error(exc)
                if not failure or not failure.retryable or attempt == 3:
                    raise
                time.sleep(attempt)
        (output / "results.json").write_text(json.dumps({"searched_at": datetime.now().astimezone().isoformat(),
            "source": "weapi", "searches": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(rows, output)


def write_markdown(rows, output):
    lines = ["# 网易云 weapi 搜索结果", "", "每首查询第 1 页，最多 20 条；按接口返回顺序。可用状态为本次账号音源检查结果。", ""]
    for row in rows:
        lines += ["## " + row["query"], "", "| 序号 | 歌名 | 歌手 | 别名／副标题 | 专辑 | 时长 | 音源可用 | 网易云 |",
                  "|---|---|---|---|---|---|---|---|"]
        for i, song in enumerate(row["results"], 1):
            escape = lambda value: str(value).replace("|", "\\|").replace("\n", " ")
            seconds = round(song["duration_seconds"])
            lines.append(f"| {i} | {escape(song['title'])} | {escape(song['artists'])} | {escape('；'.join(song['aliases']))} | {escape(song['album'])} | "
                         f"{seconds // 60}:{seconds % 60:02d} | {'是' if song['available'] else '否'} | [歌曲]({song['link']}) |")
        lines.append("")
    (output / "results.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    run()
