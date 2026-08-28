"""
Olympus → TradeLocker Webhook Server
-------------------------------------
Receives TradingView alerts and places trades on TradeLocker with
automatic 2% risk-based position sizing.

TradingView alert message format (just paste {{alert.message}}):
{
  "secret": "olympus_paul_2024",
  "raw":    "{{alert.message}}"
}

Olympus sends everything needed in its message:
  🚀 BUY GBPUSD | 1 | 01:19 | ENTRY: 1.359 | SL: 1.355 | TP1: 1.363 | TP2: 1.366 | TP3: 1.379 | R:R 1:1
"""

import logging
import os
import time
import threading

from flask import Flask, request, jsonify
from dotenv import load_dotenv

from tradelocker_client import TradeLockerClient
from risk import calculate_lots, calculate_default_sl
from parser import parse_olympus_message, remap_symbol
from trailing import TrailingStopManager
from trade_log import TradeLog
from scanner import start_scanner_thread

# ------------------------------------------------------------------
# Setup
# ------------------------------------------------------------------

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ------------------------------------------------------------------
# Config from .env
# ------------------------------------------------------------------

TL_BASE_URL    = os.getenv("TL_BASE_URL",      "https://live.tradelocker.com/backend-api")
TL_EMAIL       = os.getenv("TL_EMAIL",          "")
TL_PASSWORD    = os.getenv("TL_PASSWORD",       "")
TL_SERVER      = os.getenv("TL_SERVER_LIVE") or os.getenv("TL_SERVER", "LIVVFX")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET",    "")
# Mirror/forward: if set, every accepted webhook signal is relayed to these
# partner bots' /webhook so each trades the same alerts (sized to its own
# account). FORWARD_TO_URL/FORWARD_TO_SECRET is partner #1 for backward
# compatibility; add more accounts with FORWARD_TO_URL_2/FORWARD_TO_SECRET_2,
# FORWARD_TO_URL_3/FORWARD_TO_SECRET_3, etc. — each is a separate deployment
# of this same bot pointed at a different TradeLocker account.
FORWARD_TO_URL    = os.getenv("FORWARD_TO_URL",    "")
FORWARD_TO_SECRET = os.getenv("FORWARD_TO_SECRET", "")


def _load_forward_targets():
    targets = []
    if FORWARD_TO_URL:
        targets.append((
            os.getenv("FORWARD_TO_LABEL", "partner"),
            FORWARD_TO_URL,
            FORWARD_TO_SECRET,
        ))
    n = 2
    while True:
        url = os.getenv(f"FORWARD_TO_URL_{n}", "")
        if not url:
            break
        targets.append((
            os.getenv(f"FORWARD_TO_LABEL_{n}", f"partner{n}"),
            url,
            os.getenv(f"FORWARD_TO_SECRET_{n}", ""),
        ))
        n += 1
    return targets


FORWARD_TARGETS = _load_forward_targets()
if FORWARD_TARGETS:
    logger.info(
        f"[Mirror] {len(FORWARD_TARGETS)} partner account(s) configured: "
        + ", ".join(label for label, _url, _secret in FORWARD_TARGETS)
    )
RISK_PCT       = float(os.getenv("RISK_PCT",    "0.02"))
DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", "0.01"))
# When an alert has no take-profit, set TP at this multiple of the SL distance
# (e.g. 1.5 = risk:reward of 1.5:1). Tunable via env without a code change.
DEFAULT_TP_RR  = float(os.getenv("DEFAULT_TP_RR", "1.5"))

# Daily drawdown limit — bot stops taking new trades for the rest of the day
# once account balance drops this % below the day's opening balance.
# e.g. 0.07 = halt if down 7% on the day.
MAX_DAILY_DRAWDOWN_PCT = float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "0.07"))
MAX_OPEN_TRADES        = int(os.getenv("MAX_OPEN_TRADES", "3"))

# How often the trailing loop polls open positions (seconds).
# 10s is a good balance — responsive without hammering the API.
TRAILING_POLL_SEC = int(os.getenv("TRAILING_POLL_SEC", "10"))

# Master pause switch — set TRADING_PAUSED=true to stop new trades from
# both the webhook and the autonomous scanner without touching TradingView
# alerts or redeploying. Existing open positions still get managed by the
# trailing loop (SL/TP still apply) — this only blocks new entries.
TRADING_PAUSED = os.getenv("TRADING_PAUSED", "false").strip().lower() == "true"
if TRADING_PAUSED:
    logger.warning("[Pause] TRADING_PAUSED is set — no new trades will be placed.")

# ------------------------------------------------------------------
# Trailing stop manager + trade log (singletons)
# ------------------------------------------------------------------

trailing_manager = TrailingStopManager()
trade_log        = TradeLog()

# Tracks the most recent inbound TradingView webhook so we can confirm alerts
# are actually reaching the bot (read by /report and the daily email).
WEBHOOK_STATUS = {
    "last_received_utc": None,   # ISO timestamp of the last webhook hit
    "last_summary":      None,   # e.g. "BUY NAS100" or "rejected: bad secret"
    "total_received":    0,      # count since this deploy
    "last_accepted_utc": None,   # last webhook that passed auth + parsed OK
}


def _forward_signal(action, ticker, entry, sl, tp1, dry_run):
    """Relay an accepted signal to every partner bot (mirror). Best-effort —
    each target is independent, so one down/slow partner never blocks the
    others or this bot's own trade."""
    if not FORWARD_TARGETS:
        return
    import requests as _rq
    for label, url, secret in FORWARD_TARGETS:
        try:
            payload = {
                "secret": secret,
                "action": action,
                "ticker": ticker,
                "price":  entry,
            }
            if sl and sl > 0:
                payload["sl"] = sl
            if tp1 and tp1 > 0:
                payload["tp"] = tp1
            if dry_run:
                payload["dry_run"] = True
            # Short timeout so a slow/down partner never blocks our own trade.
            r = _rq.post(url, json=payload, timeout=8)
            logger.info(f"[Mirror] Forwarded {action.upper()} {ticker} -> {label} ({url}) "
                        f"(status {r.status_code})")
        except Exception as e:
            logger.warning(f"[Mirror] Forward to {label} ({url}) failed (own trade unaffected): {e}")


def _record_webhook(summary: str, accepted: bool = False, count: bool = True):
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc).isoformat()
    WEBHOOK_STATUS["last_received_utc"] = now
    WEBHOOK_STATUS["last_summary"]      = summary
    if count:
        WEBHOOK_STATUS["total_received"] += 1
    if accepted:
        WEBHOOK_STATUS["last_accepted_utc"] = now

# Start autonomous scanner at module load (runs under gunicorn, not just __main__)
if os.getenv("TWELVE_DATA_API_KEY"):
    start_scanner_thread()

# Start the daily report emailer (only on the bot that has RESEND_API_KEY set)
from daily_report import start_report_thread, send_report_now
if os.getenv("RESEND_API_KEY"):
    start_report_thread()

# ------------------------------------------------------------------
# Daily drawdown tracker
# ------------------------------------------------------------------

from datetime import date as _date

_drawdown_lock       = threading.Lock()
_day_open_balance    = 0.0   # balance recorded at first trade of the day
_day_open_date       = None  # the calendar date that balance belongs to


def _update_day_open(balance: float):
    """Record today's opening balance on the first call each day."""
    global _day_open_balance, _day_open_date
    today = _date.today()
    with _drawdown_lock:
        if _day_open_date != today:
            _day_open_balance = balance
            _day_open_date    = today
            logger.info(
                f"[Drawdown] New trading day {today} — "
                f"day-open balance: ${balance:,.2f}"
            )


def _is_halted(current_balance: float) -> tuple[bool, str]:
    """
    Returns (halted, reason_string).
    Halted = True when today's loss has hit MAX_DAILY_DRAWDOWN_PCT.
    """
    with _drawdown_lock:
        open_bal = _day_open_balance

    if open_bal <= 0:
        return False, ""

    loss_pct = (open_bal - current_balance) / open_bal
    limit    = MAX_DAILY_DRAWDOWN_PCT

    if loss_pct >= limit:
        reason = (
            f"Daily drawdown limit reached: account is down "
            f"{loss_pct*100:.1f}% today "
            f"(limit {limit*100:.0f}%). "
            f"Day-open: ${open_bal:,.2f} | Now: ${current_balance:,.2f}. "
            f"No new trades until tomorrow."
        )
        return True, reason

    return False, ""

# ------------------------------------------------------------------
# Background trailing stop loop
# ------------------------------------------------------------------

def _trailing_loop():
    """
    Daemon thread that wakes up every TRAILING_POLL_SEC seconds,
    fetches all open positions from TradeLocker, and:
      - Applies breakeven / trailing SL moves
      - Detects closed positions and logs the exit to the trade log
    Works on ALL open positions — bot-placed and manual alike.
    """
    client       = None
    prev_pos_map: dict[str, dict] = {}   # position_id → last known position data

    while True:
        try:
            # (Re-)authenticate when client is missing
            if client is None:
                if not TL_EMAIL or not TL_PASSWORD:
                    time.sleep(TRAILING_POLL_SEC)
                    continue
                client = TradeLockerClient(
                    base_url=TL_BASE_URL,
                    email=TL_EMAIL,
                    password=TL_PASSWORD,
                    server=TL_SERVER,
                )
                client.authenticate()
                logger.info("[Trailing] Loop client authenticated.")

            # Anchor today's opening balance once per day (for daily P&L in the
            # report). Only hits the balance API when the calendar day flips.
            if _day_open_date != _date.today():
                try:
                    _update_day_open(client.get_balance())
                except Exception as anchor_err:
                    logger.warning(f"[Trailing] Could not anchor day-open: {anchor_err}")

            positions    = client.get_open_positions()
            current_ids  = {p["id"] for p in positions}

            # ---- Detect closed positions ----
            for pid, last_pos in prev_pos_map.items():
                if pid not in current_ids:
                    # Position has closed since last poll
                    entry      = last_pos.get("openPrice", 0)
                    last_pnl   = last_pos.get("unrealisedPnl", 0)
                    side       = last_pos.get("side", "")
                    ticker     = last_pos.get("name", "?")

                    # Approximate exit price from last known P&L
                    from risk import _contract_size
                    qty      = last_pos.get("qty", 0)
                    contract = _contract_size(ticker)
                    if entry and qty and contract:
                        price_move  = last_pnl / (qty * contract) if (qty * contract) else 0
                        exit_price  = (entry + price_move) if side == "buy" else (entry - price_move)
                    else:
                        exit_price = entry

                    # Determine exit reason
                    sl = last_pos.get("stopLoss", 0)
                    tp = last_pos.get("takeProfit", 0)
                    if sl and abs(exit_price - sl) / sl < 0.001:
                        reason = "sl"
                    elif tp and abs(exit_price - tp) / tp < 0.001:
                        reason = "tp1"
                    else:
                        reason = "manual"

                    trade_log.log_exit(pid, exit_price, last_pnl, reason)

            # Update previous position map
            prev_pos_map = {p["id"]: p for p in positions}

            if not positions:
                logger.debug("[Trailing] No open positions.")
            else:
                updates = trailing_manager.process(positions)
                for pos_id, new_sl in updates:
                    try:
                        client.modify_position_sl(pos_id, new_sl)
                    except Exception as modify_err:
                        logger.error(
                            f"[Trailing] Failed to update SL for position "
                            f"{pos_id} → {new_sl}: {modify_err}"
                        )

        except Exception as e:
            err = str(e)
            if "401" in err or "Unauthorized" in err.lower():
                logger.warning("[Trailing] Auth expired — re-authenticating.")
                client = None
            elif "429" in err or "Too Many Requests" in err.lower():
                logger.warning("[Trailing] Rate limited — backing off 60s.")
                time.sleep(60)
                continue
            elif any(x in err for x in ("502", "503", "Bad Gateway", "Service Unavailable")):
                logger.warning(f"[Trailing] Broker server error — backing off 30s. ({err[:80]})")
                time.sleep(30)
                continue
            else:
                logger.error(f"[Trailing] Unexpected error — reinitialising client: {e}")
                client = None

        time.sleep(TRAILING_POLL_SEC)


_trailing_thread = threading.Thread(
    target=_trailing_loop, daemon=True, name="trailing-loop"
)
_trailing_thread.start()
logger.info(f"[Trailing] Background loop started (polling every {TRAILING_POLL_SEC}s).")

# ------------------------------------------------------------------
# Health check
# ------------------------------------------------------------------

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "Olympus bot is running ✓"}), 200

# ------------------------------------------------------------------
# Trailing stop status
# ------------------------------------------------------------------

@app.route("/trailing", methods=["GET"])
def trailing_status():
    """Shows all positions currently being trailed."""
    return jsonify(trailing_manager.status()), 200

# ------------------------------------------------------------------
# Daily risk status
# ------------------------------------------------------------------

@app.route("/risk", methods=["GET"])
def risk_status():
    """Shows today's drawdown vs the 7% limit."""
    try:
        client = TradeLockerClient(
            base_url=TL_BASE_URL, email=TL_EMAIL,
            password=TL_PASSWORD, server=TL_SERVER,
        )
        client.authenticate()
        current_balance = client.get_balance()
    except Exception:
        current_balance = 0.0

    with _drawdown_lock:
        open_bal  = _day_open_balance
        open_day  = str(_day_open_date)

    loss_pct  = ((open_bal - current_balance) / open_bal * 100) if open_bal > 0 else 0
    halted, _ = _is_halted(current_balance)

    return jsonify({
        "trading_day":        open_day,
        "day_open_balance":   round(open_bal, 2),
        "current_balance":    round(current_balance, 2),
        "loss_today_pct":     round(loss_pct, 2),
        "daily_limit_pct":    round(MAX_DAILY_DRAWDOWN_PCT * 100, 1),
        "trading_halted":     halted,
        "status":             "🔴 HALTED — limit hit" if halted else "🟢 Trading allowed",
    }), 200

# ------------------------------------------------------------------
# Trade log endpoints
# ------------------------------------------------------------------

@app.route("/trades", methods=["GET"])
def trades_summary():
    """Overall performance summary + recent closed trades."""
    return jsonify({
        "summary": trade_log.summary(),
        "recent":  trade_log.get_recent(20),
    }), 200

@app.route("/trades/open", methods=["GET"])
def trades_open():
    """All currently open (not yet exited) trades."""
    return jsonify(trade_log.get_open_trades()), 200

# ------------------------------------------------------------------
# Daily report endpoint — machine-readable snapshot for the report agent
# ------------------------------------------------------------------

@app.route("/report", methods=["GET"])
def report():
    """
    Returns a JSON snapshot of account performance for the daily report:
      account balance/equity, day-open balance + drawdown, open positions,
      closed-trade history (TP/SL outcomes), and scanner health.
    Pulls live data straight from TradeLocker so it survives redeploys.
    """
    out = {"account": os.getenv("TL_ACC_NUM", "default"), "server": TL_SERVER}

    # --- Broker data ---
    try:
        client = TradeLockerClient(
            base_url=TL_BASE_URL, email=TL_EMAIL,
            password=TL_PASSWORD, server=TL_SERVER,
        )
        client.authenticate()
        balance = client.get_balance()
        out["balance"]    = round(balance, 2)
        out["account_id"] = client.account_id

        try:
            open_positions = client.get_open_positions()
        except Exception as e:
            open_positions = []
            out["open_positions_error"] = str(e)
        out["open_positions"] = [
            {
                "ticker": p.get("name"), "side": p.get("side"),
                "qty": p.get("qty"), "entry": p.get("openPrice"),
                "sl": p.get("stopLoss"), "tp": p.get("takeProfit"),
                "unrealised_pnl": p.get("unrealisedPnl"),
            }
            for p in open_positions
        ]

        # Closed trades window. Default 24h (daily scope); ?days=N widens it
        # for per-symbol performance analysis. Win/loss is by price direction.
        try:
            days = max(1, min(int(request.args.get("days", "1")), 90))
        except Exception:
            days = 1
        since_ms = int(time.time() * 1000) - days * 24 * 3600 * 1000
        closed = client.get_closed_trades(since_ms=since_ms)
        out["window_days"] = days
        wins   = [c for c in closed if c.get("outcome") == "win"]
        losses = [c for c in closed if c.get("outcome") == "loss"]
        out["closed_trades"] = [
            {
                "ticker": c.get("name"), "side": c.get("side"),
                "qty": c.get("qty"), "entry": c.get("openPrice"),
                "exit": c.get("closePrice"), "move": c.get("move"),
                "outcome": (c.get("outcome") or "").upper(),
                "reason": (c.get("exit_reason") or "").upper(),
                "closed_at": c.get("closedAt"),
            }
            for c in closed
        ]
        out["stats"] = {
            "total_closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(100 * len(wins) / len(closed), 1) if closed else 0,
        }

        # Drawdown state
        with _drawdown_lock:
            day_open = _day_open_balance
        out["day_open_balance"] = round(day_open, 2)
        if day_open > 0:
            out["day_pnl"]      = round(balance - day_open, 2)
            out["day_pnl_pct"]  = round(100 * (balance - day_open) / day_open, 2)
        halted, halt_reason = _is_halted(balance)
        out["drawdown_halted"] = halted
        out["drawdown_reason"] = halt_reason or None

    except Exception as e:
        out["broker_error"] = str(e)
        logger.error(f"[Report] Broker fetch failed: {e}")

    # --- Scanner health ---
    try:
        from scanner import SCANNER_STATUS
        out["scanner"] = dict(SCANNER_STATUS)
        out["scanner"]["api_key_set"] = bool(os.getenv("TWELVE_DATA_API_KEY"))
    except Exception as e:
        out["scanner"] = {"error": str(e)}

    # --- Webhook (TradingView alerts) health ---
    out["webhooks"] = dict(WEBHOOK_STATUS)

    # --- Local trade log (entries the bot itself placed this deploy) ---
    try:
        out["bot_logged_trades"] = trade_log.get_open_trades()
    except Exception:
        pass

    return jsonify(out), 200

@app.route("/scalp-backtest", methods=["GET"])
def scalp_backtest():
    """
    Dry-run gold scalp backtest — places NO trades. Tunable via query params:
      ?interval=5min&bars=1000&tp_usd=5&sl_usd=5&lot=0.10&spread_price=0.30
      &ema_fast=9&ema_slow=21&max_hold=24
    """
    from scalper import run_backtest
    def f(name, default, cast):
        try:    return cast(request.args.get(name, default))
        except Exception: return default
    result = run_backtest(
        interval=request.args.get("interval", "5min"),
        bars=f("bars", 1000, int),
        ema_fast=f("ema_fast", 9, int),
        ema_slow=f("ema_slow", 21, int),
        tp_usd=f("tp_usd", 5.0, float),
        sl_usd=f("sl_usd", 5.0, float),
        lot=f("lot", 0.10, float),
        spread_price=f("spread_price", 0.30, float),
        max_hold=f("max_hold", 24, int),
    )
    return jsonify(result), 200

@app.route("/fix-open-tps", methods=["POST", "GET"])
def fix_open_tps():
    """
    Attach a take-profit to any open position that is missing one, derived
    from its entry + stop distance at DEFAULT_TP_RR (or ?rr=N override).
    Secret-protected. Lets me set TPs on already-open trades on request.
    """
    secret = request.args.get("secret") or (request.get_json(silent=True) or {}).get("secret")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        rr = float(request.args.get("rr", DEFAULT_TP_RR))
    except Exception:
        rr = DEFAULT_TP_RR

    try:
        client = TradeLockerClient(
            base_url=TL_BASE_URL, email=TL_EMAIL,
            password=TL_PASSWORD, server=TL_SERVER,
        )
        client.authenticate()
        positions = client.get_open_positions()
    except Exception as e:
        return jsonify({"error": "Broker connection failed", "detail": str(e)}), 502

    fixed, skipped = [], []
    for p in positions:
        pid   = str(p.get("id"))
        entry = float(p.get("openPrice") or 0)
        sl    = float(p.get("stopLoss") or 0)
        tp    = float(p.get("takeProfit") or 0)
        side  = str(p.get("side", "")).lower()
        name  = p.get("name")
        if tp > 0:
            skipped.append({"ticker": name, "reason": "already has TP", "tp": tp})
            continue
        if entry <= 0 or sl <= 0:
            skipped.append({"ticker": name, "reason": "no entry/SL to derive TP from"})
            continue
        sl_dist = abs(entry - sl)
        new_tp  = (entry + sl_dist * rr) if side == "buy" else (entry - sl_dist * rr)
        new_tp  = round(new_tp, 5)
        try:
            client.modify_position_tp(pid, new_tp)
            fixed.append({"ticker": name, "side": side, "entry": entry,
                          "sl": sl, "new_tp": new_tp, "rr": rr})
        except Exception as e:
            skipped.append({"ticker": name, "reason": f"broker error: {e}"})

    return jsonify({"fixed": fixed, "skipped": skipped,
                    "open_positions": len(positions)}), 200

@app.route("/instruments", methods=["GET", "POST"])
def instruments():
    """
    List every tradable instrument name on this account, exactly as
    TradeLocker returns them — for diagnosing "Instrument not found" errors
    without guessing symbol names. Secret-protected. Optional q=substring
    filters the list (case-insensitive).

    Prefer POST with a JSON body ({"secret": "...", "q": "..."}) over the
    query string — a secret with special characters (#, &, +) can get
    mangled or truncated in a URL, especially when pasted into a browser.
    """
    body = request.get_json(silent=True) or {}
    secret = body.get("secret") or request.args.get("secret")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        client = TradeLockerClient(
            base_url=TL_BASE_URL, email=TL_EMAIL,
            password=TL_PASSWORD, server=TL_SERVER,
        )
        client.authenticate()
        url = f"{client.base_url}/trade/accounts/{client.account_id}/instruments"
        resp = client.session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        names = sorted(
            str(inst.get("name", "")) for inst in data.get("d", {}).get("instruments", [])
        )
    except Exception as e:
        return jsonify({"error": "Broker connection failed", "detail": str(e)}), 502

    q = (body.get("q") or request.args.get("q") or "").upper()
    if q:
        names = [n for n in names if q in n.upper()]

    return jsonify({"count": len(names), "instruments": names}), 200

@app.route("/reset-drawdown", methods=["POST", "GET"])
def reset_drawdown():
    """
    Re-anchor the daily drawdown baseline to the CURRENT balance, clearing a
    halt so the bot can trade again. Secret-protected.
    """
    secret = request.args.get("secret") or (request.get_json(silent=True) or {}).get("secret")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    global _day_open_balance, _day_open_date
    try:
        client = TradeLockerClient(
            base_url=TL_BASE_URL, email=TL_EMAIL,
            password=TL_PASSWORD, server=TL_SERVER,
        )
        client.authenticate()
        bal = client.get_balance()
    except Exception as e:
        return jsonify({"error": "Broker auth/balance failed", "detail": str(e)}), 502

    with _drawdown_lock:
        _day_open_balance = bal
        _day_open_date    = _date.today()
    logger.info(f"[Drawdown] Manually reset — new day-open anchor ${bal:,.2f}. Bot un-halted.")
    return jsonify({"status": "drawdown_reset", "new_day_open_balance": round(bal, 2),
                    "halted": False}), 200

@app.route("/send-report", methods=["POST", "GET"])
def send_report():
    """Manually trigger the daily report email (for testing)."""
    secret = request.args.get("secret") or (request.get_json(silent=True) or {}).get("secret")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    ok = send_report_now()
    return jsonify({"sent": ok}), (200 if ok else 502)

# ------------------------------------------------------------------
# Webhook endpoint
# ------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():

    # 1. Parse body — support 3 formats:
    #    A) JSON: {"secret":..., "raw":"{{alert.message}}"}   ← preferred
    #    B) JSON: {"secret":..., "action":"buy", "ticker":..., "price":..., "sl":..., "tp":...}
    #    C) Plain text: raw Olympus message sent directly (no JSON wrapper)

    raw_body = request.get_data(as_text=True).strip()
    data = request.get_json(silent=True)

    # Record that *something* arrived (confirms TradingView is reaching us).
    _record_webhook(summary=(raw_body[:60] or "JSON payload"))

    # Dry-run flag: runs the WHOLE pipeline (auth, balance, risk, sizing) but
    # NEVER places a real order. Lets us safely test against a live account.
    dry_run = bool(data.get("dry_run")) if data else False

    if data:
        # JSON received
        logger.info(f"Webhook received (JSON): {data}")

        # 2. Verify secret
        if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
            logger.warning("Webhook secret mismatch — rejected.")
            _record_webhook(summary="rejected: bad/missing secret", count=False)
            return jsonify({"error": "Unauthorized"}), 401

        raw = data.get("raw", "").strip()

        if raw:
            # Format A — JSON wrapper with {{alert.message}}
            try:
                parsed = parse_olympus_message(raw)
            except ValueError as e:
                logger.error(f"Message parse error: {e}")
                return jsonify({"error": "Could not parse Olympus message", "detail": str(e)}), 400
        else:
            # Format B — manual JSON fields
            parsed = None

        if parsed:
            action = parsed["action"]
            ticker = parsed["ticker"]
            entry  = parsed["entry"]
            sl     = parsed["sl"]
            tp1    = parsed["tp1"]
        else:
            action = str(data.get("action", "")).lower().strip()
            ticker = str(data.get("ticker", "")).upper().strip()
            entry  = float(data.get("price", 0))
            sl     = float(data.get("sl",    0))
            tp1    = float(data.get("tp",    0))

    elif raw_body:
        # Format C — plain text Olympus message (alert message box not yet updated to JSON)
        logger.info(f"Webhook received (plain text): {raw_body}")
        try:
            parsed = parse_olympus_message(raw_body)
        except ValueError as e:
            logger.error(f"Could not parse plain text message: {e}")
            return jsonify({"error": "Could not parse message", "detail": str(e)}), 400

        action = parsed["action"]
        ticker = parsed["ticker"]
        entry  = parsed["entry"]
        sl     = parsed["sl"]
        tp1    = parsed["tp1"]

    else:
        logger.warning("Empty request body received.")
        return jsonify({"error": "Empty request"}), 400

    # Normalise the symbol so a universal "{{ticker}}" message works on any
    # chart (strips exchange prefix, maps aliases + per-broker index names).
    ticker = remap_symbol(ticker)

    # 4. Validate
    if action not in ("buy", "sell"):
        return jsonify({"error": f"Invalid action '{action}'"}), 400
    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400
    if entry <= 0:
        return jsonify({"error": "Invalid entry price"}), 400

    # Alert accepted — auth passed and message parsed into a valid signal.
    # count=False: this request was already counted on arrival above.
    _record_webhook(summary=f"{action.upper()} {ticker}", accepted=True, count=False)

    # Mirror: forward this signal to a partner bot (e.g. Derrick's) so it
    # trades the same alerts, sized to its own account. Best-effort and
    # non-blocking — never let a mirror failure affect this bot's own trade.
    _forward_signal(action, ticker, entry, sl, tp1, dry_run)

    # 4b. Paused — signal received and forwarded to any partners, but this
    # account itself takes no new trade.
    if TRADING_PAUSED:
        logger.info(f"[Pause] Signal {action.upper()} {ticker} received but TRADING_PAUSED — no order placed.")
        return jsonify({"status": "paused", "note": "Trading is paused on this account — no order placed.",
                        "ticker": ticker, "action": action}), 200

    # 5. Connect to TradeLocker
    try:
        client = TradeLockerClient(
            base_url=TL_BASE_URL,
            email=TL_EMAIL,
            password=TL_PASSWORD,
            server=TL_SERVER,
        )
        client.authenticate()
    except Exception as e:
        logger.error(f"TradeLocker auth failed: {e}")
        return jsonify({"error": "Broker authentication failed", "detail": str(e)}), 502

    # 6. Get live balance
    balance = client.get_balance()
    logger.info(f"Account balance: ${balance:,.2f}")

    # 6a. Daily drawdown gate
    _update_day_open(balance)
    halted, halt_reason = _is_halted(balance)
    if halted:
        logger.warning(f"[Drawdown] TRADE BLOCKED — {halt_reason}")
        return jsonify({"status": "blocked", "reason": halt_reason}), 403

    # 6b. Max open trades gate + one trade per symbol rule
    try:
        open_positions = client.get_open_positions()
    except Exception as e:
        logger.warning(f"Could not fetch open positions for risk checks: {e}")
        open_positions = []

    open_count = len(open_positions)
    if open_count >= MAX_OPEN_TRADES:
        msg = (
            f"Max open trades reached: {open_count}/{MAX_OPEN_TRADES} positions "
            f"already open — signal for {ticker} blocked."
        )
        logger.warning(f"[Risk] {msg}")
        return jsonify({"status": "blocked", "reason": msg}), 403

    # One trade per symbol — no stacking the same pair
    open_tickers = []
    for p in open_positions:
        name = str(p.get("name") or p.get("symbol") or "").upper()
        if name:
            open_tickers.append(name)

    if ticker.upper() in open_tickers:
        msg = (
            f"{ticker} already has an open position — "
            f"waiting for it to close before taking another signal."
        )
        logger.warning(f"[Risk] {msg}")
        return jsonify({"status": "blocked", "reason": msg}), 403

    # 7. Fallback SL if missing
    if not sl or sl <= 0:
        sl = calculate_default_sl(entry, action, DEFAULT_SL_PCT)
        logger.info(f"Using default SL: {sl}")

    # 7a. Fallback TP if missing — signals like "Buy Signal/Sell Signal" send no
    #     take-profit, so derive one from the SL distance at DEFAULT_TP_RR.
    if (not tp1 or tp1 <= 0) and sl and sl > 0:
        sl_dist = abs(entry - sl)
        if sl_dist > 0:
            tp1 = (entry + sl_dist * DEFAULT_TP_RR) if action == "buy" \
                  else (entry - sl_dist * DEFAULT_TP_RR)
            tp1 = round(tp1, 5)
            logger.info(f"Using default TP (R:R {DEFAULT_TP_RR}): {tp1}")

    # 7b. Minimum R:R filter — TP must be at least 1.0x the SL distance
    #     Olympus signals are 1:1 by design, so we allow anything >= 1.0.
    if tp1 and tp1 > 0 and sl and sl > 0:
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp1 - entry)
        rr      = tp_dist / sl_dist if sl_dist > 0 else 0
        if rr < 1.0:
            msg = (
                f"R:R too low for {ticker}: {rr:.2f} "
                f"(TP={tp1}, SL={sl}, entry={entry}). "
                f"Minimum required: 1.0 — signal skipped."
            )
            logger.warning(f"[Risk] {msg}")
            return jsonify({"status": "blocked", "reason": msg}), 403
        logger.info(f"[Risk] R:R check passed: {rr:.2f} ✓")

    # 8. Calculate lot size — check for per-symbol override first
    override_key = f"LOT_OVERRIDE_{ticker.upper()}"
    lot_override = os.getenv(override_key, "").strip()
    if lot_override:
        try:
            lots = float(lot_override)
            logger.info(f"[Risk] Using lot override for {ticker}: {lots} (via {override_key})")
        except ValueError:
            logger.warning(f"Invalid {override_key}='{lot_override}', falling back to dynamic sizing.")
            lot_override = ""

    if not lot_override:
        try:
            lots = calculate_lots(
                balance=balance,
                entry_price=entry,
                stop_loss_price=sl,
                risk_pct=RISK_PCT,
                ticker=ticker,
            )
        except Exception as e:
            logger.error(f"Position sizing error: {e}")
            return jsonify({"error": "Position sizing failed", "detail": str(e)}), 400

    logger.info(
        f"Trade: {action.upper()} {ticker} | "
        f"Lots: {lots} | Entry: {entry} | SL: {sl} | TP1: {tp1}"
    )

    # 8b. DRY RUN — stop here. Everything above (auth, balance, drawdown gate,
    #     risk checks, lot sizing) has run for real, but NO order is sent.
    if dry_run:
        logger.info(f"[DRY RUN] Would place {action.upper()} {ticker} | lots={lots} "
                    f"| entry={entry} | SL={sl} | TP1={tp1} — NO order sent.")
        return jsonify({
            "status":   "dry_run_ok",
            "note":     "Pipeline validated end-to-end. NO real order was placed.",
            "ticker":   ticker,
            "action":   action,
            "lots":     lots,
            "entry":    entry,
            "sl":       sl,
            "tp1":      tp1 if tp1 and tp1 > 0 else None,
            "balance":  balance,
            "risked":   round(balance * RISK_PCT, 2),
        }), 200

    # 9. Place order with SL and TP1
    try:
        order = client.place_market_order(
            symbol=ticker,
            side=action,
            qty=lots,
            stop_loss=sl,
            take_profit=tp1 if tp1 and tp1 > 0 else None,
        )
    except Exception as e:
        logger.error(f"Order failed: {e}")
        return jsonify({"error": "Order failed", "detail": str(e)}), 502

    # 10. Register TP with trailing manager (covers TP-dropped case too)
    order_id = str(order.get("d", {}).get("orderId", "") if isinstance(order.get("d"), dict) else "")
    if tp1 and tp1 > 0:
        trailing_manager.register_tp(order_id, tp1)

    # Log the entry
    trade_id = trade_log.log_entry(
        ticker=ticker, side=action, lots=lots,
        entry=entry, sl=sl, tp1=tp1,
        balance=balance, order_id=order_id,
    )

    # 10b. Link this log entry to its broker position ID so the trailing
    # loop can later match the close back to this trade (position_id
    # starts as None otherwise, and log_exit() can never find it — every
    # trade would sit "open" forever in bot_logged_trades). The position
    # may not be visible via the API for a moment right after the order
    # fills, so retry briefly instead of checking only once.
    matched = False
    for attempt in range(4):
        try:
            for p in client.get_open_positions():
                if p.get("name", "").upper() == ticker.upper() and p.get("side") == action:
                    trade_log.match_position(trade_id, p["id"])
                    matched = True
                    break
        except Exception as e:
            logger.warning(f"[TradeLog] Position lookup failed (attempt {attempt + 1}): {e}")
        if matched:
            break
        time.sleep(1.5)
    if not matched:
        logger.warning(f"Could not link trade {trade_id} to a position ID after retries.")

    # 11. Success response
    tp_dropped = order.pop("_tp_dropped", False)
    response = {
        "status":   "order_placed",
        "ticker":   ticker,
        "action":   action,
        "lots":     lots,
        "entry":    entry,
        "sl":       sl,
        "tp1":      tp1 if not tp_dropped else None,
        "tp_note":  "TP rejected by broker (price moved) — order placed with SL only" if tp_dropped else None,
        "trailing": "will activate at 50% toward TP",
        "balance":  balance,
        "risked":   round(balance * RISK_PCT, 2),
        "order":    order,
    }
    if tp_dropped:
        logger.warning(f"Order placed WITHOUT TP (SL attached): {response}")
    else:
        logger.info(f"Success: {response}")
    return jsonify(response), 200


# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Starting Olympus bot on port {port}...")
    app.run(host="0.0.0.0", port=port)
