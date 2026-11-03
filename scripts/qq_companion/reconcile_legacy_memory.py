"""Apply an explicitly reviewed legacy classification, with no inference or transport.

The review contains decisions and short summaries, never grants new audiences.
Video prose is extracted from existing per-video role reviews, not regenerated
from objective analyses. Default mode validates and reports without writing.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import date, datetime, time as day_time, timedelta
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from integrations.qq_astrbot.plugin.shared_memory import SharedMemory, encoded, digest
from integrations.qq_astrbot.plugin.memory_repository import identity, owner_bot
from integrations.qq_astrbot.plugin.memory_tasks import MemoryTasks
from integrations.qq_astrbot.plugin.memory_policies import register_builtin_modules
from integrations.qq_astrbot.plugin.diary_jobs import diary_window

RECEIPT = "legacy_reconciliation_v1"


def read_db(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def snapshot(db):
    return digest(encoded({table: [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("memory_items", "diary_entries", "memory_evidence", "memory_links", "memory_inbox")}))


class Tokenizer:
    def __init__(self):
        active = json.loads((ROOT / "model_store/active/qwen3.json").read_text(encoding="utf-8-sig"))
        self.model = ROOT / active["runtime"]["model_file"]
        self.cache = {}

    def __call__(self, body):
        if body not in self.cache:
            result = subprocess.run([str(ROOT / "tools/llama.cpp/llama-tokenize.exe"), "-m", str(self.model),
                "--stdin", "--ids", "--no-bos", "--no-parse-special", "--offline"], input=body, text=True,
                encoding="utf-8", capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise ValueError("local_tokenizer_failed")
            values = ast.literal_eval(result.stdout.strip())
            if not isinstance(values, list) or not all(type(v) is int for v in values):
                raise ValueError("invalid_tokenizer_result")
            self.cache[body] = len(values)
        return self.cache[body]


def build(database, video_database, review, count):
    with read_db(database) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='sm_meta'").fetchone():
            raise ValueError("shared_schema_required_run_migrate_shared_memory_first")
        fingerprint = snapshot(db)
        if fingerprint != review["source_snapshot"]:
            raise ValueError("legacy_changed_since_review")
        all_rows = [dict(r) for r in db.execute("SELECT * FROM memory_items ORDER BY created_at,id")]
        active = [r for r in all_rows if r["deleted_at"] is None]
        diaries = {r["id"]: dict(r) for r in db.execute("SELECT * FROM diary_entries")}
        evidence = [dict(r) for r in db.execute("SELECT * FROM memory_evidence")]
        links = [tuple(r) for r in db.execute("SELECT child_id,parent_id FROM memory_links")]
        config_row = db.execute("SELECT value FROM sm_meta WHERE key='config'").fetchone() if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sm_meta'").fetchone() else None
    from integrations.qq_astrbot.plugin.shared_memory_config import defaults
    config = {**defaults(), **(json.loads(config_row[0]) if config_row else {})}
    zone = ZoneInfo(config["timezone"])
    records, sources, mapped, dropped, checks = [], [], {}, [], []
    expected = dict(Counter(r["source_type"] for r in active))
    if expected != review["expected_active"]:
        raise ValueError("unexpected_legacy_categories")

    def add(row, module, kind, body, occurred, *, key, parents=(), **extra):
        cap = "video" if kind == "video_watch" else "qq" if module == "qq" else "diary"
        if not isinstance(body, str) or not body.strip() or len(body) > config[cap + "_chars"] or count(body) > config[cap + "_tokens"]:
            raise ValueError("import_body_limit:" + kind)
        record = dict(bot=row["owner_key"].split(":")[1], character=row["character_id"], module=module, kind=kind,
            event_id=key, body=body, occurred=occurred, parents=list(parents),
            metadata={"migration": RECEIPT, "body_tokens": count(body)}, **extra)
        record["id"] = identity("memory", record["bot"], record["character"], module, key, kind)
        records.append(record)
        return record

    with read_db(video_database) as video_db:
        for row in (r for r in active if r["source_type"] == "douyin"):
            match = re.search(r"/video/(\d+)", row["source_ref"] or "")
            video_id = match[1] if match else None
            decision = review["video_reviews"].get(video_id)
            if not decision:
                dropped.append(row["id"])
                continue
            values = video_db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchall()
            if len(values) != 1:
                raise ValueError("video_identity_ambiguous")
            video = dict(values[0])
            if digest(video["interest"] or "") != decision["interest_hash"]:
                raise ValueError("video_review_changed")
            interest = json.loads(video["interest"])
            excerpts = []
            for field in ("reasons", "uncertainty"):
                for index in decision.get(field, []):
                    text = interest[field][index].strip()
                    # Remove only presentation markup, retaining the recorded wording.
                    text = re.sub(r"\$\\+text\{(.*?)\}\$", r"\1", text)
                    excerpts.append(text)
            if not decision.get("reasons"):
                raise ValueError("role_review_required")
            analysis = json.loads(video["analysis"] or "{}")
            partial = "【仅视频前180秒】" if analysis.get("summary", "").startswith("【仅视频前180秒】") else ""
            body = partial + "\n".join(excerpts)
            watched = video_db.execute("SELECT min(created) FROM views WHERE account=? AND video=? AND created<=?",
                (video["account"], video_id, row["created_at"])).fetchone()[0]
            if watched is None:
                raise ValueError("verified_watch_date_required")
            bot, char = row["owner_key"].split(":")[1], row["character_id"]
            scope = "" if row["visibility"] == "shared" else row["owner_key"]
            event = identity("event", bot, char, "video", scope, "douyin:" + video["account"] + ":" + video_id)
            sources.append(dict(id=event,bot=bot,character=char,module="video",scope=scope,subject="character",
                occurred=watched,visibility="shared" if not scope else "session",payload={"legacy_id":row["id"],
                    "material_id":"douyin:"+video_id,"review_hash":decision["interest_hash"],"review_source":"douyin.videos.interest"}))
            r = add(row,"video","video_watch",body,watched,key=event,parents=[event],scope=scope,
                visibility="shared" if not scope else "session",material_id="douyin:"+video_id)
            r["metadata"].update(legacy_ids=[row["id"]],role_source="existing_video_interest",review_hash=decision["interest_hash"])
            mapped[row["id"]] = r["id"]
            checks.append([video_id,video["account"],digest(video["interest"]),watched,digest(video["analysis"] or "")])

    qq = {r["id"]:r for r in active if r["source_type"] == "qq_message"}
    for group in review["qq_sessions"]:
        rows = [qq[key] for key in group["legacy_ids"]]
        if len({(r["owner_key"],r["character_id"]) for r in rows}) != 1:
            raise ValueError("cross_scope_session")
        parents, times = [], []
        for row in rows:
            proofs = [e for e in evidence if e["memory_id"] == row["id"] and e["evidence"].strip()]
            proof = max(proofs,key=lambda e:e["created_at"]) if proofs else None
            occurred = proof["created_at"] if proof else row["created_at"]
            if datetime.fromtimestamp(occurred,zone).date().isoformat() != group["day"]:
                raise ValueError("qq_event_day_mismatch")
            event = identity("event",owner_bot(row["owner_key"]),row["character_id"],"qq",row["owner_key"],"legacy:"+row["id"])
            sources.append(dict(id=event,bot=owner_bot(row["owner_key"]),character=row["character_id"],module="qq",
                scope=row["owner_key"],subject=row["subject_key"],occurred=occurred,visibility="session",
                payload={"text":proof["evidence"] if proof else row["summary"],"legacy_id":row["id"],
                    "source_type":"original_quote" if proof else "legacy_summary_only"}))
            parents.append(event);times.append(occurred)
        r=add(rows[0],"qq","qq_session_summary",group["body"],min(times),key=identity(RECEIPT,group["legacy_ids"]),
            parents=parents,scope=rows[0]["owner_key"],subject=rows[0]["subject_key"],visibility="session",start=min(times),end=max(times))
        r["metadata"].update(legacy_ids=group["legacy_ids"],historical_partial=True,agreement_evidence=False)
        for row in rows:
            if row["id"] in mapped:raise ValueError("duplicate_classification")
            mapped[row["id"]]=r["id"]
    if set(qq)-mapped.keys():raise ValueError("unclassified_qq_records")
    for row in (r for r in active if r["source_type"] == "processed_summary"):
        targets={mapped[parent] for child,parent in links if child==row["id"] and parent in mapped}
        if not targets:raise ValueError("topic_without_verified_parent")
        mapped[row["id"]]=sorted(targets)
    for row in (r for r in active if r["source_type"] == "diary"):
        old=diaries[row["id"]]
        body=review["legacy_diaries"][old["entry_date"]]
        start,end=diary_window(date.fromisoformat(old["entry_date"]),zone)
        occurred=datetime.combine(date.fromisoformat(old["entry_date"]),day_time(),zone).timestamp()
        r=add(row,"diary","diary_daily",body,occurred,key=identity(RECEIPT,row["id"]),visibility="internal",
            derived=True,experience=False,start=start.timestamp(),end=end.timestamp())
        r["metadata"].update(legacy_ids=[row["id"]],legacy_boundary="23:30-to-23:30",source_support="unverified",
            input_version=identity([]),original_generated_at=old["created_at"])
        mapped[row["id"]]=r["id"]
    if len(mapped)+len(dropped)!=len(active):raise ValueError("unclassified_records")
    replay_blocks = set()
    media_path = Path(video_database).with_name("media_analysis.sqlite3")
    with read_db(video_database) as video_db:
        for row in (r for r in all_rows if r["source_type"] == "douyin" and r["id"] not in mapped):
            bot, char = row["owner_key"].split(":")[1], row["character_id"]
            scope = "" if row["visibility"] == "shared" else row["owner_key"]
            match = re.search(r"/video/(\d+)", row["source_ref"] or "")
            if match and not scope:
                for video in video_db.execute("SELECT account FROM videos WHERE id=?", (match[1],)):
                    replay_blocks.add(identity("event", bot, char, "video", scope, "douyin:"+video[0]+":"+match[1]))
            if scope and media_path.exists():
                with read_db(media_path) as media_db:
                    for job in media_db.execute("SELECT id FROM video_jobs WHERE owner=? AND source=?", (scope,row["source_ref"])):
                        replay_blocks.add(identity("event",bot,char,"video",scope,"qq-video:"+job[0]))
    result=dict(snapshot=fingerprint,config=config,records=records,sources=sources,mapped=mapped,dropped=dropped,video_checks=checks,
        replay_blocks=sorted(replay_blocks),
        all_legacy_ids=[r["id"] for r in all_rows],previously_deleted=len(all_rows)-len(active),review=review)
    validate_days(result,config,count,zone)
    return result


def validate_days(plan,config,count,zone):
    today=datetime.now(zone).date().isoformat()
    records=plan["records"]
    for day,value in plan["review"]["video_days"].items():
        candidates=[r for r in records if r["module"]=="video" and datetime.fromtimestamp(r["occurred"],zone).date().isoformat()==day]
        ids={r["material_id"].split(":",1)[1] for r in candidates}
        chosen=value["selected_video_ids"]
        if day>=today or not ids or len(chosen)!=min(2,len(ids)) or len(set(chosen))!=len(chosen) or not set(chosen)<=ids:
            raise ValueError("invalid_daily_selection")
        if not value["summary"].strip() or count(value["summary"])>config["video_daily_tokens"]:
            raise ValueError("daily_summary_limit")
    for day,body in plan["review"]["diaries"].items():
        if day>=today or not body.strip() or len(body)>config["diary_chars"] or count(body)>config["diary_tokens"]:
            raise ValueError("diary_limit_or_date")


def apply(database,video_database,plan):
    service=SharedMemory(database,register=False)
    register_builtin_modules(service)
    tasks=MemoryTasks(service,None,count_text=None)
    zone=ZoneInfo(service.config["timezone"])
    now=time.time()
    with service.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM sm_meta WHERE key=?",(RECEIPT,)).fetchone():raise ValueError("already_reconciled")
        if snapshot(db)!=plan["snapshot"]:raise ValueError("legacy_changed_before_commit")
        if service.config != plan["config"]:raise ValueError("config_changed_before_commit")
        if db.execute("SELECT 1 FROM sm_jobs WHERE state='running' AND lease>?",(now,)).fetchone():raise ValueError("maintenance_still_running")
        with read_db(video_database) as videos:
            for key,account,hash_value,watched,analysis_hash in plan["video_checks"]:
                current=videos.execute("SELECT interest,analysis FROM videos WHERE id=? AND account=?",(key,account)).fetchone()
                if not current or digest(current[0])!=hash_value or digest(current[1] or "")!=analysis_hash:
                    raise ValueError("video_changed_before_commit")
                if not videos.execute("SELECT 1 FROM views WHERE account=? AND video=? AND created=?",(account,key,watched)).fetchone():
                    raise ValueError("watch_source_changed")
        for source in plan["sources"]:
            if service.retention(source["bot"],source["character"],source["scope"],source["subject"])!="normal":
                raise ValueError("retention_disallows_import")
            if db.execute("SELECT 1 FROM sm_tombstones WHERE id=?",(source["id"],)).fetchone():raise ValueError("source_was_withdrawn")
            service._capacity(db,len(encoded(source["payload"]).encode("utf-8")))
            db.execute("INSERT INTO sm_events(id,bot,character,module,scope,subject,role,payload,occurred,day,created,retention,status,visibility) "
                "VALUES(?,?,?,?,?,?,'source',?,?,?,?,'normal','done',?)",(source["id"],source["bot"],source["character"],source["module"],
                    source["scope"],source["subject"],encoded(source["payload"]),source["occurred"],service.day(source["occurred"]),now,source["visibility"]))
        imported=[]
        for item in plan["records"]:
            record={k:v for k,v in item.items() if k!="id"}
            key=service.append_short_term(**record,db=db)
            imported.append(key)
            if record["module"]=="diary":
                service.promote_from_short_term(key,policy=service.policies["diary"],db=db)
                service.ack(db,[key],"diary_promote")
        pairs={(r["bot"],r["character"]) for r in plan["records"]}
        if len(pairs)!=1:raise ValueError("review_must_target_one_character_and_bot")
        bot,char=next(iter(pairs))
        for day,value in plan["review"]["video_days"].items():
            rows=[dict(r) for r in db.execute("SELECT * FROM sm_records WHERE bot=? AND character=? AND day=? AND kind='video_watch' AND status='active' ORDER BY occurred,id",(bot,char,day))]
            if set(r["id"] for r in rows)-set(imported):raise ValueError("existing_day_requires_revision")
            selected=[r["id"] for r in rows if r["material_id"].split(":",1)[1] in value["selected_video_ids"]]
            tasks._batch_commit(db,bot=bot,character=char,day=day,kind="daily",rows=rows,summary=value["summary"],selected=selected,generation=0)
            service.ack(db,[r["id"] for r in rows],"video_daily")
        for day,body in plan["review"]["diaries"].items():
            rows=[dict(r) for r in db.execute("SELECT * FROM sm_records WHERE bot=? AND character=? AND day=? AND experience=1 AND status='active' ORDER BY occurred,id",(bot,char,day))]
            if set(r["id"] for r in rows)-set(imported):raise ValueError("existing_diary_requires_revision")
            if db.execute("SELECT 1 FROM sm_records WHERE bot=? AND character=? AND day=? AND kind='diary_daily' AND status='active'",(bot,char,day)).fetchone():
                raise ValueError("duplicate_diary_day")
            start=datetime.combine(date.fromisoformat(day),day_time(),zone).timestamp()
            end=datetime.combine(date.fromisoformat(day)+timedelta(days=1),day_time(),zone).timestamp()
            fingerprint=identity([(r["id"],r["version"]) for r in rows])
            key=service.append_short_term(bot=bot,character=char,module="diary",kind="diary_daily",event_id=identity(day,fingerprint),
                body=body,visibility="internal",occurred=start,start=start,end=end,parents=[r["id"] for r in rows],derived=True,experience=False,
                metadata={"migration":RECEIPT,"input_version":fingerprint},db=db)
            service.promote_from_short_term(key,policy=service.policies["diary"],db=db)
            service.ack(db,[key],"diary_promote")
            service.ack(db,[r["id"] for r in rows],"diary")
        for key in plan["replay_blocks"]:
            service.withdraw(key,reason="video_revoked",db=db)
        # Erase old bodies only after all replacements and consumer acknowledgements exist.
        for key in plan["all_legacy_ids"]:
            if db.execute("SELECT 1 FROM memory_items WHERE id=?",(key,)).fetchone():
                service.govern_legacy(key,character=char,db=db)
        db.execute("DELETE FROM memory_links WHERE child_id NOT IN (SELECT id FROM memory_items) OR parent_id NOT IN (SELECT id FROM memory_items)")
        for day in set(plan["review"]["diaries"])|set(plan["review"]["legacy_diaries"]):
            db.execute("INSERT OR REPLACE INTO sm_days VALUES(?,?,?,'done',?,?)",(bot,char,day,RECEIPT,now))
        # Past public projections are not regenerated as newly publishable posts.
        report=dict(at=now,source_snapshot=plan["snapshot"],review_hash=digest(encoded(plan["review"])),
            video_replaced=sum(r["kind"]=="video_watch" for r in plan["records"]),
            video_deleted=len(plan["dropped"]),qq_summaries=sum(r["module"]=="qq" for r in plan["records"]),
            video_batches=len(plan["review"]["video_days"]),diaries=len(plan["review"]["diaries"])+len(plan["review"]["legacy_diaries"]),
            replay_blocks=len(plan["replay_blocks"]),
            previous_deletions_preserved=plan["previously_deleted"],legacy_mapping=plan["mapped"],deleted_ids=plan["dropped"])
        db.execute("INSERT INTO sm_meta VALUES(?,?)",(RECEIPT,encoded(report)))
        db.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
    finish_cleanup(service)
    return report


def finish_cleanup(service):
    """Resumable after a crash between the atomic import and backup cleanup."""
    service.scrub_backups()
    backup = service.path.with_name(service.path.name + ".pre-shared-v1.bak")
    if backup.exists():
        with sqlite3.connect(backup) as db:
            db.execute("PRAGMA secure_delete=ON")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_fts'").fetchone():
                db.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
            db.commit()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
    with service.connection() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("VACUUM")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database",type=Path)
    parser.add_argument("--videos",type=Path,required=True)
    parser.add_argument("--review",type=Path,required=True)
    parser.add_argument("--copy-to",type=Path)
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    with read_db(args.database) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='sm_meta'").fetchone():
            prior=db.execute("SELECT value FROM sm_meta WHERE key=?",(RECEIPT,)).fetchone()
            if prior:
                if args.apply:
                    finish_cleanup(SharedMemory(args.database))
                value=json.loads(prior[0]);print(encoded({k:v for k,v in value.items() if k not in {"legacy_mapping","deleted_ids"}}));return
    review=json.loads(args.review.read_text(encoding="utf-8"))
    plan=build(args.database,args.videos,review,Tokenizer())
    if args.copy_to:
        if args.copy_to.exists() or args.copy_to.resolve()==args.database.resolve():raise ValueError("copy_must_be_new")
        args.copy_to.parent.mkdir(parents=True,exist_ok=True)
        with read_db(args.database) as db,sqlite3.connect(args.copy_to) as out:db.backup(out)
    target=args.copy_to or args.database
    if args.apply or args.copy_to:
        report=apply(target,args.videos,plan)
    else:
        report=dict(dry_run=True,source_snapshot=plan["snapshot"],prepared_records=len(plan["records"]),
            video_replaced=len(plan["video_checks"]),video_deleted=len(plan["dropped"]),previous_deletions_preserved=plan["previously_deleted"])
    print(encoded({k:v for k,v in report.items() if k not in {"legacy_mapping","deleted_ids"}}))


if __name__=="__main__":main()
