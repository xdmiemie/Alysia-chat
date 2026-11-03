"""One explicitly authorized native test share; production quota/ledger apply."""
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


async def run(url):
    from integrations.qq_astrbot.plugin.inference_queue import state_directory
    from integrations.qq_astrbot.plugin.foreground_activity import ForegroundActivity
    from integrations.qq_astrbot.plugin.media_state import MediaState
    from services.douyin_worker.browser_session import BrowserSession
    from services.douyin_worker.config import ConfigStore
    from services.douyin_worker.feed_driver import canonical_video, FeedDriver
    from services.douyin_worker.store import Store
    directory, settings = state_directory(), ConfigStore()
    config = settings.read()
    if len(config.friends) != 1 or config.friends[0].douyin_id != "dywklklhl" or config.account.douyin_id != "52663919986":
        raise RuntimeError("unexpected_test_account_or_recipient")
    friend = config.friends[0]
    # The user explicitly authorized arbitrary video tests outside the normal
    # scheduler. This in-memory override never changes daily production hours.
    test_config = config.model_copy(update={"enabled": True, "start_hour": 0, "end_hour": 24})
    key, url = canonical_video(url)
    video = {"id": key, "url": url}
    store, browser = Store(directory), BrowserSession(directory)
    output = ROOT / "storage/temporary/q9-share-probe"
    output.mkdir(parents=True, exist_ok=True)
    reservation = None
    report = {"video": url, "recipient": friend.douyin_id, "manual_development_test": True}
    def checkpoint():
        if ForegroundActivity(directory).blocked() or MediaState(directory).mode() != "ready":
            raise RuntimeError("foreground_or_resource_busy")
        current = settings.read()
        if current.account != config.account or not current.share_enabled or not any(f == friend and f.opted_in for f in current.friends):
            raise RuntimeError("test_policy_changed")
    def before_send():
        nonlocal reservation
        checkpoint()
        reservation, reason = store.reserve_share(test_config, friend.douyin_id, video)
        if not reservation:
            raise RuntimeError(reason)
    try:
        checkpoint()
        await browser.open(config)
        receipt = await FeedDriver(browser).share(video, friend, checkpoint, before_send, test_config)
        store.finish_share(reservation, "sent", "manual_test:" + receipt)
        report.update(result="sent", evidence=receipt, ledger_id=reservation)
    except BaseException as exc:
        if reservation:
            store.finish_share(reservation, "uncertain", "manual_test_unconfirmed")
            store.set("halt", "share_result_uncertain")
        report.update(result="uncertain" if reservation else "not_sent", error=str(exc)[:150])
    finally:
        await browser.close()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return int(report["result"] != "sent")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True)
    raise SystemExit(asyncio.run(run(parser.parse_args().video)))
