import asyncio
import gc
import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import DeleteMessage, GetChat, SendMessage

from app import database as db
from app import downloader, timeutils, ui
from app.features import channels, conditions
from services import contests
from services.event_isolation import EventIsolation
from services.runtime import RuntimeTasks
from services.telegram_rate import TelegramRateLimit, background_traffic
from services.worker_sandbox import worker_command


class ScaleFixTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown
    contest = fixtures.PlanningTests.contest

    async def test_grouped_referral_counts_preserve_participant_and_channel_rules(self):
        cid = self.contest(status="active", referral_min=2)
        for uid, inviter in ((43, None), (44, 43), (45, 43)):
            db.execute(
                "INSERT INTO contest_entries(contest_id,user_id,inviter_id,joined_at,base_valid) VALUES(?,?,?,?,1)",
                (cid, uid, inviter, timeutils.iso()),
            )
        self.assertEqual(contests.participant_count(cid), 1)
        db.execute(
            "UPDATE contest_entries SET base_valid=0 WHERE contest_id=? AND user_id=45",
            (cid,),
        )
        self.assertEqual(contests.participant_count(cid), 0)
        db.execute("UPDATE contests SET referral_target='channel' WHERE id=?", (cid,))
        for uid in (44, 45):
            db.execute(
                "INSERT INTO contest_channel_referrals(contest_id,user_id,inviter_id,active) VALUES(?,?,43,1)",
                (cid, uid),
            )
        self.assertEqual(contests.participant_count(cid), 1)
        db.execute(
            "UPDATE contest_channel_referrals SET active=0 WHERE contest_id=? AND user_id=45",
            (cid,),
        )
        self.assertEqual(contests.participant_count(cid), 0)

    async def test_draw_commit_retry_does_not_duplicate_result_names(self):
        cid = self.contest(status="closing")
        db.execute(
            "INSERT INTO contest_entries(contest_id,user_id,joined_at,base_valid) VALUES(?,43,?,1)",
            (cid, timeutils.iso()),
        )
        original = db.async_call
        retried = False

        async def retry(callback, *, readonly=False):
            nonlocal retried
            if callback.__name__ == "commit_draw" and not retried:
                retried = True
                try:
                    with db.atomic():
                        callback(db.db)
                        raise sqlite3.OperationalError("database is locked")
                except sqlite3.OperationalError:
                    pass
            return await original(callback, readonly=readonly)

        with patch.object(db, "async_call", new=retry):
            await contests.finish(self.bot, contests.get(cid))
        self.assertTrue(retried)
        result = json.loads(
            db.one(
                "SELECT payload FROM contest_outbox WHERE contest_id=? AND kind='results'",
                (cid,),
            )[0]
        )
        self.assertEqual(result["text"].count("1. <a"), 1)

    async def test_runtime_metrics_store_only_counts_and_durations(self):
        runtime = RuntimeTasks()
        runtime.limiter = SimpleNamespace(pending=[1, 2])
        handler = AsyncMock()
        await runtime(handler, SimpleNamespace(), {})
        heartbeat = AsyncMock()
        await runtime.health(heartbeat)
        metrics = json.loads(db.setting("runtime_metrics"))
        heartbeat.assert_awaited_once()
        self.assertEqual(metrics["telegram_queue"], 2)
        self.assertIsNotNone(metrics["handler_p95_ms"])
        self.assertIn("loop_lag_ms", metrics)

    async def test_count_dirty_survives_entry_added_during_edit(self):
        cid = self.contest(status="active", published_ids="[77]", displayed_count=0)
        db.execute(
            "INSERT INTO contest_entries(contest_id,user_id,joined_at,base_valid) VALUES(?,43,?,1)",
            (cid, timeutils.iso()),
        )

        async def add_during_edit(**kwargs):
            db.execute(
                "INSERT OR IGNORE INTO contest_entries(contest_id,user_id,joined_at,base_valid) VALUES(?,44,?,1)",
                (cid, timeutils.iso()),
            )

        self.bot.edit_message_reply_markup.side_effect = add_during_edit
        await contests.refresh_count(self.bot, cid)
        self.assertEqual(contests.get(cid)["count_dirty"], 1)

        self.bot.edit_message_reply_markup.side_effect = None
        await contests.refresh_count(self.bot, cid)
        self.assertEqual(contests.get(cid)["displayed_count"], 2)
        self.assertEqual(contests.get(cid)["count_dirty"], 0)
        with patch.object(contests, "refresh_count", new=AsyncMock()) as refresh:
            await contests.tick(self.bot)
            refresh.assert_not_awaited()
        db.execute(
            "INSERT INTO blocked_users(telegram_id,created_at) VALUES(43,?)",
            (timeutils.iso(),),
        )
        self.assertEqual(contests.get(cid)["count_dirty"], 1)

    async def test_due_contest_is_not_starved_by_200_active_contests(self):
        for _ in range(200):
            self.contest(status="active")
        due = self.contest(status="scheduled")
        db.execute("UPDATE contests SET starts_at=? WHERE id=?", (timeutils.iso(), due))
        with (
            patch.object(contests, "process_contest", new=AsyncMock()) as process,
            patch.object(contests, "refresh_count", new=AsyncMock()),
            patch.object(contests, "closing_tick", new=AsyncMock()),
            patch.object(contests, "deliver", new=AsyncMock()),
        ):
            await contests.tick(self.bot)
        self.assertIn(due, [call.args[1]["id"] for call in process.await_args_list])

    async def test_concurrent_registration_enforces_owner_and_quota(self):
        async def chat(cid):
            await asyncio.sleep(0)
            return SimpleNamespace(
                id=cid,
                type="supergroup",
                title="Test",
                username=None,
                is_forum=False,
                invite_link=None,
            )

        self.bot.get_chat = AsyncMock(side_effect=chat)
        with patch.object(
            channels.access, "owner_and_bot_ok", new=AsyncMock(return_value=True)
        ):
            target = await chat(-10055)
            results = await asyncio.gather(
                *(channels.register_channel(uid, self.bot, target) for uid in (42, 43)),
                return_exceptions=True,
            )
            self.assertEqual(sum(isinstance(item, ValueError) for item in results), 1)
            with patch.object(channels.accounts, "channel_limit", return_value=1):
                targets = [await chat(-10060 - i) for i in range(3)]
                results = await asyncio.gather(
                    *(
                        channels.register_channel(44, self.bot, item)
                        for item in targets
                    ),
                    return_exceptions=True,
                )
                self.assertEqual(
                    sum(isinstance(item, ValueError) for item in results), 2
                )
        self.assertEqual(
            db.one(
                "SELECT COUNT(*) FROM channels WHERE telegram_chat_id=-10055 AND is_active=1"
            )[0],
            1,
        )

    async def test_constraint_blocks_direct_conflicting_insert(self):
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO channels(telegram_chat_id,title,chat_type,owner_telegram_id,is_active) VALUES(-1001,'Test','channel',43,1)"
            )

    async def test_missing_cleanup_messages_do_not_trigger_edits(self):
        ui.note_transient(42, [10, 11])
        self.bot.delete_message.side_effect = TelegramBadRequest(
            method=DeleteMessage(chat_id=42, message_id=10),
            message="message to delete not found",
        )
        await ui.cleanup_home(self.bot, 42)
        self.bot.edit_message_reply_markup.assert_not_awaited()

    async def test_permanent_approval_failure_is_not_retried_forever(self):
        rid = db.execute(
            "INSERT INTO join_requests(channel_id,telegram_user_id,status,created_at,updated_at) VALUES(?,43,'approved',?,?)",
            (self.cid, timeutils.iso(), timeutils.iso()),
        ).lastrowid
        self.bot.send_message.side_effect = TelegramForbiddenError(
            method=SendMessage(chat_id=43, text="test"), message="bot blocked"
        )
        await conditions.notify_join_approved(rid, self.bot)
        self.assertEqual(
            db.one(
                "SELECT approval_delivery_state FROM join_requests WHERE id=?", (rid,)
            )[0],
            "undeliverable",
        )


class ResourceFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_sandbox_command_does_not_mount_project_or_copy_secrets(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("services.worker_sandbox.os.getenv", return_value="required"),
            patch("services.worker_sandbox.sys.platform", "linux"),
            patch(
                "services.worker_sandbox.shutil.which", return_value="/usr/bin/bwrap"
            ),
        ):
            command, cwd = worker_command(Path(folder), {})
            self.assertIn("--unshare-pid", command)
            self.assertNotIn(str(Path(__file__).resolve().parents[1]), command)
            self.assertTrue((cwd / "app/download_worker.py").is_file())
            self.assertFalse((cwd / ".env").exists())
            self.assertFalse((cwd / "bot.db").exists())

    async def test_requested_sandbox_fails_closed_if_unavailable(self):
        with (
            patch("services.worker_sandbox.os.getenv", return_value="required"),
            patch("services.worker_sandbox.shutil.which", return_value=None),
        ):
            with self.assertRaisesRegex(ValueError, "bubblewrap"):
                worker_command("unused", {})

    async def test_weighted_draw_matches_existing_ticket_distribution(self):
        entries = [
            dict(
                user_id=i,
                inviter_id=i // 3,
                joined_at="2026-01-01",
                referral_min=int(i == 8),
            )
            for i in range(10)
        ]

        def reference(mode, count, bonus):
            scores = {entry["user_id"]: 0 for entry in entries}
            for entry in entries:
                if entry["inviter_id"] in scores:
                    scores[entry["inviter_id"]] += 1
            eligible = [
                entry
                for entry in entries
                if scores[entry["user_id"]] >= entry["referral_min"]
            ]
            result = []
            while eligible and len(result) < count:
                weights = [
                    1 + bonus * scores[entry["user_id"]] if mode == "weighted" else 1
                    for entry in eligible
                ]
                ticket = contests.secrets.randbelow(sum(weights))
                for index, weight in enumerate(weights):
                    ticket -= weight
                    if ticket < 0:
                        entry = eligible.pop(index)
                        result.append((entry, scores[entry["user_id"]]))
                        break
            return result

        for mode in ("random", "weighted"):
            for seed in range(20):
                with patch.object(
                    contests.secrets,
                    "randbelow",
                    side_effect=random.Random(seed).randrange,
                ):
                    expected = reference(mode, 20, 2)
                with patch.object(
                    contests.secrets,
                    "randbelow",
                    side_effect=random.Random(seed).randrange,
                ):
                    actual = contests.choose(entries, mode, 20, 2)
                self.assertEqual(actual, expected)
        self.assertEqual(contests.choose([], "weighted", 5, 2), [])

    async def test_slow_cleanup_does_not_block_menu_and_keeps_new_messages(self):
        case = fixtures.PlanningTests()
        case.setUp()
        try:
            ui.note_transient(42, [10])
            waiting = asyncio.Event()
            case.bot.delete_message.side_effect = lambda *args: waiting.wait()

            async def slow(*args):
                await waiting.wait()

            case.bot.delete_message.side_effect = slow
            await asyncio.wait_for(ui.cleanup_home(case.bot, 42), 0.4)
            ui.note_transient(42, [11])
            waiting.set()
            await ui.close_cleanup()
            self.assertIn("11", db.setting("ui:transient:42"))
            await ui.cleanup_tick(case.bot)
            await ui.close_cleanup()
            self.assertFalse(
                any(
                    call.args[-1] == 11
                    for call in case.bot.delete_message.await_args_list
                )
            )
        finally:
            await ui.close_cleanup()
            case.tearDown()

    async def test_completed_locks_are_released_and_waiters_remain_serial(self):
        isolation = EventIsolation()
        active = 0
        maximum = 0

        async def run(key):
            nonlocal active, maximum
            async with isolation.lock(key):
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0)
                active -= 1

        await asyncio.gather(*(run(42) for _ in range(100)))
        self.assertEqual(maximum, 1)
        for key in range(1000):
            async with isolation.lock(key):
                pass
        gc.collect()
        self.assertEqual(len(isolation.locks), 0)

    async def test_worker_output_is_rejected_before_unbounded_read(self):
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 2000)
        stream.feed_eof()
        error = asyncio.StreamReader()
        error.feed_eof()
        proc = SimpleNamespace(
            stdout=stream,
            stderr=error,
            stdin=SimpleNamespace(
                write=lambda _: None, drain=AsyncMock(), close=lambda: None
            ),
            wait=AsyncMock(),
        )
        with self.assertRaisesRegex(ValueError, "byte limit"):
            await downloader.bounded_communicate(proc, b"{}", limit=1000)

    async def test_interactive_priority_and_cancelled_waiters(self):
        limiter = TelegramRateLimit()
        limiter.next_request = asyncio.get_running_loop().time() + 0.08
        order = []

        async def request(bot, method):
            order.append(method.chat_id)

        token = background_traffic.set(True)
        background = asyncio.create_task(limiter(request, None, GetChat(chat_id=1)))
        background_traffic.reset(token)
        interactive = asyncio.create_task(limiter(request, None, GetChat(chat_id=2)))
        cancelled = asyncio.create_task(limiter(request, None, GetChat(chat_id=3)))
        await asyncio.sleep(0.01)
        cancelled.cancel()
        await asyncio.gather(background, interactive, cancelled, return_exceptions=True)
        self.assertEqual(order, [2, 1])
        self.assertEqual(limiter.pending, [])
