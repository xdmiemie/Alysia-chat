"""Measure local Qwen3 QQ Agent decisions without sending QQ messages."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.qq_adapter.agent import AgentSession, split_sentences
from runtime.qwen3.config import load_qwen3_config


SCENARIOS = {
    "single": "你好呀",
    "social": "你好呀，你叫什么名字？再和我聊一句今天的心情。",
    "long": "请用简短的话解释为什么日落时天空会变红，再给一个生活中的例子。",
    "interrupted": "我刚问了天气，但现在改问你叫什么名字？",
}


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percent + 0.5)))
    return round(ordered[index], 2)


def run(repetitions: int) -> dict:
    config = load_qwen3_config()
    rows = []
    gpu_samples: list[int] = []
    stop = threading.Event()

    def sample_gpu() -> None:
        while not stop.is_set():
            try:
                output = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    stderr=subprocess.DEVNULL, timeout=2, text=True)
                gpu_samples.append(int(output.splitlines()[0].strip()))
            except (OSError, ValueError, subprocess.TimeoutExpired, IndexError):
                pass
            stop.wait(0.5)

    monitor = threading.Thread(target=sample_gpu, daemon=True)
    monitor.start()
    try:
        for category, prompt in SCENARIOS.items():
            for sample in range(repetitions):
                session = AgentSession(config)
                started = time.perf_counter()
                first = session.decide(prompt, (), 250)
                first_ms = (time.perf_counter() - started) * 1000
                first_usage = dict(session.client.last_usage)
                first_timings = dict(session.client.last_timings)
                second_ms = None
                second_usage = {}
                second_timings = {}
                second_action = None
                sent_sentences = split_sentences(first.content) if first.action == "send_message" else []
                if category == "social" and first.continue_turn and len(sent_sentences) < 3:
                    started_second = time.perf_counter()
                    second = session.decide(prompt, tuple(sent_sentences), 250)
                    second_ms = (time.perf_counter() - started_second) * 1000
                    second_usage = dict(session.client.last_usage)
                    second_timings = dict(session.client.last_timings)
                    second_action = second.action
                rows.append({"category": category, "sample": sample + 1,
                             "first_action": first.action, "continue_turn": first.continue_turn,
                             "second_action": second_action, "first_ms": round(first_ms, 2),
                             "second_ms": round(second_ms, 2) if second_ms is not None else None,
                             "first_token_estimate_ms": round(first_timings.get("prompt_ms", 0) + first_timings.get("predicted_per_token_ms", 0), 2),
                             "second_first_token_estimate_ms": round(second_timings.get("prompt_ms", 0) + second_timings.get("predicted_per_token_ms", 0), 2) if second_timings else None,
                             "prompt_tokens": first_usage.get("prompt_tokens", 0) + second_usage.get("prompt_tokens", 0),
                             "completion_tokens": first_usage.get("completion_tokens", 0) + second_usage.get("completion_tokens", 0)})
    finally:
        stop.set()
        monitor.join(timeout=3)
    summary = {}
    for category in SCENARIOS:
        selected = [row for row in rows if row["category"] == category]
        durations = [row["first_ms"] + (row["second_ms"] or 0) for row in selected]
        summary[category] = {"samples": len(selected), "p50_ms": percentile(durations, 0.5),
                             "p95_ms": percentile(durations, 0.95),
                             "first_token_estimate_p50_ms": percentile([row["first_token_estimate_ms"] for row in selected], 0.5),
                             "first_token_estimate_p95_ms": percentile([row["first_token_estimate_ms"] for row in selected], 0.95),
                             "mean_prompt_tokens": round(statistics.mean(row["prompt_tokens"] for row in selected), 1),
                             "mean_completion_tokens": round(statistics.mean(row["completion_tokens"] for row in selected), 1),
                             "two_decision_count": sum(row["second_action"] is not None for row in selected)}
    return {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "repetitions": repetitions,
            "peak_total_gpu_memory_mib": max(gpu_samples) if gpu_samples else None,
            "summary": summary, "samples": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--output", type=Path, default=ROOT / "storage/logs/qq-stage13ab-benchmark.json")
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 100:
        parser.error("repetitions must be 1..100")
    result = run(args.repetitions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
