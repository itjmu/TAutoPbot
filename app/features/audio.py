"""Audio intake and compatible tools for previously created audio drafts."""

import html
import json
from pathlib import PurePath

from aiogram import F, Router
from aiogram.types import Message

from app import accounts, content, ui
from app import database as db
from app.features import posts
from app.i18n import tr
from app.states import AudioEdit, PostCreate

router = Router(name="features.audio")
FORMATS = {"mp3", "m4a", "ogg", "oga", "opus", "wav", "flac", "aac", "wma", "aiff"}


def metadata(payload):
    value = payload.get("audio_meta_json") or "{}"
    return json.loads(value) if isinstance(value, str) else dict(value)


def labels():
    return {
        "title": tr("🎵 Название трека"),
        "performer": tr("👤 Исполнитель"),
        "album": tr("💿 Альбом"),
        "year": tr("📅 Год"),
        "genre": tr("🎼 Жанр"),
        "filename": tr("📄 Имя файла"),
    }


def fields(post, options):
    if post["content_type"] == "voice":
        return set()
    if post["content_type"] == "audio" and options.get("format") in {"mp3", "m4a"}:
        return set(labels()) | {"cover"}
    return {"filename"}


def audio_payload(message):
    payload = content.message_payload(message)
    obj = message.audio or message.voice or message.document
    if not obj or message.media_group_id:
        raise ValueError(tr("Отправьте один аудиотрек, голосовое или аудиофайл."))
    filename = getattr(obj, "file_name", None) or ""
    extension = PurePath(filename).suffix.lower().lstrip(".")
    if message.document and extension not in FORMATS:
        raise ValueError(
            tr("Нужен аудиофайл: MP3, M4A, OGG, OPUS, WAV, FLAC, AAC, WMA или AIFF.")
        )
    if not extension:
        extension = {"audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/ogg": "ogg"}.get(
            getattr(obj, "mime_type", None), "ogg" if message.voice else "mp3"
        )
    options = dict(
        editor=True,
        format=extension,
        source_type=payload["content_type"],
        filename=filename or f"{'voice' if message.voice else 'audio'}.{extension}",
        title=getattr(obj, "title", None),
        performer=getattr(obj, "performer", None),
        changes={},
    )
    if message.document and extension in {"mp3", "m4a"}:
        payload["content_type"] = "audio"
        options["process"] = True  # Telegram file IDs cannot change their media type.
    payload["audio_meta_json"] = json.dumps(options, ensure_ascii=False)
    return payload


@router.callback_query(F.data == "menu:audio")
@router.callback_query(F.data.startswith("audio:page:"))
async def menu(c, state):
    from app.features.audio_utility import start

    await start(c, state)


@router.callback_query(F.data.startswith("audio:target:"))
async def target(c, state):
    cid = int(c.data.split(":")[-1])
    if not accounts.channel_allowed(c.from_user.id, cid):
        raise ValueError(tr("Получатель недоступен."))
    await state.clear()
    await state.update_data(audio_target=cid)
    await state.set_state(AudioEdit.content)
    await ui.edit(
        c,
        tr(
            "Отправьте аудиотрек, голосовое или аудиофайл. Инструменты появятся после загрузки."
        ),
        ui.back("menu:audio"),
    )
    await c.answer()


@router.message(AudioEdit.content, ~F.successful_payment, ~F.text.startswith("/"))
async def receive(message: Message, state, bot):
    data = await state.get_data()
    if data.get("audio_utility"):
        from app.features.audio_utility import receive as utility_receive

        await utility_receive(message, state, bot)
        return
    payload = audio_payload(message)
    pid = data.get("audio_pid")
    if pid:
        content.mutable_post(pid, message.from_user.id)
        payload.pop("buttons_json", None)
        db.execute(
            "UPDATE posts SET "
            + ",".join(key + "=?" for key in payload)
            + " WHERE id=?",
            (*payload.values(), pid),
        )
        content.reset_snapshot(pid)
    else:
        cid = data["audio_target"]
        if not accounts.channel_allowed(message.from_user.id, cid):
            raise ValueError(tr("Получатель недоступен."))
        if not accounts.use_daily(message.from_user.id, "post_create"):
            raise ValueError(tr("Лимит Free: 3 новых поста в день."))
        pid = content.create_draft(message.from_user.id, payload, [cid])
    await state.clear()
    await state.update_data(pid=pid)
    await state.set_state(PostCreate.idle)
    await posts.show_post(bot, message.from_user.id, pid)


def pause_schedule(post):
    if post["status"] == "scheduled":
        with db.atomic():
            db.db.execute(
                "UPDATE scheduled_posts SET active=0 WHERE post_id=?", (post["id"],)
            )
            db.db.execute("UPDATE posts SET status='draft' WHERE id=?", (post["id"],))
        return True
    return False


async def tools(c, post):
    options = metadata(dict(post))
    available = fields(post, options)
    rows = ui.button_grid(
        [
            ui.choice(label, f"ae:{post['id']}:{field}")
            for field, label in labels().items()
            if field in available
        ],
        2,
    )
    if "cover" in available:
        rows.append(
            [
                ui.choice(tr("🖼 Обложка"), f"ae:{post['id']}:cover"),
                ui.choice(tr("🧹 Убрать обложку"), f"ae:{post['id']}:clearcover"),
            ]
        )
    rows += [
        [
            ui.choice(tr("📝 Описание"), f"p:{post['id']}:text"),
            ui.choice(tr("🔄 Заменить"), f"ae:{post['id']}:replace"),
        ],
        [ui.choice(tr("↩️ Сбросить аудиоизменения"), f"ae:{post['id']}:reset")],
        [ui.choice(tr("👁 Предпросмотр"), f"p:{post['id']}:preview")],
    ]
    details = [tr("✍️ Аудио") + " · " + html.escape(options.get("format", "").upper())]
    for field, label in labels().items():
        if field in available:
            changes = options.get("changes", {})
            value = (
                (changes[field] or tr("очищено"))
                if field in changes
                else options.get(field, tr("без изменений"))
            )
            details.append(label + ": " + html.escape(str(value or tr("не задано"))))
    if not available:
        details.append(
            tr(
                "Голосовое сохраняет свой формат. Можно изменить описание, кнопки и настройки публикации."
            )
        )
    elif available == {"filename"}:
        details.append(
            tr(
                "Для этого формата доступны имя файла и описание. Звук не перекодируется."
            )
        )
    else:
        details.append(
            tr(
                "Теги и обложка сохраняются в файле; звук не перекодируется. Для обработки нужен файл до 20 МБ."
            )
        )
    await ui.edit(c, "\n".join(details), ui.kb(rows))


@router.callback_query(F.data.startswith("ae:"))
async def action(c, state, bot):
    _, pid, field = c.data.split(":")
    pid = int(pid)
    post = content.mutable_post(pid, c.from_user.id)
    options = metadata(dict(post))
    if not options.get("editor"):
        raise ValueError(tr("Откройте аудиоредактор заново."))
    await state.clear()
    await state.update_data(pid=pid)
    await state.set_state(PostCreate.idle)
    if field == "tools":
        await tools(c, post)
        await c.answer()
        return
    if field not in fields(post, options) | {"clearcover", "reset", "replace"}:
        raise ValueError(tr("Инструмент недоступен для этого формата."))
    if field == "clearcover" and "cover" not in fields(post, options):
        raise ValueError(tr("Инструмент недоступен для этого формата."))
    if pause_schedule(post):
        await ui.answer(
            c.message,
            tr(
                "Расписание снято на время редактирования. После изменений выберите время заново."
            ),
        )
    if field == "replace":
        await state.update_data(audio_pid=pid)
        await state.set_state(AudioEdit.content)
        await ui.edit(
            c,
            tr(
                "Отправьте аудиотрек, голосовое или аудиофайл. Инструменты появятся после загрузки."
            ),
            ui.back(f"p:{pid}:preview"),
        )
    elif field in {"reset", "clearcover"}:
        if field == "reset":
            options["changes"] = {}
            options.pop("cover_file_id", None)
            options.pop("cover_removed", None)
            options["process"] = (
                options.get("source_type") == "document"
                and post["content_type"] == "audio"
            )
        else:
            options.pop("cover_file_id", None)
            options.update(cover_removed=True, process=True)
        options.pop("prepared_file_id", None)
        save(pid, options)
        await c.answer()
        await posts.show_post(bot, c.from_user.id, pid)
        return
    else:
        await state.update_data(audio_pid=pid, audio_field=field)
        await state.set_state(AudioEdit.value)
        prompt = (
            tr("Отправьте обложку как фото или PNG/JPEG файл до 2 МБ.")
            if field == "cover"
            else tr(
                "Отправьте новое значение (до 128 символов). Для очистки отправьте -."
            )
        )
        if field == "filename":
            prompt = tr(
                "Введите имя файла с тем же расширением или без расширения. До 128 символов."
            )
        await ui.edit(c, prompt, ui.back(f"ae:{pid}:tools"))
    await c.answer()


def save(pid, options):
    db.execute(
        "UPDATE posts SET audio_meta_json=? WHERE id=?",
        (json.dumps(options, ensure_ascii=False), pid),
    )
    content.reset_snapshot(pid)


@router.message(AudioEdit.value, ~F.successful_payment, ~F.text.startswith("/"))
async def value(message: Message, state, bot):
    data = await state.get_data()
    pid, field = data["audio_pid"], data["audio_field"]
    post = content.mutable_post(pid, message.from_user.id)
    options = metadata(dict(post))
    if field not in fields(post, options):
        raise ValueError(tr("Инструмент недоступен для этого формата."))
    if field == "cover":
        obj = message.photo[-1] if message.photo else message.document
        if not obj or (
            message.document
            and message.document.mime_type not in {"image/png", "image/jpeg"}
        ):
            raise ValueError(
                tr("Отправьте обложку как фото или PNG/JPEG файл до 2 МБ.")
            )
        if not getattr(obj, "file_size", None) or obj.file_size > 2 * 1024 * 1024:
            raise ValueError(tr("Обложка должна быть не больше 2 МБ."))
        if not accounts.use_daily(message.from_user.id, "cover"):
            raise ValueError(
                tr(
                    "Дневной лимит замены обложек исчерпан. Лимит обновится в 00:00 UTC."
                )
            )
        options["cover_file_id"] = obj.file_id
        options.pop("cover_removed", None)
    else:
        text = (message.text or "").strip()
        if not 1 <= len(text) <= 128:
            raise ValueError(tr("Значение: 1–128 символов."))
        if (
            field == "year"
            and text != "-"
            and (not text.isdigit() or not 1 <= int(text) <= 9999)
        ):
            raise ValueError(tr("Введите год от 1 до 9999 или - для очистки."))
        if field == "filename":
            if (
                text == "-"
                or any(char in text for char in '/\\<>:"|?*')
                or any(ord(char) < 32 for char in text)
                or text.startswith(".")
            ):
                raise ValueError(tr("Некорректное имя файла."))
            extension = PurePath(text).suffix.lower().lstrip(".")
            if extension and extension != options["format"]:
                raise ValueError(tr("Расширение файла менять нельзя."))
            if not extension:
                text += "." + options["format"]
        options.setdefault("changes", {})[field] = "" if text == "-" else text
    options["process"] = True
    options.pop("prepared_file_id", None)
    save(pid, options)
    await state.clear()
    await state.update_data(pid=pid)
    await state.set_state(PostCreate.idle)
    await posts.show_post(bot, message.from_user.id, pid)
