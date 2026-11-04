"""Candidate errors must not lose conversation summaries or poison long-term memory."""
import asyncio
import json

import pytest

from integrations.qq_astrbot.plugin.memory_tasks import MemoryTasks
from tests.test_shared_memory import setup, run, count


def conversation(service, clock):
    user = service.append_event(bot="9", character="fish", module="qq", event_id="intent",
        scope="qq:9:dm:1", subject="1", payload={"text": "long term request"}, occurred=clock())
    sent = service.append_event(bot="9", character="fish", module="qq", event_id="agreement",
        scope="qq:9:dm:1", subject="9", role="assistant", payload={"text": "actual agreement"}, occurred=clock())
    return dict(user_event=user, agreement_event=sent, intent_quote="long term request",
        agreement_quote="actual agreement", long_term_intent=True, character_likes=True,
        attribute="request", summary="understood request")


@pytest.mark.parametrize("bad", ["quote", "empty_quote", "order", "length"])
def test_rejected_candidate_does_not_drop_summary_or_valid_sibling(setup, bad):
    s, t, clock, model = setup
    valid = conversation(s, clock)
    invalid = dict(valid, attribute="invalid", summary="invalid candidate")
    if bad in {"quote", "empty_quote"}:
        invalid["intent_quote"] = "fabricated quote" if bad == "quote" else ""
        reason = "special_unsupported_quote"
    elif bad == "order":
        invalid["agreement_event"] = s.append_event(bot="9", character="fish", module="qq", event_id="early",
            scope="qq:9:dm:1", subject="9", role="assistant", payload={"text": "actual agreement"}, occurred=clock()-1)
        reason = "special_agreement_precedes_intent"
    else:
        invalid["summary"] = "x" * (s.config["qq_chars"] + 1)
        reason = "special_summary_too_long"
    # Empty strings fail JSON-schema validation, so whitespace tests the semantic guard.
    if bad == "empty_quote":
        invalid["intent_quote"] = " "
    model.special = [invalid, valid]
    assert run(t.process_idle(character="fish", force=True))
    assert len(s.rows(bot="9", character="fish", kind="qq_session_summary")) == 1
    candidates = s.rows(bot="9", character="fish", kind="qq_explicit_liked")
    assert len(candidates) == 1 and candidates[0]["body"] == valid["summary"]
    assert not candidates[0]["long"]
    with s.connection() as db:
        assert db.execute("SELECT count(*) FROM sm_events WHERE status='pending'").fetchone()[0] == 0
        result = json.loads(db.execute("SELECT result FROM sm_jobs WHERE operation='special'").fetchone()[0])
        assert result == {"accepted": 1, "rejected": [{"index": 0, "reason": reason}]}
    calls = len(model.calls)
    assert run(t.process_idle(character="fish", force=True))
    assert len(model.calls) == calls


def test_failed_extraction_survives_restart_and_retention_without_resummarizing(setup):
    s, t, clock, model = setup
    model.special = [conversation(s, clock)]
    model.fail = "shared_qq_special"
    for _ in range(s.config["max_attempts"]):
        assert run(t.process_idle(character="fish", force=True))
        clock.now += 61
    with s.connection() as db:
        job = dict(db.execute("SELECT * FROM sm_jobs WHERE operation='special'").fetchone())
        assert job["attempts"] == s.config["max_attempts"]
        assert db.execute("SELECT count(*) FROM sm_events WHERE status='pending'").fetchone()[0] == 0
    clock.days(8)
    s.cleanup()
    with s.connection() as db:
        assert db.execute("SELECT count(*) FROM sm_events WHERE status='done' AND payload!=''").fetchone()[0] == 2
    s.retry(job["id"])
    model.fail = None
    restarted = MemoryTasks(s, model, count_text=count)
    assert run(restarted.process_idle(character="fish", force=True))
    assert sum(key == "shared_qq_summary" for key, _ in model.calls) == 1
    assert len(s.rows(bot="9", character="fish", kind="qq_explicit_liked")) == 1
    with s.connection() as db:
        assert db.execute("SELECT state FROM sm_consumers WHERE consumer='qq_special'").fetchone()[0] == "done"
    s.cleanup()
    with s.connection() as db:
        assert db.execute("SELECT count(*) FROM sm_events WHERE status='purged'").fetchone()[0] == 2


def test_cancel_after_summary_commit_recovers_optional_work(setup):
    s, t, clock, model = setup
    model.special = [conversation(s, clock)]
    async def interrupted(key, *args):
        if key == "shared_qq_special":
            raise asyncio.CancelledError()
        return await model(key, *args)
    t.model = interrupted
    with pytest.raises(asyncio.CancelledError):
        run(t.process_idle(character="fish", force=True))
    with s.connection() as db:
        assert db.execute("SELECT count(*) FROM sm_events WHERE status='pending'").fetchone()[0] == 0
        assert db.execute("SELECT state FROM sm_consumers WHERE consumer='qq_special'").fetchone()[0] == "pending"
    clock.now += 61
    assert run(MemoryTasks(s, model, count_text=count).process_idle(character="fish", force=True))
    assert sum(key == "shared_qq_summary" for key, _ in model.calls) == 1
    assert len(s.rows(bot="9", character="fish", kind="qq_explicit_liked")) == 1


@pytest.mark.parametrize("existing", ["none", "matching", "unrelated", "other_subject"])
def test_review_schema_only_allows_matching_current_ids(setup, existing):
    s, t, clock, model = setup
    old_id = None
    if existing != "none":
        old_id = s.append_short_term(bot="9", character="fish", module="qq", kind="qq_explicit_liked",
            event_id="old", scope="qq:9:dm:1", subject="2" if existing == "other_subject" else "1",
            body="old request", metadata={"attribute": "unrelated" if existing == "unrelated" else "request"})
        with s.connection() as db:
            s.promote_from_short_term(old_id, policy=s.policies["qq"], db=db)
    key = s.append_short_term(bot="9", character="fish", module="qq", kind="qq_explicit_liked",
        event_id="new", scope="qq:9:dm:1", subject="1", body="new request", metadata={"attribute": "request"})
    candidate = next(r for r in s.rows(bot="9", character="fish") if r["id"] == key)
    async def constrained_model(prompt, payload, schema, tokens):
        expected = [None, old_id] if existing == "matching" else [None]
        assert schema["properties"]["existing_id"]["enum"] == expected
        assert [r["id"] for r in payload["current"]] == expected[1:]
        return dict(supported=True, durable=True, current=True, existing_id=expected[-1], reason="supported")
    t.model = constrained_model
    assert run(t.review_special(candidate))
    current = s.rows(bot="9", character="fish", kind="qq_explicit_liked", long=1)
    assert key in {r["id"] for r in current}
    if old_id:
        assert (old_id in {r["id"] for r in current}) == (existing != "matching")


def test_review_rejects_invented_id_even_if_provider_ignores_schema(setup):
    s, t, clock, model = setup
    key = s.append_short_term(bot="9", character="fish", module="qq", kind="qq_explicit_liked",
        event_id="new", scope="qq:9:dm:1", subject="1", body="new request", metadata={"attribute": "request"})
    async def invalid(*args):
        return dict(supported=True, durable=True, current=True, existing_id="invented", reason="unsupported")
    t.model = invalid
    assert not run(t.review_special(s.rows(bot="9", character="fish")[0]))
    assert not s.rows(bot="9", character="fish", long=1)
    with s.connection() as db:
        assert db.execute("SELECT error FROM sm_jobs WHERE operation='review'").fetchone()[0] == "ValidationError"
        assert db.execute("SELECT state FROM sm_consumers WHERE record_id=? AND consumer='qq_review'", (key,)).fetchone()[0] == "pending"


@pytest.mark.parametrize("message,expected", [
    ("summary_character_limit", "ValueError:summary_character_limit"),
    ("private message contents", "ValueError"),
])
def test_job_errors_keep_safe_diagnostics_without_private_text(setup, message, expected):
    s, t, clock, model = setup
    job = s.begin_job(bot="9", character="fish", module="qq", operation="session")
    s.fail_job(job, ValueError(message))
    with s.connection() as db:
        assert db.execute("SELECT error FROM sm_jobs WHERE id=?", (job["id"],)).fetchone()[0] == expected
