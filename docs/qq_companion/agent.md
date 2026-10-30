# 大肥鱼原生工具 Agent（0.10.0）

Q5 增加自动持久化记忆和私人日记，详见 [Q5](q5.md)。主请求与主动补发在实际上下文
预算内临时召回资料，超限时使用禁用工具的压缩请求；压缩计入同一个前台请求/时长预算。
历史落盘时恢复原始用户输入，不累积重复记忆。后台提取和日记持GPU准入租约，复用
现有Provider；它们没有Agent、工具执行器或QQ发送能力。

主对话使用 AstrBot 的同一个 ToolLoopAgentRunner。模型返回原生 tool_calls 时，由
AstrBot 执行工具、追加回执，再请求模型；没有工具调用时，程序直接结束 Agent，
发送本次最终回复。没有发送前的语义检查、候选草稿或模型收尾请求。

## 最终回复与工具

主请求的第一条 system 保留人格、框架提示及 qq_context；第二条独立 system
放置 reply_output 和 agent_policy 原文，不改提示词内容。动态执行状态仍另外追加。
第二条规则在各次工具请求及主动补发中保留，参与上下文预算；保存聊天历史前移除，
不随轮次重复累积。此调整仅拆分消息，不新增格式校验或模型调用。

主回复保留 reply、thinking_level（1–5）、voice 三字段传输结构，用于正文、
发送节奏及自动语音。它就是最终回复，不是等待模型审批的候选。普通文本也由程序
直接接收，默认节奏为 3、voice=false。非法结构或空回复在同一 Agent 中重新生成
一次；提示词为 agent_reply_regenerate，重试保留原生工具能力，正文中的工具 JSON
从不执行。程序保留当前上下文及真实工具回执，重试指令只用于下一次生成，不进入
聊天历史。重试仍无效、预算不足、截断或服务异常时使用 agent_budget_reply。
reply_retries 计数持久化，每轮最多一次，工具步骤和 Provider 回退不重置。

有原生工具调用时，该步骤附带正文可以是普通文本、reply/voice 等字段、非法 JSON
或空内容；交付时统一忽略，不做回复格式校验、不触发格式重生成、不读取其语音和节奏。
该正文仍作为原生 assistant 工具消息的 content 保留，与 tool_calls、对应 tool_call_id
及工具结果一起进入当前请求和后续历史；读取旧调用记录不会再次执行工具。
只有无工具调用的最终回复才进入正文解析。原生工具请求还会移除请求参数及 Provider
配置中的 response_format，避免正文 JSON 约束抑制工具协议。工具参数仍由 schema、权限、会话版本和
幂等动作键校验。每次主模型请求前注入本轮真实的歌曲候选序号、检索次数与拒绝状态；
这些执行信息不会写入实际聊天历史。

默认全局预算为 16 次模型请求、16 次工具调用、180 秒。原生步数即将耗尽时提前
结束，避免 AstrBot 插入伪造的用户续跑消息。预算、分词或服务失败不会发起额外模型
收尾。摘要压缩请求和发送后的主动回复请求也计入同一全局预算。

## Agent 结束后的主动回复

私聊的主回复全部成功发送后，才独立判断是否自然补发一句。沿用提交 960dac3c 的
followup_system 和 followup_user 原文；主回复不等待这个判定。群聊仍不进入私聊补发。

补发继承本轮主 Agent 构建完成的全部 system 消息，包括人格、QQ 会话规则、主回复
规则和 Agent 行为规则，不再读取追加规则之前保存的 qq_persona_prompt 副本。
每次补发重新生成 agent_execution_state，反映真实查询次数、候选与拒绝状态，
不沿用已经过时的状态消息或临时重生成指令。继承的系统消息保持原顺序和分段结构，
与当前状态、补发阶段规则及对话一起计算输入预算。

followup_phase 明确本次阶段：继续遵守人格、隐私和真实回执要求，主回复的
reply/voice 格式及工具执行流程仅适用于已经结束的主流程；当前只使用原有
follow_up/thinking_level 格式，工具始终禁用。原 followup_system、followup_user
保留原文。完整调试日志中可能有多条 system 消息，需要一起查看。

每次判定只输出 follow_up 与 thinking_level；follow_up 为 null 就停止。
若有有效的一句话，按原有打字节奏发送，确认发送后加入历史，再判断下一次。
每条用户请求最多进行三次判定，最多补发三条；不是固定发送三条。

判定请求显式传 func_tool=None，没有工具 schema，没有 Agent 工具执行器。
即使提供方意外返回工具调用，也只记录并停止，不执行工具。重复、非法输出、异常、
超时、预算耗尽或用户新消息都会停止补发。失败不会撤销已发送的主回复。
同一次发送钩子不会重复启动补发；followups 计数持久化，重开状态库不重置上限。
旧状态库增加 followups 列，保留历史语义检查计数及旧审计记录。

主 Agent 的规划与选歌开启思考；主动回复、歌曲结果通知及摘要关闭思考，
reasoning_budget_tokens=0。主动回复沿用旧版 temperature=0.5、max_tokens=160，
单次请求最多 25 秒，仍受本轮总时限约束。

## 上下文与消息交付

私聊沿用 AstrBot 保存的本会话历史；群聊只使用本群有界短期窗口。主模型输入是
带 role 的消息数组。主请求及主动补发均保留 content 中完整的模型回复 JSON，
不将 reply、thinking_level、voice 剥离为纯文本。主动补发沿用最近八个完整用户轮次，
保留原生工具调用与结果，不按单条字符截断 JSON；整体仍受请求 token 预算约束。
群聊在本群有界 TTL 窗口中保留同样的协议消息，窗口过期后移除，不读取其他群或私聊。

Qwen 上下文上限为 32768 token；每次请求使用本机 llama.cpp 的 /apply-template
与 /tokenize 计算真实输入。主请求预留 1536 输出 token 和 256 余量，超限按完整
用户轮次删除最旧历史，保留工具调用与结果对。单轮仍过长时直接结束。
分词不生成回复，不计模型生成次数。共享配置位于 configs/runtime/qwen3.yaml。

模型生成、工具执行、打字等待及 OneBot 实际分发前均检查会话版本。新请求会中断
旧的前台生成和主动回复。工具执行链保留真实的原生调用和结果；最终回复全部成功
发送后保存一份原始 content，即使分句发送也不拆成多条纯文本历史。发送中途失败
时保留原字段并将 reply 限于实际送达部分，不把未送达正文存为已发送回复。
主动补发和后台歌曲通知保留各自原始 JSON；临时系统状态、重生成指令、无效最终输出
及私有推理不进入聊天历史。实际文字/语音交付回执继续单独审计。
文字正文按句拆分，主回复最多三条群消息或六条私聊消息。voice=true 时整段可朗读
正文发送为一条语音，不按句发送多条录音；长文本在内部限长合成后合并 WAV。
颜文字和表情符号仍作为文字附送，不读入语音。语音不模拟打字延迟；合成失败则
整段回退到文字分句发送。

## 完整模型调试日志

`storage/qq_companion/logs/model_debug/` 集中保存带缩进和换行的 UTF-8 JSON 文件。
每个 `model-*.json` 文件是记录数组，可直接用编辑器阅读。一次实际模型
请求及其响应合为一条；每个文件最多 50 条，最多保留五个文件，轮转时自动删除
编号最早的文件。按记录数计数，不按显示行数计数。重启后继续当前文件计数；
并发写入和轮转使用进程间锁，完整数组以原子替换写入。已有旧版 JSONL 日志自动
转换为相同编号的 JSON 文件，保留记录内容，成功后删除旧格式文件。
可用 `ASTRBOT_CHAT_MODEL_DEBUG_DIR` 指定其他目录；否则跟随
`ASTRBOT_CHAT_STATE_DIR/logs/model_debug`，默认落在上述项目目录。

每条记录的 `request` 是 SDK 最终发出的完整 HTTP JSON 请求体，包含实际 system
提示词、对话历史、工具回执、工具 schema、当前输入与生成参数；`response` 是 SDK
及 AstrBot 解析前的完整接口 JSON，保留接口原有的 `choices`、`message`、
`tool_calls`、`finish_reason`、`usage` 以及扩展字段。不截断正文或思考字段，
不记录 HTTP 鉴权头。接口返回非 JSON 内容时原样保留为字符串。

主 Agent、格式重生成、主动补发、歌曲选择/重选、结果通知和摘要请求均经过同一入口。
每次接口重试独立记录。时间使用带时区的 UTC ISO 格式，并记录阶段、Provider、会话、
耗时、HTTP 状态和错误；记录在请求结束或取消时落盘。连接失败或取消时若拿不到实际
HTTP 请求对象，`request_source=sdk_arguments` 明确标识保存的是待发送参数，不能据此
认定请求已经到达模型。正常请求为 `request_source=http_body`。
分词和健康检查不属于这里的对话生成请求。日志写入失败会报告错误，不改变模型回复。

旧 `agent.sqlite3` 中的 `text`、`tools` 仍是解析后审计字段，不是接口原始 JSON。
新增日志从启用后开始记录，无法补齐此前未保存的原始响应。

## 歌曲任务

可用原生工具为 emotion、musiccover、music、sticker、weapi。q2_echo 保留为本地
协议诊断。weapi 每页最多十首，模型根据歌名、歌手、别名、时长、可用性与链接选择
candidate；它是本任务内稳定的序号，真实歌曲 ID 和授权凭据留在程序中。

musiccover/music 受理已选定歌曲，不重新检索或选歌。任务先保存为 awaiting_commit，
本轮前台交付结束后提交。下载时仍取得有效的临时音频地址。后台歌曲任务独立于
普通聊天；只能由请求者明确取消或改点，其他群成员的消息不取消歌曲。

现有音源实际下载 403 后，music_reselection 从同一任务未尝试的候选中重选，
不重试淘汰项、不重建任务。music_result/music_clarification 生成后台结果通知。
music_selection 仍用于兼容旧任务的 awaiting_selection 恢复路径，因此保留。

## 提示词与验证

提示词统一从 configs/qq_companion/prompts.yaml 请求时读取。已删除
agent_completion_check、agent_unverified_claim、agent_continue、agent_repair、
agent_check_failed、agent_restricted_close，以及仅旧版离线流程引用的 media_decisions。
保留正在使用的工具、QQ 输入、最终回复、后台结果、音源重选与摘要提示词。

test_qq_agent.py 使用固定版本 AstrBot 的真实 Runner、执行器和本地假传输验证直接
结束、原生工具回合、主动回复上限、空值停止、工具禁用、插话取消与发送历史。
test_qq_request_provider.py 检查真实 OpenAI SDK 生成的 HTTP 请求体和解析前完整响应；
test_qq_model_debug.py 检查 50 条/五文件轮转、完整内容、并发、旧格式迁移与原子写入。

运行 envs/astrbot/Scripts/python.exe scripts/qq_companion/probe_agent_native.py --quick
可在隔离端口 18080 检查真实 Qwen 的普通聊天和原生搜歌/翻唱工具回合；媒体为模拟
回执，不连接 QQ。探针只关闭自己启动的模型，报告写入
storage/temporary/native-agent-acceptance/report.json。--existing 使用已有模型准入租约。
