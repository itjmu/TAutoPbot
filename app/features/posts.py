"""features / posts components."""

import html
import json
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from app import access as access
from app import accounts as accounts
from app import content as content
from app import database as database
from app import preferences
from app import timeutils as timeutils
from app import ui as ui
from app.features import editors as features_editors
from app.features import sources as features_sources
from app.i18n import activate, tr
from app.states import PostCreate

router = Router(name="features.posts")


def post_target_rows(pid):
    return database.all_rows(
        "SELECT c.*,pt.template_id,pt.status target_status,pt.error FROM post_targets pt JOIN channels c ON c.id=pt.channel_id WHERE pt.post_id=? ORDER BY c.id",
        (pid,),
    )


async def show_post(bot, uid, pid, with_preview=True):
    activate(uid)
    p = content.post_owned(pid, uid)
    targets = post_target_rows(pid)
    ids = []
    if with_preview and p["status"] in {"draft", "scheduled"}:
        target = next(
            (r for r in targets if r["id"] == p["preview_target"]),
            targets[0] if targets else None,
        )
        if target:
            payload = content.render_payload(p, target, target["template_id"])
        else:
            payload = content.render_payload(p, {"id": 0, "default_template_id": None})
        try:
            m = await content.send_content(
                bot,
                uid,
                payload,
                content.build_published_markup(payload["buttons"], pid, preview=True),
            )
            ids.extend(x.message_id for x in (m if isinstance(m, list) else [m]))
        except (TelegramBadRequest, ValueError) as exc:
            await bot.send_message(
                uid,
                tr("Предпросмотр не отправлен: ")
                + html.escape(str(exc)[:250])
                + tr(". Отредактируйте пост."),
            )
    labels = {
        "draft": tr("черновик"),
        "scheduled": tr("запланирован"),
        "failed": tr("ошибка"),
        "partial": tr("частично опубликован"),
        "published": tr("опубликован"),
        "skipped": tr("пропущен"),
        "publishing": tr("отправляется"),
        "uncertain": tr("нужна ручная проверка доставки"),
    }
    text = tr(
        "Пост #{v0} • {v1}\nПолучатели: ",
        v0=pid,
        v1=labels.get(p["status"], p["status"]),
    ) + (", ".join(html.escape(t["title"]) for t in targets) or tr("не выбраны"))
    if p["preview_target"] or targets:
        target = next(
            (r for r in targets if r["id"] == p["preview_target"]),
            targets[0] if targets else None,
        )
        if target:
            text += tr("\nПредпросмотр для: ") + html.escape(target["title"])
    if p["delete_after_seconds"]:
        text += (
            tr("\nАвтоудаление через ")
            + str(p["delete_after_seconds"] // 60)
            + tr(" мин после публикации.")
        )
    sched = database.one(
        "SELECT publish_at FROM scheduled_posts WHERE post_id=? AND active=1", (pid,)
    )
    if sched:
        text += tr("\nВремя: ") + preferences.display(uid, sched["publish_at"])
    if p["status"] in {"failed", "partial", "uncertain"}:
        text += "\n" + "\n".join(
            html.escape(f"{r['title']}: {r['target_status']} {r['error'] or ''}")[:220]
            for r in targets
        )
    control = await bot.send_message(
        uid, text[:3900], reply_markup=ui.post_controls(pid, p["status"])
    )
    ids.append(control.message_id)
    database.execute(
        "UPDATE posts SET preview_ids=? WHERE id=?", (json.dumps(ids), pid)
    )
    # Remove previous preview only after the replacement was delivered.
    for old in content.entity_list(p["preview_ids"]):
        try:
            await bot.delete_message(uid, old)
        except TelegramBadRequest:
            pass


@router.callback_query(F.data == "post:create")
@router.callback_query(F.data.startswith("channel:post:"))
async def post_create(c: CallbackQuery, state: FSMContext):
    await state.clear()
    if c.data.startswith("channel:post:"):
        cid = int(c.data.split(":")[2])
        if not accounts.channel_allowed(c.from_user.id, cid):
            raise ValueError(tr("Канал недоступен по текущему тарифу."))
        await state.update_data(initial_target=cid)
    await state.set_state(PostCreate.content)
    await ui.edit(
        c,
        tr(
            "Отправьте текст или перешлите пост с фото, видео, GIF, документом, аудио или voice.\nАльбом будет собран в один черновик через несколько секунд.\n/cancel — отмена"
        ),
        ui.back("menu:posts"),
    )
    await c.answer()


@router.callback_query(F.data.startswith("p:"))
async def post_action(c: CallbackQuery, state: FSMContext, bot: Bot):
    parts = c.data.split(":")
    pid = int(parts[1])
    action = parts[2]
    uid = c.from_user.id
    p = content.post_owned(pid, uid)
    await state.clear()
    await state.update_data(pid=pid)
    await state.set_state(PostCreate.idle)
    if action == "preview":
        if not post_target_rows(pid):
            raise ValueError(tr("Выберите хотя бы один канал."))
        await c.answer()
        await show_post(bot, uid, pid)
        return
    if action in {"review", "checked"}:
        if p["status"] != "uncertain":
            raise ValueError(tr("Этот пост не требует проверки доставки."))
        if action == "checked":
            cid = int(parts[3])
            verdict = parts[4]
            if verdict not in {"sent", "failed"}:
                raise ValueError(tr("Неверный результат."))
            changed = database.execute(
                "UPDATE post_targets SET status=?,error=? WHERE post_id=? AND channel_id=? AND status='uncertain'",
                (verdict, tr("Доставку проверил владелец вручную"), pid, cid),
            ).rowcount
            if not changed:
                raise ValueError(tr("Этот получатель уже проверен."))
            states = [
                r["status"]
                for r in database.all_rows(
                    "SELECT status FROM post_targets WHERE post_id=?", (pid,)
                )
            ]
            result = (
                "uncertain"
                if "uncertain" in states
                else (
                    "published"
                    if all(x == "sent" for x in states)
                    else ("partial" if "sent" in states else "failed")
                )
            )
            database.execute("UPDATE posts SET status=? WHERE id=?", (result, pid))
            database.log_event(
                uid, "DELIVERY_RECONCILED", f"post={pid};channel={cid};{verdict}"
            )
            await c.answer()
            await show_post(bot, uid, pid, False)
            return
        rows = []
        for target in post_target_rows(pid):
            if target["target_status"] == "uncertain":
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=target["title"][:20] + tr(": есть"),
                            callback_data=f"p:{pid}:checked:{target['id']}:sent",
                        ),
                        InlineKeyboardButton(
                            text=tr("Нет — разрешить повтор"),
                            callback_data=f"p:{pid}:checked:{target['id']}:failed",
                        ),
                    ]
                )
        await ui.edit(
            c,
            tr(
                "Откройте каждый канал и проверьте пост (для альбома — все элементы и кнопки). «Нет» разрешает повторную отправку и может создать дубликат, если часть поста уже доставлена. Для подтверждённой вручную доставки без известного ID автоудаление невозможно."
            ),
            ui.kb(rows),
        )
        await c.answer()
        return
    if action in {"publish", "retry"}:
        if p["status"] not in {"draft", "scheduled", "failed", "partial"}:
            raise ValueError(tr("Этот пост уже отправляется или обработан."))
        if not post_target_rows(pid):
            raise ValueError(tr("Выберите канал/группу."))
        # Publish-now ignores and cancels any prior schedule, by design.
        await c.answer(tr("Начинаю публикацию."))
        await execute_publish(pid, bot)
        await show_post(bot, uid, pid, False)
        return
    content.mutable_post(pid, uid)
    if action == "skip":
        with database.atomic():
            database.db.execute("UPDATE posts SET status='skipped' WHERE id=?", (pid,))
            database.db.execute(
                "UPDATE scheduled_posts SET active=0 WHERE post_id=?", (pid,)
            )
        await c.answer(tr("Пост пропущен."))
        await show_post(bot, uid, pid, False)
        return
    # Entering any editor cancels the old schedule visibly, preventing publication mid-edit.
    if (
        action
        in {
            "edit",
            "targets",
            "toggle",
            "links",
            "templates",
            "template",
            "text",
            "replace",
            "cover",
            "buttons",
            "clearbuttons",
            "addbutton",
            "delete",
            "delafter",
            "delcustom",
            "time",
        }
        and p["status"] == "scheduled"
    ):
        with database.atomic():
            database.db.execute(
                "UPDATE scheduled_posts SET active=0 WHERE post_id=?", (pid,)
            )
            database.db.execute("UPDATE posts SET status='draft' WHERE id=?", (pid,))
        await c.message.answer(
            tr(
                "Расписание снято на время редактирования. После изменений выберите время заново."
            )
        )
    if action == "targets":
        await ui.target_picker(c, pid)
    elif action == "toggle":
        cid = int(parts[3])
        if not accounts.channel_allowed(uid, cid):
            raise ValueError(tr("Этот канал недоступен."))
        if database.one(
            "SELECT 1 FROM post_targets WHERE post_id=? AND channel_id=?", (pid, cid)
        ):
            database.execute(
                "DELETE FROM post_targets WHERE post_id=? AND channel_id=?", (pid, cid)
            )
        else:
            database.execute(
                "INSERT INTO post_targets(post_id,channel_id) VALUES(?,?)", (pid, cid)
            )
        await ui.target_picker(c, pid)
    elif action == "links":
        await c.answer(tr("Чужие ссылки удаляются автоматически."))
        return
    elif action == "edit":
        await ui.edit(
            c,
            tr("✏️ Изменения сохраняются в этом черновике."),
            ui.kb(
                [
                    *(
                        [
                            [
                                InlineKeyboardButton(
                                    text=tr("🖼 Обложка видео"),
                                    callback_data=f"p:{pid}:cover",
                                )
                            ]
                        ]
                        if p["content_type"] == "video"
                        or (
                            p["content_type"] == "album"
                            and any(
                                i["content_type"] == "video"
                                for i in content.entity_list(p["media_json"])
                            )
                        )
                        else []
                    ),
                    [
                        InlineKeyboardButton(
                            text=tr("📝 Описание"), callback_data=f"p:{pid}:text"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=tr("🖼 Заменить сообщение"),
                            callback_data=f"p:{pid}:replace",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=tr("🔘 Кнопки"), callback_data=f"p:{pid}:buttons"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=tr("⬅️ Предпросмотр"), callback_data=f"p:{pid}:preview"
                        )
                    ],
                ]
            ),
        )
    elif action == "cover":
        items = (
            content.entity_list(p["media_json"]) if p["content_type"] == "album" else []
        )
        if items and len(parts) == 3:
            rows = [
                [ui.choice(tr("Видео {v0}", v0=i + 1), f"p:{pid}:cover:{i}")]
                for i, item in enumerate(items)
                if item["content_type"] == "video"
            ]
            if not rows:
                raise ValueError(tr("В альбоме нет видео."))
            await ui.edit(c, tr("Выберите видео для обложки."), ui.kb(rows))
        else:
            index = int(parts[3]) if items else None
            if items and (
                not 0 <= index < len(items) or items[index]["content_type"] != "video"
            ):
                raise ValueError(tr("Видео не найдено."))
            if not items and p["content_type"] != "video":
                raise ValueError(tr("Обложка доступна только для видео."))
            await state.update_data(cover_index=index)
            await state.set_state(PostCreate.cover)
            await ui.edit(
                c,
                tr(
                    "Пришлите фото для обложки видео. Отправьте - для удаления своей обложки."
                ),
                ui.back(f"p:{pid}:edit"),
            )
    elif action == "delete":
        rows = [
            [
                ui.choice(label, f"p:{pid}:delafter:{seconds}")
                for label, seconds in [
                    (tr("1 ч"), 3600),
                    (tr("6 ч"), 21600),
                    (tr("24 ч"), 86400),
                ]
            ]
        ]
        rows += [
            [
                ui.choice(tr("47 ч"), f"p:{pid}:delafter:169200"),
                ui.choice(tr("Не удалять"), f"p:{pid}:delafter:0"),
            ],
            [ui.choice(tr("Другой интервал"), f"p:{pid}:delcustom")],
            [ui.choice(tr("Назад"), f"p:{pid}:preview")],
        ]
        await ui.edit(
            c,
            tr(
                "Когда удалить пост после публикации? Боту нужно право удалять сообщения."
            ),
            ui.kb(rows),
        )
    elif action == "delcustom":
        await state.set_state(PostCreate.delete)
        await ui.edit(
            c,
            tr(
                "Введите интервал после публикации: 30 мин, 6 ч или 1 д. Максимум 47 часов. 0 — не удалять."
            ),
            ui.back(f"p:{pid}:delete"),
        )
    elif action == "delafter":
        seconds = int(parts[3])
        if not 0 <= seconds <= 169200:
            raise ValueError(tr("Максимум 47 часов."))
        database.execute(
            "UPDATE posts SET delete_after_seconds=? WHERE id=?", (seconds, pid)
        )
        content.reset_snapshot(pid)
        await show_post(bot, uid, pid, False)
    elif action in {"text", "replace"}:
        await state.set_state(
            {
                "text": PostCreate.edit_text,
                "replace": PostCreate.edit_content,
                "delete": PostCreate.delete,
            }[action]
        )
        prompt = {
            "text": tr(
                "Отправьте новое описание с форматированием. Символ - удаляет описание."
            ),
            "replace": tr(
                "Пришлите новое сообщение или медиа. Кнопки, получатели и удаление ссылок сохранятся."
            ),
            "delete": tr(
                "Через сколько минут удалить опубликованные сообщения? 0 — не удалять; максимум 2879 (ограничение Telegram: менее 48 часов)."
            ),
        }[action]
        await ui.edit(c, prompt, ui.back(f"p:{pid}:preview"))
    elif action in {"buttons", "clearbuttons", "addbutton"}:
        await features_editors.button_panel(c, "p", pid)
    elif action == "time":
        rows = [
            [
                ui.choice(label, f"p:{pid}:after:{seconds}")
                for label, seconds in [
                    (tr("Через 5 мин"), 300),
                    (tr("Через 30 мин"), 1800),
                    (tr("Через 1 ч"), 3600),
                ]
            ]
        ]
        rows += [
            [ui.choice(label, f"p:{pid}:day:{day}")]
            for label, day in [
                (tr("Сегодня"), 0),
                (tr("Завтра"), 1),
                (tr("Через неделю"), 7),
            ]
        ]
        rows += [
            [ui.choice(tr("Указать дату и время"), f"p:{pid}:at:custom")],
            [ui.choice(tr("Назад"), f"p:{pid}:preview")],
        ]
        await ui.edit(
            c,
            tr("Когда опубликовать?\nСейчас: ")
            + preferences.display(uid, timeutils.now())
            + tr("\nПеред сохранением вы увидите точную дату."),
            ui.kb(rows),
        )
    elif action == "day":
        day = int(parts[3])
        if day not in {0, 1, 7}:
            raise ValueError(tr("Выберите дату кнопкой."))
        await ui.edit(
            c,
            tr("Выберите время. Часовой пояс: ")
            + preferences.get_preferences(uid)["timezone"],
            ui.kb(
                [
                    [
                        ui.choice(t, f"p:{pid}:pick:{day}:{t}")
                        for t in ["09:00", "12:00", "15:00"]
                    ],
                    [
                        ui.choice(t, f"p:{pid}:pick:{day}:{t}")
                        for t in ["18:00", "20:00", "22:00"]
                    ],
                    [ui.choice(tr("Своя дата"), f"p:{pid}:at:custom")],
                ]
            ),
        )
    elif action in {"after", "pick", "confirm"}:
        if action == "after":
            seconds = int(parts[3])
            if seconds not in {300, 1800, 3600}:
                raise ValueError(tr("Выберите интервал кнопкой."))
            dt = timeutils.now() + timedelta(seconds=seconds)
        elif action == "pick":
            day = int(parts[3])
            hour = int(parts[4])
            minute = int(parts[5])
            if day not in {0, 1, 7}:
                raise ValueError(tr("Выберите дату кнопкой."))
            naive = (preferences.local_now(uid) + timedelta(days=day)).replace(
                hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None
            )
            dt = preferences.to_utc(uid, naive)
        else:
            dt = datetime.fromtimestamp(int(parts[3]), tz=timezone.utc)
        if dt <= timeutils.now():
            raise ValueError(tr("Это время уже прошло. Выберите более позднее время."))
        if action == "confirm":
            schedule_post(pid, uid, dt)
            await ui.edit(
                c,
                tr("✅ Публикация запланирована: ") + preferences.display(uid, dt),
                ui.back("post:scheduled"),
            )
        else:
            await ui.edit(
                c,
                tr("Подтвердите публикацию: ") + preferences.display(uid, dt),
                ui.kb(
                    [
                        [
                            ui.choice(
                                tr("✅ Запланировать"),
                                f"p:{pid}:confirm:{int(dt.timestamp())}",
                            )
                        ],
                        [ui.choice(tr("Изменить время"), f"p:{pid}:time")],
                    ]
                ),
            )
    elif action == "at":
        value = ":".join(parts[3:])
        if value == "custom":
            await state.set_state(PostCreate.schedule)
            await ui.edit(
                c,
                tr(
                    "Введите дату и время: 25.12.2026 18:30. Или только 18:30.\nВаш часовой пояс: "
                )
                + preferences.get_preferences(uid)["timezone"],
                ui.back(f"p:{pid}:time"),
            )
        else:
            schedule_post(pid, uid, preferences.parse_local(uid, value))
            await c.answer(tr("Расписание сохранено."))
            await show_post(bot, uid, pid, False)
            return
    elif action == "templates":
        rows = [
            [
                InlineKeyboardButton(
                    text=r["title"][:35], callback_data=f"p:{pid}:template:{r['id']}"
                )
            ]
            for r in post_target_rows(pid)
        ]
        rows.append(
            [
                InlineKeyboardButton(
                    text=tr("⬅️ Предпросмотр"), callback_data=f"p:{pid}:preview"
                )
            ]
        )
        await ui.edit(
            c,
            tr("Выберите получателя, чтобы задать шаблон и показать его предпросмотр."),
            ui.kb(rows),
        )
    elif action == "template":
        cid = int(parts[3])
        target = database.one(
            "SELECT 1 FROM post_targets WHERE post_id=? AND channel_id=?", (pid, cid)
        )
        if not target or not accounts.channel_allowed(uid, cid):
            raise ValueError(tr("Получатель недоступен."))
        if len(parts) == 5:
            tid = int(parts[4])
            if tid > 0 and not database.one(
                "SELECT 1 FROM templates WHERE id=? AND channel_id=? AND owner_id=?",
                (tid, cid, uid),
            ):
                raise ValueError(tr("Шаблон недоступен."))
            database.execute(
                "UPDATE post_targets SET template_id=?,payload_json=NULL WHERE post_id=? AND channel_id=?",
                (None if tid == 0 else tid, pid, cid),
            )
            database.execute("UPDATE posts SET preview_target=? WHERE id=?", (cid, pid))
            await c.answer()
            await show_post(bot, uid, pid)
            return
        templates = database.all_rows(
            "SELECT * FROM templates WHERE channel_id=? AND owner_id=?", (cid, uid)
        )
        rows = [
            [
                InlineKeyboardButton(
                    text=tr("По умолчанию"), callback_data=f"p:{pid}:template:{cid}:0"
                )
            ],
            [
                InlineKeyboardButton(
                    text=tr("Без шаблона"), callback_data=f"p:{pid}:template:{cid}:-1"
                )
            ],
        ]
        rows += [
            [
                InlineKeyboardButton(
                    text=r["name"][:40],
                    callback_data=f"p:{pid}:template:{cid}:{r['id']}",
                )
            ]
            for r in templates
        ]
        await ui.edit(c, tr("Шаблон для этого получателя:"), ui.kb(rows))
    else:
        raise ValueError(tr("Неизвестное действие."))
    await c.answer()


@router.message(PostCreate.edit_text, F.text)
@router.message(PostCreate.edit_content, ~F.successful_payment, ~F.text.startswith("/"))
@router.message(PostCreate.cover, ~F.successful_payment, ~F.text.startswith("/"))
@router.message(PostCreate.buttons, F.text)
@router.message(PostCreate.delete, F.text)
@router.message(PostCreate.schedule, F.text)
async def edit_post_value(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    pid = int(data["pid"])
    p = content.mutable_post(pid, m.from_user.id)
    stage = await state.get_state()
    if stage == PostCreate.edit_text.state:
        text = "" if m.text.strip() == "-" else m.text
        limit = 4096 if p["content_type"] == "text" else 1024
        if content.utf16len(text) > limit:
            raise ValueError(tr("Максимум {v0} символов UTF-16.", v0=limit))
        key = (
            "entities_json" if p["content_type"] == "text" else "caption_entities_json"
        )
        ents = (
            json.dumps(
                [
                    e.model_dump(mode="json", exclude_none=True)
                    for e in (m.entities or [])
                ]
            )
            if text
            else "[]"
        )
        database.execute(
            f"UPDATE posts SET text=?,{key}=? WHERE id=?", (text, ents, pid)
        )
    elif stage == PostCreate.cover.state:
        if m.photo:
            cover = m.photo[-1].file_id
        elif m.text and m.text.strip() == "-":
            cover = None
        else:
            raise ValueError(
                tr("Отправьте изображение как фото или - для удаления обложки.")
            )
        index = data.get("cover_index")
        if p["content_type"] == "album":
            items = content.entity_list(p["media_json"])
            if (
                index is None
                or not 0 <= index < len(items)
                or items[index]["content_type"] != "video"
            ):
                raise ValueError(tr("Видео не найдено."))
        elif p["content_type"] != "video" or index is not None:
            raise ValueError(tr("Видео изменилось. Откройте редактор заново."))
        if not accounts.use_daily(m.from_user.id, "cover"):
            raise ValueError(
                tr(
                    "Дневной лимит замены обложек исчерпан. Лимит обновится в 00:00 UTC."
                )
            )
        if p["content_type"] == "video" and index is None:
            database.execute(
                "UPDATE posts SET cover_file_id=? WHERE id=?", (cover, pid)
            )
        elif p["content_type"] == "album" and index is not None:
            items = content.entity_list(p["media_json"])
            if not 0 <= index < len(items) or items[index]["content_type"] != "video":
                raise ValueError(tr("Видео не найдено."))
            items[index]["cover_file_id"] = cover
            database.execute(
                "UPDATE posts SET media_json=? WHERE id=?",
                (json.dumps(items, ensure_ascii=False), pid),
            )
        else:
            raise ValueError(tr("Видео изменилось. Откройте редактор заново."))
    elif stage == PostCreate.edit_content.state:
        if m.media_group_id:
            raise ValueError(
                tr(
                    "Для замены альбомом создайте новый пост и перешлите альбом целиком."
                )
            )
        payload = content.message_payload(m)
        payload.pop("buttons_json", None)
        database.execute(
            "UPDATE posts SET " + ",".join(k + "=?" for k in payload) + " WHERE id=?",
            (*payload.values(), pid),
        )
    elif stage == PostCreate.buttons.state:
        buttons = content.entity_list(p["buttons_json"])
        b = content.parse_button(
            m.text, data["button_type"], max([b["row"] for b in buttons], default=0) + 1
        )
        if b["type"] == "subscription":
            chat = await bot.get_chat(
                int(b["chat"]) if b["chat"].lstrip("-").isdigit() else b["chat"]
            )
            me = await bot.get_me()
            member = await bot.get_chat_member(chat.id, me.id)
            if member.status not in {
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            }:
                raise ValueError(
                    tr(
                        "Для проверки подписки бот должен быть администратором указанного канала."
                    )
                )
            b["chat_label"] = (
                ("@" + chat.username) if chat.username else (chat.title or str(chat.id))
            )
            b["chat"] = str(chat.id)
        buttons.append(b)
        content.validate_buttons(buttons)
        database.execute(
            "UPDATE posts SET buttons_json=? WHERE id=?",
            (json.dumps(buttons, ensure_ascii=False), pid),
        )
    elif stage == PostCreate.delete.state:
        seconds = 0 if m.text.strip() == "0" else preferences.duration(m.text, 169200)
        database.execute(
            "UPDATE posts SET delete_after_seconds=? WHERE id=?", (seconds, pid)
        )
    elif stage == PostCreate.schedule.state:
        dt = preferences.parse_local(m.from_user.id, m.text)
        if dt <= timeutils.now():
            raise ValueError(tr("Это время уже прошло."))
        await m.answer(
            tr("Подтвердите публикацию: ") + preferences.display(m.from_user.id, dt),
            reply_markup=ui.kb(
                [
                    [
                        ui.choice(
                            tr("✅ Запланировать"),
                            f"p:{pid}:confirm:{int(dt.timestamp())}",
                        )
                    ],
                    [ui.choice(tr("Изменить время"), f"p:{pid}:time")],
                ]
            ),
        )
        await state.set_state(PostCreate.idle)
        return
    if stage != PostCreate.schedule.state:
        content.reset_snapshot(pid)
    await state.set_state(PostCreate.idle)
    await show_post(bot, m.from_user.id, pid)


def next_occurrence(hhmm):
    h, m = map(int, hhmm.split(":"))
    dt = timeutils.now().replace(hour=h, minute=m, second=0, microsecond=0)
    return dt if dt > timeutils.now() else dt + timedelta(days=1)


def schedule_post(pid, uid, dt):
    p = content.mutable_post(pid, uid)
    if dt <= timeutils.now():
        raise ValueError(tr("Время должно быть в будущем."))
    targets = post_target_rows(pid)
    if not targets:
        raise ValueError(tr("Сначала выберите получателей."))
    payloads = []
    for ch in targets:
        if not accounts.channel_allowed(uid, ch["id"]):
            raise ValueError(
                tr("Один из выбранных каналов недоступен по текущему тарифу.")
            )
        payloads.append(
            (
                json.dumps(
                    content.render_payload(p, ch, ch["template_id"]), ensure_ascii=False
                ),
                pid,
                ch["id"],
            )
        )
    with database.atomic():
        database.db.execute(
            "UPDATE scheduled_posts SET active=0 WHERE post_id=?", (pid,)
        )
        database.db.execute(
            "INSERT INTO scheduled_posts(post_id,publish_at,delete_after_seconds,created_at) VALUES(?,?,?,?)",
            (pid, timeutils.iso(dt), p["delete_after_seconds"], timeutils.iso()),
        )
        database.db.executemany(
            "UPDATE post_targets SET payload_json=? WHERE post_id=? AND channel_id=?",
            payloads,
        )
        database.db.execute("UPDATE posts SET status='scheduled' WHERE id=?", (pid,))


async def execute_publish(pid, bot):
    with database.atomic():
        claimed = database.db.execute(
            "UPDATE posts SET status='publishing' WHERE id=? AND status IN ('draft','scheduled','failed','partial')",
            (pid,),
        ).rowcount
        if not claimed:
            return
        database.db.execute(
            "UPDATE scheduled_posts SET active=0 WHERE post_id=?", (pid,)
        )
    p = database.one("SELECT * FROM posts WHERE id=?", (pid,))
    activate(p["owner_telegram_id"])
    targets = post_target_rows(pid)
    for ch in targets:
        delivery = database.one(
            "SELECT * FROM post_targets WHERE post_id=? AND channel_id=?",
            (pid, ch["id"]),
        )
        if delivery["status"] in {"sent", "uncertain", "sending"}:
            continue
        attempted = False
        try:
            if accounts.blocked(p["owner_telegram_id"]) or not accounts.channel_allowed(
                p["owner_telegram_id"], ch["id"]
            ):
                raise ValueError(
                    tr("Владелец заблокирован или канал недоступен по тарифу.")
                )
            if not await access.owner_and_bot_ok(
                bot, ch["telegram_chat_id"], p["owner_telegram_id"]
            ):
                raise ValueError(tr("Нет прав владельца или бота на публикацию."))
            d = (
                json.loads(delivery["payload_json"])
                if delivery["payload_json"]
                else content.render_payload(p, ch, ch["template_id"])
            )
            database.execute(
                "UPDATE post_targets SET status='sending',payload_json=?,error=NULL WHERE post_id=? AND channel_id=?",
                (json.dumps(d, ensure_ascii=False), pid, ch["id"]),
            )
            attempted = True
            msg = await content.send_content(
                bot,
                ch["telegram_chat_id"],
                d,
                content.build_published_markup(d["buttons"], pid),
            )
            messages = msg if isinstance(msg, list) else [msg]
            with database.atomic():
                for sent in messages:
                    database.db.execute(
                        "INSERT INTO published_messages(post_id,channel_id,telegram_message_id,delete_at,buttons_json) VALUES(?,?,?,?,?)",
                        (
                            pid,
                            ch["id"],
                            sent.message_id,
                            timeutils.iso(
                                timeutils.now()
                                + timedelta(seconds=p["delete_after_seconds"])
                            )
                            if p["delete_after_seconds"]
                            else None,
                            json.dumps(d["buttons"]),
                        ),
                    )
                database.db.execute(
                    "UPDATE post_targets SET status='sent',error=NULL WHERE post_id=? AND channel_id=?",
                    (pid, ch["id"]),
                )
        except Exception as exc:
            if isinstance(exc, content.PartialAlbumError):
                with database.atomic():
                    for sent in exc.messages:
                        database.db.execute(
                            "INSERT INTO published_messages(post_id,channel_id,telegram_message_id,delete_at,buttons_json) VALUES(?,?,?,?,?)",
                            (
                                pid,
                                ch["id"],
                                sent.message_id,
                                timeutils.iso(
                                    timeutils.now()
                                    + timedelta(seconds=p["delete_after_seconds"])
                                )
                                if p["delete_after_seconds"]
                                else None,
                                "[]",
                            ),
                        )
            # Network failures after send can mean Telegram accepted it. Never auto-retry those.
            uncertain = attempted and not isinstance(
                exc,
                (
                    TelegramBadRequest,
                    TelegramForbiddenError,
                    TelegramRetryAfter,
                    ValueError,
                ),
            )
            database.execute(
                "UPDATE post_targets SET status=?,error=? WHERE post_id=? AND channel_id=?",
                ("uncertain" if uncertain else "failed", str(exc)[:250], pid, ch["id"]),
            )
            database.log_event(
                p["owner_telegram_id"],
                "POST_FAILED",
                f"post={pid};channel={ch['id']};{exc}",
            )
    statuses = [
        r["status"]
        for r in database.all_rows(
            "SELECT status FROM post_targets WHERE post_id=?", (pid,)
        )
    ]
    result = (
        "uncertain"
        if any(s in {"uncertain", "sending"} for s in statuses)
        else (
            "published"
            if statuses and all(s == "sent" for s in statuses)
            else ("partial" if "sent" in statuses else "failed")
        )
    )
    database.execute("UPDATE posts SET status=? WHERE id=?", (result, pid))
    await accounts.check_referral(p["owner_telegram_id"], bot)
    return result


@router.callback_query(F.data == "demo")
async def demo_button(c: CallbackQuery):
    await c.answer(
        tr("Это предпросмотр. Кнопка заработает в опубликованном посте."),
        show_alert=True,
    )


@router.callback_query(F.data.startswith("action:"))
@router.callback_query(F.data.startswith("react:"))
@router.callback_query(F.data.startswith("sub:"))
@router.callback_query(F.data.startswith("alert:"))
async def published_action(c: CallbackQuery, bot: Bot):
    _, pid, bid = c.data.split(":")
    pid = int(pid)
    if not c.message:
        return
    delivery = database.one(
        "SELECT m.* FROM published_messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.post_id=? AND ch.telegram_chat_id=? AND m.telegram_message_id=?",
        (pid, c.message.chat.id, c.message.message_id),
    )
    if not delivery:
        raise ValueError(tr("Кнопка не принадлежит этой публикации."))
    buttons = content.entity_list(delivery["buttons_json"])
    b = next((x for x in buttons if x["id"] == bid), None)
    if not b:
        raise ValueError(tr("Кнопка не найдена."))
    if b["type"] == "reaction":
        database.execute(
            "INSERT OR IGNORE INTO post_reactions(post_id,channel_id,telegram_message_id,button_id,user_id,created_at) VALUES(?,?,?,?,?,?)",
            (
                pid,
                delivery["channel_id"],
                c.message.message_id,
                bid,
                c.from_user.id,
                timeutils.iso(),
            ),
        )
        counts = {
            r["button_id"]: r["n"]
            for r in database.all_rows(
                "SELECT button_id,COUNT(*) n FROM post_reactions WHERE post_id=? AND channel_id=? AND telegram_message_id=? GROUP BY button_id",
                (pid, delivery["channel_id"], c.message.message_id),
            )
        }
        await c.answer(tr("Реакций: {v0}", v0=counts.get(bid, 0)))
        try:
            await c.message.edit_reply_markup(
                reply_markup=content.build_published_markup(buttons, pid, counts)
            )
        except TelegramBadRequest:
            pass
    elif b["type"] == "subscription":
        chat = int(b["chat"]) if b["chat"].lstrip("-").isdigit() else b["chat"]
        if await access.member_ok(bot, chat, c.from_user.id):
            await c.answer(b["alert"][:200], show_alert=True)
        else:
            await c.answer(
                (
                    tr("Подпишитесь: ")
                    + b.get("chat_label", b["chat"])
                    + tr("; затем нажмите снова.")
                )[:200],
                show_alert=True,
            )
    elif b["type"] == "alert":
        await c.answer(b["alert"][:200], show_alert=True)


@router.callback_query(F.data == "menu:posts")
async def menu_posts(c: CallbackQuery):
    await ui.edit(
        c,
        tr("📢 Автопостинг\nСоздайте пост или посмотрите отложенные публикации."),
        ui.kb(
            [
                [
                    ui.choice(tr("➕ Создать пост"), "post:create"),
                    ui.choice(tr("🕐 Отложенные"), "post:scheduled"),
                ],
                [ui.choice(tr("⬅️ Главное меню"), "menu:main")],
            ]
        ),
    )
    await c.answer()


@router.message(PostCreate.content, ~F.successful_payment, ~F.text.startswith("/"))
async def receive_post(m: Message, state: FSMContext, bot: Bot):
    payload = content.message_payload(m)
    data = await state.get_data()
    target = data.get("initial_target")
    if m.media_group_id:
        features_sources.queue_album(m, m.from_user.id, [target] if target else [])
        return
    if not accounts.use_daily(m.from_user.id, "post_create"):
        raise ValueError(tr("Лимит Free: 3 новых поста в день."))
    pid = content.create_draft(m.from_user.id, payload, [target] if target else [])
    await state.update_data(pid=pid)
    await state.set_state(PostCreate.idle)
    await ui.send_target_picker(bot, m.from_user.id, pid)


@router.callback_query(F.data == "post:scheduled")
async def post_list(c: CallbackQuery):
    rows = database.all_rows(
        "SELECT p.*,s.publish_at FROM posts p JOIN scheduled_posts s ON s.post_id=p.id WHERE p.owner_telegram_id=? AND p.status='scheduled' AND s.active=1 ORDER BY s.publish_at LIMIT 50",
        (c.from_user.id,),
    )
    buttons = ui.button_grid(
        [
            ui.choice(
                f"#{r['id']} • {preferences.display(c.from_user.id, r['publish_at'])}",
                f"p:{r['id']}:preview",
            )
            for r in rows
        ],
        2,
    )
    buttons.append([ui.choice(tr("⬅️ Автопостинг"), "menu:posts")])
    await ui.edit(
        c,
        tr("🕐 Запланированные публикации") + (tr(" — пока пусто") if not rows else ""),
        ui.kb(buttons),
    )
    await c.answer()
