"""Offline end-to-end checks for planning, locales, progress and contest draws."""

import ast
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["DB_FILE"] = ":memory:"
os.environ["BOT_TOKEN"] = ""
os.environ["ADMIN_ID"] = "12345"

from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage, SendMessage

from app import accounts, content, downloader, preferences, scheduler, timeutils, ui
from app import database as db
from app.features import contests, downloads, multipost, posts
from app.i18n import CATALOGS, STATUS, language_context, tr
from services import contests as draws
from services.jobs import BackgroundJobs
from services.progress import ProgressInputFile, ProgressMessage, current_progress


class PlanningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old = db.db
        db.db = sqlite3.connect(":memory:")
        db.db.row_factory = sqlite3.Row
        db.init_db()
        self.locale = language_context.set("ru")
        for uid in (42, 43, 44, 45):
            accounts.ensure_user(
                SimpleNamespace(
                    id=uid, username=None, first_name="Test", last_name=None
                )
            )
        self.cid = db.execute(
            "INSERT INTO channels(telegram_chat_id,title,chat_type,owner_telegram_id,bot_is_admin,created_at,updated_at) VALUES(-1001,'Target','channel',42,1,?,?)",
            (timeutils.iso(), timeutils.iso()),
        ).lastrowid
        self.state = FSMContext(
            MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42)
        )
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),
            delete_message=AsyncMock(),
            edit_message_text=AsyncMock(),
            edit_message_caption=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
            get_me=AsyncMock(return_value=SimpleNamespace(id=1, username="TestBot")),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
            create_chat_invite_link=AsyncMock(
                return_value=SimpleNamespace(invite_link="https://t.me/+winner")
            ),
        )

    def tearDown(self):
        db.db.close()
        db.db = self.old
        language_context.reset(self.locale)

    def post(self):
        return content.create_draft(
            42,
            {"content_type": "text", "text": "Original <text> https://t.me/+private"},
            [self.cid],
        )

    def batch(self, count=3):
        bid = db.execute(
            "INSERT INTO multipost_batches(owner_id,channel_id,created_at,start_at,interval_seconds) VALUES(?,?,?,?,?)",
            (42, self.cid, timeutils.iso(), "now", 60),
        ).lastrowid
        for index in range(count):
            db.execute(
                "INSERT INTO multipost_items VALUES(?,?,?)", (bid, index, self.post())
            )
        return bid

    def contest(self, **changes):
        data = {
            "owner_id": 42,
            "channel_id": self.cid,
            "title": "Test <contest>",
            "post_json": json.dumps({"content_type": "text", "text": "Prize post"}),
            "prize_kind": "promo",
            "prize_json": json.dumps({"codes": ["secret-a", "secret-b"]}),
            "starts_at": timeutils.iso(timeutils.now() - timedelta(hours=1)),
            "ends_at": timeutils.iso(timeutils.now() + timedelta(hours=1)),
            "subscriptions_json": "[]",
            "mode": "ranking",
            "winner_count": 2,
            "status": "active",
            "published_ids": "[77]",
            "created_at": timeutils.iso(),
        }
        data.update(changes)
        return db.execute(
            "INSERT INTO contests("
            + ",".join(data)
            + ") VALUES("
            + ",".join("?" for _ in data)
            + ")",
            tuple(data.values()),
        ).lastrowid

    async def test_local_time_and_dst_and_language_defaults(self):
        self.assertEqual(preferences.get_preferences(42)["language"], "ru")
        preferences.save(42, timezone="Asia/Almaty")
        self.assertEqual(
            preferences.parse_local(42, "25.12.2026 18:30"),
            datetime(2026, 12, 25, 13, 30, tzinfo=timezone.utc),
        )
        preferences.save(42, timezone="UTC-03:30")
        self.assertEqual(preferences.parse_local(42, "25.12.2026 18:30").hour, 22)
        preferences.save(42, timezone="America/New_York")
        for text in ("08.03.2026 02:30", "01.11.2026 01:30"):
            with self.assertRaises(ValueError):
                preferences.parse_local(42, text)
        for text in ("UTC+14:01", "UTC-13", "not/a/zone"):
            with self.assertRaises(ValueError):
                preferences.save(42, timezone=text)
        self.assertEqual(preferences.duration("2 сағ"), 7200)
        self.assertEqual(preferences.duration("1 day"), 86400)

    async def test_schedule_requires_confirmation_and_survives_timezone_change(self):
        pid = self.post()
        preferences.save(42, timezone="Asia/Almaty")
        callback = SimpleNamespace(
            data=f"p:{pid}:after:300",
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock()),
        )
        with patch.object(ui, "edit", new=AsyncMock()) as edit:
            await posts.post_action(callback, self.state, self.bot)
            self.assertIsNone(db.one("SELECT * FROM scheduled_posts"))
            markup = edit.await_args.args[2]
            callback.data = markup.inline_keyboard[0][0].callback_data
            await posts.post_action(callback, self.state, self.bot)
        original = db.one("SELECT publish_at FROM scheduled_posts")[0]
        preferences.save(42, timezone="UTC-05:00")
        self.assertEqual(db.one("SELECT publish_at FROM scheduled_posts")[0], original)
        self.assertEqual(db.one("SELECT status FROM posts")[0], "scheduled")

    async def test_scheduled_publish_once_and_delete_at_actual_send_time(self):
        pid = self.post()
        db.execute("UPDATE posts SET delete_after_seconds=3600 WHERE id=?", (pid,))
        start = timeutils.now() + timedelta(minutes=5)
        posts.schedule_post(pid, 42, start)
        with (
            patch.object(timeutils, "now", return_value=start + timedelta(seconds=4)),
            patch("app.access.owner_and_bot_ok", new=AsyncMock(return_value=True)),
            patch.object(posts, "show_post", new=AsyncMock()),
        ):
            await scheduler.scheduler_publish(self.bot)
            await scheduler.scheduler_publish(self.bot)
        self.assertEqual(self.bot.send_message.await_count, 1)
        sent = db.one("SELECT * FROM published_messages")
        self.assertEqual(
            timeutils.parse_dt(sent["delete_at"]), start + timedelta(seconds=3604)
        )
        with patch.object(
            timeutils, "now", return_value=start + timedelta(seconds=3605)
        ):
            await scheduler.scheduler_delete(self.bot)
            await scheduler.scheduler_delete(self.bot)
        self.bot.delete_message.assert_awaited_once_with(-1001, 77)
        self.assertEqual(db.one("SELECT deleted FROM published_messages")[0], 1)

    async def test_delete_permission_failure_is_visible(self):
        db.execute(
            "INSERT INTO published_messages(post_id,channel_id,telegram_message_id,delete_at) VALUES(?,?,?,?)",
            (
                self.post(),
                self.cid,
                12,
                timeutils.iso(timeutils.now() - timedelta(seconds=1)),
            ),
        )
        self.bot.delete_message.side_effect = TelegramForbiddenError(
            method=DeleteMessage(chat_id=-1001, message_id=12), message="forbidden"
        )
        await scheduler.scheduler_delete(self.bot)
        self.assertEqual(db.one("SELECT deleted FROM published_messages")[0], -1)
        self.assertIn("Автоудаление", self.bot.send_message.await_args.args[1])

    async def test_batch_limits_atomicity_and_duplicate_confirmation(self):
        bid = self.batch()
        start = timeutils.now() + timedelta(minutes=1)
        with self.assertRaises(ValueError):
            multipost.validate_timetable(
                multipost.owned(bid, 42), 42, start, 16 * 86400
            )
        real = posts.schedule_post
        calls = 0

        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("Simulated validation error")
            return real(*args)

        with patch.object(posts, "schedule_post", side_effect=fail_second):
            with self.assertRaises(ValueError):
                multipost.confirm(bid, 42)
        self.assertEqual(db.one("SELECT COUNT(*) FROM scheduled_posts")[0], 0)
        self.assertEqual(multipost.owned(bid, 42)["status"], "draft")
        multipost.confirm(bid, 42)
        with self.assertRaises(ValueError):
            multipost.confirm(bid, 42)
        self.assertEqual(
            db.one("SELECT COUNT(*) FROM scheduled_posts WHERE active=1")[0], 3
        )
        times = [
            timeutils.parse_dt(r[0])
            for r in db.all_rows("SELECT publish_at FROM scheduled_posts ORDER BY id")
        ]
        self.assertEqual(times[1] - times[0], timedelta(minutes=1))
        too_many = self.batch(11)
        with self.assertRaises(ValueError):
            multipost.confirm(too_many, 42)
        with patch.object(accounts, "has_premium", return_value=True):
            multipost.confirm(too_many, 42)
        with self.assertRaises(ValueError):
            multipost.owned(bid, 43)

    async def test_referral_registration_is_immutable_and_rejects_self_referral(self):
        cid = self.contest()
        draws.register(cid, 42, 42)
        draws.register(cid, 43, 42)
        draws.register(cid, 43, 44)
        self.assertIsNone(
            db.one("SELECT inviter_id FROM contest_entries WHERE user_id=42")[0]
        )
        self.assertEqual(
            db.one("SELECT inviter_id FROM contest_entries WHERE user_id=43")[0], 42
        )
        db.execute("UPDATE contests SET status='completed' WHERE id=?", (cid,))
        with self.assertRaises(ValueError):
            draws.register(cid, 44, 42)

    async def test_complete_contest_wizard_publishes_once_and_confirmation_is_idempotent(
        self,
    ):
        message = SimpleNamespace(chat=SimpleNamespace(id=42), answer=AsyncMock())
        await self.state.set_data(
            {"channel_id": self.cid, "creation_token": "test-token"}
        )
        await contests.prompt(message, self.state, "title")
        for step, value in [
            ("title", "Prize contest"),
            ("post", "Public prize description"),
            ("prize_kind", "promo"),
            ("prize", "SECRET_CODE"),
            ("winners", "1"),
            ("mode", "weighted"),
            ("subscriptions", "-"),
            ("captcha", "0"),
            ("quiz", "-"),
            ("referrals", "0"),
            ("start", "now"),
            ("end", "day"),
        ]:
            self.assertEqual((await self.state.get_data())["step"], step)
            await contests.accept(
                message,
                self.state,
                self.bot,
                value,
                {"content_type": "text", "text": value} if step == "post" else None,
            )
        saved = await self.state.get_data()
        callback = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock())
        with patch.object(ui, "edit", new=AsyncMock()):
            await contests.create(callback, self.state)
            await self.state.set_data(saved)
            await contests.create(callback, self.state)
        self.assertEqual(db.one("SELECT COUNT(*) FROM contests")[0], 1)
        row = db.one("SELECT * FROM contests")
        self.bot.send_message.reset_mock()
        with (
            patch.object(
                timeutils,
                "now",
                return_value=timeutils.parse_dt(row["starts_at"])
                + timedelta(seconds=1),
            ),
            patch("app.access.owner_and_bot_ok", new=AsyncMock(return_value=True)),
        ):
            await draws.tick(self.bot)
            await draws.tick(self.bot)
        self.bot.send_message.assert_awaited_once()
        args = self.bot.send_message.await_args
        self.assertEqual(args.args[0], -1001)
        self.assertNotIn("SECRET_CODE", args.args[1])
        button = args.kwargs["reply_markup"].inline_keyboard[-1][0]
        self.assertEqual(button.callback_data, f"contest:participate:{row['id']}")
        self.assertIsNone(button.url)
        self.assertEqual(draws.get(row["id"])["status"], "active")

    async def test_quick_contest_needs_only_post_and_deadline_and_can_resume(self):
        message = SimpleNamespace(chat=SimpleNamespace(id=42), answer=AsyncMock())
        callback = SimpleNamespace(
            data=f"contest:target:{self.cid}",
            from_user=SimpleNamespace(id=42),
            message=message,
            answer=AsyncMock(),
        )
        await contests.target(callback, self.state)
        self.assertEqual((await self.state.get_data())["step"], "post")
        await contests.accept(
            message,
            self.state,
            self.bot,
            "Win a book",
            {"content_type": "text", "text": "Win a book"},
        )
        self.assertEqual((await self.state.get_data())["step"], "end")
        await contests.accept(message, self.state, self.bot, "day")
        data = await self.state.get_data()
        self.assertEqual(data["step"], "review")
        self.assertEqual(
            (data["winners"], data["mode"], data["prize_kind"], data["referrals"]),
            (1, "random", "physical", 0),
        )
        self.assertEqual(db.one("SELECT COUNT(*) FROM contests")[0], 0)
        self.assertTrue(db.setting("contest_draft:42"))
        await self.state.clear()
        with patch.object(ui, "edit", new=AsyncMock()):
            await contests.resume(callback, self.state)
        self.assertEqual((await self.state.get_data())["post"]["text"], "Win a book")
        await self.state.update_data(step="winners")
        await contests.accept(message, self.state, self.bot, "3")
        self.assertEqual((await self.state.get_data())["step"], "review")
        with patch.object(ui, "edit", new=AsyncMock()):
            await contests.create(callback, self.state)
        saved = db.one("SELECT * FROM contests")
        self.assertEqual(saved["winner_count"], 3)
        self.assertEqual(
            timeutils.parse_dt(saved["ends_at"])
            - timeutils.parse_dt(saved["starts_at"]),
            timedelta(days=1),
        )
        self.assertFalse(db.setting("contest_draft:42"))

    async def test_cancelling_prize_edit_preserves_previous_prize(self):
        message = SimpleNamespace(chat=SimpleNamespace(id=42), answer=AsyncMock())
        callback = SimpleNamespace(
            data=f"contest:target:{self.cid}",
            from_user=SimpleNamespace(id=42),
            message=message,
            answer=AsyncMock(),
        )
        await contests.target(callback, self.state)
        await contests.accept(
            message,
            self.state,
            self.bot,
            "Prize",
            {"content_type": "text", "text": "Prize"},
        )
        await contests.accept(message, self.state, self.bot, "day")
        original = (await self.state.get_data())["prize"]
        await self.state.update_data(step="prize_kind")
        await contests.accept(message, self.state, self.bot, "promo")
        self.assertEqual((await self.state.get_data())["step"], "prize")
        await contests.draft_panel(message, self.state)
        data = await self.state.get_data()
        self.assertEqual(data["prize_kind"], "physical")
        self.assertEqual(data["prize"], original)
        self.assertNotIn("pending_prize_kind", data)

    async def test_quick_subscription_picker_and_custom_dates(self):
        message = SimpleNamespace(chat=SimpleNamespace(id=42), answer=AsyncMock())
        callback = SimpleNamespace(
            data=f"contest:target:{self.cid}",
            from_user=SimpleNamespace(id=42),
            message=message,
            answer=AsyncMock(),
        )
        await contests.target(callback, self.state)
        await contests.accept(
            message,
            self.state,
            self.bot,
            "Prize",
            {"content_type": "text", "text": "Prize"},
        )
        await contests.accept(message, self.state, self.bot, "day")
        callback.data = f"contest:subtoggle:{self.cid}"
        self.bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
        with (
            patch.object(ui, "edit", new=AsyncMock()),
            patch.object(
                contests,
                "subscription_link",
                new=AsyncMock(return_value="https://t.me/+private"),
            ),
        ):
            await contests.subscription_choice(callback, self.state, self.bot)
            self.assertEqual(len((await self.state.get_data())["subscriptions"]), 1)
            await contests.subscription_choice(callback, self.state, self.bot)
            self.assertEqual((await self.state.get_data())["subscriptions"], [])
        preferences.save(42, timezone="UTC+05:00")
        await self.state.update_data(step="end")
        future = (
            (timeutils.now() + timedelta(days=5))
            .astimezone(preferences.zone("UTC+05:00"))
            .strftime("%d.%m.%Y %H:%M")
        )
        await contests.accept(message, self.state, self.bot, future)
        original = (await self.state.get_data())["end"]
        await self.state.update_data(step="start")
        await contests.accept(message, self.state, self.bot, "hour")
        self.assertEqual((await self.state.get_data())["end"], original)

    async def test_invite_prize_is_persisted_before_retry(self):
        cid = self.contest(prize_kind="invite", prize_json=json.dumps({"value": -1002}))
        draws.register(cid, 42)
        db.execute("UPDATE contest_entries SET base_valid=1")
        db.execute("UPDATE contests SET status='closing' WHERE id=?", (cid,))
        await draws.finish(self.bot, draws.get(cid))
        await draws.deliver(self.bot)
        self.bot.create_chat_invite_link.assert_awaited_once()
        self.assertEqual(
            self.bot.create_chat_invite_link.await_args.kwargs["member_limit"], 1
        )
        db.execute("UPDATE contest_outbox SET status='pending' WHERE kind='winner'")
        await draws.deliver(self.bot)
        self.bot.create_chat_invite_link.assert_awaited_once()

    async def test_failed_prize_notifies_owner_and_cannot_be_retried_by_another_user(
        self,
    ):
        cid = self.contest()
        draws.register(cid, 43)
        db.execute("UPDATE contest_entries SET base_valid=1")
        db.execute("UPDATE contests SET status='closing' WHERE id=?", (cid,))
        await draws.finish(self.bot, draws.get(cid))

        async def send(uid, text, **kwargs):
            if uid == 43:
                raise TelegramForbiddenError(
                    method=SendMessage(chat_id=uid, text=text), message="blocked"
                )
            return SimpleNamespace(message_id=90)

        self.bot.send_message.side_effect = send
        await draws.deliver(self.bot)
        await draws.deliver(self.bot)
        item = db.one("SELECT * FROM contest_outbox WHERE kind='winner'")
        self.assertEqual(item["status"], "failed")
        self.assertIsNotNone(
            db.one(
                "SELECT * FROM contest_outbox WHERE kind='failed:43' AND status='sent'"
            )
        )
        with self.assertRaises(ValueError):
            await contests.delivery_review(
                SimpleNamespace(
                    data=f"contest:retry:{item['id']}", from_user=SimpleNamespace(id=44)
                )
            )

    async def test_entry_explains_subscriptions_then_checks_captcha_and_quiz(self):
        cid = self.contest(
            subscriptions_json=json.dumps(
                [{"chat_id": -1001, "title": "Channel", "url": "https://t.me/+private"}]
            ),
            captcha=1,
            quiz_question="2+2?",
            quiz_hash=draws.digest("4"),
        )
        m = SimpleNamespace(
            chat=SimpleNamespace(id=43),
            from_user=SimpleNamespace(id=43),
            answer=AsyncMock(),
            text="",
        )
        state = FSMContext(
            MemoryStorage(), StorageKey(bot_id=1, chat_id=43, user_id=43)
        )
        self.bot.get_chat_member.return_value = SimpleNamespace(status="left")
        await contests.check_entry(m, state, self.bot, cid, 43)
        self.assertEqual(
            m.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url,
            "https://t.me/+private",
        )
        self.bot.get_chat_member.return_value = SimpleNamespace(status="member")
        await contests.check_entry(m, state, self.bot, cid, 43)
        question = db.one("SELECT captcha_question FROM contest_entries")[0]
        a, b = question.split(" = ")[0].split(" + ")
        m.text = str(int(a) + int(b))
        await contests.answer(m, state, self.bot)
        self.assertEqual((await state.get_data())["kind"], "quiz")
        m.text = "4"
        await contests.answer(m, state, self.bot)
        self.assertEqual(db.one("SELECT base_valid FROM contest_entries")[0], 1)
        self.assertIn("Вы участвуете", m.answer.await_args.args[0])

    async def test_draw_rechecks_members_and_delivers_once(self):
        cid = self.contest(
            referral_min=1, subscriptions_json=json.dumps([{"chat_id": -1001}])
        )
        draws.register(cid, 42)
        draws.register(cid, 43, 42)
        draws.register(cid, 44, 42)
        db.execute("UPDATE contest_entries SET base_valid=1")
        db.execute(
            "UPDATE contests SET status='closing',ends_at=? WHERE id=?",
            (timeutils.iso(timeutils.now() - timedelta(seconds=1)), cid),
        )

        async def member(chat, uid):
            return SimpleNamespace(status="left" if uid == 44 else "member")

        self.bot.get_chat_member.side_effect = member
        await draws.finish(self.bot, draws.get(cid))
        winners = db.all_rows("SELECT * FROM contest_winners")
        self.assertEqual([(w["user_id"], w["score"]) for w in winners], [(42, 1)])
        self.assertEqual(
            db.one("SELECT base_valid FROM contest_entries WHERE user_id=44")[0], 0
        )
        with patch.object(
            draws, "choose", side_effect=AssertionError("Must not reroll")
        ):
            await draws.finish(self.bot, draws.get(cid))
            draws.recover()
            await draws.tick(self.bot)
            await draws.tick(self.bot)
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.bot.edit_message_text.assert_awaited_once()
        public = self.bot.edit_message_text.await_args.kwargs
        self.assertEqual(public["message_id"], 77)
        self.assertNotIn("secret-a", public["text"])
        self.assertTrue(
            all(
                r[0] == "sent" for r in db.all_rows("SELECT status FROM contest_outbox")
            )
        )

    async def test_membership_api_failure_does_not_disqualify_or_draw(self):
        cid = self.contest(subscriptions_json='[{"chat_id":-1001}]')
        draws.register(cid, 42)
        db.execute("UPDATE contest_entries SET base_valid=1")
        db.execute("UPDATE contests SET status='closing' WHERE id=?", (cid,))
        self.bot.get_chat_member.side_effect = RuntimeError("Temporary API outage")
        with self.assertRaises(RuntimeError):
            await draws.finish(self.bot, draws.get(cid))
        self.assertEqual(db.one("SELECT base_valid FROM contest_entries")[0], 1)
        self.assertEqual(db.one("SELECT COUNT(*) FROM contest_winners")[0], 0)
        self.assertEqual(draws.get(cid)["status"], "closing")

    async def test_ambiguous_delivery_never_retries_automatically(self):
        cid = self.contest()
        draws.register(cid, 42)
        db.execute("UPDATE contest_entries SET base_valid=1")
        db.execute("UPDATE contests SET status='closing' WHERE id=?", (cid,))
        await draws.finish(self.bot, draws.get(cid))
        self.bot.send_message.side_effect = TelegramNetworkError(
            method=SendMessage(chat_id=42, text="x"), message="connection lost"
        )
        await draws.deliver(self.bot)
        count = self.bot.send_message.await_count
        draws.recover()
        await draws.deliver(self.bot)
        self.assertEqual(self.bot.send_message.await_count, count)
        self.assertEqual(
            db.one('SELECT COUNT(*) FROM contest_outbox WHERE status="uncertain"')[0], 2
        )

    async def test_weighted_draw_is_unique_and_ranking_ties_are_stable(self):
        entries = [
            {"user_id": i, "inviter_id": 42 if i != 42 else None, "joined_at": str(i)}
            for i in (42, 43, 44)
        ]
        self.assertEqual(draws.choose(entries, "ranking", 2, 1)[0][0]["user_id"], 42)
        selected = draws.choose(entries, "weighted", 20, 1)
        self.assertEqual(len({e["user_id"] for e, _ in selected}), 3)

    async def test_upload_progress_uses_same_message_and_can_cancel_mid_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.mp4"
            path.write_bytes(b"x" * 200000)
            progress = ProgressMessage(self.bot, 42)
            token = current_progress.set(progress)
            await progress.update("download", 50000, 200000, force=True)
            entered = asyncio.Event()
            jobs = BackgroundJobs()

            async def upload():
                async with ProgressInputFile(path, chunk_size=65536) as file:
                    async for chunk in file.read(self.bot):
                        entered.set()
                        await asyncio.Event().wait()

            try:
                jobs.start(42, upload, "upload")
                await asyncio.wait_for(entered.wait(), 2)
                self.assertTrue(jobs.cancel(42))
                await jobs.close()
            finally:
                current_progress.reset(token)
            self.assertEqual(self.bot.send_message.await_count, 1)
            self.assertTrue(self.bot.edit_message_text.await_count >= 1)
            self.assertEqual(
                self.bot.edit_message_text.await_args.kwargs["message_id"], 77
            )
            self.assertFalse(jobs.active(42))

    async def test_progress_menu_is_separate_from_progress_message(self):
        progress = ProgressMessage(self.bot, 42)
        await progress.update("download", 1, 10, force=True)
        button = self.bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard[1][0]
        self.assertEqual(button.callback_data, "download:menu")
        callback = SimpleNamespace(
            message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        await downloads.progress_menu(callback)
        callback.message.answer.assert_awaited_once()
        callback.message.edit_text.assert_not_awaited()
        await progress.update("download", 2, 10, force=True)
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], 77)

    async def test_worker_cancel_stops_running_process_and_waits_for_cleanup(self):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def communicate(request):
            started.set()
            await stopped.wait()
            return b"", b""

        proc = SimpleNamespace(communicate=communicate)

        async def stop(process):
            self.assertIs(process, proc)
            stopped.set()

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(
                asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)
            ),
            patch.object(
                downloader, "stop_download_worker", new=AsyncMock(side_effect=stop)
            ) as cleanup,
        ):
            task = asyncio.create_task(
                downloader.run_download_worker(
                    "download", "https://example.org/video", Path(folder)
                )
            )
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            cleanup.assert_awaited_once_with(proc)
            self.assertTrue(stopped.is_set())


class LocaleTests(unittest.TestCase):
    def test_all_messages_and_placeholders_have_translations(self):
        root = Path(__file__).resolve().parents[1]
        fields = lambda s: sorted(
            name for _, name, _, _ in Formatter().parse(s) if name is not None
        )
        keys = (
            set(contests.PROMPTS.values())
            | set(dict.values(accounts.CONDITION_LABELS))
            | set(dict.values(STATUS))
        )
        for folder in ("app", "services"):
            for path in (root / folder).rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "tr"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                    ):
                        keys.add(node.args[0].value)
        for language, catalog in CATALOGS.items():
            self.assertFalse(keys - set(catalog), (language, keys - set(catalog)))
            for key in keys:
                self.assertEqual(fields(key), fields(catalog[key]), (language, key))

    def test_language_switch_changes_menus_but_preserves_user_content(self):
        for language, label in [
            ("en", "Settings"),
            ("kk", "Баптаулар"),
            ("ru", "Настройки"),
        ]:
            token = language_context.set(language)
            try:
                texts = [b.text for row in ui.main_kb().inline_keyboard for b in row]
                self.assertIn("⚙️ " + label, texts)
                self.assertIn(
                    "Русский user content",
                    tr(
                        "🎉 Вы выиграли в конкурсе «{title}»!\n{prize}",
                        title="Русский user content",
                        prize="secret",
                    ),
                )
            finally:
                language_context.reset(token)
