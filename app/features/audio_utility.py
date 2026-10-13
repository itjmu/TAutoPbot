"""Private, bounded audio preparation; no posts or destinations are created."""

import asyncio
import copy
import html
import json
import math
import secrets
from contextlib import nullcontext
from pathlib import PurePath

from aiogram import F, Router

from app import content, ui
from app.features import audio
from app.i18n import tr
from app.states import AudioEdit, AudioUtility
from services.jobs import BackgroundJobs
from services.progress import ProgressMessage, current_progress

router = Router(name="features.audio_utility")
LIMIT = 20 * 1024 * 1024
FORMATS = ("mp3", "m4a", "ogg", "flac", "wav")
audio_jobs = BackgroundJobs()
_busy = set()


async def start(c, state):
    await state.clear()
    await state.update_data(audio_utility=True)
    await state.set_state(AudioEdit.content)
    await ui.edit(
        c,
        tr(
            "Отправьте музыку, аудиофайл или голосовое до 20 МБ. Выберите настройки и нажмите «Получить файл». Преобразование: записи до 10 минут, ожидание до 5 минут."
        ),
        ui.back("menu:main"),
    )
    await c.answer()


def effective_options(payload):
    options = audio.metadata(payload)
    target = options.get("convert_to", options["format"])
    target = "ogg" if target in {"voice", "oga", "opus"} else target
    presentation = options.get("presentation") or (
        payload["content_type"]
        if not options.get("convert_to")
        else "voice"
        if target == "ogg"
        else "audio"
        if target in {"mp3", "m4a"}
        else "document"
    )
    return options, target, presentation


def available_fields(payload):
    options, target, presentation = effective_options(payload)
    return audio.fields(
        dict(payload, content_type=presentation), dict(options, format=target)
    )


def keyboard(token, payload):
    options, target, presentation = effective_options(payload)
    available = available_fields(payload)
    rows = ui.button_grid(
        [
            ui.choice(label, f"au:{token}:{field}")
            for field, label in audio.labels().items()
            if field in available
        ],
        2,
    )
    if "cover" in available:
        rows.append([ui.choice(tr("📜 Текст песни"), f"au:{token}:lyrics")])
        rows.append(
            [
                ui.choice(tr("🖼 Обложка"), f"au:{token}:cover"),
                ui.choice(tr("🧹 Убрать обложку"), f"au:{token}:clearcover"),
            ]
        )
    rows.append([ui.choice(tr("🔄 Изменить формат"), f"au:{token}:formats")])
    if target in {"mp3", "m4a"}:
        rows.append(
            [
                ui.choice(
                    tr("🎙 Как голосовое")
                    if presentation != "voice"
                    else tr("🎵 Как музыку"),
                    f"au:{token}:as_voice"
                    if presentation != "voice"
                    else f"au:{token}:as_audio",
                )
            ]
        )
    rows += [
        [ui.choice(tr("📝 Описание"), f"au:{token}:caption")],
        [ui.choice(tr("👁 Получить файл"), f"au:{token}:result")],
        [
            ui.choice(tr("↩️ Сбросить аудиоизменения"), f"au:{token}:reset"),
            ui.choice(tr("🔄 Заменить"), "menu:audio"),
        ],
        [ui.choice(tr("⬅️ Главное меню"), "menu:main")],
    ]
    return ui.kb(rows)


async def panel(bot, uid, state):
    data = await state.get_data()
    payload = data["utility_payload"]
    options, target, presentation = effective_options(payload)
    changes = options.get("changes", {})
    text = tr("✍️ Аудио") + " · " + target.upper()
    text += (
        "\n"
        + {
            "audio": tr("🎵 Музыка"),
            "voice": tr("🎙 Голосовое"),
            "document": tr("📄 Файл"),
        }[presentation]
    )
    if options.get("convert_to"):
        text += "\n" + tr("Формат выбран. Обработка начнётся после «Получить файл».")
    for field, label in audio.labels().items():
        if field in changes:
            text += "\n" + label + ": " + html.escape(changes[field] or tr("очищено"))
    if options.get("trim"):
        start, end = options["trim"]
        text += "\n" + tr(
            "✂️ Сохранить участок: {start}–{end} сек.", start=start, end=end
        )
    await ui.show_panel(
        bot, uid, text, reply_markup=keyboard(data["utility_token"], payload)
    )


async def receive(message, state, bot):
    obj = message.audio or message.voice or message.document
    if not obj or not obj.file_size or obj.file_size > LIMIT:
        raise ValueError(tr("Отправьте аудиофайл с известным размером до 20 МБ."))
    payload = audio.audio_payload(message)
    options = audio.metadata(payload)
    options.update(
        fast=True, duration=getattr(obj, "duration", None), file_size=obj.file_size
    )
    payload["audio_meta_json"] = json.dumps(options, ensure_ascii=False)
    await state.clear()
    await state.update_data(
        audio_utility=True,
        utility_token=secrets.token_hex(6),
        utility_payload=payload,
        utility_original=copy.deepcopy(payload),
    )
    await state.set_state(AudioUtility.idle)
    await panel(bot, message.from_user.id, state)


@router.callback_query(F.data.startswith("au:"))
async def action(c, state, bot, dispatcher=None):
    _, token, field = c.data.split(":")
    data = await state.get_data()
    if token != data.get("utility_token"):
        raise ValueError(tr("Откройте аудиоредактор заново."))
    uid = c.from_user.id
    if uid in _busy or audio_jobs.active(uid):
        raise ValueError(tr("Аудио уже обрабатывается. Дождитесь результата."))
    payload = copy.deepcopy(data["utility_payload"])
    options = audio.metadata(payload)
    available = available_fields(payload)
    if "cover" in available:
        available.add("lyrics")
    if field not in available | {
        "clearcover",
        "mp3",
        "voice",
        "formats",
        "as_audio",
        "as_voice",
        *("fmt_" + fmt for fmt in FORMATS),
        "caption",
        "reset",
        "result",
        "tools",
    }:
        raise ValueError(tr("Инструмент недоступен для этого формата."))
    if field == "clearcover" and "cover" not in available:
        raise ValueError(tr("Инструмент недоступен для этого формата."))
    await c.answer()
    if field == "formats":
        _, target, _ = effective_options(payload)
        await ui.edit(
            c,
            tr("Выберите другой формат. Работа начнётся только после «Получить файл»."),
            ui.kb(
                [
                    [
                        ui.choice(
                            fmt.upper() + (" / Opus" if fmt == "ogg" else ""),
                            f"au:{token}:fmt_{fmt}",
                        )
                    ]
                    for fmt in FORMATS
                    if fmt != target
                ]
                + [[ui.choice(tr("⬅️ Назад"), f"au:{token}:tools")]]
            ),
        )
        return
    if field.startswith("fmt_") or field in {"mp3", "voice", "as_audio", "as_voice"}:
        if field.startswith("fmt_") or field == "mp3":
            target = field[4:] if field.startswith("fmt_") else "mp3"
            if target == effective_options(payload)[1]:
                await panel(bot, uid, state)
                return
            if target == options["format"]:
                options.pop("convert_to", None)
            else:
                options["convert_to"] = target
            options["presentation"] = (
                "audio"
                if target in {"mp3", "m4a"}
                else "voice"
                if target == "ogg"
                else "document"
            )
            if options.get("changes", {}).get("filename"):
                options["changes"]["filename"] = (
                    PurePath(options["changes"]["filename"]).stem + "." + target
                )
        else:
            _, target, _ = effective_options(payload)
            if target not in {"mp3", "m4a", "ogg"} or (
                field == "as_audio" and target == "ogg"
            ):
                raise ValueError(tr("Инструмент недоступен для этого формата."))
            options["presentation"] = "audio" if field == "as_audio" else "voice"
        options["process"] = bool(
            options.get("convert_to")
            or options.get("changes")
            or options.get("cover_file_id")
            or options.get("cover_removed")
            or options["presentation"] != payload["content_type"]
            or options.get("source_type") != payload["content_type"]
        )
        options.pop("prepared_file_id", None)
        payload["audio_meta_json"] = json.dumps(options, ensure_ascii=False)
        await state.update_data(utility_payload=payload)
        await state.set_state(AudioUtility.idle)
        await panel(bot, uid, state)
        return
    if field == "tools":
        await state.set_state(AudioUtility.idle)
        await panel(bot, uid, state)
        return
    if field == "reset":
        await state.update_data(utility_payload=copy.deepcopy(data["utility_original"]))
        await state.set_state(AudioUtility.idle)
        await panel(bot, uid, state)
        return
    if field == "result":

        async def work():
            nonlocal options
            progress = ProgressMessage(
                bot, uid, menu_callback="menu:main", cancel_callback="aujob:cancel"
            )
            progress_token = current_progress.set(progress)
            try:
                await progress.update("audio_processing", force=True)
                # Covers, transfers and encoding share one end-to-end deadline.
                result = await asyncio.wait_for(
                    content.send_content(bot, uid, payload, None), 300
                )
                ui.note_sent(uid, result)
                await progress.update("done", force=True, terminal=True)
                if field != "result" or options.get("process"):
                    options = audio.metadata(payload)
                    options.pop("convert_to", None)
                    options.pop("trim", None)
                    options.pop("prepared_file_id", None)
                    options.update(
                        {
                            key: value
                            for key, value in options.get("changes", {}).items()
                            if key != "filename"
                        }
                    )
                    options["changes"] = {}
                    options.pop("cover_file_id", None)
                    options.pop("cover_removed", None)
                    payload["audio_meta_json"] = json.dumps(options, ensure_ascii=False)
                # Do not overwrite a replacement selected while processing.
                isolation = (
                    dispatcher.fsm.events_isolation.lock(state.key)
                    if dispatcher
                    else nullcontext()
                )
                async with isolation:
                    if (await state.get_data()).get("utility_token") == token:
                        await state.update_data(utility_payload=payload)
                        await state.set_state(AudioUtility.idle)
                        await panel(bot, uid, state)
            except asyncio.CancelledError:
                await progress.update("cancelled", force=True, terminal=True)
                raise
            except Exception as exc:
                from services.audio_files import public_error

                stage = {
                    "download": tr("Скачивание"),
                    "upload": tr("Отправка в Telegram"),
                }.get(progress.phase, tr("🎵 Редактирование аудио"))
                reason = (
                    tr(
                        "Подготовка не завершилась за 5 минут. Исходник и настройки сохранены."
                    )
                    if isinstance(exc, TimeoutError)
                    else public_error(exc)
                )
                progress.item = tr(
                    "Этап: {stage}\nПричина: {reason}", stage=stage, reason=reason
                )
                await progress.update("audio_failed", force=True, terminal=True)
                await ui.answer(
                    c.message,
                    html.escape(reason),
                )
            finally:
                current_progress.reset(progress_token)

        audio_jobs.start(uid, work, "audio")
        return
    if field == "clearcover":
        options.pop("cover_file_id", None)
        options.update(cover_removed=True, process=True)
        options.pop("prepared_file_id", None)
        payload["audio_meta_json"] = json.dumps(options, ensure_ascii=False)
        await state.update_data(utility_payload=payload)
        await state.set_state(AudioUtility.idle)
        await panel(bot, uid, state)
        return
    await state.update_data(utility_field="trim_start" if field == "trim" else field)
    await state.set_state(AudioUtility.value)
    prompt = tr("Отправьте новое значение (до 128 символов). Для очистки отправьте -.")
    if field == "cover":
        prompt = tr("Отправьте обложку как фото или PNG/JPEG файл до 2 МБ.")
    elif field == "trim":
        prompt = tr(
            "С какого момента оставить звук? Введите 0:10 или 10 (секунд). Это начало сохраняемого фрагмента."
        )
    elif field == "lyrics":
        prompt = tr(
            "Пришлите текст песни до 10000 символов. Он сохранится внутри MP3/M4A для совместимых плееров. Для удаления отправьте -."
        )
    elif field == "caption":
        prompt = tr("Отправьте описание до 1024 символов или - для очистки.")
    await ui.edit(c, prompt, ui.back(f"au:{token}:tools"))


@router.message(AudioUtility.value, ~F.successful_payment, ~F.text.startswith("/"))
async def value(message, state, bot):
    data = await state.get_data()
    if message.from_user.id in _busy or audio_jobs.active(message.from_user.id):
        raise ValueError(tr("Аудио уже обрабатывается. Дождитесь результата."))
    field = data["utility_field"]
    payload = copy.deepcopy(data["utility_payload"])
    options = audio.metadata(payload)
    text = (message.text or "").strip()
    if field == "cover":
        obj = message.photo[-1] if message.photo else message.document
        if (
            not obj
            or not obj.file_size
            or obj.file_size > 2 * 1024 * 1024
            or (
                message.document
                and message.document.mime_type not in {"image/png", "image/jpeg"}
            )
        ):
            raise ValueError(
                tr("Отправьте обложку как фото или PNG/JPEG файл до 2 МБ.")
            )
        options["cover_file_id"] = obj.file_id
        options.pop("cover_removed", None)
    elif field in {"trim_start", "trim_end"}:
        moment = parse_time(text)
        if field == "trim_start":
            if options.get("duration") and moment >= options["duration"]:
                raise ValueError(tr("Начало должно быть раньше конца записи."))
            await state.update_data(trim_start=moment, utility_field="trim_end")
            await ui.answer(
                message,
                tr(
                    "До какого момента оставить звук? Например 0:40 или 40. Будет сохранён участок от выбранного начала до этого момента, максимум 2 минуты."
                ),
                ui.back(f"au:{data['utility_token']}:tools"),
            )
            return
        start = data["trim_start"]
        if (
            not start < moment
            or moment - start > 120
            or (options.get("duration") and moment > options["duration"])
        ):
            raise ValueError(
                tr(
                    "Конец должен быть после начала, внутри записи. Фрагмент — до 2 минут."
                )
            )
        options["trim"] = [start, moment]
    elif field == "lyrics":
        if not 1 <= len(text) <= 10000:
            raise ValueError(tr("Текст песни: до 10000 символов, или - для удаления."))
        options.setdefault("changes", {})["lyrics"] = "" if text == "-" else text
    elif field == "trim":
        try:
            start, end = map(float, text.split())
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or not 0 <= start < end
                or end - start > 120
            ):
                raise ValueError
            if options.get("duration") and end > options["duration"]:
                raise ValueError
        except ValueError as exc:
            raise ValueError(
                tr(
                    "Введите начало и конец в секундах: 10 40. Фрагмент до 120 секунд. Например: 0 30."
                )
            ) from exc
        options["trim"] = [start, end]
    elif field == "caption":
        if not 1 <= len(text) <= 1024:
            raise ValueError(
                tr("Отправьте описание до 1024 символов или - для очистки.")
            )
        payload["text"] = "" if text == "-" else text
        payload["entities_json"] = "[]"
        payload["caption_entities_json"] = "[]"
    else:
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
                or text.startswith(".")
                or any(char in text for char in '/\\<>:"|?*')
                or any(ord(char) < 32 for char in text)
            ):
                raise ValueError(tr("Некорректное имя файла."))
            extension = PurePath(text).suffix.lower().lstrip(".")
            target = effective_options(payload)[1]
            if extension and extension != target:
                raise ValueError(tr("Расширение файла менять нельзя."))
            if not extension:
                text += "." + target
        options.setdefault("changes", {})[field] = "" if text == "-" else text
    if field != "caption":
        options["process"] = True
        options.pop("prepared_file_id", None)
    payload["audio_meta_json"] = json.dumps(options, ensure_ascii=False)
    await state.update_data(utility_payload=payload)
    await state.set_state(AudioUtility.idle)
    await panel(bot, message.from_user.id, state)


def parse_time(text):
    try:
        parts = text.replace(",", ".").split(":")
        if not 1 <= len(parts) <= 3:
            raise ValueError
        values = [float(part) for part in parts]
        if any(not math.isfinite(v) or v < 0 for v in values) or any(
            v >= 60 for v in values[1:]
        ):
            raise ValueError
        return sum(value * 60**index for index, value in enumerate(reversed(values)))
    except ValueError as exc:
        raise ValueError(
            tr("Введите время: 0:10, 1:30 или число секунд, например 10.")
        ) from exc


@router.callback_query(F.data == "aujob:cancel")
async def cancel(c):
    audio_jobs.cancel(c.from_user.id)
    await c.answer()
