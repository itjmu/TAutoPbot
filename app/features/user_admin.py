"""Private administrator exports and individual account controls."""

import asyncio
import csv
import html
import io
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, FSInputFile

from app import accounts, timeutils, ui
from app import database as db
from config import ADMIN_ID
from services.jobs import download_jobs

router = Router(name="features.user_admin")
FIELDS = {
    "channels": "📢 Активные каналы",
    "sources": "📡 Источники",
    "post_create": "📝 Новые посты в сутки",
    "cover": "🖼 Обложки в сутки",
    "download_video": "📥 Видео в сутки",
    "button_colors": "🎨 Цвета кнопок",
}


class UserAdmin(StatesGroup):
    search = State()
    limit = State()


def authorized(event):
    message = getattr(event, "message", event)
    if (
        event.from_user.id != ADMIN_ID
        or not message
        or message.chat.type != "private"
        or message.chat.id != ADMIN_ID
    ):
        raise ValueError("Нет доступа.")


def target(uid):
    if uid == ADMIN_ID:
        raise ValueError("Нельзя ограничить или удалить администратора.")
    if not db.one(
        "SELECT 1 FROM users WHERE telegram_id=?", (uid,)
    ) and not accounts.blocked(uid):
        raise ValueError("Пользователь не найден.")


def set_block(uid, enabled):
    target(uid)
    with db.atomic():
        if enabled:
            db.execute(
                "INSERT OR REPLACE INTO blocked_users(telegram_id,reason,created_at) VALUES(?,?,?)",
                (uid, "Администратор", timeutils.iso()),
            )
        else:
            db.execute("DELETE FROM blocked_users WHERE telegram_id=?", (uid,))
        db.execute(
            "UPDATE users SET is_blocked=? WHERE telegram_id=?", (int(enabled), uid)
        )
        db.log_event(ADMIN_ID, "ADMIN_BLOCK" if enabled else "ADMIN_UNBLOCK", str(uid))
    if enabled:
        download_jobs.cancel(uid)


def set_limit(uid, field, value):
    target(uid)
    if field not in FIELDS:
        raise ValueError("Неизвестный лимит.")
    maximum = 3 if field == "button_colors" else 100000
    if value is not None and not (
        0 <= value <= maximum or (value == -1 and field != "button_colors")
    ):
        raise ValueError(
            f"Допустимо: 0–{maximum}"
            + (" или −1." if field != "button_colors" else ".")
        )
    limits = accounts.personal_limits(uid)
    if value is None:
        limits.pop(field, None)
    else:
        limits[field] = value
    with db.atomic():
        db.execute(
            "INSERT OR REPLACE INTO app_settings(key,value) VALUES(?,?)",
            (f"user_limits:{uid}", json.dumps(limits)),
        )
        db.log_event(ADMIN_ID, "ADMIN_LIMIT", f"{uid}:{field}={value}")


def delete_profile(uid):
    """Disable future use, remove profile/FSM; keep linked accounting records."""
    target(uid)
    with db.atomic():
        set_block(uid, True)
        for table, column in (
            ("users", "telegram_id"),
            ("user_preferences", "user_id"),
        ):
            db.execute(f"DELETE FROM {table} WHERE {column}=?", (uid,))
        db.execute(
            "DELETE FROM fsm_state WHERE storage_key LIKE ?", (f"%:{uid}:{uid}:%",)
        )
        for key in (
            f"user_limits:{uid}",
            f"ui:panel:{uid}",
            f"ui:tail:{uid}",
            f"ui:screen:{uid}",
            f"broadcast_draft:{uid}",
        ):
            db.execute("DELETE FROM app_settings WHERE key=?", (key,))
        db.log_event(ADMIN_ID, "ADMIN_DELETE_PROFILE", str(uid))


def csv_cell(value):
    value = "" if value is None else str(value)
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(
        ("\t", "\r", "\n")
    ):
        value = "'" + value
    return value


def users_csv(conn):
    query = """SELECT u.*, p.active AS premium_active, p.expires_at AS premium_expires_at,
    p.lifetime AS premium_lifetime, v.language, v.timezone,
    EXISTS(SELECT 1 FROM blocked_users b WHERE b.telegram_id=u.telegram_id) AS blocked,
    (SELECT reason FROM blocked_users b WHERE b.telegram_id=u.telegram_id) AS block_reason,
    (SELECT value FROM app_settings WHERE key='user_limits:' || u.telegram_id) AS personal_limits_json,
    (SELECT COUNT(*) FROM channels c WHERE c.owner_telegram_id=u.telegram_id) AS channels_count,
    (SELECT COUNT(*) FROM posts x WHERE x.owner_telegram_id=u.telegram_id) AS posts_count
    FROM users u LEFT JOIN premium p ON p.user_id=u.telegram_id
    LEFT JOIN user_preferences v ON v.user_id=u.telegram_id ORDER BY u.telegram_id"""
    cursor = conn.execute(query)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([item[0] for item in cursor.description])
    for row in cursor:
        writer.writerow([csv_cell(value) for value in row])
    return output.getvalue().encode("utf-8-sig")


def snapshot(source, destination):
    with closing(
        sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)
    ) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Не удалось проверить экспорт базы.")


async def user_list(c, page):
    authorized(c)
    page = max(0, page)
    rows = db.all_rows(
        "SELECT telegram_id,username,first_name FROM users ORDER BY id DESC LIMIT 13 OFFSET ?",
        (page * 12,),
    )
    buttons = [
        [
            ui.choice(
                f"👤 {r['username'] or r['first_name'] or r['telegram_id']} · {r['telegram_id']}",
                f"au:user:{r['telegram_id']}",
            )
        ]
        for r in rows[:12]
    ]
    nav = []
    if page:
        nav.append(ui.choice("⬅️ Предыдущие", f"au:list:{page - 1}"))
    if len(rows) > 12:
        nav.append(ui.choice("➡️ Следующие", f"au:list:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons += [
        [ui.choice("🔎 Найти по ID / @username", "au:search")],
        [ui.choice("📦 Экспорт", "au:exports")],
        [ui.choice("⬅️ Админ-панель", "admin:main")],
    ]
    await ui.edit(c, f"👥 Пользователи · страница {page + 1}", ui.kb(buttons))
    await c.answer()


async def card(c, uid):
    user = db.one("SELECT * FROM users WHERE telegram_id=?", (uid,))
    if not user and not accounts.blocked(uid):
        raise ValueError("Пользователь не найден.")
    name = (
        html.escape((user["first_name"] or "") + " " + (user["username"] or ""))
        if user
        else "Профиль удалён"
    )
    limits = accounts.personal_limits(uid)
    text = f"👤 {name}\nID: <code>{uid}</code>\nСтатус: {'заблокирован' if accounts.blocked(uid) else 'доступ разрешён'}\n\n"
    text += "\n".join(
        f"{label}: {accounts.user_limit(uid, field)}{' (личный)' if field in limits else ' (тариф)'}"
        for field, label in FIELDS.items()
    )
    buttons = [
        [
            ui.choice(
                "✅ Разблокировать" if accounts.blocked(uid) else "🚫 Заблокировать",
                f"au:block:{uid}:{0 if accounts.blocked(uid) else 1}",
            )
        ]
    ]
    buttons += [
        [ui.choice(label, f"au:limit:{uid}:{field}")] for field, label in FIELDS.items()
    ]
    buttons += [
        [ui.choice("🗑 Удалить профиль и отключить", f"au:delete:{uid}")],
        [ui.choice("⬅️ Пользователи", "admin:users")],
    ]
    await ui.edit(c, text, ui.kb(buttons))


@router.callback_query(F.data.startswith("au:"))
async def controls(c, state, bot):
    authorized(c)
    parts = c.data.split(":")
    action = parts[1]
    if action == "list":
        return await user_list(c, int(parts[2]))
    if action == "search":
        await state.set_state(UserAdmin.search)
        await ui.edit(
            c, "Введите Telegram ID или @username пользователя.", ui.back("admin:users")
        )
    elif action == "exports":
        await ui.edit(
            c,
            "📦 Экспорт\nCSV: профили всех пользователей, Premium, блокировки, настройки и лимиты.\nSQLite: вся база, включая посты и платежи. Файл придёт только в личный чат администратора.",
            ui.kb(
                [
                    [ui.choice("📊 Пользователи CSV", "au:export:csv")],
                    [ui.choice("🗄 Полная база SQLite", "au:export:db")],
                    [ui.choice("⬅️ Админ-панель", "admin:main")],
                ]
            ),
        )
    elif action == "export":
        await c.answer("Готовлю файл…")
        if parts[2] == "csv":
            payload = await db.async_call(users_csv)
            if len(payload) > 49_000_000:
                raise ValueError(
                    "CSV превышает лимит отправки. Используйте экспорт на сервере."
                )
            await bot.send_document(
                ADMIN_ID, BufferedInputFile(payload, filename="users.csv")
            )
        elif parts[2] == "db":
            source = db.one("PRAGMA database_list")[2]
            with tempfile.TemporaryDirectory(prefix="admin-export-") as folder:
                path = Path(folder) / "bot.sqlite3"
                await asyncio.to_thread(snapshot, source, path)
                if path.stat().st_size > 49_000_000:
                    raise ValueError(
                        "База превышает лимит отправки. Используйте экспорт на сервере."
                    )
                await bot.send_document(ADMIN_ID, FSInputFile(path))
        else:
            raise ValueError("Неизвестный формат.")
        db.log_event(ADMIN_ID, "ADMIN_EXPORT", parts[2])
        return
    else:
        uid = int(parts[2])
        if action == "user":
            await card(c, uid)
        elif action == "block":
            set_block(uid, parts[3] == "1")
            await card(c, uid)
        elif action == "limit":
            target(uid)
            if parts[3] not in FIELDS:
                raise ValueError("Неизвестный лимит.")
            await state.set_state(UserAdmin.limit)
            await state.set_data({"target": uid, "field": parts[3]})
            await ui.edit(
                c,
                f"{FIELDS[parts[3]]}\nВведите число: 0 — запрет; −1 — без лимита (кроме цветов: 0–3).\nВведите «тариф», чтобы убрать личный лимит.\nСуточные лимиты учитывают уже использованное сегодня; лимиты каналов/источников определяют число доступных объектов.",
                ui.back(f"au:user:{uid}"),
            )
        elif action == "delete":
            target(uid)
            await ui.edit(
                c,
                f"Удалить профиль {uid} и заблокировать дальнейший доступ?\nБудут удалены имя, username, настройки языка и состояния диалогов. Платежи, Premium и связанные публикации останутся в учёте. Это не удаление сообщений из Telegram.",
                ui.kb(
                    [
                        [ui.choice("🗑 Подтвердить удаление", f"au:erase:{uid}")],
                        [ui.choice("⬅️ Отмена", f"au:user:{uid}")],
                    ]
                ),
            )
        elif action == "erase":
            delete_profile(uid)
            await card(c, uid)
        else:
            raise ValueError("Неизвестное действие.")
    await c.answer()


@router.message(UserAdmin.search, F.text, ~F.text.startswith("/"))
async def search(m, state):
    authorized(m)
    query = m.text.strip().lstrip("@")
    row = db.one(
        "SELECT telegram_id FROM users WHERE telegram_id=? OR lower(username)=lower(?) LIMIT 1",
        (int(query) if query.isdecimal() else 0, query),
    )
    uid = (
        row[0]
        if row
        else int(query)
        if query.isdecimal() and accounts.blocked(int(query))
        else None
    )
    if uid is None:
        raise ValueError("Пользователь не найден. Попробуйте ID.")
    await state.clear()
    await ui.answer(
        m,
        f"👤 Найден пользователь {uid}",
        reply_markup=ui.kb([[ui.choice("👤 Открыть карточку", f"au:user:{uid}")]]),
    )


@router.message(UserAdmin.limit, F.text, ~F.text.startswith("/"))
async def save_limit(m, state):
    authorized(m)
    data = await state.get_data()
    value = (
        None
        if m.text.strip().lower() == "тариф"
        else int(m.text.strip().replace("−", "-"))
    )
    set_limit(data["target"], data["field"], value)
    await state.clear()
    await ui.answer(
        m, "✅ Лимит сохранён.", reply_markup=ui.back(f"au:user:{data['target']}")
    )
