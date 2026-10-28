# QQ Stage 14A 历史记录：联网点歌与 FeiyuVoice 本地翻唱

本记录来自旧 QQ 适配器。其部分代码已移至 `integrations/qq_astrbot/reference/`，供后续 AstrBot 插件迁移；本记录中的旧 QQ 实机结论不代表当前 AstrBot 路线已验收。

## 当前实现

日常聊天统一交给本地 Qwen 决策。模型先以固定温度独立判断点歌意图并提取歌曲，随后在正常聊天结构化结果中输出与语音选择并列的 `sing`、`song_query` 字段，并自行生成一句自然的等待回复；两个模型结果必须一致。回复确认送达后，程序才按模型给出的歌曲信息搜索当前账号可播放的音源，后台下载、分轨、RVC 翻唱、混音，完成后直接发送歌曲。程序不按聊天关键词决定是否点歌；聊天中不展示搜索编号、操作指令、任务编号或进度。一次只处理一个 GPU 歌曲任务，重启会恢复未发送的中间阶段；发送结果未知时停在 `send_uncertain`，不自动重发。

网易云 weapi 仅获取当前账号可播放的音源。可选登录信息从本机环境变量 `ALYSIA_NETEASE_COOKIE` 读取；Cookie 不会传给音频 CDN。音源支持 HTTP Range 续传。下载和中间文件放在 `storage/temporary/song_jobs`，按 SHA256 校验后才复用。在线音源及模型仍可能受来源可用性、许可、显存和版本影响。

## 本机模型和依赖

歌声音色使用 [FeiyuVoice-RVC](https://huggingface.co/Esmerubphire/FeiyuVoice-RVC) 的 `FeiyuVoice-RVC-v1`，固定 Hugging Face 修订 `04e5011`。权重 SHA256 为 `bcc3f4de74f87e102358db704c0fb75fc700fd8f44f491494287cc0b071a1dfe`，索引为 `3821de84cf7ad3e2be40475ee06c261da03be25b0583e571f568da2658a0732a`。运行参数由模型包给定：RVC v2、40 kHz、RMVPE、移调 0、索引率 0.3、protect 0.33。安装器同时校验发布包清单和许可文件。

本机已安装到 `model_store/registry/rvc/feiyu/v1`（忽略 Git）。Applio 源码固定于 [IAHispano/Applio](https://github.com/IAHispano/Applio) 提交 `67a38f6abab3e90a55efa534ea3185a222c102f0`，并按模型发布包的 `runtime_pins.json` 覆盖/校验兼容文件；兼容文件来自 [Feiyu voice service](https://github.com/Esmerubphire/astrbot_plugin_feiyu_voice) 提交 `91f59626ec65a00d413f9f40dc591fadc75726dc` 的 `server/runtime_compat/Applio`。ContentVec 和 RMVPE 由 `scripts/install_song_assets.py` 以固定哈希安装。分轨使用 [audio-separator](https://github.com/nomadkaraoke/python-audio-separator) 0.27.1 与 `model_bs_roformer_ep_317_sdr_12.9755.ckpt`（SHA256 `5b84f37e8d444c8cb30c79d77f613a41c05868ff9c9ac6c7049c00aefae115aa`）。FFmpeg 和 FFprobe 需在 PATH 中。

本机 `envs/song` 复用 `runtime_env` 的 CUDA Torch 2.11.0+cu128，并额外安装 audio-separator 0.27.1、faiss-cpu 1.8.0、soxr 0.5.0.post1、torchcrepe 0.0.24、torchfcpe 0.0.4、resampy 0.4.3、beartype 0.18.5、ml_collections 1.1.0、rotary-embedding-torch 0.6.5。模型安装/验证命令：

```powershell
envs/song/Scripts/python.exe scripts/qq_companion/install_feiyu_rvc.py
envs/song/Scripts/python.exe scripts/qq_companion/install_song_assets.py --applio-repo external/Applio
envs/song/Scripts/python.exe scripts/qq_companion/configure_feiyu_runtime.py --applio-repo external/Applio --python envs/song/Scripts/python.exe
envs/song/Scripts/python.exe model_store/registry/rvc/feiyu/v1/run_rvc.py --verify-only
```

`configure_feiyu_runtime.py` 会逐文件对照发布包的 Applio 哈希，并检查 CUDA。重建 Applio 时先按以上固定提交克隆到 `external/Applio`，再从固定 voice service 提交复制兼容文件到相同相对路径；哈希不匹配会拒绝配置。`envs/song`、模型权重和运行产物都不提交到 Git。

## 配置与使用

当前 AstrBot 方向的模型约定见 [示例配置](../../configs/qq_companion/app.example.yaml)。本机旧适配器配置保存在忽略 Git 的 `configs/qq_companion/local.json`，供迁移参考，不能直接作为 AstrBot 插件配置加载。

旧离线 probe 与测试文件保存在 `integrations/qq_astrbot/reference/`。它们依赖旧分支其余 QQ 模块，当前分支不作为可执行回归入口。

2026-10-28 排查两条真实请求：“大肥鱼，我要听雨爱”仅表达听歌，语义未明确翻唱；“我要听你唱雨爱”明确翻唱。此前本地配置没有 `song` 段，工具没有注册，任务数为零。即使启用工具，本地 Qwen 对第二句仍输出普通回复，因此明确的“听你唱/请翻唱 + 歌名”现在先走受控搜索、选曲流程；含糊的“我要听雨爱”先询问原曲还是翻唱，普通聊歌仍交给 Qwen。自然语言“第一首/第二首”可确认候选并入队。离线 `qq_stage14a_intent_probe.py` 实测第二句进入歌曲候选列表，聊歌请求保持普通回复；网易云当前账号不可播放的版本在候选展示前剔除。该测试没有向 QQ 发消息。

上段是早期排查记录；自然语言规则与选曲交互已被后续的模型结构化字段流程取代。

## 2026-10-27 验证记录

本机 weapi 搜索、歌曲详情、播放地址和公开可播音频下载已成功。该音频取第 40 秒起 8 秒片段，裁剪 0.12 秒、分轨 6.11 秒、RVC 9.25 秒、混音 0.09 秒；产生 7.98 秒 WAV。独立的完整持久任务 probe 同样成功，状态 `completed`、只调用一次假发送，最终 WAV 44.1 kHz、单声道、703,914 字节，峰值约 −0.4 dB。RVC 回执记录的实际加载权重和索引哈希与指定 FeiyuVoice 包一致。邻接回归 55 项通过。

截至 2026-10-27，尚缺真实 QQ 发送及桌面/手机端播放确认、真实 QQ→Qwen 自然语言入口验收、与聊天/TTS 同时运行的显存及 24 小时稳定性记录。当时的 `completed` 只代表假发送任务结果。对自有 QQ 账号主动发送测试音频的工具调用被自动审批拒绝，理由是具体内容和目标缺少明确授权。次日由用户在 QQ 中亲自发起的歌曲任务另见下方恢复记录。

## 2026-10-28：30 秒转换停滞与恢复

真实 QQ 选曲任务 `31da8e868623400d822caa715e908186` 已下载并分轨《雨爱》（网易云 ID 5235487），但从 09:21:59 起停在 `converting`。进程栈显示卡在 FAISS `index.search`；对同一索引的 3,000 帧查询，FAISS 单线程约 0.02 秒，4/16 线程超过 30 秒。模型发布包的 `run_rvc.py` 固定 `OMP_NUM_THREADS=4`，因此在不修改、破坏发布包哈希的前提下增加本地执行包装，等 Applio 按原顺序导入 FAISS 时将检索线程设为 1。转换超时缩短并记录实时本地日志，取消/超时时按 Windows 进程树回收，状态查询显示当前阶段已运行秒数。

停止卡住的 QQ 进程树后，用原任务的 30 秒人声直接转换成功；整条本地音频流水线耗时约 17.5 秒，其中 RVC 9.2 秒。重启 QQ 桥接后，原 `job_id` 从已校验的分轨产物恢复，09:37:09 记录 `completed`；`voice_sends` 仅一条 `sent` 回执，OneBot 消息 ID `1138368465`，读回 `message_type=private`、`segment_types=[record]`。这证实 QQ 协议侧发送了语音；桌面和手机 QQ 播放尚待客户端确认。

## 2026-10-28：日常点歌与整首歌测试

用户要求取消选曲和进度指令的聊天交互，并暂时翻唱整首歌。本机配置启用 `song.full_song`，默认从音源开头 `0` 秒开始，时长取音乐平台返回的歌曲总时长向上取整；FFmpeg 遇到音源结尾就停止。此前配置是固定从开头截取 `30` 秒，没有自动分析副歌或精彩片段。超过 `600` 秒的音源会被拒绝。只有用户显式指定歌手时才按歌手筛选；只给歌名时，在同名可播结果中优先取至少 180 秒的首个版本。

离线意图验证中，“我要听雨爱”“我要听你唱雨爱”“点一首雨爱”都直接入队，回复只说稍等；普通聊歌仍进入常规聊天。实际入队的《雨爱》从 `0` 秒开始，请求时长 `261` 秒。对已下载的完整音源进行本地验证：音源 `260.480227` 秒，裁剪后仍为 `260.480227` 秒，分轨 `24.55` 秒、Feiyu RVC `15.95` 秒，最终混音音频 `260.44` 秒；QQ 发送前的 24 kHz 单声道 WAV 为 `12,501,198` 字节，符合歌曲专用的本地长度与大小限制。此次验证未连接 QQ，完整歌曲在 QQ 客户端的实际接收与播放仍未验证。应用按用户要求保持退出。

## 2026-10-28：改为模型决定点歌

真实消息“大肥鱼，我还想听你唱年轮”此前没有建歌曲任务。数据库中模型连续生成两句聊天回复，未调用搜歌/翻唱工具；后续因自然语言规则未覆盖“还想”而没有进入歌曲流程。现在普通聊天不走程序关键词匹配：Qwen 先用固定温度分类，再在正常结构化回复中输出相同的 `sing=true`、`song_query=年轮` 和等待回复内容；发送成功后程序再搜索并入队。决策表记录 `sing` 与 `song_query`，便于排查模型判断和后端执行。初次复测发现采样会偶发把普通聊歌误判为点歌，因此增加独立模型分类与字段一致性校验。此后两轮离线连接本地 Qwen 的五句验证均显示：上述《年轮》请求、“我要听你唱雨爱”“点一首雨爱”为 `sing=true`；普通聊歌和播放原唱为 `sing=false`。没有向 QQ 发送验证消息。

当前音乐来源中，《年轮》两条靠前的原版搜索结果不可播放；可播放的首个同名结果只有 113 秒，疑似短版。音源匹配现优先选时长至少 180 秒的同名可播放结果，本次选中小雪版本、274.53 秒。该规则只用于落实模型已决定的歌曲任务，不负责判断聊天意图。

变更后已重启 QQ 桥接进程，并恢复本地 Qwen3 与 SnowLuma；日志确认 QQ 账号就绪、翻唱功能启用。相关本地回归测试 70 项通过。旧请求不自动重放；下一条用户点歌消息才会走新字段流程。
