import json
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.types import Chat, Message, User

from app import content, timeutils, ui
from app import database as db
from app.features import audio, conditions, posts
from app.states import AudioEdit
from services import audio_files
from services.media import run_ffmpeg


class AudioEditorTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def message(self, **values):
        return Message(
            message_id=50,
            date=timeutils.now(),
            chat=Chat(id=42, type="private"),
            from_user=User(id=42, is_bot=False, first_name="Test"),
            **values,
        )

    def callback(self, data, uid=42):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=uid),
            message=self.message(),
            answer=AsyncMock(),
        )

    def draft(self, typ="audio", extension="mp3"):
        payload = dict(
            content_type=typ,
            file_id="original",
            text="Caption",
            audio_meta_json=json.dumps(
                dict(
                    editor=True,
                    source_type=typ,
                    format=extension,
                    filename="original." + extension,
                    changes={},
                )
            ),
        )
        return content.create_draft(42, payload, [self.cid])

    async def test_main_menu_replaces_conditions_only(self):
        buttons = [button for row in ui.main_kb().inline_keyboard for button in row]
        self.assertIn(
            ("✍🎶 Аудио 🎧", "menu:audio"), [(b.text, b.callback_data) for b in buttons]
        )
        self.assertNotIn("menu:conditions", [b.callback_data for b in buttons])
        with (
            patch.object(conditions.access, "access_callback", return_value=True),
            patch.object(ui, "edit", new=AsyncMock()),
        ):
            await conditions.menu_conditions(self.callback("menu:conditions"))

    async def test_standalone_intake_without_posts_or_channels(self):
        from app.features import audio_utility
        from app.states import AudioUtility

        count = db.one("SELECT COUNT(*) AS n FROM posts")["n"]
        with patch.object(ui, "edit", new=AsyncMock()):
            await audio.menu(self.callback("menu:audio"), self.state)
        self.assertEqual(await self.state.get_state(), AudioEdit.content.state)
        message = self.message(
            audio=dict(
                file_id="track",
                file_unique_id="track",
                duration=10,
                file_size=1024,
                file_name="track.mp3",
                title="Song",
                performer="Artist",
                mime_type="audio/mpeg",
            )
        )
        with patch.object(audio_utility, "panel", new=AsyncMock()):
            await audio.receive(message, self.state, self.bot)
        data = await self.state.get_data()
        self.assertEqual(await self.state.get_state(), AudioUtility.idle.state)
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM posts")["n"], count)
        self.assertEqual(data["utility_payload"]["file_id"], "track")
        buttons = [
            b.callback_data
            for row in audio_utility.keyboard(
                data["utility_token"], data["utility_payload"]
            ).inline_keyboard
            for b in row
        ]
        self.assertFalse(any(b.startswith(("p:", "audio:target:")) for b in buttons))

    async def test_format_specific_tools_and_document_upload(self):
        for typ, extension, expected in [
            ("voice", "ogg", set()),
            ("document", "flac", {"filename"}),
            ("audio", "m4a", set(audio.labels()) | {"cover"}),
        ]:
            pid = self.draft(typ, extension)
            post = content.post_owned(pid, 42)
            self.assertEqual(audio.fields(post, audio.metadata(dict(post))), expected)
        payload = audio.audio_payload(
            self.message(
                document=dict(
                    file_id="doc",
                    file_unique_id="doc",
                    file_name="track.mp3",
                    mime_type="audio/mpeg",
                )
            )
        )
        self.assertEqual(payload["content_type"], "audio")
        self.assertTrue(json.loads(payload["audio_meta_json"])["process"])
        with self.assertRaises(ValueError):
            audio.audio_payload(
                self.message(
                    document=dict(
                        file_id="doc", file_unique_id="doc", file_name="other.pdf"
                    )
                )
            )

    async def test_ownership_and_unsupported_tools_rejected(self):
        pid = self.draft()
        with self.assertRaises(ValueError):
            await audio.action(
                self.callback(f"ae:{pid}:title", uid=43), self.state, self.bot
            )
        with self.assertRaises(ValueError):
            await audio.target(
                self.callback(f"audio:target:{self.cid}", uid=43), self.state
            )
        pid = self.draft("voice", "ogg")
        with self.assertRaises(ValueError):
            await audio.action(self.callback(f"ae:{pid}:cover"), self.state, self.bot)

    async def test_metadata_edit_invalidates_cache_and_cancels_schedule(self):
        pid = self.draft()
        options = audio.metadata(dict(content.post_owned(pid, 42)))
        options["prepared_file_id"] = "cached"
        audio.save(pid, options)
        posts.schedule_post(pid, 42, timeutils.now() + timedelta(hours=1))
        with (
            patch.object(ui, "edit", new=AsyncMock()),
            patch.object(ui, "answer", new=AsyncMock()),
            patch.object(posts, "show_post", new=AsyncMock()),
        ):
            await audio.action(self.callback(f"ae:{pid}:title"), self.state, self.bot)
            await audio.value(self.message(text="New title"), self.state, self.bot)
        post = content.post_owned(pid, 42)
        options = audio.metadata(dict(post))
        self.assertEqual(post["file_id"], "original")
        self.assertEqual(post["status"], "draft")
        self.assertEqual(options["changes"]["title"], "New title")
        self.assertNotIn("prepared_file_id", options)
        self.assertFalse(
            db.one("SELECT active FROM scheduled_posts WHERE post_id=?", (pid,))[0]
        )

    async def test_cover_filename_validation_and_reset(self):
        pid = self.draft()
        with (
            patch.object(ui, "edit", new=AsyncMock()),
            patch.object(posts, "show_post", new=AsyncMock()),
        ):
            await audio.action(
                self.callback(f"ae:{pid}:filename"), self.state, self.bot
            )
            for text in ("../escape.mp3", "track.wav", "-"):
                with self.assertRaises(ValueError):
                    await audio.value(self.message(text=text), self.state, self.bot)
            await audio.value(self.message(text="Renamed"), self.state, self.bot)
            self.assertEqual(
                audio.metadata(dict(content.post_owned(pid, 42)))["changes"][
                    "filename"
                ],
                "Renamed.mp3",
            )
            await audio.action(self.callback(f"ae:{pid}:cover"), self.state, self.bot)
            await audio.value(
                self.message(
                    photo=[
                        dict(
                            file_id="cover",
                            file_unique_id="cover",
                            width=100,
                            height=100,
                            file_size=100,
                        )
                    ]
                ),
                self.state,
                self.bot,
            )
            self.assertEqual(
                audio.metadata(dict(content.post_owned(pid, 42)))["cover_file_id"],
                "cover",
            )
            await audio.action(self.callback(f"ae:{pid}:reset"), self.state, self.bot)
            options = audio.metadata(dict(content.post_owned(pid, 42)))
            self.assertEqual(options["changes"], {})
            self.assertNotIn("cover_file_id", options)
            self.assertFalse(options["process"])

    async def test_preview_caches_upload_without_overwriting_source(self):
        pid = self.draft()
        options = audio.metadata(dict(content.post_owned(pid, 42)))
        options.update(process=True, changes={"title": "Edited"})
        audio.save(pid, options)

        async def send(bot, chat_id, payload, markup, **kwargs):
            payload["file_id"] = "prepared"
            options = audio.metadata(payload)
            options.update(prepared_file_id="prepared", process=False)
            payload["audio_meta_json"] = json.dumps(options)
            return SimpleNamespace(message_id=77)

        with patch.object(audio_files, "send_prepared", side_effect=send):
            await posts.show_post(self.bot, 42, pid)
        post = content.post_owned(pid, 42)
        self.assertEqual(post["file_id"], "original")
        self.assertEqual(audio.metadata(dict(post))["prepared_file_id"], "prepared")

    async def test_condition_created_from_card_is_assigned_to_only_that_chat(self):
        callback = self.callback(f"cond:create:{self.cid}")
        with (
            patch.object(ui, "edit", new=AsyncMock()) as edit,
            patch.object(ui, "answer", new=AsyncMock()),
        ):
            await conditions.channel_condition(
                self.callback(f"channel:cond:{self.cid}")
            )
            self.assertIn(
                callback.data,
                [
                    b.callback_data
                    for row in edit.call_args.args[2].inline_keyboard
                    for b in row
                ],
            )
            await conditions.cond_create(callback, self.state)
            data = await self.state.get_data()
            self.assertEqual(data["target_channel"], self.cid)
            await conditions.condition_value(
                self.message(text="Name"), self.state, self.bot, "Name", 42, data
            )
            data = await self.state.get_data()
            await conditions.condition_value(
                self.message(), self.state, self.bot, "math_captcha", 42, data
            )
        condition_id = db.one(
            "SELECT condition_id FROM channels WHERE id=?", (self.cid,)
        )[0]
        self.assertIsNotNone(condition_id)
        self.assertEqual(
            db.one(
                "SELECT owner_telegram_id FROM conditions WHERE id=?", (condition_id,)
            )[0],
            42,
        )
        self.assertEqual(
            db.one(
                "SELECT item_type FROM condition_items WHERE condition_id=?",
                (condition_id,),
            )[0],
            "math_captcha",
        )
        with self.assertRaises(ValueError):
            await conditions.cond_create(
                self.callback(f"cond:create:{self.cid}", uid=43), self.state
            )


class AudioProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_media_preserves_topic_protection_and_metadata(self):
        payload = dict(
            content_type="audio",
            file_id="original",
            protect_content=True,
            audio_meta_json=json.dumps(
                dict(
                    prepared_file_id="edited",
                    changes={"title": "Title", "performer": "Artist"},
                )
            ),
        )
        bot = SimpleNamespace(
            send_audio=AsyncMock(return_value=SimpleNamespace(message_id=1))
        )
        with patch.object(audio_files, "download", new=AsyncMock()) as download:
            await content.send_content(bot, 42, payload, message_thread_id=7)
        download.assert_not_awaited()
        self.assertEqual(bot.send_audio.call_args.args[1], "edited")
        self.assertEqual(bot.send_audio.call_args.kwargs["message_thread_id"], 7)
        self.assertTrue(bot.send_audio.call_args.kwargs["protect_content"])
        self.assertEqual(bot.send_audio.call_args.kwargs["title"], "Title")
        self.assertEqual(content.delivered_payload(payload, 0)["file_id"], "edited")

    async def test_preparation_failure_never_sends(self):
        payload = dict(
            content_type="audio",
            file_id="original",
            audio_meta_json=json.dumps(
                dict(process=True, format="mp3", changes={"title": "Title"})
            ),
        )
        bot = SimpleNamespace(send_audio=AsyncMock())
        with patch.object(
            audio_files, "download", new=AsyncMock(side_effect=RuntimeError("failed"))
        ):
            with self.assertRaises(ValueError):
                await content.send_content(bot, 42, payload)
        bot.send_audio.assert_not_awaited()
        self.assertEqual(payload["file_id"], "original")

    async def test_real_mp3_m4a_tags_cover_and_unchanged_audio_packets(self):
        from app.downloader import ffmpeg_path

        executable = ffmpeg_path()

        def command(*args):
            return subprocess.run(
                [executable, "-v", "error", "-i", *map(str, args)],
                capture_output=True,
                check=True,
            ).stdout

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cover = root / "cover.jpg"
            run_ffmpeg(
                executable,
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=red:s=100x100",
                    "-frames:v",
                    "1",
                    str(cover),
                ],
            )
            for extension, codec in [("mp3", "libmp3lame"), ("m4a", "aac")]:
                source, output = (
                    root / f"source.{extension}",
                    root / f"output.{extension}",
                )
                run_ffmpeg(
                    executable,
                    [
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440",
                        "-t",
                        "0.3",
                        "-c:a",
                        codec,
                        "-metadata",
                        "genre=Original",
                        str(source),
                    ],
                )
                await audio_files.prepare(
                    source,
                    output,
                    dict(
                        format=extension,
                        changes={
                            "title": "New title",
                            "performer": "Artist",
                            "album": "Album",
                        },
                    ),
                    cover,
                )
                tags = command(output, "-f", "ffmetadata", "pipe:1").decode()
                self.assertIn("title=New title", tags)
                self.assertIn("artist=Artist", tags)
                self.assertIn("album=Album", tags)
                self.assertIn("genre=Original", tags)
                digest_args = (
                    "-map",
                    "0:a:0",
                    "-c:a",
                    "copy",
                    "-f",
                    "hash",
                    "-hash",
                    "sha256",
                    "pipe:1",
                )
                self.assertEqual(
                    command(source, *digest_args), command(output, *digest_args)
                )
                image = command(
                    output,
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "pipe:1",
                )
                self.assertTrue(image.startswith(b"\xff\xd8"))
