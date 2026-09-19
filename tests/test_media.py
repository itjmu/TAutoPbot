import tempfile
import unittest
from pathlib import Path

import imageio_ffmpeg

from services.media import prepare_video, probe, run_ffmpeg, upload_metadata


class VideoTests(unittest.TestCase):
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
