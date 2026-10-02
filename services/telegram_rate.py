"""Pace outgoing traffic; only durable callers decide whether a send can be retried."""

import asyncio
import time
from collections import OrderedDict
from contextvars import ContextVar

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramRetryAfter

background_traffic = ContextVar("background_traffic", default=False)


class TelegramRateLimit(BaseRequestMiddleware):
    def __init__(self):
        self.next_request = 0.0
        self.blocked_until = 0.0
        self.chats = {}
        self.chat_types = OrderedDict()
        self.pending = []
        self.capacity = asyncio.Semaphore(512)
        self.changed = asyncio.Event()
        self.dispatcher = None

    async def _dispatch(self):
        try:
            while self.pending:
                now = time.monotonic()
                self.pending[:] = [item for item in self.pending if not item[0].done()]
                if not self.pending:
                    break

                def due(item):
                    return max(
                        self.next_request,
                        self.blocked_until,
                        self.chats.get(item[1], 0) if item[2] else 0,
                    )

                ready = [item for item in self.pending if due(item) <= now]
                if ready:
                    # Aged background calls join FIFO after two seconds.
                    item = min(
                        ready, key=lambda item: (item[3] and now - item[4] < 2, item[4])
                    )
                    self.pending.remove(item)
                    self.next_request = now + 0.05
                    if item[2] and item[1] is not None:
                        kind = self.chat_types.get(item[1])
                        group = kind in {"group", "supergroup"} or (
                            kind is None and isinstance(item[1], int) and item[1] < 0
                        )
                        self.chats[item[1]] = now + (3.1 if group else 1.05)
                    if len(self.chats) > 2048:
                        self.chats = {
                            key: value
                            for key, value in self.chats.items()
                            if value > now
                        }
                    item[0].set_result(None)
                    continue
                self.changed.clear()
                delay = max(0, min(due(item) for item in self.pending) - now)
                try:
                    await asyncio.wait_for(self.changed.wait(), delay)
                except TimeoutError:
                    pass
        finally:
            self.dispatcher = None

    async def __call__(self, make_request, bot, method):
        name = method.__api_method__
        chat = getattr(method, "chat_id", None)
        outgoing = name.startswith(("send", "copy", "forward", "editMessage"))
        if name not in {"getUpdates", "answerCallbackQuery", "answerPreCheckoutQuery"}:
            async with self.capacity:
                future = asyncio.get_running_loop().create_future()
                item = (
                    future,
                    chat,
                    outgoing,
                    background_traffic.get(),
                    time.monotonic(),
                )
                self.pending.append(item)
                self.changed.set()
                if self.dispatcher is None:
                    self.dispatcher = asyncio.create_task(self._dispatch())
                try:
                    await future
                finally:
                    if item in self.pending:
                        self.pending.remove(item)
                    self.changed.set()
        try:
            result = await make_request(bot, method)
            if name == "getChat":
                self.chat_types[result.id] = result.type
                self.chat_types.move_to_end(result.id)
                if len(self.chat_types) > 8192:
                    self.chat_types.popitem(last=False)
            return result
        except TelegramRetryAfter as exc:
            # Telegram does not identify the scope; retain conservative pacing.
            self.blocked_until = max(
                self.blocked_until, time.monotonic() + exc.retry_after + 1
            )
            self.changed.set()
            raise
