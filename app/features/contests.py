"""Contest setup, public directory and private participation checks."""

import html
import json
import secrets
from datetime import timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton

from app import access, accounts, content, preferences, timeutils, ui
from app import database as db
from app.features import contest_form
from app.i18n import STATUS, tr
from services import contests as service
from services.telegram_links import chat_reference, subscription_link

router = Router(name="contests")


class Create(StatesGroup):
    input = State()


class Entry(StatesGroup):
    answer = State()


class Recover(StatesGroup):
    post = State()


STEPS = [
    "title",
    "post",
    "prize_kind",
    "prize",
    "winners",
    "mode",
    "subscriptions",
    "captcha",
    "quiz",
    "referrals",
    "start",
    "end",
    "review",
]
STEPS += [
    "prize_title",
    "post_style",
    "contact",
    "subscription_layout",
    "referral_target",
]
PROMPTS = {
    "prize_title": "Что разыгрываем? Опишите приз (до 100 символов).",
    "post_style": "Создайте свой пост или выберите готовый шаблон:",
    "contact": "Контакт для получения приза (до 150 символов). Отправьте - если организатор сам свяжется с победителями.",
    "subscription_layout": "Где разместить ссылки обязательных подписок?",
    "referral_target": "Куда приглашать друзей по личной ссылке?",
    "title": "Название конкурса (до 100 символов):",
    "post": "Отправьте пост конкурса: текст, фото или видео. Укажите приз в тексте. Кнопку «Участвовать» бот добавит сам.",
    "prize_kind": "Как выдать приз?",
    "prize": "Промокоды: по одному на строке. Для закрытого канала: @username или ID -100… (бот должен быть администратором). Для физического приза: инструкция и контакт организатора. Эти данные увидят только победители.",
    "winners": "Сколько победителей? От 1 до 20. Промокодов должно хватить каждому.",
    "mode": "Как выбрать победителей? В режиме «Больше приглашений» равенство решает более ранняя регистрация. В режиме «Больше шансов» каждый приглашённый друг даёт дополнительный шанс.",
    "subscriptions": "Укажите каналы и группы для обязательной подписки: @username или ID, по одному на строке (до 10). Бот должен быть администратором. Или отправьте - без подписок.",
    "captcha": "Добавить проверку на бота?",
    "quiz": "Квиз: первая строка — вопрос, вторая — правильный ответ. Или - без квиза.",
    "referrals": "Сколько друзей обязательно пригласить для участия? 0 — приглашения необязательны. Засчитываются друзья, выполнившие подписки, капчу и квиз.",
    "start": "Когда начать? Выберите кнопку или введите дату и время: 25.12.2026 18:30.",
    "end": "Когда завершить? Выберите срок от начала или введите дату и время: 26.12.2026 18:30.",
}


PROMPTS.update(contest_form.PROMPTS)
STEPS.extend(contest_form.PROMPTS)


async def prompt(message, state, step):
    await state.set_state(Create.input)
    await state.update_data(step=step)
    choices = {
        "post_style": [
            (tr("✨ Готовый шаблон"), "template"),
            (tr("✍️ Свой пост"), "custom"),
        ],
        "contact": [(tr("Свяжемся сами"), "-")],
        "subscription_layout": [
            (tr("В тексте поста"), "text"),
            (tr("Кнопками под постом"), "buttons"),
        ],
        "referral_target": [
            (tr("Участвовать в конкурсе / розыгрыше"), "participants"),
            (tr("Подписаться на канал публикации"), "channel"),
        ],
        "winners": [(str(n), str(n)) for n in (1, 3, 5, 10)],
        "referrals": [(tr("Нет"), "0"), ("1", "1"), ("3", "3"), ("5", "5")],
        "quiz": [(tr("Без квиза"), "-")],
        "prize_kind": [
            (tr("Промокоды"), "promo"),
            (tr("Закрытый канал"), "invite"),
            (tr("Приз выдаёт организатор"), "physical"),
        ],
        "mode": [
            (tr("Случайно"), "random"),
            (tr("С задачей"), "task"),
            (tr("Больше шансов за друзей"), "weighted"),
            (tr("Больше приглашений"), "ranking"),
        ],
        "captcha": [(tr("Да"), "1"), (tr("Нет"), "0")],
        "start": [
            (tr("Сейчас"), "now"),
            (tr("Через час"), "hour"),
            (tr("Завтра"), "tomorrow"),
        ],
        "end": [
            (tr("Через час"), "hour"),
            (tr("Через день"), "day"),
            (tr("Через неделю"), "week"),
        ],
    }
    rows = [
        [ui.choice(label, f"contest:setup:{step}:{value}")]
        for label, value in choices.get(step, [])
    ]
    rows.append([ui.choice(tr("Отменить"), "menu:contests")])
    text = tr(PROMPTS[step])
    data = await state.get_data()
    if data.get("form"):
        rows[-1] = [ui.choice("⬅️ " + tr("Назад"), "contest:panel")]
    if data.get("launch_v4") and step == "post_style":
        label = (
            tr("✨ Шаблон 1 — Конкурс")
            if data["giveaway_type"] == "contest"
            else tr("✨ Шаблон 2 — Розыгрыш")
        )
        rows[0] = [ui.choice(label, "contest:setup:post_style:template")]
    if data.get("quick") and not data.get("launch_v4") and not data.get("form"):
        if step == "post":
            text = tr(
                "1/2 · Отправьте пост розыгрыша\nНапишите, что разыгрываете. Можно приложить фото или видео. Кнопку «Участвовать» добавим сами."
            )
        elif step == "end":
            text = tr(
                "2/2 · Когда подвести итоги?\nВыберите срок или отправьте дату и время. Например: 25.12.2026 18:30."
            )
        elif step == "prize":
            kind = data.get("pending_prize_kind", data["prize_kind"])
            text = tr(
                {
                    "promo": "Отправьте промокоды — каждый с новой строки. На каждого победителя нужен отдельный код. Участники не увидят коды до победы.",
                    "invite": "Отправьте @username или ID закрытого канала. Бот должен иметь право приглашать участников.",
                    "physical": "Что написать победителю? Укажите, как получить приз и с кем связаться.",
                }[kind]
            )
        if data.get("post") and data.get("end"):
            rows[-1] = [ui.choice(tr("⬅️ К настройкам розыгрыша"), "contest:panel")]
    if step in {"post", "prize_title", "prize_description", "quiz"}:
        text += "\n\n" + tr(
            "Выделите текст в Telegram и выберите формат: жирный, курсив, ссылка, цитата, спойлер и другие."
        )
    if step in {"start", "end"}:
        text += "\n" + preferences.display(message.chat.id, timeutils.now())
    await ui.answer(message, text, reply_markup=ui.kb(rows))


def draft_row(data, uid):
    return {
        "id": 0,
        "owner_id": uid,
        "post_json": json.dumps(
            dict(data["post"], field_entities=data.get("field_entities", {}))
        ),
        "prize_title": data.get("prize_title", ""),
        "title": data["title"],
        "winner_count": data["winners"],
        "ends_at": data["end"],
        "subscription_layout": data.get("subscription_layout", "buttons"),
        "subscriptions_json": json.dumps(data["subscriptions"]),
        "status": "scheduled",
    }


def generated_post(data):
    if data.get("form"):
        return contest_form.generated(data)
    title = (
        tr("🏆 КОНКУРС ДРУЗЕЙ")
        if data["giveaway_type"] == "contest"
        else tr("🎉 БОЛЬШОЙ РОЗЫГРЫШ")
    )
    details = (
        tr(
            "Приглашайте друзей по своей ссылке. Побеждают участники с наибольшим числом приглашений!"
        )
        if data["giveaway_type"] == "contest"
        else tr(
            "Победителей выберем случайно. Каждый приглашённый друг добавляет один шанс!"
        )
    )
    raw = (
        "<b>"
        + title
        + "</b>\n━━━━━━━━━━━━━━\n"
        + details
        + "\n\n"
        + tr("Подпишитесь на каналы и нажмите «Участвовать». Удачи! 🎁")
    )
    text, entities = content.parse_template_html(raw)
    return {"content_type": "text", "text": text, "entities_json": entities}


def save_draft(uid, data):
    db.execute(
        "INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"contest_draft:{uid}", json.dumps(data, ensure_ascii=False)),
    )


async def draft_panel(message, state, callback=False):
    data = await state.get_data()
    if data.get("form"):
        return await contest_form.panel(message, state, callback)
    if not data.get("quick") or not data.get("post") or not data.get("end"):
        raise ValueError(tr("Сначала заполните настройки конкурса."))
    data.pop("pending_prize_kind", None)
    data["step"] = "review"
    await state.set_state(Create.input)
    await state.set_data(data)
    uid = message.from_user.id if callback else message.chat.id
    save_draft(uid, data)
    channel = db.one("SELECT title FROM channels WHERE id=?", (data["channel_id"],))
    start = (
        timeutils.now() + timedelta(seconds=15)
        if data["start_now"]
        else timeutils.parse_dt(data["start"])
    )
    end = (
        start + timedelta(seconds=data["end_duration"])
        if data.get("end_duration")
        else timeutils.parse_dt(data["end"])
    )
    subscriptions = ", ".join(
        html.escape(ch["title"]) for ch in data["subscriptions"]
    ) or tr("Только нажать «Участвовать»")
    prize = tr(
        {
            "physical": "Приз выдаёт организатор",
            "promo": "Промокоды в личные сообщения",
            "invite": "Приглашение в закрытый канал",
        }[data["prize_kind"]]
    )
    text = tr(
        "🎉 Розыгрыш готов к запуску\nПроверьте настройки. Любой пункт можно изменить."
    )
    text += "\n\n" + html.escape(data["title"]) + "\n" + html.escape(channel["title"])
    text += tr("\nНачало: ") + (
        tr("Сразу после запуска")
        if data["start_now"]
        else preferences.display(uid, start)
    )
    text += tr("\nКонец: ") + preferences.display(uid, end)
    text += tr(
        "\nПобедителей: {count} · {mode}\nУчастие: {conditions}\nПриз: {prize}",
        count=data["winners"],
        mode=STATUS[data["mode"]],
        conditions=subscriptions,
        prize=prize,
    )
    if data.get("launch_v4"):
        text += "\n🎁 " + html.escape(data["prize_title"])
        text += "\n" + (
            tr("Конкурс") if data["giveaway_type"] == "contest" else tr("Розыгрыш")
        )
        text += "\n" + (
            html.escape(data.get("claim_contact") or "")
            or tr("Ожидайте, скоро с вами свяжутся для вручения призов.")
        )
    extras = []
    if data["captcha"]:
        extras.append(tr("Проверка на бота"))
    if data["quiz"]:
        extras.append(tr("Вопрос участникам"))
    if data["referrals"]:
        extras.append(tr("Пригласить друзей: {count}", count=data["referrals"]))
    if extras:
        text += "\n" + ", ".join(extras)
    rows = [
        [ui.choice(tr("🚀 Запустить розыгрыш"), "contest:create")],
        [ui.choice(tr("👁 Предпросмотр"), "contest:preview")],
        [
            ui.choice(tr("🏆 Победители"), "contest:edit:winners"),
            ui.choice(tr("🕒 Дата итогов"), "contest:edit:end"),
        ],
        [
            ui.choice(tr("📢 Подписки"), "contest:subscriptions"),
            ui.choice(tr("🎲 Как выбрать"), "contest:edit:mode"),
        ],
        [
            ui.choice(tr("🎁 Выдача приза"), "contest:edit:prize_kind"),
            ui.choice(tr("📅 Начало"), "contest:edit:start"),
        ],
        [
            ui.choice(tr("✏️ Изменить пост"), "contest:edit:post"),
            ui.choice(tr("⚙️ Дополнительно"), "contest:extra"),
        ],
        [
            ui.choice(tr("Сохранить и выйти"), "menu:contests"),
            ui.choice(tr("Удалить черновик"), "contest:discard"),
        ],
    ]
    if data.get("launch_v4"):
        rows[3][1] = ui.choice(tr("📞 Контакт победителям"), "contest:edit:contact")
        rows.insert(
            -1,
            [
                ui.choice(tr("🎁 Название приза"), "contest:edit:prize_title"),
                ui.choice(tr("🔗 Вид ссылок"), "contest:edit:subscription_layout"),
            ],
        )
    rows.insert(1, [ui.choice(tr("🧮 Математическая капча"), "contest:edit:captcha")])
    if callback:
        await ui.edit(message, text, ui.kb(rows))
    else:
        await ui.answer(message, text, reply_markup=ui.kb(rows))


@router.callback_query(F.data == "contest:panel")
async def panel(c, state):
    await draft_panel(c, state, True)
    await c.answer()


@router.callback_query(F.data == "contest:resume")
async def resume(c, state):
    raw = db.setting(f"contest_draft:{c.from_user.id}")
    if not raw:
        raise ValueError(tr("Сначала заполните настройки конкурса."))
    await state.set_data(json.loads(raw))
    if (await state.get_data()).get("selecting_channels"):
        await contest_form.channels(c, state)
        await c.answer()
        return
    await draft_panel(c, state, True)
    await c.answer()


@router.callback_query(F.data == "contest:discard")
async def discard(c, state):
    await state.clear()
    db.execute(
        "DELETE FROM app_settings WHERE key=?", (f"contest_draft:{c.from_user.id}",)
    )
    await menu(c, state)


@router.callback_query(F.data.startswith("contest:edit:"))
async def edit_field(c, state):
    data = await state.get_data()
    field = c.data.split(":")[2]
    if data.get("launch_v4") and field == "mode":
        raise ValueError(
            tr(
                "Тип выбран при создании: конкурс — рейтинг, розыгрыш — случайный выбор с шансами за друзей."
            )
        )
    if not data.get("quick") or field not in PROMPTS:
        raise ValueError(tr("Этот шаг устарел. Откройте настройку заново."))
    await prompt(c.message, state, field)
    await c.answer()


@router.callback_query(F.data == "contest:preview")
async def preview(c, state, bot):
    data = await state.get_data()
    if not data.get("quick") or not data.get("post"):
        raise ValueError(tr("Сначала заполните настройки конкурса."))
    sent = await content.send_content(
        bot,
        c.from_user.id,
        service.publication(draft_row(data, c.from_user.id))
        if data.get("launch_v4") or data.get("form")
        else data["post"],
        service.public_markup(draft_row(data, c.from_user.id), 0)
        if data.get("launch_v4") or data.get("form")
        else ui.kb([[ui.choice(tr("🎉 Участвовать"), "demo")]]),
    )
    await ui.track_controls(bot, c.from_user.id, "contest_preview", sent)
    await draft_panel(c.message, state)
    await c.answer()


@router.callback_query(F.data.startswith("contest:section:"))
async def form_section(c, state):
    await contest_form.section(c, state)


@router.callback_query(F.data == "contest:extra")
async def extra(c, state):
    data = await state.get_data()
    if not data.get("quick"):
        raise ValueError(tr("Сначала заполните настройки конкурса."))
    await ui.edit(
        c,
        tr(
            "Дополнительные настройки\nДля обычного розыгрыша здесь ничего менять не нужно."
        ),
        ui.kb(
            [
                [ui.choice(tr("Проверка на бота"), "contest:edit:captcha")],
                [
                    ui.choice(
                        tr("Куда приглашать друзей"), "contest:edit:referral_target"
                    )
                ],
                [ui.choice(tr("Вопрос участникам"), "contest:edit:quiz")],
                [
                    ui.choice(
                        tr("Обязательное приглашение друзей"), "contest:edit:referrals"
                    )
                ],
                [ui.choice(tr("Инструкция победителю"), "contest:edit:prize")],
                [ui.choice(tr("Название для списка"), "contest:edit:title")],
                [ui.choice(tr("⬅️ К настройкам розыгрыша"), "contest:panel")],
            ]
        ),
    )
    await c.answer()


async def subscription_panel(c, state):
    data = await state.get_data()
    if not data.get("quick"):
        raise ValueError(tr("Сначала заполните настройки конкурса."))
    selected = {ch["chat_id"] for ch in data["subscriptions"]}
    rows = [
        [
            ui.choice(
                ("✅ " if ch["telegram_chat_id"] in selected else "▫️ ")
                + ch["title"][:40],
                f"contest:subtoggle:{ch['id']}",
            )
        ]
        for ch in accounts.eligible_channels(c.from_user.id)
    ]
    rows += [
        [ui.choice(tr("Другой канал или группа"), "contest:edit:subscriptions")],
        [ui.choice(tr("Без обязательной подписки"), "contest:subclear")],
        [ui.choice(tr("✅ Готово"), "contest:panel")],
    ]
    await ui.edit(
        c,
        tr(
            "Выберите обязательные подписки\nНажмите на каналы и группы. Повторное нажатие убирает подписку. Бот проверит их перед участием и перед итогами."
        ),
        ui.kb(rows),
    )


@router.callback_query(F.data == "contest:subscriptions")
async def subscriptions(c, state):
    await subscription_panel(c, state)
    await c.answer()


@router.callback_query(
    F.data.startswith("contest:subtoggle:") | (F.data == "contest:subclear")
)
async def subscription_choice(c, state, bot):
    data = await state.get_data()
    if not data.get("quick"):
        raise ValueError(tr("Сначала заполните настройки конкурса."))
    selected = data["subscriptions"]
    if c.data == "contest:subclear":
        selected = []
    else:
        cid = int(c.data.split(":")[2])
        if not accounts.channel_allowed(c.from_user.id, cid):
            raise ValueError(tr("Канал недоступен."))
        ch = db.one("SELECT * FROM channels WHERE id=?", (cid,))
        if any(x["chat_id"] == ch["telegram_chat_id"] for x in selected):
            selected = [x for x in selected if x["chat_id"] != ch["telegram_chat_id"]]
        else:
            if len(selected) >= 10:
                raise ValueError(tr("Можно указать не больше 10 подписок."))
            me = await bot.get_me()
            member = await bot.get_chat_member(ch["telegram_chat_id"], me.id)
            if member.status not in {"creator", "administrator"}:
                raise ValueError(tr("Добавьте бота администратором указанного канала."))
            item = {"chat_id": ch["telegram_chat_id"], "title": ch["title"]}
            item["url"] = await subscription_link(bot, item)
            selected = selected + [item]
    data["subscriptions"] = selected
    await state.set_data(data)
    save_draft(c.from_user.id, data)
    await subscription_panel(c, state)
    await c.answer()


@router.callback_query(F.data == "menu:contests")
async def menu(c, state):
    data = await state.get_data()
    if data.get("form") or data.get("selecting_channels"):
        save_draft(c.from_user.id, data)
    await state.clear()
    markup = ui.kb(
        [[ui.choice(tr("Продолжить черновик"), "contest:resume")]]
        if db.setting(f"contest_draft:{c.from_user.id}")
        else []
    )
    await ui.edit(
        c,
        tr("🏆 Конкурсы\nСоздайте свой конкурс или участвуйте в активных."),
        ui.kb(
            list(markup.inline_keyboard)
            + [
                [
                    ui.choice(tr("➕ Создать КР"), "contest:new"),
                    ui.choice(tr("Мои КР"), "contest:mine:0"),
                ],
                [ui.choice(tr("🎉 Активные конкурсы"), "contest:list:0")],
                [ui.choice(tr("Главное меню"), "menu:main")],
            ]
        ),
    )
    await c.answer()


@router.callback_query(
    F.data.startswith("contest:list:") | F.data.startswith("contest:mine:")
)
async def directory(c):
    mine = c.data.split(":")[1] == "mine"
    page = max(0, int(c.data.split(":")[2]))
    offset = page * 10
    query = (
        "SELECT * FROM contests WHERE owner_id=? AND status IN ('active','scheduled','publishing','uncertain') AND julianday(ends_at)>julianday('now')"
        if mine
        else "SELECT * FROM contests WHERE status='active' AND julianday(ends_at)>julianday(?)"
    )
    rows = db.all_rows(
        query + " ORDER BY id DESC LIMIT 11 OFFSET ?",
        (c.from_user.id if mine else timeutils.iso(), offset),
    )
    buttons = [
        [
            ui.choice(
                row["title"][:45] + (" · " + STATUS[row["status"]] if mine else ""),
                f"contest:{'manage' if mine else 'view'}:{row['id']}",
            )
        ]
        for row in rows[:10]
    ]
    navigation = []
    if page:
        navigation.append(
            ui.choice("←", f"contest:{'mine' if mine else 'list'}:{page - 1}")
        )
    if len(rows) > 10:
        navigation.append(
            ui.choice("→", f"contest:{'mine' if mine else 'list'}:{page + 1}")
        )
    if navigation:
        buttons.append(navigation)
    buttons.append([ui.choice(tr("Назад"), "menu:contests")])
    await ui.edit(
        c,
        tr("Ваши конкурсы") if mine else tr("Активные конкурсы: выберите для участия."),
        ui.kb(buttons),
    )
    await c.answer()


@router.callback_query(F.data == "contest:new")
async def new(c, state):
    await contest_form.start(c, state)


@router.callback_query(F.data.startswith("contest:select:"))
async def select_channel(c, state):
    await contest_form.toggle(c, state)


@router.callback_query(F.data == "contest:selected")
async def selected_channels(c, state):
    await contest_form.selected(c, state)


async def legacy_new(c, state):
    await state.clear()
    await ui.edit(
        c,
        tr(
            "Выберите тип: конкурс — больше приглашений; розыгрыш — случайный выбор с шансами за друзей."
        ),
        ui.kb(
            [
                [
                    ui.choice(tr("🏆 Конкурс"), "contest:type:contest"),
                    ui.choice(tr("🎉 Розыгрыш"), "contest:type:raffle"),
                ],
                [ui.choice(tr("Назад"), "menu:contests")],
            ]
        ),
    )
    await c.answer()


@router.callback_query(F.data.startswith("contest:type:"))
async def choose_type(c, state):
    kind = c.data.split(":")[2]
    if kind not in {"contest", "raffle"}:
        raise ValueError(tr("Неизвестное действие."))
    await state.set_data({"giveaway_type": kind, "launch_v4": True})
    rows = [
        [ui.choice(ch["title"][:45], f"contest:target:{ch['id']}")]
        for ch in accounts.eligible_channels(c.from_user.id)
    ]
    await ui.edit(
        c,
        tr(
            "Где опубликовать конкурс? Если список пуст, сначала добавьте канал или группу."
        ),
        ui.kb(rows + [[ui.choice(tr("Назад"), "menu:contests")]]),
    )
    await c.answer()


@router.callback_query(F.data.startswith("contest:target:"))
async def target(c, state, bot=None):
    cid = int(c.data.split(":")[2])
    if not accounts.channel_allowed(c.from_user.id, cid):
        raise ValueError(tr("Канал недоступен."))
    previous = await state.get_data()
    launch = previous.get("launch_v4", False)
    kind = previous.get("giveaway_type", "raffle")
    subscriptions = []
    if launch:
        ch = db.one("SELECT * FROM channels WHERE id=?", (cid,))
        item = {"chat_id": ch["telegram_chat_id"], "title": ch["title"]}
        item["url"] = await subscription_link(bot, item)
        subscriptions = [item]
    await state.set_data(
        {
            "channel_id": cid,
            "giveaway_type": kind,
            "launch_v4": launch,
            "prize_title": "",
            "claim_contact": "",
            "subscription_layout": "buttons",
            "referral_target": "participants",
            "creation_token": secrets.token_hex(16),
            "quick": True,
            "prize_kind": "physical",
            "prize": {
                "value": tr(
                    "Организатор свяжется с вами и расскажет, как получить приз."
                )
            },
            "winners": 1,
            "mode": ("ranking" if kind == "contest" else "weighted")
            if launch
            else "random",
            "subscriptions": subscriptions,
            "captcha": 0,
            "quiz": None,
            "referrals": 0,
            "start_now": True,
            "start": timeutils.iso(timeutils.now() + timedelta(seconds=15)),
        }
    )
    await prompt(c.message, state, "prize_title" if launch else "post")
    await c.answer()


async def accept(message, state, bot, value, payload=None):
    data = await state.get_data()
    field = data.get("step")
    if field in {"prize_title", "title", "quiz"}:
        raw = value
        value = value.strip()
        entities = content.input_entities(message, raw, value)
        if field == "quiz":
            entities = content.slice_entities(
                entities, 0, content.utf16len(value.split("\n")[0])
            )
        fields = dict(data.get("field_entities", {}))
        fields[field] = entities
        data["field_entities"] = fields
    if data.get("form") and await contest_form.accept(
        message, state, bot, value, payload
    ):
        return
    step = data.get("step")
    uid = message.chat.id
    if data.get("quick") and step == "review":
        await draft_panel(message, state)
        return
    if step not in STEPS or step == "review":
        raise ValueError(tr("Начните создание конкурса заново."))
    value = value.strip()
    if step == "prize_title":
        if not 1 <= len(value) <= 100:
            raise ValueError(tr("Название должно содержать от 1 до 100 символов."))
        data["prize_title"] = value
        data["title"] = value
    elif step == "post_style":
        if value not in {"template", "custom"}:
            raise ValueError(tr("Выберите вариант кнопкой."))
        if value == "template":
            data["custom_post"] = False
            data["post"] = generated_post(data)
        else:
            await state.set_data(data)
            await prompt(message, state, "post")
            return
    elif step == "contact":
        if len(value) > 150 or not value:
            raise ValueError(tr("Контакт: до 150 символов, или - без контакта."))
        data["claim_contact"] = "" if value == "-" else value
        if data["prize_kind"] == "physical":
            data["prize"] = {
                "value": data["claim_contact"]
                or tr("Ожидайте, скоро с вами свяжутся для вручения призов.")
            }
    elif step == "subscription_layout":
        if value not in {"text", "buttons"}:
            raise ValueError(tr("Выберите вариант кнопкой."))
        data[step] = value
    elif step == "referral_target":
        if value not in {"participants", "channel"}:
            raise ValueError(tr("Выберите вариант кнопкой."))
        if value == "channel":
            target_chat = db.one(
                "SELECT telegram_chat_id FROM channels WHERE id=?",
                (data["channel_id"],),
            )
            me = await bot.get_me()
            member = await bot.get_chat_member(target_chat[0], me.id)
            if not getattr(member, "can_invite_users", False):
                raise ValueError(tr("Дайте боту право приглашать пользователей."))
        data[step] = value
    elif step == "title":
        if not 1 <= len(value) <= 100:
            raise ValueError(tr("Название должно содержать от 1 до 100 символов."))
        data["title"] = value
    elif step == "post":
        if not payload or payload["content_type"] not in {
            "text",
            "photo",
            "video",
            "animation",
        }:
            raise ValueError(tr("Отправьте один текст, фото или видео без альбома."))
        data["post"] = payload
        data["custom_post"] = True
        if data.get("quick") and not data.get("title"):
            data["title"] = " ".join((payload.get("text") or "").split())[:80] or tr(
                "Розыгрыш"
            )
    elif step == "prize_kind":
        if value not in {"promo", "invite", "physical"}:
            raise ValueError(tr("Выберите вид приза кнопкой."))
        if data.get("quick"):
            await state.update_data(pending_prize_kind=value)
            await prompt(message, state, "prize")
            return
        data[step] = value
    elif step == "prize":
        if not value or len(value) > 8000:
            raise ValueError(tr("Отправьте данные приза, не больше 8000 символов."))
        kind = data.get("pending_prize_kind", data["prize_kind"])
        if kind == "promo":
            codes = [v.strip() for v in value.splitlines() if v.strip()]
            if (
                not codes
                or len(set(codes)) != len(codes)
                or any(len(v) > 1000 for v in codes)
            ):
                raise ValueError(
                    tr("Промокоды должны быть уникальными, до 1000 символов каждый.")
                )
            data[step] = {"codes": codes}
            if data.get("quick") and len(codes) < data["winners"] * data.get(
                "prize_count", 1
            ):
                raise ValueError(
                    tr("Промокодов меньше, чем победителей. Укажите меньшее число.")
                )
        elif kind == "invite":
            chat = await bot.get_chat(chat_reference(value))
            if not await access.owner_and_bot_ok(bot, chat.id, uid):
                raise ValueError(
                    tr("Вы и бот должны быть администраторами канала для выдачи приза.")
                )
            me = await bot.get_me()
            member = await bot.get_chat_member(chat.id, me.id)
            if not getattr(member, "can_invite_users", False):
                raise ValueError(tr("Дайте боту право приглашать пользователей."))
            data[step] = {"value": chat.id}
        else:
            if len(value) > 1000:
                raise ValueError(tr("Инструкция должна быть не длиннее 1000 символов."))
            data[step] = {"value": value}
        data["prize_kind"] = kind
        data.pop("pending_prize_kind", None)
    elif step == "winners":
        if not value.isdigit() or not 1 <= int(value) <= 20:
            raise ValueError(tr("Укажите число от 1 до 20."))
        if data["prize_kind"] == "promo" and len(data["prize"]["codes"]) < int(
            value
        ) * data.get("prize_count", 1):
            raise ValueError(
                tr("Промокодов меньше, чем победителей. Укажите меньшее число.")
            )
        data[step] = int(value)
    elif step == "mode":
        if data.get("launch_v4"):
            raise ValueError(
                tr(
                    "Тип выбран при создании: конкурс — рейтинг, розыгрыш — случайный выбор с шансами за друзей."
                )
            )
        if value not in {"random", "weighted", "ranking"}:
            raise ValueError(tr("Выберите способ определения победителей кнопкой."))
        data[step] = value
        if data.get("form"):
            data["giveaway_type"] = "contest" if value == "ranking" else "raffle"
            data["quiz"] = None
    elif step == "subscriptions":
        chats = []
        if value != "-":
            refs = value.split()
            if len(refs) > 10:
                raise ValueError(tr("Можно указать не больше 10 подписок."))
            for ref in refs:
                chat = await bot.get_chat(chat_reference(ref))
                me = await bot.get_me()
                member = await bot.get_chat_member(chat.id, me.id)
                if chat.type not in {
                    "channel",
                    "group",
                    "supergroup",
                } or member.status not in {"creator", "administrator"}:
                    raise ValueError(
                        tr(
                            "Добавьте бота администратором во все каналы и группы условий."
                        )
                    )
                item = {"chat_id": chat.id, "title": chat.title}
                item["url"] = await subscription_link(bot, item)
                if not any(ch["chat_id"] == chat.id for ch in chats):
                    chats.append(item)
        if data.get("quick") and value != "-":
            combined = {ch["chat_id"]: ch for ch in data["subscriptions"] + chats}
            if len(combined) > 10:
                raise ValueError(tr("Можно указать не больше 10 подписок."))
            chats = list(combined.values())
        data[step] = chats
    elif step == "captcha":
        if value not in {"0", "1"}:
            raise ValueError(tr("Выберите ответ кнопкой."))
        data[step] = int(value)
    elif step == "quiz":
        parts = value.splitlines()
        if value != "-" and (
            len(parts) != 2 or not all(p.strip() for p in parts) or len(value) > 500
        ):
            raise ValueError(
                tr(
                    "Отправьте вопрос и ответ двумя строками, до 500 символов. Или - без квиза."
                )
            )
        data[step] = (
            None
            if value == "-"
            else {"question": parts[0], "hash": service.digest(parts[1])}
        )
    elif step == "referrals":
        if not value.isdigit() or not 0 <= int(value) <= 100:
            raise ValueError(tr("Укажите число от 0 до 100."))
        data[step] = int(value)
    elif step == "start":
        dt = (
            timeutils.now()
            + timedelta(seconds={"now": 15, "hour": 3600, "tomorrow": 86400}[value])
            if value in {"now", "hour", "tomorrow"}
            else preferences.parse_local(uid, value)
        )
        if not timeutils.now() < dt < timeutils.now() + timedelta(days=365):
            raise ValueError(tr("Начало должно быть в будущем, в течение года."))
        data[step] = timeutils.iso(dt)
        data["start_now"] = value == "now"
        if data.get("end_duration"):
            data["end"] = timeutils.iso(dt + timedelta(seconds=data["end_duration"]))
    elif step == "end":
        if data.get("start_now"):
            data["start"] = timeutils.iso(timeutils.now() + timedelta(seconds=15))
        dt = (
            timeutils.parse_dt(data["start"])
            + timedelta(seconds={"hour": 3600, "day": 86400, "week": 604800}[value])
            if value in {"hour", "day", "week"}
            else preferences.parse_local(uid, value)
        )
        if (
            not timeutils.parse_dt(data["start"]) + timedelta(minutes=1)
            <= dt
            <= timeutils.parse_dt(data["start"]) + timedelta(days=365)
        ):
            raise ValueError(tr("Конкурс должен длиться от минуты до года."))
        data[step] = timeutils.iso(dt)
        data["end_duration"] = {"hour": 3600, "day": 86400, "week": 604800}.get(value)
    await state.set_data(data)
    if data.get("form") and step == "quiz":
        await state.update_data(giveaway_type="contest" if data["quiz"] else "raffle")
    if data.get("launch_v4") and not data.get("creation_complete"):
        next_step = {
            "prize_title": "post_style",
            "post_style": "winners",
            "post": "winners",
            "winners": "end",
            "end": "contact",
        }.get(step)
        if next_step:
            await prompt(message, state, next_step)
            return
        await state.update_data(creation_complete=True)
    if data.get("quick"):
        if step == "post" and not data.get("end"):
            await prompt(message, state, "end")
        else:
            await draft_panel(message, state)
        return
    next_step = STEPS[STEPS.index(step) + 1]
    if next_step == "review":
        await state.update_data(step="review")
        sent = await content.send_content(bot, uid, data["post"])
        ui.note_sent(uid, sent)
        await ui.answer(
            message,
            tr("Проверьте конкурс\n")
            + html.escape(data["title"])
            + tr("\nНачало: ")
            + preferences.display(uid, data["start"])
            + tr("\nКонец: ")
            + preferences.display(uid, data["end"])
            + tr(
                "\nПобедителей: {v0}\nПодписок: {v1}\nДрузей для участия: {v2}\nСпособ выбора: {v3}",
                v0=data["winners"],
                v1=len(data["subscriptions"]),
                v2=data["referrals"],
                v3=STATUS[data["mode"]],
            ),
            reply_markup=ui.kb(
                [
                    [ui.choice(tr("✅ Создать конкурс"), "contest:create")],
                    [
                        ui.choice(tr("Начать заново"), "contest:new"),
                        ui.choice(tr("Отменить"), "menu:contests"),
                    ],
                ]
            ),
        )
    else:
        await prompt(message, state, next_step)


@router.message(Create.input, ~F.successful_payment, ~F.text.startswith("/"))
async def input_message(m, state, bot):
    data = await state.get_data()
    if m.media_group_id:
        raise ValueError(tr("Для конкурса отправьте одно фото или видео без альбома."))
    await accept(
        m,
        state,
        bot,
        m.text or m.caption or "",
        content.message_payload(m) if data.get("step") in {"post", "media"} else None,
    )


@router.callback_query(F.data.startswith("contest:setup:"))
async def setup(c, state, bot):
    _, _, step, value = c.data.split(":")
    if (await state.get_data()).get("step") != step:
        raise ValueError(tr("Эта кнопка устарела. Ответьте на последний вопрос."))
    await accept(c.message, state, bot, value)
    await c.answer()


@router.callback_query(F.data == "contest:create")
async def create(c, state):
    d = await state.get_data()
    if d.get("post"):
        d["post"]["field_entities"] = d.get("field_entities", {})
    uid = c.from_user.id
    if d.get("step") != "review":
        raise ValueError(tr("Сначала заполните настройки конкурса."))
    if not accounts.channel_allowed(uid, d["channel_id"]):
        raise ValueError(tr("Канал недоступен."))
    if not all(
        accounts.channel_allowed(uid, cid)
        for cid in d.get("channel_ids", [d["channel_id"]])
    ):
        raise ValueError(tr("Канал недоступен."))
    if d.get("form"):
        if not d.get("prize_title"):
            raise ValueError(tr(PROMPTS["prize_title"]))
        if (
            d["giveaway_type"] == "contest"
            and d["mode"] == "random"
            and not d.get("quiz")
        ):
            raise ValueError(tr(PROMPTS["quiz"]))
        service.publication(draft_row(d, uid))
        d["prize"]["quantity"] = d.get("prize_count", 1)
    if d.get("start_now"):
        d["start"] = timeutils.iso(timeutils.now() + timedelta(seconds=5))
        if d.get("end_duration"):
            d["end"] = timeutils.iso(
                timeutils.parse_dt(d["start"]) + timedelta(seconds=d["end_duration"])
            )
    start_dt = timeutils.parse_dt(d["start"])
    end_dt = timeutils.parse_dt(d["end"])
    if not start_dt + timedelta(minutes=1) <= end_dt <= start_dt + timedelta(days=365):
        raise ValueError(tr("Конкурс должен длиться от минуты до года."))
    if d["prize_kind"] == "promo" and len(d["prize"].get("codes", [])) < d[
        "winners"
    ] * d.get("prize_count", 1):
        raise ValueError(
            tr("Промокодов меньше, чем победителей. Укажите меньшее число.")
        )
    if timeutils.parse_dt(d["end"]) <= timeutils.now() + timedelta(minutes=1):
        raise ValueError(
            tr("Выберите более позднюю дату итогов в настройках розыгрыша.")
        )
    if not d.get("start_now") and timeutils.parse_dt(d["start"]) <= timeutils.now():
        raise ValueError(tr("Измените начало розыгрыша: выбранное время уже прошло."))
    quiz = d.get("quiz") or {}
    if d.get("launch_v4"):
        if not d.get("creation_complete") or not d["subscriptions"]:
            raise ValueError(
                tr(
                    "Укажите приз, пост, дату итогов и хотя бы одну обязательную подписку."
                )
            )
        service.publication(draft_row(d, uid))
        d["mode"] = "ranking" if d["giveaway_type"] == "contest" else "weighted"
    creation_key = f"contest_created:{uid}:{d['creation_token']}"
    with db.atomic():
        existing = db.setting(creation_key)
        if existing:
            cid = int(existing)
        else:
            cid = db.db.execute(
                "INSERT INTO contests(owner_id,channel_id,title,post_json,prize_kind,prize_json,starts_at,ends_at,subscriptions_json,captcha,quiz_question,quiz_hash,mode,referral_min,winner_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uid,
                    d["channel_id"],
                    d["title"],
                    json.dumps(d["post"]),
                    d["prize_kind"],
                    json.dumps(d["prize"]),
                    d["start"],
                    d["end"],
                    json.dumps(d["subscriptions"]),
                    d["captcha"],
                    quiz.get("question"),
                    quiz.get("hash"),
                    d["mode"],
                    d["referrals"],
                    d["winners"],
                    timeutils.iso(),
                ),
            ).lastrowid
            db.db.execute(
                "UPDATE contests SET giveaway_type=?,prize_title=?,claim_contact=?,subscription_layout=?,referral_target=? WHERE id=?",
                (
                    d.get(
                        "giveaway_type",
                        "contest" if d["mode"] == "ranking" else "raffle",
                    ),
                    d.get("prize_title", ""),
                    d.get("claim_contact", ""),
                    d.get("subscription_layout", "buttons"),
                    d.get("referral_target", "participants"),
                    cid,
                ),
            )
            db.db.execute(
                "INSERT INTO app_settings VALUES(?,?)", (creation_key, str(cid))
            )
            for target_id in d.get("channel_ids", [d["channel_id"]]):
                db.db.execute(
                    "INSERT INTO contest_publications(contest_id,channel_id) VALUES(?,?)",
                    (cid, target_id),
                )
    db.execute("DELETE FROM app_settings WHERE key=?", (f"contest_draft:{uid}",))
    await state.clear()
    if getattr(c, "bot", None) is not None:
        await ui.retire_controls(c.bot, uid, "contest_preview")
    await ui.edit(
        c,
        tr(
            "✅ Конкурс #{v0} запланирован. Он появится в списке активных после публикации.",
            v0=cid,
        ),
        ui.back("menu:contests"),
    )
    await c.answer()


async def show_entry(message, state, bot, cid, uid, inviter=None):
    row = service.get(cid)
    if not service.is_open(row):
        await ui.answer(
            message,
            tr("Конкурс сейчас не принимает участников.\nНачало: ")
            + preferences.display(uid, row["starts_at"])
            + tr("\nКонец: ")
            + preferences.display(uid, row["ends_at"]),
        )
        return
    await state.set_data({"contest_id": cid, "inviter": inviter})
    # Attribute a referral immediately; further navigation must not lose it.
    service.register(cid, uid, inviter)
    sent = await content.send_content(bot, uid, json.loads(row["post_json"]))
    ui.note_sent(uid, sent)
    me = await bot.get_me()
    await ui.answer(
        message,
        "🏆 "
        + html.escape(row["title"])
        + tr("\nКонец: ")
        + preferences.display(uid, row["ends_at"])
        + tr(
            "\nПобедителей: {v0}\nДрузей для участия: {v1}",
            v0=row["winner_count"],
            v1=row["referral_min"],
        )
        + "\n"
        + STATUS[row["mode"]]
        + "\n"
        + f"https://t.me/{me.username}?start=contest_{cid}",
        reply_markup=ui.kb(
            [
                [ui.choice(tr("🎉 Участвовать"), f"contest:join:{cid}")],
                [ui.choice(tr("Другие конкурсы"), "contest:list:0")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("contest:view:"))
async def view(c, state, bot):
    await state.clear()
    await show_entry(c.message, state, bot, int(c.data.split(":")[2]), c.from_user.id)
    await c.answer()


async def check_entry(message, state, bot, cid, uid):
    row = service.get(cid)
    if not service.is_open(row):
        raise ValueError(tr("Приём участников завершён или ещё не начался."))
    data = await state.get_data()
    inviter = data.get("inviter") if data.get("contest_id") == cid else None
    entry = service.register(cid, uid, inviter)
    try:
        missing = await service.missing_subscriptions(bot, row, uid)
    except TelegramAPIError:
        await ui.answer(
            message,
            tr("Не удалось проверить подписки. Попробуйте ещё раз чуть позже."),
            reply_markup=ui.kb(
                [[ui.choice(tr("Проверить снова"), f"contest:join:{cid}")]]
            ),
        )
        return
    if missing:
        db.execute(
            "UPDATE contest_entries SET base_valid=0 WHERE contest_id=? AND user_id=?",
            (cid, uid),
        )
        rows = [
            [InlineKeyboardButton(text=ch["title"][:50], url=ch["url"])]
            for ch in missing
        ]
        rows.append([ui.choice(tr("Проверить снова"), f"contest:join:{cid}")])
        await ui.answer(
            message,
            tr("Подпишитесь на эти каналы и группы, затем нажмите «Проверить снова»."),
            reply_markup=ui.kb(rows),
        )
        return
    for kind in ("captcha", "quiz"):
        if (row["captcha"] if kind == "captcha" else row["quiz_hash"]) and not entry[
            kind + "_passed"
        ]:
            if kind == "captcha":
                if not entry["captcha_question"]:
                    a = secrets.randbelow(20) + 1
                    b = secrets.randbelow(20) + 1
                    db.execute(
                        "UPDATE contest_entries SET captcha_question=?,captcha_hash=? WHERE contest_id=? AND user_id=?",
                        (f"{a} + {b} = ?", service.digest(str(a + b)), cid, uid),
                    )
                question = db.one(
                    "SELECT captcha_question FROM contest_entries WHERE contest_id=? AND user_id=?",
                    (cid, uid),
                )[0]
            else:
                question = row["quiz_question"]
            await state.set_state(Entry.answer)
            await state.update_data(contest_id=cid, kind=kind)
            await ui.answer(
                message, tr("Ответьте на вопрос:\n") + html.escape(question)
            )
            return
    if not service.is_open(service.get(cid)):
        raise ValueError(tr("Приём участников завершён."))
    db.execute(
        "UPDATE contest_entries SET base_valid=1 WHERE contest_id=? AND user_id=?",
        (cid, uid),
    )
    count = service.referral_count(cid, uid)
    link = await service.referral_link(bot, row, uid)
    await service.refresh_count(bot, cid)
    await state.clear()
    text = (
        tr("✅ Вы участвуете!")
        if count >= row["referral_min"]
        else tr("Осталось пригласить друзей: ") + str(row["referral_min"] - count)
    )
    await ui.answer(
        message,
        text
        + tr("\nПриглашено: {count}\nВаша ссылка:\n{link}", count=count, link=link),
        reply_markup=ui.kb(
            [
                [ui.choice(tr("Проверить снова"), f"contest:join:{cid}")],
                [ui.choice(tr("Конкурсы"), "menu:contests")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("contest:join:"))
async def join(c, state, bot):
    await c.answer()
    await check_entry(c.message, state, bot, int(c.data.split(":")[2]), c.from_user.id)


@router.callback_query(F.data.startswith("contest:participate:"))
async def participate(c, bot):
    cid = int(c.data.split(":")[2])
    row = service.get(cid)
    if not c.message or not any(
        c.message.chat.id == target["telegram_chat_id"]
        and c.message.message_id == target["message_id"]
        for target in service.publication_targets(row)
    ):
        raise ValueError(tr("Используйте кнопку в исходном посте канала."))
    if not service.is_open(row):
        await c.answer(
            tr("Приём участников завершён или ещё не начался."), show_alert=True
        )
        return
    if c.from_user.is_bot or accounts.blocked(c.from_user.id):
        await c.answer(tr("Доступ запрещён."), show_alert=True)
        return
    uid = c.from_user.id
    await accounts.ensure_user_async(c.from_user)
    entry = service.register(cid, uid)
    try:
        missing = await service.missing_subscriptions(bot, row, uid)
    except TelegramAPIError:
        await c.answer(
            tr("Не удалось проверить подписки. Попробуйте ещё раз чуть позже."),
            show_alert=True,
        )
        return
    if missing:
        db.execute(
            "UPDATE contest_entries SET base_valid=0 WHERE contest_id=? AND user_id=?",
            (cid, uid),
        )
        text = tr(
            "Подпишитесь: {channels}", channels=", ".join(ch["title"] for ch in missing)
        )
        await c.answer(text[:190], show_alert=True)
        await service.refresh_count(bot, cid)
        return
    if not service.is_open(service.get(cid)):
        await c.answer(tr("Приём участников завершён."), show_alert=True)
        return
    needs_private = (row["captcha"] and not entry["captcha_passed"]) or (
        row["quiz_hash"] and not entry["quiz_passed"]
    )
    if not needs_private:
        db.execute(
            "UPDATE contest_entries SET base_valid=1 WHERE contest_id=? AND user_id=?",
            (cid, uid),
        )
    remaining = max(0, row["referral_min"] - service.referral_count(cid, uid))
    if needs_private or remaining:
        me = await bot.get_me()
        reason = (
            tr("Пройдите капчу / вопрос в боте.")
            if needs_private
            else tr("Осталось пригласить друзей: {count}.", count=remaining)
        )
        await c.answer(
            (
                reason
                + tr(
                    " Откройте @{bot} и отправьте /start contest_{id}",
                    bot=me.username,
                    id=cid,
                )
            )[:190],
            show_alert=True,
        )
    else:
        await c.answer(
            tr("✅ Вы участвуете в конкурсе!")
            if row["mode"] == "ranking"
            else tr("✅ Вы участвуете в розыгрыше!"),
            show_alert=True,
        )
    await service.refresh_count(bot, cid)


@router.chat_member()
async def channel_referral(event, bot):
    def present(member):
        return member.status in {"creator", "administrator", "member"} or (
            member.status == "restricted" and member.is_member
        )

    uid = event.new_chat_member.user.id
    if event.new_chat_member.user.is_bot:
        return
    active = int(present(event.new_chat_member))
    # Rejoins never earn another referral or change the original inviter.
    for row in db.all_rows(
        "SELECT c.* FROM contests c JOIN channels ch ON ch.id=c.channel_id WHERE ch.telegram_chat_id=? AND c.status='active' AND c.referral_target='channel'",
        (event.chat.id,),
    ):
        if not service.is_open(row):
            continue
        db.execute(
            "UPDATE contest_channel_referrals SET active=? WHERE contest_id=? AND user_id=?",
            (active, row["id"], uid),
        )
        link = event.invite_link.invite_link if event.invite_link else None
        invite = db.one(
            "SELECT * FROM contest_invites WHERE contest_id=? AND invite_link=?",
            (row["id"], link),
        )
        if (
            active
            and not present(event.old_chat_member)
            and invite
            and invite["user_id"] != uid
            and not accounts.blocked(uid)
        ):
            db.execute(
                "INSERT OR IGNORE INTO contest_channel_referrals(contest_id,user_id,inviter_id) VALUES(?,?,?)",
                (row["id"], uid, invite["user_id"]),
            )
        await service.refresh_count(bot, row["id"])


@router.message(Entry.answer, F.text, ~F.text.startswith("/"))
async def answer(m, state, bot):
    data = await state.get_data()
    cid = data["contest_id"]
    uid = m.from_user.id
    row = service.get(cid)
    if not service.is_open(row):
        raise ValueError(tr("Приём участников завершён."))
    entry = db.one(
        "SELECT * FROM contest_entries WHERE contest_id=? AND user_id=?", (cid, uid)
    )
    if (
        entry["locked_until"]
        and timeutils.parse_dt(entry["locked_until"]) > timeutils.now()
    ):
        raise ValueError(tr("Слишком много попыток. Повторите через минуту."))
    kind = data["kind"]
    expected = entry["captcha_hash"] if kind == "captcha" else row["quiz_hash"]
    if not secrets.compare_digest(service.digest(m.text), expected):
        attempts = entry["attempts"] + 1
        lock = (
            timeutils.iso(timeutils.now() + timedelta(minutes=1))
            if attempts >= 5
            else None
        )
        db.execute(
            "UPDATE contest_entries SET attempts=?,locked_until=? WHERE contest_id=? AND user_id=?",
            (0 if lock else attempts, lock, cid, uid),
        )
        raise ValueError(tr("Неверный ответ. Попробуйте ещё раз."))
    db.execute(
        f"UPDATE contest_entries SET {kind}_passed=1,attempts=0,locked_until=NULL WHERE contest_id=? AND user_id=?",
        (cid, uid),
    )
    await check_entry(m, state, bot, cid, uid)


@router.callback_query(
    F.data.startswith("contest:manage:") | F.data.startswith("contest:cancel:")
)
async def manage(c):
    op = c.data.split(":")[1]
    row = service.get(int(c.data.split(":")[2]))
    if row["owner_id"] != c.from_user.id:
        raise ValueError(tr("Нет доступа к этому конкурсу."))
    if op == "cancel":
        db.execute(
            "UPDATE contests SET status='cancelled' WHERE id=? AND status IN ('scheduled','active','uncertain')",
            (row["id"],),
        )
        row = service.get(row["id"])
        if row["status"] == "cancelled":
            with db.atomic():
                for target in service.publication_targets(row):
                    if target["message_id"]:
                        service.enqueue(
                            row["id"],
                            "results",
                            target["telegram_chat_id"],
                            {
                                "text": tr(
                                    "⛔ Организатор отменил конкурс / розыгрыш."
                                ),
                                "message_id": target["message_id"],
                            },
                        )
    entries = service.participant_count(row["id"])
    delivery = db.all_rows(
        "SELECT kind,recipient,status FROM contest_outbox WHERE contest_id=?",
        (row["id"],),
    )
    text = (
        html.escape(row["title"])
        + tr("\nСтатус: {v0}\nУчастников: {v1}\n", v0=STATUS[row["status"]], v1=entries)
        + html.escape(row["error"] or "")
    )
    text += "\n" + "\n".join(
        f"{d['recipient']} · {STATUS[d['status']]}" for d in delivery
    )
    rows = (
        [[ui.choice(tr("Отменить конкурс"), f"contest:cancel:{row['id']}")]]
        if row["status"] in {"scheduled", "active", "uncertain"}
        else []
    )
    rows.append([ui.choice(tr("Назад"), "contest:mine:0")])
    if row["status"] == "uncertain":
        text += "\n" + tr(
            "Проверьте канал перед повтором: публикация могла уже появиться."
        )
        rows.insert(
            0,
            [
                ui.choice(
                    tr("Пост уже опубликован"), f"contest:recover:{row['id']}:sent"
                )
            ],
        )
        rows.insert(
            1,
            [
                ui.choice(
                    tr("Публикации нет, повторить"),
                    f"contest:recover:{row['id']}:retry",
                )
            ],
        )
    for item in db.all_rows(
        "SELECT id,recipient FROM contest_outbox WHERE contest_id=? AND status IN ('failed','uncertain')",
        (row["id"],),
    ):
        rows.insert(
            0,
            [
                ui.choice(
                    tr("Проверить отправку: {uid}", uid=item["recipient"]),
                    f"contest:delivery:{item['id']}",
                )
            ],
        )
    await ui.edit(c, text, ui.kb(rows))
    await c.answer()


@router.callback_query(
    F.data.startswith("contest:delivery:")
    | F.data.startswith("contest:retry:")
    | F.data.startswith("contest:sent:")
)
async def delivery_review(c):
    _, op, oid = c.data.split(":")
    oid = int(oid)
    item = db.one(
        "SELECT o.*,c.owner_id FROM contest_outbox o JOIN contests c ON c.id=o.contest_id WHERE o.id=?",
        (oid,),
    )
    if not item or item["owner_id"] != c.from_user.id:
        raise ValueError(tr("Нет доступа."))
    if item["status"] not in {"failed", "uncertain"}:
        raise ValueError(tr("Уже отправлено."))
    if op == "delivery":
        await ui.edit(
            c,
            tr(
                "Проверьте, получено ли сообщение. Повторная отправка может создать дубликат."
            ),
            ui.kb(
                [
                    [ui.choice(tr("Да, получено"), f"contest:sent:{oid}")],
                    [ui.choice(tr("Нет, отправить снова"), f"contest:retry:{oid}")],
                    [ui.choice(tr("Назад"), f"contest:manage:{item['contest_id']}")],
                ]
            ),
        )
    else:
        db.execute(
            "UPDATE contest_outbox SET status=?,error=NULL WHERE id=? AND status IN ('failed','uncertain')",
            ("pending" if op == "retry" else "sent", oid),
        )
        await ui.edit(
            c,
            tr("✅ Решение сохранено."),
            ui.back(f"contest:manage:{item['contest_id']}"),
        )
    await c.answer()


@router.callback_query(F.data.startswith("contest:recover:"))
async def recover_publication(c, state):
    _, _, cid, decision = c.data.split(":")
    row = service.get(int(cid))
    if row["owner_id"] != c.from_user.id:
        raise ValueError(tr("Нет доступа."))
    if row["status"] != "uncertain" or decision not in {"sent", "retry"}:
        raise ValueError(tr("Этот шаг устарел. Откройте настройку заново."))
    if decision == "sent":
        await state.set_state(Recover.post)
        targets = service.publication_targets(row)
        pending = next((t for t in targets if not t["message_id"]), targets[0])
        await state.set_data(
            {
                "recover_contest_id": row["id"],
                "recover_channel_id": pending["channel_id"],
            }
        )
        title = db.one(
            "SELECT title FROM channels WHERE id=?", (pending["channel_id"],)
        )[0]
        await ui.edit(
            c,
            html.escape(title)
            + "\n"
            + tr(
                "Перешлите исходный пост из канала или отправьте его числовой ID сообщения. Он нужен для кнопки участия и обновления итогов."
            ),
            ui.back(f"contest:manage:{row['id']}"),
        )
        await c.answer()
        return
    db.execute(
        "UPDATE contests SET status=?,error=NULL WHERE id=? AND status='uncertain'",
        ("active" if decision == "sent" else "scheduled", row["id"]),
    )
    await ui.edit(
        c, tr("✅ Решение сохранено."), ui.back(f"contest:manage:{row['id']}")
    )
    await c.answer()


@router.message(Recover.post, ~F.text.startswith("/"))
async def recover_message(m, state):
    data = await state.get_data()
    row = service.get(data["recover_contest_id"])
    if row["owner_id"] != m.from_user.id or row["status"] != "uncertain":
        raise ValueError(tr("Этот шаг устарел. Откройте настройку заново."))
    origin = m.forward_origin
    channel_id = data.get("recover_channel_id", row["channel_id"])
    if origin and origin.type == "channel":
        target_chat = db.one(
            "SELECT telegram_chat_id FROM channels WHERE id=?", (channel_id,)
        )
        if origin.chat.id != target_chat[0]:
            raise ValueError(tr("Используйте кнопку в исходном посте канала."))
        message_id = origin.message_id
    else:
        raw = (m.text or "").strip()
        if not raw.isdigit() or not 0 < int(raw) <= 2147483647:
            raise ValueError(
                tr("Отправьте положительный числовой ID исходного сообщения.")
            )
        message_id = int(raw)
    db.execute(
        "INSERT INTO contest_publications(contest_id,channel_id,message_id) VALUES(?,?,?) ON CONFLICT(contest_id,channel_id) DO UPDATE SET message_id=excluded.message_id",
        (row["id"], channel_id, message_id),
    )
    if channel_id == row["channel_id"]:
        db.execute(
            "UPDATE contests SET published_ids=? WHERE id=?",
            (json.dumps([message_id]), row["id"]),
        )
    pending = any(not t["message_id"] for t in service.publication_targets(row))
    db.execute(
        "UPDATE contests SET status=?,displayed_count=-1,error=NULL WHERE id=? AND status='uncertain'",
        ("uncertain" if pending else "active", row["id"]),
    )
    await state.clear()
    await ui.answer(
        m,
        tr("✅ Решение сохранено."),
        reply_markup=ui.back(f"contest:manage:{row['id']}"),
    )
