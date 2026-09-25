"""features / admin components."""

import html
import json
import secrets

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app import access as access
from app import accounts as accounts
from app import database as database
from app import plans, preferences
from app import timeutils as timeutils
from app import ui as ui
from app.features import editors as features_editors
from app.i18n import tr
from app.states import Broadcast
from config import ADMIN_ID

router = Router(name="features.admin")


class PlanSetting(StatesGroup):
    value = State()


@router.callback_query(F.data == "admin:plans")
async def plan_panel(c):
    if not access.admin_only(c):
        raise ValueError(tr("Нет доступа."))
    labels = [
        tr("Каналы"),
        tr("Источники"),
        tr("Новые посты"),
        tr("Обложки"),
        tr("Скачивания видео"),
        tr("Цвета кнопок"),
    ]
    fields = [
        "channels",
        "sources",
        "post_create",
        "cover",
        "download_video",
        "button_colors",
    ]
    rows = [
        [
            ui.choice(
                f"{tier.title()} · {label}: {plans.value(tier + '_' + field)}",
                f"plan:edit:{tier}_{field}",
            )
            for tier in ("free", "premium")
        ]
        for field, label in zip(fields, labels)
    ]
    rows += [
        [ui.choice(tr("Цены и оплата"), "payment:settings")],
        [ui.choice(tr("Назад"), "admin:premium")],
    ]
    await ui.edit(c, plans.description(), ui.kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("plan:edit:"))
async def plan_edit(c, state):
    if not access.admin_only(c):
        raise ValueError(tr("Нет доступа."))
    key = c.data.split(":", 2)[2]
    if key not in plans.LIMITS:
        raise ValueError(tr("Неизвестная настройка."))
    await state.set_state(PlanSetting.value)
    await state.set_data({"plan_key": key})
    _, low, high = plans.LIMITS[key]
    await ui.edit(
        c,
        tr(
            "Введите новый лимит: от {low} до {high}. −1 означает без ограничений.",
            low=low,
            high=high,
        ),
        ui.back("admin:plans"),
    )
    await c.answer()


@router.message(PlanSetting.value, F.text, ~F.text.startswith("/"))
async def plan_save(m, state):
    if m.from_user.id != ADMIN_ID:
        raise ValueError(tr("Нет доступа."))
    data = await state.get_data()
    plans.set_value(data["plan_key"], int(m.text), m.from_user.id)
    await state.clear()
    await ui.answer(
        m, tr("Настройки тарифа сохранены."), reply_markup=ui.back("admin:plans")
    )


@router.callback_query(F.data == "admin:main")
async def admin_main(c: CallbackQuery):
    if not access.admin_only(c):
        await c.answer(tr("Нет доступа"), show_alert=True)
        return
    await ui.edit(c, tr("👨\u200d💻 <b>Админ-панель</b>"), ui.admin_kb())
    await c.answer()


@router.callback_query(F.data == "admin:stats")
async def admin_stats(c: CallbackQuery):
    if not access.admin_only(c):
        await c.answer(tr("Нет доступа"), show_alert=True)
        return
    vals = {
        "users": database.one("SELECT COUNT(*) c FROM users")["c"],
        "objects": database.one("SELECT COUNT(*) c FROM channels WHERE is_active=1")[
            "c"
        ],
        "posts": database.one("SELECT COUNT(*) c FROM posts")["c"],
        "requests": database.one("SELECT COUNT(*) c FROM join_requests")["c"],
        "premium": database.one(
            "SELECT COUNT(*) c FROM premium WHERE active=1 AND (lifetime=1 OR julianday(expires_at)>julianday('now'))"
        )["c"],
    }
    await ui.edit(
        c,
        tr(
            "📊 <b>Статистика</b>\n\nПользователей: {v0}\nОбъектов: {v1}\nПостов: {v2}\nЗаявок: {v3}\nPremium: {v4}",
            v0=vals["users"],
            v1=vals["objects"],
            v2=vals["posts"],
            v3=vals["requests"],
            v4=vals["premium"],
        ),
        ui.admin_kb(),
    )
    await c.answer()


@router.callback_query(F.data == "admin:users")
async def admin_users(c: CallbackQuery):
    if not access.admin_only(c):
        await c.answer(tr("Нет доступа"), show_alert=True)
        return
    from app.features.user_admin import user_list

    await user_list(c, 0)


@router.callback_query(F.data == "admin:channels")
async def admin_channels(c: CallbackQuery):
    if not access.admin_only(c):
        await c.answer(tr("Нет доступа"), show_alert=True)
        return
    rows = database.all_rows(
        "SELECT title,chat_type,owner_telegram_id,is_active,auto_requests FROM channels ORDER BY id DESC LIMIT 30"
    )
    text = tr("📺 <b>Объекты</b>\n\n") + "\n".join(
        tr(
            "{v0} {v1} • {v2} • {v3} • заявки={v4}",
            v0="🟢" if r["is_active"] else "🔴",
            v1=html.escape(r["title"]),
            v2=r["chat_type"],
            v3=r["owner_telegram_id"],
            v4="ON" if r["auto_requests"] else "OFF",
        )
        for r in rows
    )
    await ui.edit(c, text[:4000], ui.admin_kb())
    await c.answer()


@router.callback_query(F.data == "admin:logs")
async def admin_logs(c: CallbackQuery):
    if not access.admin_only(c):
        await c.answer(tr("Нет доступа"), show_alert=True)
        return
    rows = database.all_rows(
        "SELECT telegram_id,event,details,created_at FROM logs ORDER BY id DESC LIMIT 40"
    )
    text = tr("📝 <b>Логи</b>\n\n") + "\n".join(
        f"{preferences.display(ADMIN_ID, r['created_at'])} | {r['event']} | {r['telegram_id']} | {html.escape(r['details'] or '')}"
        for r in rows
    )
    await ui.edit(c, text[:4000], ui.admin_kb())
    await c.answer()


@router.callback_query(F.data == "admin:blocks")
async def admin_blocks(c: CallbackQuery):
    if not access.admin_only(c):
        await c.answer(tr("Нет доступа"), show_alert=True)
        return
    rows = database.all_rows(
        "SELECT telegram_id,reason FROM blocked_users ORDER BY created_at DESC LIMIT 30"
    )
    text = tr("🚫 <b>Блокировки</b>\n\n") + "\n".join(
        f"{r['telegram_id']} — {html.escape(r['reason'] or '')}" for r in rows
    ) or tr("🚫 <b>Блокировки</b>\n\nПусто.")
    await ui.edit(c, text, ui.admin_kb())
    await c.answer()


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast(c: CallbackQuery, state: FSMContext):
    if not access.admin_only(c):
        await c.answer(tr("Нет доступа"), show_alert=True)
        return
    await state.set_state(Broadcast.content)
    await ui.edit(
        c,
        tr(
            "📢 Отправьте или перешлите сообщение любого типа, затем подтвердите рассылку. /cancel — отмена"
        ),
        ui.back("admin:main"),
    )
    await c.answer()


@router.message(Broadcast.content, ~F.successful_payment, ~F.text.startswith("/"))
async def broadcast_send(m: Message, state: FSMContext, bot: Bot):
    if m.from_user.id != ADMIN_ID:
        return
    key = f"broadcast_draft:{m.from_user.id}"
    saved = database.setting(key)
    draft = json.loads(saved) if saved else {}
    if not m.media_group_id or draft.get("group") != m.media_group_id:
        draft = {
            "token": secrets.token_hex(4),
            "chat_id": m.chat.id,
            "ids": [],
            "group": m.media_group_id,
        }
    if m.message_id not in draft["ids"]:
        draft["ids"].append(m.message_id)
    database.execute(
        "INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(draft)),
    )
    await ui.answer(
        m,
        tr(
            "📢 Сообщение готово к рассылке. Файлов/сообщений: {count}",
            count=len(draft["ids"]),
        ),
        reply_markup=ui.kb(
            [
                [
                    ui.choice(
                        tr("📢 Отправить рассылку"), f"broadcast:send:{draft['token']}"
                    )
                ],
                [ui.choice(tr("❌ Отмена"), "admin:main")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("broadcast:send:"))
async def broadcast_confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
    if c.from_user.id != ADMIN_ID:
        raise ValueError(tr("Нет доступа."))
    key = f"broadcast_draft:{c.from_user.id}"
    if await state.get_state() != Broadcast.content.state:
        raise ValueError(tr("Этот шаг устарел. Откройте настройку заново."))
    saved = database.setting(key)
    draft = json.loads(saved) if saved else {}
    if draft.get("token") != c.data.rsplit(":", 1)[-1]:
        raise ValueError(tr("Этот шаг устарел. Откройте настройку заново."))
    from services import broadcasts

    broadcasts.enqueue(c.from_user.id, draft)
    database.execute("DELETE FROM app_settings WHERE key=?", (key,))
    await state.clear()
    await c.answer()
    await ui.edit(c, tr("📢 Рассылка выполняется."), ui.back("admin:main"))
    await broadcasts.tick(bot)


@router.callback_query(F.data.startswith("mail:"))
async def broadcast_status(c: CallbackQuery):
    if not access.admin_only(c):
        raise ValueError(tr("Нет доступа."))
    parts = c.data.split(":")
    action = parts[1]
    if action == "list":
        rows = [
            [ui.choice(f"#{r['id']} · {r['created_at'][:16]}", f"mail:open:{r['id']}")]
            for r in database.all_rows(
                "SELECT id,created_at FROM broadcast_jobs ORDER BY id DESC LIMIT 30"
            )
        ]
        rows.append([ui.choice(tr("Назад"), "admin:main")])
        await ui.edit(c, tr("📬 Состояние рассылок"), ui.kb(rows))
    else:
        jid = int(parts[2])
        job = database.one(
            "SELECT * FROM broadcast_jobs WHERE id=? AND owner_id=?",
            (jid, c.from_user.id),
        )
        if not job:
            raise ValueError(tr("Не найдено."))
        if action in {"retry", "sent", "confirm"}:
            uid = int(parts[3])
            row = database.one(
                "SELECT status FROM broadcast_targets WHERE job_id=? AND user_id=?",
                (jid, uid),
            )
            if not row or row[0] not in {"failed", "uncertain"}:
                raise ValueError(tr("Этот шаг устарел. Откройте настройку заново."))
            if action == "confirm":
                await ui.edit(
                    c,
                    tr("Проверьте доставку вручную. Повтор может создать дубликат."),
                    ui.kb(
                        [
                            [
                                ui.choice(
                                    tr("Повторить после проверки"),
                                    f"mail:retry:{jid}:{uid}",
                                )
                            ],
                            [ui.choice(tr("Уже доставлено"), f"mail:sent:{jid}:{uid}")],
                            [ui.choice(tr("Назад"), f"mail:open:{jid}")],
                        ]
                    ),
                )
                await c.answer()
                return
            database.execute(
                "UPDATE broadcast_targets SET status=?,retry_at=NULL,error=NULL WHERE job_id=? AND user_id=?",
                ("pending" if action == "retry" else "sent", jid, uid),
            )
            database.execute(
                "UPDATE broadcast_jobs SET status='queued' WHERE id=?", (jid,)
            )
        elif action != "open":
            raise ValueError(tr("Неизвестное действие."))
        counts = {
            r["status"]: r["n"]
            for r in database.all_rows(
                "SELECT status,COUNT(*) n FROM broadcast_targets WHERE job_id=? GROUP BY status",
                (jid,),
            )
        }
        text = tr(
            "Рассылка #{id}: отправлено {sent}, ожидают {pending}, ошибок {failed}, требуют проверки {uncertain}.",
            id=jid,
            sent=counts.get("sent", 0),
            pending=counts.get("pending", 0) + counts.get("sending", 0),
            failed=counts.get("failed", 0),
            uncertain=counts.get("uncertain", 0),
        )
        rows = [
            [ui.choice(str(r["user_id"]), f"mail:confirm:{jid}:{r['user_id']}")]
            for r in database.all_rows(
                "SELECT user_id FROM broadcast_targets WHERE job_id=? AND status IN ('failed','uncertain') ORDER BY user_id LIMIT 10",
                (jid,),
            )
        ]
        rows += [
            [
                ui.choice(tr("Обновить"), f"mail:open:{jid}"),
                ui.choice(tr("Назад"), "mail:list"),
            ]
        ]
        await ui.edit(c, text, ui.kb(rows))
    await c.answer()


@router.callback_query(F.data == "admin:premium")
async def admin_premium(c: CallbackQuery):
    if not access.admin_only(c):
        raise ValueError(tr("Нет доступа."))
    await ui.edit(
        c,
        tr("💎 Premium\nВыберите, что хотите изменить."),
        ui.kb(
            [
                [ui.choice(tr("⚙️ Лимиты и возможности тарифов"), "admin:plans")],
                [
                    ui.choice(tr("👤 Premium пользователя"), "aw:user"),
                    ui.choice(tr("👥 За приглашения"), "aw:ref"),
                ],
                [
                    ui.choice(tr("🎟 Промокоды"), "aw:promos"),
                    ui.choice(tr("⬅️ Админ-панель"), "admin:main"),
                ],
            ]
        ),
    )
    await c.answer()


def admin_user_card(uid):
    u = database.one("SELECT * FROM users WHERE telegram_id=?", (uid,))
    if not u:
        raise ValueError(tr("Пользователь ещё не запускал бота."))
    p = database.one("SELECT * FROM premium WHERE user_id=?", (uid,))
    status = tr("нет")
    if accounts.has_premium(uid):
        status = (
            tr("бессрочно")
            if p["lifetime"]
            else tr("до ") + preferences.display(ADMIN_ID, p["expires_at"])
        )
    text = f"👤 {uid} @{html.escape(u['username'] or '')}\n💎 Premium: {status}"
    rows = ui.button_grid(
        [
            ui.choice(label, f"aw:person:{uid}:{action}")
            for action, label in [
                ("add", tr("🎁 Добавить дни")),
                ("subtract", tr("➖ Убрать дни")),
                ("set", tr("📅 До даты")),
                ("lifetime", tr("♾ Бессрочно")),
                ("revoke", tr("⛔ Отменить Premium")),
                ("history", tr("📝 История")),
                ("block", tr("🚫 Заблокировать")),
                ("unblock", tr("✅ Разблокировать")),
            ]
        ],
        2,
    )
    rows.append([ui.choice("⬅️ Premium", "admin:premium")])
    return text, ui.kb(rows)


def ref_panel():
    label = accounts.CONDITION_LABELS.get(
        database.setting("ref_condition"), tr("не задано")
    )
    text = tr(
        "👥 Награда за приглашение\nПригласившему: {v0} дн.\nНовому пользователю: {v1} дн.\nКогда выдавать: {v2}.",
        v0=database.setting("ref_inviter_days"),
        v1=database.setting("ref_invitee_days"),
        v2=label,
    )
    return text, ui.kb(
        [
            [
                ui.choice(tr("Дни пригласившему"), "aw:refdays:ref_inviter_days"),
                ui.choice(tr("Дни новичку"), "aw:refdays:ref_invitee_days"),
            ],
            [
                ui.choice(tr("🔐 Условие награды"), "aw:refcondition"),
                ui.choice("⬅️ Premium", "admin:premium"),
            ],
        ]
    )


def conditions_buttons(prefix, token, allow_none=False):
    options = [
        ("channel", tr("📺 Добавил канал")),
        ("channel_and_publish", tr("📢 Опубликовал пост")),
        ("purchase", tr("⭐ Купил Premium")),
        ("subscription", tr("✅ Подписался на каналы")),
    ]
    if allow_none:
        options.insert(0, ("none", tr("Без условий")))
    return ui.kb(
        ui.button_grid(
            [
                ui.choice(label, f"{prefix}:condition:{token}:{value}")
                for value, label in options
            ],
            2,
        )
    )


async def promo_card(c, code):
    p = database.one("SELECT * FROM promos WHERE code=?", (code,))
    if not p:
        raise ValueError(tr("Промокод не найден."))
    n = database.one("SELECT COUNT(*) n FROM promo_uses WHERE code=?", (code,))["n"]
    text = tr(
        "🎟 {v0}\nPremium: {v1} дней\nИспользовано: {v2} из {v3}\nДля: {v4}\nСрок кода: {v5}\nУсловие: {v6}\n{v7}",
        v0=code,
        v1=p["days"],
        v2=n,
        v3=p["max_uses"],
        v4=p["personal_id"] or tr("всех"),
        v5=preferences.display(ADMIN_ID, p["expires_at"])
        if p["expires_at"]
        else tr("без срока"),
        v6=accounts.CONDITION_LABELS[p["condition"]],
        v7=tr("Включён") if p["active"] else tr("Выключен"),
    )
    rows = ui.button_grid(
        [
            ui.choice(label, f"aw:promo:{code}:{action}")
            for action, label in [
                ("edit", tr("✏️ Изменить")),
                ("toggle", tr("Вкл. / выкл.")),
                ("uses", tr("👥 Активации")),
                ("delete", tr("🗑 Удалить")),
            ]
        ],
        2,
    )
    rows.append([ui.choice(tr("⬅️ Промокоды"), "aw:promos")])
    await ui.edit(c, html.escape(text), ui.kb(rows))


async def promo_step(m, state):
    d = await state.get_data()
    step = d["step"]
    token = d["token"]
    p = d.get("promo", {})
    if step == "days":
        values = list(
            dict.fromkeys([int(database.setting("promo_default_days")), 1, 3, 7, 30])
        )
        await ui.answer(
            m,
            tr("Сколько дней Premium даёт этот код? Можно написать своё число."),
            reply_markup=ui.kb(
                ui.button_grid(
                    [
                        ui.choice(tr("{v0} дн.", v0=n), f"aw:value:{token}:{n}")
                        for n in values
                    ],
                    3,
                )
            ),
        )
        return
    if step == "uses":
        await ui.answer(
            m,
            tr(
                "Сколько всего активаций разрешено?\nНажмите вариант или напишите число."
            ),
            reply_markup=ui.kb(
                ui.button_grid(
                    [
                        ui.choice(label, f"aw:value:{token}:{n}")
                        for n, label in [
                            (1, tr("Одноразовый")),
                            (10, "10"),
                            (100, "100"),
                            (1000, "1000"),
                        ]
                    ],
                    2,
                )
            ),
        )
        return
    if step == "expiry":
        await ui.answer(
            m,
            tr(
                "До какой даты можно активировать код?\nНапишите дату ДД.ММ.ГГГГ или выберите:"
            ),
            reply_markup=ui.kb(
                [[ui.choice(tr("Без срока"), f"aw:value:{token}:none")]]
            ),
        )
        return
    if step == "audience":
        await ui.answer(
            m,
            tr("Кому доступен код?"),
            reply_markup=ui.kb(
                [
                    [
                        ui.choice(tr("Всем"), f"aw:value:{token}:all"),
                        ui.choice(tr("Одному человеку"), f"aw:value:{token}:personal"),
                    ]
                ]
            ),
        )
        return
    if step == "condition":
        await ui.answer(
            m,
            tr("Что нужно сделать для активации?"),
            reply_markup=conditions_buttons("aw", token, True),
        )
        return
    if step == "confirm":
        text = tr(
            "Проверьте промокод {v0}:\n{v1} дней Premium, {v2} активаций.\nДля: {v3}.\nДо: {v4}.\nУсловие: {v5}",
            v0=p["code"],
            v1=p["days"],
            v2=p["max_uses"],
            v3=p.get("personal_id") or tr("всех"),
            v4=preferences.display(ADMIN_ID, p["expires_at"])
            if p.get("expires_at")
            else tr("без срока"),
            v5=accounts.CONDITION_LABELS[p["condition"]],
        )
        await ui.answer(
            m,
            html.escape(text),
            reply_markup=ui.kb(
                [
                    [
                        ui.choice(tr("✅ Сохранить"), f"aw:save:{token}"),
                        ui.choice(tr("❌ Отмена"), "aw:promos"),
                    ]
                ]
            ),
        )


@router.callback_query(F.data.startswith("aw:"))
async def admin_wizard(c: CallbackQuery, state: FSMContext, bot: Bot):
    if not access.admin_only(c):
        raise ValueError(tr("Нет доступа."))
    p = c.data.split(":")
    action = p[1]
    if action not in {"value", "condition", "save"}:
        await state.clear()
    if action in {"value", "condition", "save"}:
        d = await state.get_data()
        features_editors.wizard_check(d, p[2])
        if action == "save":
            if d.get("kind") != "promo" or d.get("step") != "confirm":
                raise ValueError(tr("Заполните все поля."))
            promo = d["promo"]
            code = promo["code"]
            if d.get("edit_code"):
                uses = database.one(
                    "SELECT COUNT(*) n FROM promo_uses WHERE code=?", (code,)
                )["n"]
                if promo["max_uses"] < uses:
                    raise ValueError(tr("Лимит меньше уже выполненных активаций."))
                database.execute(
                    "UPDATE promos SET days=?,max_uses=?,expires_at=?,personal_id=?,condition=? WHERE code=?",
                    (
                        promo["days"],
                        promo["max_uses"],
                        promo.get("expires_at"),
                        promo.get("personal_id"),
                        promo["condition"],
                        code,
                    ),
                )
            else:
                if database.one("SELECT 1 FROM promos WHERE code=?", (code,)):
                    raise ValueError(tr("Код уже существует."))
                database.execute(
                    "INSERT INTO promos(code,days,max_uses,expires_at,personal_id,condition,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        code,
                        promo["days"],
                        promo["max_uses"],
                        promo.get("expires_at"),
                        promo.get("personal_id"),
                        promo["condition"],
                        timeutils.iso(),
                    ),
                )
            await state.clear()
            await promo_card(c, code)
            await c.answer(tr("Сохранено."))
            return
        await features_editors.guided_value(c.message, state, bot, p[3], c.from_user.id)
        await c.answer()
        return
    if action == "user":
        await features_editors.wizard_start(
            state, {"kind": "admin_user", "step": "uid"}
        )
        await ui.edit(
            c, tr("Отправьте Telegram ID пользователя."), ui.back("admin:premium")
        )
    elif action == "person":
        uid = int(p[2])
        mode = p[3]
        admin_user_card(uid)
        if mode == "history":
            rows = database.all_rows(
                "SELECT * FROM premium_history WHERE user_id=? ORDER BY id DESC LIMIT 15",
                (uid,),
            )
            await ui.edit(
                c,
                tr("История\n")
                + "\n".join(
                    html.escape(
                        f"{preferences.display(ADMIN_ID, r['created_at'])}: {r['reason']} → {preferences.display(ADMIN_ID, r['new_expiry']) if r['new_expiry'] not in {None, 'lifetime'} else tr('бессрочно') if r['new_expiry'] == 'lifetime' else tr('нет')}"
                    )
                    for r in rows
                ),
                ui.back("aw:user"),
            )
        elif mode in {"lifetime", "revoke", "block", "unblock"}:
            if uid == ADMIN_ID and mode == "block":
                raise ValueError(tr("Нельзя заблокировать администратора."))
            if mode == "block":
                database.execute(
                    "INSERT OR REPLACE INTO blocked_users VALUES(?,?,?)",
                    (uid, "admin", timeutils.iso()),
                )
            elif mode == "unblock":
                database.execute(
                    "DELETE FROM blocked_users WHERE telegram_id=?", (uid,)
                )
            else:
                with database.atomic():
                    accounts.premium_change_tx(
                        uid, mode, None, "admin:" + mode, ADMIN_ID
                    )
            text, markup = admin_user_card(uid)
            await ui.edit(c, text, markup)
        elif mode in {"add", "subtract", "set"}:
            d = await features_editors.wizard_start(
                state,
                {"kind": "admin_change", "uid": uid, "mode": mode, "step": "value"},
            )
            rows = (
                ui.button_grid(
                    [
                        ui.choice(tr("{v0} дн.", v0=n), f"aw:value:{d['token']}:{n}")
                        for n in (1, 3, 7, 30)
                    ],
                    4,
                )
                if mode != "set"
                else []
            )
            rows.append([ui.choice("⬅️ Premium", "admin:premium")])
            await ui.edit(
                c,
                tr("Введите дату ДД.ММ.ГГГГ (до конца дня UTC).")
                if mode == "set"
                else tr("Сколько дней? Выберите или напишите число."),
                ui.kb(rows),
            )
        else:
            raise ValueError(tr("Неизвестное действие."))
    elif action == "ref":
        await state.clear()
        text, markup = ref_panel()
        await ui.edit(c, text, markup)
    elif action == "refdays":
        key = p[2]
        if key not in {"ref_inviter_days", "ref_invitee_days"}:
            raise ValueError(tr("Неверная настройка."))
        d = await features_editors.wizard_start(
            state, {"kind": "refdays", "key": key, "step": "value"}
        )
        await ui.edit(
            c,
            tr(
                "Сколько дней выдавать? Можно написать число; 0 — без награды этой стороне."
            ),
            ui.kb(
                ui.button_grid(
                    [
                        ui.choice(str(n), f"aw:value:{d['token']}:{n}")
                        for n in (0, 1, 2, 3, 7, 14)
                    ],
                    3,
                )
            ),
        )
    elif action == "refcondition":
        d = await features_editors.wizard_start(
            state, {"kind": "refcondition", "step": "condition"}
        )
        await ui.edit(
            c, tr("Когда выдавать награду?"), conditions_buttons("aw", d["token"])
        )
    elif action == "promos":
        await state.clear()
        promos = database.all_rows(
            "SELECT code,active FROM promos ORDER BY created_at DESC LIMIT 30"
        )
        rows = ui.button_grid(
            [
                ui.choice(
                    ("🟢 " if r["active"] else "⚪ ") + r["code"],
                    f"aw:promo:{r['code']}:open",
                )
                for r in promos
            ],
            2,
        )
        rows += [
            [
                ui.choice(tr("➕ Создать код"), "aw:newpromo"),
                ui.choice(tr("⚙️ Срок по умолчанию"), "aw:defaultdays"),
            ],
            [ui.choice("⬅️ Premium", "admin:premium")],
        ]
        await ui.edit(c, tr("🎟 Промокоды"), ui.kb(rows))
    elif action == "defaultdays":
        await features_editors.wizard_start(
            state, {"kind": "defaultdays", "step": "value"}
        )
        await ui.edit(
            c,
            tr("Сколько дней Premium предлагать для нового кода?"),
            ui.back("aw:promos"),
        )
    elif action == "newpromo":
        await features_editors.wizard_start(
            state, {"kind": "promo", "step": "code", "promo": {}}
        )
        await ui.edit(
            c,
            tr("Придумайте промокод: латинские буквы и цифры.\nНапример: WELCOME7"),
            ui.back("aw:promos"),
        )
    elif action == "promo":
        code = p[2]
        mode = p[3]
        promo = database.one("SELECT * FROM promos WHERE code=?", (code,))
        if not promo:
            raise ValueError(tr("Промокод не найден."))
        if mode == "edit":
            await features_editors.wizard_start(
                state,
                {
                    "kind": "promo",
                    "step": "days",
                    "promo": dict(promo),
                    "edit_code": code,
                },
            )
            await promo_step(c.message, state)
        elif mode == "toggle":
            database.execute("UPDATE promos SET active=1-active WHERE code=?", (code,))
            await promo_card(c, code)
        elif mode == "delete":
            if database.one("SELECT 1 FROM promo_uses WHERE code=?", (code,)):
                database.execute("UPDATE promos SET active=0 WHERE code=?", (code,))
            else:
                database.execute("DELETE FROM promos WHERE code=?", (code,))
            await ui.edit(
                c,
                tr("Код удалён или отключён с сохранением использованных активаций."),
                ui.back("aw:promos"),
            )
        elif mode == "uses":
            uses = database.all_rows(
                "SELECT user_id,created_at FROM promo_uses WHERE code=? ORDER BY created_at DESC LIMIT 30",
                (code,),
            )
            await ui.edit(
                c,
                tr("Последние активации:\n")
                + "\n".join(
                    f"{r['user_id']} • {preferences.display(ADMIN_ID, r['created_at'])}"
                    for r in uses
                ),
                ui.back(f"aw:promo:{code}:open"),
            )
        else:
            await promo_card(c, code)
    else:
        raise ValueError(tr("Неизвестное действие."))
    await c.answer()
