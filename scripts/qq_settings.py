"""Local QQ timing and turn settings window. Saves the existing policy JSON."""

from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.qq_adapter.chat import ChatPolicy, TimingPolicy


LOCAL = ROOT / "configs/qq_agent/local.json"
EXAMPLE = ROOT / "configs/qq_agent/example.json"


def main() -> None:
    source = LOCAL if LOCAL.exists() else EXAMPLE
    raw = json.loads(source.read_text(encoding="utf-8-sig"))
    timing = dict(raw.get("timing", {}))
    root = tk.Tk()
    root.title("QQ 回复设置")
    frame = ttk.Frame(root, padding=16)
    frame.grid(sticky="nsew")
    values: dict[str, tk.StringVar] = {}

    def field(row: int, label: str, key: str, value: object) -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
        variable = tk.StringVar(value=str(value))
        ttk.Entry(frame, textvariable=variable, width=20).grid(row=row, column=1, sticky="ew")
        values[key] = variable

    enabled = tk.BooleanVar(value=timing.get("enabled", True))
    ttk.Checkbutton(frame, text="启用拟人延迟", variable=enabled).grid(row=0, column=0, columnspan=2, sticky="w")
    mode = tk.StringVar(value=timing.get("mode", "smart"))
    ttk.Label(frame, text="模式").grid(row=1, column=0, sticky="w")
    ttk.Combobox(frame, textvariable=mode, values=("smart", "random"), state="readonly", width=17).grid(row=1, column=1)
    for row, key in enumerate(("instant", "quick", "normal", "thoughtful"), start=2):
        pair = timing.get(key, getattr(TimingPolicy(), key))
        field(row, key + "（最小,最大 秒）", key, f"{pair[0]},{pair[1]}")
    field(6, "最大思考时间（秒）", "max_delay", timing.get("max_delay", 8))
    field(7, "单回合最多消息", "max_turn_messages", raw.get("max_turn_messages", 3))
    always = tk.BooleanVar(value=raw.get("always_reply", False))
    ttk.Checkbutton(frame, text="测试模式：始终回复", variable=always).grid(row=8, column=0, columnspan=2, sticky="w")

    def save() -> None:
        try:
            updated = dict(raw)
            updated["max_turn_messages"] = int(values["max_turn_messages"].get())
            updated["always_reply"] = always.get()
            updated["timing"] = {"enabled": enabled.get(), "mode": mode.get(),
                                 "max_delay": float(values["max_delay"].get())}
            for key in ("instant", "quick", "normal", "thoughtful"):
                updated["timing"][key] = [float(item.strip()) for item in values[key].get().split(",")]
            # Validate all fields through the same loader used by the bridge.
            preview = LOCAL.with_suffix(".preview.json")
            try:
                preview.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                ChatPolicy.load(preview)
                preview.replace(LOCAL)
            finally:
                preview.unlink(missing_ok=True)
        except (ValueError, TypeError, OSError) as exc:
            messagebox.showerror("设置无效", str(exc))
            return
        messagebox.showinfo("已保存", "设置已保存；重新启动 QQ 桥接后生效。")

    ttk.Button(frame, text="保存", command=save).grid(row=9, column=0, columnspan=2, pady=12)
    root.mainloop()


if __name__ == "__main__":
    main()
