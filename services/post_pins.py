"""Durable pin/unpin jobs, independent from post delivery retries."""

import asyncio
import html
import logging
from datetime import timedelta

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from app import database as db
from app import timeutils
from app.i18n import activate, tr

_lock = asyncio.Lock()
log = logging.getLogger(__name__)


async def check_rights(bot, chat_id, uid):
    me = await bot.get_me()
    chat = await bot.get_chat(chat_id)
    right = "can_edit_messages" if chat.type == "channel" else "can_pin_messages"
    for member_id in (uid, me.id):
        member = await bot.get_chat_member(chat_id, member_id)
        if member.status == ChatMemberStatus.CREATOR:
            continue
        if member.status != ChatMemberStatus.ADMINISTRATOR or not getattr(
            member, right, False
        ):
            raise ValueError(
                tr(
                    "Для закрепления нужны права владельца и бота: закреплять сообщения в группе или редактировать сообщения в канале."
                )
            )


async def process(bot, pid=None):
    async with _lock:
        rows = db.all_rows(
            "SELECT m.*,c.telegram_chat_id,c.owner_telegram_id FROM published_messages m "
            "JOIN channels c ON c.id=m.channel_id WHERE m.deleted=0 "
            "AND (m.pin_state='pending' OR (m.pin_state='pinned' AND m.unpin_at IS NOT NULL AND julianday(m.unpin_at)<=julianday(?))) "
            "AND (m.pin_retry_at IS NULL OR julianday(m.pin_retry_at)<=julianday(?)) "
            + ("AND m.post_id=? " if pid is not None else "")
            + "ORDER BY m.id LIMIT 50",
            (timeutils.iso(), timeutils.iso(), *((pid,) if pid is not None else ())),
        )
        for r in rows:
            expired = (
                r["unpin_at"] and timeutils.parse_dt(r["unpin_at"]) <= timeutils.now()
            )
            # A pending pin may have succeeded before a crash; expired jobs unpin by exact ID.
            unpin = r["pin_state"] == "pinned" or expired
            try:
                if unpin:
                    await bot.unpin_chat_message(
                        chat_id=r["telegram_chat_id"],
                        message_id=r["telegram_message_id"],
                    )
                else:
                    await bot.pin_chat_message(
                        chat_id=r["telegram_chat_id"],
                        message_id=r["telegram_message_id"],
                        disable_notification=True,
                    )
            except TelegramRetryAfter as exc:
                db.execute(
                    "UPDATE published_messages SET pin_retry_at=? WHERE id=?",
                    (
                        timeutils.iso(
                            timeutils.now() + timedelta(seconds=exc.retry_after + 1)
                        ),
                        r["id"],
                    ),
                )
                continue
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                if unpin and any(
                    s in str(exc).lower()
                    for s in (
                        "message to unpin not found",
                        "message is not pinned",
                        "message not found",
                    )
                ):
                    pass
                else:
                    db.execute(
                        "UPDATE published_messages SET pin_state='failed',pin_error=? WHERE id=?",
                        (str(exc)[:250], r["id"]),
                    )
                    activate(r["owner_telegram_id"])
                    from app.scheduler import notify_once

                    await notify_once(
                        bot,
                        r["owner_telegram_id"],
                        f"pin:{r['id']}",
                        tr(
                            "Не удалось закрепить или открепить пост. Проверьте права бота. Сообщение: {mid}. {error}",
                            mid=r["telegram_message_id"],
                            error=html.escape(str(exc)[:150]),
                        ),
                    )
                    continue
            except Exception as exc:
                db.execute(
                    "UPDATE published_messages SET pin_retry_at=?,pin_error=? WHERE id=?",
                    (
                        timeutils.iso(timeutils.now() + timedelta(seconds=30)),
                        str(exc)[:250],
                        r["id"],
                    ),
                )
                log.warning("Pin job will retry for publication %s", r["id"])
                continue
            db.execute(
                "UPDATE published_messages SET pin_state=?,pin_retry_at=NULL,pin_error=NULL WHERE id=?",
                ("done" if unpin else "pinned", r["id"]),
            )
