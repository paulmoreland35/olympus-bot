"""
Autonomous Market Scanner
Wakes at every 4H candle close, fetches live OHLC data via Twelve Data,
runs the full ICT/SMC confluence analysis (same logic as strategy.pine),
and places trades directly through TradeLocker.

No TradingView required.

Setup:
  1. Free API key from https://twelvedata.com (800 calls/day, no card)
  2. Add TWELVE_DATA_API_KEY to Railway env vars (or .env locally)
  3. Set SCANNER_SYMBOLS comma-separated, e.g. XAU/USD,NDX,DJI
"""

import os
import time
import logging
import threading
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
from twelvedata import TDClient

from risk import calculate_lots

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol map: Twelve Data symbol -> TradeLocker broker symbol
# ---------------------------------------------------------------------------
DEFAULT_SYMBOLS = {
    "XAU/USD": "XAUUSD",
    "NDX":     "NAS100",
    "DJI":     "DJIUSD",
}

# ---------------------------------------------------------------------------
# Strategy parameters (mirror strategy.pine defaults)
# ---------------------------------------------------------------------------
SW_LEFT      = 5
SW_RIGHT     = 5
FVG_BARS     = 15
SWEEP_BARS   = 20
OB_DISP_LEN  = 8
PD_LEN       = 100
MIN_SCORE    = 4
SL_BUF       = 0.3
ATR_SL       = 1.5
TP_RR        = 2.0
ATR_LEN      = 14
RISK_PCT     = 0.02
Q_SIZE       = 25.0       # Quarter Theory step (gold $25 levels)


# ---------------------------------------------------------------------------
# Technical helpers
# ---------------------------------------------------------------------------

def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _atr(df: pd.DataFrame, length: int = ATR_LEN) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean()


def _pivot_highs(series: pd.Series, left: int, right: int) -> pd.Series:
    result = pd.Series(np.nan, index=series.index)
    for i in range(left, len(series) - right):
        window = series.iloc[i - left: i + right + 1]
        if series.iloc[i] >= window.max():
            result.iloc[i] = series.iloc[i]
    return result


def _pivot_lows(series: pd.Series, left: int, right: int) -> pd.Series:
    result = pd.Series(np.nan, index=series.index)
    for i in range(left, len(series) - right):
        window = series.iloc[i - left: i + right + 1]
        if series.iloc[i] <= window.min():
            result.iloc[i] = series.iloc[i]
    return result


# ---------------------------------------------------------------------------
# Data fetcher
# ---------------------------------------------------------------------------

def fetch_candles(td: TDClient, symbol: str, interval: str, outputsize: int = 300) -> pd.DataFrame:
    """Fetch OHLC candles, return DataFrame sorted oldest-first."""
    ts = td.time_series(symbol=symbol, interval=interval, outputsize=outputsize, timezone="UTC")
    df = ts.as_pandas()
    df.columns = [c.lower() for c in df.columns]
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_index()
    return df


# ---------------------------------------------------------------------------
# Core analysis (mirrors strategy.pine logic exactly)
# ---------------------------------------------------------------------------

def analyze(df_4h: pd.DataFrame, df_daily: pd.DataFrame, symbol: str):
    """
    Run full ICT confluence analysis on the last closed 4H bar.

    Returns (action, entry, sl, tp, score) or None if no signal.
    """
    if len(df_4h) < 200 or len(df_daily) < 205:
        logger.warning(f"[{symbol}] Not enough candles for analysis.")
        return None

    # Use the last CLOSED bar (iloc[-1] because we fetch at bar close)
    c = df_4h["close"].iloc[-1]
    h = df_4h["high"].iloc[-1]
    l = df_4h["low"].iloc[-1]
    o = df_4h["open"].iloc[-1]
    atr_val = _atr(df_4h).iloc[-1]

    if np.isnan(atr_val) or atr_val == 0:
        logger.warning(f"[{symbol}] ATR is nan/zero, skipping.")
        return None

    # -- 1. Macro Bias -------------------------------------------------------
    ema200_d  = _ema(df_daily["close"], 200).iloc[-1]
    ema50_4h  = _ema(df_4h["close"],  50).iloc[-1]
    ema200_4h = _ema(df_4h["close"], 200).iloc[-1]

    biasBull = c > ema200_d and ema50_4h > ema200_4h
    biasBear = c < ema200_d and ema50_4h < ema200_4h

    # -- 2. Session filter (UTC) --------------------------------------------
    now_h = datetime.now(timezone.utc).hour
    inSession = 6 <= now_h < 21

    # -- 3. Market Structure (HH/HL or LH/LL) --------------------------------
    ph = _pivot_highs(df_4h["high"], SW_LEFT, SW_RIGHT)
    pl = _pivot_lows(df_4h["low"],  SW_LEFT, SW_RIGHT)

    valid_ph = ph.dropna().values
    valid_pl = pl.dropna().values

    bullStruct = (
        len(valid_ph) >= 2 and len(valid_pl) >= 2
        and valid_ph[-1] > valid_ph[-2]
        and valid_pl[-1] > valid_pl[-2]
    )
    bearStruct = (
        len(valid_ph) >= 2 and len(valid_pl) >= 2
        and valid_pl[-1] < valid_pl[-2]
        and valid_ph[-1] < valid_ph[-2]
    )

    # -- 4. Premium / Discount + OTE -----------------------------------------
    rng_hi  = df_4h["high"].iloc[-PD_LEN:].max()
    rng_lo  = df_4h["low"].iloc[-PD_LEN:].min()
    rng_mid = (rng_hi + rng_lo) / 2.0
    discount = c < rng_mid
    premium  = c > rng_mid

    # -- 5. Fair Value Gaps --------------------------------------------------
    bFVG_top = bFVG_bot = sFVG_top = sFVG_bot = None

    highs = df_4h["high"].values
    lows  = df_4h["low"].values
    n     = len(highs)

    for i in range(1, min(FVG_BARS + 1, n - 3)):
        j = i + 2
        if highs[n - j - 1] < lows[n - i - 1]:
            bFVG_top = lows[n - i - 1]
            bFVG_bot = highs[n - j - 1]
            break

    for i in range(1, min(FVG_BARS + 1, n - 3)):
        j = i + 2
        if lows[n - j - 1] > highs[n - i - 1]:
            sFVG_top = lows[n - j - 1]
            sFVG_bot = highs[n - i - 1]
            break

    atBullFVG = bFVG_top is not None and (bFVG_bot - atr_val * 0.2) <= c <= (bFVG_top + atr_val * 0.1)
    atBearFVG = sFVG_top is not None and (sFVG_bot - atr_val * 0.1) <= c <= (sFVG_top + atr_val * 0.2)

    # -- 6. Order Blocks -----------------------------------------------------
    close_ = df_4h["close"].values
    open_  = df_4h["open"].values

    bullDisplace = c > o and h > df_4h["high"].iloc[-OB_DISP_LEN - 1:-1].max()
    bearDisplace = c < o and l < df_4h["low"].iloc[-OB_DISP_LEN - 1:-1].min()

    bullOB_hi = bullOB_lo = bearOB_hi = bearOB_lo = None

    if bullDisplace:
        for k in range(1, 6):
            if n - k - 1 >= 0 and close_[n - k - 1] < open_[n - k - 1]:
                bullOB_hi = max(open_[n - k - 1], close_[n - k - 1])
                bullOB_lo = min(open_[n - k - 1], close_[n - k - 1])
                break

    if bearDisplace:
        for k in range(1, 6):
            if n - k - 1 >= 0 and close_[n - k - 1] > open_[n - k - 1]:
                bearOB_hi = max(open_[n - k - 1], close_[n - k - 1])
                bearOB_lo = min(open_[n - k - 1], close_[n - k - 1])
                break

    atBullOB = bullOB_hi is not None and l <= bullOB_hi and c >= bullOB_lo and c > o
    atBearOB = bearOB_hi is not None and h >= bearOB_lo and c <= bearOB_hi and c < o

    # -- 7. Liquidity Sweeps -------------------------------------------------
    prevLow  = df_4h["low"].iloc[-SWEEP_BARS - 1:-1].min()
    prevHigh = df_4h["high"].iloc[-SWEEP_BARS - 1:-1].max()

    sweepSSL = l < prevLow  and c > prevLow  and c > o
    sweepBSL = h > prevHigh and c < prevHigh and c < o

    sweepBullLow  = l if sweepSSL else None
    sweepBearHigh = h if sweepBSL else None

    # -- 8. Quarter Theory ---------------------------------------------------
    q_nearest = round(c / Q_SIZE) * Q_SIZE
    nearQ = abs(c - q_nearest) / c * 100 < 0.12

    # -- Confluence scoring --------------------------------------------------
    bullScore = sum([biasBull, bullStruct, inSession, discount,
                     atBullFVG, atBullOB, sweepSSL, nearQ])
    bearScore = sum([biasBear, bearStruct, inSession, premium,
                     atBearFVG, atBearOB, sweepBSL, nearQ])

    logger.info(
        f"[{symbol}] Bull={bullScore}/8 Bear={bearScore}/8 | "
        f"bias={'BULL' if biasBull else 'BEAR' if biasBear else 'SPLIT'} | "
        f"struct={'BULL' if bullStruct else 'BEAR' if bearStruct else 'MIX'} | "
        f"sess={'YES' if inSession else 'NO'} | "
        f"zone={'DISC' if discount else 'PREM'} | "
        f"fvg={'B' if atBullFVG else 'S' if atBearFVG else '-'} | "
        f"ob={'B' if atBullOB else 'S' if atBearOB else '-'} | "
        f"sweep={'SSL' if sweepSSL else 'BSL' if sweepBSL else '-'} | "
        f"q={'Y' if nearQ else 'N'}"
    )

    # -- Signal conditions ---------------------------------------------------
    if bullScore >= MIN_SCORE and c > o:
        sl = (
            (sweepBullLow - atr_val * SL_BUF) if sweepBullLow else
            (bullOB_lo    - atr_val * SL_BUF) if bullOB_lo    else
            (c - atr_val * ATR_SL)
        )
        tp = c + (c - sl) * TP_RR
        return ("buy", c, sl, tp, bullScore)

    if bearScore >= MIN_SCORE and c < o:
        sl = (
            (sweepBearHigh + atr_val * SL_BUF) if sweepBearHigh else
            (bearOB_hi     + atr_val * SL_BUF) if bearOB_hi     else
            (c + atr_val * ATR_SL)
        )
        tp = c - (sl - c) * TP_RR
        return ("sell", c, sl, tp, bearScore)

    return None


# ---------------------------------------------------------------------------
# Scheduler helper
# ---------------------------------------------------------------------------

def _seconds_until_next_4h_close() -> float:
    """Seconds until the next 4H UTC candle close (00,04,08,12,16,20)."""
    now = datetime.now(timezone.utc)
    current_4h = (now.hour // 4) * 4
    next_close = now.replace(hour=current_4h, minute=0, second=0, microsecond=0) + timedelta(hours=4)
    secs = (next_close - now).total_seconds()
    # Add 30s buffer so the candle is definitely closed on the data provider
    return secs + 30


# ---------------------------------------------------------------------------
# Main scanner loop
# ---------------------------------------------------------------------------

def run_scanner(client):
    """
    Background thread: wakes at every 4H candle close, scans all symbols,
    places trades when confluence score >= MIN_SCORE.
    """
    api_key = os.getenv("TWELVE_DATA_API_KEY", "")
    if not api_key:
        logger.error("[Scanner] TWELVE_DATA_API_KEY not set — scanner disabled.")
        return

    # Parse symbols from env (optional override)
    raw = os.getenv("SCANNER_SYMBOLS", "")
    if raw:
        symbols = {}
        for pair in raw.split(","):
            pair = pair.strip()
            if ":" in pair:
                td_sym, broker_sym = pair.split(":", 1)
                symbols[td_sym.strip()] = broker_sym.strip()
            else:
                symbols[pair] = DEFAULT_SYMBOLS.get(pair, pair)
    else:
        symbols = DEFAULT_SYMBOLS

    td = TDClient(apikey=api_key)
    last_signals: dict[str, object] = {}  # broker_symbol -> last bar index that fired

    logger.info(f"[Scanner] Started. Symbols: {symbols}")

    while True:
        wait = _seconds_until_next_4h_close()
        logger.info(f"[Scanner] Next scan in {wait/3600:.1f}h ({wait:.0f}s)")
        time.sleep(wait)

        logger.info("[Scanner] Scanning all symbols...")

        for td_symbol, broker_symbol in symbols.items():
            try:
                df_4h    = fetch_candles(td, td_symbol, "4h",    outputsize=300)
                df_daily = fetch_candles(td, td_symbol, "1day",  outputsize=250)

                if df_4h.empty or df_daily.empty:
                    logger.warning(f"[Scanner] {broker_symbol}: empty data returned.")
                    continue

                # Dedup: skip if we already fired on this exact bar
                last_bar = df_4h.index[-1]
                if last_signals.get(broker_symbol) == last_bar:
                    logger.info(f"[Scanner] {broker_symbol}: already processed bar {last_bar}.")
                    continue

                result = analyze(df_4h, df_daily, broker_symbol)

                if result is None:
                    logger.info(f"[Scanner] {broker_symbol}: no signal.")
                    continue

                action, entry, sl, tp, score = result

                logger.info(
                    f"[Scanner] SIGNAL {action.upper()} {broker_symbol} | "
                    f"Score: {score}/8 | Entry: {entry} | SL: {sl:.4f} | TP: {tp:.4f}"
                )

                # Refresh balance + size position
                balance = client.get_balance()
                if balance <= 0:
                    logger.warning(f"[Scanner] Balance is zero, skipping {broker_symbol}.")
                    continue

                lots = calculate_lots(balance, entry, sl, risk_pct=RISK_PCT, ticker=broker_symbol)

                order = client.place_market_order(
                    symbol=broker_symbol,
                    side=action,
                    qty=lots,
                    stop_loss=sl,
                    take_profit=tp,
                )

                last_signals[broker_symbol] = last_bar
                logger.info(f"[Scanner] Order placed for {broker_symbol}: {order}")

            except Exception as e:
                logger.error(f"[Scanner] Error on {broker_symbol}: {e}", exc_info=True)

        logger.info("[Scanner] Scan cycle complete.")


def start_scanner_thread(client):
    """Launch the scanner as a daemon background thread."""
    t = threading.Thread(target=run_scanner, args=(client,), daemon=True, name="scanner")
    t.start()
    logger.info("[Scanner] Background thread started.")
    return t
