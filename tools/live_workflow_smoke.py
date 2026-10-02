"""Real Telegram API + synthetic updates, isolated DB; never starts polling."""

import argparse
import asyncio
import json
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Message, Update, User

from app import accounts, content, storage, timeutils, ui
from app import database as db
from app.features import channels, posts, published_editor
from app.routing import build_router
from config import ADMIN_ID, BOT_TOKEN
from services import contests
from services.event_isolation import EventIsolation
from services.forum_topics import remember
from services.telegram_rate import TelegramRateLimit


class SafeLiveSession:
    def __init__(self, uid, chat):
        self.uid, self.chat = uid, chat
        self.created = set()
        self.last_private = None
        self.last_group = None
        self.errors = []

    async def __call__(self, make_request, bot, method):
        name = method.__api_method__
        if name == "answerCallbackQuery":
            if method.show_alert:
                self.errors.append("Callback returned an alert")
            return True  # these callback IDs are synthetic, never real update IDs
        chat = getattr(method, "chat_id", None)
        if name.startswith(("send", "copy", "forward")) and chat not in {
            self.uid,
            self.chat,
        }:
            raise RuntimeError("Test attempted to send outside authorized destinations")
        if name.startswith(("editMessage", "deleteMessage")):
            key = (chat, getattr(method, "message_id", None))
            if key not in self.created:
                if name == "deleteMessage":
                    return True  # a synthetic incoming user message
                raise RuntimeError("Test attempted to edit an unrelated message")
        result = await make_request(bot, method)
        for message in result if isinstance(result, list) else [result]:
            if isinstance(message, Message):
                self.created.add((message.chat.id, message.message_id))
                if message.chat.id == self.uid:
                    self.last_private = message
                else:
                    self.last_group = message
        return result


async def main(target, topic):
    with tempfile.TemporaryDirectory(prefix="tautopbot-live-") as folder:
        db.connect(str(Path(folder) / "isolated.db"))
        db.init_db()
        bot = Bot(BOT_TOKEN)
        dp = Dispatcher(
            storage=storage.SQLiteStorage(), events_isolation=EventIsolation()
        )
        dp.include_router(build_router())
        results = {
            "mode": "real API with synthetic updates; no polling; isolated database"
        }
        session = None
        try:
            chat = await bot.get_chat(target)
            await bot.me()
            user = User(id=ADMIN_ID, is_bot=False, first_name="Temporary verification")
            session = SafeLiveSession(user.id, chat.id)
            bot.session.middleware(session)
            bot.session.middleware(TelegramRateLimit())
            accounts.ensure_user(user)
            await channels.register_channel(user.id, bot, chat)
            cid = db.one(
                "SELECT id FROM channels WHERE telegram_chat_id=?", (chat.id,)
            )[0]
            remember(chat.id, topic, "Verification topic")
            counter = 0

            async def command(text):
                nonlocal counter
                counter += 1
                message = Message(
                    message_id=900000000 + counter,
                    date=timeutils.now(),
                    chat={"id": user.id, "type": "private"},
                    from_user=user,
                    text=text,
                )
                await dp.feed_update(bot, Update(update_id=counter, message=message))
                return message.as_(bot)

            async def click(data):
                nonlocal counter
                counter += 1
                if not session.last_private:
                    raise RuntimeError("Missing live UI panel")
                query = CallbackQuery(
                    id=f"synthetic_scale_{counter}",
                    from_user=user,
                    chat_instance="scale-verification",
                    message=session.last_private,
                    data=data,
                )
                await dp.feed_update(
                    bot, Update(update_id=counter, callback_query=query)
                )
                if session.errors:
                    raise RuntimeError(session.errors[-1])

            await command("/start")
            for section in (
                "channels",
                "posts",
                "requests",
                "conditions",
                "download",
                "contests",
                "settings",
            ):
                await click(f"menu:{section}")
                await click("menu:main")
            results["private_menus_and_home"] = True
            await click("contest:new")
            await click(f"contest:select:{cid}")
            await click("contest:selected")
            await click("menu:main")
            results["contest_questionnaire_navigation"] = True
            pid = content.create_draft(
                user.id,
                {
                    "content_type": "text",
                    "text": "Temporary workflow verification; this is not a real publication.",
                },
                [cid],
            )
            db.execute(
                "UPDATE post_targets SET message_thread_id=? WHERE post_id=?",
                (topic, pid),
            )
            db.execute(
                "UPDATE posts SET buttons_json=? WHERE id=?",
                (
                    json.dumps(
                        [
                            {
                                "id": "test",
                                "type": "url",
                                "text": "Test",
                                "url": "https://telegram.org",
                                "row": 1,
                            }
                        ]
                    ),
                    pid,
                ),
            )
            await posts.execute_publish(pid, bot)
            delivery = db.one(
                "SELECT status,error FROM post_targets WHERE post_id=?", (pid,)
            )
            if delivery["status"] != "sent":
                diagnostic = re.sub(
                    r"\d{6,}:[A-Za-z0-9_-]+",
                    "[redacted]",
                    delivery["error"] or "No delivery error",
                )
                raise RuntimeError(
                    f"Test publication {delivery['status']}: {diagnostic}"
                )
            published = db.one(
                "SELECT telegram_message_id FROM published_messages WHERE post_id=?",
                (pid,),
            )[0]
            results["durable_topic_publication"] = (
                db.one("SELECT status FROM posts WHERE id=?", (pid,))[0] == "published"
            )
            state = dp.fsm.get_context(bot=bot, chat_id=user.id, user_id=user.id)
            message = await command("/menu")
            await published_editor.original(
                message, state, bot, saved_target=(cid, published)
            )
            data = await state.get_data()
            await click(f"live:clearbuttons:{data['token']}")
            await click(f"live:apply:{data['token']}")
            results["published_editor_clear_and_save"] = (
                session.last_group.reply_markup is None
            )
            # Close a synthetic one-entry contest against our own test message.
            contest_id = db.execute(
                "INSERT INTO contests(owner_id,channel_id,title,post_json,prize_kind,prize_json,starts_at,ends_at,subscriptions_json,status,published_ids,created_at) VALUES(?,?,?,?,?,?,?,?,?,'closing',?,?)",
                (
                    user.id,
                    cid,
                    "Temporary verification — no actual prize",
                    json.dumps({"content_type": "text", "text": "Test"}),
                    "text",
                    json.dumps({"value": "Verification only; no actual prize."}),
                    timeutils.iso(timeutils.now() - timedelta(minutes=1)),
                    timeutils.iso(),
                    json.dumps([{"chat_id": chat.id}]),
                    json.dumps([published]),
                    timeutils.iso(),
                ),
            ).lastrowid
            db.execute(
                "INSERT INTO contest_entries(contest_id,user_id,joined_at,base_valid) VALUES(?,?,?,1)",
                (contest_id, user.id, timeutils.iso()),
            )
            await contests.finish(bot, contests.get(contest_id))
            await contests.deliver(bot)
            results["contest_live_membership_draw_results"] = contests.get(contest_id)[
                "status"
            ] == "completed" and not db.one(
                "SELECT 1 FROM contest_outbox WHERE status!='sent'"
            )
            await command("/menu")
            await ui.close_cleanup()
            results["sqlite_integrity"] = db.one("PRAGMA integrity_check")[0] == "ok"
        finally:
            await ui.close_cleanup()
            if session:
                failures = 0
                for chat_id, mid in session.created:
                    try:
                        await bot.delete_message(chat_id, mid)
                    except Exception as exc:
                        if "not found" not in str(exc).lower():
                            failures += 1
                results["cleanup_failures"] = failures
            await dp.storage.close()
            await bot.session.close()
            db.db.close()
            db.db = None
            print(json.dumps(results, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", required=True)
    parser.add_argument("--topic", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.chat, args.topic))
