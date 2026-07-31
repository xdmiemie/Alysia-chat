from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.adapters.wechat_desktop.call_window_locator import (
    _snapshot_from_uia_window,
    _top_level_uia_windows,
    _trusted_wechat_call_snapshot,
)
from runtime.adapters.wechat_desktop.config import load_wechat_desktop_config
from runtime.adapters.wechat_desktop.process_locator import find_wechat_processes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely dump top-level windows for Stage 16 call debugging.")
    parser.add_argument("--config", default="configs/runtime/wechat.yaml")
    parser.add_argument("--output", default="storage/logs/wechat_desktop/call_windows_dump.jsonl")
    parser.add_argument("--limit-windows", type=int, default=80)
    parser.add_argument("--limit-descendants", type=int, default=500)
    parser.add_argument("--include-all-processes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_wechat_desktop_config(args.config)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    try:
        from pywinauto import Desktop
    except ImportError:
        print("Missing pywinauto; cannot dump UIA windows.", file=sys.stderr)
        return 1

    wanted_pids = {process.pid for process in find_wechat_processes(config.client.executable_names)}
    windows = _top_level_uia_windows(Desktop)

    for index, window in enumerate(windows[: args.limit_windows]):
        window_row = _control_row(window, index=index, level=0)
        pid = int(window_row.get("pid") or 0)
        call = _safe_call_snapshot(window, account_key=config.account.account_key)
        trusted_call = bool(
            call
            and _trusted_wechat_call_snapshot(call, process_ids=wanted_pids, process_id=pid)
        )
        if (
            wanted_pids
            and not args.include_all_processes
            and pid not in wanted_pids
            and not trusted_call
        ):
            continue
        window_row["stage16_call_match"] = call is not None
        window_row["stage16_trusted_call_match"] = trusted_call
        if call is not None:
            window_row["call"] = {
                "call_id": call.call_id,
                "title": call.title,
                "state": call.state,
                "type": call.call_type,
                "contact_display_text": call.contact_display_text,
                "buttons": sorted(call.buttons),
                "controls": call.controls,
            }
        rows.append(window_row)
        for child_index, child in enumerate(_safe_descendants(window)[: args.limit_descendants]):
            child_row = _control_row(child, index=child_index, level=1, parent_index=index)
            text_blob = f"{child_row.get('text', '')} {child_row.get('name', '')}".strip()
            if text_blob or child_row.get("control_type") in {"Button", "Window", "Pane", "Custom"}:
                rows.append(child_row)

    with output.open("w", encoding="utf-8") as dump:
        for row in rows:
            dump.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"WeChat process ids: {sorted(wanted_pids)}")
    print(f"Wrote {len(rows)} window/control rows to {output}")
    matches = [row for row in rows if row.get("stage16_trusted_call_match")]
    print(f"Stage 16 trusted call matches: {len(matches)}")
    for match in matches:
        print(json.dumps(match.get("call"), ensure_ascii=False))
    return 0


def _safe_call_snapshot(window, *, account_key: str):
    try:
        return _snapshot_from_uia_window(window, account_key=account_key)
    except Exception:
        return None


def _control_row(control, *, index: int, level: int, parent_index: int | None = None) -> dict[str, object]:
    rectangle = _rect(control)
    hwnd = _handle(control)
    return {
        "ts": int(time.time()),
        "index": index,
        "parent_index": parent_index,
        "level": level,
        "hwnd": hwnd,
        "pid": _pid(control, hwnd),
        "control_type": _control_type(control),
        "class_name": _class_name(control),
        "text": _window_text(control)[:300],
        "name": _name(control)[:300],
        "automation_id": _automation_id(control),
        "rect": rectangle,
        "visible": _bool_method(control, "is_visible", True),
        "enabled": _bool_method(control, "is_enabled", True),
    }


def _safe_descendants(window):
    try:
        return list(window.descendants())
    except Exception:
        return []


def _rect(control) -> list[int]:
    try:
        rect = control.rectangle()
        return [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
    except Exception:
        return []


def _handle(control) -> int:
    for getter in (
        lambda: getattr(control, "handle", 0),
        lambda: getattr(getattr(control, "element_info", None), "handle", 0),
    ):
        try:
            return int(getter() or 0)
        except Exception:
            continue
    return 0


def _pid(control, hwnd: int) -> int:
    try:
        return int(control.process_id() or 0)
    except Exception:
        pass
    try:
        return int(getattr(getattr(control, "element_info", None), "process_id", 0) or 0)
    except Exception:
        pass
    if hwnd <= 0:
        return 0
    try:
        import win32process

        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
        return int(pid or 0)
    except Exception:
        return 0


def _control_type(control) -> str:
    try:
        return str(getattr(control.element_info, "control_type", ""))
    except Exception:
        return ""


def _class_name(control) -> str:
    try:
        return str(control.class_name())
    except Exception:
        return ""


def _window_text(control) -> str:
    try:
        return str(control.window_text()).strip()
    except Exception:
        return ""


def _name(control) -> str:
    try:
        return str(getattr(control.element_info, "name", "")).strip()
    except Exception:
        return ""


def _automation_id(control) -> str:
    try:
        return str(getattr(control.element_info, "automation_id", "")).strip()
    except Exception:
        return ""


def _bool_method(control, name: str, default: bool) -> bool:
    method = getattr(control, name, None)
    if not callable(method):
        return default
    try:
        return bool(method())
    except Exception:
        return default


if __name__ == "__main__":
    raise SystemExit(main())
