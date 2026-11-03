"""The sole Q6 publisher. No QQ conversation/history/remember callback exists."""
from __future__ import annotations

import asyncio
from datetime import date, datetime
import json
from pathlib import Path
import re
import sqlite3

from .diary_jobs import resolve_timezone
from .persistent_memory import data_safe
from .prompt_loader import load_prompts
from .qzone_client import QzoneError
from .qzone_config import load_config
from .qzone_store import QzoneStore, digest


TEXT_SCHEMA = {"type": "object", "properties": {"sentences": {"type": "array", "minItems": 2, "maxItems": 2,
    "items": {"type": "string", "minLength": 1, "maxLength": 90}}},
    "required": ["sentences"], "additionalProperties": False}
INTERACTION_SCHEMA = {"type": "object", "properties": {
    "like": {"type": "boolean"}, "comment": {"type": ["string", "null"], "maxLength": 60}},
    "required": ["like", "comment"], "additionalProperties": False}
SELF_GATES = ("cookies", "login", "list_self", "detail_self", "post", "delete")


def public_text(text, limit=400):
    return (isinstance(text, str) and 0 < len(text.strip()) <= limit and data_safe(text)
            and not re.search(r"https?://|[0-9]{5,}|qq\s*[:：]|@|<|>|\[CQ:|[\x00-\x08]", text, re.I))


def two_sentences(value, limit=100):
    lines = value.get("sentences") if isinstance(value, dict) else None
    if not isinstance(lines, list) or len(lines) != 2:
        raise ValueError("qzone_requires_two_sentences")
    result = []
    for line in lines:
        if not isinstance(line, str):
            raise ValueError("qzone_invalid_sentence")
        line = line.strip().rstrip("。！？.!? ")
        if not line or re.search(r"[。！？!?\r\n]", line):
            raise ValueError("qzone_invalid_sentence")
        result.append(line + "。")
    text = "".join(result)
    if not public_text(text, limit):
        raise ValueError("qzone_invalid_public_text")
    return text


def post_content(post):
    return str(post.get("content", ""))


def audience_verified(payload, audience):
    """Never infer permission from a successful HTTP response alone."""
    candidates = [payload]
    if isinstance(payload.get("data"), dict):
        candidates.append(payload["data"])
    for item in candidates:
        rights = item.get("rights")
        if not isinstance(rights, dict):
            rights = item
        raw = rights.get("ugc_right", rights.get("right"))
        if str(raw) != ("64" if audience["visibility"] == "self" else "16"):
            continue
        if audience["visibility"] == "self":
            return True
        allowed = rights.get("allow_uins", item.get("allow_uins", item.get("ugc_whitelist")))
        if isinstance(allowed, str):
            allowed = re.findall(r"[0-9]+", allowed)
        if isinstance(allowed, list) and all(isinstance(x, dict) and "uin" in x for x in allowed):
            allowed = [str(x["uin"]) for x in allowed]
        if isinstance(allowed, list) and {str(x) for x in allowed} == set(audience["friends"]):
            return True
    return False


class DiaryPublisher:
    def __init__(self, directory, memory, client_factory, *, configuration=None, logger=None):
        self.directory = Path(directory)
        self.memory, self.client_factory = memory, client_factory
        self.configuration, self.logger = configuration, logger
        self.store = QzoneStore(self.directory / "qzone.sqlite3")
        self.lock = asyncio.Lock()
        self.scheduler, self.task = None, None
        self.closed = False

    async def start(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.scheduler.add_job(self.tick, "interval", seconds=60, id="q6-publisher",
            coalesce=True, max_instances=1, misfire_grace_time=60)
        self.scheduler.start()
        self.task = asyncio.create_task(self.tick())

    async def close(self):
        self.closed = True
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        # Track both the startup task and scheduler invocations.
        tasks = list(getattr(self, "running", set()))
        if self.task and self.task not in tasks:
            tasks.append(self.task)
        for task in tasks:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in tasks if t is not asyncio.current_task()), return_exceptions=True)

    def config(self):
        return load_config(self.configuration)

    def candidate(self, account, day):
        """SQL view deliberately excludes the private diary column."""
        path = self.directory / "memories.sqlite3"
        if not path.exists():
            return []
        with sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='sm_records'").fetchone():
                # New private diaries are never relabeled public. Only a separate
                # shared-input projection may become a publication candidate.
                rows = db.execute("SELECT body FROM sm_records WHERE bot=? AND character=? AND day=? "
                    "AND kind='diary_public' AND visibility='shared' AND retention='normal' AND status='active'",
                    (account, self.memory.character, day)).fetchall()
                return [r[0] for r in rows if public_text(r[0], 400)]
            row = db.execute("SELECT draft FROM diary_public_candidates WHERE bot=? AND character_id=? AND entry_date=?",
                             (account, self.memory.character, day)).fetchone()
            if row:
                return [row[0]] if public_text(row[0], 400) else None
            private_exists = db.execute("SELECT 1 FROM diary_entries WHERE bot=? AND character_id=? AND entry_date=?",
                                       (account, self.memory.character, day)).fetchone()
            if private_exists or not self.memory.enabled or not self.memory.diary_enabled:
                return []  # Quiet day: never substitute the private draft.
            account_exists = db.execute("SELECT 1 FROM memory_accounts WHERE bot=? AND character_id=?",
                                       (account, self.memory.character)).fetchone()
            return None if account_exists else []  # Pending Q5 work waits until today's deadline.

    async def generate(self, key, materials, config):
        if any(not public_text(m, 400) for m in materials):
            raise ValueError("qzone_non_public_material")
        prompts = load_prompts()
        async with self.memory.admission():
            provider = await self.memory.provider_factory()
            persona = await self.memory.persona_factory() if self.memory.persona_factory else ""
            value = await self.memory._utility(provider, prompts[key], {
                "persona": persona, "materials": materials, "date": self.now(config).date().isoformat(),
                "max_characters": config["daily_max_chars"]}, TEXT_SCHEMA, max_tokens=384)
        return two_sentences(value, config["daily_max_chars"])

    @staticmethod
    def now(config):
        return datetime.now(resolve_timezone(config["timezone"]))

    @staticmethod
    def audience(config):
        return {"visibility": config["visibility"], "friends": sorted(config["friend_allowlist"])
                if config["visibility"] == "allowlist" else []}

    def can_send(self, config, kind):
        account = config["account_id"]
        if not config["enabled"] or self.store.stopped(account) or not self.store.passed(account, *SELF_GATES):
            return False
        if kind in {"comment", "like"}:
            return (config["interactions_enabled"] and self.store.passed(account, "read_friend", kind))
        return (config["visibility"] == "self" and self.store.passed(account, "visibility_self")) or (config["visibility"] == "allowlist" and bool(config["friend_allowlist"])
                and self.store.passed(account, "visibility_allowlist"))

    async def verify_post(self, client, tid, content, audience):
        for attempt in range(3):
            posts = await client.list_posts(limit=20)
            matches = [p for p in posts if str(p.get("tid")) == str(tid)]
            if matches and digest(post_content(matches[0])) == digest(content):
                detail = await client.detail(client.account, tid)
                if audience_verified(detail, audience) or audience_verified(matches[0], audience):
                    return True
                return False
            if attempt < 2:
                await asyncio.sleep(1)
        return False

    async def deliver(self, row, config, client):
        # Re-read all mutable policy immediately before reserving a side effect.
        current = self.config()
        if self.closed or current != config or row["account"] != current["account_id"] or not self.can_send(current, row["kind"]):
            return False
        if row["day"] != self.now(current).date().isoformat():
            self.store.finish(row["id"], "expired")
            return False
        if row["kind"] == "daily" and not current["daily_time"] <= self.now(current).strftime("%H:%M") <= current["daily_deadline"]:
            if self.now(current).strftime("%H:%M") > current["daily_deadline"]:
                self.store.finish(row["id"], "expired")
            return False
        interaction = row["kind"] in {"comment", "like"}
        target, _, tid = row["target"].partition(":")
        if interaction and target not in current["friend_allowlist"]:
            return False
        if not interaction and json.loads(row["audience"]) != self.audience(current):
            self.store.finish(row["id"], "expired", error="audience_changed")
            return False
        limit = 1 if row["kind"] == "daily" else (current[row["kind"] + "_daily_limit"] if interaction else None)
        # Authentication is a read operation. A failure here has not sent a write.
        await client.login()
        if self.closed or self.config() != current:
            return False
        if not self.store.claim(row["id"], daily_limit=limit):
            return False
        try:
            if row["kind"] in {"daily", "event"}:
                platform_id = await client.publish(row["content"], json.loads(row["audience"]))
                # Save the returned ID before verification, including crash recovery.
                self.store.finish(row["id"], "unknown", platform_id=platform_id)
                if not await self.verify_post(client, platform_id, row["content"], json.loads(row["audience"])):
                    self.store.stop(row["account"], "post_or_audience_not_verified")
                    return False
            elif row["kind"] == "comment":
                await client.comment(target, tid, row["content"])
                detail = await client.detail(target, tid)
                if not self.comment_visible(detail, client.account, row["content"]):
                    raise QzoneError("qzone_comment_not_verified", uncertain=True)
                platform_id = tid
            elif row["kind"] == "like":
                await client.like(target, tid)
                detail = await client.detail(target, tid)
                if not self.like_visible(detail):
                    raise QzoneError("qzone_like_not_verified", uncertain=True)
                platform_id = tid
            else:
                raise ValueError("qzone_test_path_required")
            self.store.finish(row["id"], "sent", platform_id=platform_id)
            return True
        except asyncio.CancelledError:
            self.store.finish(row["id"], "unknown", error="canceled_during_write")
            raise
        except QzoneError as exc:
            self.store.finish(row["id"], "unknown" if exc.uncertain else "failed", error=exc.code)
            self.store.stop(row["account"], exc.code)
            return False
        except Exception:
            self.store.finish(row["id"], "unknown", error="unexpected_write_failure")
            self.store.stop(row["account"], "unexpected_write_failure")
            return False

    @staticmethod
    def comment_visible(detail, account, text):
        data = detail.get("data") if isinstance(detail.get("data"), dict) else detail
        comments = data.get("commentlist", [])
        return isinstance(comments, list) and any(isinstance(r, dict) and str(r.get("uin")) == account
            and r.get("content") == text for r in comments)

    @staticmethod
    def like_visible(detail):
        data = detail.get("data") if isinstance(detail.get("data"), dict) else detail
        return data.get("isLiked") is True or data.get("isLiked") == 1

    async def reconcile(self, client, account):
        self.store.recover(account)
        rows = [r for r in self.store.rows(account, statuses={"unknown"}) if r["kind"] in {"daily", "event", "test"}]
        if not rows:
            return
        posts = await client.list_posts(limit=20)
        for row in rows:
            matched = [p for p in posts if (not row["platform_id"] or str(p.get("tid")) == row["platform_id"])
                       and digest(post_content(p)) == row["content_hash"]]
            if len(matched) == 1:
                tid = str(matched[0]["tid"])
                if await self.verify_post(client, tid, row["content"], json.loads(row["audience"])):
                    self.store.finish(row["id"], "sent", platform_id=tid)
        # No match never authorizes replay; an unknown request stays reserved.

    async def delete(self, client, key):
        row = self.store.get(key)
        if not row or row["account"] != client.account or row["kind"] not in {"daily", "event", "test"} or not row["platform_id"]:
            raise ValueError("qzone_delete_not_owned")
        if row["status"] == "deleted":
            return True
        before = await client.list_posts(limit=20)
        if not any(str(p.get("tid")) == row["platform_id"] and digest(post_content(p)) == row["content_hash"] for p in before):
            if row["status"] == "delete_unknown":
                self.store.finish(key, "deleted")
                return True
            # A limited first page cannot prove an old/absent post was deleted.
            raise QzoneError("qzone_delete_target_not_verified")
        self.store.finish(key, "delete_unknown")
        error = None
        try:
            await client.delete(row["platform_id"])
        except QzoneError as exc:
            error = exc
            if exc.auth:
                self.store.stop(client.account, exc.code)
        for attempt in range(3):
            posts = await client.list_posts(limit=20)
            if not any(str(p.get("tid")) == row["platform_id"] for p in posts):
                self.store.finish(key, "deleted")
                return True
            if attempt < 2:
                await asyncio.sleep(1)
        raise error or QzoneError("qzone_delete_not_verified")

    async def interactions(self, client, config, now):
        account, day = config["account_id"], now.date().isoformat()
        if (not config["interactions_enabled"] or now.strftime("%H:%M") < config["interaction_time"]
                or not self.store.passed(account, "read_friend", "comment", "like")
                or not self.can_send(config, "comment") or self.store.scanned(account, day)):
            return
        # One small scan per day, limited strictly to the configured friends.
        if not self.store.mark_scanned(account, day):
            return
        for target in config["friend_allowlist"]:
            posts = await client.list_posts(target, limit=3)
            for post in posts[:1]:
                tid = str(post.get("tid", ""))
                content = post_content(post)
                if str(post.get("uin")) != target or not re.fullmatch(r"[\w-]{1,100}", tid) or not public_text(content, 1000):
                    continue
                # External posts stay data in this one request, never memory.
                async with self.memory.admission():
                    provider = await self.memory.provider_factory()
                    persona = await self.memory.persona_factory() if self.memory.persona_factory else ""
                    result = await self.memory._utility(provider, load_prompts()["qzone_interaction"],
                        {"persona": persona, "post": content}, INTERACTION_SCHEMA, max_tokens=256)
                reference = target + ":" + tid
                comment = result["comment"]
                if comment and public_text(comment, 60):
                    self.store.draft(account, "comment", day, reference, comment, {}, target=reference)
                if result["like"]:
                    self.store.draft(account, "like", day, reference, "", {}, target=reference)

    async def tick(self, now=None):
        if self.closed or self.lock.locked():
            return
        task = asyncio.current_task()
        if not hasattr(self, "running"):
            self.running = set()
        self.running.add(task)
        client = None
        try:
            async with self.lock:
                config = self.config()
                account = config["account_id"]
                if not config["enabled"] or not account or self.store.stopped(account):
                    return
                now = now or self.now(config)
                if now.tzinfo is None:
                    raise ValueError("qzone_aware_clock_required")
                day, clock = now.date().isoformat(), now.strftime("%H:%M")
                if not self.can_send(config, "daily"):
                    return
                client = self.client_factory(account)
                await self.reconcile(client, account)
                audience = self.audience(config)
                existing = self.store.rows(account)
                if config["daily_time"] <= clock <= config["daily_deadline"] and not any(
                        r["kind"] == "daily" and r["day"] == day for r in existing):
                    materials = self.candidate(account, day)
                    if materials is not None:
                        text = await self.generate("qzone_daily", materials, config)
                        self.store.draft(account, "daily", day, "daily:" + day, text, audience)
                if clock >= config["event_time"]:
                    for event in config["events"]:
                        scheduled = date.fromisoformat(event["date"])
                        if (scheduled == now.date() or (event.get("annual", False)
                                and (scheduled.month, scheduled.day) == (now.month, now.day))):
                            source = "event:" + event["id"] + ":" + day
                            if not any(r["kind"] == "event" and r["source_id"] == source for r in existing):
                                text = await self.generate("qzone_event", [event["text"]], config)
                                self.store.draft(account, "event", day, source, text, audience)
                await self.interactions(client, config, now)
                for row in self.store.rows(account, statuses={"draft"}):
                    if self.closed or self.store.stopped(account):
                        break
                    await self.deliver(row, config, client)
        except QzoneError as exc:
            if client and exc.auth:
                self.store.stop(client.account, exc.code)
            if self.logger:
                self.logger.warning("Q6 deferred: %s", exc.code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.logger:
                self.logger.warning("Q6 deferred: %s", type(exc).__name__)
        finally:
            if client:
                await client.close()
            self.running.discard(task)
