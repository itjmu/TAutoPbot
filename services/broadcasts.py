"""Durable, bounded broadcast delivery. Ambiguous sends are never auto-repeated."""

import json
import logging
from datetime import timedelta

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from app import accounts, timeutils, ui
from app import database as db


def enqueue(owner, draft):
    with db.atomic():
        db.db.execute(
            "INSERT OR IGNORE INTO broadcast_jobs(owner_id,token,payload,created_at) VALUES(?,?,?,?)",
            (owner, draft["token"], json.dumps(draft), timeutils.iso()),
        )
        jid = db.one("SELECT id FROM broadcast_jobs WHERE token=?", (draft["token"],))[
            0
        ]
        db.db.execute(
            "INSERT OR IGNORE INTO broadcast_targets(job_id,user_id) SELECT ?,telegram_id FROM users WHERE is_blocked=0 AND telegram_id NOT IN (SELECT telegram_id FROM blocked_users)",
            (jid,),
        )
    return jid


def recover():
    db.execute(
        "UPDATE broadcast_targets SET status='uncertain',error='Restart during delivery' WHERE status='sending'"
    )


async def tick(bot):
    rows = db.all_rows(
        "SELECT t.*,j.payload FROM broadcast_targets t JOIN broadcast_jobs j ON j.id=t.job_id WHERE t.status='pending' AND (t.retry_at IS NULL OR t.retry_at<=?) ORDER BY t.job_id,t.user_id LIMIT 40",
        (timeutils.iso(),),
    )
    for row in rows:
        key = (row["job_id"], row["user_id"])
        if accounts.blocked(row["user_id"]):
            db.execute(
                "UPDATE broadcast_targets SET status='skipped' WHERE job_id=? AND user_id=?",
                key,
            )
            continue
        if not db.execute(
            "UPDATE broadcast_targets SET status='sending' WHERE job_id=? AND user_id=? AND status='pending'",
            key,
        ).rowcount:
            continue
        draft = json.loads(row["payload"])
        try:
            if len(draft["ids"]) > 1:
                sent = await bot.copy_messages(
                    row["user_id"], draft["chat_id"], sorted(draft["ids"])
                )
                if len(sent) != len(draft["ids"]):
                    db.execute(
                        "UPDATE broadcast_targets SET status='uncertain',error='Partial album copied' WHERE job_id=? AND user_id=?",
                        key,
                    )
                    continue
            else:
                sent = await bot.copy_message(
                    row["user_id"], draft["chat_id"], draft["ids"][0]
                )
            db.execute(
                "UPDATE broadcast_targets SET status='sent',error=NULL WHERE job_id=? AND user_id=?",
                key,
            )
            try:
                ui.note_sent(row["user_id"], sent)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Broadcast delivered; UI tracking failed"
                )
        except TelegramRetryAfter as exc:
            due = timeutils.iso(
                timeutils.now() + timedelta(seconds=exc.retry_after + 1)
            )
            db.execute(
                "UPDATE broadcast_targets SET status='pending',retry_at=?,error=? WHERE job_id=? AND user_id=?",
                (due, str(exc)[:250], *key),
            )
            # Telegram may apply the restriction to the entire bot.
            db.execute(
                "UPDATE broadcast_targets SET retry_at=? WHERE status='pending' AND (retry_at IS NULL OR retry_at<?)",
                (due, due),
            )
            break
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            db.execute(
                "UPDATE broadcast_targets SET status='failed',error=? WHERE job_id=? AND user_id=?",
                (str(exc)[:250], *key),
            )
        except Exception as exc:
            db.execute(
                "UPDATE broadcast_targets SET status='uncertain',error=? WHERE job_id=? AND user_id=?",
                (str(exc)[:250], *key),
            )
    db.execute(
        "UPDATE broadcast_jobs SET status=CASE WHEN EXISTS(SELECT 1 FROM broadcast_targets t WHERE t.job_id=broadcast_jobs.id AND t.status IN ('failed','uncertain')) THEN 'review' ELSE 'completed' END WHERE status='queued' AND NOT EXISTS(SELECT 1 FROM broadcast_targets t WHERE t.job_id=broadcast_jobs.id AND t.status IN ('pending','sending'))"
    )
