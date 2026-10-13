import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.types import FSInputFile

from app import content
from app import database as db
from app.features import editors, posts
from services import template_logos as logos
from services.media import run_ffmpeg


class TemplateTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.PlanningTests.setUp
    tearDown = fixtures.PlanningTests.tearDown

    def template(self):
        return db.execute(
            "INSERT INTO templates(channel_id,owner_id,name,before_html,logo_file_id) VALUES(?,42,'Brand','Header','logo')",
            (self.cid,),
        ).lastrowid

    def callback(self, data):
        return SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=42), answer=AsyncMock()
        )

    async def test_legacy_template_migration_preserves_automatic_default(self):
        tid = self.template()
        db.execute(
            "UPDATE channels SET default_template_id=? WHERE id=?", (tid, self.cid)
        )
        for column in ("logo_file_id", "logo_position", "logo_scale"):
            db.execute(f"ALTER TABLE templates DROP COLUMN {column}")
        db.init_db()
        db.init_db()
        template = db.one("SELECT * FROM templates WHERE id=?", (tid,))
        self.assertEqual(template["name"], "Brand")
        self.assertEqual(template["before_html"], "Header")
        self.assertIsNone(template["logo_file_id"])
        self.assertEqual(template["logo_scale"], 20)
        self.assertEqual(
            db.one("SELECT default_template_id FROM channels WHERE id=?", (self.cid,))[
                0
            ],
            tid,
        )

    async def test_template_cover_fallback_override_and_removal(self):
        tid = self.template()
        db.execute(
            "UPDATE templates SET video_cover_file_id='template-cover' WHERE id=?",
            (tid,),
        )
        channel = db.one("SELECT * FROM channels WHERE id=?", (self.cid,))
        pid = content.create_draft(
            42,
            dict(content_type="video", file_id="large-video", text="Body"),
            [self.cid],
        )
        post = dict(db.one("SELECT * FROM posts WHERE id=?", (pid,)))
        self.assertEqual(
            content.render_payload(post, channel, tid)["cover_file_id"],
            "template-cover",
        )
        self.assertFalse(content.render_payload(post, channel, -1).get("cover_file_id"))
        post["cover_file_id"] = "personal-cover"
        self.assertEqual(
            content.render_payload(post, channel, tid)["cover_file_id"],
            "personal-cover",
        )
        post.update(cover_file_id=None, cover_disabled=1)
        self.assertFalse(
            content.render_payload(post, channel, tid).get("cover_file_id")
        )
        post.update(
            content_type="album",
            media_json=json.dumps(
                [
                    dict(content_type="video", file_id="one"),
                    dict(content_type="video", file_id="two", cover_file_id="own"),
                    dict(content_type="video", file_id="three", cover_disabled=True),
                    dict(content_type="photo", file_id="photo"),
                ]
            ),
        )
        items = content.render_payload(post, channel, tid)["media_json"]
        self.assertEqual(items[0]["cover_file_id"], "template-cover")
        self.assertEqual(items[1]["cover_file_id"], "own")
        self.assertNotIn("cover_file_id", items[2])
        self.assertNotIn("cover_file_id", items[3])

    async def test_precise_coordinates_size_and_movement_bounds(self):
        tid = self.template()
        message = SimpleNamespace()
        with (
            patch.object(editors.ui, "edit", new=AsyncMock()),
            patch.object(editors.ui, "answer", new=AsyncMock()),
        ):
            await editors.template_action(
                self.callback(f"tpl:{tid}:logo_coordinates"), self.state
            )
            with self.assertRaises(ValueError):
                await editors.guided_value(message, self.state, self.bot, "101 50", 42)
            await editors.guided_value(message, self.state, self.bot, "45 60", 42)
            await editors.template_action(
                self.callback(f"tpl:{tid}:logo_move_left"), self.state
            )
            template = db.one("SELECT * FROM templates WHERE id=?", (tid,))
            self.assertEqual((template["logo_x"], template["logo_y"]), (40, 60))
            await editors.template_action(
                self.callback(f"tpl:{tid}:logo_scale"), self.state
            )
            with self.assertRaises(ValueError):
                await editors.guided_value(message, self.state, self.bot, "0", 42)
            await editors.guided_value(message, self.state, self.bot, "37", 42)
            self.assertEqual(
                db.one("SELECT logo_scale FROM templates WHERE id=?", (tid,))[0], 37
            )
            db.execute("UPDATE templates SET logo_x=0,logo_y=100 WHERE id=?", (tid,))
            await editors.template_action(
                self.callback(f"tpl:{tid}:logo_move_left"), self.state
            )
            await editors.template_action(
                self.callback(f"tpl:{tid}:logo_move_down"), self.state
            )
            template = db.one("SELECT * FROM templates WHERE id=?", (tid,))
            self.assertEqual((template["logo_x"], template["logo_y"]), (0, 100))

    async def test_template_cover_upload_and_clear(self):
        tid = self.template()
        await self.state.update_data(kind="template_cover", tid=tid)
        message = SimpleNamespace(
            photo=[SimpleNamespace(file_id="cover", file_size=100)],
            document=None,
            from_user=SimpleNamespace(id=42),
        )
        with (
            patch.object(editors.ui, "answer", new=AsyncMock()),
            patch.object(editors.ui, "edit", new=AsyncMock()),
        ):
            await editors.template_logo_input(message, self.state)
            self.assertEqual(
                db.one("SELECT video_cover_file_id FROM templates WHERE id=?", (tid,))[
                    0
                ],
                "cover",
            )
            await editors.template_action(
                self.callback(f"tpl:{tid}:video_cover_clear"), self.state
            )
            template = db.one("SELECT * FROM templates WHERE id=?", (tid,))
            self.assertIsNone(template["video_cover_file_id"])
            self.assertEqual(template["logo_file_id"], "logo")

    async def test_logo_controls_and_removal_preserve_template_text(self):
        tid = self.template()
        with patch.object(editors.ui, "edit", new=AsyncMock()):
            for action in ("logo_at_bottom_left", "logo_larger", "logo_clear"):
                await editors.template_action(
                    self.callback(f"tpl:{tid}:{action}"), self.state
                )
        template = db.one("SELECT * FROM templates WHERE id=?", (tid,))
        self.assertEqual(template["logo_position"], "bottom_left")
        self.assertEqual(template["logo_scale"], 25)
        self.assertIsNone(template["logo_file_id"])
        self.assertEqual(template["before_html"], "Header")

    async def test_automatic_toggle_preserves_template_and_manual_choices(self):
        tid = self.template()
        with patch.object(editors.ui, "edit", new=AsyncMock()):
            await editors.template_action(
                self.callback(f"tpl:{tid}:automatic"), self.state
            )
            channel = db.one("SELECT * FROM channels WHERE id=?", (self.cid,))
            pid = content.create_draft(
                42,
                {"content_type": "photo", "file_id": "original", "text": "Body"},
                [self.cid],
            )
            post = db.one("SELECT * FROM posts WHERE id=?", (pid,))
            automatic = content.render_payload(post, channel)
            self.assertEqual(automatic["text"], "Header\nBody")
            self.assertEqual(automatic["template_logo"]["file_id"], "logo")
            self.assertEqual(automatic["file_id"], "original")
            plain = content.render_payload(post, channel, -1)
            self.assertEqual(plain["text"], "Body")
            self.assertNotIn("template_logo", plain)
            await editors.template_action(
                self.callback(f"tpl:{tid}:automatic"), self.state
            )
            channel = db.one("SELECT * FROM channels WHERE id=?", (self.cid,))
            self.assertIsNone(channel["default_template_id"])
            self.assertNotIn("template_logo", content.render_payload(post, channel))
            self.assertIn("template_logo", content.render_payload(post, channel, tid))
            db.init_db()
            self.assertEqual(
                db.one("SELECT logo_file_id FROM templates WHERE id=?", (tid,))[0],
                "logo",
            )

    async def test_post_override_remove_and_switch_channel_template(self):
        first = self.template()
        second = self.template()
        db.execute("UPDATE templates SET before_html='Second' WHERE id=?", (second,))
        db.execute(
            "UPDATE channels SET default_template_id=? WHERE id=?", (first, self.cid)
        )
        pid = content.create_draft(
            42, {"content_type": "text", "text": "Body"}, [self.cid]
        )
        with patch.object(posts, "show_post", new=AsyncMock()):
            for choice, expected in [
                (-1, "Body"),
                (second, "Second\nBody"),
                (0, "Header\nBody"),
            ]:
                await posts.post_action(
                    self.callback(f"p:{pid}:template:{self.cid}:{choice}"),
                    self.state,
                    self.bot,
                )
                target = posts.post_target_rows(pid)[0]
                self.assertEqual(
                    content.render_payload(
                        db.one("SELECT * FROM posts WHERE id=?", (pid,)),
                        target,
                        target["template_id"],
                    )["text"],
                    expected,
                )

    async def test_logo_upload_validation_and_ownership(self):
        tid = self.template()
        await self.state.update_data(kind="template_logo", tid=tid)
        message = SimpleNamespace(
            photo=None,
            document=SimpleNamespace(
                file_id="new-logo", file_size=100, mime_type="image/png"
            ),
            from_user=SimpleNamespace(id=42),
        )
        with patch.object(editors.ui, "answer", new=AsyncMock()):
            message.document.mime_type = "application/pdf"
            with self.assertRaises(ValueError):
                await editors.template_logo_input(message, self.state)
            message.document.mime_type = "image/png"
            message.from_user.id = 43
            with self.assertRaises(ValueError):
                await editors.template_logo_input(message, self.state)
            message.from_user.id = 42
            await editors.template_logo_input(message, self.state)
            self.assertEqual(
                db.one("SELECT logo_file_id FROM templates WHERE id=?", (tid,))[0],
                "new-logo",
            )
            self.assertIsNone(await self.state.get_state())


class LogoDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_without_cover_does_not_download_or_encode(self):
        bot = SimpleNamespace(
            send_video=AsyncMock(
                return_value=SimpleNamespace(
                    video=SimpleNamespace(file_id="large-video")
                )
            )
        )
        payload = dict(
            content_type="video",
            file_id="large-video",
            template_logo=dict(file_id="logo", position="center", scale=20),
        )
        with (
            patch.object(logos, "download", new=AsyncMock()) as download,
            patch.object(logos, "overlay", new=AsyncMock()) as overlay,
        ):
            await content.send_content(bot, 42, payload)
        download.assert_not_awaited()
        overlay.assert_not_awaited()
        self.assertEqual(bot.send_video.call_args.args[1], "large-video")

    async def test_video_cover_only_is_processed_original_video_preserved(self):
        downloaded = []

        async def download(bot, file_id, path, limit):
            downloaded.append(file_id)
            path.write_bytes(b"image")

        async def overlay(source, logo, output, typ, settings):
            self.assertEqual(typ, "photo")
            output.write_bytes(b"cover")

        async def send(chat_id, file_id, **kwargs):
            self.assertEqual(file_id, "large-video")
            self.assertIsInstance(kwargs["cover"], FSInputFile)
            self.assertEqual(Path(kwargs["cover"].path).read_bytes(), b"cover")
            return SimpleNamespace(
                video=SimpleNamespace(
                    file_id="large-video",
                    cover=[SimpleNamespace(file_id="branded-cover")],
                )
            )

        bot = SimpleNamespace(send_video=AsyncMock(side_effect=send))
        payload = dict(
            content_type="video",
            file_id="large-video",
            cover_file_id="cover",
            template_logo=dict(file_id="logo", position="center", scale=37, x=40, y=60),
        )
        with (
            patch.object(logos, "download", side_effect=download),
            patch.object(logos, "overlay", side_effect=overlay),
        ):
            await content.send_content(bot, 42, payload)
        self.assertEqual(downloaded, ["logo", "cover"])
        self.assertEqual(payload["file_id"], "large-video")
        self.assertEqual(payload["cover_file_id"], "branded-cover")
        json.dumps(payload)

    async def test_partial_album_retains_branded_message_identity(self):
        async def download(bot, file_id, path, limit):
            path.write_bytes(b"source")

        async def overlay(source, logo, output, typ, settings):
            output.write_bytes(b"branded")

        messages = [
            SimpleNamespace(
                photo=[SimpleNamespace(file_id=f"branded-{index}")], message_id=index
            )
            for index in range(2)
        ]
        bot = SimpleNamespace(
            send_media_group=AsyncMock(return_value=messages),
            send_message=AsyncMock(side_effect=RuntimeError("companion failed")),
        )
        payload = dict(
            content_type="album",
            media_json=[
                dict(content_type="photo", file_id="original") for _ in range(2)
            ],
            template_logo=dict(file_id="logo", scale=20, position="bottom_right"),
        )
        with (
            patch.object(logos, "download", side_effect=download),
            patch.object(logos, "overlay", side_effect=overlay),
        ):
            with self.assertRaises(content.PartialAlbumError) as caught:
                await content.send_content(
                    bot,
                    42,
                    payload,
                    content.build_published_markup(
                        [
                            dict(
                                id="b",
                                row=1,
                                type="url",
                                text="Link",
                                url="https://example.com",
                            )
                        ],
                        1,
                    ),
                )
        self.assertEqual(caught.exception.messages, messages)
        self.assertEqual(payload["media_json"][1]["file_id"], "branded-1")
        self.assertNotIn("template_logo", payload)
        json.dumps(payload)

    async def test_album_snapshot_protection_topic_and_temp_cleanup(self):
        paths = []

        async def download(bot, file_id, path, limit):
            path.write_bytes(b"source")

        async def overlay(source, logo, output, typ, settings):
            output.write_bytes(b"branded")

        async def send(chat_id, media, **kwargs):
            self.assertEqual(kwargs, {"protect_content": True, "message_thread_id": 7})
            self.assertEqual(media[1].media, "video")
            for file in (media[0].media, media[1].cover):
                self.assertIsInstance(file, FSInputFile)
                paths.append(Path(file.path))
                self.assertEqual(paths[-1].read_bytes(), b"branded")
            return [
                SimpleNamespace(photo=[SimpleNamespace(file_id="branded-photo")]),
                SimpleNamespace(
                    video=SimpleNamespace(
                        file_id="video",
                        cover=[SimpleNamespace(file_id="branded-cover")],
                    )
                ),
            ]

        bot = SimpleNamespace(send_media_group=AsyncMock(side_effect=send))
        original = [
            dict(content_type="photo", file_id="photo"),
            dict(content_type="video", file_id="video", cover_file_id="cover"),
        ]
        payload = dict(
            content_type="album",
            media_json=json.dumps(original),
            protect_content=True,
            template_logo=dict(file_id="logo", scale=20, position="bottom_right"),
        )
        with (
            patch.object(logos, "download", side_effect=download),
            patch.object(logos, "overlay", side_effect=overlay),
        ):
            await content.send_content(bot, -1001, payload, message_thread_id=7)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(original[0]["file_id"], "photo")
        self.assertEqual(
            content.delivered_payload(payload, 0)["file_id"], "branded-photo"
        )
        self.assertEqual(
            content.delivered_payload(payload, 1)["cover_file_id"], "branded-cover"
        )
        self.assertNotIn("template_logo", payload)
        json.dumps(payload)

    async def test_preparation_failure_sends_nothing(self):
        bot = SimpleNamespace(send_photo=AsyncMock())
        payload = dict(
            content_type="photo",
            file_id="original",
            template_logo=dict(file_id="logo", scale=20, position="bottom_right"),
        )
        with patch.object(
            logos, "download", side_effect=RuntimeError("download failed")
        ):
            with self.assertRaises(ValueError):
                await content.send_content(bot, 42, payload)
        bot.send_photo.assert_not_awaited()
        self.assertEqual(payload["file_id"], "original")

    async def test_download_size_bound(self):
        bot = SimpleNamespace(
            get_file=AsyncMock(
                return_value=SimpleNamespace(file_size=logos.SOURCE_LIMIT + 1)
            ),
            download_file=AsyncMock(),
        )
        with self.assertRaises(ValueError):
            await logos.download(bot, "id", Path("unused"), logos.SOURCE_LIMIT)
        bot.download_file.assert_not_awaited()
        with logos.LimitedBuffer(2) as buffer:
            with self.assertRaises(ValueError):
                buffer.write(b"123")

    async def test_real_photo_overlay_and_video_processing_rejected(self):
        from app.downloader import ffmpeg_path

        executable = ffmpeg_path()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            logo, photo, video = (
                root / "logo.png",
                root / "photo.jpg",
                root / "video.mp4",
            )
            for path, color, size in [
                (logo, "red", "30x30"),
                (photo, "blue", "320x240"),
            ]:
                run_ffmpeg(
                    executable,
                    [
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={color}:s={size}",
                        "-frames:v",
                        "1",
                        str(path),
                    ],
                )
            with self.assertRaises(ValueError):
                await logos.overlay(
                    video,
                    logo,
                    root / "not-created.mp4",
                    "video",
                    dict(position="center", scale=20),
                )
            self.assertFalse((root / "not-created.mp4").exists())
            for settings, red_position, blue_position in [
                (dict(position="bottom_right", scale=20), (300, 220), (0, 0)),
                (dict(position="center", scale=37, x=0, y=0), (10, 10), (300, 220)),
            ]:
                output = root / "output.jpg"
                await logos.overlay(photo, logo, output, "photo", settings)
                pixels = subprocess.run(
                    [
                        executable,
                        "-v",
                        "error",
                        "-i",
                        str(output),
                        "-frames:v",
                        "1",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "rgb24",
                        "pipe:1",
                    ],
                    check=True,
                    capture_output=True,
                ).stdout
                offset = (red_position[1] * 320 + red_position[0]) * 3
                self.assertGreater(pixels[offset], 180)
                self.assertLess(pixels[offset + 2], 80)
                blue_offset = (blue_position[1] * 320 + blue_position[0]) * 3
                self.assertGreater(pixels[blue_offset + 2], 180)
