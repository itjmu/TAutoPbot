"""middleware components."""

import html
import logging

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app import accounts as accounts
from app import preferences
from app.features import sources as features_sources
from app.i18n import language_context, tr

log = logging.getLogger(__name__)


class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.chat.type != "private":
            if not event.successful_payment:
                await features_sources.ingest_telegram(event, data["bot"])
            return
        user = getattr(event, "from_user", None)
        if not user:
            return
        language_context.set(preferences.get_preferences(user.id)["language"])
        if accounts.blocked(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer(tr("Доступ запрещён."), show_alert=True)
            return
        # Do not register public button clicks before /start: referrals are attributed on first start.
        public = isinstance(event, CallbackQuery) and (event.data or "").startswith(
            ("action:", "react:", "sub:", "alert:", "demo", "contest:participate:")
        )
        if not public and not (
            isinstance(event, Message) and (event.text or "").startswith("/start")
        ):
            accounts.ensure_user(user)
        if (
            isinstance(event, CallbackQuery)
            and not public
            and (not event.message or event.message.chat.type != "private")
        ):
            await event.answer(tr("Откройте личный чат с ботом."))
            return
        if (
            isinstance(event, CallbackQuery)
            and not public
            and not (event.data or "").startswith(
                ("bw:", "aw:", "tw:", "cw:", "download:", "multi:", "contest:")
            )
        ):
            await data["state"].clear()
        try:
            return await handler(event, data)
        except (ValueError, KeyError, IndexError) as exc:
            log.warning("Rejected input: %s", exc)
            msg = str(exc)[:180] or tr("Неверные данные.")
            if isinstance(event, CallbackQuery):
                await event.answer(msg, show_alert=True)
            else:
                await event.answer(html.escape(msg) + tr("\n/cancel — отмена"))
        except Exception:
            log.exception("Handler failed")
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer(
                        tr("Операция не завершена. Черновик сохранён."), show_alert=True
                    )
                except TelegramBadRequest:
                    pass
            else:
                await event.answer(
                    tr(
                        "Не удалось завершить действие. Попробуйте снова; /cancel — отмена."
                    )
                )
