"""Supervised background tasks; callers return before slow work starts."""

import asyncio
import logging
from collections.abc import Callable, Coroutine

from app.i18n import tr

log = logging.getLogger(__name__)


class BackgroundJobs:
    def __init__(self):
        self.tasks: dict[int, asyncio.Task] = {}
        self.labels: dict[int, str] = {}
        self.closing = False

    def active(self, user_id):
        task = self.tasks.get(user_id)
        return task is not None and not task.done()

    def start(self, user_id: int, factory: Callable[[], Coroutine], label: str):
        if self.closing:
            raise ValueError(tr("Бот перезапускается. Повторите позже."))
        if self.active(user_id):
            raise ValueError(
                tr(
                    "У вас уже есть загрузка. Дождитесь её или отмените в разделе скачивания."
                )
            )
        if len(self.tasks) >= 32:
            raise ValueError(tr("Очередь загрузок заполнена. Попробуйте позже."))
        task = asyncio.create_task(factory(), name=f"download:{user_id}")
        self.tasks[user_id] = task
        self.labels[user_id] = label
        task.add_done_callback(lambda completed: self._finished(user_id, completed))
        return task

    def _finished(self, user_id, task):
        if self.tasks.get(user_id) is task:
            self.tasks.pop(user_id, None)
            self.labels.pop(user_id, None)
        if not task.cancelled() and task.exception():
            error = task.exception()
            log.error(
                "Background job failed for user %s",
                user_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    def cancel(self, user_id):
        if not self.active(user_id):
            return False
        task = self.tasks[user_id]
        if not task.cancelling():
            task.cancel()
        return True

    async def close(self):
        self.closing = True
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.labels.clear()


download_jobs = BackgroundJobs()
