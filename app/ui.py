"""ui components."""

import html

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import accounts as accounts
from app import content as content
from app import database as database
from app.features import posts as features_posts
from app.i18n import tr
from config import ADMIN_ID
from services.telegram_links import supports_requests


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
    return [buttons[i : i + columns] for i in range(0, len(buttons), columns)]


def choice(label, data):
    return InlineKeyboardButton(text=label, callback_data=data)


def kb(rows):
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
        if data.startswith(prefixes) and not row[0].text.startswith(
            ("⬅", "✍", "🚀", "❌", "🔄", "🔎")
        ):
            pending.extend(row)
        else:
            flush()
            out.append(row)
    flush()
    return InlineKeyboardMarkup(inline_keyboard=out)


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


def post_controls(pid, status="draft"):
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
    return kb(
        [
            [
                choice(tr("✏️ Редактировать"), f"p:{pid}:edit"),
                choice(tr("📺 Каналы"), f"p:{pid}:targets"),
            ],
            [
                choice(tr("🧩 Шаблон"), f"p:{pid}:templates"),
                choice(tr("🔘 Кнопки"), f"p:{pid}:buttons"),
            ],
            [
                choice(tr("🕐 Время"), f"p:{pid}:time"),
                choice(tr("🗑 Автоудаление"), f"p:{pid}:delete"),
            ],
            [
                choice(tr("🚀 Опубликовать"), f"p:{pid}:publish"),
                choice(tr("❌ Пропустить"), f"p:{pid}:skip"),
            ],
            [choice(tr("⬅️ Автопостинг"), "menu:posts")],
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


async def send_target_picker(bot, uid, pid):
    await bot.send_message(
        uid,
        tr(
            "📺 Куда опубликовать?\nВыберите один или несколько каналов, затем нажмите «Готово»."
        ),
        reply_markup=target_markup(uid, pid),
    )


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
                (tr("📺 Каналы"), "admin:channels"),
                ("💎 Premium", "admin:premium"),
                (tr("📢 Рассылка"), "admin:broadcast"),
                (tr("🚫 Блокировки"), "admin:blocks"),
                (tr("📝 Логи"), "admin:logs"),
                (tr("⬅️ Настройки"), "menu:settings"),
            ]
        ],
        2,
    )
    rows.insert(0, [choice(tr("💳 Оплата Premium"), "payment:settings")])
    return kb(rows)
