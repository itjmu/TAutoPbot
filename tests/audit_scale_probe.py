"""Offline audit reproductions. Uses only synthetic data and mocked Telegram."""

import asyncio
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_planning as fixtures
from aiogram.fsm.storage.base import StorageKey

from app import database as db
from app import timeutils, ui
from app.features import channels, posts, sources
from services import contests
from services.event_isolation import EventIsolation


async def functional_probes():
    case = fixtures.PlanningTests()
    case.setUp()
    try:
        for _ in range(200):
            case.contest(status="active")
        due = case.contest(status="scheduled")
        db.execute(
            "UPDATE contests SET starts_at=? WHERE id=?",
            (timeutils.iso(timeutils.now()), due),
        )
        with (
            patch.object(contests, "process_contest", new=AsyncMock()) as process,
            patch.object(contests, "closing_tick", new=AsyncMock()),
            patch.object(contests, "deliver", new=AsyncMock()),
            patch.object(contests, "refresh_count", new=AsyncMock()),
        ):
            await contests.tick(case.bot)
        selected = [call.args[1]["id"] for call in process.await_args_list]
        db.db.executemany(
            "INSERT INTO contest_entries(contest_id,user_id,joined_at,base_valid) VALUES(?,?,?,1)",
            ((due, 100000 + i, timeutils.iso()) for i in range(50000)),
        )
        db.db.commit()
        started = time.perf_counter()
        for _ in range(10):
            contests.participant_count(due)
        count_seconds = time.perf_counter() - started
        started = time.perf_counter()
        for _ in range(10):
            db.one(
                "SELECT COUNT(*) FROM contest_entries e WHERE e.contest_id=? AND e.base_valid=1 AND e.user_id NOT IN (SELECT telegram_id FROM blocked_users) AND (SELECT COUNT(*) FROM contest_entries r WHERE r.contest_id=e.contest_id AND r.inviter_id=e.user_id AND r.base_valid=1)>=0",
                (due,),
            )
        baseline_count_seconds = time.perf_counter() - started
        db.execute(
            "UPDATE contest_entries SET inviter_id=100000+(user_id-100000)/3 WHERE contest_id=?",
            (due,),
        )
        db.execute("UPDATE contests SET referral_min=2 WHERE id=?", (due,))
        started = time.perf_counter()
        for _ in range(10):
            baseline_referrals = db.one(
                "SELECT COUNT(*) FROM contest_entries e WHERE e.contest_id=? AND e.base_valid=1 AND e.user_id NOT IN (SELECT telegram_id FROM blocked_users) AND (SELECT COUNT(*) FROM contest_entries r WHERE r.contest_id=e.contest_id AND r.inviter_id=e.user_id AND r.base_valid=1)>=2",
                (due,),
            )[0]
        referral_baseline = time.perf_counter() - started
        started = time.perf_counter()
        for _ in range(10):
            grouped_referrals = contests.participant_count(due)
        referral_current = time.perf_counter() - started
        ui.note_transient(42, list(range(1000, 1100)))
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import DeleteMessage

        case.bot.delete_message.side_effect = TelegramBadRequest(
            method=DeleteMessage(chat_id=42, message_id=1000),
            message="message to delete not found",
        )
        await ui.cleanup_home(case.bot, 42)
        cleanup_calls = dict(
            delete=case.bot.delete_message.await_count,
            fallback_edits=case.bot.edit_message_reply_markup.await_count,
        )

        async def get_chat(chat_id):
            await asyncio.sleep(0.005)
            return SimpleNamespace(
                id=chat_id,
                type="supergroup",
                title="Synthetic",
                username=None,
                invite_link=None,
                is_forum=False,
            )

        case.bot.get_chat = get_chat
        chat = await get_chat(-100555)
        with patch.object(
            channels.access, "owner_and_bot_ok", new=AsyncMock(return_value=True)
        ):
            await asyncio.gather(
                channels.register_channel(42, case.bot, chat),
                channels.register_channel(43, case.bot, chat),
                return_exceptions=True,
            )
            with patch.object(channels.accounts, "channel_limit", return_value=1):
                chats = [await get_chat(-100600 - i) for i in range(3)]
                await asyncio.gather(
                    *(channels.register_channel(44, case.bot, chat) for chat in chats),
                    return_exceptions=True,
                )
        for uid in (42, 43):
            db.execute(
                "INSERT INTO post_sources(owner_telegram_id,source_chat_id,source_title,created_at) VALUES(?,-100777,'Synthetic',?)",
                (uid, timeutils.iso()),
            )
        db.execute(
            "INSERT INTO usage_daily(telegram_id,day,action,count) VALUES(42,?,'post_create',3)",
            (timeutils.now().date().isoformat(),),
        )
        from aiogram.types import Message

        message = Message(
            message_id=200,
            date=0,
            chat=dict(
                id=-100777,
                type="supergroup",
                username="public_source",
                title="Synthetic",
            ),
            text="Synthetic source post",
        )
        source_error = None
        with patch.object(posts, "show_post", new=AsyncMock()):
            try:
                await sources.ingest_telegram(message, case.bot)
            except ValueError as exc:
                source_error = type(exc).__name__
        return dict(
            contest_selection=dict(
                selected=len(selected), due_contest_selected=due in selected
            ),
            registration_race=dict(
                active_owners_same_chat=db.one(
                    "SELECT COUNT(*) FROM channels WHERE telegram_chat_id=-100555 AND is_active=1"
                )[0],
                active_channels_with_limit_one=db.one(
                    "SELECT COUNT(*) FROM channels WHERE owner_telegram_id=44 AND is_active=1"
                )[0],
            ),
            participant_count=dict(
                entries=50000,
                iterations=10,
                seconds=round(count_seconds, 6),
                baseline_seconds=round(baseline_count_seconds, 6),
            ),
            obsolete_cleanup=cleanup_calls,
            referral_count=dict(
                entries=50000,
                iterations=10,
                baseline_seconds=round(referral_baseline, 6),
                current_seconds=round(referral_current, 6),
                same_count=baseline_referrals == grouped_referrals,
                count=grouped_referrals,
            ),
            source_fanout=dict(
                first_owner_error=source_error,
                second_owner_received_draft=bool(
                    db.one("SELECT 1 FROM posts WHERE owner_telegram_id=43")
                ),
            ),
        )
    finally:
        case.tearDown()


def query_probes():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE post_sources(id INTEGER PRIMARY KEY,owner_telegram_id INTEGER,source_chat_id INTEGER,kind TEXT,active INTEGER); CREATE UNIQUE INDEX source_owner ON post_sources(owner_telegram_id,source_chat_id);"
    )
    conn.executemany(
        "INSERT INTO post_sources VALUES(?,?,?,'telegram',1)",
        ((i, i, -100000 - i) for i in range(50000)),
    )
    query = "SELECT * FROM post_sources WHERE source_chat_id=? AND kind='telegram' AND active=1"

    def measure():
        start = time.perf_counter()
        for _ in range(300):
            conn.execute(query, (-150000,)).fetchall()
        return round(time.perf_counter() - start, 6)

    before_plan = [
        row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + query, (-150000,))
    ]
    before = measure()
    conn.execute(
        "CREATE INDEX audit_source_chat ON post_sources(source_chat_id,kind,active)"
    )
    after = measure()
    after_plan = [
        row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + query, (-150000,))
    ]
    conn.close()
    return dict(
        rows=50000,
        lookups=300,
        before_seconds=before,
        indexed_seconds=after,
        before_plan=before_plan,
        after_plan=after_plan,
    )


def draw_probe():
    entries = [
        dict(user_id=i, inviter_id=i // 3, joined_at="2026-01-01") for i in range(50000)
    ]
    scores = {entry["user_id"]: 0 for entry in entries}
    for entry in entries:
        scores[entry["inviter_id"]] += 1

    def baseline():
        eligible = entries.copy()
        result = []
        for _ in range(100):
            weights = [1 + 2 * scores[entry["user_id"]] for entry in eligible]
            ticket = contests.secrets.randbelow(sum(weights))
            for index, weight in enumerate(weights):
                ticket -= weight
                if ticket < 0:
                    result.append(eligible.pop(index))
                    break
        return result

    with patch.object(contests.secrets, "randbelow", return_value=0):
        started = time.perf_counter()
        previous = baseline()
        before = time.perf_counter() - started
        started = time.perf_counter()
        current = contests.choose(entries, "weighted", 100, 2)
        after = time.perf_counter() - started
    return dict(
        entries=len(entries),
        winners=100,
        baseline_seconds=round(before, 6),
        current_seconds=round(after, 6),
        same_ticket_outcome=[entry["user_id"] for entry in previous]
        == [entry["user_id"] for entry, _ in current],
    )


async def lock_probe():
    with tempfile.TemporaryDirectory(prefix="tautopbot-audit-") as folder:
        path = str(Path(folder) / "synthetic.db")
        first = sqlite3.connect(path, timeout=0.1)
        second = sqlite3.connect(path)
        first.execute("PRAGMA journal_mode=WAL")
        first.execute("CREATE TABLE example(value INTEGER)")
        first.commit()
        second.execute("BEGIN IMMEDIATE")
        start = time.perf_counter()
        try:
            first.execute("INSERT INTO example VALUES(1)")
            error = None
        except sqlite3.OperationalError as exc:
            error = str(exc)
        elapsed = time.perf_counter() - start
        second.rollback()
        first.close()
        second.close()
    isolation = EventIsolation()
    for uid in range(10000):
        async with isolation.lock(StorageKey(bot_id=1, chat_id=uid, user_id=uid)):
            pass
    retained = len(isolation.locks)
    await isolation.close()
    return dict(
        synchronous_write_block_seconds=round(elapsed, 6),
        write_error=error,
        finished_user_locks_retained=retained,
        external_lock_test="Uncoordinated external writer intentionally remains a contention failure",
    )


async def main():
    result = await functional_probes()
    result["source_query"] = query_probes()
    result["weighted_draw"] = draw_probe()
    result["sqlite_and_isolation"] = await lock_probe()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
