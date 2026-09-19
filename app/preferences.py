"""Per-user language and local time; stored schedule instants always use UTC."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import database, timeutils
from app.i18n import tr


def get_preferences(uid):
    row = database.one("SELECT * FROM user_preferences WHERE user_id=?", (uid,))
    return dict(row) if row else {"user_id": uid, "language": "ru", "timezone": "UTC"}


def zone(name):
    match = re.fullmatch(r"UTC([+-])(\d{1,2})(?::?(\d{2}))?", name.upper())
    if match:
        hours, minutes = int(match[2]), int(match[3] or 0)
        if hours > 14 or minutes > 59 or (hours == 14 and minutes):
            raise ValueError(tr("Укажите часовой пояс от UTC-12:00 до UTC+14:00."))
        offset = (hours * 60 + minutes) * (1 if match[1] == "+" else -1)
        if offset < -720:
            raise ValueError(tr("Укажите часовой пояс от UTC-12:00 до UTC+14:00."))
        return timezone(timedelta(minutes=offset))
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            tr("Неизвестный часовой пояс. Пример: Asia/Almaty или UTC+05:00.")
        ) from exc


def save(uid, **values):
    if set(values) - {"language", "timezone"}:
        raise ValueError(tr("Неизвестная настройка."))
    if "language" in values and values["language"] not in {"ru", "en", "kk"}:
        raise ValueError(tr("Выберите язык кнопкой."))
    if "timezone" in values:
        zone(values["timezone"])
    with database.atomic():
        database.db.execute(
            "INSERT OR IGNORE INTO user_preferences(user_id) VALUES(?)", (uid,)
        )
        for key, value in values.items():
            database.db.execute(
                f"UPDATE user_preferences SET {key}=? WHERE user_id=?", (value, uid)
            )


def local_now(uid):
    return timeutils.now().astimezone(zone(get_preferences(uid)["timezone"]))


def display(uid, value):
    dt = timeutils.parse_dt(value) if isinstance(value, str) else value
    if not dt:
        return "—"
    name = get_preferences(uid)["timezone"]
    return dt.astimezone(zone(name)).strftime("%d.%m.%Y %H:%M") + " (" + name + ")"


def to_utc(uid, naive):
    tz = zone(get_preferences(uid)["timezone"])
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError(
            tr(
                "Время попадает на перевод часов. Выберите другое время или фиксированный часовой пояс UTC±HH:MM."
            )
        )
    utc = first.astimezone(timezone.utc)
    if utc.astimezone(tz).replace(tzinfo=None) != naive:
        raise ValueError(
            tr(
                "Такого местного времени нет из-за перевода часов. Выберите другое время."
            )
        )
    return utc


def parse_local(uid, raw):
    raw = raw.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            today = local_now(uid)
            dt = dt.replace(year=today.year, month=today.month, day=today.day)
            if to_utc(uid, dt) <= timeutils.now():
                dt += timedelta(days=1)
        return to_utc(uid, dt)
    raise ValueError(
        tr("Введите дату и время: 25.12.2026 18:30. Или только время: 18:30.")
    )


def duration(raw, maximum=60 * 86400):
    match = re.fullmatch(
        r"\s*(\d+)\s*(m|min|minute|minutes|мин|м|минут|минуты|h|hour|hours|ч|час|часа|часов|сағ|сағат|d|day|days|д|день|дня|дней|күн)?\s*",
        raw.casefold(),
    )
    if not match:
        raise ValueError(tr("Введите интервал: 15 мин, 2 ч или 1 д."))
    unit = match[2] or "мин"
    factor = (
        3600
        if unit in {"h", "hour", "hours", "ч", "час", "часа", "часов", "сағ", "сағат"}
        else 86400
        if unit in {"d", "day", "days", "д", "день", "дня", "дней", "күн"}
        else 60
    )
    value = int(match[1]) * factor
    if not 60 <= value <= maximum:
        raise ValueError(tr("Интервал слишком короткий или превышает допустимый срок."))
    return value
