"""Validated, hot-readable parameters shared by all memory processes."""
from __future__ import annotations

import json
from functools import lru_cache
from zoneinfo import ZoneInfo


def integer(default, minimum, maximum, title):
    return dict(type="integer", default=default, minimum=minimum, maximum=maximum, title=title)


PARAMETERS = {
    "timezone": dict(type="string", default="Asia/Hong_Kong", title="自然日时区（IANA）"),
    "finalize_time": dict(type="string", default="00:05", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$", title="次日日结时间"),
    "short_term_hours": integer(72, 1, 8760, "短期最短保留小时"),
    "recovery_days": integer(7, 1, 90, "旧版本恢复天数（非经历覆盖期限）"),
    "soft_records": integer(10000, 10, 1000000, "短期条数软阈值"),
    "soft_bytes": integer(67108864, 4096, 1073741824, "短期正文软阈值（字节）"),
    "hard_bytes": integer(268435456, 8192, 4294967296, "未消费来源及短期正文硬阈值（字节）"),
    "memory_tokens": integer(6144, 0, 32768, "总记忆预算 tokens"),
    "recent_tokens": integer(512, 0, 2048, "共同近况预算 tokens（包含在总预算内）"),
    "checkpoint_messages": integer(50, 1, 1000, "QQ 增量检查点消息数"),
    "checkpoint_minutes": integer(30, 1, 240, "QQ 增量检查点分钟"),
    "max_attempts": integer(5, 1, 20, "维护任务自动重试上限"),
    "selected_history_limit": integer(80, 1, 200, "历史精选上限（不含最多九个待合并日批）"),
    "video_chars": integer(400, 50, 2000, "角色观看摘要字数"),
    "video_tokens": integer(768, 128, 4096, "角色观看摘要 tokens"),
    "qq_chars": integer(600, 50, 3000, "会话摘要字数"),
    "qq_tokens": integer(1024, 128, 4096, "会话摘要 tokens"),
    "video_daily_tokens": integer(1024, 128, 4096, "视频日批总摘要 tokens"),
    "video_rollup_tokens": integer(2048, 256, 8192, "视频累计总摘要 tokens"),
    "diary_chars": integer(400, 50, 2000, "每日日记字数"),
    "diary_tokens": integer(768, 128, 4096, "每日日记 tokens"),
    "diary_rollup_tokens": integer(2048, 256, 8192, "累计日记 tokens（无日期截断）"),
}


def validate_config(value):
    import jsonschema
    jsonschema.validate(value, {"type": "object", "properties": PARAMETERS,
        "additionalProperties": False, "required": list(PARAMETERS)})
    ZoneInfo(value["timezone"])
    if value["recent_tokens"] > value["memory_tokens"] or value["soft_bytes"] > value["hard_bytes"]:
        raise ValueError("inconsistent_memory_limits")
    return value


def defaults():
    return {key: item["default"] for key, item in PARAMETERS.items()}


def read_config(db):
    row = db.execute("SELECT value FROM sm_meta WHERE key='config'").fetchone()
    return dict(_parsed_config(row[0] if row else "{}"))


@lru_cache(maxsize=32)
def _parsed_config(raw):
    return validate_config({**defaults(), **json.loads(raw)})
