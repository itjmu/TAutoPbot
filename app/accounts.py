"""accounts components."""

from datetime import timedelta

from app import access as access
from app import database as database
from app import plans
from app import timeutils as timeutils
from app.i18n import Labels, tr


def ensure_user(user):
    with database.atomic():
        fresh = not database.one("SELECT 1 FROM users WHERE telegram_id=?", (user.id,))
        database.db.execute(
            "INSERT INTO users(telegram_id,username,first_name,last_name,created_at,last_active_at) VALUES(?,?,?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,last_name=excluded.last_name,last_active_at=excluded.last_active_at",
            (
                user.id,
                user.username,
                user.first_name,
                user.last_name,
                timeutils.iso(),
                timeutils.iso(),
            ),
        )
        database.db.execute(
            "INSERT OR IGNORE INTO premium(user_id,created_at,updated_at) VALUES(?,?,?)",
            (user.id, timeutils.iso(), timeutils.iso()),
        )
        if fresh:
            database.db.execute(
                "INSERT INTO logs(telegram_id,event,created_at) VALUES(?,?,?)",
                (user.id, "USER_REGISTERED", timeutils.iso()),
            )


def blocked(uid: int) -> bool:
    return (
        database.one("SELECT 1 FROM blocked_users WHERE telegram_id=?", (uid,))
        is not None
    )


def premium_expiry(uid: int):
    r = database.one("SELECT expires_at FROM premium WHERE user_id=?", (uid,))
    return timeutils.parse_dt(r["expires_at"]) if r else None


def has_premium(uid: int) -> bool:
    row = database.one("SELECT * FROM premium WHERE user_id=?", (uid,))
    if not row or not row["active"]:
        return False
    if row["lifetime"]:
        return True
    exp = timeutils.parse_dt(row["expires_at"])
    return bool(exp and exp > timeutils.now())


def activate_premium(uid: int, days: int, reason="manual"):
    if not 0 < days <= 36500:
        raise ValueError(tr("Срок: 1–36500 дней."))
    with database.atomic():
        premium_change_tx(uid, "add", days, reason)


def start_trial(uid: int) -> bool:
    # Retained for old callbacks/imports; trials can never grant access.
    return False


def channel_limit(uid: int):
    return plans.value(("premium" if has_premium(uid) else "free") + "_channels")


def channel_count(uid: int):
    return database.one(
        "SELECT COUNT(*) c FROM channels WHERE owner_telegram_id=? AND is_active=1",
        (uid,),
    )["c"]


def use_daily(uid: int, action: str, amount=1) -> bool:
    if amount < 0:
        raise ValueError("Negative usage")
    limit = plans.value(("premium" if has_premium(uid) else "free") + "_" + action)
    day = timeutils.now().date().isoformat()
    with database.atomic():
        r = database.one(
            "SELECT count FROM usage_daily WHERE telegram_id=? AND day=? AND action=?",
            (uid, day, action),
        )
        if limit >= 0 and (r["count"] if r else 0) + amount > limit:
            return False
        database.execute(
            "INSERT INTO usage_daily(telegram_id,day,action,count) VALUES(?,?,?,?) ON CONFLICT(telegram_id,day,action) DO UPDATE SET count=count+excluded.count",
            (uid, day, action, amount),
        )
    return True


def refund_daily(uid, action, day, amount=1):
    database.execute(
        "UPDATE usage_daily SET count=MAX(0,count-?) WHERE telegram_id=? AND day=? AND action=?",
        (amount, uid, day, action),
    )


def button_styles(uid):
    count = plans.value(("premium" if has_premium(uid) else "free") + "_button_colors")
    return [None] + ["primary", "success", "danger"][:count]


def eligible_channels(uid):
    return database.all_rows(
        "SELECT * FROM channels WHERE owner_telegram_id=? AND is_active=1 ORDER BY id LIMIT ?",
        (uid, channel_limit(uid)),
    )


def channel_allowed(uid, cid):
    return any(r["id"] == cid for r in eligible_channels(uid))


def source_limit(uid):
    return plans.value(("premium" if has_premium(uid) else "free") + "_sources")


def source_allowed(row):
    if blocked(row["owner_telegram_id"]):
        return False
    rows = database.all_rows(
        "SELECT id FROM post_sources WHERE owner_telegram_id=? AND active=1 ORDER BY id LIMIT ?",
        (row["owner_telegram_id"], source_limit(row["owner_telegram_id"])),
    )
    return row["id"] in {r["id"] for r in rows}


def premium_change_tx(uid, mode, value, reason, actor=0):
    if not database.one("SELECT 1 FROM users WHERE telegram_id=?", (uid,)):
        raise ValueError(tr("Пользователь должен сначала запустить бота."))
    database.db.execute(
        "INSERT OR IGNORE INTO premium(user_id,created_at,updated_at) VALUES(?,?,?)",
        (uid, timeutils.iso(), timeutils.iso()),
    )
    r = database.one("SELECT * FROM premium WHERE user_id=?", (uid,))
    old = "lifetime" if r["lifetime"] else r["expires_at"]
    exp = timeutils.parse_dt(r["expires_at"])
    lifetime = 0
    if mode == "add":
        if r["lifetime"] and r["active"]:
            lifetime = 1
        base = exp if r["active"] and exp and exp > timeutils.now() else timeutils.now()
        exp = base + timedelta(days=int(value))
    elif mode == "subtract":
        if r["lifetime"]:
            raise ValueError(tr("Сначала замените бессрочный Premium точной датой."))
        exp = max(
            timeutils.now(), (exp or timeutils.now()) - timedelta(days=int(value))
        )
    elif mode == "set":
        exp = timeutils.parse_dt(value)
        if not exp:
            raise ValueError(tr("Неверная дата."))
    elif mode == "lifetime":
        lifetime = 1
        exp = None
    elif mode == "revoke":
        exp = timeutils.now()
    else:
        raise ValueError(tr("Неизвестное действие."))
    active = bool(lifetime or (exp and exp > timeutils.now()))
    database.db.execute(
        "UPDATE premium SET active=?,lifetime=?,expires_at=?,updated_at=? WHERE user_id=?",
        (
            int(active),
            lifetime,
            timeutils.iso(exp) if exp else None,
            timeutils.iso(),
            uid,
        ),
    )
    database.db.execute(
        "INSERT INTO premium_history(user_id,actor_id,reason,old_expiry,new_expiry,created_at) VALUES(?,?,?,?,?,?)",
        (
            uid,
            actor,
            reason,
            old,
            "lifetime" if lifetime else timeutils.iso(exp),
            timeutils.iso(),
        ),
    )


async def reward_condition(uid, condition, bot):
    if condition == "none":
        return True
    has_channel = bool(
        database.one(
            "SELECT 1 FROM channels WHERE owner_telegram_id=? AND is_active=1", (uid,)
        )
    )
    used = bool(
        database.one(
            "SELECT 1 FROM posts p JOIN published_messages m ON m.post_id=p.id WHERE p.owner_telegram_id=?",
            (uid,),
        )
    )
    if condition == "channel":
        return has_channel
    if condition == "channel_and_publish":
        return has_channel and used
    if condition == "purchase":
        return bool(
            database.one(
                "SELECT 1 FROM payments WHERE telegram_id=? AND status='paid'", (uid,)
            )
        )
    if condition == "subscription":
        chats = [
            x.strip()
            for x in database.setting("ref_subscription").split(",")
            if x.strip()
        ]
        return bool(chats) and all(
            [
                await access.member_ok(
                    bot, int(x) if x.lstrip("-").isdigit() else x, uid
                )
                for x in chats
            ]
        )
    return False


async def check_referral(uid, bot):
    row = database.one(
        "SELECT * FROM referrals WHERE invited_id=? AND rewarded_at IS NULL", (uid,)
    )
    if not row or blocked(uid) or blocked(row["inviter_id"]):
        return False
    if not await reward_condition(uid, database.setting("ref_condition"), bot):
        return False
    with database.atomic():
        claimed = database.db.execute(
            "UPDATE referrals SET rewarded_at=? WHERE invited_id=? AND rewarded_at IS NULL",
            (timeutils.iso(), uid),
        ).rowcount
        if not claimed:
            return False
        for user_id, key in (
            (row["inviter_id"], "ref_inviter_days"),
            (uid, "ref_invitee_days"),
        ):
            days = int(database.setting(key))
            if days:
                premium_change_tx(user_id, "add", days, "referral")
    return True


CONDITION_LABELS = Labels(
    {
        "none": "без условия",
        "channel": "добавить канал",
        "channel_and_publish": "добавить канал и успешно опубликовать пост",
        "purchase": "оплатить Premium Stars",
        "subscription": "подписаться на заданные администратором каналы",
    }
)
