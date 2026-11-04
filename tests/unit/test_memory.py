import json
from datetime import UTC, datetime

import pytest

from robot_voice.memory.daily_context import daily_context, day_bounds, slices
from robot_voice.memory.store import encode
from robot_voice.memory.summaries import Summary, SummaryWorker


@pytest.mark.parametrize(
    "date,hours", [("2026-03-08T12:00:00+00:00", 23), ("2026-11-01T12:00:00+00:00", 25)]
)
def test_dst_calendar_midnight(date, hours):
    start, end = day_bounds(datetime.fromisoformat(date), "America/New_York")
    assert (end - start).total_seconds() == hours * 3600


def test_latest_ten_in_chronological_order(store, config):
    identities = []
    for n in range(12):
        session = store.session(config, at=f"2026-11-04T{n:02}:00:00+00:00")
        identities.append(session)
        store.fragment(session, f"e{n}", "user", str(n), at=f"2026-11-04T{n:02}:00:00+00:00")
    context = daily_context(store, "UTC", datetime(2026, 11, 4, 14, tzinfo=UTC))
    assert [x["session"] for x in context] == identities[-10:]


def test_midnight_and_zone_change_regenerate_from_evidence(store, config):
    session = store.session(config, at="2026-11-03T23:50:00+00:00")
    store.fragment(session, "yesterday", "user", "YESTERDAY ONLY", at="2026-11-03T23:59:00+00:00")
    store.fragment(session, "today", "user", "TODAY ONLY", at="2026-11-04T00:01:00+00:00")
    context = daily_context(store, "UTC", datetime(2026, 11, 4, 1, tzinfo=UTC))
    assert "YESTERDAY" not in encode(context)
    assert "TODAY" in encode(context)
    other = daily_context(store, "America/New_York", datetime(2026, 11, 4, 1, tzinfo=UTC))
    assert "YESTERDAY" in encode(other) and "TODAY" in encode(other)
    assert len(slices(store, session, "UTC")) == 2


def test_summary_stale_fallback_and_budget(store, config):
    session = store.session(config)
    store.fragment(session, "a", "user", "old")
    seq = store.rows("SELECT MAX(seq) AS n FROM events")[0]["n"]
    today = datetime.now(UTC).date().isoformat()
    store.execute(
        "INSERT INTO summaries VALUES(?,?,?,?,?,?,?)",
        (
            session,
            "UTC",
            today,
            seq,
            encode({"topics": ["summary"]}),
            "fake",
            datetime.now(UTC).isoformat(),
        ),
    )
    assert daily_context(store, "UTC")[0]["kind"] == "summary_historical_untrusted"
    store.fragment(session, "b", "user", "new evidence " * 1000)
    context = daily_context(store, "UTC", max_chars=1000)
    assert "extractive" in context[0]["kind"]
    assert len(encode(context)) < 1200


def test_fragments_deduplicated_exact_and_usage_cumulative(store, config):
    session = store.session(config)
    for _ in range(2):
        store.fragment(session, "f1", "assistant", " Hello,", 5, 10)
    assert len(store.rows("SELECT * FROM transcript_fragments")) == 1
    assert store.rows("SELECT text FROM transcript_fragments")[0]["text"] == " Hello,"
    for n in [3, 8, 10]:
        store.usage("provider", session, "live", {"seconds": n}, n == 10)
    store.usage("provider", session, "live", {"seconds": 1}, False)
    assert json.loads(store.rows("SELECT data FROM usage")[0]["data"])["seconds"] == 10


def test_restart_marks_orphans_and_jobs_unknown(store, config):
    session = store.session(config)
    store.execute(
        "INSERT INTO tasks VALUES('task',?,'running',2,'base','navigate','{}',NULL,?,?,0)",
        (session, "a", "b"),
    )
    store.recover()
    assert store.rows("SELECT reason FROM sessions")[0]["reason"] == "process_interrupted"
    assert store.rows("SELECT state FROM tasks")[0]["state"] == "interrupted"
    assert store.rows("SELECT state FROM summary_jobs")[0]["state"] == "pending"


def test_secret_safe_log_export_and_delete(store, config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-abcdefgh")
    session = store.session(config)
    store.event(
        session, "test", {"api_key": "secret", "text": "sk-secret-abcdefgh", "audio": "BASE64"}
    )
    store.fragment(session, "secret", "user", "key sk-secret-abcdefgh")
    dump = encode(store.export(session))
    assert "sk-secret" not in dump and "BASE64" not in dump
    store.finish(session, "test")
    store.delete(session)
    for table in ("sessions", "events", "transcript_fragments", "summary_jobs"):
        assert not store.rows(f"SELECT * FROM {table}")


async def test_summary_retries_capped_and_fallback_available(store, config, monkeypatch):
    session = store.session(config)
    store.fragment(session, "x", "user", "pending request")
    store.finish(session, "test")

    class Failing:
        async def summarize(self, evidence):
            raise RuntimeError("failure")

    async def no_sleep(*args):
        pass

    monkeypatch.setattr("robot_voice.memory.summaries.asyncio.sleep", no_sleep)
    worker = SummaryWorker(store, Failing(), "UTC")
    for _ in range(3):
        await worker.once()
    assert not await worker.once()
    assert store.rows("SELECT state,attempts FROM summary_jobs")[0] == {
        "state": "failed",
        "attempts": 3,
    }
    assert "pending request" in encode(daily_context(store, "UTC"))


async def test_midnight_summaries_are_separate_jobs_slices(store, config):
    session = store.session(config)
    for day in [3, 4]:
        store.fragment(session, f"d{day}", "user", str(day), at=f"2026-11-{day:02}T12:00:00+00:00")
    store.finish(session, "test")

    class Fake:
        async def summarize(self, evidence):
            return Summary(
                topics=["test"],
                user_requests=[],
                confirmed_outcomes=[],
                unfinished_tasks=[],
                relevant_facts=[],
                open_questions=[],
                source_event_ids=[e["id"] for e in evidence],
            )

    await SummaryWorker(store, Fake(), "UTC").once()
    assert len(store.rows("SELECT * FROM summaries")) == 2


def test_summary_compacts_fragments_without_destroying_original_evidence(store, config):
    from robot_voice.memory.summaries import summary_evidence

    session = store.session(config)
    store.fragment(session, "one", "user", "The basket ")
    store.fragment(session, "two", "user", "is in the bedroom.")
    compact = summary_evidence(store.rows("SELECT * FROM events"))
    assert compact[0]["payload"]["text"] == "The basket is in the bedroom."
    assert compact[0]["id"] == "one" and compact[0]["last_source_id"] == "two"
    assert len(store.rows("SELECT * FROM transcript_fragments")) == 2


async def test_summary_cancellation_recovers_durable_job(store, config):
    import asyncio

    session = store.session(config)
    store.fragment(session, "one", "user", "hello")
    store.finish(session, "test")
    entered = asyncio.Event()

    class Blocking:
        async def summarize(self, evidence):
            entered.set()
            await asyncio.Event().wait()

    worker = SummaryWorker(store, Blocking(), "UTC")
    task = asyncio.create_task(worker.once())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.rows("SELECT state FROM summary_jobs")[0]["state"] == "pending"


def test_database_version_rejects_future_schema(tmp_path):
    import sqlite3

    from robot_voice.memory.store import Store

    path = tmp_path / "future.sqlite3"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version=99")
    db.close()
    with pytest.raises(ValueError, match="newer"):
        Store(path)


def test_finish_crash_between_closure_and_enqueue_is_atomic(store, config):
    import subprocess
    import sys

    session = store.session(config)
    code = """
import os,sys
from robot_voice.memory.store import Store
store=Store(sys.argv[1])
def crash_before_enqueue(sql):
    if sql.startswith("INSERT OR IGNORE INTO summary_jobs"):
        os._exit(23)
store.db.set_trace_callback(crash_before_enqueue)
store.finish(sys.argv[2], "normal_close", True)
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(config.database), session], timeout=10, capture_output=True
    )
    assert child.returncode == 23
    assert store.rows("SELECT end FROM sessions WHERE id=?", (session,))[0]["end"] is None
    assert not store.rows("SELECT * FROM summary_jobs")
    store.recover()
    assert (
        store.rows("SELECT reason FROM sessions WHERE id=?", (session,))[0]["reason"]
        == "process_interrupted"
    )
    assert store.rows("SELECT state FROM summary_jobs")[0]["state"] == "pending"


def test_recovery_backfills_preexisting_closed_session_without_summary_job(store, config):
    session = store.session(config)
    store.execute(
        "UPDATE sessions SET end=?,reason='normal_close' WHERE id=?",
        (datetime.now(UTC).isoformat(), session),
    )
    store.recover()
    assert store.rows("SELECT state FROM summary_jobs WHERE session_id=?", (session,)) == [
        {"state": "pending"}
    ]
    assert (
        store.rows("SELECT reason FROM sessions WHERE id=?", (session,))[0]["reason"]
        == "normal_close"
    )


@pytest.mark.parametrize("state", ["running", "pending"])
def test_recovery_marks_interrupted_exhausted_summary_attempts_failed(store, config, state):
    session = store.session(config)
    store.fragment(session, "source", "user", "fallback remains usable")
    store.finish(session, "test")
    store.execute("UPDATE summary_jobs SET state=?,attempts=3 WHERE session_id=?", (state, session))
    store.recover()
    assert store.rows("SELECT state,attempts FROM summary_jobs")[0] == {
        "state": "failed",
        "attempts": 3,
    }
    assert "fallback remains usable" in encode(daily_context(store, "UTC"))


async def test_third_summary_attempt_cancellation_is_exhausted_not_pending(store, config):
    import asyncio

    session = store.session(config)
    store.fragment(session, "source", "user", "unfinished summary")
    store.finish(session, "test")
    store.execute("UPDATE summary_jobs SET attempts=2 WHERE session_id=?", (session,))
    entered = asyncio.Event()

    class Blocking:
        async def summarize(self, evidence):
            entered.set()
            await asyncio.Event().wait()

    worker = SummaryWorker(store, Blocking(), "UTC")
    task = asyncio.create_task(worker.once())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.rows("SELECT state,attempts FROM summary_jobs")[0] == {
        "state": "failed",
        "attempts": 3,
    }
    assert not await worker.once()
    assert "unfinished summary" in encode(daily_context(store, "UTC"))


def test_redaction_covers_dictionary_keys_and_invalid_tool_arguments(store, config, monkeypatch):
    from robot_voice.privacy import sanitize

    secret = "sk-synthetic-test-secret-12345678"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    session = store.session(config)
    payload = {secret: "value", "nested": {"prefix-" + secret: [secret]}, "Authorization": "opaque"}
    store.event(session, "untrusted", payload)
    exported = json.dumps(store.export(session))
    assert secret not in exported
    cleaned = sanitize(payload)
    assert secret not in json.dumps(cleaned)
    assert cleaned["Authorization"] == "[redacted]"
