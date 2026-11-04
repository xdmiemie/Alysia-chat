"""Resumable module policies. Model calls occur outside SQLite transactions.

The injected model is tool-free, admitted by the existing inference scheduler,
and counts the actual request. Tests inject a deterministic model and clock.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, time as day_time, timedelta
import json
from zoneinfo import ZoneInfo

import jsonschema

from .shared_memory import MemoryConflict, encoded, digest
from .memory_repository import identity


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1}
SUMMARY = obj({"summary": TEXT})
SELECT = obj({"summary": TEXT, "selected_ids": {"type": "array", "uniqueItems": True, "items": TEXT}})
SPECIAL = obj({"entries": {"type": "array", "items": obj({
    "user_event": TEXT, "agreement_event": TEXT, "intent_quote": TEXT, "agreement_quote": TEXT,
    "long_term_intent": {"type": "boolean"}, "character_likes": {"type": "boolean"},
    "attribute": {"type": "string", "minLength": 1, "maxLength": 80}, "summary": TEXT})}})
REVIEW = obj({"supported": {"type": "boolean"}, "durable": {"type": "boolean"},
    "current": {"type": "boolean"}, "existing_id": {"type": ["string", "null"]},
    "reason": {"type": "string", "maxLength": 160}})
CORRECTION = obj({"corrections": {"type": "array", "items": obj({"existing_id": TEXT, "user_event": TEXT,
    "quote": TEXT, "replacement": TEXT})}})
CORRECTION_REVIEW = obj({"approved_ids": {"type": "array", "uniqueItems": True, "items": TEXT}})


class InputTooLarge(ValueError):
    pass


class MemoryTasks:
    def __init__(self, service, model, *, count_text, persona="", checkpoint=None):
        self.service, self.model, self.count_text = service, model, count_text
        self.persona, self.checkpoint = persona, checkpoint
        self.job = None

    async def call(self, key, payload, schema, tokens):
        if self.checkpoint:
            self.checkpoint()
        if self.job:
            self.service.renew(self.job)
        operation = asyncio.create_task(self.model(key, {"persona": self.persona, **payload}, schema, tokens))
        try:
            while not operation.done():
                await asyncio.wait({operation}, timeout=.25)
                if self.checkpoint:
                    self.checkpoint()
            value = await operation
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
        jsonschema.validate(value, schema)
        if self.checkpoint:
            self.checkpoint()
        return value

    async def summary(self, prompt, materials, *, tokens, chars=None, candidates=None, k=0, extra=None):
        """Hierarchical coverage, including old rollups; never silently take first N.

        Selection is a model tournament. Every candidate competes, nodes carry
        original IDs, and only validated original bodies enter the final batch.
        """
        candidates = candidates or []
        schema = SELECT if k else SUMMARY
        payload = dict(materials=materials, candidates=candidates, required_count=k,
            max_summary_tokens=tokens, max_summary_chars=chars, **(extra or {}))
        try:
            value = await self.call(prompt, payload, schema, max(256, tokens + 512 + k * 90))
        except (InputTooLarge,) as exc:
            if len(materials) <= 1 and len(candidates) <= 1:
                # Split long real messages by character, preserving their source IDs.
                if len(materials) == 1 and len(materials[0].get("summary", "")) > 256:
                    item = materials[0]
                    text = item["summary"]
                    materials = [{**item, "summary": text[:len(text)//2]}, {**item, "summary": text[len(text)//2:]}]
                else:
                    raise exc
            middle = max(1, len(materials) // 2)
            halves = (materials[:middle], materials[middle:])
            candidate_middle = max(1, len(candidates) // 2)
            candidate_halves = (candidates[:candidate_middle], candidates[candidate_middle:])
            nodes, finalists = [], []
            for index, block in enumerate(halves):
                selected = candidate_halves[index]
                if not block and not selected:
                    continue
                result = await self.summary(prompt, block, tokens=max(128, tokens // 2), chars=chars,
                    candidates=selected, k=min(k, len(selected)), extra=extra)
                nodes.append({"id": "derived:" + str(index), "summary": result["summary"],
                    "derived": True, "source_set_hash": identity([m["id"] for m in block])})
                ids = set(result.get("selected_ids", []))
                finalists.extend(c for c in selected if c["id"] in ids)
            try:
                value = await self.call(prompt, dict(payload, materials=nodes, candidates=finalists), schema,
                    max(256, tokens + 512 + k * 90))
            except InputTooLarge:
                # A large K may leave many finalists. Compare bounded sketches,
                # while selected output still references untouched original bodies.
                sketches = []
                for candidate in finalists:
                    sketch = await self.summary("shared_memory_digest", [candidate], tokens=64)
                    sketches.append({"id": candidate["id"], "summary": sketch["summary"]})
                value = await self.call(prompt, dict(payload, materials=nodes, candidates=sketches), schema,
                    max(256, tokens + 512 + k * 90))
        if chars is not None and len(value["summary"]) > chars:
            raise ValueError("summary_character_limit")
        if await self.count_text(value["summary"]) > tokens:
            raise ValueError("summary_token_limit")
        if not value["summary"].strip():
            raise ValueError("empty_summary")
        if k:
            selected = value["selected_ids"]
            if len(selected) != k or len(set(selected)) != k or not set(selected) <= {c["id"] for c in candidates}:
                raise ValueError("invalid_selected_memory_ids")
        return value

    @staticmethod
    def material(row):
        return {"id": row["id"], "summary": row["body"], "kind": row["kind"],
                "start": row["start"], "end": row["end"], "derived": bool(row["derived"])}

    async def run(self, *, bot, character, module, operation, inputs, commit, generate, day="", suffix=""):
        job = self.service.begin_job(bot=bot, character=character, module=module, operation=operation,
            day=day, inputs=inputs, suffix=suffix)
        if not job:
            return False
        self.job = job
        try:
            value = await generate()
            return self.service.commit_job(job, lambda db: commit(db, value))
        except BaseException as exc:
            self.service.fail_job(job, exc)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            return False
        finally:
            self.job = None

    async def process_idle(self, *, character, force=False):
        s, now = self.service, self.service.clock()
        with s.connection() as db:
            events = [dict(r) for r in db.execute("SELECT * FROM sm_events WHERE character=? AND module='qq' AND status='pending' ORDER BY seq", (character,))]
        groups = {}
        for row in events:
            groups.setdefault((row["bot"], row["scope"], row["day"], row["retention"]), []).append(row)
        complete = True
        for (bot, scope, day, retention), rows in groups.items():
            if not force and len(rows) < s.config["checkpoint_messages"] and now - rows[0]["created"] < s.config["checkpoint_minutes"] * 60 and day == s.day(now):
                continue
            if not await self.correct_existing(bot, character, scope, rows):
                complete = False
                continue
            complete = bool(await self.session(bot, character, scope, day, rows, retention)) and complete
        await self.process_specials(character)
        return complete

    async def correct_existing(self, bot, character, scope, rows):
        """An independent governance path; neither new facts nor character liking."""
        s = self.service
        users = {r["id"]: r for r in rows if r["role"] == "user"}
        if not users:
            return True
        subjects = {r["subject"] for r in users.values()}
        with s.connection() as db:
            existing = [dict(r, legacy=False) for r in db.execute("SELECT id,subject,body,version FROM sm_records WHERE bot=? AND character=? "
                "AND scope=? AND kind='qq_explicit_liked' AND long=1 AND status='active'", (bot, character, scope)) if r["subject"] in subjects]
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_items'").fetchone():
                existing += [dict(id=r["id"], subject=r["subject_key"], body=r["summary"], version=r["updated_at"], legacy=True)
                    for r in db.execute("SELECT id,subject_key,summary,updated_at FROM memory_items WHERE owner_key=? AND character_id=? "
                        "AND source_type='qq_message' AND deleted_at IS NULL", (scope, character)) if r["subject_key"] in subjects]
        if not existing:
            return True
        known = {r["id"]: r for r in existing}
        async def generate():
            corrections = {}
            async def examine(events, entries):
                try:
                    value = await self.call("shared_qq_correction", {"events": events, "existing": entries}, CORRECTION, 1024)
                except InputTooLarge:
                    if len(events) > 1:
                        middle = len(events)//2
                        await examine(events[:middle], entries)
                        await examine(events[middle:], entries)
                    elif len(entries) > 1:
                        middle = len(entries)//2
                        await examine(events, entries[:middle])
                        await examine(events, entries[middle:])
                    elif len(events[0]["text"]) > 512:
                        item, text = events[0], events[0]["text"]
                        middle = len(text)//2
                        await examine([{**item, "text": text[:middle+64], "partial_source": True}], entries)
                        await examine([{**item, "text": text[middle-64:], "partial_source": True}], entries)
                    else:
                        raise
                    return
                for candidate in value["corrections"]:
                    source, old = users.get(candidate["user_event"]), known.get(candidate["existing_id"])
                    evidence = next((e for e in events if e["id"] == candidate["user_event"]), None)
                    if not source or not old or old not in entries or not evidence or source["subject"] != old["subject"] or not candidate["quote"].strip() or candidate["quote"] not in evidence["text"]:
                        raise ValueError("unsupported_correction")
                    if len(candidate["replacement"]) > s.config["qq_chars"] or await self.count_text(candidate["replacement"]) > s.config["qq_tokens"]:
                        raise ValueError("correction_too_long")
                    review = await self.call("shared_qq_correction_review", {"candidates": [candidate], "existing": [old],
                        "events": [evidence]}, CORRECTION_REVIEW, 512)
                    if not set(review["approved_ids"]) <= {old["id"]}:
                        raise ValueError("invalid_correction_review")
                    if old["id"] in review["approved_ids"]:
                        previous = corrections.get(old["id"])
                        if previous and previous["user_event"] == candidate["user_event"] and previous["replacement"] != candidate["replacement"]:
                            raise ValueError("ambiguous_correction")
                        if not previous or users[previous["user_event"]]["seq"] <= source["seq"]:
                            corrections[old["id"]] = candidate
            # Cover every real input and existing entry, including long sessions.
            for offset in range(0, len(existing), 16):
                await examine([dict(id=r["id"], subject=r["subject"], text=json.loads(r["payload"])["text"]) for r in users.values()], existing[offset:offset+16])
            return list(corrections.values())
        def commit(db, corrections):
            for value in corrections:
                old = known[value["existing_id"]]
                if old["legacy"]:
                    current = db.execute("SELECT summary,updated_at FROM memory_items WHERE id=? AND deleted_at IS NULL", (old["id"],)).fetchone()
                    if not current or current[0] != old["body"] or current[1] != old["version"]:
                        raise MemoryConflict("legacy_correction_changed")
                    s.govern_legacy(old["id"], character=character, body=value["replacement"], db=db)
                else:
                    s.correct_record(old["id"], value["replacement"], old["version"], character=character, db=db)
            return "checked"
        result = await self.run(bot=bot, character=character, module="qq", operation="correction",
            suffix=identity([r["id"] for r in rows]), inputs=rows, generate=generate, commit=commit)
        # A committed governance pass is idempotent even if summarization subsequently retries.
        if not result:
            with s.connection() as db:
                result = bool(db.execute("SELECT 1 FROM sm_jobs WHERE id=? AND state='done'", (identity("job", bot, character, "qq", "correction", "", identity([r["id"] for r in rows])),)).fetchone())
        return bool(result)

    async def session(self, bot, character, scope, day, rows, retention):
        s, c = self.service, self.service.config
        source_ids = [r["id"] for r in rows]
        event_key = identity(scope, day, source_ids)
        materials = [dict(id=r["id"], subject=r["subject"], role=r["role"], occurred=r["occurred"],
            summary=json.loads(r["payload"]).get("text", "")) for r in rows]

        async def generate():
            return await self.summary("shared_qq_summary", materials, tokens=c["qq_tokens"], chars=c["qq_chars"])

        def commit(db, result):
            key = s.append_short_term(bot=bot, character=character, module="qq", kind="qq_session_summary",
                event_id=event_key, body=result["summary"], scope=scope, visibility="session", retention=retention,
                occurred=rows[0]["occurred"], start=min(r["occurred"] for r in rows), end=max(r["occurred"] for r in rows),
                parents=source_ids, db=db)
            db.executemany("UPDATE sm_events SET status='done' WHERE id=?", [(i,) for i in source_ids])
            # Persist optional extraction with the summary, including across a crash.
            # Its failures must never roll back the real conversation's summary/ACK.
            # Legacy/imported summaries have no original chat to extract from.
            db.execute("INSERT OR IGNORE INTO sm_consumers SELECT id,'qq_special','pending','',? "
                "FROM sm_records WHERE id=? AND retention='normal'", (s.clock(), key))
            return key
        return await self.run(bot=bot, character=character, module="qq", operation="session", day=day,
            suffix=event_key, inputs=rows, generate=generate, commit=commit)

    async def process_specials(self, character):
        with self.service.connection() as db:
            pending = [dict(r) for r in db.execute("SELECT r.* FROM sm_records r JOIN sm_consumers c ON c.record_id=r.id "
                "WHERE r.character=? AND r.status='active' AND c.state='pending' AND c.consumer='qq_special' "
                "ORDER BY r.created,r.id", (character,))]
        for row in pending:
            await self.extract_special(row)

    async def extract_special(self, session):
        s, c = self.service, self.service.config
        bot, character, scope = session["bot"], session["character"], session["scope"]
        with s.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT s.* FROM sm_edges e JOIN sm_events s ON s.id=e.parent "
                "WHERE e.child=? ORDER BY s.seq", (session["id"],))]
            if session["retention"] != "normal" or not rows or any(
                    r["status"] != "done" or r["retention"] != "normal" or not r["payload"] for r in rows):
                s.ack(db, [session["id"]], "qq_special", skipped="special_sources_unavailable")
                return False
        source_ids = {r["id"] for r in rows}
        materials = [dict(id=r["id"], subject=r["subject"], role=r["role"], occurred=r["occurred"],
            summary=json.loads(r["payload"]).get("text", "")) for r in rows]
        # An actual agreement can arrive just after the previous checkpoint.
        # Prior real messages supply evidence only; they are not summarized again.
        with s.connection() as db:
            prior = [dict(r) for r in db.execute("SELECT * FROM sm_events WHERE bot=? AND character=? AND scope=? "
                "AND module='qq' AND status='done' AND retention='normal' AND payload!='{}' AND seq<? AND occurred>=? "
                "ORDER BY seq DESC LIMIT ?", (bot, character, scope, min(r["seq"] for r in rows),
                    min(r["occurred"] for r in rows)-c["short_term_hours"]*3600, c["checkpoint_messages"]))]
        evidence = [dict(id=r["id"], subject=r["subject"], role=r["role"], occurred=r["occurred"],
            summary=json.loads(r["payload"]).get("text", "")) for r in reversed(prior)] + materials

        async def generate():
            # Full source, never a summary, proves explicit intent AND actual agreement.
            async def extract(block):
                user_ids = [r["id"] for r in block if r["role"] == "user"]
                agreement_ids = [r["id"] for r in block if r["role"] == "assistant"]
                if not user_ids or not agreement_ids:
                    return []
                # Bind structured generation to real, role-correct source IDs.
                # Free strings let models invent IDs and exhaust retries.
                schema = deepcopy(SPECIAL)
                properties = schema["properties"]["entries"]["items"]["properties"]
                properties["user_event"] = {"type": "string", "enum": user_ids}
                properties["agreement_event"] = {"type": "string", "enum": agreement_ids}
                try:
                    return (await self.call("shared_qq_special", {"events": block}, schema, 1536))["entries"]
                except InputTooLarge:
                    if len(block) < 2:
                        raise
                    middle = len(block) // 2
                    return await extract(block[:middle]) + await extract(block[middle:])
            entries = await extract(evidence)
            index = {r["id"]: r for r in evidence}
            valid, rejected = [], []
            for position, e in enumerate(entries):
                user, agreement = index.get(e["user_event"]), index.get(e["agreement_event"])
                if not e["long_term_intent"] or not e["character_likes"]:
                    continue
                reason = None
                if not user or not agreement or user["role"] != "user" or agreement["role"] != "assistant":
                    reason = "special_missing_real_agreement"
                elif not {user["id"], agreement["id"]} & source_ids:
                    continue
                elif agreement["occurred"] < user["occurred"]:
                    reason = "special_agreement_precedes_intent"
                elif not e["intent_quote"].strip() or not e["agreement_quote"].strip() or e["intent_quote"] not in user["summary"] or e["agreement_quote"] not in agreement["summary"]:
                    reason = "special_unsupported_quote"
                elif len(e["summary"]) > c["qq_chars"] or await self.count_text(e["summary"]) > c["qq_tokens"]:
                    reason = "special_summary_too_long"
                if reason:
                    rejected.append({"index": position, "reason": reason})
                else:
                    valid.append((e, user["subject"]))
            return valid, rejected

        def commit(db, result):
            entries, rejected = result
            for e, subject in entries:
                s.append_short_term(bot=bot, character=character, module="qq", kind="qq_explicit_liked",
                    event_id=identity(e["user_event"], e["attribute"]), body=e["summary"], scope=scope,
                    subject=subject, visibility="session", occurred=next(r["occurred"] for r in evidence if r["id"] == e["user_event"]),
                    parents=[e["user_event"], e["agreement_event"]], metadata=e, db=db)
            s.ack(db, [session["id"]], "qq_special")
            return {"accepted": len(entries), "rejected": rejected}
        return await self.run(bot=bot, character=character, module="qq", operation="special", day=session["day"],
            suffix=session["id"], inputs=[session, *prior, *rows], generate=generate, commit=commit)

    async def review_special(self, row):
        s = self.service
        with s.connection() as db:
            old = [dict(r) for r in db.execute("SELECT * FROM sm_records WHERE bot=? AND character=? AND scope=? AND subject=? "
                "AND kind='qq_explicit_liked' AND long=1 AND status='active'", (row["bot"], row["character"], row["scope"], row["subject"]))]
        attribute = json.loads(row["metadata"]).get("attribute", "").strip().casefold()
        matches = [r for r in old if r["hash"] == row["hash"] or (attribute and
            json.loads(r["metadata"]).get("attribute", "").strip().casefold() == attribute)]
        schema = deepcopy(REVIEW)
        schema["properties"]["existing_id"] = {"type": ["string", "null"], "enum": [None, *[r["id"] for r in matches]]}
        async def generate():
            return await self.call("shared_qq_review", {"candidate": self.material(row), "evidence": json.loads(row["metadata"]),
                "current": [self.material(r) for r in matches]}, schema, 512)
        def commit(db, value):
            existing = value["existing_id"]
            if existing and existing not in {r["id"] for r in matches}:
                raise ValueError("invalid_existing_special")
            if not existing and matches:
                # A missing model reference must not multiply an identical request.
                existing = max(matches, key=lambda r: r["created"])["id"]
            accepted = value["supported"] and value["durable"] and value["current"]
            if accepted and row["retention"] == "normal":
                if existing:
                    previous = next(r for r in old if r["id"] == existing)
                    if previous["hash"] != row["hash"] and json.loads(previous["metadata"]).get("attribute", "").strip().casefold() != attribute:
                        raise ValueError("special_cannot_replace_other_attribute")
                    if previous["hash"] == row["hash"]:
                        s.ack(db, [row["id"]], "qq_review", skipped="duplicate_confirmation")
                        return existing
                    s.retire(db, [existing])
                    db.execute("INSERT OR IGNORE INTO sm_edges VALUES(?,?)", (row["id"], existing))
                s.promote_from_short_term(row["id"], policy=s.policies["qq"], db=db)
                s.ack(db, [row["id"]], "qq_review")
            else:
                s.ack(db, [row["id"]], "qq_review", skipped="review_not_durable_or_current")
            return row["id"]
        return await self.run(bot=row["bot"], character=row["character"], module="qq", operation="review",
            suffix=row["id"], inputs=[row, *old], generate=generate, commit=commit)

    async def watch(self, *, bot, character, event_id, material_id, analysis, scope="", shared=False, occurred=None, source_ids=()):
        s, c = self.service, self.service.config
        if not isinstance(analysis, dict) or not analysis.get("summary"):
            raise ValueError("missing_video_evidence")
        with s.connection() as db:
            existing = db.execute("SELECT id FROM sm_records WHERE bot=? AND character=? AND module='video' "
                "AND event_id=? AND kind='video_watch' AND status='active'", (bot, character, event_id)).fetchone()
        if existing:
            return existing[0]
        # Analysis confidence limits assertions, not whether a valid experience is stored.
        with s.connection() as db:
            sources = [dict(db.execute("SELECT * FROM sm_events WHERE id=?", (key,)).fetchone()) for key in source_ids]
        from .shared_memory import RequestContext
        recall = [r for r in s.candidates(RequestContext(character, bot, "video", scope,
            query=analysis["summary"])) if r["retention"] == "normal"][:12]
        # Freeze every optional remembered source along with evidence before model generation.
        dependencies = [*sources, *recall]
        parent_ids = [*source_ids, *[r["id"] for r in recall]]
        async def generate():
            return await self.summary("shared_video_watch", [{"id": event_id, "summary": encoded(analysis)}],
                tokens=c["video_tokens"], chars=c["video_chars"], extra={"activity": "watching_video"})
        def commit(db, value):
            key = s.append_short_term(bot=bot, character=character, module="video", kind="video_watch", event_id=event_id,
                material_id=material_id, body=value["summary"], scope=scope, visibility="shared" if shared else "session",
                occurred=occurred, retention=s.retention(bot, character, scope), parents=parent_ids,
                metadata={"partial": bool(analysis.get("uncovered_intervals") or analysis.get("truncated")),
                    "uncertainty": analysis.get("uncertainty", [])}, db=db)
            db.executemany("UPDATE sm_events SET status='done' WHERE id=?", [(key,) for key in source_ids])
            return key
        return await self.run(bot=bot, character=character, module="video", operation="watch", suffix=event_id,
            inputs=dependencies, generate=generate, commit=commit)

    async def process_videos(self, character):
        with self.service.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM sm_events WHERE character=? AND module='video' AND status='pending' ORDER BY seq LIMIT 16", (character,))]
        for row in rows:
            payload = json.loads(row["payload"])
            await self.watch(bot=row["bot"], character=character, event_id=row["id"], material_id=payload["material_id"],
                analysis=payload["analysis"], scope=row["scope"], shared=row["visibility"] == "shared",
                occurred=row["occurred"], source_ids=[row["id"]])

    def batches(self, bot, character):
        with self.service.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM sm_batches WHERE bot=? AND character=? AND module='video' AND active=1 ORDER BY created,id", (bot, character))]

    @staticmethod
    def unique_videos(rows):
        result = {}
        for row in rows:
            result.setdefault(row["material_id"] or row["event_id"], row)
        return list(result.values())

    def _batch_commit(self, db, *, bot, character, day, kind, rows, summary, selected, generation, replaces=()):
        s = self.service
        stable = identity("batch", bot, character, kind, day if kind == "daily" else generation)
        # Daily revisions retain the batch ID, while content versions have distinct IDs.
        old = db.execute("SELECT * FROM sm_batches WHERE id=?", (stable,)).fetchone()
        if old:
            replaces = list({*replaces, stable})
        parent_ids = [r["id"] for r in rows]
        summary_id = s.append_short_term(bot=bot, character=character, module="video",
            kind="video_daily" if kind == "daily" else "video_rollup", event_id=identity(stable, parent_ids, summary),
            body=summary, visibility="shared", occurred=min(r["occurred"] for r in rows),
            start=min(r["start"] for r in rows), end=max(r["end"] for r in rows), parents=parent_ids,
            derived=True, experience=False, db=db)
        s.promote_from_short_term(summary_id, policy=s.policies["video"], db=db)
        for batch_id in replaces:
            prior = db.execute("SELECT summary_id FROM sm_batches WHERE id=?", (batch_id,)).fetchone()
            if prior and prior[0] != summary_id:
                dependents = s.replace_dependency(db, prior[0], summary_id) if old and batch_id == stable else []
                if kind == "daily" and dependents:
                    old_selected = [r[0] for r in db.execute("SELECT record_id FROM sm_members WHERE batch=?", (batch_id,))]
                    for child in dependents:
                        for key in old_selected:
                            db.execute("DELETE FROM sm_edges WHERE child=? AND parent=?", (child, key))
                        db.executemany("INSERT OR IGNORE INTO sm_edges VALUES(?,?)", [(child, key) for key in selected])
                s.retire(db, [prior[0]])
            db.execute("UPDATE sm_batches SET active=0,retired=? WHERE id=?", (s.clock(), batch_id))
        # Retire unselected specifics only after a successful new summary exists.
        previous_members = []
        for batch_id in replaces:
            previous_members.extend(r[0] for r in db.execute("SELECT record_id FROM sm_members WHERE batch=?", (batch_id,)))
        for key in previous_members:
            if key not in selected:
                db.execute("UPDATE sm_records SET long=0,retired=? WHERE id=?", (s.clock(), key))
        for key in selected:
            # Already-long records can have expired short visibility; retain exact identity/hash.
            db.execute("UPDATE sm_records SET short=1,status='active' WHERE id=? AND status!='withdrawn'", (key,))
            s.promote_from_short_term(key, policy=s.policies["video"], db=db)
        db.execute("INSERT OR REPLACE INTO sm_batches VALUES(?,?,?,'video',?,?,?,?,?,?,NULL)",
            (stable, bot, character, day, kind, summary_id, 1, generation, s.clock()))
        db.execute("DELETE FROM sm_members WHERE batch=?", (stable,))
        for key in selected:
            db.execute("INSERT INTO sm_members SELECT ?,id,hash FROM sm_records WHERE id=?", (stable, key))
        db.execute("UPDATE sm_records SET generation=? WHERE id=?", (generation, summary_id))
        if old and not old["active"]:
            db.execute("UPDATE sm_batches SET active=0,retired=? WHERE id=?", (s.clock(), stable))
            s.retire(db, [summary_id])
            for key in selected:
                db.execute("UPDATE sm_records SET long=0 WHERE id=? AND NOT EXISTS (SELECT 1 FROM sm_members m JOIN sm_batches b "
                    "ON b.id=m.batch WHERE m.record_id=? AND b.active=1)", (key, key))
        return stable

    async def video_day(self, bot, character, day):
        s = self.service
        rows = [r for r in s.rows(bot=bot, character=character, module="video", day=day, kind="video_watch")
                if r["body"] and r["retention"] == "normal" and r["internal_allowed"]]
        if not rows:
            return True
        batch_list = self.batches(bot, character)
        same = next((b for b in batch_list if b["kind"] == "daily" and b["day"] == day), None)
        if len(batch_list) >= 10:
            if not await self.compact_video(bot, character):
                return False
            batch_list = self.batches(bot, character)
        # A late day already inside a rollup cannot be reconstructed from a lossy rollup.
        # Withdraw affected derived content; retain/flag original late material for review.
        with s.connection() as db:
            historical = db.execute("SELECT id,summary_id FROM sm_batches WHERE bot=? AND character=? AND day=? AND kind='daily' AND active=0", (bot, character, day)).fetchone()
            missing = db.execute("SELECT 1 FROM sm_records WHERE bot=? AND character=? AND day=? AND kind='video_watch' AND status='purged'", (bot, character, day)).fetchone()
            if historical and not same and missing:
                s.withdraw(historical["summary_id"], reason="late_day_sources_expired", db=db)
                db.execute("INSERT OR REPLACE INTO sm_days VALUES(?,?,?,'needs_rebuild','',?)", (bot, character, day, s.clock()))
                return False
        unique = self.unique_videos(rows)
        k = min(2, len(unique))
        async def generate():
            return await self.summary("shared_video_daily", [self.material(r) for r in rows],
                candidates=[self.material(r) for r in unique], k=k, tokens=s.config["video_daily_tokens"], extra={"day": day})
        def commit(db, value):
            key = self._batch_commit(db, bot=bot, character=character, day=day, kind="daily", rows=rows,
                summary=value["summary"], selected=value["selected_ids"], generation=0)
            s.ack(db, [r["id"] for r in rows], "video_daily")
            return key
        result = await self.run(bot=bot, character=character, module="video", operation="daily", day=day,
            inputs=rows, generate=generate, commit=commit)
        if not result:
            with s.connection() as db:
                result = bool(db.execute("SELECT 1 FROM sm_jobs WHERE id=? AND state='done' AND snapshot=?",
                    (identity("job", bot, character, "video", "daily", day, ""), encoded([["sm_records", r["id"], r["version"]] for r in rows]))).fetchone())
        if len(self.batches(bot, character)) >= 10:
            await self.compact_video(bot, character)
        await self.repair_derivatives(bot, character)
        return bool(result)

    async def compact_video(self, bot, character, *, reselect=False):
        s = self.service
        batches = self.batches(bot, character)
        if reselect:
            batches = [b for b in batches if b["kind"] == "history"]
        elif len(batches) < 10:
            return True
        else:
            batches = batches[:10]
        if not batches:
            return True
        with s.connection() as db:
            summaries = [dict(db.execute("SELECT * FROM sm_records WHERE id=?", (b["summary_id"],)).fetchone()) for b in batches]
            if any(r["status"] != "active" for r in summaries):
                return False
            candidates = self.unique_videos([dict(r) for b in batches for r in db.execute("SELECT r.* FROM sm_members m JOIN sm_records r ON r.id=m.record_id "
                "WHERE m.batch=? AND r.status='active' AND r.retention='normal'", (b["id"],))])
            generation = s.generation(db, bot, character, "video") + (0 if reselect else 1)
        k = min(2 + 2 * generation, s.config["selected_history_limit"], len(candidates))
        inputs = list({r["id"]: r for r in [*summaries, *candidates]}.values())
        async def generate():
            return await self.summary("shared_video_compact", [self.material(r) for r in summaries],
                candidates=[self.material(r) for r in candidates], k=k, tokens=s.config["video_rollup_tokens"],
                extra={"generation": generation, "same_source_not_new_experience": True})
        def commit(db, value):
            key = self._batch_commit(db, bot=bot, character=character, day=min(b["day"] for b in batches), kind="history",
                rows=inputs, summary=value["summary"], selected=value.get("selected_ids", []), generation=generation,
                replaces=[b["id"] for b in batches])
            if not reselect:
                s.set_generation(db, bot, character, "video", generation)
            return key
        return await self.run(bot=bot, character=character, module="video", operation="reselect" if reselect else "compact",
            suffix=str(generation) + (":" + str(s.config["selected_history_limit"]) if reselect else ""),
            inputs=inputs, generate=generate, commit=commit)

    async def diary_day(self, bot, character, day):
        s = self.service
        materials = [r for r in s.rows(bot=bot, character=character, day=day)
            if r["experience"] and r["internal_allowed"] and r["retention"] == "normal" and r["body"]]
        # Special items share original QQ event parents with session summaries.
        with s.connection() as db:
            roots, rows = set(), []
            for r in sorted(materials, key=lambda r: r["kind"] == "qq_explicit_liked"):
                parents = {p[0] for p in db.execute("SELECT parent FROM sm_edges WHERE child=?", (r["id"],))} or {r["id"]}
                if parents <= roots:
                    continue
                roots.update(parents)
                rows.append(r)
            old = [dict(r) for r in db.execute("SELECT * FROM sm_records WHERE bot=? AND character=? AND kind='diary_daily' AND day=? "
                "AND status NOT IN ('withdrawn','purged') ORDER BY created DESC LIMIT 1", (bot, character, day))]
            expired_sources = db.execute("SELECT 1 FROM sm_records WHERE bot=? AND character=? AND day=? AND experience=1 "
                "AND retention='normal' AND status='purged' LIMIT 1", (bot, character, day)).fetchone()
            if expired_sources and old:
                s.withdraw(old[0]["id"], reason="late_day_sources_expired", db=db)
                db.execute("INSERT OR REPLACE INTO sm_days VALUES(?,?,?,'needs_rebuild','',?)", (bot, character, day, s.clock()))
                return False
        fingerprint = identity([(r["id"], r["version"]) for r in rows])
        if old and json.loads(old[-1]["metadata"]).get("input_version") == fingerprint:
            return await self.public_diary(bot, character, day, [r for r in rows if r["visibility"] == "shared"])
        async def generate():
            return await self.summary("shared_diary_daily", [self.material(r) for r in rows], tokens=s.config["diary_tokens"],
                chars=s.config["diary_chars"], extra={"day": day, "timezone": s.config["timezone"], "empty": not rows})
        def commit(db, value):
            zone = ZoneInfo(s.config["timezone"])
            start = datetime.combine(date.fromisoformat(day), day_time(), zone).timestamp()
            end = datetime.combine(date.fromisoformat(day) + timedelta(days=1), day_time(), zone).timestamp()
            key = s.append_short_term(bot=bot, character=character, module="diary", kind="diary_daily",
                event_id=identity(day, fingerprint), body=value["summary"], visibility="internal", occurred=start,
                start=start, end=end, parents=[r["id"] for r in rows], derived=True, experience=False,
                metadata={"input_version": fingerprint}, db=db)
            for previous in old:
                if previous["id"] != key:
                    s.replace_dependency(db, previous["id"], key)
                    s.retire(db, [previous["id"]])
            s.ack(db, [r["id"] for r in materials], "diary")
            self._promote_diary(db, key, bot, character)
            if old and old[0]["status"] == "superseded":
                s.retire(db, [key])
            return key
        result = await self.run(bot=bot, character=character, module="diary", operation="daily", day=day,
            inputs=rows, generate=generate, commit=commit)
        if result:
            public_ok = await self.public_diary(bot, character, day, [r for r in rows if r["visibility"] == "shared"])
            await self.repair_derivatives(bot, character)
            result = result if public_ok else False
        await self.compact_diary(bot, character)
        return result

    async def public_diary(self, bot, character, day, rows):
        if not rows:
            return True
        s = self.service
        fingerprint = identity([(r["id"], r["version"]) for r in rows])
        with s.connection() as db:
            old = [dict(r) for r in db.execute("SELECT * FROM sm_records WHERE bot=? AND character=? AND day=? "
                "AND kind='diary_public' AND status='active'", (bot, character, day))]
        if any(json.loads(r["metadata"]).get("input_version") == fingerprint for r in old):
            return True
        async def generate():
            result = await self.summary("shared_diary_public", [self.material(r) for r in rows],
                tokens=s.config["diary_tokens"], chars=min(400, s.config["diary_chars"]), extra={"day": day})
            from .qzone_runtime import public_text
            if not public_text(result["summary"], 400):
                raise ValueError("invalid_public_projection")
            return result
        def commit(db, value):
            key = s.append_short_term(bot=bot, character=character, module="diary", kind="diary_public",
                event_id=identity(day, fingerprint), body=value["summary"], visibility="shared",
                occurred=min(r["occurred"] for r in rows), parents=[r["id"] for r in rows],
                derived=True, experience=False, metadata={"input_version": fingerprint}, db=db)
            s.retire(db, [r["id"] for r in old if r["id"] != key])
            # Public candidates are short-lived publication data, not an unbounded second diary archive.
            s.ack(db, [key], "diary_promote", skipped="public_projection_short_only")
            return key
        return await self.run(bot=bot, character=character, module="diary", operation="public", day=day,
            inputs=rows, generate=generate, commit=commit)

    def _promote_diary(self, db, key, bot, character):
        s = self.service
        count = db.execute("SELECT count(*) FROM sm_records WHERE bot=? AND character=? AND module='diary' AND kind!='diary_public' AND long=1 AND status IN ('active','stale')", (bot, character)).fetchone()[0]
        if count < 15:
            s.promote_from_short_term(key, policy=s.policies["diary"], db=db)
            s.ack(db, [key], "diary_promote")

    async def compact_diary(self, bot, character):
        s = self.service
        rows = [r for r in s.rows(bot=bot, character=character, module="diary", long=1) if r["kind"] != "diary_public"]
        if len(rows) < 15:
            return True
        rows = rows[:15]
        with s.connection() as db:
            generation = s.generation(db, bot, character, "diary") + 1
        async def generate():
            return await self.summary("shared_diary_compact", [self.material(r) for r in rows], tokens=s.config["diary_rollup_tokens"],
                extra={"coverage_start": min(r["start"] for r in rows), "coverage_end": max(r["end"] for r in rows), "no_date_cutoff": True})
        def commit(db, value):
            key = s.append_short_term(bot=bot, character=character, module="diary", kind="diary_rollup",
                event_id=identity("rollup", generation, [r["id"] for r in rows]), body=value["summary"], visibility="internal",
                occurred=min(r["occurred"] for r in rows), start=min(r["start"] for r in rows), end=max(r["end"] for r in rows),
                parents=[r["id"] for r in rows], derived=True, experience=False, db=db)
            s.promote_from_short_term(key, policy=s.policies["diary"], db=db)
            s.ack(db, [key], "diary_promote")
            db.execute("UPDATE sm_records SET generation=? WHERE id=?", (generation, key))
            s.set_generation(db, bot, character, "diary", generation)
            s.retire(db, [r["id"] for r in rows])
            return key
        return await self.run(bot=bot, character=character, module="diary", operation="compact", suffix=str(generation),
            inputs=rows, generate=generate, commit=commit)

    async def pending_promotions(self, bot, character):
        s = self.service
        await self.repair_derivatives(bot, character)
        with s.connection() as db:
            pending = [dict(r) for r in db.execute("SELECT r.* FROM sm_records r JOIN sm_consumers c ON c.record_id=r.id "
                "WHERE r.bot=? AND r.character=? AND r.status='active' AND c.state='pending' AND c.consumer='qq_review'", (bot, character))]
        for row in pending:
            await self.review_special(row)
        await self.compact_diary(bot, character)
        for row in s.rows(bot=bot, character=character, module="diary", long=0):
            if row["kind"] == "diary_public":
                continue
            with s.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                self._promote_diary(db, row["id"], bot, character)
            await self.compact_diary(bot, character)
        history = [b for b in self.batches(bot, character) if b["kind"] == "history"]
        with s.connection() as db:
            count = sum(db.execute("SELECT count(*) FROM sm_members WHERE batch=?", (b["id"],)).fetchone()[0] for b in history)
        if count > s.config["selected_history_limit"]:
            await self.compact_video(bot, character, reselect=True)

    async def repair_derivatives(self, bot, character):
        """Rebuild retained derived parents bottom-up; never increment merge generations."""
        s = self.service
        with s.connection() as db:
            stale = [dict(r) for r in db.execute("SELECT * FROM sm_records WHERE bot=? AND character=? AND status='stale' ORDER BY generation,created", (bot, character))]
        for row in stale:
            with s.connection() as db:
                parents = [dict(r) for r in db.execute("SELECT r.* FROM sm_edges e JOIN sm_records r ON r.id=e.parent WHERE e.child=?", (row["id"],))]
            if not row["derived"] or not parents or any(p["status"] in {"purged", "withdrawn"} or not p["body"] for p in parents):
                s.withdraw(row["id"], reason="cannot_rebuild_missing_sources")
                with s.connection() as db:
                    db.execute("INSERT OR REPLACE INTO sm_days VALUES(?,?,?,'needs_rebuild','',?)", (bot, character, row["day"], s.clock()))
                continue
            if any(p["status"] == "stale" for p in parents):
                continue
            video = row["module"] == "video"
            candidates = self.unique_videos([p for p in parents if p["kind"] == "video_watch"]) if video else []
            materials = [p for p in parents if p["kind"] != "video_watch"] if video else parents
            k = min(2 + 2 * row["generation"], s.config["selected_history_limit"], len(candidates)) if video else 0
            prompt = "shared_video_compact" if video else "shared_diary_compact"
            tokens = s.config["video_rollup_tokens" if video else "diary_rollup_tokens"]
            async def generate():
                return await self.summary(prompt, [self.material(p) for p in materials], tokens=tokens,
                    candidates=[self.material(p) for p in candidates], k=k, extra={"revision": True, "no_date_cutoff": True})
            def commit(db, value):
                # Permissions can only stay as strict or become more restrictive during repair.
                if any(p["retention"] != "normal" for p in parents):
                    raise MemoryConflict("parent_retention_changed")
                visibility, scope = row["visibility"], row["scope"]
                restricted = [p for p in parents if p["visibility"] != "shared"]
                scopes = {p["scope"] for p in restricted}
                if restricted:
                    if visibility == "internal" or any(p["visibility"] == "internal" for p in restricted) or len(scopes) != 1:
                        visibility, scope = "internal", ""
                    else:
                        visibility, scope = "session", scopes.pop()
                db.execute("UPDATE sm_records SET body=?,hash=?,version=version+1,status='active',start=?,end=?,visibility=?,scope=? WHERE id=?",
                    (value["summary"], digest(value["summary"]), min(p["start"] for p in parents), max(p["end"] for p in parents), visibility, scope, row["id"]))
                if video:
                    batch = db.execute("SELECT id FROM sm_batches WHERE summary_id=?", (row["id"],)).fetchone()
                    if batch:
                        db.execute("DELETE FROM sm_members WHERE batch=?", (batch[0],))
                        for key in value.get("selected_ids", []):
                            db.execute("INSERT INTO sm_members SELECT ?,id,hash FROM sm_records WHERE id=?", (batch[0], key))
                        db.execute("UPDATE sm_records SET long=CASE WHEN EXISTS (SELECT 1 FROM sm_members m JOIN sm_batches b ON b.id=m.batch "
                            "WHERE m.record_id=sm_records.id AND b.active=1) THEN 1 ELSE 0 END WHERE bot=? AND character=? AND kind='video_watch'", (bot, character))
                return row["id"]
            await self.run(bot=bot, character=character, module=row["module"], operation="repair", suffix=row["id"],
                inputs=[row, *parents], generate=generate, commit=commit)

    async def finalize_day(self, bot, character, day, *, diary=True):
        s = self.service
        with s.connection() as db:
            pending = db.execute("SELECT 1 FROM sm_events WHERE bot=? AND character=? AND day=? AND status='pending' LIMIT 1", (bot, character, day)).fetchone()
            if pending:
                db.execute("INSERT OR REPLACE INTO sm_days VALUES(?,?,?,'upstream_pending','',?)", (bot, character, day, s.clock()))
                return False
        video_ok = await self.video_day(bot, character, day)
        diary_ok = await self.diary_day(bot, character, day) if diary else True
        with s.connection() as db:
            db.execute("INSERT INTO sm_days VALUES(?,?,?,?,?,?) ON CONFLICT(bot,character,day) DO UPDATE SET "
                "state=CASE WHEN sm_days.state='needs_rebuild' THEN sm_days.state ELSE excluded.state END,updated=excluded.updated",
                (bot, character, day, "done" if video_ok and diary_ok else "retry", "", s.clock()))
        return video_ok and diary_ok

    async def catch_up(self, character, *, diary=True, max_days=7):
        s = self.service
        now = datetime.fromtimestamp(s.clock(), ZoneInfo(s.config["timezone"]))
        h, m = map(int, s.config["finalize_time"].split(":"))
        last = now.date() - timedelta(days=1 if (now.hour, now.minute) >= (h, m) else 2)
        with s.connection() as db:
            accounts = db.execute("SELECT bot,min(day) FROM (SELECT bot,day FROM sm_events WHERE character=? UNION ALL "
                "SELECT bot,day FROM sm_records WHERE character=?) GROUP BY bot", (character, character)).fetchall()
        for bot, first in accounts:
            await self.pending_promotions(bot, character)
            day, processed = date.fromisoformat(first), 0
            while day <= last and processed < max_days:
                with s.connection() as db:
                    status = db.execute("SELECT state FROM sm_days WHERE bot=? AND character=? AND day=?", (bot, character, day.isoformat())).fetchone()
                if not status or status[0] not in {"done", "needs_rebuild"}:
                    await self.finalize_day(bot, character, day.isoformat(), diary=diary)
                    processed += 1
                day += timedelta(days=1)
