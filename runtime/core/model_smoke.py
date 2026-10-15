"""Short runtime load checks used before an interactive model switch."""

from __future__ import annotations


def smoke_test_qwen(model) -> None:
    from runtime.qwen3.server_manager import qwen_server_running, start_qwen_server, stop_qwen_server

    if qwen_server_running():
        raise RuntimeError("请先关闭正在运行的 Qwen3 服务")

    started = False
    try:
        start_qwen_server(model, timeout_seconds=120, skip_activation=True)
        started = True
    finally:
        if started and not stop_qwen_server():
            raise RuntimeError("Qwen3 测试服务未能正常关闭")


def smoke_test_voice(model) -> None:
    from runtime.gpt_sovits.server_manager import gpt_sovits_server_running, start_gpt_sovits_server, stop_gpt_sovits_server

    if gpt_sovits_server_running():
        raise RuntimeError("请先关闭正在运行的 GPT-SoVITS 服务")

    started = False
    try:
        start_gpt_sovits_server(model, timeout_seconds=180)
        started = True
    finally:
        if started and not stop_gpt_sovits_server():
            raise RuntimeError("GPT-SoVITS 测试服务未能正常关闭")
