# QQ Agent 开发环境

在 Windows PowerShell 中，从仓库根目录运行以下命令。统一解释器由 `quickstart/bootstrap.ps1` 准备；此机当前为 `runtime_env/python.exe`（Python 3.10.11）。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/bootstrap.ps1 -SkipLfsPull -SkipThirdParty -SkipModelDownloads
runtime_env/python.exe scripts/doctor.py
runtime_env/python.exe -m pytest -q tests/test_stage10_core.py tests/test_qq_stage11_baseline.py tests/test_qwen3_training.py tests/test_gpt_sovits_training.py
```

现有启动流程仍为 `quickstart/run.ps1` 和 `scripts/start_app.ps1`。完整实时语音启动需要可用的 Qwen3、SenseVoice、GPT-SoVITS 权重以及本地 GPU。诊断脚本会检查路径、CUDA、音频设备和端口，不会代替一次真实推理。

## 真实服务回归

`scripts/qq_stage11_baseline.py` 使用已启动的本机 Qwen3 `llama-server`（8080）和 GPT-SoVITS（9880）。可选 `--audio` 会加载 SenseVoice 并验证语音输入。脚本交替使用两个会话处理固定的 20 条文字输入，再重建 Core 验证重启。逐条结果和耗时写到被 Git 忽略的 `storage/logs/qq-stage11-baseline.json`，WAV 写到 `storage/generated_audio/qq_stage11_baseline/`。

```powershell
runtime_env/python.exe scripts/qq_stage11_baseline.py --audio path/to/input.wav
```

先运行 `runtime_env/python.exe scripts/doctor.py`，确认当前激活模型实际存在。不要将测试生成的音频、日志、登录资料或密钥加入 Git。

## 本机基线（2026-10-26，香港时间）

| 项目 | 记录 |
| --- | --- |
| 分支起点 | `aca495e801f061210ce4961da8df2f63ab3f3c44`（Stage 10） |
| GPU | NVIDIA GeForce RTX 5080，显存 16303 MiB，驱动 616.56 |
| Python | 3.10.11 |
| torch / CUDA | 2.11.0+cu128 |
| pytest / FunASR | 9.1.1 / 1.3.26 |
| transformers / peft | 4.50.0 / 0.17.1 |
| accelerate / datasets | 1.15.0 / 3.6.0 |

本地现有工作区还包含与本阶段无关的未提交文件；这些文件没有被 Stage 11 清理或提交。
