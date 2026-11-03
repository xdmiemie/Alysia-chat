"""Authenticated AstrBot plugin control panel backed by production stores."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
import time
import uuid

import yaml

from .control_catalog import prompt_entries
from .control_store import (
    ControlStore,
    current_content,
    public_exchange,
    revision,
    validate_messages,
)
from .memory_repository import owner_bot
from .prompt_loader import load_prompts, prompt_path
from .proactive_store import normalize_settings, probability_curve


APP_FIELDS = {
    "diary_enabled": ("生成私人日记", bool, None, None),
    "diary_time": ("私人日记生成时间", str, None, None),
    "memory_enabled": ("自动记忆", bool, None, None),
    "followup_limit": ("每次私聊追加回复上限", int, 0, 5),
    "followup_average_probability": ("平均主动回复概率（0–1）", float, 0, 1),
    "followup_pity_limit": ("主动回复保底次数", int, 1, 100),
    "context_plans_enabled": ("启用聊天内容计划（仅私聊）", bool, None, None),
    "context_plan_limit": ("每个私聊待执行计划上限", int, 1, 100),
    "periodic_proactive_enabled": ("启用固定轮次主动回复（仅私聊）", bool, None, None),
    "periodic_interval_minutes": ("固定轮次间隔（分钟）", float, 1, 10080),
    "periodic_jitter_minutes": ("前后随机波动（分钟）", float, 0, 10080),
    "auto_probability": ("群自动接话概率", float, 0, 1),
    "auto_hourly_limit": ("每群每小时主动接话上限", int, 0, 3),
    "auto_cooldown_seconds": ("群主动接话冷却秒", float, 60, 86400),
    "merge_seconds": ("连续消息合并秒", float, 0, 5),
    "context_messages": ("群上下文消息上限", int, 1, 100),
    "context_ttl_seconds": ("群上下文保留秒", int, 1, 3600),
    "context_token_budget": ("群暂存内容字节预算", int, 512, 16384),
    "agent_max_requests": ("单次任务模型请求上限", int, 4, 64),
    "agent_max_tools": ("单次任务工具调用上限", int, 1, 64),
    "agent_max_seconds": ("单次任务执行秒数", float, 10, 600),
}
DOUYIN_FIELDS = {
    "enabled": "启用刷抖音",
    "share_enabled": "启用好友分享",
    "draft_only": "分享先存草稿",
    "hourly_videos": "每小时视频数",
    "long_video_percent": "长视频额度占比（%）",
    "start_hour": "后台活动开始小时",
    "end_hour": "后台活动结束小时（24 为零点）",
    "idle_resume_seconds": "QQ 空闲后恢复秒数",
    "daily_share_limit": "每位好友每日分享上限",
    "share_cooldown_seconds": "分享冷却秒",
}


def fields_json(fields, values):
    return [
        {
            "key": key,
            "name": spec[0],
            "type": spec[1].__name__,
            "min": spec[2],
            "max": spec[3],
            "value": values.get(key),
        }
        for key, spec in fields.items()
    ]


class WebControl:
    def __init__(self, plugin, directory, config, *, core=None):
        self.plugin, self.directory, self.config = plugin, Path(directory), config
        self.core = core
        self.root = prompt_path().parents[2]
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        self.store = ControlStore(self.directory)
        self.lock = asyncio.Lock()
        self.exchange_signature = None
        self.exchange_rows = []
        self.context_gate = asyncio.Event()
        self.context_gate.set()
        self.config.setdefault("followup_limit", 1)
        self.apply_app()
        for endpoint in (
            "overview",
            "videos",
            "memory",
            "contexts",
            "exchanges",
            "model",
            "persona",
            "usage",
            "settings",
            "whitelist",
            "douyin-whitelist",
            "prompts",
        ):

            async def handler(_endpoint=endpoint):
                from astrbot.api.web import request

                if not request.username:
                    return {"status": "error", "message": "需要 WebUI 管理员登录"}, 401
                try:
                    # The pinned plugin request wraps the authenticated ASGI
                    # request, whose app owns the running native pipelines.
                    self.core = request._request.app.state.core_lifecycle
                    if request.method == "POST":
                        if len(await request.body()) > 2_000_000:
                            raise ValueError("请求超过 2 MB")
                        body = await request.json()
                        if not isinstance(body, dict):
                            raise ValueError("请求必须是 JSON 对象")
                        async with self.lock:
                            result = await self.write(_endpoint, body)
                    else:
                        result = await self.read(_endpoint, dict(request.query.items()))
                    return {"status": "ok", "data": result}
                except (ValueError, KeyError, TypeError) as exc:
                    return {"status": "error", "message": str(exc)}, 400
                except Exception:
                    self.plugin.logger.exception(
                        "Control panel request failed: %s", _endpoint
                    )
                    return {
                        "status": "error",
                        "message": "操作失败，请查看 AstrBot 日志；修改未确认生效",
                    }, 500

            plugin.context.register_web_api(
                f"/astrbot_chat_q1/control/{endpoint}",
                handler,
                ["GET", "POST"],
                "大肥鱼控制台",
            )

    def apply_app(self):
        p, c = self.plugin, self.config
        c.update(normalize_settings(c))
        if proactive := getattr(p, "proactive", None):
            proactive.configure(c)
        p.followup_limit = c.get("followup_limit", 1)
        p.agent_store.max_followups = p.followup_limit
        for field in ("auto_probability", "auto_hourly_limit"):
            if field in c:
                setattr(p.router, field, c[field])
        if "auto_cooldown_seconds" in c:
            p.router.auto_cooldown = c["auto_cooldown_seconds"]
        for key, attr in {
            "merge_seconds": "merge_seconds",
            "context_messages": "max_messages",
            "context_ttl_seconds": "ttl",
            "context_token_budget": "token_budget",
        }.items():
            if key in c:
                setattr(p.router.context, attr, c[key])
        for key in ("max_requests", "max_tools", "max_seconds"):
            if "agent_" + key in c:
                p.agent_limits[key] = c["agent_" + key]
        p.memory.enabled = not p.test_peer and c.get("memory_enabled", True)
        p.memory.diary_enabled = not p.test_peer and c.get("diary_enabled", True)
        p.memory.diary_hour, p.memory.diary_minute = map(
            int, (p.memory.shared.config["finalize_time"] if hasattr(p.memory, "shared") else c.get("diary_time", "00:05")).split(":")
        )
        if p.memory.scheduler:
            p.memory.scheduler.reschedule_job(
                "q5-diary",
                trigger="cron",
                hour=p.memory.diary_hour,
                minute=p.memory.diary_minute,
                timezone=p.memory.zone,
            )

    def check_revision(self, body, value):
        if body.get("revision") != revision(value):
            raise ValueError("内容已变化，请刷新后再保存，避免覆盖新数据")

    def check_idle(self):
        if self.plugin.foreground_activity.blocked():
            raise ValueError("QQ 正在处理消息，请完成后再编辑上下文")

    def read_whitelist(self, profile_id="default"):
        if self.core is None:
            return {"available": False, "rows": [], "profiles": []}
        manager = self.core.astrbot_config_mgr
        if profile_id not in manager.confs:
            raise ValueError("配置文件不存在，请刷新")
        config = manager.confs[profile_id]
        settings = config["platform_settings"]
        ids = [str(i).strip() for i in settings.get("id_whitelist", []) if str(i).strip()]
        values = {"profile": profile_id,
                  "enabled": settings.get("enable_id_white_list", True),
                  "ids": ids,
                  "ignore_admin_group": settings.get("wl_ignore_admin_on_group", False),
                  "ignore_admin_friend": settings.get("wl_ignore_admin_on_friend", False)}
        platforms = manager.default_conf.get("platform", [])
        return {"available": True, **values, "revision": revision(values),
                "profiles": [{"id": p["id"], "name": p["name"]} for p in manager.get_conf_list()],
                "platforms": [{"id": p["id"], "name": p.get("name") or p["id"]}
                              for p in platforms if p.get("type") == "aiocqhttp"],
                "rows": [{"id": item, "kind": "私聊" if ":FriendMessage:" in item else
                          "群聊" if ":GroupMessage:" in item or item.isdecimal() else "会话"}
                         for item in ids]}

    async def write_whitelist(self, body):
        if set(body) - {"profile", "revision", "action", "id", "value", "enabled"}:
            raise ValueError("不支持的白名单参数")
        current = self.read_whitelist(body.get("profile", "default"))
        if not current["available"]:
            raise ValueError("当前实例未连接 AstrBot 配置")
        if body.get("revision") != current["revision"]:
            raise ValueError("白名单已变化，请刷新后重试")
        ids = list(current["ids"])
        action = body.get("action")
        enabled = current["enabled"]
        if action in {"create", "update"}:
            value = body.get("value")
            if not isinstance(value, str):
                raise ValueError("请填写群号或完整会话 ID")
            value = value.strip()
            if not (re.fullmatch(r"[1-9][0-9]{0,19}", value) or re.fullmatch(
                    r"[^:\s\x00-\x1f]{1,80}:(?:FriendMessage|GroupMessage):[^\s\x00-\x1f]{1,256}", value)):
                raise ValueError("请填写有效群号或完整会话 ID，例如 snowluma:FriendMessage:123456")
            if value in ids and (action == "create" or value != body.get("id")):
                raise ValueError("该会话已在白名单中")
            if action == "create":
                if len(ids) >= 1000:
                    raise ValueError("白名单最多保存 1000 条")
                ids.append(value)
            else:
                if body.get("id") not in ids:
                    raise ValueError("白名单条目不存在，请刷新")
                ids[ids.index(body["id"])] = value
        elif action == "delete":
            if body.get("id") not in ids:
                raise ValueError("白名单条目不存在，请刷新")
            ids.remove(body["id"])
        elif action == "configure":
            if type(body.get("enabled")) is not bool:
                raise ValueError("白名单开关须为布尔值")
            enabled = body["enabled"]
        else:
            raise ValueError("不支持的白名单操作")
        config = self.core.astrbot_config_mgr.confs[current["profile"]]
        before = deepcopy(config["platform_settings"])
        self.store.audit("whitelist", current["profile"], current)
        config["platform_settings"] = {**before, "id_whitelist": ids,
                                       "enable_id_white_list": enabled}
        try:
            config.save_config()
            await self.core.reload_pipeline_scheduler(current["profile"])
        except Exception:
            config["platform_settings"] = before
            config.save_config()
            with suppress(Exception):
                await self.core.reload_pipeline_scheduler(current["profile"])
            raise
        return {"message": "白名单已保存，后续消息立即按新名单处理", **self.read_whitelist(current["profile"])}

    def read_douyin_whitelist(self):
        from services.douyin_worker.config import ConfigStore

        config = ConfigStore().read()
        values = {"account": config.account.model_dump() if config.account else None,
                  "friends": [friend.model_dump() for friend in config.friends]}
        return {"rows": values["friends"], "account": values["account"],
                "limit": 20, "revision": revision(values)}

    def write_douyin_whitelist(self, body):
        from services.douyin_worker.config import ConfigStore, Friend

        if set(body) - {"revision", "action", "id", "values"}:
            raise ValueError("不支持的抖音白名单参数")
        current = self.read_douyin_whitelist()
        if body.get("revision") != current["revision"]:
            raise ValueError("抖音好友名单或分享账号已变化，请刷新后重试")
        rows, action = deepcopy(current["rows"]), body.get("action")
        index = next((i for i, r in enumerate(rows) if r["douyin_id"] == body.get("id")), None)
        if action in {"update", "delete"} and index is None:
            raise ValueError("抖音好友不存在，请刷新")
        if action in {"create", "update"}:
            values = body.get("values")
            if not isinstance(values, dict) or set(values) - {"douyin_id", "name", "opted_in"}:
                raise ValueError("请填写抖音号、昵称和分享开关")
            values = {**(rows[index] if action == "update" else {}), **values}
            for key in ("douyin_id", "name"):
                if not isinstance(values.get(key), str) or not values[key].strip():
                    raise ValueError("抖音号与昵称不能为空")
                values[key] = values[key].strip()
            friend = Friend.model_validate(values).model_dump()
            if current["account"] and friend["douyin_id"] == current["account"]["douyin_id"]:
                raise ValueError("不能将当前分享账号加入好友名单")
            if any(r["douyin_id"] == friend["douyin_id"] for i, r in enumerate(rows)
                   if action == "create" or i != index):
                raise ValueError("该抖音号已在好友名单中")
            if action == "create":
                if len(rows) >= current["limit"]:
                    raise ValueError("抖音分享好友最多 20 位")
                rows.append(friend)
            else:
                rows[index] = friend
        elif action == "delete":
            rows.pop(index)
        else:
            raise ValueError("不支持的抖音好友操作")
        audit_key = friend["douyin_id"] if action == "create" else body["id"]
        self.store.audit("douyin_whitelist", audit_key, current)
        ConfigStore().update({"friends": rows})
        return {"message": "抖音好友名单已保存，后续分享按新名单执行", **self.read_douyin_whitelist()}

    async def read(self, endpoint, query):
        p = self.plugin
        if endpoint == "whitelist":
            return self.read_whitelist(query.get("profile", "default"))
        if endpoint == "douyin-whitelist":
            return self.read_douyin_whitelist()
        page = max(1, min(100000, int(query.get("page", 1))))
        limit, offset = 30, (page - 1) * 30
        search = str(query.get("q", ""))[:200]
        if endpoint == "overview":
            from .media_state import MediaState
            from services.douyin_worker.config import ConfigStore
            from services.douyin_worker.store import Store

            store, config = Store(self.directory), ConfigStore().read()
            return {
                "character": p.memory.character,
                "qq_busy": p.foreground_activity.blocked(),
                "gpu": MediaState(self.directory).mode(),
                "douyin_enabled": config.enabled,
                "douyin_awake": config.awake(time.time()),
                "douyin_halt": store.get("halt"),
                "timezone": config.timezone,
                "supervisor_online": self.supervisor_online(),
            }
        if endpoint == "videos":
            from services.douyin_worker.store import Store
            from services.douyin_worker.interest_scorer import recommend

            store = Store(self.directory)
            where, args = "(title LIKE ? OR id LIKE ?)", ["%" + search + "%"] * 2
            with store.connection() as db:
                total = db.execute(
                    "SELECT count(*) FROM videos WHERE " + where, args
                ).fetchone()[0]
                rows = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM videos WHERE "
                        + where
                        + " ORDER BY updated DESC LIMIT ? OFFSET ?",
                        (*args, limit, offset),
                    )
                ]
                for row in rows:
                    row["analysis"] = (
                        json.loads(row["analysis"]) if row["analysis"] else None
                    )
                    row["interest"] = (
                        json.loads(row["interest"]) if row["interest"] else None
                    )
                    row["share_intent"] = (
                        recommend(row["analysis"], row["interest"])
                        if row["analysis"] and row["interest"]
                        else None
                    )
                    row["shares"] = [
                        dict(r)
                        for r in db.execute(
                            "SELECT recipient,state,sent,evidence FROM share_ledger WHERE account=? AND video=? ORDER BY created DESC",
                            (row["account"], row["id"]),
                        )
                    ]
                    row["share_skips"] = [
                        dict(r)
                        for r in db.execute(
                            "SELECT recipient,reason FROM share_decisions WHERE account=? AND video=?",
                            (row["account"], row["id"]),
                        )
                    ]
            return {"rows": rows, "total": total, "page": page}
        if endpoint == "memory":
            if hasattr(p.memory, "shared"):
                return self.read_shared_memory(query, page, limit, offset, search)
            repo = p.memory.repository
            kind = query.get("kind", "memory")
            with repo.connection() as db:
                if kind == "diary":
                    where = "character_id=? AND (draft LIKE ? OR entry_date LIKE ?)"
                    args = (p.memory.character, "%" + search + "%", "%" + search + "%")
                    table, order = "diary_entries", "entry_date"
                else:
                    where = "character_id=? AND deleted_at IS NULL AND (summary LIKE ? OR owner_key LIKE ?)"
                    args = (p.memory.character, "%" + search + "%", "%" + search + "%")
                    table, order = "memory_items", "updated_at"
                total = db.execute(
                    f"SELECT count(*) FROM {table} WHERE {where}", args
                ).fetchone()[0]
                rows = [
                    dict(r)
                    for r in db.execute(
                        f"SELECT * FROM {table} WHERE {where} ORDER BY {order} DESC LIMIT ? OFFSET ?",
                        (*args, limit, offset),
                    )
                ]
            return {
                "rows": [{**r, "revision": revision(r)} for r in rows],
                "total": total,
                "page": page,
            }
        if endpoint == "contexts":
            manager = p.context.conversation_manager
            window = p.router.context
            window._prune(time.time())
            if key := query.get("id"):
                if key.startswith("group:"):
                    scope = key[6:]
                    raw = self.group_messages(scope)
                    return {
                        "id": key,
                        "session": scope,
                        "messages": self.display_messages(raw),
                        "revision": revision(
                            [raw, [vars(m) for m in window._windows.get(scope, [])]]
                        ),
                    }
                conv = await manager.get_conversation("", key)
                if not conv:
                    raise ValueError("会话不存在")
                raw = json.loads(conv.history or "[]")
                return {
                    "id": key,
                    "session": conv.user_id,
                    "messages": self.display_messages(raw),
                    "revision": revision(raw),
                }
            convs, total = await manager.get_filtered_conversations(
                page=page, page_size=limit, search_query=search, include_history=False
            )
            rows = [
                {
                    "id": c.cid,
                    "session": c.user_id,
                    "title": c.title or c.user_id,
                    "kind": "conversation",
                }
                for c in convs
            ]
            if page == 1:
                rows = [
                    {
                        "id": "group:" + scope,
                        "session": scope,
                        "title": "群临时上下文",
                        "kind": "group",
                    }
                    for scope in dict.fromkeys([*window._protocol, *window._windows])
                    if ":group:" in scope and search in scope
                ] + rows
            return {"rows": rows, "total": total, "page": page}
        if endpoint in {"exchanges", "usage"}:
            records = await asyncio.to_thread(self.load_exchanges)
            if endpoint == "usage":
                return self.store.usage(query.get("since", ""))
            records = [
                r
                for r in reversed(records)
                if search in str(r.get("session") or "")
                or search in str(r.get("phase") or "")
            ]
            return {
                "rows": records[offset : offset + limit],
                "total": len(records),
                "page": page,
            }
        if endpoint == "model":
            from scripts.qq_companion.model_control import MODEL_FIELDS
            from runtime.qwen3.config import load_qwen3_config

            values = vars(load_qwen3_config(self.root / "configs/runtime/qwen3.yaml"))
            editable = {k: values[k] for k in MODEL_FIELDS}
            with self.store.connection() as db:
                row = db.execute(
                    "SELECT id,state,error,created,updated FROM model_jobs ORDER BY created DESC LIMIT 1"
                ).fetchone()
            return {
                "fields": fields_json(MODEL_FIELDS, editable),
                "revision": revision(editable),
                "job": dict(row) if row else None,
                "online": self.supervisor_online(),
                "vision": self.vision_status(),
            }
        if endpoint == "persona":
            persona = await p.context.persona_manager.get_persona(p.memory.character)
            if persona is None:
                raise ValueError("当前人格不存在")
            value = {"id": persona.persona_id, "text": persona.system_prompt}
            return {
                **value,
                "revision": revision(value),
                "source": "AstrBot 人格库；QQ、日记和抖音共用",
            }
        if endpoint == "settings":
            from services.douyin_worker.config import ConfigStore

            defaults = json.loads(
                (Path(__file__).parent / "_conf_schema.json").read_text(
                    encoding="utf-8"
                )
            )
            values = {k: self.config.get(k, defaults[k]["default"]) for k in APP_FIELDS}
            dy = ConfigStore().read().model_dump()
            dv = {k: dy[k] for k in DOUYIN_FIELDS}
            return {
                "fields": fields_json(APP_FIELDS, values),
                "douyin": [
                    {
                        "key": k,
                        "name": name,
                        "value": dy[k],
                        "type": type(dy[k]).__name__,
                        **({"min": 0, "max": 100} if k == "long_video_percent" else {}),
                    }
                    for k, name in DOUYIN_FIELDS.items()
                ],
                "timezone": dy["timezone"],
                "followup_probabilities": probability_curve(values["followup_average_probability"], values["followup_pity_limit"]),
                "whitelist": self.read_whitelist(),
                "douyin_whitelist": self.read_douyin_whitelist(),
                "revision": revision({"app": values, "douyin": dv}),
            }
        if endpoint == "prompts":
            values = yaml.safe_load(prompt_path().read_text(encoding="utf-8-sig"))
            return {"rows": prompt_entries(values), "revision": revision(values)}
        raise ValueError("未知页面")

    def load_exchanges(self):
        from .model_debug import (
            debug_directory,
            _archive_lock,
            _archive_files,
            _read_records,
        )

        directory = debug_directory()
        with _archive_lock(directory):
            files = _archive_files(directory)
            signature = [
                (p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files
            ]
            if signature == self.exchange_signature:
                return self.exchange_rows
            prompts = load_prompts()
            raw = yaml.safe_load(prompt_path().read_text(encoding="utf-8-sig"))
            for profile, values in raw.get("model_profiles", {}).items():
                prompts.update(
                    {f"model_profiles.{profile}.{k}": v for k, v in values.items()}
                )
            rows = []
            for path in files:
                for record in _read_records(path):
                    self.store.record_usage(record)
                    rows.append(public_exchange(record, prompts))
            self.exchange_signature, self.exchange_rows = signature, rows
            return rows

    def vision_status(self):
        from .vision_config import VisionGates

        gates = VisionGates(self.directory)
        return {
            kind: gates.enabled(kind) for kind in ("image", "sticker", "gif", "video")
        }

    @staticmethod
    def display_messages(messages):
        return [
            {
                "index": i,
                **{
                    k: current_content(v) if k == "content" else v
                    for k, v in m.items()
                    if k in {"role", "content", "tool_calls", "tool_call_id", "name"}
                },
            }
            for i, m in enumerate(messages)
            if m.get("role") != "system"
        ]

    def group_messages(self, scope):
        window = self.plugin.router.context
        raw = window.model_history(scope)
        present = json.dumps(raw, ensure_ascii=False)
        for message in window._windows.get(scope, []):
            if message.text and message.text not in present:
                raw.append({"role": message.role, "content": message.text})
        return raw

    def supervisor_online(self):
        import psutil

        try:
            data = json.loads(
                (self.directory / "supervisor.json").read_text(encoding="utf-8")
            )
            return (
                abs(
                    psutil.Process(data["supervisor_pid"]).create_time()
                    - data["supervisor_created_at"]
                )
                < 1
            )
        except (OSError, ValueError, KeyError, psutil.Error):
            return False

    async def write(self, endpoint, body):
        if endpoint == "whitelist":
            return await self.write_whitelist(body)
        if endpoint == "douyin-whitelist":
            return self.write_douyin_whitelist(body)
        p = self.plugin
        if endpoint == "memory":
            return self.write_memory(body)
        if endpoint == "contexts":
            self.context_gate.clear()
            try:
                self.check_idle()
                return await self.write_context(body)
            finally:
                self.context_gate.set()
        if endpoint == "model":
            from scripts.qq_companion.model_control import ModelControl, MODEL_FIELDS
            from runtime.qwen3.config import load_qwen3_config

            values = vars(load_qwen3_config(self.root / "configs/runtime/qwen3.yaml"))
            self.check_revision(body, {k: values[k] for k in MODEL_FIELDS})
            if not self.supervisor_online():
                raise ValueError(
                    "监督器未运行；请通过 start_all.ps1 启动后保存，才能自动重启模型"
                )
            return {
                "job_id": ModelControl(self.root, self.directory).submit(
                    body["values"]
                ),
                "message": "已提交；等待在途任务结束后保存并重启模型，失败会回滚",
            }
        if endpoint == "persona":
            current = await self.read("persona", {})
            self.check_revision(body, {"id": current["id"], "text": current["text"]})
            text = body.get("text")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 50000:
                raise ValueError("人物性格须为 1–50000 个字符")
            self.store.audit("persona", current["id"], current)
            await p.context.persona_manager.update_persona(
                current["id"], system_prompt=text
            )
            return {"message": "已保存；下一次 QQ、日记和抖音请求生效，重启后保留"}
        if endpoint == "settings":
            current = await self.read("settings", {})
            if body.get("revision") != current["revision"]:
                raise ValueError("设置已变化，请刷新")
            patch, dy = body.get("app", {}), body.get("douyin", {})
            if (
                not isinstance(patch, dict)
                or set(patch) - APP_FIELDS.keys()
                or not isinstance(dy, dict)
                or set(dy) - DOUYIN_FIELDS.keys()
            ):
                raise ValueError("不支持的应用设置")
            for key, value in patch.items():
                _, kind, low, high = APP_FIELDS[key]
                if (
                    kind in {int, bool, str}
                    and type(value) is not kind
                    or kind is float
                    and type(value) not in {int, float}
                    or low is not None
                    and not low <= value <= high
                ):
                    raise ValueError(f"{key} 的值不合法")
            if "diary_time" in patch and not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d", patch["diary_time"]
            ):
                raise ValueError("日记时间格式应为 HH:MM")
            from services.douyin_worker.config import ConfigStore, DouyinConfig

            ds = ConfigStore()
            checked = DouyinConfig.model_validate({**ds.read().model_dump(), **dy})
            if checked.start_hour >= checked.end_hour:
                raise ValueError("后台开始小时必须早于结束小时")
            self.store.audit("settings", "application", current)
            old = dict(self.config)
            normalized = normalize_settings({**old, **patch})
            corrections = [APP_FIELDS[key][0] + " → " + str(value)
                           for key, value in normalized.items() if value != {**old, **patch}.get(key)]
            self.config.update(patch)
            self.config.update(normalized)
            try:
                self.config.save_config()
                ds.update(dy)
            except Exception:
                self.config.clear()
                self.config.update(old)
                self.config.save_config()
                raise
            self.apply_app()
            if p.memory.enabled and p.memory.scheduler is None:
                await p.memory.start(
                    p.memory.provider_factory,
                    p.memory.admission,
                    p.memory.persona_factory,
                )
            return {"message": "已保存并生效；休息时段仅停止后台刷视频，QQ 正常响应" +
                    ("。已自动纠正：" + "；".join(corrections) if corrections else "")}
        if endpoint == "prompts":
            path = prompt_path()
            before = path.read_text(encoding="utf-8-sig")
            values = yaml.safe_load(before)
            self.check_revision(body, values)
            key, text = body.get("key"), body.get("text")
            known = {r["key"] for r in prompt_entries(values)}
            if (
                key not in known
                or not isinstance(text, str)
                or not 1 <= len(text.strip()) <= 50000
            ):
                raise ValueError("提示词名称或内容无效")
            parts, target = key.split("."), values
            for part in parts[:-1]:
                target = target[part]
            old = target[parts[-1]]
            if set(re.findall(r"\$[a-zA-Z_]+", old)) != set(
                re.findall(r"\$[a-zA-Z_]+", text)
            ):
                raise ValueError("必须保留原提示词的变量占位符")
            target[parts[-1]] = text
            # Replace one YAML block and preserve comments and unrelated fields.
            indent = "  " * (len(parts) - 1)
            pattern = (
                r"(?m)^"
                + re.escape(indent + parts[-1] + ":")
                + r"[^\n]*(?:\n(?:"
                + re.escape(indent + " ")
                + r"[^\n]*|[ \t]*))*"
            )
            if len(parts) > 1:
                lines = before.splitlines(keepends=True)
                begin = next(
                    i
                    for i, line in enumerate(lines)
                    if line.startswith("  " + parts[1] + ":")
                )
                end = next(
                    (
                        i
                        for i in range(begin + 1, len(lines))
                        if lines[i].strip() and not lines[i].startswith("    ")
                    ),
                    len(lines),
                )
                segment = "".join(lines[begin:end])
                changed = re.sub(
                    pattern,
                    lambda _: (
                        indent
                        + parts[-1]
                        + ": |\n"
                        + "\n".join(indent + "  " + line for line in text.splitlines())
                        + "\n"
                    ),
                    segment,
                    count=1,
                )
                after = "".join(lines[:begin]) + changed + "".join(lines[end:])
            else:
                after = re.sub(
                    pattern,
                    lambda _: (
                        key
                        + ": |\n"
                        + "\n".join("  " + line for line in text.splitlines())
                        + "\n"
                    ),
                    before,
                    count=1,
                )
            if yaml.safe_load(after) != values:
                # YAML block scalar trailing newline normalization is harmless.
                parsed = yaml.safe_load(after)
                if {r["key"]: r["text"].strip() for r in prompt_entries(parsed)} != {
                    r["key"]: r["text"].strip() for r in prompt_entries(values)
                }:
                    raise ValueError("无法安全保存该 YAML 段落")
            temp = path.with_suffix(".web.tmp")
            try:
                temp.write_text(after, encoding="utf-8")
                load_prompts(temp)
                for profile in values.get("model_profiles", {}):
                    load_prompts(temp, profile=profile)
                self.store.audit("prompts", key, old)
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
            return {"message": "提示词已保存；下一次调用读取新内容"}
        raise ValueError("此页面不支持写入")

    def read_shared_memory(self, query, page, limit, offset, search):
        memory = self.plugin.memory
        s, char = memory.shared, memory.character
        # Administrator authentication occurs in the registered route, before this method.
        kind = query.get("kind", "shared")
        if kind in {"memory", "diary", "legacy"}:
            table, body_col = ("diary_entries", "draft") if kind == "diary" else ("memory_items", "summary")
            with s.connection() as db:
                where = "character_id=? AND " + body_col + " LIKE ?"
                if table == "memory_items":
                    where += " AND deleted_at IS NULL"
                args = (char, "%" + search + "%")
                total = db.execute(f"SELECT count(*) FROM {table} WHERE {where}", args).fetchone()[0]
                rows = [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*args, limit, offset))]
            return dict(rows=[{**r, "legacy": True} for r in rows], total=total, page=page,
                inventory=s.get_inventory_stats(char), requests=s.get_request_usage(character=char))
        clauses, args = ["r.character=?", "r.status='active'", "(r.body LIKE ? OR r.scope LIKE ?)"], [char, "%"+search+"%", "%"+search+"%"]
        for column in ("module", "scope", "visibility", "day", "kind"):
            value = query.get("record_type" if column == "kind" else column)
            if value:
                clauses.append("r." + column + "=?")
                args.append(value)
        if query.get("batch"):
            clauses.append("r.id IN (SELECT record_id FROM sm_members WHERE batch=? UNION SELECT summary_id FROM sm_batches WHERE id=?)")
            args.extend([query["batch"], query["batch"]])
        if query.get("layer") in {"short", "long"}:
            clauses.append("r." + query["layer"] + "=1")
        where = " AND ".join(clauses)
        with s.connection() as db:
            total = db.execute("SELECT count(*) FROM sm_records r WHERE " + where, args).fetchone()[0]
            rows = [dict(r) for r in db.execute("SELECT r.* FROM sm_records r WHERE " + where + " ORDER BY r.occurred DESC LIMIT ? OFFSET ?", (*args, limit, offset))]
            for row in rows:
                row["consumers"] = [dict(r) for r in db.execute("SELECT consumer,state,reason FROM sm_consumers WHERE record_id=?", (row["id"],))]
        return dict(rows=rows, total=total, page=page, inventory=s.get_inventory_stats(char), requests=s.get_request_usage(character=char))

    def write_shared_memory(self, body):
        s, char = self.plugin.memory.shared, self.plugin.memory.character
        action = body.get("action")
        if action == "configure":
            config = s.configure(body.get("values", {}))
            self.apply_app()
            self.plugin.memory.wake()
            return dict(config=config, message="配置已保存；预算和 TTL 下次请求生效，降低历史精选上限需重选任务成功后生效")
        if action == "retry":
            with s.connection() as db:
                if not db.execute("SELECT 1 FROM sm_jobs WHERE id=? AND character=?", (body.get("id"), char)).fetchone():
                    raise ValueError("任务不存在")
            s.retry(body["id"])
            self.plugin.memory.wake()
            return {"message": "已恢复同一任务，不新增批次"}
        if action == "retention":
            scope = body.get("scope", "")
            bot = owner_bot(scope)
            s.set_retention(bot, char, scope, body.get("retention"))
            s.scrub_backups()
            return {"message": "保留限制已生效；受影响派生内容已撤回"}
        key = body.get("id")
        if body.get("kind") in {"memory", "diary", "legacy"}:
            if action != "delete":
                raise ValueError("历史兼容数据只读；可撤回，不能继续使用旧长期写入口")
            s.govern_legacy(key, character=char)
            return {"message": "历史记录及派生内容已撤回，正文与受管理备份已清理"}
        with s.connection() as db:
            row = db.execute("SELECT version FROM sm_records WHERE id=? AND character=?", (key, char)).fetchone()
        if not row or row[0] != body.get("version"):
            raise ValueError("记录已改变，请刷新")
        if action == "delete":
            s.withdraw(key)
            s.scrub_backups()
            return {"message": "已撤回来源及所有派生摘要，不保留被删除正文"}
        if action == "correct":
            s.correct_record(key, body.get("text"), body["version"], character=char)
            return {"message": "纠正已生效；含旧内容的派生摘要已撤回"}
        raise ValueError("只允许配置、重试、纠正和数据治理；新业务内容须先通过模块短期入口")

    def write_memory(self, body):
        if hasattr(self.plugin.memory, "shared"):
            return self.write_shared_memory(body)
        repo, char = self.plugin.memory.repository, self.plugin.memory.character
        kind, action = body.get("kind", "memory"), body.get("action", "save")
        key, now = body.get("id"), time.time()
        with repo.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            table = "diary_entries" if kind == "diary" else "memory_items"
            row = (
                db.execute(
                    f"SELECT * FROM {table} WHERE id=? AND character_id=?", (key, char)
                ).fetchone()
                if key
                else None
            )
            old = dict(row) if row else None
            if key:
                if not old:
                    raise ValueError("记录不存在")
                self.check_revision(body, old)
                self.store.audit(kind, key, old)
            if action == "delete":
                if not old:
                    raise ValueError("记录不存在")
                if kind == "diary":
                    # Publishing state is retained separately; this never removes a QQ post.
                    db.execute("DELETE FROM diary_entries WHERE id=?", (key,))
                    db.execute(
                        "UPDATE memory_items SET deleted_at=?,updated_at=? WHERE source_ref=?",
                        (now, now, key),
                    )
                else:
                    db.execute(
                        "UPDATE memory_items SET deleted_at=?,updated_at=? WHERE id=? OR id IN (SELECT child_id FROM memory_links WHERE parent_id=?)",
                        (now, now, key, key),
                    )
                db.execute(
                    "WITH RECURSIVE descendants(id) AS (SELECT child_id FROM memory_links WHERE parent_id=? "
                    "UNION SELECT l.child_id FROM memory_links l JOIN descendants d ON l.parent_id=d.id) "
                    "UPDATE memory_items SET deleted_at=?,updated_at=? WHERE id IN (SELECT id FROM descendants)",
                    (key, now, now),
                )
                db.execute(
                    "DELETE FROM memory_compactions WHERE character_id=?", (char,)
                )
                return {
                    "message": "已删除，后续召回不再使用；操作前内容保存在本地编辑记录"
                }
            value = body.get("values", {})
            if kind == "diary":
                draft = value.get("draft", "")
                if not isinstance(draft, str) or not 1 <= len(draft) <= 10000:
                    raise ValueError("日记正文长度应为 1–10000")
                if old:
                    db.execute(
                        "UPDATE diary_entries SET draft=?,public_candidate=NULL WHERE id=?",
                        (draft, key),
                    )
                else:
                    from datetime import date

                    bot, day = (
                        str(value.get("bot", "")),
                        str(value.get("entry_date", "")),
                    )
                    if not bot.isdecimal():
                        raise ValueError("QQ 账号须为数字")
                    date.fromisoformat(day)
                    key = uuid.uuid4().hex
                    db.execute(
                        "INSERT INTO diary_entries VALUES(?,?,?,?,?,'private',NULL,'[]',NULL,?)",
                        (key, bot, char, day, draft, now),
                    )
                from .memory_repository import identity

                db.execute(
                    "UPDATE memory_items SET deleted_at=?,updated_at=? WHERE id=?",
                    (now, now, identity(key, "public")),
                )
                repo._put(
                    db,
                    key=key,
                    character=char,
                    scope="diary",
                    owner=f"qq:{old['bot'] if old else bot}:diary:{char}",
                    subject="character",
                    fact_key=old["entry_date"] if old else day,
                    source_type="diary",
                    source_ref=key,
                    summary=draft,
                    tags=["diary", old["entry_date"] if old else day],
                    salience=0.7,
                    confidence=1,
                    now=now,
                )
            else:
                summary = value.get("summary", "")
                tags = value.get("tags", [])
                if (
                    not isinstance(summary, str)
                    or not 1 <= len(summary.strip()) <= 4000
                ):
                    raise ValueError("记忆正文长度应为 1–4000")
                if (
                    not isinstance(tags, list)
                    or len(tags) > 12
                    or any(not isinstance(t, str) or len(t) > 32 for t in tags)
                ):
                    raise ValueError("标签最多 12 项，每项最多 32 字")
                confidence, salience = (
                    value.get("confidence", 1.0),
                    value.get("salience", 0.5),
                )
                if any(
                    type(v) not in {int, float} or not 0 <= v <= 1
                    for v in (confidence, salience)
                ):
                    raise ValueError("置信度和重要性须在 0–1 之间")
                if old:
                    owner, visibility = old["owner_key"], old["visibility"]
                else:
                    owner = str(value.get("owner_key", ""))
                    if re.fullmatch(r"qq:\d{1,20}:shared:" + re.escape(char), owner):
                        visibility = "shared"
                    else:
                        owner_bot(owner)
                        visibility = "private"
                key = key or uuid.uuid4().hex
                repo._put(
                    db,
                    key=key,
                    character=char,
                    scope=old["scope"] if old else "fact",
                    owner=owner,
                    subject=old["subject_key"] if old else "operator",
                    fact_key=old["fact_key"] if old else key,
                    source_type=old["source_type"] if old else "operator",
                    source_ref=old["source_ref"] if old else "webui:" + key,
                    summary=summary,
                    tags=tags,
                    confidence=confidence,
                    salience=salience,
                    sensitive=bool(old["sensitive"])
                    if old
                    else visibility == "private",
                    visibility=visibility,
                    now=now,
                )
                # Derived projections cannot retain the pre-edit private fact.
                db.execute(
                    "UPDATE memory_items SET deleted_at=?,updated_at=? WHERE id IN (SELECT child_id FROM memory_links WHERE parent_id=?)",
                    (now, now, key),
                )
            db.execute("DELETE FROM memory_compactions WHERE character_id=?", (char,))
        return {"id": key, "message": "已保存，下次记忆检索生效"}

    async def write_context(self, body):
        manager, window = (
            self.plugin.context.conversation_manager,
            self.plugin.router.context,
        )
        key, action = body.get("id", ""), body.get("action", "save")
        if action == "create":
            session = str(body.get("session", ""))
            if not re.fullmatch(r"qq:\d{1,20}:dm:\d{1,20}", session):
                raise ValueError("私聊会话应为 qq:机器人QQ:dm:好友QQ")
            # This is the same canonical transport session used in filter_inbound.
            origin = "snowluma:FriendMessage:" + session
            cid = await manager.new_conversation(
                origin,
                platform_id="snowluma",
                content=[],
                persona_id=self.plugin.memory.character,
            )
            return {"id": cid, "message": "已创建并选为当前会话"}
        is_group = key.startswith("group:")
        if is_group:
            scope = key[6:]
            window._prune(time.time())
            raw = self.group_messages(scope)
            self.check_revision(
                body, [raw, [vars(m) for m in window._windows.get(scope, [])]]
            )
        else:
            conv = await manager.get_conversation("", key)
            if conv is None:
                raise ValueError("会话不存在")
            raw = json.loads(conv.history or "[]")
            self.check_revision(body, raw)
            scope = conv.user_id.rsplit("Message:", 1)[-1]
        edited = deepcopy(raw)
        if action in {"clear", "delete_session"}:
            edited = []
        elif action == "add":
            edited.extend(body["messages"] if "messages" in body else [body["message"]])
        elif action in {"save", "delete"}:
            index = body.get("index")
            if (
                type(index) is not int
                or not 0 <= index < len(edited)
                or edited[index].get("role") == "system"
            ):
                raise ValueError("消息位置无效")
            if action == "delete":
                selected = edited[index]
                calls = {c["id"] for c in selected.get("tool_calls", [])}
                if selected.get("role") == "tool":
                    target = selected.get("tool_call_id")
                    for m in edited:
                        if any(c.get("id") == target for c in m.get("tool_calls", [])):
                            calls = {c["id"] for c in m["tool_calls"]}
                            break
                edited = [
                    m
                    for i, m in enumerate(edited)
                    if i != index
                    and m.get("tool_call_id") not in calls
                    and not any(c.get("id") in calls for c in m.get("tool_calls", []))
                ]
            else:
                replacement = body["message"]
                # Preserve structured current-message input without restoring hidden history.
                old_content = edited[index].get("content")
                if isinstance(old_content, str):
                    try:
                        payload = json.loads(old_content)
                    except ValueError:
                        payload = None
                    if (
                        isinstance(payload, dict)
                        and "current_message" in payload
                        and replacement.get("role") == "user"
                    ):
                        text = replacement.get("content", "")
                        payload["current_message"]["text"] = text
                        payload["recent_messages"] = []
                        replacement = {
                            **replacement,
                            "content": json.dumps(payload, ensure_ascii=False),
                        }
                edited[index] = replacement
        else:
            raise ValueError("未知上下文操作")
        validate_messages([m for m in edited if m.get("role") != "system"])
        self.store.audit("context", key, raw)
        if is_group:
            window._protocol.pop(scope, None)
            window._windows.pop(scope, None)
            if edited:
                window.remember_protocol(scope, "webui", edited)
        elif action == "delete_session":
            await manager.delete_conversation(conv.user_id, key)
            window._windows.pop(scope, None)
        else:
            await manager.update_conversation(
                conv.user_id, key, history=edited, token_usage=0
            )
            if action == "clear":
                window._windows.pop(scope, None)
        return {
            "message": "上下文已更新；下一次消息使用新内容，历史工具调用不会重新执行"
        }
