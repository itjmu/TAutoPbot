"""Editable Free/Premium entitlements; no automatic trials."""

from app import database as db
from app.i18n import tr

# key: (default, minimum, maximum)
LIMITS = {
    "free_channels": (10, 1, 1000),
    "premium_channels": (20, 1, 1000),
    "free_sources": (2, 0, 100),
    "premium_sources": (10, 0, 100),
    "free_post_create": (3, 0, 10000),
    "premium_post_create": (-1, -1, 10000),
    "free_cover": (3, 0, 10000),
    "premium_cover": (-1, -1, 10000),
    "free_download_video": (5, 0, 10000),
    "premium_download_video": (20, 0, 10000),
    "free_button_colors": (2, 0, 3),
    "premium_button_colors": (3, 0, 3),
}


def value(key):
    default, low, high = LIMITS[key]
    raw = db.setting("plan:" + key) or str(default)
    try:
        number = int(raw)
    except ValueError:
        return default
    return number if low <= number <= high else default


def set_value(key, number, actor):
    if key not in LIMITS:
        raise ValueError(tr("Неизвестная настройка."))
    _, low, high = LIMITS[key]
    if not low <= number <= high:
        raise ValueError(
            tr("Допустимое значение: от {low} до {high}.", low=low, high=high)
        )
    with db.atomic():
        db.execute(
            "INSERT INTO app_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("plan:" + key, str(number)),
        )
        db.log_event(actor, "PLAN_CHANGED", f"{key}={number}")


def description():
    return tr(
        "💎 Тарифы Free / Premium\nКаналы: {fc} / {pc}\nИсточники: {fs} / {ps}\nНовые посты в день: {fp} / {pp}\nЗамена обложки в день: {fv} / {pv}\nСкачивания видео в день: {fd} / {pd}\nЦвета кнопок: {fb} / {pb}\n−1 означает без ограничений. Лимиты обновляются в 00:00 UTC. Пробного Premium нет.",
        fc=value("free_channels"),
        pc=value("premium_channels"),
        fs=value("free_sources"),
        ps=value("premium_sources"),
        fp=value("free_post_create"),
        pp=value("premium_post_create"),
        fv=value("free_cover"),
        pv=value("premium_cover"),
        fd=value("free_download_video"),
        pd=value("premium_download_video"),
        fb=value("free_button_colors"),
        pb=value("premium_button_colors"),
    )
