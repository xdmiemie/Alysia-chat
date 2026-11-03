"""Registered module semantics; no model can grant itself a policy capability."""
from __future__ import annotations

from dataclasses import dataclass

from .shared_memory import ModuleDescriptor


@dataclass
class ModulePolicy:
    promotable: tuple[str, ...] = ()
    consumer_map: dict | None = None

    def consumers(self, kind, experience):
        return (self.consumer_map or {}).get(kind, ("diary",) if experience else ())

    def can_promote(self, kind):
        return kind in self.promotable


def register_builtin_modules(service):
    service.register_module(ModuleDescriptor("qq", "QQ Agent", ("qq_session_summary", "qq_explicit_liked"),
        ("qq_chars", "qq_tokens", "checkpoint_messages", "checkpoint_minutes")), ModulePolicy(
        ("qq_explicit_liked",), {"qq_session_summary": ("diary",), "qq_explicit_liked": ("diary", "qq_review")}))
    service.register_module(ModuleDescriptor("video", "短视频", ("video_watch", "video_daily", "video_rollup"),
        ("video_chars", "video_tokens", "selected_history_limit", "video_daily_tokens", "video_rollup_tokens")), ModulePolicy(
        ("video_watch", "video_daily", "video_rollup"), {"video_watch": ("video_daily", "diary")}))
    service.register_module(ModuleDescriptor("diary", "日记", ("diary_daily", "diary_rollup", "diary_public"),
        ("diary_chars", "diary_tokens", "diary_rollup_tokens")), ModulePolicy(
        ("diary_daily", "diary_rollup", "diary_public"),
        {"diary_daily": ("diary_promote",), "diary_rollup": ("diary_promote",), "diary_public": ("diary_promote",)}))
