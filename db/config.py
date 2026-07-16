"""
Dynamic Configuration Service
Reads and writes settings dynamically to the SQLite `system_config` table.
"""
import json
import logging
from db.database import get_sqlite, _sqlite_lock

_log = logging.getLogger(__name__)

class DbConfig:
    def get(self, key: str, default=None) -> str | None:
        """Synchronous get of config value from DB."""
        conn = get_sqlite()
        with _sqlite_lock:
            row = conn.execute("SELECT value FROM system_config WHERE key = ?", [key]).fetchone()
            if row:
                return row["value"]
            return default

    def set(self, key: str, value: str):
        """Synchronous set of config value in DB."""
        conn = get_sqlite()
        with _sqlite_lock:
            conn.execute(
                "INSERT OR REPLACE INTO system_config (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                [key, str(value)]
            )
            conn.commit()

    async def get_async(self, key: str, default=None) -> str | None:
        """Asynchronous wrapper for get."""
        import asyncio
        return await asyncio.to_thread(self.get, key, default)

    async def set_async(self, key: str, value: str):
        """Asynchronous wrapper for set."""
        import asyncio
        await asyncio.to_thread(self.set, key, value)

    async def get_typed(self, key: str, expected_type: type, default=None):
        """Retrieve config value casted to expected type."""
        val = await self.get_async(key)
        if val is None:
            return default
        try:
            if expected_type == bool:
                return val.lower() in ("true", "1", "yes", "on")
            elif expected_type == int:
                return int(val)
            elif expected_type == float:
                return float(val)
            elif expected_type in (list, dict):
                return json.loads(val)
            return expected_type(val)
        except Exception as e:
            _log.warning("Casting config key %s value %r to %s failed: %s", key, val, expected_type, e)
            return default

# Global configuration helper
cfg = DbConfig()
