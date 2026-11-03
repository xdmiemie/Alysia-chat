"""Historical reconciliation uses synthetic data; never starts services or sends messages."""
from collections import Counter
from datetime import datetime
import json
import sqlite3
import sys

import pytest

from integrations.qq_astrbot.plugin.memory_repository import MemoryRepository, identity
from integrations.qq_astrbot.plugin.shared_memory import SharedMemory, RequestContext, digest
from scripts.qq_companion.reconcile_legacy_memory import build, apply, main, read_db, snapshot, RECEIPT
from tests.test_qq_memory import remember


@pytest.fixture
def legacy(tmp_path):
    database, videos = tmp_path / "memories.sqlite3", tmp_path / "douyin.sqlite3"
    repo = MemoryRepository(database)
    stamp = datetime.fromisoformat("2026-01-02T12:00:00+08:00").timestamp()
    for number in (1, 2, 3):
        repo.add_public_video("999", f"https://douyin.com/video/{number}", f"objective analysis {number}", [], now=stamp)
    repo.add_video("qq:999:dm:101", "private-source", "private objective analysis", [], now=stamp)
    remember(repo, now=stamp, summary="private corrected preference")
    repo.save_diary("999", "elysia", "2026-01-01", "unsupported old diary claim", None, [], now=stamp)
    with repo.connection() as db:
        db.execute("UPDATE memory_items SET deleted_at=? WHERE source_ref=?", (stamp, "https://douyin.com/video/3"))
        qq_id = db.execute("SELECT id FROM memory_items WHERE source_type='qq_message'").fetchone()[0]
    interest = json.dumps({"reasons": [r"$\\text{I loved this fish joke!}$"], "uncertainty": ["I did not see its ending."]})
    with sqlite3.connect(videos) as db:
        db.executescript("CREATE TABLE videos(account TEXT,id TEXT,interest TEXT,analysis TEXT);"
                         "CREATE TABLE views(account TEXT,video TEXT,created REAL);")
        for number in (1, 2, 3):
            db.execute("INSERT INTO videos VALUES('account',?,?,?)", (str(number), interest, '{}'))
            db.execute("INSERT INTO views VALUES('account',?,?)", (str(number), stamp - 10))
    with sqlite3.connect(tmp_path / "media_analysis.sqlite3") as db:
        db.execute("CREATE TABLE video_jobs(id TEXT,owner TEXT,source TEXT)")
        db.execute("INSERT INTO video_jobs VALUES('job','qq:999:dm:101','private-source')")
    service = SharedMemory(database)
    with service.connection() as db:
        review = dict(source_snapshot=snapshot(db), expected_active=dict(Counter(r[0] for r in db.execute(
            "SELECT source_type FROM memory_items WHERE deleted_at IS NULL"))),
            video_reviews={'1': dict(interest_hash=digest(interest), reasons=[0], uncertainty=[0])},
            qq_sessions=[dict(day="2026-01-02", legacy_ids=[qq_id], body="We discussed a private corrected preference.")],
            legacy_diaries={"2026-01-01": "The old diary has no evidence for its claim."},
            video_days={"2026-01-02": dict(selected_video_ids=['1'], summary="The fish joke was fun; its ending is unknown.")},
            diaries={"2026-01-02": "I enjoyed a fish joke and remembered our private conversation."})
    return database, videos, review, service


def test_reconciliation_exact_role_text_acl_consumers_and_backup(legacy):
    database, videos, review, service = legacy
    plan = build(database, videos, review, len)
    report = apply(database, videos, plan)
    assert (report['video_replaced'], report['video_deleted'], report['previous_deletions_preserved']) == (1, 2, 1)
    with read_db(database) as db:
        watch = db.execute("SELECT * FROM sm_records WHERE kind='video_watch'").fetchone()
        assert watch['body'] == "I loved this fish joke!\nI did not see its ending."
        assert watch['day'] == '2026-01-02'
        assert watch['occurred'] < watch['created']
        assert db.execute("SELECT count(*) FROM sm_records").fetchone()[0] == 5
        assert db.execute("SELECT count(*) FROM sm_records WHERE module='qq' AND long=1").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM sm_consumers WHERE state!='done'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM sm_members WHERE hash=?", (watch['hash'],)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM sm_records WHERE kind='diary_public'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM sm_records WHERE visibility='internal' AND kind='diary_daily'").fetchone()[0] == 2
        for table in ('memory_items','diary_entries','memory_evidence','memory_revisions','memory_links'):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with read_db(database.with_name(database.name+'.pre-shared-v1.bak')) as db:
        assert db.execute('SELECT count(*) FROM memory_items').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM diary_entries').fetchone()[0] == 0
    own = service.candidates(RequestContext(bot='999',character='elysia',module='qq',scope='qq:999:dm:101'))
    other = service.candidates(RequestContext(bot='999',character='elysia',module='qq',scope='qq:999:dm:102'))
    assert any(r['kind']=='qq_session_summary' for r in own)
    assert not any(r['kind'] in {'qq_session_summary','diary_daily'} for r in other)
    for event_id, scope in [('douyin:account:2',''),('douyin:account:3',''),('qq-video:job','qq:999:dm:101')]:
        assert service.append_event(bot='999',character='elysia',module='video',scope=scope,subject='character',
            event_id=event_id,payload={'analysis':'must not revive'}) is None


def test_changed_source_rolls_back_before_erasing_legacy(legacy):
    database, videos, review, service = legacy
    plan = build(database, videos, review, len)
    with sqlite3.connect(videos) as db:
        db.execute("UPDATE videos SET interest='{}' WHERE id='1'")
    with pytest.raises(ValueError,match='video_changed_before_commit'):
        apply(database, videos, plan)
    with read_db(database) as db:
        assert snapshot(db) == review['source_snapshot']
        assert db.execute('SELECT count(*) FROM sm_records').fetchone()[0] == 0
        assert not db.execute('SELECT 1 FROM sm_meta WHERE key=?',(RECEIPT,)).fetchone()


def test_failure_after_short_writes_rolls_back_whole_import(legacy, monkeypatch):
    from integrations.qq_astrbot.plugin.memory_tasks import MemoryTasks
    database, videos, review, _ = legacy
    def fail(*args, **kwargs):
        raise RuntimeError('simulated_batch_failure')
    monkeypatch.setattr(MemoryTasks, '_batch_commit', fail)
    with pytest.raises(RuntimeError,match='simulated_batch_failure'):
        apply(database,videos,build(database,videos,review,len))
    with read_db(database) as db:
        assert snapshot(db) == review['source_snapshot']
        assert db.execute('SELECT count(*) FROM sm_records').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM sm_events').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM sm_tombstones').fetchone()[0] == 0


@pytest.mark.parametrize('restriction', ['tombstone','no_store','no_long_term'])
def test_import_honors_existing_governance(legacy, restriction):
    database, videos, review, service = legacy
    plan = build(database, videos, review, len)
    source = plan['sources'][0]
    if restriction == 'tombstone':
        service.withdraw(source['id'])
    else:
        # Set the rule directly so this test isolates the import's own rollback.
        with service.connection() as db:
            db.execute('INSERT INTO sm_governance VALUES(?,?,?,?)',
                (source['bot'],source['character'],source['scope'],restriction))
    with pytest.raises(ValueError,match='source_was_withdrawn|retention_disallows_import'):
        apply(database, videos, plan)
    with read_db(database) as db:
        assert snapshot(db) == review['source_snapshot']
        assert db.execute('SELECT count(*) FROM sm_events').fetchone()[0] == 0


@pytest.mark.parametrize('invalid', ['selection','budget','snapshot','watch'])
def test_review_requires_exact_candidates_limits_and_provenance(legacy, invalid):
    database, videos, review, _ = legacy
    if invalid == 'selection':review['video_days']['2026-01-02']['selected_video_ids'] = ['1','1']
    elif invalid == 'budget':review['qq_sessions'][0]['body'] = 'x'*601
    elif invalid == 'snapshot':review['source_snapshot'] = 'stale'
    else:
        with sqlite3.connect(videos) as db:db.execute("DELETE FROM views WHERE video='1'")
    with pytest.raises(ValueError):build(database,videos,review,len)
    with read_db(database) as db:
        assert db.execute('SELECT count(*) FROM sm_records').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM memory_items').fetchone()[0] > 0


def test_repeat_cli_uses_receipt_without_reimporting(legacy, monkeypatch, capsys):
    database,videos,review,_ = legacy
    apply(database,videos,build(database,videos,review,len))
    monkeypatch.setattr(sys,'argv',['reconcile',str(database),'--videos',str(videos),'--review','unused.json','--apply'])
    main()
    assert json.loads(capsys.readouterr().out)['video_replaced'] == 1
    with read_db(database) as db:
        assert db.execute('SELECT count(*) FROM sm_records').fetchone()[0] == 5
