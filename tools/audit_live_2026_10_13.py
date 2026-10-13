"""Authorized isolated live audit; never polls or writes the production DB."""

import asyncio
import json
import sqlite3
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot
from aiogram.types import FSInputFile

from app import accounts, content, scheduler, timeutils
from app import database as db
from app.features import posts
from config import ADMIN_ID, BOT_TOKEN, DB_FILE
from services import audio_files
from services.telegram_rate import TelegramRateLimit
from tools.live_workflow_smoke import SafeLiveSession, main


async def media_checks(chat, topic):
    result = {"mode": "real Telegram media transfer and scheduler; isolated database"}
    bot = Bot(BOT_TOKEN)
    scope = SafeLiveSession(ADMIN_ID, chat)
    bot.session.middleware(scope)
    bot.session.middleware(TelegramRateLimit())
    try:
        with tempfile.TemporaryDirectory(prefix="audit-media-") as folder:
            root = Path(folder)
            db.connect(str(root / "isolated.db"))
            db.init_db()
            from aiogram.types import User

            accounts.ensure_user(User(id=ADMIN_ID, is_bot=False, first_name="Audit"))
            cid = db.execute(
                "INSERT INTO channels(telegram_chat_id,title,chat_type,owner_telegram_id,bot_is_admin,created_at,updated_at) VALUES(?, 'Audit', 'supergroup', ?,1,?,?)",
                (chat, ADMIN_ID, timeutils.iso(), timeutils.iso()),
            ).lastrowid
            audio = root / "audit.mp3"
            await audio_files.ffmpeg(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=2",
                    "-c:a",
                    "libmp3lame",
                    str(audio),
                ]
            )
            original = await bot.send_audio(
                ADMIN_ID, FSInputFile(audio), title="Temporary audio audit"
            )
            conversions = {}
            for target in ("mp3", "m4a", "ogg", "flac", "wav"):
                payload = {
                    "content_type": "audio",
                    "file_id": original.audio.file_id,
                    "audio_meta_json": json.dumps(
                        {
                            "format": "mp3",
                            "filename": "audit.mp3",
                            "fast": True,
                            "process": True,
                            "convert_to": target if target != "mp3" else None,
                            "presentation": "voice"
                            if target == "ogg"
                            else "audio"
                            if target in {"mp3", "m4a"}
                            else "document",
                            "changes": {
                                "title": "Audit prepared",
                                "lyrics": "Temporary audit lyrics",
                            }
                            if target in {"mp3", "m4a"}
                            else {},
                        }
                    ),
                }
                started = time.monotonic()
                message = await audio_files.send_prepared(bot, ADMIN_ID, payload, None)
                # The service returns the API Message, not a local-only artifact.
                media = message.audio or message.voice or message.document
                conversions[target] = {
                    "delivered": bool(media),
                    "seconds": round(time.monotonic() - started, 3),
                }
                if not media:
                    raise AssertionError("Prepared audio was not delivered")
                if target in {"mp3", "m4a"}:
                    saved = root / ("returned." + target)
                    await audio_files.download(
                        bot, media.file_id, saved, audio_files.SOURCE_LIMIT
                    )
                    if target == "mp3":
                        from mutagen.id3 import ID3

                        tags = ID3(saved)
                        preserved = (
                            tags.getall("USLT")[0].text == "Temporary audit lyrics"
                        )
                    else:
                        from mutagen.mp4 import MP4

                        preserved = MP4(saved)["\xa9lyr"][0] == "Temporary audit lyrics"
                    conversions[target]["lyrics_after_telegram_roundtrip"] = preserved
                    if not preserved:
                        raise AssertionError("Lyrics missing after Telegram round trip")
            result["audio_formats"] = conversions
            pid = content.create_draft(
                ADMIN_ID,
                {
                    "content_type": "text",
                    "text": "Temporary scheduled audit; will be removed.",
                },
                [cid],
            )
            db.execute(
                "UPDATE posts SET protect_content=1,disable_notification=1 WHERE id=?",
                (pid,),
            )
            db.execute(
                "UPDATE post_targets SET message_thread_id=? WHERE post_id=?",
                (topic, pid),
            )
            posts.schedule_post(pid, ADMIN_ID, timeutils.now() + timedelta(seconds=2))
            with patch.object(posts, "show_post", new=AsyncMock()):
                await scheduler.scheduler_publish(bot)
                if (
                    db.one("SELECT status FROM posts WHERE id=?", (pid,))[0]
                    != "scheduled"
                ):
                    raise AssertionError("Published before scheduled time")
                await asyncio.sleep(2.1)
                await scheduler.scheduler_publish(bot)
                await scheduler.scheduler_publish(bot)
            result["scheduled_exactly_once"] = (
                db.one(
                    "SELECT COUNT(*) FROM published_messages WHERE post_id=?", (pid,)
                )[0]
                == 1
            )
            result["live_copy_protection"] = bool(
                scope.last_group.has_protected_content
            )
            db.execute(
                "UPDATE published_messages SET delete_at=? WHERE post_id=?",
                (timeutils.iso(timeutils.now() - timedelta(seconds=1)), pid),
            )
            await scheduler.scheduler_delete(bot)
            result["live_auto_delete"] = (
                db.one(
                    "SELECT deleted FROM published_messages WHERE post_id=?", (pid,)
                )[0]
                == 1
            )
            result["temporary_db_integrity"] = db.one("PRAGMA integrity_check")[0]
            await db.close_async()
            db.db.close()
            db.db = None
    finally:
        if db.db is not None:
            await db.close_async()
            db.db.close()
            db.db = None
        failures = 0
        for destination, mid in scope.created:
            try:
                await bot.delete_message(destination, mid)
            except Exception as exc:
                if "not found" not in str(exc).lower():
                    failures += 1
        result["cleanup_failures"] = failures
        await bot.session.close()
        print(json.dumps(result, indent=2), flush=True)


async def run():
    connection = sqlite3.connect(
        Path(DB_FILE).resolve().as_uri() + "?mode=ro", uri=True
    )
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        candidates = connection.execute(
            "SELECT telegram_chat_id FROM channels WHERE is_active=1 AND bot_is_admin=1 "
            "ORDER BY is_forum DESC, id"
        ).fetchall()
        if not candidates:
            raise RuntimeError("No registered test destination available")
        chat = candidates[0][0]
        channel = connection.execute(
            "SELECT telegram_chat_id FROM channels WHERE is_active=1 AND bot_is_admin=1 AND chat_type='channel' LIMIT 1"
        ).fetchone()
        topic = connection.execute(
            "SELECT topic_id FROM forum_topics WHERE chat_id=? AND COALESCE(closed,0)=0 LIMIT 1",
            (chat,),
        ).fetchone()
        print(json.dumps({"production_readonly_integrity": integrity}), flush=True)
    finally:
        connection.close()
    if "--channel-only" not in sys.argv:
        await main(chat, topic[0] if topic else None)
        await media_checks(chat, topic[0] if topic else None)
    if channel:
        bot = Bot(BOT_TOKEN)
        scope = SafeLiveSession(ADMIN_ID, channel[0])
        bot.session.middleware(scope)
        bot.session.middleware(TelegramRateLimit())
        result = {}
        try:
            message = await content.send_content(
                bot,
                channel[0],
                {
                    "content_type": "text",
                    "text": "Temporary channel audit; will be removed.",
                    "protect_content": True,
                    "disable_notification": True,
                },
            )
            result["channel_publication_protection"] = bool(
                message.has_protected_content
            )
            await bot.edit_message_text(
                chat_id=channel[0],
                message_id=message.message_id,
                text="Temporary channel audit: edit passed.",
            )
            result["channel_edit"] = True
            with tempfile.TemporaryDirectory(prefix="audit-album-") as folder:
                photo_path = Path(folder) / "photo.jpg"
                await audio_files.ffmpeg(
                    [
                        "-f",
                        "lavfi",
                        "-i",
                        "color=c=blue:s=320x240",
                        "-frames:v",
                        "1",
                        str(photo_path),
                    ]
                )
                photo = await bot.send_photo(
                    channel[0], FSInputFile(photo_path), caption="Temporary image audit"
                )
                album = await content.send_content(
                    bot,
                    channel[0],
                    {
                        "content_type": "album",
                        "media_json": [
                            {
                                "content_type": "photo",
                                "file_id": photo.photo[-1].file_id,
                                "text": "Temporary album audit",
                            },
                            {
                                "content_type": "photo",
                                "file_id": photo.photo[-1].file_id,
                                "text": "Temporary album audit",
                            },
                        ],
                    },
                )
                result["channel_photo"] = bool(photo.photo)
                result["channel_album_two_items"] = len(album) == 2
        finally:
            failures = 0
            for destination, mid in scope.created:
                try:
                    await bot.delete_message(destination, mid)
                except Exception:
                    failures += 1
            result["channel_cleanup_failures"] = failures
            await bot.session.close()
            print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(run())
