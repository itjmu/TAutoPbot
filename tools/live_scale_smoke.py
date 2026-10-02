"""Explicit live topic smoke test; no polling and no production database access."""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.content import send_content
from app.downloader import ffmpeg_path
from config import BOT_TOKEN
from services.telegram_rate import TelegramRateLimit


async def main(target, topic):
    bot = Bot(BOT_TOKEN)
    bot.session.middleware(TelegramRateLimit())
    created = []
    results = {}
    try:
        me = await bot.me()
        chat = await bot.get_chat(target)
        member = await bot.get_chat_member(chat.id, me.id)
        results.update(
            chat_type=str(chat.type),
            is_forum=bool(chat.is_forum),
            bot_status=str(member.status),
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Test", url="https://telegram.org")]
            ]
        )
        sent = await send_content(
            bot,
            chat.id,
            {
                "content_type": "text",
                "text": "Temporary bot verification: topic posting and editing.",
            },
            markup,
            message_thread_id=topic,
        )
        messages = sent if isinstance(sent, list) else [sent]
        created.extend((chat.id, item.message_id) for item in messages)
        item = messages[0]
        results["topic_delivery"] = item.message_thread_id == topic
        await bot.edit_message_text(
            chat_id=chat.id,
            message_id=item.message_id,
            text="Temporary verification: edit passed.",
            reply_markup=markup,
        )
        results["text_edit"] = True
        await bot.edit_message_reply_markup(
            chat_id=chat.id, message_id=item.message_id, reply_markup=None
        )
        results["clear_buttons"] = True
        with tempfile.TemporaryDirectory(prefix="telegram-smoke-") as folder:
            path = Path(folder) / "round.mp4"
            ffmpeg = await asyncio.create_subprocess_exec(
                ffmpeg_path(),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=240x240:d=1",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(ffmpeg.wait(), 30)
            if ffmpeg.returncode:
                raise RuntimeError("Test clip generation failed")
            note = await send_content(
                bot,
                chat.id,
                {"content_type": "video_note", "file_id": FSInputFile(path)},
                markup,
                message_thread_id=topic,
            )
            created.append((chat.id, note.message_id))
            results["round_video_delivery"] = (
                note.video_note is not None and note.message_thread_id == topic
            )
            changed = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Edited test button", url="https://telegram.org/faq"
                        )
                    ]
                ]
            )
            await bot.edit_message_reply_markup(
                chat_id=chat.id, message_id=note.message_id, reply_markup=changed
            )
            results["round_video_buttons_edit"] = True
            await bot.edit_message_reply_markup(
                chat_id=chat.id, message_id=note.message_id, reply_markup=None
            )
            results["round_video_buttons_clear"] = True
    finally:
        for chat_id, mid in created:
            try:
                await bot.delete_message(chat_id, mid)
                results["test_message_deleted"] = True
            except Exception as exc:
                results["cleanup_error_type"] = type(exc).__name__
        await bot.session.close()
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", required=True)
    parser.add_argument("--topic", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.chat, args.topic))
