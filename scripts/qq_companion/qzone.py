"""Local Q6 admin CLI. No QQ chat command can invoke these side effects."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
STATE = ROOT / "storage/qq_companion"

from integrations.qq_astrbot.plugin.qzone_client import OneBotHTTP, QzoneClient, QzoneError
from integrations.qq_astrbot.plugin.qzone_config import DEFAULTS, load_config, save_config
from integrations.qq_astrbot.plugin.qzone_runtime import DiaryPublisher, audience_verified, post_content, public_text
from integrations.qq_astrbot.plugin.qzone_store import QzoneStore, digest


def client_factory(account):
    from scripts.qq_companion.configure import locate_snowluma_config, read_json
    path = locate_snowluma_config(os.environ.get("QQ_COMPANION_SNOWLUMA_CONFIG"))
    config = read_json(path)
    server = next((s for s in config.get("networks", {}).get("httpServers", []) if s.get("port") == 3000), None)
    if not server or server.get("host") not in {"127.0.0.1", "localhost"}:
        raise ValueError("qzone_loopback_onebot_required")
    return QzoneClient(OneBotHTTP(server.get("accessToken", "")), account)


async def discover(account, destination=None):
    """Read identities only. Require the exact user-described group/peer shape."""
    client = client_factory(account)
    try:
        info = await client.onebot.call("get_login_info")
        if str(info.get("user_id")) != account:
            raise QzoneError("qzone_account_mismatch", auth=True)
        groups = await client.onebot.call("get_group_list")
        friends = {str(x["user_id"]) for x in await client.onebot.call("get_friend_list")}
        members = set()
        for group in groups:
            values = await client.onebot.call("get_group_member_list", group_id=int(group["group_id"]))
            members.update(str(x["user_id"]) for x in values)
        allowed = sorted((friends & members) - {account})
        if len(groups) != 2 or len(allowed) != 3:
            print(f"Discovery pending: {len(groups)} groups, {len(allowed)} matching friends; allowlist unchanged.")
            return False
        config = load_config(destination)
        config["account_id"], config["friend_allowlist"] = account, allowed
        save_config(config, destination)
        print("Resolved the three authorized friends from the two groups; saved to local config only.")
        return True
    finally:
        await client.close()


async def self_test(account, count=5, *, store=None, config=None, factory=None):
    """Own posts only, no friend feeds/comments/likes or model/memory access.

    Persist every write reservation. Before deletion is verified the lifetime
    test-write budget is ten, including unknown and rejected submissions.
    """
    if not 1 <= count <= 10:
        raise ValueError("qzone_test_count_1_to_10")
    store = store or QzoneStore(STATE / "qzone.sqlite3")
    config = config or load_config()
    client = (factory or client_factory)(account)
    publisher = DiaryPublisher.__new__(DiaryPublisher)
    publisher.store = store
    report = {"account_matched": False, "count_requested": count, "posts_verified": 0, "deletions_verified": 0,
              "friend_reads": 0, "comments": 0, "likes": 0, "memory_writes": 0, "chat_context_writes": 0,
              "gate": "PENDING", "errors": []}
    report_path = ROOT / "storage/temporary/q6/self-test-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    def save():
        report["gates"] = store.summary(account)["gates"]
        tests = [r for r in store.rows(account) if r["kind"] == "test"]
        report["total_test_submissions"] = sum(r["attempts"] > 0 for r in tests)
        report["total_tests_deleted"] = sum(r["status"] == "deleted" for r in tests)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        present = await client.login()
        report["account_matched"], report["cookie_fields"] = True, present
        store.gate(account, "cookies", True, "required_fields_present_and_account_matched")
        await client.list_posts(limit=20)
        store.gate(account, "login", True, "authenticated_self_read")
        store.gate(account, "list_self", True, "authenticated_self_read")
        # Clean up only our own previously recorded tests before creating more.
        for row in store.rows(account, statuses={"sent", "unknown", "sending", "delete_unknown"}):
            if row["kind"] == "test":
                if not row["platform_id"]:
                    posts = await client.list_posts(limit=20)
                    matches = [p for p in posts if digest(post_content(p)) == row["content_hash"]]
                    if len(matches) == 1:
                        store.finish(row["id"], "unknown", platform_id=str(matches[0]["tid"]))
                        row = store.get(row["id"])
                    else:
                        raise QzoneError("qzone_unresolved_previous_test", uncertain=True)
                await publisher.delete(client, row["id"])
                store.gate(account, "delete", True, "own_test_removed_from_authenticated_list")
        # Test private visibility first; later tests may verify the authorized audience.
        for index in range(count):
            previous = [r for r in store.rows(account) if r["kind"] == "test" and r["attempts"] > 0]
            if not store.passed(account, "delete") and len(previous) >= 10:
                raise QzoneError("qzone_test_budget_exhausted")
            if store.stopped(account):
                # Explicit test-self is the admin recovery path; never an automatic retry.
                store.resume(account)
            audience = {"visibility": "self", "friends": []}
            if index > 0 and len(config["friend_allowlist"]) == 3:
                audience = {"visibility": "allowlist", "friends": sorted(config["friend_allowlist"])}
            marker = uuid.uuid4().hex[:12]
            text = "Q6功能验证临时内容（" + marker + "）。验证后删除，不作为聊天或记忆资料。"
            source = "self-test:" + uuid.uuid4().hex
            row = store.draft(account, "test", datetime.now().date().isoformat(), source, text, audience)
            if not store.claim(row["id"]):
                raise QzoneError("qzone_test_reservation_failed")
            try:
                tid = await client.publish(text, audience)
                store.finish(row["id"], "unknown", platform_id=tid)
                visible = False
                for attempt in range(3):
                    posts = await client.list_posts(limit=20)
                    matches = [p for p in posts if str(p.get("tid")) == tid and post_content(p) == text]
                    if matches:
                        visible = True
                        break
                    if attempt < 2:
                        await asyncio.sleep(1)
                if not visible:
                    raise QzoneError("qzone_test_post_not_visible", uncertain=True)
                detail = await client.detail(account, tid)
                store.gate(account, "detail_self", True, "own_post_detail_read")
                store.gate(account, "post", True, "own_test_content_and_id_visible")
                verified = audience_verified(detail, audience) or audience_verified(matches[0], audience)
                report.setdefault("audience_checks", []).append({"requested": audience["visibility"], "verified": verified,
                    "detail_keys": sorted(detail),
                    "data_keys": sorted(detail["data"]) if isinstance(detail.get("data"), dict) else [],
                    "whitelist_shape": re.sub(r'[0-9]+', '<id>', json.dumps(detail.get('ugc_whitelist'), ensure_ascii=False)),
                    "list_keys": sorted(matches[0]),
                    "rights_shape": {k: (sorted(v) if isinstance(v, dict) else type(v).__name__)
                        for k, v in matches[0].items() if k in {"rights", "ugc_right", "right", "allow_uins"}}})
                if verified:
                    store.gate(account, "visibility_" + audience["visibility"], True, "server_returned_exact_access_rights")
                report["posts_verified"] += 1
                store.finish(row["id"], "sent", platform_id=tid)
            except BaseException as exc:
                store.finish(row["id"], "unknown" if not isinstance(exc, QzoneError) or exc.uncertain else "failed",
                             error=exc.code if isinstance(exc, QzoneError) else type(exc).__name__)
                raise
            finally:
                # Delete even when detail/privacy verification fails after publishing.
                owned = store.get(row["id"])
                if not owned["platform_id"] and owned["status"] == "unknown":
                    posts = await client.list_posts(limit=20)
                    matches = [p for p in posts if digest(post_content(p)) == owned["content_hash"]]
                    if len(matches) == 1:
                        store.finish(row["id"], "unknown", platform_id=str(matches[0]["tid"]))
                        owned = store.get(row["id"])
                if owned["platform_id"]:
                    await publisher.delete(client, row["id"])
                    store.gate(account, "delete", True, "own_test_removed_from_authenticated_list")
                    report["deletions_verified"] += 1
                save()
            print(f"Q6 self-post {index + 1}/{count}: published, queried, deleted.", flush=True)
        report["gate"] = "SELF_POST_PASS_INTERACTIONS_PENDING"
    except QzoneError as exc:
        report["errors"].append(exc.code)
        report["gate"] = "SELF_POST_BLOCKED"
        store.stop(account, exc.code)
        if exc.auth:
            store.gate(account, "cookies", False, exc.code)
    finally:
        save()
        report_path.with_name("self-test-" + str(time.time_ns()) + ".json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        await client.close()
    print(report["gate"] + "; no friend interaction or chat/memory writes.")
    return report


async def main_async(args):
    config = load_config(args.config)
    account = str(args.account or config["account_id"])
    store = QzoneStore(STATE / "qzone.sqlite3")
    if args.command == "configure":
        if args.time:
            config["daily_time"] = args.time
        if args.enable:
            config["enabled"] = True
        if args.disable:
            config["enabled"] = False
        if account:
            config["account_id"] = account
        save_config(config, args.config)
        print("Q6 local policy saved; the running publisher reloads it each minute.")
        return 0
    if not account:
        raise ValueError("qzone_account_required")
    if args.command == "status":
        print(json.dumps(store.summary(account), ensure_ascii=False, indent=2))
    elif args.command == "discover-friends":
        return 0 if await discover(account, args.config) else 2
    elif args.command == "test-self":
        report = await self_test(account, args.count, store=store, config=config)
        return 0 if report["gate"].startswith("SELF_POST_PASS") else 2
    elif args.command in {"stop", "resume"}:
        store.stop(account, "admin_stop") if args.command == "stop" else store.resume(account)
        print("Q6 " + args.command + " recorded; capability gates still apply.")
    elif args.command == "confirm-interaction-gate":
        if not args.confirmed_visible or not args.evidence.strip():
            raise ValueError("qzone_visible_evidence_required")
        # Local administrator attestation after actual webpage verification.
        # No model/QQ tool can reach this entry point; never infer from HTTP 200.
        store.gate(account, args.capability, True, "admin_web_verification: " + args.evidence)
        print("Administrator's visible verification recorded; interaction policy remains separately disabled.")
    elif args.command == "event":
        if not public_text(args.text, 400):
            raise ValueError("qzone_event_must_be_public")
        event = {"id": args.id, "date": args.date, "annual": args.annual, "text": args.text}
        config["events"] = [e for e in config["events"] if e["id"] != args.id] + [event]
        save_config(config, args.config)
        print("Public event saved. Same event and date publish at most once, without a daily event quota.")
    elif args.command in {"delete", "reconcile"}:
        client = client_factory(account)
        publisher = DiaryPublisher.__new__(DiaryPublisher)
        publisher.store = store
        try:
            await publisher.delete(client, args.id) if args.command == "delete" else await publisher.reconcile(client, account)
            print("Q6 " + args.command + " completed; inspect status for unresolved writes.")
        finally:
            await client.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--account")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "stop", "resume", "discover-friends", "reconcile"):
        commands.add_parser(name)
    p = commands.add_parser("configure")
    p.add_argument("--time")
    switch = p.add_mutually_exclusive_group()
    switch.add_argument("--enable", action="store_true")
    switch.add_argument("--disable", action="store_true")
    p = commands.add_parser("test-self")
    p.add_argument("--count", type=int, default=5)
    p = commands.add_parser("delete")
    p.add_argument("id", help="Local outbox ID, never an arbitrary platform post ID")
    p = commands.add_parser("event")
    p.add_argument("--id", required=True)
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--annual", action="store_true")
    p.add_argument("--text", required=True, help="Public event description; never private chat content")
    p = commands.add_parser("confirm-interaction-gate", help="Admin attestation after real visible tests; never used for untested capabilities")
    p.add_argument("--capability", required=True, choices=["read_friend", "comment", "like"])
    p.add_argument("--evidence", required=True, help="Date, five real checks and webpage verification evidence")
    p.add_argument("--confirmed-visible", action="store_true")
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(args))
    except (QzoneError, ValueError, OSError) as exc:
        print("Q6 stopped: " + (exc.code if isinstance(exc, QzoneError) else type(exc).__name__))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
