"""Atomic single-choice reactions and per-publication markup serialization."""

import json
from collections import Counter
from weakref import WeakValueDictionary

from app import database as db
from app import timeutils

locks = WeakValueDictionary()


def inherit_edited(uid, data):
    """Keep voter identities when a publication gets a new editor session."""
    if (
        (data.get("reaction_voters_ready") and data.get("closed"))
        or "token" not in data
        or "buttons" not in data
    ):
        return
    oid = int(data["token"], 16)
    current = json.loads(
        db.setting(f"published_edit:{data['chat_id']}:{data['message_id']}") or "{}"
    )
    source_owner, source_id = current.get("button_owner"), current.get("button_session")
    source = json.loads(db.setting(f"live_session:{source_owner}:{source_id}") or "{}")
    votes = []
    baseline = Counter()
    if source.get("reaction_voters_ready") and (source_owner, source_id) != (uid, oid):
        votes = db.all_rows(
            "SELECT user_id,button_id FROM edited_reactions WHERE owner_id=? AND session_id=? ORDER BY rowid",
            (source_owner, source_id),
        )
        offsets = {
            b["id"]: b.get("initial_count", 0) for b in source.get("buttons", [])
        }
    else:
        # Upgrade legacy aggregate snapshots without dropping identifiable voters.
        votes = db.all_rows(
            "SELECT r.user_id,r.button_id FROM post_reactions r JOIN channels c ON c.id=r.channel_id "
            "WHERE c.telegram_chat_id=? AND r.telegram_message_id=? ORDER BY r.created_at,r.rowid",
            (data["chat_id"], data["message_id"]),
        )
        baseline.update(r["button_id"] for r in votes)
        sessions = set() if data.get("reaction_voters_ready") else {oid}
        for record in db.all_rows(
            "SELECT value FROM app_settings WHERE key LIKE ?",
            (f"live_session:{uid}:%",),
        ):
            old = json.loads(record["value"])
            if old.get("closed") and (old.get("chat_id"), old.get("message_id")) == (
                data["chat_id"],
                data["message_id"],
            ):
                sessions.add(int(old["token"], 16))
        older = (
            db.all_rows(
                "SELECT user_id,button_id,session_id FROM edited_reactions WHERE owner_id=? AND session_id IN ("
                + ",".join("?" for _ in sessions)
                + ") ORDER BY rowid",
                (uid, *sessions),
            )
            if sessions
            else []
        )
        baseline.update(r["button_id"] for r in older if r["session_id"] != oid)
        votes += older
        offsets = {
            b["id"]: max(0, b.get("initial_count", 0) - baseline[b["id"]])
            for b in data["buttons"]
        }
    selections = {r["user_id"]: r["button_id"] for r in votes}
    valid = {b["id"] for b in data["buttons"] if b["type"] == "reaction"}
    with db.atomic():
        db.execute(
            "DELETE FROM edited_reactions WHERE owner_id=? AND session_id=?", (uid, oid)
        )
        db.db.executemany(
            "INSERT INTO edited_reactions(owner_id,session_id,button_id,user_id) VALUES(?,?,?,?)",
            [
                (uid, oid, bid, voter)
                for voter, bid in selections.items()
                if bid in valid
            ],
        )
        for b in data["buttons"]:
            if b["type"] == "reaction":
                b["initial_count"] = offsets.get(b["id"], 0)
        data["reaction_voters_ready"] = True
        db.execute(
            "INSERT OR REPLACE INTO app_settings(key,value) VALUES(?,?)",
            (f"live_session:{uid}:{oid}", json.dumps(data, ensure_ascii=False)),
        )


def toggle(table, scope, bid, uid):
    allowed = {
        "post_reactions": {"post_id", "channel_id", "telegram_message_id"},
        "edited_reactions": {"owner_id", "session_id"},
    }
    if table not in allowed or set(scope) != allowed[table]:
        raise ValueError("Invalid reaction scope")
    where = " AND ".join(key + "=?" for key in scope) + " AND user_id=?"
    params = (*scope.values(), uid)
    with db.atomic():
        previous = db.one(
            f"SELECT 1 FROM {table} WHERE {where} AND button_id=?", (*params, bid)
        )
        # Also normalize this user's legacy multiple selections without resetting others.
        db.execute(f"DELETE FROM {table} WHERE {where}", params)
        if not previous:
            values = dict(scope, button_id=bid, user_id=uid)
            if table == "post_reactions":
                values["created_at"] = timeutils.iso()
            db.execute(
                f"INSERT INTO {table} ({','.join(values)}) VALUES({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
    return not bool(previous)
