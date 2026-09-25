"""Explicitly authorized smoke test for @Fiveton only, with a temporary database.

No polling, no broadcast recipients, no DMs, no real payments or prizes.
Every message created by this script is recorded and removed in finally.
"""
import asyncio
import json
import struct
import tempfile
import uuid
import zlib
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile

from config import ADMIN_ID, BOT_TOKEN, DB_FILE
from app import accounts, content, database as db, scheduler, timeutils
from app.features import posts
from services import contests
from services.telegram_rate import TelegramRateLimit

CHAT = -1001136154916
REPORT = Path("live_smoke_results.json")


@asynccontextmanager
async def sandbox():
    with tempfile.TemporaryDirectory(prefix="live-smoke-", dir=".") as folder:
        try:
            yield folder
        finally:
            await db.close_async()
            if db.db is not None:
                db.db.close()
                db.db = None


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    pixels = b"".join(b"\x00" + b"\x30\x90\xc0" * 128 for _ in range(128))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB",128,128,8,2,0,0,0)) + chunk(b"IDAT",zlib.compress(pixels)) + chunk(b"IEND",b"")


async def main():
    report = {"chat_id": CHAT, "run": uuid.uuid4().hex[:8], "checks": [], "created_messages": [], "deleted_messages": [], "errors": []}
    def save():
        REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    def record(name, **details):
        report["checks"].append({"name":name,**details})
        save()
        print(json.dumps(report["checks"][-1],ensure_ascii=False),flush=True)

    class Scope(BaseRequestMiddleware):
        async def __call__(self, make_request, bot, method):
            name = method.__api_method__
            allowed_reads = {"getMe", "getChat", "getChatMember", "getWebhookInfo"}
            allowed_writes = {"sendMessage", "sendPhoto", "sendMediaGroup", "editMessageReplyMarkup", "editMessageText", "editMessageCaption", "deleteMessage"}
            if name not in allowed_reads and (name not in allowed_writes or getattr(method,"chat_id",None)!=CHAT):
                raise RuntimeError("Live smoke scope forbids this request")
            result = await make_request(bot, method)
            if name.startswith("send"):
                for message in result if isinstance(result,list) else [result]:
                    report["created_messages"].append(message.message_id)
                save()
            return result

    lock = scheduler.RuntimeLock(DB_FILE)
    if not lock.acquire():
        raise RuntimeError("Stop the production bot before the isolated live test")
    bot = Bot(BOT_TOKEN)
    bot.session.middleware(Scope())
    bot.session.middleware(TelegramRateLimit())
    try:
        me = await bot.get_me()
        chat = await bot.get_chat(CHAT)
        member = await bot.get_chat_member(CHAT,me.id)
        owner = await bot.get_chat_member(CHAT,ADMIN_ID)
        if member.status not in {"administrator","creator"} or owner.status not in {"administrator","creator"}:
            raise RuntimeError("The bot and configured organizer must be administrators")
        record("telegram_access", chat_type=str(chat.type), bot_status=str(member.status))
        async with sandbox() as folder:
            db.connect(str(Path(folder)/"sandbox.db"))
            db.init_db()
            accounts.ensure_user(owner.user)
            cid = db.execute("INSERT INTO channels(telegram_chat_id,title,chat_type,owner_telegram_id,bot_is_admin,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",(CHAT,chat.title,str(chat.type),ADMIN_ID,timeutils.iso(),timeutils.iso())).lastrowid
            text, entities = content.parse_template_html(
                f"Технический тест {report['run']} — будет удалён.\n"
                '<b>Жирный 😀</b> <i>Курсив</i> <u>Подчёркнутый</u> <s>Зачёркнутый</s>\n'
                '<a href="https://example.com">Ссылка внутри текста</a> <tg-spoiler>Спойлер</tg-spoiler>\n'
                '<blockquote expandable>Сворачиваемая цитата</blockquote>\n'
                '<pre><code class="language-python">print("test")</code></pre>'
            )
            sent = await content.send_content(bot,CHAT,{"content_type":"text","text":text,"entities_json":entities})
            expected = {e["type"] for e in entities}
            actual = {str(e.type) for e in sent.entities or []}
            if not expected.issubset(actual):
                raise AssertionError(f"Missing formatting: {expected-actual}")
            record("rich_text", preserved=sorted(actual), message_id=sent.message_id)
            photo = await bot.send_photo(CHAT,BufferedInputFile(png(),filename="smoke.png"),caption="Технический тест изображения — будет удалён.")
            payload = {"content_type":"photo","file_id":photo.photo[-1].file_id,"text":text,"caption_entities_json":entities}
            pid = content.create_draft(ADMIN_ID,dict(payload, caption_entities_json=json.dumps(entities)),[cid])
            posts.schedule_post(pid,ADMIN_ID,timeutils.now()+timedelta(seconds=4))
            with patch.object(posts,"show_post",new=AsyncMock()):  # DM notification explicitly excluded from this channel-only test
                await scheduler.scheduler_publish(bot)
                if db.one("SELECT status FROM posts WHERE id=?",(pid,))[0]!="scheduled":
                    raise AssertionError("Post published before its scheduled time")
                await asyncio.sleep(4.2)
                await scheduler.scheduler_publish(bot)
                await scheduler.scheduler_publish(bot)
            if db.one("SELECT status FROM posts WHERE id=?",(pid,))[0]!="published":
                raise AssertionError("Scheduled delivery failed")
            if db.one("SELECT COUNT(*) FROM published_messages WHERE post_id=?",(pid,))[0]!=1:
                raise AssertionError("Unexpected scheduled delivery count")
            record("scheduled_photo", status="published", repeated_tick_duplicates=0)
            album = await content.send_content(bot,CHAT,{"content_type":"album","media_json":[payload,payload]})
            if len(album)!=2:
                raise AssertionError("Album delivery incomplete")
            record("album", messages=len(album))
            giveaway = db.execute("INSERT INTO contests(owner_id,channel_id,title,prize_title,post_json,prize_kind,prize_json,starts_at,ends_at,winner_count,created_at) VALUES(?,?,?,?,?,'physical',?,?,?,?,?)",(ADMIN_ID,cid,"ТЕСТ — без призов","Тест",json.dumps({"content_type":"text","text":f"Технический тест конкурса {report['run']}. Не участвуйте: призов нет. Пост будет удалён."}),json.dumps({"value":"No prize"}),timeutils.iso(timeutils.now()-timedelta(seconds=1)),timeutils.iso(timeutils.now()+timedelta(seconds=5)),1,timeutils.iso())).lastrowid
            await contests.process_contest(bot,contests.get(giveaway))
            if contests.get(giveaway)["status"]!="active":
                raise AssertionError("Contest did not publish")
            await asyncio.sleep(5.2)
            await contests.process_contest(bot,contests.get(giveaway))
            await contests.finish(bot,contests.get(giveaway))
            db.execute("UPDATE contest_outbox SET status='skipped' WHERE recipient!=?",(CHAT,))
            await contests.deliver(bot)
            if db.one("SELECT status FROM contest_outbox WHERE kind='results'")[0]!="sent":
                raise AssertionError("Contest result edit failed")
            record("contest", status=contests.get(giveaway)["status"], participants=0, results_edited=True)
            scheduled_mid = db.one("SELECT telegram_message_id FROM published_messages WHERE post_id=?",(pid,))[0]
            db.execute("UPDATE published_messages SET delete_at=? WHERE post_id=?",(timeutils.iso(timeutils.now()-timedelta(seconds=1)),pid))
            await scheduler.scheduler_delete(bot)
            if db.one("SELECT deleted FROM published_messages WHERE post_id=?",(pid,))[0]!=1:
                raise AssertionError("Automatic deletion failed")
            report["deleted_messages"].append(scheduled_mid)
            record("scheduled_delete", message_id=scheduled_mid)
            await db.close_async()
            db.db.close()
            db.db=None
    except Exception as exc:
        report["errors"].append({"type":type(exc).__name__,"message":str(exc).replace(BOT_TOKEN,"[redacted]")[:500]})
        save()
    finally:
        if db.db is not None:
            await db.close_async()
            db.db.close()
            db.db=None
        for mid in reversed(report["created_messages"]):
            if mid in report["deleted_messages"]:
                continue
            try:
                try:
                    await bot.delete_message(CHAT,mid)
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after+1)
                    await bot.delete_message(CHAT,mid)
                report["deleted_messages"].append(mid)
            except Exception as exc:
                report["errors"].append({"cleanup_message_id":mid,"type":type(exc).__name__})
            save()
        await bot.session.close()
        lock.release()
        save()
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)


if __name__=="__main__":
    asyncio.run(main())
