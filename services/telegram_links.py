"""Normalize chat references without confusing invite links with usernames."""

import re
from urllib.parse import urlsplit

from app.i18n import tr


def chat_reference(value: str):
    value = value.strip()
    if re.fullmatch(r"-\d+", value):
        return int(value)
    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", value):
        return value
    if value.startswith(("t.me/", "telegram.me/")):
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname in {
        "t.me",
        "www.t.me",
        "telegram.me",
        "www.telegram.me",
    }:
        parts = parsed.path.strip("/").split("/")
        if parts[0] == "s":
            parts = parts[1:]
        if parts and parts[0] == "c" and len(parts) > 1 and parts[1].isdigit():
            return int("-100" + parts[1])
        if (
            parts
            and re.fullmatch(r"[A-Za-z0-9_]{5,32}", parts[0])
            and parts[0] != "joinchat"
        ):
            return "@" + parts[0]
    raise ValueError(
        tr(
            "Отправьте @username, ссылку t.me/username, ID -100… или перешлите пост. По частной пригласительной ссылке Bot API не может определить канал."
        )
    )


def supports_requests(channel):
    return channel["chat_type"] in {"group", "supergroup"} or (
        channel["chat_type"] == "channel" and not channel["username"]
    )


async def subscription_link(bot, data):
    if data.get("url"):
        return data["url"]
    chat = await bot.get_chat(int(data["chat_id"]))
    if chat.username:
        return "https://t.me/" + chat.username
    if chat.invite_link:
        return chat.invite_link
    invite = await bot.create_chat_invite_link(chat.id, name="Subscription condition")
    return invite.invite_link
