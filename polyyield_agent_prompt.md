# Master Agent Prompt: Bootstrapping PolyYield Live Bot

Copy and paste the entire prompt below into a new Antigravity session in your new, empty workspace folder. Make sure to also copy `polyyield_handover_context.md` into the same directory so the agent can reference it.

***

```markdown
You are an expert quantitative trading developer. We are starting a new project in this empty workspace to build and deploy a standalone, 24/7 autonomous prediction market trading bot on Polymarket called **PolyYield**. 

Our goal is to build a highly robust, profitable system that exploits mathematically grounded, low-risk, and risk-free arbitrage opportunities on Polymarket using the Gamma API (data) and the CLOB API (execution).

Please read the architectural specifications, mathematical formulas, and database schemas documented in the attached context file:
[polyyield_handover_context.md](file:///polyyield_handover_context.md) (or refer to the PolyYield specifications).

We will implement 6 core yield strategies, ranging from lowest risk (mathematically guaranteed risk-free) to highest risk:
1. **S3 — Buy-All Arbitrage:** Multi-outcome price sum < 1.00 (Risk-Free).
2. **S4 — Correlation Arbitrage:** Parent vs subset price logical contradictions (Risk-Free).
3. **S5 — Sub-Event / Sum Arbitrage:** Parent price vs sum of sub-events discrepancies (Very Low Risk).
4. **S2 — Split Share Farming:** Delta-neutral market-making on reward pools (Low to Medium Risk).
5. **S1 — Novelty Yield:** Buying NO on low-probability meme/culture markets (Medium Risk).
6. **S6 — Longshot Bias Market Maker:** Systematic selling of overpriced longshots (Medium to High Risk).

---

### Step 1: Install Dependencies
Create a `requirements.txt` file and install:
- `py-clob-client` (Polymarket's Python SDK for order signing and execution)
- `httpx` (Asynchronous HTTP client)
- `fastapi` & `uvicorn` (Dashboard API & serving)
- `web3` & `eth-account` (For checking hot wallet balance & Polygon transactions)
- `pydantic` & `python-dotenv` (Config loading)
- `python-dateutil` (Parsing expiry dates)

---

### Step 2: Database Layer
Create `db/database.py` and `db/config.py` using SQLite.
1. Scaffold the tables according to the schema in `polyyield_handover_context.md`:
   - `poly_yield_opportunities` (scanned trades, actions, legs)
   - `poly_yield_positions` (executed trades, status, realized PnL, actual slippage)
   - `poly_yield_stats` (per-strategy metrics: win/loss count, open positions, total PnL)
2. Add a dynamic configuration table `system_config` to store variables like:
   - `poly_yield.active_mode`: 'paper' or 'live'
   - `poly_yield.scan_interval_s`: default 120
   - `poly_yield.enabled`: true/false
   - `poly_yield.auto_exec_drawdown_limit`: default 50.0%
   - `s1.enabled`, `s2.enabled`, `s3.enabled`, `s4.enabled`, `s5.enabled`, `s6.enabled`
   - Strategy-specific parameters (`min_apy`, `max_position_pct`, `exec_mode` [auto/semi/manual], etc.)

---

### Step 3: Execution Services
Create these key helper modules under `services/`:
1. **`keystore.py`**: Safe loading of private keys from environments or config.
2. **`gas_tracker.py`**: Queries the Polygon gas API/RPC to estimate execution costs (converted to USDC) so we can deduct them from profit margins.
3. **`alerts.py`**: Asynchronously dispatches alerts via Discord/Telegram webhooks for new trades, balance warnings, settlements, and critical failures.

---

### Step 4: Core Engine and Calibrator
Create the strategies under `strategies/`:
1. **`calibration.py`**: Implements the historical calibrator that scans resolved S6 positions in the database to adjust correction multipliers for S6.
2. **`registry.py`**: Defies the risk level, default execution type, and name registry for the 6 strategies.
3. **`engine.py`**: 
   - Runs the main asynchronous scanning loop.
   - Fetches active markets from Polymarket's Gamma API.
   - Implements **Institutional L2 Book VWAP walking** to calculate the exact execution price and slippage of a given USDC trade size on bids/asks.
   - Runs strategies S1–S6. For each opportunity found, check slippage and calculate net APY (deducting gas and commissions). Upsert the opportunity to the database.
   - For auto-execution (`exec_mode = 'auto'`), verify the drawdown limit, check gas, and dispatch orders.
   - **CRITICAL SAFEGUARDS:** For multi-leg strategies (S3, S4, S5), perform a pre-flight book walk for ALL legs. Abort if any leg fails the slippage limit. Place legs in parallel using `asyncio.gather`. If any leg fails to fill while others succeeded, immediately trigger a cancellation roll-back and engage a **Killswitch** that freezes the system and fires a critical alert.
4. **`settlement.py`**:
   - Background worker polling Gamma API every 5 minutes for open positions.
   - Resolves positions as "won" or "lost" when a market closes.
   - Computes realized PnL, actual APY, and updates `poly_yield_stats`.

---

### Step 5: Dashboard and Control Center
Create `main.py` to orchestrate everything:
1. Boot the `PolyYieldEngine` scanning loop and the `PolyYieldSettlement` loop.
2. Serve a FastAPI application with REST endpoints:
   - `GET /api/poly-yield/opportunities` (active opps)
   - `GET /api/poly-yield/positions` (open/historical trades)
   - `GET /api/poly-yield/stats` (performance analytics)
   - `GET /api/poly-yield/config` & `POST /api/poly-yield/config` (mode updates, parameters)
   - `POST /api/poly-yield/execute/{opp_id}` (manually trigger a semi/manual opportunity)
3. Create a beautiful, premium, single-page web dashboard using Vanilla HTML/CSS/JS (embedded in or served by FastAPI). Give it:
   - **Vibrant Dark-mode/Glassmorphism design** with curated colors (e.g. deep charcoal backgrounds, neon green accents for wins, orange for alerts).
   - Metrics cards showing: Wallet Balance, Active Mode (Paper/Live banner), Active Positions, and Total Realized PnL.
   - Table of **Open Opportunities** displaying: Strategy, Market Question, Implied Prob, Projected APY/Profit, Slippage, Action, and an "Execute" button.
   - Table of **Active Positions** showing: Strategy, Market, Entry Price, Current Price, Profit/PnL, and Status.
   - Configuration panel allowing real-time toggle of Paper/Live, strategy limits, and global killswitch reset.

Let's write this codebase completely, cleanly, and with premium styling. Scaffold the folders and files now!
```
***
