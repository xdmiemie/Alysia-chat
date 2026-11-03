"""Regressions for repeated projections, evidence laundering and false preferences."""
import json
import sys

import pytest

from integrations.qq_astrbot.plugin.memory_repository import MemoryRepository, identity
from tests.test_qq_memory import remember


@pytest.mark.parametrize("intent,liked,agreement,quote,expected",[
    (True,True,True,True,True),(False,True,True,True,False),(True,False,True,True,False),
    (True,True,False,True,False),(True,True,True,False,False)])
def test_special_requires_and_and_actual_sent_evidence(tmp_path,intent,liked,agreement,quote,expected):
    from tests.test_shared_memory import Clock,Model,count,run
    from integrations.qq_astrbot.plugin.shared_memory import SharedMemory
    from integrations.qq_astrbot.plugin.memory_tasks import MemoryTasks
    clock,model=Clock(),Model()
    s=SharedMemory(tmp_path/"m.db",clock=clock)
    tasks=MemoryTasks(s,model,count_text=count)
    user=s.append_event(bot="9",character="fish",module="qq",event_id="user",scope="qq:9:dm:1",subject="1",payload={"text":"long term request"})
    sent=s.append_event(bot="9",character="fish",module="qq",event_id="sent",scope="qq:9:dm:1",subject="9",role="assistant" if agreement else "user",payload={"text":"actual agreement"})
    model.special=[dict(user_event=user,agreement_event=sent,intent_quote="long term request" if quote else "fabricated",agreement_quote="actual agreement",long_term_intent=intent,character_likes=liked,attribute="request",summary="understood request")]
    run(tasks.process_idle(character="fish",force=True))
    rows=s.rows(bot="9",character="fish",kind="qq_explicit_liked")
    assert bool(rows)==expected
    if expected:
        assert not rows[0]["long"]
        assert run(tasks.review_special(rows[0]))
        assert len(s.rows(bot="9",character="fish",kind="qq_explicit_liked",long=1))==1


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
