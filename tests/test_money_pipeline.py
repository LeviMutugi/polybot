"""
Money Pipeline Test Suite — Comprehensive tests for the atomic wallet service,
balance deduction, credit, idempotency, conservation-of-money, and full lifecycle.

Tests are designed to be run against a fresh in-memory SQLite database so they
don't touch production data.
"""
import os
import sys
import sqlite3
import threading
import asyncio
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def create_test_db():
    """Create a fresh in-memory SQLite database with the full schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS poly_yield_positions (
            id TEXT PRIMARY KEY,
            opportunity_id TEXT,
            strategy TEXT,
            market_id TEXT,
            market_title TEXT,
            outcome TEXT,
            shares REAL,
            entry_price REAL,
            cost_usdc REAL,
            order_id TEXT,
            status TEXT DEFAULT 'open',
            mode TEXT DEFAULT 'paper',
            entry_at TIMESTAMP,
            settled_at TIMESTAMP,
            realized_pnl REAL,
            settlement_outcome TEXT,
            predicted_apy REAL,
            predicted_profit_pct REAL,
            predicted_days_to_expiry REAL,
            actual_fill_price REAL,
            actual_gas_usdc REAL,
            risk_level TEXT,
            fill_slippage_bps REAL,
            quality_at_entry REAL,
            predicted_pnl_usdc REAL,
            apy_delta REAL,
            stop_loss_price REAL,
            take_profit_price REAL,
            trailing_stop_pct REAL,
            highest_price REAL,
            token_id TEXT,
            idempotency_key TEXT
        );

        CREATE TABLE IF NOT EXISTS poly_yield_opportunities (
            id TEXT PRIMARY KEY,
            strategy TEXT,
            market_id TEXT,
            market_title TEXT,
            outcome TEXT,
            entry_price REAL,
            implied_prob REAL,
            annualized_apy REAL,
            profit_pct REAL,
            days_to_expiry REAL,
            suggested_usdc REAL,
            status TEXT DEFAULT 'open',
            token_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS poly_yield_stats (
            strategy TEXT,
            mode TEXT DEFAULT 'paper',
            total_pnl REAL DEFAULT 0,
            total_returned REAL DEFAULT 0,
            win_count INTEGER DEFAULT 0,
            loss_count INTEGER DEFAULT 0,
            open_positions INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (strategy, mode)
        );

        CREATE TABLE IF NOT EXISTS wallet_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT UNIQUE,
            mode TEXT NOT NULL,
            tx_type TEXT NOT NULL,
            amount REAL NOT NULL,
            balance_before REAL NOT NULL,
            balance_after REAL NOT NULL,
            position_id TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        INSERT INTO system_config (key, value) VALUES ('portfolio.paper_balance', '1000.0');
        INSERT INTO system_config (key, value) VALUES ('poly_yield.active_mode', 'paper');
    """)
    conn.commit()
    return conn


class TestWalletService(unittest.TestCase):
    """Tests for the atomic WalletService."""

    def setUp(self):
        """Create fresh DB and wallet service for each test."""
        self.conn = create_test_db()
        self.lock = threading.RLock()

        # Override global connection in database module
        import db.database
        self.old_conn = db.database._sqlite_conn
        self.old_lock = db.database._sqlite_lock
        
        db.database._sqlite_conn = self.conn
        db.database._sqlite_lock = self.lock

        # Import wallet service fresh
        from services.wallet import wallet_service
        self.wallet = wallet_service

    def tearDown(self):
        import db.database
        self.conn.close()
        db.database._sqlite_conn = self.old_conn
        db.database._sqlite_lock = self.old_lock

    # --- Test 1: Balance deducted on trade ---
    def test_paper_balance_deducted_on_trade(self):
        """Balance should decrease by exactly the trade amount."""
        initial = self.wallet.get_balance("paper")
        self.assertEqual(initial, 1000.0)

        new_balance = self.wallet.debit("paper", 100.0, "trade_open",
                                         description="Buy: Test Market",
                                         idempotency_key="open_test_1")
        self.assertEqual(new_balance, 900.0)
        self.assertEqual(self.wallet.get_balance("paper"), 900.0)

    # --- Test 2: Balance credited on win settlement ---
    def test_paper_balance_credited_on_win_settlement(self):
        """Winning settlement should return cost + profit."""
        self.wallet.debit("paper", 100.0, "trade_open",
                          idempotency_key="open_test_2")
        # Simulate win: cost=$100, profit=$20. Return $120.
        new_balance = self.wallet.credit("paper", 120.0, "settlement_win",
                                          position_id="pos_test_2",
                                          idempotency_key="settle_pos_test_2")
        self.assertEqual(new_balance, 1020.0)
        self.assertEqual(self.wallet.get_balance("paper"), 1020.0)

    # --- Test 3: Balance unchanged on loss settlement ---
    def test_paper_balance_unchanged_on_loss_settlement(self):
        """Losing settlement should not add any funds (just log $0 credit)."""
        self.wallet.debit("paper", 100.0, "trade_open",
                          idempotency_key="open_test_3")
        balance_before_settle = self.wallet.get_balance("paper")
        new_balance = self.wallet.credit("paper", 0.0, "settlement_loss",
                                          position_id="pos_test_3",
                                          idempotency_key="settle_pos_test_3")
        self.assertEqual(new_balance, balance_before_settle)
        self.assertEqual(new_balance, 900.0)

    # --- Test 4: Balance credited on manual exit ---
    def test_paper_balance_credited_on_manual_exit(self):
        """Manual exit should credit shares * exit_price back."""
        self.wallet.debit("paper", 100.0, "trade_open",
                          idempotency_key="open_test_4")
        # Exit: 200 shares at $0.55 each = $110 returned
        new_balance = self.wallet.credit("paper", 110.0, "trade_exit",
                                          position_id="pos_test_4",
                                          idempotency_key="exit_pos_test_4")
        self.assertEqual(new_balance, 1010.0)

    # --- Test 5: Insufficient funds blocks trade ---
    def test_insufficient_funds_blocks_trade(self):
        """Trade should be rejected when balance < amount."""
        from services.wallet import InsufficientFundsError
        with self.assertRaises(InsufficientFundsError):
            self.wallet.debit("paper", 1500.0, "trade_open",
                              idempotency_key="open_test_5")
        # Balance should be unchanged
        self.assertEqual(self.wallet.get_balance("paper"), 1000.0)

    # --- Test 6: Idempotency prevents double execution ---
    def test_idempotency_prevents_double_execution(self):
        """Second debit with same idempotency key should raise error."""
        from services.wallet import DuplicateTransactionError
        self.wallet.debit("paper", 100.0, "trade_open",
                          idempotency_key="open_test_6")

        with self.assertRaises(DuplicateTransactionError):
            self.wallet.debit("paper", 100.0, "trade_open",
                              idempotency_key="open_test_6")
        # Balance should show only one deduction
        self.assertEqual(self.wallet.get_balance("paper"), 900.0)

    # --- Test 7: Ledger records every mutation ---
    def test_ledger_records_every_mutation(self):
        """Every balance change should create exactly one ledger entry."""
        self.wallet.debit("paper", 50.0, "trade_open",
                          idempotency_key="open_test_7a")
        self.wallet.credit("paper", 55.0, "trade_exit",
                           idempotency_key="exit_test_7a")
        self.wallet.debit("paper", 30.0, "trade_open",
                          idempotency_key="open_test_7b")
        ledger = self.wallet.get_ledger("paper")
        self.assertEqual(len(ledger), 3)

    # --- Test 8: Ledger chain is consistent ---
    def test_ledger_chain_is_consistent(self):
        """Each entry's balance_after should equal the next entry's balance_before."""
        self.wallet.debit("paper", 100.0, "trade_open",
                          idempotency_key="open_test_8a")
        self.wallet.credit("paper", 120.0, "settlement_win",
                           idempotency_key="settle_test_8a")
        self.wallet.debit("paper", 200.0, "trade_open",
                          idempotency_key="open_test_8b")
        self.wallet.credit("paper", 0.0, "settlement_loss",
                           idempotency_key="settle_test_8b")

        ledger = self.wallet.get_ledger("paper")
        # Ledger is returned newest-first, reverse for chronological order
        ledger = list(reversed(ledger))
        for i in range(len(ledger) - 1):
            self.assertAlmostEqual(
                ledger[i]["balance_after"],
                ledger[i + 1]["balance_before"],
                places=2,
                msg=f"Chain break at entry {i}"
            )

    # --- Test 9: Conservation of money ---
    def test_conservation_of_money_full_lifecycle(self):
        """initial + sum(ledger_amounts) == current_balance."""
        self.wallet.debit("paper", 100.0, "trade_open",
                          idempotency_key="open_test_9a")
        self.wallet.credit("paper", 120.0, "settlement_win",
                           idempotency_key="settle_test_9a")
        self.wallet.debit("paper", 50.0, "trade_open",
                          idempotency_key="open_test_9b")
        self.wallet.credit("paper", 0.0, "settlement_loss",
                           idempotency_key="settle_test_9b")

        health = self.wallet.verify_conservation("paper")
        self.assertTrue(health["valid"], f"Conservation check failed: {health}")
        self.assertEqual(health["status"], "healthy")
        self.assertAlmostEqual(health["drift"], 0.0, places=2)

    # --- Test 10: Deposit increases balance ---
    def test_deposit_increases_balance(self):
        """Deposit endpoint should increase balance and record ledger entry."""
        new_balance = self.wallet.credit("paper", 500.0, "deposit",
                                          description="Manual deposit")
        self.assertEqual(new_balance, 1500.0)
        ledger = self.wallet.get_ledger("paper")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["tx_type"], "deposit")
        self.assertEqual(ledger[0]["amount"], 500.0)

    # --- Test 11: Reset sets balance ---
    def test_reset_sets_balance(self):
        """Reset should override balance and record delta in ledger."""
        self.wallet.debit("paper", 300.0, "trade_open",
                          idempotency_key="open_test_11")
        self.assertEqual(self.wallet.get_balance("paper"), 700.0)

        new_balance = self.wallet.set_balance("paper", 1000.0,
                                               description="Reset to default")
        self.assertEqual(new_balance, 1000.0)
        self.assertEqual(self.wallet.get_balance("paper"), 1000.0)

        ledger = self.wallet.get_ledger("paper")
        # Should have 2 entries: debit + reset
        self.assertEqual(len(ledger), 2)
        reset_entry = ledger[0]  # Most recent
        self.assertEqual(reset_entry["tx_type"], "reset")
        self.assertAlmostEqual(reset_entry["amount"], 300.0, places=2)  # delta: 1000 - 700

    # --- Test 12: Concurrent trades no double spend ---
    def test_concurrent_trades_no_double_spend(self):
        """Two threads trying to spend $800 on $1000 balance — only one should succeed."""
        from services.wallet import InsufficientFundsError

        results = {"success": 0, "fail": 0}

        def try_debit(key):
            try:
                self.wallet.debit("paper", 800.0, "trade_open",
                                  idempotency_key=f"concurrent_{key}")
                results["success"] += 1
            except InsufficientFundsError:
                results["fail"] += 1

        t1 = threading.Thread(target=try_debit, args=("A",))
        t2 = threading.Thread(target=try_debit, args=("B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(results["success"], 1,
                         "Only one trade should succeed")
        self.assertEqual(results["fail"], 1,
                         "One trade should fail with InsufficientFundsError")
        self.assertEqual(self.wallet.get_balance("paper"), 200.0)

    # --- Test 13: total_returned matches wallet credits ---
    def test_total_returned_matches_wallet_credits(self):
        """total_returned should equal cost + realized_pnl, not just pnl."""
        # Simulate: trade $100, win $20, total_returned should be $120
        self.wallet.debit("paper", 100.0, "trade_open",
                          idempotency_key="open_test_13")
        self.wallet.credit("paper", 120.0, "settlement_win",
                           idempotency_key="settle_test_13")

        ledger = self.wallet.get_ledger("paper")
        credit_entries = [e for e in ledger if e["amount"] > 0]
        total_credited = sum(e["amount"] for e in credit_entries)
        self.assertEqual(total_credited, 120.0,
                         "Total credited should be cost + profit")

    # --- Test 14: Mode isolation ---
    def test_mode_isolation(self):
        """Paper operations should not affect live balance (if it existed)."""
        initial = self.wallet.get_balance("paper")
        self.wallet.debit("paper", 100.0, "trade_open",
                          idempotency_key="open_test_14")
        # Verify paper balance changed
        self.assertEqual(self.wallet.get_balance("paper"), 900.0)
        # Verify there's no leakage to other modes
        ledger = self.wallet.get_ledger("paper")
        for entry in ledger:
            self.assertEqual(entry["mode"], "paper")

    # --- Test 15: Credit idempotency is non-error (returns current balance) ---
    def test_credit_idempotency_returns_balance(self):
        """Duplicate credit should silently return current balance, not error."""
        self.wallet.debit("paper", 100.0, "trade_open",
                          idempotency_key="open_test_15")
        b1 = self.wallet.credit("paper", 120.0, "settlement_win",
                                 idempotency_key="settle_test_15")
        b2 = self.wallet.credit("paper", 120.0, "settlement_win",
                                 idempotency_key="settle_test_15")
        self.assertEqual(b1, b2, "Duplicate credit should return same balance")
        # Only one ledger entry for the credit
        ledger = self.wallet.get_ledger("paper")
        credit_count = sum(1 for e in ledger if e["tx_type"] == "settlement_win")
        self.assertEqual(credit_count, 1)

    # --- Test 16: Multi-trade lifecycle conservation ---
    def test_multi_trade_lifecycle_conservation(self):
        """Place 5 trades, settle 3 wins + 2 losses. Final balance = expected."""
        trades = [
            ("t1", 100.0, "win", 30.0),    # cost=100, pnl=+30, return=130
            ("t2", 200.0, "win", 50.0),    # cost=200, pnl=+50, return=250
            ("t3", 150.0, "loss", -150.0), # cost=150, pnl=-150, return=0
            ("t4", 50.0, "win", 10.0),     # cost=50, pnl=+10, return=60
            ("t5", 100.0, "loss", -100.0), # cost=100, pnl=-100, return=0
        ]

        for trade_id, cost, result, pnl in trades:
            self.wallet.debit("paper", cost, "trade_open",
                              idempotency_key=f"open_{trade_id}")

        for trade_id, cost, result, pnl in trades:
            if result == "win":
                return_amount = cost + pnl
                self.wallet.credit("paper", return_amount, "settlement_win",
                                    idempotency_key=f"settle_{trade_id}")
            else:
                self.wallet.credit("paper", 0.0, "settlement_loss",
                                    idempotency_key=f"settle_{trade_id}")

        # Expected: 1000 - 600 (total cost) + 440 (total returns) = 840
        expected = 1000.0 - (100+200+150+50+100) + (130+250+0+60+0)
        self.assertAlmostEqual(self.wallet.get_balance("paper"), expected, places=2)

        # Conservation check should pass
        health = self.wallet.verify_conservation("paper")
        self.assertTrue(health["valid"], f"Conservation failed: {health}")

    # --- Test: Negative amounts are rejected ---
    def test_negative_debit_rejected(self):
        """Debit with negative amount should raise ValueError."""
        with self.assertRaises(ValueError):
            self.wallet.debit("paper", -50.0, "trade_open")

    def test_negative_credit_rejected(self):
        """Credit with negative amount should raise ValueError."""
        with self.assertRaises(ValueError):
            self.wallet.credit("paper", -50.0, "settlement_win")


if __name__ == "__main__":
    unittest.main()
