"""Offline tests for social links, fallback downloads and authentication."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, DB_FILE=":memory:", BOT_TOKEN="", ADMIN_ID="12345"):
    from app import downloader as d


class DownloadTests(unittest.TestCase):
    def multilingual_formats(self, original="ru"):
        return [
            {
                "format_id": "1080",
                "height": 1080,
                "protocol": "https",
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "none",
            },
            {
                "format_id": "360",
                "height": 360,
                "protocol": "https",
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "none",
            },
            {
                "format_id": "dub",
                "protocol": "https",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "aac",
                "language": "en",
                "language_preference": -1,
                "abr": 129.539,
            },
            {
                "format_id": "original",
                "protocol": "https",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "aac",
                "language": original,
                "language_preference": 10,
                "abr": 129.538,
            },
        ]

    def test_original_audio_wins_over_slightly_higher_bitrate_dub(self):
        for language in ("ru", "en", "kk"):
            with self.subTest(language=language):
                choices = d.media_choices(
                    {"formats": self.multilingual_formats(language)}
                )
                self.assertEqual(
                    [c["format"] for c in choices],
                    ["1080+original", "360+original", "original"],
                )

    def test_muxed_dub_does_not_override_original_audio(self):
        formats = self.multilingual_formats()
        formats.append(
            {
                **formats[0],
                "format_id": "muxed",
                "acodec": "aac",
                "language_preference": -1,
            }
        )
        self.assertEqual(
            d.media_choices({"formats": formats})[0]["format"], "1080+original"
        )

    def test_playlist_offers_quality_and_mp3_and_resolves_each_video(self):
        choices = d.playlist_choices()
        self.assertEqual(
            [c["max_height"] for c in choices[:-1]],
            [2160, 1440, 1080, 720, 480, 360, 240, 144],
        )
        info = {"formats": self.multilingual_formats()}
        self.assertEqual(
            d.playlist_format(info, {"kind": "video", "max_height": 720}),
            "360+original",
        )
        self.assertEqual(d.playlist_format(info, choices[0]), "1080+original")
        self.assertEqual(d.playlist_format(info, choices[-1]), "original")
        with self.assertRaises(ValueError):
            d.playlist_format(info, {"kind": "video", "max_height": 144})

    def test_playlist_inspection_returns_quality_buttons(self):
        session = MagicMock()
        session.__enter__.return_value.extract_info.return_value = {
            "_type": "playlist",
            "title": "List",
            "entries": [{"url": "https://example.org/video", "title": "Video"}],
        }
        with (
            patch.object(d, "youtube_session", return_value=session),
            patch.object(d, "ydl_options", return_value={}),
        ):
            info = d.inspect_download("https://example.org/list", Path("."))
        self.assertEqual(info["backend"], "playlist")
        self.assertEqual(info["choices"], d.playlist_choices())

    def test_locked_progress_file_does_not_abort_download_and_next_update_retries(self):
        from services import progress

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with patch.object(Path, "replace", side_effect=PermissionError("locked")):
                progress.write_worker_progress(folder, "download", 10, 100)
            progress.write_worker_progress(folder, "download", 20, 100)
            self.assertEqual(
                json.loads((folder / "progress.json").read_text())["done"], 20
            )

    def test_size_scan_tolerates_renamed_part_and_counts_other_files(self):
        folder = MagicMock()
        vanished, present = MagicMock(), MagicMock()
        vanished.is_file.return_value = True
        vanished.stat.side_effect = FileNotFoundError()
        present.is_file.return_value = True
        present.stat.return_value.st_size = 1234
        folder.rglob.return_value = [vanished, present]
        self.assertEqual(d.temporary_size(folder), 1234)

    def response(self, url=None, body=b""):
        response = MagicMock()
        response.__enter__.return_value = response
        response.url = url
        response.read.return_value = body
        return response

    def embed(self, video_id="123", media_url="https://cdn.example.com/video.mp4"):
        data = {
            "source": {
                "data": {
                    "/embed/v2/123": {
                        "videoData": {
                            "itemInfos": {
                                "id": video_id,
                                "text": "Example",
                                "video": {"urls": [media_url]},
                            }
                        }
                    }
                }
            }
        }
        return (
            '<script id="__FRONTITY_CONNECT_STATE__">' + json.dumps(data) + "</script>"
        ).encode()

    def test_short_link_uses_resolved_post(self):
        url = "https://www.tiktok.com/@user/video/123"
        with patch.object(d, "open_media_url", return_value=self.response(url)):
            self.assertEqual(
                d.resolve_download_url("https://vt.tiktok.com/example/"), url
            )

    def test_short_link_to_homepage_is_actionable(self):
        with patch.object(
            d,
            "open_media_url",
            return_value=self.response("https://www.tiktok.com/?_r=1"),
        ):
            with self.assertRaisesRegex(ValueError, "Поделиться"):
                d.resolve_download_url("https://vm.tiktok.com/expired")

    def test_short_link_cannot_redirect_to_local_address(self):
        with patch.object(
            d, "open_media_url", return_value=self.response("http://127.0.0.1/secret")
        ):
            with self.assertRaises(ValueError):
                d.resolve_download_url("https://vt.tiktok.com/example/")

    def test_other_sites_are_not_resolved_or_rewritten(self):
        with patch.object(d, "open_media_url") as open_url:
            url = "https://example.com/video?signature=abc"
            self.assertEqual(d.resolve_download_url(url), url)
            open_url.assert_not_called()

    def test_embed_checks_video_identity_and_media_url(self):
        url = "https://www.tiktok.com/@user/video/123"
        with patch.object(
            d, "open_media_url", return_value=self.response(body=self.embed())
        ):
            self.assertEqual(d.tiktok_embed_info(url)["backend"], "tiktok_embed")
        with patch.object(
            d,
            "open_media_url",
            return_value=self.response(body=self.embed(video_id="456")),
        ):
            self.assertIsNone(d.tiktok_embed_info(url))
        with patch.object(
            d,
            "open_media_url",
            return_value=self.response(
                body=self.embed(media_url="http://127.0.0.1/a.mp4")
            ),
        ):
            with self.assertRaises(ValueError):
                d.tiktok_embed_info(url)

    def test_embed_is_not_used_on_lookalike_host(self):
        with patch.object(d, "open_media_url") as open_url:
            self.assertIsNone(
                d.tiktok_embed_info("https://tiktok.com.example.com/@u/video/123")
            )
            open_url.assert_not_called()

    def test_inspection_uses_embed_and_does_not_persist_signed_url(self):
        session = MagicMock()
        session.__enter__.return_value.extract_info.side_effect = RuntimeError(
            "Unexpected response from webpage request"
        )
        with (
            patch.object(d, "youtube_session", return_value=session),
            patch.object(d, "ydl_options", return_value={}),
            patch.object(
                d, "open_media_url", return_value=self.response(body=self.embed())
            ),
            patch.object(d, "gallery_items") as gallery,
        ):
            info = d.inspect_download("https://www.tiktok.com/@u/video/123", Path("."))
            self.assertEqual(info["backend"], "tiktok_embed")
            self.assertNotIn("url", info)
            self.assertNotIn("headers", info)
            gallery.assert_not_called()

    def test_embed_download_refreshes_media_url(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            (
                patch.object(
                    d, "open_media_url", return_value=self.response(body=self.embed())
                )
            ),
            patch.object(
                d, "download_direct_file", return_value=Path(tmp) / "media.mp4"
            ) as fetch,
        ):
            result = d.download_files(
                {
                    "url": "https://www.tiktok.com/@u/video/123",
                    "info": {"backend": "tiktok_embed", "choices": [{"kind": "video"}]},
                    "choice": 0,
                },
                Path(tmp),
            )
            self.assertEqual(len(result), 1)
            self.assertEqual(
                fetch.call_args.args[0], "https://cdn.example.com/video.mp4"
            )
            self.assertEqual(
                fetch.call_args.args[2],
                {"Referer": "https://www.tiktok.com/embed/v2/123"},
            )

    def test_site_cookie_file_overrides_shared_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            common = Path(tmp) / "common.txt"
            tiktok = Path(tmp) / "tiktok.txt"
            common.touch()
            tiktok.touch()
            with patch.dict(
                os.environ,
                {
                    "DOWNLOAD_COOKIES_FILE": str(common),
                    "TIKTOK_COOKIES_FILE": str(tiktok),
                },
                clear=True,
            ):
                self.assertEqual(
                    d.cookies_file("https://vt.tiktok.com/a/"), str(tiktok)
                )
                self.assertEqual(d.cookies_file("https://example.com/a"), str(common))
                self.assertEqual(
                    d.cookies_file("https://tiktok.com.example.com/a"), str(common)
                )

    def test_cookie_file_is_not_rewritten_on_session_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.txt"
            original = b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t0\tsession\ttest\n"
            path.write_bytes(original)
            with d.youtube_session({"cookiefile": str(path), "quiet": True}) as ydl:
                self.assertEqual(len(list(ydl.cookiejar)), 1)
                ydl.cookiejar.clear()
            self.assertEqual(path.read_bytes(), original)

    def test_gallery_receives_explicit_cookies(self):
        from gallery_dl import config, extractor

        instance = MagicMock()
        instance.__iter__.return_value = iter(
            [(3, "https://cdn.example.com/a.jpg", {"extension": "jpg"})]
        )
        with (
            patch.object(d, "cookies_file", return_value="configured.txt"),
            patch.object(extractor, "find", return_value=instance),
        ):
            items = d.gallery_items("https://example.com/post")
            self.assertEqual(config.get(("extractor",), "cookies"), "configured.txt")
            self.assertFalse(config.get(("extractor",), "cookies-update"))
            self.assertIs(items[0]["cookiejar"], instance.session.cookies)

    def test_errors_do_not_expose_bug_report_trailer(self):
        for message in (
            "Unexpected response from webpage request; please report this issue on https://github.com/yt-dlp/yt-dlp/issues?q=",
            "HTTP Error 429: Too Many Requests",
            "HTTP Error 403: Forbidden",
            "Unsupported URL: https://example.com",
            "Login required",
        ):
            with self.subTest(message=message):
                result = d.download_error("https://example.com", RuntimeError(message))
                self.assertNotIn("https://", result)
                self.assertNotIn("please report", result)
                self.assertLess(len(result), 300)

    def test_numeric_video_id_is_not_http_error(self):
        result = d.download_error(
            "https://example.com",
            RuntimeError("[TikTok] 1429403404: unexpected failure"),
        )
        self.assertNotIn("временно ограничил", result)
        self.assertNotIn("удалена", result)
