"""storage components."""

import json

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage

from app import database as database


class SQLiteStorage(BaseStorage):
    @staticmethod
    def key(k):
        return f"{k.bot_id}:{k.chat_id}:{k.user_id}:{k.thread_id}:{k.business_connection_id}:{k.destiny}"

    async def set_state(self, key, state=None):
        value = state.state if isinstance(state, State) else state
        await database.async_call(
            lambda conn: (
                conn.execute(
                    "INSERT INTO fsm_state(storage_key,state) VALUES(?,?) ON CONFLICT(storage_key) DO UPDATE SET state=excluded.state",
                    (self.key(key), value),
                ).rowcount
            )
        )

    async def get_state(self, key):
        r = await database.async_call(
            lambda conn: conn.execute(
                "SELECT state FROM fsm_state WHERE storage_key=?", (self.key(key),)
            ).fetchone()
        )
        return r["state"] if r else None

    async def set_data(self, key, data):
        await database.async_call(
            lambda conn: (
                conn.execute(
                    "INSERT INTO fsm_state(storage_key,data) VALUES(?,?) ON CONFLICT(storage_key) DO UPDATE SET data=excluded.data",
                    (self.key(key), json.dumps(dict(data))),
                ).rowcount
            )
        )

    async def get_data(self, key):
        r = await database.async_call(
            lambda conn: conn.execute(
                "SELECT data FROM fsm_state WHERE storage_key=?", (self.key(key),)
            ).fetchone()
        )
        return json.loads(r["data"]) if r else {}

    async def close(self):
        await database.close_async()
