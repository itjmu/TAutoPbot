"""Distinguish a connection challenge from restricted content."""

import unittest

from app.downloader import download_error


class DownloadErrorTests(unittest.TestCase):
    def test_bot_check_is_not_reported_as_private_content(self):
        for message in (
            "Sign in to confirm you're not a bot. Use --cookies for authentication.",
            "Sign in to confirm you’re not a bot. Use --cookies-from-browser.",
        ):
            with self.subTest(message=message):
                result = download_error(
                    "https://www.youtube.com/watch?v=38oW0h49uIA", Exception(message)
                )
                self.assertIn("проверка соединения", result)
                self.assertNotIn("публикации требуется вход", result)

    def test_private_video_still_reports_login_requirement(self):
        self.assertIn(
            "публикации требуется вход",
            download_error(
                "https://www.youtube.com/watch?v=example",
                Exception("Private video. Sign in."),
            ),
        )

    def test_rate_limit_takes_precedence_over_cookie_guidance(self):
        self.assertIn(
            "временно ограничил",
            download_error(
                "https://www.youtube.com/watch?v=example",
                Exception("HTTP Error 429: Too Many Requests; sign in with cookies"),
            ),
        )
