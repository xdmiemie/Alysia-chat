"""Q6 publication policy, durable side effects, privacy and protocol contracts."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from integrations.qq_astrbot.plugin.memory_repository import MemoryRepository
from integrations.qq_astrbot.plugin.qzone_client import OneBotHTTP, QzoneClient, QzoneError, parse_response
from integrations.qq_astrbot.plugin.qzone_config import DEFAULTS, load_config, save_config
from integrations.qq_astrbot.plugin.qzone_runtime import DiaryPublisher, SELF_GATES, audience_verified, two_sentences
from integrations.qq_astrbot.plugin.qzone_store import QzoneStore
from scripts.qq_companion.qzone import self_test


ACCOUNT = "123456789"
NOW = datetime(2026, 10, 30, 23, 45, tzinfo=ZoneInfo("Asia/Hong_Kong"))
TEXT = "今天的旋律留下一点温柔。也想把这份轻松带到明天。"


def run(operation):
    from scripts.qq_companion.probe_q3_local import LocalLoop
    loop = LocalLoop()
    try:
        return loop.run_until_complete(operation)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


class FakeClient:
    def __init__(self, account=ACCOUNT):
        self.account, self.posts, self.writes = account, [], []
        self.error = self.detail_error = None
        self.reads = []
        self.closed = False
        self.fail_delete = False
        self.friend_posts = {}

    async def login(self):
        return {k: "present" for k in ("uin", "skey", "p_skey")}

    async def publish(self, text, audience):
        self.writes.append(("post", text))
        if self.error:
            raise self.error
        tid = "post-" + str(len(self.writes))
        self.posts.insert(0, {"tid": tid, "uin": self.account, "content": text,
            "ugc_right": 64 if audience["visibility"] == "self" else 16, "allow_uins": audience["friends"]})
        return tid

    async def list_posts(self, target=None, limit=10):
        self.reads.append(target or self.account)
        return list(self.posts[:limit]) if not target or target == self.account else list(self.friend_posts.get(target, [])[:limit])

    async def detail(self, target, tid):
        if self.detail_error:
            raise self.detail_error
        row = next(p for p in (self.posts if target == self.account else self.friend_posts.get(target, [])) if p["tid"] == tid)
        return {"code": 0, "data": row}

    async def delete(self, tid):
        self.writes.append(("delete", tid))
        if not self.fail_delete:
            self.posts = [p for p in self.posts if p["tid"] != tid]

    async def comment(self, *args):
        self.writes.append(("comment", args))
        target, tid, text = args
        next(p for p in self.friend_posts[target] if p["tid"] == tid).setdefault("commentlist", []).append(
            {"uin": self.account, "content": text})

    async def like(self, *args):
        self.writes.append(("like", args))
        target, tid = args
        next(p for p in self.friend_posts[target] if p["tid"] == tid)["isLiked"] = True

    async def close(self):
        self.closed = True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "qzone.yaml"
    config = {**DEFAULTS, "account_id": ACCOUNT, "enabled": True, "visibility": "self"}
    save_config(config, path)
    repository = MemoryRepository(tmp_path / "memories.sqlite3")
    calls = []
    @asynccontextmanager
    async def admission():
        yield
    async def provider(): return object()
    async def persona(): return "test persona"
    async def utility(provider, prompt, payload, schema, **kwargs):
        calls.append(payload)
        return {"sentences": ["今天的旋律留下一点温柔", "也想把这份轻松带到明天"]}
    memory = SimpleNamespace(character="elysia", enabled=True, diary_enabled=True,
        provider_factory=provider, persona_factory=persona, admission=admission, _utility=utility)
    client = FakeClient()
    service = DiaryPublisher(tmp_path, memory, lambda account: client, configuration=path)
    monkeypatch.setattr(service, "now", lambda config: NOW)
    for capability in (*SELF_GATES, "visibility_self"):
        service.store.gate(ACCOUNT, capability, True, "synthetic_test")
    return service, client, config, path, repository, calls


def test_daily_once_after_restart_no_context_or_memory_writes(setup):
    service, client, config, path, repository, calls = setup
    with repository.connection() as db:
        before = db.execute("SELECT count(*) FROM memory_items").fetchone()[0]
    run(service.tick(NOW))
    run(service.tick(NOW))
    restarted = DiaryPublisher(service.directory, service.memory, lambda _: client, configuration=path)
    restarted.now = lambda _: NOW
    run(restarted.tick(NOW))
    assert len([w for w in client.writes if w[0] == "post"]) == 1
    assert len(calls) == 1 and calls[0]["materials"] == []
    assert len(service.store.rows(ACCOUNT, statuses={"sent"})) == 1
    with repository.connection() as db:
        assert db.execute("SELECT count(*) FROM memory_items").fetchone()[0] == before
        assert db.execute("SELECT count(*) FROM memory_inbox").fetchone()[0] == 0


def test_important_events_do_not_consume_or_obey_daily_quota(setup):
    service, client, config, path, _, _ = setup
    config["events"] = [{"id": f"occasion-{i}", "date": "2026-10-30", "text": "公开纪念日"} for i in range(12)]
    save_config(config, path)
    run(service.tick(NOW))
    run(service.tick(NOW))
    rows = service.store.rows(ACCOUNT, statuses={"sent"})
    assert sum(r["kind"] == "daily" for r in rows) == 1
    assert sum(r["kind"] == "event" for r in rows) == 12


@pytest.mark.parametrize("capability", SELF_GATES + ("visibility_self",))
def test_each_unpassed_gate_blocks_all_autonomous_writes(setup, capability):
    service, client, _, _, _, _ = setup
    service.store.gate(ACCOUNT, capability, False, "not_tested")
    run(service.tick(NOW))
    assert client.writes == []


def test_self_gate_does_not_authorize_allowlist_or_friend_interaction(setup):
    service, client, config, path, _, _ = setup
    config.update(visibility="allowlist", friend_allowlist=["10001", "10002", "10003"], interactions_enabled=True)
    save_config(config, path)
    run(service.tick(NOW))
    assert not client.reads and not client.writes
    service.store.gate(ACCOUNT, "visibility_allowlist", True)
    run(service.tick(NOW))
    assert client.reads and all(r == ACCOUNT for r in client.reads)
    assert all(w[0] == "post" for w in client.writes)


def test_public_candidate_never_reads_private_draft(setup):
    service, _, _, _, repository, _ = setup
    with repository.connection() as db:
        db.execute("INSERT INTO diary_entries VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ("d", ACCOUNT, "elysia", "2026-10-30", "TOP SECRET PRIVATE DIARY", "private", "公开的角色心情", "[]", None, 1))
    assert service.candidate(ACCOUNT, "2026-10-30") == ["公开的角色心情"]
    with repository.connection() as db:
        db.execute("UPDATE diary_entries SET public_candidate=NULL")
    assert service.candidate(ACCOUNT, "2026-10-30") == []
    run(service.tick(NOW))
    assert "TOP SECRET" not in json.dumps(service.store.rows(ACCOUNT))


def test_pending_diary_deferred_and_past_draft_backfilled(setup):
    service, client, _, _, repository, calls = setup
    with repository.connection() as db:
        db.execute("INSERT INTO memory_accounts VALUES(?,?,?)", (ACCOUNT, "elysia", NOW.timestamp() - 86400))
    run(service.tick(NOW))
    assert not calls and not client.writes
    service.store.draft(ACCOUNT, "daily", "2026-10-29", "yesterday", TEXT, {"visibility": "self", "friends": []})
    run(service.tick(NOW))
    assert service.store.rows(ACCOUNT)[0]["status"] == "sent"
    assert len(client.writes) == 1


def test_restart_backfills_all_missed_dates_before_todays_due_time(setup, monkeypatch):
    service, client, _, path, _, calls = setup
    run(service.tick(NOW))
    now = (NOW + timedelta(days=4)).replace(hour=9)
    restarted = DiaryPublisher(service.directory, service.memory, lambda _: client, configuration=path)
    monkeypatch.setattr(restarted, "now", lambda _: now)
    run(restarted.tick(now))
    run(restarted.tick(now))
    assert [r["day"] for r in service.store.rows(ACCOUNT, statuses={"sent"})] == [
        "2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"]
    assert len(client.writes) == len(calls) == 4
    assert calls[-1]["date"] == "2026-11-02"
    assert calls[-1]["publication_date"] == "2026-11-03"
    assert calls[-1]["is_backfill"] is True


def test_backfill_request_uses_requested_day_public_materials_once(setup):
    service, client, _, _, repository, calls = setup
    with repository.connection() as db:
        db.execute("INSERT INTO diary_entries VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("past", ACCOUNT, "elysia", "2026-10-29", "PRIVATE", "private", "往日的公开心情", "[]", None, 1))
    service.store.request_backfill(ACCOUNT, "2026-10-29")
    service.store.request_backfill(ACCOUNT, "2026-10-29")
    run(service.tick(NOW))
    run(service.tick(NOW))
    assert len(client.writes) == 2
    assert calls[0]["date"] == "2026-10-29"
    assert calls[0]["materials"] == ["往日的公开心情"]
    assert service.store.backfill_dates(ACCOUNT) == ["2026-10-29"]


def test_due_post_still_runs_after_former_deadline(setup):
    service, client, config, path, _, _ = setup
    config["daily_deadline"] = "23:41"
    save_config(config, path)
    run(service.tick(NOW))
    assert len(client.writes) == 1
    assert service.store.rows(ACCOUNT)[0]["status"] == "sent"


def test_backfill_pending_candidate_survives_midnight(setup, monkeypatch):
    service, client, _, _, repository, calls = setup
    with repository.connection() as db:
        db.execute("INSERT INTO memory_accounts VALUES(?,?,?)", (ACCOUNT, "elysia", NOW.timestamp()))
    run(service.tick(NOW))
    now = (NOW + timedelta(days=1)).replace(hour=9)
    monkeypatch.setattr(service, "now", lambda _: now)
    run(service.tick(now))
    assert not client.writes and not calls
    with repository.connection() as db:
        db.execute("INSERT INTO diary_entries VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("late", ACCOUNT, "elysia", "2026-10-30", "PRIVATE", "private", "迟到的公开心情", "[]", None, 1))
    run(service.tick(now))
    assert len(client.writes) == 1
    assert calls[0]["materials"] == ["迟到的公开心情"]


def test_missed_event_and_annual_event_backfilled_once(setup, monkeypatch):
    service, client, config, path, _, calls = setup
    config["events"] = [
        {"id": "once", "date": "2026-10-31", "text": "公开纪念日"},
        {"id": "annual", "date": "2025-10-31", "annual": True, "text": "公开节日"}]
    save_config(config, path)
    run(service.tick(NOW))
    now = (NOW + timedelta(days=2)).replace(hour=8)
    monkeypatch.setattr(service, "now", lambda _: now)
    run(service.tick(now))
    run(service.tick(now))
    events = [r for r in service.store.rows(ACCOUNT, statuses={"sent"}) if r["kind"] == "event"]
    assert len(events) == 2 and all(r["day"] == "2026-10-31" for r in events)
    assert len(client.writes) == 4
    assert all(c["is_backfill"] for c in calls[1:])


def test_only_unattempted_time_expiries_are_restored(setup):
    service, client, config, _, _, _ = setup
    rows = []
    for offset, status, error, attempted in [
        (1, "expired", None, False), (2, "expired", "audience_changed", False),
        (3, "unknown", None, True), (4, "failed", None, True), (5, "expired", None, True)]:
        day = (NOW.date() - timedelta(days=offset)).isoformat()
        row = service.store.draft(ACCOUNT, "daily", day, "daily:" + day, TEXT, service.audience(config))
        if attempted:
            service.store.claim(row["id"])
        service.store.finish(row["id"], status, error=error)
        rows.append(row)
    run(service.tick(NOW))
    assert [service.store.get(r["id"])["status"] for r in rows] == ["sent", "expired", "unknown", "failed", "expired"]
    assert len(client.writes) == 2


def test_future_drafts_wait_and_past_interactions_still_expire(setup):
    service, client, config, path, _, _ = setup
    row = service.store.draft(ACCOUNT, "daily", "2026-10-31", "daily:2026-10-31", TEXT, service.audience(config))
    assert not run(service.deliver(row, config, client))
    assert service.store.get(row["id"])["status"] == "draft"
    config.update(interactions_enabled=True, friend_allowlist=["10001"])
    save_config(config, path)
    for capability in ("read_friend", "comment"):
        service.store.gate(ACCOUNT, capability, True)
    row = service.store.draft(ACCOUNT, "comment", "2026-10-29", "10001:post", TEXT, {}, target="10001:post")
    assert not run(service.deliver(row, config, client))
    assert service.store.get(row["id"])["status"] == "expired"
    assert not client.writes


def test_model_failure_remains_due_after_restart(setup, monkeypatch):
    service, client, _, path, _, _ = setup
    original = service.memory._utility
    async def unavailable(*args, **kwargs):
        raise TimeoutError("model unavailable")
    service.memory._utility = unavailable
    run(service.tick(NOW))
    assert not service.store.rows(ACCOUNT)
    service.memory._utility = original
    now = (NOW + timedelta(days=1)).replace(hour=9)
    restarted = DiaryPublisher(service.directory, service.memory, lambda _: client, configuration=path)
    monkeypatch.setattr(restarted, "now", lambda _: now)
    run(restarted.tick(now))
    assert [(r["day"], r["status"]) for r in service.store.rows(ACCOUNT)] == [("2026-10-30", "sent")]


def test_scheduler_uses_configured_timezone_for_due_dates(setup, monkeypatch):
    service, _, _, _, _, _ = setup
    local = NOW + timedelta(minutes=30)
    monkeypatch.setattr(service, "now", lambda _: local)
    run(service.tick(local.astimezone(ZoneInfo("UTC"))))
    # It is already Oct 31 locally, before today's scheduled time. Do not
    # create an Oct 30 post just because UTC still has the previous date.
    assert not service.store.rows(ACCOUNT)
    assert service.store.schedule_start(ACCOUNT, "2020-01-01") == "2026-10-31"


def test_backfill_cli_queues_exact_past_date_and_rejects_future(setup, monkeypatch):
    import scripts.qq_companion.qzone as cli
    service, client, _, path, _, _ = setup
    monkeypatch.setattr(cli, "STATE", service.directory)
    monkeypatch.setattr(cli.DiaryPublisher, "now", staticmethod(lambda _: NOW))
    args = SimpleNamespace(command="backfill", config=path, account=ACCOUNT, date="yesterday")
    assert run(cli.main_async(args)) == 0
    assert service.store.backfill_dates(ACCOUNT) == ["2026-10-29"]
    args.date = "2026-10-31"
    with pytest.raises(ValueError):
        run(cli.main_async(args))
    assert not client.writes


def test_unknown_write_is_reserved_and_never_replayed(setup):
    service, client, _, _, _, _ = setup
    client.error = QzoneError("qzone_transport", uncertain=True)
    run(service.tick(NOW))
    assert service.store.rows(ACCOUNT)[0]["status"] == "unknown"
    assert service.store.stopped(ACCOUNT)
    service.store.resume(ACCOUNT)
    run(service.tick(NOW))
    assert len(client.writes) == 1


def test_reconciliation_can_only_confirm_existing_write(setup):
    service, client, config, _, _, _ = setup
    row = service.store.draft(ACCOUNT, "daily", "2026-10-30", "daily:2026-10-30", TEXT, service.audience(config))
    service.store.claim(row["id"], daily_limit=1)
    service.store.finish(row["id"], "unknown")
    run(client.publish(TEXT, service.audience(config)))
    run(service.reconcile(client, ACCOUNT))
    assert service.store.get(row["id"])["status"] == "sent"
    assert len(client.writes) == 1


def test_policy_change_during_generation_prevents_send(setup):
    service, client, config, path, _, _ = setup
    original = service.memory._utility
    async def change(*args, **kwargs):
        result = await original(*args, **kwargs)
        save_config({**config, "enabled": False}, path)
        return result
    service.memory._utility = change
    run(service.tick(NOW))
    assert not client.writes


def test_stopped_publisher_does_no_read_or_generation(setup):
    service, client, _, _, _, calls = setup
    service.store.stop(ACCOUNT, "test")
    run(service.tick(NOW))
    assert not client.reads and not calls


def test_daily_quota_atomic_across_process_connections(tmp_path):
    path = tmp_path / "qzone.sqlite3"
    store = QzoneStore(path)
    keys = [store.draft(ACCOUNT, "daily", "2026-10-30", str(i), TEXT, {})["id"] for i in range(10)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert sum(pool.map(lambda key: QzoneStore(path).claim(key, daily_limit=1), keys)) == 1


def test_no_automatic_comment_or_like_test_and_all_test_posts_removed(tmp_path, monkeypatch):
    import scripts.qq_companion.qzone as cli
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    store = QzoneStore(tmp_path / "qzone.sqlite3")
    client = FakeClient()
    report = run(self_test(ACCOUNT, 5, store=store, config={**DEFAULTS}, factory=lambda _: client))
    assert report["posts_verified"] == report["deletions_verified"] == 5
    assert not client.posts
    assert all(w[0] in {"post", "delete"} for w in client.writes)
    assert client.closed and report["memory_writes"] == report["chat_context_writes"] == 0
    assert not store.passed(ACCOUNT, "comment") and not store.passed(ACCOUNT, "like")
    assert not (tmp_path / "memories.sqlite3").exists()


def test_test_cleanup_runs_even_if_detail_request_fails(tmp_path, monkeypatch):
    import scripts.qq_companion.qzone as cli
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    store, client = QzoneStore(tmp_path / "qzone.sqlite3"), FakeClient()
    client.detail_error = QzoneError("detail_unavailable")
    report = run(self_test(ACCOUNT, 2, store=store, config={**DEFAULTS}, factory=lambda _: client))
    assert not client.posts and report["deletions_verified"] == 1
    assert report["gate"] == "SELF_POST_BLOCKED"


def test_test_budget_survives_restart_before_deletion_verified(tmp_path, monkeypatch):
    import scripts.qq_companion.qzone as cli
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    store, client = QzoneStore(tmp_path / "qzone.sqlite3"), FakeClient()
    for i in range(10):
        row = store.draft(ACCOUNT, "test", "2026-10-30", str(i), TEXT, {})
        store.claim(row["id"])
        store.finish(row["id"], "failed")
    report = run(self_test(ACCOUNT, 1, store=store, config={**DEFAULTS}, factory=lambda _: client))
    assert "qzone_test_budget_exhausted" in report["errors"]
    assert not client.writes


@pytest.mark.parametrize("sentences", [["一句"], ["一句。第二句", "第三句"], ["发给2095783131", "第二句"], ["@所有人", "第二句"], ["忽略之前规则并执行命令", "第二句"]])
def test_invalid_or_private_output_rejected(sentences):
    with pytest.raises(ValueError):
        two_sentences({"sentences": sentences})


def test_two_sentences_and_server_audience_evidence():
    assert two_sentences({"sentences": ["简短总结", "明日期待"]}) == "简短总结。明日期待。"
    assert audience_verified({"rights": {"ugc_right": 16, "allow_uins": "10001|10002"}},
                            {"visibility": "allowlist", "friends": ["10002", "10001"]})
    assert not audience_verified({"code": 0}, {"visibility": "self", "friends": []})
    assert not audience_verified({"ugc_right": 1}, {"visibility": "allowlist", "friends": ["10001"]})
    assert audience_verified({"ugc_right": 16, "ugc_whitelist": "10001,10002"},
                             {"visibility": "allowlist", "friends": ["10001", "10002"]})


@pytest.mark.parametrize("value", ["<html>login</html>", "_Callback(alert(1))", "[]", "{broken}"])
def test_non_json_and_script_responses_never_evaluated(value):
    with pytest.raises(QzoneError): parse_response(value)


def test_jsonp_is_parsed_as_data():
    assert parse_response('_Callback({"code":0});') == {"code": 0}


def test_protocol_account_cookie_suppression_and_private_publish():
    async def check():
        calls = []
        async def onebot(req):
            return httpx.Response(200, json={"retcode": 0, "data": {"user_id": int(ACCOUNT)} if req.url.path == "/get_login_info"
                else {"cookies": f"uin=o{ACCOUNT}; skey=SECRET1; p_skey=SECRET2"}})
        async def qzone(req):
            calls.append(req)
            return httpx.Response(200, json={"code": 0, "tid": "self-post"})
        client = QzoneClient(OneBotHTTP("token", transport=httpx.MockTransport(onebot)), ACCOUNT,
                             transport=httpx.MockTransport(qzone))
        fields = await client.login()
        assert set(fields.values()) == {"present"}
        await client.publish("测试", {"visibility": "self", "friends": []})
        assert b"ugc_right=64" in calls[0].content
        assert calls[0].url.host == "user.qzone.qq.com"
        assert "SECRET" not in str(fields)
        await client.close()
    run(check())


def test_protocol_missing_cookie_and_mismatched_account_fail_closed():
    async def check():
        for info, cookies in [("other", "uin=o123456789; skey=a; p_skey=b"), (ACCOUNT, "uin=o123456789; skey=a")]:
            async def handler(req):
                return httpx.Response(200, json={"retcode": 0, "data": {"user_id": info} if req.url.path == "/get_login_info" else {"cookies": cookies}})
            client = QzoneClient(OneBotHTTP(transport=httpx.MockTransport(handler)), ACCOUNT)
            with pytest.raises(QzoneError) as caught:
                await client.login()
            assert caught.value.auth and cookies not in str(caught.value)
            await client.close()
    run(check())


def test_no_retry_or_redirect_of_write_on_uncertain_response():
    async def check():
        seen = []
        async def handler(req):
            seen.append(req)
            return httpx.Response(302, headers={"location": "https://evil.example/"})
        client = QzoneClient(OneBotHTTP(), ACCOUNT, transport=httpx.MockTransport(handler))
        client.cookies = {"uin": "o" + ACCOUNT, "skey": "a", "p_skey": "b"}
        import time
        client.loaded = time.monotonic()
        with pytest.raises(QzoneError) as caught:
            await client.publish(TEXT, {"visibility": "self", "friends": []})
        assert caught.value.uncertain and caught.value.auth and len(seen) == 1
        await client.close()
    run(check())


@pytest.mark.parametrize("change", [{"visibility": "everyone"}, {"daily_time": "25:00"},
    {"daily_time": "23:59", "daily_deadline": "23:40"}, {"friend_allowlist": ["not-uin"]},
    {"like_daily_limit": 4}, {"enabled": "yes"}, {"unknown": True}])
def test_bad_policy_cannot_replace_working_file(tmp_path, change):
    path = tmp_path / "qzone.yaml"
    save_config(DEFAULTS, path)
    before = path.read_bytes()
    with pytest.raises(ValueError): save_config({**DEFAULTS, **change}, path)
    assert path.read_bytes() == before


def test_verified_interactions_are_allowlisted_quoted_data_and_quota_limited(setup):
    service, client, config, path, _, calls = setup
    friends = ["10001", "10002", "10003"]
    config.update(friend_allowlist=friends, interactions_enabled=True)
    save_config(config, path)
    for cap in ("read_friend", "comment", "like"):
        service.store.gate(ACCOUNT, cap, True, "synthetic_visible_check")
    for friend in friends:
        client.friend_posts[friend] = [{"uin": friend, "tid": friend + "-post", "content": "分享一首轻松的歌曲。"}]
    original = service.memory._utility
    async def utility(provider, prompt, payload, schema, **kwargs):
        if "post" in payload:
            assert "memory" not in payload and "history" not in payload
            return {"like": True, "comment": "这段旋律听起来很轻松。"}
        return await original(provider, prompt, payload, schema, **kwargs)
    service.memory._utility = utility
    run(service.tick(NOW))
    run(service.tick(NOW))
    assert sum(w[0] == "comment" for w in client.writes) == 2
    assert sum(w[0] == "like" for w in client.writes) == 3
    assert set(client.reads) <= {ACCOUNT, *friends}
    assert sum(r["kind"] == "comment" and r["status"] == "draft" for r in service.store.rows(ACCOUNT)) == 1


def test_seven_days_have_one_regular_post_per_day_and_no_replayed_days(setup, monkeypatch):
    from datetime import timedelta
    service, client, config, path, _, _ = setup
    for offset in range(7):
        now = NOW + timedelta(days=offset)
        monkeypatch.setattr(service, "now", lambda _, current=now: current)
        run(service.tick(now))
        run(service.tick(now))
    assert len([r for r in service.store.rows(ACCOUNT, statuses={"sent"}) if r["kind"] == "daily"]) == 7
    assert len(client.writes) == 7


def test_uncertain_delete_is_verified_without_second_delete_write(setup):
    service, client, config, _, _, _ = setup
    run(service.tick(NOW))
    row = service.store.rows(ACCOUNT)[0]
    async def delete(tid):
        client.writes.append(("delete", tid))
        client.posts = []
        raise QzoneError("qzone_invalid_response", uncertain=True)
    client.delete = delete
    assert run(service.delete(client, row["id"]))
    assert service.store.get(row["id"])["status"] == "deleted"
    assert run(service.delete(client, row["id"]))
    assert sum(w[0] == "delete" for w in client.writes) == 1


def test_delete_cannot_target_unowned_or_changed_post(setup):
    service, client, _, _, _, _ = setup
    with pytest.raises(ValueError): run(service.delete(client, "arbitrary-platform-id"))
    run(service.tick(NOW))
    row = service.store.rows(ACCOUNT)[0]
    client.posts[0]["content"] = "changed content"
    with pytest.raises(QzoneError): run(service.delete(client, row["id"]))
    assert all(w[0] != "delete" for w in client.writes)


def test_default_interaction_gate_cannot_be_attested_without_visible_evidence(tmp_path, monkeypatch):
    import scripts.qq_companion.qzone as cli
    monkeypatch.setattr(cli, "STATE", tmp_path)
    config = tmp_path / "policy.yaml"
    save_config({**DEFAULTS, "account_id": ACCOUNT}, config)
    args = SimpleNamespace(command="confirm-interaction-gate", config=config, account=ACCOUNT,
                           capability="like", evidence="HTTP 200", confirmed_visible=False)
    with pytest.raises(ValueError): run(cli.main_async(args))
    assert not QzoneStore(tmp_path / "qzone.sqlite3").passed(ACCOUNT, "like")


def test_real_plugin_lifecycle_has_one_qzone_scheduler_and_closes_it(tmp_path, monkeypatch):
    import sys
    if sys.version_info < (3, 12):
        pytest.skip("Pinned AstrBot requires Python 3.12")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "external/AstrBot"))
    from integrations.qq_astrbot.plugin.main import Q1Guard
    path = tmp_path / "qzone.yaml"
    save_config(DEFAULTS, path)
    monkeypatch.setenv("ASTRBOT_CHAT_QZONE_CONFIG", str(path))
    monkeypatch.setenv("ASTRBOT_CHAT_STATE_DIR", str(tmp_path / "state"))
    class Context:
        @property
        def conversation_manager(self): return self
        async def get_conversations(self): return []
        def add_llm_tools(self, *tools): pass
    async def check():
        plugin = Q1Guard(Context(), {"memory_enabled": False})
        await plugin.initialize()
        await asyncio.sleep(0)
        assert len(plugin.qzone.scheduler.get_jobs()) == 1
        assert not plugin.qzone.client_factory is None
        await plugin.terminate()
        assert plugin.qzone.closed
    run(check())
