"""Persistent questionnaire for contest drafts; publishing stays in contests."""

import html
import secrets
from datetime import timedelta

from aiogram.types import InlineKeyboardMarkup

from app import accounts, content, preferences, timeutils, ui
from app import database as db
from app.i18n import STATUS, tr

PROMPTS = {
    "prize_description": "Описание приза (до 500 символов):",
    "prize_count": "Количество призов на победителя (от 1 до 100):",
    "media": "Отправьте фото, видео или GIF для поста конкурса.",
}


async def channels(c, state):
    from app.features import contests as flow

    data = await state.get_data()
    selected = data.get("channel_ids", [])
    rows = ui.button_grid(
        [
            ui.choice(
                ("✅ " if ch["id"] in selected else "▫️ ") + ch["title"][:30],
                f"contest:select:{ch['id']}",
            )
            for ch in accounts.eligible_channels(c.from_user.id)
        ],
        2,
    )
    rows += [
        [ui.choice(tr("✅ Готово"), "contest:selected")],
        [ui.choice(tr("Назад"), "menu:contests")],
    ]
    flow.save_draft(c.from_user.id, data)
    await ui.edit(c, tr("📺 Выберите каналы для публикации:"), ui.kb(rows))


async def start(c, state):
    await state.clear()
    await state.set_data({"selecting_channels": True, "channel_ids": []})
    await channels(c, state)
    await c.answer()


async def toggle(c, state):
    data = await state.get_data()
    if not data.get("selecting_channels"):
        raise ValueError(tr("Этот шаг устарел. Откройте настройку заново."))
    cid = int(c.data.split(":")[-1])
    if not accounts.channel_allowed(c.from_user.id, cid):
        raise ValueError(tr("Канал недоступен."))
    ids = data.get("channel_ids", [])
    ids.remove(cid) if cid in ids else ids.append(cid)
    await state.update_data(channel_ids=ids)
    await channels(c, state)
    await c.answer()


async def selected(c, state):
    from app.features import contests as flow

    previous = await state.get_data()
    ids = previous.get("channel_ids", [])
    if not previous.get("selecting_channels") or not ids:
        raise ValueError(tr("Выберите хотя бы один канал."))
    if not all(accounts.channel_allowed(c.from_user.id, cid) for cid in ids):
        raise ValueError(tr("Канал недоступен."))
    start_at = timeutils.now() + timedelta(seconds=15)
    data = dict(
        form=True,
        quick=True,
        channel_id=ids[0],
        channel_ids=ids,
        giveaway_type="raffle",
        title=tr("Розыгрыш"),
        prize_title="",
        prize_count=1,
        prize_description="",
        claim_contact="",
        subscription_layout="buttons",
        referral_target="participants",
        creation_token=secrets.token_hex(16),
        prize_kind="physical",
        prize={"value": tr("Ожидайте, скоро с вами свяжутся для вручения призов.")},
        winners=1,
        mode="random",
        subscriptions=[],
        captcha=0,
        quiz=None,
        referrals=0,
        start_now=True,
        start=timeutils.iso(start_at),
        end=timeutils.iso(start_at + timedelta(days=1)),
        end_duration=86400,
    )
    data["post"] = generated(data)
    await state.set_data(data)
    flow.save_draft(c.from_user.id, data)
    await flow.prompt(c.message, state, "prize_title")
    await c.answer()


def generated(data):
    title = (
        tr("🏆 Конкурс")
        if data.get("giveaway_type") == "contest"
        else tr("🎉 Розыгрыш")
    )
    details = (data.get("quiz") or {}).get("question") or STATUS[
        data.get("mode", "random")
    ]
    text = title + "\n\n" + details
    entities = [
        dict(e, offset=e["offset"] + content.utf16len(title + "\n\n"))
        for e in data.get("field_entities", {}).get("quiz", [])
    ]
    if not data.get("quiz"):
        entities = []
    if data.get("prize_description"):
        entities += [
            dict(e, offset=e["offset"] + content.utf16len(text + "\n\n"))
            for e in data.get("field_entities", {}).get("prize_description", [])
        ]
        text += "\n\n" + data["prize_description"]
    text += "\n🎁 × " + str(data.get("prize_count", 1))
    if data.get("captcha"):
        text += "\n" + tr("🧮 Математическая капча")
    if data.get("referrals"):
        text += "\n👥 " + str(data["referrals"])
    return {"content_type": "text", "text": text, "entities_json": entities}


async def panel(message, state, callback=False):
    from app.features import contests as flow
    from services import contests as service

    data = await state.get_data()
    if not data.get("custom_post"):
        post = generated(data)
        if data["post"]["content_type"] in {"photo", "video", "animation"}:
            data["post"].update(
                text=post["text"], caption_entities_json=post["entities_json"]
            )
        else:
            data["post"] = post
    data["post"]["field_entities"] = data.get("field_entities", {})
    uid = message.from_user.id if callback else message.chat.id
    data["step"] = "review"
    await state.set_state(flow.Create.input)
    await state.set_data(data)
    flow.save_draft(uid, data)
    text = (
        tr("Настройте приз, условия и сроки. Проверьте пост перед публикацией.")
        + "\n\n"
    )
    publication = service.publication(flow.draft_row(data, uid))["text"]
    text += publication[:2200] + ("…" if len(publication) > 2200 else "")
    text += "\n\n📅 " + (
        tr("Сразу после запуска")
        if data["start_now"]
        else preferences.display(uid, data["start"])
    )
    channel_ids = data["channel_ids"]
    channels_by_id = {
        row["id"]: row["title"]
        for row in db.all_rows(
            "SELECT id,title FROM channels WHERE id IN ("
            + ",".join("?" for _ in channel_ids)
            + ")",
            channel_ids,
        )
    }
    titles = [channels_by_id.get(cid, str(cid)) for cid in channel_ids]
    text += "\n📺 " + ", ".join(titles)[:600]
    rows = [
        [
            ui.choice(tr("📝 Описание"), "contest:edit:post"),
            ui.choice(tr("🖼 Медиа"), "contest:edit:media"),
            ui.choice(tr("👁 Предпросмотр"), "contest:preview"),
        ],
        [
            ui.choice(tr("🎁 Приз"), "contest:section:prize"),
            ui.choice(tr("🏆 Победители"), "contest:edit:winners"),
            ui.choice(tr("🎁 Выдача приза"), "contest:edit:prize_kind"),
        ],
        [
            ui.choice(tr("📢 Подписки"), "contest:subscriptions"),
            ui.choice(tr("🎲 Как выбрать"), "contest:edit:mode"),
            ui.choice(
                ("☑ " if data.get("captcha") else "☐ ") + tr("🧮 Капча"),
                "contest:edit:captcha",
            ),
        ],
        [
            ui.choice(tr("📅 Начало"), "contest:edit:start"),
            ui.choice(tr("🕒 Дата итогов"), "contest:edit:end"),
            ui.choice(tr("📞 Контакт"), "contest:edit:contact"),
        ],
        [
            ui.choice(tr("🧩 Шаблон"), "contest:edit:post_style"),
            ui.choice(tr("⚙️ Дополнительно"), "contest:extra"),
        ],
        [
            ui.choice(tr("❌ Отмена"), "contest:discard"),
            ui.choice(tr("💾 Сохранить"), "menu:contests"),
            ui.choice(tr("🚀 Опубликовать"), "contest:create"),
        ],
    ]
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[ui.button_style(button) for button in row] for row in rows]
    )
    if callback:
        await ui.edit(message, html.escape(text), markup)
    else:
        await ui.answer(message, html.escape(text), reply_markup=markup)


async def accept(message, state, bot, value, payload):
    """Handle additional fields; return False for existing validated fields."""
    from app.features import contests as flow

    data = await state.get_data()
    step = data.get("step")
    raw = value
    value = value.strip()
    if step == "prize_description":
        if not 1 <= len(value) <= 500:
            raise ValueError(tr(PROMPTS[step]))
        data[step] = value
        fields = dict(data.get("field_entities", {}))
        fields[step] = content.input_entities(message, raw, value)
        data["field_entities"] = fields
    elif step == "prize_count":
        if not value.isdigit() or not 1 <= int(value) <= 100:
            raise ValueError(tr(PROMPTS[step]))
        data[step] = int(value)
    elif step == "media":
        if not payload or payload["content_type"] not in {
            "photo",
            "video",
            "animation",
        }:
            raise ValueError(tr(PROMPTS[step]))
        old = data["post"]
        data["post"] = dict(
            payload,
            text=old.get("text", ""),
            caption_entities_json=old.get(
                "entities_json", old.get("caption_entities_json", [])
            ),
        )
    elif step == "mode" and value == "task":
        data.update(giveaway_type="contest", mode="random")
        await state.set_data(data)
        flow.save_draft(message.chat.id, data)
        await flow.prompt(message, state, "quiz")
        return True
    else:
        return False
    await state.set_data(data)
    await panel(message, state)
    return True


SECTIONS = {
    "prize": (
        "🎁 Приз",
        [
            ("Название приза", "edit:prize_title"),
            ("Описание приза", "edit:prize_description"),
            ("Количество призов", "edit:prize_count"),
            ("Победители", "edit:winners"),
            ("Выдача приза", "edit:prize_kind"),
            ("Контакт победителям", "edit:contact"),
        ],
    ),
    "rules": (
        "👥 Условия участия",
        [
            ("Как выбрать победителей", "edit:mode"),
            ("Подписки", "subscriptions"),
            ("Дополнительно", "extra"),
        ],
    ),
    "dates": ("📅 Сроки", [("Начало", "edit:start"), ("Дата итогов", "edit:end")]),
    "post": (
        "✏️ Оформление поста",
        [
            ("Изменить текст", "edit:post"),
            ("Фото / видео / GIF", "edit:media"),
            ("Готовый шаблон", "edit:post_style"),
            ("Удалить черновик", "discard"),
        ],
    ),
}


async def section(c, state):
    data = await state.get_data()
    key = c.data.split(":")[-1]
    if not data.get("form") or key not in SECTIONS:
        raise ValueError(tr("Сначала заполните настройки конкурса."))
    title, items = SECTIONS[key]
    rows = ui.button_grid(
        [ui.choice(tr(label), "contest:" + action) for label, action in items], 2
    )
    rows.append([ui.choice("⬅️ " + tr("Назад"), "contest:panel")])
    await ui.edit(c, tr(title), ui.kb(rows))
    await c.answer()
