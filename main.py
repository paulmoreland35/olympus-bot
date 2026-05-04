"""
Olympus → TradeLocker Webhook Server
-------------------------------------
Receives TradingView alerts and places trades on TradeLocker with
automatic 2% risk-based position sizing.

Expected JSON payload from TradingView alert:
{
  "secret":  "YOUR_WEBHOOK_SECRET",   ← security token
  "action":  "buy" | "sell",          ← trade direction
  "ticker":  "EURUSD",                ← symbol (use {{ticker}} in TV)
  "price":   1.08500,                 ← entry price (use {{close}} in TV)
  "sl":      1.07800,                 ← stop loss price  (0 = use default)
  "tp":      1.09500                  ← take profit price (0 = skip)
}
"""

import logging
import os

from flask import Flask, request, jsonify
from dotenv import load_dotenv

from tradelocker_client import TradeLockerClient
from risk import calculate_lots, calculate_default_sl

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

TL_BASE_URL      = os.getenv("TL_BASE_URL",      "https://members.livvfxtrading.com/backend-api")
TL_EMAIL         = os.getenv("TL_EMAIL",          "")
TL_PASSWORD      = os.getenv("TL_PASSWORD",       "")
TL_SERVER        = os.getenv("TL_SERVER",         "LIVVFX")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET",    "")
RISK_PCT         = float(os.getenv("RISK_PCT",    "0.02"))    # 2%
DEFAULT_SL_PCT   = float(os.getenv("DEFAULT_SL_PCT", "0.01")) # 1% fallback SL

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
    # 1. Parse JSON
    data = request.get_json(silent=True)
    if not data:
        logger.warning("Received non-JSON request.")
        return jsonify({"error": "Invalid JSON"}), 400

    logger.info(f"Webhook received: {data}")

    # 2. Verify secret
    if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
        logger.warning("Webhook secret mismatch — request rejected.")
        return jsonify({"error": "Unauthorized"}), 401

    # 3. Extract fields
    action = str(data.get("action", "")).lower().strip()
    ticker = str(data.get("ticker", "")).upper().strip()
    price  = float(data.get("price", 0))
    sl     = float(data.get("sl",    0))
    tp     = float(data.get("tp",    0))

    # 4. Validate required fields
    if action not in ("buy", "sell"):
        return jsonify({"error": f"Invalid action '{action}'. Must be 'buy' or 'sell'."}), 400
    if not ticker:
        return jsonify({"error": "Missing 'ticker' field."}), 400
    if price <= 0:
        return jsonify({"error": "Invalid or missing 'price' field."}), 400

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

    # 7. Determine stop loss
    if sl <= 0:
        sl = calculate_default_sl(price, action, DEFAULT_SL_PCT)
        logger.info(f"No SL in alert — using default SL: {sl}")

    # 8. Calculate position size (2% risk)
    try:
        lots = calculate_lots(
            balance=balance,
            entry_price=price,
            stop_loss_price=sl,
            risk_pct=RISK_PCT,
        )
    except Exception as e:
        logger.error(f"Position sizing error: {e}")
        return jsonify({"error": "Position sizing failed", "detail": str(e)}), 400

    logger.info(
        f"Trade decision: {action.upper()} {ticker} | "
        f"Lots: {lots} | Entry: {price} | SL: {sl} | TP: {tp or 'none'}"
    )

    # 9. Place order
    try:
        order = client.place_market_order(
            symbol=ticker,
            side=action,
            qty=lots,
            stop_loss=sl,
            take_profit=tp if tp > 0 else None,
        )
    except Exception as e:
        logger.error(f"Order placement failed: {e}")
        return jsonify({"error": "Order failed", "detail": str(e)}), 502

    # 10. Return success
    response = {
        "status":  "order_placed",
        "ticker":  ticker,
        "action":  action,
        "lots":    lots,
        "entry":   price,
        "sl":      sl,
        "tp":      tp or None,
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
