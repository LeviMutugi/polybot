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

`tests/` now has **40 passing tests** including new regression tests for: book
normalization, VWAP walking, fillability guard, APY bounds, Kelly unit sanitization,
allocation caps, staleness rejection/marking, manual-mode rejection, definitive-winner
settlement, full paper-trade wallet conservation, and exit price validation.

Run: `python -m pytest tests/ -q`

---

## 5. What still remains (honest assessment)

These are **not** hidden logic bugs — they are the risks you said you accept, plus
known limitations that fail safe (produce nothing rather than something wrong):

1. **Strategy risk** — S1/S6 depend on the longshot-bias edge actually existing; the
   calibrator needs ≥ 5 settled S6 positions per bucket before it corrects the 0.60
   default. S4 profits only if your curated rules are genuinely subset⊆superset.
   The engine now executes exactly what the strategy computed — whether the strategy
   *thesis* makes money is on the strategy.
2. **S5 sub-event arb is effectively dormant** — Polymarket slugs don't encode a
   parent/child hierarchy, so the grouping never matches. It stays manual-mode and
   produces no signals rather than wrong ones. Making it real requires the Gamma
   *events* API plus manual curation of parent↔sub relationships.
3. **S3 buy-all** requires true multi-outcome markets (≥ 3 outcomes in one Gamma
   market). Most modern Polymarket multi-outcome events are groups of *binary*
   markets (neg-risk), which S3 intentionally does not touch — supporting neg-risk
   baskets safely would be a meaningful new feature, not a patch.
4. **S9–S19 are stubs** — they scan nothing and return nothing. Their toggles exist
   in the UI but do nothing except waste your attention; consider hiding them until
   implemented.
5. **Fill-price approximation (live)** — fills are recorded at the order's limit
   price, which is conservative but can slightly understate profit if the book gave
   price improvement. Reading the trades endpoint for exact average fills would be
   more precise.
6. **Platform risk** — Polymarket/Gamma API changes, CLOB downtime, UMA resolution
   disputes, RPC outages. The bot fails safe (skips scans, keeps positions open,
   killswitch on partial fills) but cannot eliminate these.
7. **Single-process SQLite + in-memory market locks** — correct for one bot instance.
   Do **not** run two instances against the same database.

## 6. Go-live checklist (recommended order)

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
8. Keep only strategies you understand on `auto` (S3 is the only one whose payoff is
   structurally guaranteed *when its preconditions hold*); leave the rest on `semi`.
