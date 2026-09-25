"""features / downloads components."""

import asyncio
import html
import json
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app import accounts
from app import content as content
from app import database as database
from app import downloader as downloader
from app import timeutils as timeutils
from app import ui as ui
from app.features import common as features_common
from app.i18n import tr
from app.states import DownloadInput
from services.jobs import download_jobs
from services.progress import ProgressInputFile, ProgressMessage, current_progress

log = logging.getLogger(__name__)

router = Router(name="features.downloads")

DOWNLOAD_SLOTS = asyncio.Semaphore(2)


@router.callback_query(F.data == "download:menu")
async def progress_menu(c: CallbackQuery, state: FSMContext = None):
    # Keep navigation separate so subsequent progress edits cannot replace it.
    if state is not None:
        await state.clear()
    bot = getattr(c, "bot", None) or getattr(c.message, "bot", None)
    if bot is not None:
        ui.note_message(c.message.chat.id, c.message.message_id)
        await ui.show_panel(
            bot,
            c.message.chat.id,
            tr("🤖 <b>Главное меню</b>\n\nВыберите раздел:"),
            ui.main_kb(),
        )
    else:
        await c.message.answer(
            tr("🤖 <b>Главное меню</b>\n\nВыберите раздел:"), reply_markup=ui.main_kb()
        )
    await c.answer()


@router.callback_query(F.data == "menu:download")
async def download_menu(c: CallbackQuery, state: FSMContext):
    if download_jobs.active(c.from_user.id):
        await ui.edit(
            c,
            "⏳ "
            + download_jobs.labels.get(c.from_user.id, tr("Загрузка"))
            + tr("\nМожно пользоваться остальными функциями бота."),
            ui.kb(
                [
                    [
                        ui.choice(tr("🔄 Статус"), "download:status"),
                        ui.choice(tr("✖ Отменить загрузку"), "download:cancel"),
                    ],
                    [ui.choice(tr("⬅️ Главное меню"), "menu:main")],
                ]
            ),
        )
        await c.answer()
        return
    await state.set_state(DownloadInput.url)
    await ui.edit(
        c,
        tr(
            "Скачать по 🔗\nОтправьте ссылку на видео, плейлист, фото или аудио.\nЯ покажу доступные качества и вариант «Только звук».\nБольшие видео отправляются частями до 49 МБ. Поддерживаются доступные без входа публикации."
        ),
        ui.back("menu:main"),
    )
    await c.answer()


async def inspect_for_user(bot, uid, url):
    downloader.validate_download_url(url)
    download_jobs.start(
        uid, lambda: inspect_in_background(bot, uid, url), tr("Проверяю ссылку")
    )


async def inspect_in_background(bot, uid, url):
    try:
        await bot.send_message(
            uid,
            tr("🔎 Проверяю ссылку в фоне. Вы можете пользоваться меню."),
            reply_markup=ui.kb(
                [
                    [ui.choice(tr("⏹ Остановить загрузку"), "download:cancel")],
                    [ui.choice(tr("⬅️ Главное меню"), "menu:main")],
                ]
            ),
        )
        async with DOWNLOAD_SLOTS:
            with tempfile.TemporaryDirectory(prefix="telegram-inspect-") as tmp:
                info = await downloader.run_download_worker("inspect", url, Path(tmp))
        did = database.execute(
            "INSERT INTO downloads(owner_id,url,info_json,status,created_at) VALUES(?,?,?,?,?)",
            (uid, url, json.dumps(info, ensure_ascii=False), "ready", timeutils.iso()),
        ).lastrowid
        rows = ui.button_grid(
            [
                ui.choice(x["label"], f"dl:{did}:{i}")
                for i, x in enumerate(info["choices"])
            ],
            2,
        )
        rows.append([ui.choice(tr("❌ Пропустить"), "menu:main")])
        await bot.send_message(
            uid,
            html.escape(info["title"]) + tr("\nЧто скачать?"),
            reply_markup=ui.kb(rows),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("Download inspection failed")
        await bot.send_message(
            uid,
            tr("Не удалось проверить ссылку: ") + html.escape(str(exc)[:300]),
            reply_markup=ui.back("menu:download"),
        )


@router.callback_query(F.data == "download:status")
async def download_status(c: CallbackQuery):
    await c.answer(
        download_jobs.labels.get(c.from_user.id, tr("Нет активной загрузки.")),
        show_alert=True,
    )


@router.callback_query(F.data == "download:cancel")
async def download_cancel(c: CallbackQuery):
    cancelled = download_jobs.cancel(c.from_user.id)
    await c.answer(
        tr("Загрузка останавливается.") if cancelled else tr("Нет активной загрузки."),
        show_alert=True,
    )


@router.message(DownloadInput.url, F.text, ~F.text.startswith("/"))
async def download_url_message(m: Message, state: FSMContext, bot: Bot):
    links = downloader.external_links(m)
    if not links:
        raise ValueError(
            tr(
                "Пришлите внешнюю ссылку. Сообщения Telegram можно переслать боту для создания поста."
            )
        )
    await state.clear()
    if len(links) > 1:
        await features_common.offer_incoming(m, bot)
        return
    await inspect_for_user(bot, m.from_user.id, links[0])


async def send_downloaded_file(bot, uid, path, title, metadata=None):
    async with ProgressInputFile(path) as upload:
        return await _send_downloaded_file(bot, uid, path, title, metadata, upload)


async def _send_downloaded_file(bot, uid, path, title, metadata, upload):
    suffix = path.suffix.lower()
    caption = title[:900]
    # Telegram may reject an image codec or non-streamable video; document fallback preserves the file.
    try:
        if (
            suffix in {".jpg", ".jpeg", ".png", ".webp"}
            and path.stat().st_size <= 9_000_000
        ):
            return await bot.send_photo(
                uid, upload, caption=caption, parse_mode=None, request_timeout=180
            )
        if suffix == ".gif":
            return await bot.send_animation(
                uid, upload, caption=caption, parse_mode=None, request_timeout=180
            )
        if suffix == ".mp4":
            meta = dict(metadata or {})
            if meta.get("thumbnail"):
                thumbnail = Path(meta["thumbnail"])
                meta["thumbnail"] = BufferedInputFile(
                    thumbnail.read_bytes(), filename=thumbnail.name
                )
            return await bot.send_video(
                uid,
                upload,
                caption=caption,
                parse_mode=None,
                supports_streaming=True,
                request_timeout=180,
                **meta,
            )
        if suffix in {".mp3", ".m4a"}:
            return await bot.send_audio(
                uid, upload, caption=caption, parse_mode=None, request_timeout=180
            )
    except TelegramBadRequest:
        pass
    return await bot.send_document(
        uid, upload, caption=caption, parse_mode=None, request_timeout=180
    )


@router.callback_query(F.data.startswith("dl:"))
async def download_selected(c: CallbackQuery, bot: Bot):
    _, did, index = c.data.split(":")
    did = int(did)
    index = int(index)
    uid = c.from_user.id
    row = database.one("SELECT * FROM downloads WHERE id=? AND owner_id=?", (did, uid))
    if not row:
        raise ValueError(tr("Ссылка не найдена."))
    info = json.loads(row["info_json"])
    if not 0 <= index < len(info["choices"]):
        raise ValueError(tr("Этот формат недоступен."))
    if download_jobs.active(uid):
        raise ValueError(tr("Дождитесь текущего скачивания или отмените его."))
    if row["status"] == "done":
        await c.answer(tr("Уже отправлено."))
        return
    await c.answer(tr("Загрузка запущена в фоне."))
    download_jobs.start(
        uid,
        lambda: download_in_background(
            bot,
            uid,
            did,
            index,
            dict(row),
            info,
            getattr(getattr(c, "message", None), "message_id", None),
        ),
        tr("В очереди на скачивание"),
    )


async def download_in_background(bot, uid, did, index, row, info, control_id=None):
    database.execute(
        "UPDATE downloads SET status='running',error=NULL WHERE id=?", (did,)
    )
    delivered = 0
    progress = ProgressMessage(bot, uid)
    token = current_progress.set(progress)
    try:
        await progress.update("queued", force=True)
        entries = (
            info["entries"]
            if info["backend"] == "playlist"
            else [{"url": row["url"], "title": info["title"]}]
        )
        failures = []
        for entry_index, entry in enumerate(entries):
            download_jobs.labels[uid] = tr(
                "Видео {v0}/{v1}: скачивание и обработка",
                v0=entry_index + 1,
                v1=len(entries),
            )
            if database.one(
                "SELECT 1 FROM download_deliveries WHERE download_id=? AND selection=? AND entry_index=? AND part_index=-1",
                (did, index, entry_index),
            ):
                continue
            progress.item = (
                f"{entry_index + 1}/{len(entries)} · " + entry["title"][:120]
            )
            await progress.update("queued", force=True)
            try:
                # Release the slot and all temporary files after each playlist entry.
                async with DOWNLOAD_SLOTS:
                    with tempfile.TemporaryDirectory(
                        prefix="telegram-download-"
                    ) as tmp:
                        folder = Path(tmp)
                        item_info = (
                            {"backend": "yt", "choices": [info["choices"][index]]}
                            if info["backend"] == "playlist"
                            else info
                        )
                        choice = item_info["choices"][
                            0 if info["backend"] == "playlist" else index
                        ]
                        units = (
                            item_info.get("video_count", 0)
                            if choice["kind"] == "gallery"
                            else int(choice["kind"] in {"video", "audio"})
                        )
                        day = timeutils.now().date().isoformat()
                        if units and not accounts.use_daily(
                            uid, "download_video", units
                        ):
                            raise ValueError(
                                tr(
                                    "Дневной лимит скачивания видео исчерпан. Лимит обновится в 00:00 UTC."
                                )
                            )
                        try:
                            result = await downloader.run_download_worker(
                                "download",
                                entry["url"],
                                folder,
                                item_info,
                                0 if info["backend"] == "playlist" else index,
                            )
                        except BaseException:
                            if units:
                                accounts.refund_daily(uid, "download_video", day, units)
                            raise
                        files = [Path(p).resolve() for p in result["files"]]
                        if not files or any(
                            not p.is_relative_to(folder.resolve())
                            or not p.is_file()
                            or not 0 < p.stat().st_size <= downloader.DOWNLOAD_LIMIT
                            for p in files
                        ):
                            raise ValueError(tr("Недопустимый результат скачивания."))
                        for part_index, path in enumerate(files):
                            download_jobs.labels[uid] = tr(
                                "Видео {v0}/{v1}, часть {v2}/{v3}: отправка",
                                v0=entry_index + 1,
                                v1=len(entries),
                                v2=part_index + 1,
                                v3=len(files),
                            )
                            key = (did, index, entry_index, part_index)
                            if database.one(
                                "SELECT 1 FROM download_deliveries WHERE download_id=? AND selection=? AND entry_index=? AND part_index=?",
                                key,
                            ):
                                continue
                            meta = result.get("metadata", {}).get(str(path), {})
                            if meta.get("thumbnail") and not Path(
                                meta["thumbnail"]
                            ).resolve().is_relative_to(folder.resolve()):
                                raise ValueError(tr("Недопустимая обложка."))
                            title = entry["title"] + (
                                tr(
                                    " — часть {v0}/{v1}",
                                    v0=part_index + 1,
                                    v1=len(files),
                                )
                                if len(files) > 1
                                else ""
                            )
                            sent = await send_downloaded_file(
                                bot, uid, path, title, meta
                            )
                            delivered += 1
                            ui.note_message(uid, sent.message_id)
                            database.execute(
                                "INSERT OR IGNORE INTO download_deliveries VALUES(?,?,?,?,?)",
                                (*key, sent.message_id),
                            )
                            pid = content.create_draft(
                                uid, content.message_payload(sent)
                            )
                            await ui.send_target_picker(bot, uid, pid)
                        database.execute(
                            "INSERT OR IGNORE INTO download_deliveries VALUES(?,?,?,?,?)",
                            (did, index, entry_index, -1, 0),
                        )
            except Exception as exc:
                if len(entries) == 1:
                    raise
                failures.append(entry_index + 1)
                await bot.send_message(
                    uid,
                    tr("Не удалось скачать видео {v0}: ", v0=entry_index + 1)
                    + html.escape(str(exc)[:250]),
                )
        if failures:
            raise ValueError(
                tr("Не загружены видео: ")
                + ", ".join(map(str, failures))
                + tr(". Повторите выбор: отправленные части будут пропущены.")
            )
        database.execute("UPDATE downloads SET status='done' WHERE id=?", (did,))
        await progress.update("done", force=True, terminal=True)
        await ui.clear_controls(bot, uid, control_id)
        await ui.refresh_panel(bot, uid)
    except asyncio.CancelledError:
        database.execute(
            "UPDATE downloads SET status='cancelled',error='Загрузка остановлена; можно повторить' WHERE id=?",
            (did,),
        )
        await progress.update("cancelled", force=True, terminal=True)
        raise
    except Exception as exc:
        database.execute(
            "UPDATE downloads SET status='failed',error=? WHERE id=?",
            (str(exc)[:500], did),
        )
        await progress.update("failed", force=True, terminal=True)
        await bot.send_message(
            uid,
            tr("Не удалось завершить скачивание. ")
            + html.escape(str(exc)[:300])
            + (
                tr("\nЧасть файлов уже отправлена — повтор может прислать их снова.")
                if delivered
                else ""
            ),
            reply_markup=ui.back("menu:download"),
        )
    finally:
        current_progress.reset(token)
