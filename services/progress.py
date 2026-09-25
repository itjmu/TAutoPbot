"""Throttled, single-message progress for worker downloads and Telegram uploads."""

import json
import time
from contextvars import ContextVar
from pathlib import Path

from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.i18n import tr

current_progress = ContextVar("media_progress", default=None)
_last_write = {}


def write_worker_progress(folder, phase, done=0, total=None):
    key = str(folder)
    clock = time.monotonic()
    previous, previous_phase = _last_write.get(key, (0, None))
    if phase == previous_phase and clock - previous < 0.5:
        return
    target = folder / "progress.json"
    temporary = folder / "progress.tmp"
    try:
        temporary.write_text(
            json.dumps({"phase": phase, "done": done, "total": total}), encoding="utf-8"
        )
        temporary.replace(target)
    except OSError:
        # Windows may briefly lock the report while the parent reads it.
        # Progress is advisory: never fail the media download for a report.
        return
    _last_write[key] = (clock, phase)


class ProgressMessage:
    def __init__(self, bot, uid):
        self.bot = bot
        self.uid = uid
        self.message_id = None
        self.last = 0
        self.last_text = ""
        self.item = ""

    async def update(self, phase, done=0, total=None, force=False, terminal=False):
        labels = {
            "download": tr("Скачивание"),
            "upload": tr("Отправка в Telegram"),
            "processing": tr("Подготовка видео"),
            "queued": tr("Ожидание очереди"),
            "done": tr("Готово"),
            "cancelled": tr("Загрузка остановлена"),
            "failed": tr("Не удалось завершить загрузку"),
        }
        text = labels.get(phase, phase)
        if self.item:
            text += "\n" + self.item
        if total:
            percent = min(100, int(done * 100 / total))
            text += (
                f"\n{percent}% · {done / 1_000_000:.1f} / {total / 1_000_000:.1f} MB"
            )
            if phase == "upload" and done >= total:
                text += "\n" + tr("Ожидание подтверждения Telegram…")
        elif done:
            text += f"\n{done / 1_000_000:.1f} MB"
        if not force and (time.monotonic() - self.last < 2 or text == self.last_text):
            return
        markup = (
            None
            if terminal
            else InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=tr("⏹ Остановить"), callback_data="download:cancel"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🏠 " + tr("Главное меню"),
                            callback_data="download:menu",
                        )
                    ],
                ]
            )
        )
        try:
            if self.message_id is None:
                sent = await self.bot.send_message(
                    self.uid, text, reply_markup=markup, parse_mode=None
                )
                self.message_id = sent.message_id
            else:
                await self.bot.edit_message_text(
                    text,
                    chat_id=self.uid,
                    message_id=self.message_id,
                    reply_markup=markup,
                    parse_mode=None,
                )
        except TelegramAPIError:
            pass
        self.last = time.monotonic()
        self.last_text = text


class ProgressInputFile(FSInputFile):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.readers = set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        for reader in tuple(self.readers):
            await reader.aclose()
        self.readers.clear()

    async def read(self, bot):
        progress = current_progress.get()
        sent = 0
        total = Path(self.path).stat().st_size
        if progress:
            await progress.update("upload", 0, total, force=True)
        reader = super().read(bot)
        self.readers.add(reader)
        try:
            async for chunk in reader:
                yield chunk
                sent += len(chunk)
                if progress:
                    await progress.update("upload", sent, total)
        finally:
            await reader.aclose()
            self.readers.discard(reader)
        if progress:
            await progress.update("upload", sent, total, force=True)
