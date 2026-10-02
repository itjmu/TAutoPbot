"""scheduler components."""

import asyncio
import html
import logging
import secrets
from datetime import timedelta

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app import access as access
from app import accounts as accounts
from app import database as database
from app import timeutils as timeutils
from app.features import conditions as features_conditions
from app.features import posts as features_posts
from app.i18n import activate, tr

log = logging.getLogger(__name__)


async def scheduler_publish(bot):
    rows = database.all_rows(
        "SELECT DISTINCT s.post_id FROM scheduled_posts s JOIN posts p ON p.id=s.post_id WHERE s.active=1 AND p.status='scheduled' AND julianday(s.publish_at)<=julianday(?) ORDER BY s.publish_at LIMIT 200",
        (timeutils.iso(),),
    )
    slots = asyncio.Semaphore(4)

    async def publish(r):
        async with slots:
            await publish_one(r)

    async def publish_one(r):
        try:
            await features_posts.execute_publish(r["post_id"], bot, automatic=True)
            p = database.one(
                "SELECT owner_telegram_id FROM posts WHERE id=?", (r["post_id"],)
            )
            await features_posts.show_post(
                bot, p["owner_telegram_id"], r["post_id"], False, panel=False
            )
        except Exception:
            log.exception("Scheduled post failed: %s", r["post_id"])

    await asyncio.gather(*(publish(r) for r in rows))
    database.execute(
        "UPDATE multipost_batches SET status='completed' WHERE status='scheduled' AND NOT EXISTS (SELECT 1 FROM multipost_items i JOIN posts p ON p.id=i.post_id WHERE i.batch_id=multipost_batches.id AND p.status IN ('draft','scheduled','publishing'))"
    )


async def scheduler_delete(bot):
    rows = database.all_rows(
        "SELECT m.*,c.telegram_chat_id,c.owner_telegram_id FROM published_messages m JOIN channels c ON c.id=m.channel_id WHERE m.deleted=0 AND m.delete_at IS NOT NULL AND julianday(m.delete_at)<=julianday(?) LIMIT 50",
        (timeutils.iso(),),
    )
    for r in rows:
        try:
            await bot.delete_message(r["telegram_chat_id"], r["telegram_message_id"])
        except TelegramBadRequest as exc:
            activate(r["owner_telegram_id"])
            if "message to delete not found" not in str(exc).lower():
                database.execute(
                    "UPDATE published_messages SET deleted=-1,delete_error=? WHERE id=?",
                    (str(exc)[:250], r["id"]),
                )
                await notify_once(
                    bot,
                    r["owner_telegram_id"],
                    "delete:" + str(r["id"]),
                    tr(
                        "Не удалось удалить публикацию. Проверьте право бота удалять сообщения. Подробности: "
                    )
                    + html.escape(str(exc)[:180]),
                )
                continue
        except TelegramForbiddenError as exc:
            activate(r["owner_telegram_id"])
            database.execute(
                "UPDATE published_messages SET deleted=-1,delete_error=? WHERE id=?",
                (str(exc)[:250], r["id"]),
            )
            await notify_once(
                bot,
                r["owner_telegram_id"],
                "delete:" + str(r["id"]),
                tr(
                    "Автоудаление не выполнено: боту не хватает прав. Удалите сообщение вручную и верните боту право удалять сообщения."
                ),
            )
            continue
        except Exception:
            log.exception("Delete failed: %s", r["id"])
            continue
        database.execute(
            "UPDATE published_messages SET deleted=1,delete_error=NULL WHERE id=?",
            (r["id"],),
        )


async def scheduler_pins(bot):
    from services import post_pins

    await post_pins.process(bot)


async def notify_once(bot, uid, key, text):
    if database.one("SELECT 1 FROM notification_keys WHERE key=?", (key,)):
        return
    try:
        await bot.send_message(uid, text)
    except (TelegramBadRequest, TelegramForbiddenError):
        return
    database.execute(
        "INSERT OR IGNORE INTO notification_keys VALUES(?,?)", (key, timeutils.iso())
    )


async def scheduler_rights(bot):
    rows = database.all_rows(
        "SELECT * FROM channels WHERE is_active=1 AND (rights_checked_at IS NULL OR rights_checked_at<=?) "
        "AND (rights_retry_at IS NULL OR rights_retry_at<=?) "
        "ORDER BY COALESCE(rights_checked_at,''),id LIMIT 32",
        (timeutils.iso(timeutils.now() - timedelta(hours=6)), timeutils.iso()),
    )

    async def check_one(r):
        activate(r["owner_telegram_id"])
        try:
            ok = await access.owner_and_bot_ok(
                bot, r["telegram_chat_id"], r["owner_telegram_id"]
            )
            database.execute(
                "UPDATE channels SET rights_checked_at=?,rights_retry_at=NULL WHERE id=?",
                (timeutils.iso(), r["id"]),
            )
            if not ok and r["bot_is_admin"]:
                database.execute(
                    "UPDATE channels SET bot_is_admin=0,updated_at=? WHERE id=?",
                    (timeutils.iso(), r["id"]),
                )
                await notify_once(
                    bot,
                    r["owner_telegram_id"],
                    "rights:" + str(r["id"]) + ":" + r["updated_at"],
                    tr("⚠️ Потеряны права: ") + html.escape(r["title"]),
                )
        except Exception:
            database.execute(
                "UPDATE channels SET rights_retry_at=? WHERE id=?",
                (timeutils.iso(timeutils.now() + timedelta(minutes=1)), r["id"]),
            )
            log.exception("Rights check failed")

    slots = asyncio.Semaphore(4)

    async def check(r):
        async with slots:
            await check_one(r)

    await asyncio.gather(*(check(r) for r in rows))


async def scheduler_premium_notifications(bot):
    rows = database.all_rows(
        "SELECT * FROM premium WHERE active=1 AND lifetime=0 AND expires_at IS NOT NULL"
    )
    for r in rows:
        activate(r["user_id"])
        exp = timeutils.parse_dt(r["expires_at"])
        if not exp:
            continue
        left = (exp - timeutils.now()).total_seconds()
        if left <= 0:
            await notify_once(
                bot,
                r["user_id"],
                f"expired:{r['user_id']}:{r['expires_at']}",
                tr("Premium закончился. Данные сохранены; действуют лимиты Free."),
            )
            database.execute(
                "UPDATE premium SET active=0 WHERE user_id=?", (r["user_id"],)
            )
        elif left <= 86400:
            await notify_once(
                bot,
                r["user_id"],
                f"expiring:{r['user_id']}:{r['expires_at']}",
                tr("Premium закончится в течение 24 часов."),
            )


async def scheduler_requests(bot):
    for approved in database.all_rows(
        "SELECT id FROM join_requests WHERE status='approved' AND approval_notified_at IS NULL "
        "AND COALESCE(approval_delivery_state,'pending')='pending' "
        "AND (approval_retry_at IS NULL OR approval_retry_at<=?) ORDER BY id LIMIT 100",
        (timeutils.iso(),),
    ):
        try:
            await features_conditions.notify_join_approved(approved["id"], bot)
        except Exception:
            log.exception("Approval notification failed")
    rows = database.all_rows(
        "SELECT j.id,j.last_checked,c.owner_telegram_id FROM join_requests j JOIN channels c ON c.id=j.channel_id WHERE j.status='pending' AND c.is_active=1 AND c.auto_requests=1 AND (j.next_check_at IS NULL OR j.next_check_at<=?) AND j.telegram_user_id NOT IN (SELECT telegram_id FROM blocked_users) AND c.owner_telegram_id NOT IN (SELECT telegram_id FROM blocked_users) ORDER BY COALESCE(j.next_check_at,''),j.id LIMIT 100",
        (timeutils.iso(),),
    )
    for r in rows:
        last = timeutils.parse_dt(r["last_checked"])
        hours = 8 if accounts.has_premium(r["owner_telegram_id"]) else 168
        database.execute(
            "UPDATE join_requests SET next_check_at=? WHERE id=?",
            (
                timeutils.iso(
                    max(timeutils.now(), last or timeutils.now())
                    + timedelta(hours=hours)
                )
                if not last or timeutils.now() - last >= timedelta(hours=hours)
                else timeutils.iso(last + timedelta(hours=hours)),
                r["id"],
            ),
        )
        if not last or timeutils.now() - last >= timedelta(hours=hours):
            try:
                await features_conditions.check_request(r["id"], bot)
            except Exception:
                log.exception("Request check failed")
                database.execute(
                    "UPDATE join_requests SET next_check_at=? WHERE id=?",
                    (timeutils.iso(timeutils.now() + timedelta(minutes=5)), r["id"]),
                )
    for r in database.all_rows(
        "SELECT invited_id FROM referrals WHERE rewarded_at IS NULL ORDER BY id LIMIT 500"
    ):
        try:
            await accounts.check_referral(r["invited_id"], bot)
        except Exception:
            log.exception("Referral check failed")


RUN_OWNER = secrets.token_hex(16)


class RuntimeLock:
    """OS-owned lock: automatically released even after a forced process exit."""

    def __init__(self, database_path):
        self.path = database_path
        self.stream = None

    def acquire(self):
        import os
        from pathlib import Path

        if self.path == ":memory:":
            return True
        path = Path(self.path).resolve().with_suffix(Path(self.path).suffix + ".lock")
        self.stream = path.open("a+b")
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            return False
        return True

    def release(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def acquire_runtime_lock():
    with database.atomic():
        database.db.execute(
            "INSERT OR REPLACE INTO runtime_lock VALUES(1,?,?)",
            (RUN_OWNER, timeutils.iso()),
        )
        database.db.execute(
            "UPDATE post_targets SET status='uncertain',error='Перезапуск во время отправки; проверьте канал вручную' WHERE status='sending'"
        )
        database.db.execute(
            "UPDATE posts SET status='uncertain' WHERE status='publishing'"
        )


async def heartbeat():
    if (
        await database.async_call(
            lambda conn: (
                conn.execute(
                    "UPDATE runtime_lock SET heartbeat=? WHERE id=1 AND owner=?",
                    (timeutils.iso(), RUN_OWNER),
                ).rowcount
            )
        )
        != 1
    ):
        raise RuntimeError(tr("Потеряна блокировка процесса."))
