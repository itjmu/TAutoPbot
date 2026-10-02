"""Known forum topics, learned from updates or explicitly named by the owner."""

from app import database as db
from app import timeutils


def remember_chat(chat):
    db.execute(
        "UPDATE channels SET is_forum=? WHERE telegram_chat_id=? AND is_forum!=?",
        (
            int(bool(getattr(chat, "is_forum", False))),
            chat.id,
            int(bool(getattr(chat, "is_forum", False))),
        ),
    )


def remember(chat_id, topic_id, name=None, closed=None):
    if not topic_id or topic_id == 1:
        return
    db.execute(
        "INSERT INTO forum_topics(chat_id,topic_id,name,closed,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(chat_id,topic_id) DO UPDATE SET name=COALESCE(excluded.name,forum_topics.name), "
        "closed=COALESCE(excluded.closed,forum_topics.closed),updated_at=excluded.updated_at "
        "WHERE (excluded.name IS NOT NULL AND excluded.name IS NOT forum_topics.name) "
        "OR (excluded.closed IS NOT NULL AND excluded.closed IS NOT forum_topics.closed)",
        (chat_id, topic_id, name, closed, timeutils.iso()),
    )


def observe(message):
    remember_chat(message.chat)
    if not getattr(message.chat, "is_forum", False):
        return
    tid = getattr(message, "message_thread_id", None)
    created = getattr(message, "forum_topic_created", None)
    edited = getattr(message, "forum_topic_edited", None)
    name = created.name if created else edited.name if edited else None
    closed = (
        1
        if getattr(message, "forum_topic_closed", None)
        else 0
        if getattr(message, "forum_topic_reopened", None) or created
        else None
    )
    remember(message.chat.id, tid, name, closed)


def known(chat_id):
    return db.all_rows(
        "SELECT * FROM forum_topics WHERE chat_id=? ORDER BY COALESCE(name,''),topic_id",
        (chat_id,),
    )
