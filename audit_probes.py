"""Isolated audit reproductions: assertions document existing defects, not fixes.

Uses an in-memory database and mocked Telegram methods only.
"""
import asyncio
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent / "tests"))
import test_planning as fixtures
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Message
from app import accounts, content, scheduler, timeutils, ui
from app import database as db
from app.features import admin, conditions, payments, posts, sources
from app.middleware import Guard
from app.states import Broadcast
from services import contests as draws


class AuditProbes(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown
    post = fixtures.PlanningTests.post
    contest = fixtures.PlanningTests.contest

    async def test_01_rate_limited_scheduled_post_never_retries(self):
        pid = self.post()
        posts.schedule_post(pid, 42, timeutils.now() + timedelta(hours=1))
        db.execute("UPDATE scheduled_posts SET publish_at=?", (timeutils.iso(timeutils.now() - timedelta(seconds=1)),))
        send = AsyncMock(side_effect=TelegramRetryAfter(method=SendMessage(chat_id=-1001, text="x"), message="rate limit", retry_after=1))
        with patch.object(posts.access, "owner_and_bot_ok", new=AsyncMock(return_value=True)), patch.object(content, "send_content", new=send), patch.object(posts, "show_post", new=AsyncMock()), patch.object(accounts, "check_referral", new=AsyncMock()):
            await scheduler.scheduler_publish(self.bot)
            await scheduler.scheduler_publish(self.bot)
        self.assertEqual(send.await_count, 1)
        self.assertEqual(db.one("SELECT status FROM posts WHERE id=?", (pid,))[0], "failed")
        self.assertEqual(db.one("SELECT active FROM scheduled_posts WHERE post_id=?", (pid,))[0], 0)

    async def test_02_blocked_customer_checkout_accepted_payment_ignored(self):
        db.execute("INSERT INTO payments(telegram_id,days,amount,currency,payload,created_at) VALUES(42,7,100,'XTR','audit',?)", (timeutils.iso(),))
        query = SimpleNamespace(invoice_payload="audit", from_user=SimpleNamespace(id=42), currency="XTR", total_amount=100, answer=AsyncMock())
        message = Message(message_id=1, date=0, chat={"id":42,"type":"private"}, from_user={"id":42,"is_bot":False,"first_name":"Test"}, successful_payment={"currency":"XTR","total_amount":100,"invoice_payload":"audit","telegram_payment_charge_id":"audit-charge","provider_payment_charge_id":""})
        handler = AsyncMock()
        with patch.object(accounts, "blocked", return_value=True):
            await payments.pre_checkout(query, self.bot)
            await Guard()(handler, message, {"bot":self.bot,"state":self.state})
        self.assertTrue(query.answer.await_args.kwargs["ok"])
        handler.assert_not_awaited()
        self.assertEqual(db.one("SELECT status FROM payments WHERE payload='audit'")[0], "pending")

    async def test_03_one_forbidden_album_blocks_following_users(self):
        for uid in (42,43):
            pid = content.create_draft(uid, {"content_type":"text","text":"audit"})
            db.execute("INSERT INTO album_intake(owner_id,chat_id,group_id,source_id,items_json,targets_json,ready_at,post_id) VALUES(?,?,?,0,?,'[]',?,?)", (uid,uid,str(uid),json.dumps([{"content_type":"photo","file_id":"audit","message_id":1}]), timeutils.iso(timeutils.now()-timedelta(seconds=5)),pid))
        picker = AsyncMock(side_effect=TelegramForbiddenError(method=SendMessage(chat_id=42,text="x"),message="bot blocked"))
        with patch.object(ui, "send_target_picker", new=picker):
            for _ in range(2):
                with self.assertRaises(TelegramForbiddenError):
                    await sources.flush_albums(self.bot)
        self.assertEqual([c.args[1] for c in picker.await_args_list], [42,42])
        self.assertEqual(db.one("SELECT COUNT(*) FROM album_intake")[0],2)

    async def test_04_blocked_join_requests_starve_later_requests(self):
        db.execute("UPDATE channels SET auto_requests=1 WHERE id=?",(self.cid,))
        for uid in range(1000,1101):
            db.execute("INSERT INTO join_requests(telegram_user_id,channel_id,created_at,updated_at) VALUES(?,?,?,?)",(uid,self.cid,timeutils.iso(),timeutils.iso()))
        check = AsyncMock(wraps=conditions.check_request)
        with patch.object(accounts,"blocked",side_effect=lambda uid:1000<=uid<1100), patch.object(conditions,"check_request",new=check):
            await scheduler.scheduler_requests(self.bot)
            await scheduler.scheduler_requests(self.bot)
        self.assertEqual(check.await_count,200)
        self.assertNotIn(101,[c.args[0] for c in check.await_args_list])

    async def test_05_broadcast_rate_limit_discards_resume_state(self):
        await self.state.set_state(Broadcast.content)
        draft={"token":"audit","ids":[123],"chat_id":42}
        db.execute("INSERT INTO app_settings VALUES('broadcast_draft:42',?)",(json.dumps(draft),))
        c=SimpleNamespace(from_user=SimpleNamespace(id=42),data="broadcast:send:audit",answer=AsyncMock())
        self.bot.copy_message=AsyncMock(side_effect=TelegramRetryAfter(method=SendMessage(chat_id=42,text="x"),message="rate limit",retry_after=1))
        with patch.object(admin,"ADMIN_ID",42), patch.object(ui,"edit",new=AsyncMock()):
            with self.assertRaises(TelegramRetryAfter):
                await admin.broadcast_confirm(c,self.state,self.bot)
        self.assertFalse(db.setting("broadcast_draft:42"))
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(self.bot.copy_message.await_count,1)

    async def test_06_due_posts_processed_only_twenty_per_tick(self):
        for _ in range(100):
            pid=self.post()
            db.execute("UPDATE posts SET status='scheduled' WHERE id=?",(pid,))
            db.execute("INSERT INTO scheduled_posts(post_id,publish_at,created_at) VALUES(?,?,?)",(pid,timeutils.iso(timeutils.now()-timedelta(seconds=5)),timeutils.iso()))
        publish=AsyncMock()
        with patch.object(posts,"execute_publish",new=publish), patch.object(posts,"show_post",new=AsyncMock()):
            await scheduler.scheduler_publish(self.bot)
        self.assertEqual(publish.await_count,20)

    async def test_07_contest_subscription_failure_restarts_scan(self):
        cid=self.contest(status="closing", subscriptions_json=json.dumps([{"chat_id":-1001}]))
        for uid in range(1000,1100):
            db.execute("INSERT INTO contest_entries(contest_id,user_id,joined_at,base_valid) VALUES(?,?,?,1)",(cid,uid,timeutils.iso()))
        calls=[]
        async def membership(chat_id,uid):
            calls.append(uid)
            if uid==1050:
                raise TelegramRetryAfter(method=SendMessage(chat_id=42,text="x"),message="rate limit",retry_after=10)
            return SimpleNamespace(status="member")
        self.bot.get_chat_member=AsyncMock(side_effect=membership)
        for _ in range(2):
            with self.assertRaises(TelegramRetryAfter):
                await draws.finish(self.bot,draws.get(cid))
        self.assertEqual(calls.count(1000),2)
        self.assertNotIn(1051,calls)
        self.assertEqual(draws.get(cid)["status"],"closing")

    async def test_08_large_contest_blocks_other_contest_publication(self):
        first=self.contest(status="closing",ends_at=timeutils.iso(timeutils.now()-timedelta(seconds=1)))
        second=self.contest(status="scheduled",published_ids="[]")
        entered=asyncio.Event()
        release=asyncio.Event()
        async def slow_finish(bot,row):
            self.assertEqual(row["id"],first)
            entered.set()
            await release.wait()
        send=AsyncMock(return_value=SimpleNamespace(message_id=999))
        with patch.object(draws,"finish",new=slow_finish), patch.object(draws,"deliver",new=AsyncMock()), patch.object(draws,"refresh_count",new=AsyncMock()), patch.object(draws.access,"owner_and_bot_ok",new=AsyncMock(return_value=True)), patch.object(content,"send_content",new=send):
            task=asyncio.create_task(draws.tick(self.bot))
            await asyncio.wait_for(entered.wait(),2)
            self.assertEqual(draws.get(second)["status"],"scheduled")
            send.assert_not_awaited()
            release.set()
            await task
            self.assertEqual(draws.get(second)["status"],"active")


if __name__ == "__main__":
    unittest.main(verbosity=2)
