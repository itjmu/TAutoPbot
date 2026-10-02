"""Edit a forwarded channel message in place; never publish a replacement."""

import asyncio
import json
import re
import secrets
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

from app import access, accounts, content, ui
from app import database as db
from services.reactions import inherit_edited, locks, toggle

router = Router(name="features.published_editor")
MEDIA = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "animation": InputMediaAnimation,
    "document": InputMediaDocument,
    "audio": InputMediaAudio,
}


class PublishedEdit(StatesGroup):
    original = State()
    ready = State()
    text = State()
    media = State()
    cover = State()


def store_session(uid, data):
    oid = int(data["token"], 16)
    db.execute(
        "INSERT OR REPLACE INTO app_settings(key,value) VALUES(?,?)",
        (f"live_session:{uid}:{oid}", json.dumps(data, ensure_ascii=False)),
    )
    return oid


def load_session(uid, oid, published=False):
    raw = db.setting(f"live_session:{uid}:{oid}")
    if not raw:
        raise ValueError("Редактор устарел. Перешлите сообщение заново.")
    data = json.loads(raw)
    if not published and data.get("closed"):
        raise ValueError("Редактирование уже завершено.")
    return data


def import_buttons(markup, known=()):
    definitions = {b["id"]: b for b in known}
    buttons = []
    for row, values in enumerate(markup.inline_keyboard if markup else [], 1):
        for button in values:
            bid = (button.callback_data or "").split(":")[-1]
            if bid in definitions:
                item = dict(definitions[bid], row=row)
            elif button.url:
                item = {
                    "id": secrets.token_hex(4),
                    "type": "url",
                    "text": button.text,
                    "url": button.url,
                    "style": button.style,
                    "row": row,
                }
            else:
                item = {
                    "id": secrets.token_hex(4),
                    "type": "raw",
                    "text": button.text,
                    "style": button.style,
                    "row": row,
                    "raw_button": button.model_dump(mode="json", exclude_none=True),
                }
            buttons.append(item)
    return buttons


def live_markup(uid, data, preview=False):
    if "buttons" not in data:
        return (
            InlineKeyboardMarkup.model_validate(data["markup"])
            if data.get("markup")
            else None
        )
    from aiogram.types import InlineKeyboardButton

    oid = int(data["token"], 16)
    rows = {}
    counts = {
        r["button_id"]: r["n"]
        for r in db.all_rows(
            "SELECT button_id,COUNT(*) n FROM edited_reactions WHERE owner_id=? AND session_id=? GROUP BY button_id",
            (uid, oid),
        )
    }
    for b in data["buttons"]:
        label = b["text"]
        if b["type"] == "raw":
            button = InlineKeyboardButton.model_validate(
                dict(b["raw_button"], text=label, style=b.get("style"))
            )
            if preview and button.callback_data:
                button = button.model_copy(update={"callback_data": "demo"})
        elif b["type"] == "url":
            button = InlineKeyboardButton(
                text=label, url=b["url"], style=b.get("style")
            )
        else:
            if b["type"] == "reaction":
                count = counts.get(b["id"], 0)
                label += f" {count + b.get('initial_count', 0)}"
            button = InlineKeyboardButton(
                text=label,
                callback_data="demo" if preview else f"lb:{uid}:{oid}:{b['id']}",
                style=b.get("style"),
            )
        rows.setdefault(b["row"], []).append(button)
    return (
        InlineKeyboardMarkup(inline_keyboard=[rows[k] for k in sorted(rows)])
        if rows
        else None
    )


async def show_button_editor(bot, uid, oid):
    from app.features import editors

    data = load_session(uid, oid)
    markup = editors.button_canvas(uid, "l", oid)
    old = data.get("preview_id")
    if old and not data.get("refresh_preview"):
        try:
            await bot.edit_message_reply_markup(
                chat_id=uid, message_id=old, reply_markup=markup
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            if "message to edit not found" not in str(exc).lower():
                raise
    sent = await content.send_content(bot, uid, data["payload"], markup)
    ui.note_transient(uid, sent)
    data.update(preview_id=sent.message_id, refresh_preview=False)
    store_session(uid, data)
    if old:
        try:
            await bot.delete_message(uid, old)
        except TelegramBadRequest:
            pass


async def return_to_editor(c, state, oid):
    data = load_session(c.from_user.id, oid)
    await state.set_data(data)
    await preview(c.bot, c.from_user.id, state)


async def check_access(bot, uid, chat_id, message_id=None):
    if accounts.blocked(uid):
        raise ValueError("Доступ запрещён.")
    me = await bot.get_me()
    owner, member = await asyncio.gather(
        bot.get_chat_member(chat_id, uid), bot.get_chat_member(chat_id, me.id)
    )
    channel = db.one(
        "SELECT * FROM channels WHERE telegram_chat_id=? AND owner_telegram_id=? AND is_active=1",
        (chat_id, uid),
    )
    if channel and channel["chat_type"] in {"group", "supergroup"}:
        if not accounts.channel_allowed(uid, channel["id"]):
            raise ValueError("Группа недоступна.")
        if not message_id or not db.one(
            "SELECT 1 FROM published_messages WHERE channel_id=? AND telegram_message_id=? AND deleted=0",
            (channel["id"], message_id),
        ):
            raise ValueError(
                "Можно редактировать только сообщения, опубликованные этим ботом в вашей группе."
            )
        if not await access.owner_and_bot_ok(bot, chat_id, uid):
            raise ValueError("Вам и боту нужны права администратора группы.")
        return
    for actor in (owner, member):
        if actor.status != "creator" and (
            actor.status != "administrator"
            or not getattr(actor, "can_edit_messages", False)
        ):
            raise ValueError(
                "Вам и боту нужны права администратора канала с разрешением редактировать сообщения."
            )


def controls(data):
    token = data["token"]
    note = data["payload"]["content_type"] == "video_note"
    rows = (
        []
        if note
        else [[ui.choice("📝 Изменить текст / подпись", f"live:text:{token}")]]
    )
    rows.append([ui.choice("🔘 Кнопки", f"live:buttons:{token}")])
    if data.get("buttons"):
        rows.append([ui.choice("🗑 Удалить все кнопки", f"live:clearbuttons:{token}")])
    if not note:
        rows.append(
            [
                ui.choice("🧩 Добавить шаблон", f"live:templates:{token}"),
                ui.choice("🧹 Убрать чужие ссылки", f"live:clean:{token}"),
            ]
        )
    rows.append([ui.choice("↩️ Сбросить изменения", f"live:undo:{token}")])
    if data["payload"]["content_type"] == "video":
        rows.append([ui.choice("🖼 Обложка видео", f"live:cover:{token}")])
    if data["payload"]["content_type"] in MEDIA:
        rows.append([ui.choice("🖼 Заменить медиа", f"live:media:{token}")])
    rows += [
        [ui.choice("💾 Сохранить в канале/группе", f"live:apply:{token}")],
        [ui.choice("❌ Отмена", "live:cancel")],
    ]
    return ui.kb(rows)


async def preview(bot, uid, state):
    data = await state.get_data()
    current_markup = live_markup(uid, data, preview=True)
    rows = current_markup.inline_keyboard if current_markup else []
    # The preview must not activate live reactions or subscription callbacks.
    rows = [
        [
            b.model_copy(update={"callback_data": "demo"}) if b.callback_data else b
            for b in row
        ]
        for row in rows
    ]
    navigation = controls(data)
    markup = (
        InlineKeyboardMarkup(inline_keyboard=rows + navigation.inline_keyboard)
        if sum(map(len, rows + navigation.inline_keyboard)) <= 100
        else navigation
    )
    old = data.get("preview_id")
    if old:
        try:
            await edit_payload(
                bot, uid, old, data["payload"], markup, data.get("media_changed", False)
            )
            await state.set_state(PublishedEdit.ready)
            store_session(uid, await state.get_data())
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                await state.set_state(PublishedEdit.ready)
                store_session(uid, await state.get_data())
                return
            if "message to edit not found" not in str(exc).lower():
                raise
    sent = await content.send_content(bot, uid, data["payload"], markup)
    await state.update_data(preview_id=sent.message_id)
    ui.note_transient(uid, sent)
    await state.set_state(PublishedEdit.ready)
    store_session(uid, await state.get_data())
    if old:
        try:
            await bot.delete_message(uid, old)
        except TelegramBadRequest:
            pass


async def edit_payload(bot, chat_id, message_id, payload, markup, media_changed=False):
    kwargs = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": markup,
    }
    text = payload.get("text") or ""
    typ = payload["content_type"]
    entities = content.entity_list(
        payload.get("entities_json" if typ == "text" else "caption_entities_json")
    )
    try:
        if typ == "video_note":
            if media_changed:
                raise ValueError("Для круглого видео можно изменить только кнопки.")
            await bot.edit_message_reply_markup(**kwargs)
        elif typ == "text":
            await bot.edit_message_text(
                text=text, entities=entities, parse_mode=None, **kwargs
            )
        elif media_changed:
            media = MEDIA[typ](
                media=payload["file_id"],
                caption=text,
                caption_entities=entities,
                parse_mode=None,
                **({"cover": payload.get("cover_file_id")} if typ == "video" else {}),
            )
            await bot.edit_message_media(media=media, **kwargs)
        else:
            await bot.edit_message_caption(
                caption=text, caption_entities=entities, parse_mode=None, **kwargs
            )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def apply_edit(bot, uid, data):
    key = (data["chat_id"], data["message_id"])
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    async with lock:
        await apply_edit_locked(bot, uid, data)


async def apply_edit_locked(bot, uid, data):
    await check_access(bot, uid, data["chat_id"], data["message_id"])
    inherit_edited(uid, data)
    payload = data["payload"]
    markup = live_markup(uid, data)
    await edit_payload(
        bot,
        data["chat_id"],
        data["message_id"],
        payload,
        markup,
        data.get("media_changed", False),
    )
    data["markup"] = (
        markup.model_dump(mode="json", exclude_none=True) if markup else None
    )
    if "token" in data:
        data["closed"] = True
        store_session(uid, data)
    db.log_event(uid, "EDIT_PUBLISHED", f"{data['chat_id']}:{data['message_id']}")
    if "buttons" in data:
        # New interactive buttons are owned by this exact-message edit session.
        # Retire old action callbacks so delayed clicks cannot restore old markup.
        db.execute(
            "UPDATE published_messages SET buttons_json='[]' WHERE telegram_message_id=? AND channel_id IN (SELECT id FROM channels WHERE telegram_chat_id=?)",
            (data["message_id"], data["chat_id"]),
        )
    db.execute(
        "INSERT OR REPLACE INTO app_settings(key,value) VALUES(?,?)",
        (
            f"published_edit:{data['chat_id']}:{data['message_id']}",
            json.dumps(
                {
                    "payload": payload,
                    "markup": data.get("markup"),
                    "buttons": data.get("buttons"),
                    "button_owner": uid,
                    "button_session": int(data["token"], 16)
                    if data.get("token")
                    else None,
                },
                ensure_ascii=False,
            ),
        ),
    )


@router.callback_query(F.data.startswith("live:"))
async def action(c, state, bot):
    uid = c.from_user.id
    parts = c.data.split(":")
    action = parts[1]
    if action == "start":
        await state.clear()
        await state.set_state(PublishedEdit.original)
        await ui.edit(
            c,
            "✏️ Перешлите сообщение из канала или отправьте ссылку на сообщение, опубликованное этим ботом в группе/теме. Изменится исходное сообщение; ссылка и ID сохранятся. Для альбома выберите один элемент. Вам и боту нужны права администратора.",
            ui.kb(
                [
                    [ui.choice("📋 Мои публикации", "live:recent:0")],
                    [ui.choice("⬅️ Назад", "menu:posts")],
                ]
            ),
        )
        await c.answer()
        return
    if action == "recent":
        page = max(0, int(parts[2]))
        records = db.all_rows(
            "SELECT pm.telegram_message_id,ch.id,ch.title,pt.message_thread_id,ft.name FROM published_messages pm JOIN channels ch ON ch.id=pm.channel_id JOIN post_targets pt ON pt.post_id=pm.post_id AND pt.channel_id=pm.channel_id LEFT JOIN forum_topics ft ON ft.chat_id=ch.telegram_chat_id AND ft.topic_id=pt.message_thread_id WHERE ch.owner_telegram_id=? AND ch.is_active=1 AND pm.deleted=0 ORDER BY pm.id DESC LIMIT 11 OFFSET ?",
            (uid, page * 10),
        )
        rows = []
        for row in records[:10]:
            label = row["title"][:20]
            if row["message_thread_id"]:
                label += " · " + (row["name"] or str(row["message_thread_id"]))[:15]
            label += f" · #{row['telegram_message_id']}"
            rows.append(
                [
                    ui.choice(
                        label, f"live:open:{row['id']}:{row['telegram_message_id']}"
                    )
                ]
            )
        navigation = []
        if page:
            navigation.append(ui.choice("◀️", f"live:recent:{page - 1}"))
        if len(records) > 10:
            navigation.append(ui.choice("▶️", f"live:recent:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([ui.choice("⬅️ Назад", "live:start")])
        await ui.edit(
            c, "📋 Выберите опубликованное сообщение для редактирования:", ui.kb(rows)
        )
        await c.answer()
        return
    if action == "open":
        message = SimpleNamespace(
            from_user=c.from_user,
            forward_origin=None,
            text=None,
            reply_markup=None,
            media_group_id=None,
        )
        await original(message, state, bot, saved_target=(int(parts[2]), int(parts[3])))
        await c.answer()
        return
    if action == "cancel":
        data = await state.get_data()
        if data.get("token"):
            data["closed"] = True
            store_session(uid, data)
        await state.clear()
        await ui.edit(c, "Изменения отменены.", ui.back("menu:posts"))
        await c.answer()
        return
    lock = locks.get(uid)
    if lock is None:
        lock = locks[uid] = asyncio.Lock()
    async with lock:
        data = await state.get_data()
        if len(parts) != 3 or data.get("token") != parts[2] or not data.get("payload"):
            raise ValueError("Этот редактор устарел. Перешлите пост заново.")
        if action == "apply" and data.get("unknown_markup"):
            await bot.edit_message_reply_markup(
                chat_id=uid,
                message_id=data["preview_id"],
                reply_markup=ui.kb(
                    [
                        [
                            ui.choice(
                                "💾 Сохранить без исходных кнопок",
                                f"live:confirmed:{data['token']}",
                            )
                        ],
                        [ui.choice("❌ Отмена", "live:cancel")],
                    ]
                ),
            )
            await c.answer(
                "В пересылке нет исходных кнопок, и бот не хранит их. При сохранении они могут исчезнуть. Подтвердите сохранение без них или отмените.",
                show_alert=True,
            )
            return
        if action in {"apply", "confirmed"}:
            await c.answer("Сохраняю…")
            await apply_edit(bot, uid, data)
            await state.clear()
            await bot.edit_message_reply_markup(
                chat_id=uid,
                message_id=data["preview_id"],
                reply_markup=ui.back("menu:posts"),
            )
            await ui.answer(
                c.message,
                "✅ Исходное сообщение в канале обновлено. Новое сообщение не отправлялось.",
                reply_markup=ui.back("menu:posts"),
            )
            return
        if action == "buttons":
            oid = store_session(uid, data)
            await show_button_editor(bot, uid, oid)
            await c.answer()
            return
        if action in {"clearbuttons", "confirmclear"}:
            data.setdefault(
                "original_unknown_markup", data.get("unknown_markup", False)
            )
            data.update(buttons=[], buttons_changed=True, unknown_markup=False)
        if data["payload"]["content_type"] == "video_note" and (
            action in {"text", "media", "cover", "templates", "clean"}
            or action.startswith("template_")
        ):
            raise ValueError("Для круглого видео можно изменить только кнопки.")
        if action == "templates":
            templates = db.all_rows(
                "SELECT id,name FROM templates WHERE owner_id=? ORDER BY id DESC LIMIT 40",
                (uid,),
            )
            rows = [
                [ui.choice(t["name"][:50], f"live:template_{t['id']}:{data['token']}")]
                for t in templates
            ]
            rows.append([ui.choice("⬅️ Назад", f"live:back:{data['token']}")])
            await ui.edit(
                c,
                "Выберите шаблон. Его текст и кнопки добавятся к текущему содержимому."
                if templates
                else "Сначала создайте шаблон в настройках канала.",
                ui.kb(rows),
            )
            await c.answer()
            return
        if action.startswith("template_"):
            tid = int(action.split("_", 1)[1])
            template = db.one(
                "SELECT * FROM templates WHERE id=? AND owner_id=?", (tid, uid)
            )
            if not template:
                raise ValueError("Шаблон не найден.")
            source = dict(
                data["payload"],
                owner_telegram_id=uid,
                buttons_json=json.dumps(data.get("buttons", [])),
                remove_links=False,
            )
            rendered = content.render_payload(
                source, {"id": template["channel_id"], "default_template_id": None}, tid
            )
            key = (
                "entities_json"
                if data["payload"]["content_type"] == "text"
                else "caption_entities_json"
            )
            data["payload"].update(text=rendered["text"], **{key: rendered[key]})
            data.update(buttons=rendered["buttons"], buttons_changed=True)
        elif action == "clean":
            key = (
                "entities_json"
                if data["payload"]["content_type"] == "text"
                else "caption_entities_json"
            )
            text, entities = content.clean_text_links(
                data["payload"].get("text") or "",
                content.entity_list(data["payload"].get(key)),
                uid,
            )
            if not text.strip() and data["payload"]["content_type"] == "text":
                raise ValueError("После удаления ссылок пост пуст.")
            data["payload"].update(text=text, **{key: entities})
        elif action == "undo":
            if not data.get("original_snapshot"):
                raise ValueError("Исходный снимок недоступен. Перешлите пост заново.")
            data.update(json.loads(data["original_snapshot"]), buttons_changed=False)
            if "original_unknown_markup" in data:
                data["unknown_markup"] = data["original_unknown_markup"]
            data["media_changed"] = data["payload"]["content_type"] in MEDIA
        if action in {
            "back",
            "clean",
            "undo",
            "clearbuttons",
            "confirmclear",
        } or action.startswith("template_"):
            await state.set_data(data)
            await preview(bot, uid, state)
            await c.answer()
            return
        if action == "cover":
            if data["payload"]["content_type"] != "video":
                raise ValueError("Обложка доступна только для видео.")
            await state.set_state(PublishedEdit.cover)
            await ui.edit(
                c, "Отправьте фото для новой обложки видео.", ui.back("live:cancel")
            )
            await c.answer()
            return
        if action not in {"text", "media"}:
            raise ValueError("Неизвестное действие.")
        if action == "media" and data["payload"]["content_type"] not in MEDIA:
            raise ValueError("Для этого сообщения замена медиа недоступна.")
        await state.set_state(
            PublishedEdit.text if action == "text" else PublishedEdit.media
        )
        await ui.edit(
            c,
            "Отправьте новый текст с форматированием Telegram. Для пустой подписи отправьте /empty."
            if action == "text"
            else "Пришлите новое фото, видео, GIF, документ или аудио. Текущая подпись сохранится. Для элементов альбома действуют ограничения на тип медиа.",
            ui.kb([[ui.choice("❌ Отмена", "live:cancel")]]),
        )
        await c.answer()


def original_link(m):
    match = re.fullmatch(
        r"https?://t\.me/(?:(c)/([0-9]+)|([A-Za-z0-9_]+))/(?:[0-9]+/)?([0-9]+)(?:\?[^\s]*)?",
        m.text.strip(),
    )
    if not match:
        raise ValueError("Нужна ссылка Telegram на опубликованное сообщение.")
    private, number, username, mid = match.groups()
    channel = db.one(
        "SELECT * FROM channels WHERE owner_telegram_id=? AND is_active=1 AND "
        + ("telegram_chat_id=?" if private else "LOWER(username)=LOWER(?)"),
        (m.from_user.id, int("-100" + number) if private else username),
    )
    if not channel or not accounts.channel_allowed(m.from_user.id, channel["id"]):
        raise ValueError("Группа/канал недоступны.")
    return saved_original(m.from_user.id, channel["id"], int(mid))


def saved_original(uid, cid, mid):
    channel = db.one(
        "SELECT * FROM channels WHERE id=? AND owner_telegram_id=? AND is_active=1",
        (cid, uid),
    )
    if not channel or not accounts.channel_allowed(uid, cid):
        raise ValueError("Группа/канал недоступны.")
    saved = db.one(
        "SELECT pm.content_json,pt.payload_json FROM published_messages pm JOIN post_targets pt ON pt.post_id=pm.post_id AND pt.channel_id=pm.channel_id WHERE pm.channel_id=? AND pm.telegram_message_id=? AND pm.deleted=0",
        (channel["id"], mid),
    )
    if not saved:
        raise ValueError("Нет сохранённой публикации для этого сообщения.")
    previous = db.setting(f"published_edit:{channel['telegram_chat_id']}:{int(mid)}")
    if previous:
        payload = json.loads(previous)["payload"]
    elif saved and (saved["content_json"] or saved["payload_json"]):
        payload = json.loads(saved["content_json"] or saved["payload_json"])
        if payload["content_type"] == "album" and not saved["content_json"]:
            raise ValueError("Для старого альбома перешлите нужный элемент из канала.")
    else:
        raise ValueError("Нет сохранённой публикации для этого сообщения.")
    payload.pop("protect_content", None)
    if payload["content_type"] == "video_note":
        if not saved["content_json"]:
            first = db.one(
                "SELECT MIN(telegram_message_id) FROM published_messages WHERE channel_id=? AND post_id=(SELECT post_id FROM published_messages WHERE channel_id=? AND telegram_message_id=? LIMIT 1)",
                (cid, cid, mid),
            )[0]
            if mid != first:
                payload = content.delivered_payload(payload, 1)
        if payload["content_type"] == "video_note":
            payload = dict(payload, text="", caption_entities_json=None)
    return SimpleNamespace(
        chat=SimpleNamespace(id=channel["telegram_chat_id"]), message_id=int(mid)
    ), payload


@router.message(PublishedEdit.original, ~F.successful_payment, ~F.text.startswith("/"))
async def original(m, state, bot, *, saved_target=None):
    if saved_target is not None:
        origin, payload = saved_original(m.from_user.id, *saved_target)
    elif (
        not m.forward_origin
        and m.text
        and m.text.startswith(("https://t.me/", "http://t.me/"))
    ):
        origin, payload = original_link(m)
    else:
        origin = m.forward_origin
        if not origin or origin.type != "channel":
            raise ValueError(
                "Нужна пересылка из канала с доступным источником, а не копия текста."
            )
        payload = content.message_payload(m)
    await check_access(bot, m.from_user.id, origin.chat.id, origin.message_id)
    if payload["content_type"] not in {"text", "voice", "video_note", *MEDIA}:
        raise ValueError(
            "Это сообщение не поддерживает редактирование текста или медиа."
        )
    markup = m.reply_markup
    known = []
    saved = db.one(
        "SELECT pm.post_id, pm.channel_id, pm.buttons_json, pm.content_json, pt.payload_json FROM published_messages pm JOIN channels ch ON ch.id=pm.channel_id JOIN post_targets pt ON pt.post_id=pm.post_id AND pt.channel_id=pm.channel_id WHERE ch.telegram_chat_id=? AND pm.telegram_message_id=? AND pm.deleted=0",
        (origin.chat.id, origin.message_id),
    )
    if saved and saved["payload_json"]:
        rendered = json.loads(saved["payload_json"])
        if saved["content_json"] or rendered.get("content_type") != "album":
            reactions = {
                r["button_id"]: r["count"]
                for r in db.all_rows(
                    "SELECT button_id,COUNT(*) count FROM post_reactions WHERE post_id=? AND channel_id=? AND telegram_message_id=? GROUP BY button_id",
                    (saved["post_id"], saved["channel_id"], origin.message_id),
                )
            }
            markup = content.build_published_markup(
                content.entity_list(saved["buttons_json"]), saved["post_id"], reactions
            )
            known = [
                dict(b, initial_count=reactions.get(b["id"], 0))
                for b in content.entity_list(saved["buttons_json"])
            ]
    previous = db.setting(f"published_edit:{origin.chat.id}:{origin.message_id}")
    if previous:
        previous = json.loads(previous)
        markup = (
            InlineKeyboardMarkup.model_validate(previous["markup"])
            if previous.get("markup")
            else None
        )
        known = previous.get("buttons") or []
        if previous.get("button_session"):
            known = [
                dict(
                    b,
                    initial_count=b.get("initial_count", 0)
                    + db.one(
                        "SELECT COUNT(*) FROM edited_reactions WHERE owner_id=? AND session_id=? AND button_id=?",
                        (previous["button_owner"], previous["button_session"], b["id"]),
                    )[0],
                )
                if b["type"] == "reaction"
                else b
                for b in known
            ]
    await state.set_data(
        {
            "token": secrets.token_hex(6),
            "chat_id": origin.chat.id,
            "message_id": origin.message_id,
            "payload": payload,
            "markup": markup.model_dump(mode="json", exclude_none=True)
            if markup
            else None,
            "unknown_markup": not saved and not previous and m.reply_markup is None,
            "buttons": import_buttons(markup, known),
            "album": bool(m.media_group_id or payload.get("album")),
        }
    )
    initial = await state.get_data()
    await state.update_data(
        original_snapshot=json.dumps(
            {
                "payload": initial["payload"],
                "buttons": initial["buttons"],
                "markup": initial["markup"],
            },
            ensure_ascii=False,
        )
    )
    await preview(bot, m.from_user.id, state)


@router.message(PublishedEdit.text, F.text, ~F.successful_payment)
async def new_text(m, state, bot):
    if m.text.startswith("/") and m.text != "/empty":
        return
    data = await state.get_data()
    payload = data["payload"]
    text = "" if m.text == "/empty" else m.text
    if not text and payload["content_type"] == "text":
        raise ValueError("Текстовый пост не может быть пустым.")
    if content.utf16len(text) > (4096 if payload["content_type"] == "text" else 1024):
        raise ValueError("Текст превышает лимит Telegram.")
    payload["text"] = text
    payload[
        "entities_json"
        if payload["content_type"] == "text"
        else "caption_entities_json"
    ] = json.dumps(
        [e.model_dump(mode="json", exclude_none=True) for e in (m.entities or [])]
        if text
        else []
    )
    await state.update_data(payload=payload)
    await preview(bot, m.from_user.id, state)


@router.message(PublishedEdit.media, ~F.successful_payment, ~F.text.startswith("/"))
async def new_media(m, state, bot):
    data = await state.get_data()
    incoming = content.message_payload(m)
    payload = data["payload"]
    allowed = (
        {payload["content_type"]}
        if data.get("album") and payload["content_type"] in {"audio", "document"}
        else {"photo", "video"}
        if data.get("album")
        else set(MEDIA)
    )
    if incoming["content_type"] not in allowed:
        raise ValueError(
            "Этот тип медиа несовместим с исходным альбомом или сообщением."
        )
    payload["content_type"] = incoming["content_type"]
    payload["file_id"] = incoming["file_id"]
    payload["cover_file_id"] = incoming.get("cover_file_id")
    await state.update_data(payload=payload, media_changed=True)
    await preview(bot, m.from_user.id, state)


@router.message(PublishedEdit.cover, F.photo)
async def new_cover(m, state, bot):
    data = await state.get_data()
    if data["payload"]["content_type"] != "video":
        raise ValueError("Обложка доступна только для видео.")
    if not accounts.use_daily(m.from_user.id, "cover"):
        raise ValueError("Лимит обложек исчерпан.")
    data["payload"]["cover_file_id"] = m.photo[-1].file_id
    await state.update_data(payload=data["payload"], media_changed=True)
    await preview(bot, m.from_user.id, state)


@router.callback_query(F.data.startswith("lb:"))
async def public_button(c, bot):
    if not c.message:
        raise ValueError("Сообщение недоступно.")
    key = (c.message.chat.id, c.message.message_id)
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    async with lock:
        await public_button_locked(c, bot)


async def public_button_locked(c, bot):
    _, owner, oid, bid = c.data.split(":")
    owner, oid = int(owner), int(oid)
    data = load_session(owner, oid, published=True)
    if (
        not c.message
        or c.message.chat.id != data["chat_id"]
        or c.message.message_id != data["message_id"]
        or not data.get("closed")
    ):
        raise ValueError("Кнопка не принадлежит этой публикации.")
    current = json.loads(
        db.setting(f"published_edit:{data['chat_id']}:{data['message_id']}") or "{}"
    )
    if (current.get("button_owner"), current.get("button_session")) != (owner, oid):
        raise ValueError("Эта кнопка устарела.")
    b = next((b for b in data.get("buttons", []) if b["id"] == bid), None)
    if not b:
        raise ValueError("Кнопка удалена.")
    if b["type"] == "reaction":
        inherit_edited(owner, data)
        selected = toggle(
            "edited_reactions",
            dict(owner_id=owner, session_id=oid),
            bid,
            c.from_user.id,
        )
        try:
            await bot.edit_message_reply_markup(
                chat_id=data["chat_id"],
                message_id=data["message_id"],
                reply_markup=live_markup(owner, data),
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        await c.answer("Реакция учтена" if selected else "Реакция снята")
    elif b["type"] == "subscription":
        if await access.member_ok(
            bot,
            int(b["chat"]) if b["chat"].lstrip("-").isdigit() else b["chat"],
            c.from_user.id,
        ):
            await c.answer(b["alert"][:200], show_alert=True)
        else:
            await c.answer("Подпишитесь на канал и нажмите ещё раз.", show_alert=True)
    elif b["type"] == "alert":
        await c.answer(b["alert"][:200], show_alert=True)
