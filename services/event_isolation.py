"""Per-key event isolation without retaining completed user locks forever."""

import asyncio
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary

from aiogram.fsm.storage.base import BaseEventIsolation


class EventIsolation(BaseEventIsolation):
    def __init__(self):
        self.locks = WeakValueDictionary()

    @asynccontextmanager
    async def lock(self, key):
        # The local reference keeps the lock alive for holders AND waiting tasks.
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

    async def close(self):
        pass
