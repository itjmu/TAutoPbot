"""Offline database maintenance. Never starts polling or sends Telegram messages."""

import argparse
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app import database
from app.scheduler import RuntimeLock
from config import DB_FILE


def check(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Database integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Database foreign key check failed")


def backup(path):
    folder = path.parent / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = folder / f"{path.stem}-{stamp}.sqlite3"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
    os.chmod(target, 0o600)
    check(target)
    return target


def health(path, max_age=90, *, details=False):
    """Read-only liveness check suitable for an external monitor."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT heartbeat FROM runtime_lock WHERE id=1").fetchone()
        metrics = None
        if details:
            saved = conn.execute(
                "SELECT value FROM app_settings WHERE key='runtime_metrics'"
            ).fetchone()
            metrics = json.loads(saved[0]) if saved else None
    if not row:
        raise RuntimeError("Bot heartbeat is missing")
    heartbeat = datetime.fromisoformat(row[0])
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - heartbeat).total_seconds() > max_age:
        raise RuntimeError("Bot heartbeat is stale")
    return metrics


def reset(path, *, keep_backup=True):
    """Caller must hold the same OS lock as the application."""
    if database.db is not None:
        raise RuntimeError("Close application database before resetting")
    saved = backup(path) if keep_backup and path.exists() else None
    if path.exists():
        connection = sqlite3.connect(path)
        try:
            if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise RuntimeError("Database is busy; stop the bot first")
        finally:
            connection.close()
    # Build and validate a complete fresh database before atomically replacing it.
    fd, temporary = tempfile.mkstemp(prefix=".fresh-", suffix=".db", dir=path.parent)
    os.close(fd)
    fresh = Path(temporary)
    try:
        database.connect(str(fresh))
        database.init_db()
        database.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        database.db.close()
        database.db = None
        check(fresh)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                raise RuntimeError(
                    "Database sidecars remain; close all database clients first"
                )
        os.chmod(fresh, 0o600)
        os.replace(fresh, path)
    finally:
        if database.db is not None:
            database.db.close()
            database.db = None
        fresh.unlink(missing_ok=True)
    return saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("check", "backup", "backup-live", "health", "reset")
    )
    parser.add_argument("--database", default=DB_FILE)
    parser.add_argument("--confirm-reset", action="store_true")
    parser.add_argument(
        "--details",
        action="store_true",
        help="Show runtime queue and latency metrics with health",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Explicitly discard old data on reset without creating a backup",
    )
    args = parser.parse_args()
    path = Path(args.database).expanduser().resolve()
    if args.action == "backup-live":
        # SQLite's backup API snapshots committed WAL data while the writer runs.
        print(backup(path))
        return
    if args.action == "health":
        metrics = health(path, details=args.details)
        if args.details:
            print(json.dumps(metrics, indent=2))
        print("Bot heartbeat: OK")
        return
    if args.action == "reset" and not args.confirm_reset:
        parser.error(
            "Reset deletes all users, posts, giveaways and Premium settings; use --confirm-reset"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = RuntimeLock(str(path))
    if not lock.acquire():
        parser.error("Bot is running. Stop it before maintenance.")
    try:
        if args.action == "reset":
            saved = reset(path, keep_backup=not args.no_backup)
            print(f"Fresh database: {path}\nBackup: {saved or 'not needed'}")
        elif args.action == "backup":
            print(backup(path))
        else:
            check(path)
            print("Database integrity: OK")
    finally:
        lock.release()


if __name__ == "__main__":
    main()
