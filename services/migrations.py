"""Idempotent migrations for databases from earlier bot releases."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def backup_database(db):
    """SQLite's backup API includes committed WAL content."""
    location = db.execute("PRAGMA database_list").fetchone()[2]
    if not location:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    backup = Path(location).with_name(Path(location).name + "." + stamp + ".bak")
    with sqlite3.connect(backup) as target:
        db.backup(target)


def has_unique_key(db, table, columns):
    for index in db.execute(f"PRAGMA index_list({table})"):
        if index[2] and not index[4]:
            names = [row[2] for row in db.execute(f'PRAGMA index_info("{index[1]}")')]
            if names == list(columns):
                return True
    return False


def migrate_sources(db):
    """Repair legacy schemas, preserving destinations and intake during deduplication."""
    if has_unique_key(
        db, "post_sources", ("owner_telegram_id", "source_chat_id")
    ) and has_unique_key(db, "source_targets", ("source_id", "channel_id")):
        return
    backup_database(db)
    with db:
        db.execute("BEGIN IMMEDIATE")
        duplicates = db.execute("""SELECT owner_telegram_id,source_chat_id,MIN(id)
            FROM post_sources GROUP BY owner_telegram_id,source_chat_id HAVING COUNT(*)>1""").fetchall()
        for owner, chat, keep in duplicates:
            rows = db.execute(
                "SELECT id,active FROM post_sources WHERE owner_telegram_id=? AND source_chat_id=?",
                (owner, chat),
            ).fetchall()
            db.execute(
                "UPDATE post_sources SET active=? WHERE id=?",
                (max(r[1] or 0 for r in rows), keep),
            )
            for old, _ in rows:
                if old == keep:
                    continue
                db.execute(
                    """INSERT INTO source_targets(source_id,channel_id)
                    SELECT ?,channel_id FROM source_targets t WHERE source_id=? AND NOT EXISTS
                    (SELECT 1 FROM source_targets k WHERE k.source_id=? AND k.channel_id=t.channel_id)""",
                    (keep, old, keep),
                )
                db.execute("DELETE FROM source_targets WHERE source_id=?", (old,))
                db.execute(
                    "INSERT OR IGNORE INTO source_seen(source_id,item_key,post_id) SELECT ?,item_key,post_id FROM source_seen WHERE source_id=?",
                    (keep, old),
                )
                db.execute("DELETE FROM source_seen WHERE source_id=?", (old,))
                for album in db.execute(
                    "SELECT owner_id,chat_id,group_id,items_json,targets_json FROM album_intake WHERE source_id=?",
                    (old,),
                ).fetchall():
                    key = tuple(album[:3])
                    existing = db.execute(
                        "SELECT items_json,targets_json FROM album_intake WHERE owner_id=? AND chat_id=? AND group_id=? AND source_id=?",
                        (*key, keep),
                    ).fetchone()
                    if existing:
                        items = json.loads(existing[0] or "[]")
                        for item in json.loads(album[3] or "[]"):
                            if not any(
                                x.get("message_id") == item.get("message_id")
                                for x in items
                            ):
                                items.append(item)
                        targets = sorted(
                            set(
                                json.loads(existing[1] or "[]")
                                + json.loads(album[4] or "[]")
                            )
                        )
                        db.execute(
                            "UPDATE album_intake SET items_json=?,targets_json=? WHERE owner_id=? AND chat_id=? AND group_id=? AND source_id=?",
                            (json.dumps(items), json.dumps(targets), *key, keep),
                        )
                        db.execute(
                            "DELETE FROM album_intake WHERE owner_id=? AND chat_id=? AND group_id=? AND source_id=?",
                            (*key, old),
                        )
                    else:
                        db.execute(
                            "UPDATE album_intake SET source_id=? WHERE owner_id=? AND chat_id=? AND group_id=? AND source_id=?",
                            (keep, *key, old),
                        )
                db.execute("DELETE FROM post_sources WHERE id=?", (old,))
        db.execute(
            "DELETE FROM source_targets WHERE rowid NOT IN (SELECT MIN(rowid) FROM source_targets GROUP BY source_id,channel_id)"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_sources_owner_chat ON post_sources(owner_telegram_id,source_chat_id)"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_source_targets ON source_targets(source_id,channel_id)"
        )


def migrate_answers(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(request_answers)")}
    if "question_id" not in columns:
        return
    # SQLite backup includes WAL contents. Keep the original schema for recovery.
    location = db.execute("PRAGMA database_list").fetchone()[2]
    if location:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        backup = Path(location).with_name(Path(location).name + "." + stamp + ".bak")
        with sqlite3.connect(backup) as target:
            db.backup(target)
    item = "COALESCE(item_id,question_id)" if "item_id" in columns else "question_id"
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE request_answers_migrated(
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL, answer TEXT, is_correct INTEGER DEFAULT 0,
            created_at TEXT NOT NULL)""")
        db.execute(f"""INSERT INTO request_answers_migrated
            SELECT id,request_id,{item},answer,is_correct,created_at FROM request_answers""")
        db.execute("DROP TABLE request_answers")
        db.execute("ALTER TABLE request_answers_migrated RENAME TO request_answers")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_answers_request_item ON request_answers(request_id,item_id,is_correct)"
        )
