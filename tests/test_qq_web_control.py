from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from integrations.qq_astrbot.plugin.control_catalog import PROMPTS
from integrations.qq_astrbot.plugin.control_store import (
    ControlStore,
    public_exchange,
    validate_messages,
)
from integrations.qq_astrbot.plugin.foreground_activity import ForegroundActivity
from integrations.qq_astrbot.plugin.memory_context import MemoryContext
from integrations.qq_astrbot.plugin.memory_repository import MemoryRepository
from integrations.qq_astrbot.plugin.prompt_loader import REQUIRED_PROMPTS, load_prompts
from integrations.qq_astrbot.plugin.web_control import WebControl
from scripts.qq_companion.model_control import ModelControl, validate_model

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Config(dict):
    def save_config(self):
        self.saved = dict(self)


class Conversations:
    def __init__(self):
        self.rows = {
            "one": NS(
                cid="one",
                user_id="snowluma:FriendMessage:qq:1:dm:2",
                history="[]",
                title="test",
            )
        }

    async def get_conversation(self, origin, key):
        return self.rows.get(key)

    async def update_conversation(self, origin, key, history, token_usage):
        self.rows[key].history = json.dumps(history)
        self.rows[key].token_usage = token_usage


@pytest.fixture
def panel(tmp_path, monkeypatch):
    path = tmp_path / "configs/qq_companion/prompts.yaml"
    path.parent.mkdir(parents=True)
    path.write_bytes((ROOT / "configs/qq_companion/prompts.yaml").read_bytes())
    model = tmp_path / "configs/runtime/qwen3.yaml"
    model.parent.mkdir(parents=True)
    model.write_bytes((ROOT / "configs/runtime/qwen3.yaml").read_bytes())
    monkeypatch.setenv("ASTRBOT_CHAT_PROMPTS_FILE", str(path))
    monkeypatch.setenv(
        "ASTRBOT_CHAT_DOUYIN_CONFIG",
        str(tmp_path / "configs/qq_companion/douyin.local.json"),
    )
    state = tmp_path / "state"
    memory = NS(
        repository=MemoryRepository(state / "memories.sqlite3"),
        character="elysia",
        scheduler=NS(reschedule_job=lambda *a, **k: None),
        zone="Asia/Hong_Kong",
    )
    manager = Conversations()
    plugin = NS(
        memory=memory,
        agent_store=NS(),
        router=NS(context=MemoryContext()),
        test_peer="",
        agent_limits={},
        context=NS(conversation_manager=manager, register_web_api=lambda *args: None),
        foreground_activity=ForegroundActivity(state),
    )
    return WebControl(plugin, state, Config())


def test_all_prompts_have_chinese_name_and_invocation():
    assert set(PROMPTS) == REQUIRED_PROMPTS
    for name, when in PROMPTS.values():
        assert any("\u4e00" <= c <= "\u9fff" for c in name)
        assert when


@pytest.mark.anyio
async def test_proactive_settings_normalize_persist_and_hot_apply(panel):
    applied = []
    panel.plugin.proactive = NS(configure=lambda c: applied.append(dict(c)))
    data = await panel.read('settings', {})
    assert len(data['followup_probabilities']) == 5 and data['followup_probabilities'][-1] == 1
    result = await panel.write('settings', {'revision':data['revision'], 'app':{
        'context_plans_enabled':False, 'context_plan_limit':5,
        'periodic_interval_minutes':10, 'periodic_jitter_minutes':20,
        'followup_average_probability':.1, 'followup_pity_limit':5}})
    assert '自动纠正' in result['message']
    assert panel.config.saved['followup_average_probability'] == .2
    assert panel.config.saved['periodic_jitter_minutes'] == 9
    assert applied[-1]['context_plans_enabled'] is False
    assert applied[-1]['periodic_proactive_enabled'] is True
    data = await panel.read('settings', {})
    assert data['followup_probabilities'] == (0,0,0,0,1)
    for bad in (float('nan'),float('inf'),True):
        with pytest.raises(ValueError):
            await panel.write('settings', {'revision':data['revision'], 'app':{'followup_average_probability':bad}})


@pytest.mark.anyio
async def test_prompt_edit_preserves_other_blocks_comments_and_variables(panel):
    path = panel.root / "configs/qq_companion/prompts.yaml"
    for key in [
        "douyin_interest",
        "qq_context",
        "model_profiles.qwen35.model_protocol",
        "datetime_reminder",
    ]:
        data = await panel.read("prompts", {})
        row = next(r for r in data["rows"] if r["key"] == key)
        await panel.write(
            "prompts",
            {
                "revision": data["revision"],
                "key": key,
                "text": row["text"].strip() + "\n测试补充规则。",
            },
        )
        after = await panel.read("prompts", {})
        assert len(after["rows"]) == len(data["rows"])
        for original in data["rows"]:
            actual = next(r for r in after["rows"] if r["key"] == original["key"])
            if original["key"] != key:
                assert actual["text"] == original["text"]
        assert path.read_text(encoding="utf-8").startswith("#")
        load_prompts(path, profile="qwen35")
    data = await panel.read("prompts", {})
    with pytest.raises(ValueError, match="占位符"):
        await panel.write(
            "prompts",
            {
                "revision": data["revision"],
                "key": "datetime_reminder",
                "text": "丢失变量",
            },
        )


@pytest.mark.anyio
async def test_memory_crud_scope_search_conflict_and_cache_invalidation(panel):
    created = await panel.write(
        "memory",
        {"values": {"summary": "喜欢音乐", "owner_key": "qq:1:dm:2", "tags": ["音乐"]}},
    )
    rows = (await panel.read("memory", {"q": "音乐"}))["rows"]
    assert len(rows) == 1
    row = rows[0]
    repo = panel.plugin.memory.repository
    assert repo.retrieve("qq:1:dm:2", "音乐")
    assert not repo.retrieve("qq:1:dm:3", "音乐")
    repo.save_compaction("old", "qq:1:dm:2", "elysia", "outdated")
    await panel.write(
        "memory",
        {
            "id": created["id"],
            "revision": row["revision"],
            "values": {"summary": "喜欢绘画", "tags": ["绘画"]},
        },
    )
    with pytest.raises(ValueError, match="内容已变化"):
        await panel.write(
            "memory",
            {
                "id": created["id"],
                "revision": row["revision"],
                "values": {"summary": "stale"},
            },
        )
    with repo.connection() as db:
        assert db.execute("SELECT count(*) FROM memory_compactions").fetchone()[0] == 0
    row = (await panel.read("memory", {"q": "绘画"}))["rows"][0]
    await panel.write(
        "memory", {"id": row["id"], "revision": row["revision"], "action": "delete"}
    )
    assert not repo.retrieve("qq:1:dm:2", "绘画")
    assert not (await panel.read("memory", {}))["rows"]


@pytest.mark.anyio
async def test_context_display_and_edit_changes_real_next_history(panel):
    manager = panel.plugin.context.conversation_manager
    raw = [
        {"role": "system", "content": "PRIVATE SYSTEM"},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "current_message": {"text": "hello"},
                    "recent_messages": ["HIDDEN HISTORY"],
                }
            ),
        },
        {"role": "assistant", "content": "response"},
    ]
    manager.rows["one"].history = json.dumps(raw)
    data = await panel.read("contexts", {"id": "one"})
    assert "PRIVATE SYSTEM" not in json.dumps(data)
    assert "HIDDEN HISTORY" not in json.dumps(data)
    assert data["messages"][0]["content"]["text"] == "hello"
    await panel.write(
        "contexts",
        {
            "id": "one",
            "revision": data["revision"],
            "index": 1,
            "message": {"role": "user", "content": "edited"},
        },
    )
    saved = json.loads(manager.rows["one"].history)
    assert json.loads(saved[1]["content"])["current_message"]["text"] == "edited"
    assert json.loads(saved[1]["content"])["recent_messages"] == []
    assert manager.rows["one"].token_usage == 0
    lease = panel.plugin.foreground_activity.begin()
    with pytest.raises(ValueError, match="正在处理"):
        await panel.write("contexts", {"id": "one", "action": "clear"})
    panel.plugin.foreground_activity.end(lease)
    data = await panel.read("contexts", {"id": "one"})
    await panel.write(
        "contexts", {"id": "one", "revision": data["revision"], "action": "clear"}
    )
    assert json.loads(manager.rows["one"].history) == []


@pytest.mark.anyio
async def test_context_tool_delete_removes_complete_round(panel):
    call = {
        "id": "call1",
        "type": "function",
        "function": {"name": "music", "arguments": "{}"},
    }
    raw = [
        {"role": "user", "content": "music"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call1", "content": "result"},
        {"role": "assistant", "content": "done"},
    ]
    manager = panel.plugin.context.conversation_manager
    manager.rows["one"].history = json.dumps(raw)
    data = await panel.read("contexts", {"id": "one"})
    await panel.write(
        "contexts",
        {"id": "one", "revision": data["revision"], "index": 2, "action": "delete"},
    )
    assert json.loads(manager.rows["one"].history) == [raw[0], raw[3]]
    with pytest.raises(ValueError):
        validate_messages(raw[:2])


def test_exchange_projection_does_not_leak_history_system_tools_or_reasoning():
    record = {
        "request_id": "x",
        "request": {
            "messages": [
                {
                    "role": "system",
                    "content": "secret persona\nVISIBLE-PROMPT-NAME-MATCH",
                },
                {"role": "user", "content": "OLD HISTORY"},
                {"role": "assistant", "content": "OLD RESPONSE"},
                {"role": "user", "content": "CURRENT"},
            ],
            "tools": [{"description": "SECRET TOOL"}],
        },
        "response": {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "reply",
                        "reasoning_content": "PRIVATE REASONING",
                        "tool_calls": [{"id": "x"}],
                    }
                }
            ]
        },
    }
    result = public_exchange(record, {"qq_context": "VISIBLE-PROMPT-NAME-MATCH"})
    encoded = json.dumps(result)
    assert result["prompt_names"] == ["qq_context"]
    assert result["input"] == [{"role": "user", "content": "CURRENT"}]
    assert result["output"][0]["tool_calls"]
    assert all(
        text not in encoded
        for text in [
            "OLD HISTORY",
            "OLD RESPONSE",
            "secret persona",
            "SECRET TOOL",
            "PRIVATE REASONING",
            "VISIBLE-PROMPT-NAME-MATCH",
        ]
    )


def test_usage_exact_idempotent_including_unreported(tmp_path):
    store = ControlStore(tmp_path)
    one = {
        "request_id": "one",
        "started_at": "2026-11-02T01:00:00+00:00",
        "phase": "agent",
        "status": "received",
        "response": {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 25,
                "prompt_tokens_details": {"cached_tokens": 40},
            }
        },
    }
    store.record_usage(one)
    store.record_usage(one)
    store.record_usage(
        {
            **one,
            "request_id": "two",
            "phase": "douyin",
            "status": "canceled",
            "response": None,
        }
    )
    result = store.usage()
    assert sum(r["calls"] for r in result["phases"]) == 2
    assert sum(r["input"] for r in result["phases"]) == 100
    assert result["unreported"] == 1


def test_model_restart_waits_then_rolls_back_on_failure(panel):
    directory = panel.directory
    control = ModelControl(panel.root, directory)
    path = panel.root / "configs/runtime/qwen3.yaml"
    before = path.read_bytes()
    key = control.submit({"temperature": 0.45})
    with pytest.raises(ValueError, match="已有"):
        control.submit({"temperature": 0.4})
    mode, activity, stopped, restores = ["ready"], [1], [], []
    media = NS(
        mode=lambda: mode[0],
        set_mode=lambda v: mode.__setitem__(0, v),
        active=lambda: activity[0],
    )

    def restore(names):
        restores.append(names)
        if len(restores) == 1:
            raise RuntimeError("failed new configuration")

    switch = NS(
        stop=lambda names: (stopped.append(True), names.append("qwen3")),
        restore=restore,
        children=[],
        write_registry=lambda: None,
    )
    control.step(switch, media, panel.plugin.foreground_activity)
    assert mode[0] == "draining" and not stopped and path.read_bytes() == before
    activity[0] = 0
    control.step(switch, media, panel.plugin.foreground_activity)
    assert mode[0] == "ready" and path.read_bytes() == before
    with control.store.connection() as db:
        assert (
            db.execute("SELECT state FROM model_jobs WHERE id=?", (key,)).fetchone()[0]
            == "rolled_back"
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"host": "0.0.0.0"},
        {"temperature": float("nan")},
        {"max_tokens": -1},
        {"context_size": 4096, "max_tokens": 4096},
        {"enable_thinking": "false"},
        {"cache_type_k": "invalid"},
    ],
)
def test_bad_model_settings_are_rejected(panel, patch):
    with pytest.raises(ValueError):
        validate_model(panel.root, patch)


@pytest.mark.anyio
async def test_settings_persist_and_change_running_objects(panel):
    current = await panel.read("settings", {})
    percent = next(field for field in current["douyin"] if field["key"] == "long_video_percent")
    assert (percent["value"], percent["min"], percent["max"]) == (40, 0, 100)
    await panel.write(
        "settings",
        {
            "revision": current["revision"],
            "app": {
                "diary_time": "21:15",
                "followup_limit": 3,
                "auto_probability": 0.4,
            },
            "douyin": {"hourly_videos": 30, "start_hour": 11, "long_video_percent": 50},
        },
    )
    assert (
        panel.config.saved["followup_limit"]
        == panel.plugin.agent_store.max_followups
        == 3
    )
    assert (
        panel.plugin.memory.diary_hour == 21 and panel.plugin.memory.diary_minute == 15
    )
    assert panel.plugin.router.auto_probability == 0.4
    from services.douyin_worker.config import ConfigStore

    assert ConfigStore().read().hourly_videos == 30
    assert ConfigStore().read().long_video_percent == 50
    assert ConfigStore().read().long_video_limit == 15


@pytest.fixture
def whitelist_panel(panel):
    from astrbot.core.pipeline.whitelist_check.stage import WhitelistCheckStage
    configs = {}
    for key in ("default", "second"):
        configs[key] = Config(platform_settings={"enable_id_white_list": True, "id_whitelist": [],
            "id_whitelist_log": False, "wl_ignore_admin_on_group": False, "wl_ignore_admin_on_friend": False},
            platform=[{"id": "snowluma", "type": "aiocqhttp"}])
    manager = NS(confs=configs, default_conf=configs["default"],
                 get_conf_list=lambda: [{"id": k, "name": k} for k in configs])
    core = NS(astrbot_config_mgr=manager, stages={})
    async def reload(profile):
        stage = WhitelistCheckStage()
        await stage.initialize(NS(astrbot_config=configs[profile]))
        core.stages[profile] = stage
    core.reload_pipeline_scheduler = reload
    panel.core = core
    return panel


@pytest.mark.anyio
async def test_whitelist_crud_persists_and_native_gate_uses_new_values(whitelist_panel):
    panel = whitelist_panel
    async def write(action, **values):
        current = await panel.read("whitelist", {})
        return await panel.write("whitelist", {"revision": current["revision"], "action": action, **values})
    current = (await panel.read("settings", {}))["whitelist"]
    assert current["available"] and current["platforms"][0]["id"] == "snowluma"
    friend, group = "snowluma:FriendMessage:12345", "snowluma:GroupMessage:23456"
    await write("create", value=friend)
    await write("create", value=group)
    native = panel.core.astrbot_config_mgr.default_conf
    assert native.saved["platform_settings"]["id_whitelist"] == [friend, group]
    def event(origin, group_id=""):
        value = NS(role="member", unified_msg_origin=origin, stopped=False,
                   get_platform_name=lambda: "aiocqhttp", get_group_id=lambda: group_id)
        value.stop_event = lambda: setattr(value, "stopped", True)
        return value
    accepted, blocked = event(friend), event("snowluma:FriendMessage:99999")
    await panel.core.stages["default"].process(accepted)
    await panel.core.stages["default"].process(blocked)
    assert not accepted.stopped and blocked.stopped
    await write("update", id=friend, value="snowluma:FriendMessage:34567")
    former = event(friend)
    await panel.core.stages["default"].process(former)
    assert former.stopped
    await write("delete", id=group)
    assert [r["id"] for r in (await panel.read("whitelist", {}))["rows"]] == ["snowluma:FriendMessage:34567"]
    await write("configure", enabled=False)
    unrestricted = event(friend)
    await panel.core.stages["default"].process(unrestricted)
    assert not unrestricted.stopped
    assert panel.core.astrbot_config_mgr.confs["second"]["platform_settings"]["id_whitelist"] == []


@pytest.mark.anyio
async def test_whitelist_rejects_invalid_duplicate_stale_and_wrong_profile(whitelist_panel):
    panel = whitelist_panel
    current = await panel.read("whitelist", {})
    base = {"revision": current["revision"], "action": "create"}
    for value in ("", "  ", 12345, "friend:12345", "snowluma:FriendMessage:bad value", "123\n456"):
        with pytest.raises(ValueError):
            await panel.write("whitelist", {**base, "value": value})
    await panel.write("whitelist", {**base, "value": "12345"})
    with pytest.raises(ValueError, match="已变化"):
        await panel.write("whitelist", {**base, "value": "23456"})
    current = await panel.read("whitelist", {})
    with pytest.raises(ValueError, match="已在"):
        await panel.write("whitelist", {"revision": current["revision"], "action": "create", "value": "12345"})
    with pytest.raises(ValueError, match="不存在"):
        await panel.read("whitelist", {"profile": "missing"})


@pytest.mark.anyio
async def test_whitelist_reload_failure_restores_persisted_and_running_policy(whitelist_panel):
    panel = whitelist_panel
    real_reload = panel.core.reload_pipeline_scheduler
    async def fail_once(profile):
        if panel.core.astrbot_config_mgr.confs[profile]["platform_settings"]["id_whitelist"]:
            raise RuntimeError("reload failed")
        await real_reload(profile)
    panel.core.reload_pipeline_scheduler = fail_once
    current = await panel.read("whitelist", {})
    with pytest.raises(RuntimeError, match="reload failed"):
        await panel.write("whitelist", {"revision": current["revision"], "action": "create", "value": "12345"})
    config = panel.core.astrbot_config_mgr.default_conf
    assert config["platform_settings"]["id_whitelist"] == config.saved["platform_settings"]["id_whitelist"] == []
    assert panel.core.stages["default"].whitelist == []


@pytest.mark.anyio
async def test_douyin_whitelist_crud_hot_reads_and_share_policy(panel):
    from services.douyin_worker.config import ConfigStore
    from services.douyin_worker.store import Store

    ds = ConfigStore()
    ds.update({"enabled": True, "share_enabled": True, "start_hour": 0, "end_hour": 24,
               "account": {"douyin_id": "self123", "name": "分享账号"}})
    original = ds.read().model_dump()
    settings = await panel.read("settings", {})
    assert settings["douyin_whitelist"]["rows"] == []
    async def write(action, **values):
        data = await panel.read("douyin-whitelist", {})
        return await panel.write("douyin-whitelist", {"revision": data["revision"], "action": action, **values})
    await write("create", values={"douyin_id": " friend123 ", "name": " 好友 ", "opted_in": True})
    assert ConfigStore().read().friends[0].name == "好友"
    await write("update", id="friend123", values={"douyin_id": "friend456", "name": "修改昵称"})
    assert ds.read().friends[0].douyin_id == "friend456"
    store = Store(panel.directory, clock=lambda: 1793584800)
    video = {"id": "7000000000000000001", "url": "https://www.douyin.com/video/7000000000000000001"}
    assert store.reserve_share(ds.read(), "friend456", video)[1] == "reserved"
    await write("update", id="friend456", values={"opted_in": False})
    assert store.reserve_share(ConfigStore().read(), "friend456", video)[1] == "disabled_or_not_allowed"
    await write("delete", id="friend456")
    assert ConfigStore().read().friends == []
    assert ds.read().model_dump() == original
    # An application-settings form read before the friend edits preserves the new list.
    await write("create", values={"douyin_id": "friend789", "name": "保留好友"})
    await panel.write("settings", {"revision": settings["revision"], "douyin": {"hourly_videos": 21}})
    assert ds.read().friends[0].douyin_id == "friend789"
    assert ds.read().hourly_videos == 21


@pytest.mark.anyio
async def test_douyin_whitelist_invalid_duplicate_and_stale_are_atomic(panel):
    from services.douyin_worker.config import ConfigStore

    current = await panel.read("douyin-whitelist", {})
    base = {"revision": current["revision"], "action": "create"}
    for values in ({"douyin_id": "", "name": "x"}, {"douyin_id": "http://bad", "name": "x"},
                   {"douyin_id": "valid", "name": "   "}, {"douyin_id": "valid", "name": "x", "opted_in": "true"},
                   {"douyin_id": "valid", "name": "x", "unexpected": True}):
        with pytest.raises(ValueError):
            await panel.write("douyin-whitelist", {**base, "values": values})
        assert ConfigStore().read().friends == []
    await panel.write("douyin-whitelist", {**base, "values": {"douyin_id": "friend123", "name": "好友"}})
    with pytest.raises(ValueError, match="已变化"):
        await panel.write("douyin-whitelist", {**base, "values": {"douyin_id": "friend456", "name": "好友二"}})
    current = await panel.read("douyin-whitelist", {})
    with pytest.raises(ValueError, match="已在"):
        await panel.write("douyin-whitelist", {"revision": current["revision"], "action": "create",
                          "id": "friend123", "values": {"douyin_id": "friend123", "name": "重复"}})
    with pytest.raises(ValueError, match="不存在"):
        await panel.write("douyin-whitelist", {"revision": current["revision"], "action": "delete", "id": "missing"})
    assert len(ConfigStore().read().friends) == 1


@pytest.mark.anyio
async def test_douyin_whitelist_limits_and_self_account(panel):
    from services.douyin_worker.config import ConfigStore

    ds = ConfigStore()
    ds.update({"account": {"douyin_id": "self123", "name": "账号"}})
    current = await panel.read("douyin-whitelist", {})
    with pytest.raises(ValueError, match="当前分享账号"):
        await panel.write("douyin-whitelist", {"revision": current["revision"], "action": "create",
                          "values": {"douyin_id": "self123", "name": "账号"}})
    ds.update({"friends": [{"douyin_id": f"friend{i}", "name": f"好友{i}"} for i in range(20)]})
    current = await panel.read("douyin-whitelist", {})
    with pytest.raises(ValueError, match="最多 20"):
        await panel.write("douyin-whitelist", {"revision": current["revision"], "action": "create",
                          "values": {"douyin_id": "extra", "name": "超额好友"}})
    assert len(ds.read().friends) == 20


@pytest.mark.anyio
async def test_douyin_whitelist_revision_tracks_account_identity(panel):
    from services.douyin_worker.config import ConfigStore

    current = await panel.read("douyin-whitelist", {})
    ConfigStore().update({"account": {"douyin_id": "newaccount", "name": "新账号"}})
    with pytest.raises(ValueError, match="分享账号已变化"):
        await panel.write("douyin-whitelist", {"revision": current["revision"], "action": "create",
                          "values": {"douyin_id": "friend123", "name": "好友"}})


def test_persona_startup_preserves_webui_edits(tmp_path):
    import sqlite3
    from scripts.qq_companion.configure_model import sync_persona

    db_path = tmp_path / "astrbot.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE personas(persona_id TEXT PRIMARY KEY,system_prompt TEXT,begin_dialogs TEXT,tools TEXT,skills TEXT,sort_order INTEGER,created_at TEXT,updated_at TEXT)"
        )
    sync_persona(db_path, "elysia", "initial", model_config=NS(few_shot_examples=0))
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE personas SET system_prompt='operator changed'")
    sync_persona(db_path, "elysia", "initial", model_config=NS(few_shot_examples=0))
    with sqlite3.connect(db_path) as db:
        assert (
            db.execute("SELECT system_prompt FROM personas").fetchone()[0]
            == "operator changed"
        )


@pytest.mark.anyio
async def test_diary_edits_revoke_public_projection_and_delete(panel):
    repo = panel.plugin.memory.repository
    repo.save_diary(
        "1", "elysia", "2026-11-02", "private original", "public original", []
    )
    row = (await panel.read("memory", {"kind": "diary"}))["rows"][0]
    assert repo.retrieve("qq:1:dm:2", "public original")
    await panel.write(
        "memory",
        {
            "kind": "diary",
            "id": row["id"],
            "revision": row["revision"],
            "values": {"draft": "private corrected"},
        },
    )
    assert not repo.retrieve("qq:1:dm:2", "public original")
    row = (await panel.read("memory", {"kind": "diary"}))["rows"][0]
    assert row["draft"] == "private corrected" and row["public_candidate"] is None
    await panel.write(
        "memory",
        {
            "kind": "diary",
            "id": row["id"],
            "revision": row["revision"],
            "action": "delete",
        },
    )
    assert not (await panel.read("memory", {"kind": "diary"}))["rows"]


def test_model_recovery_uses_exact_interrupted_job_backup(panel):
    control = ModelControl(panel.root, panel.directory)
    path = panel.root / "configs/runtime/qwen3.yaml"
    before = path.read_text(encoding="utf-8-sig")
    key = control.submit({"temperature": 0.2})
    with control.store.connection() as db:
        db.execute("UPDATE model_jobs SET state='restarting' WHERE id=?", (key,))
    path.write_text("invalid interrupted configuration", encoding="utf-8")
    control.recover()
    assert path.read_text(encoding="utf-8-sig") == before


def test_repeated_model_saves_accept_windows_line_endings(panel):
    control = ModelControl(panel.root, panel.directory)
    path = panel.root / "configs/runtime/qwen3.yaml"
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    mode = ["ready"]
    media = NS(mode=lambda: mode[0], set_mode=lambda v: mode.__setitem__(0, v), active=lambda: 0)
    switch = NS(stop=lambda names: names.append("qwen3"), restore=lambda names: None)
    for value in [.65, .7]:
        key = control.submit({"temperature": value})
        control.step(switch, media, panel.plugin.foreground_activity)
        with control.store.connection() as db:
            assert db.execute("SELECT state FROM model_jobs WHERE id=?", (key,)).fetchone()[0] == "completed"


def test_applied_model_settings_reach_request_and_budget(panel, monkeypatch):
    from integrations.qq_astrbot.plugin.request_provider import request_config
    from integrations.qq_astrbot.plugin.context_budget import RequestBudget

    monkeypatch.setenv("ASTRBOT_CHAT_STATE_DIR", str(panel.directory))
    (panel.directory / "model_settings.applied.json").write_text("{}")
    from scripts.qq_companion.model_control import write_model

    write_model(
        panel.root / "configs/runtime/qwen3.yaml",
        {"temperature": 0.3, "context_size": 16384, "enable_thinking": False},
    )
    provider = NS(
        provider_config={
            "id": "astrbot_chat_qwen3",
            "custom_extra_body": {"temperature": 0.9},
        }
    )
    actual = request_config(provider, "agent")
    assert actual["custom_extra_body"]["temperature"] == 0.3
    assert actual["custom_extra_body"]["reasoning_budget_tokens"] == 0
    assert RequestBudget(provider).window == 16384
    assert provider.provider_config["custom_extra_body"]["temperature"] == 0.9
