"""Durable contest draws and deliveries. A persisted draw is never repeated."""

import asyncio
import hashlib
import html
import json
import logging
import secrets
from datetime import timedelta

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton

from app import access, accounts, content, preferences, timeutils, ui
from app import database as db
from app.i18n import language_context, tr

log = logging.getLogger(__name__)
_post_locks = {}


def kind_label(row):
    return tr("Конкурс") if row["mode"] == "ranking" else tr("Розыгрыш")


def participant_count(cid):
    row = get(cid)
    table = (
        "contest_channel_referrals"
        if row["referral_target"] == "channel"
        else "contest_entries"
    )
    valid = "active=1" if row["referral_target"] == "channel" else "base_valid=1"
    return db.one(
        f"SELECT COUNT(*) FROM contest_entries e WHERE e.contest_id=? AND e.base_valid=1 AND e.user_id NOT IN (SELECT telegram_id FROM blocked_users) AND (SELECT COUNT(*) FROM {table} r WHERE r.contest_id=e.contest_id AND r.inviter_id=e.user_id AND r.{valid}) >= ?",
        (cid, row["referral_min"]),
    )[0]


def public_markup(row, count=None):
    if not is_open(row) and row["status"] not in {"scheduled", "publishing"}:
        return ui.kb([])
    buttons = []
    if row["subscription_layout"] == "buttons":
        buttons = [
            [InlineKeyboardButton(text=ch["title"][:45], url=ch["url"])]
            for ch in json.loads(row["subscriptions_json"])
        ]
    count = participant_count(row["id"]) if count is None else count
    buttons.append(
        [
            InlineKeyboardButton(
                text=tr("🎉 Участвовать · {count}", count=count),
                callback_data=f"contest:participate:{row['id']}"
                if row["id"]
                else "demo",
                style="success",
            )
        ]
    )
    return ui.kb(buttons)


async def refresh_count(bot, cid):
    lock = _post_locks.setdefault(cid, asyncio.Lock())
    async with lock:
        row = get(cid)
        if not is_open(row):
            return
        ids = json.loads(row["published_ids"])
        count = participant_count(cid)
        if not ids or row["displayed_count"] == count:
            return
        target = db.one(
            "SELECT telegram_chat_id FROM channels WHERE id=?", (row["channel_id"],)
        )
        try:
            await bot.edit_message_reply_markup(
                chat_id=target[0],
                message_id=ids[0],
                reply_markup=public_markup(row, count),
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                log.warning("Cannot update participant count for %s: %s", cid, exc)
                return
        except Exception:
            log.exception("Cannot update participant count for %s", cid)
            return
        db.execute("UPDATE contests SET displayed_count=? WHERE id=?", (count, cid))


def publication(row):
    """Preserve custom entities and append entry links and the deadline."""
    post = json.loads(row["post_json"])
    suffix = tr(
        "\n\n🎁 Приз: {prize}\n🏆 Победителей: {count}\n📅 Итоги: {date}",
        prize=row["prize_title"] or row["title"],
        count=row["winner_count"],
        date=preferences.display(row["owner_id"], row["ends_at"]),
    )
    key = "entities_json" if post["content_type"] == "text" else "caption_entities_json"
    entities = content.entity_list(post.get(key))
    text = (post.get("text") or "") + suffix
    if row["subscription_layout"] == "text":
        for ch in json.loads(row["subscriptions_json"]):
            text += "\n"
            entities.append(
                {
                    "type": "text_link",
                    "offset": content.utf16len(text),
                    "length": content.utf16len(ch["title"][:45]),
                    "url": ch["url"],
                }
            )
            text += ch["title"][:45]
    limit = 4096 if post["content_type"] == "text" else 1024
    if content.utf16len(text) > limit:
        raise ValueError(
            tr(
                "Пост с условиями слишком длинный. Сократите текст или разместите подписки кнопками."
            )
        )
    return dict(post, text=text, **{key: entities})


def digest(value):
    return hashlib.sha256(" ".join(value.casefold().split()).encode()).hexdigest()


def get(cid):
    row = db.one("SELECT * FROM contests WHERE id=?", (cid,))
    if not row:
        raise ValueError(tr("Конкурс не найден."))
    return row


def is_open(row):
    return row["status"] == "active" and timeutils.parse_dt(
        row["starts_at"]
    ) <= timeutils.now() < timeutils.parse_dt(row["ends_at"])


async def missing_subscriptions(bot, row, uid):
    missing = []
    # API failures propagate: inability to check is not evidence of leaving a chat.
    for chat in json.loads(row["subscriptions_json"]):
        member = await bot.get_chat_member(chat["chat_id"], uid)
        if member.status not in {"creator", "administrator", "member"} and not (
            member.status == "restricted" and member.is_member
        ):
            missing.append(chat)
    return missing


def referral_count(cid, uid):
    row = get(cid)
    if row["referral_target"] == "channel":
        return db.one(
            "SELECT COUNT(*) FROM contest_channel_referrals WHERE contest_id=? AND inviter_id=? AND active=1",
            (cid, uid),
        )[0]
    return db.one(
        "SELECT COUNT(*) n FROM contest_entries WHERE contest_id=? AND inviter_id=? AND base_valid=1",
        (cid, uid),
    )["n"]


def register(cid, uid, inviter=None):
    row = get(cid)
    if not is_open(row):
        raise ValueError(tr("Сейчас нельзя вступить в этот конкурс."))
    if inviter == uid or not db.one(
        "SELECT 1 FROM contest_entries WHERE contest_id=? AND user_id=?", (cid, inviter)
    ):
        inviter = None
    db.execute(
        "INSERT OR IGNORE INTO contest_entries(contest_id,user_id,inviter_id,joined_at) VALUES(?,?,?,?)",
        (cid, uid, inviter, timeutils.iso()),
    )
    return db.one(
        "SELECT * FROM contest_entries WHERE contest_id=? AND user_id=?", (cid, uid)
    )


def choose(entries, mode, count, bonus):
    counts = {entry["user_id"]: 0 for entry in entries}
    for entry in entries:
        if entry["inviter_id"] in counts:
            counts[entry["inviter_id"]] += 1
    for entry in entries:
        if "referral_score" in entry:
            counts[entry["user_id"]] = entry["referral_score"]
    eligible = [e for e in entries if counts[e["user_id"]] >= e.get("referral_min", 0)]
    if mode == "ranking":
        return [
            (e, counts[e["user_id"]])
            for e in sorted(
                eligible,
                key=lambda e: (-counts[e["user_id"]], e["joined_at"], e["user_id"]),
            )[:count]
        ]
    result = []
    while eligible and len(result) < count:
        weights = [
            1 + bonus * counts[e["user_id"]] if mode == "weighted" else 1
            for e in eligible
        ]
        ticket = secrets.randbelow(sum(weights))
        for index, weight in enumerate(weights):
            ticket -= weight
            if ticket < 0:
                entry = eligible.pop(index)
                result.append((entry, counts[entry["user_id"]]))
                break
    return result


def enqueue(cid, kind, recipient, payload):
    db.db.execute(
        "INSERT OR IGNORE INTO contest_outbox(contest_id,kind,recipient,payload) VALUES(?,?,?,?)",
        (cid, kind, recipient, json.dumps(payload, ensure_ascii=False)),
    )


async def finish(bot, row):
    cid = row["id"]
    if get(cid)["status"] != "closing":
        return
    entries = []
    channel_scores = {}
    if row["referral_target"] == "channel":
        target = db.one(
            "SELECT telegram_chat_id FROM channels WHERE id=?", (row["channel_id"],)
        )
        for referral in db.all_rows(
            "SELECT * FROM contest_channel_referrals WHERE contest_id=? AND active=1",
            (cid,),
        ):
            member = await bot.get_chat_member(target[0], referral["user_id"])
            present = member.status in {"creator", "administrator", "member"} or (
                member.status == "restricted" and member.is_member
            )
            if not accounts.blocked(referral["user_id"]) and present:
                channel_scores[referral["inviter_id"]] = (
                    channel_scores.get(referral["inviter_id"], 0) + 1
                )
    for entry in db.all_rows(
        "SELECT * FROM contest_entries WHERE contest_id=? AND base_valid=1", (cid,)
    ):
        if not accounts.blocked(entry["user_id"]) and not await missing_subscriptions(
            bot, row, entry["user_id"]
        ):
            entries.append(dict(entry, referral_min=row["referral_min"]))
            if row["referral_target"] == "channel":
                entries[-1]["referral_score"] = channel_scores.get(entry["user_id"], 0)
    # No writes or random draw until every subscription check succeeded.
    selected = choose(entries, row["mode"], row["winner_count"], row["referral_bonus"])
    prize = json.loads(row["prize_json"])
    names = []
    with db.atomic():
        if get(cid)["status"] != "closing":
            return
        db.db.execute(
            "UPDATE contest_entries SET base_valid=0 WHERE contest_id=?", (cid,)
        )
        for entry in entries:
            db.db.execute(
                "UPDATE contest_entries SET base_valid=1 WHERE contest_id=? AND user_id=?",
                (cid, entry["user_id"]),
            )
        for rank, (entry, score) in enumerate(selected, 1):
            uid = entry["user_id"]
            award = {
                "kind": row["prize_kind"],
                "value": prize["codes"][rank - 1]
                if row["prize_kind"] == "promo"
                else prize["value"],
            }
            db.db.execute(
                "INSERT INTO contest_winners VALUES(?,?,?,?,?)",
                (cid, uid, rank, score, json.dumps(award, ensure_ascii=False)),
            )
            enqueue(cid, "winner", uid, award)
            user = db.one(
                "SELECT username,first_name FROM users WHERE telegram_id=?", (uid,)
            )
            label = (
                "@" + user["username"]
                if user and user["username"]
                else (user["first_name"] if user else str(uid))
            ) or str(uid)
            names.append(
                f'{rank}. <a href="tg://user?id={uid}">{html.escape(label[:8])}</a>'
            )
        result = (
            (
                tr("🏆 Конкурс завершён: ")
                if row["mode"] == "ranking"
                else tr("🎉 Розыгрыш завершён: ")
            )
            + html.escape(row["title"][:60])
            + "\n"
            + (
                "\n".join(names)
                if names
                else tr("Нет участников, выполнивших все условия.")
            )
        )
        result += "\n\n" + (
            tr(
                "Для получения приза: {contact}",
                contact=html.escape(row["claim_contact"]),
            )
            if row["claim_contact"]
            else tr("Ожидайте, скоро с вами свяжутся для вручения призов.")
        )
        target = db.one(
            "SELECT telegram_chat_id FROM channels WHERE id=?", (row["channel_id"],)
        )
        enqueue(cid, "results", target["telegram_chat_id"], {"text": result})
        enqueue(
            cid,
            "owner",
            row["owner_id"],
            {"text": result + tr("\nПроверьте выдачу призов победителям.")},
        )
        db.db.execute(
            "UPDATE contests SET status='completed',error=NULL WHERE id=?", (cid,)
        )


async def deliver(bot):
    for item in db.all_rows(
        "SELECT * FROM contest_outbox WHERE status='pending' ORDER BY id LIMIT 40"
    ):
        payload = json.loads(item["payload"])
        row = get(item["contest_id"])
        token = language_context.set(
            preferences.get_preferences(
                item["recipient"] if item["recipient"] > 0 else row["owner_id"]
            )["language"]
        )
        try:
            if item["kind"] == "winner":
                if payload["kind"] == "invite" and not payload.get("link"):
                    invite = await bot.create_chat_invite_link(
                        int(payload["value"]),
                        member_limit=1,
                        expire_date=timeutils.now() + timedelta(days=7),
                        name=f"Contest {row['id']} winner",
                    )
                    payload["link"] = invite.invite_link
                    db.execute(
                        "UPDATE contest_outbox SET payload=? WHERE id=?",
                        (json.dumps(payload), item["id"]),
                    )
                    db.execute(
                        "UPDATE contest_winners SET prize=? WHERE contest_id=? AND user_id=?",
                        (json.dumps(payload), row["id"], item["recipient"]),
                    )
                text = tr(
                    "🎉 Вы выиграли в конкурсе «{title}»!\n{prize}",
                    title=row["title"],
                    prize=str(payload.get("link") or payload["value"]),
                )
                parse_mode = None
            else:
                text = payload["text"]
                parse_mode = "HTML"
            if (
                db.execute(
                    "UPDATE contest_outbox SET status='sending' WHERE id=? AND status='pending'",
                    (item["id"],),
                ).rowcount
                != 1
            ):
                continue
            if item["kind"] == "results":
                ids = json.loads(row["published_ids"])
                if not ids:
                    raise ValueError("Original giveaway message ID is missing")
                media = json.loads(row["post_json"])["content_type"] != "text"
                method = bot.edit_message_caption if media else bot.edit_message_text
                async with _post_locks.setdefault(row["id"], asyncio.Lock()):
                    try:
                        await method(
                            chat_id=item["recipient"],
                            message_id=ids[0],
                            **{"caption" if media else "text": text},
                            parse_mode="HTML",
                            reply_markup=ui.kb([]),
                        )
                    except TelegramBadRequest as exc:
                        if "message is not modified" not in str(exc).lower():
                            raise
                _post_locks.pop(row["id"], None)
            else:
                await bot.send_message(item["recipient"], text, parse_mode=parse_mode)
            db.execute(
                "UPDATE contest_outbox SET status='sent',error=NULL WHERE id=?",
                (item["id"],),
            )
        except TelegramRetryAfter:
            db.execute(
                "UPDATE contest_outbox SET status='pending' WHERE id=?", (item["id"],)
            )
            break
        except (TelegramBadRequest, TelegramForbiddenError, ValueError) as exc:
            db.execute(
                "UPDATE contest_outbox SET status='failed',error=? WHERE id=?",
                (str(exc)[:500], item["id"]),
            )
            if item["kind"] == "winner":
                with db.atomic():
                    enqueue(
                        row["id"],
                        f"failed:{item['recipient']}",
                        row["owner_id"],
                        {
                            "text": tr(
                                "Не удалось выдать приз участнику {v0}. Откройте конкурс и проверьте доставку приза.",
                                v0=item["recipient"],
                            )
                        },
                    )
            elif item["kind"] == "results":
                with db.atomic():
                    enqueue(
                        row["id"],
                        "results_failed",
                        row["owner_id"],
                        {
                            "text": tr(
                                "Не удалось обновить пост с итогами. Проверьте права бота и исходный пост в разделе «Мои конкурсы»."
                            )
                        },
                    )
        except Exception as exc:
            db.execute(
                "UPDATE contest_outbox SET status=CASE WHEN kind='results' THEN 'pending' WHEN status='sending' THEN 'uncertain' ELSE 'pending' END,error=? WHERE id=?",
                (str(exc)[:500], item["id"]),
            )
            log.exception("Contest delivery failed")
        finally:
            language_context.reset(token)


def recover():
    db.execute(
        "UPDATE contests SET status='uncertain',error='Перезапуск во время публикации. Проверьте канал.' WHERE status='publishing'"
    )
    db.execute(
        "UPDATE contest_outbox SET status=CASE WHEN kind='results' THEN 'pending' ELSE 'uncertain' END,error='Перезапуск во время отправки' WHERE status='sending'"
    )


async def tick(bot):
    for row in db.all_rows(
        "SELECT * FROM contests WHERE status IN ('scheduled','active','closing') ORDER BY id"
    ):
        token = language_context.set(
            preferences.get_preferences(row["owner_id"])["language"]
        )
        try:
            if (
                row["status"] == "scheduled"
                and timeutils.parse_dt(row["starts_at"]) <= timeutils.now()
            ):
                if timeutils.parse_dt(row["ends_at"]) <= timeutils.now():
                    db.execute(
                        "UPDATE contests SET status='expired',error='Бот был выключен до конца конкурса' WHERE id=?",
                        (row["id"],),
                    )
                    with db.atomic():
                        enqueue(
                            row["id"],
                            "expired",
                            row["owner_id"],
                            {
                                "text": tr(
                                    "Конкурс не опубликован: бот был выключен до его окончания."
                                )
                            },
                        )
                    continue
                target = db.one(
                    "SELECT * FROM channels WHERE id=?", (row["channel_id"],)
                )
                if (
                    accounts.blocked(row["owner_id"])
                    or not accounts.channel_allowed(row["owner_id"], row["channel_id"])
                    or not await access.owner_and_bot_ok(
                        bot, target["telegram_chat_id"], row["owner_id"]
                    )
                ):
                    raise ValueError(tr("Нет прав владельца или бота на публикацию."))
                if (
                    db.execute(
                        "UPDATE contests SET status='publishing' WHERE id=? AND status='scheduled'",
                        (row["id"],),
                    ).rowcount
                    != 1
                ):
                    continue
                target = db.one(
                    "SELECT telegram_chat_id FROM channels WHERE id=?",
                    (row["channel_id"],),
                )
                markup = public_markup(row, 0)
                sent = await content.send_content(
                    bot,
                    target["telegram_chat_id"],
                    publication(row),
                    markup,
                )
                db.execute(
                    "UPDATE contests SET status='active',published_ids=? WHERE id=?",
                    (json.dumps([sent.message_id]), row["id"]),
                )
                row = get(row["id"])
            if is_open(row):
                await refresh_count(bot, row["id"])
            if (
                row["status"] in {"active", "closing"}
                and timeutils.parse_dt(row["ends_at"]) <= timeutils.now()
            ):
                db.execute(
                    "UPDATE contests SET status='closing' WHERE id=? AND status='active'",
                    (row["id"],),
                )
                await finish(bot, get(row["id"]))
        except Exception as exc:
            db.execute(
                "UPDATE contests SET status=CASE WHEN status='publishing' THEN 'uncertain' ELSE status END,error=? WHERE id=?",
                (str(exc)[:500], row["id"]),
            )
            log.exception("Contest tick failed: %s", row["id"])
            with db.atomic():
                enqueue(
                    row["id"],
                    "error",
                    row["owner_id"],
                    {
                        "text": tr(
                            "Конкурс #{id}: действие не завершено. Откройте «Мои конкурсы» и проверьте статус.",
                            id=row["id"],
                        )
                    },
                )
        finally:
            language_context.reset(token)
    await deliver(bot)


async def referral_link(bot, row, uid):
    if row["referral_target"] != "channel":
        me = await bot.get_me()
        return f"https://t.me/{me.username}?start=contest_{row['id']}_{uid}"
    existing = db.one(
        "SELECT invite_link FROM contest_invites WHERE contest_id=? AND user_id=?",
        (row["id"], uid),
    )
    if existing:
        return existing[0]
    target = db.one(
        "SELECT telegram_chat_id FROM channels WHERE id=?", (row["channel_id"],)
    )
    invite = await bot.create_chat_invite_link(
        target[0],
        name=f"Giveaway {row['id']} {uid}"[:32],
        expire_date=timeutils.parse_dt(row["ends_at"]),
    )
    db.execute(
        "INSERT OR IGNORE INTO contest_invites VALUES(?,?,?)",
        (row["id"], uid, invite.invite_link),
    )
    return db.one(
        "SELECT invite_link FROM contest_invites WHERE contest_id=? AND user_id=?",
        (row["id"], uid),
    )[0]
