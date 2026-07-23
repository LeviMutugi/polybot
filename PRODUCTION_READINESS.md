# PolyYield — Production Readiness Audit & Fixes

This document records the full audit performed before real-money deployment: every
defect found, what was fixed, and what risk **remains by design** (strategy risk and
platform/hosting risk — which you accepted — versus logic/math/data risk, which has
been eliminated wherever found).

---

## 1. Critical bugs fixed (these would have lost real money)

### 1.1 Order book read upside-down (every strategy affected)
Polymarket's CLOB `/book` endpoint returns **bids ascending and asks descending — the
best price is LAST**. The VWAP walker iterated the arrays in order, so it:
- walked the **worst** prices first → wrong fill estimates, wrong slippage, wrong APY;
- used `bids[0]` as "best bid" → the stop-loss/trailing-stop checker read the **worst**
  bid (e.g. $0.01) as the current price and could **instantly stop-loss every position
  at a bogus price**.

**Fix:** `sort_book_levels()` in `strategies/base.py` normalizes both sides
(asks ascending, bids descending) regardless of API ordering; settlement stop checks
and the UI order-book panel use the same normalization. Regression-tested.

### 1.2 Kelly sizing unit bug — every auto trade sized at the 20 % cap
Strategies passed `est_true_prob` as a **percent** (e.g. `93.5`) while the allocator
compared it to a **fraction** price (`0.93`). The Kelly formula then computed an
absurd edge and every auto/semi trade was silently sized at the maximum 20 % of
capital instead of ~2 %.

**Fix:** `est_true_prob` is now a fraction everywhere; the allocator additionally
**validates units** (both inputs must be in (0,1)) and falls back to fixed-fraction
sizing when they aren't, and **never allocates more than the strategy suggested**.

### 1.3 Stale opportunities were shown and executable forever
Nothing ever expired opportunity rows. The UI showed every historic row as "open",
and clicking Execute traded at the price from an old scan — exactly the
"executed a trade, then failed / opp wasn't available" problem.

**Fix (three layers):**
1. After every scan, opportunities not re-confirmed are marked `stale`; rows older
   than a day are purged.
2. The `/opportunities` API only returns rows that are `open` **and** fresher than
   3× the scan interval.
3. `execute_opportunity` re-checks status/age **and re-walks the live order book**
   before committing: if the price drifted more than `max_slippage_pct` since the
   scan, execution is refused with a clear message instead of filling at a bad price.
   This applies to paper and live, single-leg and every leg of multi-leg baskets.

### 1.4 Live multi-leg execution crashed after spending money
`_place_live_order` returned fill results **without the order id**, so on the success
path `p["order_id"]` raised `KeyError` → orders were filled on-chain but **no position
was recorded**; on the failure path the rollback couldn't cancel anything.
**Fix:** the order id is carried through; rollback cancellation runs in a thread; the
partial-fill killswitch path is intact.

### 1.5 Settlement could declare a winner on a guess
If a market was `closed` but not definitively priced, a fallback declared any outcome
above **50 %** the winner and realized PnL on it. **Fix:** settlement now requires a
definitive ≥ 0.99 settlement price; otherwise the position stays open and is
re-checked on the next poll.

### 1.6 Re-entry permanently blocked in paper mode
The wallet idempotency key was `open_{opportunity_id}` and opportunity ids are
deterministic — after one trade in a market, any later trade on the same opportunity
id raised "duplicate" forever. **Fix:** keys are per-position (`open_{pos_id}`),
while duplicate protection moved inside the market lock where it belongs. If
recording a position fails after a paper debit, the funds are **auto-refunded**
(`trade_refund` ledger entry) so wallet ≡ positions always.

### 1.7 API leaked your secret
`GET /api/debug-secret` returned the API secret **unauthenticated**, and the auth
dependency printed the expected secret to stdout on every request. Both removed.

### 1.8 Encryption master key committed to git
`.secret` (the Fernet key) **and** `poly_yield.db` (containing your encrypted wallet
key) were tracked in the repository — anyone with repo access could decrypt the
wallet private key. Both are now untracked and ignored.
**⚠️ ACTION REQUIRED: treat any private key that was stored under the old committed
`.secret` as compromised. Generate a fresh wallet before going live.**

---

## 2. Correctness bugs fixed (wrong numbers / broken features)

| Area | Bug | Fix |
|---|---|---|
| APY math | S3/S4/S5 annualized with `days` floored at **0.1** (up to 3650× multiplier); formulas inconsistent across strategies | `calculate_simple_apy` / `calculate_compounding_apy` with days floored at 1 and a sanity cap; `apy_delta` now compares like with like |
| Liquidity | "Insufficient liquidity" walks returned a `warning` (not `error`) with a partial-fill price — scans accepted them | shared `is_fillable()` guard: every strategy now rejects unfillable/over-slippage books |
| Suggested size in UI | `/opportunities` recomputed sizing by passing `implied_prob/100` as "true probability" → Kelly denied everything → **every opportunity showed $0.00** (with a per-row RPC call in live mode) | sizing context computed once; suggested size clipped to remaining drawdown room; authoritative check still runs at execution |
| Copy trading | Wrong CTF contract address **and** wrong topic index (filtered transfers *from* the wallet, i.e. sells, not buys) → silently dead | canonical CTF `0x4D97…2830`, `topics[3]` = recipient; dedup cache bounded |
| S4 correlation | Shipped with naive substring rules ("trump wins" matching any market) on **auto** mode, and random UUID ids created duplicate rows every scan | only user-curated `s4_corr.correlation_rules` are used (none → no signals); deterministic ids; default mode `semi` (+ one-time DB migration) |
| S8 late-stage | No token id, no liquidity check, hard-coded $10 stake, `execute()` unimplemented **but default mode was auto** (live execution failed every scan) | fully implemented: book walk, gas, config-driven sizing/thresholds, real execute; default `semi` |
| S2 split farm | stored `condition_id` as `market_id` → every downstream Gamma lookup 404'd; limit prices could go ≤ 0; rewards API dict response crashed the scan | real Gamma id stored; prices clamped to [0.01, 0.99]; response shape handled |
| S6 longshot | open-position slot count ignored mode (paper positions consumed live slots) | counts filtered by active mode |
| Positions | `token_id` was never persisted → stop-loss/exit had to guess via Gamma title-matching (failed for manual trades entirely) | token id persisted at entry and used for stops/exits |
| Live redemption | `Web3(AsyncHTTPProvider)` mixes sync/async — every awaited call would fail | `AsyncWeb3` |
| Engine | auto-exec sort crashed when APY was `None`; multi-leg stake divisor used parent price instead of leg-price sum (wrong S5 splits) | both fixed |
| Risk labels | scanner opps carried no `risk_level` → UI showed everything as "high risk" and the risk filter never matched | engine stamps the strategy's risk level |
| Validation | manual trade accepted price ≥ 1 / ≤ 0, negative stakes, SL above entry; exits accepted any price | full server-side validation on trade, SL/TP/TS relationships, and exit price |
| Config API | could overwrite `portfolio.paper_balance` directly, silently breaking the ledger's conservation-of-money guarantee | protected key — only auditable deposit/reset endpoints may change balances |
| Boot | `python main.py` (as documented for PM2) did nothing — no server runner existed | `uvicorn.run` main block added |

## 3. UI bugs fixed

- **Quote-breaking rows**: market titles containing `'` or `"` were inlined into
  `onclick` attributes — clicking those rows threw JS errors (details/autofill/execute
  broken for those markets). All handlers are now index-based lookups.
- **Manual-only opportunities** had a live Execute button that really executed;
  now rendered disabled with an explanatory tooltip. The server also rejects
  execution of manual-mode strategy opportunities (defense in depth), so
  **manual = instructions only, semi = executes on your click, auto = bot executes** —
  enforced in both layers.
- **Live-mode confirmation** dialog (market, outcome, size, price) before any
  execute click spends real money.
- Order-book panel showed the **worst** 8 levels (same ordering bug) — now sorted.
- Positions table `colspan` mismatch; unescaped outcome text; `null` entry price
  crashing the exit prompt — fixed.
- One `fetchOpportunities` per scanned opportunity (50 finds = 50 refetches) —
  debounced; failed executions auto-refresh the table since denial usually means
  the price moved.
- Testnet toggle now warns that market data remains mainnet (testnet is for order
  plumbing tests only).

## 4. Test coverage

`tests/` now has **95 passing tests**. Beyond the original regression tests (book
normalization, VWAP walking, fillability guard, APY bounds, Kelly unit sanitization,
allocation caps, staleness rejection/marking, manual-mode rejection, definitive-winner
settlement, full paper-trade wallet conservation, and exit price validation), there
are 19 tests covering every S9-S19 strategy's core logic plus the S5/S3 fixes
(`tests/test_new_strategies.py`), 9 covering full-catalog pagination, S20's
unfillable-leg flagging, and the engine's auto-exec eligibility filter
(`tests/test_market_coverage_and_safety.py`), and 6 covering the manual multi-leg
basket trade endpoint — validation errors and a full paper-mode execution proving
proportional (equal-shares) sizing across legs (`tests/test_manual_basket_trade.py`).

**Test harness fix:** `pytest-asyncio` was never declared anywhere (not in
`requirements.txt`, no `pytest.ini`/`conftest.py` setting `asyncio_mode`), so every
`@pytest.mark.asyncio` test silently never ran its body on a clean install — the
"40 passing tests" figure above could not have been verified the way `pip install
-r requirements.txt && pytest` describes. Added `pytest.ini` (`asyncio_mode = auto`)
and `requirements-dev.txt`. With the async bodies actually executing, two stale
assertions surfaced and were fixed (exact-stake wallet-conservation checks that
didn't account for the engine's own simulated gas debit; a hardcoded wrong
Dutching instance id).

Run: `pip install -r requirements.txt -r requirements-dev.txt && python -m pytest tests/ -q`

---

## 5. S9-S19, S5, and S3: implemented (previously stubs/documented gaps)

As of this pass, every strategy that used to be a stub or a documented dormant
limitation has real logic — see each strategy file's module docstring for the
honest specifics of what data it uses and what it doesn't:

- **S9 Stablecoin Peg Arb, S10 Oracle Discrepancy, S15 Theta Harvester, S17
  Liquidity Sniper, S18 Catalyst Straddle, S19 Longshot YES** — real, built only
  from Gamma market snapshots + the live CLOB book (no external data).
- **S11 Overreaction, S12 Momentum** — real, using a new shared in-process rolling
  price-history utility (`strategies/base.py`); needs a short warm-up per market
  since there's no external time-series feed, and resets on process restart.
- **S13 Sentiment, S16 Poll Drift, S14 Macro Correlation** — real, but explicitly
  PROXIES: S13 uses order-book imbalance + price momentum (not news/social
  sentiment — true multi-source sentiment via GDELT is on the roadmap, called out
  separately in the UI so the two are never conflated), S16 uses election-keyword-
  scoped price trend (not a real polling aggregator), S14 trades user-curated
  correlated Polymarket *pairs* only (empty/no-signal by default, same safe
  pattern as S4) rather than real macro/economic index data. All three are
  labeled as proxies in both code and the Strategy Control Panel UI.
- **S5 Sub-Event Arb** was dormant because Polymarket slugs don't encode a
  parent/child hierarchy. Replaced the slug-guessing with question-text grouping
  (strip a trailing temporal clause like "in Q1 2026" off each market question;
  markets sharing the remaining prefix are sub-events, and a same-prefix market
  with only a bare year clause is the parent) — reconstructs the exact
  relationship the strategy was designed for, from data already fetched.
- **S3 Buy-All** only covered native multi-outcome markets (≥ 3 outcomes in one
  Gamma market object). Added a second pass for neg-risk baskets — multi-outcome
  events represented as several separate binary Yes/No markets sharing a neg-risk
  group id — running the identical sum-of-YES-prices-< $1.00 arbitrage math
  across them. (The exact neg-risk group-id field name can't be verified against
  a live API from this environment; the lookup is fully defensive, so a wrong
  field name just means zero extra groups, not a wrong trade — same fail-safe
  posture as everywhere else in this codebase.)
- While adding S3's second pass, found and fixed a **real latent bug**: `max_slippage`
  and an `is_fillable` import were fetched/imported *inside* the per-market loop,
  making them function-local names only bound once that specific line executes for
  at least one market. Any code path in `scan()` that could return before that
  line ran — which the new second pass did — would hit `UnboundLocalError`.
  Hoisted both to the top of the function.

## 6. Full-catalog scanning, S20 Dutching accuracy, and auto-mode safety (this pass)

Triggered by an external code review of S20 Dutching that found a misleading
docstring, a UI-spam risk in auto mode, and an artificial scan cap. All three are
fixed, and the fixes generalize to every strategy, not just Dutching:

- **S20's docstring falsely claimed** it uses "LLM-based sentiment / tail-risk
  modeling to discount stake size" — it doesn't; sizing is a fixed fraction of
  wallet balance split proportionally per leg. Rewritten to describe what's
  actually there, and to note that per-market LLM tail-risk evaluation already
  exists as a separate, human-triggered tool (Dutching Bot Arena's
  "Run Multi-LLM Evaluation" / `/api/dutching/evaluate`) — wiring that same
  evaluation into S20's own automatic scan/sizing loop is a planned addition,
  not present today.
- **Unfillable-opportunity spam in auto mode.** S20 deliberately still surfaces
  an opportunity even when a leg fails the live order-book fillability check
  ("we still want to show the market" — reasonable for visibility), but nothing
  distinguished that state, so if S20 were switched to `auto` the engine would
  re-attempt — and fail — that same execution every single scan, spamming logs.
  Fixed with a `fillable` / `unfillable_reason` field on the opportunity (DB
  migration on `poly_yield_opportunities`): the UI now shows a clear "⚠️
  Unfillable" badge and disables the Execute/Quick-Trade button with the reason
  as a tooltip, and the engine's auto-exec eligibility filter
  (`PolyYieldEngine._select_auto_opportunities`) now excludes `fillable=False`
  rows outright — the auto loop never even attempts an execution already known
  bad. `fillable` defaults `True` and every other strategy is unaffected (they
  already just skip an unfillable leg rather than surfacing it).
- **Every strategy was scanning a capped subset of the market**, not the whole
  thing: `PolyYieldEngine._fetch_markets` capped Gamma `/markets` at a single
  500-item page (ordered by liquidity — so anything past the top 500 by
  liquidity was invisible to every strategy), and S20 separately capped Gamma
  `/events` at a single 100-item page. Added `strategies.base.fetch_all_paginated`
  — a shared offset/limit pagination helper with a small delay between page
  requests (a self-imposed courtesy since Polymarket's exact rate limit isn't
  published/knowable from here) and a single backoff-retry on an explicit 429 —
  and switched both call sites to it. Page size, max-page safety valve, and
  inter-page delay are all configurable
  (`poly_yield.market_fetch_*`, `s20_dutching.event_fetch_*`) with defaults that
  preserve today's page sizes (500 / 100) while removing the *cap* on total
  pages fetched. A failed/rate-limited page stops pagination gracefully and
  keeps whatever was already gathered, rather than losing the whole scan.
  Scans will take longer wall-clock time than before (proportional to how many
  markets/events actually exist) — if that starts to bump up against
  `poly_yield.scan_interval_s`, raise the interval before shrinking page
  coverage back down.

## 7. Manual multi-leg / Dutching-style basket trades

Previously the *only* way to place a Dutching-style "spread stake across several
picks for a uniform payout if any one hits" trade was to wait for S20 to scan and
surface it — the generic Manual Trade panel only ever supported a single outcome,
so a user who wanted to build that kind of basket from candidates *they* picked
(not ones the bot had already found) had no way to do it.

Added `POST /api/poly-yield/manual-basket-trade` (`main.py`): takes N
user-specified legs (market_id/token_id/outcome/price) and a total stake, does not
reimplement any sizing math itself — it hands the legs straight to the exact same
`execute_opportunity()` → `_execute_multi_leg()` pipeline every multi-leg strategy
(S3, S5, S17, S18, S20) already uses, so it gets proportional-by-price sizing, the
live per-leg order-book pre-flight, and partial-fill rollback/killswitch for free.
Defaults to the conservative `conditional_multi_leg` payoff assumption (a covered
subset can still lose if none of it hits) unless the user explicitly flags the set
as `guaranteed_arb` (every possible outcome covered).

The dashboard's Manual Trade panel now has a **Single Outcome** / **Multi-Leg
Basket** toggle. Basket mode: dynamic add/remove leg rows (minimum 2), a live
JS-computed sizing preview (set price, per-leg dollar amount, edge %) that updates
on every keystroke without needing a server round-trip, and a "covers every
outcome" checkbox. Verified in a real running instance via headless-browser
click-through (toggle, add/remove rows, live math correctness), not just unit
tests.

## 8. What still remains (honest assessment)

These are **not** hidden logic bugs — they are the risks you said you accept, plus
known limitations that fail safe (produce nothing rather than something wrong):

1. **Strategy risk** — S1/S6 depend on the longshot-bias edge actually existing; the
   calibrator needs ≥ 5 settled S6 positions per bucket before it corrects the 0.60
   default. S4/S14 profit only if your curated rules/pairs are genuinely correct.
   The engine now executes exactly what the strategy computed — whether the strategy
   *thesis* makes money is on the strategy. S19 is a deliberate negative-EV tail bet
   by design, not a bug.
2. **Proxy strategies are proxies, not the real thing** — S13's sentiment score, S16's
   poll-drift signal, and S14's macro correlation are all derived from data this bot
   already has (order flow, price trends, intra-platform market pairs), not real
   news/social/polling/macro-economic feeds. Treat them accordingly; they're labeled
   as such in the UI.
3. **S18 Catalyst Straddle requires active management** — holding both legs to
   resolution is a small guaranteed loss by construction; the actual edge requires a
   human to notice the catalyst has landed and exit the winning leg early. There is
   no automated exit-timing logic for this.
4. **Fill-price approximation (live)** — fills are recorded at the order's limit
   price, which is conservative but can slightly understate profit if the book gave
   price improvement. Reading the trades endpoint for exact average fills would be
   more precise.
5. **Platform risk** — Polymarket/Gamma API changes, CLOB downtime, UMA resolution
   disputes, RPC outages. The bot fails safe (skips scans, keeps positions open,
   killswitch on partial fills) but cannot eliminate these.
6. **Single-process SQLite + in-memory market locks** — correct for one bot instance.
   Do **not** run two instances against the same database.

## 9. Go-live checklist (recommended order)

1. **Generate a fresh wallet** (the old key material was in git — see §1.8). Fund
   with USDC + a few POL for gas. Store the key via the UI (never in git/env files).
2. Set a real `API_SECRET` in `.env` — with the default secret, **auth is disabled**.
   Never expose the port publicly without it (and ideally bind behind a reverse
   proxy with TLS).
3. Your current `poly_yield.db` carries inflated paper history from the old sizing
   bug (balance ≈ $230k, huge open exposure that now correctly blocks new trades via
   the drawdown limit). Use **Reset Balance** to a realistic figure and let the old
   positions settle, or start a fresh DB file.
4. Run **paper mode ≥ 2 weeks / ≥ 10 settled trades** with the fixed engine. Watch
   `apy_delta` per settled position (predicted vs realized APY) — it is your
   truth-in-advertising metric.
5. Check `/api/poly-yield/wallet-health` weekly — `status` must stay `healthy`.
6. Go live with `portfolio.tradeable_limit` small (default $100) — live mode never
   risks more than this cap regardless of wallet balance. Keep daily-loss ($50) and
   consecutive-loss (3) circuit breakers on.
7. Configure Telegram/Discord alerts **before** live mode — the killswitch path
   assumes a human sees the critical alert.
8. Keep only strategies you understand on `auto` (S3 and S17 are the only ones whose
   payoff is structurally guaranteed *when its preconditions hold* — both are
   arbitrage baskets; S9 also defaults to `auto` but is a directional near-certain
   bet, not a guaranteed arb); leave the rest on `semi`/`manual`. Never put S13,
   S14, or S16 on `auto` without understanding they trade on proxies, not real
   external data.
