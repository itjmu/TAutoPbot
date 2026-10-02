"""database components."""

import asyncio
import secrets
import sqlite3
from contextlib import contextmanager
from threading import RLock
from typing import Optional

from app import timeutils as timeutils
from config import DB_FILE
from services.migrations import migrate_answers, migrate_sources

db: sqlite3.Connection | None = None
_worker = None
_worker_connection = None
_write_gate = RLock()
_worker_init_lock = asyncio.Lock()


def connect(path=DB_FILE):
    global db
    if db is not None:
        return db
    db = sqlite3.connect(path, timeout=0.1)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


async def async_call(callback, *, readonly=False):
    """Callback receives its own connection; do not access the global db in it."""
    global _worker, _worker_connection
    if db.in_transaction:
        raise RuntimeError("Cannot await database work inside a transaction")
    path = db.execute("PRAGMA database_list").fetchone()[2]
    if not path:  # isolated in-memory tests stay on their owning thread
        await asyncio.sleep(0)
        with atomic():
            return callback(db)
    if _worker_connection is not db:
        async with _worker_init_lock:
            if _worker_connection is not db:
                await _close_worker()
                from services.sqlite_worker import SQLiteWorker

                _worker = SQLiteWorker(path, write_gate=_write_gate)
                _worker_connection = db
    return await _worker.call(callback, readonly=readonly)


async def close_async():
    async with _worker_init_lock:
        await _close_worker()


async def _close_worker():
    global _worker, _worker_connection
    worker, _worker = _worker, None
    _worker_connection = None
    if worker is not None:
        await worker.close()


def execute(sql: str, params=()):
    with _write_gate:
        return _execute(sql, params)


def _execute(sql: str, params=()):
    in_transaction = db.in_transaction
    try:
        cur = db.execute(sql, params)
        if not in_transaction:
            db.commit()
        return cur
    except BaseException:
        if not in_transaction:
            db.rollback()
        raise


def one(sql: str, params=()):
    return db.execute(sql, params).fetchone()


def all_rows(sql: str, params=()):
    return db.execute(sql, params).fetchall()


def init_db():
    connect()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS forum_topics(
      chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, name TEXT, closed INTEGER,
      updated_at TEXT NOT NULL, PRIMARY KEY(chat_id,topic_id)
    );
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER UNIQUE NOT NULL,
      username TEXT, first_name TEXT, last_name TEXT, is_blocked INTEGER DEFAULT 0,
      created_at TEXT NOT NULL, last_active_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS premium(
      user_id INTEGER PRIMARY KEY, active INTEGER DEFAULT 0, expires_at TEXT,
      trial_used INTEGER DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS channels(
      id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_chat_id INTEGER NOT NULL,
      title TEXT NOT NULL, username TEXT, chat_type TEXT NOT NULL, owner_telegram_id INTEGER NOT NULL,
      bot_is_admin INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1,
      auto_requests INTEGER DEFAULT 0, condition_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(telegram_chat_id, owner_telegram_id)
    );
    CREATE TABLE IF NOT EXISTS conditions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, owner_telegram_id INTEGER NOT NULL,
      name TEXT NOT NULL, active INTEGER DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS condition_items(
      id INTEGER PRIMARY KEY AUTOINCREMENT, condition_id INTEGER NOT NULL,
      item_type TEXT NOT NULL, data TEXT, position INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS join_requests(
      id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_user_id INTEGER NOT NULL,
      channel_id INTEGER NOT NULL, status TEXT DEFAULT 'pending', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS request_answers(
      id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL,
      item_id INTEGER NOT NULL, answer TEXT, is_correct INTEGER DEFAULT 0, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS posts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, owner_telegram_id INTEGER NOT NULL,
      content_type TEXT NOT NULL, text TEXT, file_id TEXT, created_at TEXT NOT NULL, status TEXT DEFAULT 'draft'
    );
    CREATE TABLE IF NOT EXISTS post_targets(
      id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
      UNIQUE(post_id, channel_id)
    );
    CREATE TABLE IF NOT EXISTS scheduled_posts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL, publish_at TEXT NOT NULL,
      repeat_type TEXT DEFAULT 'once', delete_after_seconds INTEGER, active INTEGER DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS published_messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
      telegram_message_id INTEGER NOT NULL, delete_at TEXT, deleted INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS post_sources(
      id INTEGER PRIMARY KEY AUTOINCREMENT, owner_telegram_id INTEGER NOT NULL,
      source_chat_id INTEGER NOT NULL, source_title TEXT, active INTEGER DEFAULT 1, created_at TEXT NOT NULL,
      UNIQUE(owner_telegram_id, source_chat_id)
    );
    CREATE TABLE IF NOT EXISTS source_targets(
      source_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, UNIQUE(source_id, channel_id)
    );
    CREATE TABLE IF NOT EXISTS post_reactions(
      post_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, telegram_message_id INTEGER NOT NULL,
      button_id TEXT NOT NULL, user_id INTEGER NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(post_id, channel_id, telegram_message_id, button_id, user_id)
    );
    CREATE INDEX IF NOT EXISTS idx_post_reactions_count ON post_reactions(post_id, channel_id, telegram_message_id, button_id);
    CREATE INDEX IF NOT EXISTS idx_post_reactions_user ON post_reactions(post_id, channel_id, telegram_message_id, user_id);
    CREATE TABLE IF NOT EXISTS payments(
      id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, days INTEGER NOT NULL,
      amount INTEGER NOT NULL, currency TEXT NOT NULL, payload TEXT UNIQUE NOT NULL,
      telegram_charge_id TEXT, status TEXT DEFAULT 'pending', created_at TEXT NOT NULL, paid_at TEXT
    );
    CREATE TABLE IF NOT EXISTS usage_daily(
      telegram_id INTEGER NOT NULL, day TEXT NOT NULL, action TEXT NOT NULL, count INTEGER DEFAULT 0,
      PRIMARY KEY(telegram_id, day, action)
    );
    CREATE TABLE IF NOT EXISTS notifications(
      id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, kind TEXT NOT NULL,
      text TEXT NOT NULL, sent INTEGER DEFAULT 0, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS logs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER, event TEXT NOT NULL, details TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS referrals(
      id INTEGER PRIMARY KEY AUTOINCREMENT, inviter_id INTEGER NOT NULL, invited_id INTEGER UNIQUE NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS blocked_users(
      telegram_id INTEGER PRIMARY KEY, reason TEXT, created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_channels_owner ON channels(owner_telegram_id, is_active);
    CREATE INDEX IF NOT EXISTS idx_requests_channel ON join_requests(channel_id, status);
    CREATE INDEX IF NOT EXISTS idx_schedule ON scheduled_posts(active, publish_at);
    CREATE INDEX IF NOT EXISTS idx_published_delete ON published_messages(deleted, delete_at);
    """)
    # Safe migrations for databases created by the previous draft.
    migrations = {
        "channels": ["auto_requests INTEGER DEFAULT 0", "condition_id INTEGER"],
        "posts": [
            "cover_file_id TEXT",
            "buttons_json TEXT DEFAULT '[]'",
            "remove_links INTEGER DEFAULT 0",
            "source_chat_id INTEGER",
            "source_message_id INTEGER",
            "entities_json TEXT DEFAULT '[]'",
        ],
    }
    for table, cols in migrations.items():
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            name = col.split()[0]
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    db.commit()
    migrate_answers(db)
    init_extensions()
    migrate_sources(db)
    init_planning()
    init_delivery_queues()


def init_planning():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS user_preferences(user_id INTEGER PRIMARY KEY,language TEXT NOT NULL DEFAULT 'ru',timezone TEXT NOT NULL DEFAULT 'UTC');
    CREATE TABLE IF NOT EXISTS multipost_batches(id INTEGER PRIMARY KEY,owner_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'draft',created_at TEXT NOT NULL,start_at TEXT,interval_seconds INTEGER);
    CREATE TABLE IF NOT EXISTS multipost_items(batch_id INTEGER,position INTEGER,post_id INTEGER NOT NULL,PRIMARY KEY(batch_id,position),UNIQUE(batch_id,post_id));
    CREATE TABLE IF NOT EXISTS multipost_albums(batch_id INTEGER,group_id TEXT,post_id INTEGER,PRIMARY KEY(batch_id,group_id));
    CREATE TABLE IF NOT EXISTS contests(id INTEGER PRIMARY KEY,owner_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,title TEXT NOT NULL,post_json TEXT NOT NULL,prize_kind TEXT NOT NULL,prize_json TEXT NOT NULL,starts_at TEXT NOT NULL,ends_at TEXT NOT NULL,subscriptions_json TEXT NOT NULL DEFAULT '[]',captcha INTEGER NOT NULL DEFAULT 0,quiz_question TEXT,quiz_hash TEXT,mode TEXT NOT NULL DEFAULT 'random',referral_min INTEGER NOT NULL DEFAULT 0,referral_bonus INTEGER NOT NULL DEFAULT 1,winner_count INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'scheduled',published_ids TEXT NOT NULL DEFAULT '[]',error TEXT,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS contest_publications(contest_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,message_id INTEGER,PRIMARY KEY(contest_id,channel_id));
    CREATE TABLE IF NOT EXISTS contest_entries(contest_id INTEGER,user_id INTEGER,inviter_id INTEGER,joined_at TEXT NOT NULL,base_valid INTEGER NOT NULL DEFAULT 0,captcha_passed INTEGER NOT NULL DEFAULT 0,quiz_passed INTEGER NOT NULL DEFAULT 0,captcha_question TEXT,captcha_hash TEXT,attempts INTEGER NOT NULL DEFAULT 0,locked_until TEXT,PRIMARY KEY(contest_id,user_id));
    CREATE TABLE IF NOT EXISTS contest_winners(contest_id INTEGER,user_id INTEGER,rank INTEGER,score INTEGER,prize TEXT,PRIMARY KEY(contest_id,user_id),UNIQUE(contest_id,rank));
    CREATE TABLE IF NOT EXISTS contest_outbox(id INTEGER PRIMARY KEY,contest_id INTEGER NOT NULL,kind TEXT NOT NULL,recipient INTEGER NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',error TEXT,UNIQUE(contest_id,kind,recipient));
    CREATE INDEX IF NOT EXISTS idx_contest_schedule ON contests(status,starts_at,ends_at);
    CREATE INDEX IF NOT EXISTS idx_contest_end ON contests(status,ends_at);
    CREATE INDEX IF NOT EXISTS idx_contest_start_due ON contests(status,julianday(starts_at));
    CREATE INDEX IF NOT EXISTS idx_contest_end_due ON contests(status,julianday(ends_at));
    CREATE INDEX IF NOT EXISTS idx_contest_referrals ON contest_entries(contest_id,inviter_id,base_valid);
    CREATE INDEX IF NOT EXISTS idx_contest_entry_user ON contest_entries(user_id,contest_id);
    CREATE TABLE IF NOT EXISTS contest_invites(contest_id INTEGER NOT NULL,user_id INTEGER NOT NULL,invite_link TEXT NOT NULL UNIQUE,PRIMARY KEY(contest_id,user_id));
    CREATE TABLE IF NOT EXISTS contest_channel_referrals(contest_id INTEGER NOT NULL,user_id INTEGER NOT NULL,inviter_id INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(contest_id,user_id));
    CREATE INDEX IF NOT EXISTS idx_contest_channel_scores ON contest_channel_referrals(contest_id,inviter_id,active);
    """)
    existing = {r[1] for r in db.execute("PRAGMA table_info(contests)")}
    for spec in (
        "giveaway_type TEXT NOT NULL DEFAULT 'raffle'",
        "prize_title TEXT NOT NULL DEFAULT ''",
        "claim_contact TEXT NOT NULL DEFAULT ''",
        "subscription_layout TEXT NOT NULL DEFAULT 'buttons'",
        "referral_target TEXT NOT NULL DEFAULT 'participants'",
        "displayed_count INTEGER NOT NULL DEFAULT -1",
        "count_dirty INTEGER NOT NULL DEFAULT 1",
        "count_revision INTEGER NOT NULL DEFAULT 0",
    ):
        if spec.split()[0] not in existing:
            db.execute("ALTER TABLE contests ADD COLUMN " + spec)
    db.execute("UPDATE contests SET giveaway_type='contest' WHERE mode='ranking'")
    db.executescript("""
    CREATE INDEX IF NOT EXISTS idx_contest_dirty ON contests(status,count_dirty,id);
    CREATE TRIGGER IF NOT EXISTS contest_entry_count_insert AFTER INSERT ON contest_entries
    BEGIN UPDATE contests SET count_dirty=1,count_revision=count_revision+1 WHERE id=NEW.contest_id; END;
    CREATE TRIGGER IF NOT EXISTS contest_entry_count_update AFTER UPDATE OF base_valid,inviter_id ON contest_entries
    WHEN NEW.base_valid IS NOT OLD.base_valid OR NEW.inviter_id IS NOT OLD.inviter_id
    BEGIN UPDATE contests SET count_dirty=1,count_revision=count_revision+1 WHERE id=NEW.contest_id; END;
    CREATE TRIGGER IF NOT EXISTS contest_entry_count_delete AFTER DELETE ON contest_entries
    BEGIN UPDATE contests SET count_dirty=1,count_revision=count_revision+1 WHERE id=OLD.contest_id; END;
    CREATE TRIGGER IF NOT EXISTS contest_channel_count_insert AFTER INSERT ON contest_channel_referrals
    BEGIN UPDATE contests SET count_dirty=1,count_revision=count_revision+1 WHERE id=NEW.contest_id; END;
    CREATE TRIGGER IF NOT EXISTS contest_channel_count_update AFTER UPDATE OF active,inviter_id ON contest_channel_referrals
    WHEN NEW.active IS NOT OLD.active OR NEW.inviter_id IS NOT OLD.inviter_id
    BEGIN UPDATE contests SET count_dirty=1,count_revision=count_revision+1 WHERE id=NEW.contest_id; END;
    CREATE TRIGGER IF NOT EXISTS contest_channel_count_delete AFTER DELETE ON contest_channel_referrals
    BEGIN UPDATE contests SET count_dirty=1,count_revision=count_revision+1 WHERE id=OLD.contest_id; END;
    CREATE TRIGGER IF NOT EXISTS contest_count_block AFTER INSERT ON blocked_users
    BEGIN UPDATE contests SET count_dirty=1,count_revision=count_revision+1 WHERE id IN (SELECT contest_id FROM contest_entries WHERE user_id=NEW.telegram_id); END;
    CREATE TRIGGER IF NOT EXISTS contest_count_unblock AFTER DELETE ON blocked_users
    BEGIN UPDATE contests SET count_dirty=1,count_revision=count_revision+1 WHERE id IN (SELECT contest_id FROM contest_entries WHERE user_id=OLD.telegram_id); END;
    CREATE TRIGGER IF NOT EXISTS contest_count_rules AFTER UPDATE OF referral_min,referral_target ON contests
    WHEN NEW.referral_min IS NOT OLD.referral_min OR NEW.referral_target IS NOT OLD.referral_target
    BEGIN UPDATE contests SET count_dirty=1,count_revision=count_revision+1 WHERE id=NEW.id; END;
    """)
    db.commit()


def log_event(uid: Optional[int], event: str, details: str = ""):
    execute(
        "INSERT INTO logs(telegram_id,event,details,created_at) VALUES(?,?,?,?)",
        (uid, event, details, timeutils.iso()),
    )


@contextmanager
def atomic():
    with _write_gate:
        with _atomic():
            yield


@contextmanager
def _atomic():
    """No await inside transactions; all DB access stays on the event-loop thread."""
    if db.in_transaction:
        savepoint = "nested_" + secrets.token_hex(5)
        db.execute("SAVEPOINT " + savepoint)
        try:
            yield
            db.execute("RELEASE SAVEPOINT " + savepoint)
        except BaseException:
            db.execute("ROLLBACK TO SAVEPOINT " + savepoint)
            db.execute("RELEASE SAVEPOINT " + savepoint)
            raise
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def init_extensions():
    columns = {
        "premium": ["lifetime INTEGER DEFAULT 0"],
        "channels": [
            "rights_checked_at TEXT",
            "rights_retry_at TEXT",
            "default_template_id INTEGER",
            "invite_link TEXT",
            "is_forum INTEGER DEFAULT 0",
        ],
        "posts": [
            "caption_entities_json TEXT DEFAULT '[]'",
            "media_json TEXT DEFAULT '[]'",
            "delete_after_seconds INTEGER DEFAULT 0",
            "preview_ids TEXT",
            "preview_target INTEGER",
            "protect_content INTEGER DEFAULT 0",
            "pin_enabled INTEGER DEFAULT 0",
            "pin_until TEXT",
        ],
        "post_targets": [
            "message_thread_id INTEGER",
            "retry_at TEXT",
            "status TEXT DEFAULT 'pending'",
            "error TEXT",
            "template_id INTEGER",
            "payload_json TEXT",
        ],
        "published_messages": [
            "content_json TEXT",
            "buttons_json TEXT DEFAULT '[]'",
            "delete_error TEXT",
            "pin_state TEXT",
            "unpin_at TEXT",
            "pin_retry_at TEXT",
            "pin_error TEXT",
        ],
        "post_sources": [
            "kind TEXT DEFAULT 'telegram'",
            "url TEXT",
            "last_error TEXT",
            "last_poll TEXT",
            "initialized INTEGER DEFAULT 0",
        ],
        "referrals": ["rewarded_at TEXT"],
        "join_requests": [
            "next_check_at TEXT",
            "contact_chat_id INTEGER",
            "last_checked TEXT",
            "approval_notified_at TEXT",
            "approval_delivery_state TEXT DEFAULT 'pending'",
            "approval_retry_at TEXT",
        ],
    }
    for table, specs in columns.items():
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for spec in specs:
            if spec.split()[0] not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {spec}")
                if table == "join_requests" and spec.startswith(
                    "approval_notified_at "
                ):
                    # Do not send historical approval messages merely because of an upgrade.
                    db.execute(
                        "UPDATE join_requests SET approval_notified_at=COALESCE(updated_at,created_at) WHERE status='approved'"
                    )
    db.executescript("""
    CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS premium_history(id INTEGER PRIMARY KEY,user_id INTEGER,actor_id INTEGER,reason TEXT,old_expiry TEXT,new_expiry TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY,days INTEGER NOT NULL,max_uses INTEGER NOT NULL,expires_at TEXT,personal_id INTEGER,condition TEXT DEFAULT 'none',active INTEGER DEFAULT 1,created_at TEXT);
    CREATE TABLE IF NOT EXISTS promo_uses(code TEXT,user_id INTEGER,created_at TEXT,PRIMARY KEY(code,user_id));
    CREATE TABLE IF NOT EXISTS templates(id INTEGER PRIMARY KEY,channel_id INTEGER NOT NULL,owner_id INTEGER NOT NULL,name TEXT NOT NULL,before_html TEXT DEFAULT '',after_html TEXT DEFAULT '',signature_html TEXT DEFAULT '',buttons_json TEXT DEFAULT '[]');
    CREATE TABLE IF NOT EXISTS source_seen(source_id INTEGER,item_key TEXT,post_id INTEGER,PRIMARY KEY(source_id,item_key));
    CREATE TABLE IF NOT EXISTS fsm_state(storage_key TEXT PRIMARY KEY,state TEXT,data TEXT DEFAULT '{}');
    CREATE TABLE IF NOT EXISTS notification_keys(key TEXT PRIMARY KEY,created_at TEXT);
    CREATE TABLE IF NOT EXISTS runtime_lock(id INTEGER PRIMARY KEY CHECK(id=1),owner TEXT,heartbeat TEXT);
    CREATE TABLE IF NOT EXISTS album_intake(owner_id INTEGER,chat_id INTEGER,group_id TEXT,source_id INTEGER DEFAULT 0,items_json TEXT,targets_json TEXT,ready_at TEXT,post_id INTEGER,PRIMARY KEY(owner_id,chat_id,group_id,source_id));
    CREATE INDEX IF NOT EXISTS idx_source_seen_post ON source_seen(post_id);
    CREATE INDEX IF NOT EXISTS idx_post_owner_status ON posts(owner_telegram_id,status);
    CREATE INDEX IF NOT EXISTS idx_source_dispatch ON post_sources(source_chat_id,kind,active);
    CREATE INDEX IF NOT EXISTS idx_channel_owner_active ON channels(owner_telegram_id,is_active,id);
    CREATE INDEX IF NOT EXISTS idx_scheduled_due ON scheduled_posts(active,julianday(publish_at),post_id);
    CREATE INDEX IF NOT EXISTS idx_published_delete_due ON published_messages(deleted,julianday(delete_at));
    CREATE INDEX IF NOT EXISTS idx_channel_rights ON channels(is_active,rights_checked_at,id);
    CREATE TRIGGER IF NOT EXISTS channel_single_owner_insert BEFORE INSERT ON channels
    WHEN NEW.is_active=1 AND EXISTS(SELECT 1 FROM channels c WHERE c.telegram_chat_id=NEW.telegram_chat_id AND c.is_active=1 AND c.owner_telegram_id!=NEW.owner_telegram_id)
    BEGIN SELECT RAISE(ABORT,'Channel already has an active owner'); END;
    CREATE TRIGGER IF NOT EXISTS channel_single_owner_activate BEFORE UPDATE OF is_active,owner_telegram_id,telegram_chat_id ON channels
    WHEN NEW.is_active=1 AND (OLD.is_active!=1 OR OLD.owner_telegram_id!=NEW.owner_telegram_id OR OLD.telegram_chat_id!=NEW.telegram_chat_id)
    AND EXISTS(SELECT 1 FROM channels c WHERE c.id!=NEW.id AND c.telegram_chat_id=NEW.telegram_chat_id AND c.is_active=1 AND c.owner_telegram_id!=NEW.owner_telegram_id)
    BEGIN SELECT RAISE(ABORT,'Channel already has an active owner'); END;
    CREATE INDEX IF NOT EXISTS idx_published_lookup ON published_messages(channel_id,telegram_message_id);
    CREATE INDEX IF NOT EXISTS idx_approval_delivery ON join_requests(status,approval_delivery_state,approval_retry_at,id);
    CREATE INDEX IF NOT EXISTS idx_published_pin ON published_messages(pin_state,deleted,unpin_at);
    """)
    for key, value in {
        "ref_inviter_days": "0",
        "ref_invitee_days": "0",
        "ref_condition": "channel_and_publish",
        "ref_subscription": "",
        "promo_default_days": "7",
    }.items():
        db.execute("INSERT OR IGNORE INTO app_settings VALUES(?,?)", (key, value))
    # Preserve known successful legacy deliveries; never resend them on a retry.
    db.execute(
        "UPDATE post_targets SET status='sent' WHERE status='pending' AND EXISTS(SELECT 1 FROM published_messages m WHERE m.post_id=post_targets.post_id AND m.channel_id=post_targets.channel_id)"
    )
    if not one("SELECT 1 FROM app_settings WHERE key='delivery_migration_v2'"):
        db.execute(
            "UPDATE published_messages SET buttons_json=COALESCE((SELECT buttons_json FROM posts WHERE posts.id=published_messages.post_id),'[]')"
        )
        db.execute("INSERT INTO app_settings VALUES('delivery_migration_v2','done')")
    db.commit()
    init_v21()


def init_delivery_queues():
    db.execute(
        "CREATE TABLE IF NOT EXISTS edited_reactions(owner_id INTEGER, session_id INTEGER, button_id TEXT, user_id INTEGER, PRIMARY KEY(owner_id,session_id,button_id,user_id))"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_edited_reactions_user ON edited_reactions(owner_id,session_id,user_id)"
    )
    for table, fields in {
        "contests": ["retry_at TEXT"],
        "contest_outbox": ["retry_at TEXT"],
        "album_intake": ["attempts INTEGER NOT NULL DEFAULT 0", "error TEXT"],
    }.items():
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for field in fields:
            if field.split()[0] not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {field}")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS broadcast_jobs(
      id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, token TEXT UNIQUE NOT NULL,
      payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS broadcast_targets(
      job_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
      retry_at TEXT, error TEXT, PRIMARY KEY(job_id,user_id));
    CREATE INDEX IF NOT EXISTS idx_broadcast_due ON broadcast_targets(status,retry_at);
    CREATE TABLE IF NOT EXISTS contest_checks(
      contest_id INTEGER NOT NULL, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
      present INTEGER NOT NULL, checked_at TEXT NOT NULL,
      PRIMARY KEY(contest_id,user_id,chat_id));
    CREATE INDEX IF NOT EXISTS idx_join_due ON join_requests(status,next_check_at);
    """)
    db.commit()


def setting(key):
    r = one("SELECT value FROM app_settings WHERE key=?", (key,))
    return r["value"] if r else ""


def init_v21():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS incoming(id INTEGER PRIMARY KEY,owner_id INTEGER NOT NULL,message_json TEXT NOT NULL,links_json TEXT DEFAULT '[]',created_at TEXT,post_id INTEGER);
    CREATE TABLE IF NOT EXISTS downloads(id INTEGER PRIMARY KEY,owner_id INTEGER NOT NULL,url TEXT NOT NULL,info_json TEXT DEFAULT '{}',status TEXT DEFAULT 'new',created_at TEXT,error TEXT);
    CREATE TABLE IF NOT EXISTS download_deliveries(download_id INTEGER,selection INTEGER,entry_index INTEGER,part_index INTEGER,message_id INTEGER,PRIMARY KEY(download_id,selection,entry_index,part_index));
    CREATE TABLE IF NOT EXISTS manual_payments(id INTEGER PRIMARY KEY,telegram_id INTEGER NOT NULL,days INTEGER NOT NULL,reference TEXT NOT NULL,method TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL,reviewed_at TEXT);
    """)
    db.execute("UPDATE post_sources SET active=0 WHERE kind!='telegram'")
    if not one("SELECT 1 FROM app_settings WHERE key='preserve_links_v22'"):
        db.execute(
            "UPDATE posts SET remove_links=0 WHERE status IN ('draft','scheduled','failed','partial')"
        )
        db.execute(
            "UPDATE post_targets SET payload_json=NULL WHERE status!='sent' AND post_id IN (SELECT id FROM posts WHERE status IN ('draft','scheduled','failed','partial'))"
        )
        db.execute("INSERT INTO app_settings VALUES('preserve_links_v22','done')")
    db.commit()
