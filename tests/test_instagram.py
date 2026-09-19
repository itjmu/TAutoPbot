"""Offline Instagram authentication and error handling regression tests."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

with patch.dict(os.environ, DB_FILE=":memory:", BOT_TOKEN="", ADMIN_ID="12345"):
    from app import downloader


class InstagramTests(unittest.TestCase):
    def test_cookies_are_only_enabled_for_instagram(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            cookies = folder / "cookies.txt"
            cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            with (
                patch.dict(os.environ, INSTAGRAM_COOKIES_FILE=str(cookies)),
                patch.object(downloader, "ffmpeg_path", return_value="ffmpeg"),
                patch.object(downloader, "deno_path", return_value=None),
            ):
                options = downloader.ydl_options(
                    folder, "https://www.instagram.com/p/test/"
                )
                self.assertEqual(options["cookiefile"], str(cookies))
                for url in (
                    "https://example.com/video",
                    "https://instagram.com.example.com/p/test/",
                ):
                    self.assertNotIn("cookiefile", downloader.ydl_options(folder, url))

    def test_missing_cookie_file_is_reported(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                INSTAGRAM_COOKIES_FILE=str(Path(directory) / "missing.txt"),
            ),
            patch.object(downloader, "ffmpeg_path", return_value="ffmpeg"),
        ):
            with self.assertRaisesRegex(ValueError, "INSTAGRAM_COOKIES_FILE"):
                downloader.ydl_options(Path(directory), "https://instagram.com/p/test/")

    def test_empty_response_has_actionable_message(self):
        error = RuntimeError(
            "ERROR: [Instagram] test: Instagram sent an empty media response."
        )
        message = downloader.download_error("https://instagram.com/p/test/", error)
        self.assertIn("INSTAGRAM_COOKIES_FILE", message)
        self.assertNotIn("ERROR:", message)
        self.assertNotIn(
            "ERROR:", downloader.download_error("https://example.com", error)
        )

    def test_inspection_fallback_preserves_friendly_error(self):
        import yt_dlp

        with (
            patch.object(downloader, "ydl_options", return_value={}),
            patch.object(
                yt_dlp.YoutubeDL,
                "extract_info",
                side_effect=RuntimeError("Instagram sent an empty media response"),
            ),
            patch.object(
                downloader, "gallery_items", side_effect=ValueError("No images")
            ),
        ):
            with self.assertRaisesRegex(ValueError, "INSTAGRAM_COOKIES_FILE"):
                downloader.inspect_download("https://instagram.com/p/test/", Path("."))
