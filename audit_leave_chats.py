"""One-time, explicitly requested departure from the audited chat inventory.

No messages are posted, no database data is erased, no polling is started.
"""
import asyncio
import json
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from app.scheduler import RuntimeLock
from config import BOT_TOKEN, DB_FILE


async def main():
    inventory = json.loads(Path("audit_inventory.json").read_text(encoding="utf-8"))
    lock = RuntimeLock(DB_FILE)
    if not lock.acquire():
        raise RuntimeError("Bot is running; refusing maintenance")
    bot = Bot(BOT_TOKEN)
    result = {"bot_id": inventory["bot"]["id"], "results": []}
    saved = Path("audit_leave_results.json")
    if saved.exists():
        previous = json.loads(saved.read_text(encoding="utf-8"))
        if previous["bot_id"] != result["bot_id"]:
            raise RuntimeError("Previous maintenance used a different bot")
        result = previous
    try:
        me = await bot.get_me(request_timeout=20)
        if me.id != result["bot_id"]:
            raise RuntimeError("Bot identity differs from audited inventory")
        for chat_id in inventory["known_chat_ids"]:
            old = next((r for r in result["results"] if r["chat_id"] == chat_id), None)
            if old and (old.get("left") or old.get("error") == "Bad Request: chat not found"):
                continue
            item = {"chat_id": chat_id}
            try:
                item["left"] = await bot.leave_chat(chat_id, request_timeout=20)
            except TelegramRetryAfter as exc:
                item["error"] = "rate_limited"
                item["retry_after"] = exc.retry_after
            except Exception as exc:
                item["error_type"] = type(exc).__name__
                item["error"] = getattr(exc, "message", "Request failed").replace(BOT_TOKEN, "[redacted]")[:300]
            result["results"] = [r for r in result["results"] if r["chat_id"] != chat_id] + [item]
            Path("audit_leave_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(item, ensure_ascii=False), flush=True)
            await asyncio.sleep(0.25)
    finally:
        await bot.session.close()
        lock.release()


if __name__ == "__main__":
    asyncio.run(main())
