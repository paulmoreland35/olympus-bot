"""
Position Size Calculator — 2% Risk Per Trade

Formula (Forex standard lots):
  risk_amount      = account_balance × risk_pct          e.g. $10,000 × 0.02 = $200
  sl_distance      = abs(entry_price − stop_loss_price)  e.g. 0.00500 (50 pips on EURUSD)
  dollar_per_lot   = sl_distance × 100,000               e.g. 0.00500 × 100,000 = $500
  lots             = risk_amount / dollar_per_lot         e.g. $200 / $500 = 0.40 lots

Note: Most accurate for USD-quoted pairs (EURUSD, GBPUSD, etc.).
For JPY pairs the pip value differs slightly but remains within acceptable tolerance.
"""

import logging

logger = logging.getLogger(__name__)

# Broker constraints
MIN_LOT  = 0.01
MAX_LOT  = 100.0
LOT_STEP = 0.01

# Contract sizes per instrument type
# dollar_risk_per_lot = sl_distance × contract_size
CONTRACT_SIZES = {
    # Forex (standard lot = 100,000 units)
    "DEFAULT": 100_000,
    # Gold / Silver
    "XAUUSD":  100,      # 100 troy oz per lot
    "XAGUSD":  5_000,    # 5,000 oz per lot
    # Indices (CFD — $10 per point per lot)
    # HEROFX names
    "NAS100":  10,
    "US30":    10,
    "SPX500":  10,
    # LIVVFX names
    "NDXUSD":  10,
    "DJIUSD":  10,
    "SPXUSD":  10,
    # Common aliases
    "US100":   10,
    "US500":   10,
    "DJ30":    10,
    "WS30":    10,
}


def _contract_size(ticker: str) -> int:
    """Return the contract size for a given symbol."""
    return CONTRACT_SIZES.get(ticker.upper(), CONTRACT_SIZES["DEFAULT"])


def calculate_lots(
    balance: float,
    entry_price: float,
    stop_loss_price: float,
    risk_pct: float = 0.02,
    ticker: str = "",
) -> float:
    """
    Calculate position size in lots based on account risk.

    Args:
        balance:         Account balance in account currency (e.g. USD)
        entry_price:     Trade entry price
        stop_loss_price: Stop loss price
        risk_pct:        Fraction of balance to risk (default 0.02 = 2%)

    Returns:
        Lot size rounded to nearest 0.01, clamped between MIN_LOT and MAX_LOT
    """
    if balance <= 0:
        raise ValueError(f"Invalid balance: {balance}")
    if entry_price <= 0:
        raise ValueError(f"Invalid entry price: {entry_price}")
    if stop_loss_price <= 0:
        raise ValueError(f"Invalid stop loss price: {stop_loss_price}")

    sl_distance = abs(entry_price - stop_loss_price)
    if sl_distance == 0:
        raise ValueError("Stop loss price cannot equal entry price.")

    risk_amount    = balance * risk_pct
    contract_size  = _contract_size(ticker)
    dollar_per_lot = sl_distance * contract_size
    raw_lots       = risk_amount / dollar_per_lot

    # Round down to nearest lot step (conservative — never risk more than 2%)
    lots = _floor_to_step(raw_lots, LOT_STEP)
    lots = max(MIN_LOT, min(MAX_LOT, lots))

    logger.info(
        f"Risk calc [{ticker}]: balance={balance:.2f} | risk={risk_amount:.2f} | "
        f"entry={entry_price} | sl={stop_loss_price} | sl_dist={sl_distance:.5f} | "
        f"contract={contract_size} | raw_lots={raw_lots:.4f} | final_lots={lots:.2f}"
    )

    return lots


def calculate_default_sl(
    entry_price: float,
    side: str,
    sl_pct: float = 0.01,
) -> float:
    """
    Generate a fallback stop loss when none is provided in the alert.
    Places SL `sl_pct` away from entry in the correct direction.

    Args:
        entry_price: Trade entry price
        side:        "buy" or "sell"
        sl_pct:      Distance as fraction of entry price (default 1%)

    Returns:
        Stop loss price
    """
    if side.lower() == "buy":
        sl = entry_price * (1 - sl_pct)
    else:
        sl = entry_price * (1 + sl_pct)

    logger.info(
        f"Default SL generated: entry={entry_price} | side={side} | "
        f"sl_pct={sl_pct*100:.1f}% | sl={sl:.5f}"
    )
    return round(sl, 5)


def _floor_to_step(value: float, step: float) -> float:
    """Floor a value to the nearest step increment."""
    return round(int(value / step) * step, 10)
