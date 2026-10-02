"""Local video normalization, bounded Telegram parts and upload metadata."""

import math
import subprocess

from app.i18n import tr

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}


def run_ffmpeg(executable, arguments):
    result = subprocess.run(
        [
            executable,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "2",
            "-filter_threads",
            "2",
            "-filter_complex_threads",
            "2",
            "-y",
            *arguments[:-1],
            "-threads",
            "2",
            arguments[-1],
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=1800,
        **(
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else {}
        ),
    )
    if result.returncode:
        raise ValueError("FFmpeg: " + result.stderr.decode(errors="replace")[-400:])


def probe(path, executable, *, codecs=False):
    # read_frames honors IMAGEIO_FFMPEG_EXE; configured binaries must be consistent.
    import os

    import imageio_ffmpeg

    os.environ["IMAGEIO_FFMPEG_EXE"] = executable
    frames = imageio_ffmpeg.read_frames(
        str(path), input_params=["-protocol_whitelist", "file,pipe"]
    )
    try:
        data = next(frames)
        width, height = data["size"]
        result = {"width": width, "height": height, "duration": float(data["duration"])}
        if codecs:
            result.update(
                codec=data.get("codec"),
                pix_fmt=(data.get("pix_fmt") or "").split("(", 1)[0].strip(),
            )
        return result
    finally:
        frames.close()


def prepare_video(path, executable, limit):
    """Prepare H.264/AAC MP4 without cropping; every returned part is size-checked."""
    meta = probe(path, executable, codecs=True)
    duration = meta["duration"]
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(tr("Не удалось определить длительность видео."))
    bitrate = min(8_000_000, max(400_000, int(path.stat().st_size * 8 / duration)))
    # Reserve space for audio, container overhead and encoder rate variation.
    seconds = max(1, int(limit * 0.85 * 8 / (bitrate + 128_000)))
    output = path.parent / (path.stem + "_telegram")
    output.mkdir(exist_ok=True)
    small_source = path.stat().st_size <= limit
    if small_source and meta["codec"] == "h264" and meta["pix_fmt"] == "yuv420p":
        target = output / "part_0001.mp4"
        try:
            # Keep compatible video intact instead of inflating it by re-encoding.
            run_ffmpeg(
                executable,
                [
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-movflags",
                    "+faststart",
                    str(target),
                ],
            )
            if 0 < target.stat().st_size <= limit:
                return [target]
        except ValueError:
            # If remuxing is unsupported, use the existing normalization path.
            pass
    files = []
    start = 0.0
    while start < duration - 0.001:
        # A size estimate alone must not split a small source. Try it whole first.
        length = (
            duration if small_source and start == 0 else min(seconds, duration - start)
        )
        target = output / f"part_{len(files) + 1:04d}.mp4"
        while True:
            run_ffmpeg(
                executable,
                [
                    "-ss",
                    str(start),
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(path),
                    "-t",
                    str(length),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-vf",
                    "scale=trunc(iw*sar/2)*2:trunc(ih/2)*2,setsar=1",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-pix_fmt",
                    "yuv420p",
                    "-b:v",
                    str(bitrate),
                    "-maxrate",
                    str(bitrate),
                    "-bufsize",
                    str(bitrate * 2),
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-movflags",
                    "+faststart",
                    str(target),
                ],
            )
            if 0 < target.stat().st_size <= limit:
                break
            if length < 1:
                raise ValueError(tr("Не удалось разделить видео до лимита Telegram."))
            length /= 2
        files.append(target)
        start += length
    return files


def upload_metadata(path, executable):
    meta = probe(path, executable)
    meta["duration"] = max(1, math.ceil(meta["duration"]))
    thumb = path.with_suffix(".thumbnail.jpg")
    run_ffmpeg(
        executable,
        [
            "-ss",
            str(min(1, meta["duration"] / 2)),
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "scale=320:320:force_original_aspect_ratio=decrease",
            "-q:v",
            "5",
            str(thumb),
        ],
    )
    if thumb.is_file() and thumb.stat().st_size < 200_000:
        meta["thumbnail"] = str(thumb.resolve())
    return meta
