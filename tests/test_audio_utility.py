import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_audio_editor as editor_tests

from app.features import audio
from app.features import audio_utility as utility
from services import audio_files
from services.media import run_ffmpeg


class UtilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        editor_tests.AudioEditorTests.setUp(self)
        utility.audio_jobs = utility.BackgroundJobs()

    async def asyncTearDown(self):
        await utility.audio_jobs.close()

    tearDown = editor_tests.AudioEditorTests.tearDown
    message = editor_tests.AudioEditorTests.message
    callback = editor_tests.AudioEditorTests.callback

    async def intake(self, typ="voice", size=1024):
        message = self.message(
            **{
                typ: dict(
                    file_id="original",
                    file_unique_id="original",
                    duration=3,
                    file_size=size,
                )
            }
        )
        with patch.object(utility, "panel", new=AsyncMock()):
            await utility.receive(message, self.state, self.bot)
        return await self.state.get_data()

    async def test_limits_and_stale_callbacks(self):
        await self.intake(size=utility.LIMIT)
        for size in (None, 0, utility.LIMIT + 1):
            with self.assertRaises(ValueError):
                await self.intake(size=size)
        await self.intake()
        with self.assertRaises(ValueError):
            await utility.action(
                self.callback("au:expired:result"), self.state, self.bot
            )

    async def test_presentation_is_deferred_and_unchanged_result_reuses_id(self):
        self.bot.send_audio = AsyncMock(return_value=SimpleNamespace(message_id=99))
        data = await self.intake("audio")
        token = data["utility_token"]
        with patch.object(utility, "panel", new=AsyncMock()):
            await utility.action(
                self.callback(f"au:{token}:as_voice"), self.state, self.bot
            )
            options = audio.metadata((await self.state.get_data())["utility_payload"])
            self.assertEqual(options["presentation"], "voice")
            self.assertNotIn("convert_to", options)
            self.assertFalse(utility.audio_jobs.active(42))
            await utility.action(
                self.callback(f"au:{token}:as_audio"), self.state, self.bot
            )
            self.assertFalse(
                audio.metadata((await self.state.get_data())["utility_payload"])[
                    "process"
                ]
            )
        with (
            patch.object(audio_files, "download", new=AsyncMock()) as download,
            patch.object(utility, "panel", new=AsyncMock()),
        ):
            await utility.action(
                self.callback(f"au:{token}:result"), self.state, self.bot
            )
            await utility.audio_jobs.tasks[42]
            download.assert_not_awaited()
            self.bot.send_audio.assert_awaited()
            self.assertEqual(self.bot.send_audio.call_args.args[1], "original")

    async def test_background_job_continues_after_menu_exit(self):
        import asyncio

        data = await self.intake()
        entered, release = asyncio.Event(), asyncio.Event()
        from services.event_isolation import EventIsolation

        dispatcher = SimpleNamespace(
            fsm=SimpleNamespace(events_isolation=EventIsolation())
        )

        async def prepared(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(message_id=99)

        with (
            patch.object(utility.content, "send_content", side_effect=prepared),
            patch.object(utility, "panel", new=AsyncMock()) as panel,
        ):
            await utility.action(
                self.callback(f"au:{data['utility_token']}:result"),
                self.state,
                self.bot,
                dispatcher=dispatcher,
            )
            job = utility.audio_jobs.tasks[42]
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(job.done())
            buttons = self.bot.send_message.call_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            self.assertEqual(buttons[-1][0].callback_data, "menu:main")
            await self.state.clear()
            release.set()
            await job
            panel.assert_not_awaited()
        self.assertEqual(await self.state.get_data(), {})

    async def test_conversion_unlocks_tags_preserves_original_and_reset(self):
        data = await self.intake()
        token = data["utility_token"]

        async def prepared(bot, uid, payload, markup):
            options = audio.metadata(payload)
            self.assertEqual(options["convert_to"], "mp3")
            payload.update(content_type="audio", file_id="converted")
            options.update(
                format="mp3",
                filename="voice.mp3",
                process=False,
                prepared_file_id="converted",
            )
            payload["audio_meta_json"] = json.dumps(options)

        with (
            patch.object(utility.content, "send_content", side_effect=prepared),
            patch.object(utility, "panel", new=AsyncMock()),
        ):
            await utility.action(self.callback(f"au:{token}:mp3"), self.state, self.bot)
            self.assertFalse(utility.audio_jobs.active(42))
            await utility.action(
                self.callback(f"au:{token}:result"), self.state, self.bot
            )
            await utility.audio_jobs.tasks[42]
            result = await self.state.get_data()
            self.assertEqual(result["utility_original"]["file_id"], "original")
            self.assertIn(
                "cover",
                audio.fields(
                    result["utility_payload"], audio.metadata(result["utility_payload"])
                ),
            )
            await utility.action(
                self.callback(f"au:{token}:reset"), self.state, self.bot
            )
        self.assertEqual(
            (await self.state.get_data())["utility_payload"]["content_type"], "voice"
        )

    async def test_trim_removed_and_formats_deferred(self):
        data = await self.intake("audio")
        token = data["utility_token"]
        with self.assertRaises(ValueError):
            await utility.action(
                self.callback(f"au:{token}:trim"), self.state, self.bot
            )
        buttons = utility.keyboard(token, data["utility_payload"]).inline_keyboard
        self.assertFalse(
            any(b.callback_data.endswith(":trim") for row in buttons for b in row)
        )
        with patch.object(utility.ui, "edit", new=AsyncMock()) as edit:
            await utility.action(
                self.callback(f"au:{token}:formats"), self.state, self.bot
            )
            choices = edit.call_args.args[2].inline_keyboard
            self.assertFalse(
                any(
                    b.callback_data.endswith(":fmt_mp3") for row in choices for b in row
                )
            )
            self.assertEqual(
                sum(
                    b.callback_data.startswith(f"au:{token}:fmt_")
                    for row in choices
                    for b in row
                ),
                4,
            )
        with (
            patch.object(utility, "panel", new=AsyncMock()),
            patch.object(utility.content, "send_content", new=AsyncMock()) as send,
        ):
            for fmt in ("m4a", "flac", "wav", "ogg"):
                await utility.action(
                    self.callback(f"au:{token}:fmt_{fmt}"), self.state, self.bot
                )
                self.assertEqual(
                    audio.metadata((await self.state.get_data())["utility_payload"])[
                        "convert_to"
                    ],
                    fmt,
                )
                self.assertFalse(utility.audio_jobs.active(42))
            send.assert_not_awaited()

    async def test_timeout_and_failure_preserve_session(self):
        data = await self.intake()
        for failure in (TimeoutError(), ValueError("failed")):
            with (
                patch.object(utility.ui, "answer", new=AsyncMock()) as answer,
                patch.object(
                    utility.content, "send_content", new=AsyncMock(side_effect=failure)
                ),
            ):
                await utility.action(
                    self.callback(f"au:{data['utility_token']}:result"),
                    self.state,
                    self.bot,
                )
                await utility.audio_jobs.tasks[42]
                answer.assert_awaited_once()
                self.assertIn("Причина:", self.bot.edit_message_text.call_args.args[0])
            self.assertEqual(
                (await self.state.get_data())["utility_payload"],
                data["utility_payload"],
            )
            self.assertNotIn(42, utility._busy)

    async def test_result_bakes_trim_once(self):
        data = await self.intake()
        payload = copy.deepcopy(data["utility_payload"])
        options = audio.metadata(payload)
        options.update(process=True, trim=[0, 2])
        payload["audio_meta_json"] = json.dumps(options)
        await self.state.update_data(utility_payload=payload)

        async def prepared(bot, uid, payload, markup):
            opts = audio.metadata(payload)
            opts.update(process=False, prepared_file_id="trimmed", duration=2)
            payload.update(file_id="trimmed", audio_meta_json=json.dumps(opts))

        with (
            patch.object(utility.content, "send_content", side_effect=prepared),
            patch.object(utility, "panel", new=AsyncMock()),
        ):
            await utility.action(
                self.callback(f"au:{data['utility_token']}:result"),
                self.state,
                self.bot,
            )
            await utility.audio_jobs.tasks[42]
        current = (await self.state.get_data())["utility_payload"]
        self.assertEqual(current["file_id"], "trimmed")
        self.assertNotIn("trim", audio.metadata(current))


class RealConversionTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_lossless_output_is_not_delivered_as_truncated_success(
        self,
    ):
        from app.downloader import ffmpeg_path

        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "source.ogg", Path(tmp) / "edited.flac"
            run_ffmpeg(
                ffmpeg_path(),
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "anoisesrc=sample_rate=48000:seed=1",
                    "-t",
                    "3",
                    "-c:a",
                    "libopus",
                    str(source),
                ],
            )
            original = source.read_bytes()
            with patch.object(audio_files, "SOURCE_LIMIT", 20_000):
                with self.assertRaisesRegex(ValueError, "20 МБ"):
                    await audio_files.prepare(
                        source, output, dict(format="ogg", convert_to="flac", fast=True)
                    )
            self.assertEqual(source.read_bytes(), original)

    async def test_cancel_download_waits_for_transfer_cleanup(self):
        import asyncio

        entered, closed = asyncio.Event(), asyncio.Event()

        async def transfer(path, destination, **kwargs):
            try:
                destination.write(b"partial")
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

        bot = SimpleNamespace(
            get_file=AsyncMock(
                return_value=SimpleNamespace(file_path="source", file_size=100)
            ),
            download_file=transfer,
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "source"
            task = asyncio.create_task(
                audio_files.download(bot, "source", output, 1024)
            )
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(closed.is_set())
            self.assertFalse(output.exists())

    async def test_all_five_formats_and_presentation_deliver_real_bytes(self):
        from app.downloader import ffmpeg_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.ogg"
            run_ffmpeg(
                ffmpeg_path(),
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440",
                    "-t",
                    "3",
                    "-c:a",
                    "libopus",
                    str(source),
                ],
            )

            async def download(bot, file_id, path, limit):
                path.write_bytes(source.read_bytes())

            for fmt, presentation in (
                ("mp3", "audio"),
                ("m4a", "audio"),
                ("ogg", "voice"),
                ("flac", "document"),
                ("wav", "document"),
                ("mp3", "voice"),
            ):
                with self.subTest(format=fmt, presentation=presentation):
                    delivered = root / ("delivered." + fmt)

                    async def send(uid, upload, **kwargs):
                        data = bytearray()
                        async for chunk in upload.read(bot):
                            data.extend(chunk)
                        delivered.write_bytes(data)
                        self.assertEqual(upload.filename, "source." + fmt)
                        return SimpleNamespace(
                            message_id=1,
                            **{
                                presentation: SimpleNamespace(
                                    file_id="ready", file_size=len(data), duration=3
                                )
                            },
                        )

                    bot = SimpleNamespace(**{"send_" + presentation: send})
                    payload = dict(
                        content_type="voice",
                        file_id="original",
                        audio_meta_json=json.dumps(
                            dict(
                                format="ogg",
                                filename="source.ogg",
                                fast=True,
                                process=True,
                                convert_to=fmt,
                                presentation=presentation,
                            )
                        ),
                    )
                    with patch.object(audio_files, "download", side_effect=download):
                        await audio_files.send_prepared(bot, 42, payload, None)
                    self.assertAlmostEqual(
                        await audio_files.duration(delivered), 3, delta=0.15
                    )
                    self.assertEqual(payload["content_type"], presentation)
                    self.assertEqual(audio.metadata(payload)["format"], fmt)
                    self.assertEqual(payload["file_id"], "ready")

    async def test_wav_oversize_rejected_before_encoding(self):
        payload = dict(
            content_type="audio",
            file_id="source",
            audio_meta_json=json.dumps(
                dict(format="mp3", fast=True, process=True, convert_to="wav")
            ),
        )
        with (
            patch.object(audio_files, "download", new=AsyncMock()),
            patch.object(audio_files, "duration", new=AsyncMock(return_value=180)),
            patch.object(audio_files, "prepare", new=AsyncMock()) as prepare,
        ):
            with self.assertRaisesRegex(ValueError, "WAV"):
                await audio_files.send_prepared(SimpleNamespace(), 42, payload, None)
            prepare.assert_not_awaited()
        self.assertEqual(payload["file_id"], "source")

    async def test_conversion_accepts_ten_minute_boundary(self):
        payload = dict(
            content_type="voice",
            file_id="source",
            audio_meta_json=json.dumps(
                dict(format="ogg", fast=True, process=True, convert_to="mp3")
            ),
        )

        async def prepare(source, output, options, cover=None):
            output.write_bytes(b"prepared")

        bot = SimpleNamespace(
            send_audio=AsyncMock(
                return_value=SimpleNamespace(
                    audio=SimpleNamespace(file_id="ready", duration=600, file_size=8)
                )
            )
        )
        with (
            patch.object(audio_files, "download", new=AsyncMock()),
            patch.object(audio_files, "duration", new=AsyncMock(return_value=600)),
            patch.object(audio_files, "prepare", side_effect=prepare),
        ):
            await audio_files.send_prepared(bot, 42, payload, None)
        self.assertEqual(payload["file_id"], "ready")

    async def test_upload_timeout_is_not_automatically_retried(self):
        async def download(bot, file_id, path, limit):
            path.write_bytes(b"source")

        bot = SimpleNamespace(
            send_audio=AsyncMock(side_effect=TimeoutError("upload response timed out"))
        )
        payload = dict(
            content_type="audio",
            file_id="original",
            audio_meta_json=json.dumps(
                dict(
                    format="mp3",
                    fast=True,
                    process=True,
                    changes={"filename": "new.mp3"},
                )
            ),
        )
        with patch.object(audio_files, "download", side_effect=download):
            with self.assertRaises(TimeoutError):
                await audio_files.send_prepared(bot, 42, payload, None)
        self.assertEqual(bot.send_audio.await_count, 1)
        self.assertEqual(payload["file_id"], "original")

    async def test_download_retries_once_with_fresh_buffer(self):
        calls = 0

        async def transfer(path, destination, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                destination.write(b"partial")
                raise TimeoutError("interrupted")
            destination.write(b"complete")

        bot = SimpleNamespace(
            get_file=AsyncMock(
                return_value=SimpleNamespace(file_path="source", file_size=8)
            ),
            download_file=transfer,
        )
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "source"
            await audio_files.download(bot, "source", output, 1024)
            self.assertEqual(output.read_bytes(), b"complete")
        self.assertEqual(calls, 2)

    async def test_permanent_download_error_does_not_retry(self):
        bot = SimpleNamespace(
            get_file=AsyncMock(
                return_value=SimpleNamespace(file_path="source", file_size=1025)
            ),
            download_file=AsyncMock(),
        )
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                await audio_files.download(bot, "source", Path(folder) / "source", 1024)
        self.assertEqual(bot.get_file.await_count, 1)
        bot.download_file.assert_not_awaited()

    async def test_two_network_failures_stop_without_upload(self):
        bot = SimpleNamespace(
            get_file=AsyncMock(side_effect=TimeoutError("network failed")),
            download_file=AsyncMock(),
        )
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "30 секунд"):
                await audio_files.download(bot, "source", Path(folder) / "source", 1024)
        self.assertEqual(bot.get_file.await_count, 2)
        self.assertEqual(
            audio_files.public_error(
                ValueError(
                    "https://api.telegram.org/bot123456789:abcdefghijklmnopqrstuvwxyz123456789/file"
                )
            ),
            "https://api.telegram.org/bot[hidden]/file",
        )

    async def test_full_preparation_download_trim_all_tags_cover_and_upload(self):
        from mutagen.id3 import ID3

        from app.downloader import ffmpeg_path
        from services.progress import ProgressMessage, current_progress

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, cover, delivered = [
                root / name for name in ("source.mp3", "cover.jpg", "delivered.mp3")
            ]
            run_ffmpeg(
                ffmpeg_path(),
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440",
                    "-t",
                    "8",
                    "-c:a",
                    "libmp3lame",
                    str(source),
                ],
            )
            run_ffmpeg(
                ffmpeg_path(),
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

            async def get_file(file_id):
                path = cover if file_id == "cover" else source
                return SimpleNamespace(
                    file_path=str(path), file_size=path.stat().st_size
                )

            async def download(path, destination, **kwargs):
                destination.write(Path(path).read_bytes())
                destination.seek(0)

            async def send(uid, upload, **kwargs):
                data = bytearray()
                async for chunk in upload.read(bot):
                    data.extend(chunk)
                delivered.write_bytes(data)
                self.assertEqual(upload.filename, "Новое имя.mp3")
                self.assertEqual(kwargs["title"], "Новый трек")
                self.assertEqual(kwargs["performer"], "Исполнитель")
                self.assertEqual(kwargs["caption"], "Описание")
                return SimpleNamespace(
                    message_id=99,
                    audio=SimpleNamespace(
                        file_id="edited", duration=3, file_size=len(data)
                    ),
                )

            bot = SimpleNamespace(
                get_file=get_file,
                download_file=download,
                send_audio=send,
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)),
                edit_message_text=AsyncMock(),
            )
            payload = dict(
                content_type="audio",
                file_id="source",
                text="Описание",
                audio_meta_json=json.dumps(
                    dict(
                        fast=True,
                        process=True,
                        format="mp3",
                        filename="original.mp3",
                        trim=[2, 5],
                        cover_file_id="cover",
                        changes=dict(
                            title="Новый трек",
                            performer="Исполнитель",
                            album="Альбом",
                            year="2026",
                            genre="Pop",
                            filename="Новое имя.mp3",
                            lyrics="Строка один\nСтрока два",
                        ),
                    )
                ),
            )
            token = current_progress.set(ProgressMessage(bot, 42))
            try:
                await audio_files.send_prepared(bot, 42, payload, None)
            finally:
                current_progress.reset(token)
            tags = ID3(delivered)
            self.assertEqual(str(tags["TIT2"]), "Новый трек")
            self.assertEqual(str(tags["TALB"]), "Альбом")
            self.assertEqual(str(tags["TDRC"]), "2026")
            self.assertEqual(tags.getall("USLT")[0].text, "Строка один\nСтрока два")
            self.assertTrue(tags.getall("APIC"))
            self.assertAlmostEqual(await audio_files.duration(delivered), 3, delta=0.15)
            self.assertEqual(payload["file_id"], "edited")

    async def test_real_embedded_lyrics_and_removal(self):
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4

        from app.downloader import ffmpeg_path

        with tempfile.TemporaryDirectory() as folder:
            for extension, codec in (("mp3", "libmp3lame"), ("m4a", "aac")):
                source, output, cleared = [
                    Path(folder) / (name + "." + extension)
                    for name in ("source", "output", "cleared")
                ]
                run_ffmpeg(
                    ffmpeg_path(),
                    [
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440",
                        "-t",
                        "0.2",
                        "-c:a",
                        codec,
                        str(source),
                    ],
                )
                await audio_files.prepare(
                    source,
                    output,
                    dict(format=extension, changes={"lyrics": "Строка один\nLine two"}),
                )
                if extension == "mp3":
                    self.assertEqual(
                        ID3(output).getall("USLT")[0].text, "Строка один\nLine two"
                    )
                else:
                    self.assertEqual(MP4(output)["©lyr"], ["Строка один\nLine two"])
                await audio_files.prepare(
                    output, cleared, dict(format=extension, changes={"lyrics": ""})
                )
                self.assertFalse(
                    ID3(cleared).getall("USLT")
                    if extension == "mp3"
                    else MP4(cleared).get("©lyr")
                )

    async def test_large_file_has_clear_limit_error(self):
        payload = dict(
            content_type="audio",
            file_id="source",
            audio_meta_json=json.dumps(
                dict(format="mp3", file_size=40 * 1024 * 1024, process=True)
            ),
        )
        with self.assertRaisesRegex(ValueError, "20"):
            await audio_files.send_prepared(SimpleNamespace(), 42, payload, None)

    async def test_real_voice_mp3_roundtrip_and_trim(self):
        from app.downloader import ffmpeg_path

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            voice, track, trimmed, back = [
                root / name
                for name in ("voice.ogg", "track.mp3", "trimmed.mp3", "back.ogg")
            ]
            run_ffmpeg(
                ffmpeg_path(),
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440",
                    "-t",
                    "3",
                    "-c:a",
                    "libopus",
                    str(voice),
                ],
            )
            await audio_files.prepare(
                voice,
                track,
                dict(format="ogg", convert_to="mp3", changes={"title": "Test"}),
            )
            await audio_files.prepare(
                track, trimmed, dict(format="mp3", trim=[1, 2], changes={})
            )
            await audio_files.prepare(
                trimmed, back, dict(format="mp3", convert_to="voice", changes={})
            )
            for file, expected in ((voice, 3), (track, 3), (trimmed, 1), (back, 1)):
                self.assertAlmostEqual(
                    await audio_files.duration(file), expected, delta=0.15
                )

    async def test_long_conversion_rejected_before_encoding(self):
        payload = dict(
            content_type="voice",
            file_id="source",
            audio_meta_json=json.dumps(
                dict(format="ogg", fast=True, process=True, convert_to="mp3")
            ),
        )
        with (
            patch.object(audio_files, "download", new=AsyncMock()),
            patch.object(audio_files, "duration", new=AsyncMock(return_value=601)),
            patch.object(audio_files, "prepare", new=AsyncMock()) as encode,
        ):
            with self.assertRaises(ValueError):
                await audio_files.send_prepared(SimpleNamespace(), 42, payload, None)
            encode.assert_not_awaited()
