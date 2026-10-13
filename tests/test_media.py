import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import imageio_ffmpeg

from services.media import prepare_video, probe, run_ffmpeg, upload_metadata


class VideoTests(unittest.TestCase):
    def test_keyframe_splitting_preserves_packets_and_duration(self):
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "keyframes.mp4"
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
                    "8",
                    "-c:v",
                    "libx264",
                    "-g",
                    "24",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(source),
                ],
            )
            self.assertGreater(source.stat().st_size, 150_000)
            with patch("services.media.run_ffmpeg", wraps=run_ffmpeg) as encode:
                parts = prepare_video(source, executable, 150_000)
            self.assertGreater(len(parts), 1)
            self.assertTrue(all(part.parent.name == "lossless" for part in parts))
            self.assertTrue(all(0 < part.stat().st_size <= 150_000 for part in parts))
            self.assertFalse(
                any("libx264" in call.args[1] for call in encode.call_args_list)
            )
            self.assertAlmostEqual(
                sum(probe(part, executable)["duration"] for part in parts),
                probe(source, executable)["duration"],
                delta=0.5,
            )

            def packets(path, stream):
                return subprocess.check_output(
                    [
                        executable,
                        "-nostdin",
                        "-v",
                        "error",
                        "-i",
                        str(path),
                        "-map",
                        "0:" + stream + ":0",
                        "-c",
                        "copy",
                        "-f",
                        "data",
                        "pipe:1",
                    ],
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )

            for stream in ("v", "a"):
                self.assertEqual(
                    b"".join(packets(part, stream) for part in parts),
                    packets(source, stream),
                )

    def test_segment_failure_uses_existing_encoding_fallback(self):
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fallback.mp4"
            run_ffmpeg(
                executable,
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=180x320:rate=24",
                    "-t",
                    "4",
                    "-c:v",
                    "libx264",
                    "-g",
                    "24",
                    "-pix_fmt",
                    "yuv420p",
                    str(source),
                ],
            )

            def fail_segment(executable, args):
                if "segment" in args:
                    raise ValueError("unsupported segmentation")
                return run_ffmpeg(executable, args)

            with patch("services.media.run_ffmpeg", side_effect=fail_segment):
                parts = prepare_video(source, executable, 75_000)
            self.assertTrue(all(0 < part.stat().st_size <= 75_000 for part in parts))
            self.assertAlmostEqual(
                sum(probe(part, executable)["duration"] for part in parts), 4, delta=0.5
            )

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
            encode.assert_not_called()
            self.assertEqual(parts, [source])
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
