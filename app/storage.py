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
        database.execute(
            "INSERT INTO fsm_state(storage_key,state) VALUES(?,?) ON CONFLICT(storage_key) DO UPDATE SET state=excluded.state",
            (self.key(key), value),
        )

    async def get_state(self, key):
        r = database.one(
            "SELECT state FROM fsm_state WHERE storage_key=?", (self.key(key),)
        )
        return r["state"] if r else None

    async def set_data(self, key, data):
        database.execute(
            "INSERT INTO fsm_state(storage_key,data) VALUES(?,?) ON CONFLICT(storage_key) DO UPDATE SET data=excluded.data",
            (self.key(key), json.dumps(dict(data))),
        )

    async def get_data(self, key):
        r = database.one(
            "SELECT data FROM fsm_state WHERE storage_key=?", (self.key(key),)
        )
        return json.loads(r["data"]) if r else {}

    async def close(self):
        pass
