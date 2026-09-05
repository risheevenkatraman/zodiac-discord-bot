from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.connection: sqlite3.Connection | None = None
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS channels (
                guild_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, kind)
            );
            CREATE TABLE IF NOT EXISTS roles (
                guild_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, kind)
            );
            CREATE TABLE IF NOT EXISTS twitch_accounts (
                guild_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                PRIMARY KEY (guild_id, username)
            );
            """
        )
        self.connection.commit()

    def _require_connection(self) -> sqlite3.Connection:
        if not self.connection:
            raise RuntimeError("Database is not initialized")
        return self.connection

    async def set_channel(self, guild_id: int, kind: str, channel_id: int) -> None:
        async with self.lock:
            connection = self._require_connection()
            connection.execute("INSERT OR REPLACE INTO channels VALUES (?, ?, ?)", (guild_id, kind, channel_id))
            connection.commit()

    async def get_channel(self, guild_id: int, kind: str) -> int | None:
        async with self.lock:
            row = self._require_connection().execute("SELECT channel_id FROM channels WHERE guild_id = ? AND kind = ?", (guild_id, kind)).fetchone()
            return int(row["channel_id"]) if row else None

    async def set_role(self, guild_id: int, kind: str, role_id: int) -> None:
        async with self.lock:
            connection = self._require_connection()
            connection.execute("INSERT OR REPLACE INTO roles VALUES (?, ?, ?)", (guild_id, kind, role_id))
            connection.commit()

    async def get_role(self, guild_id: int, kind: str) -> int | None:
        async with self.lock:
            row = self._require_connection().execute("SELECT role_id FROM roles WHERE guild_id = ? AND kind = ?", (guild_id, kind)).fetchone()
            return int(row["role_id"]) if row else None

    async def add_twitch_account(self, guild_id: int, username: str) -> None:
        async with self.lock:
            connection = self._require_connection()
            connection.execute("INSERT OR IGNORE INTO twitch_accounts VALUES (?, ?)", (guild_id, username))
            connection.commit()

    async def remove_twitch_account(self, guild_id: int, username: str) -> bool:
        async with self.lock:
            connection = self._require_connection()
            cursor = connection.execute("DELETE FROM twitch_accounts WHERE guild_id = ? AND username = ?", (guild_id, username))
            connection.commit()
            return cursor.rowcount > 0

    async def list_twitch_accounts(
        self, guild_id: int | None = None
    ) -> list[dict[str, Any]]:
        async with self.lock:
            connection = self._require_connection()
            if guild_id is None:
                rows = connection.execute(
                    "SELECT guild_id, username FROM twitch_accounts"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT guild_id, username FROM twitch_accounts "
                    "WHERE guild_id = ? ORDER BY username",
                    (guild_id,),
                ).fetchall()
            return [dict(row) for row in rows]

    async def list_guild_ids(self) -> list[int]:
        async with self.lock:
            rows = self._require_connection().execute("SELECT DISTINCT guild_id FROM channels").fetchall()
            return [int(row["guild_id"]) for row in rows]
