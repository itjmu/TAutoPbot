"""Bounded logo processing; source Telegram media is never overwritten."""

import asyncio
import io
import subprocess
import tempfile
from pathlib import Path

from aiogram.types import FSInputFile

from app.i18n import tr

_slots = asyncio.Semaphore(2)
SOURCE_LIMIT = 20 * 1024 * 1024
LOGO_LIMIT = 2 * 1024 * 1024
OUTPUT_LIMIT = 49 * 1024 * 1024
POSITIONS = {
    "bottom_right": "W-w-p:H-h-p",
    "bottom_left": "p:H-h-p",
    "top_left": "p:p",
    "top_right": "W-w-p:p",
    "center": "(W-w)/2:(H-h)/2",
}
COORDINATES = {
    "bottom_right": (100, 100),
    "bottom_left": (0, 100),
    "top_left": (0, 0),
    "top_right": (100, 0),
    "center": (50, 50),
}


def coordinates(settings):
    x, y = settings.get("x"), settings.get("y")
    if x is None and y is None:
        return COORDINATES.get(settings.get("position"), (100, 100))
    if not all(isinstance(value, int) and 0 <= value <= 100 for value in (x, y)):
        raise ValueError(tr("Некорректные настройки логотипа."))
    return x, y


class LimitedBuffer(io.BytesIO):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise ValueError(tr("Файл слишком большой для добавления логотипа."))
        return super().write(data)


async def download(bot, file_id, path, limit):
    file = await bot.get_file(file_id)
    if file.file_size and file.file_size > limit:
        raise ValueError(tr("Логотип — до 2 МБ; фото или обложка — до 20 МБ."))
    with LimitedBuffer(limit) as buffer:
        await bot.download_file(file.file_path, destination=buffer, timeout=60)
        path.write_bytes(buffer.getvalue())


async def overlay(source, logo, output, typ, settings):
    from app.downloader import ffmpeg_path
    from services.media import probe

    if typ != "photo":
        raise ValueError(tr("Логотип добавляется только на фото или обложку."))
    executable = ffmpeg_path()
    meta = await asyncio.to_thread(probe, source, executable)
    scale = settings.get("scale", 20)
    if not isinstance(scale, int) or not 1 <= scale <= 100:
        raise ValueError(tr("Некорректные настройки логотипа."))
    x, y = coordinates(settings)
    width, height = meta["width"], meta["height"]
    size_w, size_h = max(2, width * scale // 100), max(2, height * scale // 100)
    padding = max(2, min(width, height) // 50)
    position = (
        f"(W-w)*{x}/100:(H-h)*{y}/100"
        if settings.get("x") is not None
        else POSITIONS.get(settings.get("position"), POSITIONS["bottom_right"]).replace(
            "p", str(padding)
        )
    )
    filters = (
        f"[1:v]scale={size_w}:{size_h}:force_original_aspect_ratio=decrease,format=rgba[logo];"
        f"[0:v][logo]overlay={position}:eof_action=repeat"
    )
    args = [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        "2",
        "-filter_complex_threads",
        "2",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source),
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(logo),
        "-filter_complex",
        filters,
    ]
    args += ["-frames:v", "1", "-q:v", "2"]
    args += ["-threads", "2", str(output)]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        **(
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else {}
        ),
    )
    try:
        _, error = await asyncio.wait_for(process.communicate(), timeout=180)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError(
            tr("Не удалось добавить логотип: ") + error.decode(errors="replace")[-300:]
        )
    if not output.is_file() or not 0 < output.stat().st_size <= OUTPUT_LIMIT:
        raise ValueError(tr("Результат с логотипом превышает лимит отправки 49 МБ."))


def remember_media(payload, messages):
    items = payload["media_json"] if payload["content_type"] == "album" else [payload]
    for item, message in zip(items, messages):
        typ = item["content_type"]
        media = getattr(message, typ, None)
        if typ == "photo" and media:
            media = media[-1]
        if media:
            item["file_id"] = media.file_id
            if typ == "video" and getattr(media, "cover", None):
                item["cover_file_id"] = media.cover[-1].file_id
    payload.pop("template_logo", None)


async def send_with_logo(bot, chat_id, payload, markup, *, message_thread_id=None):
    from app.content import PartialAlbumError, entity_list, send_content

    # Prepare every album item before the first send. Never publish an unbranded fallback.
    async with _slots:
        with tempfile.TemporaryDirectory(prefix="tautop-logo-") as folder:
            root = Path(folder)
            prepared = dict(payload)
            settings = prepared.pop("template_logo")
            if prepared["content_type"] == "album":
                prepared["media_json"] = [
                    dict(item) for item in entity_list(payload.get("media_json"))
                ]
                items = prepared["media_json"]
            else:
                items = [prepared]
            if not any(
                item["content_type"] == "photo"
                or (item["content_type"] == "video" and item.get("cover_file_id"))
                for item in items
            ):
                result = await send_content(
                    bot, chat_id, prepared, markup, message_thread_id=message_thread_id
                )
                payload.pop("template_logo", None)
                return result
            try:
                logo = root / "logo"
                await download(bot, settings["file_id"], logo, LOGO_LIMIT)
                for index, item in enumerate(items):
                    typ = item["content_type"]
                    field = "file_id" if typ == "photo" else "cover_file_id"
                    if typ not in {"photo", "video"} or not item.get(field):
                        continue
                    source = root / f"source-{index}"
                    output = root / f"branded-{index}.jpg"
                    await download(bot, item[field], source, SOURCE_LIMIT)
                    await overlay(source, logo, output, "photo", settings)
                    item[field] = FSInputFile(output)
            except Exception as exc:
                # No publication has been attempted, so preparation failures are safe to retry.
                raise ValueError(tr("Логотип не добавлен. ") + str(exc)) from exc
            try:
                result = await send_content(
                    bot, chat_id, prepared, markup, message_thread_id=message_thread_id
                )
            except PartialAlbumError as exc:
                if payload["content_type"] == "album":
                    payload["media_json"] = entity_list(payload.get("media_json"))
                remember_media(payload, exc.messages)
                raise
            if payload["content_type"] == "album":
                payload["media_json"] = entity_list(payload.get("media_json"))
            remember_media(payload, result if isinstance(result, list) else [result])
            return result
