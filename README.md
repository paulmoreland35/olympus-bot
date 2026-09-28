# 🚀 Olympus → TradeLocker Auto-Trader

Automatically executes trades on your **TradeLocker** broker account whenever your **TradingView** scanner fires an alert — with proper risk management (2% per trade), stop loss, and take profit set automatically.

## How It Works

```
TradingView Alert (Olympus fires) → This Bot → TradeLocker (trade placed with SL + TP)
```

1. Your TradingView scanner fires an alert
2. The alert is sent to this bot via webhook
3. The bot reads the direction, entry, SL, and TP from the alert
4. Calculates lot size based on 2% account risk
5. Places the trade on TradeLocker instantly with SL and TP attached

---

## ⚡ One-Click Deploy

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template/deploy?template=https://github.com/paulmoreland35/olympus-bot)

Click the button above, fill in your details, and you're live in 2 minutes.

---

## 🔧 Manual Setup (Step by Step)

### Step 1 — Fork this repo
Click **Fork** at the top right of this GitHub page.

### Step 2 — Deploy to Railway
1. Go to [railway.app](https://railway.app) and sign up with GitHub
2. Click **New Project** → **Deploy from GitHub repo**
3. Select your forked `olympus-bot` repo
4. Click **Deploy**

### Step 3 — Set your environment variables
In Railway → your project → **Variables** tab, add:

| Variable | Description | Example |
|----------|-------------|---------|
| `TL_BASE_URL` | TradeLocker API URL | `https://live.tradelocker.com/backend-api` |
| `TL_EMAIL` | Your TradeLocker email | `you@email.com` |
| `TL_PASSWORD` | Your TradeLocker password | `yourpassword` |
| `TL_SERVER` | Your broker server name | `LIVVFX` |
| `WEBHOOK_SECRET` | Any random secret you choose | `my_secret_2024` |
| `RISK_PCT` | Risk per trade (decimal) | `0.02` (= 2%) |
| `DEFAULT_SL_PCT` | Fallback SL if none in alert | `0.01` (= 1%) |

> **Finding your TL_BASE_URL and TL_SERVER:**
> These are shown on your TradeLocker login screen.
> Common URL: `https://live.tradelocker.com/backend-api`
> Your server name is shown next to your broker name at login.

### Step 4 — Get your webhook URL
In Railway → **Settings** → **Generate Domain**
Your webhook URL will be:
```
https://YOUR-RAILWAY-DOMAIN.up.railway.app/webhook
```

### Step 5 — Set up TradingView alerts
For each chart/pair you want automated:
1. Open the chart with your scanner running
2. Create a new alert → condition = **Any alert() function call**
3. Set expiry to **Open-ended**
4. Paste your webhook URL
5. Set the message to:
```json
{"secret":"YOUR_WEBHOOK_SECRET","raw":"{{alert.message}}"}
```
Replace `YOUR_WEBHOOK_SECRET` with the same value you set in Railway.

### Step 6 — Test it
Visit your Railway URL in a browser. You should see:
```json
{"status": "Olympus bot is running ✓"}
```

---

## 📊 Supported Alert Formats

The bot parses the Olympus scanner's native alert format:
```
🚀 BUY GBPUSD | 1 | 01:19 | ENTRY: 1.359 | SL: 1.355 | TP1: 1.363 | TP2: 1.366 | TP3: 1.379 | R:R 1:1
```

It automatically extracts:
- **Direction** — BUY or SELL
- **Symbol** — any forex pair, gold, indices
- **Entry price**
- **Stop Loss**
- **Take Profit 1**

## 👥 Trading on Multiple Accounts

To run the same TradingView signals on more than one TradeLocker account
(e.g. a partner's account, or a second funded account of your own):

1. **Deploy this repo again as a separate Railway service** for each
   additional account, with its own `TL_EMAIL` / `TL_PASSWORD` / `TL_SERVER`
   / `WEBHOOK_SECRET` variables (Step 2–3 above). Each deployment gets its
   own webhook URL, e.g. `https://account2-bot.up.railway.app/webhook`.
2. On your **main** bot (the one TradingView actually sends alerts to), add:

   | Variable | Description |
   |----------|-------------|
   | `FORWARD_TO_URL_2` | Webhook URL of account 2's deployment |
   | `FORWARD_TO_SECRET_2` | That deployment's `WEBHOOK_SECRET` |
   | `FORWARD_TO_LABEL_2` | Optional name shown in logs (e.g. `Derrick`) |
   | `FORWARD_TO_URL_3` | Webhook URL of account 3's deployment |
   | `FORWARD_TO_SECRET_3` | That deployment's `WEBHOOK_SECRET` |
   | `FORWARD_TO_LABEL_3` | Optional name shown in logs |

   (`FORWARD_TO_URL` / `FORWARD_TO_SECRET`, no number, still works for the
   first partner account — this just adds `_2`, `_3`, etc. for more.)

Every accepted alert is now relayed to all configured accounts, each sized
by its **own** balance and risk settings — a slow or failed partner never
blocks your own trade. Add each account's `/report` URL to `REPORT_SOURCES`
on whichever bot has `RESEND_API_KEY` set to include it in the daily email.

## 🔄 Symbol Mapping (for indices)
TradeLocker uses different symbol names for indices:

| TradingView | TradeLocker |
|-------------|-------------|
| NAS100 | NDXUSD |
| US30 | DJIUSD |

Add your own mappings in `parser.py` → `SYMBOL_MAP`.

## 💰 Risk Calculation
- Default: **2% of account balance per trade**
- Lot size = `(balance × risk%) ÷ (SL distance × contract size)`
- Contract sizes: Forex = 100,000 | Gold = 100 | Indices = 10

---

## 🔒 Security
- All credentials stored as environment variables — never in code
- Webhook secret prevents unauthorized trade requests
- `.env` file is gitignored — never committed

---

## 🎯 Olympus Sniper FX Scanner (TradingView, Pine Script v6)

`olympus_sniper_fx.pine` is a chart indicator that calls forex trades. It is tuned for
GBPUSD, EURUSD, GBPJPY, USDJPY, GBPCAD and NZDCAD, and runs on any pair.

**Install:** TradingView → Pine Editor → paste the file → *Add to chart*.

**What you see**
- ▲ BUY / ▼ SELL labels with a grade (A+ / A / B). Hover a label to see the setup and its levels.
- **White** line = entry, **red** line = stop loss, **green** lines = TP1 / TP2 / TP3.
  A TP line turns solid once it is hit, and every closed trade is stamped ✅ WIN / ❌ LOSS with its R result.
- Dashboard: market bias, the live trade, win rate and net R on this chart, and win rate for 5 timeframes.
- Pair scanner: 12 pairs showing live BUY/SELL, trend, win rate and trade age.

**How a trade is called:** a trigger has to fire (Break & Retest, Supply & Demand, SMC Order Block,
Fair Value Gap, ICT liquidity sweep + displacement, or a trend-filtered 9/21 EMA cross). Then the
confluence score has to reach the mode's minimum: *Sniper* 5, *Balanced* 4, *Aggressive* 3.
Confluence comes from the 50/200 EMA trend, market structure (last BOS), ICT kill zones,
premium/discount and RSI. The chart, the timeframe table and the scanner all use the same engine,
so their numbers match.

**Trade rules used for the stats:** entry on the signal candle's close. The SL sits beyond the recent swing,
kept between 0.8 and 3 ATR. TP1/2/3 are at 1R/2R/3R, with 1/3 of the position closed at each.
After TP1 the SL moves to entry, and after TP2 it moves to TP1. If a candle touches both SL and TP, the stop counts first.
A trade is a win when its total R is above zero.

**Auto-trading:** create one alert with *Condition: Olympus Sniper FX → Any alert() function call* and point it
at the webhook. The entry message uses the parser's format, and the bot tags these trades as source `Olympus Sniper`:
```
Olympus Sniper 🚀 BUY GBPUSD | 15 | ENTRY: 1.27012 | SL: 1.26850 | TP1: 1.27174 | TP2: 1.27336 | TP3: 1.27498 | R:R 1:3 | Grade A+ | Break & Retest + Order Block
```
The optional TP/SL-hit alerts never contain BUY or SELL, so the bot will not trade on them.

> Past signal statistics are simulated on the history TradingView has loaded, and they are not a guarantee of future results.
> The timeframe table and the scanner run 17 extra data requests. If the script is slow, lower the number of chart bars or use fewer pairs.

---

## 📁 File Structure
```
olympus-bot/
├── main.py              # Webhook server
├── tradelocker_client.py # TradeLocker API client
├── risk.py              # Position size calculator
├── parser.py            # Alert message parser
├── strategy.pine        # Olympus SMC v3 indicator (gold / indices)
├── olympus_sniper_fx.pine # Sniper FX scanner (forex, Pine v6)
├── requirements.txt
├── Procfile
└── railway.json
```

---

## ⚠️ Disclaimer
This bot places real trades with real money. Test on a demo account first.
The authors are not responsible for trading losses.
