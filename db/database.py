"""
PolyYield Database Layer
Scaffolds SQLite tables for opportunities, positions, statistics, and dynamic configurations.
"""
import sqlite3
import threading
import logging
from pathlib import Path
from config import settings

_log = logging.getLogger(__name__)

_sqlite_conn = None
_sqlite_lock = threading.RLock()

def get_sqlite() -> sqlite3.Connection:
    """Get persistent SQLite connection (thread-safe via RLock)."""
    global _sqlite_conn
    with _sqlite_lock:
        if _sqlite_conn is not None:
            try:
                _sqlite_conn.execute("SELECT 1")
            except sqlite3.Error:
                _sqlite_conn = None

        if _sqlite_conn is None:
            db_path = Path(settings.sqlite_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            _sqlite_conn = sqlite3.connect(str(db_path), check_same_thread=False)
            _sqlite_conn.row_factory = sqlite3.Row
            # Enable WAL mode for concurrent reads & writes
            _sqlite_conn.execute("PRAGMA journal_mode=WAL")
            _sqlite_conn.execute("PRAGMA busy_timeout=5000")
        return _sqlite_conn

def close_db():
    """Close sqlite database connection on shutdown."""
    global _sqlite_conn
    with _sqlite_lock:
        if _sqlite_conn:
            try:
                _sqlite_conn.close()
            except Exception as e:
                _log.warning("SQLite close failed: %s", e)
            _sqlite_conn = None

def init_db():
    """Scaffold all tables needed for PolyYield."""
    conn = get_sqlite()
    with _sqlite_lock:
        # Check if poly_yield_stats has 'mode' column, if not, drop it to migrate
        try:
            cursor = conn.execute("PRAGMA table_info(poly_yield_stats)")
            cols = [r["name"] for r in cursor.fetchall()]
            if cols and "mode" not in cols:
                conn.execute("DROP TABLE poly_yield_stats")
                conn.commit()
        except Exception:
            pass

        conn.executescript("""
            -- Dynamic system configurations table
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Opportunities found during scanning
            CREATE TABLE IF NOT EXISTS poly_yield_opportunities (
                id TEXT PRIMARY KEY,
                strategy TEXT NOT NULL,
                risk_level TEXT,
                execution_type TEXT,
                market_type TEXT,
                reward_score REAL,
                slippage_bps REAL,
                market_id TEXT NOT NULL,
                market_title TEXT,
                market_url TEXT,
                token_id TEXT,
                outcome TEXT,
                entry_price REAL,
                implied_prob REAL,
                yes_price REAL,
                no_price REAL,
                annualized_apy REAL,
                profit_pct REAL,
                days_to_expiry REAL,
                action TEXT,
                exec_mode TEXT,
                suggested_usdc REAL,
                status TEXT DEFAULT 'open',
                notes TEXT,
                instructions TEXT, -- JSON array of manual steps
                legs TEXT,         -- JSON array of legs (for multi-outcome)
                max_profit_usdc REAL, -- best-case payoff if held to resolution
                max_loss_usdc REAL,   -- worst-case payoff if held to resolution
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Active and historical positions
            CREATE TABLE IF NOT EXISTS poly_yield_positions (
                id TEXT PRIMARY KEY,
                opportunity_id TEXT,
                strategy TEXT NOT NULL,
                market_id TEXT NOT NULL,
                market_title TEXT,
                token_id TEXT,
                outcome TEXT NOT NULL,
                shares REAL NOT NULL,
                entry_price REAL NOT NULL,
                cost_usdc REAL NOT NULL,
                order_id TEXT,
                status TEXT DEFAULT 'open', -- open, won, lost, settled
                entry_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                settled_at TIMESTAMP,
                realized_pnl REAL,
                predicted_apy REAL,
                predicted_profit_pct REAL,
                predicted_days_to_expiry REAL,
                actual_fill_price REAL,
                actual_gas_usdc REAL,
                risk_level TEXT,
                fill_slippage_bps REAL,
                quality_at_entry REAL,
                predicted_pnl_usdc REAL,
                mode TEXT,                  -- paper, live
                settlement_outcome TEXT,
                apy_delta REAL,
                stop_loss_price REAL,
                take_profit_price REAL,
                trailing_stop_pct REAL,
                highest_price REAL,
                max_profit_usdc REAL,  -- best-case payoff at entry (actual fill, not predicted)
                max_loss_usdc REAL,    -- worst-case payoff at entry (actual fill, not predicted)
                executed_by TEXT DEFAULT 'bot', -- 'bot' (auto scan loop) or 'manual' (human-triggered API call)
                exit_price REAL        -- price at manual/stop exit, or 1.0/0.0 on resolution settlement
            );

            -- Strategy performance stats
            CREATE TABLE IF NOT EXISTS poly_yield_stats (
                strategy TEXT,
                mode TEXT NOT NULL,
                total_pnl REAL DEFAULT 0.0,
                total_returned REAL DEFAULT 0.0,
                win_count INTEGER DEFAULT 0,
                loss_count INTEGER DEFAULT 0,
                open_positions INTEGER DEFAULT 0,
                last_scan_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (strategy, mode)
            );

            -- API keys for storing encrypted wallet secrets
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                service TEXT NOT NULL,
                key_value TEXT NOT NULL,
                label TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_tested_at TIMESTAMP,
                status TEXT DEFAULT 'untested'
            );

            -- Event-sourced wallet ledger (audit trail for all balance changes)
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

            -- Dutching Multi-LLM Arena Instance Allocations
            CREATE TABLE IF NOT EXISTS dutching_arena_instances (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,         -- openai, anthropic, kimi, deepseek
                model_name TEXT NOT NULL,       -- gpt-4o, claude-3-5-sonnet-20241022, etc.
                allocated_budget_usdc REAL NOT NULL DEFAULT 10.0,
                used_budget_usdc REAL NOT NULL DEFAULT 0.0,
                active_positions INTEGER NOT NULL DEFAULT 0,
                win_count INTEGER NOT NULL DEFAULT 0,
                loss_count INTEGER NOT NULL DEFAULT 0,
                total_pnl REAL NOT NULL DEFAULT 0.0,
                status TEXT DEFAULT 'active',    -- active, paused, drained
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Dutching Multi-Leg Executions
            CREATE TABLE IF NOT EXISTS dutching_trades (
                id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                market_title TEXT NOT NULL,
                top_candidates_json TEXT NOT NULL,
                sum_market_price REAL NOT NULL,
                sum_fill_price REAL NOT NULL,
                p_model_top_set REAL NOT NULL,
                p_tail_risk REAL NOT NULL,
                confidence REAL NOT NULL,
                stake_usdc REAL NOT NULL,
                legs_json TEXT NOT NULL,
                status TEXT DEFAULT 'open',     -- open, won, lost, settled
                mode TEXT DEFAULT 'paper',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                settled_at TIMESTAMP,
                pnl_usdc REAL
            );
        """)
        
        # Safe migration for existing databases: Add columns if they do not exist
        migration_cols = [
            ("poly_yield_positions", "stop_loss_price", "REAL"),
            ("poly_yield_positions", "take_profit_price", "REAL"),
            ("poly_yield_positions", "trailing_stop_pct", "REAL"),
            ("poly_yield_positions", "highest_price", "REAL"),
            ("poly_yield_positions", "token_id", "TEXT"),
            ("poly_yield_positions", "idempotency_key", "TEXT"),
            ("poly_yield_opportunities", "token_id", "TEXT"),
            ("poly_yield_opportunities", "max_profit_usdc", "REAL"),
            ("poly_yield_opportunities", "max_loss_usdc", "REAL"),
            ("poly_yield_positions", "max_profit_usdc", "REAL"),
            ("poly_yield_positions", "max_loss_usdc", "REAL"),
            ("poly_yield_positions", "executed_by", "TEXT"),
            ("poly_yield_positions", "exit_price", "REAL"),
        ]
        for table_name, col_name, col_type in migration_cols:
            try:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                # Column already exists, ignore
                pass
        
        conn.commit()
        
        # Seed default configuration parameters if they don't exist
        defaults = [
            ("poly_yield.active_mode", "paper"),
            ("poly_yield.scan_interval_s", "120"),
            ("poly_yield.enabled", "true"),
            ("poly_yield.auto_exec_drawdown_limit", "50.0"),
            ("poly_yield.max_slippage_pct", "1.5"),
            # Strategy switches
            ("s1_novelty.enabled", "true"),
            ("s2_split.enabled", "true"),
            ("s3_buy_all.enabled", "true"),
            ("s4_corr.enabled", "true"),
            ("s5_sub_event.enabled", "true"),
            ("s6_longshot.enabled", "true"),
            ("s8_late_stage.enabled", "true"),
            ("s9_stablecoin_peg.enabled", "true"),
            ("s10_oracle.enabled", "true"),
            ("s11_overreaction.enabled", "true"),
            ("s12_momentum.enabled", "true"),
            ("s13_sentiment.enabled", "true"),
            ("s14_macro_corr.enabled", "true"),
            ("s15_theta.enabled", "true"),
            ("s16_poll_drift.enabled", "true"),
            ("s17_sniper.enabled", "true"),
            ("s18_straddle.enabled", "true"),
            ("s19_longshot_yes.enabled", "true"),
            ("s20_dutching.enabled", "true"),
            ("favorite_compounding.enabled", "true"),
            ("copy_trading.enabled", "true"),
            # Strategy execution modes: auto, semi, manual
            ("s1_novelty.exec_mode", "semi"),
            ("s2_split.exec_mode", "semi"),
            ("s3_buy_all.exec_mode", "auto"),
            ("s4_corr.exec_mode", "semi"),
            ("s5_sub_event.exec_mode", "manual"),
            ("s6_longshot.exec_mode", "auto"),
            ("s8_late_stage.exec_mode", "semi"),
            ("s9_stablecoin_peg.exec_mode", "auto"),
            ("s10_oracle.exec_mode", "semi"),
            ("s11_overreaction.exec_mode", "semi"),
            ("s12_momentum.exec_mode", "semi"),
            ("s13_sentiment.exec_mode", "manual"),
            ("s14_macro_corr.exec_mode", "manual"),
            ("s15_theta.exec_mode", "manual"),
            ("s16_poll_drift.exec_mode", "semi"),
            ("s17_sniper.exec_mode", "auto"),
            ("s18_straddle.exec_mode", "manual"),
            ("s19_longshot_yes.exec_mode", "semi"),
            ("s20_dutching.exec_mode", "manual"),
            ("favorite_compounding.exec_mode", "auto"),
            ("copy_trading.exec_mode", "auto"),
            # Strategy parameters
            ("s1_novelty.max_yes_price", "0.08"),
            ("s1_novelty.min_apy", "4.0"),
            ("s1_novelty.max_position_pct", "0.02"),
            ("s2_split.min_apy", "10.0"),
            ("s2_split.max_position_pct", "0.05"),
            ("s3_buy_all.min_profit_pct", "0.5"),
            ("s3_buy_all.max_position_pct", "0.10"),
            ("s4_corr.min_gap_pct", "1.0"),
            ("s4_corr.max_position_pct", "0.05"),
            ("s4_corr.correlation_rules", "[]"),
            ("s5_sub_event.min_gap_pct", "1.5"),
            ("s5_sub_event.max_position_pct", "0.05"),
            ("s6_longshot.max_yes_price", "0.08"),
            ("s6_longshot.max_positions", "10"),
            ("s6_longshot.position_pct", "0.02"),
            ("s8_late_stage.min_price", "0.98"),
            ("s8_late_stage.max_days_left", "3.0"),
            ("s8_late_stage.max_position_pct", "0.05"),
            ("s8_late_stage.min_apy", "3.0"),
            ("favorite_compounding.min_yes_price", "0.95"),
            ("favorite_compounding.max_days_left", "7.0"),
            ("favorite_compounding.max_position_pct", "0.10"),
            ("favorite_compounding.min_apy", "5.0"),
            ("copy_trading.max_position_pct", "0.05"),
            ("copy_trading.target_wallets", "[]"), # JSON array of addresses to track
            # Money Management
            ("portfolio.kelly_fraction", "0.10"),
            ("portfolio.paper_balance", "1000.0"),
            ("portfolio.tradeable_limit", "100.0"),
            ("portfolio.default_stop_loss_pct", ""),      # Empty means disabled by default
            ("portfolio.default_take_profit_pct", ""),     # Empty means disabled by default
            ("portfolio.default_trailing_stop_pct", ""),   # Empty means disabled by default
            ("portfolio.daily_loss_limit", "50.0"),            # Max USDC daily loss allowed
            ("portfolio.consecutive_loss_limit", "3"),       # Max consecutive losses before shutdown
            ("portfolio.circuit_breaker_active", "false")  # Manual override switch
        ]
        
        for k, v in defaults:
            conn.execute(
                "INSERT OR IGNORE INTO system_config (key, value) VALUES (?, ?)",
                [k, v]
            )

        # One-time safety migration (v2): S4/S8 shipped with exec_mode 'auto' before
        # their execution paths were hardened — existing databases keep seeded values,
        # so force these to 'semi' once. Users can re-enable auto deliberately in the UI.
        row = conn.execute("SELECT value FROM system_config WHERE key = 'schema_version'").fetchone()
        current_version = int(row["value"]) if row else 1
        if current_version < 2:
            conn.execute(
                "UPDATE system_config SET value = 'semi', updated_at = datetime('now') "
                "WHERE key IN ('s4_corr.exec_mode', 's8_late_stage.exec_mode') AND value = 'auto'"
            )
            conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('schema_version', '2')")

        # One-time migration (v3): backfill executed_by for positions recorded before
        # this column existed. Manual-trade-panel positions are identifiable by
        # strategy key; everything else predates the distinction and defaults to 'bot'
        # (the vast majority of pre-existing rows came from the auto-scan loop).
        if current_version < 3:
            conn.execute(
                "UPDATE poly_yield_positions SET executed_by = 'manual' "
                "WHERE executed_by IS NULL AND strategy IN ('manual', 'manual_trade')"
            )
            conn.execute(
                "UPDATE poly_yield_positions SET executed_by = 'bot' WHERE executed_by IS NULL"
            )
            conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('schema_version', '3')")

        conn.commit()
