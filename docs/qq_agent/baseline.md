# QQ Stage 11 验收记录

记录日期：2026-10-26（香港时间）。分支 `qq` 从 Stage 10 提交 `aca495e801f061210ce4961da8df2f63ab3f3c44` 建立；`main` 当时也指向该提交。本记录只覆盖 Stage 11，未处理工作区原有的 Stage 19 改动。

## 已通过

| 项目 | 结果 |
| --- | --- |
| Core、Qwen3/GPT-SoVITS 训练回归 | `runtime_env/python.exe -m pytest -q tests/test_stage10_core.py tests/test_qq_stage11_baseline.py tests/test_qwen3_training.py tests/test_gpt_sovits_training.py`：25 passed |
| 固定文字输入 | 实际 Qwen3 基础模型 + GPT-SoVITS 服务处理 20/20 条，均生成可读取 WAV |
| 真实语音输入 | `stage13-live-check.wav` 经 SenseVoice 转写后完成 Qwen3 回复和 WAV 合成 |
| 两个会话隔离 | 固定输入测试交替运行两个 `session_id`，检查 Qwen3 客户端收到的历史无另一会话文字 |
| Core 重建 | 真实服务和固定测试均在重建 Core 后继续成功处理新请求；旧进程内历史按设计不保留 |
| Qwen3 旧 CLI | `quickstart/train_qwen3.ps1 -Character elysia -SkipNormalize -SkipSplit -DryRun -SkipConvert` 成功，生成 dry-run 检查点 |
| GPT-SoVITS 数据集 CLI | `dataset_manifest.py --json --no-write-normalized` 返回 `ready: true`、2128 条、0 错误 |
| 诊断与本地设备 | `scripts/doctor.py` 可运行；GPU、CUDA、FFmpeg、llama-server、音频设备、模型文件（除下面的活动 Qwen3）和端口检查正常 |
| 忽略规则 | `runtime_env_qwen/`、聊天数据库、本地桌面状态、训练任务日志、QQ 登录资料及本地 Token 的测试路径均被 `.gitignore` 忽略 |

真实回归命令：

```powershell
runtime_env/python.exe scripts/qq_stage11_baseline.py --audio storage/temporary/stage13-live-check.wav
```

逐条结果保存在本机 `storage/logs/qq-stage11-baseline.json`（Git 忽略）。运行时 Qwen3 使用单独启动的 `qwen3-8b-q4-k-m-base-v1`，没有修改活动模型配置。20 条文字流程的测量值（毫秒）：

| 指标 | 最小 | 中位数 | 最大 |
| --- | ---: | ---: | ---: |
| Qwen3 回复 | 429 | 582 | 794 |
| GPT-SoVITS 合成 | 1319 | 2357.5 | 4113 |
| Core 总耗时 | 1749 | 2985 | 4908 |

语音轮次的 ASR 耗时为 1003 毫秒。以上是这台机器和本次输入的基线，不是性能门槛。GPT-SoVITS 数据集验证有 2189 条警告，主要为采样率需要转换；验证仍报告 `ready: true`，正式训练前应处理报告中的警告。

## 待完成的启动验收

活动清单 `model_store/active/qwen3.json` 指向 Fish 的 `adapter_model.gguf`，本机该文件不存在，因此 `scripts/doctor.py --strict` 和不指定模型的默认启动流程无法通过。Elysia 注册表中有可用的基础模型和 LoRA；本次真实回归使用基础模型绕开了活动清单。活动模型属于持久用户配置，尚未切换。自动审批拒绝了切换该清单的命令，因为此前已说明保持活动配置不变；获得对此具体变更的授权后，才能修复并重跑严格诊断及默认启动验收。
