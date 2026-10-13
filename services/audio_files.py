"""Bounded tag/cover edits and optional short audio conversions/trimming."""

import asyncio
import json
import re
import subprocess
import tempfile
from pathlib import Path

from aiogram.exceptions import TelegramNetworkError
from aiogram.types import FSInputFile
from aiohttp import ClientConnectionError, ClientPayloadError

from app.i18n import tr
from services.progress import ProgressInputFile, current_progress
from services.template_logos import LimitedBuffer

_slots = asyncio.Semaphore(2)
SOURCE_LIMIT = 20 * 1024 * 1024
OUTPUT_LIMIT = 49 * 1024 * 1024
TAGS = {
    "title": "title",
    "performer": "artist",
    "album": "album",
    "year": "date",
    "genre": "genre",
}


async def download(bot, file_id, path, limit):
    progress = current_progress.get()
    original_item = progress.item if progress else ""
    for attempt in range(2):
        try:
            if progress:
                await progress.update("download", force=True)
            try:
                file = await asyncio.wait_for(bot.get_file(file_id), 30)
            except TimeoutError as exc:
                raise TelegramNetworkError(
                    method=None,
                    message=tr("Telegram не вернул адрес файла за 30 секунд."),
                ) from exc
            if file.file_size and file.file_size > limit:
                raise ValueError(
                    tr("Файл превышает лимит {limit} МБ.", limit=limit // (1024 * 1024))
                )
            with LimitedBuffer(limit) as buffer:
                if progress:
                    await progress.update("download", 0, file.file_size)
                transfer = asyncio.create_task(
                    bot.download_file(
                        file.file_path,
                        destination=buffer,
                        timeout=120,
                        chunk_size=256 * 1024,
                    )
                )
                try:
                    while not transfer.done():
                        await asyncio.wait({transfer}, timeout=1)
                        if progress:
                            await progress.update(
                                "download", buffer.getbuffer().nbytes, file.file_size
                            )
                    try:
                        await transfer
                    except TimeoutError as exc:
                        raise TelegramNetworkError(
                            method=None,
                            message=tr("Telegram не передал файл за 2 минуты."),
                        ) from exc
                finally:
                    if not transfer.done():
                        transfer.cancel()
                    await asyncio.gather(transfer, return_exceptions=True)
                path.write_bytes(buffer.getvalue())
            if progress:
                progress.item = original_item
                await progress.update("audio_processing", force=True)
            return
        except (
            TelegramNetworkError,
            TimeoutError,
            ClientConnectionError,
            ClientPayloadError,
        ) as exc:
            if attempt:
                raise ValueError(
                    tr(
                        "Скачивание не удалось после двух попыток: {reason}",
                        reason=public_error(exc),
                    )
                ) from exc
            if progress:
                progress.item = tr("Повтор скачивания: 2/2")


def public_error(error):
    return re.sub(r"\d{5,}:[A-Za-z0-9_-]{20,}", "[hidden]", str(error))[:350]


async def ffmpeg(arguments, timeout=60):
    from app.downloader import ffmpeg_path

    process = await asyncio.create_subprocess_exec(
        ffmpeg_path(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        "2",
        "-filter_threads",
        "2",
        "-y",
        *arguments,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        **(
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else {}
        ),
    )
    try:
        _, error = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except BaseException as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(exc, TimeoutError):
            raise ValueError(
                tr("Обработка аудио заняла слишком много времени. Исходник сохранён.")
            ) from exc
        raise
    if process.returncode:
        raise ValueError(
            tr("Не удалось оформить аудиофайл: ")
            + error.decode(errors="replace")[-250:]
        )


async def prepare(source, output, options, cover=None):
    """Preserve encoded audio packets and existing tags unless explicitly changed."""
    extension = options["format"]
    conversion = options.get("convert_to")
    target = "ogg" if conversion == "voice" else conversion or extension
    if extension not in {"mp3", "m4a"} and not conversion and not options.get("trim"):
        raise ValueError(tr("Теги и обложка доступны для MP3 и M4A."))
    args = ["-protocol_whitelist", "file,pipe", "-i", str(source)]
    if options.get("trim"):
        start, end = options["trim"]
        args = ["-ss", str(start)] + args
    if cover:
        args += ["-protocol_whitelist", "file,pipe", "-i", str(cover)]
    codec = {
        "mp3": "libmp3lame",
        "m4a": "aac",
        "ogg": "libopus",
        "voice": "libopus",
        "flac": "flac",
        "wav": "pcm_s16le",
    }.get(conversion, "copy")
    args += ["-map", "0:a:0", "-map_metadata", "0", "-c:a", codec]
    if options.get("trim"):
        args += ["-t", str(end - start)]
    if conversion in {"voice", "ogg"}:
        args += ["-ac", "1", "-ar", "48000", "-b:a", "48k", "-vn"]
    elif conversion == "mp3":
        args += ["-b:a", "128k"]
    elif conversion == "m4a":
        args += ["-b:a", "128k", "-movflags", "+faststart"]
    elif conversion in {"flac", "wav"}:
        args += ["-ac", "2", "-ar", "48000"]
    if cover and target in {"mp3", "m4a"}:
        args += ["-map", "1:v:0", "-c:v", "copy", "-disposition:v", "attached_pic"]
    elif not options.get("cover_removed") and target in {"mp3", "m4a"}:
        args += ["-map", "0:v?", "-c:v", "copy"]
    for field, tag in TAGS.items():
        if field in options.get("changes", {}):
            args += ["-metadata", tag + "=" + options["changes"][field]]
    if target == "mp3":
        args += ["-id3v2_version", "3"]
    capped = options.get("fast") and target in {"flac", "wav"} and conversion
    if capped:
        args += ["-fs", str(SOURCE_LIMIT + 1)]
    args.append(str(output))
    await ffmpeg(args, timeout=180 if options.get("fast") else 60)
    if "lyrics" in options.get("changes", {}) and target in {"mp3", "m4a"}:
        writer = asyncio.create_task(
            asyncio.to_thread(write_lyrics, output, options["changes"]["lyrics"])
        )
        try:
            await asyncio.shield(writer)
        except asyncio.CancelledError:
            await writer  # Finish the bounded tag write before temporary-file cleanup.
            raise
    if not output.is_file() or not 0 < output.stat().st_size <= (
        SOURCE_LIMIT if options.get("fast") else OUTPUT_LIMIT
    ):
        raise ValueError(
            tr("Результат превышает лимит 20 МБ.")
            if options.get("fast")
            else tr("Оформленный аудиофайл превышает лимит отправки.")
        )
    if capped:
        expected = options.get("_expected_duration")
        if expected is None:
            expected = await duration(source)
        # A muxer size cap must never turn an oversized output into a silently
        # shortened successful recording, even when it stops below the cap.
        if await duration(output) + 0.15 < expected:
            raise ValueError(tr("Результат превышает лимит 20 МБ."))


def write_lyrics(path, text):
    """Standard MP3 USLT / M4A ©lyr tags, supported by compatible players."""
    if path.suffix == ".mp3":
        from mutagen.id3 import ID3, USLT

        tags = ID3(path)
        tags.delall("USLT")
        if text:
            tags.add(USLT(encoding=1, lang="eng", desc="", text=text))
        tags.save(path, v2_version=3)
    else:
        from mutagen.mp4 import MP4

        tags = MP4(path)
        tags.pop("©lyr", None)
        if text:
            tags["©lyr"] = [text]
        tags.save()


async def duration(source):
    """Read container headers, without decoding the recording."""
    from app.downloader import ffmpeg_path

    process = await asyncio.create_subprocess_exec(
        ffmpeg_path(),
        "-nostdin",
        "-hide_banner",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        **(
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else {}
        ),
    )
    try:
        _, error = await asyncio.wait_for(process.communicate(), 3)
    except BaseException as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(exc, TimeoutError):
            raise ValueError(
                tr("Не удалось прочитать аудиофайл за 3 секунды.")
            ) from exc
        raise
    match = re.search(
        r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", error.decode(errors="replace")
    )
    if not match:
        raise ValueError(tr("Не удалось определить длительность аудио."))
    hours, minutes, seconds = map(float, match.groups())
    return hours * 3600 + minutes * 60 + seconds


async def send_prepared(bot, chat_id, payload, markup, *, message_thread_id=None):
    from app.content import send_content

    options = payload.get("audio_meta_json") or {}
    options = json.loads(options) if isinstance(options, str) else dict(options)
    if (options.get("file_size") or 0) > SOURCE_LIMIT:
        raise ValueError(tr("Файл превышает лимит {limit} МБ.", limit=20))
    if options.get("fast") and _slots.locked():
        raise ValueError(tr("Аудиоредактор занят. Попробуйте через несколько секунд."))
    async with _slots:
        with tempfile.TemporaryDirectory(prefix="tautop-audio-") as folder:
            root = Path(folder)
            extension = options.get("format", "mp3")
            conversion = options.get("convert_to")
            if conversion and conversion not in {
                "mp3",
                "m4a",
                "ogg",
                "voice",
                "flac",
                "wav",
            }:
                raise ValueError(tr("Инструмент недоступен для этого формата."))
            target = "ogg" if conversion == "voice" else conversion or extension
            if (
                options.get("fast")
                and target not in {"mp3", "m4a"}
                and (
                    options.get("cover_file_id")
                    or "lyrics" in options.get("changes", {})
                )
            ):
                raise ValueError(
                    tr(
                        "Обложка и текст песни доступны для MP3/M4A. Выберите этот формат или уберите эти изменения."
                    )
                )
            if extension not in {
                "mp3",
                "m4a",
                "ogg",
                "oga",
                "opus",
                "wav",
                "flac",
                "aac",
                "wma",
                "aiff",
            }:
                raise ValueError(tr("Инструмент недоступен для этого формата."))
            filename = (
                options.get("changes", {}).get("filename")
                or options.get("filename")
                or f"audio.{extension}"
            )
            filename = Path(filename.replace("\\", "/")).name
            thumbnail = None
            try:
                source = root / f"source.{extension}"
                await download(
                    bot,
                    payload["file_id"],
                    source,
                    SOURCE_LIMIT,
                )
                if options.get("convert_to") or options.get("trim"):
                    actual_duration = await duration(source)
                    if options.get("trim"):
                        start, end = options["trim"]
                        if (
                            not 0 <= start < end <= actual_duration + 0.1
                            or end - start > 120
                        ):
                            raise ValueError(
                                tr(
                                    "Фрагмент должен быть внутри записи и не длиннее 120 секунд."
                                )
                            )
                    elif actual_duration > (600.1 if options.get("fast") else 120):
                        raise ValueError(
                            tr(
                                "Запись слишком длинная для преобразования: максимум {minutes} минут.",
                                minutes=10 if options.get("fast") else 2,
                            )
                        )
                    if (
                        options.get("convert_to") == "wav"
                        and actual_duration * 48000 * 2 * 2 + 4096 > SOURCE_LIMIT
                    ):
                        raise ValueError(
                            tr(
                                "WAV получится больше 20 МБ. Выберите MP3, M4A или OGG. Исходник сохранён."
                            )
                        )
                if options.get("cover_file_id"):
                    image = root / "cover-source"
                    thumbnail = root / "cover.jpg"
                    await download(
                        bot, options["cover_file_id"], image, 2 * 1024 * 1024
                    )
                    await ffmpeg(
                        [
                            "-protocol_whitelist",
                            "file,pipe",
                            "-i",
                            str(image),
                            "-vf",
                            "scale=320:320:force_original_aspect_ratio=decrease",
                            "-frames:v",
                            "1",
                            "-q:v",
                            "4",
                            str(thumbnail),
                        ]
                    )
                    if thumbnail.stat().st_size >= 200 * 1024:
                        raise ValueError(tr("Не удалось подготовить обложку аудио."))
                file = source
                if (
                    any(field in options.get("changes", {}) for field in TAGS)
                    or thumbnail
                    or options.get("cover_removed")
                    or options.get("convert_to")
                    or options.get("trim")
                    or "lyrics" in options.get("changes", {})
                ):
                    output_extension = (
                        "ogg"
                        if options.get("convert_to") == "voice"
                        else options.get("convert_to") or extension
                    )
                    file = root / f"edited.{output_extension}"
                    processing_options = dict(options)
                    if options.get("convert_to"):
                        processing_options["_expected_duration"] = (
                            (options["trim"][1] - options["trim"][0])
                            if options.get("trim")
                            else actual_duration
                        )
                    await prepare(source, file, processing_options, thumbnail)
            except Exception as exc:
                raise ValueError(tr("Аудио не изменено: ") + str(exc)) from exc
            if options.get("fast") and file.stat().st_size > SOURCE_LIMIT:
                raise ValueError(tr("Результат превышает лимит 20 МБ."))
            prepared = dict(payload, file_id=ProgressInputFile(file, filename=filename))
            if options.get("convert_to"):
                extension = (
                    "ogg" if options["convert_to"] == "voice" else options["convert_to"]
                )
                filename = Path(filename).stem + "." + extension
                prepared["file_id"] = ProgressInputFile(file, filename=filename)
                prepared["content_type"] = (
                    "audio"
                    if extension in {"mp3", "m4a"}
                    else "voice"
                    if extension == "ogg"
                    else "document"
                )
            if options.get("presentation"):
                presentation = options["presentation"]
                if (
                    presentation not in {"audio", "voice", "document"}
                    or (presentation == "audio" and extension not in {"mp3", "m4a"})
                    or (
                        presentation == "voice"
                        and extension not in {"mp3", "m4a", "ogg"}
                    )
                ):
                    raise ValueError(tr("Инструмент недоступен для этого формата."))
                prepared["content_type"] = presentation
            clean_options = dict(options, process=False)
            prepared["audio_meta_json"] = json.dumps(clean_options, ensure_ascii=False)
            if thumbnail and prepared["content_type"] == "audio":
                prepared["audio_thumbnail"] = FSInputFile(thumbnail)
            if progress := current_progress.get():
                progress.phase = "upload"
            async with prepared["file_id"]:
                result = await send_content(
                    bot, chat_id, prepared, markup, message_thread_id=message_thread_id
                )
            media = getattr(result, prepared["content_type"], None)
            if media:
                payload["file_id"] = media.file_id
                payload["content_type"] = prepared["content_type"]
                options["source_type"] = prepared["content_type"]
                options["format"] = extension
                options["filename"] = filename
                options["file_size"] = (
                    getattr(media, "file_size", None) or file.stat().st_size
                )
                options["duration"] = getattr(
                    media, "duration", options.get("duration")
                )
                options["prepared_file_id"] = media.file_id
                options["process"] = False
                for key in ("title", "performer"):
                    if not options.get(key) and key not in options.get("changes", {}):
                        options[key] = getattr(media, key, None)
                payload["audio_meta_json"] = json.dumps(options, ensure_ascii=False)
            return result
