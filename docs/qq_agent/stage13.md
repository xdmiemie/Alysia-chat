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
