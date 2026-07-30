from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.adapters.wechat_desktop.client import _find_visible_weixin_hwnd, _open_weixin_by_visible_window


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dump the visible Windows WeChat UIA tree for Stage 11 debugging.")
    parser.add_argument("--output", default="storage/logs/wechat_desktop/ui_dump.jsonl")
    parser.add_argument("--text-output", default="storage/logs/wechat_desktop/ui_text_dump.jsonl")
    parser.add_argument("--limit", type=int, default=3000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    window = _open_weixin_by_visible_window()
    if window is None:
        print("Could not find visible WeChat window.")
        return 1
    selected_hwnd = _find_visible_weixin_hwnd()
    try:
        rectangle = window.rectangle()
        selected_rect = [rectangle.left, rectangle.top, rectangle.right, rectangle.bottom]
    except Exception:
        selected_rect = []

    output = Path(args.output)
    text_output = Path(args.text_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    text_output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    text_rows = []
    roots = [window, *window.descendants()[: args.limit]]
    for index, item in enumerate(roots[: args.limit]):
        try:
            rectangle = item.rectangle()
            hwnd = int(getattr(item, "handle", 0) or 0)
            row = {
                "index": index,
                "hwnd": hwnd,
                "pid": _pid_for_hwnd(hwnd),
                "control_type": str(getattr(item.element_info, "control_type", "")),
                "class_name": str(item.class_name()),
                "text": str(item.window_text()).strip()[:300],
                "name": str(getattr(item.element_info, "name", "")).strip()[:300],
                "rect": [rectangle.left, rectangle.top, rectangle.right, rectangle.bottom],
            }
            rows.append(row)
            if row["text"] or row["name"]:
                text_rows.append(row)
        except Exception as exc:
            rows.append({"index": index, "error": str(exc)})

    with output.open("w", encoding="utf-8") as dump:
        for row in rows:
            dump.write(json.dumps(row, ensure_ascii=False) + "\n")
    with text_output.open("w", encoding="utf-8") as dump:
        for row in text_rows:
            dump.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Selected hwnd: {selected_hwnd}")
    print(f"Selected rect: {selected_rect}")
    print(f"Wrote {len(rows)} UI rows to {output}")
    print(f"Wrote {len(text_rows)} text UI rows to {text_output}")
    if len(rows) <= 2:
        print(
            "Only the WeChat render surface is visible to UI Automation. "
            "Try scripts/prepare_wechat_accessibility.ps1 -StartNarrator and restart/re-login WeChat. "
            "If this remains unchanged, this account/version is not UIA-readable here."
        )
    return 0


def _pid_for_hwnd(hwnd: int) -> int:
    if hwnd <= 0:
        return 0
    try:
        import win32process
    except ImportError:
        return 0
    try:
        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        return 0
    return int(pid or 0)


if __name__ == "__main__":
    raise SystemExit(main())
