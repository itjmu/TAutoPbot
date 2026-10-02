import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.types import Message

from app import content, ui
from app import database as db
from app.features import channels, posts
from app.features import published_editor as live
from services import forum_topics


class GroupWorkflowTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def group(self):
        db.execute(
            "UPDATE channels SET chat_type='supergroup',username='group' WHERE id=?",
            (self.cid,),
        )
        self.bot.get_chat = AsyncMock(
            return_value=SimpleNamespace(
                id=-1001,
                type="supergroup",
                title="Group",
                username="group",
                is_forum=True,
            )
        )
        self.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status="administrator")
        )
        return content.create_draft(
            42, {"content_type": "text", "text": "Original"}, [self.cid]
        )

    async def test_topic_names_updates_and_picker(self):
        pid = self.group()
        message = Message(
            message_id=25,
            date=0,
            chat=dict(id=-1001, type="supergroup", title="Group", is_forum=True),
            message_thread_id=25,
            forum_topic_created=dict(name="News", icon_color=1),
        )
        forum_topics.observe(message)
        forum_topics.observe(
            message.model_copy(
                update={
                    "forum_topic_created": None,
                    "forum_topic_edited": SimpleNamespace(name="Updates"),
                }
            )
        )
        callback = SimpleNamespace(from_user=SimpleNamespace(id=42))
        with patch.object(ui, "edit", new=AsyncMock()) as edit:
            await posts.topic_picker(callback, self.state, self.bot, pid, self.cid)
        self.assertIn("Updates", str(edit.await_args.args[2]))
        db.execute(
            "UPDATE post_targets SET message_thread_id=25 WHERE post_id=?", (pid,)
        )
        self.assertIn("Updates", str(ui.target_markup(42, pid)))
        forum_topics.observe(
            message.model_copy(
                update={
                    "forum_topic_created": None,
                    "forum_topic_closed": SimpleNamespace(),
                }
            )
        )
        self.assertEqual(forum_topics.known(-1001)[0]["closed"], 1)
        forum_topics.observe(
            message.model_copy(
                update={
                    "forum_topic_created": None,
                    "forum_topic_reopened": SimpleNamespace(),
                }
            )
        )
        self.assertEqual(forum_topics.known(-1001)[0]["closed"], 0)

    async def test_manual_name_persisted_with_chat_and_topic_id(self):
        pid = self.group()
        await self.state.update_data(post_id=pid, topic_channel_id=self.cid)
        message = SimpleNamespace(text="25 | News", from_user=SimpleNamespace(id=42))
        with patch.object(ui, "send_target_picker", new=AsyncMock()):
            await posts.topic_input(message, self.state, self.bot)
        row = forum_topics.known(-1001)[0]
        self.assertEqual(
            (row["chat_id"], row["topic_id"], row["name"]), (-1001, 25, "News")
        )

    async def test_topic_control_only_for_forum_groups(self):
        pid = self.group()
        await ui.refresh_target_forums(self.bot, pid)
        self.assertIn(f"p:{pid}:topic:{self.cid}", str(ui.target_markup(42, pid)))
        self.bot.get_chat.return_value.is_forum = False
        await ui.refresh_target_forums(self.bot, pid)
        self.assertNotIn(f"p:{pid}:topic:{self.cid}", str(ui.target_markup(42, pid)))
        self.assertNotIn("Общая", str(ui.target_markup(42, pid)))

    async def test_published_group_topic_edit_keeps_exact_message(self):
        pid = self.group()
        db.execute(
            "UPDATE post_targets SET message_thread_id=25 WHERE post_id=?", (pid,)
        )
        await posts.execute_publish(pid, self.bot)
        m = Message(
            message_id=12,
            date=0,
            chat=dict(id=42, type="private"),
            from_user=dict(id=42, is_bot=False, first_name="U"),
            text="https://t.me/c/1/25/77",
        )
        with patch.object(
            content,
            "send_content",
            new=AsyncMock(return_value=SimpleNamespace(message_id=901)),
        ):
            await live.original(m, self.state, self.bot)
        data = await self.state.get_data()
        data["payload"]["text"] = "Edited"
        self.bot.send_message.reset_mock()
        await live.apply_edit(self.bot, 42, data)
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["chat_id"], -1001)
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], 77)
        self.bot.send_message.assert_not_awaited()
        origin, payload = live.original_link(m)
        self.assertEqual(payload["text"], "Edited")
        with self.assertRaises(ValueError):
            live.original_link(
                m.model_copy(update={"from_user": SimpleNamespace(id=43)})
            )

    async def test_connection_notices_deduplicated_under_race(self):
        chat = SimpleNamespace(id=-1001, title="Group")
        await asyncio.gather(
            channels.connection_notice(self.bot, 42, chat),
            channels.connection_notice(self.bot, 42, chat),
        )
        self.bot.send_message.assert_awaited_once()

    async def test_basic_group_opens_from_recent_publications(self):
        pid = self.group()
        db.execute(
            "UPDATE channels SET chat_type='group',telegram_chat_id=-123 WHERE id=?",
            (self.cid,),
        )
        self.bot.get_chat.return_value.id = -123
        self.bot.get_chat.return_value.type = "group"
        await posts.execute_publish(pid, self.bot)
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42), data="live:recent:0", answer=AsyncMock()
        )
        with patch.object(ui, "edit", new=AsyncMock()) as edit:
            await live.action(callback, self.state, self.bot)
            self.assertIn(f"live:open:{self.cid}:77", str(edit.await_args.args[2]))
        callback.data = f"live:open:{self.cid}:77"
        with patch.object(
            content,
            "send_content",
            new=AsyncMock(return_value=SimpleNamespace(message_id=901)),
        ):
            await live.action(callback, self.state, self.bot)
        self.assertEqual((await self.state.get_data())["chat_id"], -123)
        with self.assertRaises(ValueError):
            live.saved_original(43, self.cid, 77)

    async def test_group_album_link_edits_only_selected_element(self):
        pid = self.group()
        album = dict(
            content_type="album",
            media_json=json.dumps(
                [
                    dict(content_type="photo", file_id="a", text="First"),
                    dict(content_type="photo", file_id="b", text="Second"),
                ]
            ),
        )
        db.execute(
            "UPDATE posts SET content_type='album',media_json=? WHERE id=?",
            (album["media_json"], pid),
        )
        self.bot.send_media_group = AsyncMock(
            return_value=[
                SimpleNamespace(message_id=201),
                SimpleNamespace(message_id=202),
            ]
        )
        await posts.execute_publish(pid, self.bot)
        m = Message(
            message_id=12,
            date=0,
            chat=dict(id=42, type="private"),
            from_user=dict(id=42, is_bot=False, first_name="U"),
            text="https://t.me/group/25/202",
        )
        with patch.object(
            content,
            "send_content",
            new=AsyncMock(return_value=SimpleNamespace(message_id=901)),
        ):
            await live.original(m, self.state, self.bot)
        data = await self.state.get_data()
        self.assertTrue(data["album"])
        self.assertEqual(data["payload"]["file_id"], "b")
        data["payload"]["text"] = "Updated caption"
        await live.apply_edit(self.bot, 42, data)
        self.assertEqual(
            self.bot.edit_message_caption.await_args.kwargs["message_id"], 202
        )

    async def test_join_cleanup_only_removes_retired_controls(self):
        await ui.track_controls(self.bot, 42, "join:1", SimpleNamespace(message_id=80))
        await ui.track_controls(self.bot, 42, "join:2", SimpleNamespace(message_id=90))
        await ui.retire_controls(self.bot, 42, "join:1")
        await ui.cleanup_home(self.bot, 42)
        self.bot.delete_message.assert_awaited_once_with(42, 80)
        self.assertTrue(db.setting("ui:controls:42:join:2"))

    async def test_home_cleanup_preserves_downloads_and_current_panel(self):
        ui.remember_panel(42, 100)
        ui.note_transient(
            42, [SimpleNamespace(message_id=80), SimpleNamespace(message_id=100)]
        )
        await ui.track_controls(self.bot, 42, "post:1", SimpleNamespace(message_id=90))
        await ui.track_controls(
            self.bot, 42, "download:1", SimpleNamespace(message_id=95)
        )
        await ui.cleanup_home(self.bot, 42)
        self.assertEqual(
            {call.args[1] for call in self.bot.delete_message.await_args_list}, {80, 90}
        )
        self.assertEqual(json.loads(db.setting("ui:controls:42:download:1")), [95])
        self.assertEqual(ui.panel_id(42), 100)

    def test_album_and_video_note_snapshots_are_message_specific(self):
        album = dict(
            content_type="album",
            media_json=[
                dict(content_type="photo", file_id="a", text="Caption"),
                dict(content_type="video", file_id="b"),
            ],
        )
        self.assertEqual(content.delivered_payload(album, 0)["file_id"], "a")
        self.assertTrue(content.delivered_payload(album, 1)["album"])
        self.assertEqual(content.delivered_payload(album, 2)["content_type"], "text")
        note = dict(
            content_type="video_note", text="Description", caption_entities_json="[]"
        )
        self.assertEqual(content.delivered_payload(note, 1)["text"], "Description")


if __name__ == "__main__":
    unittest.main()
