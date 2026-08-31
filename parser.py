"""
Olympus Alert Message Parser

Parses the native Olympus indicator alert format:
  🚀 BUY GBPUSD | 1 | 01:19 | ENTRY: 1.359 | SL: 1.355 | TP1: 1.363 | TP2: 1.366 | TP3: 1.379 | R:R 1:1

Returns a clean dict with action, ticker, entry, sl, tp1, tp2, tp3.
"""

import os
import re
import logging

logger = logging.getLogger(__name__)

# Per-broker symbol maps — TradingView name → broker instrument name.
# Controlled by the TL_SERVER env var set in Railway.
_SYMBOL_MAPS = {
    "HEROFX": {
        "NAS100":  "NAS100",
        "US100":   "NAS100",
        "NASDAQ":  "NAS100",
        "NDX100":  "NAS100",
        "NDXUSD":  "NAS100",
        "DJ30":    "US30",
        "WS30":    "US30",
        "DOW":     "US30",
        "DJIUSD":  "US30",
        "SPX":     "SPX500",
        "SP500":   "SPX500",
        "US500":   "SPX500",
    },
    "LIVVFX": {
        # LIVVFX renamed its indices to plain names (matching Gold-style
        # sizing) in their late-2026 update — NDXUSD/DJIUSD/SPXUSD are gone.
        "NAS100":  "NAS100",
        "US100":   "NAS100",
        "NASDAQ":  "NAS100",
        "NDX100":  "NAS100",
        "NDXUSD":  "NAS100",
        "DJ30":    "US30",
        "WS30":    "US30",
        "DOW":     "US30",
        "DJIUSD":  "US30",
        "SPX":     "SPX500",
        "SP500":   "SPX500",
        "US500":   "SPX500",
        "SPXUSD":  "SPX500",
    },
}

# Gold/Silver pass through unchanged on all brokers
_METALS = {"XAUUSD", "XAGUSD"}

# Common aliases that resolve to a canonical metal symbol regardless of broker
_METAL_ALIASES = {
    "GOLD": "XAUUSD", "XAU": "XAUUSD", "XAUUSD": "XAUUSD",
    "SILVER": "XAGUSD", "XAG": "XAGUSD", "XAGUSD": "XAGUSD",
}

def _get_symbol_map() -> dict:
    # Symbol map is keyed off TL_SERVER only. Deployments that need index
    # remapping (e.g. Derrick's LIVVFX #721022 -> NDXUSD/DJIUSD) set
    # TL_SERVER=LIVVFX explicitly. Accounts whose broker uses NAS100/US30
    # directly (e.g. paul-livvfx #746347) leave TL_SERVER unset, so this
    # falls back to the pass-through HEROFX map. Do NOT read TL_SERVER_LIVE
    # here — that would force the LIVVFX map onto accounts that don't want it.
    server = os.getenv("TL_SERVER", "HEROFX").upper()
    return _SYMBOL_MAPS.get(server, _SYMBOL_MAPS["HEROFX"])

def remap_symbol(ticker: str) -> str:
    """
    Normalise ANY TradingView ticker to the correct broker instrument name,
    so a single universal alert message ("ticker":"{{ticker}}") works on
    every chart. Handles:
      - exchange prefixes:  "OANDA:XAUUSD" -> "XAUUSD"
      - perp/futures suffixes: "BTCUSD.P", "NAS100!" -> stripped
      - metal aliases: "GOLD" -> "XAUUSD"
      - per-broker index names (HEROFX vs LIVVFX) via _SYMBOL_MAPS
    """
    upper = ticker.upper().strip()

    # Strip TradingView exchange prefix, e.g. "PEPPERSTONE:NAS100" -> "NAS100"
    if ":" in upper:
        upper = upper.split(":")[-1].strip()

    # Strip common perpetual/futures markers
    for suf in (".P", ".PERP", "PERP", "!"):
        if upper.endswith(suf):
            upper = upper[: -len(suf)].strip()

    # Metals (and their aliases) pass through to the canonical symbol
    if upper in _METAL_ALIASES:
        return _METAL_ALIASES[upper]
    if upper in _METALS:
        return upper

    symbol_map = _get_symbol_map()
    return symbol_map.get(upper, upper)


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

    # Remap TradingView symbol → broker-specific instrument name
    remapped = remap_symbol(ticker)
    if remapped != ticker:
        logger.info(f"Symbol remapped: {ticker} → {remapped} (broker: {os.getenv('TL_SERVER','?')})")
    ticker = remapped

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
