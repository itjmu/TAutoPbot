"""Private support relay using native Telegram messages and persistent reply tokens."""

import html
import json
import secrets
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.state import State, StatesGroup

from app import database as db
from app import ui
from app.i18n import activate, tr
from config import ADMIN_ID

router = Router(name="support")


class Support(StatesGroup):
    writing = State()
    replying = State()


def save(key, value):
    db.execute(
        "INSERT INTO app_settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def navigation():
    return ui.kb(
        [
            [
                ui.choice(tr("🔙 Назад"), "settings:help"),
                ui.choice(tr("📱Главное Меню"), "menu:main"),
            ]
        ]
    )


@router.callback_query(F.data == "support:write")
async def open_callback(c, state):
    await state.clear()
    await state.set_state(Support.writing)
    await ui.edit(
        c,
        tr(
            "🆘 Написать Админу 💻\n\nОтправьте сообщения для администратора. Для выхода нажмите «Назад» или «Главное Меню»."
        ),
        navigation(),
    )
    await c.answer()


@router.message(Command("paysupport"))
async def open_command(m, state):
    await state.clear()
    await state.set_state(Support.writing)
    await ui.answer(
        m,
        tr(
            "🆘 Написать Админу 💻\n\nОтправьте сообщения для администратора. Для выхода нажмите «Назад» или «Главное Меню»."
        ),
        reply_markup=navigation(),
    )


@router.callback_query(F.data.startswith("support:reply:"))
async def reply_callback(c, state):
    if c.from_user.id != ADMIN_ID:
        await c.answer(tr("Доступ запрещён."), show_alert=True)
        return
    token = c.data.split(":")[-1]
    raw = db.setting("support:request:" + token)
    record = json.loads(raw) if raw else None
    if not record or record.get("control_id") != c.message.message_id:
        await c.answer(tr("Сообщение недоступно."), show_alert=True)
        return
    await state.clear()
    await state.set_state(Support.replying)
    await state.update_data(support_user=record["user_id"])
    await ui.answer(
        c.message,
        tr(
            "Отправьте один ответ или альбом пользователю {user_id}.",
            user_id=record["user_id"],
        ),
        reply_markup=navigation(),
    )
    await c.answer()


def supported(m):
    return not m.has_protected_content and any(
        getattr(m, name, None) is not None
        for name in (
            "text",
            "photo",
            "video",
            "audio",
            "voice",
            "video_note",
            "document",
            "animation",
            "sticker",
            "contact",
            "location",
            "venue",
            "poll",
            "dice",
        )
    )


def album_key(m):
    return f"support:album:{m.chat.id}:{m.media_group_id}"


class PendingAlbum(BaseFilter):
    async def __call__(self, m):
        return bool(m.media_group_id and db.setting(album_key(m)))


def append_album(m, record):
    record["ids"] = sorted(set(record["ids"] + [m.message_id]))
    record["ready"] = time.time() + 1.5
    save(album_key(m), record)


@router.message(PendingAlbum(), ~F.successful_payment)
async def album_part(m):
    record = json.loads(db.setting(album_key(m)))
    if supported(m):
        append_album(m, record)


async def failure(bot, sender):
    activate(sender)
    await bot.send_message(
        sender,
        tr(
            "Не удалось передать сообщение. Telegram запрещает передачу или получатель недоступен. Можно отправить другое сообщение."
        ),
    )


async def deliver(bot, record):
    sender, recipient = record["sender"], record["recipient"]
    try:
        if record["reply"]:
            if len(record["ids"]) == 1:
                sent = await bot.copy_message(recipient, sender, record["ids"][0])
            else:
                result = await bot.copy_messages(recipient, sender, record["ids"])
                ui.note_sent(recipient, result)
                if len(result) != len(record["ids"]):
                    await failure(bot, sender)
                    return False
                sent = None
            if sent is not None:
                ui.note_sent(recipient, sent)
        else:
            token = secrets.token_hex(12)
            request = {"user_id": sender}
            save("support:request:" + token, request)
            if len(record["ids"]) == 1:
                sent = await bot.forward_message(recipient, sender, record["ids"][0])
                ui.note_sent(recipient, sent)
            else:
                result = await bot.forward_messages(recipient, sender, record["ids"])
                ui.note_sent(recipient, result)
                if len(result) != len(record["ids"]):
                    await failure(bot, sender)
                    return False
            activate(recipient)
            card = await bot.send_message(
                recipient,
                tr(
                    "🆘 Обращение от {name}\nID: {user_id}",
                    name=html.escape(record["name"]),
                    user_id=sender,
                ),
                reply_markup=ui.kb(
                    [[ui.choice(tr("💬 Ответить"), "support:reply:" + token)]]
                ),
            )
            request["control_id"] = card.message_id
            save("support:request:" + token, request)
            ui.note_sent(recipient, card)
    except TelegramAPIError:
        await failure(bot, sender)
        return False
    activate(sender)
    try:
        sent = await bot.send_message(
            sender,
            tr("✅ Ответ отправлен.")
            if record["reply"]
            else tr("✅ Сообщение отправлено администратору."),
        )
        ui.note_sent(sender, sent)
    except TelegramAPIError:
        pass  # A missing receipt must not turn a delivered reply into a retry.
    return True


@router.message(Support.writing, ~F.successful_payment)
@router.message(Support.replying, ~F.successful_payment)
async def receive(m, state, bot):
    replying = await state.get_state() == Support.replying.state
    if replying and m.from_user.id != ADMIN_ID:
        await state.clear()
        return
    if not supported(m):
        await m.answer(
            tr(
                "Этот тип сообщения нельзя передать через поддержку. Отправьте другое сообщение."
            )
        )
        return
    data = await state.get_data()
    record = dict(
        sender=m.chat.id,
        recipient=data["support_user"] if replying else ADMIN_ID,
        reply=replying,
        ids=[m.message_id],
        name=m.from_user.full_name,
    )
    if m.media_group_id:
        append_album(m, record)
        if replying:
            await state.clear()
    elif await deliver(bot, record) and replying:
        await state.clear()


async def flush_albums(bot):
    for row in db.all_rows(
        "SELECT key,value FROM app_settings WHERE key LIKE 'support:album:%' LIMIT 100"
    ):
        record = json.loads(row["value"])
        if record["ready"] > time.time():
            continue
        # Never blindly retry an ambiguous send after a network failure or restart.
        db.execute("DELETE FROM app_settings WHERE key=?", (row["key"],))
        try:
            await deliver(bot, record)
        except TelegramAPIError:
            pass  # Even the sender may have blocked the bot.
