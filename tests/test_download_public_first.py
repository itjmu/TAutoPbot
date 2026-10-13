"""Public-first site access, bounded authentication and account isolation."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app import downloader as d


class PublicFirstTests(unittest.TestCase):
    url = "https://www.youtube.com/watch?v=example"

    def test_public_success_does_not_resolve_or_load_configured_cookies(self):
        def operation():
            self.assertIsNone(d._site_cookie_file.get())
            return "public"

        with patch.object(
            d, "cookies_file", side_effect=d.DownloadConfigError("missing")
        ) as cookies:
            self.assertEqual(d.public_first(self.url, operation), "public")
            cookies.assert_not_called()

    def test_one_login_retry_then_context_restored_and_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.txt"
            original = b"# Netscape HTTP Cookie File\n"
            path.write_bytes(original)
            states = []

            def operation():
                states.append(d._site_cookie_file.get())
                if len(states) == 1:
                    raise RuntimeError("Sign in to confirm you're not a bot")
                return "authenticated"

            with patch.object(d, "cookies_file", return_value=str(path)) as cookies:
                self.assertEqual(d.public_first(self.url, operation), "authenticated")
                cookies.assert_called_once_with(self.url)
            self.assertEqual(states, [None, str(path)])
            self.assertIsNone(d._site_cookie_file.get())
            self.assertEqual(path.read_bytes(), original)

    def test_failure_does_not_loop_and_no_configured_account_does_not_retry(self):
        for configured, expected in ((None, 1), ("configured.txt", 2)):
            with (
                self.subTest(configured=configured),
                patch.object(d, "cookies_file", return_value=configured),
                patch.dict(os.environ, {}, clear=True),
            ):
                operation = MagicMock(side_effect=RuntimeError("login required"))
                with self.assertRaisesRegex(RuntimeError, "login required"):
                    d.public_first(self.url, operation)
                self.assertEqual(operation.call_count, expected)
                self.assertIsNone(d._site_cookie_file.get())

    def test_rate_network_and_unavailable_errors_never_use_account(self):
        errors = (
            RuntimeError("HTTP Error 429: Too Many Requests; sign in"),
            TimeoutError("sign in"),
            RuntimeError("connection reset; cookies; sign in"),
            RuntimeError("Video unavailable"),
            RuntimeError("HTTP Error 403: Forbidden"),
            ValueError("Unsupported URL"),
            d.DownloadConfigError("login required"),
        )
        for error in errors:
            with (
                self.subTest(error=str(error)),
                patch.object(d, "cookies_file") as cookies,
            ):
                operation = MagicMock(side_effect=error)
                with self.assertRaises(type(error)):
                    d.public_first(self.url, operation)
                operation.assert_called_once()
                cookies.assert_not_called()

    def test_both_inspection_causes_are_preserved_for_access_decision(self):
        auth = RuntimeError("LoginRequired")
        unsupported = RuntimeError("Unsupported URL")
        error = d.DownloadInspectionError("friendly message", unsupported, auth)
        self.assertTrue(d.needs_site_cookies(self.url, error))
        blocked = d.DownloadInspectionError(
            "friendly message", RuntimeError("HTTP Error 429"), auth
        )
        self.assertFalse(d.needs_site_cookies(self.url, blocked))

    def test_rate_limit_warning_before_bot_check_does_not_trigger_login(self):
        def operation():
            for _ in range(10):
                d.QuietYDL().warning("Optional format information unavailable")
            d.QuietYDL().warning(
                "Unable to download webpage: HTTP Error 429: Too Many Requests"
            )
            raise RuntimeError("Sign in to confirm you're not a bot")

        with patch.object(d, "cookies_file") as cookies:
            with self.assertRaisesRegex(ValueError, "временно ограничил"):
                d.public_first(self.url, operation)
            cookies.assert_not_called()
        self.assertIsNone(d._access_warnings.get())

    def test_missing_account_is_reported_only_after_public_auth_failure(self):
        with (
            patch.dict(os.environ, DOWNLOAD_COOKIE_CONFIG_ERROR="Missing site cookies"),
            patch.object(d, "cookies_file") as cookies,
        ):
            self.assertEqual(d.public_first(self.url, lambda: "public"), "public")
            with self.assertRaisesRegex(d.DownloadConfigError, "Missing site cookies"):
                d.public_first(
                    self.url, MagicMock(side_effect=RuntimeError("login required"))
                )
            cookies.assert_not_called()

    def test_yt_and_gallery_options_are_anonymous_by_default(self):
        from gallery_dl import config, extractor

        instance = MagicMock()
        instance.__iter__.return_value = iter(
            [(3, "https://cdn.example.com/a.jpg", {"extension": "jpg"})]
        )
        with (
            patch.object(d, "cookies_file") as cookies,
            patch.object(d, "ffmpeg_path", return_value="ffmpeg"),
            patch.object(d, "deno_path", return_value=None),
            patch.object(extractor, "find", return_value=instance),
        ):
            self.assertNotIn("cookiefile", d.ydl_options(Path("."), self.url))
            d.gallery_items("https://example.com/post")
            self.assertIsNone(config.get(("extractor",), "cookies"))
            cookies.assert_not_called()

    def test_site_fallback_reaches_yt_options_and_gallery_read_only(self):
        from gallery_dl import config, extractor

        instance = MagicMock()
        instance.__iter__.return_value = iter(
            [(3, "https://cdn.example.com/a.jpg", {"extension": "jpg"})]
        )
        attempts = 0

        def operation():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("login required")
            self.assertEqual(
                d.ydl_options(Path("."), self.url)["cookiefile"], "site.txt"
            )
            d.gallery_items(self.url)
            self.assertEqual(config.get(("extractor",), "cookies"), "site.txt")
            self.assertFalse(config.get(("extractor",), "cookies-update"))

        with (
            patch.object(d, "cookies_file", return_value="site.txt"),
            patch.object(d, "ffmpeg_path", return_value="ffmpeg"),
            patch.object(d, "deno_path", return_value=None),
            patch.object(extractor, "find", return_value=instance),
        ):
            d.public_first(self.url, operation)


class PublicWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_cookie_file_does_not_block_public_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = json.dumps({"ok": True, "result": {"title": "public"}}).encode()
            with (
                patch.object(
                    d,
                    "cookies_file",
                    side_effect=d.DownloadConfigError(
                        "Missing configured site cookie file"
                    ),
                ),
                patch.object(d, "worker_command", return_value=(["worker"], Path(tmp))),
                patch.object(
                    d.asyncio,
                    "create_subprocess_exec",
                    new=AsyncMock(return_value=SimpleNamespace()),
                ) as launch,
                patch.object(
                    d, "bounded_communicate", new=AsyncMock(return_value=(output, b""))
                ),
                patch.object(d, "stop_download_worker", new=AsyncMock()),
            ):
                result = await d.run_download_worker(
                    "inspect", "https://example.org/public", Path(tmp)
                )
                self.assertEqual(result, {"title": "public"})
                self.assertNotIn(
                    "DOWNLOAD_COOKIES_FILE", launch.call_args.kwargs["env"]
                )
                self.assertEqual(
                    launch.call_args.kwargs["env"]["DOWNLOAD_COOKIE_CONFIG_ERROR"],
                    "Missing configured site cookie file",
                )
