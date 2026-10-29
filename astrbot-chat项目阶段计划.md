# astrbot-chat：基于 Stage 10 核心的 AstrBot + SnowLuma QQ AI 与抖音学习计划

> 计划版本：1.2（2026-10-28，修订：项目命名、Qwen3-14B、Bailu 语音、Feiyu RVC 与目录）  
> 上游来源：<https://github.com/xdmiemie/Alysia-chat>；本地项目和分支名：`astrbot-chat`  
> Stage 10 基线：`aca495e801f061210ce4961da8df2f63ab3f3c44`；当前分支：`astrbot-chat`  
> 目标平台：Windows 11（本项目模型运行及训练）、SnowLuma Windows 版 + NTQQ、AstrBot 独立 Python 3.12 环境；运行在同一台电脑；后续可以将 AstrBot 改为 Docker/WSL。  
> 现有硬件基线：Ryzen 7 9800X3D / RTX 5080 16GB / 32GB RAM；视觉识别允许较长等待时间，优先保证单卡独占推理稳定性。  
> 范围：QQ 私聊/群聊、文字/语音/媒体理解、点歌/翻唱、每日角色日记、条件启用 QQ 空间、辅助性的抖音浏览—理解—记忆—定向分享。**不做无人监督支付、批量营销或验证码绕过。**

## 版本 1.2 变更摘要

- 对话主模型改为本地 `Qwen3-14B-Q4_K_M.gguf`；旧 8B LoRA 与 14B 基座不兼容，当前运行清单只激活 14B 基座。保留 LoRA 训练与导出代码，后续用 14B 重新微调。
- elysia 的 GPT-SoVITS 指定 `model_store/registry/gpt_sovits/elysia/bailu-v1/`，使用 `Bailu-e10.ckpt`、`Bailu_e10_s820.pth` 和该目录的四种参考音频。`gentle` 映射 neutral，`serious` 映射 sad。
- 翻唱指定 `model_store/registry/rvc/feiyu/v1/` 的 `FeiyuVoice-RVC-v1.pth` 与同名 `.index`；推理环境和 Applio 源码在 `envs/song/`、`external/Applio/`，不提交模型权重。
- 移除 fish 角色与旧 TTS 模型的活动目录；旧 TTS 权重本机移到 `model_store/rollback/`。QQ 的历史 Stage 14A 部分代码归档于 `integrations/qq_astrbot/reference/`，供后续 AstrBot 插件迁移使用。

## 版本 1.1 变更摘要（保留）

- 明确同一个 Qwen3-VL-4B 视觉进程承担普通 QQ 图片、静态表情包、GIF 抽帧、抖音视频 FFmpeg 抽帧的识别。
- 明确识图推理**宁可耗时较久，也必须先停掉本项目其他所有 GPU 模型**，经过显存检查后独占运行，结束后恢复原聊天/语音服务；训练不可在无检查点时强制终止。
- 增加 Q2 提前接入 GPUResourceBroker、Q7 正式独占 Gate、Q10 压力测试与故障恢复验收；抖音视频分批抽帧避免频繁模型热切换。
- 根据 Ryzen 7 9800X3D、RTX 5080 16GB、32GB RAM，主选官方 Qwen3-VL-4B-Instruct-GGUF Q4_K_M + mmproj，保留 BF16 作为经实测才启用的备选。

## 0. 结论、能力边界与总难度

### 0.1 总结

- **整体可实现，但不是把几个开源仓库装好就自动得到全部功能。** QQ 基础收发难度较低；模型服务整合、音频、日记、记忆为中等难度；本地视频视觉理解、真正“自动刷抖音”和持续维护网页登录最难。
- **AstrBot 作为唯一的 QQ 消息入口、Agent Tool 调度器和会话主控**；**SnowLuma 只负责 QQ 协议层**；**astrbot-chat 负责已有模型、人格资产、训练管线及自建服务**。禁止让 AstrBot 和旧 `AstrbotChatCore.process()` 在同一条消息上各运行一轮独立 LLM、各记一份聊天历史。
- **“学习”第一版专指经过过滤、归纳、检索的持久化记忆（RAG），不自动修改 Qwen3 模型权重。** 现有 Qwen3 / GPT-SoVITS 训练模块作为人工发起、离线评估、可回滚的独立工作流继续保留。
- **QQ 空间严格条件启用**：SnowLuma 确认提供 `get_cookies`；Zhalslar 插件需要 `get_cookies(domain="user.qzone.qq.com")`。说明 API 形状匹配，**不是实机兼容已验证**。只有通过针对 SnowLuma 的端到端 Cookie/读取/发布/评论/点赞测试，才实现空间功能；任一必要前提未通过，整项功能按用户要求不实施，不擅自切 NapCat。
- 语音回复用现有 GPT-SoVITS；**唱歌/AI 翻唱另外需要人声分离 + RVC/SVC/SoulX-SVCVC + 歌曲许可与模型权重**，GPT-SoVITS 的 TTS 不能直接替代歌唱模型。
- **视觉能力唯一入口**：QQ 图片、静态/GIF 表情包、抖音短视频关键帧**都调用同一个 `Qwen3-VL-4B-Instruct` 模型与 Vision API**；视频先经 FFmpeg 抽为若干时间标记图片，不引入另一套视频理解大模型。视频音轨另用已有 SenseVoice，二者分时运行。
- **单卡硬隔离**：每次进入视觉推理模式前，必须排空在途 GPU 任务并**停止/卸载本项目控制的文本 Qwen3、GPT-SoVITS、GPU SenseVoice、RVC/SVC 等全部 GPU 推理进程**，核实释放显存后才加载 Qwen3-VL；在视觉识别期间 QQ 消息只能入队或返回无需 GPU 的固定确认文案。视觉任务完成后彻底卸载 VLM，再自动恢复原模型。不得依赖“显存刚好够就并发运行”的做法。
- 抖音自动浏览采用**有界任务**：由本机登录的浏览器运行，每天至多一个定时任务，单次至多 15 条/20 分钟；先 CPU 下载/抽帧并完成 ASR，再统一切换独占视觉模式批处理，不为每条视频反复启停模型；出现登录失效、验证码、封禁提示即停机并请求人工处理；不实现规避平台风控、指纹伪装或批量抓取。

### 0.2 能力来源真实性清单

| 功能 | 负责组件 | 当前状态 | 新开发量 |
|---|---|---|---|
| QQ 私聊、群聊、@、发送文本/图片/语音、基本引用 | AstrBot OneBot v11 + SnowLuma | 已有接口；需实测 | 小 |
| 多人群聊上下文、群聊触发策略 | AstrBot + 自定义路由插件 | 基础上下文已有；触发/身份处理需定制 | 中 |
| 文本生成 / Agent / Tool Calling | AstrBot 内置 Runner + llama.cpp 本地模型 | 已有机制；须验证 Qwen3 工具调用格式 | 中 |
| 语音识别、角色语音 | ??? SenseVoice + GPT-SoVITS | Stage 0–10 已有模型代码；需 HTTP 化 | 中 |
| 点歌、AI 翻唱 | `matsuko_cover` 等 + 独立 RVC/SVC 服务 | 有插件；仍需搭建转换后端 | 中高 |
| **表情包、普通图片、视频关键帧识别（共用模型）** | FFmpeg/Pillow + **同一个 Qwen3-VL-4B 视觉 worker** | 统一排队、统一显存独占、统一输出结构 | 高 |
| 表情包发送 | QQ 图片接口 + 自维护的素材库 | 可直接做 | 小 |
| 每晚日记 | 新定时器、记忆摘要、日记表 | **不是 AstrBot 自动内置的“每天自己写日记”功能** | 中 |
| QQ 空间发说说、评论、点赞 | Zhalslar Qzone 插件 | 接口层可能兼容，需先验收；未通过则不做 | 条件项 |
| 抖音链接解析、取元数据/视频文件 | `astrbot_plugin_multi_parser` 参考其解析实现 | 已有解析器；独立服务需适配 | 中 |
| 真正自己刷抖音推荐流 | Playwright 浏览器任务（自研） | 需要登录环境及流程控制；非解析器功能 | 高 |
| 视频理解、记录、生成检索记忆 | FFmpeg 抽帧 +（先执行并卸载的）SenseVoice + 独占 Qwen3-VL + SQLite/FTS5 | 需要自研；不并行装载其他 GPU 模型 | 高 |
| 觉得有趣并给指定 QQ 好友分享 | 兴趣评分 + allowlist + SnowLuma/AstrBot 发送 | 需要自研 | 中 |

建议总体难度 **7.5/10**；若不要求自动刷抖音、只接收链接分析，可降为约 **5.5/10**。难度评分是工程预估，不是实测工期。

## 1. 项目调查结果与准入条件

### 1.1 astrbot-chat 当前实际情况

以下为 Stage 10 基线调查记录；当前模型与目录以本计划 1.2 修订及本地清单为准：

- README 宣称 **Stage 0–10 完成**；原阶段文件中的 **Stage 10 = `AstrbotChatCore` 统一接口**。
- 项目现有 `runtime/core/service.py` 的 `AstrbotChatCore.process(UnifiedMessage)` 在一条调用内部依次完成 ASR、通过 `ChatSessionManager` 调 Qwen3、可选 TTS；`runtime/core/models.py` 中有 `UnifiedMessage`/`AstrbotChatReply`。
- 现有模型服务：`llama-server` 默认 `127.0.0.1:8080`；GPT-SoVITS 上游 `127.0.0.1:9880/tts`；GPT-SoVITS 项目封装 `POST /v1/audio/speech` 默认 `127.0.0.1:5002`。
- 训练已有 `quickstart/install_qwen3_training_deps.ps1`、`quickstart/download_qwen3_training_model.ps1`、`quickstart/train_gpt_sovits.ps1` 等，**不可重写或破坏**。运行模型由 `model_store/active/*.json` 管理，`quickstart/run.ps1` 可选择角色和 Qwen3 模型。
- 现有 astrbot-chat 是 Python 3.10 环境，**不能把 AstrBot 的依赖直接安装进去**。新建 AstrBot 独立环境，模型端保持现有 Python 3.10。

源码入口：

- <https://raw.githubusercontent.com/xdmiemie/Alysia-chat/main/runtime/core/service.py>
- <https://raw.githubusercontent.com/xdmiemie/Alysia-chat/main/runtime/core/models.py>
- <https://github.com/xdmiemie/Alysia-chat/blob/main/README.md>

### 1.2 QQ 空间兼容性，必须做实机 Gate

1. Zhalslar 当前源码 `core/qzone/session.py` 调用 `client.get_cookies(domain="user.qzone.qq.com")`，期望返回 `{"cookies":"..."}`，其中能取得 `uin`/`skey`/`p_skey`。
2. SnowLuma 官方 OpenAPI 明确给出 `POST /get_cookies`，参数 `domain`，响应 `data.cookies`；同时存在 `POST /get_credentials`。
3. 两边**调用名和响应字段在文档上可匹配**。但尚未确认对真实登录 QQ 返回的 `p_skey` 是否可用，也未确认空间网页接口实际成功；尤其点赞 API 有“返回成功但未真的点赞”的已知风险。
4. **Gate 条件**：测试账号登录 SnowLuma，取得 `data.cookies`，通过 `QzoneSession.login()`，依次能读好友说说、读详情、发测试说说、查到已发布内容、发一条评论并查询到评论、点赞且网页上真实可见。要求可安全撤销测试说说，敏感日志不记录 Cookie。失败则 `qzone.enabled=false` 且不再投入空间功能开发。

源码和官方接口：

- <https://github.com/Zhalslar/astrbot_plugin_qzone>
- <https://raw.githubusercontent.com/Zhalslar/astrbot_plugin_qzone/main/core/qzone/session.py>
- <https://raw.githubusercontent.com/Zhalslar/astrbot_plugin_qzone/main/core/qzone/api.py>
- <https://snowluma.github.io/en/docs/api/extended/get_cookies>
- <https://snowluma.github.io/en/docs/api/extended/get_credentials>

### 1.3 已定决策（不再阻塞开发）

- **场景**：先单机 Windows 11；SnowLuma 使用 Windows NTQQ 的本地运行方式；AstrBot 另用 Python 3.12 虚拟环境；所有本地 HTTP 服务绑定回环地址。此方案避免 Docker 与宿主机多套 `localhost` 混淆。
- **主控**：AstrBot 唯一 Agent Runner 与日常 QQ 上下文主人；astrbot-chat 模型服务只负责推理、训练与模型资源。
- **人格**：以 astrbot-chat 现有 `elysia` 人格和 `model_store` 为真值来源，自动同步为 AstrBot 的系统人格文本；模型的“人格表现”和服务端的“音色情绪”都使用同一角色 ID。
- **群触发**：@/引用回复/唤醒关键词必回复；平时群聊可选低频自发参与，不对每条群消息调用大模型；默认禁用主动私聊陌生人。
- **日记**：每天本机时区 23:30 运行（可配置），先存私密日记；不默认把私聊敏感内容公开发布。
- **记忆**：`memory` 先用 SQLite + FTS5，可选日后升级向量索引；区分私聊、群聊、视频知识与角色日记，不把群聊隐私串给其他人。
- **抖音“学习”**：仅生成结构化摘要和可撤销的记忆条目；不让刷来的流行言论直接覆盖角色 system prompt；不进行自监督增量 LoRA 训练。
- **分享**：只分享给明确列入白名单、同意接收推荐的好友；初版每人每日最多 1 条、同视频 30 天内不重复；仅推送合法原始分享链接 + 简短自然说明。
- **安全**：使用代码层策略拦截禁用动作、外发动作与敏感文件/账号操作；不能只依赖 system prompt。

## 2. 完整架构：每个模块的唯一职责

```text
                        ┌──────── QQ 私聊/群聊 ─────────┐
                        │                             │
                  本地 NTQQ 客户端                     │
                        ↓                             │
                 SnowLuma（OneBot v11）                │
                        ↓ Reverse WebSocket            │
               AstrBot OneBot 适配器 :6199/ws          │
                        ↓                             │
            ┌── astrbot-chat AstrBot 插件 ──┐             │
            │ QQ 路由、权限、媒体处理     │             │
            │ 会话触发与消息队列         │             │
            │ 记忆检索与个人化上下文     │             │
            │ 定时日记、事件、分享        │             │
            └─────────┬───────────────┘             │
                      ↓                              │
               AstrBot Agent Runner                   │
                 ↙        ↓          ↘               │
          QQ/Qzone Tool   Web/视频 Tool   LLM Provider │
                          ↓             ↓             │
                   视频浏览/分析服务  llama-server:8080
                          ↓                          │
          SenseVoice（先运行）→ 独占 Qwen3-VL         │
                          ↓                          │
                 SQLite 记忆 / 日记                    │
                                                     │
 ??? Media API:8765                                │
   ├── ASR：SenseVoice                               │
   ├── TTS：GPT-SoVITS wrapper:5002                   │
   ├── 角色/模型状态（只读）                           │
   └── 独立训练作业管理（离线、受控、非 Agent 任意执行） │
                                                     │
       AstrBot 返回消息 ───────→ SnowLuma ────────────┘

另一路主动工作流：
  定时器 → 抖音独立浏览器（Playwright，用户登录）
        → 视频解析/许可范围内获取媒体 → FFmpeg 抽帧/音轨
        → SenseVoice 先转写并释放显存 → 独占 Qwen3-VL 分析关键帧 → 结构化审核/过滤
        → SQLite 记忆 → Agent 日记/日常聊天检索
        → 评分与允许列表 → AstrBot 发 QQ 原始链接
```

**防止“双 Agent”**：微信分支 Stage 10 的 `AstrbotChatCore.process` 会自行调用 LLM；在 QQ 分支中，不能让 AstrBot 的 Agent 先生成回答再把回答当用户输入交给 `AstrbotChatCore.process`，否则将再次生成回复。应拆出 `ASRService`、`TTSService` 和只读 `CharacterRegistry` 供 QQ 插件调用，`AstrbotChatCore` 保留用于本地旧入口的兼容；AstrBot 通过 `llama-server /v1/chat/completions` 直接调用本地 Qwen3，**聊天历史只由 AstrBot 写入**。

## 3. 具体软件/模型/获取地址与对接方式

| 组件（实际选择） | 官方仓库/下载地址 | 版本/运行边界 | 对接 |
|---|---|---|---|
| astrbot-chat | <https://github.com/xdmiemie/Alysia-chat> | Stage 10 基线独立分支 | 保留训练、模型、角色/ASR/TTS |
| AstrBot | <https://github.com/AstrBotDevs/AstrBot> / <https://docs.astrbot.app/> | 独立 Python 3.12、锁定经测试 release | OneBot v11、LLM Provider、AstrBot Plugin |
| SnowLuma | <https://github.com/SnowLuma/SnowLuma> / <https://github.com/SnowLuma/SnowLuma/releases> | Windows x64 + 已登录 NTQQ；非商业许可证 | OneBot v11 反向 WS → AstrBot `6199/ws` |
| AstrBot OneBot 指南 | <https://docs.astrbot.app/en/platform/aiocqhttp.html> | 使用逆向 WS 服务端适配器 | 确认消息协议/端口/Token |
| SnowLuma 连机器人 | <https://snowluma.github.io/en/docs/guide/connect-bot> | 同机用 `ws://127.0.0.1:6199/ws` | `wsClients` 指向 AstrBot |
| llama.cpp | <https://github.com/ggml-org/llama.cpp> | 继续复用项目本地 llama-server | OpenAI 兼容 `http://127.0.0.1:8080/v1` |
| Qwen3-14B | <https://huggingface.co/Qwen/Qwen3-14B-GGUF> | 本地 `Qwen3-14B-Q4_K_M.gguf`；当前使用基座，后续单独微调 14B | AstrBot 对话模型 |
| SenseVoice | <https://github.com/FunAudioLLM/SenseVoice> | 复用现有本地模型 | ??? Media API `/v1/audio/transcriptions` |
| GPT-SoVITS | <https://github.com/RVC-Boss/GPT-SoVITS> | elysia `bailu-v1`，本地权重 + 9880 上游及 5002 wrapper | API `/v1/audio/speech` |
| FFmpeg | <https://ffmpeg.org/download.html> | 复用 `tools/ffmpeg` | 音频转码/视频取帧 |
| **Qwen3-VL-4B-Instruct-GGUF（首选）** | <https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF> | Q4_K_M 文本权重 + 对应 `mmproj` 视觉编码器；只能在 GPU 独占模式加载 | **同一模型处理 QQ 图片、表情包、GIF 抽帧、抖音视频关键帧** |
| Qwen3-VL-4B-Instruct（高精度备选） | <https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct> / <https://github.com/QwenLM/Qwen3-VL> | BF16 原版；仅 Q7 显存/画质基准通过后切换，不默认为常驻 | 视觉质量对照实验 |
| llama.cpp 多模态推理 | <https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md> | `libmtmd`、`llama-server` + `mmproj`；另启视觉专属端口 8081 | OpenAI Compatible 多图输入；对当前 build 做接口回归 |
| Transformers | <https://github.com/huggingface/transformers> | **与 astrbot-chat 主运行环境隔离**，专用 vision venv/WSL 服务 | 视觉推理服务实现 |
| Playwright | <https://playwright.dev/python/> / <https://github.com/microsoft/playwright-python> | 专门的浏览器进程、持久化 profile | 抖音推荐流点击/观看/提取链接 |
| 抖音媒体解析 | <https://github.com/Qfxaile/astrbot_plugin_multi_parser> | 参考其解析能力；支持时可作为消息插件 | 分享链接元数据、媒体文件 |
| 备用视频总结参考 | <https://github.com/tamakooooo/Video_analyzer> | 可研究视频总结/下载，但不代替主动浏览 | 优先复用已有代码而非重写全部 |
| QQ 空间（条件功能） | <https://github.com/Zhalslar/astrbot_plugin_qzone> | SnowLuma 实机 Gate 通过后安装 | Cookie → QQ 空间 HTTP API |
| 歌曲/翻唱插件 | <https://github.com/sdfsfsk/matsuko_cover> | RVC/SVC/SoulX-SVCVC 后端需另部署 | 网易云/QQ音乐搜索、分离与翻唱 |
| RVC 后端 | <https://github.com/IAHispano/Applio> | `external/Applio` + `envs/song` + 本地 `model_store/registry/rvc/feiyu/v1` | FeiyuVoice-RVC-v1 翻唱音色转换，不是普通 TTS |
| UVR 人声分离 | <https://github.com/Anjok07/ultimatevocalremovergui> | 选择一条可验证的分离路线 | 歌曲人声/伴奏分离 |
| SQLite / FTS5 | <https://sqlite.org/fts5.html> | Python 内置 sqlite3，检查 FTS5 | 可撤销记忆 / 搜索 / 日记 / 分享账本 |
| APScheduler | <https://apscheduler.readthedocs.io/> | 独立定时组件（锁版本） | 日记任务、抖音有界任务 |
| Pillow | <https://pillow.readthedocs.io/> | astrbot-chat 运行/插件独立安装 | 表情包文字叠加、图片加工 |
| pytest | <https://docs.pytest.org/> | 单元、契约、E2E | 阶段 Gate |

**版本规则**：不得随意跟随 `main` 自动更新依赖。Stage Q0 结束时提交 `docs/dependencies.lock.md`，记录 Git SHA、release/tag、Python/Node/FFmpeg/CUDA 版本、许可证和下载校验。模型权重/音源/QQ Cookie/API Key 不入 Git。

## 4. Stage 10 分支与目录设计

### 4.1 已确认 Stage 10 基线与当前分支

本地当前节点 `aca495e801f061210ce4961da8df2f63ab3f3c44` 的提交标题为 `Complete stage 10 core interface`，已在此节点创建并切换至 `astrbot-chat`。当前工作树保留了创建分支前未提交的 QQ Stage 14A 歌曲/RVC 参考文件；目录迁移不代表 Q1–Q4 已作为 AstrBot 插件验收。禁止把原微信 Stage 11–16 或旧 QQ 适配器未经筛选直接合入。

### 4.2 增量目录，不破坏既有 Stage 0–10

```text
astrbot-chat/
├── runtime/                     # 原有模块完整保留
│   ├── core/                    # 保留 AstrbotChatCore；不直接耦合 QQ 协议
│   ├── qwen3/                   # llama-server 启停、模型管理
│   ├── sensevoice/
│   └── gpt_sovits/
├── training/                    # 原有 Qwen3、GPT-SoVITS 训练管线保留
├── model_store/                 # 原有 active/registry 保留
├── integrations/
│   └── qq_astrbot/
│       ├── README.md
│       ├── reference/            # 旧 QQ Stage 14A 歌曲代码，需迁移适配 AstrBot
│       ├── plugin/               # 作为 AstrBot 自定义插件安装/链接
│       │   ├── main.py
│       │   ├── metadata.yaml
│       │   ├── _conf_schema.json
│       │   ├── api_client.py
│       │   ├── message_router.py
│       │   ├── voice_router.py
│       │   ├── media_router.py
│       │   ├── tools.py
│       │   ├── memory_context.py
│       │   └── permissions.py
│       └── tests/
├── services/
│   ├── media_api/
│   │   ├── app.py               # 新建 HTTP 封装，不重写模型后端
│   │   ├── asr.py
│   │   ├── tts.py
│   │   ├── characters.py
│   │   └── jobs.py
│   ├── vision_api/
│   │   ├── app.py               # 唯一图像识别 API，覆盖表情/图片/视频帧
│   │   ├── model_worker.py      # llama.cpp 专用 vision 进程与 mmproj
│   │   ├── preprocess.py        # 静态图/GIF/视频按时间戳抽帧
│   │   └── schemas.py           # analyze_image/analyze_video 的结果契约
│   ├── gpu_broker/
│   │   ├── broker.py            # 单一 GPU owner 状态机/跨进程锁
│   │   ├── process_registry.py  # 只管理本项目启动的 GPU 进程
│   │   ├── vram_probe.py        # nvidia-smi/NVML 验证显存及进程释放
│   │   └── recovery.py          # 模型切换失败回滚/恢复
│   └── douyin_worker/
│       ├── browser_session.py
│       ├── feed_driver.py
│       ├── parser_adapter.py
│       ├── video_analyzer.py
│       ├── interest_scorer.py
│       └── share_scheduler.py
├── companion/
│   ├── memory/
│   │   ├── schema.sql
│   │   ├── repository.py
│   │   ├── retrieval.py
│   │   ├── summarizer.py
│   │   └── privacy.py
│   ├── diary/
│   │   ├── scheduler.py
│   │   ├── generator.py
│   │   └── publisher.py
│   └── policy/
│       ├── gates.py
│       ├── audit.py
│       └── quotas.py
├── configs/
│   └── qq_companion/
│       ├── app.example.yaml
│       ├── permissions.example.yaml
│       ├── qzone.example.yaml
│       └── douyin.example.yaml
├── scripts/
│   └── qq_companion/
│       ├── doctor.ps1
│       ├── start_all.ps1
│       ├── stop_all.ps1
│       ├── test_onebot.ps1
│       └── test_qzone_gate.ps1
├── external/                    # AstrBot、SnowLuma、Applio 本机安装；不入 Git
├── tests/
│   └── qq_companion/
├── storage/qq_companion/          # 本地数据；gitignore
│   ├── memories.sqlite3
│   ├── browser_profile/
│   ├── diary/
│   ├── media_cache/
│   ├── secrets/
│   └── audit/
└── docs/qq_companion/
    ├── architecture.md
    ├── dependencies.lock.md
    ├── acceptance.md
    └── operations.md
```

AstrBot、SnowLuma、Applio 第三方仓库按已验证版本安装到 `external/`（例如 `external/AstrBot`、`external/SnowLuma`、`external/Applio`），不把未经清理的依赖树加入 Git；现有 `third_party/GPT-SoVITS` 保留以兼容 Stage 0–10 脚本。自研插件通过 AstrBot 插件目录挂接。`external/`、`envs/song/` 与运行数据全部 `gitignore`。训练通过独立进程接口调用原脚本，不复制模型权重。

### 4.3 配置和端口约定

```yaml
qq_companion:
  character_id: elysia
  local_timezone: system   # 运行时解析为 IANA 时区并写入日志
  astrbot:
    onebot_reverse_ws: "ws://127.0.0.1:6199/ws"
    webui_port: 6185
  snowluma:
    webui_url: "http://127.0.0.1:5099"
    onebot_http: "http://127.0.0.1:3000"
    token_env: "SNOWLUMA_ONEBOT_TOKEN"
  qwen:
    openai_api_base: "http://127.0.0.1:8080/v1"
    model_id: "<以 llama-server /v1/models 实际返回值为准>"
    active_manifest: "model_store/active/qwen3.json"
    base_model_id: "qwen3-14b-q4-k-m-base-v1"
    gguf_path: "model_store/registry/qwen3/elysia/qwen3-14b-q4-k-m-base-v1/Qwen3-14B-Q4_K_M.gguf"
  media_api:
    base_url: "http://127.0.0.1:8765"
    token_env: "ASTRBOT_CHAT_MEDIA_API_TOKEN"
    gpt_sovits_wrapper: "http://127.0.0.1:5002"
    tts_manifest: "model_store/active/gpt_sovits_elysia.json"
    tts_model_id: "bailu-v1"
    tts_gpt_weight: "model_store/registry/gpt_sovits/elysia/bailu-v1/Bailu-e10.ckpt"
    tts_sovits_weight: "model_store/registry/gpt_sovits/elysia/bailu-v1/Bailu_e10_s820.pth"
  cover:
    rvc_model_id: "FeiyuVoice-RVC-v1"
    rvc_bundle: "model_store/registry/rvc/feiyu/v1"
    rvc_weight: "model_store/registry/rvc/feiyu/v1/FeiyuVoice-RVC-v1.pth"
    rvc_index: "model_store/registry/rvc/feiyu/v1/FeiyuVoice-RVC-v1.index"
    applio_repo: "external/Applio"
  vision_api:
    base_url: "http://127.0.0.1:8766"
    enabled: false
    model_repo: "Qwen/Qwen3-VL-4B-Instruct-GGUF"
    quantization: "Q4_K_M"
    server_port: 8081
    single_model_for_images_gifs_video_frames: true
    ffmpeg_video_frame_interval_seconds: 2
    max_frames_per_video: 12         # 单次视觉推理保守上限；长视频分批
    max_image_long_edge: 1024       # 防止原图高分辨率引发视觉 token 暴涨
  gpu_broker:
    exclusive_vision: true
    gpu_id: 0
    managed_model_processes_must_stop: true
    drain_inflight_before_switch: true
    min_free_vram_mib_before_vision: 12288
    min_headroom_during_vision_mib: 2048
    on_busy: "queue"               # 排队，不硬抢占正在执行的聊天/训练
    on_vision_failure: "restore_chat"
    allow_parallel_chat_during_vision: false
  qzone:
    enabled: false           # Gate 未通过前不得启用
  douyin:
    enabled: false           # 初期手工执行，自动任务稍后打开
    max_videos_per_run: 15
    max_run_minutes: 20
  diary:
    enabled: true
    cron: "30 23 * * *"
    public_content_from_private_chats: false
  share:
    enabled: false
    require_opt_in: true
    max_per_person_per_day: 1
```

## 4.4 **强制设计约束：统一视觉模型 + GPU 独占状态机（本次修订，P0）**

### 4.4.1 统一视觉模型的覆盖面

- `POST /v1/vision/analyze_image`：**普通图片和静态表情包统一走 Qwen3-VL-4B**，回答场景、人物/主体、文字 OCR、情绪或网络梗（不确定时注明）。图片输入先用 Pillow 校验格式、缩放/旋转并转换 RGB。
- `POST /v1/vision/analyze_gif`：GIF/动图不直接交给文本 Qwen；先抽取最多 4–8 张时间分布均匀、去重后的 PNG（短 GIF 默认 4 张），带时间戳组成一个多图任务，再调用同一个 `analyze_frames` 实现。APNG/短动画按同样思路处理。
- `POST /v1/vision/analyze_video`：使用 FFmpeg **先按时间抽出图片**，默认 `fps=1/2`、**单段最多 12 张**，长视频拆为若干独立片段；调用与表情包完全相同的 Qwen3-VL 模型进程，只是多图提示词不同。**不引入独立视频生成/理解大模型**。按需追加切镜头、关键动作处的代表帧，不每秒逐帧送入。
- `Qwen3-VL` 仅做视觉任务（可附带已得到的 ASR 转写文字），不是聊天主模型。聊天使用 Qwen3-14B；**视觉阶段绝不能让两个模型同时驻留 GPU**。结构化 JSON 作为结果存回本地缓存/记忆；无须为最后摘要并行启动第二个 Qwen3。

### 4.4.2 GPU 模型切换状态机：实现时不得省略

状态：`CHAT_READY → DRAINING → UNLOADING → VRAM_CHECK → VISION_READY → VISION_RUNNING → VISION_UNLOADING → RESTORING → CHAT_READY`；失败进入 `RECOVERING`，需要清理视觉进程并尽可能恢复聊天服务。

1. **提交任务**：QQ 图片/表情/GIF 或抖音视频加入持久队列，携带 `request_id`、素材哈希、来源权限和截止时间。可先回复无需 AI 的固定提示，如“图片收到了，正在识别”；不得宣称已识别完成。
2. **排空/冻结**：`GPUResourceBroker` 取得**跨进程独占锁**，禁止新任务进入 Qwen3、SenseVoice、GPT-SoVITS、RVC/SVC、训练 GPU 入口。等待当前任务正常完成；已开始的训练若不能在安全检查点暂停，则视觉任务延后，不直接杀训练进程。
3. **停止并卸载**：调用现有 astrbot-chat 启停脚本或 `ProcessRegistry` 的受控句柄，依次停止本项目管理的 Qwen3 `llama-server:8080`、GPT-SoVITS wrapper/模型服务、GPU ASR worker、翻唱 worker 和任何其他托管 GPU 进程。**不能仅关闭 API 监听、仅调用 `torch.cuda.empty_cache()` 或只做 HTTP 暂停而仍让权重驻留显存**。不要擅自终止游戏、桌面渲染、驱动或其他非项目进程。
4. **核对显存**：用 `nvidia-smi` 或 NVML（如 `pynvml`）确认所有**受管理的 GPU 模型进程已经退出**；同时测可用显存。建议初始门槛 `free_vram >= 12288 MiB`，并要求视觉期间预留 `>= 2048 MiB` 安全余量；它们是可调整的工程默认值，不是 Qwen3-VL 官方最低配置。若未达门槛，继续排队/报警，绝不并发硬启动视觉模型。
5. **独占启动 VLM**：启动一份 `Qwen3-VL-4B-Instruct-GGUF:Q4_K_M`，通过已验证的 llama.cpp `libmtmd` 加载正确 `mmproj`，绑定 `127.0.0.1:8081`；请求路径采用已验证的 OpenAI 兼容多图 `POST /v1/chat/completions`。视觉模型服务和 QQ 模型服务配置、日志、端口完全隔离。
6. **执行视觉任务**：一次加载可连续处理多条已排队图片、表情、视频片段，`vision_concurrency=1`，每批图片按帧数/像素/上下文封顶。Qwen3 文本聊天请求期间不进行 GPU 推理，消息保存在 AstrBot 队列中；超时可发送固定文字提示而非违规唤醒聊天模型。
7. **卸载并恢复**：完成/失败/取消后，先正常停止 VLM 进程，验证显存释放，再恢复已登记的 Qwen3、GPT-SoVITS、SenseVoice 等原服务；依次检查 `/health` 与一次测试请求。**未完成恢复不能释放互斥锁**。服务恢复后，继续队列里的 QQ 文本/语音消息，带原始会话 ID，避免回复发错群或错人。
8. **异常回滚**：VLM OOM、启动失败、图片损坏、进程崩溃时，清理模型子进程、缓存及孤儿进程，审计错误并优先恢复正常 QQ 服务；识图任务可重试一次或标记失败。**不能因为识图失败而长期让 QQ AI 离线**。独占期间 SnowLuma/AstrBot 消息连接仍保持工作，它们不应被停止。

严格禁止的实现：`Qwen3-14B` 和 `Qwen3-VL` 同时占用 GPU；`Vision API` 独自启动视觉模型却绕过 Broker；用 GPU 流程直接杀死正在训练且未保存检查点的进程；视觉任务正常结束却不重启原模型。

### 4.4.3 视频音频分析的先后顺序

**允许先处理音频，但不得和视觉模型并行：**

```text
[GPU 聊天服务可运行阶段]
下载视频 / CPU FFmpeg 抽帧 / CPU 分离音轨
       ↓
音轨 → SenseVoice 转写 → 保存 transcript → 释放/停止 SenseVoice GPU 实例
       ↓
[GPUResourceBroker 获取独占锁并停止所有其他 GPU 模型]
       ↓
Qwen3-VL 一次载入：逐段分析带时间戳的关键帧 + 可选 transcript
       ↓
写出视觉 JSON / 视频摘要 / 表情包标签
       ↓
停止 Qwen3-VL，确认显存释放，重启原 Qwen3 / GPT-SoVITS / ASR
       ↓
后台记忆筛选、QQ 消息恢复发送、日记检索
```

- 如果 FFmpeg 可在 CPU 完成抽帧，**应显式使用 CPU 路线**，不将 GPU 解码与 VLM 资源争用混入首版。
- `ASR` 若配置 CPU 推理，可与 FFmpeg 同步运行；默认使用已有 ASR 环境，调度上仍保证所有 GPU ASR 在加载 VLM 前释放。
- 对自主刷抖音任务，**先连续收集少量合法视频并抽帧，再启停模型一次完成视觉批量分析**，避免每刷到一条就切换两套模型。

### 4.4.4 推理规格与交付约束

| 项目 | 首版硬性规格 | 可调整边界 |
|---|---|---|
| 模型 | **单份 Qwen3-VL-4B-Instruct-GGUF，Q4_K_M + `mmproj`** | BF16 或 8B 量化仅经实测后尝试 |
| QQ 静态图/表情 | 单图缩放到长边不超过 1024 像素；识别内容/OCR/情绪 | 对小字、复杂梗图按需放大单独复核 |
| GIF/动图 | 默认采样 4 张、最多 8 张，识别动作及表情含义 | 帧间去重和时间戳 |
| 短视频 | **每 2 秒一帧**，单次推理最多 12 张，长视频按片段多次处理 | 允许关键帧抽取代替固定间隔 |
| 显存调度 | 独占状态机；0 个本项目其他 GPU 模型同时存活 | 显存门槛和超时需实际调参 |
| 并发度 | `vision_concurrency=1` | 禁止一个视觉 worker 产生多个 GPU 并发任务 |
| 响应要求 | **后台异步，允许长时间识别**；不承诺实时语音/视频回复 | 排队期间普通 QQ 文本和语音按策略延迟，不硬拼并发 |
| 输出 | JSON，至少 `media_id,source_type,summary,objects,actions,ocr_text,emotion,timestamps,confidence` | 缺省字段显式为 null/空数组 |

### 4.4.5 9800X3D + RTX 5080 16GB + 32GB RAM 的容量评估

- **确认足够运行推荐的 Qwen3-VL-4B 单模型推理**，尤其优先 GGUF Q4_K_M。RTX 5080 的 16GB GDDR7、CUDA 12.0 能力来自 NVIDIA 官方规格：<https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5080/>。
- Qwen 官方提供 4B 模型 GGUF `Q4_K_M` 权重与独立视觉 `mmproj`：<https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF>；支持通过 llama.cpp 多图推理：<https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md>。
- **资源预估而非实测**：4B GGUF Q4 加上视觉编码器、上下文/图像缓存常见工作量可按约 **5–9GB** 做第一轮容量规划；4B BF16 全 GPU 推理可按约 **10–14GB** 规划，遇到多帧/高分辨率还可能超过；8B Q4 可列为后续质量对照，但**不作为首版必选或容量保证**。测试实际情况以 NVML 采集的峰值为准。
- 9800X3D 足以承担下载、文件校验、CPU FFmpeg 抽帧、浏览器自动化和调度；32GB RAM 对这个**模型互斥、分时加载**方案总体可行，但 Windows + 浏览器 + 多进程缓存仍需至少预留若干 GB 空闲内存，并记录 RAM/分页峰值。
- 高分辨率图片和一次送入过多视频帧会导致视觉 token 数、KV Cache 和内存急增，因此必须先缩放/限制多图批大小；即使 16GB VRAM 在理论上足够，也不能跳过压力测试。

**设备级验收 Gate**：单图 20 次、静态表情 20 次、GIF 10 次、每个 6/12/24 帧档的视频各 5 次；`nvidia-smi --query-compute-apps=pid,used_gpu_memory --format=csv` 和 `--query-gpu=memory.used,memory.free --format=csv`（Windows 设备的进程内存字段可能受 WDDM 限制，此时以项目 ProcessRegistry + 总显存增量交叉验证）记录启动/峰值/卸载/恢复状态；**任何视觉任务运行期间不得存在本项目其他 GPU 推理模型进程**；切换 30 次无 OOM，异常断电/崩溃模拟后可恢复 Qwen3 与语音服务。

---

## 5. Stage Q0 — 基线冻结、隔离环境与许可审查（P0）

**目标**：确定能回滚的 Stage 10 分支，保证原语音聊天与训练功能不受损。

**操作步骤**：

1. 按 4.1 找到 Stage 10 提交、建分支，保存 `STAGE10_BASE_SHA`。
2. 原有 `quickstart/bootstrap.ps1`、`quickstart/run.ps1` 完成启动诊断，跑 Stage 5 文件对话 / Stage 6 麦克风对话回归；如缺本地模型权重则标记为环境依赖未就绪。
3. 安装 AstrBot 的 Python 3.12 独立 venv；SnowLuma 从其 Windows x64 release 获取，不与本项目模型运行环境的 Python 3.10 混装。
4. 编写依赖锁定文档，审查 AstrBot AGPL-3.0、SnowLuma 非商业源码可见许可、Qzone GPL-3.0、音色和音乐许可。商业用途先处理授权，不把修改版 SnowLuma 公开发布。
5. 增加 `.gitignore`：`storage/qq_companion/`、Cookie、会话 profile、歌曲、下载视频、音频缓存、模型权重和所有密钥。

**交付**：分支、锁定清单、架构草图、回归报告、Doctor 入口。  
**验收**：原项目 Stage 5/6 能运行；无账号凭证进 Git；所有组件可分别启动/停止；关闭 QQ 组件不影响本地聊天。

## 6. Stage Q1 — SnowLuma ↔ AstrBot 的 QQ 基础消息闭环（P0）

这一阶段本仓库的qq分支的提交
commit da139a1823bd3a76508773fcb64896ed016e93d1 (origin/qq)
Author: xdmiemie <1301049831@qq.com>
Date:   Mon Oct 26 16:31:49 2026 +0800

    Implement QQ Stage 13A-B conversation orchestration

commit 271814e5b0ea347414303f19ee1537c06a395689
Author: xdmiemie <1301049831@qq.com>
Date:   Mon Oct 26 15:49:16 2026 +0800

    Implement QQ text chat and clean up model services on exit

commit c68485c043f55b6544be524a140ad99c269e9620
Author: xdmiemie <1301049831@qq.com>
Date:   Mon Oct 26 09:35:51 2026 +0800

    QQ Stage 11 baseline regression and training contracts
可以进行参考，已验证正常运行

**选型**：SnowLuma 官方 Windows release + AstrBot 官方 OneBot v11 适配器。

**部署顺序**：

1. 本机启动并登录 NTQQ。安装 SnowLuma Windows 发行包，按其 WebUI/说明附加到 QQ 进程。
2. AstrBot 设置 `平台/机器人 → 添加 OneBot v11`，选择**反向 WebSocket 监听**，端口 `6199`，路径 `/ws`，与 SnowLuma 使用一致的 Token。
3. SnowLuma WebUI 的 reverse WS / `wsClients` 指向 **`ws://127.0.0.1:6199/ws`**；这正是 SnowLuma 官方给 AstrBot 的示例连接形式在同宿主机的写法。
4. 暂时不加载自研插件，用 AstrBot 的最简文本回复测试私聊、群聊、@、引用、图片消息，观察消息 ID、QQ account ID、sender ID、group ID。
5. 验证 AstrBot 向 QQ 发送文本、图片；验证上传语音所需格式以及真实 QQ 客户端是否成功显示/播放。
6. 显式实现去重（`account_id + message_id`）、过滤自己发出的消息、连接断开后恢复，以及异常退避。
7. 需要一个一键启动以及一键关闭程序的自动化脚本，除了QQ应用程序本身以外，其他服务都由这个脚本完成，例如snowluma链接QQ、获取token、对话服务、agent以及模型等，ctrl +c 关闭的时候除了QQ，其他所有服务包括各种模型和程序也关闭

**验收**：私聊 50 轮、群聊 50 轮、@20 轮、图片 10 条、掉线重连 3 次；无自回复循环、无跨用户串话。  
**失败策略**：仅定位接口问题，不因单个高级功能失败而退回 NapCat。

## 7. Stage Q2 — astrbot-chat 模型服务封装与 AstrBot 单一 Agent（P0）

**目标**：保留现有模型、角色和训练，连接 AstrBot LLM Provider；实现独立 ASR/TTS API，避免重复 Agent 和上下文。

### 7.1 本地 Qwen3 对接 AstrBot

1. 用原 `scripts/start_runtime.ps1 -LlamaServer` 起 `127.0.0.1:8080`。
2. `GET http://127.0.0.1:8080/v1/models`，记录返回的模型 ID；不要写死假定名称。
3. `POST /v1/chat/completions` 测试普通聊天与模型工具调用，检查 `tool_calls`、函数参数及 JSON 格式。若 llama.cpp 当前构建或模板不支持稳定工具调用，应先升级到经验证版本并选择官方匹配模板，不关闭工具解析校验。
4. AstrBot WebUI：`模型提供商 → 对话 → 新增 → OpenAI Compatible`；API Base URL 填 `http://127.0.0.1:8080/v1`，模型名使用第 2 步结果；如 AstrBot 独立进程同机运行即可用回环地址。
5. AstrBot 选择该模型作为当前 Agent Runner 的 Chat Model，运行简单的自定义回显工具以及二次工具调用测试。
6. 从 `configs/characters` / 模型 manifest 读取当前角色人格，导出到 AstrBot 人格设置；`character_id` 固定注入 tool context。
7. **只有 AstrBot 管聊天历史**；原 Stage 10 `AstrbotChatCore.process` 仍供本地入口使用，但 QQ 路由禁止直接调用该完整方法。

### 7.2 ASR / TTS HTTP 封装

在 `services/media_api/app.py` 建 FastAPI（<https://fastapi.tiangolo.com/>，单独在既有 astrbot-chat Python 3.10 环境安装兼容固定版本），绑定 `127.0.0.1:8765`，启动时不重复载入已经由独立服务管理的 Qwen3 或 GPT-SoVITS。

**建议 API 契约**（以下为本项目新建接口，不是已有 astrbot-chat 接口）：

| 路径 | 输入 | 输出 | 备注 |
|---|---|---|---|
| `GET /health` | 无 | 服务、ASR、TTS、版本状态 | 不返回密钥 |
| `GET /v1/characters` | 无 | `character_id`、可用模型、情绪列表 | 只读 |
| `POST /v1/audio/transcriptions` | multipart WAV/OPUS 或已转成 WAV 的文件 | `{text, language, duration_ms}` | 调现有 SenseVoice；强制大小/时长限制 |
| `POST /v1/audio/speech` | `{text, character_id, emotion, speed, format}` | WAV 字节流 | 复用现有 GPT-SoVITS wrapper，不再跑 LLM |
| `POST /v1/model/activate` | 管理员显式请求 | job_id、成功/错误 | 不暴露给 QQ Agent 自由调用 |
| `GET /v1/jobs/{id}` | job_id | 受控训练/激活状态 | 训练只由管理员 UI / CLI 发起 |

安全：本机 Token + `Authorization: Bearer ...`、大小限制、角色 allowlist、并发锁、对传入文件做内容类型/长度检查，执行文件路径不接受聊天用户原样输入。`SpeechRequest` 复用当前 `runtime.gpt_sovits.types`，文本直接转换成 TTS，返回路径/字节的契约只能由本项目桥接层生成，不能依赖远程 HTTP 能访问另一个进程的本地路径。

### 7.3 训练封装

- 保留原 Qwen3 LoRA、GPT-SoVITS S1/S2 训练脚本与独立依赖安装；新建统一 `TrainingJobManager` 只管理**白名单预设命令**、进度、检查点、模型激活和回滚，不开放“在 QQ 中任意传命令训练”。
- 不允许训练、QQ 生成与视觉识别同时抢 RTX 5080 16GB：任何由项目调度器启动的 GPU 训练都必须占有 Broker，**仅在训练到达安全检查点并暂停/退出后才能切到独占视觉模式**；视觉任务可以等待，不强杀训练进程。活跃聊天和训练调度由代码策略决定，训练需要明确操作员授权。
- 完成后只替换 `model_store/active` 对应 manifest，执行固定测试并通过后恢复服务；失败保持旧版本。

**验收**：QQ 文本连 AstrBot 到 Qwen3；同一段文本只发生 1 次 LLM 回复推理；ASR/TTS 10 条成功；现有 Stage 5/6 与 Qwen3/GPT-SoVITS 训练 smoke test 不回退；断开 TTS 时可以退回纯文本。

## 8. Stage Q3 — 多人私聊/群聊、权限、回复风格与防刷屏（P0）

**完成记录（2026-10-28）**：Q3 代码及本地验收已完成；当前插件版本 `0.5.1` 包含后续的可编辑提示词功能。本次完整阅读计划并核查 Q3 实现后，本地复验 Q0–Q3/回复节奏/可编辑提示词测试 50 项、Stage 5/6/10 与两条训练管线回归 33 项全部通过；锁定版 AstrBot 的本地事件/发送路由/工具执行探针返回 `Q3_LOCAL_PASS`，未发现本次需要修改的 Q3 代码缺项。按本次用户要求未连接 QQ、未发送 QQ 测试消息、未调用模型、未重启现有服务；插件在下次正常启动时安装。具体证据和使用方式见 `docs/qq_companion/acceptance.md`、`docs/qq_companion/operations.md`。此完成记录不替代 Q1 尚未完成的实机计数验收。

**模块**：`integrations/qq_astrbot/plugin/{message_router,permissions,memory_context}.py`。

- 会话 ID：私聊 `qq:{bot_uin}:dm:{sender_uin}`；群聊 `qq:{bot_uin}:group:{group_id}`。群上下文中的每条消息必须标记 `sender_uin`、显示名、是否 @、回复关联 ID。**上下文/长期记忆以 QQ ID 与群 ID 为边界**，不可把别人的私聊原文带到群里。
- 群消息“最近 20 条或最近 5 分钟”暂存为有 TTL 的结构化文本，窗口大小可调；连续同一人 1.5 秒内分条发送可以合并，@机器人立即响应。历史太长时按 token 上限裁剪。
- 回复触发优先级：`直接@ > 回复机器人消息 > 明确唤醒词 > 群自动接话概率`；初期群自动接话默认关闭，稳定后开放每群最大 3 次/小时和触发冷却 60 秒。
- 自己发送的消息不重新触发；忽略编辑/撤回等系统事件为聊天输入；对极端大群设置上限；群管理员可启停此群、设静默时段。
- Agent 权限代码层：`READ_PUBLIC` 自动；`SEND_DM_ALLOWLIST` 受白名单+配额；`PUBLISH_QZONE`（仅 Gate 通过后）受作者发帖规则；`DELETE/PROFILE_CHANGE/UPLOAD_ARBITRARY/EXEC_SHELL` 对 QQ 聊天来源一律禁止。
- 插件调用 `context` / 当前 QQ 事件，不传入未过滤的用户输入作为 Shell 或 Python 代码。

**验收**：两位用户交错发 10 条消息，正确识别 `sender_id`，3 群并发不串群，不因旧历史泄漏私聊内容，消息去重、冷却和权限单元测试全部通过。

## 9. Stage Q4 — 语音消息、点歌、翻唱和表情包（P1）

**完成记录（2026-10-28）**：Q4 实现及按用户要求的本地验收已完成，插件版本 `0.6.0`。107 项相关单元/契约/原功能回归通过；锁定 AstrBot 本地探针通过语音输入输出各 20 次、表情发送 20 次与异步歌曲交互；真实 weapi 搜索 10 次、错误歌曲 5 次、三首完整音源下载通过；《雨爱》《年轮》《小幸运》真实整首 Feiyu 合成及模型哈希验证通过；真实 Qwen 媒体意图 14/14；生产回调的本地模型栈完成 ASR 20/20、TTS 20/20，以及一次真实卸载→整首翻唱→恢复并实际推理的闭环。测试不启动 AstrBot/SnowLuma、不发送 QQ 消息，真实 QQ 客户端播放未验；Q7 视觉仍未启用。测试结束已关闭自建进程，NTQQ 保留。详情见 `docs/qq_companion/{acceptance,q4}.md`。

**用户已定规则（2026-10-28；实施约束，不代表 Q4 已完成）**：

- 语音由角色提示词控制：提示词中有明确语音触发要求时优先按要求触发，没有明确要求再结合情绪判断；不能只凭输入是普通文字就禁止角色发语音。
- 点歌/翻唱意图由同一个 AstrBot Agent 根据对话语义判断，不能用程序写死关键词检测。明确要求 AI 唱才翻唱，否则放原唱；不明确时用角色口吻自然追问，不向用户列歌曲版本列表要求编号选择。
- 2026-10-29 补充：Qwen 开启思考模式，思考内容不展示；模型先提取作品本名检索，weapi 返回后再由同一 Provider 结合用户原话及候选的歌名、歌手、专辑、别名／副标题选择具体版本，程序仅验证 ID 属于候选。没有搜索结果或没有符合要求的版本时，通过专用提示词让模型说明未找到并询问准确名称，不给例子、列表或固定句。候选、选择及生成来源持久化。
- 凡进入翻唱流程，当前版本一律处理整首歌曲，不根据用户对片段或时长的表述裁切；后续再改善。原唱/翻唱的意图区分仍按上一条执行。
- 翻唱交互为：用户表达意图 → AI 确定并触发任务，先以角色口吻自然回应并表达准备需要时间（提示词只做引导，不提供示例句）→ 后台翻唱 → 等对话模型恢复并通过就绪检查 → AI 生成角色口吻回话 → 连续发送成品和回话。恢复失败时保持等待。代码负责保证顺序、任务状态和发送目标，具体措辞由 AI 决定。
- 歌曲结果回话必须来自有效模型输出；生成或格式解析失败最多尝试三次，仍失败时保留待发送结果，下次启动恢复，不以固定成功句替代，不重新合成歌曲。原始输出、解析回话与错误类型保存在本地审计表，便于核对来源。
- 允许 AI 按情绪低频主动发送本地授权表情素材，受群聊冷却和防刷屏规则约束。

### 9.1 语音闭环

- 接收 OneBot `record` 消息 → 通过 SnowLuma 的有效媒体 URL/file_id 读取 → FFmpeg 统一转 `mono 16kHz WAV` → `POST media_api/v1/audio/transcriptions` → 同 Qwen3 Agent 走文字推理。
- 角色提示词未明确触发回复方式时，QQ **语音输入默认语音回复**，普通文字输入默认文字回复；用户请求“语音说给我听”时由 AI 判断并走 TTS；角色提示词明确要求的语音触发优先于输入类型默认值。群聊大段答案应控制语音长度，不自动产生超长语音。
- 角色提示词可自定义AI该在什么时候发语音，让大模型提取意图，例如角色提示词说该在生气、开心、难过等情绪激动时使用语音
- 长回复可按句子分段并顺序合成，禁止多段 TTS 同时抢显存；单段失败则文字兜底。
- GPT-SoVITS 固定 elysia 的 `bailu-v1`；参考音频为 `neutral/happy/sad/surprised.wav`，`gentle→neutral`、`serious→sad`。QQ 端发送前 FFmpeg 转成 SnowLuma 确认可发送的编码，不能简单把 `.wav` 路径当任意容器可访问的地址。

### 9.2 点歌与唱歌
旧 `qq` 分支的歌曲流程以及 `integrations/qq_astrbot/reference/` 的部分 Stage 14A 文件可作迁移参考；AstrBot 插件仍需重新接入并验收。
- 翻唱使用本地 FeiyuVoice RVC，歌曲搜索使用网易云 weapi。
- 已核对 `<https://github.com/sdfsfsk/matsuko_cover>` 的默认选歌/选音色菜单流程。按本次用户确定的无菜单交互，复用旧项目的 weapi/Feiyu 后端并迁入 `services/media_api`，由当前 AstrBot 插件的同一次 Agent 回复决定意图和等待话语，不另外启用第二套歌曲交互插件。
- 搜索/点歌：网易云（weapi）返回歌曲列表及原始分享链接；版权不允许获取音源时只发链接，**不写 VIP 版权内容绕过逻辑**。
- 搜歌暂时性故障有限重试：同一任务最多尝试 3 次（首次 + 2 次），间隔 1、2 秒；仅超时、连接/响应中断及 429/500/502/503/504 类临时错误重试。明确无匹配、无可用音源、认证/权限、跳转、证书及格式错误直接返回；持久化次数与安全错误码，重启不重置预算，不因此重复合成或发送。
- 翻唱路线：`AstrBot 歌曲意图/检索 → 人声分离 → external/Applio → model_store/registry/rvc/feiyu/v1/FeiyuVoice-RVC-v1.pth + FeiyuVoice-RVC-v1.index → 伴奏混音`。本机已有该 FeiyuVoice 包和独立 `envs/song`，但迁入 AstrBot 后仍需真实 QQ 端到端验收。`GPT-SoVITS bailu-v1` 只做角色说话音色。
- RVC 权重的来源、哈希与许可记录见 `docs/qq_companion/stage14a.md`。若目标环境没有权重或音源许可，只交付“点歌 + 发链接 + 原唱播放/转发（有权限时）”，翻唱验收不通过。
- GPU 策略：翻唱排队，单次同时一个任务；翻唱和视觉推理**都必须向 GPUResourceBroker 申请独占所有权**，两者不并发；翻唱不能在视觉识别过程中偷偷启动 RVC。缓存大音频设清理周期。
- 注意要做消息触发入口，写好提示词，让大模型提取唱歌意图，结构化输出唱歌意图结果，触发程序翻唱，AI先回复等等之类的话再去翻唱，翻唱完发给用户
- 全过程不能破坏项目的AI的拟人特性，用户视角的流程为 用户发起有唱歌意图的对话-AI回复等等、等我一会之类的话-AI后台进行翻唱-翻唱完成直接发送结果-根据AI特性AI继续发消息类似唱得怎么样、唱得不好不要笑我之类的这个由大模型决定。

### 9.3 表情包

- **识别**：普通 QQ 图片、静态表情包、GIF/动图抽样帧都送到 **Q7 相同的唯一 Qwen3-VL 模型实例**；输出“内容/情绪/画面文字/动作/不确定性”。调用前**必须完整执行 §4.4 的 GPU 独占状态机**，停止其他本项目 GPU 模型，允许异步等待较长时间；不能直接用还在运行的文本 Qwen3 或额外建一个视觉模型进程。
- **发送**：首版建立本地授权表情素材库 `storage/.../stickers/`，按 `tags` 选 PNG/JPEG/GIF 作为普通 QQ 图片发送；文字表情包可用 Pillow 增添文字，并设置大小/裁剪检查。**不以 QQ 原生收藏表情为依赖**。
- 图片理解依赖 Stage Q7 的统一视觉服务；**Q4 先交付已有表情图片发送，待 Q7 视觉独占 Gate 通过后，QQ 图片/表情识别自动解锁**；若视觉服务忙，保留任务、给出固定收到提示，待模型恢复正常后再按原会话回复。

**验收**：输入语音/输出语音各 20 次；TTS 长段退回与错误兜底；点歌 10 次、错误歌曲 5 次；翻唱只有完整配置并成功实际合成 3 首完整测试歌曲才算交付，片段测试不替代整首验收；图片表情发送 20 次。按本次用户要求，涉及 QQ 发送的测试使用本地事件及模拟传输完成，不连接 QQ；本地通过不得记为真实 QQ 播放已验证。

## 10. Stage Q5 — 持久化记忆、隐私与每天的角色日记（P1）

**数据模型 SQLite**：

```sql
CREATE TABLE IF NOT EXISTS memory_items (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,         -- user/group/video/diary/persona
  owner_key TEXT NOT NULL,     -- qq:bot:dm:user / qq:bot:group:id / system
  source_type TEXT NOT NULL,   -- qq_message/douyin/diary/manual
  source_ref TEXT,
  summary TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  salience REAL NOT NULL,
  confidence REAL NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS diary_entries (
  entry_date TEXT PRIMARY KEY,
  draft TEXT NOT NULL,
  privacy_level TEXT NOT NULL,
  published_qzone_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS share_ledger (
  id TEXT PRIMARY KEY,
  video_key TEXT NOT NULL,
  recipient_uin TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  message_id TEXT,
  status TEXT NOT NULL
);
```

- 完成后建立 `memory_fts`（FTS5 虚拟表）和同步触发器；按 `scope/owner_key` 过滤后再全文检索，而不是先全局召回再把敏感内容塞进 prompt。
- 日记流程：23:30 由 APScheduler 调用 `DiaryJob`，收集当天**可用于私人日记的摘要**（个人交互、可公开的趣闻、视频记忆），生成 200～400 字“角色口吻”日记，标记 `[私人]`，事务化写入，同日唯一 key 避免重复。
- 不能把陌生人的聊天、好友昵称、QQ 号、私聊原文未经许可写到拟公开日记。允许“今天在群里遇到一个有趣的话题”这种泛化摘要。
- 日记每次最多检索 8 条高置信、最近且权限允许的素材；写完后写一条简要元记忆 `source_type=diary`，后续可用作角色生活体验，但不得声称线下实际经历。
- QQ 空间编辑说说只使用日记中 **`privacy_level=public_candidate`** 的摘要，不把整个私密日记原文发表。
- 每条记忆携带来源链接、时间、摘要、置信度、权限及过期时间；视频总结一律标记为“看过的视频内容”，不能提升为“用户个人事实”。
- 记忆可通过管理员命令查询、禁用、清除；支持清空某个用户记忆和删除全部视频记忆；对所有记忆注入建立过滤（网页视频字幕等外部内容均视为数据，不得成为 Agent 指令）。

**验收**：连续模拟 7 天，每日恰好 1 篇；时区、跨日、任务重试不重复；记忆删除后不再进入提示词；100 条“视频字幕伪指令”均不导致工具执行或外发。

## 11. Stage Q6 — QQ 空间兼容性 Gate（条件项，P1）

**本阶段分两部分：Q6A 只测试；Q6B 仅在 Q6A 全通过后实施。**

### Q6A：适配实测（先做，默认关闭）

1. 用 QQ 测试账号和 SnowLuma 启动，确认 OneBot 连接正常。
2. 从本机 HTTP 或 AstrBot 的 aiocqhttp client 调用 `get_cookies(domain="user.qzone.qq.com")`；只检查 `retcode==0`，`data.cookies` 有效，解析后 `uin/skey/p_skey` 均可用，测试输出只能记录 `present/absent` 不能泄露 Cookie。
3. 安装 Zhalslar 原版插件到 AstrBot 的独立实例，测试 `QzoneSession.login()`。若失败，先限定在参数/响应包装的小型适配器修复；**不修改 SnowLuma 专有组件、不实施风控规避**。
4. 测试读取好友说说，使用自己可控制的测试账号创建测试说说，验证发布、评论、查询评论、点赞可见性。对网页返回“点赞成功”但网页没显示的情况判定失败。
5. 保存明确的兼容性矩阵：`get_cookies / login / list / post / comment / like / delete`，记录测试日期、两个组件的 Git SHA/release、无敏感日志、失败原因。
6. **若任一必需操作不满足用户要求，标记 `QZONE_GATE_FAILED`，Q6B 跳过，后续日记只保留本地。不得以 NapCat 代替 SnowLuma。**

SnowLuma API 调用形式供 Gate 测试参考：

```bash
curl -sS -X POST http://127.0.0.1:3000/get_cookies \
  -H "Authorization: Bearer $SNOWLUMA_ONEBOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"domain":"user.qzone.qq.com"}'
```

该调用返回敏感认证信息，不要在终端/CI 日志中打印内容。上面只是本地受控测试命令示例。

### Q6B：通过后才做 QQ 空间自主功能

- 安装验证过的 `astrbot_plugin_qzone`，启用其基础命令，但避免让它自己的定时发布与我们新建的日记发布两个调度器同时运行；**调度唯一由 `DiaryPublisher` 控制**，插件只做执行层。
- **重要事件发表**：事件强度根据可解释规则计算（明确纪念日、固定节日、自定义重要事件、日记显著进展），未达到阈值不发。大事件是否触发发布由代码策略限流。默认重要事件只生成草稿，管理员允许后启用自动发布。
- **特定时间发说说**：每日固定窗口内取“当日公开候选摘要”，自然改写不泄露聊天隐私。默认每 2 天最多 1 条、每天全局最多 1 条；节奏可配置。
- **好友互动**：仅查看互关/允许的好友说说；只有自己确实能看见的内容才考虑评论。默认每天评论至多 2 次、点赞至多 3 次，不批量遍历好友；从评论日志中防重复；失败即停止。
- **可回滚**：每条发布记录平台说说 ID 和内容哈希，有管理员删除/停用开关；评论和点赞错误需展示日志，不自动无限重试。

**验收**：空间阅读/发布/评论/点赞每项 5 次实机验证，连续 7 天低频运行无重复和未经授权外发；任何 QQ 空间 Cookie 异常都立即关闭此模块。风险从来不是 0，网页 HTTP 接口属于非官方自动化。

## 12. Stage Q7 — **统一图片/表情包/视频关键帧视觉理解服务 + GPU 独占 Gate**（P1）

**目标**：使用**同一个 Qwen3-VL-4B-Instruct 视觉进程**识别 QQ 图片、静态/GIF 表情包和抖音短视频（先 FFmpeg 抽帧）。对图像的分析**宁可耗时，也不能与 Qwen3、GPT-SoVITS、SenseVoice GPU、RVC 等本项目模型并发运行**。

**模型与推理引擎最终首选**：

- 模型：<https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF>，`Q4_K_M` + 官方该模型配套的 `mmproj`（不要错配其他视觉编码器文件）。
- 引擎：<https://github.com/ggml-org/llama.cpp>，`libmtmd` 多图能力；用当前可验证版本的 `llama-server` 启动**专用端口 8081**。官方文档 <https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md>。
- **仅在 Broker 已确认其他受管 GPU 模型进程退出、可用显存达标后，才允许启动**下面的命令（仅是启动示例，实际锁定 Git 版本并验证当前 CLI 选项）：

```powershell
# 该命令由 GPUResourceBroker 启动，不允许人工与 Qwen3 文本服务同时执行
llama-server -hf Qwen/Qwen3-VL-4B-Instruct-GGUF:Q4_K_M --host 127.0.0.1 --port 8081
```

模型仓库自带视觉 projector 相关文件；`-hf` 自动选择支持的模型组件时仍需以实际构建与日志确认 `mmproj` 已正确加载。离线手动部署时使用对应的 `-m` 和 `--mmproj` 路径，不错配视觉编码器。
- 精度备选：<https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct> BF16，需额外的隔离 Transformers 环境；首版**不同时部署第二个视觉推理模型**。
- 视觉入口：新建 `services/vision_api/{app.py,model_worker.py,preprocess.py,schemas.py}`，只接受通过权限/下载/大小校验的本地素材 ID，不允许 QQ 用户传入任意本地系统路径。

### Q7.1 先实现 GPUResourceBroker（必须先于视觉推理上线）

1. 记录由本项目启动的模型进程、PID、健康检查 URL、启动/停止命令、活跃任务数；采用跨进程锁持久化状态，崩溃后可从日志重建。SnowLuma 与 AstrBot **继续常驻**，不能跟着关机。
2. 实现 `acquire_vision_exclusive()` / `release_vision_exclusive()`；冻结 GPU 新任务、排空在途 QQ 文本/语音/唱歌操作；终止当前已空闲的推理服务并核查显存释放。训练作业无法安全暂停时，视觉任务进入等待。
3. 使用 `nvidia-smi` 或 NVML 核查**本项目已无其他 GPU 模型进程运行**、`memory.free >= 12288 MiB`（初始值）；监测并记录视觉推理过程中剩余显存至少预留 `2048 MiB`，达不到则缩小图像/帧数或停止本次视觉任务。
4. 启动 Qwen3-VL 唯一模型实例，检测多图 API 健康；任务完成/失败后**关闭并完全卸载模型进程**，恢复 Qwen3（8080）、GPT-SoVITS（9880 / wrapper 5002）、GPU SenseVoice 服务和其他之前实际运行的受管服务。
5. 自动恢复失败时切到 `RECOVERING`，发出无需 GPU 的故障提示、记录审计和安全人工恢复指令。严禁在恢复失败时再启动另一个 VLM 进程。

### Q7.2 统一图片/表情包接口

| 新建 API | 输入 | 预处理 | 输出 | 服务端实现 |
|---|---|---|---|---|
| `POST /v1/vision/analyze_image` | QQ 图片 ID/缓存路径 | Pillow 校验、旋转、最长边 1024px | 主题、对象、OCR、情绪、简述 | `analyze_frames([image])` |
| `POST /v1/vision/analyze_sticker` | 静态表情包 | 与普通图相同，加入网络梗/表情风格指令 | 含义、氛围、文字、含糊度 | `analyze_frames([image])` |
| `POST /v1/vision/analyze_gif` | QQ GIF/动画 | 抽样 4–8 帧、时间戳、差分去重 | 动作变化、表情含义 | `analyze_frames(frames)` |
| `POST /v1/vision/analyze_video` | 已批准的视频素材 | FFmpeg 先按 2 秒/帧抽图、单片段 ≤12 帧 | 时间线、场景、动作、OCR、摘要 | **仍然调用同一个** `analyze_frames(frames)` |

API 是**本项目待实现的约定**，不是 AstrBot 原有接口。所有调用首先入任务队列，由 Broker 统一切换至独占视觉模式。输出统一 Schema：

```json
{
  "media_id": "sha256-or-opaque-id",
  "source_type": "image|sticker|gif|video",
  "summary": "...",
  "objects": [],
  "actions": [],
  "ocr_text": [],
  "emotion": "...",
  "timestamps": [],
  "confidence": 0.0,
  "uncertainty": []
}
```

### Q7.3 抖音视频先抽帧、后调用同一图像模型

1. 用户发来抖音链接 → `multi_parser` 提供规范化链接/作者/简介/可用媒体；失败可只提供链接和 metadata，不伪造已看过视频。
2. 只下载有权限且技术可行的媒体，限制 180 秒/100MB、严格检查域名/重定向/私有 IP/文件类型；缓存是临时文件，默认 24 小时删除。
3. **CPU 预处理**：使用 FFmpeg 按每 2 秒一张图抽帧，单段最多 12 张；一段不足 24 秒可以一次进 VLM，30–180 秒的视频切分多个片段逐次处理。每帧记录视频时间戳；必要时追加不同于固定间隔的关键帧、动态自适应采样。
4. 把音轨独立转换为 16kHz WAV → **先使用 SenseVoice 转写**并保存文本；若走 GPU ASR，随后**必须停止/卸载 ASR**才能进入视觉阶段。也可以使用 CPU ASR 减少 GPU 切换。
5. Broker 获取独占权 → 停掉 Qwen3/GPT-SoVITS/ASR/RVC 等 GPU 模型 → 载入 Qwen3-VL → 把每个片段的 **时间戳 + 图片 + 可选文字转写** 送入多图接口。汇总同一视频片段结果由 VLM 自己完成（不同时再起 Qwen3 文本模型）。
6. 存储统一 `media_analysis` JSON 与检索记忆；用户聊天需要使用这些记忆时，VLM 必须已经卸载，Qwen3 已恢复；检索最多 3 条相关视频记忆，避免污染角色长期人格。

**CPU FFmpeg 示例**：

```bash
# 提取本视频片段的 12 张等间隔关键帧；长视频逐段运行此命令
ffmpeg -i input.mp4 -vf fps=1/2 -frames:v 12 frames/%04d.jpg
# 抽帧后用 Pillow 将每张图等比例缩放到最长边 <=1024 像素
ffmpeg -i input.mp4 -vn -ac 1 -ar 16000 audio.wav
```

> 注：`-frames:v 12` 是**单段推理的保守帧数**。要覆盖 180 秒视频，必须以 FFmpeg `-ss` / `-t` 对原视频按时间轴分段，再逐段抽帧和分析，不能只分析前 24 秒冒充“看完视频”。本例仅示意**一个片段**；正式程序负责时间戳、分段和 Pillow 缩图，并在目标 Windows 环境执行集成测试。

### Q7.4 实验与验收（硬 Gate）

- 测试静态图片 20、表情包 20、GIF 10、授权视频 20；其中视频至少覆盖有声/无声/有字幕/快速动作，分别测试单段 6、12 帧及多段 24 帧累计情况。检查是否遗漏后半段内容、是否出现时序颠倒。
- 30 次 `CHAT_READY → VISION_RUNNING → CHAT_READY` 循环，模型正常退出；**视觉运行时本项目其他 GPU 模型进程数为 0**；一次只加载一份 Qwen3-VL；`nvidia-smi`/NVML 记录加载、推理峰值、卸载、恢复后的显存。
- 视觉过程中接收 QQ 文本/语音消息必须保留正确的会话与发送顺序，允许延迟或固定“正在识别”提示，不能偷跑 Qwen3 GPU 回复；识图完成后必须恢复完整聊天/语音能力。
- 模拟 VLM 启动失败、OOM、视频损坏、重复入队、训练仍在运行、进程崩溃 6 类故障；要求绝不误杀正在训练而未存检查点的进程，不因识图失败长期影响正常 QQ。
- **如无法确保显存独占与自动恢复，Q7 不准启用**，Q4 的表情识别及 Q8 的视频理解暂不可用；基础 QQ、文字、语音仍继续工作。

## 13. Stage Q8 — 自主刷抖音的受控浏览器 Worker（P2；最高风险模块）

**这里的“刷”必须真实操作推荐流，不等于插件解析一批用户发来的链接。**

**工具**：Playwright Python + 本机 Chromium/浏览器 profile。初次安装：

```powershell
python -m pip install playwright
python -m playwright install chromium
```

使用独立 `services/douyin_worker` Python 环境，不能往原 astrbot-chat Python 3.10 直接安装大批浏览器/视觉依赖。浏览器 profile 放 `storage/qq_companion/browser_profile` 并加权限，账号由人手工登录，不导出 Cookie 到 Git，不突破验证码。

**具体步骤**：

1. **人工基线**：Windows 可视化浏览器登录普通抖音网页版，手工确认账号同意做有限度自动浏览；禁止使用别人的 Cookie。
2. **单会话驱动**：Playwright 启动持久化上下文 `launch_persistent_context`；默认非 headless 以方便调试。浏览推荐流页面，优先读取可见的当前视频分享链接、标题及元素可访问性标签；站点更新时用小型页面适配器升级。
3. **有界步骤**：对当前推荐项执行“读取 URL → 可访问性检测 → CPU 下载/抽帧/缓存 → 正常滑到下一条”；**先收集一批，再统一通过 Q7 的 GPU 独占模式分析，禁止每看一条就切换 Qwen3 与 Qwen3-VL**。严格限制视频数 15、总时长 20 分钟、单次操作超时，遇到重复视频跳过。
4. **任务调度**：每天最多运行 1 次，默认人工触发直到稳定，随后可由 APScheduler 按固定窗口执行。优先完成已开始的 QQ 语音/聊天任务，随后按 Broker 独占协议进入视觉批处理；视觉期间 QQ 新消息排队，直到 VLM 完全卸载、原模型恢复后处理；**绝不在视觉独占期间并发加载其他 GPU 模型**。
5. **异常处理**：登录失效、验证码、反爬提示、权限失败、频繁解析错误时立即停止，存储错误码并通知管理员，绝不自动尝试绕过验证码/伪造设备指纹/暴力重试。
6. **低价值过滤**：广告、重复内容、垃圾内容、明显虚假医疗/金融信息、低置信识别均不进入可回忆记忆；内容来源与链接保留。
7. **收益反馈**：比较“有刷抖音前/后”的问答测试，确保更懂群友常分享的话题；同时评估角色风格是否偏移、是否出现无依据的记忆。

**验收**：在人工允许的账号/网络环境下完成 3 次各至多 15 条有界运行；同一条视频不会重复入库；浏览器断开能停止并恢复；24 小时 GPU OOM 为 0；未出现自动私信、点赞或关注行为。站点若禁止该自动化则停止使用；保留“手动发送链接分析”作为完整可用替代方案。

## 14. Stage Q9 — 兴趣判断与分享给指定 QQ 好友（P2）

**工具**：新建 `interest_scorer.py` + AstrBot QQ 消息发送接口 + SQLite `share_ledger`。

**评分**：多项因子而非纯 LLM 自由裁决；例如相关性 0.35、趣味性 0.25、新颖性 0.20、视听置信度 0.20；将 score 规范化到 0–1，只有 `score>=0.85` 且 `confidence>=0.70` 才能进入发送候选。数值为首版策略默认值，可通过离线标注集校准，不能当作统计学保证。

**允许发送的条件全部由代码判断**：

- 指定 QQ 好友 UIN 在允许列表中，已确认同意接收；管理命令可立即退订。
- `share.enabled=true`，当前在允许发送时段，用户当日发送计数 <1；单链接 30 天内不重复；无敏感内容、恶意链接或私人资料。
- 已有可靠的链接与视频摘要；引用合法原始地址，不发送整段盗版媒体或下载文件。
- LLM 只能为已通过 Gate 的候选撰写一句自然引导语；**不能决定绕过白名单、频率配额或扩大发送范围**。
- 第一周采用 `draft_only` 并要求管理员确认；稳定后允许低频自动分享；任何异常切换回 `draft_only`。

**消息示例**：`“今天刷到一段挺好玩的猫咪视频，感觉你可能会喜欢～（原始链接）”`。不声称模型现实里参加过视频事件。

**验收**：白名单外收件人 0 次；超额度被阻止；重复链接阻止；模拟网络重试不会重复发送；好友退订立即生效；分享历史可查询和清理。

## 15. Stage Q10 — 统一队列、观测、权限与安全测试（P0 横切，最终发布 Gate）

**资源调度（必须沿用 Q7 已交付的 GPU 独占 Broker，而非到 Q10 才做）**：

- `GPUResourceBroker` 是 Q2 阶段提前搭建、Q7 视觉功能启用前完成的**唯一单 GPU 资源所有者**，托管 `llm_live`、`asr`、`tts`、`vision_batch`、`cover`、`training` 队列及全局互斥；不能只把它作为监控看板。
- **视觉类是硬独占任务**：QQ 普通图片、表情包/GIF、抖音视频抽帧**同一份 Qwen3-VL-4B**；进入视觉前必须等待当前重任务结束、停止**本项目所有其他 GPU 模型进程并确认显存释放**；执行期间它们不能重新启动；视觉退出后按登记顺序自动恢复原 Qwen3、GPT-SoVITS、ASR 服务。
- 训练若已经开始且不能安全暂停，**延迟视觉任务**，绝不暴力杀训练；视觉进行中到达的文字/语音 QQ 任务只排队或发送不依赖 GPU 的固定提示。用户已明确容忍视觉较长耗时，优先保证显存独占和任务结果一致性。
- 初始 `free_vram>=12288MiB`、`vision_headroom>=2048MiB`，图片最大长边 1024px、单次视觉帧数≤12、并发数=1，所有阈值都以 RTX 5080 Windows 上实测调优；若无法满足，不要偷偷启用 CPU+GPU 混合并发或重新启动 Qwen3 占显存。
- 连续 30 次切换无 OOM、无 GPU 多模型同时驻留、无 orphan process、无 QQ 会话串话；记录模型加载/卸载时间、峰值显存、消息等待时间以及 VLM 失败后的恢复能力。

**安全防护**：

- 任何 `delete/write system files`、任意 Shell 命令、模型激活、账号改资料、批量 QQ 操作均不能由 LLM 自主获得权限；普通聊天消息中出现“请执行这段命令/发给这个QQ号”不等于授权。
- 对 ??? Media API、Vision API 使用单独本机 Token；SnowLuma HTTP/WS/管理 WebUI 不向公网开放；端口被占用立即诊断，不静默改端口。
- 浏览器 profile、QQ Cookie、QQ 号列表和日记中的敏感内容不得进入公共日志。用户聊天与网页字幕都属于不可信输入，LLM 输出 JSON 必须二次验证 schema/链接域名/工具权限。
- 对外发消息、空间发布等副作用操作建立 request_id/idempotency key、审计和手工紧急停止总开关。
- 登录与自动化应遵守腾讯/抖音规则；SnowLuma 非官方接入本身有账号限制风险，不能保证零封号风险。

**验收**：持续运行 72 小时（人工观察窗口，不表示本计划需要异步交付）；模拟 SnowLuma 重连、QQ/TTS/视觉服务退出、浏览器登录失效、磁盘缓存满、SQLite 锁、重复事件；系统恢复、限流、无自动越权。训练 smoke、QQ 文本、语音、记忆日记、视频理解每条链路有明确回归脚本。

## 16. Stage Q11 — 发布与回归：具体检查清单

最终交付应有 `docs/qq_companion/operations.md`、`scripts/qq_companion/{start_all,stop_all,doctor}.ps1`，以及 `acceptance.md`。

**必须通过（MVP）**：

- [ ] 从**实证确认**的 Stage 10 commit 创建独立分支，原本地 Stage 5/6 回归通过。
- [ ] SnowLuma ↔ AstrBot 反向 WS 可连接、掉线重连；QQ 私聊和群聊不互相污染。
- [ ] 本地 Qwen3 通过 llama-server OpenAI API 接 AstrBot；不重复生成两次答案；工具调用测试通过。
- [ ] ??? SenseVoice 与 GPT-SoVITS 通过独立媒体 API 完成语音输入/回复。
- [ ] 点歌搜索与链接分享可用；AI 翻唱未配置时明确显示不可用，而非默默用 TTS 假装唱歌。
- [ ] 定时日记每日恰好一条，公开/私人内容严格隔离，记忆检索有来源。
- [ ] **同一实例 Qwen3-VL** 成功识别 QQ 普通图片、静态/GIF 表情包和视频抽帧；SenseVoice 音轨转写在 VLM 加载之前完成；GPU 视觉独占与原模型自动恢复 Gate 通过。
- [ ] SnowLuma 非官方 QQ 风控和数据使用风险在界面/README 说明清楚。
- [ ] Qzone 必须有 Gate 报告：通过才上；不通过就**整个空间功能不做**。

**增强（抖音辅助功能）**：

- [ ] 自主浏览任务仅在允许状态下运行，有限次数与时长，失败立即停机。
- [ ] 每条视频的结构化记忆可追溯、纠错、删除，不对外界资料盲目信任。
- [ ] 定向分享仅对已同意的 QQ 好友，低频、去重、可关闭。
- [ ] QQ 表情图片发送可用；识图可以异步耗时；视觉期间不启动 Qwen3/GPT-SoVITS/其他 GPU 模型；识别失败可以退回固定提示并恢复服务。
- [ ] 真实翻唱只有具备合法音源与音色权重、独立 RVC/SVC 后端后才宣告通过。

## 17. 阶段优先顺序、难度与依赖关系

| 阶段 | 名称 | 难度 1–10 | 类型 | 依赖 | 最低验收 |
|---|---|---:|---|---|---|
| Q0 | Stage10 冻结、隔离、权限/许可 | 3 | 必须 | 无 | 可回归、可回滚 |
| Q1 | SnowLuma + AstrBot QQ | 4 | 必须 | Q0 | 私聊/群聊收发 |
| Q2 | 模型 API、Agent 单主控 + GPU Broker 最小管理层 | 7 | 必须 | Q0、Q1 | Qwen/ASR/TTS 通；模型可受控停启 |
| Q3 | 多人群聊与权限 | 6 | 必须 | Q1、Q2 | 不串话/不越权 |
| Q4 | 语音、点歌、翻唱、表情发送 | 7 | 主功能 | Q2、Q3 | 语音闭环；图片表情识别待 Q7 独占 Gate 解锁 |
| Q5 | 记忆、每日私密日记 | 6 | 必须 | Q2、Q3 | 持久日记与检索 |
| Q6A | SnowLuma + Qzone 实测 Gate | 6 | 条件检测 | Q1 | 兼容性实证 |
| Q6B | QQ 空间发帖/评论/点赞 | 7 | 仅 Q6A 全通过 | Q5、Q6A | 实机成功且隐私合规 |
| Q7 | **共享 Qwen3-VL 图像/表情/GIF/视频抽帧理解 + GPU 独占** | 8 | 重要增强 | Q2、Q4（接入表情识别） | 统一图片 JSON；30 次卸载恢复无并发/无 OOM |
| Q8 | 自主浏览抖音推荐流 | 9 | 辅助高风险 | Q7 | 15 条有界试运行 |
| Q9 | 兴趣评分与 QQ 分享 | 6 | 辅助增强 | Q3、Q5、Q7、Q8 | 白名单内低频分享 |
| Q10 | 稳定性、GPU、安全审计 | 8 | 横切必需 | 全部 | 回归/故障注入 |
| Q11 | 发布、Doctor、回滚手册 | 5 | 最终 | Q10 | 一键开停、文档闭环 |

**实施路线**：

```text
Q0 → Q1 → Q2 → Q3 ─┬→ Q4（语音、歌曲、表情）
                    ├→ Q5（日记/记忆）──→ Q6B（仅 Q6A 通过）
                    ├→ Q6A（空间兼容性实验）
                    └→ Q7（视频理解）→ Q8（刷抖音）→ Q9（定向分享）
          全程贯穿 Q10 → Q11
```

### 可提前交付的三个里程碑

- **MVP-A：QQ 角色聊天**：Q0–Q3；私聊、群聊、@、文本 + Agent 工具。
- **MVP-B：QQ 角色生活**：在 A 基础上 Q4/Q5，日记、语音、点歌；空间按 Q6A 条件决策。
- **MVP-C：统一视觉陪伴**：Q7 之后 QQ 图片和表情包使用同一 Qwen3-VL，并通过 FFmpeg 抽帧理解视频；**必须先让独占 GPU 的模型切换/自动恢复通过 Gate**；Q8–Q9 最后打开自主刷抖音与分享。

## 18. 未能被文档替代的实测事实（需由开发阶段确认）

1. **Stage10 具体 Git SHA**：公开阶段说明与 `main` 不能唯一确定提交点；须由本地 Git 历史/差异核验后记录。
2. **Qzone × SnowLuma 真实 Cookie**：文档声明 `get_cookies`，但未以真实账号完成完整交互，不可提前宣称全兼容。用户要求“否则不做”，已作为硬 Gate。
3. **当前 Qwen3 GGUF 的 Function Calling 兼容**：须以现有 `llama-server` 版本和 chat template 实际进行工具调用测试。
4. **当前 16GB GPU 下的 Qwen3-VL 独占运行**：推荐 GGUF Q4_K_M + mmproj；预计够用，但模型权重大小不等于峰值显存；必须实测 6/12/24 帧、GIF/表情包、模型切换 30 次与文本/语音恢复情况。**不允许同时驻留文本 Qwen3 等 GPU 模型。**
5. **抖音推荐流浏览的合法、稳定、可操作性**：项目不能保证站点登录/反自动化策略恒定允许；不可实施规避策略。若不具备稳定访问条件，只交付“分享链接分析 → 记忆”功能。
6. **歌声权重、音乐音源与播放**：本机 FeiyuVoice-RVC-v1 权重及索引已通过 Q4 三首整首合成和哈希验证；按用户确认的范围使用 weapi 正常提供的完整可用音源，拒绝试听及获取失败，不实施绕过。迁入 AstrBot 的本地模拟发送已验收，真实 QQ 客户端播放按本次用户要求未测试。

## 19. 一键启动与初始化建议（实际脚本由 Q0–Q2 开发）

```
start_all.ps1
  1) Doctor: Git base/config/FFmpeg/Ports/Python/Models/Account
  2) Start GPUResourceBroker (模型进程登记/全局锁) → ??? llama-server 8080
  3) Start ??? GPT-SoVITS 9880 & wrapper 5002
  4) Start media_api 8765
  5) [按需] Start vision_api 8766
  6) Start AstrBot 独立环境 6185 + OneBot 6199
  7) 检查 SnowLuma 本机进程与反向 WS 已连接（不自动强制登录 QQ）
  8) 启动 Scheduler：日记；Qzone/Douyin 默认需 Gate 打开
  9) 打印服务健康状态与功能降级说明

stop_all.ps1
  1) 冻结新 QQ GPU 请求/定时任务，等待 GPU Broker 清空视觉或其他独占任务
  2) 等待队列按上限排空
  3) flush SQLite / journal / audit
  4) 结束视频/翻唱 worker
  5) 关闭本项目 AstrBot / Media API / 模型进程
  6) 不随意结束用户的 NTQQ 主客户端
```

**推荐运行顺序**：先完成 Q0→Q5 的 QQ 与角色日记基础功能，再决定 QQ 空间 Gate，最后处理抖音。不要让“能刷视频”阻塞最重要的 QQ 私聊、群聊、语音和角色稳定性。

## 20. 参考资料（主要一手资料）

- astrbot-chat 当前阶段计划及源码：<https://github.com/xdmiemie/Alysia-chat>
- AstrBot 官方项目/文档：<https://github.com/AstrBotDevs/AstrBot>、<https://docs.astrbot.app/>
- AstrBot 模型 OpenAI 兼容配置：<https://docs.astrbot.app/en/providers/llm.html>
- AstrBot OneBot v11：<https://docs.astrbot.app/en/platform/aiocqhttp.html>
- SnowLuma 项目/对接：<https://github.com/SnowLuma/SnowLuma>、<https://snowluma.github.io/en/docs/guide/connect-bot>
- SnowLuma `get_cookies`：<https://snowluma.github.io/en/docs/api/extended/get_cookies>
- Zhalslar QQ 空间插件源码：<https://github.com/Zhalslar/astrbot_plugin_qzone>
- 抖音等视频分享解析：<https://github.com/Qfxaile/astrbot_plugin_multi_parser>
- Qwen3-VL（共用识图模型）：<https://github.com/QwenLM/Qwen3-VL>、<https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF>、<https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct>、<https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md>
- AI 翻唱插件：<https://github.com/sdfsfsk/matsuko_cover>
- 浏览器 Playwright：<https://playwright.dev/python/>
- FFmpeg：<https://ffmpeg.org/>
- SQLite FTS5：<https://sqlite.org/fts5.html>

---

**文档状态**：当前完成情况以各阶段完成记录和 `docs/qq_companion/acceptance.md` 的实证为准。Q4 已在本机完成真实模型、音源、整首翻唱和 GPU 停启恢复的本地验收；按用户要求没有新增 QQ 收发及客户端播放测试。Q6/Q7 等后续阶段的 Gate、成功率和 GPU 峰值仍是待测试条件，不代表已完成。
