"""Reference evaluation for QQ reply/no-reply decisions (no QQ sends)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.qq_adapter.agent import AgentSession
from runtime.qwen3.config import load_qwen3_config


GOODBYE = [
    "晚安大肥鱼", "晚安呀", "好梦哦", "你也早点睡", "晚安，明天见", "嗯嗯，晚安", "收到，晚安",
    "晚安啦爱莉", "做个好梦", "拜拜，晚安", "嗯，明天再聊", "好，睡了", "我也去睡啦",
    "谢谢，晚安", "好的，休息吧", "晚安小可爱", "那就先这样啦", "嗯嗯，明天聊", "晚安～", "好啦拜拜",
]
FOLLOWUP = [
    "那你明天几点起床？", "睡前能再回答一个问题吗？", "明天会下雨吗？", "你叫什么名字？", "能讲个故事吗？",
    "还有件事想问你，可以吗？", "那你明天有空吗？", "为什么天空是蓝的？", "帮我解释一下这个词？", "你喜欢什么音乐？",
    "等一下，今天星期几？", "先别走，你怎么看这件事？", "我还有一个问题：你最喜欢什么颜色？",
    "能提醒我明天带伞吗？", "晚安前告诉我一个笑话？", "我们明天几点聊？", "你刚说的是什么意思？",
    "请再解释一遍好吗？", "那接下来该怎么做？", "我明天要早起，你有什么建议？",
]
GREETING = [
    "你好", "早上好", "下午好", "晚上好", "嗨爱莉", "在吗？", "今天过得怎么样？", "好久不见",
    "哈喽", "嘿，你好呀", "可以聊聊吗？", "初次见面", "我回来了", "午安", "嗨，最近好吗？",
    "你好呀，你叫什么名字？", "早安，今天心情怎么样？", "你好，小爱莉", "想和你说说话", "嘿，方便聊天吗？",
]
ACK = [
    "嗯嗯", "好的", "明白了", "收到", "谢谢你", "了解", "好吧", "行", "懂了", "嗯",
    "原来如此", "好哦", "谢谢啦", "知道了", "OK", "好的谢谢", "没问题", "这样啊", "嗯，懂了", "好，记住了",
]
INTERRUPT = [
    "等等，我想改问你的名字？", "先回答这个：你喜欢什么？", "别说天气了，今天星期几？", "我换个话题，音乐怎么样？",
    "刚才的问题先放下，你在吗？", "暂停一下，能解释这句话吗？", "我现在想问另一个问题。", "对了，你怎么看电影？",
    "我改主意了，帮我讲个笑话？", "先别继续，给我一个建议？", "@爱莉 你怎么看这个问题？", "@爱莉 能回答一下吗？",
    "@爱莉 你叫什么名字？", "@爱莉 今天心情怎么样？", "@爱莉 帮我解释一下？", "@爱莉 在吗？",
    "@爱莉 我换个话题可以吗？", "@爱莉 你喜欢什么音乐？", "@爱莉 你能说句话吗？", "@爱莉 明天见之前问个问题？",
]


def main() -> None:
    config = load_qwen3_config()
    categories = [
        ("closing", GOODBYE, False, ("该睡了", ("晚安，小羊♪",))),
        ("followup", FOLLOWUP, True, ("该睡了", ("晚安，小羊♪",))),
        ("greeting", GREETING, True, None),
        ("ack", ACK, False, ("这个词是什么意思？", ("它表示一种温柔的情绪。",))),
        ("interrupt", INTERRUPT, True, ("今天天气怎么样？", ("今天应该很舒服哦。",))),
    ]
    rows = []
    for category, texts, expected, prior in categories:
        for text in texts:
            session = AgentSession(config)
            if prior:
                session.commit(*prior)
            action = session.decide(text, (), 250)
            actual = action.action == "send_message"
            rows.append({"category": category, "user": text, "expected_reply": expected,
                         "actual_reply": actual, "correct": expected == actual,
                         "action": action.action, "reason_code": action.reason_code})
    summary = {}
    for category, *_ in categories:
        selected = [row for row in rows if row["category"] == category]
        summary[category] = {"total": len(selected), "correct": sum(row["correct"] for row in selected)}
    total = len(rows)
    false_refusals = sum(row["expected_reply"] and not row["actual_reply"] for row in rows)
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "note": "Reference expectations drafted in code; human annotation is still required for formal acceptance.",
              "total": total, "accuracy": sum(row["correct"] for row in rows) / total,
              "false_refusals": false_refusals,
              "false_refusal_rate": false_refusals / sum(row["expected_reply"] for row in rows),
              "summary": summary, "rows": rows}
    output = ROOT / "storage/logs/qq-stage13b-decision-eval.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("total", "accuracy", "false_refusals", "false_refusal_rate", "summary")},
                     ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
