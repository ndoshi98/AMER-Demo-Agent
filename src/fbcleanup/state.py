"""SQLite persistence: last-seen index, quarantine ledger, run history.

All times are integer epoch seconds, passed in explicitly so tests need no clock.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Iterable, Optional, Set, Tuple

Key = Tuple[str, str]  # (entity_type, entity_id)
DAY = 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, started_at INTEGER NOT NULL, mode TEXT NOT NULL, executed INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS last_seen(actor TEXT PRIMARY KEY, last_ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS quarantine(
    entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    status TEXT NOT NULL,               -- 'flagged' | 'acted'
    first_flagged_at INTEGER NOT NULL, last_flagged_at INTEGER NOT NULL,
    consecutive_runs INTEGER NOT NULL, last_run_id TEXT NOT NULL, acted_at INTEGER,
    PRIMARY KEY(entity_type, entity_id));
"""


class State:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- runs --------------------------------------------------------------------------------
    def start_run(self, run_id: str, now: int, mode: str, executed: bool) -> None:
        self.db.execute("INSERT INTO runs VALUES (?,?,?,?)", (run_id, now, mode, int(executed)))
        self.db.commit()

    # -- last-seen index (outlives the API's own 1-year audit window) ------------------------
    def record_activity(self, actor: str, ts: int) -> None:
        """Keeps the MAX timestamp regardless of the order events arrive in."""
        self.db.execute(
            "INSERT INTO last_seen(actor,last_ts) VALUES(?,?) "
            "ON CONFLICT(actor) DO UPDATE SET last_ts = MAX(last_ts, excluded.last_ts)",
            (actor.lower(), int(ts)))
        self.db.commit()

    def ingest_audit(self, events: Iterable[Dict], activity_events: Iterable[str] = ()) -> int:
        """Fold normalized audit events into the last-seen index (keyed by user id and by user name/email).

        `activity_events` empty => any audited action counts as activity. Returns events ingested.
        """
        wanted = {e.lower() for e in activity_events}
        n = 0
        for e in events:
            if e.get("ts") is None or (wanted and str(e.get("event", "")).lower() not in wanted):
                continue
            for actor in (e.get("user_id"), e.get("user")):
                if actor:
                    self.record_activity(actor, e["ts"])
            n += 1
        return n

    def last_seen(self, actor: str) -> Optional[int]:
        row = self.db.execute("SELECT last_ts FROM last_seen WHERE actor=?", (actor.lower(),)).fetchone()
        return row["last_ts"] if row else None

    def update_audit_coverage(self, now: int, lookback_days: int) -> int:
        """Return the earliest time from which the last-seen index is complete.

        First run (or a gap longer than the API window) can only see `lookback_days` back; later runs
        extend continuous coverage. Absence of activity only counts as inactivity inside coverage.
        """
        last_sync = self._meta("audit_last_sync")
        start = self._meta("audit_coverage_start")
        window = lookback_days * DAY
        if last_sync is None or start is None or now - int(last_sync) > window:
            start = now - window
        self._set_meta("audit_coverage_start", str(int(start)))
        self._set_meta("audit_last_sync", str(now))
        return int(start)

    def _meta(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.db.commit()

    # -- quarantine ledger -------------------------------------------------------------------
    def observe(self, run_id: str, now: int, flagged: Iterable[Key], evaluated_types: Iterable[str]) -> None:
        """Record one run's view.

        - newly flagged  -> inserted at 1 run
        - still flagged  -> consecutive_runs += 1 (once per run_id)
        - no longer flagged, AND its type was evaluated this run -> released (row deleted, clock restarts)
        - types NOT evaluated this run (e.g. collection failed) are left untouched, so an API blip
          can neither advance nor reset anything
        - acted entities are never touched again
        """
        flagged_set: Set[Key] = set(flagged)
        evaluated = set(evaluated_types)
        for etype, eid in flagged_set:
            if etype not in evaluated:
                continue
            row = self._row((etype, eid))
            if row is None:
                self.db.execute("INSERT INTO quarantine VALUES(?,?,?,?,?,?,?,NULL)",
                                (etype, eid, "flagged", now, now, 1, run_id))
            elif row["status"] == "flagged" and row["last_run_id"] != run_id:
                self.db.execute(
                    "UPDATE quarantine SET consecutive_runs=consecutive_runs+1, last_flagged_at=?, last_run_id=? "
                    "WHERE entity_type=? AND entity_id=?", (now, run_id, etype, eid))
        for row in self.db.execute("SELECT entity_type, entity_id FROM quarantine WHERE status='flagged'").fetchall():
            key = (row["entity_type"], row["entity_id"])
            if key[0] in evaluated and key not in flagged_set:
                self.db.execute("DELETE FROM quarantine WHERE entity_type=? AND entity_id=?", key)
        self.db.commit()

    def _row(self, key: Key):
        return self.db.execute("SELECT * FROM quarantine WHERE entity_type=? AND entity_id=?", key).fetchone()

    def progress(self, key: Key) -> Optional[Dict[str, int]]:
        row = self._row(key)
        if row is None:
            return None
        return {"status": row["status"], "runs": row["consecutive_runs"],
                "first_flagged_at": row["first_flagged_at"], "last_flagged_at": row["last_flagged_at"]}

    def eligible(self, key: Key, now: int, min_runs: int, min_days: int) -> bool:
        """Both thresholds required; min_runs is floored at 2 so a first sighting is never eligible."""
        row = self._row(key)
        if row is None or row["status"] != "flagged":
            return False
        return (row["consecutive_runs"] >= max(2, min_runs)
                and now - row["first_flagged_at"] >= min_days * DAY)

    def mark_acted(self, key: Key, now: int) -> None:
        self.db.execute("UPDATE quarantine SET status='acted', acted_at=? WHERE entity_type=? AND entity_id=?",
                        (now, key[0], key[1]))
        self.db.commit()

    def release(self, key: Key) -> None:
        """Forget an entity entirely (e.g. after its action was undone) so it restarts quarantine from zero."""
        self.db.execute("DELETE FROM quarantine WHERE entity_type=? AND entity_id=?", key)
        self.db.commit()

    def is_acted(self, key: Key) -> bool:
        row = self._row(key)
        return bool(row and row["status"] == "acted")
