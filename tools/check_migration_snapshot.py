"""Read-only production snapshot, then migrate ONLY the temporary copy."""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database
from config import DB_FILE


def main():
    path = Path(DB_FILE).expanduser().resolve()
    if not path.is_file():
        print(json.dumps({"database_exists": False}))
        return
    protected = (
        "users",
        "channels",
        "posts",
        "post_targets",
        "published_messages",
        "contests",
        "contest_entries",
        "contest_winners",
        "downloads",
    )
    with tempfile.TemporaryDirectory(prefix="tautopbot-migration-") as folder:
        snapshot = str(Path(folder) / "snapshot.db")
        source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        copy = sqlite3.connect(snapshot)
        try:
            source.backup(copy)
            source_integrity = (
                source.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            )
            conflicts = source.execute(
                "SELECT COUNT(*) FROM (SELECT telegram_chat_id FROM channels WHERE is_active=1 GROUP BY telegram_chat_id HAVING COUNT(DISTINCT owner_telegram_id)>1)"
            ).fetchone()[0]
            available = {
                row[0]
                for row in copy.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            before = {
                table: copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in protected
                if table in available
            }
        finally:
            source.close()
            copy.close()
        try:
            database.connect(snapshot)
            database.init_db()
            after = {
                table: database.one(f"SELECT COUNT(*) FROM {table}")[0]
                for table in before
            }
            result = {
                "production_read_only_integrity": source_integrity,
                "conflicting_active_chat_owners": conflicts,
                "snapshot_migration_integrity": database.one("PRAGMA integrity_check")[
                    0
                ]
                == "ok",
                "protected_table_row_counts_preserved": before == after,
                "protected_tables_checked": len(before),
            }
            # A repeated startup migration must also preserve records.
            database.init_db()
            result["migration_idempotent_row_counts"] = all(
                database.one(f"SELECT COUNT(*) FROM {table}")[0] == count
                for table, count in after.items()
            )
            print(json.dumps(result, indent=2))
            if not all(
                result[key]
                for key in (
                    "production_read_only_integrity",
                    "snapshot_migration_integrity",
                    "protected_table_row_counts_preserved",
                    "migration_idempotent_row_counts",
                )
            ):
                raise RuntimeError("Migration snapshot validation failed")
        finally:
            database.db.close()
            database.db = None


if __name__ == "__main__":
    main()
