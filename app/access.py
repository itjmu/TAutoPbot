"""access components."""

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message

from app import accounts as accounts
from app.i18n import tr
from config import ADMIN_ID


async def access_message(message: Message) -> bool:
    if accounts.blocked(message.from_user.id):
        await message.answer(tr("⛔ Доступ запрещён."))
        return False
    await accounts.ensure_user_async(message.from_user)
    return True


async def access_callback(c: CallbackQuery) -> bool:
    if accounts.blocked(c.from_user.id):
        await c.answer(tr("⛔ Доступ запрещён."), show_alert=True)
        return False
    await accounts.ensure_user_async(c.from_user)
    return True


def admin_only(c):
    return c.from_user.id == ADMIN_ID


async def owner_and_bot_ok(bot, chat_id, uid):
    try:
        me = await bot.get_me()
        owner = await bot.get_chat_member(chat_id, uid)
        member = await bot.get_chat_member(chat_id, me.id)
        admins = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
        if owner.status not in admins or member.status not in admins:
            return False
        chat = await bot.get_chat(chat_id)
        if (
            chat.type == "channel"
            and member.status != ChatMemberStatus.CREATOR
            and not getattr(member, "can_post_messages", False)
        ):
            return False
        if (
            chat.type == "channel"
            and owner.status != ChatMemberStatus.CREATOR
            and not getattr(owner, "can_post_messages", False)
        ):
            return False
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


async def member_ok(bot, chat_id, uid):
    try:
        m = await bot.get_chat_member(chat_id, uid)
        return m.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        } or (m.status == ChatMemberStatus.RESTRICTED and m.is_member)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False
