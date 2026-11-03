"""Isolated native AstrBot WebUI acceptance; no QQ or Douyin connection."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BASE = ROOT / "storage/temporary/web-control-acceptance"
URL = "http://127.0.0.1:6186"


async def serve():
    sys.path.insert(0, os.environ["ASTRBOT_ROOT"])
    from astrbot.core import db_helper, LogBroker, LogManager
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    from astrbot.dashboard.server import AstrBotDashboard

    log = LogBroker()
    LogManager.set_queue_handler(
        __import__("astrbot.core", fromlist=["logger"]).logger, log
    )
    core = AstrBotCoreLifecycle(log, db_helper)
    await core.initialize()
    persona = core.star_context.persona_manager
    try:
        await persona.get_persona("elysia")
    except ValueError:
        await persona.create_persona(
            "elysia", "这是独立验证实例的人格，不用于实际聊天。"
        )
    cm = core.star_context.conversation_manager
    existing = await cm.get_conversations("snowluma:FriendMessage:qq:1:dm:2")
    if not existing:
        await cm.new_conversation(
            "snowluma:FriendMessage:qq:1:dm:2",
            platform_id="snowluma",
            content=[
                {"role": "user", "content": "界面测试消息"},
                {"role": "assistant", "content": "界面测试回复"},
            ],
            persona_id="elysia",
        )
    dashboard = AstrBotDashboard(
        core,
        db_helper,
        core.dashboard_shutdown_event,
        str(ROOT / "external/AstrBot/data/dist"),
    )
    await asyncio.gather(core.start(), dashboard.run())


async def browser():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 1050})
        token = os.environ["WEB_CONTROL_PROBE_TOKEN"]
        await context.add_cookies(
            [{"name": "astrbot_dashboard_token", "value": token, "url": URL}]
        )
        await context.add_init_script(
            "if(window===window.top){localStorage.setItem('token', "
            + json.dumps(token)
            + ");localStorage.setItem('user','webtest');}"
        )
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        await page.goto(URL + "/#/plugin-view/astrbot_chat_q1/console")
        try:
            await page.get_by_role("button", name="关闭", exact=True).wait_for(
                timeout=12000
            )
            await page.get_by_role("button", name="关闭", exact=True).click()
        except Exception:
            pass
        frame = page.frame_locator("iframe")
        await frame.locator("#nav button").first.wait_for(timeout=45000)
        for name in [
            "抖音视频监控",
            "记忆库",
            "上下文控制",
            "模型参数",
            "人物性格",
            "流量监控",
            "应用设置",
            "系统提示词",
        ]:
            await frame.get_by_role("button", name=name, exact=True).click()
            await frame.locator("#refresh:not([disabled])").wait_for()
            assert not await frame.locator("#notice.error").count(), (
                await frame.locator("#notice").inner_text()
            )
            assert await frame.locator("#content").inner_text()
            await page.screenshot(path=str(BASE / (name + ".png")), full_page=True)
        await frame.get_by_role("button", name="记忆库", exact=True).click()
        await frame.locator("#refresh:not([disabled])").wait_for()
        await frame.get_by_role("button", name="新增", exact=True).click()
        await frame.locator("#field-owner_key").fill("qq:1:dm:2")
        await frame.locator("#field-summary").fill("来自浏览器的测试记忆")
        await frame.locator("#save-dialog").click()
        await frame.get_by_text("来自浏览器的测试记忆", exact=True).wait_for()
        await frame.get_by_role("button", name="编辑", exact=True).first.click()
        await frame.locator("#field-summary").fill("浏览器编辑已生效")
        await frame.locator("#save-dialog").click()
        await frame.get_by_text("浏览器编辑已生效", exact=True).wait_for()
        await frame.get_by_role("button", name="删除", exact=True).first.click()
        await frame.locator("#save-dialog").click()
        await frame.get_by_text("浏览器编辑已生效", exact=True).wait_for(
            state="detached"
        )
        await frame.get_by_role("button", name="上下文控制", exact=True).click()
        await frame.locator("#refresh:not([disabled])").wait_for()
        await frame.get_by_role("button", name="查看 / 编辑", exact=True).first.click()
        await frame.locator("#refresh:not([disabled])").wait_for()
        await frame.get_by_role("button", name="编辑", exact=True).first.click()
        await frame.locator("#field-content").fill("上下文修改已生效")
        await frame.locator("#save-dialog").click()
        await frame.get_by_text("上下文修改已生效", exact=True).wait_for()
        await frame.get_by_role("button", name="人物性格", exact=True).click()
        await frame.locator("#refresh:not([disabled])").wait_for()
        await frame.locator("#field-persona").fill("浏览器保存的人物性格，仅测试实例。")
        await frame.get_by_role("button", name="保存人物性格", exact=True).click()
        await frame.get_by_text(
            "已保存；下一次 QQ、日记和抖音请求生效，重启后保留", exact=True
        ).wait_for()
        await frame.get_by_role("button", name="应用设置", exact=True).click()
        await frame.locator("#refresh:not([disabled])").wait_for()
        await frame.locator("#field-hourly_videos").fill("22")
        await frame.get_by_role("button", name="新增白名单", exact=True).click()
        await frame.locator("#whitelist-kind").select_option("raw")
        await frame.locator("#field-whitelist-value").fill("snowluma:FriendMessage:12345")
        await frame.locator("#save-dialog").click()
        await frame.get_by_text("snowluma:FriendMessage:12345", exact=True).wait_for()
        await frame.locator("#whitelist-search").fill("not-present")
        await frame.get_by_text("没有匹配的会话。", exact=True).wait_for()
        await frame.locator("#whitelist-search").fill("12345")
        await frame.get_by_role("button", name="编辑", exact=True).click()
        await frame.locator("#field-whitelist-value").fill("23456")
        await frame.locator("#save-dialog").click()
        await frame.get_by_text("snowluma:FriendMessage:23456", exact=True).wait_for()
        await frame.get_by_role("button", name="删除", exact=True).click()
        await frame.locator("#save-dialog").click()
        await frame.get_by_text("白名单为空，当前不限制会话。", exact=True).wait_for()
        await frame.locator("#field-whitelist-enabled").uncheck()
        await frame.get_by_role("button", name="保存白名单开关", exact=True).click()
        assert not await frame.locator("#field-whitelist-enabled").is_checked()
        assert await frame.locator("#field-hourly_videos").input_value() == "22"
        await frame.get_by_role("button", name="新增抖音好友", exact=True).click()
        await frame.locator("#field-douyin-friend-id").fill("local_probe_friend")
        await frame.locator("#field-douyin-friend-name").fill("本地测试好友")
        await frame.locator("#save-dialog").click()
        await frame.get_by_text("本地测试好友", exact=True).wait_for()
        await frame.get_by_role("heading", name="抖音分享好友白名单", exact=True).scroll_into_view_if_needed()
        await page.screenshot(path=str(BASE / "douyin-friends.png"), full_page=True)
        await frame.locator("#douyin-whitelist-search").fill("not-present")
        await frame.get_by_text("没有匹配的抖音好友。", exact=True).wait_for()
        await frame.locator("#douyin-whitelist-search").fill("测试好友")
        await frame.get_by_role("button", name="编辑好友", exact=True).click()
        await frame.locator("#field-douyin-friend-name").fill("修改后的本地好友")
        await frame.locator("#save-dialog").click()
        await frame.get_by_text("修改后的本地好友", exact=True).wait_for()
        await frame.get_by_role("button", name="停用分享", exact=True).click()
        await frame.get_by_role("button", name="启用分享", exact=True).wait_for()
        await frame.get_by_role("button", name="删除好友", exact=True).click()
        await frame.locator("#save-dialog").click()
        await frame.get_by_text("暂无抖音分享好友，不会向任何好友分享。", exact=True).wait_for()
        assert await frame.locator("#field-hourly_videos").input_value() == "22"
        await frame.locator("#field-hourly_videos").fill("21")
        await frame.get_by_role("heading", name="对话内主动回复", exact=True).wait_for()
        await frame.get_by_role("heading", name="聊天内容计划", exact=True).wait_for()
        await frame.get_by_role("heading", name="固定轮次主动回复", exact=True).wait_for()
        await frame.locator("#field-followup_average_probability").fill("0.1")
        await frame.locator("#field-followup_pity_limit").fill("5")
        await frame.locator("#field-context_plans_enabled").uncheck()
        await frame.locator("#field-periodic_proactive_enabled").uncheck()
        await frame.locator("#field-periodic_interval_minutes").fill("120")
        await frame.locator("#field-periodic_jitter_minutes").fill("20")
        await frame.get_by_role("button", name="保存应用设置", exact=True).click()
        await frame.locator("#notice").filter(has_text="自动纠正").wait_for()
        await frame.locator("#refresh:not([disabled])").wait_for()
        assert await frame.locator("#field-followup_average_probability").input_value() == "0.2"
        assert not await frame.locator("#field-context_plans_enabled").is_checked()
        assert not await frame.locator("#field-periodic_proactive_enabled").is_checked()
        assert await frame.locator("#field-context_plan_limit").input_value() == "5"
        await frame.get_by_text(
            "已保存并生效；休息时段仅停止后台刷视频，QQ 正常响应", exact=False
        ).wait_for()
        assert await frame.locator("#field-hourly_videos").input_value() == "21"
        await frame.get_by_role("button", name="系统提示词", exact=True).click()
        await frame.locator("#refresh:not([disabled])").wait_for()
        await frame.get_by_role("button", name="编辑提示词", exact=True).first.click()
        text = await frame.locator("#field-prompt").input_value()
        await frame.locator("#field-prompt").fill(text + "\n这是本地界面验证补充。")
        await frame.locator("#save-dialog").click()
        await frame.get_by_text(
            "提示词已保存；下一次调用读取新内容", exact=True
        ).wait_for()
        await page.set_viewport_size({"width": 430, "height": 900})
        await frame.get_by_role("button", name="应用设置", exact=True).click()
        await frame.locator("#refresh:not([disabled])").wait_for()
        overflow = await frame.locator("body").evaluate(
            "el => document.documentElement.scrollWidth > window.innerWidth + 1"
        )
        assert not overflow, "Control panel overflows narrow viewport"
        await page.screenshot(path=str(BASE / "mobile.png"), full_page=True)
        await frame.get_by_role("heading", name="抖音分享好友白名单", exact=True).scroll_into_view_if_needed()
        await page.screenshot(path=str(BASE / "mobile-douyin.png"), full_page=True)
        assert not errors, errors
        await browser.close()
    print(
        "WEB_CONTROL_BROWSER_PASS: 8 native pages, memory create/edit/delete, mobile, zero JavaScript errors"
    )


def main():
    import httpx

    BASE.mkdir(parents=True, exist_ok=True)
    sandbox = BASE / "instance"
    sandbox.mkdir(exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(sandbox)
    from astrbot.core.utils.auth_password import (
        hash_dashboard_password,
        hash_md5_dashboard_password,
    )

    data = sandbox / "data"
    data.mkdir(exist_ok=True)
    password = secrets.token_urlsafe(32)
    config = json.loads(
        (ROOT / "external/AstrBot/data/cmd_config.json").read_text(encoding="utf-8-sig")
    )
    config.update(
        platform=[],
        provider=[],
        provider_sources=[],
        admins_id=[],
        disable_metrics=True,
    )
    config["dashboard"] = {
        "enable": True,
        "host": "127.0.0.1",
        "port": 6186,
        "username": "webtest",
        "password": hash_md5_dashboard_password(password),
        "pbkdf2_password": hash_dashboard_password(password),
        "password_change_required": False,
        "password_storage_upgraded": True,
        "jwt_secret": secrets.token_urlsafe(48),
    }
    (data / "cmd_config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )
    source = ROOT / "integrations/qq_astrbot/plugin"
    target = data / "plugins/astrbot_chat_q1"
    shutil.copytree(
        source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
    )
    for name in [
        "qq_companion/prompts.yaml",
        "qq_companion/vision.yaml",
        "runtime/qwen3.yaml",
    ]:
        dest = sandbox / "configs" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "configs" / name, dest)
    state = BASE / "state"
    state.mkdir(exist_ok=True)
    from services.douyin_worker.store import Store

    store = Store(state)
    video = {
        "id": "7697985255608831242",
        "url": "https://www.douyin.com/video/7697985255608831242",
        "title": "开发验证视频（仅本地测试数据）",
    }
    store.encounter("test", video)
    store.save_video(
        "test",
        video["id"],
        "analyzed",
        analysis={
            "summary": "用于检查列表展示与折叠摘要。",
            "confidence": 0.9,
            "uncovered_intervals": [],
        },
        interest={
            "interesting": True,
            "funny": False,
            "high_quality": True,
            "confidence": 0.9,
            "reasons": ["表达清晰，节奏自然，有值得分享的内容。"],
            "uncertainty": [],
            **{
                k: False
                for k in (
                    "advertisement",
                    "spam",
                    "misinformation",
                    "sensitive",
                    "private_information",
                )
            },
        },
    )
    env = {
        **os.environ,
        "ASTRBOT_CHAT_STATE_DIR": str(state),
        "ASTRBOT_CHAT_PROMPTS_FILE": str(sandbox / "configs/qq_companion/prompts.yaml"),
        "ASTRBOT_CHAT_DOUYIN_CONFIG": str(
            sandbox / "configs/qq_companion/douyin.local.json"
        ),
        "QQ_COMPANION_TEST_PEER": "999",
        "ASTRBOT_DISABLE_METRICS": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    from scripts.qq_companion.owned_process import OwnedProcess

    with (BASE / "server.log").open("ab") as log:
        owner = OwnedProcess(
            [sys.executable, str(Path(__file__).resolve()), "--serve"],
            cwd=sandbox,
            env=env,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
        )
        try:
            with httpx.Client(base_url=URL, trust_env=False, timeout=30) as client:
                deadline = time.monotonic() + 100
                while True:
                    try:
                        response = client.get("/api/v1/auth/me")
                        break
                    except httpx.ConnectError:
                        if (
                            owner.process.poll() is not None
                            or time.monotonic() > deadline
                        ):
                            raise RuntimeError(
                                "Isolated AstrBot startup failed; inspect server.log"
                            )
                        time.sleep(0.5)
                prefix = "/api/v1/plugins/extensions/astrbot_chat_q1/control/"
                assert client.get(prefix + "memory").status_code == 401
                response = client.post(
                    "/api/v1/auth/login",
                    json={"username": "webtest", "password": password},
                )
                response.raise_for_status()
                token = response.json()["data"]["token"]
                client.headers["Authorization"] = "Bearer " + token
                for name in [
                    "overview",
                    "videos",
                    "memory",
                    "contexts",
                    "exchanges",
                    "model",
                    "persona",
                    "usage",
                    "settings",
                    "whitelist",
                    "douyin-whitelist",
                    "prompts",
                ]:
                    response = client.get(prefix + name)
                    assert (
                        response.status_code == 200
                        and response.json()["status"] == "ok"
                    ), (name, response.text[:300])
                env["WEB_CONTROL_PROBE_TOKEN"] = token
                env["PLAYWRIGHT_BROWSERS_PATH"] = str(
                    ROOT / "tools/playwright-browsers"
                )
                subprocess.run(
                    [
                        str(ROOT / "envs/douyin/Scripts/python.exe"),
                        str(Path(__file__).resolve()),
                        "--browser",
                    ],
                    env=env,
                    cwd=ROOT,
                    check=True,
                )
                assert client.get(prefix + "whitelist").json()["data"]["enabled"] is False
                assert client.get(prefix + "douyin-whitelist").json()["data"]["rows"] == []
            print(
                "WEB_CONTROL_NATIVE_PASS: authentication enforced, 12 real plugin endpoints, QQ and Douyin whitelist CRUD, no external sends"
            )
        finally:
            owner.stop()


if __name__ == "__main__":
    if "--serve" in sys.argv:
        asyncio.run(serve())
    elif "--browser" in sys.argv:
        asyncio.run(browser())
    else:
        main()
