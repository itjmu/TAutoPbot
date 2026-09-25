"""Verify a real backup restores and migrates in isolation. No Telegram calls."""

import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import manage
from app import database
from app.scheduler import RuntimeLock
from config import DB_FILE


def counts(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name: conn.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in names}


def main():
    path = Path(DB_FILE).resolve()
    lock = RuntimeLock(str(path))
    if not lock.acquire():
        raise RuntimeError("Stop the bot before checking restoration")
    try:
        saved = manage.backup(path)
        before = counts(saved)
        with tempfile.TemporaryDirectory(prefix="restore-check-") as folder:
            restored = Path(folder) / "restored.db"
            shutil.copy2(saved, restored)
            manage.check(restored)
            assert counts(restored) == before, "Restored table counts differ"
            try:
                database.connect(str(restored))
                database.init_db()
                database.init_db()
            finally:
                if database.db is not None:
                    database.db.close()
                    database.db = None
            manage.check(restored)
            after = counts(restored)
            assert all(after[name] == value for name, value in before.items()), "Migration changed existing row counts"
            result = {"backup": str(saved), "restoration": "ok", "migration_twice": "ok", "integrity": "ok", "tables_before": len(before), "tables_after": len(after)}
        Path("restore_check_results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    finally:
        lock.release()


if __name__ == "__main__":
    main()
