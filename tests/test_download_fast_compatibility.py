"""Persisted download preparation versions keep partial retries compatible."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import downloader


class DownloadPreparationVersionTests(unittest.TestCase):
    def test_worker_keeps_legacy_layout_and_enables_new_layout_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "source.mp4"
            source.write_bytes(b"sample")
            for info, expected in (({}, False), ({"media_prepare_version": 2}, True)):
                with self.subTest(info=info):
                    request = dict(
                        action="download",
                        url="https://example.org/video",
                        folder=str(folder),
                        info=info,
                    )
                    with (
                        patch.object(
                            downloader.sys, "stdin", io.StringIO(json.dumps(request))
                        ),
                        patch.object(downloader, "install_public_network_guard"),
                        patch.object(
                            downloader,
                            "validate_download_url",
                            side_effect=lambda url: url,
                        ),
                        patch.object(
                            downloader,
                            "resolve_download_url",
                            side_effect=lambda url: url,
                        ),
                        patch.object(
                            downloader, "download_files", return_value=[source]
                        ),
                        patch.object(downloader, "ffmpeg_path", return_value="ffmpeg"),
                        patch.object(
                            downloader, "prepare_video", return_value=[source]
                        ) as prepare,
                        patch.object(downloader, "upload_metadata", return_value={}),
                        contextlib.redirect_stdout(io.StringIO()) as output,
                    ):
                        downloader.download_worker_main()
                    self.assertTrue(json.loads(output.getvalue())["ok"])
                    self.assertEqual(prepare.call_args.kwargs, {"fast": expected})
