"""Operator model changes executed by the existing process supervisor."""

from __future__ import annotations

import json
from pathlib import Path
import re
import time
import uuid

from integrations.qq_astrbot.plugin.control_store import ControlStore

# Paths, network bindings and arbitrary command arguments are never UI inputs.
MODEL_FIELDS = {
    "context_size": ("上下文容量", int, 4096, 131072),
    "gpu_layers": ("GPU 层数", int, 0, 999),
    "prompt_cache_mib": ("主机提示词缓存 MiB", int, 0, 2048),
    "image_min_tokens": ("每张图最少 token", int, 1, 4096),
    "image_max_tokens": ("每张图最多 token", int, 1, 4096),
    "max_tokens": ("单次最大输出 token", int, 64, 32768),
    "context_margin_tokens": ("上下文预留 token", int, 0, 8192),
    "reasoning_budget_tokens": ("思考 token 预算", int, 0, 32768),
    "temperature": ("主回复温度", float, 0, 2),
    "utility_temperature": ("后台任务温度", float, 0, 2),
    "result_temperature": ("结果通知温度", float, 0, 2),
    "top_p": ("Top P", float, 0.01, 1),
    "top_k": ("Top K", int, 0, 1000),
    **{
        key: (name, bool, None, None)
        for key, name in {
            "mmproj_offload": "视觉投影使用 GPU",
            "enable_thinking": "启用思考",
            "thinking_agent": "主回复思考",
            "thinking_selection": "选歌思考",
            "thinking_followup": "追加回复思考",
            "thinking_music_result": "结果通知思考",
            "thinking_compression": "压缩思考",
        }.items()
    },
    "cache_type_k": ("K 缓存精度", str, None, None),
    "cache_type_v": ("V 缓存精度", str, None, None),
}


def validate_model(root, patch):
    from dataclasses import replace
    from runtime.qwen3.config import load_qwen3_config

    if not isinstance(patch, dict) or set(patch) - MODEL_FIELDS.keys():
        raise ValueError("不支持的模型参数")
    for key, value in patch.items():
        _, kind, low, high = MODEL_FIELDS[key]
        if kind in {int, bool, str} and type(value) is not kind:
            raise ValueError(f"{key} 类型不正确")
        if kind is float and (type(value) not in {int, float}):
            raise ValueError(f"{key} 必须为数值")
        if low is not None and not low <= value <= high:
            raise ValueError(f"{key} 应在 {low}–{high} 之间")
    return replace(
        load_qwen3_config(Path(root) / "configs/runtime/qwen3.yaml"), **patch
    )


def write_model(path, patch):
    text = path.read_text(encoding="utf-8-sig")
    for key, value in patch.items():
        scalar = str(value).lower() if isinstance(value, bool) else str(value)
        text, n = re.subn(
            r"^" + re.escape(key) + r":[^\n]*",
            lambda _: key + ": " + scalar,
            text,
            flags=re.M,
        )
        if not n:
            text += f"\n{key}: {scalar}\n"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class ModelControl:
    def __init__(self, root, directory):
        self.root, self.directory = Path(root), Path(directory)
        self.store = ControlStore(directory)
        self.started = None
        self.job = None

    def recover(self):
        with self.store.connection() as db:
            interrupted = db.execute(
                "SELECT * FROM model_jobs WHERE state IN ('draining','restarting')"
            ).fetchall()
            if not interrupted:
                return
            for row in interrupted:
                previous = json.loads(row["payload"]).get("before")
                if row["state"] == "restarting" and previous is not None:
                    path = self.root / "configs/runtime/qwen3.yaml"
                    tmp = path.with_suffix(".recover.tmp")
                    tmp.write_text(previous, encoding="utf-8")
                    tmp.replace(path)
            db.execute(
                "UPDATE model_jobs SET state='rolled_back',error='监督器重启中断任务，已恢复原配置',updated=? "
                "WHERE state IN ('draining','restarting')",
                (time.time(),),
            )

    def submit(self, patch):
        validate_model(self.root, patch)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM model_jobs WHERE state IN ('queued','draining','restarting')"
            ).fetchone():
                raise ValueError("已有模型重启任务，请等待其完成")
            key, now = uuid.uuid4().hex, time.time()
            payload = {
                "changes": patch,
                "before": (self.root / "configs/runtime/qwen3.yaml").read_text(
                    encoding="utf-8-sig"
                ),
            }
            db.execute(
                "INSERT INTO model_jobs VALUES(?,'queued',?,'',?,?)",
                (key, json.dumps(payload), now, now),
            )
        return key

    def state(self, state, error=""):
        with self.store.connection() as db:
            db.execute(
                "UPDATE model_jobs SET state=?,error=?,updated=? WHERE id=?",
                (state, error, time.time(), self.job["id"]),
            )

    def step(self, switch, media, foreground):
        if self.job is None:
            if media.mode() != "ready" or foreground.blocked():
                return
            with self.store.connection() as db:
                row = db.execute(
                    "SELECT * FROM model_jobs WHERE state='queued' ORDER BY created LIMIT 1"
                ).fetchone()
            if row is None:
                return
            self.job, self.started = dict(row), time.monotonic()
            media.set_mode("draining")
            self.state("draining")
        if media.active():
            if time.monotonic() - self.started > 180:
                self.state("failed", "等待在途任务结束超时；原配置保留")
                media.set_mode("ready")
                self.job = None
            return
        path = self.root / "configs/runtime/qwen3.yaml"
        previous = path.read_bytes()
        stopped = []
        try:
            payload = json.loads(self.job["payload"])
            if payload["before"] != previous.decode("utf-8-sig").replace("\r\n", "\n"):
                self.state("failed", "等待期间模型配置被修改，请刷新后重新提交")
                return
            patch = payload["changes"]
            validate_model(self.root, patch)
            self.store.audit("model", "qwen3", previous.decode("utf-8-sig"))
            self.state("restarting")
            switch.stop(stopped)
            write_model(path, patch)
            media.set_mode("restoring")
            switch.restore(["qwen3"])
            applied = self.directory / "model_settings.applied.json"
            tmp = applied.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"job": self.job["id"], "updated": time.time()}),
                encoding="utf-8",
            )
            tmp.replace(applied)
            self.state("completed")
        except Exception as exc:
            path.write_bytes(previous)
            try:
                # Force any partially started new model to stop before rollback.
                for entry in list(switch.children):
                    if entry["name"] == "qwen3":
                        switch.stop_entry(entry)
                        switch.children.remove(entry)
                switch.write_registry()
                switch.restore(["qwen3"])
                self.state(
                    "rolled_back", f"新参数启动失败，已恢复旧参数：{type(exc).__name__}"
                )
            except Exception as rollback:
                self.state(
                    "failed", f"恢复失败：{type(rollback).__name__}；请检查模型日志"
                )
                media.set_mode("recovering")
                self.job = None
                raise RuntimeError("Model rollback failed") from rollback
        finally:
            if self.job is not None:
                media.set_mode("ready")
                self.job = None
