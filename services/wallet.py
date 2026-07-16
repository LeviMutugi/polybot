"""
WalletService — Atomic, Event-Sourced Balance Management
All paper balance mutations go through this service.
Every change is recorded in the wallet_ledger table for full audit trail.
Thread-safe via SQLite lock, atomic check-and-deduct prevents double-spending.
"""
import logging
from db.database import get_sqlite, _sqlite_lock

_log = logging.getLogger(__name__)


class InsufficientFundsError(Exception):
    """Raised when a debit exceeds available balance."""
    pass


class DuplicateTransactionError(Exception):
    """Raised when an idempotency key has already been used."""
    pass


class WalletService:
    """Thread-safe, atomic, event-sourced wallet for paper trading."""

    BALANCE_KEYS = {
        "paper": "portfolio.paper_balance",
    }

    def _get_balance_key(self, mode: str) -> str:
        key = self.BALANCE_KEYS.get(mode)
        if not key:
            raise ValueError(f"Unsupported wallet mode: {mode}")
        return key

    def get_balance(self, mode: str) -> float:
        """Get current balance for the given mode."""
        conn = get_sqlite()
        balance_key = self._get_balance_key(mode)
        with _sqlite_lock:
            row = conn.execute(
                "SELECT value FROM system_config WHERE key = ?", [balance_key]
            ).fetchone()
            if row:
                return float(row["value"])
            return 0.0

    def debit(self, mode: str, amount: float, tx_type: str,
              position_id: str = None, description: str = "",
              idempotency_key: str = None) -> float:
        """
        Atomically: read balance -> check sufficient -> deduct -> write ledger.
        All within a single SQLite transaction under the lock.
        
        Raises InsufficientFundsError if balance < amount.
        Raises DuplicateTransactionError if idempotency_key already used.
        Returns new balance.
        """
        if amount < 0:
            raise ValueError(f"Debit amount must be non-negative, got {amount}")
        if amount == 0:
            return self.get_balance(mode)

        conn = get_sqlite()
        balance_key = self._get_balance_key(mode)

        with _sqlite_lock:
            # Check idempotency
            if idempotency_key:
                existing = conn.execute(
                    "SELECT id FROM wallet_ledger WHERE idempotency_key = ?",
                    [idempotency_key]
                ).fetchone()
                if existing:
                    raise DuplicateTransactionError(
                        f"Transaction already processed: {idempotency_key}"
                    )

            # Read current balance
            row = conn.execute(
                "SELECT value FROM system_config WHERE key = ?", [balance_key]
            ).fetchone()
            balance_before = float(row["value"]) if row else 0.0

            # Check sufficient funds
            if amount > balance_before:
                raise InsufficientFundsError(
                    f"Insufficient {mode} balance: have ${balance_before:.2f}, "
                    f"need ${amount:.2f}"
                )

            # Compute new balance
            balance_after = round(balance_before - amount, 4)

            # Atomic: update balance + insert ledger in one transaction
            conn.execute(
                "UPDATE system_config SET value = ?, updated_at = datetime('now') "
                "WHERE key = ?",
                [str(balance_after), balance_key]
            )
            conn.execute(
                """INSERT INTO wallet_ledger
                   (idempotency_key, mode, tx_type, amount, balance_before,
                    balance_after, position_id, description, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                [idempotency_key, mode, tx_type, -amount,
                 balance_before, balance_after, position_id, description]
            )
            conn.commit()

        _log.info("[Wallet] DEBIT %s $%.2f (%s) | $%.2f -> $%.2f | %s",
                  mode, amount, tx_type, balance_before, balance_after, description)
        return balance_after

    def credit(self, mode: str, amount: float, tx_type: str,
               position_id: str = None, description: str = "",
               idempotency_key: str = None) -> float:
        """
        Atomically credit funds and record ledger entry.
        Returns new balance.
        """
        if amount < 0:
            raise ValueError(f"Credit amount must be non-negative, got {amount}")

        conn = get_sqlite()
        balance_key = self._get_balance_key(mode)

        with _sqlite_lock:
            # Check idempotency
            if idempotency_key:
                existing = conn.execute(
                    "SELECT id FROM wallet_ledger WHERE idempotency_key = ?",
                    [idempotency_key]
                ).fetchone()
                if existing:
                    # Idempotent: return current balance without error
                    row = conn.execute(
                        "SELECT value FROM system_config WHERE key = ?",
                        [balance_key]
                    ).fetchone()
                    return float(row["value"]) if row else 0.0

            # Read current balance
            row = conn.execute(
                "SELECT value FROM system_config WHERE key = ?", [balance_key]
            ).fetchone()
            balance_before = float(row["value"]) if row else 0.0

            # For $0 credits (loss recording), still log but don't change balance
            balance_after = round(balance_before + amount, 4)

            # Atomic: update balance + insert ledger
            conn.execute(
                "UPDATE system_config SET value = ?, updated_at = datetime('now') "
                "WHERE key = ?",
                [str(balance_after), balance_key]
            )
            conn.execute(
                """INSERT INTO wallet_ledger
                   (idempotency_key, mode, tx_type, amount, balance_before,
                    balance_after, position_id, description, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                [idempotency_key, mode, tx_type, amount,
                 balance_before, balance_after, position_id, description]
            )
            conn.commit()

        if amount > 0:
            _log.info("[Wallet] CREDIT %s $%.2f (%s) | $%.2f -> $%.2f | %s",
                      mode, amount, tx_type, balance_before, balance_after,
                      description)
        else:
            _log.info("[Wallet] RECORD %s $0.00 (%s) | balance $%.2f | %s",
                      mode, tx_type, balance_after, description)
        return balance_after

    def set_balance(self, mode: str, amount: float,
                    description: str = "") -> float:
        """
        Administrative reset/set balance. Records a 'reset' ledger entry.
        Returns new balance.
        """
        if amount < 0:
            raise ValueError(f"Balance must be non-negative, got {amount}")

        conn = get_sqlite()
        balance_key = self._get_balance_key(mode)

        with _sqlite_lock:
            row = conn.execute(
                "SELECT value FROM system_config WHERE key = ?", [balance_key]
            ).fetchone()
            balance_before = float(row["value"]) if row else 0.0
            balance_after = round(amount, 4)
            delta = balance_after - balance_before

            conn.execute(
                "UPDATE system_config SET value = ?, updated_at = datetime('now') "
                "WHERE key = ?",
                [str(balance_after), balance_key]
            )
            conn.execute(
                """INSERT INTO wallet_ledger
                   (idempotency_key, mode, tx_type, amount, balance_before,
                    balance_after, position_id, description, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                [None, mode, "reset", delta,
                 balance_before, balance_after, None, description]
            )
            conn.commit()

        _log.info("[Wallet] RESET %s $%.2f -> $%.2f | %s",
                  mode, balance_before, balance_after, description)
        return balance_after

    def get_ledger(self, mode: str = None, limit: int = 100) -> list:
        """Return recent ledger entries for audit display."""
        conn = get_sqlite()
        with _sqlite_lock:
            if mode:
                rows = conn.execute(
                    "SELECT * FROM wallet_ledger WHERE mode = ? "
                    "ORDER BY id DESC LIMIT ?",
                    [mode, limit]
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM wallet_ledger ORDER BY id DESC LIMIT ?",
                    [limit]
                ).fetchall()
        return [dict(r) for r in rows]

    def verify_conservation(self, mode: str) -> dict:
        """
        Conservation-of-money health check.
        Verifies: current_balance == first_entry.balance_before + sum(all ledger amounts)
        
        If no ledger entries exist, the system is 'unaudited' (pre-migration).
        """
        conn = get_sqlite()
        balance_key = self._get_balance_key(mode)

        with _sqlite_lock:
            # Current balance
            row = conn.execute(
                "SELECT value FROM system_config WHERE key = ?", [balance_key]
            ).fetchone()
            actual_balance = float(row["value"]) if row else 0.0

            # Ledger entries
            ledger_rows = conn.execute(
                "SELECT amount, balance_before, balance_after FROM wallet_ledger "
                "WHERE mode = ? ORDER BY id ASC",
                [mode]
            ).fetchall()

        if not ledger_rows:
            return {
                "valid": True,
                "status": "unaudited",
                "message": "No ledger entries yet. System is pre-migration.",
                "actual_balance": actual_balance,
                "expected_balance": actual_balance,
                "drift": 0.0,
                "ledger_entries": 0
            }

        # Expected balance = first entry's balance_before + sum(all amounts)
        initial_balance = float(ledger_rows[0]["balance_before"])
        total_delta = sum(float(r["amount"]) for r in ledger_rows)
        expected_balance = round(initial_balance + total_delta, 4)

        drift = round(actual_balance - expected_balance, 4)
        valid = abs(drift) < 0.01  # Allow 1 cent rounding tolerance

        # Also verify chain consistency: entry[N].balance_after == entry[N+1].balance_before
        chain_breaks = []
        for i in range(len(ledger_rows) - 1):
            after = round(float(ledger_rows[i]["balance_after"]), 4)
            next_before = round(float(ledger_rows[i + 1]["balance_before"]), 4)
            if abs(after - next_before) > 0.01:
                chain_breaks.append({
                    "index": i,
                    "balance_after": after,
                    "next_balance_before": next_before
                })

        return {
            "valid": valid and len(chain_breaks) == 0,
            "status": "healthy" if (valid and len(chain_breaks) == 0) else "DRIFT_DETECTED",
            "actual_balance": actual_balance,
            "expected_balance": expected_balance,
            "drift": drift,
            "ledger_entries": len(ledger_rows),
            "chain_breaks": chain_breaks
        }


# Singleton instance
wallet_service = WalletService()
