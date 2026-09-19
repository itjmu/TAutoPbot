"""Local, deterministic UI translations; user content is never translated."""

import json
from contextvars import ContextVar
from pathlib import Path

language_context = ContextVar("language", default="ru")
_path = Path(__file__).with_name("locales")
CATALOGS = {language: {} for language in ("en", "kk")}
for language, catalog in CATALOGS.items():
    for suffix in ("", "_launch"):
        path = _path / (language + suffix + ".json")
        if path.exists():
            catalog.update(json.loads(path.read_text(encoding="utf-8")))


def tr(text, **values):
    translated = CATALOGS.get(language_context.get(), {}).get(text, text)
    if language_context.get() == "ru":
        translated = (
            translated.replace("АвтоЗаявки", "Заявки на вступление")
            .replace("до конца дня UTC", "до конца дня в вашем часовом поясе")
            .replace("Premium Trial", "Пробный Premium")
        )
    return translated.format(**values) if values else translated


def activate(uid):
    from app.preferences import get_preferences

    language_context.set(get_preferences(uid)["language"])


class Labels(dict):
    """Translate built-in labels at lookup time, not at module import time."""

    def __getitem__(self, key):
        return tr(super().__getitem__(key))

    def get(self, key, default=None):
        return self[key] if key in self else default


STATUS = Labels(
    {
        "draft": "черновик",
        "scheduled": "запланирован",
        "active": "Активен",
        "publishing": "отправляется",
        "closing": "Подводим итоги",
        "completed": "Завершён",
        "cancelled": "Отменён",
        "expired": "Срок истёк",
        "failed": "ошибка",
        "uncertain": "нужна ручная проверка доставки",
        "sent": "Доставлено",
        "pending": "Ожидает отправки",
        "sending": "отправляется",
        "published": "опубликован",
        "partial": "частично опубликован",
        "skipped": "пропущен",
        "random": "Случайно",
        "weighted": "Больше шансов за друзей",
        "ranking": "Больше приглашений",
    }
)
