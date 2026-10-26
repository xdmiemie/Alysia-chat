# QQ Stage 12：SnowLuma / OneBot v11 验证

本阶段只验证 QQ 文字收发，不启动 Qwen3 或其他模型。项目启动脚本可以启动 QQ 和 SnowLuma；账号登录、协议确认及 SnowLuma WebUI 配置仍由用户在可见窗口完成。

## 外部软件与许可

截至 2026-10-26，SnowLuma 官方 [Releases](https://github.com/SnowLuma/SnowLuma/releases) 提供 `v2.1.1` 的 Windows x64 完整包 `SnowLuma-v2.1.1-win-x64.zip`。项目本地安装目录为 Git 忽略的 `third_party/SnowLuma/v2.1.1/`。发行包中的 [EULA 第 5.4 条](https://github.com/SnowLuma/SnowLuma/blob/main/EULA.md) 对通过自动化脚本部署其专有组件要求事先书面授权，因此项目不会自动下载或解压 SnowLuma。请阅读发行包的 `EULA.md` 和 `PRIVACY.md`，再首次启动。发行包和 QQ 登录数据不会进入 Git。

如需重新下载，在项目根目录的 PowerShell 中执行；如果 ZIP 已存在，可跳过。下载后请在文件资源管理器中自行解压到上述本地目录：

```powershell
$zip = 'E:\Alysia-chat\storage\temporary\SnowLuma-v2.1.1-win-x64.zip'
New-Item -ItemType Directory -Force -Path (Split-Path $zip) | Out-Null
Invoke-WebRequest 'https://github.com/SnowLuma/SnowLuma/releases/download/v2.1.1/SnowLuma-v2.1.1-win-x64.zip' -OutFile $zip
```

之后用 `powershell -NoProfile -ExecutionPolicy Bypass -File .\quickstart\run_qq.ps1` 一键启动 QQ、已安装的 SnowLuma 和现有应用依赖服务。双击 `quickstart/run_qq.cmd` 也可以启动。启动过程只使用本地依赖，不运行 bootstrap 或 `pip install`；SnowLuma 的远程版本检查已关闭，自动注入 QQ 已开启。如首次安装仍缺 Python 或模型依赖，先按 [开发环境说明](development.md) 手动完成依赖准备。只运行阶段 12 验证时，可执行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_snowluma.ps1`，然后运行下方探针。停止项目启动的 SnowLuma 用 `scripts/stop_snowluma.ps1`；不会退出 QQ。

如果 WebUI「进程注入」持续显示「等待连接」，先检查自动注入设置、QQ 是否已登录，以及 OneBot 的 `3000`/`3001` 端口是否监听。2026-10-26 本机曾出现端口未监听，但随后同一 QQ `9.9.19.35469` 与 SnowLuma `v2.1.1` 组合已通过真实收发验证，因此不能仅凭版本判断不兼容。项目启动脚本不会检查或下载 QQ 更新。

用户在 QQ 客户端手动登录，在 SnowLuma WebUI（默认 `http://127.0.0.1:5099/`）完成账号接入。按[官方连接指南](https://snowluma.github.io/en/docs/guide/connect-bot)启用正向 Universal WebSocket，默认地址 `ws://127.0.0.1:3001/`；本机 HTTP 端口默认 `3000`，用于区分 SnowLuma 不可达和 WebSocket 未就绪。只监听本机地址。WebSocket Token 从 WebUI 或实例配置取得，**不要写入仓库、命令行参数或测试日志**。

部署记录（2026-10-26 本机实测）：

| 项目 | 实测值 |
| --- | --- |
| SnowLuma 版本 | 项目本地包 `v2.1.1`，运行进程位于该目录 |
| QQ 客户端版本 | `D:\QQ\QQ.exe`，`9.9.19.35469` |
| OneBot 接口 | 正向 Universal WebSocket，探针连接成功 |
| WebSocket 地址 | `ws://127.0.0.1:3001/`，本机监听 |
| HTTP 端口 | `3000`，本机监听 |
| SnowLuma 日志路径 | `third_party/SnowLuma/v2.1.1/logs/snowluma-2026-10-26.log` |
| Token | 已配置并通过探针认证；不记录值 |
| 真实收发 | 探针 `READY`、发送成功、接收 20 条；用户确认电脑可收发 QQ 消息 |

## 本地测试客户端

`quickstart/bootstrap.ps1` 会安装 `websockets>=12,<17`。也可以只在当前 Python 环境中安装 `runtime/qq_adapter/requirements.txt`。在 **普通 PowerShell** 中运行，`--prompt-token` 会隐藏输入；切勿把 Token 贴到聊天、终端历史或提交到 Git：

```powershell
.\runtime_env\python.exe scripts/qq_stage12_probe.py --prompt-token
```

默认命令做 `get_login_info` 与 `get_status` 诊断。用户确认目标是好友账号后，可发送一条测试消息，同时等待 20 条真实消息：

```powershell
$FriendQq = 123456789 # 替换为已确认的好友 QQ 号
.\runtime_env\python.exe scripts/qq_stage12_probe.py --prompt-token --send-to $FriendQq --receive 20 --receive-timeout 180
```

测试客户端记录每条消息的类型、发送者、群号、消息 ID 和消息段数量；不会写入消息正文或 Token。结果写在 Git 忽略的 `storage/logs/qq-stage12-probe.json`。如需改端口，使用 `--ws-url ws://127.0.0.1:<端口>/` 与 `--http-port <端口>`。

状态码：

| 状态码 | 判定 | 操作 |
| --- | --- | --- |
| `SNOWLUMA_UNREACHABLE` | WebSocket 和 HTTP 端口均不可达 | 启动 SnowLuma，检查端口 |
| `ONEBOT_NOT_READY` | WebSocket 不可用、认证失败、接口异常或 `get_status.good` 非真 | 检查正向 WebSocket、Token、账号挂载 |
| `QQ_NOT_LOGGED_IN` | `get_status.online` 非真或没有登录账号 ID | 在 QQ 手动登录并刷新 SnowLuma |
| `READY` | `get_login_info` 有账号 ID，`get_status.online` 与 `good` 均为真 | 进行真实文字收发 |

当前连接不会无限重连：诊断默认最多三次，每次间隔一秒；单次 API 请求有五秒超时。接收阶段断线会结束并报告失败。端口探测只是辅助判断，如果 HTTP 服务被单独关闭，`SNOWLUMA_UNREACHABLE` 不能单凭该结果证明 SnowLuma 进程不存在。

## 阶段门禁

Mock WebSocket 测试覆盖 20 条事件、乱序 `echo` 关联、动作失败、超时、断线和登录状态。2026-10-26 的真实探针报告 `storage/logs/qq-stage12-probe.json` 记录 `READY`、好友文字发送成功、接收 20 条；QQ/SnowLuma 版本已记录。Stage 12 验收通过，可以进入 Stage 13 Core 集成。
