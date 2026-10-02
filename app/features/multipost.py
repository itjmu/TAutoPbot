"""Collect ready posts, preview a timetable, then schedule the batch atomically."""

import html
import json
from datetime import timedelta

from aiogram import F, Router
from aiogram.fsm.state import State, StatesGroup

from app import accounts, content, database, preferences, timeutils, ui
from app.features import posts
from app.i18n import STATUS, tr

router = Router(name="multipost")


class MultiInput(StatesGroup):
    posts = State()
    interval = State()
    start = State()


def owned(bid, uid, draft=False):
    row = database.one(
        "SELECT * FROM multipost_batches WHERE id=? AND owner_id=?", (bid, uid)
    )
    if not row or (draft and row["status"] != "draft"):
        raise ValueError(tr("Эта серия недоступна для редактирования."))
    return row


def items(bid):
    return database.all_rows(
        "SELECT i.*,p.status FROM multipost_items i JOIN posts p ON p.id=i.post_id WHERE batch_id=? ORDER BY position",
        (bid,),
    )


def limits(uid):
    return (20, 60) if accounts.has_premium(uid) else (10, 30)


def remember_batch_id():
    """Keep stale callback IDs from ever referring to a new series."""
    row = database.one("SELECT value FROM app_settings WHERE key='multipost_last_id'")
    latest = database.one("SELECT COALESCE(MAX(id),0) AS id FROM multipost_batches")[
        "id"
    ]
    latest = max(latest, int(row["value"]) if row else 0)
    database.db.execute(
        "INSERT OR REPLACE INTO app_settings(key,value) VALUES('multipost_last_id',?)",
        (str(latest),),
    )
    return latest


def cancel_batch(bid, uid):
    with database.atomic():
        owned(bid, uid)
        remember_batch_id()
        for item in items(bid):
            pid = item["post_id"]
            database.db.execute(
                "UPDATE scheduled_posts SET active=0 WHERE post_id=?", (pid,)
            )
            # Keep delivery records needed by live reactions, expiry and uncertain sends.
            safe = item["status"] in {
                "draft",
                "scheduled",
                "skipped",
                "failed",
            } and not database.one(
                "SELECT 1 FROM published_messages WHERE post_id=? UNION ALL SELECT 1 FROM post_targets WHERE post_id=? AND status IN ('sent','sending','uncertain')",
                (pid, pid),
            )
            if safe:
                for table in ("scheduled_posts", "post_targets", "post_reactions"):
                    database.db.execute(f"DELETE FROM {table} WHERE post_id=?", (pid,))
                database.db.execute("DELETE FROM posts WHERE id=?", (pid,))
            else:
                database.db.execute(
                    "UPDATE posts SET status='skipped' WHERE id=? AND status IN ('draft','scheduled')",
                    (pid,),
                )
        database.db.execute("DELETE FROM multipost_albums WHERE batch_id=?", (bid,))
        database.db.execute("DELETE FROM multipost_items WHERE batch_id=?", (bid,))
        database.db.execute("DELETE FROM multipost_batches WHERE id=?", (bid,))


def cleanup_cancelled(uid):
    for row in database.all_rows(
        "SELECT id FROM multipost_batches WHERE owner_id=? AND status='cancelled'",
        (uid,),
    ):
        cancel_batch(row["id"], uid)


def intake_buttons(bid):
    return ui.kb(
        [
            [ui.choice(tr("✅ Все посты добавлены"), f"multi:{bid}:done")],
            [ui.choice(tr("Убрать последний пост"), f"multi:{bid}:remove")],
            [ui.choice(tr("Отменить серию"), f"multi:{bid}:cancel")],
        ]
    )


@router.callback_query(F.data == "menu:multi")
async def menu(c, state):
    await state.clear()
    await posts.menu_posts(c)


@router.callback_query(F.data == "multi:new")
async def new(c, state):
    await state.clear()
    rows = [
        [ui.choice(ch["title"][:40], f"multi:target:{ch['id']}")]
        for ch in accounts.eligible_channels(c.from_user.id)
    ]
    if not rows:
        raise ValueError(tr("Сначала добавьте канал или группу."))
    await ui.edit(
        c,
        tr("Куда публиковать серию?"),
        ui.kb(rows + [[ui.choice(tr("Назад"), "menu:multi")]]),
    )
    await c.answer()


@router.callback_query(F.data.startswith("multi:target:"))
async def target(c, state):
    cid = int(c.data.split(":")[2])
    uid = c.from_user.id
    if not accounts.channel_allowed(uid, cid):
        raise ValueError(tr("Канал недоступен."))
    with database.atomic():
        bid = remember_batch_id() + 1
        database.db.execute(
            "INSERT INTO multipost_batches(id,owner_id,channel_id,created_at) VALUES(?,?,?,?)",
            (bid, uid, cid, timeutils.iso()),
        )
    await state.set_state(MultiInput.posts)
    await state.set_data({"batch_id": bid})
    await ui.edit(
        c,
        tr(
            "Отправляйте готовые посты по одному. Альбом считается одним постом. Когда закончите, нажмите «Все посты добавлены»."
        ),
        intake_buttons(bid),
    )
    await c.answer()


@router.message(MultiInput.posts, ~F.successful_payment, ~F.text.startswith("/"))
async def receive(m, state):
    bid = (await state.get_data())["batch_id"]
    batch = owned(bid, m.from_user.id, True)
    payload = content.message_payload(m)
    payload["message_id"] = m.message_id
    album = (
        database.one(
            "SELECT post_id FROM multipost_albums WHERE batch_id=? AND group_id=?",
            (bid, m.media_group_id),
        )
        if m.media_group_id
        else None
    )
    with database.atomic():
        if album:
            post = content.post_owned(album["post_id"], m.from_user.id)
            media = content.entity_list(post["media_json"])
            if not any(i.get("message_id") == m.message_id for i in media):
                media.append(payload)
            if len(media) > 10:
                raise ValueError(tr("В одном альбоме можно не больше 10 файлов."))
            media.sort(key=lambda i: i["message_id"])
            database.db.execute(
                "UPDATE posts SET content_type='album',media_json=? WHERE id=?",
                (json.dumps(media, ensure_ascii=False), post["id"]),
            )
        else:
            count = len(items(bid))
            maximum, _ = limits(m.from_user.id)
            if count >= maximum:
                raise ValueError(
                    tr("Достигнут лимит постов в серии. Нажмите «Все посты добавлены».")
                )
            if m.media_group_id:
                payload["media_json"] = json.dumps([payload], ensure_ascii=False)
            pid = content.create_draft(m.from_user.id, payload, [batch["channel_id"]])
            database.db.execute(
                "INSERT INTO multipost_items VALUES(?,?,?)", (bid, count, pid)
            )
            if m.media_group_id:
                database.db.execute(
                    "INSERT INTO multipost_albums VALUES(?,?,?)",
                    (bid, m.media_group_id, pid),
                )
    if not album:
        await ui.answer(
            m,
            tr("Добавлено постов: ") + str(len(items(bid))),
            reply_markup=intake_buttons(bid),
        )


async def ask_start(message, uid, bid, callback=True):
    markup = ui.kb(
        [
            [
                ui.choice(tr("Начать сейчас"), f"multi:{bid}:start:now"),
                ui.choice(tr("Через 10 мин"), f"multi:{bid}:start:later"),
            ],
            [ui.choice(tr("Завтра в 09:00"), f"multi:{bid}:start:tomorrow")],
            [ui.choice(tr("Указать дату"), f"multi:{bid}:start:custom")],
            [ui.choice(tr("Назад"), f"multi:{bid}:done")],
        ]
    )
    text = tr("Когда начать?\nСейчас: ") + preferences.display(uid, timeutils.now())
    if callback:
        await ui.edit(message, text, markup)
    else:
        await ui.answer(message, text, reply_markup=markup)


def validate_timetable(batch, uid, start, interval):
    queued = items(batch["id"])
    maximum, days = limits(uid)
    if not 1 <= len(queued) <= maximum:
        raise ValueError(tr("Количество постов превышает лимит вашего тарифа."))
    if not accounts.channel_allowed(uid, batch["channel_id"]):
        raise ValueError(tr("Канал недоступен."))
    if interval < 60 or interval * (len(queued) - 1) > days * 86400:
        raise ValueError(
            tr("Серия превышает допустимый срок: 30 дней Free / 60 дней Premium.")
        )
    if start <= timeutils.now() or start > timeutils.now() + timedelta(days=days):
        raise ValueError(tr("Выберите будущее время начала в пределах вашего тарифа."))
    return [start + timedelta(seconds=interval * i) for i in range(len(queued))]


async def summary(c, uid, bid):
    batch = owned(bid, uid, True)
    start = (
        timeutils.now() + timedelta(seconds=5)
        if batch["start_at"] == "now"
        else timeutils.parse_dt(batch["start_at"])
    )
    times = validate_timetable(batch, uid, start, batch["interval_seconds"])
    ch = database.one("SELECT title FROM channels WHERE id=?", (batch["channel_id"],))
    text = (
        tr("Проверьте расписание\n")
        + html.escape(ch["title"])
        + "\n"
        + "\n".join(
            f"{i + 1}. {preferences.display(uid, t)}" for i, t in enumerate(times)
        )
    )
    await ui.edit(
        c,
        text,
        ui.kb(
            [
                [ui.choice(tr("✅ Запланировать серию"), f"multi:{bid}:confirm")],
                [
                    ui.choice(tr("Изменить"), f"multi:{bid}:done"),
                    ui.choice(tr("Отменить"), f"multi:{bid}:cancel"),
                ],
            ]
        ),
    )


def confirm(bid, uid):
    with database.atomic():
        batch = owned(bid, uid, True)
        start = (
            timeutils.now() + timedelta(seconds=5)
            if batch["start_at"] == "now"
            else timeutils.parse_dt(batch["start_at"])
        )
        times = validate_timetable(batch, uid, start, batch["interval_seconds"])
        for item, dt in zip(items(bid), times):
            posts.schedule_post(item["post_id"], uid, dt)
        database.db.execute(
            "UPDATE multipost_batches SET status='scheduled',start_at=? WHERE id=?",
            (timeutils.iso(start), bid),
        )


@router.callback_query(F.data.regexp(r"^multi:\d+:"))
async def action(c, state):
    parts = c.data.split(":")
    bid = int(parts[1])
    op = parts[2]
    uid = c.from_user.id
    batch = owned(bid, uid)
    if op == "cancel":
        cancel_batch(bid, uid)
        await state.clear()
        await ui.edit(
            c,
            tr("Серия отменена. Уже опубликованные посты остаются в канале."),
            ui.back("menu:multi"),
        )
    elif op == "open" and batch["status"] != "draft":
        text = tr("Посты серии\n") + "\n".join(
            f"#{i['post_id']}: {STATUS.get(i['status'], i['status'])}"
            for i in items(bid)
        )
        await ui.edit(
            c,
            text,
            ui.kb(
                [
                    [ui.choice(tr("Отменить оставшиеся"), f"multi:{bid}:cancel")],
                    [ui.choice(tr("Назад"), "post:scheduled")],
                ]
            ),
        )
    elif op == "open":
        owned(bid, uid, True)
        await state.set_state(MultiInput.posts)
        await state.set_data({"batch_id": bid})
        await ui.edit(
            c, tr("Отправьте ещё посты или настройте расписание."), intake_buttons(bid)
        )
    else:
        owned(bid, uid, True)
        if op == "remove":
            queued = items(bid)
            if queued:
                last = queued[-1]
                with database.atomic():
                    database.db.execute(
                        "DELETE FROM multipost_items WHERE batch_id=? AND post_id=?",
                        (bid, last["post_id"]),
                    )
                    database.db.execute(
                        "DELETE FROM multipost_albums WHERE batch_id=? AND post_id=?",
                        (bid, last["post_id"]),
                    )
                    database.db.execute(
                        "UPDATE posts SET status='skipped' WHERE id=?",
                        (last["post_id"],),
                    )
            await ui.edit(
                c, tr("Осталось постов: ") + str(len(items(bid))), intake_buttons(bid)
            )
        elif op == "done":
            if not items(bid):
                raise ValueError(tr("Сначала добавьте хотя бы один пост."))
            await state.set_state(MultiInput.interval)
            await state.set_data({"batch_id": bid})
            await ui.edit(
                c,
                tr(
                    "С каким интервалом отправлять посты? Можно написать: 15 мин, 2 ч или 1 д."
                ),
                ui.kb(
                    [
                        [
                            ui.choice(label, f"multi:{bid}:interval:{seconds}")
                            for label, seconds in [
                                (tr("1 мин"), 60),
                                (tr("10 мин"), 600),
                                (tr("1 ч"), 3600),
                                (tr("1 д"), 86400),
                            ]
                        ],
                        [ui.choice(tr("Вернуться к постам"), f"multi:{bid}:open")],
                    ]
                ),
            )
        elif op == "interval":
            seconds = int(parts[3])
            _, days = limits(uid)
            if not 60 <= seconds <= days * 86400:
                raise ValueError(tr("Интервал выходит за лимит тарифа."))
            database.execute(
                "UPDATE multipost_batches SET interval_seconds=? WHERE id=?",
                (seconds, bid),
            )
            await state.set_state(MultiInput.start)
            await state.set_data({"batch_id": bid})
            await ask_start(c, uid, bid)
        elif op == "start":
            if parts[3] not in {"custom", "now", "later", "tomorrow"}:
                raise ValueError(tr("Выберите вариант кнопкой."))
            if parts[3] == "custom":
                await state.set_state(MultiInput.start)
                await state.set_data({"batch_id": bid})
                await ui.edit(
                    c,
                    tr("Введите дату и время: 25.12.2026 18:30.\nЧасовой пояс: ")
                    + preferences.get_preferences(uid)["timezone"],
                    ui.back(f"multi:{bid}:done"),
                )
            else:
                start = (
                    "now"
                    if parts[3] == "now"
                    else timeutils.iso(timeutils.now() + timedelta(minutes=10))
                    if parts[3] == "later"
                    else timeutils.iso(
                        preferences.to_utc(
                            uid,
                            (preferences.local_now(uid) + timedelta(days=1)).replace(
                                hour=9, minute=0, second=0, microsecond=0, tzinfo=None
                            ),
                        )
                    )
                )
                database.execute(
                    "UPDATE multipost_batches SET start_at=? WHERE id=?", (start, bid)
                )
                await summary(c, uid, bid)
        elif op == "confirm":
            confirm(bid, uid)
            await state.clear()
            await ui.edit(
                c,
                tr("✅ Серия запланирована. Бот отправит посты автоматически."),
                ui.back("post:scheduled"),
            )
    await c.answer()


@router.message(MultiInput.interval, F.text, ~F.text.startswith("/"))
async def interval_text(m, state):
    bid = (await state.get_data())["batch_id"]
    owned(bid, m.from_user.id, True)
    value = preferences.duration(m.text, limits(m.from_user.id)[1] * 86400)
    database.execute(
        "UPDATE multipost_batches SET interval_seconds=? WHERE id=?", (value, bid)
    )
    await state.set_state(MultiInput.start)
    await ask_start(m, m.from_user.id, bid, False)


@router.message(MultiInput.start, F.text, ~F.text.startswith("/"))
async def start_text(m, state):
    bid = (await state.get_data())["batch_id"]
    batch = owned(bid, m.from_user.id, True)
    dt = preferences.parse_local(m.from_user.id, m.text)
    times = validate_timetable(batch, m.from_user.id, dt, batch["interval_seconds"])
    database.execute(
        "UPDATE multipost_batches SET start_at=? WHERE id=?", (timeutils.iso(dt), bid)
    )
    await ui.answer(
        m,
        tr("Проверьте расписание\n")
        + "\n".join(
            f"{i + 1}. {preferences.display(m.from_user.id, t)}"
            for i, t in enumerate(times)
        ),
        reply_markup=ui.kb(
            [
                [ui.choice(tr("✅ Запланировать серию"), f"multi:{bid}:confirm")],
                [ui.choice(tr("Изменить"), f"multi:{bid}:done")],
            ]
        ),
    )
