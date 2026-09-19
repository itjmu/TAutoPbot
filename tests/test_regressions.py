import asyncio
import json
import os
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["DB_FILE"] = ":memory:"
os.environ["BOT_TOKEN"] = ""
os.environ["ADMIN_ID"] = "12345"

from aiogram.enums import ChatMemberStatus

from app import access as access
from app import accounts as accounts
from app import content as content
from app import database as database
from app import downloader as downloader
from app import timeutils as timeutils
from app import ui as ui
from app.features import conditions as features_conditions
from app.features import downloads as features_downloads
from app.features import payments as features_payments
from app.features import sources as features_sources
from services.jobs import download_jobs
from services.migrations import has_unique_key, migrate_answers
from services.telegram_links import chat_reference, subscription_link, supports_requests


class MigrationTests(unittest.TestCase):
    def test_legacy_answers_and_new_writes(self):
        for partially_migrated in (False, True):
            with self.subTest(partially_migrated=partially_migrated):
                db = sqlite3.connect(":memory:")
                db.execute(
                    "CREATE TABLE request_answers(id INTEGER PRIMARY KEY,request_id INTEGER NOT NULL,question_id INTEGER NOT NULL,answer TEXT,is_correct INTEGER,created_at TEXT)"
                )
                db.execute("INSERT INTO request_answers VALUES(1,10,20,'yes',1,'date')")
                if partially_migrated:
                    db.execute("ALTER TABLE request_answers ADD COLUMN item_id INTEGER")
                db.commit()
                migrate_answers(db)
                migrate_answers(db)
                self.assertEqual(
                    db.execute("SELECT item_id FROM request_answers").fetchone()[0], 20
                )
                db.execute(
                    "INSERT INTO request_answers(request_id,item_id,answer,is_correct,created_at) VALUES(11,21,'new',1,'date')"
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM request_answers").fetchone()[0], 2
                )
                db.close()


class LinkTests(unittest.TestCase):
    def test_sources(self):
        for raw in (
            "@example",
            "t.me/example",
            "https://t.me/example/123",
            "https://t.me/s/example/123",
        ):
            self.assertEqual(chat_reference(raw), "@example")
        self.assertEqual(chat_reference("https://t.me/c/123456/9"), -100123456)
        self.assertEqual(chat_reference("-100123456"), -100123456)

    def test_invite_not_misinterpreted_as_username(self):
        for raw in (
            "https://t.me/+abc",
            "https://t.me/joinchat/abc",
            "https://evil.test/example",
        ):
            with self.assertRaises(ValueError):
                chat_reference(raw)

    def test_request_filter(self):
        self.assertFalse(
            supports_requests({"chat_type": "channel", "username": "public"})
        )
        self.assertTrue(supports_requests({"chat_type": "channel", "username": None}))
        self.assertTrue(
            supports_requests({"chat_type": "supergroup", "username": "public"})
        )

    def test_private_subscription_link(self):
        api = SimpleNamespace(
            get_chat=AsyncMock(
                return_value=SimpleNamespace(id=-1001, username=None, invite_link=None)
            ),
            create_chat_invite_link=AsyncMock(
                return_value=SimpleNamespace(invite_link="https://t.me/+private")
            ),
        )
        self.assertEqual(
            asyncio.run(subscription_link(api, {"chat_id": -1001})),
            "https://t.me/+private",
        )


class BotTests(unittest.TestCase):
    def test_channel_promotion_completes_only_requested_setup(self):
        from app.features import channels

        event = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            chat=SimpleNamespace(id=-1002, type="channel", title="New channel"),
            new_chat_member=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR),
        )
        api = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
        state = SimpleNamespace(
            get_state=AsyncMock(return_value="AddChannel:waiting"), clear=AsyncMock()
        )
        from unittest.mock import Mock

        dispatcher = SimpleNamespace(
            fsm=SimpleNamespace(get_context=Mock(return_value=state))
        )
        with (
            patch.object(channels, "register_channel", new=AsyncMock()) as register,
            patch.object(accounts, "check_referral", new=AsyncMock()),
        ):
            asyncio.run(channels.channel_bot_added(event, api, dispatcher))
            register.assert_not_awaited()
            database.execute(
                "INSERT INTO app_settings VALUES(?,?)",
                ("channel_setup:42", timeutils.iso()),
            )
            asyncio.run(channels.channel_bot_added(event, api, dispatcher))
            register.assert_awaited_once_with(42, api, event.chat)
            state.clear.assert_awaited_once()
            self.assertEqual(database.setting("channel_setup:42"), "")

    def test_cover_editor_saves_and_removes_cover(self):
        from app.features import posts
        from app.states import PostCreate

        pid = content.create_draft(42, {"content_type": "video", "file_id": "video"})
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={"pid": pid, "cover_index": None}),
            get_state=AsyncMock(return_value=PostCreate.cover.state),
            set_state=AsyncMock(),
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            photo=[SimpleNamespace(file_id="custom-cover")],
            text=None,
        )
        with patch.object(posts, "show_post", new=AsyncMock()):
            asyncio.run(posts.edit_post_value(message, state, None))
            self.assertEqual(
                content.post_owned(pid, 42)["cover_file_id"], "custom-cover"
            )
            message.photo = []
            message.text = "-"
            asyncio.run(posts.edit_post_value(message, state, None))
            self.assertIsNone(content.post_owned(pid, 42)["cover_file_id"])

    def test_stale_heartbeat_does_not_delay_restart(self):
        from app.scheduler import acquire_runtime_lock

        database.execute(
            "INSERT INTO runtime_lock VALUES(1,?,?)", ("old-process", timeutils.iso())
        )
        acquire_runtime_lock()
        self.assertNotEqual(
            database.one("SELECT owner FROM runtime_lock")["owner"], "old-process"
        )

    def test_video_cover_saved_and_passed_to_telegram(self):
        pid = content.create_draft(
            42, {"content_type": "video", "file_id": "video", "cover_file_id": "cover"}
        )
        rendered = content.render_payload(
            content.post_owned(pid, 42), {"default_template_id": None}
        )
        api = SimpleNamespace(send_video=AsyncMock())
        asyncio.run(content.send_content(api, 42, rendered))
        self.assertEqual(api.send_video.await_args.kwargs["cover"], "cover")

    def test_album_cover_only_applies_to_video(self):
        api = SimpleNamespace(send_media_group=AsyncMock(return_value=[]))
        payload = {
            "content_type": "album",
            "media_json": [
                {"content_type": "photo", "file_id": "photo"},
                {"content_type": "video", "file_id": "video", "cover_file_id": "cover"},
            ],
        }
        asyncio.run(content.send_content(api, 42, payload))
        media = api.send_media_group.await_args.args[1]
        self.assertEqual(media[1].cover, "cover")
        self.assertNotIn("cover", media[0].model_dump(exclude_none=True))

    def test_channel_setup_link_uses_current_bot_username(self):
        from app.features import channels

        state = SimpleNamespace(set_state=AsyncMock())
        callback = SimpleNamespace(
            from_user=SimpleNamespace(
                id=42, username="u", first_name="U", last_name=None
            ),
            answer=AsyncMock(),
        )
        api = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="ExampleBot"))
        )
        with patch.object(ui, "edit", new=AsyncMock()) as edit:
            asyncio.run(channels.channel_add(callback, state, api))
        button = edit.await_args.args[2].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, "channel:picker")
        self.assertIsNone(button.url)
        group_url = edit.await_args.args[2].inline_keyboard[0][1].url
        self.assertTrue(
            group_url.startswith("https://t.me/ExampleBot?startgroup&admin=")
        )
        self.assertIn("restrict_members", group_url)

    def setUp(self):
        self.old = database.db
        database.db = sqlite3.connect(":memory:")
        database.db.row_factory = sqlite3.Row
        database.init_db()
        accounts.ensure_user(
            SimpleNamespace(id=42, username="user", first_name="Test", last_name=None)
        )

    def tearDown(self):
        database.db.close()
        database.db = self.old

    def test_init_is_idempotent(self):
        database.init_db()
        self.assertIsNotNone(database.one("SELECT 1 FROM users WHERE telegram_id=42"))

    def test_execute_does_not_commit_outer_transaction(self):
        with self.assertRaises(RuntimeError):
            with database.atomic():
                database.execute("INSERT INTO app_settings VALUES('rollback','test')")
                raise RuntimeError()
        self.assertIsNone(
            database.one("SELECT 1 FROM app_settings WHERE key='rollback'")
        )

    def test_private_and_external_buttons_survive_render(self):
        buttons = [
            {"type": "url", "text": "Private", "url": "https://t.me/+secret", "row": 1},
            {"type": "url", "text": "Website", "url": "https://example.org", "row": 2},
        ]
        pid = content.create_draft(
            42,
            {
                "content_type": "text",
                "text": "https://example.org",
                "buttons_json": json.dumps(buttons),
            },
        )
        result = content.render_payload(
            content.post_owned(pid, 42), {"default_template_id": None}
        )
        self.assertEqual(result["buttons"], buttons)
        self.assertEqual(result["text"], "https://example.org")

    def test_manual_approval_is_idempotent(self):
        database.execute(
            "INSERT INTO manual_payments(telegram_id,days,reference,method,created_at) VALUES(42,7,'ref','test',?)",
            (timeutils.iso(),),
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=12345),
            data="payment:review:1:approve",
            answer=AsyncMock(),
        )
        with patch.object(ui, "edit", new=AsyncMock()):
            asyncio.run(features_payments.manual_payment_review(callback))
            expiry = accounts.premium_expiry(42)
            with self.assertRaises(ValueError):
                asyncio.run(features_payments.manual_payment_review(callback))
        self.assertEqual(accounts.premium_expiry(42), expiry)

    def test_manual_approval_rejects_non_admin(self):
        callback = SimpleNamespace(from_user=SimpleNamespace(id=42))
        with self.assertRaises(ValueError):
            asyncio.run(features_payments.manual_payment_review(callback))

    def test_stars_payment_replay_does_not_extend_twice(self):
        database.execute(
            "INSERT INTO payments(telegram_id,days,amount,currency,payload,created_at) VALUES(42,7,100,'XTR','order',?)",
            (timeutils.iso(),),
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=42, username="u", first_name="U", last_name=None
            ),
            successful_payment=SimpleNamespace(
                invoice_payload="order",
                currency="XTR",
                total_amount=100,
                telegram_payment_charge_id="charge",
            ),
            answer=AsyncMock(),
        )
        with patch.object(accounts, "check_referral", new=AsyncMock()):
            asyncio.run(features_payments.successful_payment(message, None))
            expiry = accounts.premium_expiry(42)
            asyncio.run(features_payments.successful_payment(message, None))
        self.assertEqual(accounts.premium_expiry(42), expiry)
        self.assertEqual(message.answer.await_count, 1)

    def test_wrong_stars_amount_does_not_grant_premium(self):
        database.execute(
            "INSERT INTO payments(telegram_id,days,amount,currency,payload,created_at) VALUES(42,7,100,'XTR','order',?)",
            (timeutils.iso(),),
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=42, username="u", first_name="U", last_name=None
            ),
            successful_payment=SimpleNamespace(
                invoice_payload="order",
                currency="XTR",
                total_amount=1,
                telegram_payment_charge_id="charge",
            ),
            answer=AsyncMock(),
        )
        asyncio.run(features_payments.successful_payment(message, None))
        self.assertFalse(accounts.has_premium(42))
        self.assertEqual(
            database.one("SELECT status FROM payments")["status"], "pending"
        )

    def test_playlist_continues_after_failure_and_retry_skips_sent(self):
        info = {
            "backend": "playlist",
            "title": "List",
            "choices": [{"format": "best", "kind": "video"}],
            "entries": [
                {"url": f"https://example.org/{i}", "title": str(i)} for i in range(3)
            ],
        }
        did = database.execute(
            "INSERT INTO downloads(owner_id,url,info_json,status,created_at) VALUES(?,?,?,?,?)",
            (
                42,
                "https://example.org/list",
                json.dumps(info),
                "ready",
                timeutils.iso(),
            ),
        ).lastrowid
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42), data=f"dl:{did}:0", answer=AsyncMock()
        )
        api = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
        attempted = []
        fail = True

        async def worker(action, url, folder, *args):
            attempted.append(url)
            if fail and url.endswith("/1"):
                raise ValueError("Unavailable")
            path = folder / "file.mp4"
            path.write_bytes(b"mocked media")
            return {"files": [str(path.resolve())]}

        async def scenario():
            nonlocal fail
            await features_downloads.download_selected(callback, api)
            await download_jobs.tasks[42]
            self.assertEqual(
                database.one("SELECT status FROM downloads")["status"], "failed"
            )
            self.assertEqual(len(attempted), 3)
            fail = False
            await features_downloads.download_selected(callback, api)
            await download_jobs.tasks[42]

        with (
            patch.object(downloader, "run_download_worker", side_effect=worker),
            patch.object(
                features_downloads,
                "send_downloaded_file",
                new=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            ) as send,
            patch.object(
                content,
                "message_payload",
                return_value={"content_type": "video", "file_id": "file"},
            ),
            patch.object(ui, "send_target_picker", new=AsyncMock()),
        ):
            asyncio.run(scenario())
            self.assertEqual(send.await_count, 3)
        self.assertEqual(attempted, [f"https://example.org/{i}" for i in (0, 1, 2, 1)])
        self.assertEqual(database.one("SELECT status FROM downloads")["status"], "done")

    def test_join_conditions_include_private_subscription_button(self):
        cid = database.execute(
            "INSERT INTO channels(telegram_chat_id,title,chat_type,owner_telegram_id,auto_requests,condition_id,created_at,updated_at) VALUES(-1001,'Private','channel',42,1,1,?,?)",
            (timeutils.iso(), timeutils.iso()),
        ).lastrowid
        database.execute(
            "INSERT INTO condition_items(condition_id,item_type,data) VALUES(1,'subscription',?)",
            (
                json.dumps(
                    {
                        "chat_id": -1002,
                        "title": "Subscribe",
                        "url": "https://t.me/+private",
                    }
                ),
            ),
        )
        rid = database.execute(
            "INSERT INTO join_requests(telegram_user_id,channel_id,created_at,updated_at) VALUES(43,?,?,?)",
            (cid, timeutils.iso(), timeutils.iso()),
        ).lastrowid
        api = SimpleNamespace(send_message=AsyncMock())
        with patch.object(access, "member_ok", new=AsyncMock(return_value=False)):
            asyncio.run(features_conditions.check_request(rid, api, True))
        markup = api.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://t.me/+private")

    def test_large_video_format_is_available(self):
        choices = downloader.media_choices(
            {
                "formats": [
                    {
                        "format_id": "18",
                        "protocol": "https",
                        "ext": "mp4",
                        "vcodec": "h264",
                        "acodec": "aac",
                        "height": 720,
                        "filesize": 100_000_000,
                    }
                ]
            }
        )
        self.assertTrue(any(c["kind"] == "video" for c in choices))

    def test_public_channel_can_be_connected_as_source(self):
        cid = database.execute(
            "INSERT INTO channels(telegram_chat_id,title,chat_type,owner_telegram_id,created_at,updated_at) VALUES(-1001,'Target','channel',42,?,?)",
            (timeutils.iso(), timeutils.iso()),
        ).lastrowid
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            text="https://t.me/s/example/12",
            forward_origin=None,
            answer=AsyncMock(),
        )
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={"source_target": cid}), clear=AsyncMock()
        )
        api = SimpleNamespace(
            get_chat=AsyncMock(
                return_value=SimpleNamespace(
                    id=-1002, type="channel", username="example", title="Source"
                )
            ),
            get_me=AsyncMock(return_value=SimpleNamespace(id=99)),
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
            ),
        )
        asyncio.run(features_sources.source_value(message, state, api))
        api.get_chat.assert_awaited_once_with("@example")
        self.assertEqual(
            database.one("SELECT source_chat_id FROM post_sources")["source_chat_id"],
            -1002,
        )
        self.assertEqual(
            database.one("SELECT channel_id FROM source_targets")["channel_id"], cid
        )

    def test_legacy_source_constraints_preserve_destinations_and_seen(self):
        database.db.executescript("""
            DROP TABLE post_sources;
            DROP TABLE source_targets;
            CREATE TABLE post_sources(id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_telegram_id INTEGER NOT NULL,source_chat_id INTEGER NOT NULL,
                source_title TEXT,active INTEGER DEFAULT 1,created_at TEXT NOT NULL);
            CREATE TABLE source_targets(id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,channel_id INTEGER NOT NULL);
            INSERT INTO post_sources VALUES(1,42,-1002,'Source',0,'date');
            INSERT INTO post_sources VALUES(2,42,-1002,'Source',1,'date');
            INSERT INTO source_targets(source_id,channel_id) VALUES(1,10),(2,10),(2,11),(2,11);
            INSERT INTO source_seen VALUES(2,'message:1',99);
        """)
        database.init_db()
        database.init_db()
        self.assertTrue(
            has_unique_key(
                database.db, "post_sources", ("owner_telegram_id", "source_chat_id")
            )
        )
        self.assertTrue(
            has_unique_key(database.db, "source_targets", ("source_id", "channel_id"))
        )
        self.assertEqual(
            [
                tuple(r)
                for r in database.all_rows(
                    "SELECT source_id,channel_id FROM source_targets ORDER BY channel_id"
                )
            ],
            [(1, 10), (1, 11)],
        )
        self.assertEqual(database.one("SELECT active FROM post_sources")["active"], 1)
        self.assertEqual(
            database.one("SELECT source_id FROM source_seen")["source_id"], 1
        )
        database.execute(
            "INSERT INTO post_sources(owner_telegram_id,source_chat_id,source_title,created_at) VALUES(42,-1002,'Reconnected','date') ON CONFLICT(owner_telegram_id,source_chat_id) DO UPDATE SET active=1"
        )
        database.execute(
            "INSERT OR IGNORE INTO source_targets(source_id,channel_id) VALUES(1,10)"
        )
        self.assertEqual(database.one("SELECT COUNT(*) n FROM source_targets")["n"], 2)

    def test_successful_join_sends_confirmation_once(self):
        cid = database.execute(
            "INSERT INTO channels(telegram_chat_id,title,chat_type,owner_telegram_id,auto_requests,invite_link,created_at,updated_at) VALUES(-1001,'Private','channel',42,1,'https://t.me/+invite',?,?)",
            (timeutils.iso(), timeutils.iso()),
        ).lastrowid
        rid = database.execute(
            "INSERT INTO join_requests(telegram_user_id,channel_id,created_at,updated_at) VALUES(43,?,?,?)",
            (cid, timeutils.iso(), timeutils.iso()),
        ).lastrowid
        api = SimpleNamespace(
            approve_chat_join_request=AsyncMock(), send_message=AsyncMock()
        )
        asyncio.run(features_conditions.check_request(rid, api, True))
        asyncio.run(features_conditions.notify_join_approved(rid, api))
        self.assertEqual(
            database.one("SELECT status FROM join_requests")["status"], "approved"
        )
        self.assertEqual(api.send_message.await_count, 1)
        self.assertIn("одобрена", api.send_message.await_args.args[1])
        callback = SimpleNamespace(
            data=f"joincheck:{rid}",
            from_user=SimpleNamespace(id=43),
            answer=AsyncMock(),
        )
        asyncio.run(features_conditions.join_recheck(callback, api))
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])

    def test_recheck_updates_original_message_when_new_dm_is_forbidden(self):
        from aiogram.exceptions import TelegramForbiddenError

        cid = database.execute(
            "INSERT INTO channels(telegram_chat_id,title,chat_type,owner_telegram_id,auto_requests,created_at,updated_at) VALUES(-1001,'Private','channel',42,1,?,?)",
            (timeutils.iso(), timeutils.iso()),
        ).lastrowid
        rid = database.execute(
            "INSERT INTO join_requests(telegram_user_id,channel_id,created_at,updated_at) VALUES(43,?,?,?)",
            (cid, timeutils.iso(), timeutils.iso()),
        ).lastrowid
        api = SimpleNamespace(
            approve_chat_join_request=AsyncMock(),
            send_message=AsyncMock(
                side_effect=TelegramForbiddenError(method=None, message="Cannot send")
            ),
        )
        callback = SimpleNamespace(
            data=f"joincheck:{rid}",
            from_user=SimpleNamespace(id=43),
            answer=AsyncMock(),
        )
        with patch.object(ui, "edit", new=AsyncMock()) as edit:
            asyncio.run(features_conditions.join_recheck(callback, api))
            self.assertIn("одобрена", edit.await_args.args[1])
        self.assertIsNotNone(
            database.one("SELECT approval_notified_at FROM join_requests")[
                "approval_notified_at"
            ]
        )


class ApplicationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_and_shutdown_close_jobs_before_telegram(self):
        from unittest.mock import Mock

        from app import application

        previous = database.db
        database.db = None
        events = []

        async def close_jobs():
            events.append("jobs")

        async def close_session():
            events.append("telegram")

        client = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(username="offline")),
            session=SimpleNamespace(close=AsyncMock(side_effect=close_session)),
        )
        dispatcher = SimpleNamespace(
            include_router=Mock(),
            resolve_used_update_types=Mock(return_value=["message"]),
            start_polling=AsyncMock(),
        )
        scheduler = SimpleNamespace(
            add_job=Mock(), start=Mock(), shutdown=Mock(), running=True
        )
        try:
            with (
                patch.object(application, "validate_config"),
                patch.object(application, "Bot", return_value=client),
                patch.object(application, "Dispatcher", return_value=dispatcher),
                patch.object(application, "AsyncIOScheduler", return_value=scheduler),
                patch.object(application, "build_router", return_value=object()),
                patch.object(
                    download_jobs, "close", new=AsyncMock(side_effect=close_jobs)
                ),
            ):
                await application.main()
            dispatcher.start_polling.assert_awaited_once()
            self.assertEqual(events, ["jobs", "telegram"])
            self.assertIsNone(database.db)
            self.assertEqual(scheduler.add_job.call_count, 8)
        finally:
            if database.db is not None:
                database.db.close()
            database.db = previous


class BackgroundJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_returns_while_work_is_running_and_cancellation_cleans_up(self):
        from services.jobs import BackgroundJobs

        jobs = BackgroundJobs()
        entered = asyncio.Event()
        finished = asyncio.Event()

        async def slow():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        task = jobs.start(42, slow, "Downloading")
        await asyncio.wait_for(entered.wait(), 1)
        self.assertTrue(jobs.active(42))
        with self.assertRaises(ValueError):
            jobs.start(42, slow, "Duplicate")
        self.assertTrue(jobs.cancel(42))
        self.assertTrue(jobs.cancel(42))
        self.assertEqual(task.cancelling(), 1)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertTrue(finished.is_set())
        self.assertFalse(jobs.active(42))
        await jobs.close()


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_user_can_open_menu_during_download(self):
        from aiogram import Bot, Dispatcher
        from aiogram.client.session.base import BaseSession
        from aiogram.fsm.storage.memory import SimpleEventIsolation
        from aiogram.types import CallbackQuery, Chat, Message, Update, User

        from app.routing import build_router
        from app.storage import SQLiteStorage

        class OfflineSession(BaseSession):
            async def close(self):
                pass

            async def make_request(self, bot, method, timeout=None):
                if method.__api_method__ == "answerCallbackQuery":
                    return True
                return Message(
                    message_id=10,
                    date=timeutils.now(),
                    chat=Chat(id=42, type="private"),
                    text="reply",
                )

            async def stream_content(self, *args, **kwargs):
                yield b""

        old = database.db
        database.db = sqlite3.connect(":memory:")
        database.db.row_factory = sqlite3.Row
        database.init_db()
        user = User(id=42, is_bot=False, first_name="Test")
        accounts.ensure_user(user)
        info = {
            "backend": "yt",
            "title": "Video",
            "choices": [{"format": "best", "kind": "video"}],
        }
        did = database.execute(
            "INSERT INTO downloads(owner_id,url,info_json,status,created_at) VALUES(?,?,?,?,?)",
            (
                42,
                "https://example.org/video",
                json.dumps(info),
                "ready",
                timeutils.iso(),
            ),
        ).lastrowid
        client = Bot(token="12345:" + "A" * 35, session=OfflineSession())
        dp = Dispatcher(
            storage=SQLiteStorage(), events_isolation=SimpleEventIsolation()
        )
        dp.include_router(build_router())
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_worker(action, url, folder, *args):
            entered.set()
            await release.wait()
            path = folder / "video.mp4"
            path.write_bytes(b"fake")
            return {"files": [str(path.resolve())]}

        def update(number, data):
            message = Message(
                message_id=number,
                date=timeutils.now(),
                chat=Chat(id=42, type="private"),
                text="menu",
            )
            return Update(
                update_id=number,
                callback_query=CallbackQuery(
                    id=str(number),
                    from_user=user,
                    chat_instance="test",
                    message=message,
                    data=data,
                ),
            )

        try:
            with (
                patch.object(
                    downloader, "run_download_worker", side_effect=slow_worker
                ),
                patch.object(
                    features_downloads,
                    "send_downloaded_file",
                    new=AsyncMock(return_value=SimpleNamespace(message_id=12)),
                ),
                patch.object(
                    content,
                    "message_payload",
                    return_value={"content_type": "video", "file_id": "file"},
                ),
                patch.object(ui, "send_target_picker", new=AsyncMock()),
                patch.object(ui, "edit", new=AsyncMock()) as edit,
            ):
                await asyncio.wait_for(
                    dp.feed_update(client, update(1, f"dl:{did}:0")), 1
                )
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.wait_for(
                    dp.feed_update(client, update(2, "menu:main")), 1
                )
                self.assertTrue(download_jobs.active(42))
                edit.assert_awaited()
                release.set()
                await asyncio.wait_for(download_jobs.tasks[42], 1)
                self.assertEqual(
                    database.one("SELECT status FROM downloads")["status"], "done"
                )
        finally:
            release.set()
            if download_jobs.active(42):
                download_jobs.cancel(42)
                await asyncio.gather(download_jobs.tasks[42], return_exceptions=True)
            await dp.storage.close()
            await client.session.close()
            database.db.close()
            database.db = old


if __name__ == "__main__":
    unittest.main()
