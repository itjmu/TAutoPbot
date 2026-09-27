"""ui components."""

import asyncio
import html
import json
import unicodedata
from weakref import WeakValueDictionary

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import accounts as accounts
from app import content as content
from app import database as database
from app.features import posts as features_posts
from app.i18n import tr
from config import ADMIN_ID
from services.telegram_links import supports_requests

_panel_locks = WeakValueDictionary()


def panel_id(uid):
    if database.db is None:
        return None
    value = database.setting(f"ui:panel:{uid}")
    return int(value) if value and value.isdigit() else None


def remember_panel(uid, message_id):
    if database.db is not None and type(message_id) is int:
        database.execute(
            "INSERT INTO app_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"ui:panel:{uid}", str(message_id)),
        )


def note_message(uid, message_id):
    if database.db is not None and type(message_id) is int:
        database.execute(
            "INSERT INTO app_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER)) AS TEXT)",
            (f"ui:tail:{uid}", str(message_id)),
        )


async def note_message_async(uid, message_id):
    if database.db is not None and type(message_id) is int:
        await database.async_call(
            lambda conn: (
                conn.execute(
                    "INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER)) AS TEXT)",
                    (f"ui:tail:{uid}", str(message_id)),
                ).rowcount
            )
        )


async def clear_controls(bot, uid, message_id):
    if type(message_id) is int:
        try:
            await bot.edit_message_reply_markup(
                chat_id=uid, message_id=message_id, reply_markup=None
            )
        except TelegramAPIError:
            pass


def note_sent(uid, sent):
    for message in sent if isinstance(sent, list) else [sent]:
        note_message(uid, getattr(message, "message_id", None))


async def retire_controls(bot, uid, workflow):
    key = f"ui:controls:{uid}:{workflow}"
    saved = database.setting(key) if database.db is not None else ""
    if saved:
        for mid in json.loads(saved):
            await clear_controls(bot, uid, mid)
        database.execute("DELETE FROM app_settings WHERE key=?", (key,))


async def track_controls(bot, uid, workflow, sent):
    await retire_controls(bot, uid, workflow)
    messages = sent if isinstance(sent, list) else [sent]
    ids = [
        m.message_id for m in messages if type(getattr(m, "message_id", None)) is int
    ]
    if database.db is not None and ids:
        database.execute(
            "INSERT OR REPLACE INTO app_settings(key,value) VALUES(?,?)",
            (f"ui:controls:{uid}:{workflow}", json.dumps(ids)),
        )
    note_sent(uid, sent)


async def refresh_panel(bot, uid):
    saved = database.setting(f"ui:screen:{uid}") if database.db is not None else ""
    if saved:
        screen = json.loads(saved)
        try:
            await show_panel(
                bot,
                uid,
                screen["text"],
                InlineKeyboardMarkup.model_validate(screen["markup"]),
                force_bottom=True,
            )
        except TelegramAPIError:
            pass


async def show_panel(
    bot, uid, text, reply_markup=None, *, anchor=None, force_bottom=False, **kwargs
):
    """One durable private navigation message; never a media or progress message."""
    key = (id(database.db), uid)
    lock = _panel_locks.get(key)
    if lock is None:
        lock = _panel_locks[key] = asyncio.Lock()
    reply_markup = panel_markup(reply_markup)
    async with lock:
        message_id = panel_id(uid)
        previous_id = message_id
        if anchor is not None:
            note_message(uid, getattr(anchor, "message_id", None))
        tail = database.setting(f"ui:tail:{uid}") if database.db is not None else ""
        if force_bottom or (message_id and tail and int(tail) > message_id):
            message_id = None
        if database.db is not None:
            database.execute(
                "INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    f"ui:screen:{uid}",
                    json.dumps(
                        {
                            "text": text,
                            "markup": reply_markup.model_dump(exclude_none=True),
                        }
                    ),
                ),
            )
        if (
            message_id is None
            and previous_id is None
            and not force_bottom
            and anchor is not None
            and getattr(getattr(anchor, "chat", None), "type", None) == "private"
            and anchor.chat.id == uid
            and getattr(getattr(anchor, "from_user", None), "is_bot", False)
            and getattr(anchor, "text", None) is not None
        ):
            message_id = anchor.message_id
        if message_id is not None:
            try:
                await bot.edit_message_text(
                    text,
                    chat_id=uid,
                    message_id=message_id,
                    reply_markup=reply_markup,
                    **kwargs,
                )
            except TelegramBadRequest as exc:
                error = str(exc).lower()
                if "message is not modified" in error:
                    remember_panel(uid, message_id)
                    return message_id
                if not any(
                    marker in error
                    for marker in (
                        "message to edit not found",
                        "message can't be edited",
                        "message can not be edited",
                        "there is no text in the message",
                    )
                ):
                    raise
            else:
                remember_panel(uid, message_id)
                return message_id
        sent = await bot.send_message(uid, text, reply_markup=reply_markup, **kwargs)
        remember_panel(uid, sent.message_id)
        if previous_id and previous_id != sent.message_id:
            await clear_controls(bot, uid, previous_id)
        return sent.message_id


async def answer(message, text, reply_markup=None, **kwargs):
    """Show a navigation prompt after a message or a callback's bot message."""
    bot = getattr(message, "bot", None)
    if (
        bot is None
        or getattr(getattr(message, "chat", None), "type", None) != "private"
    ):
        return await message.answer(text, reply_markup=reply_markup, **kwargs)
    return await show_panel(
        bot, message.chat.id, text, reply_markup, anchor=message, **kwargs
    )


def panel_markup(markup):
    rows = list(markup.inline_keyboard) if markup else []
    callbacks = {button.callback_data for row in rows for button in row}
    if "menu:main" not in callbacks and not {
        "menu:posts",
        "menu:settings",
        "menu:download",
    }.issubset(callbacks):
        rows.append([choice(tr("⬅️ Главное меню"), "menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=readable_rows(rows))


async def dismiss_command(message):
    if getattr(getattr(message, "chat", None), "type", None) == "private":
        try:
            await message.delete()
        except TelegramAPIError:
            # The menu is usable even when Telegram cannot remove the command.
            pass


def back(cb="menu:main"):
    return kb([[InlineKeyboardButton(text=tr("⬅️ Назад"), callback_data=cb)]])


def channels_kb():
    return kb(
        [
            [InlineKeyboardButton(text=tr("➕ Добавить"), callback_data="channel:add")],
            [
                InlineKeyboardButton(
                    text=tr("📋 Мои каналы/группы"), callback_data="channel:list"
                )
            ],
            [
                InlineKeyboardButton(
                    text=tr("⬅️ Главное меню"), callback_data="menu:main"
                )
            ],
        ]
    )


def settings_kb(uid):
    rows = [
        [InlineKeyboardButton(text=label, callback_data=cb)]
        for label, cb in [
            ("💎 Premium", "settings:premium"),
            (tr("👥 Пригласить друзей"), "ref:open"),
            (tr("🎟 Активировать промокод"), "promo:redeem"),
            (tr("👤 Профиль"), "settings:profile"),
            (tr("❓ Помощь"), "settings:help"),
        ]
    ]
    if uid == ADMIN_ID:
        rows.append(
            [
                InlineKeyboardButton(
                    text=tr("👨\u200d💻 Админ-панель"), callback_data="admin:main"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text=tr("⬅️ Главное меню"), callback_data="menu:main")]
    )
    rows.insert(
        0,
        [
            choice(tr("🌐 Язык"), "settings:language"),
            choice(tr("🕒 Часовой пояс"), "settings:timezone"),
        ],
    )
    return kb(rows)


async def edit(c, text, markup=None):
    bot = getattr(c, "bot", None) or getattr(c.message, "bot", None)
    if bot is not None and getattr(c.message.chat, "type", None) == "private":
        mid = await show_panel(bot, c.message.chat.id, text, markup, anchor=c.message)
        buttons = getattr(c.message, "reply_markup", None)
        active_download = buttons and any(
            b.callback_data == "download:cancel"
            for row in buttons.inline_keyboard
            for b in row
        )
        if mid != c.message.message_id and not active_download:
            await clear_controls(bot, c.message.chat.id, c.message.message_id)
        return mid
    try:
        if c.message.text is not None:
            await c.message.edit_text(text, reply_markup=markup)
        else:
            await c.message.answer(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        if (
            "message to edit not found" in str(exc).lower()
            or "message can't be edited" in str(exc).lower()
        ):
            await c.bot.send_message(c.from_user.id, text, reply_markup=markup)
        else:
            raise


def button_grid(buttons, columns=2):
    columns = max(1, min(3, columns))
    return [buttons[i : i + columns] for i in range(0, len(buttons), columns)]


def choice(label, data):
    return InlineKeyboardButton(text=label, callback_data=data)


def button_icon(button):
    """Choose language-independent icons from stable action IDs."""
    if button.icon_custom_emoji_id or any(
        unicodedata.category(char) == "So" or char in "←→✓✔✕×" for char in button.text
    ):
        return button
    parts = (button.callback_data or "").split(":")
    actions = {
        "delete": "🗑",
        "remove": "🗑",
        "cancel": "✖️",
        "back": "⬅️",
        "save": "💾",
        "confirm": "✅",
        "yes": "✅",
        "no": "✖️",
        "add": "➕",
        "create": "➕",
        "new": "➕",
        "edit": "✏️",
        "title": "✏️",
        "text": "📝",
        "description": "📝",
        "preview": "👁",
        "publish": "🚀",
        "send": "📤",
        "schedule": "🕒",
        "time": "🕒",
        "date": "📅",
        "refresh": "🔄",
        "retry": "🔄",
        "check": "🔎",
        "settings": "⚙️",
        "main": "🏠",
        "menu": "🏠",
        "next": "➡️",
        "prev": "⬅️",
        "search": "🔎",
    }
    families = {
        "channel": "📢",
        "cond": "🔐",
        "contest": "🎉",
        "give": "🎉",
        "p": "📝",
        "post": "📝",
        "download": "📥",
        "premium": "⭐",
        "pay": "⭐",
        "promo": "🎟",
        "mail": "📬",
        "admin": "🛠",
        "lang": "🌐",
        "tz": "🌐",
        "source": "📡",
        "reaction": "👍",
    }
    icon = next((actions[part] for part in reversed(parts) if part in actions), None)
    icon = icon or ("🔗" if button.url else families.get(parts[0], "🔹"))
    return button.model_copy(update={"text": f"{icon} {button.text}"})


def button_style(button):
    """Four semantic categories; None lets Telegram use its neutral theme style."""
    if button.style is not None:
        return button
    data = button.callback_data or ""
    parts = set(data.split(":"))
    label = button.text.lstrip()
    if data.startswith("au:block:"):
        style = "danger" if data.endswith(":1") else "success"
    elif label.startswith(("❌", "✖", "🗑", "🚫", "⏹")) or parts & {
        "delete",
        "del",
        "erase",
        "remove",
        "cancel",
        "stop",
        "reject",
        "revoke",
        "clear",
        "block",
    }:
        style = "danger"
    elif label.startswith(("⬅", "←", "➡", "→")) or parts & {
        "back",
        "prev",
        "next",
        "noop",
        "demo",
    }:
        style = None
    elif (
        label.startswith(("✅", "💾", "🚀", "➕"))
        or parts
        & {
            "save",
            "confirm",
            "done",
            "yes",
            "approve",
            "accept",
            "publish",
            "send",
            "start",
            "create",
            "new",
            "add",
            "participate",
            "unblock",
            "pay",
            "activate",
        }
        or button.pay
    ):
        style = "success"
    elif (
        button.url
        or not data
        or parts & {"select", "toggle", "choice", "value", "position"}
    ):
        style = None
    else:
        style = "primary"
    return button.model_copy(update={"style": style})


def readable_rows(rows, decorate=True):
    result = []
    for row in rows:
        pending = []
        for original in row:
            button = button_style(button_icon(original)) if decorate else original
            width = 1 if len(button.text) > 32 else 2 if len(button.text) > 16 else 3
            if pending and len(pending) >= min(width, *(item[1] for item in pending)):
                result.append([item[0] for item in pending])
                pending = []
            pending.append((button, width))
        if pending:
            result.append([item[0] for item in pending])
    return result


def kb(rows, *, published=False):
    rows = list(rows)
    compact = not published and sum(len(row) for row in rows) > 3
    # Compact long object lists without changing the user's published button rows.
    out = []
    pending = []
    prefixes = (
        "channel:open:",
        "channel:req:",
        "cond:open:",
        "channel:cond_set:",
        "p:",
    )

    def flush():
        out.extend(button_grid(pending, 2))
        pending.clear()

    for row in rows:
        data = (row[0].callback_data or "") if len(row) == 1 else ""
        if (
            (
                data.startswith(prefixes)
                or (compact and len(row) == 1 and len(row[0].text) <= 30)
            )
            and not row[0].text.startswith(("⬅", "✍", "🚀", "❌", "🔄", "🔎"))
            and data != "menu:main"
            and not data.endswith(":panel")
        ):
            pending.extend(row)
        else:
            flush()
            out.append(row)
    flush()
    return InlineKeyboardMarkup(
        inline_keyboard=readable_rows(out, decorate=not published)
    )


def main_kb():
    return kb(
        [
            [
                choice(tr("📢 Автопостинг"), "menu:posts"),
                choice(tr("📥 АвтоЗаявки"), "menu:requests"),
            ],
            [
                choice(tr("📚 Мультипостинг"), "menu:multi"),
                choice(tr("🔐 Условия"), "menu:conditions"),
            ],
            [
                choice(tr("📺 Каналы / группы"), "menu:channels"),
                choice(tr("📥 Скачать по ссылке"), "menu:download"),
            ],
            [
                choice(tr("🎉 Конкурсы"), "menu:contests"),
                choice(tr("⚙️ Настройки"), "menu:settings"),
            ],
        ]
    )


def post_controls(pid, status="draft", *, has_video=False):
    if status == "uncertain":
        return kb(
            [
                [choice(tr("🔎 Проверить доставку"), f"p:{pid}:review")],
                [choice(tr("⬅️ Автопостинг"), "menu:posts")],
            ]
        )
    if status in {"published", "skipped", "publishing"}:
        return back("menu:posts")
    if status in {"failed", "partial"}:
        return kb(
            [
                [choice(tr("🔄 Повторить неотправленные"), f"p:{pid}:retry")],
                [choice(tr("⬅️ Автопостинг"), "menu:posts")],
            ]
        )
    editing = [
        choice(tr("📝 Описание"), f"p:{pid}:text"),
        choice(tr("🖼 Заменить сообщение"), f"p:{pid}:replace"),
        choice(tr("🔘 Кнопки"), f"p:{pid}:buttons"),
    ]
    if has_video:
        editing.append(choice(tr("🖼 Обложка видео"), f"p:{pid}:cover"))
    return kb(
        [
            editing,
            [
                choice(tr("📺 Каналы"), f"p:{pid}:targets"),
                choice(tr("🧩 Шаблон"), f"p:{pid}:templates"),
                choice(tr("🕐 Время"), f"p:{pid}:time"),
                choice(tr("🗑 Автоудаление"), f"p:{pid}:delete"),
            ],
            [
                choice(tr("🚀 Опубликовать"), f"p:{pid}:publish"),
                choice(tr("❌ Пропустить"), f"p:{pid}:skip"),
                choice(tr("⬅️ Автопостинг"), "menu:posts"),
            ],
        ]
    )


def target_markup(uid, pid):
    selected = {r["id"] for r in features_posts.post_target_rows(pid)}
    rows = button_grid(
        [
            choice(
                ("✅ " if r["id"] in selected else "▫️ ") + r["title"][:25],
                f"p:{pid}:toggle:{r['id']}",
            )
            for r in accounts.eligible_channels(uid)
        ],
        2,
    )
    if rows:
        rows.append(
            [
                choice(tr("✅ Готово"), f"p:{pid}:preview"),
                choice(tr("Пропустить"), f"p:{pid}:skip"),
            ]
        )
    else:
        rows.append(
            [
                choice(tr("➕ Добавить канал"), "channel:add"),
                choice(tr("Пропустить"), f"p:{pid}:skip"),
            ]
        )
    return kb(rows)


async def send_target_picker(bot, uid, pid, *, panel=False):
    text = tr(
        "📺 Куда опубликовать?\nВыберите один или несколько каналов, затем нажмите «Готово»."
    )
    markup = target_markup(uid, pid)
    if panel:
        await show_panel(bot, uid, text, markup)
    else:
        sent = await bot.send_message(uid, text, reply_markup=markup)
        await track_controls(bot, uid, f"post:{pid}", sent)


async def target_picker(c, pid):
    content.mutable_post(pid, c.from_user.id)
    await edit(
        c, tr("📺 Выберите каналы для публикации:"), target_markup(c.from_user.id, pid)
    )


def source_card(sid, uid):
    source = database.one(
        "SELECT * FROM post_sources WHERE id=? AND owner_telegram_id=? AND kind='telegram'",
        (sid, uid),
    )
    if not source:
        raise ValueError(tr("Источник Telegram не найден."))
    selected = {
        r["channel_id"]
        for r in database.all_rows(
            "SELECT channel_id FROM source_targets WHERE source_id=?", (sid,)
        )
    }
    rows = button_grid(
        [
            choice(
                ("✅ " if r["id"] in selected else "▫️ ") + r["title"][:24],
                f"source:set:{sid}:{r['id']}",
            )
            for r in accounts.eligible_channels(uid)
        ],
        2,
    )
    parent = f"channel:sources:{min(selected)}" if selected else "channel:list"
    rows.append(
        [choice(tr("🗑 Отключить"), f"source:del:{sid}"), choice(tr("⬅️ Назад"), parent)]
    )
    return tr("Источник: ") + html.escape(
        source["source_title"] or str(source["source_chat_id"])
    ) + tr(
        "\nНовые посты приходят вам на проверку.\nКаналы для будущих публикаций:"
    ), kb(rows)


def channel_card_buttons(cid):
    ch = database.one("SELECT * FROM channels WHERE id=?", (cid,))
    actions = [
        (tr("📢 Создать пост"), f"channel:post:{cid}"),
        (tr("🔄 Источники"), f"channel:sources:{cid}"),
        (tr("🧩 Шаблоны"), f"templates:{cid}"),
    ]
    if ch and supports_requests(ch):
        actions += [
            (tr("📥 АвтоЗаявки"), f"channel:req:{cid}"),
            (tr("🔐 Условия"), f"channel:cond:{cid}"),
        ]
    actions += [
        (tr("🔄 Проверить права"), f"channel:check:{cid}"),
        (tr("🗑 Отключить"), f"channel:del:{cid}"),
        (tr("⬅️ Каналы"), "channel:list"),
    ]
    return kb(button_grid([choice(label, cb) for label, cb in actions], 2))


def admin_kb():
    rows = button_grid(
        [
            choice(label, cb)
            for label, cb in [
                (tr("📊 Статистика"), "admin:stats"),
                (tr("👥 Пользователи"), "admin:users"),
                ("📦 Экспорт данных", "au:exports"),
                (tr("📺 Каналы"), "admin:channels"),
                ("💎 Premium", "admin:premium"),
                (tr("📢 Рассылка"), "admin:broadcast"),
                (tr("📬 Состояние рассылок"), "mail:list"),
                (tr("🚫 Блокировки"), "admin:blocks"),
                (tr("📝 Логи"), "admin:logs"),
                (tr("⬅️ Настройки"), "menu:settings"),
            ]
        ],
        2,
    )
    rows.insert(0, [choice(tr("💳 Оплата Premium"), "payment:settings")])
    return kb(rows)
