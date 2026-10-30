from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from integrations.qq_astrbot.plugin import model_debug
from integrations.qq_astrbot.plugin.model_debug import append_exchange, debug_directory


def records(directory):
    return [record for path in sorted(directory.glob("model-*.json"))
            for record in json.loads(path.read_text(encoding="utf-8"))]


def test_archive_rotates_at_50_and_deletes_only_oldest_after_five_files(tmp_path):
    for number in range(251):
        append_exchange({"number": number, "request": {"messages": ["line one\nline two"]}}, tmp_path)
        if number == 49:
            assert len(list(tmp_path.glob("*.json"))) == 1
        if number == 50:
            assert [len(json.loads(p.read_text())) for p in sorted(tmp_path.glob("*.json"))] == [50, 1]
    paths = sorted(tmp_path.glob("*.json"))
    assert [p.name for p in paths] == [f"model-{n:06d}.json" for n in range(2, 7)]
    assert [len(json.loads(p.read_text())) for p in paths] == [50, 50, 50, 50, 1]
    assert [record["number"] for record in records(tmp_path)] == list(range(50, 251))


@pytest.mark.parametrize("tail", [b'{"number":2}', b'{"number":'])
def test_legacy_migration_recovers_an_interrupted_tail(tmp_path, tail):
    path = tmp_path / "model-000013.jsonl"
    path.write_bytes(b'{"number":1}\n' + tail)
    append_exchange({"number": 3}, tmp_path)
    assert [record["number"] for record in records(tmp_path)] == ([1, 2, 3] if tail.endswith(b"}") else [1, 3])
    assert not path.exists()


def test_full_bodies_are_preserved_without_audit_preview_limits(tmp_path):
    text = "完整内容\n" * 30000
    value = {"request": {"messages": [{"role": "system", "content": text}]},
             "response": {"choices": [{"message": {"content": text, "tool_calls": []}}],
                          "usage": {"total_tokens": 12345}, "custom_extension": {"keep": True}}}
    append_exchange(value, tmp_path)
    assert records(tmp_path) == [value]
    saved = (tmp_path / "model-000001.json").read_text(encoding="utf-8")
    assert saved.startswith('[\n  {\n    "request": {\n')


def test_parallel_threads_do_not_lose_records_or_overfill_files(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: append_exchange({"number": n}, tmp_path), range(110)))
    assert sorted(record["number"] for record in records(tmp_path)) == list(range(110))
    assert [len(json.loads(path.read_text())) for path in sorted(tmp_path.glob("*.json"))] == [50, 50, 10]


def test_parallel_processes_share_rotation_and_keep_every_record(tmp_path):
    script = ("import sys; from integrations.qq_astrbot.plugin.model_debug import append_exchange; "
              "[append_exchange({'worker':sys.argv[2],'number':n}, sys.argv[1]) for n in range(40)]")
    workers = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(n)],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for n in range(3)]
    try:
        for process in workers:
            _, errors = process.communicate(timeout=30)
            assert process.returncode == 0, errors.decode(errors="replace")
    finally:
        for process in workers:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    assert {(record["worker"], record["number"]) for record in records(tmp_path)} == {
        (str(worker), number) for worker in range(3) for number in range(40)}
    assert [len(json.loads(path.read_text())) for path in sorted(tmp_path.glob("*.json"))] == [50, 50, 20]


def test_migration_is_repeatable_and_keeps_existing_record_counts(tmp_path):
    original = [{"number": n} for n in range(50)]
    legacy = tmp_path / "model-000001.jsonl"
    legacy.write_text("\n".join(json.dumps(record) for record in original) + "\n", encoding="utf-8")
    model_debug.migrate_archive(tmp_path)
    model_debug.migrate_archive(tmp_path)
    assert records(tmp_path) == original
    assert not legacy.exists()
    append_exchange({"number": 50}, tmp_path)
    assert [len(json.loads(path.read_text())) for path in sorted(tmp_path.glob("*.json"))] == [50, 1]


def test_interrupted_replacement_preserves_previous_complete_json(tmp_path, monkeypatch):
    append_exchange({"number": 1}, tmp_path)

    def failed_replace(*args):
        raise OSError("simulated interruption")

    monkeypatch.setattr(model_debug.os, "replace", failed_replace)
    with pytest.raises(OSError, match="interruption"):
        append_exchange({"number": 2}, tmp_path)
    assert records(tmp_path) == [{"number": 1}]


def test_debug_directory_follows_explicit_override_or_state_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRBOT_CHAT_MODEL_DEBUG_DIR", raising=False)
    monkeypatch.setenv("ASTRBOT_CHAT_STATE_DIR", str(tmp_path / "state"))
    assert debug_directory() == tmp_path / "state/logs/model_debug"
    monkeypatch.setenv("ASTRBOT_CHAT_MODEL_DEBUG_DIR", str(tmp_path / "custom"))
    assert debug_directory() == tmp_path / "custom"


def test_debug_write_failure_does_not_replace_a_successful_model_result(monkeypatch, caplog):
    result = object()
    raw = SimpleNamespace(http_response=SimpleNamespace(
        request=SimpleNamespace(content=b'{"messages":[]}'), content=b'{"choices":[]}', status_code=200),
        parse=lambda: result)

    async def create(**kwargs):
        return raw

    def disk_failure(record):
        raise OSError("simulated full disk")

    monkeypatch.setattr(model_debug, "append_exchange", disk_failure)
    completions = model_debug.DebugCompletions(SimpleNamespace(with_raw_response=SimpleNamespace(create=create)),
        phase="agent", provider_id="test", session_id=None)
    assert asyncio.run(completions.create(messages=[])) is result
    assert "Could not write complete model debug exchange" in caplog.text
