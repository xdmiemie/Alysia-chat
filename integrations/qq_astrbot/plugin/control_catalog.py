"""Chinese labels and invocation descriptions for the editable prompt bundle."""

PROMPTS = {
    "shared_memory_use": ("共享记忆使用规则", "角色记忆任务与请求视图"),
    "shared_memory_digest": ("候选临时摘要", "大量候选的分层比较；不替换具体视频原文"),
    "shared_qq_summary": ("会话分事项摘要", "空闲或增量检查点；逐会话生成短期记忆"),
    "shared_qq_special": ("QQ 特殊候选", "长期意图与角色认同同时成立时"),
    "shared_qq_review": ("QQ 特殊复核", "特殊条目提升长期前"),
    "shared_qq_correction": ("已有记忆纠正", "真实用户对已有状态明确纠正时，独立于喜欢判定"),
    "shared_qq_correction_review": ("纠正依据复核", "更新已有记忆前独立核验"),
    "shared_video_watch": ("角色观看摘要", "客观分析后的独立内部任务"),
    "shared_video_daily": ("视频每日整理", "完整自然日结束后"),
    "shared_video_compact": ("视频十批整理", "十个活跃批次或受控重选"),
    "shared_diary_daily": ("共享素材私人日记", "次日读取所有合格短期素材"),
    "shared_diary_public": ("独立公开日记候选", "仅基于允许公开的材料，禁止私人日记换标签"),
    "shared_diary_compact": ("累计日记整理", "十五条合并，覆盖日期不截断"),
    "model_protocol": ("模型协议", "每次模型请求；当前模型配置可覆盖此项"),
    "qq_context": ("QQ 会话规则", "QQ 主回复及继承其规则的追问"),
    "reply_output": ("回复格式", "主 Agent 生成最终回复"),
    "astrbot_safety": ("AstrBot 安全规则", "框架启用安全提示时替换原规则"),
    "astrbot_tool_call": ("工具调用规则", "框架启用工具提示时替换原规则"),
    "datetime_reminder": ("当前时间提醒", "请求包含框架时间提醒时"),
    "echo_description": ("回声工具说明", "注册 q2_echo 测试工具时"),
    "echo_value_description": ("回声参数说明", "注册 q2_echo 的 value 参数时"),
    "music_selection": ("歌曲选择", "检索歌曲后挑选候选"),
    "music_reselection": ("重新选歌", "候选歌曲不可用后重新选择"),
    "music_clarification": ("选歌澄清", "需要用户补充歌曲信息时"),
    "music_result": ("音乐结果通知", "音乐或翻唱任务完成后"),
    "agent_policy": ("Agent 行为规则", "主 Agent 每轮请求"),
    "agent_execution_state": ("执行状态说明", "注入当前任务状态、预算和真实执行结果时"),
    "agent_reply_regenerate": ("回复重新生成", "回复格式不可用时的一次重新生成"),
    "agent_budget_reply": ("预算耗尽回复", "达到 Agent 预算后直接回复"),
    "agent_summary": ("会话压缩", "上下文超限需要压缩时"),
    "followup_phase": ("追加回复阶段规则", "主回复发送完毕后决定是否追加"),
    "followup_system": ("追加回复系统规则", "私聊追加回复决策"),
    "followup_user": ("追加回复请求", "私聊追加回复决策的当次输入"),
    "proactive_plan": ("聊天主动计划", "私聊结束后根据内容创建、更新或取消后续关心计划"),
    "proactive_reply": ("计划主动回复", "聊天计划或固定轮次到期时，结合当前时间和会话决定是否发消息"),
    "proactive_review": ("主动联系审核", "到期后先核验事件进展、历史回复和免打扰状态，通过后才按人设生成消息"),
    "memory_use": ("记忆使用规则", "检索并注入长期记忆时"),
    "memory_extract": ("记忆提取", "后台提取聊天中的持久事实"),
    "memory_verify": ("记忆核验", "保存新事实前验证依据和重复项"),
    "memory_compress": ("记忆压缩", "召回记忆超出预算时"),
    "memory_shared_topics": ("共享话题", "生成可跨会话使用的抽象话题"),
    "diary_generate": ("私人日记", "到达设定时间生成本地日记"),
    "diary_public": ("公开日记候选", "从私人日记生成可公开内容"),
    "qzone_daily": ("QQ 空间日常动态", "按 QQ 空间日常发布时间生成动态"),
    "qzone_event": ("QQ 空间事件动态", "命中已配置日历或管理员事件时"),
    "qzone_interaction": ("QQ 空间互动", "空间互动功能开启且通过检查时"),
    "vision_context": ("视觉上下文规则", "QQ 请求附带图像或视频理解结果时"),
    "vision_analyze": ("画面分析", "分批理解图像、动图或视频采样帧"),
    "vision_merge": ("视频分析合并", "合并多个画面分析批次"),
    "vision_reply": ("视觉结果回复", "将视觉结果转为会话回复时"),
    "douyin_interest": ("抖音兴趣判断", "看完视频后判断有趣、好笑或高质量及分享意图"),
    "douyin_title_interest": ("抖音标题兴趣预判", "正式观看前，仅凭标题和大肥鱼的性格判断是否想看"),
}
for key, title in {
    "emotion": "语音情绪工具",
    "musiccover": "翻唱工具",
    "music": "音乐工具",
    "sticker": "表情工具",
    "weapi": "音乐搜索工具",
    "speech_text": "语音文本参数",
    "emotion_value": "情绪参数",
    "song_id": "歌曲编号参数",
    "candidate": "歌曲候选参数",
    "sticker_tag": "表情标签参数",
    "weapi_action": "音乐搜索操作参数",
    "music_query": "歌曲查询参数",
    "music_page": "搜索分页参数",
    "song_action": "歌曲操作参数",
}.items():
    PROMPTS["tool_" + key] = (title, "构建原生工具定义时；上下文监控中不展示工具说明")


def prompt_entries(values):
    rows = [
        {
            "key": key,
            "name": PROMPTS.get(key, (key, "请求时读取"))[0],
            "when": PROMPTS.get(key, (key, "请求时读取"))[1],
            "text": value,
        }
        for key, value in values.items()
        if isinstance(value, str)
    ]
    for profile, overrides in values.get("model_profiles", {}).items():
        for key, value in overrides.items():
            rows.append(
                {
                    "key": f"model_profiles.{profile}.{key}",
                    "name": f"模型协议 · {profile}",
                    "when": f"当前模型 prompt_profile 为 {profile} 时覆盖 {key}",
                    "text": value,
                }
            )
    return rows
