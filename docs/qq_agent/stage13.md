# QQ Stage 13.1–13.4：文字聊天

## 启动

先在 QQ 客户端登录已配置给 SnowLuma 的账号。随后在项目根目录运行一条命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\quickstart\run_qq.ps1
```

也可双击 `quickstart/run_qq.cmd`。脚本复用或启动本地 SnowLuma，选择当前 Elysia Qwen3 模型并启动 llama-server，然后运行 QQ 文字桥接。启动不需要再手动复制或输入 OneBot Token；桥接进程直接读取 Git 忽略的 `third_party/SnowLuma/v2.1.1/config/onebot_<账号>.json` 中 Universal 正向 WebSocket 的 `accessToken`，只接受本机监听地址。Token 不进入命令行、应用日志或仓库。SnowLuma 的初次安装与 QQ 登录仍按阶段 12 的说明进行。

默认私聊白名单来自当前 QQ 好友列表。默认不回复任何群；要启用群，在 Git 忽略的 `configs/qq_agent/local.json` 中填写群号，默认只回复 @ 机器人的文字。可从 `configs/qq_agent/example.json` 复制配置，再按需设置 `private_users`、`disabled_users`、`disabled_groups`、`user_characters`、`group_characters`、消息长度、频率、队列和超时。`use_friend_list: false` 时仅允许 `private_users`。角色目录必须存在于 `configs/characters/`。

`Ctrl+C` 停止桥接并关闭项目 Qwen3 模型进程；SnowLuma 和 QQ 保持运行。SnowLuma 进程可用 `scripts/stop_snowluma.ps1` 停止。桥接断线后指数退避重连，重新读取本地 Token 文件并检查 `get_status`、`get_login_info`、`get_friend_list`。

## 状态与测试

`UnifiedMessage` 保留源消息 ID、发送者、时间和消息段。私聊与群聊使用独立 Session ID；Qwen3 会话在进程内保存上下文，QQ 文字回复不启动 TTS。消息状态写入 `storage/qq/messages.sqlite3`，以 `(bot_id, message_id)` 去重。状态包括 `received`、`queued`、`generating`、`pending_send`、`sent`、`failed`、`unknown`。崩溃时处于发送阶段的消息转为 `unknown`，不会自动重发；数据库可能包含聊天正文，应按私人数据保护。

Mock 测试覆盖消息转换、私聊与群聊规则、20 条队列消息、Core 的 20 轮文字上下文、发送后状态及重启去重。真实 QQ 端到端对话需要在账号已登录时验证；当前测试不将模拟结果视作真实 QQ 收发验收。

## Stage 13A–B：逐条 Agent 与回复时机

QQ 桥接现在从同一 Qwen3 请求取得结构化动作：`send_message`（候选回复、是否继续、速度档）或 `finish_turn`（不发消息并等待用户）。每个候选回复按句末标点拆成独立 QQ 消息，例如“故事嘛……让我想想看。那个关于火焰、命运和羁绊的故事……很长很长哦。”会分两次发送；省略号留在所在句内。`call_tool` 在 Stage 13D 前一律拒绝。候选回复的所有句子逐条取得 OneBot 回执后，才按 `continue_turn` 决定是否再次调用模型；继续时上下文包含实际已发送的每一句。每个会话有版本号；新消息进入后，旧回合的未发送句子会取消。单回合默认最多 3 条实际 QQ 消息、累计 500 字、单次候选最多 250 字、总计 120 秒。结构无效最多重试一次，然后结束本回合；服务端拒绝 JSON Schema 时才启用纯文本降级。

持久化数据库增加 `decisions` 和 `outbound_messages`，分别记录动作、原因、速度、推理时间、服务端 Token 用量，以及每条消息的准备时间、计划发送时间、状态和独立回执。未知发送结果不自动重试。机器人自身消息、非白名单、群内未 @、禁用对象、限频、重复事件、忙碌状态和明确“别回复了”在调用 Qwen3 前拦截。默认启用模型自行决定是否回复；`always_reply` 仅供测试，仍服从代码门禁。

在仓库根目录运行 `python scripts/qq_settings.py` 可打开本地 QQ 设置窗口，编辑四档**思考时间**、模式、思考时间上限和单回合条数。保存到 Git 忽略的 `configs/qq_agent/local.json`，重启桥接后生效；其余白名单配置也会保留。不开窗口时可参考 `configs/qq_agent/example.json` 手动编辑。每一句另加固定打字时间：每个中文汉字 0.4 秒，其他每个字符（含英文、空格、标点和符号）0.05 秒。首句以收到用户消息为起点，后续句子以上一句成功发送为起点；计划时间为 `max(起点 + 思考时间 + 打字时间, 到达延迟计算代码的时间)`。模型推理和其余程序运行耗时已在两时间点之差中抵扣，超过目标时间就立即进入发送。关闭拟人延迟会同时关闭思考和打字等待。旧配置中的 `continuation` 区间会被忽略。

例如“故事嘛……让我想想看。”有 8 个汉字、3 个标点，打字时间为 `8 × 0.4 + 3 × 0.05 = 3.35` 秒；若 normal 思考时间抽到 1.7 秒，目标总时间就是 5.05 秒。到达延迟计算代码时若已经过去 2 秒，仅再等约 3.05 秒；若已经过去 6 秒则直接发送。

本地模型回归可运行：

```powershell
python -m pytest -q tests/test_qq_stage13_chat.py tests/test_qq_stage13ab.py
python scripts/qq_stage13_benchmark.py --repetitions 20
python scripts/qq_stage13_decision_eval.py
```

后两项要求本机 Qwen3 `llama-server` 已启动，不发送 QQ 消息。结果写入 `storage/logs/`。决策评估脚本包含 100 条参考期望，用于发现收尾与误拒答问题；这些期望尚未经过真人标注审核，不能替代计划中的正式人工标注验收。当前 QQ 文字链路不调用 TTS，逐条语音播报仍需 Stage 14 接入。真实 QQ 收发也需要已登录账号现场验证。

### 本机采样（2026-10-26，RTX 5080，Qwen3 8B GGUF + LoRA）

每类 20 次，已加载模型常驻，时间只含模型决策，不含 QQ 网络和人为延迟：

| 场景 | 决策调用 | P50 / P95 | 平均输入 / 输出 Token |
| --- | ---: | ---: | ---: |
| 单条问候 | 1 | 730 / 856 ms | 940 / 58 |
| 社交回复 | 19/20 次为 2 步 | 1318 / 1405 ms | 1912 / 107 |
| 长问题 | 1 | 790 / 907 ms | 960 / 69 |
| 打断语境 | 1 | 594 / 707 ms | 952 / 53 |

采样期间整卡显存最高 7443 MiB（包含其他桌面进程）；`llama-server` 返回的首 Token *计算时间估计* P95 为 24–30 ms，这不是用户端首字到达时间。100 条项目内参考样本调参后正确 99 条、误拒答 0 条；此集合已用于提示优化，不能代表独立泛化正确率。多步回复的输入 Token 因重复带入原问题与已发送句子而增加，符合逐段决策的成本预期。表中时间只含模型决策；按字符计算的交付等待另计。

单独运行 10,000 次 JSON 动作校验与速度抽样，本机 P95 为 0.003 ms；此数字只衡量两项纯代码操作，不包括数据库、事件调度与系统负载，不能当作完整桥接控制逻辑的 P95。
