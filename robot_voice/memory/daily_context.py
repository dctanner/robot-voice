import json
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .store import encode


def day_bounds(instant: datetime, zone: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(zone)
    day = instant.astimezone(tz).date()
    return (
        datetime.combine(day, time.min, tz).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(UTC),
    )


def slices(store, session: str, zone: str) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for row in store.rows(
        "SELECT * FROM events WHERE session_id=? AND (kind='transcript' OR kind LIKE 'task.%' OR kind='tool.result') ORDER BY seq",
        (session,),
    ):
        day = datetime.fromisoformat(row["at"]).astimezone(ZoneInfo(zone)).date().isoformat()
        result.setdefault(day, []).append(row)
    return result


def daily_context(
    store, zone: str, instant: datetime | None = None, limit=10, max_chars=9000
) -> list[dict]:
    instant = instant or datetime.now(UTC)
    start, end = day_bounds(instant, zone)
    day = instant.astimezone(ZoneInfo(zone)).date().isoformat()
    sessions = store.rows(
        """SELECT session_id, MAX(seq) AS latest FROM events
        WHERE at>=? AND at<? AND session_id IS NOT NULL
        AND (kind='transcript' OR kind='tool.result')
        GROUP BY session_id ORDER BY latest DESC LIMIT ?""",
        (start.isoformat(), end.isoformat(), limit),
    )
    selected = []
    remaining = max_chars
    for row in sessions:
        evidence = slices(store, row["session_id"], zone).get(day, [])
        summaries = store.rows(
            "SELECT * FROM summaries WHERE session_id=? AND zone=? AND day=?",
            (row["session_id"], zone, day),
        )
        latest = max((e["seq"] for e in evidence), default=0)
        if summaries and summaries[0]["source_seq"] >= latest:
            entry = {
                "session": row["session_id"],
                "kind": "summary_historical_untrusted",
                "data": json.loads(summaries[0]["data"]),
            }
        else:
            # Regenerates from UTC evidence after timezone changes; no yesterday leakage.
            entry = {
                "session": row["session_id"],
                "kind": "pending_summary_extractive_untrusted",
                "data": [
                    {"id": e["id"], "kind": e["kind"], "payload": json.loads(e["payload"])}
                    for e in evidence[-12:]
                ],
            }
        size = len(encode(entry))
        if size > remaining:
            entry = {
                "session": row["session_id"],
                "kind": "bounded_extractive_untrusted",
                "excerpt": encode(entry)[-max(0, remaining - 160) :],
            }
            size = len(encode(entry))
        if remaining < 200:
            break
        selected.append(entry)
        remaining -= size
    return list(reversed(selected))
