"""
Olympus Alert Message Parser

Parses the native Olympus indicator alert format:
  🚀 BUY GBPUSD | 1 | 01:19 | ENTRY: 1.359 | SL: 1.355 | TP1: 1.363 | TP2: 1.366 | TP3: 1.379 | R:R 1:1

Returns a clean dict with action, ticker, entry, sl, tp1, tp2, tp3.
"""

import re
import logging

logger = logging.getLogger(__name__)

# Maps TradingView symbol names → TradeLocker/HEROFX symbol names
SYMBOL_MAP = {
    # NAS100 variants → HEROFX: NAS100
    "NAS100":   "NAS100",
    "US100":    "NAS100",
    "NASDAQ":   "NAS100",
    "NDX100":   "NAS100",
    "NDXUSD":   "NAS100",
    # US30 / Dow variants → HEROFX: US30
    "DJ30":     "US30",
    "WS30":     "US30",
    "DOW":      "US30",
    "DJIUSD":   "US30",
    # SPX500 variants → HEROFX: SPX500
    "SPX":      "SPX500",
    "SP500":    "SPX500",
    "US500":    "SPX500",
    # Gold/Silver
    "XAUUSD":   "XAUUSD",
    "XAGUSD":   "XAGUSD",
    # Forex pairs pass through unchanged
}


def parse_olympus_message(message: str) -> dict:
    """
    Parse an Olympus alert message string into trade parameters.

    Args:
        message: Raw Olympus alert string (from {{alert.message}})

    Returns:
        {
            "action":  "buy" | "sell",
            "ticker":  "GBPUSD",
            "entry":   1.359,
            "sl":      1.355,
            "tp1":     1.363,
            "tp2":     1.366,   # optional
            "tp3":     1.379,   # optional
        }

    Raises:
        ValueError if required fields cannot be parsed.
    """
    msg = message.strip()
    logger.info(f"Parsing Olympus message: {msg}")

    # ---- Direction (BUY / SELL) ----------------------------------------
    action_match = re.search(r'\b(BUY|SELL)\b', msg, re.IGNORECASE)
    if not action_match:
        raise ValueError(f"Could not find BUY/SELL in message: {msg}")
    action = action_match.group(1).lower()

    # ---- Symbol -----------------------------------------------------------
    # Filler words that follow BUY/SELL but are NOT the symbol
    _FILLER = {"ON", "AT", "FOR", "SIGNAL", "ALERT", "TRADE", "ENTRY",
               "THE", "A", "IN", "NOW", "SETUP", "BUY", "SELL"}

    # Try word immediately after BUY/SELL (skipping fillers)
    ticker = ""
    after_action = re.findall(
        r'\b(?:BUY|SELL)\s+((?:[A-Z0-9]+:)?[A-Z0-9]+)',
        msg, re.IGNORECASE
    )
    for candidate in after_action:
        candidate = candidate.upper()
        if ":" in candidate:
            candidate = candidate.split(":", 1)[-1]
        if candidate not in _FILLER and len(candidate) >= 2:
            ticker = candidate
            break

    # Fallback: scan all pipe-separated segments for a symbol-shaped word
    if not ticker:
        for segment in msg.split("|"):
            words = re.findall(r'\b([A-Z]{2,6}[A-Z0-9]{0,4})\b', segment.upper())
            for word in words:
                if word not in _FILLER and not word.isdigit():
                    ticker = word
                    break
            if ticker:
                break

    if not ticker:
        raise ValueError(f"Could not find symbol in message: {msg}")

    # Remap TradingView symbol → TradeLocker symbol if needed
    if ticker in SYMBOL_MAP:
        mapped = SYMBOL_MAP[ticker]
        if mapped != ticker:
            logger.info(f"Symbol remapped: {ticker} → {mapped}")
        ticker = mapped

    # ---- Entry price ------------------------------------------------------
    entry_match = re.search(r'ENTRY[:\s]+([\d.]+)', msg, re.IGNORECASE)
    if not entry_match:
        raise ValueError(f"Could not find ENTRY in message: {msg}")
    entry = float(entry_match.group(1))

    # ---- Stop Loss --------------------------------------------------------
    sl_match = re.search(r'\bSL[:\s]+([\d.]+)', msg, re.IGNORECASE)
    if not sl_match:
        raise ValueError(f"Could not find SL in message: {msg}")
    sl = float(sl_match.group(1))

    # ---- Take Profits (all optional — trade executes with SL only if absent)
    tp1_match = re.search(r'TP1[:\s]+([\d.]+)', msg, re.IGNORECASE)
    tp1 = float(tp1_match.group(1)) if tp1_match else 0.0

    tp2_match = re.search(r'TP2[:\s]+([\d.]+)', msg, re.IGNORECASE)
    tp2 = float(tp2_match.group(1)) if tp2_match else None

    tp3_match = re.search(r'TP3[:\s]+([\d.]+)', msg, re.IGNORECASE)
    tp3 = float(tp3_match.group(1)) if tp3_match else None

    result = {
        "action": action,
        "ticker": ticker,
        "entry":  entry,
        "sl":     sl,
        "tp1":    tp1,
        "tp2":    tp2,
        "tp3":    tp3,
    }

    logger.info(
        f"Parsed: {action.upper()} {ticker} | "
        f"Entry: {entry} | SL: {sl} | TP1: {tp1} | TP2: {tp2} | TP3: {tp3}"
    )
    return result
