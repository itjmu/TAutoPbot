"""features / sources components."""

import html
import json
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import accounts as accounts
from app import content as content
from app import database as database
from app import timeutils as timeutils
from app import ui as ui
from app.features import channels as features_channels
from app.features import posts as features_posts
from app.i18n import tr
from app.states import SourceCreate
from services.telegram_links import chat_reference

router = Router(name="features.sources")


async def deliver_source_draft(source, key, payload, bot):
    if not accounts.source_allowed(source):
        return
    targets = [
        r["channel_id"]
        for r in database.all_rows(
            "SELECT channel_id FROM source_targets WHERE source_id=?", (source["id"],)
        )
    ]
    with database.atomic():
        if database.one(
            "SELECT 1 FROM source_seen WHERE source_id=? AND item_key=?",
            (source["id"], key),
        ):
            return
        pid = content.create_draft(source["owner_telegram_id"], payload, targets)
        database.db.execute(
            "INSERT INTO source_seen(source_id,item_key,post_id) VALUES(?,?,?)",
            (source["id"], key, pid),
        )
    try:
        await features_posts.show_post(bot, source["owner_telegram_id"], pid)
    except Exception as exc:
        database.execute(
            "UPDATE post_sources SET last_error=? WHERE id=?",
            (
                tr("Черновик #{v0} сохранён, но ЛС недоступны: {v1}", v0=pid, v1=exc)[
                    :250
                ],
                source["id"],
            ),
        )


async def ingest_telegram(message, bot):
    rows = database.all_rows(
        "SELECT * FROM post_sources WHERE source_chat_id=? AND kind='telegram' AND active=1",
        (message.chat.id,),
    )
    if not rows:
        return
    # Prevent feedback loops when a target is also monitored as a source.
    if database.one(
        "SELECT 1 FROM published_messages m JOIN channels c ON c.id=m.channel_id WHERE c.telegram_chat_id=? AND m.telegram_message_id=?",
        (message.chat.id, message.message_id),
    ):
        return
    try:
        payload = content.message_payload(message)
    except ValueError:
        return
    payload.update(source_chat_id=message.chat.id, source_message_id=message.message_id)
    for source in rows:
        if not message.chat.username:
            member = await bot.get_chat_member(
                message.chat.id, source["owner_telegram_id"]
            )
            if member.status not in {
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            }:
                continue
        if message.media_group_id:
            if accounts.source_allowed(source):
                targets = [
                    r["channel_id"]
                    for r in database.all_rows(
                        "SELECT channel_id FROM source_targets WHERE source_id=?",
                        (source["id"],),
                    )
                ]
                queue_album(message, source["owner_telegram_id"], targets, source["id"])
        else:
            await deliver_source_draft(source, str(message.message_id), payload, bot)


def queue_album(message, uid, targets, source_id=0):
    key = (uid, message.chat.id, message.media_group_id, source_id)
    existing = database.one(
        "SELECT items_json FROM album_intake WHERE owner_id=? AND chat_id=? AND group_id=? AND source_id=?",
        key,
    )
    items = content.entity_list(existing["items_json"]) if existing else []
    item = content.message_payload(message)
    item["message_id"] = message.message_id
    if not any(x["message_id"] == item["message_id"] for x in items):
        items.append(item)
    database.execute(
        "INSERT INTO album_intake(owner_id,chat_id,group_id,source_id,items_json,targets_json,ready_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_id,chat_id,group_id,source_id) DO UPDATE SET items_json=excluded.items_json,ready_at=excluded.ready_at",
        (
            *key,
            json.dumps(items, ensure_ascii=False),
            json.dumps(targets),
            timeutils.iso(timeutils.now() + timedelta(seconds=2)),
        ),
    )


async def flush_albums(bot):
    rows = database.all_rows(
        "SELECT * FROM album_intake WHERE ready_at<=?", (timeutils.iso(),)
    )
    for row in rows:
        items = sorted(
            content.entity_list(row["items_json"]), key=lambda x: x["message_id"]
        )
        payload = dict(items[0])
        targets = content.entity_list(row["targets_json"])
        if len(items) > 1:
            payload.update(
                content_type="album", media_json=json.dumps(items, ensure_ascii=False)
            )
        if row["source_id"]:
            source = database.one(
                "SELECT * FROM post_sources WHERE id=?", (row["source_id"],)
            )
            if source:
                await deliver_source_draft(
                    source, "album:" + row["group_id"], payload, bot
                )
        elif row["post_id"] or accounts.use_daily(row["owner_id"], "post_create"):
            pid = row["post_id"]
            if not pid:
                with database.atomic():
                    pid = content.create_draft(row["owner_id"], payload, targets)
                    database.db.execute(
                        "UPDATE album_intake SET post_id=? WHERE owner_id=? AND chat_id=? AND group_id=? AND source_id=?",
                        (
                            pid,
                            row["owner_id"],
                            row["chat_id"],
                            row["group_id"],
                            row["source_id"],
                        ),
                    )
            await ui.send_target_picker(bot, row["owner_id"], pid)
        else:
            await bot.send_message(
                row["owner_id"],
                tr("Лимит Free: 3 новых поста в день. Альбом не добавлен."),
            )
        database.execute(
            "DELETE FROM album_intake WHERE owner_id=? AND chat_id=? AND group_id=? AND source_id=?",
            (row["owner_id"], row["chat_id"], row["group_id"], row["source_id"]),
        )


@router.channel_post()
async def channel_source(message: Message, bot: Bot):
    await ingest_telegram(message, bot)


@router.callback_query(F.data.startswith("source:open:"))
@router.callback_query(F.data.startswith("source:set:"))
@router.callback_query(F.data.startswith("source:del:"))
async def source_action(c: CallbackQuery):
    parts = c.data.split(":")
    sid = int(parts[2])
    text, markup = ui.source_card(sid, c.from_user.id)
    if parts[1] == "set":
        cid = int(parts[3])
        if not accounts.channel_allowed(c.from_user.id, cid):
            raise ValueError(tr("Канал недоступен."))
        if database.one(
            "SELECT 1 FROM source_targets WHERE source_id=? AND channel_id=?",
            (sid, cid),
        ):
            if (
                database.one(
                    "SELECT COUNT(*) n FROM source_targets WHERE source_id=?", (sid,)
                )["n"]
                <= 1
            ):
                raise ValueError(
                    tr("Оставьте хотя бы один канал или отключите источник.")
                )
            database.execute(
                "DELETE FROM source_targets WHERE source_id=? AND channel_id=?",
                (sid, cid),
            )
        else:
            database.execute(
                "INSERT INTO source_targets(source_id,channel_id) VALUES(?,?)",
                (sid, cid),
            )
        text, markup = ui.source_card(sid, c.from_user.id)
    if parts[1] == "del":
        database.execute("UPDATE post_sources SET active=0 WHERE id=?", (sid,))
        text = tr("Источник отключён.")
        markup = ui.back("channel:list")
    await ui.edit(c, text, markup)
    await c.answer()


@router.callback_query(F.data.startswith("channel:sources:"))
async def sources_list(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    ch = features_channels.template_channel(cid, c.from_user.id)
    sources = database.all_rows(
        "SELECT s.* FROM post_sources s WHERE s.owner_telegram_id=? AND s.active=1 AND s.kind='telegram' AND (EXISTS (SELECT 1 FROM source_targets t WHERE t.source_id=s.id AND t.channel_id=?) OR NOT EXISTS (SELECT 1 FROM source_targets t WHERE t.source_id=s.id)) ORDER BY s.id",
        (c.from_user.id, cid),
    )
    rows = ui.button_grid(
        [
            ui.choice(
                ("🟢 " if accounts.source_allowed(s) else "🔒 ")
                + (s["source_title"] or str(s["source_chat_id"]))[:25],
                f"source:open:{s['id']}",
            )
            for s in sources
        ],
        2,
    )
    rows += [
        [
            ui.choice(tr("➕ Источник"), f"source:add:{cid}"),
            ui.choice(tr("⬅️ Канал"), f"channel:open:{cid}"),
        ]
    ]
    await ui.edit(
        c,
        tr("🔄 Источники Telegram для ")
        + html.escape(ch["title"])
        + tr(
            "\nОбщий лимит: {v0}.\nПодключаются только каналы и группы Telegram.",
            v0=accounts.source_limit(c.from_user.id),
        ),
        ui.kb(rows),
    )
    await c.answer()


@router.callback_query(F.data.startswith("source:add:"))
async def source_add(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    cid = int(c.data.split(":")[2])
    if not accounts.channel_allowed(uid, cid):
        raise ValueError(tr("Канал недоступен."))
    if database.one(
        "SELECT COUNT(*) n FROM post_sources WHERE owner_telegram_id=? AND active=1",
        (uid,),
    )["n"] >= accounts.source_limit(uid):
        raise ValueError(tr("Лимит источников: 2 Free / 10 Premium."))
    await state.set_data({"source_target": cid})
    await state.set_state(SourceCreate.source)
    await ui.edit(
        c,
        tr(
            "Перешлите пост из канала/группы Telegram или отправьте @username.\nБот должен быть администратором источника. Для частного источника вы тоже должны быть его администратором."
        ),
        ui.back(f"channel:sources:{cid}"),
    )
    await c.answer()


@router.message(SourceCreate.source, ~F.successful_payment, ~F.text.startswith("/"))
async def source_value(m: Message, state: FSMContext, bot: Bot):
    uid = m.from_user.id
    data = await state.get_data()
    cid = int(data["source_target"])
    raw = (m.text or "").strip()
    chat = content.forward_chat(m)
    if not accounts.channel_allowed(uid, cid):
        raise ValueError(tr("Канал недоступен."))
    if not chat:
        chat = await bot.get_chat(chat_reference(raw))
    else:
        chat = await bot.get_chat(chat.id)
    if chat.type not in {"channel", "supergroup", "group"}:
        raise ValueError(tr("Нужен канал или группа."))
    if not chat.username:
        member = await bot.get_chat_member(chat.id, uid)
        if member.status not in {
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }:
            raise ValueError(tr("Вы должны быть администратором частного источника."))
    me = await bot.get_me()
    member = await bot.get_chat_member(chat.id, me.id)
    if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
        raise ValueError(tr("Добавьте бота администратором источника."))
    existing = database.one(
        "SELECT * FROM post_sources WHERE owner_telegram_id=? AND source_chat_id=?",
        (uid, chat.id),
    )
    if (not existing or not existing["active"]) and database.one(
        "SELECT COUNT(*) n FROM post_sources WHERE owner_telegram_id=? AND active=1",
        (uid,),
    )["n"] >= accounts.source_limit(uid):
        raise ValueError(tr("Лимит источников достигнут."))
    with database.atomic():
        database.db.execute(
            "INSERT INTO post_sources(owner_telegram_id,source_chat_id,source_title,created_at,kind) VALUES(?,?,?,?,'telegram') ON CONFLICT(owner_telegram_id,source_chat_id) DO UPDATE SET active=1,kind='telegram',url=NULL,source_title=excluded.source_title",
            (uid, chat.id, chat.title, timeutils.iso()),
        )
        sid = database.one(
            "SELECT id FROM post_sources WHERE owner_telegram_id=? AND source_chat_id=?",
            (uid, chat.id),
        )["id"]
        database.db.execute(
            "INSERT OR IGNORE INTO source_targets(source_id,channel_id) VALUES(?,?)",
            (sid, cid),
        )
    await state.clear()
    text, markup = ui.source_card(sid, uid)
    await m.answer(tr("✅ Источник подключён.\n") + text, reply_markup=markup)
