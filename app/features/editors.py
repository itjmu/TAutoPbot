"""features / editors components."""

import html
import json
import re
import secrets
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import accounts as accounts
from app import content as content
from app import database as database
from app import preferences
from app import timeutils as timeutils
from app import ui as ui
from app.features import admin as features_admin
from app.features import channels as features_channels
from app.features import conditions as features_conditions
from app.i18n import tr
from app.states import GuidedInput
from config import ADMIN_ID

router = Router(name="features.editors")


def button_document(uid, scope, oid, writing=False):
    if scope == "p":
        p = content.mutable_post(oid, uid) if writing else content.post_owned(oid, uid)
        if writing and p["status"] == "scheduled":
            with database.atomic():
                database.db.execute(
                    "UPDATE scheduled_posts SET active=0 WHERE post_id=?", (oid,)
                )
                database.db.execute(
                    "UPDATE posts SET status='draft' WHERE id=?", (oid,)
                )
        return content.entity_list(p["buttons_json"])
    if scope == "t":
        t = database.one(
            "SELECT * FROM templates WHERE id=? AND owner_id=?", (oid, uid)
        )
        if not t:
            raise ValueError(tr("Шаблон не найден."))
        return content.entity_list(t["buttons_json"])
    raise ValueError(tr("Неверный редактор."))


def save_buttons(uid, scope, oid, buttons):
    button_document(uid, scope, oid, True)
    content.validate_buttons(buttons)
    if any(b.get("style") not in accounts.button_styles(uid) for b in buttons):
        raise ValueError(tr("Этот цвет недоступен в вашем тарифе."))
    table = "posts" if scope == "p" else "templates"
    database.execute(
        f"UPDATE {table} SET buttons_json=? WHERE id=?",
        (json.dumps(buttons, ensure_ascii=False), oid),
    )
    if scope == "p":
        content.reset_snapshot(oid)


async def button_panel(c, scope, oid):
    buttons = button_document(c.from_user.id, scope, oid)
    rows = ui.button_grid(
        [
            ui.choice(label, f"bw:new:{scope}:{oid}:{typ}")
            for typ, label in [
                ("url", tr("🔗 Ссылка")),
                ("reaction", tr("❤️ Реакция")),
                ("subscription", tr("🔐 Подписка")),
                ("alert", tr("💬 Подсказка")),
            ]
        ],
        2,
    )
    for b in buttons:
        rows.append(
            [
                ui.choice("✏️ " + b["text"][:22], f"bw:edit:{scope}:{oid}:{b['id']}"),
                ui.choice(tr("✖ Удалить"), f"bw:del:{scope}:{oid}:{b['id']}"),
            ]
        )
    if buttons:
        rows.append(
            [
                ui.choice(tr("{v0} в ряд", v0=n), f"bw:layout:{scope}:{oid}:{n}")
                for n in (1, 2, 3, 4)
            ]
        )
    parent = f"p:{oid}:preview" if scope == "p" else f"tpl:{oid}:open"
    rows.append([ui.choice(tr("✅ Готово"), parent)])
    await ui.edit(
        c,
        tr(
            "🔘 Кнопки под постом\nВыберите, какую кнопку добавить. Я задам несколько простых вопросов.\nДля изменения нажмите название уже добавленной кнопки."
        ),
        ui.kb(rows),
    )


async def wizard_start(state, values):
    values = dict(values, token=secrets.token_hex(3))
    await state.set_data(values)
    await state.set_state(GuidedInput.value)
    return values


def wizard_check(data, token):
    if data.get("token") != token:
        raise ValueError(tr("Этот шаг устарел. Откройте настройку заново."))


async def ask_position(m, state):
    d = await state.get_data()
    token = d["token"]
    await state.update_data(step="position")
    await m.answer(
        tr("Где разместить кнопку?"),
        reply_markup=ui.kb(
            [
                [
                    ui.choice(tr("⬇️ Новая строка"), f"bw:position:{token}:new"),
                    ui.choice(tr("➡️ Рядом с последней"), f"bw:position:{token}:same"),
                ]
            ]
        ),
    )


async def ask_color(m, state, uid):
    d = await state.get_data()
    await state.update_data(step="color")
    labels = {
        None: tr("Обычный"),
        "primary": tr("🔵 Синий"),
        "success": tr("🟢 Зелёный"),
        "danger": tr("🔴 Красный"),
    }
    await m.answer(
        tr("Выберите цвет кнопки:"),
        reply_markup=ui.kb(
            [
                [
                    ui.choice(
                        label
                        + (
                            " · Premium"
                            if style not in accounts.button_styles(uid)
                            else ""
                        ),
                        f"bw:color:{d['token']}:{style or 'default'}",
                    )
                ]
                for style, label in labels.items()
            ]
        ),
    )


@router.callback_query(F.data.startswith("bw:"))
async def button_wizard(c: CallbackQuery, state: FSMContext):
    parts = c.data.split(":")
    action = parts[1]
    if action == "color":
        d = await state.get_data()
        wizard_check(d, parts[2])
        if d.get("step") != "color":
            raise ValueError(tr("Этот шаг устарел. Откройте настройку заново."))
        style = None if parts[3] == "default" else parts[3]
        if style not in accounts.button_styles(c.from_user.id):
            raise ValueError(tr("Этот цвет недоступен в вашем тарифе."))
        d["button"]["style"] = style
        await state.update_data(button=d["button"])
        await ask_position(c.message, state)
        await c.answer()
        return
    if action == "position":
        d = await state.get_data()
        wizard_check(d, parts[2])
        if d.get("step") != "position":
            raise ValueError(tr("Сначала заполните кнопку."))
        buttons = button_document(c.from_user.id, d["scope"], d["oid"], True)
        b = d["button"]
        buttons = [x for x in buttons if x["id"] != b["id"]]
        last = max([x["row"] for x in buttons], default=0)
        b["row"] = last if parts[3] == "same" and last else last + 1
        buttons.append(b)
        save_buttons(c.from_user.id, d["scope"], d["oid"], buttons)
        await state.clear()
        await button_panel(c, d["scope"], d["oid"])
        await c.answer(tr("Кнопка сохранена."))
        return
    scope = parts[2]
    oid = int(parts[3])
    buttons = button_document(c.from_user.id, scope, oid, True)
    if action == "del":
        save_buttons(
            c.from_user.id, scope, oid, [b for b in buttons if b["id"] != parts[4]]
        )
        await state.clear()
        await button_panel(c, scope, oid)
        await c.answer()
        return
    if action == "layout":
        n = int(parts[4])
        if n not in {1, 2, 3, 4}:
            raise ValueError(tr("Неверное количество."))
        save_buttons(
            c.from_user.id,
            scope,
            oid,
            [dict(b, row=i // n + 1) for i, b in enumerate(buttons)],
        )
        await state.clear()
        await button_panel(c, scope, oid)
        await c.answer(tr("Расположение сохранено."))
        return
    if action == "panel":
        await state.clear()
        await button_panel(c, scope, oid)
        await c.answer()
        return
    if action == "edit":
        b = next((dict(x) for x in buttons if x["id"] == parts[4]), None)
        if not b:
            raise ValueError(tr("Кнопка не найдена."))
    elif action == "new":
        if len(buttons) >= 20:
            raise ValueError(tr("Максимум 20 кнопок."))
        typ = parts[4]
        if typ not in {"url", "reaction", "subscription", "alert"}:
            raise ValueError(tr("Неверный тип."))
        b = {"id": secrets.token_hex(4), "type": typ, "row": 1}
    else:
        raise ValueError(tr("Неизвестное действие."))
    await wizard_start(
        state,
        {"kind": "button", "scope": scope, "oid": oid, "button": b, "step": "label"},
    )
    await ui.edit(
        c,
        tr("Как назвать кнопку?\nНапример: «Наш канал», «❤️ Нравится», «Подробнее»."),
        ui.back(f"bw:panel:{scope}:{oid}"),
    )
    await c.answer()


@router.callback_query(F.data.startswith("templates:"))
async def template_list(c: CallbackQuery):
    cid = int(c.data.split(":")[1])
    ch = features_channels.template_channel(cid, c.from_user.id)
    rows = ui.button_grid(
        [
            ui.choice(
                ("⭐ " if ch["default_template_id"] == r["id"] else "")
                + r["name"][:24],
                f"tpl:{r['id']}:open",
            )
            for r in database.all_rows(
                "SELECT * FROM templates WHERE channel_id=? AND owner_id=?",
                (cid, c.from_user.id),
            )
        ],
        2,
    )
    rows += [
        [
            ui.choice(tr("➕ Новый шаблон"), f"tw:new:{cid}"),
            ui.choice(tr("Без шаблона"), f"tw:none:{cid}"),
        ],
        [ui.choice(tr("⬅️ Канал"), f"channel:open:{cid}")],
    ]
    await ui.edit(
        c,
        tr(
            "🧩 Шаблоны\nШаблон добавляет к постам текст, подпись и кнопки. Создайте один раз и используйте снова."
        ),
        ui.kb(rows),
    )
    await c.answer()


@router.callback_query(F.data.startswith("tpl:"))
async def template_action(c: CallbackQuery, state: FSMContext):
    _, tid, action = c.data.split(":")
    tid = int(tid)
    t = database.one(
        "SELECT * FROM templates WHERE id=? AND owner_id=?", (tid, c.from_user.id)
    )
    if not t:
        raise ValueError(tr("Шаблон не найден."))
    cid = t["channel_id"]
    if action == "default":
        database.execute(
            "UPDATE channels SET default_template_id=? WHERE id=?", (tid, cid)
        )
        await c.answer(tr("Используется по умолчанию."))
        return
    if action == "delete":
        with database.atomic():
            database.db.execute("DELETE FROM templates WHERE id=?", (tid,))
            database.db.execute(
                "UPDATE channels SET default_template_id=NULL WHERE default_template_id=?",
                (tid,),
            )
            database.db.execute(
                "UPDATE post_targets SET template_id=-1 WHERE template_id=?", (tid,)
            )
        await ui.edit(c, tr("Шаблон удалён."), ui.back(f"templates:{cid}"))
        await c.answer()
        return
    if action == "buttons_json":
        await button_panel(c, "t", tid)
        await c.answer()
        return
    if action == "open":
        rows = ui.button_grid(
            [
                ui.choice(label, f"tpl:{tid}:{field}")
                for field, label in [
                    ("before_html", tr("⬆️ Текст до поста")),
                    ("after_html", tr("⬇️ Текст после поста")),
                    ("signature_html", tr("✍️ Подпись")),
                    ("buttons_json", tr("🔘 Кнопки")),
                    ("name", tr("✏️ Название")),
                    ("default", tr("⭐ По умолчанию")),
                ]
            ],
            2,
        )
        rows.append(
            [
                ui.choice(tr("🗑 Удалить"), f"tpl:{tid}:delete"),
                ui.choice(tr("⬅️ Шаблоны"), f"templates:{cid}"),
            ]
        )
        summaries = []
        for key, label in [
            ("before_html", tr("До")),
            ("after_html", tr("После")),
            ("signature_html", tr("Подпись")),
        ]:
            plain, _ = content.parse_template_html(t[key])
            summaries.append(label + ": " + html.escape(plain[:180] or tr("не задано")))
        await ui.edit(
            c, "🧩 " + html.escape(t["name"]) + "\n" + "\n".join(summaries), ui.kb(rows)
        )
        await c.answer()
        return
    if action not in {"name", "before_html", "after_html", "signature_html"}:
        raise ValueError(tr("Неизвестное поле."))
    await wizard_start(
        state, {"kind": "template", "tid": tid, "field": action, "step": "value"}
    )
    rows = (
        [
            [
                ui.choice(tr("🧹 Очистить поле"), f"tw:clear:{tid}:{action}"),
                ui.choice(tr("⬅️ Назад"), f"tpl:{tid}:open"),
            ]
        ]
        if action != "name"
        else [[ui.choice(tr("⬅️ Назад"), f"tpl:{tid}:open")]]
    )
    await ui.edit(
        c,
        tr("Напишите новое название.")
        if action == "name"
        else tr(
            "Отправьте текст так, как он должен выглядеть.\nМожно использовать жирный текст, курсив и ссылки средствами Telegram. Писать HTML не нужно."
        ),
        ui.kb(rows),
    )
    await c.answer()


@router.callback_query(F.data.startswith("tw:"))
async def template_wizard(c: CallbackQuery, state: FSMContext):
    p = c.data.split(":")
    action = p[1]
    oid = int(p[2])
    if action in {"new", "none"}:
        features_channels.template_channel(oid, c.from_user.id)
        if action == "none":
            database.execute(
                "UPDATE channels SET default_template_id=NULL WHERE id=?", (oid,)
            )
            await c.answer(tr("Шаблон по умолчанию отключён."))
            return
        await wizard_start(state, {"kind": "template_new", "cid": oid, "step": "name"})
        await ui.edit(
            c,
            tr("Как назвать шаблон? Например: «Обычные новости»."),
            ui.back(f"templates:{oid}"),
        )
        await c.answer()
        return
    if action == "clear":
        field = p[3]
        if field not in {"before_html", "after_html", "signature_html"}:
            raise ValueError(tr("Недопустимое поле."))
        if not database.one(
            "SELECT 1 FROM templates WHERE id=? AND owner_id=?", (oid, c.from_user.id)
        ):
            raise ValueError(tr("Шаблон не найден."))
        database.execute(f"UPDATE templates SET {field}='' WHERE id=?", (oid,))
        await state.clear()
        await ui.edit(c, tr("Поле очищено."), ui.back(f"tpl:{oid}:open"))
        await c.answer()
        return
    raise ValueError(tr("Неизвестное действие."))


def formatted_input(m):
    # aiogram converts incoming entities to valid Telegram HTML; validate our supported subset.
    value = m.html_text if m.text is not None else m.html_caption
    value = (
        (value or "")
        .replace('<span class="tg-spoiler">', "<tg-spoiler>")
        .replace("</span>", "</tg-spoiler>")
    )
    content.parse_template_html(value)
    return value


async def guided_value(m, state, bot, raw, uid):
    d = await state.get_data()
    kind = d.get("kind")
    step = d.get("step")
    raw = raw.strip()
    if kind == "condition":
        await features_conditions.condition_value(m, state, bot, raw, uid, d)
        return
    if (
        kind
        in {
            "admin_user",
            "admin_change",
            "refdays",
            "refcondition",
            "defaultdays",
            "promo",
        }
        and uid != ADMIN_ID
    ):
        raise ValueError(tr("Нет доступа."))
    if kind == "button":
        b = d["button"]
        if step == "label":
            if not 1 <= len(raw) <= 50:
                raise ValueError(tr("Название: 1–50 символов."))
            b["text"] = raw
            await state.update_data(button=b)
            if b["type"] == "reaction":
                await ask_color(m, state, uid)
                return
            await state.update_data(
                step="target" if b["type"] == "subscription" else "value"
            )
            await m.answer(
                tr("На какой канал нужна подписка? Отправьте @username.")
                if b["type"] == "subscription"
                else (
                    tr(
                        "Пришлите любую ссылку https://… (включая частную ссылку Telegram)."
                    )
                    if b["type"] == "url"
                    else tr("Что показать при нажатии? До 200 символов.")
                )
            )
            return
        if step == "target":
            chat = await bot.get_chat(int(raw) if raw.lstrip("-").isdigit() else raw)
            me = await bot.get_me()
            member = await bot.get_chat_member(chat.id, me.id)
            if member.status not in {
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            }:
                raise ValueError(
                    tr(
                        "Для проверки подписки добавьте бота администратором этого канала."
                    )
                )
            b.update(
                chat=str(chat.id),
                chat_label="@" + chat.username if chat.username else chat.title,
            )
            await state.update_data(button=b, step="value")
            await m.answer(tr("Какой текст открыть после подписки? До 200 символов."))
            return
        if step == "value":
            if b["type"] == "url":
                if raw.startswith("@"):
                    raw = "https://t.me/" + raw[1:]
                if not content.valid_url(raw):
                    raise ValueError(tr("Нужна корректная ссылка https://…"))
                b["url"] = raw
            else:
                if not 1 <= len(raw) <= 200:
                    raise ValueError(tr("Текст: 1–200 символов."))
                b["alert"] = raw
            await state.update_data(button=b)
            await ask_color(m, state, uid)
            return
        raise ValueError(tr("Выберите расположение кнопками ниже."))
    if kind == "template_new":
        if not 1 <= len(raw) <= 80:
            raise ValueError(tr("Название: 1–80 символов."))
        features_channels.template_channel(d["cid"], uid)
        tid = database.execute(
            "INSERT INTO templates(channel_id,owner_id,name) VALUES(?,?,?)",
            (d["cid"], uid, raw),
        ).lastrowid
        await state.clear()
        await m.answer(
            tr("✅ Шаблон создан. Теперь добавьте текст или кнопки."),
            reply_markup=ui.back(f"tpl:{tid}:open"),
        )
        return
    if kind == "template":
        if not database.one(
            "SELECT 1 FROM templates WHERE id=? AND owner_id=?", (d["tid"], uid)
        ):
            raise ValueError(tr("Шаблон не найден."))
        field = d["field"]
        if field == "name":
            if not 1 <= len(raw) <= 80:
                raise ValueError(tr("Название: 1–80 символов."))
            value = raw
        else:
            value = formatted_input(m)
            if content.utf16len(content.parse_template_html(value)[0]) > 3000:
                raise ValueError(tr("Слишком длинный текст."))
        database.execute(
            f"UPDATE templates SET {field}=? WHERE id=?", (value, d["tid"])
        )
        await state.clear()
        await m.answer(
            tr("✅ Сохранено."), reply_markup=ui.back(f"tpl:{d['tid']}:open")
        )
        return
    if kind == "admin_user":
        text, markup = features_admin.admin_user_card(int(raw))
        await state.clear()
        await m.answer(text, reply_markup=markup)
        return
    if kind == "admin_change":
        value = end_of_day(raw, uid) if d["mode"] == "set" else int(raw)
        if d["mode"] != "set" and not 1 <= value <= 36500:
            raise ValueError(tr("Введите от 1 до 36500 дней."))
        with database.atomic():
            accounts.premium_change_tx(
                d["uid"], d["mode"], value, "admin:" + d["mode"], ADMIN_ID
            )
        text, markup = features_admin.admin_user_card(d["uid"])
        await state.clear()
        await m.answer(text, reply_markup=markup)
        return
    if kind in {"refdays", "defaultdays"}:
        n = int(raw)
        if not (0 if kind == "refdays" else 1) <= n <= 3650:
            raise ValueError(tr("Введите корректное число дней до 3650."))
        key = d["key"] if kind == "refdays" else "promo_default_days"
        database.execute("UPDATE app_settings SET value=? WHERE key=?", (str(n), key))
        await state.clear()
        await m.answer(
            tr("✅ Сохранено."),
            reply_markup=ui.back("aw:ref" if kind == "refdays" else "aw:promos"),
        )
        return
    if kind == "refcondition":
        if step == "channels":
            chats = [x.strip() for x in re.split(r"[\s,]+", raw) if x.strip()]
            me = await bot.get_me()
            if not chats or len(chats) > 10:
                raise ValueError(tr("Укажите от 1 до 10 каналов."))
            for name in chats:
                chat = await bot.get_chat(name)
                member = await bot.get_chat_member(chat.id, me.id)
                if member.status not in {
                    ChatMemberStatus.ADMINISTRATOR,
                    ChatMemberStatus.CREATOR,
                }:
                    raise ValueError(
                        tr("Бот должен быть администратором всех указанных каналов.")
                    )
            with database.atomic():
                database.db.execute(
                    "UPDATE app_settings SET value=? WHERE key='ref_subscription'",
                    (",".join(chats),),
                )
                database.db.execute(
                    "UPDATE app_settings SET value='subscription' WHERE key='ref_condition'"
                )
        else:
            if raw not in set(accounts.CONDITION_LABELS) - {"none"}:
                raise ValueError(tr("Выберите условие кнопкой."))
            if raw == "subscription":
                await state.update_data(step="channels")
                await m.answer(
                    tr(
                        "Отправьте @username обязательных каналов — каждый с новой строки."
                    )
                )
                return
            database.execute(
                "UPDATE app_settings SET value=? WHERE key='ref_condition'", (raw,)
            )
        await state.clear()
        text, markup = features_admin.ref_panel()
        await m.answer(text, reply_markup=markup)
        return
    if kind == "promo":
        p = d["promo"]
        if step == "code":
            code = raw.upper()
            if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
                raise ValueError(tr("Код: 3–32 латинские буквы, цифры, _ или -."))
            if database.one("SELECT 1 FROM promos WHERE code=?", (code,)):
                raise ValueError(tr("Код уже существует."))
            p["code"] = code
            step = "days"
        elif step == "days":
            n = int(raw)
            if not 1 <= n <= 36500:
                raise ValueError(tr("От 1 до 36500 дней."))
            p["days"] = n
            step = "uses"
        elif step == "uses":
            n = int(raw)
            if not 1 <= n <= 1000000:
                raise ValueError(tr("От 1 до 1000000 активаций."))
            p["max_uses"] = n
            step = "expiry"
        elif step == "expiry":
            expiry = None if raw == "none" else end_of_day(raw, uid)
            if expiry and timeutils.parse_dt(expiry) <= timeutils.now():
                raise ValueError(tr("Дата уже прошла."))
            p["expires_at"] = expiry
            step = "audience"
        elif step == "audience":
            if raw == "personal":
                await state.update_data(step="personal")
                await m.answer(tr("Отправьте Telegram ID получателя."))
                return
            if raw != "all":
                raise ValueError(tr("Выберите вариант кнопкой."))
            p["personal_id"] = None
            step = "condition"
        elif step == "personal":
            uid_value = int(raw)
            if uid_value <= 0:
                raise ValueError(tr("Нужен положительный Telegram ID."))
            p["personal_id"] = uid_value
            step = "condition"
        elif step == "condition":
            if raw not in accounts.CONDITION_LABELS:
                raise ValueError(tr("Выберите условие кнопкой."))
            if raw == "subscription" and not database.setting("ref_subscription"):
                raise ValueError(
                    tr(
                        "Сначала настройте обязательные каналы в разделе наград за приглашения."
                    )
                )
            p["condition"] = raw
            step = "confirm"
        else:
            raise ValueError(tr("Нажмите «Сохранить»."))
        await state.update_data(promo=p, step=step)
        await features_admin.promo_step(m, state)
        return
    raise ValueError(tr("Откройте нужную настройку заново."))


def end_of_day(raw, uid):
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return timeutils.iso(
                preferences.to_utc(uid, datetime.strptime(raw, fmt) + timedelta(days=1))
            )
        except ValueError:
            pass
    raise ValueError(tr("Введите дату ДД.ММ.ГГГГ, например 31.12.2026."))


@router.message(GuidedInput.value, F.text, ~F.text.startswith("/"))
async def guided_text(m: Message, state: FSMContext, bot: Bot):
    await guided_value(m, state, bot, m.text, m.from_user.id)
