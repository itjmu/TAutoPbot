"""features / conditions components."""

import html
import json

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatJoinRequest, InlineKeyboardButton, Message

from app import access as access
from app import accounts as accounts
from app import database as database
from app import timeutils as timeutils
from app import ui as ui
from app.features import channels as features_channels
from app.features import editors as features_editors
from app.i18n import activate, tr
from services.telegram_links import chat_reference, subscription_link, supports_requests

router = Router(name="features.conditions")


@router.callback_query(F.data == "menu:conditions")
async def menu_conditions(c: CallbackQuery):
    if not await access.access_callback(c):
        return
    rows = database.all_rows(
        "SELECT * FROM conditions WHERE owner_telegram_id=? ORDER BY id DESC",
        (c.from_user.id,),
    )
    buttons = [
        [
            InlineKeyboardButton(
                text=f"🔐 {r['name'][:30]}", callback_data=f"cond:open:{r['id']}"
            )
        ]
        for r in rows
    ]
    buttons += [
        [InlineKeyboardButton(text=tr("➕ Создать"), callback_data="cond:create")],
        [InlineKeyboardButton(text=tr("⬅️ Главное меню"), callback_data="menu:main")],
    ]
    await ui.edit(
        c,
        tr(
            "🔐 <b>Условия</b>\n\nМожно создавать условия на подписку и текстовые ответы."
        ),
        ui.kb(buttons),
    )
    await c.answer()


@router.callback_query(F.data.startswith("cond:del:"))
async def cond_del(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    if not database.one(
        "SELECT 1 FROM conditions WHERE id=? AND owner_telegram_id=?",
        (cid, c.from_user.id),
    ):
        raise ValueError(tr("Условие не найдено."))
    with database.atomic():
        database.db.execute("DELETE FROM condition_items WHERE condition_id=?", (cid,))
        database.db.execute("DELETE FROM conditions WHERE id=?", (cid,))
        database.db.execute(
            "UPDATE channels SET condition_id=NULL WHERE condition_id=? AND owner_telegram_id=?",
            (cid, c.from_user.id),
        )
    await ui.edit(c, tr("Условие удалено."), ui.back("menu:conditions"))
    await c.answer()


@router.callback_query(F.data.startswith("channel:cond:"))
async def channel_condition(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    ch = features_channels.template_channel(cid, c.from_user.id)
    if not supports_requests(ch):
        raise ValueError(tr("Условия вступления доступны для частных каналов и групп."))
    conds = database.all_rows(
        "SELECT id,name FROM conditions WHERE owner_telegram_id=? AND active=1",
        (c.from_user.id,),
    )
    rows = [
        [
            InlineKeyboardButton(
                text=tr("Без условия"), callback_data=f"channel:cond_set:{cid}:0"
            )
        ]
    ] + [
        [
            InlineKeyboardButton(
                text=r["name"][:40], callback_data=f"channel:cond_set:{cid}:{r['id']}"
            )
        ]
        for r in conds
    ]
    rows.append(
        [InlineKeyboardButton(text=tr("⬅️ Канал"), callback_data=f"channel:open:{cid}")]
    )
    await ui.edit(c, tr("Условие для ") + html.escape(ch["title"]), ui.kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("channel:cond_set:"))
async def channel_condition_set(c: CallbackQuery):
    _, _, cid, cond = c.data.split(":")
    cid = int(cid)
    cond = int(cond)
    ch = features_channels.template_channel(cid, c.from_user.id)
    if not supports_requests(ch):
        raise ValueError(tr("Условия вступления доступны для частных каналов и групп."))
    if cond and not database.one(
        "SELECT 1 FROM conditions WHERE id=? AND owner_telegram_id=?",
        (cond, c.from_user.id),
    ):
        raise ValueError(tr("Условие не найдено."))
    database.execute(
        "UPDATE channels SET condition_id=? WHERE id=?", (cond or None, cid)
    )
    await ui.edit(c, tr("Условие сохранено."), ui.back(f"channel:open:{cid}"))
    await c.answer()


async def check_request(rid, bot, notify=False):
    r = database.one(
        "SELECT j.*,c.telegram_chat_id,c.condition_id,c.title,c.auto_requests,c.owner_telegram_id,c.is_active FROM join_requests j JOIN channels c ON c.id=j.channel_id WHERE j.id=? AND j.status='pending'",
        (rid,),
    )
    if (
        not r
        or not r["is_active"]
        or not r["auto_requests"]
        or accounts.blocked(r["telegram_user_id"])
        or accounts.blocked(r["owner_telegram_id"])
        or not accounts.channel_allowed(r["owner_telegram_id"], r["channel_id"])
    ):
        return
    if not supports_requests(
        database.one("SELECT * FROM channels WHERE id=?", (r["channel_id"],))
    ):
        return
    activate(r["telegram_user_id"])
    items = (
        database.all_rows(
            "SELECT * FROM condition_items WHERE condition_id=? ORDER BY position",
            (r["condition_id"],),
        )
        if r["condition_id"]
        else []
    )
    missing = []
    question = None
    links = []
    for item in items:
        data = json.loads(item["data"] or "{}")
        if item["item_type"] == "subscription":
            if not await access.member_ok(
                bot, int(data["chat_id"]), r["telegram_user_id"]
            ):
                missing.append(
                    tr("Подпишитесь: ") + data.get("title", str(data["chat_id"]))
                )
                try:
                    url = await subscription_link(bot, data)
                    links.append(
                        [
                            InlineKeyboardButton(
                                text=("📢 " + data.get("title", tr("Подписаться")))[
                                    :64
                                ],
                                url=url,
                            )
                        ]
                    )
                    if data.get("url") != url:
                        data["url"] = url
                        database.execute(
                            "UPDATE condition_items SET data=? WHERE id=?",
                            (json.dumps(data, ensure_ascii=False), item["id"]),
                        )
                except (TelegramBadRequest, TelegramForbiddenError):
                    missing.append(
                        tr(
                            "Не удалось получить ссылку: администратору нужно дать боту право приглашать пользователей."
                        )
                    )
        elif item["item_type"] == "question_text":
            correct = database.one(
                "SELECT 1 FROM request_answers WHERE request_id=? AND item_id=? AND is_correct=1",
                (rid, item["id"]),
            )
            if not correct:
                question = question or item
                missing.append(tr("Ответьте: ") + data["question"])
        else:
            missing.append(tr("Условие пока не поддерживается: ") + item["item_type"])
    database.execute(
        "UPDATE join_requests SET last_checked=?,updated_at=? WHERE id=?",
        (timeutils.iso(), timeutils.iso(), rid),
    )
    if not missing:
        try:
            await bot.approve_chat_join_request(
                r["telegram_chat_id"], r["telegram_user_id"]
            )
            database.execute(
                "UPDATE join_requests SET status='approved',updated_at=? WHERE id=?",
                (timeutils.iso(), rid),
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            if await access.member_ok(
                bot, r["telegram_chat_id"], r["telegram_user_id"]
            ):
                database.execute(
                    "UPDATE join_requests SET status='approved',updated_at=? WHERE id=?",
                    (timeutils.iso(), rid),
                )
            else:
                database.log_event(
                    r["telegram_user_id"], "JOIN_APPROVE_FAILED", str(exc)
                )
                if notify:
                    await bot.send_message(
                        r["telegram_user_id"],
                        tr(
                            "✅ Условия выполнены, но принять заявку пока не удалось. Администратору нужно проверить права бота. Заявка остаётся ожидающей."
                        ),
                    )
                return
        await notify_join_approved(rid, bot)
    elif notify:
        target = r["contact_chat_id"] or r["telegram_user_id"]
        try:
            await bot.send_message(
                target,
                "🔐 "
                + html.escape(r["title"])
                + "\n"
                + "\n".join(html.escape(x) for x in missing)
                + (
                    tr("\nДля ответа: /answer ") + str(rid) + tr(" ваш ответ")
                    if question
                    else ""
                )
                + tr("\nЗаявка остаётся ожидающей."),
                reply_markup=ui.kb(
                    links
                    + [
                        [
                            InlineKeyboardButton(
                                text=tr("🔄 Проверить"),
                                callback_data=f"joincheck:{rid}",
                            )
                        ]
                    ]
                ),
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            pass


async def notify_join_approved(rid, bot):
    r = database.one(
        "SELECT j.*,c.title,c.username,c.invite_link FROM join_requests j JOIN channels c ON c.id=j.channel_id WHERE j.id=? AND j.status='approved' AND j.approval_notified_at IS NULL",
        (rid,),
    )
    if not r:
        return
    activate(r["telegram_user_id"])
    url = "https://t.me/" + r["username"] if r["username"] else r["invite_link"]
    markup = (
        ui.kb([[InlineKeyboardButton(text=tr("📢 Открыть канал"), url=url)]])
        if url
        else None
    )
    for target in dict.fromkeys([r["telegram_user_id"], r["contact_chat_id"]]):
        if not target:
            continue
        try:
            await bot.send_message(
                target,
                tr("✅ Условия выполнены! Ваша заявка в «")
                + html.escape(r["title"])
                + tr("» одобрена."),
                reply_markup=markup,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            continue
        database.execute(
            "UPDATE join_requests SET approval_notified_at=? WHERE id=?",
            (timeutils.iso(), rid),
        )
        return


@router.chat_join_request()
async def join_request(req: ChatJoinRequest, bot: Bot):
    uid = req.from_user.id
    if accounts.blocked(uid):
        return
    ch = database.one(
        "SELECT * FROM channels WHERE telegram_chat_id=? AND is_active=1 AND auto_requests=1",
        (req.chat.id,),
    )
    if (
        not ch
        or accounts.blocked(ch["owner_telegram_id"])
        or not accounts.channel_allowed(ch["owner_telegram_id"], ch["id"])
    ):
        return
    r = database.one(
        "SELECT id FROM join_requests WHERE telegram_user_id=? AND channel_id=? AND status='pending'",
        (uid, ch["id"]),
    )
    if r:
        rid = r["id"]
        database.execute(
            "UPDATE join_requests SET contact_chat_id=? WHERE id=?",
            (req.user_chat_id, rid),
        )
    else:
        rid = database.execute(
            "INSERT INTO join_requests(telegram_user_id,channel_id,contact_chat_id,created_at,updated_at) VALUES(?,?,?,?,?)",
            (uid, ch["id"], req.user_chat_id, timeutils.iso(), timeutils.iso()),
        ).lastrowid
    await check_request(rid, bot, True)


@router.callback_query(F.data.startswith("joincheck:"))
async def join_recheck(c: CallbackQuery, bot: Bot):
    rid = int(c.data.split(":")[1])
    r = database.one(
        "SELECT status FROM join_requests WHERE id=? AND telegram_user_id=?",
        (rid, c.from_user.id),
    )
    if not r:
        raise ValueError(tr("Заявка не найдена."))
    if r["status"] == "approved":
        await c.answer(
            tr("✅ Условия выполнены, заявка уже одобрена."), show_alert=True
        )
        return
    await c.answer(tr("Проверяю."))
    await check_request(rid, bot, True)
    approved = database.one(
        "SELECT c.title,c.username,c.invite_link FROM join_requests j JOIN channels c ON c.id=j.channel_id WHERE j.id=? AND j.status='approved'",
        (rid,),
    )
    if approved:
        url = (
            "https://t.me/" + approved["username"]
            if approved["username"]
            else approved["invite_link"]
        )
        markup = (
            ui.kb([[InlineKeyboardButton(text=tr("📢 Открыть канал"), url=url)]])
            if url
            else None
        )
        await ui.edit(
            c,
            tr("✅ Условия выполнены! Заявка в «")
            + html.escape(approved["title"])
            + tr("» одобрена."),
            markup,
        )
        database.execute(
            "UPDATE join_requests SET approval_notified_at=COALESCE(approval_notified_at,?) WHERE id=?",
            (timeutils.iso(), rid),
        )


@router.message(Command("answer"))
async def join_answer(m: Message, bot: Bot):
    parts = m.text.split(maxsplit=2)
    if len(parts) != 3:
        raise ValueError(tr("Формат: /answer номер_заявки ответ"))
    rid = int(parts[1])
    r = database.one(
        "SELECT j.*,c.condition_id FROM join_requests j JOIN channels c ON c.id=j.channel_id WHERE j.id=? AND j.telegram_user_id=? AND j.status='pending'",
        (rid, m.from_user.id),
    )
    if not r:
        raise ValueError(tr("Ожидающая заявка не найдена."))
    items = database.all_rows(
        "SELECT * FROM condition_items WHERE condition_id=? AND item_type='question_text' ORDER BY position",
        (r["condition_id"],),
    )
    for item in items:
        if database.one(
            "SELECT 1 FROM request_answers WHERE request_id=? AND item_id=? AND is_correct=1",
            (rid, item["id"]),
        ):
            continue
        expected = json.loads(item["data"])["answer"]
        correct = expected.strip().casefold() == parts[2].strip().casefold()
        database.execute(
            "INSERT INTO request_answers(request_id,item_id,answer,is_correct,created_at) VALUES(?,?,?,?,?)",
            (rid, item["id"], parts[2], int(correct), timeutils.iso()),
        )
        await m.answer(
            tr("Ответ верный.")
            if correct
            else tr("Ответ неверный. Можно попробовать ещё раз.")
        )
        break
    await check_request(rid, bot, True)


@router.callback_query(F.data == "cond:create")
async def cond_create(c: CallbackQuery, state: FSMContext):
    await features_editors.wizard_start(state, {"kind": "condition", "step": "name"})
    await ui.edit(
        c,
        tr("Как назвать условие? Например: «Подписка на наш канал»."),
        ui.back("menu:conditions"),
    )
    await c.answer()


@router.callback_query(F.data.startswith("cw:"))
async def condition_choice(c: CallbackQuery, state: FSMContext, bot: Bot):
    _, token, value = c.data.split(":")
    d = await state.get_data()
    features_editors.wizard_check(d, token)
    if d.get("kind") != "condition" or d.get("step") != "type":
        raise ValueError(tr("Откройте создание условия заново."))
    await condition_value(c.message, state, bot, value, c.from_user.id, d)
    await c.answer()


async def condition_value(m, state, bot, raw, uid, d):
    step = d["step"]
    if step == "name":
        if not 1 <= len(raw) <= 80:
            raise ValueError(tr("Название: 1–80 символов."))
        await state.update_data(name=raw, step="type")
        await m.answer(
            tr("Что должен сделать человек?"),
            reply_markup=ui.kb(
                [
                    [
                        ui.choice(
                            tr("✅ Подписаться"), f"cw:{d['token']}:subscription"
                        ),
                        ui.choice(
                            tr("💬 Ответить на вопрос"),
                            f"cw:{d['token']}:question_text",
                        ),
                    ]
                ]
            ),
        )
        return
    if step == "type":
        if raw not in {"subscription", "question_text"}:
            raise ValueError(tr("Выберите вариант кнопкой."))
        await state.update_data(
            item_type=raw, step="channel" if raw == "subscription" else "question"
        )
        await m.answer(
            tr("Отправьте @username канала. Бот должен быть его администратором.")
            if raw == "subscription"
            else tr("Какой вопрос задать человеку?")
        )
        return
    if step == "question":
        if not 1 <= len(raw) <= 500:
            raise ValueError(tr("Вопрос: 1–500 символов."))
        await state.update_data(question=raw, step="answer")
        await m.answer(tr("Теперь отправьте правильный ответ отдельным сообщением."))
        return
    if step == "channel":
        chat = await bot.get_chat(chat_reference(raw))
        if chat.type not in {"channel", "group", "supergroup"}:
            raise ValueError(tr("Нужен канал или группа."))
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status not in {
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }:
            raise ValueError(tr("Добавьте бота администратором указанного канала."))
        value = {"chat_id": chat.id, "title": chat.title or raw}
        value["url"] = await subscription_link(bot, value)
    elif step == "answer":
        if not 1 <= len(raw) <= 200:
            raise ValueError(tr("Ответ: 1–200 символов."))
        value = {"question": d["question"], "answer": raw}
    else:
        raise ValueError(tr("Откройте создание условия заново."))
    with database.atomic():
        cid = database.db.execute(
            "INSERT INTO conditions(owner_telegram_id,name,created_at) VALUES(?,?,?)",
            (uid, d["name"], timeutils.iso()),
        ).lastrowid
        database.db.execute(
            "INSERT INTO condition_items(condition_id,item_type,data,position) VALUES(?,?,?,0)",
            (cid, d["item_type"], json.dumps(value, ensure_ascii=False)),
        )
    await state.clear()
    await m.answer(
        tr("✅ Условие создано. Выберите его в карточке нужного канала."),
        reply_markup=ui.back(f"cond:open:{cid}"),
    )


@router.callback_query(F.data.startswith("cond:open:"))
async def cond_open(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    r = database.one(
        "SELECT * FROM conditions WHERE id=? AND owner_telegram_id=?",
        (cid, c.from_user.id),
    )
    if not r:
        raise ValueError(tr("Условие не найдено."))
    lines = []
    for item in database.all_rows(
        "SELECT * FROM condition_items WHERE condition_id=? ORDER BY position", (cid,)
    ):
        value = json.loads(item["data"])
        lines.append(
            tr("✅ Подписка: ") + str(value.get("title", value.get("chat_id", "")))
            if item["item_type"] == "subscription"
            else tr("💬 Вопрос: ")
            + value.get("question", "")
            + tr("\nОтвет: ")
            + value.get("answer", "")
        )
    await ui.edit(
        c,
        html.escape(r["name"] + "\n\n" + "\n".join(lines)),
        ui.kb(
            [
                [
                    ui.choice(tr("🗑 Удалить"), f"cond:del:{cid}"),
                    ui.choice(tr("⬅️ Условия"), "menu:conditions"),
                ]
            ]
        ),
    )
    await c.answer()
