"""middleware components."""

import html
import logging

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app import accounts as accounts
from app import preferences, ui
from app.features import sources as features_sources
from app.features.channels import dismiss_picker
from app.i18n import language_context, tr

log = logging.getLogger(__name__)


class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.chat.type != "private":
            from services.forum_topics import observe

            observe(event)
            if not event.successful_payment:
                await features_sources.ingest_telegram(event, data["bot"])
            return
        user = getattr(event, "from_user", None)
        if not user:
            return
        if isinstance(event, Message) and event.chat.type == "private":
            await ui.note_message_async(user.id, event.message_id)
        language_context.set(preferences.get_preferences(user.id)["language"])
        payment = isinstance(event, Message) and event.successful_payment is not None
        if accounts.blocked(user.id) and not payment:
            if isinstance(event, CallbackQuery):
                await event.answer(tr("Доступ запрещён."), show_alert=True)
            return
        # Do not register public button clicks before /start: referrals are attributed on first start.
        public = isinstance(event, CallbackQuery) and (event.data or "").startswith(
            (
                "action:",
                "react:",
                "sub:",
                "alert:",
                "demo",
                "lb:",
                "contest:participate:",
            )
        )
        if not public and not (
            isinstance(event, Message) and (event.text or "").startswith("/start")
        ):
            await accounts.ensure_user_async(user)
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
            and event.data not in {"channel:add", "channel:picker"}
        ) or (
            isinstance(event, Message)
            and (event.text or "").split(" ")[0].split("@")[0] in {"/cancel", "/start"}
        ):
            await dismiss_picker(data["bot"], user.id)
        if (
            isinstance(event, CallbackQuery)
            and not public
            and not (event.data or "").startswith(
                (
                    "bw:",
                    "aw:",
                    "tw:",
                    "cw:",
                    "download:",
                    "multi:",
                    "contest:",
                    "broadcast:",
                    "live:",
                )
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
                await ui.answer(event, html.escape(msg) + tr("\n/cancel — отмена"))
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
                await ui.answer(
                    event,
                    tr(
                        "Не удалось завершить действие. Попробуйте снова; /cancel — отмена."
                    ),
                )
