"""Track in-flight updates/jobs so shutdown drains them before closing resources."""

import asyncio
from functools import wraps

from aiogram import BaseMiddleware


class RuntimeTasks(BaseMiddleware):
    def __init__(self):
        self.tasks = set()

    async def __call__(self, handler, event, data):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            return await handler(event, data)
        finally:
            self.tasks.discard(task)

    def job(self, function):
        @wraps(function)
        async def run(*args, **kwargs):
            task = asyncio.current_task()
            self.tasks.add(task)
            try:
                return await function(*args, **kwargs)
            finally:
                self.tasks.discard(task)

        return run

    async def close(self):
        tasks = set(self.tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=25)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
