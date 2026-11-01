"""Regressions for repeated projections, evidence laundering and false preferences."""
import json
import sys

import pytest

from integrations.qq_astrbot.plugin.memory_repository import MemoryRepository, identity
from tests.test_qq_memory import remember


def test_shared_topic_is_single_record_with_daily_observations(tmp_path):
    repo = MemoryRepository(tmp_path / "memory.db")
    remember(repo, ref="first", now=1000)
    remember(repo, ref="second", now=90000)
    shared = repo.retrieve("qq:999:group:201", "音乐", now=100000)
    assert len(shared) == 1
    assert len(repo.diary_material("999", "elysia", 900, 1100, shared=True)) == 1
    assert len(repo.diary_material("999", "elysia", 89900, 90100, shared=True)) == 1
    assert repo.diary_material("999", "elysia", 5000, 6000, shared=True) == []


def test_identical_fact_with_new_key_reuses_identity_and_keeps_evidence(tmp_path):
    repo = MemoryRepository(tmp_path / "memory.db")
    remember(repo, key="music", now=1000)
    remember(repo, ref="repeat", key="favorite_artist", now=2000)
    rows = [r for r in repo.retrieve("qq:999:dm:101", "音乐", now=3000) if r["visibility"] == "private"]
    assert len(rows) == 1 and rows[0]["fact_key"] == "music"
    with repo.connection() as db:
        assert db.execute("SELECT count(*) FROM memory_evidence WHERE memory_id=?", (rows[0]["id"],)).fetchone()[0] == 2


def test_legacy_duplicates_archive_without_merging_owners_or_events(tmp_path):
    repo = MemoryRepository(tmp_path / "memory.db")
    remember(repo, now=1000)
    with repo.connection() as db:
        original = dict(db.execute("SELECT * FROM memory_items WHERE source_type='processed_summary'").fetchone())
        duplicate = dict(original, id=identity("legacy-day-2"), created_at=90000, updated_at=90000)
        db.execute("INSERT INTO memory_items VALUES(" + ",".join("?" * len(duplicate)) + ")", tuple(duplicate.values()))
    remember(repo, owner="qq:999:dm:102", sender="102", ref="other", now=2000)
    remember(repo, ref="e1", kind="event", key="concert", summary="101参加音乐会", now=3000)
    remember(repo, ref="e2", kind="event", key="concert", summary="101参加音乐会", now=4000)
    assert len(repo.deduplicate(now=100000)) == 1
    assert repo.deduplicate(now=100001) == []
    assert len(repo.diary_material("999", "elysia", 89900, 90100, shared=True)) == 1
    with repo.connection() as db:
        assert db.execute("SELECT count(*) FROM memory_items WHERE source_type='qq_message' AND deleted_at IS NULL").fetchone()[0] == 4
        assert db.execute("SELECT count(*) FROM memory_revisions WHERE payload LIKE '%canonical_id%'").fetchone()[0] == 1


@pytest.mark.skipif(sys.version_info < (3, 12), reason="AstrBot requires Python 3.12")
@pytest.mark.parametrize("case", ["copied_old", "missing_quote", "wrong_meaning", "temporary", "corrected_alias", "unrelated_key", "missing_review", "foreign_key"])
def test_extraction_requires_current_evidence_and_independent_review(tmp_path, monkeypatch, case):
    import asyncio
    from integrations.qq_astrbot.plugin.persistent_memory import PersistentMemory
    from tests.test_qq_memory_runtime import facts

    async def check():
        service = PersistentMemory(tmp_path, timezone_name="UTC")
        remember(service.repository, key="music_preference", summary="101喜欢周杰伦", sensitive=True)
        message = "我现在更喜欢林俊杰"
        evidence = message
        summary = "101现在更喜欢林俊杰"
        if case == "copied_old":
            message, evidence, summary = "这个是你哦", "喜欢周杰伦", "101喜欢周杰伦"
        if case == "missing_quote":
            evidence = "我很喜欢音乐"
        if case in {"wrong_meaning", "temporary", "unrelated_key"}:
            message = evidence = "不用唱啦，熬夜再给你加功能呢"
            summary = "用户表示不想唱歌，因为熬夜会添加新功能。"
        service.repository.enqueue("qq:999:dm:101", "101", "new", message, now=2000)
        candidate = facts(summary, evidence=evidence, key="favorite_singer" if case == "corrected_alias" else "music_preference")
        calls = []

        async def utility(provider, prompt, payload, schema, **kwargs):
            calls.append(payload)
            if "candidates" not in payload:
                return candidate
            if case == "missing_review":
                return {"decisions": []}
            return {"decisions": [{"index": 0, "supported": case != "wrong_meaning",
                "durable": case != "temporary", "existing_key":
                "foreign_owner_key" if case == "foreign_key" else None if case == "unrelated_key" else "music_preference"}]}

        monkeypatch.setattr(service, "_utility", utility)
        ok = await service.extract_one(object())
        rows = service.repository.known("qq:999:dm:101", "101", "elysia")
        assert len(rows) == 1
        assert rows[0]["summary"] == (summary if case == "corrected_alias" else "101喜欢周杰伦")
        assert len(calls) == (1 if case in {"copied_old", "missing_quote"} else 2)
        assert ok == (case not in {"missing_review", "foreign_key"})
        with service.repository.connection() as db:
            row = db.execute("SELECT source_ref FROM memory_items WHERE fact_key='music_preference'").fetchone()
            assert row[0] == ("new" if case == "corrected_alias" else "m1")
    asyncio.run(check())
