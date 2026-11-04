"""Small synchronous SQLite transactions, owned by the asyncio thread, never audio callbacks."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from robot_voice.privacy import sanitize


def now() -> str:
    return datetime.now(UTC).isoformat()


def encode(value) -> str:
    return json.dumps(sanitize(value), ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, provider_id TEXT, start TEXT NOT NULL, end TEXT, timezone TEXT NOT NULL,
 config TEXT NOT NULL, reason TEXT, finalized INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
 at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transcript_fragments (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 at TEXT NOT NULL, speaker TEXT NOT NULL, text TEXT NOT NULL, start_ms INTEGER, end_ms INTEGER,
 provenance TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tool_calls (
 call_id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
 delegation_id TEXT, response_id TEXT, operation_id TEXT NOT NULL, name TEXT NOT NULL,
 arguments TEXT NOT NULL, result TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations (
 id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE, result TEXT);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
 state TEXT NOT NULL, version INTEGER NOT NULL, resource TEXT, name TEXT NOT NULL,
 arguments TEXT NOT NULL, result TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
 announced INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS summaries (
 session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 zone TEXT NOT NULL, day TEXT NOT NULL, source_seq INTEGER NOT NULL,
 data TEXT NOT NULL, model TEXT NOT NULL, created TEXT NOT NULL,
 PRIMARY KEY(session_id,zone,day));
CREATE TABLE IF NOT EXISTS summary_jobs (
 session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS usage (
 id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
 model TEXT NOT NULL, data TEXT NOT NULL, final INTEGER NOT NULL);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        if str(path) != ":memory:":
            self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 1:
            self.db.close()
            raise ValueError("Database version newer than this application")
        self.db.executescript(SCHEMA)

    def rows(self, sql: str, args=()) -> list[dict]:
        return [dict(row) for row in self.db.execute(sql, args)]

    def execute(self, sql: str, args=()):
        with self.db:
            return self.db.execute(sql, args)

    def session(self, config, at: str | None = None) -> str:
        identity = "s_" + uuid4().hex
        self.execute(
            "INSERT INTO sessions(id,start,timezone,config) VALUES(?,?,?,?)",
            (identity, at or now(), config.timezone, encode(config.model_dump(mode="json"))),
        )
        return identity

    def event(self, session: str | None, kind: str, payload: dict, *, identity=None, at=None):
        identity = identity or "e_" + uuid4().hex
        self.execute(
            "INSERT OR IGNORE INTO events(id,session_id,at,kind,payload) VALUES(?,?,?,?,?)",
            (identity, session, at or now(), kind, encode(payload)),
        )
        return identity

    def fragment(
        self,
        session,
        identity,
        speaker,
        text,
        start_ms=None,
        end_ms=None,
        provenance="provider_approximate",
        at=None,
    ):
        self.execute(
            "INSERT OR IGNORE INTO transcript_fragments VALUES(?,?,?,?,?,?,?,?)",
            (identity, session, at or now(), speaker, sanitize(text), start_ms, end_ms, provenance),
        )
        self.event(
            session,
            "transcript",
            {
                "speaker": speaker,
                "text": text,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "provenance": provenance,
            },
            identity=identity,
            at=at,
        )

    def finish(self, session, reason, finalized=False):
        # One transaction: a committed closure must always have its durable summary job.
        with self.db:
            self.db.execute(
                "UPDATE sessions SET end=?,reason=?,finalized=? WHERE id=?",
                (now(), reason, int(finalized), session),
            )
            self.db.execute("INSERT OR IGNORE INTO summary_jobs(session_id) VALUES(?)", (session,))

    def usage(self, identity, session, model, data, final=False):
        self.execute(
            """INSERT INTO usage VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
         data=excluded.data, final=excluded.final WHERE usage.final=0 OR excluded.final=1""",
            (identity, session, model, encode(data), int(final)),
        )

    def recover(self):
        for row in self.rows("SELECT id FROM sessions WHERE end IS NULL"):
            self.finish(row["id"], "process_interrupted", False)
        for row in self.rows(
            "SELECT id,session_id FROM tasks WHERE state IN ('accepted','running','cancelling')"
        ):
            self.execute(
                "UPDATE tasks SET state='interrupted',version=version+1,result=?,updated=? WHERE id=?",
                (encode({"verified": False, "reason": "process_restart"}), now(), row["id"]),
            )
            self.event(row["session_id"], "task.interrupted", {"task_id": row["id"]})
        # Repair databases written before closure/enqueue became atomic.
        self.execute(
            "INSERT OR IGNORE INTO summary_jobs(session_id) SELECT id FROM sessions WHERE end IS NOT NULL"
        )
        self.execute("""UPDATE summary_jobs SET state='failed',error='InterruptedRetryLimit'
            WHERE state IN ('pending','running') AND attempts>=3""")
        self.execute("UPDATE summary_jobs SET state='pending' WHERE state='running' AND attempts<3")

    def export(self, session: str) -> dict:
        result = {}
        for table in (
            "sessions",
            "events",
            "transcript_fragments",
            "tool_calls",
            "tasks",
            "summaries",
            "usage",
        ):
            key = "id" if table == "sessions" else "session_id"
            result[table] = self.rows(f"SELECT * FROM {table} WHERE {key}=?", (session,))
        return sanitize(result)

    def delete(self, session: str):
        active = self.rows(
            "SELECT id FROM tasks WHERE session_id=? AND state IN ('accepted','running','cancelling')",
            (session,),
        )
        if active:
            raise ValueError("Cannot delete a session with active jobs")
        self.execute("DELETE FROM tasks WHERE session_id=?", (session,))
        self.execute("DELETE FROM sessions WHERE id=?", (session,))
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.execute("VACUUM")

    def close(self):
        self.db.close()
