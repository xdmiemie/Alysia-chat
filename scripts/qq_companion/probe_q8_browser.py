"""Read-only real-browser adapter probe; never clicks send/like/follow."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "tools/playwright-browsers"))


async def run(mode):
    from integrations.qq_astrbot.plugin.inference_queue import state_directory
    from services.douyin_worker.browser_session import BrowserSession
    from services.douyin_worker.config import ConfigStore
    from services.douyin_worker.feed_driver import FeedDriver
    directory = state_directory()
    browser, config = BrowserSession(directory), ConfigStore().read()
    output = ROOT / "storage/temporary/q8-browser-probe"
    output.mkdir(parents=True, exist_ok=True)
    report = {}
    try:
        await browser.open(config)
        if mode in {"verify", "feed", "share-ui"}:
            report["account"] = await browser.verify_account(config)
        if mode == "verify":
            report["friends"] = [await browser.verify_friend(friend) for friend in config.friends if friend.opted_in]
            report["friend_metadata"] = browser.friend_metadata
        if mode in {"feed", "share-ui"}:
            link = browser.page.get_by_role("link", name="推荐", exact=True)
            if await link.count() == 1:
                await link.click()
            await asyncio.sleep(4)
            await browser.resume()
            report["current"] = await FeedDriver(browser).current()
        if mode == "share-ui":
            report["friend"] = await browser.verify_friend(config.friends[0])
            report["friend_metadata"] = browser.friend_metadata
            await browser.pause()
            await browser.page.locator('[data-e2e="feed-active-video"] [data-e2e="video-player-share"]').hover()
            await asyncio.sleep(2)
            report["share_ui"] = await browser.page.locator('body').inner_text()
            report["share_dom"] = await browser.page.get_by_text(config.friends[0].name, exact=False).evaluate_all(
                "els=>els.map(e=>e.parentElement.parentElement.parentElement.outerHTML.slice(0,26000))")
        await asyncio.sleep(3)
        report["diagnostics"] = await browser.diagnostics()
        await browser.page.screenshot(path=str(output / "page.png"))
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "code": str(exc)[:300]}
        if browser.page and not browser.page.is_closed():
            report["profile_dom"] = await browser.page.get_by_text(config.account.name, exact=False).evaluate_all(
                "els=>els.map(e=>({tag:e.tagName,text:e.innerText,visible:e.getBoundingClientRect().width>0,html:e.outerHTML.slice(0,2000)}))")
            report["diagnostics"] = await browser.diagnostics()
            await browser.page.screenshot(path=str(output / "page.png"))
    finally:
        await browser.close()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"success": "error" not in report, "report": str(output / "report.json")}, ensure_ascii=False))
    return int("error" in report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["inspect", "verify", "feed", "share-ui"], default="inspect", nargs="?")
    raise SystemExit(asyncio.run(run(parser.parse_args().mode)))
