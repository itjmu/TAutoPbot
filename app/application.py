"""application components."""

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import SimpleEventIsolation
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import database as database
from app import scheduler as scheduled_jobs
from app import storage as storage
from app.features import sources as features_sources
from app.i18n import tr
from app.routing import build_router
from config import BOT_TOKEN, DB_FILE, validate_config
from services import contests
from services.jobs import download_jobs

log = logging.getLogger(__name__)


async def main():
    validate_config()
    lock = scheduled_jobs.RuntimeLock(DB_FILE)
    if not lock.acquire():
        print(tr("Используется уже работающий экземпляр бота."))
        return
    try:
        await run_application()
    finally:
        if database.db is not None:
            database.db.close()
            database.db = None
        lock.release()


async def run_application():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    validate_config()
    database.init_db()
    contests.recover()
    scheduled_jobs.acquire_runtime_lock()
    database.execute(
        "UPDATE downloads SET status='failed',error='Перезапуск бота; выберите формат ещё раз' WHERE status='running'"
    )
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(
        storage=storage.SQLiteStorage(), events_isolation=SimpleEventIsolation()
    )
    dp.include_router(build_router())
    scheduler = AsyncIOScheduler(timezone="UTC")
    for function, seconds in [
        (scheduled_jobs.scheduler_publish, 10),
        (contests.tick, 10),
        (features_sources.flush_albums, 2),
        (scheduled_jobs.scheduler_delete, 30),
        (scheduled_jobs.scheduler_requests, 300),
        (scheduled_jobs.scheduler_rights, 21600),
        (scheduled_jobs.scheduler_premium_notifications, 1800),
    ]:
        scheduler.add_job(
            function,
            "interval",
            seconds=seconds,
            args=[bot],
            max_instances=1,
            coalesce=True,
        )
    scheduler.add_job(
        scheduled_jobs.heartbeat, "interval", seconds=20, max_instances=1, coalesce=True
    )
    try:
        me = await bot.get_me()
        log.info("Started v4.0.0 @%s", me.username)
        scheduler.start()
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await download_jobs.close()
        await bot.session.close()
        database.execute(
            "DELETE FROM runtime_lock WHERE owner=?", (scheduled_jobs.RUN_OWNER,)
        )
        database.db.close()
        database.db = None
