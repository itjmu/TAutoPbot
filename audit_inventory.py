"""Read-only launch inventory. Never polls, publishes, leaves chats or resets data."""
import asyncio
import json
import sqlite3
from pathlib import Path

from aiogram import Bot
from config import BOT_TOKEN, DB_FILE


async def main():
    path = Path(DB_FILE).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    ids = set()

    def collect(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in {"chat_id", "source_chat_id", "telegram_chat_id"} and isinstance(value, int) and value < 0:
                    ids.add(value)
                collect(value)
        elif isinstance(obj, list):
            for value in obj:
                collect(value)

    for table in tables:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        for col in cols:
            if col in {"chat_id", "source_chat_id", "telegram_chat_id"}:
                for row in conn.execute(f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" < 0'):
                    ids.add(int(row[0]))
            elif col.endswith("_json") or (table in {"condition_items", "fsm_state"} and col == "data"):
                for row in conn.execute(f'SELECT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL'):
                    try:
                        collect(json.loads(row[0]))
                    except (ValueError, TypeError):
                        pass
    for row in conn.execute("SELECT prize_json FROM contests WHERE prize_kind='invite'"):
        value = json.loads(row[0]).get("value")
        if isinstance(value, int) and value < 0:
            ids.add(value)
    report = {"database": str(path), "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
              "foreign_key_violations": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
              "table_counts": counts, "known_chat_ids": sorted(ids), "chats": []}
    conn.close()
    bot = Bot(BOT_TOKEN)
    try:
        me = await bot.get_me(request_timeout=20)
        report["bot"] = {"id": me.id, "username": me.username}
        hook = await bot.get_webhook_info(request_timeout=20)
        report["webhook"] = {"configured": bool(hook.url), "pending_updates": hook.pending_update_count}
        for chat_id in sorted(ids):
            try:
                member = await bot.get_chat_member(chat_id, me.id, request_timeout=15)
                report["chats"].append({"chat_id": chat_id, "status": str(member.status),
                                       "can_post_messages": getattr(member, "can_post_messages", None)})
            except Exception as exc:
                report["chats"].append({"chat_id": chat_id, "error_type": type(exc).__name__})
    except Exception as exc:
        report["telegram_error_type"] = type(exc).__name__
    finally:
        await bot.session.close()
    Path("audit_inventory.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
