"""features / channels components."""

import html
from datetime import timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ChatAdministratorRights,
    ChatMemberUpdated,
    InlineKeyboardButton,
    KeyboardButton,
    KeyboardButtonRequestChat,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app import access as access
from app import accounts as accounts
from app import content as content
from app import database as database
from app import timeutils as timeutils
from app import ui as ui
from app.i18n import activate, tr
from app.states import AddChannel
from services.telegram_links import chat_reference, supports_requests

router = Router(name="features.channels")


@router.callback_query(F.data == "menu:channels")
async def menu_channels(c: CallbackQuery):
    await channel_list(c)


@router.callback_query(F.data == "channel:add")
async def channel_add(c: CallbackQuery, state: FSMContext, bot: Bot):
    if not await access.access_callback(c):
        return
    if accounts.channel_limit(c.from_user.id) >= 0 and accounts.channel_count(
        c.from_user.id
    ) >= accounts.channel_limit(c.from_user.id):
        await c.answer(tr("Лимит объектов достигнут."), show_alert=True)
        return
    await state.set_state(AddChannel.waiting)
    database.execute(
        "INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"channel_setup:{c.from_user.id}", timeutils.iso()),
    )
    await ui.edit(c, tr("➕ Добавить"), ui.back("menu:channels"))
    rights = dict(
        is_anonymous=False,
        can_send_welcome_messages=False,
        can_manage_chat=True,
        can_delete_messages=True,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=True,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
    )
    channel_rights = ChatAdministratorRights(
        **rights, can_post_messages=True, can_edit_messages=True
    )
    group_rights = ChatAdministratorRights(
        **{**rights, "can_restrict_members": True}, can_pin_messages=True
    )
    sent = await c.message.answer(
        tr(
            "➕ <b>Добавление</b>\n\nОтправьте @username канала/группы или перешлите сообщение из него.\n\nБот должен быть администратором."
        ),
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text=tr("➕ Выбрать канал и добавить бота"),
                        request_chat=KeyboardButtonRequestChat(
                            request_id=701,
                            chat_is_channel=True,
                            user_administrator_rights=channel_rights,
                            bot_administrator_rights=channel_rights,
                        ),
                    ),
                    KeyboardButton(
                        text=tr("➕ Выбрать группу"),
                        request_chat=KeyboardButtonRequestChat(
                            request_id=702,
                            chat_is_channel=False,
                            user_administrator_rights=group_rights,
                            bot_administrator_rights=group_rights,
                        ),
                    ),
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )
    ui.note_sent(c.from_user.id, sent)
    await c.answer()


@router.callback_query(F.data == "channel:picker")
async def channel_picker(c: CallbackQuery, state: FSMContext):
    # Compatibility with older inline buttons.
    await channel_add(c, state, c.bot)


@router.message(F.chat_shared.request_id.in_({701, 702}))
async def channel_shared(message: Message, state: FSMContext, bot: Bot):
    # ChatShared also arrives when the bot was already an administrator, unlike
    # my_chat_member. Always recheck both parties' permissions server-side.
    chat = await bot.get_chat(message.chat_shared.chat_id)
    await connect_channel(message, state, bot, chat)


async def connect_channel(message, state, bot, chat):
    uid = message.from_user.id
    await register_channel(uid, bot, chat)
    database.execute("DELETE FROM app_settings WHERE key=?", (f"channel_setup:{uid}",))
    await state.clear()
    await accounts.check_referral(uid, bot)
    await message.answer(
        "✅ " + html.escape(chat.title or tr("Объект")) + tr(" подключён."),
        reply_markup=ReplyKeyboardRemove(),
    )
    await ui.answer(
        message,
        tr("📺 <b>Мои каналы/группы</b>"),
        reply_markup=channel_list_keyboard(uid),
    )


async def register_channel(uid, bot, chat):
    if not chat or chat.type not in {"channel", "supergroup", "group"}:
        raise ValueError(tr("Нужен канал/группа; источник пересылки скрыт."))
    existing = database.one(
        "SELECT * FROM channels WHERE telegram_chat_id=? AND owner_telegram_id=?",
        (chat.id, uid),
    )
    if (
        accounts.channel_limit(uid) >= 0
        and (not existing or not existing["is_active"])
        and accounts.channel_count(uid) >= accounts.channel_limit(uid)
    ):
        raise ValueError(tr("Лимит каналов/групп достигнут."))
    if not await access.owner_and_bot_ok(bot, chat.id, uid):
        raise ValueError(
            tr(
                "Вы и бот должны быть администраторами; для канала нужно право публикации."
            )
        )
    # One controlling owner prevents contradictory join-request policies.
    other = database.one(
        "SELECT 1 FROM channels WHERE telegram_chat_id=? AND owner_telegram_id!=? AND is_active=1",
        (chat.id, uid),
    )
    if other:
        raise ValueError(
            tr(
                "Канал уже подключён другим администратором бота. Сначала отключите прежнее подключение."
            )
        )
    full = await bot.get_chat(chat.id)
    t = timeutils.iso()
    database.execute(
        "INSERT INTO channels(telegram_chat_id,title,username,chat_type,owner_telegram_id,bot_is_admin,is_active,created_at,updated_at,invite_link) VALUES(?,?,?,?,?,1,1,?,?,?) ON CONFLICT(telegram_chat_id,owner_telegram_id) DO UPDATE SET title=excluded.title,username=excluded.username,invite_link=excluded.invite_link,bot_is_admin=1,is_active=1,updated_at=excluded.updated_at",
        (
            chat.id,
            chat.title or tr("Канал"),
            chat.username,
            chat.type,
            uid,
            t,
            t,
            getattr(full, "invite_link", None),
        ),
    )


@router.my_chat_member()
async def channel_bot_added(event: ChatMemberUpdated, bot: Bot, dispatcher: Dispatcher):
    uid = event.from_user.id
    activate(uid)
    started = timeutils.parse_dt(database.setting(f"channel_setup:{uid}"))
    if not started or timeutils.now() - started > timedelta(minutes=15):
        return
    if (
        event.chat.type not in {"channel", "group", "supergroup"}
        or event.new_chat_member.status != ChatMemberStatus.ADMINISTRATOR
        or accounts.blocked(uid)
    ):
        return
    try:
        await register_channel(uid, bot, event.chat)
    except ValueError as exc:
        await bot.send_message(uid, html.escape(str(exc)))
        return
    database.execute("DELETE FROM app_settings WHERE key=?", (f"channel_setup:{uid}",))
    private_state = dispatcher.fsm.get_context(bot=bot, chat_id=uid, user_id=uid)
    if await private_state.get_state() == AddChannel.waiting.state:
        await private_state.clear()
    await accounts.check_referral(uid, bot)
    await bot.send_message(
        uid,
        "✅ " + html.escape(event.chat.title or tr("Канал")) + tr(" подключён."),
        reply_markup=ReplyKeyboardRemove(),
    )
    await ui.show_panel(
        bot,
        uid,
        tr("📺 <b>Мои каналы/группы</b>"),
        channel_list_keyboard(uid),
        force_bottom=True,
    )


@router.message(AddChannel.waiting, F.text, ~F.forward_origin)
async def channel_text(message: Message, state: FSMContext, bot: Bot):
    text = chat_reference(message.text)
    try:
        chat = await bot.get_chat(text)
    except Exception:
        await ui.answer(message, tr("❌ Не удалось найти объект."))
        return
    await connect_channel(message, state, bot, chat)


@router.message(AddChannel.waiting, F.forward_origin)
async def channel_forward(message: Message, state: FSMContext, bot: Bot):
    await connect_channel(message, state, bot, content.forward_chat(message))


@router.callback_query(F.data == "channel:list")
async def channel_list(c: CallbackQuery):
    if not await access.access_callback(c):
        return
    await ui.edit(
        c, tr("📺 <b>Мои каналы/группы</b>"), channel_list_keyboard(c.from_user.id)
    )
    await c.answer()


def channel_list_keyboard(uid):
    rows = database.all_rows(
        "SELECT * FROM channels WHERE owner_telegram_id=? AND is_active=1 ORDER BY title",
        (uid,),
    )
    buttons = [
        [
            InlineKeyboardButton(
                text=("📢" if r["chat_type"] == "channel" else "👥")
                + " "
                + r["title"][:32],
                callback_data=f"channel:open:{r['id']}",
            )
        ]
        for r in rows
    ]
    buttons += [
        [InlineKeyboardButton(text=tr("➕ Добавить"), callback_data="channel:add")],
        [InlineKeyboardButton(text=tr("⬅️ Главное меню"), callback_data="menu:main")],
    ]
    return ui.kb(buttons)


async def dismiss_picker(bot, uid):
    if not database.setting(f"channel_setup:{uid}"):
        return
    sent = await bot.send_message(
        uid, tr("⬅️ Назад"), reply_markup=ReplyKeyboardRemove()
    )
    database.execute("DELETE FROM app_settings WHERE key=?", (f"channel_setup:{uid}",))
    try:
        await bot.delete_message(uid, sent.message_id)
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("channel:check:"))
async def channel_check(c: CallbackQuery, bot: Bot):
    cid = int(c.data.split(":")[2])
    r = template_channel(cid, c.from_user.id)
    ok = await access.owner_and_bot_ok(bot, r["telegram_chat_id"], c.from_user.id)
    full = await bot.get_chat(r["telegram_chat_id"])
    database.execute(
        "UPDATE channels SET bot_is_admin=?,title=?,username=?,chat_type=?,updated_at=? WHERE id=?",
        (int(ok), full.title, full.username, full.type, timeutils.iso(), cid),
    )
    await c.answer(
        tr("Права есть.") if ok else tr("Недостаточно прав у вас или бота."),
        show_alert=True,
    )


@router.callback_query(F.data.startswith("channel:del:"))
async def channel_del(c: CallbackQuery):
    if not await access.access_callback(c):
        return
    cid = int(c.data.split(":")[2])
    r = database.one(
        "SELECT title FROM channels WHERE id=? AND owner_telegram_id=?",
        (cid, c.from_user.id),
    )
    if not r:
        await c.answer(tr("Не найдено"), show_alert=True)
        return
    await ui.edit(
        c,
        tr("🗑 Отключить <b>{v0}</b>?", v0=html.escape(r["title"])),
        ui.kb(
            [
                [
                    InlineKeyboardButton(
                        text=tr("✅ Да"), callback_data=f"channel:del_yes:{cid}"
                    ),
                    InlineKeyboardButton(
                        text=tr("❌ Нет"), callback_data=f"channel:open:{cid}"
                    ),
                ]
            ]
        ),
    )
    await c.answer()


@router.callback_query(F.data.startswith("channel:del_yes:"))
async def channel_del_yes(c: CallbackQuery):
    if not await access.access_callback(c):
        return
    cid = int(c.data.split(":")[2])
    database.execute(
        "UPDATE channels SET is_active=0,auto_requests=0,updated_at=? WHERE id=? AND owner_telegram_id=?",
        (timeutils.iso(), cid, c.from_user.id),
    )
    await ui.edit(c, tr("✅ Объект отключён."), ui.channels_kb())
    await c.answer()


@router.callback_query(F.data == "menu:requests")
async def menu_requests(c: CallbackQuery):
    if not await access.access_callback(c):
        return
    rows = [
        r for r in accounts.eligible_channels(c.from_user.id) if supports_requests(r)
    ]
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{'🟢' if r['auto_requests'] else '🔴'} {r['title'][:28]}",
                callback_data=f"channel:req:{r['id']}",
            )
        ]
        for r in rows
    ]
    buttons += [
        [InlineKeyboardButton(text=tr("🔐 Условия"), callback_data="menu:conditions")],
        [InlineKeyboardButton(text=tr("⬅️ Главное меню"), callback_data="menu:main")],
    ]
    await ui.edit(c, tr("📥 <b>АвтоЗаявки</b>\n\nВыберите объект."), ui.kb(buttons))
    await c.answer()


@router.callback_query(F.data.startswith("channel:req:"))
async def request_toggle(c: CallbackQuery):
    if not await access.access_callback(c):
        return
    cid = int(c.data.split(":")[2])
    r = database.one(
        "SELECT * FROM channels WHERE id=? AND owner_telegram_id=? AND is_active=1",
        (cid, c.from_user.id),
    )
    if not r:
        await c.answer(tr("Не найдено"), show_alert=True)
        return
    if not supports_requests(r):
        raise ValueError(tr("АвтоЗаявки доступны для частных каналов и групп."))
    new = 0 if r["auto_requests"] else 1
    database.execute(
        "UPDATE channels SET auto_requests=?,updated_at=? WHERE id=?",
        (new, timeutils.iso(), cid),
    )
    await ui.edit(
        c,
        tr(
            "📥 <b>{v0}</b>\n\nАвтоЗаявки: {v1}",
            v0=html.escape(r["title"]),
            v1=tr("🟢 включены") if new else tr("🔴 выключены"),
        ),
        ui.kb(
            [
                [
                    InlineKeyboardButton(
                        text=tr("🔐 Выбрать условие"),
                        callback_data=f"channel:cond:{cid}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=tr("⬅️ Назад"), callback_data="menu:requests"
                    )
                ],
            ]
        ),
    )
    await c.answer()


def template_channel(cid, uid):
    ch = database.one(
        "SELECT * FROM channels WHERE id=? AND owner_telegram_id=?", (cid, uid)
    )
    if not ch:
        raise ValueError(tr("Канал не найден."))
    return ch


@router.callback_query(F.data.startswith("channel:open:"))
async def channel_open(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    r = template_channel(cid, c.from_user.id)
    await ui.edit(
        c,
        html.escape(r["title"])
        + tr(
            "\nАвтоЗаявки: {v0}",
            v0=tr("включены") if r["auto_requests"] else tr("выключены"),
        ),
        ui.channel_card_buttons(cid),
    )
    await c.answer()
