"""features / common components."""

import json
import re
from urllib.parse import urlsplit

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import access as access
from app import accounts as accounts
from app import content as content
from app import database as database
from app import downloader as downloader
from app import preferences
from app import timeutils as timeutils
from app import ui as ui
from app.features import downloads as features_downloads
from app.i18n import tr
from services.jobs import download_jobs

router = Router(name="features.common")


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(message: Message, state: FSMContext, bot: Bot):
    uid = message.from_user.id
    if accounts.blocked(uid):
        return
    existing = database.one("SELECT 1 FROM users WHERE telegram_id=?", (uid,))
    await accounts.ensure_user_async(message.from_user)
    payload = (message.text or "").split(maxsplit=1)
    if len(payload) == 2 and re.fullmatch(r"contest_\d+(?:_\d+)?", payload[1]):
        from app.features.contests import show_entry

        parts = payload[1].split("_")
        await state.clear()
        await show_entry(
            message,
            state,
            bot,
            int(parts[1]),
            uid,
            int(parts[2]) if len(parts) > 2 else None,
        )
        return
    if not existing:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2 and re.fullmatch(r"ref_\d+", parts[1]):
            inviter = int(parts[1][4:])
            if (
                inviter != uid
                and database.one("SELECT 1 FROM users WHERE telegram_id=?", (inviter,))
                and not accounts.blocked(inviter)
            ):
                database.execute(
                    "INSERT OR IGNORE INTO referrals(inviter_id,invited_id,created_at) VALUES(?,?,?)",
                    (inviter, uid, timeutils.iso()),
                )
    await state.clear()
    await ui.answer(
        message,
        tr("🤖 <b>Главное меню</b>\n\nВыберите раздел:"),
        reply_markup=ui.main_kb(),
    )
    await ui.dismiss_command(message)
    await ui.cleanup_home(bot, uid)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await ui.answer(message, tr("❌ Отменено."), reply_markup=ui.main_kb())
    await ui.dismiss_command(message)
    await ui.cleanup_home(message.bot, message.from_user.id)


@router.message(Command("stopdownload", "stop_download"))
async def stop_download(message: Message):
    stopped = download_jobs.cancel(message.from_user.id)
    await ui.answer(
        message,
        tr("⏹ Останавливаю скачивание и отправку. Уже отправленные файлы сохранены.")
        if stopped
        else tr("Нет активной загрузки."),
    )


@router.callback_query(F.data == "menu:main")
async def menu_main(c: CallbackQuery):
    if not await access.access_callback(c):
        return
    await ui.edit(c, tr("🤖 <b>Главное меню</b>\n\nВыберите раздел:"), ui.main_kb())
    await ui.cleanup_home(c.bot, c.from_user.id)
    await c.answer()


@router.callback_query(F.data == "menu:settings")
async def settings(c: CallbackQuery):
    if not await access.access_callback(c):
        return
    await ui.edit(c, tr("⚙️ <b>Настройки</b>"), ui.settings_kb(c.from_user.id))
    await c.answer()


@router.callback_query(F.data == "settings:profile")
async def profile(c: CallbackQuery):
    uid = c.from_user.id
    exp = accounts.premium_expiry(uid)
    premium = database.one("SELECT * FROM premium WHERE user_id=?", (uid,))
    status = (
        (
            tr("бессрочно")
            if premium and premium["lifetime"]
            else preferences.display(uid, exp)
        )
        if accounts.has_premium(uid)
        else tr("нет")
    )
    refs = database.one(
        "SELECT COUNT(*) n,SUM(rewarded_at IS NOT NULL) rewarded FROM referrals WHERE inviter_id=?",
        (uid,),
    )
    await ui.edit(
        c,
        tr(
            "👤 ID: {v0}\n💎 Premium: {v1}\nКаналы: {v2}/{v3}\nИсточники: до {v4}\nПриглашено: {v5}; награждено: {v6}",
            v0=uid,
            v1=status,
            v2=accounts.channel_count(uid),
            v3=accounts.channel_limit(uid),
            v4=accounts.source_limit(uid),
            v5=refs["n"],
            v6=refs["rewarded"] or 0,
        ),
        ui.settings_kb(uid),
    )
    await c.answer()


@router.callback_query(F.data == "settings:notifications")
async def notifications(c: CallbackQuery):
    if not await access.access_callback(c):
        return
    await ui.edit(
        c,
        tr(
            "🔔 <b>Уведомления</b>\n\nБот автоматически сообщает о публикациях, ошибках, потере прав и Premium."
        ),
        ui.back("menu:settings"),
    )
    await c.answer()


@router.callback_query(F.data == "settings:general")
async def general(c: CallbackQuery):
    await ui.edit(
        c,
        tr("Часовой пояс: ") + preferences.get_preferences(c.from_user.id)["timezone"],
        ui.back("menu:settings"),
    )
    await c.answer()


@router.callback_query(F.data == "settings:help")
async def help_menu(c: CallbackQuery):
    await ui.edit(
        c,
        tr(
            "📢 Для поста отправьте сообщение → выберите каналы → время или «Опубликовать». Можно добавлять любые ссылки.\n🔄 Источники Telegram находятся в карточке канала.\n🔗 Скачивание видео, плейлистов, звука и изображений. Большие видео отправляются частями до 49 МБ.\n🧩 Шаблоны и кнопки настраиваются по шагам.\n/cancel — выйти из текущего ввода.\n/paysupport — поддержка оплаты."
        )
        + tr(
            "\n🕒 В настройках выберите часовой пояс и язык.\n⏹ /stopdownload останавливает скачивание и отправку.\n📚 Мультипостинг: серия готовых постов с интервалом.\n🏆 Конкурсы: создавайте свои или участвуйте в активных."
        ),
        ui.back("menu:settings"),
    )
    await c.answer()


def normalize_incoming_message(raw):
    """Restore discriminator fields omitted by the old default-excluding serializer."""
    data = json.loads(raw)

    def repair(value):
        if isinstance(value, list):
            for item in value:
                repair(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if (
                    key in {"forward_origin", "origin"}
                    and isinstance(item, dict)
                    and "type" not in item
                ):
                    for field, kind in (
                        ("sender_user", "user"),
                        ("sender_user_name", "hidden_user"),
                        ("sender_chat", "chat"),
                        ("chat", "channel"),
                    ):
                        if field in item:
                            item["type"] = kind
                            break
                repair(item)

    repair(data)
    return data


def restore_incoming_message(raw):
    return Message.model_validate(normalize_incoming_message(raw))


async def offer_incoming(m, bot):
    if m.from_user is None:
        return
    links = downloader.external_links(m)
    iid = database.execute(
        "INSERT INTO incoming(owner_id,message_json,links_json,created_at) VALUES(?,?,?,?)",
        (
            m.from_user.id,
            json.dumps(
                normalize_incoming_message(
                    m.model_dump_json(exclude_none=True, exclude_defaults=True)
                ),
                ensure_ascii=False,
            ),
            json.dumps(links),
            timeutils.iso(),
        ),
    ).lastrowid
    rows = []
    if links:
        rows.append([ui.choice(tr("📥 Скачать по ссылке"), f"in:{iid}:links")])
    rows.append(
        [
            ui.choice(tr("📢 Создать пост"), f"in:{iid}:post"),
            ui.choice(tr("Пропустить"), f"in:{iid}:skip"),
        ]
    )
    await ui.show_panel(
        bot,
        m.from_user.id,
        tr("Что сделать с этим сообщением?"),
        reply_markup=ui.kb(rows),
    )


@router.callback_query(F.data.startswith("in:"))
async def incoming_action(c: CallbackQuery, bot: Bot):
    p = c.data.split(":")
    iid = int(p[1])
    action = p[2]
    uid = c.from_user.id
    row = database.one("SELECT * FROM incoming WHERE id=? AND owner_id=?", (iid, uid))
    if not row:
        raise ValueError(tr("Сообщение недоступно."))
    if action == "skip":
        await ui.edit(c, tr("Пропущено."), ui.main_kb())
        await c.answer()
        return
    links = json.loads(row["links_json"])
    if action == "links":
        if len(links) == 1:
            await c.answer()
            await features_downloads.inspect_for_user(bot, uid, links[0])
            return
        rows = ui.button_grid(
            [
                ui.choice(f"{i + 1}. {urlsplit(url).hostname}", f"in:{iid}:url:{i}")
                for i, url in enumerate(links)
            ],
            2,
        )
        await ui.edit(c, tr("Какую ссылку скачать?"), ui.kb(rows))
        await c.answer()
        return
    if action == "url":
        idx = int(p[3])
        if not 0 <= idx < len(links):
            raise ValueError(tr("Ссылка не найдена."))
        await c.answer()
        await features_downloads.inspect_for_user(bot, uid, links[idx])
        return
    if action == "post":
        if row["post_id"]:
            pid = row["post_id"]
        else:
            m = restore_incoming_message(row["message_json"])
            payload = content.message_payload(m)
            if not accounts.use_daily(uid, "post_create"):
                raise ValueError(tr("Лимит Free: 3 новых поста в день."))
            with database.atomic():
                pid = content.create_draft(uid, payload)
                database.db.execute(
                    "UPDATE incoming SET post_id=? WHERE id=?", (pid, iid)
                )
        await c.answer()
        await ui.send_target_picker(bot, uid, pid, panel=True)
        return
    raise ValueError(tr("Неизвестное действие."))
