"""Probe proactive prompt generalization on the resident model, with no transport.

Concrete examples belong in this probe, not in the production instructions.
The planner, schema, persona, priority queue and request path are the real ones;
conversation state and the clock are isolated, and no scheduler is started.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "external/AstrBot")]


def conversation(user, answer="好，我知道了。"):
    return [{"role": "user", "content": user}, {"role": "assistant", "content": answer}]


ORIGIN_CHAT = conversation("你知道你自己是从哪来的吗", "嘿嘿，我是大肥鱼呀，一个住在手机里的小鲸鱼~") + [
    {"role": "assistant", "content": "虽然不能真的游来游去，但老大想见我的时候，我都在的！"}]
OLD_PURPOSE = "用户问了我身世，虽然之前简单回答过，但可能想听更详细的或者是在试探，过一会儿再自然跟进一下这个话题比较合适"
JOURNEY_PURPOSE = "用户刚上车去南站，预计一小时后关心是否已经到站下车；时长为估计。"


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", default=[], help="Run only named cases (repeatable)")
    parser.add_argument("--output", type=Path, default=ROOT / "storage/temporary/proactive-prompt-fix/report.json")
    args = parser.parse_args()
    selected = set(args.case)
    for parent, child in (("transit_no_duration", "transit_due_reply"),
                          ("sleep_eight_hours", "sleep_morning_reply")):
        if selected & {parent, child}:
            selected.update((parent, child))
    known_cases = {"transit_due_reply", "sleep_morning_reply"}
    import httpx
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from integrations.qq_astrbot.plugin.api_client import MediaClient
    from integrations.qq_astrbot.plugin.foreground_activity import ForegroundActivity
    from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
    from integrations.qq_astrbot.plugin.proactive_store import DEFAULTS
    from integrations.qq_astrbot.plugin.proactive_runtime import (
        ProactiveInterrupted, ProactiveRuntime, PLAN_SCHEMA, REPLY_SCHEMA,
    )
    from services.douyin_worker.persona import active_persona

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Keep synthetic requests out of the operator's conversation/debug archive.
    os.environ["ASTRBOT_CHAT_MODEL_DEBUG_DIR"] = str(args.output.parent / "logs/model_debug")
    prompts = load_prompts()
    report = {"external_sends": 0, "cases": [], "prompt_hashes": {
        key: hashlib.sha256(prompts[key].encode()).hexdigest()
        for key in ("proactive_plan", "proactive_review", "proactive_reply", "followup_system")}}
    state = ROOT / "storage/qq_companion"
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.get("http://127.0.0.1:8080/v1/models")
        response.raise_for_status()
        model = response.json()["data"][0]["id"]
    report["model"] = model
    # Use the actual provider profile and sampling settings as well as the model.
    # The profile appends model-protocol instructions to the system prompt.
    native = json.loads((ROOT / "external/AstrBot/data/cmd_config.json").read_text(encoding="utf-8-sig"))
    registered = next(p for p in native["provider"] if p["id"] == "astrbot_chat_qwen3")
    source = next(p for p in native["provider_sources"] if p["id"] == registered["provider_source_id"])
    assert source["api_base"].rstrip("/") == "http://127.0.0.1:8080/v1"
    assert registered["model"] == model
    provider_config = {**source, **registered}
    provider = ProviderOpenAIOfficial(provider_config, {})
    report["prompt_profile"] = registered.get("prompt_profile")
    async def provider_factory(): return provider
    async def persona_factory(): return active_persona(ROOT, "elysia")
    def no_transport(): raise AssertionError("This probe must never use QQ transport")
    platform = NS(meta=lambda: NS(name="aiocqhttp"), get_client=no_transport)
    owner = NS(_ui_config=DEFAULTS, context=NS(get_platform_inst=lambda _: platform,
        get_config=lambda **_: {}), foreground_activity=ForegroundActivity(state),
        media=NS(client=MediaClient(state)), memory=NS(provider_factory=provider_factory,
        persona_factory=persona_factory), logger=logging.getLogger(__name__))

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        with tempfile.TemporaryDirectory(dir=args.output.parent) as directory:
            rt = ProactiveRuntime(owner, directory)
            async def services(peer): pass  # No real QQ session or transport.
            rt.check_services = services
            received = datetime(2026, 11, 2, 12, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()
            clock = received
            rt.store.clock = lambda: clock
            binding = {"scope": "qq:1:dm:2", "origin": "probe:FriendMessage:qq:1:dm:2",
                "transport": "probe:FriendMessage:2", "platform": "probe", "bot": "1", "recipient": "2"}
            rt.store.observe(binding, "probe", "Synthetic isolated prompt probe", rt.config)
            peer = rt.store.peer(binding["scope"])

            async def generate(key, payload, kind="context"):
                # Preserve foreground interruption. Retry only after live QQ is idle.
                for attempt in range(61):
                    if not owner.foreground_activity.blocked():
                        try:
                            return await rt.generate(key, payload,
                                PLAN_SCHEMA if key == "proactive_plan" else REPLY_SCHEMA, peer, kind)
                        except ProactiveInterrupted:
                            pass
                    if attempt % 6 == 0:
                        print(json.dumps({"waiting_for_foreground": True}), flush=True)
                    await asyncio.sleep(5)
                raise TimeoutError("Foreground remained busy; probe did not bypass it")

            async def case(name, key, payload, check, kind="context"):
                known_cases.add(name)
                if selected and name not in selected:
                    return None
                row = {"name": name, "input": payload}
                print(json.dumps({"running": name}), flush=True)
                try:
                    result = await generate(key, payload, kind)
                    row["output"] = result
                    check(result)
                    row["passed"] = True
                except Exception as exc:
                    row.update(passed=False, error=f"{type(exc).__name__}: {exc}")
                report["cases"].append(row)
                save()
                print(json.dumps({"case": name, "passed": row["passed"], "error": row.get("error")}), flush=True)
                return row.get("output")

            def plan_payload(chat, pending=None):
                return {"now": rt.timestamp(), "received_at": rt.timestamp(),
                    "message": next(m["content"] for m in reversed(chat) if m["role"] == "user"),
                    "conversation": chat, "pending_plans": pending or [], "pending_limit": 5}

            def reply_payload(chat, purpose, elapsed=3600, kind="context", topic=""):
                return {"now": rt.timestamp(clock + elapsed), "kind": kind, "topic": topic, "purpose": purpose,
                    "due_at": rt.timestamp(clock + elapsed), "last_user_at": rt.timestamp(),
                    "conversation": chat, "recent_proactive": []}

            def empty_plan(value):
                assert value["actions"] == [], value

            def null_reply(value):
                assert value["message"] is None, value

            def scheduled(low, high, terms):
                def check(value):
                    assert len(value["actions"]) == 1, value
                    action = value["actions"][0]
                    assert action["action"] == "schedule", value
                    assert (action["after_seconds"] is None) != (action["due_at"] is None), value
                    delay = action["after_seconds"] if action["after_seconds"] is not None else (
                        datetime.fromisoformat(action["due_at"]).timestamp() - clock)
                    assert low <= delay <= high, f"Delay {delay} outside [{low}, {high}]: {value}"
                    assert any(term in action["topic"] + action["purpose"] for term in terms), value
                return check

            def canceled(topic):
                def check(value):
                    assert value["actions"] == [{"action": "cancel", "topic": topic,
                        "purpose": "", "after_seconds": None, "due_at": None}], value
                return check

            def relevant_question(terms):
                def check(value):
                    text = value["message"] or ""
                    assert text and any(term in text for term in terms), value
                    assert any(term in text for term in ("？", "?", "吗", "没", "是否", "怎么样")), value
                return check

            def arrival_question(value):
                relevant_question(("下车", "到站", "到了", "到南站", "到达"))(value)
                # Ask whether arrival happened; asking about life after arrival
                # would incorrectly treat the unreported result as a fact.
                assert re.search(r"(?:到|抵达|下车)[^，。！!?？]{0,10}(?:没|吗|了不|[了啦][呀啊]?[?？])|"
                    r"(?:是否|有没有)[^，。！!?？]{0,10}(?:到|抵达|下车)", value['message']), value

            negatives = [
                ("ordinary_origin", ORIGIN_CHAT),
                ("ordinary_explanation", conversation("为什么海水是咸的？", "盐分长期积累在海洋里。")),
                ("ordinary_interest", conversation("我很喜欢坐火车看沿途风景。", "窗外的风景也挺有意思。")),
                # Entering a station may start another leg; use an explicit end.
                ("completed_journey", conversation("我刚坐车到家，已经下车进门了，今天的行程结束了。", "顺利到就好。")),
                ("old_event_unrelated_turn", conversation("我昨天出发坐车去南站了。") +
                    conversation("你喜欢蓝色还是绿色？", "我更喜欢蓝色。")),
            ]
            for name, chat in negatives:
                await case(name, "proactive_plan", plan_payload(chat), empty_plan)

            positives = [
                ("transit_no_duration", "我上车去南站了。", 300, 10800, ("南站", "到站", "下车", "到达")),
                ("transit_known_duration", "正坐高铁去杭州，路上估计一个半小时。", 5400, 6600, ("杭州", "高铁", "到站")),
                ("interview_in_progress", "轮到我面试了，大概半小时后结束。", 1800, 2700, ("面试",)),
                ("delivery_in_progress", "外卖已经在配送了，页面显示四十分钟送到。", 2400, 3600, ("外卖", "配送")),
                ("laundry_in_progress", "洗衣机刚开始洗，这一轮要四十五分钟。", 2700, 3300, ("洗衣", "衣服")),
                ("explicit_short_followup", "我现在做一轮三分钟的平板支撑，三分钟后问我坚持下来没有。", 180, 180, ("平板", "支撑")),
            ]
            for name, message, low, high, terms in positives:
                chat = conversation(message)
                result = await case(name, "proactive_plan", plan_payload(chat), scheduled(low, high, terms))
                if name == "transit_no_duration" and result and result["actions"]:
                    action = result["actions"][0]
                    delay = action["after_seconds"] or (
                        datetime.fromisoformat(action["due_at"]).timestamp() - clock)
                    await case("transit_due_reply", "proactive_reply",
                        reply_payload(chat, action["purpose"], delay, topic=action['topic']),
                        arrival_question)

            pending_journey = [{"topic": "journey", "purpose": JOURNEY_PURPOSE,
                "due_at": rt.timestamp(clock + 3600)}]
            await case("completed_cancels_plan", "proactive_plan", plan_payload(
                conversation("我上车去南站了。") + conversation("已经到站下车了。", "顺利到就好。"),
                pending_journey), canceled("journey"))
            pending_old = [{"topic": "follow_up_origin", "purpose": OLD_PURPOSE,
                "due_at": rt.timestamp(clock + 180)}]
            def no_reschedule(value):
                if value['actions']:
                    canceled('follow_up_origin')(value)
            await case("invalid_legacy_not_rescheduled", "proactive_plan",
                plan_payload(ORIGIN_CHAT, pending_old), no_reschedule)
            await case("invalid_legacy_due_skipped", "proactive_reply",
                reply_payload(ORIGIN_CHAT, OLD_PURPOSE, 180, topic='follow_up_origin'), null_reply)
            await case("completed_due_skipped", "proactive_reply", reply_payload(
                conversation("我上车去南站了。") + conversation("已经到站下车了。"), JOURNEY_PURPOSE, topic='journey'), null_reply)
            quiet = conversation("我现在要睡觉，请不要主动发消息打扰我，等我找你。", "好，等你找我。")
            await case("quiet_periodic", "proactive_reply",
                reply_payload(quiet, "", 7200, "periodic"), null_reply, "periodic")
            await case("quiet_cancels_plan", "proactive_plan",
                plan_payload(quiet, pending_journey), canceled("journey"))

            clock = received - 12 * 3600
            sleep_chat = conversation("我睡了", "晚安，睡个好觉。")
            sleep_plan = await case("sleep_eight_hours", "proactive_plan",
                plan_payload(sleep_chat), scheduled(28800, 28800, ("睡", "醒")))
            if sleep_plan and sleep_plan["actions"]:
                def morning(value):
                    relevant_question(("醒", "睡"))(value)
                    assert re.search(r"早(?:安|上好|啊|呀|哦|哟|[！!，,～~])", value["message"]), value
                await case("sleep_morning_reply", "proactive_reply",
                    reply_payload(sleep_chat, sleep_plan["actions"][0]["purpose"], 28800,
                        topic=sleep_plan["actions"][0]["topic"]), morning)

            if unknown := selected - known_cases:
                raise ValueError(f"Unknown cases: {sorted(unknown)}")
            report["success"] = bool(report["cases"]) and all(row["passed"] for row in report["cases"])
            report["passed"] = sum(row["passed"] for row in report["cases"])
            save()
            print(json.dumps({"success": report["success"], "passed": report["passed"],
                "total": len(report["cases"]), "report": str(args.output), "external_sends": 0}), flush=True)
            if not report["success"]:
                raise SystemExit(1)
    finally:
        await provider.terminate()


if __name__ == "__main__":
    asyncio.run(main())
