# astrbot-chat：基于 Stage 10 核心的 AstrBot + SnowLuma QQ AI 与抖音学习计划

> 计划版本：1.3（2026-11-01，修订：Qwen3.5 原生多模态，取消识图模型切换）\
> 上游来源：<https://github.com/xdmiemie/Alysia-chat>；本地项目和分支名：`astrbot-chat`  
> Stage 10 基线：`aca495e801f061210ce4961da8df2f63ab3f3c44`；当前分支：`astrbot-chat`  
> 目标平台：Windows 11（本项目模型运行及训练）、SnowLuma Windows 版 + NTQQ、AstrBot 独立 Python 3.12 环境；运行在同一台电脑；后续可以将 AstrBot 改为 Docker/WSL。  
> 现有硬件基线：Ryzen 7 9800X3D / RTX 5080 16GB / 32GB RAM；聊天与识图共用常驻 Qwen3.5，优先保证显存余量和请求队列稳定性。\
> 范围：QQ 私聊/群聊、文字/语音/媒体理解、点歌/翻唱、每日角色日记、条件启用 QQ 空间、辅助性的抖音浏览—理解—记忆—定向分享。**不做无人监督支付、批量营销或验证码绕过。**

## 版本 1.3 变更摘要（当前执行方案）

- 对话、QQ 图片、静态/GIF 表情包和视频关键帧统一使用本地 `Qwen3.5-9B-Q8_0.gguf`，复用 `llama-server:8080`。一次请求可同时包含文字和图片，不要求先将图片描述成文字再交给聊天模型。
- 取消识图专用 Qwen3-VL、`8081` 模型服务、视觉模型装卸状态机及其 Broker 独占验收；Q7 改为接通主模型的原生多模态输入。`vision_api:8766` 仅作为可选的素材预处理/结构化分析封装，不加载另一份模型。
- 保留图片校验、缩放、GIF/视频抽帧、共享请求队列、超时与显存观测；QQ 前台请求优先于后台视频片段。翻唱和离线训练已有的资源互斥独立保留，不再作为每次识图的前置步骤。
- **当前实证边界（2026-11-01）**：Q7 已接入并校验配套 `mmproj-F16.gguf`，复用活动 `qwen3.5-9b-q8-0-base-v1` 的常驻8080服务；图片20、表情20、GIF10、视频20及30轮图文交替真实推理通过。真实QQ收发仅限好友 `1301049831`，最终结果与限制见 `docs/qq_companion/acceptance.md`。

## 版本 1.2 变更摘要（历史记录，模型选型已由 1.3 替代）

- 当时对话主模型改为本地 `Qwen3-14B-Q4_K_M.gguf`。保留旧模型和 LoRA 训练、导出代码；旧 8B/14B LoRA 不可直接套用 Qwen3.5-9B，后续训练需匹配新基座并单独验收。
- elysia 的 GPT-SoVITS 指定 `model_store/registry/gpt_sovits/elysia/bailu-v1/`，使用 `Bailu-e10.ckpt`、`Bailu_e10_s820.pth` 和该目录的四种参考音频。`gentle` 映射 neutral，`serious` 映射 sad。
- 翻唱指定 `model_store/registry/rvc/feiyu/v1/` 的 `FeiyuVoice-RVC-v1.pth` 与同名 `.index`；推理环境和 Applio 源码在 `envs/song/`、`external/Applio/`，不提交模型权重。
- 移除 fish 角色与旧 TTS 模型的活动目录；旧 TTS 权重本机移到 `model_store/rollback/`。QQ 的历史 Stage 14A 部分代码归档于 `integrations/qq_astrbot/reference/`，供后续 AstrBot 插件迁移使用。

## 版本 1.1 变更摘要（历史记录，视觉方案已由 1.3 替代）

- 当时采用独立 Qwen3-VL-4B 视觉进程，识图前卸载聊天/语音等 GPU 模型，结束后恢复。该方案及专门为其设计的 GPUResourceBroker 视觉切换要求已取消，不作为当前开发或验收依据。
- 继续保留媒体统一处理、视频分段抽帧、资源压力测试和训练检查点保护。

## 0. 结论、能力边界与总难度

### 0.1 总结

- **整体可实现，但不是把几个开源仓库装好就自动得到全部功能。** QQ 基础收发难度较低；模型服务整合、音频、日记、记忆为中等难度；本地视频视觉理解、真正“自动刷抖音”和持续维护网页登录最难。
- **AstrBot 作为唯一的 QQ 消息入口、Agent Tool 调度器和会话主控**；**SnowLuma 只负责 QQ 协议层**；**astrbot-chat 负责已有模型、人格资产、训练管线及自建服务**。禁止让 AstrBot 和旧 `AstrbotChatCore.process()` 在同一条消息上各运行一轮独立 LLM、各记一份聊天历史。
- **“学习”第一版专指经过过滤、归纳、检索的持久化记忆（RAG），不自动修改 Qwen3 模型权重。** 现有 Qwen3 / GPT-SoVITS 训练模块作为人工发起、离线评估、可回滚的独立工作流继续保留。
- **QQ 空间按 Q6 最新用户规则分能力启用**：账号/Cookie/自己列表/发布/详情/权限/删除通过后可启用自身发布；好友阅读、评论、点赞分别保留未验收 Gate。2026-10-30 已完成自身十条发布及删除实测，好友互动待测试；不擅自切 NapCat。
- 语音回复用现有 GPT-SoVITS；**唱歌/AI 翻唱另外需要人声分离 + RVC/SVC/SoulX-SVCVC + 歌曲许可与模型权重**，GPT-SoVITS 的 TTS 不能直接替代歌唱模型。
- **视觉能力唯一入口**：QQ 图片、静态/GIF 表情包、抖音短视频关键帧都直接送入**同一个常驻 `Qwen3.5-9B` 的多模态接口**；图片与用户文字、角色指令和授权上下文一起参与推理。视频先经 FFmpeg 抽为带时间戳的图片，音轨另用已有 SenseVoice 转写。
- **取消识图模型调度**：识图不停止聊天 LLM、不加载独立 VLM、不在完成后重载 LLM；文字与图片请求共用 `8080` 服务和请求额度。请求排队、像素/帧数/上下文限制与显存监测仍需实现；这不表示可无限并发，翻唱/训练原有资源策略仍有效。
- 抖音自动浏览采用**有界任务**：由本机登录的浏览器运行，每天至多一个定时任务，单次至多 15 条/20 分钟；CPU 下载/抽帧、可选 ASR 后按片段低优先级调用常驻 Qwen3.5，片段之间让出前台聊天请求；出现登录失效、验证码、封禁提示即停机并请求人工处理；不实现规避平台风控、指纹伪装或批量抓取。

### 0.2 能力来源真实性清单

| 功能 | 负责组件 | 当前状态 | 新开发量 |
|---|---|---|---|
| QQ 私聊、群聊、@、发送文本/图片/语音、基本引用 | AstrBot OneBot v11 + SnowLuma | 已有接口；需实测 | 小 |
| 多人群聊上下文、群聊触发策略 | AstrBot + 自定义路由插件 | 基础上下文已有；触发/身份处理需定制 | 中 |
| 文本生成 / Agent / Tool Calling | AstrBot 内置 Runner + llama.cpp 本地模型 | 已有机制；须验证 Qwen3 工具调用格式 | 中 |
| 语音识别、角色语音 | ??? SenseVoice + GPT-SoVITS | Stage 0–10 已有模型代码；需 HTTP 化 | 中 |
| 点歌、AI 翻唱 | `matsuko_cover` 等 + 独立 RVC/SVC 服务 | 有插件；仍需搭建转换后端 | 中高 |
| **表情包、普通图片、视频关键帧识别（共用主模型）** | FFmpeg/Pillow + **常驻 Qwen3.5-9B 多模态接口** | Q7 已实现；真实模型与容量验收通过，QQ闭环结果见验收记录 | 中 |
| 表情包发送 | QQ 图片接口 + 自维护的素材库 | 可直接做 | 小 |
| 每晚日记 | 新定时器、记忆摘要、日记表 | **不是 AstrBot 自动内置的“每天自己写日记”功能** | 中 |
| QQ 空间发说说、评论、点赞 | Zhalslar Qzone 插件 | 接口层可能兼容，需先验收；未通过则不做 | 条件项 |
| 抖音链接解析、取元数据/视频文件 | `astrbot_plugin_multi_parser` 参考其解析实现 | 已有解析器；独立服务需适配 | 中 |
| 真正自己刷抖音推荐流 | Playwright 浏览器任务（自研） | 需要登录环境及流程控制；非解析器功能 | 高 |
| 视频理解、记录、生成检索记忆 | FFmpeg 抽帧 + 可选 SenseVoice 转写 + 常驻 Qwen3.5 + SQLite/FTS5 | 需要自研；按片段限制资源，不切换识图模型 | 高 |
| 觉得有趣并给指定 QQ 好友分享 | 兴趣评分 + allowlist + SnowLuma/AstrBot 发送 | 需要自研 | 中 |

建议总体难度 **7.5/10**；若不要求自动刷抖音、只接收链接分析，可降为约 **5.5/10**。难度评分是工程预估，不是实测工期。

## 1. 项目调查结果与准入条件

### 1.1 astrbot-chat 当前实际情况

以下为 Stage 10 基线调查记录；当前模型与目录以本计划 1.3 修订及本地清单为准：

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
4. **Gate 条件（2026-10-30 按用户新范围调整）**：当前账号认证读取自己列表/详情、发布并查到测试内容、核对指定可见范围、删除并确认消失后，自身发布可用。好友阅读、评论、点赞暂不实测，必须另有真实可见证据才启用。日志不记录 Cookie；自身必要前提失败则关闭发布，Cookie 异常立即停止空间模块。

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
          可选 SenseVoice 转写 → Qwen3.5:8080         │
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
        → 可选 SenseVoice 转写 → 常驻 Qwen3.5 分析关键帧 + 文字 → 结构化审核/过滤
        → SQLite 记忆 → Agent 日记/日常聊天检索
        → 评分与允许列表 → AstrBot 发 QQ 原始链接
```

**防止“双 Agent”**：微信分支 Stage 10 的 `AstrbotChatCore.process` 会自行调用 LLM；在 QQ 分支中，不能让 AstrBot 的 Agent 先生成回答再把回答当用户输入交给 `AstrbotChatCore.process`，否则将再次生成回复。应拆出 `ASRService`、`TTSService` 和只读 `CharacterRegistry` 供 QQ 插件调用，`AstrbotChatCore` 保留用于本地旧入口的兼容；AstrBot 通过 `llama-server /v1/chat/completions` 直接调用本地 Qwen3.5，**聊天历史只由 AstrBot 写入**。

## 3. 具体软件/模型/获取地址与对接方式

| 组件（实际选择） | 官方仓库/下载地址 | 版本/运行边界 | 对接 |
|---|---|---|---|
| astrbot-chat | <https://github.com/xdmiemie/Alysia-chat> | Stage 10 基线独立分支 | 保留训练、模型、角色/ASR/TTS |
| AstrBot | <https://github.com/AstrBotDevs/AstrBot> / <https://docs.astrbot.app/> | 独立 Python 3.12、锁定经测试 release | OneBot v11、LLM Provider、AstrBot Plugin |
| SnowLuma | <https://github.com/SnowLuma/SnowLuma> / <https://github.com/SnowLuma/SnowLuma/releases> | Windows x64 + 已登录 NTQQ；非商业许可证 | OneBot v11 反向 WS → AstrBot `6199/ws` |
| AstrBot OneBot 指南 | <https://docs.astrbot.app/en/platform/aiocqhttp.html> | 使用逆向 WS 服务端适配器 | 确认消息协议/端口/Token |
| SnowLuma 连机器人 | <https://snowluma.github.io/en/docs/guide/connect-bot> | 同机用 `ws://127.0.0.1:6199/ws` | `wsClients` 指向 AstrBot |
| llama.cpp | <https://github.com/ggml-org/llama.cpp> | 继续复用项目本地 llama-server | OpenAI 兼容 `http://127.0.0.1:8080/v1` |
| **Qwen3.5-9B（聊天与识图共用）** | <https://huggingface.co/Qwen/Qwen3.5-9B> | 当前 `Qwen3.5-9B-Q8_0.gguf`；原生支持图文输入，GGUF 部署需补齐匹配的视觉组件 | AstrBot 对话与媒体理解统一模型，端口 8080 |
| SenseVoice | <https://github.com/FunAudioLLM/SenseVoice> | 复用现有本地模型 | ??? Media API `/v1/audio/transcriptions` |
| GPT-SoVITS | <https://github.com/RVC-Boss/GPT-SoVITS> | elysia `bailu-v1`，本地权重 + 9880 上游及 5002 wrapper | API `/v1/audio/speech` |
| FFmpeg | <https://ffmpeg.org/download.html> | 复用 `tools/ffmpeg` | 音频转码/视频取帧 |
| llama.cpp 多模态推理 | <https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md> | 复用主 `llama-server:8080`，启动时加载主 GGUF 和匹配的 `mmproj`；锁定支持 Qwen3.5 视觉的构建 | OpenAI Compatible 图文/多图输入，不另启视觉 LLM 端口 |
| Qwen3.5 视觉组件（待接入） | <https://huggingface.co/Qwen/Qwen3.5-9B> / <https://huggingface.co/docs/transformers/en/model_doc/qwen3_5> | 从对应 GGUF 发布源取得，或由同一基座经验证转换；登记来源、版本、哈希与兼容性 | 视觉编码器/投影组件属于同一多模态模型，不是第二个聊天模型 |
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
│   │   ├── app.py               # 可选轻量媒体分析 API，不加载模型
│   │   ├── model_client.py      # 调用常驻 Qwen3.5:8080，保留图文 content
│   │   ├── preprocess.py        # 静态图/GIF/视频按时间戳抽帧
│   │   └── schemas.py           # analyze_image/analyze_video 的结果契约
│   ├── gpu_broker/
│   │   ├── broker.py            # 沿用翻唱/离线训练互斥，不承接识图模型切换
│   │   ├── process_registry.py  # 只管理本项目启动的 GPU 进程
│   │   ├── vram_probe.py        # nvidia-smi/NVML 记录显存余量与峰值
│   │   └── recovery.py          # 既有翻唱/训练任务失败恢复
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

以下为概念配置示例；Q7实际入口为 `configs/runtime/qwen3.yaml`、`configs/qq_companion/vision.yaml`、活动模型清单与分能力 `vision_gates.json`，示例字段名不作为运行配置契约。

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
    base_model_id: "qwen3.5-9b-q8-0-base-v1"
    gguf_path: "model_store/registry/qwen3/elysia/qwen3.5-9b-q8-0-base-v1/Qwen3.5-9B-Q8_0.gguf"
    mmproj_path: "<待登记的匹配 Qwen3.5-9B 视觉组件本地路径>"
    multimodal_enabled: false      # Q7 实测通过后启用，不能只凭模型名称打开
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
    base_url: "http://127.0.0.1:8766"  # 可选预处理/结构化封装，无独立模型
    enabled: false
    backend_api_base: "http://127.0.0.1:8080/v1"
    use_main_qwen: true
    single_model_for_images_gifs_video_frames: true
    ffmpeg_video_frame_interval_seconds: 2
    segment_seconds: 24             # 时间轴每段约24秒，末段按实际剩余时长
    max_frames_per_segment: 12      # 每2秒1帧；60秒视频分为12、12、6帧
    max_image_long_edge: 1024
  inference_queue:
    max_inflight_requests: 1         # 文字与图片共享；后台调用也必须计入
    foreground_before_background: true
    yield_between_video_segments: true
    on_busy: "queue"

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

## 4.4 **强制设计约束：主 Qwen3.5 原生图文推理，不切换识图模型（P0）**

### 4.4.1 识图原理与一次请求的输入

Qwen3.5 是带视觉编码器的原生多模态模型。图片经解码、缩放和分块后，由视觉编码器提取特征，再通过投影/合并组件形成语言主干可使用的视觉表示；文字则由 tokenizer 编码。视觉表示与文字表示一起进入模型上下文，模型据此生成回答。**内部视觉编码不等于先生成一段图片描述文字**；OCR、描述、推理都可以是本次生成的结果。依据：<https://huggingface.co/Qwen/Qwen3.5-9B>、<https://huggingface.co/docs/transformers/en/model_doc/qwen3_5>。

一次 `POST http://127.0.0.1:8080/v1/chat/completions` 可同时传入角色指令、授权聊天历史，以及用户消息中的文字和一张/多张图片。兼容接口的用户消息示意（占位内容需替换为真实图片数据）：

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "这张表情包是什么意思？结合我们刚才的话回答。"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,<真实图片的base64>"}}
  ]
}
```

- `image_url`/base64 是图片的传输格式；服务端解码为像素，再编码为视觉特征，不把 base64 当普通文字送入 tokenizer。
- 普通 QQ 图片问答直接保留原图、用户文字和上下文参与同一轮回复生成，不额外固定插入“先描述图片、再调用一次 LLM 回答”的链路。原有 Agent 工具循环需要的后续调用照常执行。
- 视频分段、缓存分析和长期记忆可另存结构化摘要；这是应用层处理，不是识图必须经过的中间步骤。聊天历史仍由 AstrBot 唯一管理，素材缓存不另建会话历史。
- `mmproj` 在 llama.cpp GGUF 部署中承载配套视觉编码/投影组件，与主 GGUF 同属一套多模态模型；它需要在主服务启动时接入，不是在每张图到来时切换另一套 LLM。启动方式依据：<https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md>。

### 4.4.2 请求流程与服务生命周期

```text
QQ 图片 + 用户文字 + 授权上下文
       ↓
素材校验 / CPU 缩放、抽帧 / 请求预算检查
       ↓
共享请求队列 → 常驻 Qwen3.5:8080（主 GGUF + 匹配 mmproj）
       ↓
直接生成角色回复，或按媒体分析任务生成结构化结果
       ↓
AstrBot 按原会话发送 / 后台任务存储记忆
```

1. 任务携带 `request_id`、素材哈希、来源/会话、权限和截止时间，先完成下载、大小、格式检查。
2. 图片与文字使用同一服务和同一请求额度，首版总并发设为 1；前台 QQ 请求优先，后台视频在片段之间让出队列。串行请求不涉及模型卸载或切换。
3. QQ 前台与后台分析必须共用入口限流，不能只对 `vision_api` 限流而让 AstrBot 直连请求绕开额度；Q7 须以实际请求日志验证统一生效。
4. 正常识图全程保持主服务和视觉组件常驻，不停止 GPT-SoVITS/ASR，不启动第二份模型；是否允许其他音频 GPU 任务并发，沿用已有容量/互斥策略并实测，不因改用多模态就放开限制。
5. 图片无效、超时、超预算时只结束对应任务；可缩小图片或减少帧数后有限重试。若主服务因 OOM/崩溃不可用，按现有服务故障恢复处理，并保留队列和会话身份。异常恢复不作为正常识图的装卸流程。
6. 不再实现 `acquire_vision_exclusive()` / `release_vision_exclusive()`、停止主 LLM 后加载专用视觉模型、以及每次识图结束重启主 LLM。翻唱和离线训练已有互斥机制单独保留，训练不得被无检查点强杀。

### 4.4.3 图片、表情和视频的统一处理

- 静态图片/表情：Pillow 校验格式、EXIF 旋转、转 RGB、等比例缩放，图片与用户问题一起送入 Qwen3.5；网络梗或小字无法确认时明确表达不确定性。
- GIF/APNG：先抽取带时间戳的代表帧，默认 4 张、最多 8 张并去重；作为多图输入同一个模型，不能只读第一帧却声称理解整段动画。
- 视频：CPU FFmpeg 默认每 2 秒一帧、每段预处理最多 12 张，30–180 秒内容分段覆盖完整时间轴。实际单次图片预算须同时满足显存和上下文限制，必要时缩小批次，不保证 12 帧都能单批运行。
- 音轨：可选 SenseVoice 转写并附上时间戳，与图片和问题在同次请求中提交。CPU ASR 可与抽帧并行；GPU ASR 沿用既有资源限制，不新增“为识图卸载全部模型”的要求。
- 后台视频分析只生成结构化结果/候选记忆，不占用其他用户的聊天历史；缓存按素材哈希、预处理参数、模型及提示词版本区分，避免复用不匹配结果。

### 4.4.4 推理规格与交付约束

| 项目 | 首版规格 | 可调整边界 |
|---|---|---|
| 模型 | **活动 Qwen3.5-9B-Q8_0 + 匹配的视觉组件，单个主 llama-server:8080** | 组件来源、构建和兼容性须实际验证 |
| QQ 静态图/表情 | 长边不超过 1024px；原图与文字直接推理 | 小字可用局部裁剪复核，计入图片预算 |
| GIF/动图 | 默认 4 张、最多 8 张，保留时序与去重 | 按实测减少单批图片数 |
| 短视频 | 每 2 秒一帧、每段最多 12 张预处理图片 | 超预算进一步拆批，逐段覆盖全部内容 |
| 模型生命周期 | 正常图文请求共用常驻模型，不执行识图装卸 | 故障恢复沿用主服务机制 |
| 请求并发 | 文字/图片共享 `max_inflight_requests=1` | 仅压力测试通过后调大，不自动推定可并发 |
| 上下文 | 以当前 `configs/runtime/qwen3.yaml` 为准，目前 32768 | 文字、工具、视觉 token、输出和余量共同计入 |
| 响应 | 单图走正常 QQ 回复；视频后台分段处理 | 在队列/请求截止时间内等待，不承诺实时视频 |
| 分析输出 | JSON 至少含 `media_id,source_type,summary,objects,actions,ocr_text,emotion,timestamps,confidence,uncertainty` | 与普通角色回复分契约；缺省字段为 null/空数组 |

### 4.4.5 9800X3D + RTX 5080 16GB + 32GB RAM 的容量评估

- 当前活动模型是 9B Q8_0，旧 4B Q4 视觉方案的 5–9GB 估算和“启动前空闲 12288MiB”门槛已失效。主模型常驻时，应测量增加视觉组件后的基线及每次图文推理的增量峰值。
- 峰值由主权重、视觉编码器、32K 上下文缓存、图像分辨率/数量和其他服务占用共同决定；Q7按限定预算实测GPU总占用峰值13322MiB、最低空闲2656MiB，不代表任意多图输入都足够。
- 先限制像素、帧数、上下文和并发，测量安全余量；若 Q8_0 加视觉组件仍无法满足预算，记录阻塞及可复现数据，再评估组件 CPU offload、上下文或量化调整，不静默更换当前主模型，也不回退到识图模型热切换。
- 9800X3D 承担 CPU 抽帧/缩图/浏览器工作；32GB RAM 还需记录 Windows、浏览器、模型和缓存的峰值与分页情况。

**设备级验收 Gate**：单图 20 次、表情 20 次、GIF 10 次、6/12 帧及累计 24 帧分段视频测试；另做 30 轮文字↔图片交替请求，核对主模型 PID 不变且无重复加载。通过 `nvidia-smi`/NVML 记录常驻基线、视觉增量峰值、延迟和请求等待时间；Windows WDDM 下按项目进程登记与总显存交叉验证。正常请求无 OOM，失败后文本/语音链路仍可使用，具体 Gate 见 Q7.4。

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
- GPU 离线训练继续与在线推理受控互斥，训练需要明确操作员授权并保护安全检查点；图文请求同属主 Qwen3.5 在线推理，不为识图另建模型切换状态机。训练占用资源时请求按现有策略等待或报告不可用，不强杀训练进程。
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
- GPU 策略：翻唱继续沿用已交付的排队、互斥及必要的主模型卸载/恢复流程，单次同时一个任务。识图属于主 Qwen3.5 在线推理，翻唱占用资源期间同文字请求一起等待；不额外增加视觉模型切换。缓存大音频设清理周期。
- 注意要做消息触发入口，写好提示词，让大模型提取唱歌意图，结构化输出唱歌意图结果，触发程序翻唱，AI先回复等等之类的话再去翻唱，翻唱完发给用户
- 全过程不能破坏项目的AI的拟人特性，用户视角的流程为 用户发起有唱歌意图的对话-AI回复等等、等我一会之类的话-AI后台进行翻唱-翻唱完成直接发送结果-根据AI特性AI继续发消息类似唱得怎么样、唱得不好不要笑我之类的这个由大模型决定。

### 9.3 表情包

- **识别**：普通 QQ 图片、静态表情包、GIF/动图抽样帧直接送到 **Q7 接通的常驻 Qwen3.5 多模态接口**，与用户文字和授权会话上下文一起推理。结构化分析按需输出内容/情绪/画面文字/动作/不确定性；执行 §4.4 的素材和请求预算检查，不停止主 LLM、不加载另一份视觉模型。
- **发送**：首版建立本地授权表情素材库 `storage/.../stickers/`，按 `tags` 选 PNG/JPEG/GIF 作为普通 QQ 图片发送；文字表情包可用 Pillow 增添文字，并设置大小/裁剪检查。**不以 QQ 原生收藏表情为依赖**。
- 图片理解依赖 Stage Q7 的原生多模态接入；**Q4 已有表情图片发送继续可用，待 Q7 图文输入与容量 Gate 通过后启用图片/表情识别**。服务繁忙时进入共享请求队列，始终保留原会话和发送顺序。

**验收**：输入语音/输出语音各 20 次；TTS 长段退回与错误兜底；点歌 10 次、错误歌曲 5 次；翻唱只有完整配置并成功实际合成 3 首完整测试歌曲才算交付，片段测试不替代整首验收；图片表情发送 20 次。按本次用户要求，涉及 QQ 发送的测试使用本地事件及模拟传输完成，不连接 QQ；本地通过不得记为真实 QQ 播放已验证。

## 10. Stage Q5 — 持久化记忆、隐私与每天的角色日记（P1）

**完成记录（2026-10-30）**：Q5 实现及本地验收完成，插件 `0.10.0`。新增记忆/日记测试117项，全部QQ相关测试411项及原Stage 5/6/10与训练回归33项通过；真实Qwen提取、敏感事实隔离、纠正、提示词回忆、64条记忆压缩和私人/公开候选日记通过，返回`Q5_MODEL_PASS`。全程零QQ连接、零QQ发送；七天为模拟验收。详见 `docs/qq_companion/q5.md`、`acceptance.md`。

**用户已定规则（2026-10-30，替代本节旧的过期/遗忘接口设计）**：

- 普通偏好、重要事件自动记忆；敏感信息同样可自动记录，不再询问同意。
- 原始事实按机器人、角色及私聊/群严格隔离；跨会话只使用处理后允许共享的泛化摘要。
- 长期记录无限期保存；按实际模型上下文分配记忆预算，超限压缩本轮召回结果，保留原始记录及修订历史。暂不提供“忘记某人”或清除接口。

**实现与步骤**：

1. `memory_repository.py` 建立SQLite事实表、FTS5及同步触发器、持久处理队列、修订、压缩缓存、来源关系、账号、日记与任务租约。所有模块随AstrBot插件部署，不复制完整聊天历史；正式数据库是`storage/qq_companion/memories.sqlite3`。
2. 通过入站去重与群策略的文字、成功ASR转写自动入队。后台使用同一个本地Provider与GPU准入租约，只读提取明确事实；由程序绑定owner和发言者，同属性纠正更新活动版本，原版本保留。失败退避、重启恢复，成功后清空队列原文。
3. SQL先授权后FTS检索，中文双字词与英文词法检索结合重要性、置信度、更新时间；最多召回64条，视频最多3条。共享区只含代码按普通话题分类选择的固定泛化投影，不复制私人自由文本、昵称、QQ号或来源消息。
4. 每次主Agent/主动补发临时注入`relevant_memories`，固定规则在`configs/qq_companion/prompts.yaml`。当前32768上下文最多分配8192记忆token，并扣除系统、工具、当前轮次、1536输出及256余量。用真实分词计算，超限生成并缓存有来源关系的压缩摘要；不修改人格文件或模型权重，不把召回资料重复存进聊天历史。
5. APScheduler按已解析IANA时区23:30生成日记，并每分钟恢复遗漏日期。日期D覆盖前日23:30至当日23:30，末段消息计入次日；无素材也可记录平静感受，不虚构交流。未完成提取的素材会延后日记。每次最多八条高置信素材，生成200～400字`[私人]`日记，按机器人/角色/日期唯一事务提交。
6. 私人混合来源日记归角色私密区，不供普通QQ会话直接召回。公开候选用独立请求，只输入泛化资料，生成后作为共享元记忆。`diary_public_candidates`视图只提供`privacy_level=public_candidate`候选，供Q6通过Gate后使用；Q5不发布空间内容。
7. 本机CLI提供查询，插件配置可禁用记忆或日记；不暴露QQ记忆管理工具及遗忘接口。外部字幕、记忆和模型输出均视为数据，结构校验、指令样式过滤、整理请求禁用工具；视频来源不升级为用户个人事实。

**验收**：七天模拟每日恰好一篇；跨日、时区/DST、重试/重启不重复；敏感事实不串私聊/群/机器人/角色；修订后不继续召回旧事实；压缩不丢数据库原记录；100条字幕伪指令不触发工具或外发。真实模型验证完整请求压缩至1841 token，私人/公开候选日记217/208字。真实连续运行七天不在本次本地验收范围内。

## 11. Stage Q6 — QQ 空间兼容性 Gate（条件项，P1）

**最新用户规则（2026-10-30，覆盖旧的全能力一次性 Gate 和发布频率）**：常规总结每天一条、精简两句话；重要事件不设每日次数限制，单独去重。具体时间、候选处理和故障恢复由开发决定并提供可修改入口。仅对当前登录账号进行最多十条自身发布测试，验证删除后可继续并清理；不测试好友评论/点赞，测试内容不进入聊天上下文和记忆。两个 QQ 群中的三个既定好友作为互动/可见名单，不自动扩充。

**完成记录（2026-10-30）**：Q6 开发完成，插件 `0.11.0`；自身十条临时说说均确认删除，账号/Cookie/自己列表/详情/发布/仅自己及指定三个好友权限/删除 Gate 通过。47项Q6测试、451项QQ全量回归及原功能/训练33项通过，真实Qwen两句话生成通过。好友阅读/评论/点赞和连续七天实际运行未测，保持相应能力关闭。详见 `docs/qq_companion/q6.md` 与 `acceptance.md`。

**Q6A 分能力实测；Q6B 只启用已验证能力。自身通过不代表好友互动通过。**

### Q6A：适配实测（先做，默认关闭）

1. 用 QQ 测试账号和 SnowLuma 启动，确认 OneBot 连接正常。
2. 从本机 HTTP 或 AstrBot 的 aiocqhttp client 调用 `get_cookies(domain="user.qzone.qq.com")`；只检查 `retcode==0`，`data.cookies` 有效，解析后 `uin/skey/p_skey` 均可用，测试输出只能记录 `present/absent` 不能泄露 Cookie。
3. 参考 Zhalslar 会话及执行协议，用受控适配器验证认证与自己列表；本次原版仓库下载不可用，未安装未锁定的第二个发布插件。**不修改 SnowLuma 专有组件、不实施风控规避**。
4. 当前只测试自己的发布、列表、详情、可见权限、删除和删除后消失，不读取好友说说、不评论/点赞。未验证删除前累计提交最多十条；逐条清理，响应不明先核对，不自动重发。
5. 分别保存 `cookies/login/list_self/detail_self/post/visibility_self/visibility_allowlist/delete/read_friend/comment/like` 证据、日期、锁定版本和安全错误码；未测试能力不记为失败或通过。
6. **自身必要 Gate 未通过则保持发布关闭；好友 Gate 未通过则保持互动关闭。Cookie 异常立即停止模块。不得以 NapCat 代替 SnowLuma。**

SnowLuma API 调用形式供 Gate 测试参考：

```bash
curl -sS -X POST http://127.0.0.1:3000/get_cookies \
  -H "Authorization: Bearer $SNOWLUMA_ONEBOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"domain":"user.qzone.qq.com"}'
```

该调用返回敏感认证信息，不要在终端/CI 日志中打印内容。上面只是本地受控测试命令示例。

### Q6B：通过后才做 QQ 空间自主功能

- **唯一调度**：现有 AstrBot 插件内 `DiaryPublisher` 调度，HTTP 适配器只执行，不另起自主发布插件或第二套 Agent。
- **重要事件**：本机配置的公开节日、纪念日和管理员事件默认10:00后自动生成，不设每日次数上限；同一事件ID/日期去重，不直接公开私人聊天事件。
- **常规总结**：每天23:40后取Q5当日公开候选，生成不超过100字的两句话；候选延迟等到23:59，无公开素材只写平静心情，过期不补旧日。每天每账号最多一条，重要事件不占配额。
- **好友互动**：仅三个既定白名单好友，每天19:30后一次有界扫描，评论至多2次、点赞至多3次，按说说去重。当前开关和Gate均关闭，不发起好友实测。
- **回滚与配置**：独立SQLite保存平台ID、内容哈希、可见范围与外发状态；本机CLI查询、删除已登记自己内容、停止/恢复和配置事件。`qzone.local.yaml`每分钟重读；`prompts.yaml`的`qzone_daily/qzone_event/qzone_interaction`每次请求重读。临时读故障延后，Cookie异常停用，外发不明只核对不重发。

**验收**：本次自身十条发布/查询/权限/删除已验证，测试内容不写上下文或记忆；好友阅读/评论/点赞各五次真实可见验收延后。连续七天实际运行待验；本地七天模拟仅验证配额和幂等。Cookie异常立即停止模块。

## 12. Stage Q7 — **主 Qwen3.5 原生图片/表情包/视频关键帧理解**（P1）

**用户已定规则（2026-11-01）**：Q7 真实 QQ 收发测试仅使用好友 `1301049831`，允许收发测试素材，不扩大发送范围。图片、静态表情、GIF/动图和视频分别验收、分别启用；未通过的能力保持关闭，不阻塞已通过能力。视频每2秒抽1帧，每段约24秒且最多12帧；60秒视频覆盖0–24、24–48、48–60秒，分别12、12、6帧。每段预处理帧数与实际推理批次分开，超预算可进一步拆批，但不能遗漏时间区间。

**目标**：为已经激活的 `Qwen3.5-9B-Q8_0` 接通视觉组件、同次图文输入及 QQ 媒体透传；聊天与识图共享常驻 `llama-server:8080`。取消原独立视觉模型、专用 8081 端口和 GPU 视觉独占切换开发任务。

**实现记录（2026-11-01）**：插件 `0.12.0` 接入配套视觉组件、原Agent图文输入、跨进程前台优先队列、结构化媒体分析、完整视频分段/ASR/缓存/恢复及可选8766接口。原创测试集图片20/20、静态表情20/20、GIF10/10、视频20/20通过，30轮图文交替保持同一主模型PID；语音服务常驻时无正常请求OOM。QQ闭环、故障证据、资源数值及适用边界见 `docs/qq_companion/acceptance.md` 和 `docs/qq_companion/q7.md`。

### Q7.1 主模型多模态部署与 QQ 图片透传

1. 核对当前 llama.cpp 构建对 Qwen3.5-9B 图文推理的实际支持，锁定可验证版本；以模型卡和多模态文档为准：<https://huggingface.co/Qwen/Qwen3.5-9B>、<https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md>。
2. 取得与当前基座匹配的视觉编码器/投影 GGUF，登记下载/转换来源、哈希、版本与兼容性；扩展模型 registry/active manifest、运行配置、启动脚本和 Doctor，将该文件作为视觉能力必需组件验证。主模型量化与视觉组件精度无需同名，但基座/架构必须兼容并实测。
3. 主 `llama-server:8080` 启动时一起加载主 GGUF 和 `--mmproj <已验证组件路径>`，后续文字/图片请求均复用该进程。部署接入可能需要一次正常服务重启，之后不随图片任务启停模型；不另启视觉 LLM 服务或 vision Transformers 环境。
4. 先用一张有明确内容的真实测试图和文字问题验证 API 回答、服务日志和视觉组件加载，再验证多图；仅 `/health` 或 `/v1/models` 成功不能证明具备识图能力。
5. 检查 AstrBot Provider 的图片能力设置、OneBot 图片下载/引用和请求构造，确保 `content` 中的文字与 `image_url` 图片块完整到达后端。普通图片问答直接在原 Agent 中生成回复，不能先把图片块丢掉只发送文件名或 `[图片]`。
6. 打通 QQ 前台、可选 `vision_api`、视频后台，以及记忆提取/压缩、日记、Qzone 文案和歌曲通知等全部主模型调用的共享请求限流与预算检查；首版同时只执行一条主模型请求。图片 token 应按实际后端计数/已验证保守预算处理，不能用 base64 字符长度代替视觉 token。

### Q7.2 统一媒体预处理与分析契约

以下 `vision_api:8766` 接口为本项目待实现的可选封装，只处理素材、调用共享主模型和校验结果，不拥有模型进程。普通 QQ 图文问答可直接走 AstrBot Provider；仅需要缓存、结构化标签、视频摘要时使用分析接口，避免每张图都固定多做一次描述推理。

| 新建 API | 输入 | 预处理 | 输出 | 模型调用 |
|---|---|---|---|---|
| `POST /v1/vision/analyze_image` | 已批准素材 ID + 问题 | Pillow 校验、旋转、最长边 1024px | 主题、对象、OCR、情绪、简述 | 主 Qwen3.5，文字 + 单图 |
| `POST /v1/vision/analyze_sticker` | 静态表情包 ID + 问题 | 同普通图，加入表情含义指令 | 含义、氛围、文字、不确定性 | 同一主模型 |
| `POST /v1/vision/analyze_gif` | 动画素材 ID | 默认 4 帧、最多 8 帧，时间戳与去重 | 动作变化、表情含义 | 同一主模型，预算内多图 |
| `POST /v1/vision/analyze_video` | 已批准视频 ID | 每 2 秒/帧、每段预处理 ≤12 帧 | 时间线、动作、OCR、摘要 | 同一主模型，分段排队 |

素材 ID 由服务端解析为授权缓存路径，禁止 QQ 用户指定任意系统路径；图片由本地封装编码为 data URL，或使用经过域名/重定向校验的可达地址。统一结构化输出如下，必须做 Schema 校验；`confidence` 是模型自评，不视为统计校准概率。

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

### Q7.3 抖音视频抽帧后直接调用主模型

1. 用户发来抖音链接 → 解析规范化链接/作者/简介/可用媒体；失败只保留链接和元数据，不伪造已看过视频。
2. 只下载有权限且技术可行的媒体，限制 180 秒/100MB，校验域名/重定向/私有 IP/文件类型；临时缓存默认 24 小时删除。
3. CPU FFmpeg 按完整时间轴分段抽帧，默认每 2 秒一张、每段预处理最多 12 张，保留绝对时间戳。实际单次输入受像素、视觉 token 和显存预算约束，可进一步拆批。
4. 有需要时把音轨转成 16kHz WAV，使用 SenseVoice 取得带时间信息的转写；无音轨或 ASR 失败时注明仅分析画面。ASR 继续使用已有资源策略，不因后续识图停掉主 LLM。
5. 每个片段将 **图片 + 时间戳 + 可选转写 + 分析要求** 在同次请求提交给主 Qwen3.5；后台低优先级逐段执行，片段之间处理前台 QQ 请求。跨段汇总仍调用同一个主模型，不切换或重载模型。
6. 存储 `media_analysis` JSON 和经筛选的记忆；聊天按授权检索最多 3 条相关视频记忆。后台分析不写入用户聊天历史，不覆盖角色人格。

**CPU FFmpeg 示例（单片段）**：

```bash
ffmpeg -i input.mp4 -vf fps=1/2 -frames:v 12 frames/%04d.jpg
# 抽帧后用 Pillow 等比例缩放到最长边 <=1024 像素
ffmpeg -i input.mp4 -vn -ac 1 -ar 16000 audio.wav
```

> `-frames:v 12` 只覆盖示例片段，正式实现须用 `-ss` / `-t` 按时间轴遍历全部片段，并记录未覆盖/未识别的区间；不能以视频前 24 秒的结果冒充看完整段。

### Q7.4 实验与验收（图文输入与容量 Gate）

- **输入真实性**：同一请求同时含文字与真实图片，模型可按文字问题辨别画面；覆盖换图同问、同图换问、多图比较及无图纯文字，日志证明图片块已传到后端，不以文件名或固定描述替代。
- **单实例与生命周期**：30 轮文字↔图片交替请求，主服务 PID 不变、主权重与视觉组件只在服务启动时加载；无识图触发的主模型停止/恢复，无独立视觉 LLM 进程。
- **质量与覆盖**：静态图片 20、表情 20、GIF 10、授权视频 20，覆盖有声/无声/字幕/快速动作；测试单段 6、12 帧和累计 24 帧分段输入，记录实际可用批次大小、时序、遗漏及不确定性。
- **资源与前台可用性**：记录 Q8_0 加视觉组件的常驻/峰值显存、RAM、视觉 token、首 token 延迟及排队时间；验证文字/图片合计并发上限、前台优先和后台分段让出，不串用户或群聊；正常负载无 OOM，不套用旧“启动前空闲 12GB”的门槛。
- **故障与回归**：覆盖缺失/错配视觉组件、损坏图片/视频、超预算、请求超时、重复入队、主服务故障及训练/翻唱占用资源；有限重试、队列可恢复、保护训练检查点。图片不可用时明确告知，普通文本、工具调用、语音和已完成阶段功能不回退。
- **启用条件**：按图片、静态表情、GIF/动图、视频分别保存质量、容量和故障回归证据，通过对应 Gate 即可启用对应能力；普通图片通过不代表视频通过。Q8 仍须自身浏览器阶段验收，不随 Q7 视频 Gate 自动打开。未通过的能力保持关闭，不能仅按模型名称宣告识图已交付。

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
3. **有界步骤**：对当前推荐项执行“读取 URL → 可访问性检测 → CPU 下载/抽帧/缓存 → 正常滑到下一条”；**通过 Q7 共用常驻 Qwen3.5 按片段低优先级分析，前台请求可在片段之间优先执行，不启停识图模型**。严格限制视频数 15、总时长 20 分钟、单次操作超时，遇到重复视频跳过。
4. **任务调度**：每天最多运行 1 次，默认人工触发直到稳定，随后可由 APScheduler 按固定窗口执行。文字、图片和视频分析共用主模型请求额度，QQ 前台优先于下一段后台视频；按截止时间和预算排队，不为整批视频长期阻塞聊天，不执行视觉模型卸载/恢复。
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

**请求队列与资源观测（Q7 接入，Q10 压力验证）**：

- 文字/图片/GIF/视频片段统一调用常驻 Qwen3.5:8080，首版共享并发上限为 1；前台优先、后台分段让出、去重、取消、超时和会话身份在所有入口一致生效。不再新增 `vision_batch` 独占 GPU owner 或识图模型切换状态机。
- GPUResourceBroker/进程管理仅沿用既有翻唱与离线训练职责；翻唱需要卸载/恢复主模型时，图文在线请求一起等待。不能由本次计划推定已允许训练、翻唱与在线推理并发，训练不能无检查点强杀。
- 记录主 GGUF + 视觉组件常驻基线、各类请求峰值、视觉 token、排队时间与失败码。图片长边 1024px、每段预处理帧数≤12、请求并发=1 均为初始上限，按 16GB 显存和 32K 上下文实测收紧。
- 30 轮文字/图片交替无 OOM、无模型重复加载、无 QQ 会话串话；视频后台运行时前台能在片段之间得到服务。分别验证错误图片的任务隔离和主服务真实崩溃时的恢复，不用每次模型装卸代替故障处理。

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
- [ ] **同一常驻 Qwen3.5 实例** 完成 QQ 普通图片、静态/GIF 表情包和视频抽帧理解；文字与图片同次请求、多图预算和时序覆盖验证通过；30 轮图文交替不触发模型装卸，容量 Gate 通过。
- [ ] SnowLuma 非官方 QQ 风控和数据使用风险在界面/README 说明清楚。
- [ ] Qzone 必须有分能力 Gate 报告，只启用已验证能力；自身通过不代表好友互动通过。

**增强（抖音辅助功能）**：

- [ ] 自主浏览任务仅在允许状态下运行，有限次数与时长，失败立即停机。
- [ ] 每条视频的结构化记忆可追溯、纠错、删除，不对外界资料盲目信任。
- [ ] 定向分享仅对已同意的 QQ 好友，低频、去重、可关闭。
- [ ] QQ 表情图片发送可用；后台视频分段让出前台请求；识图失败隔离到任务，主模型无需正常装卸，文本/语音及会话发送顺序回归通过。
- [ ] 真实翻唱只有具备合法音源与音色权重、独立 RVC/SVC 后端后才宣告通过。

## 17. 阶段优先顺序、难度与依赖关系

| 阶段 | 名称 | 难度 1–10 | 类型 | 依赖 | 最低验收 |
|---|---|---:|---|---|---|
| Q0 | Stage10 冻结、隔离、权限/许可 | 3 | 必须 | 无 | 可回归、可回滚 |
| Q1 | SnowLuma + AstrBot QQ | 4 | 必须 | Q0 | 私聊/群聊收发 |
| Q2 | 模型 API、Agent 单主控与既有模型进程管理 | 7 | 必须 | Q0、Q1 | Qwen/ASR/TTS 通；沿用翻唱/训练资源管理 |
| Q3 | 多人群聊与权限 | 6 | 必须 | Q1、Q2 | 不串话/不越权 |
| Q4 | 语音、点歌、翻唱、表情发送 | 7 | 主功能 | Q2、Q3 | 语音闭环；图片表情识别待 Q7 图文输入与容量 Gate 解锁 |
| Q5 | 记忆、每日私密日记 | 6 | 必须 | Q2、Q3 | 持久日记与检索 |
| Q6A | SnowLuma + Qzone 实测 Gate | 6 | 条件检测 | Q1 | 兼容性实证 |
| Q6B | QQ 空间发帖/评论/点赞 | 7 | 只启用 Q6A 已验证能力 | Q5、Q6A | 自身已测；好友互动待实测 |
| Q7 | **主 Qwen3.5 原生图像/表情/GIF/视频抽帧理解** | 6 | 重要增强 | Q2、Q4（接入表情识别） | 同次图文输入；30 轮交替无模型装卸/无 OOM |
| Q8 | 自主浏览抖音推荐流 | 9 | 辅助高风险 | Q7 | 15 条有界试运行 |
| Q9 | 兴趣评分与 QQ 分享 | 6 | 辅助增强 | Q3、Q5、Q7、Q8 | 白名单内低频分享 |
| Q10 | 稳定性、GPU、安全审计 | 8 | 横切必需 | 全部 | 回归/故障注入 |
| Q11 | 发布、Doctor、回滚手册 | 5 | 最终 | Q10 | 一键开停、文档闭环 |

**实施路线**：

```text
Q0 → Q1 → Q2 → Q3 ─┬→ Q4（语音、歌曲、表情）
                    ├→ Q5（日记/记忆）──→ Q6B（仅 Q6A 通过）
                    ├→ Q6A（空间兼容性实验）
                    └→ Q7（主模型图文/视频理解）→ Q8（刷抖音）→ Q9（定向分享）
          全程贯穿 Q10 → Q11
```

### 可提前交付的三个里程碑

- **MVP-A：QQ 角色聊天**：Q0–Q3；私聊、群聊、@、文本 + Agent 工具。
- **MVP-B：QQ 角色生活**：在 A 基础上 Q4/Q5，日记、语音、点歌；空间按 Q6A 条件决策。
- **MVP-C：统一视觉陪伴**：Q7 接通主 Qwen3.5 的图片/表情输入，并通过 FFmpeg 抽帧理解视频；**视觉组件、图文透传、共享队列和容量 Gate 通过后启用，无识图模型切换**；Q8–Q9 最后打开自主刷抖音与分享。

## 18. 未能被文档替代的实测事实（需由开发阶段确认）

1. **Stage10 具体 Git SHA**：公开阶段说明与 `main` 不能唯一确定提交点；须由本地 Git 历史/差异核验后记录。
2. **Qzone × SnowLuma 实机范围**：Cookie、自己列表/详情、十条发布/权限/删除已通过；好友阅读/评论/点赞未测，不宣称完整兼容，保持对应Gate关闭。
3. **当前 Qwen3 GGUF 的 Function Calling 兼容**：须以现有 `llama-server` 版本和 chat template 实际进行工具调用测试。
4. **当前 16GB GPU 下的 Qwen3.5-9B-Q8_0 多模态容量**：Q7已完成匹配组件和固定构建的限定预算实测；保持长边1024、每请求最多4帧、主存prompt cache 512MiB及共享单并发。修改模型、组件、视觉提示词或预算后旧证据失效，须重验；容量不足先收紧预算。
5. **抖音推荐流浏览的合法、稳定、可操作性**：项目不能保证站点登录/反自动化策略恒定允许；不可实施规避策略。若不具备稳定访问条件，只交付“分享链接分析 → 记忆”功能。
6. **歌声权重、音乐音源与播放**：本机 FeiyuVoice-RVC-v1 权重及索引已通过 Q4 三首整首合成和哈希验证；按用户确认的范围使用 weapi 正常提供的完整可用音源，拒绝试听及获取失败，不实施绕过。迁入 AstrBot 的本地模拟发送已验收，真实 QQ 客户端播放按本次用户要求未测试。

## 19. 一键启动与初始化建议（实际脚本由 Q0–Q2 开发）

```
start_all.ps1
  1) Doctor: Git base/config/FFmpeg/Ports/Python/Models/Account
  2) 初始化既有模型进程管理/翻唱训练互斥 → Qwen3.5 llama-server 8080
     Q7 启用后，同次启动加载主 GGUF 与匹配 mmproj，检查真实图文输入能力
  3) Start ??? GPT-SoVITS 9880 & wrapper 5002
  4) Start media_api 8765
  5) [按需] Start 轻量 vision_api 8766（共用 8080，不启动独立视觉模型）
  6) Start AstrBot 独立环境 6185 + OneBot 6199
  7) 检查 SnowLuma 本机进程与反向 WS 已连接（不自动强制登录 QQ）
  8) 启动 Scheduler：日记；Qzone/Douyin 默认需 Gate 打开
  9) 打印服务健康状态与功能降级说明

stop_all.ps1
  1) 冻结新图文请求/定时任务，停止后台视频入队，等待在途请求及既有翻唱任务
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
- Qwen3.5（聊天与识图共用）：<https://huggingface.co/Qwen/Qwen3.5-9B>、<https://huggingface.co/docs/transformers/en/model_doc/qwen3_5>
- llama.cpp 图文部署与 mmproj：<https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md>
- AI 翻唱插件：<https://github.com/sdfsfsk/matsuko_cover>
- 浏览器 Playwright：<https://playwright.dev/python/>
- FFmpeg：<https://ffmpeg.org/>
- SQLite FTS5：<https://sqlite.org/fts5.html>

---

**文档状态**：以各阶段记录和 `docs/qq_companion/acceptance.md` 实证为准。Q4本机真实模型、整首翻唱与GPU恢复通过，QQ客户端播放未测；Q5本地验收完成；Q6开发及自身十条发布/权限/删除实测完成，好友互动和七天实际运行待验；2026-11-01 Q7主模型多模态实现及限定预算真实推理通过，QQ闭环结果单独记入验收记录；Q8/Q9保持后续阶段。
