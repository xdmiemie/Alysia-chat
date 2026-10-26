# 现有模块入口索引

| 能力 | 实际入口 | 输入 → 输出 |
| --- | --- | --- |
| 统一消息 | `runtime/core/models.py:UnifiedMessage` | `local_text` / `local_audio` → 带 `session_id`、`character_id` 的消息 |
| Core | `runtime/core/service.py:AlysiaCore.process` | `UnifiedMessage` → `AlysiaReply`（文字、WAV 路径、状态、耗时） |
| 会话管理 | `runtime/conversation/session_manager.py:ChatSessionManager` | `session_id` → 独立的 `ChatSession` 实例；`clear()` 丢弃内存历史 |
| Qwen3 | `runtime/qwen3/service.py:Qwen3ChatSession.from_config` | 文字 → `CharacterReply`；内部使用 llama-server OpenAI 兼容接口 |
| SenseVoice | `runtime/sensevoice/service.py:SenseVoiceRecognizer.from_config` | WAV 路径 → `TranscriptionResult` |
| GPT-SoVITS | `runtime/gpt_sovits/service.py:GPTSoVITSSynthesizer.from_config` | `SpeechRequest` → `SpeechResult` WAV 字节 |
| 旧实时应用 | `runtime/app/main.py`、`scripts/start_app.ps1` | 麦克风 → Core → 扬声器 |
| 诊断 | `scripts/doctor.py`、`scripts/start_runtime.ps1 -Doctor` | 本地依赖、设备、端口和模型文件检查 |

`AlysiaCore` 当前即使输入是文字，也执行 TTS 并写 WAV；QQ Stage 13 若需要纯文字回复，应在适配层明确处理，不应假设 Core 的文字输入会跳过 TTS。Core 的 `ChatSessionManager` 只保存进程内会话；重启后的“恢复运行”是重新建立可用服务，不表示恢复旧对话历史。

## Stage 7/8 训练入口

| 流程 | CLI / 模块 | 主要产物 |
| --- | --- | --- |
| Qwen3 语料 | `training/qwen3/preprocess/{normalize,split,validate,status}.py` | 标准化、训练、验证 JSONL 和预处理状态 |
| Qwen3 LoRA | `quickstart/train_qwen3.ps1`、`training/qwen3/train/train_lora.py` | `training/qwen3/outputs/<character>/<run>/`，`run_manifest.json`、`checkpoint-*`、`adapter-final` |
| Qwen3 导出和激活 | `quickstart/convert_qwen3_lora_to_gguf.ps1`、`training/qwen3/export/{export_gguf,activate_model}.py` | GGUF、`export_log.json`、`model_store/active/qwen3.json` |
| GPT-SoVITS 音频准备 | `training/gpt_sovits/preprocess/dataset_manifest.py` | `work/manifest.jsonl`、`work/validation_report.json` |
| GPT-SoVITS S1/S2 | `quickstart/train_gpt_sovits.ps1`、`training/gpt_sovits/train/{train_s1,train_s2}.py` | `training/gpt_sovits/outputs/<character>/<run>/`，`run_manifest.json`、S1/S2 检查点 |
| GPT-SoVITS 试听与注册 | `training/gpt_sovits/export/{select_references,collect_weights}.py` | `references/`、权重注册、`export_log.json`；可用 `--activate` 激活 |

原 CLI 的命令行参数是兼容入口。未来 UI 通过训练任务契约启动 CLI 并观察产物，不直接修改训练脚本中的全局变量或参数。
