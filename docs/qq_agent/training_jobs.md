# 训练任务接口规范（Stage 11）

`runtime/training_manager/contracts.py` 定义桌面 UI 的 `TrainingJobController` 协议与 `TrainingJob` 快照。Stage 11 只固定边界，进程管理和界面实现在后续训练 UI 阶段完成。

| 方法 | 约定 |
| --- | --- |
| `create(kind, character_id, config_path)` | 校验输入并登记任务，返回唯一 `job_id` 和 `created` 状态，不启动训练。`kind` 为 `qwen3` 或 `gpt_sovits`。 |
| `start(job_id)` | 以固定配置启动原 CLI，记录进程、标准输出和标准错误日志；状态转为 `running`。 |
| `stop(job_id)` | 请求终止当前任务并等待进程退出；状态先为 `stopping`，最终为 `stopped` 或 `failed`。保留检查点。 |
| `resume(job_id, checkpoint=None)` | 使用旧 CLI 的 `--resume-from`（空值使用其 `auto` 策略）恢复，不修改脚本内变量。 |
| `status(job_id)` | 返回任务快照；UI 依据状态、日志路径、错误、模型 ID 展示进度。 |
| `activate(job_id, model_id)` | 仅在训练与验收成功、注册产物存在后调用现有模型激活入口。 |
| `rollback(job_id)` | 恢复激活前记录的模型版本；若无先前版本，报告明确错误。 |

状态流为 `created → running → completed/failed`，运行中停止为 `running → stopping → stopped/failed`；恢复从 `stopped/failed` 回到 `running`。实现者须持久化任务快照、记录 PID 和退出码，并限制同一任务重复启动。状态字段不能仅根据日志文本猜测。

## 输入、产物与日志

| 类型 | 输入 | 产物与状态来源 | 日志位置 |
| --- | --- | --- | --- |
| Qwen3 | 角色训练 YAML、原始 JSONL、基础 HF 模型 | 预处理状态 JSON、`run_manifest.json`、`checkpoint-*`、`adapter-final`、`export_log.json`、注册表 | 控制器的 `stdout_log` / `stderr_log`；默认训练目录 `training/qwen3/outputs/<character>/<run>/` |
| GPT-SoVITS | 角色训练 YAML、原始音频、基础 S1/S2 权重 | `work/validation_report.json`、`run_manifest.json`、S1/S2 检查点、`references/`、`export_log.json` | 控制器的 `stdout_log` / `stderr_log`；默认训练目录 `training/gpt_sovits/outputs/<character>/<run>/` |

控制器日志建议放在 `storage/training_jobs/<job_id>.{out,err}.log`，任务快照放在同目录。该目录已被 Git 忽略。已有 CLI 不负责提供 `create/start/stop/status/rollback` 全部语义；控制器实现这些操作，并保留 `quickstart/train_qwen3.ps1`、`quickstart/train_gpt_sovits.ps1` 作为原有命令行入口。
