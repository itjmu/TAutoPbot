"""Offline preview workload using the real pacer and a 10 ms simulated API."""

import asyncio
import json
import statistics
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_planning as fixtures
from aiogram.methods import EditMessageReplyMarkup, SendMessage

from app import content, ui
from app.features import posts
from services.telegram_rate import TelegramRateLimit


async def main():
    case = fixtures.PlanningTests()
    case.setUp()
    timings, requests = [], []
    try:
        for i in range(3):
            pacer = TelegramRateLimit()
            calls = []

            async def transport(bot, method):
                calls.append(method.__api_method__)
                await asyncio.sleep(0.01)
                return SimpleNamespace(message_id=100 + i)

            async def send(chat_id, text, **kwargs):
                return await pacer(
                    transport, None, SendMessage(chat_id=chat_id, text=text, **kwargs)
                )

            async def edit(**kwargs):
                return await pacer(transport, None, EditMessageReplyMarkup(**kwargs))

            case.bot.send_message = send
            case.bot.edit_message_reply_markup = edit
            pid = content.create_draft(42, dict(content_type="text", text="Preview"))
            with (
                patch.object(ui, "clear_controls", new=AsyncMock()),
                patch.object(ui, "track_controls", new=AsyncMock()),
            ):
                started = time.perf_counter()
                await posts.show_post(case.bot, 42, pid)
                timings.append(time.perf_counter() - started)
            requests.append(calls)
        print(
            json.dumps(
                dict(
                    median_seconds=round(statistics.median(timings), 4),
                    requests=requests,
                )
            )
        )
    finally:
        case.tearDown()


if __name__ == "__main__":
    asyncio.run(main())
