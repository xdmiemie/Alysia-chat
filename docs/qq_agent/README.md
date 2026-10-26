# QQ Agent：Stage 11 基线

本路线从 Stage 10 的 Core 完成提交 `aca495e801f061210ce4961da8df2f63ab3f3c44` 建立 `qq` 分支。该提交的父提交是 `ea2c67c6bf9001913e0ff57ce183be4eac791868`。原阶段计划和已有 CLI 保留；QQ 路线以仓库根目录的 `Alysia-chat_QQ_Agent_项目阶段计划.md` 为准。

## 本阶段交付

| 内容 | 位置 |
| --- | --- |
| 开发环境与回归命令 | [development.md](development.md) |
| Core 和训练模块入口 | [interfaces.md](interfaces.md) |
| 训练 UI 任务接口契约 | [training_jobs.md](training_jobs.md)、`runtime/training_manager/contracts.py` |
| 固定 20 条输入 | [fixed_text_inputs.txt](fixed_text_inputs.txt) |
| 验收结果与限制 | [baseline.md](baseline.md) |

Stage 11 不连接 QQ。Stage 12 的独立 OneBot 验证工具和真实验收步骤见 [stage12.md](stage12.md)。

Stage 13.1–13.4 的一条命令启动、QQ 文字桥接和本地配置见 [stage13.md](stage13.md)。
