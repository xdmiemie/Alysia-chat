# Stage 19 运行与验收

桌面应用在启动 Qwen3 或 GPT-SoVITS 训练前进入 `SWITCHING`，异步关闭它自己启动的 Qwen3 与 GPT-SoVITS 服务，确认端口释放后进入 `TRAIN_MODE`。如果通话、录音、聊天或其他训练正在进行，新的训练请求会被拒绝。训练进程退出后恢复 `USE_MODE`，重新加载并检查当前角色模型。关闭桌面应用时，运行中的训练先请求安全暂停；`configs/runtime/timeouts.yaml` 的 `training_stop_seconds` 到期后结束未响应的进程，保留此前的检查点。

SenseVoice 目前在应用进程内运行。`asr_seconds` 可检测返回过慢的识别调用并报错，但无法中断仍卡在底层模型中的调用；此限制需要在硬件长稳验收中重点观察。

推理服务每 30 秒在后台检查一次。异常退出后，10 分钟内最多自动重启 3 次；连续失败会提示用户检查日志并手动启动。模型切换失败时保留或回退到上一版本。启动时检查 `storage/state/last_session.json`、训练任务 JSON、active manifest 和过期 PID；未完成任务只提示继续或放弃，不自动恢复训练。损坏或缺少文件的 active manifest 会尝试回滚到上一有效版本。

日志位置：

- `storage/logs/core/events.jsonl`：Core 请求 ID、会话、角色、消息类型、ASR/LLM/TTS/总耗时、模型版本、状态和错误码。
- `storage/logs/ui/events.jsonl`：桌面聊天与语音气泡的实际 conversation ID、request ID、各阶段耗时和最终状态。
- `storage/logs/training/qwen3/events.jsonl` 与 `storage/logs/training/gpt_sovits/events.jsonl`：训练任务状态、配置、数据哈希、模型、进度、检查点和错误。
- `storage/logs/app/desktop.log`：桌面应用滚动日志；`storage/logs/qwen3/`、`storage/logs/gpt_sovits/` 存放推理子进程输出；`storage/training_jobs/` 保留每个任务的原始进程日志与可恢复状态。

自动验证：

```powershell
.venv\Scripts\python.exe -m pytest -q
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m desktop.main --no-startup --smoke-test-ms 1000
```

硬件长稳验收仍需在目标 RTX 5080 机器上运行：连续文本聊天 4 小时、语音通话 1 小时，比较进程内存和音频设备句柄的起止值。还应实际启动与暂停 Qwen3/GPT-SoVITS 训练，并在训练运行时尝试启动推理服务、强制结束桌面应用后重新启动，确认任务恢复提示与检查点可用。自动测试不替代这些持续运行测试。
