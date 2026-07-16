"""
Encrypted API Key Store — securely manages Polygon wallet keys and credentials.
Uses Fernet symmetric encryption. Decryption key lives in .secret on disk.
If run directly, allows local retrieval of keys: `python services/keystore.py get <service>`
"""
import sys
import uuid
import logging
from pathlib import Path
from datetime import datetime, timezone
from cryptography.fernet import Fernet

_SECRET_PATH = Path(__file__).resolve().parent.parent / ".secret"
_fernet = None
_log = logging.getLogger(__name__)

def _get_fernet() -> Fernet:
    """Load or generate the master encryption key from .secret file."""
    global _fernet
    if _fernet:
        return _fernet

    if _SECRET_PATH.exists():
        key = _SECRET_PATH.read_bytes()
    else:
        key = Fernet.generate_key()
        _SECRET_PATH.write_bytes(key)
        print(f"[KeyStore] Generated new master secret key at {_SECRET_PATH}")

    _fernet = Fernet(key)
    return _fernet

def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string and return base64 token."""
    return _get_get_fernet_instance().encrypt(plaintext.encode()).decode()

def decrypt_value(token: str) -> str:
    """Decrypt an encrypted token back to plaintext."""
    return _get_get_fernet_instance().decrypt(token.encode()).decode()

def _get_get_fernet_instance() -> Fernet:
    return _get_fernet()

class KeyStore:
    """CRUD interface for encrypted API credentials inside SQLite."""

    def add_key(self, service: str, raw_value: str, label: str = "") -> dict:
        """Encrypt and store an API key."""
        from db.database import get_sqlite
        key_id = f"key_{uuid.uuid4().hex[:8]}"
        encrypted = encrypt_value(raw_value)
        now = datetime.now(timezone.utc).isoformat()

        conn = get_sqlite()
        conn.execute(
            """INSERT OR REPLACE INTO api_keys (id, service, key_value, label, created_at, status)
               VALUES (?, ?, ?, ?, ?, 'active')""",
            [key_id, service, encrypted, label or None, now],
        )
        conn.commit()
        return {"id": key_id, "service": service, "status": "active"}

    def get_decrypted(self, service: str) -> str | None:
        """Retrieve and decrypt the key for a given service."""
        from db.database import get_sqlite
        conn = get_sqlite()
        row = conn.execute(
            "SELECT key_value FROM api_keys WHERE service = ? ORDER BY created_at DESC LIMIT 1",
            [service],
        ).fetchone()
        if row:
            try:
                return decrypt_value(row["key_value"])
            except Exception as e:
                _log.warning("Keystore decrypt failed for %s: %s", service, e)
                return None
        return None

    def delete_key(self, service: str) -> bool:
        """Delete key for a service."""
        from db.database import get_sqlite
        conn = get_sqlite()
        conn.execute("DELETE FROM api_keys WHERE service = ?", [service])
        conn.commit()
        return True

    def list_keys(self) -> list[dict]:
        """List metadata of stored keys (masked value)."""
        from db.database import get_sqlite
        conn = get_sqlite()
        rows = conn.execute(
            "SELECT id, service, label, created_at, status FROM api_keys"
        ).fetchall()
        return [dict(r) for r in rows]

# Global singleton
keystore = KeyStore()

if __name__ == "__main__":
    # Local CLI helper to retrieve keys easily
    if len(sys.argv) < 2:
        print("Usage: python keystore.py [get|set|list] [args...]")
        sys.exit(0)

    cmd = sys.argv[1].lower()
    if cmd == "list":
        for k in keystore.list_keys():
            print(f"- {k['service']} ({k['label'] or 'no label'}) [status: {k['status']}]")
    elif cmd == "get" and len(sys.argv) == 3:
        svc = sys.argv[2]
        val = keystore.get_decrypted(svc)
        if val:
            print(f"Decrypted value for '{svc}':\n{val}")
        else:
            print(f"No key found for service: {svc}")
    elif cmd == "set" and len(sys.argv) == 4:
        svc = sys.argv[2]
        val = sys.argv[3]
        keystore.add_key(svc, val, f"CLI Added {svc}")
        print(f"Encrypted and saved '{svc}' successfully.")
    else:
        print("Invalid command arguments.")
