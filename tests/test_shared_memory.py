"""Synthetic acceptance for shared memory: no production DB, model or transport."""
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from integrations.qq_astrbot.plugin.shared_memory import SharedMemory, RequestContext, ModuleDescriptor, MemoryConflict, MemoryPressure
from integrations.qq_astrbot.plugin.memory_policies import ModulePolicy
from integrations.qq_astrbot.plugin.memory_tasks import MemoryTasks, InputTooLarge


class Clock:
    def __init__(self):
        self.now = datetime.fromisoformat("2026-01-01T12:00:00+08:00").timestamp()
    def __call__(self):
        return self.now
    def days(self, n=1):
        self.now += n * 86400


class Model:
    def __init__(self):
        self.calls, self.fail, self.limit, self.hook = [], None, None, None
        self.special = []
    async def __call__(self, key, payload, schema, tokens):
        self.calls.append((key, payload))
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        if self.fail and self.fail in key:
            raise ValueError("synthetic_failure")
        if self.limit and len(json.dumps(payload)) > self.limit:
            raise InputTooLarge()
        if key == "shared_qq_special":
            return {"entries": self.special}
        if key == "shared_qq_review":
            return dict(supported=True, durable=True, current=True,
                existing_id=payload["current"][0]["id"] if payload["current"] else None, reason="supported")
        if key == "shared_qq_correction":
            return {"corrections": []}
        if key == "shared_qq_correction_review":
            return {"approved_ids": []}
        # Unique summary per source snapshot; kept short, as a fake tokenizer counts characters.
        value = {"summary": "summary:" + str(len(self.calls))}
        if "selected_ids" in schema["properties"]:
            value["selected_ids"] = [c["id"] for c in payload["candidates"][:payload["required_count"]]]
        return value


async def count(text):
    return len(text)


@pytest.fixture
def setup(tmp_path):
    clock, model = Clock(), Model()
    service = SharedMemory(tmp_path / "memory.db", clock=clock)
    tasks = MemoryTasks(service, model, count_text=count)
    return service, tasks, clock, model


def append(s, c, number, **kwargs):
    return s.append_short_term(bot="9", character="fish", module="video", kind="video_watch",
        event_id="view:" + str(number), material_id="video:" + str(number), body="watch " + str(number),
        occurred=c(), visibility="shared", **kwargs)


def run(coro):
    return asyncio.run(coro)


def test_short_identity_consumers_and_no_threshold(setup):
    s, t, c, m = setup
    for i, feeling in enumerate(("ordinary", "boring", "dislike", "uncertain")):
        key = append(s, c, i)
        assert append(s, c, i) == key
    rows = s.rows(bot="9", character="fish")
    assert len(rows) == 4 and all(r["short"] and not r["long"] for r in rows)
    with s.connection() as db:
        assert db.execute("SELECT count(*) FROM sm_consumers WHERE state='pending'").fetchone()[0] == 8


@pytest.mark.parametrize("size", [0, 1, 2, 7])
def test_video_day_counts_hashes_and_retries(setup, size):
    s, t, c, m = setup
    originals = {append(s, c, i) for i in range(size)}
    assert run(t.video_day("9", "fish", s.day(c())))
    before = t.batches("9", "fish")
    assert len(before) == bool(size)
    assert run(t.video_day("9", "fish", s.day(c())))
    assert t.batches("9", "fish") == before
    with s.connection() as db:
        members = list(db.execute("SELECT m.record_id,m.hash,r.hash FROM sm_members m JOIN sm_records r ON m.record_id=r.id"))
    assert len(members) == min(2, size)
    assert all(r[0] in originals and r[1] == r[2] for r in members)


def test_ten_batches_math_10_19_28_and_prior_summaries(setup):
    s, t, c, m = setup
    for day in range(28):
        append(s, c, day*2)
        append(s, c, day*2+1)
        assert run(t.video_day("9", "fish", s.day(c())))
        batches = t.batches("9", "fish")
        if day + 1 in (10, 19, 28):
            assert len(batches) == 1
            generation = (day + 1 - 10) // 9 + 1
            assert batches[0]["generation"] == generation
            with s.connection() as db:
                assert db.execute("SELECT count(*) FROM sm_members WHERE batch=?", (batches[0]["id"],)).fetchone()[0] == 2 + generation*2
        c.days()
    calls = [p for k, p in m.calls if k == "shared_video_compact"]
    assert [len(p["materials"]) for p in calls] == [10, 10, 10]
    assert [len(p["candidates"]) for p in calls] == [20, 22, 24]


def test_history_cap_and_pending_18_reselection_does_not_add_generation(setup):
    s, t, c, m = setup
    s.configure({"selected_history_limit": 4})
    for day in range(18):
        append(s, c, day*2)
        append(s, c, day*2+1)
        run(t.video_day("9", "fish", s.day(c())))
        c.days()
    batches = t.batches("9", "fish")
    assert len(batches) == 9  # history + eight daily; the next day triggers compaction immediately
    s.configure({"selected_history_limit": 2})
    assert run(t.compact_video("9", "fish", reselect=True))
    history = next(b for b in t.batches("9", "fish") if b["kind"] == "history")
    assert history["generation"] == 1
    with s.connection() as db:
        assert db.execute("SELECT count(*) FROM sm_members WHERE batch=?", (history["id"],)).fetchone()[0] == 2


def test_diary_15_29_preserves_first_day_and_no_recursion(setup):
    s, t, c, m = setup
    first = c()
    for day in range(29):
        append(s, c, day)
        assert run(t.diary_day("9", "fish", s.day(c())))
        rows = s.rows(bot="9", character="fish", module="diary", long=1)
        assert len(rows) <= 15
        if day + 1 in (15, 29):
            assert len(rows) == 1 and rows[0]["kind"] == "diary_rollup"
            assert rows[0]["start"] <= first
        c.days()
    final = [p for k,p in m.calls if k == "shared_diary_compact"][-1]
    assert any(r["kind"] == "diary_rollup" for r in final["materials"])
    daily = [p for k,p in m.calls if k == "shared_diary_daily"]
    assert all(all(r["kind"] == "video_watch" for r in p["materials"]) for p in daily)


def test_failure_caps_keep_old_bodies_and_pending_promotions(setup):
    s, t, c, m = setup
    m.fail = "compact"
    for day in range(18):
        append(s, c, day*2)
        append(s, c, day*2+1)
        run(t.video_day("9", "fish", s.day(c())))
        run(t.diary_day("9", "fish", s.day(c())))
        c.days()
    assert len(t.batches("9", "fish")) == 10
    assert len(s.rows(bot="9", character="fish", module="diary", long=1)) == 15
    assert len(s.rows(bot="9", character="fish", kind="diary_daily", long=0)) == 3
    assert all(r["body"] for r in s.rows(bot="9", character="fish", long=1))
    m.fail = None
    c.now += 61
    with s.connection() as db:
        failed = [r[0] for r in db.execute("SELECT id FROM sm_jobs WHERE state='failed'")]
    for key in failed:
        s.retry(key)
    assert run(t.compact_video("9", "fish"))
    assert run(t.compact_diary("9", "fish"))


def test_permissions_before_ranking_and_mixed_derivation(setup):
    s, t, c, m = setup
    private = s.append_short_term(bot="9", character="fish", module="qq", kind="qq_session_summary", event_id="dm",
        body="private secret", scope="qq:9:dm:1", visibility="session", occurred=c())
    group = s.append_short_term(bot="9", character="fish", module="qq", kind="qq_session_summary", event_id="group",
        body="other group", scope="qq:9:group:2", visibility="session", occurred=c())
    derived = s.append_short_term(bot="9", character="fish", module="diary", kind="diary_daily", event_id="mixed",
        body="mixed secrets", visibility="shared", parents=[private, group], derived=True, experience=False, occurred=c())
    rows = s.candidates(RequestContext("fish", "9", "video", "qq:9:dm:3", query="secret"))
    assert rows == []
    assert {r["id"] for r in s.candidates(RequestContext("fish", "9", "qq", "qq:9:dm:1"))} == {private}
    internal = s.candidates(RequestContext("fish", "9", "diary", internal=True))
    assert len(internal) == 3 and next(r for r in internal if r["id"] == derived)["visibility"] == "internal"


def test_retention_no_long_term_cannot_bypass_diary_and_deletion_cascades(setup):
    s, t, c, m = setup
    key = append(s, c, 1, retention="no_long_term")
    assert run(t.diary_day("9", "fish", s.day(c())))
    assert [p for k,p in m.calls if k == "shared_diary_daily"][0]["materials"] == []
    private = append(s,c,2)
    derived = s.append_short_term(bot="9", character="fish", module="diary", kind="diary_daily", event_id="derived",
        body="sensitive", parents=[private], derived=True, experience=False, occurred=c())
    s.withdraw(private)
    with s.connection() as db:
        for ident in (private,derived):
            row = db.execute("SELECT body,status FROM sm_records WHERE id=?", (ident,)).fetchone()
            assert tuple(row) == ("", "withdrawn")
    with pytest.raises(MemoryConflict):
        append(s,c,2)


def test_ttl_ack_and_pin_all_required(setup):
    s, t, c, m = setup
    key = append(s,c,1)
    c.days(4)
    s.cleanup()
    assert s.rows(bot="9",character="fish")[0]["short"] == 1
    with s.connection() as db:
        s.ack(db,[key],"video_daily")
    s.cleanup()
    assert s.rows(bot="9",character="fish")[0]["short"] == 1
    row = s.rows(bot="9",character="fish")[0]
    job = s.begin_job(bot="9",character="fish",module="video",operation="test",inputs=[row])
    with s.connection() as db:
        s.ack(db,[key],"diary")
    s.cleanup()
    assert s.rows(bot="9",character="fish")[0]["short"] == 1
    s.fail_job(job,ValueError())
    s.cleanup()
    assert s.rows(bot="9",character="fish")[0]["short"] == 0
    c.days(8)
    s.cleanup()
    with s.connection() as db:
        assert tuple(db.execute("SELECT body,status FROM sm_records WHERE id=?",(key,)).fetchone()) == ("","purged")


def test_illegal_selection_and_delete_during_model_cannot_commit(setup):
    s,t,c,m=setup
    key=append(s,c,1)
    m.hook=lambda:s.withdraw(key)
    assert not run(t.video_day("9","fish",s.day(c())))
    assert t.batches("9","fish") == []
    assert not s.rows(bot="9",character="fish",long=1)


def test_fourth_module_receives_storage_acl_stats_and_own_policy(setup):
    s,t,c,m=setup
    policy=ModulePolicy(("reading_note",))
    s.register_module(ModuleDescriptor("reading","阅读",("reading_note",)),policy)
    key=s.append_short_term(bot="9",character="fish",module="reading",kind="reading_note",event_id="book",
        body="reading experience",visibility="shared",occurred=c())
    with s.connection() as db:
        with pytest.raises(PermissionError):
            s.promote_from_short_term(key,policy=s.policies["video"],db=db)
        s.promote_from_short_term(key,policy=policy,db=db)
    view=run(s.build_memory_view(RequestContext("fish","9","qq"),count=count,available=6144))
    assert len(view["items"])==1 and view["attribution"][0]["layer"]=="long"
    assert s.get_inventory_stats("fish")["modules"][-1]["module_id"]=="reading"
    assert s.get_request_usage(character="fish")==[]


def test_session_snapshot_incremental_midnight_and_new_arrival(setup):
    s,t,c,m=setup
    def event(scope,ident):
        return s.append_event(bot="9",character="fish",module="qq",event_id=ident,scope=scope,subject="1",payload={"text":ident},occurred=c())
    for scope in ("qq:9:dm:1","qq:9:group:2","qq:9:group:3"):
        event(scope,scope)
    m.hook=lambda:event("qq:9:dm:1","late during generation")
    assert run(t.process_idle(character="fish",force=True))
    assert len(s.rows(bot="9",character="fish",module="qq"))==3
    with s.connection() as db:
        assert db.execute("SELECT count(*) FROM sm_events WHERE status='pending'").fetchone()[0]==1
    c.days()
    event("qq:9:dm:1","next day")
    assert run(t.process_idle(character="fish",force=True))
    rows=s.rows(bot="9",character="fish",module="qq")
    assert len(rows)==5 and len({r["day"] for r in rows})==2
    assert not s.rows(bot="9",character="fish",module="qq",long=1)


def test_hierarchical_diary_covers_more_than_eight(setup):
    s,t,c,m=setup
    for i in range(40):
        append(s,c,i)
    m.limit=2500
    assert run(t.diary_day("9","fish",s.day(c())))
    seen={r["id"] for k,p in m.calls if k=="shared_diary_daily" and len(json.dumps(p))<=m.limit for r in p["materials"]}
    originals={r["id"] for r in s.rows(bot="9",character="fish",module="video")}
    assert originals<=seen


def test_capacity_config_and_nostore_tombstone(setup):
    s,t,c,m=setup
    with pytest.raises(ValueError):
        s.configure({"memory_tokens":1})
    s.set_retention("9","fish","qq:9:dm:1","no_store")
    assert s.append_event(bot="9",character="fish",module="qq",event_id="x",scope="qq:9:dm:1",subject="1",payload={"text":"secret"}) is None
    with s.connection() as db:
        assert db.execute("SELECT count(*) FROM sm_events").fetchone()[0]==0


def test_legacy_backup_permissions_and_disabled_queue(tmp_path):
    from integrations.qq_astrbot.plugin.memory_repository import MemoryRepository
    from tests.test_qq_memory import remember
    repo=MemoryRepository(tmp_path/"m.db")
    remember(repo,summary="legacy private",sensitive=True)
    repo.enqueue("qq:999:dm:101","101","pending","private")
    s=SharedMemory(repo.path)
    assert repo.path.with_name("m.db.pre-shared-v1.bak").exists()
    with pytest.raises(sqlite3.IntegrityError,match="legacy_long_term_write_disabled"):
        repo.add_video("qq:999:dm:101","ref","old path",[])
    assert repo.retrieve("qq:999:dm:101","legacy")
    assert not repo.retrieve("qq:999:dm:102","legacy")
    SharedMemory(repo.path)
    with s.connection() as db:
        assert db.execute("SELECT status FROM memory_inbox WHERE source_ref='pending'").fetchone()[0]=="migrated_short_source"
        assert db.execute("SELECT count(*) FROM sm_events WHERE status='pending'").fetchone()[0]==1


def test_late_day_revises_same_batch_and_repairs_rollup_without_generation(setup):
    s,t,c,m=setup
    first_day=s.day(c())
    first_time=c()
    for day in range(10):
        append(s,c,day*2)
        append(s,c,day*2+1)
        assert run(t.video_day("9","fish",s.day(c())))
        c.days()
    with s.connection() as db:
        assert s.generation(db,"9","fish","video")==1
    s.append_short_term(bot="9",character="fish",module="video",kind="video_watch",event_id="late",material_id="late",
        body="late completed analysis",occurred=first_time,visibility="session",scope="qq:9:dm:1")
    assert run(t.video_day("9","fish",first_day))
    assert len(t.batches("9","fish"))==1
    with s.connection() as db:
        assert s.generation(db,"9","fish","video")==1
        history=db.execute("SELECT r.* FROM sm_records r JOIN sm_batches b ON b.summary_id=r.id WHERE b.active=1").fetchone()
        assert history["status"]=="active" and history["visibility"] in {"session","internal"}
        assert db.execute("SELECT count(*) FROM sm_batches WHERE day=? AND kind='daily'",(first_day,)).fetchone()[0]==1


def test_late_diary_repair_preserves_original_start_and_generation(setup):
    s,t,c,m=setup
    first=c()
    day=s.day(first)
    for n in range(15):
        append(s,c,n)
        assert run(t.diary_day("9","fish",s.day(c())))
        c.days()
    s.append_short_term(bot="9",character="fish",module="qq",kind="qq_session_summary",event_id="late",
        body="late real session",scope="qq:9:dm:1",visibility="session",occurred=first)
    assert run(t.diary_day("9","fish",day))
    current=s.rows(bot="9",character="fish",module="diary",long=1)
    assert len(current)==1 and current[0]["start"]<=first and current[0]["generation"]==1


def test_midnight_completion_and_offline_dates_not_generation_dates(setup):
    s,t,c,m=setup
    c.now=datetime.fromisoformat("2026-01-01T23:59:00+08:00").timestamp()
    event=s.append_event(bot="9",character="fish",module="video",event_id="midnight",scope="",subject="character",role="source",
        visibility="shared",occurred=c(),payload={"analysis":{"summary":"real partial video","uncovered_intervals":[[3,9]]},"material_id":"video"})
    c.now+=600
    assert run(t.process_videos("fish")) is None
    row=s.rows(bot="9",character="fish",kind="video_watch")[0]
    assert row["day"]=="2026-01-01" and row["created"]>row["occurred"]
    assert json.loads(row["metadata"])["partial"]
    c.days(3)
    run(t.catch_up("fish"))
    with s.connection() as db:
        days=[r[0] for r in db.execute("SELECT day FROM sm_days WHERE state='done' ORDER BY day")]
    assert days==["2026-01-01","2026-01-02","2026-01-03","2026-01-04"]


def test_leases_crash_recovery_and_invalid_ids_are_atomic(setup):
    s,t,c,m=setup
    key=append(s,c,1)
    inputs=s.rows(bot="9",character="fish")
    job=s.begin_job(bot="9",character="fish",module="video",operation="crash",inputs=inputs)
    second=SharedMemory(s.path,clock=c)
    assert second.begin_job(bot="9",character="fish",module="video",operation="crash",inputs=inputs) is None
    c.now+=301
    recovered=second.begin_job(bot="9",character="fish",module="video",operation="crash",inputs=inputs)
    with pytest.raises(MemoryConflict):
        s.commit_job(job,lambda db:"stale")
    assert second.commit_job(recovered,lambda db:"restored")=="restored"
    async def illegal(*args):return {"summary":"summary","selected_ids":["invented"]}
    t.model=illegal
    assert not run(t.video_day("9","fish",s.day(c())))
    assert t.batches("9","fish")==[] and s.rows(bot="9",character="fish")[0]["body"]=="watch 1"


def test_group_member_no_store_propagates_without_deleting_other_members(setup):
    s,t,c,m=setup
    for member in ("1","2"):
        s.append_event(bot="9",character="fish",module="qq",scope="qq:9:group:3",subject=member,event_id=member,payload={"text":"member "+member})
    run(t.process_idle(character="fish",force=True))
    s.set_subject_retention("9","fish","qq:9:group:3","1","no_store")
    with s.connection() as db:
        assert db.execute("SELECT payload FROM sm_events WHERE subject='1'").fetchone()[0]==""
        assert db.execute("SELECT payload FROM sm_events WHERE subject='2'").fetchone()[0]
    assert not s.rows(bot="9",character="fish")
    assert s.append_event(bot="9",character="fish",module="qq",scope="qq:9:group:3",subject="1",event_id="later",payload={"text":"not retained"}) is None


def test_default_80_plus_18_and_failed_merge_backlog(setup):
    s,t,c,m=setup
    ids=[append(s,c,i) for i in range(80)]
    rows=s.rows(bot="9",character="fish")
    with s.connection() as db:
        t._batch_commit(db,bot="9",character="fish",day=s.day(c()),kind="history",rows=rows,summary="retained history",selected=ids,generation=39)
        s.set_generation(db,"9","fish","video",39)
    m.fail="compact"
    for day in range(9):
        c.days()
        append(s,c,100+day*2)
        append(s,c,101+day*2)
        run(t.video_day("9","fish",s.day(c())))
    stats=next(r for r in s.get_inventory_stats("fish")["modules"] if r["module_id"]=="video")
    assert {b["kind"]:b["selected"] for b in stats["batches"]}=={"daily":18,"history":80}
    c.days()
    append(s,c,200)
    assert not run(t.video_day("9","fish",s.day(c())))
    assert len(t.batches("9","fish"))==10
    m.fail=None
    c.now+=61
    assert run(t.compact_video("9","fish"))
    history=t.batches("9","fish")
    assert len(history)==1 and history[0]["generation"]==40
    with s.connection() as db:
        assert db.execute("SELECT count(*) FROM sm_members WHERE batch=?",(history[0]["id"],)).fetchone()[0]==80


def test_migration_dry_run_copy_and_governed_rollback(tmp_path):
    from scripts.qq_companion.migrate_shared_memory import migrate,rollback_copy
    from integrations.qq_astrbot.plugin.memory_repository import MemoryRepository
    from tests.test_qq_memory import remember
    source=tmp_path/"source.db"
    repo=MemoryRepository(source)
    remember(repo,summary="private synthetic fixture",sensitive=True)
    assert migrate(source)["dry_run"]
    with repo.connection() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='sm_records'").fetchone()
    copied=tmp_path/"copy.db"
    migrate(source,copied)
    s=SharedMemory(copied)
    row=MemoryRepository(copied).retrieve("qq:999:dm:101","private")[0]
    s.govern_legacy(row["id"],character="elysia")
    rollback=tmp_path/"rollback.db"
    rollback_copy(copied,rollback)
    assert MemoryRepository(rollback).retrieve("qq:999:dm:101","private")==[]
    assert repo.retrieve("qq:999:dm:101","private")


def test_agreement_after_checkpoint_uses_real_prior_evidence_without_resummarizing(setup):
    s,t,c,m=setup
    scope="qq:9:dm:1"
    user=s.append_event(bot="9",character="fish",module="qq",event_id="intent",scope=scope,subject="1",payload={"text":"long term request"})
    assert run(t.process_idle(character="fish",force=True))
    c.now+=1
    sent=s.append_event(bot="9",character="fish",module="qq",event_id="agreement",scope=scope,subject="9",role="assistant",payload={"text":"actual agreement"})
    m.special=[dict(user_event=user,agreement_event=sent,intent_quote="long term request",agreement_quote="actual agreement",
        long_term_intent=True,character_likes=True,attribute="request",summary="understood request")]
    assert run(t.process_idle(character="fish",force=True))
    summaries=[payload for key,payload in m.calls if key=="shared_qq_summary"]
    assert [item["id"] for item in summaries[-1]["materials"]]==[sent]
    special=s.rows(bot="9",character="fish",kind="qq_explicit_liked")
    assert len(special)==1 and run(t.review_special(special[0]))


@pytest.mark.parametrize("long_session",[False,True])
def test_explicit_correction_updates_existing_without_liking_or_new_fact(setup,long_session):
    s,t,c,m=setup
    scope="qq:9:dm:1"
    original=s.append_short_term(bot="9",character="fish",module="qq",kind="qq_explicit_liked",event_id="old",
        body="old preference",scope=scope,subject="1",visibility="session",metadata={"attribute":"preference"})
    with s.connection() as db:
        s.promote_from_short_term(original,policy=s.policies["qq"],db=db)
    assert run(t.diary_day("9","fish",s.day(c())))
    event=s.append_event(bot="9",character="fish",module="qq",event_id="correct",scope=scope,subject="1",payload={"text":"replace the old preference"})
    if long_session:
        m.limit=1800
        for i in range(20):
            s.append_event(bot="9",character="fish",module="qq",event_id="noise"+str(i),scope=scope,subject="1",payload={"text":"other real event "+str(i)+"x"*240})
    async def model(key,payload,schema,tokens):
        if m.limit and len(json.dumps(payload))>m.limit:
            raise InputTooLarge()
        if key=="shared_qq_correction":
            return {"corrections":[dict(existing_id=original,user_event=event,quote="replace the old preference",replacement="corrected preference")] if any(e["id"]==event for e in payload["events"]) else []}
        if key=="shared_qq_correction_review":
            return {"approved_ids":[original]}
        return await m(key,payload,schema,tokens)
    t.model=model
    assert run(t.process_idle(character="fish",force=True))
    entries=s.rows(bot="9",character="fish",kind="qq_explicit_liked",long=1)
    assert len(entries)==1 and entries[0]["id"]==original and entries[0]["body"]=="corrected preference"
    assert not s.rows(bot="9",character="fish",kind="diary_daily")


def test_public_projection_retries_and_replaces_same_day_candidate(setup):
    s,t,c,m=setup
    append(s,c,1)
    day=s.day(c())
    m.fail="shared_diary_public"
    assert not run(t.diary_day("9","fish",day))
    assert len(s.rows(bot="9",character="fish",kind="diary_daily"))==1
    c.now+=61
    m.fail=None
    assert run(t.diary_day("9","fish",day))
    append(s,c,2)
    assert run(t.diary_day("9","fish",day))
    assert len(s.rows(bot="9",character="fish",kind="diary_public"))==1


def test_special_duplicate_and_changed_attribute_do_not_depend_on_model_id(setup):
    s,t,c,m=setup
    async def review(*args):
        return dict(supported=True,durable=True,current=True,existing_id=None,reason="supported")
    t.model=review
    for index,body in enumerate(("same request","same request","changed request")):
        c.now+=1
        key=s.append_short_term(bot="9",character="fish",module="qq",kind="qq_explicit_liked",event_id=str(index),
            body=body,scope="qq:9:dm:1",subject="1",visibility="session",metadata={"attribute":"stable_attribute"})
        row=next(r for r in s.rows(bot="9",character="fish",kind="qq_explicit_liked") if r["id"]==key)
        assert run(t.review_special(row))
        current=s.rows(bot="9",character="fish",kind="qq_explicit_liked",long=1)
        assert len(current)==1 and current[0]["body"]==body
