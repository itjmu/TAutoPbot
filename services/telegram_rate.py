"""Pace outgoing traffic; only durable callers decide whether a send can be retried."""

import asyncio
import time

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramRetryAfter


class TelegramRateLimit(BaseRequestMiddleware):
    def __init__(self):
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.blocked_until = 0.0
        self.chats = {}
        self.chat_types = {}

    async def __call__(self, make_request, bot, method):
        name = method.__api_method__
        chat = getattr(method, "chat_id", None)
        outgoing = name.startswith(("send", "copy", "forward", "editMessage"))
        if name not in {"getUpdates", "answerCallbackQuery", "answerPreCheckoutQuery"}:
            while True:
                async with self.lock:
                    now = time.monotonic()
                    due = max(self.next_request, self.blocked_until)
                    if outgoing and chat is not None:
                        due = max(due, self.chats.get(chat, 0))
                    delay = due - now
                    if delay <= 0:
                        self.next_request = now + 0.05
                        if outgoing and chat is not None:
                            spacing = (
                                3.1
                                if self.chat_types.get(chat) in {"group", "supergroup"}
                                else 1.05
                            )
                            self.chats[chat] = now + spacing
                        if len(self.chats) > 2000:
                            self.chats = {
                                key: value
                                for key, value in self.chats.items()
                                if value > now
                            }
                        break
                await asyncio.sleep(delay)
        try:
            result = await make_request(bot, method)
            if name == "getChat":
                if len(self.chat_types) >= 2000:
                    self.chat_types.clear()
                self.chat_types[result.id] = result.type
            return result
        except TelegramRetryAfter as exc:
            self.blocked_until = max(
                self.blocked_until, time.monotonic() + exc.retry_after + 1
            )
            raise
