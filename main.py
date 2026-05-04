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

from flask import Flask, request, jsonify
from dotenv import load_dotenv

from tradelocker_client import TradeLockerClient
from risk import calculate_lots, calculate_default_sl
from parser import parse_olympus_message

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
TL_SERVER      = os.getenv("TL_SERVER",         "LIVVFX")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET",    "")
RISK_PCT       = float(os.getenv("RISK_PCT",    "0.02"))
DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", "0.01"))

# ------------------------------------------------------------------
# Health check
# ------------------------------------------------------------------

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "Olympus bot is running ✓"}), 200

# ------------------------------------------------------------------
# Webhook endpoint
# ------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():

    # 1. Parse JSON body
    data = request.get_json(silent=True)
    if not data:
        logger.warning("Received non-JSON request.")
        return jsonify({"error": "Invalid JSON"}), 400

    logger.info(f"Webhook received: {data}")

    # 2. Verify secret
    if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
        logger.warning("Webhook secret mismatch — rejected.")
        return jsonify({"error": "Unauthorized"}), 401

    # 3. Parse Olympus message  ─────────────────────────────────────
    #    Supports two formats:
    #    A) {"secret":..., "raw": "{{alert.message}}"}   ← preferred
    #    B) {"secret":..., "action":"buy", "ticker":..., "price":..., "sl":..., "tp":...}
    # ──────────────────────────────────────────────────────────────

    raw = data.get("raw", "").strip()

    if raw:
        # Format A — parse full Olympus message
        try:
            parsed = parse_olympus_message(raw)
        except ValueError as e:
            logger.error(f"Message parse error: {e}")
            return jsonify({"error": "Could not parse Olympus message", "detail": str(e)}), 400

        action = parsed["action"]
        ticker = parsed["ticker"]
        entry  = parsed["entry"]
        sl     = parsed["sl"]
        tp1    = parsed["tp1"]

    else:
        # Format B — manual JSON fields (legacy / testing)
        action = str(data.get("action", "")).lower().strip()
        ticker = str(data.get("ticker", "")).upper().strip()
        entry  = float(data.get("price", 0))
        sl     = float(data.get("sl",    0))
        tp1    = float(data.get("tp",    0))

    # 4. Validate
    if action not in ("buy", "sell"):
        return jsonify({"error": f"Invalid action '{action}'"}), 400
    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400
    if entry <= 0:
        return jsonify({"error": "Invalid entry price"}), 400

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

    # 7. Fallback SL if missing
    if not sl or sl <= 0:
        sl = calculate_default_sl(entry, action, DEFAULT_SL_PCT)
        logger.info(f"Using default SL: {sl}")

    # 8. Calculate lot size (2% risk)
    try:
        lots = calculate_lots(
            balance=balance,
            entry_price=entry,
            stop_loss_price=sl,
            risk_pct=RISK_PCT,
        )
    except Exception as e:
        logger.error(f"Position sizing error: {e}")
        return jsonify({"error": "Position sizing failed", "detail": str(e)}), 400

    logger.info(
        f"Trade: {action.upper()} {ticker} | "
        f"Lots: {lots} | Entry: {entry} | SL: {sl} | TP1: {tp1}"
    )

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

    # 10. Success response
    response = {
        "status":  "order_placed",
        "ticker":  ticker,
        "action":  action,
        "lots":    lots,
        "entry":   entry,
        "sl":      sl,
        "tp1":     tp1,
        "balance": balance,
        "risked":  round(balance * RISK_PCT, 2),
        "order":   order,
    }
    logger.info(f"Success: {response}")
    return jsonify(response), 200


# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Starting Olympus bot on port {port}...")
    app.run(host="0.0.0.0", port=port)
