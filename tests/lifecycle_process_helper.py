"""Subprocess helper for lifecycle SQLite crash/concurrency tests."""

from __future__ import annotations

import os
import sqlite3
import sys

from iicp_client.service_lifecycle import LifecycleConflict, SqliteLifecyclePersistence


def main() -> int:
    action, path = sys.argv[1:3]
    store = SqliteLifecyclePersistence(path, max_events=3)
    if action == "complete":
        try:
            store.transition("shared-task", "completed", {"outcome": "completed"})
            return 0
        except LifecycleConflict:
            return 2
    if action == "fail":
        try:
            store.transition("shared-task", "failed", {"reason_code": "worker_failed"})
            return 0
        except LifecycleConflict:
            return 2
    if action == "crash-mid-transition":
        db = sqlite3.connect(path, timeout=5, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT latest_sequence FROM lifecycle_tasks WHERE task_id='shared-task'"
        ).fetchone()
        db.execute(
            "UPDATE lifecycle_tasks SET state='streaming', latest_sequence=? WHERE task_id='shared-task'",
            (int(row[0]) + 1,),
        )
        os._exit(77)
    raise ValueError(action)


if __name__ == "__main__":
    raise SystemExit(main())
