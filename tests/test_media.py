import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import imageio_ffmpeg

from services.media import prepare_video, probe, run_ffmpeg, upload_metadata


class VideoTests(unittest.TestCase):
    def test_small_incompatible_video_is_encoded_whole_before_splitting(self):
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "small.mp4"
            run_ffmpeg(
                executable,
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:size=180x320:rate=24",
                    "-t",
                    "6",
                    "-c:v",
                    "mpeg4",
                    str(source),
                ],
            )
            self.assertLess(source.stat().st_size, 150_000)
            parts = prepare_video(source, executable, 150_000)
            self.assertEqual(len(parts), 1)
            self.assertLessEqual(parts[0].stat().st_size, 150_000)
            meta = probe(parts[0], executable, codecs=True)
            self.assertEqual(meta["codec"], "h264")
            self.assertAlmostEqual(meta["duration"], 6, delta=0.1)

    def test_small_low_bitrate_video_stays_whole_without_video_reencoding(self):
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "small.mp4"
            run_ffmpeg(
                executable,
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:size=180x320:rate=24",
                    "-t",
                    "6",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(source),
                ],
            )
            self.assertLess(source.stat().st_size, 150_000)
            with patch("services.media.run_ffmpeg", wraps=run_ffmpeg) as encode:
                parts = prepare_video(source, executable, 150_000)
            self.assertEqual(len(parts), 1)
            self.assertLessEqual(parts[0].stat().st_size, 150_000)
            self.assertAlmostEqual(
                probe(parts[0], executable)["duration"], 6, delta=0.1
            )
            args = encode.call_args.args[1]
            self.assertEqual(args[args.index("-c:v") + 1], "copy")
            self.assertNotIn("codec", upload_metadata(parts[0], executable))

    def test_portrait_video_splits_and_keeps_metadata(self):
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "portrait.mp4"
            run_ffmpeg(
                executable,
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=180x320:rate=24",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=44100",
                    "-t",
                    "6",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(source),
                ],
            )
            parts = prepare_video(source, executable, 150_000)
            self.assertGreater(len(parts), 1)
            total = 0
            for part in parts:
                self.assertLessEqual(part.stat().st_size, 150_000)
                meta = upload_metadata(part, executable)
                self.assertEqual((meta["width"], meta["height"]), (180, 320))
                self.assertGreater(meta["duration"], 0)
                self.assertTrue(Path(meta["thumbnail"]).is_file())
                total += probe(part, executable)["duration"]
            self.assertAlmostEqual(total, 6, delta=0.5)


if __name__ == "__main__":
    unittest.main()
