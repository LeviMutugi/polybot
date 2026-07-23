# PolyYield — Testing, Validation & Hosting Guide

This is a real-money trading bot (in live mode). Read the whole "Validation"
section before you ever set `poly_yield.active_mode` to `live` or fund a real
wallet — skipping it is how the bugs in `PRODUCTION_READINESS.md` §1 happened
the first time.

---

## 1. Local Testing

### 1.1 Setup

```bash
git clone <your-repo-url> polybot
cd polybot
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
```

Open `.env` and set at minimum:
- `API_SECRET` — any long random string (e.g. `openssl rand -hex 32`). With the
  default value, **auth is disabled** — fine for local testing, never for anything
  reachable off `localhost`.

Leave `ALCHEMY_API_KEY` / `POLYGON_RPC_URL` / Telegram / Discord blank for now —
the app runs fine in paper mode without them (it falls back to a public RPC and
just skips alert delivery).

### 1.2 Run the automated test suite

```bash
python -m pytest tests/ -q
```

Expect `80 passed`. If you see async tests reported as errors/skipped instead of
running, `pytest-asyncio` isn't installed — re-run
`pip install -r requirements-dev.txt`.

Useful variants:
```bash
python -m pytest tests/ -v                      # verbose, one line per test
python -m pytest tests/test_new_strategies.py   # just the S9-S19/S5/S3 tests
python -m pytest tests/ -k s17                   # tests matching a keyword
```

### 1.3 Run the app locally

```bash
python main.py
```

or, for auto-reload while you edit:
```bash
uvicorn main:app --reload --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`. You should see the dashboard, with the wallet
balance defaulting to the paper balance and strategies scanning in the
background (watch the terminal for `[PolyYieldEngine] Running Scan #N...`).

Sanity-check the process compiles and the strategy registry loads cleanly
any time you change strategy code:
```bash
python -m py_compile main.py config.py db/*.py services/*.py strategies/*.py
python -c "from strategies.registry import load_strategies, get_all_strategies as g; load_strategies(); print(len(g()), 'strategies loaded')"
```

---

## 2. Validating (before you trust it, before you go live)

This is a checklist, not a formality — every item maps to a real bug this app
has had before (see `PRODUCTION_READINESS.md`).

1. **Functional smoke test in paper mode**
   - Toggle a few strategies on in the Strategy Control Panel (⚙️ Bot Manager).
   - Wait for a scan interval (`poly_yield.scan_interval_s`, default 120s) and
     confirm the Opportunities table populates.
   - Click **Execute** on a `semi`-mode opportunity (or use the Manual Trade
     panel) and confirm a position appears in the Positions table.
   - Open **Trade History & Audit** → confirm the trade shows up, and that
     `/api/poly-yield/wallet-health` (or the Wallet Ledger panel) reports
     `"valid": true` — this is the conservation-of-money check; if it's ever
     `false`, stop and investigate before going further.
   - Use **Reset Balance** / **Add Paper Funds** and confirm the balance and
     ledger update consistently.

2. **Run paper mode for real** — the audit's own recommendation:
   **≥ 2 weeks and ≥ 10 settled trades** before considering live. Watch
   `apy_delta` per settled position (predicted vs. realized APY) in the
   Accounting tab — it should hover near zero, not be consistently negative.

3. **Review exec_mode per strategy before touching `auto`.** Only S3 (Buy-All)
   and S17 (Liquidity Sniper) are structurally guaranteed arbitrage when their
   preconditions hold. S9 defaults to `auto` but is a directional bet, not a
   guaranteed arb. Never set S13 (Sentiment), S14 (Macro Correlation), or S16
   (Poll Drift) to `auto` — they trade on proxies, not real external data (see
   their in-UI notes).

4. **Secrets sanity check**
   - `git status` should never show `.env`, `.secret`, or `poly_yield.db` as
     trackable — confirm `.gitignore` still covers them.
   - Confirm `API_SECRET` is a real random value before exposing the port
     beyond `127.0.0.1`.
   - If you're going live, generate a **fresh wallet** for the bot — never
     reuse a wallet holding other funds — and store the key only via the
     dashboard's Key Vault (never in `.env`, never in git).

5. **Alerts.** Configure Telegram and/or Discord in `.env` *before* live mode,
   then trigger a paper trade and confirm the alert actually arrives — the
   killswitch path assumes a human sees the critical alert.

6. **Circuit breakers.** Confirm `portfolio.tradeable_limit` (default $100),
   daily-loss limit (default $50), and consecutive-loss limit (default 3) are
   set to values you're comfortable with before flipping to live — live mode
   never risks more than `tradeable_limit` regardless of wallet balance.

Only after all of the above: switch `poly_yield.active_mode` to `live` with a
small `tradeable_limit`, and keep watching `wallet-health` and the alerts
channel closely for the first few days.

---

## 3. Hosting

### What this app actually needs from a host

- A **long-running process** (not a request/response serverless function) —
  `main.py` starts a background asyncio scan loop and a settlement loop on
  startup that must keep running continuously.
- **Persistent disk** — the SQLite database (`poly_yield.db`) and the Fernet
  key (`.secret`) used to encrypt your wallet key in the Key Vault must
  survive restarts/redeploys, or you lose your trade history and stored keys.
- **Environment variables / secrets storage** for `API_SECRET`,
  `POLYGON_RPC_URL`, and optionally Telegram/Discord tokens.
- Outbound HTTPS (to Polymarket's Gamma/CLOB APIs, your Polygon RPC provider,
  Coinbase's spot price API, and Telegram/Discord if configured).

That combination rules out pure serverless (Vercel/Netlify functions, AWS
Lambda) and rules out free tiers that spin your app down when idle (that would
silently stop the scan loop). A small persistent VM or a PaaS with a
persistent volume are the right shapes. A `Dockerfile` is included in this
repo so any Docker-friendly host works the same way.

| Option | Cost | Sleeps? | Persistent disk? | Effort |
|---|---|---|---|---|
| Oracle Cloud "Always Free" VM | **$0 forever** | No | Yes (full disk) | Medium (you manage the VM) |
| Google Cloud `e2-micro` Free Tier | **$0 forever*** | No | Yes | Medium |
| Fly.io free allowance | $0 up to small usage | No | Yes (volumes) | Low |
| Render.com free web service | $0 | **Yes, sleeps after 15 min** | **No** | Low — but not fit for this app |
| DigitalOcean Droplet | ~$4-6/mo | No | Yes | Medium |
| Railway.app | pay-as-you-go, ~$5+/mo | No | Yes (volumes) | Low |
| Render.com paid web service | ~$7/mo + disk | No | Yes (paid add-on) | Low |
| Fly.io paid | ~$5+/mo | No | Yes | Low |

\* Google's "always free" e2-micro is restricted to specific US regions and one
instance per billing account.

**Render's free tier is included above only for completeness — do not use it
for this app.** Sleeping kills the scan loop and it has no free persistent
disk, so your database resets on every redeploy.

---

### 3.1 Free option: Oracle Cloud "Always Free" VM (recommended free path)

Genuinely free forever, no credit-card-timeout gotcha like AWS's 12-month
free tier, and gives you a full VPS — the best fit for a 24/7 background bot.

1. **Sign up** at [oracle.com/cloud/free](https://www.oracle.com/cloud/free/)
   and create a Compute instance:
   - Shape: an "Always Free" eligible shape (e.g. `VM.Standard.A1.Flex`, ARM,
     up to 4 OCPU / 24GB RAM free, or the free x86 micro shape).
   - Image: Ubuntu 22.04/24.04 LTS.
   - Add your SSH public key during creation.
2. **Open ports** in the instance's attached Security List / Network Security
   Group: allow inbound `22` (SSH), `80` and `443` (HTTP/HTTPS). Do **not**
   open `8080` publicly — the app should only be reached through a reverse
   proxy (see below).
3. **SSH in and install dependencies:**
   ```bash
   ssh ubuntu@<your-instance-ip>
   sudo apt update && sudo apt install -y python3.11 python3.11-venv git nginx
   curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
   sudo apt install -y nodejs
   sudo npm install -g pm2
   ```
4. **Clone and set up the app:**
   ```bash
   git clone <your-repo-url> ~/polybot
   cd ~/polybot
   python3.11 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   nano .env   # fill in a real API_SECRET, RPC URL, alert webhooks
   ```
5. **Run it under PM2** (auto-restarts on crash and on VM reboot):
   ```bash
   pm2 start main.py --name polyyield --interpreter ~/polybot/venv/bin/python3
   pm2 save
   pm2 startup   # follow the printed command to enable boot-time start
   ```
6. **Put nginx in front with TLS.** Point a domain's DNS `A` record at the
   instance IP first, then:
   ```bash
   sudo apt install -y certbot python3-certbot-nginx
   sudo nano /etc/nginx/sites-available/polyyield
   ```
   Minimal nginx config (proxies to the app, which should stay on `127.0.0.1`):
   ```nginx
   server {
       listen 80;
       server_name your-domain.com;
       location / {
           proxy_pass http://127.0.0.1:8080;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_http_version 1.1;
           proxy_set_header Upgrade $http_upgrade;   # needed for the /ws websocket
           proxy_set_header Connection "upgrade";
       }
   }
   ```
   ```bash
   sudo ln -s /etc/nginx/sites-available/polyyield /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot --nginx -d your-domain.com   # issues + auto-renews a free TLS cert
   ```
   With nginx in front and `HOST=127.0.0.1` in `.env`, the app itself is never
   directly reachable from the internet — only through nginx's TLS endpoint.
7. **Back up** `~/polybot/poly_yield.db` and `~/polybot/.secret` together,
   off-host, on a schedule (e.g. a nightly `cron` job copying them to
   encrypted cloud storage). If you lose `.secret`, any wallet key stored in
   `poly_yield.db`'s Key Vault becomes permanently undecryptable.

### 3.2 Free option: Fly.io (Docker-based, less setup)

Uses the `Dockerfile` already in this repo.

```bash
curl -L https://fly.io/install.sh | sh
fly auth signup     # or: fly auth login
cd polybot
fly launch --no-deploy     # generates fly.toml; choose a region near you, say no to a Postgres db
fly volumes create polyyield_data --size 1     # 1GB persistent volume
```
Edit the generated `fly.toml` to mount the volume and expose the right port:
```toml
[mounts]
  source = "polyyield_data"
  destination = "/app/data"

[[services]]
  internal_port = 8080
  protocol = "tcp"
  [[services.ports]]
    port = 443
    handlers = ["tls", "http"]
```
Set secrets (never in `fly.toml`, which can end up in git):
```bash
fly secrets set API_SECRET=$(openssl rand -hex 32) \
                 POLYGON_RPC_URL=https://your-alchemy-url \
                 TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... DISCORD_WEBHOOK_URL=...
fly deploy
```
Fly terminates TLS for you automatically on the assigned `*.fly.dev` domain
(or your own domain via `fly certs add`).

---

### 3.3 Paid option: Render.com or Railway.app (easiest — no server to manage)

Both build directly from this repo's `Dockerfile` and both support persistent
volumes on paid plans (Render's free plan does **not** — see the warning
above).

**Render.com:**
1. New → Web Service → connect this repo. Render detects the `Dockerfile`
   automatically.
2. Plan: any paid tier (Starter, ~$7/mo) so the service doesn't sleep.
3. Add a **Persistent Disk** (Render dashboard → your service → Disks), mount
   path `/app/data`, at least 1GB.
4. Environment → add `API_SECRET`, `POLYGON_RPC_URL`, `SQLITE_PATH=/app/data/poly_yield.db`,
   and any alert tokens as secret environment variables (never commit them).
5. Deploy. Render provisions HTTPS on its `*.onrender.com` domain automatically
   (or attach your own domain in Settings → Custom Domains).

**Railway.app:**
1. New Project → Deploy from GitHub repo → Railway detects the `Dockerfile`.
2. Add a Volume, mount path `/app/data`.
3. Variables tab → add the same environment variables as above.
4. Railway assigns a public HTTPS domain automatically; add a custom domain if
   you want one.

### 3.4 Paid option: a small VPS (DigitalOcean / Linode / Hetzner)

Identical steps to the Oracle Cloud walkthrough in §3.1 — these are the same
kind of plain Ubuntu VPS, just paid ($4-6/mo for a small droplet is plenty for
this app). Use the same PM2 + nginx + certbot setup.

---

## 4. Whichever host you pick

- **Never expose port 8080 (or whatever `PORT` is) directly to the internet.**
  Terminate TLS at a reverse proxy or at the platform's edge, and keep the app
  itself reachable only on localhost/private networking behind it.
- **Set a real, random `API_SECRET`** before the app is reachable from
  anywhere but your own machine — with the default value, every state-changing
  endpoint (execute trades, change config, withdraw... ) is unauthenticated.
- **Back up `poly_yield.db` and `.secret` together**, encrypted, off the host —
  never to another git repo.
- **Start in paper mode on whatever host you choose**, and only flip to
  `live` after the full checklist in §2, with a small `tradeable_limit`.
