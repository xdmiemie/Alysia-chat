# 共享记忆 v1

本实现使用 `memories.sqlite3` 中的 `sm_*` 表，QQ、QQ 视频、自主浏览、日记共用。新业务先写短期，只有注册模块策略可以提升长期。原 `memory_items`、`diary_entries` 保持历史兼容与原权限，数据库触发器阻止继续直写。

## 改造审计

| 原入口 | 当前链路 |
| --- | --- |
| `PersistentMemory.enqueue → extract_one → save_extraction` | 真实消息事件 → 独立会话/日期快照 → 普通短期摘要；不再普通事实提取 |
| `VisionRuntime.work → repository.add_video` | 客观分析不变 → 持久化分析来源 → 独立角色观看任务 → 短期 |
| `Worker.process → acceptable → remember → add_public_video` | 有效分析后先记忆，与兴趣、分享条件独立；分享契约不变 |
| `generate_diary → save_diary` | 全部合格短期素材 → 私人日记短期 → 策略立即提升 |
| 旧召回与 `memory_compactions` | 新统一权限视图与实际请求预算；旧数据按原 owner 只读兼容 |
| WebUI 手动新增长期 | 关闭；支持已有记录纠正、撤回、保留限制及原任务重试 |
| 公开日记与 QQ 空间 | 仅独立 shared 输入投影；私人日记不得改 visibility 外发 |
| 普通已发送回复、追问、主动消息、音乐回执 | 持久化实际发送记录；草稿不进入摘要来源 |

底层 `vision_analyze`、`vision_merge`、`reply_output`、`vision_reply`、`douyin_interest` schema 不变。新增提示词在 `configs/qq_companion/prompts.yaml`，已登记到加载校验与提示词管理页。

| 实现文件 | 职责 |
| --- | --- |
| `integrations/qq_astrbot/plugin/shared_memory.py` | 公共存储、ACL、来源图、消费者、租约、治理、迁移、缓存与统计 |
| `shared_memory_config.py`、`memory_policies.py` | 参数 schema、默认值、模块描述及提升权限 |
| `memory_tasks.py` | QQ 摘要/特殊条目/纠正、视频日批和十批整理、日记十五篇整理、迟到修订和补跑 |
| `memory_request.py`、`context_budget.py` | 完整请求模板与 tokenizer 预算，实际请求元数据 |
| `persistent_memory.py`、`agent_runtime.py`、`main.py` | QQ 真实来源、实际发送回执、增量摘要、请求接入 |
| `vision_runtime.py`、`proactive_runtime.py`、`services/douyin_worker/` | 原分析链保持独立；角色表达与自主观看接入共享视图 |
| `web_control.py`、`views/console/` | 注册模块统计、请求归因、筛选、参数、纠正、撤回和会话保留设置 |
| `scripts/qq_companion/configure.py` | 部署清单包含五个新增核心文件，避免开发目录可用而部署目录缺文件 |

## 配置和管理页

打开 AstrBot WebUI → 插件 → 大肥鱼控制台 → 记忆库。页面按模块注册表生成，默认查看当前角色；可筛选模块、会话、可见范围、业务日期、类型、批次和层级。管理端登录不改变聊天请求权限。

参数保存于数据库 `sm_meta.config`，schema 与默认值在 `shared_memory_config.py`。有效时区默认为 `Asia/Hong_Kong`，完整自然日结束后的次日 `00:05` 整理；已有非空库更改时区必须另行进行显式日期迁移，不静默改写日期。旧 23:30 日界线仅作为 legacy 保留，迁移不会把旧日记重新当成新经历。

默认短期 72 小时，10000 条/64 MiB 软阈值、256 MiB 来源与短期正文硬阈值；恢复正文/服务管理备份保留 7 天。软压力暂停自主浏览，不中断 QQ 回复；写入失败不推进消费游标，日志只记录错误类型。任务自动尝试最多 5 次，之后在记忆页重试原任务。

总记忆预算 6144 tokens，包含至多 512 tokens 共同近况。使用当前运行模型 tokenizer、聊天模板及配置窗口，扣除对话、任务材料、工具、输出和余量后再分配。完整请求再次计数；分项采用独立分块估算，模板/BPE 差额单列。无请求记录显示未统计。模型窗口不能据此假定永远是 32K。

观看摘要 400 字/768 tokens；会话摘要 600 字/1024 tokens；视频日批总摘要 1024 tokens、累计总摘要 2048 tokens；日记 400 字/768 tokens、累计日记 2048 tokens。篇幅检查发生在写入前，入选视频直接引用原正文和 hash。

## 数量与生命周期

每天视频日批保留两条不同视频；不足两条按实际候选数。空日期不创建视频批。十个活跃批合并，历史批计一批，第 r 轮具体视频数 `min(2+2*r, selected_history_limit, 不同视频候选数)`。默认历史精选上限 80；失败时最多保留九个待合并日批的 18 条，所以具体摘要最多 98 条。它不是每个请求注入 80 条，也不是整个库永远最多 80 条。降低上限等待模型受控重选成功，提高上限不恢复已清理正文。

日记十五条合一，累计摘要随后与十四篇新日记再次合并；第 15、29 天触发，最早覆盖日期不重置。失败时活跃长期最多十五条，后续日记在短期待提升。恢复旧正文的期限与累计摘要覆盖期限无关。累计摘要有损压缩细节，不保证恢复历史原话。

短期退出同时要求过 TTL、所有必要消费者完成或合法跳过、无任务 pin。任务使用稳定身份、输入版本与租约，模型调用不持有写事务；提交冲突安全重试。删除撤回沿来源图传播，清空正文与受管理恢复数据，墓碑阻止旧任务重放复活。

普通 QQ 摘要仅经日记间接进入长期。特殊条目需要真实长期意图 AND 已发送角色认同证据，再独立复核；不由礼貌附和推定。明确控制和禁止保留不受喜好限制。私聊支持 `/memory no_store`、`/memory no_long_term`、`/memory allow`；管理页 API 也支持范围保留治理。

群聊中的个人保留命令绑定稳定成员 ID，不修改其他成员规则；混合摘要沿来源链撤回。涉及受限成员的角色回复保守继承限制。跨检查点的已发送认同可核对有限范围内的真实原消息，旧消息只作为证据，不再次总结。明确纠正使用独立识别和复核，仅更新已有事实，不能借纠正新增长期事实。关闭日记期间不会把待消费素材假标为已写日记，积压达到容量阈值时暂停浏览并在管理页保留待处理状态。

迟到材料保留事件日期，通过稳定日批身份修订。恢复期限内重建受影响累计摘要，代数保持不变；所需来源已清理则撤回并标记 `needs_rebuild`，不编造输入。公开日记只从 shared 素材另行生成，失败可重试，同日修订替换旧候选；私人日记不会作为公开候选来源。

维护任务调试日志不保存记忆正文，普通请求日志移除注入的记忆视图。治理会清理相关调试副本和编辑归档；后台视觉/主动表达无法从正文可靠识别会话时采用保守脱敏。统计日志只保留数值元数据。

## 迁移、备份与回滚

开发测试只能使用临时库或生产库副本，不向真实 QQ 发测试消息。迁移工具只输出数量和配置，不输出正文。默认 dry-run 不修改源库：

```powershell
envs/astrbot/Scripts/python.exe scripts/qq_companion/migrate_shared_memory.py <数据库路径>
envs/astrbot/Scripts/python.exe scripts/qq_companion/migrate_shared_memory.py <数据库路径> --copy-to <新的副本路径>
```

正式部署前停止 QQ 与视频两个写进程。对确认的数据库执行 `--apply`，或首次启动由核心进行相同增量迁移。SQLite Backup API 产生一次 `.pre-shared-v1.bak`，包含 WAL 已提交数据，重复运行不重复导入。旧事实、旧视频、旧日记不直接删除、不猜历史批次数、不伪造第一人称体验；旧提取队列隔离为 `legacy_pending`，不会自动补写长期。

可验证来源、权限和原事件时间的旧待处理 QQ 消息转为真实短期来源事件，再经过新摘要链；无法验证的仍留在待整理区。旧视频重试若没有可靠观看时间，只保留原视频分析及 `watch_time_unknown` 标记，不以重试完成时间冒充新观看日期。

初次开发验收在生产副本中验证：69 条旧记忆、4 篇旧日记、17 条旧收件记录保持完整；连续两次 schema 迁移数量不变，正文与 ACL 指纹不变。此阶段没有清理生产数据。用户随后明确要求将旧记忆分类并入新机制，且删除没有对应角色口吻的客观视频记忆，已按下述独立整理流程执行。

部署使用项目原有配置/启动流程，它会根据更新后的 `PLUGIN_FILES` 同步代码；随后由新核心进行增量迁移。参数热读取，维护任务下一轮使用新值；降低历史精选上限须等待受控重选成功，管理页可查看任务状态。运行中的旧进程不会因为源码变化自动切换到新逻辑。

安全回滚使用 `--rollback-copy <新路径>` 从**当前**库生成只读兼容副本，保留新表、墓碑与旧写入阻断触发器；不以旧备份覆盖当前库。停止新维护任务后旧版本可读取 legacy，不能恢复旧自动写链。回滚不恢复撤权或删除内容。服务仅管理自己的有限迁移备份；管理员自行导出的备份必须独立执行同等删除治理，不能未经核对直接恢复。

### 按明确指令整理历史正文

`scripts/qq_companion/reconcile_legacy_memory.py` 用于 schema 迁移后的人工审核整理。它不启动模型服务或 QQ，不自动把分析改写成角色感受。先审核原库、按视频 ID 对应的既有角色评价、真实观看时间和原会话证据，形成本地 review JSON，再 dry-run、验证副本、最后提交。写进程须保持停止。

```powershell
envs/astrbot/Scripts/python.exe scripts/qq_companion/reconcile_legacy_memory.py <数据库路径> --videos <douyin.sqlite3路径> --review <审核JSON>
envs/astrbot/Scripts/python.exe scripts/qq_companion/reconcile_legacy_memory.py <数据库路径> --videos <douyin.sqlite3路径> --review <审核JSON> --copy-to <新副本路径>
envs/astrbot/Scripts/python.exe scripts/qq_companion/reconcile_legacy_memory.py <数据库路径> --videos <douyin.sqlite3路径> --review <审核JSON> --apply
```

审核文件包含 `source_snapshot`、`expected_active`、按视频 ID 索引的 `video_reviews`（原评价 hash 和完整句子的 `reasons/uncertainty` 下标）、按原会话/日期分组的 `qq_sessions`、`legacy_diaries`、`video_days` 和 `diaries`。这些是已核对的内容与选择，不是模型调用参数；不应把真实审核正文提交到 Git。当前本地 GGUF tokenizer 校验所有新正文限额。视频正文只提取原角色评价完整句子，并去除展示用 LaTeX 标记；不能用客观分析生成不存在的历史感受。

提交前再次检查原库指纹、配置、评价与分析 hash、观看证据、保留限制、墓碑及维护租约。所有新短期记录、策略提升、消费者确认和旧正文清理同事务提交。旧视频的标准来源 ID 加墓碑，防止旧 QQ 视频任务或自主浏览重试复活。完成后同步清理服务管理备份、全文索引、编辑归档和相关调试副本。`sm_meta.legacy_reconciliation_v1` 只保留映射 ID、hash 和数量；重复 `--apply` 不重复导入，并可继续未完成的备份清理。

2026-11-03 已在 `storage/qq_companion/memories.sqlite3` 完成实际整理：

| 历史内容 | 结果 |
| --- | --- |
| 47 条有效旧视频记忆 | 18 条找到对应既有大肥鱼评价，替换为观看短期；29 条无合格替换内容，删除 |
| 4 条个人事实及 2 条共享主题投影 | 按原会话/日期合并为 2 条私聊短期摘要；投影去重，不推定特殊长期认同 |
| 1 篇有效旧日记 | 保留为私人日记，缺乏素材或发送凭证的说法明确标为不可证实，保留旧日界元数据 |
| 已完成日期 | 基于迁移后的真实素材补齐 3 篇私人日记；11 月 2 日的 17 条视频生成 1 个日批、2 条原文精选 |
| 11 月 3 日观看 | 1 条仍留短期，等待当天正常日结 |
| 原来已删除的 15 条记录 | 不恢复；清除遗留正文与派生副本 |

整理后共 **25 条新机制记录、7 条长期可见记录**：18 条观看、2 条 QQ 摘要、4 篇私人日记、1 条视频日批总摘要；长期为 4 篇日记、日批摘要及其中 2 条原文精选，短期与长期共享身份，不重复计数。没有新增公开日记或普通事实长期条目。旧记忆/日记/证据/修订/压缩缓存表中的正文已清空；原始视频业务分析库保留作为证据，不参与角色记忆冒充。未启动或重启 QQ 服务，也未发送真实消息。

验证包含真实库副本及正式库的结构完整性、原文 hash、观看日期、ACL、全部正文的实际 tokenizer 限额、精选身份、消费者状态与重复执行。新增 `tests/test_legacy_memory_reconciliation.py` 的 **11 项测试通过**，覆盖中途失败全事务回滚、过期审核、保留限制、删除重放、备份清理和幂等执行。这次历史角色文本为逐条人工核对后的既有评价，不代表已经验证后续模型持续生成的风格质量。

## 扩展模块

1. 实现适配器，写真实来源事件与有依据的短期摘要。
2. 创建 `ModuleDescriptor`，登记稳定 ID、展示名、类型、参数、统计维度、版本。
3. 注册策略的消费者与允许提升类型；策略通过快照任务提交提升或压缩，不开放模型长期写工具。
4. 请求用 `RequestContext` 和统一预算视图；后台派生结果将实际输入纳入版本快照/来源链。

第四模块自动获得存储、权限过滤、视图、通用指标和 UI 行，不修改三个已有模块的业务判断。

## 验证

自动化使用模拟模型、假时钟、临时 SQLite 和模拟外发。执行命令：

```powershell
$sharedMemoryRegressionTests = @(Get-ChildItem tests -Filter 'test_qq*.py' | ForEach-Object FullName) + @(Get-ChildItem tests -Filter 'test_douyin*.py' | ForEach-Object FullName) + @('tests/test_shared_memory.py','tests/test_shared_memory_web.py')
envs/astrbot/Scripts/python.exe -m pytest @sharedMemoryRegressionTests -q --disable-warnings
envs/douyin/Scripts/python.exe scripts/qq_companion/probe_shared_memory_ui.py
envs/astrbot/Scripts/python.exe -m compileall -q integrations/qq_astrbot/plugin services/douyin_worker scripts/qq_companion
git diff --check
```

UI 检查使用隔离的无头 Edge 和合成数据，不连接真实 QQ；验证第四模块自动展示、无请求占比为空、历史精选默认 80，以及实际请求分项的百分比显示。截图在 `storage/temporary/shared-memory-ui/`，已人工查看布局。部署清单另经 AST 导入闭包检查，48 个部署文件无缺失的本地相对依赖。

2026-11-03 实测结果：上述完整相关回归 **735 passed、2 skipped**；最后加入程序级特殊条目去重及长会话纠正用例后，相关四个记忆测试文件再次运行 **51 passed**。跳过项为需要显式启用 `QQ_TEST_DESKTOP=1` 的桌面退出检查。UI 输出 `SHARED_MEMORY_UI_PASS`，编译检查与 `git diff --check` 通过。测试输出保存在 `storage/temporary/shared-memory-regression.txt` 和 `shared-memory-focused.txt`。

数量测试包括 10/19/28 批的 20→4、22→6、24→8，80 条历史＋18 条待合并，封顶后的成功整理、配置降上限与失败重试；日记第 15/29 天仍覆盖首日。另覆盖全部素材分层输入、ACL、保留限制、删除传播、租约冲突、午夜迟到、离线补跑、恢复不足撤回、工具与发送契约、2048/4096/32768 窗口预算和迁移副本。

尚未做真实模型的角色风格、精选质量及长时间运行验收，也未在真实服务上调用 tokenizer 复测新记忆请求。自动化预算测试使用确定性计数器；生产实现调用实际模板与 tokenizer，图像沿用已有视觉 token 上界预算。这些测试不代表真实模型一定符合输出 schema，失败会进入有限重试而不会先删除旧记忆。

2026-11-03 后续运行修复：真实模型曾在 `shared_qq_special` 输出不存在或角色错误的事件 ID，触发 `special_missing_real_agreement`，会话任务达到五次重试上限后使抖音等待总结。候选结构化生成现用真实用户事件 ID 和真实已发送回复 ID 的分别枚举约束；缺少任一角色时不生成特殊候选。逐字证据、时间顺序、长期意图和角色认同检查继续保留。原始事件先在副本中复测，再通过现有任务重试接口恢复处理，正式库 14 条待处理事件全部成功总结，没有删除来源或绕过校验。相关记忆测试 164 项通过；真实模型验证仅覆盖此批会话，不代表全面的角色风格或长期运行验收。
