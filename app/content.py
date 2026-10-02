"""content components."""

import html
import json
import re
import secrets
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from aiogram.types import (
    InlineKeyboardButton,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
)

from app import database as database
from app import timeutils as timeutils
from app import ui as ui
from app.i18n import tr


def forward_chat(m):
    origin = m.forward_origin
    return getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)


def utf16len(text):
    return len(text.encode("utf-16-le")) // 2


def entity_list(value):
    return json.loads(value or "[]") if isinstance(value, str) else list(value or [])


def slice_entities(entities, start, length):
    """Clip formatting to a UTF-16 substring and rebase its offsets."""
    result = []
    for entity in entity_list(entities):
        left = max(start, entity["offset"])
        right = min(start + length, entity["offset"] + entity["length"])
        if right > left:
            result.append(dict(entity, offset=left - start, length=right - left))
    return result


def input_entities(message, raw, value):
    entities = (
        getattr(message, "entities", None)
        or getattr(message, "caption_entities", None)
        or []
    )
    start = utf16len(raw[: len(raw) - len(raw.lstrip())])
    return slice_entities(
        [e.model_dump(mode="json", exclude_none=True) for e in entities],
        start,
        utf16len(value),
    )


def valid_url(value):
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or re.search(r"[\s\x00-\x1f\\]", value)
    ):
        return False
    try:
        p = urlsplit(value)
        return (
            p.scheme.lower() in {"https", "http"}
            and bool(p.hostname)
            and not p.username
            and not p.password
            and (p.port is None or 1 <= p.port <= 65535)
            and ("." in p.hostname or ":" in p.hostname)
        )
    except ValueError:
        return False


def own_link(value, uid):
    value = html.unescape(value).strip()
    channels = database.all_rows(
        "SELECT telegram_chat_id,username,invite_link FROM channels WHERE owner_telegram_id=? AND is_active=1",
        (uid,),
    )
    names = {r["username"].casefold() for r in channels if r["username"]}
    if value.startswith("@"):
        return value[1:].casefold() in names
    candidate = value if "://" in value else "https://" + value
    if not valid_url(candidate):
        return False
    p = urlsplit(candidate)
    if p.hostname.casefold() not in {
        "t.me",
        "www.t.me",
        "telegram.me",
        "www.telegram.me",
    } or p.port not in {None, 80, 443}:
        return False
    path = unquote(p.path).strip("/").split("/")
    if not path or not path[0]:
        return False
    if path[0] in {"joinchat"} or path[0].startswith("+"):
        return any(
            r["invite_link"]
            and urlsplit(r["invite_link"]).path.rstrip("/") == p.path.rstrip("/")
            for r in channels
        )
    if path[0] == "s":
        path = path[1:]
    if path and path[0] == "c" and len(path) > 1:
        return any(str(r["telegram_chat_id"]) == "-100" + path[1] for r in channels)
    return bool(path and path[0].casefold() in names)


LINK_RE = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.-]*://|www\.|(?:t\.me|telegram\.me)/)[^\s<>]+|(?<![\w@])@[a-z0-9_]{1,64}|(?<![\w@])(?:[\w-]+\.)+[a-z\u0400-\u04ff]{2,63}(?:/[^\s<>]*)?"
)


def clean_text_links(text, entities, uid):
    """Telegram offsets are UTF-16 units, not Python character indices."""
    entities = entity_list(entities)
    cuts = []
    keep = []
    encoded = text.encode("utf-16-le")
    n = len(encoded) // 2
    for e in entities:
        e = dict(e)
        start = e["offset"]
        end = start + e["length"]
        if start < 0 or end > n:
            continue
        typ = e["type"]
        visible = encoded[start * 2 : end * 2].decode("utf-16-le")
        if typ == "text_link" and not own_link(e.get("url", ""), uid):
            continue  # Preserve anchor text, remove its hidden hyperlink.
        if typ == "text_mention":
            continue
        if typ in {"url", "mention", "email", "phone_number"} and not own_link(
            visible, uid
        ):
            cuts.append((start, end))
            continue
        keep.append(e)
    for match in LINK_RE.finditer(text):
        value = match.group().rstrip(".,!?;:)]}")
        if not own_link(value, uid):
            start = utf16len(text[: match.start()])
            cuts.append((start, start + utf16len(value)))
    removed = [False] * n
    for start, end in cuts:
        for i in range(max(0, start), min(n, end)):
            removed[i] = True
    mapping = [0]
    for flag in removed:
        mapping.append(mapping[-1] + int(not flag))
    output = b"".join(
        encoded[i * 2 : i * 2 + 2] for i in range(n) if not removed[i]
    ).decode("utf-16-le")
    adjusted = []
    for e in keep:
        start, end = e["offset"], e["offset"] + e["length"]
        length = mapping[end] - mapping[start]
        if length:
            e.update(offset=mapping[start], length=length)
            adjusted.append(e)
    return output, adjusted


class TemplateHTML(HTMLParser):
    TAGS = {
        "b": "bold",
        "strong": "bold",
        "i": "italic",
        "em": "italic",
        "u": "underline",
        "s": "strikethrough",
        "del": "strikethrough",
        "code": "code",
        "pre": "pre",
        "a": "text_link",
        "blockquote": "blockquote",
        "tg-spoiler": "spoiler",
        "tg-emoji": "custom_emoji",
        "strike": "strikethrough",
        "ins": "underline",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.entities = []
        self.stack = []

    def handle_data(self, data):
        self.text += data

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.text += "\n"
            return
        if tag not in self.TAGS:
            raise ValueError(tr("HTML-тег {v0} не поддерживается.", v0=tag))
        attrs = dict(attrs)
        e = {"type": self.TAGS[tag], "offset": utf16len(self.text), "length": 0}
        if tag == "a":
            href = attrs.get("href", "")
            mention = re.fullmatch(r"tg://user\?id=(\d+)", href)
            if mention:
                e.update(
                    type="text_mention",
                    user={"id": int(mention[1]), "is_bot": False, "first_name": "User"},
                )
            elif not (
                valid_url(href) or re.fullmatch(r"tg://[A-Za-z0-9_/?=&.+%#-]+", href)
            ):
                raise ValueError(tr("Некорректная HTML-ссылка."))
            else:
                e["url"] = href
        if tag == "blockquote" and "expandable" in attrs:
            e["type"] = "expandable_blockquote"
        if tag == "tg-emoji":
            if not attrs.get("emoji-id", "").isdigit():
                raise ValueError(tr("Некорректный ID эмодзи."))
            e["custom_emoji_id"] = attrs["emoji-id"]
        if tag == "code" and self.stack and self.stack[-1][0] == "pre":
            language = attrs.get("class", "")
            if language.startswith("language-"):
                self.stack[-1][1]["language"] = language[9:]
            e["nested_code"] = True
        if tag == "pre" and attrs.get("language"):
            e["language"] = attrs["language"]
        self.stack.append((tag, e))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            raise ValueError(tr("HTML-теги должны быть корректно закрыты."))
        _, e = self.stack.pop()
        e["length"] = utf16len(self.text) - e["offset"]
        nested_code = e.pop("nested_code", False)
        if e["length"] and not nested_code:
            self.entities.append(e)


def parse_template_html(raw):
    p = TemplateHTML()
    p.feed(raw)
    p.close()
    if p.stack:
        raise ValueError(tr("Закройте HTML-теги."))
    return p.text, p.entities


def parse_button(raw, typ, default_row=1):
    parts = [p.strip() for p in raw.split("|")]
    if typ == "reaction":
        required = 1
    elif typ == "subscription":
        required = 3
    else:
        required = 2
    if len(parts) not in {required, required + 1}:
        raise ValueError(
            tr(
                "Неверный формат кнопки. Номер строки необязателен и указывается последним."
            )
        )
    if not parts[0] or len(parts[0]) > 50:
        raise ValueError(tr("Название кнопки: 1–50 символов."))
    row = int(parts[-1]) if len(parts) == required + 1 else default_row
    if not 1 <= row <= 100:
        raise ValueError("Строка: от 1 до 100.")
    b = {"id": secrets.token_hex(4), "type": typ, "text": parts[0], "row": row}
    if typ == "url":
        if not valid_url(parts[1]):
            raise ValueError(tr("Нужна корректная ссылка http:// или https://."))
        b["url"] = parts[1]
    elif typ == "alert":
        if not 1 <= len(parts[1]) <= 200:
            raise ValueError(tr("Подсказка: 1–200 символов."))
        b["alert"] = parts[1]
    elif typ == "subscription":
        if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}|-\d+", parts[1]):
            raise ValueError(tr("Укажите @username или числовой ID канала."))
        if not 1 <= len(parts[2]) <= 200:
            raise ValueError(tr("Скрытый текст: 1–200 символов."))
        b.update(chat=parts[1], alert=parts[2])
    elif typ != "reaction":
        raise ValueError(tr("Неизвестный тип кнопки."))
    return b


def validate_buttons(buttons):
    if any(type(b.get("row")) is not int or b["row"] < 1 for b in buttons):
        raise ValueError("Номер строки должен быть положительным целым числом.")
    if len(buttons) > 100:
        raise ValueError("Telegram допускает максимум 100 кнопок в клавиатуре.")
    if any(
        b.get("style") not in {None, "primary", "success", "danger"} for b in buttons
    ):
        raise ValueError(tr("Неверный цвет кнопки."))
    for row in {b["row"] for b in buttons}:
        if sum(b["row"] == row for b in buttons) > 8:
            raise ValueError(tr("Максимум 8 кнопок в одной строке."))


def message_payload(m):
    typ = "text"
    fid = None
    if m.media_group_id:
        pass  # Each album item is retained as its own editable draft.
    for kind in (
        "photo",
        "animation",
        "video",
        "video_note",
        "document",
        "audio",
        "voice",
    ):
        obj = getattr(m, kind, None)
        if obj:
            typ = kind
            fid = obj[-1].file_id if kind == "photo" else obj.file_id
            break
    if typ == "text" and m.text is None:
        raise ValueError(
            tr(
                "Поддерживаются текст, фото, видео, кружок, GIF, документ, аудио и voice."
            )
        )
    buttons = []
    if m.reply_markup:
        for row_no, row in enumerate(m.reply_markup.inline_keyboard, 1):
            for b in row:
                if b.url and valid_url(b.url):
                    buttons.append(
                        {
                            "id": secrets.token_hex(4),
                            "type": "url",
                            "text": b.text[:50],
                            "url": b.url,
                            "row": row_no,
                        }
                    )
    chat = forward_chat(m)
    return dict(
        content_type=typ,
        cover_file_id=(
            m.video.cover[-1].file_id if typ == "video" and m.video.cover else None
        ),
        file_id=fid,
        text=(m.text if typ == "text" else m.caption) or "",
        entities_json=json.dumps(
            [e.model_dump(mode="json", exclude_none=True) for e in (m.entities or [])]
        ),
        caption_entities_json=json.dumps(
            [
                e.model_dump(mode="json", exclude_none=True)
                for e in (m.caption_entities or [])
            ]
        ),
        source_chat_id=chat.id if chat else None,
        source_message_id=getattr(m.forward_origin, "message_id", None),
        buttons_json=json.dumps(buttons, ensure_ascii=False),
    )


def create_draft(uid, payload, targets=()):
    fields = [
        "content_type",
        "text",
        "file_id",
        "entities_json",
        "caption_entities_json",
        "source_chat_id",
        "source_message_id",
        "buttons_json",
        "media_json",
        "cover_file_id",
    ]
    with database.atomic():
        cur = database.db.execute(
            "INSERT INTO posts(owner_telegram_id,created_at,"
            + ",".join(fields)
            + ") VALUES("
            + ",".join("?" for _ in range(len(fields) + 2))
            + ")",
            (uid, timeutils.iso(), *[payload.get(f) for f in fields]),
        )
        pid = cur.lastrowid
        database.db.execute("UPDATE posts SET remove_links=0 WHERE id=?", (pid,))
        for cid in targets:
            if database.one(
                "SELECT 1 FROM channels WHERE id=? AND owner_telegram_id=?", (cid, uid)
            ):
                database.db.execute(
                    "INSERT INTO post_targets(post_id,channel_id) VALUES(?,?)",
                    (pid, cid),
                )
    return pid


def post_owned(pid, uid):
    p = database.one(
        "SELECT * FROM posts WHERE id=? AND owner_telegram_id=?", (pid, uid)
    )
    if not p:
        raise ValueError(tr("Пост не найден или принадлежит другому пользователю."))
    return p


def mutable_post(pid, uid):
    p = post_owned(pid, uid)
    if p["status"] not in {"draft", "scheduled"}:
        raise ValueError(tr("Отправленный пост нельзя редактировать. Создайте новый."))
    if database.one(
        "SELECT 1 FROM post_targets WHERE post_id=? AND status IN ('sent','sending','uncertain')",
        (pid,),
    ):
        raise ValueError(tr("Публикация уже началась."))
    return p


def reset_snapshot(pid):
    database.execute(
        "UPDATE post_targets SET payload_json=NULL WHERE post_id=?", (pid,)
    )


def render_payload(p, ch, template_id=None):
    d = dict(p)
    uid = p["owner_telegram_id"]
    key = "entities_json" if d["content_type"] == "text" else "caption_entities_json"
    text = d.get("text") or ""
    entities = entity_list(d.get(key))
    buttons = entity_list(d.get("buttons_json"))
    if d.get("remove_links", True):
        text, entities = clean_text_links(text, entities, uid)
    tid = ch["default_template_id"] if template_id is None else template_id
    template = (
        database.one(
            "SELECT * FROM templates WHERE id=? AND channel_id=? AND owner_id=?",
            (tid, ch["id"], uid),
        )
        if tid and tid != -1
        else None
    )
    if template:
        prefix, pe = parse_template_html(template["before_html"])
        suffix, se = parse_template_html(
            "\n".join(
                v for v in (template["after_html"], template["signature_html"]) if v
            )
        )
        if prefix:
            prefix += "\n"
        if suffix:
            suffix = "\n" + suffix
            for e in se:
                e["offset"] += 1
        shift = utf16len(prefix)
        entities = [dict(e, offset=e["offset"] + shift) for e in entities]
        tail = utf16len(prefix + text)
        entities = pe + entities + [dict(e, offset=e["offset"] + tail) for e in se]
        text = prefix + text + suffix
        offset = max([b["row"] for b in buttons], default=0)
        buttons += [
            dict(b, row=b["row"] + offset)
            for b in entity_list(template["buttons_json"])
        ]
    if d.get("remove_links", True):
        text, entities = clean_text_links(text, entities, uid)
    limit = 4096 if d["content_type"] == "text" else 1024
    if utf16len(text) > limit:
        raise ValueError(
            tr("Текст с шаблоном длиннее лимита {v0}; сократите его.", v0=limit)
        )
    if d["content_type"] == "text" and not text.strip():
        raise ValueError(tr("После удаления ссылок пост пуст. Добавьте текст."))
    validate_buttons(buttons)
    from app.accounts import button_styles

    buttons = [
        dict(b) if b.get("style") in button_styles(uid) else dict(b, style=None)
        for b in buttons
    ]
    d.update(text=text, buttons=buttons)
    d[key] = entities
    if d["content_type"] == "album":
        items = entity_list(d.get("media_json"))
        if not items:
            raise ValueError(tr("Пустой альбом."))
        items[0] = dict(items[0], text=text, caption_entities_json=entities)
        if d.get("remove_links", True):
            for i, item in enumerate(items):
                txt, ents = clean_text_links(
                    item.get("text") or "", item.get("caption_entities_json"), uid
                )
                items[i] = dict(item, text=txt, caption_entities_json=ents)
        d["media_json"] = items
    return d


class PartialAlbumError(Exception):
    def __init__(self, messages, error):
        super().__init__(
            tr("Альбом отправлен, но доставка кнопок не подтверждена: ") + str(error)
        )
        self.messages = messages


def delivered_payload(payload, index):
    """Snapshot one delivered message, including album/note companions."""
    typ = payload["content_type"]
    if typ == "album":
        items = entity_list(payload.get("media_json"))
        if index < len(items):
            return dict(items[index], album=True)
        return dict(content_type="text", text=tr("Кнопки к публикации ↑"))
    if typ == "video_note" and index:
        return dict(
            content_type="text",
            text=payload.get("text") or "",
            entities_json=payload.get("caption_entities_json"),
        )
    if typ == "video_note":
        return dict(payload, text="", caption_entities_json=None)
    return dict(payload)


async def send_content(bot, chat_id, d, reply_markup=None, *, message_thread_id=None):
    typ = d["content_type"]
    text = d.get("text") or ""
    protection = {"protect_content": True} if d.get("protect_content") else {}
    if message_thread_id is not None:
        protection["message_thread_id"] = message_thread_id
    if typ == "album":
        classes = {
            "photo": InputMediaPhoto,
            "video": InputMediaVideo,
            "audio": InputMediaAudio,
            "document": InputMediaDocument,
        }
        items = entity_list(d.get("media_json"))
        if not 2 <= len(items) <= 10:
            raise ValueError(tr("Альбом должен содержать 2–10 элементов."))
        media = [
            classes[item["content_type"]](
                media=item["file_id"],
                caption=item.get("text") or None,
                caption_entities=entity_list(item.get("caption_entities_json")) or None,
                parse_mode=None,
                **(
                    {"cover": item.get("cover_file_id")}
                    if item["content_type"] == "video"
                    else {}
                ),
            )
            for item in items
        ]
        messages = await bot.send_media_group(chat_id, media, **protection)
        if reply_markup:
            try:
                messages.append(
                    await bot.send_message(
                        chat_id,
                        tr("Кнопки к публикации ↑"),
                        reply_markup=reply_markup,
                        parse_mode=None,
                        **protection,
                    )
                )
            except Exception as exc:
                raise PartialAlbumError(messages, exc) from exc
        return messages
    if typ == "text":
        return await bot.send_message(
            chat_id,
            text,
            entities=entity_list(d.get("entities_json")) or None,
            parse_mode=None,
            reply_markup=reply_markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            **protection,
        )
    if typ == "video_note":
        note = await bot.send_video_note(
            chat_id,
            d["file_id"],
            reply_markup=None if text else reply_markup,
            **protection,
        )
        if not text:
            return note
        try:
            description = await bot.send_message(
                chat_id,
                text,
                entities=entity_list(d.get("caption_entities_json")) or None,
                parse_mode=None,
                reply_markup=reply_markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                **protection,
            )
        except Exception as exc:
            raise PartialAlbumError([note], exc) from exc
        return [note, description]
    if typ not in {"photo", "video", "animation", "document", "audio", "voice"}:
        raise ValueError(tr("Старый copy-пост: перешлите оригинал боту заново."))
    return await getattr(bot, "send_" + typ)(
        chat_id,
        d["file_id"],
        caption=text or None,
        caption_entities=entity_list(d.get("caption_entities_json")) or None,
        parse_mode=None,
        reply_markup=reply_markup,
        **protection,
        **({"cover": d.get("cover_file_id")} if typ == "video" else {}),
    )


def build_published_markup(buttons, pid, counts=None, preview=False):
    rows = {}
    counts = counts or {}
    for b in buttons:
        typ = b["type"]
        label = b["text"]
        bid = b["id"]
        if typ == "url":
            button = InlineKeyboardButton(
                text=label, url=b["url"], style=b.get("style")
            )
        else:
            if typ == "reaction":
                label += f" {counts.get(bid, 0)}"
            button = InlineKeyboardButton(
                text=label,
                callback_data="demo" if preview else f"action:{pid}:{bid}",
                style=b.get("style"),
            )
        rows.setdefault(b["row"], []).append(button)
    return ui.kb([rows[k] for k in sorted(rows)], published=True) if rows else None
