from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from integrations.qq_astrbot.plugin.guard import InboundGuard
from integrations.qq_astrbot.plugin.memory_context import ContextMessage, MemoryContext, session_key
from integrations.qq_astrbot.plugin.message_router import Incoming, MessageRouter
from integrations.qq_astrbot.plugin.permissions import FORBIDDEN, PermissionPolicy
from scripts.qq_companion.configure import PLUGIN_FILES, ROOT


def immediate(coro):
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("Unexpected I/O in a pure policy test")


def message(sender="101", group="201", text="你好", **kwargs):
    return Incoming("999", sender, kwargs.pop("message_id", "m1"), text, group, "同名群友", **kwargs)


def test_exact_numeric_identity_boundaries():
    assert session_key("999", "101") == "qq:999:dm:101"
    assert session_key("999", "101", "201") == "qq:999:group:201"
    assert session_key("998", "101", "201") != session_key("999", "101", "201")
    with pytest.raises(ValueError):
        session_key("999", "101:group:201")


def test_two_users_ten_interleaved_messages_and_three_groups(tmp_path):
    router = MessageRouter(tmp_path / "routing.db", context=MemoryContext(token_budget=8192))
    try:
        for index in range(10):
            sender = str(101 + index % 2)
            router.route(message(sender, "", f"private-secret-{sender}", message_id=f"dm{index}"), now=100 + index)
            for group in ("201", "202", "203"):
                router.route(message(sender, group, f"only-{group}-{index}", message_id=f"g{group}-{index}"), now=100 + index)
        for group in ("201", "202", "203"):
            rows = json.loads(router.context.render(f"qq:999:group:{group}", now=110))
            assert len(rows) == 10
            assert [r["sender_uin"] for r in rows] == [str(101 + i % 2) for i in range(10)]
            assert all(r["display_name"] == "同名群友" for r in rows)
            rendered = json.dumps(rows)
            assert "private-secret" not in rendered
            assert all(f"only-{other}" not in rendered for other in ("201", "202", "203") if other != group)
        assert "private-secret-102" not in router.context.render("qq:999:dm:101", now=110)
        assert router.context.render("qq:998:group:201", now=110) == "[]"
    finally:
        router.close()


def test_group_context_metadata_ttl_size_and_token_budget():
    memory = MemoryContext(max_messages=20, token_budget=1024)
    for i in range(25):
        memory.add("qq:999:group:201", ContextMessage(str(100 + i), "昵称", "中" * 2000,
                                                    [str(i)], i == 24, "quoted" if i == 24 else None, i))
    text = memory.render("qq:999:group:201", now=25)
    assert len(text.encode()) <= 1024
    rows = json.loads(text)
    assert rows[-1]["sender_uin"] == "124"
    assert rows[-1]["at_bot"] and rows[-1]["reply_to"] == "quoted"
    assert len(memory._windows["qq:999:group:201"]) == 20
    assert memory.render("qq:999:group:201", now=324) == "[]"


def test_context_lru_and_owner_filtered_future_memory():
    memory = MemoryContext(max_sessions=2)
    for i in range(3):
        memory.add(str(i), ContextMessage("101", "", "x", [str(i)], False, None, 100))
    assert list(memory._windows) == ["1", "2"]
    rows = [{"owner_key": "qq:999:dm:101"}, {"owner_key": "qq:999:group:201"}, {},
            {"owner_key": "qq:999:group:201", "deleted_at": "today"}]
    assert memory.filter_memories("qq:999:group:201", rows) == [rows[1]]


@pytest.mark.parametrize("kwargs,expected", [
    ({"at_bot": True, "reply_to": "r", "reply_sender": "999"}, "at"),
    ({"reply_to": "r", "reply_sender": "999"}, "reply"),
    ({"reply_to": "r", "reply_sender": "102"}, "wake"),
    ({}, "wake"),
])
def test_trigger_priority(tmp_path, kwargs, expected):
    router = MessageRouter(tmp_path / "routing.db", auto_probability=1)
    try:
        assert router.route(message(text="elysia 你好", **kwargs), now=100).trigger == expected
    finally:
        router.close()


def test_auto_disabled_default_and_other_person_quote_is_passive(tmp_path):
    router = MessageRouter(tmp_path / "routing.db")
    try:
        assert not router.route(message(reply_to="r", reply_sender="102"), now=100).should_reply
        assert not router.route(message(text="unrelated_elysia_name"), now=102).should_reply
        assert router.route(message(group=""), now=104).trigger == "dm"
        assert not router.route(message(sender="999"), now=106).should_reply
        assert not router.route(message(post_type="notice"), now=108).should_reply
        assert not router.route(message(at_bot=True, member_count=2001), now=110).should_reply
    finally:
        router.close()


def test_auto_probability_cooldown_hourly_cap_restart_and_group_independence(tmp_path):
    path = tmp_path / "routing.db"
    router = MessageRouter(path, auto_probability=0.5)
    msg = message(member_count=100)
    assert router.route(msg, now=100, roll=0.9).trigger == "passive"
    assert router.route(msg, now=100, roll=0.1).trigger == "auto"
    assert router.route(msg, now=159, roll=0.1).trigger == "passive"
    assert router.route(msg, now=160, roll=0.1).trigger == "auto"
    router.close()
    router = MessageRouter(path, auto_probability=1)
    try:
        assert router.route(msg, now=220).trigger == "auto"
        assert router.route(msg, now=280).trigger == "passive"
        assert router.route(replace(msg, group_id="202"), now=280).trigger == "auto"
        assert router.route(replace(msg, at_bot=True), now=281).trigger == "at"
        assert router.route(msg, now=3700).trigger == "auto"
        assert router.route(replace(msg, group_id="203", member_count=None), now=3700).trigger == "passive"
    finally:
        router.close()


def test_fragment_merge_retains_wake_and_sender_and_at_is_immediate(tmp_path):
    router = MessageRouter(tmp_path / "routing.db")
    try:
        first = message(text="elysia", message_id="1")
        decision = router.route(first, now=100)
        second = replace(first, text="你好", message_id="2")
        newer = router.route(second, now=101)
        assert newer.trigger == "wake"
        assert not router.is_latest(first, decision)
        assert router.is_latest(second, newer)
        rows = json.loads(router.context.render(first.scope, now=101))
        assert len(rows) == 1 and rows[0]["text"] == "elysia\n你好"
        assert rows[0]["message_ids"] == ["1", "2"]
        router.route(replace(first, sender_uin="102", message_id="3"), now=101.1)
        assert len(json.loads(router.context.render(first.scope, now=102))) == 2
        assert router.route(replace(first, at_bot=True, message_id="4"), now=102).trigger == "at"
    finally:
        router.close()


def test_group_admin_controls_are_local_persistent_and_support_midnight(tmp_path):
    path = tmp_path / "routing.db"
    router = MessageRouter(path)
    admin = message(text="/q3 off", is_group_admin=True)
    assert "仅限" in router.control(replace(admin, is_group_admin=False))
    assert "已更新" in router.control(admin)
    assert router.generation(admin.scope) == 1
    assert not router.route(replace(admin, text="hi", at_bot=True)).should_reply
    assert router.route(replace(admin, group_id="202", text="hi", at_bot=True)).should_reply
    router.control(replace(admin, text="/q3 on"))
    assert router.generation(admin.scope) == 2
    router.control(replace(admin, text="/q3 quiet 23:00 07:00"))
    router.close()
    router = MessageRouter(path)
    try:
        for hour in (0, 6, 23):
            assert not router.is_active(admin.scope, local_now=datetime(2026, 10, 28, hour, 0))
        for hour in (7, 12, 22):
            assert router.is_active(admin.scope, local_now=datetime(2026, 10, 28, hour, 0))
        assert "格式" in router.control(replace(admin, text="/q3 quiet 24:00 07:00"))
        router.control(replace(admin, text="/q3 quiet off"))
        assert router.is_active(admin.scope)
    finally:
        router.close()


@pytest.mark.parametrize("action", sorted(FORBIDDEN) + ["unknown", "PUBLISH_QZONE", "SEND_DM_ALLOWLIST"])
def test_default_permission_denies_unsafe_actions_even_for_admin(tmp_path, action):
    policy = PermissionPolicy(tmp_path / "policy.db")
    try:
        assert not policy.allowed(action, actor="admin", recipient="101", public_candidate=True)
        assert policy.allowed("READ_PUBLIC")
    finally:
        policy.close()


def test_dm_quota_atomic_across_groups_connections_retries_and_restarts(tmp_path):
    path = tmp_path / "policy.db"
    first = PermissionPolicy(path, dm_allowlist=("101",))
    second = PermissionPolicy(path, dm_allowlist=("101",))
    try:
        assert not first.reserve_dm("qq:999:group:201", "102", "bad", day="2026-10-28")
        assert first.reserve_dm("qq:999:group:201", "101", "one", day="2026-10-28")
        assert not second.reserve_dm("qq:999:group:202", "101", "two", day="2026-10-28")
        assert not second.reserve_dm("qq:999:dm:101", "101", "one", day="2026-10-29")
        assert second.reserve_dm("qq:999:dm:101", "101", "three", day="2026-10-29")
    finally:
        first.close(); second.close()
    restarted = PermissionPolicy(path, dm_allowlist=("101",))
    try:
        assert not restarted.reserve_dm("qq:999:dm:101", "101", "four", day="2026-10-29")
    finally:
        restarted.close()


def test_qzone_requires_gate_author_and_public_candidate(tmp_path):
    policy = PermissionPolicy(tmp_path / "policy.db", qzone_gate_passed=True, qzone_authors=("101",))
    try:
        assert not policy.allowed("PUBLISH_QZONE", actor="101")
        assert not policy.allowed("PUBLISH_QZONE", actor="102", public_candidate=True)
        assert policy.allowed("PUBLISH_QZONE", actor="101", public_candidate=True)
    finally:
        policy.close()


def test_tools_are_filtered_copied_and_rechecked_before_execution(tmp_path):
    policy = PermissionPolicy(tmp_path / "policy.db")
    executed = []

    async def echo(**kwargs):
        executed.append(kwargs)
        return "ok"

    tools = SimpleNamespace(tools=[SimpleNamespace(name="q2_echo", handler=echo),
                                   SimpleNamespace(name="astrbot_execute_shell", handler=echo),
                                   SimpleNamespace(name="send_message_to_user", handler=echo)])
    active = [True]
    try:
        safe = policy.restrict_tools(tools, lambda: active[0], handlers={"q2_echo": echo})
        assert len(tools.tools) == 3 and len(safe.tools) == 1
        assert immediate(safe.tools[0].handler(value="test")) == "ok"
        active[0] = False
        with pytest.raises(PermissionError):
            immediate(safe.tools[0].handler(value="test"))
        assert len(executed) == 1
    finally:
        policy.close()


def test_dedup_and_self_events_remain_persistent(tmp_path):
    guard = InboundGuard(tmp_path / "inbound.db")
    assert guard.should_process("999", "m", "101", now=100)
    assert not guard.should_process("999", "m", "101", now=101)
    assert not guard.should_process("999", "self", "999", now=102)
    guard.close()


def test_installer_includes_all_runtime_modules_and_valid_schema():
    for name in PLUGIN_FILES:
        assert (ROOT / "integrations/qq_astrbot/plugin" / name).is_file()
    schema = json.loads((ROOT / "integrations/qq_astrbot/plugin/_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["auto_probability"]["default"] == 0
    assert schema["merge_seconds"]["default"] == 1.5
