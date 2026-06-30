"""
Gold Scalp Backtest (dry-run / analysis only — places NO trades)
----------------------------------------------------------------
Simulates a momentum scalping strategy on XAUUSD over recent intraday data
so we can measure whether catching small moves for a fixed $ target is
actually profitable AFTER spread — before risking a single real dollar.

Strategy (simple, transparent, tunable):
  - EMA(fast) vs EMA(slow) on the chosen intraday timeframe sets direction.
  - Entry on the EMA crossover (one trade per crossover), in the cross
    direction.
  - Fixed dollar take-profit and stop-loss (converted to a gold price move
    via lot size). Spread is charged on every trade.
  - Each trade is walked forward bar-by-bar: whichever of TP/SL the bar
    range hits first decides the outcome (SL assumed first if a bar hits
    both — conservative). Times out after max_hold bars (exit at close).

Nothing here calls the broker. Results are returned as JSON for /scalp-backtest.
"""

import os
import logging

import numpy as np
import pandas as pd
from twelvedata import TDClient

from scanner import fetch_candles

logger = logging.getLogger(__name__)

GOLD_CONTRACT = 100  # 1.00 lot XAUUSD = 100 oz -> $1 move = $100 per lot


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def run_backtest(
    interval: str = "5min",
    bars: int = 1000,
    ema_fast: int = 9,
    ema_slow: int = 21,
    tp_usd: float = 5.0,
    sl_usd: float = 5.0,
    lot: float = 0.10,
    spread_price: float = 0.30,   # gold $ spread charged per round-trip trade
    max_hold: int = 24,           # bars before timeout exit
    symbol: str = "XAU/USD",
) -> dict:
    api_key = os.getenv("TWELVE_DATA_API_KEY", "")
    if not api_key:
        return {"error": "TWELVE_DATA_API_KEY not set — cannot fetch data."}

    try:
        td = TDClient(apikey=api_key)
        df = fetch_candles(td, symbol, interval, outputsize=min(bars, 5000))
    except Exception as e:
        return {"error": f"Could not fetch candles: {e}"}

    if df is None or df.empty or len(df) < ema_slow + 5:
        return {"error": "Not enough candle data returned."}

    df = df.reset_index(drop=True)
    df["ema_f"] = _ema(df["close"], ema_fast)
    df["ema_s"] = _ema(df["close"], ema_slow)

    # Convert dollar targets to gold price moves for this lot size
    per_dollar_move = lot * GOLD_CONTRACT          # $ per $1 gold move
    if per_dollar_move <= 0:
        return {"error": "lot must be > 0"}
    tp_move = tp_usd / per_dollar_move
    sl_move = sl_usd / per_dollar_move
    spread_cost = spread_price * per_dollar_move    # $ cost charged per trade

    trades = []
    i = ema_slow + 1
    n = len(df)
    while i < n - 1:
        prev_f, prev_s = df["ema_f"].iloc[i - 1], df["ema_s"].iloc[i - 1]
        cur_f,  cur_s  = df["ema_f"].iloc[i],     df["ema_s"].iloc[i]

        side = None
        if prev_f <= prev_s and cur_f > cur_s:
            side = "buy"
        elif prev_f >= prev_s and cur_f < cur_s:
            side = "sell"

        if side is None:
            i += 1
            continue

        entry = float(df["close"].iloc[i])  # enter at signal-bar close
        if side == "buy":
            tp_price, sl_price = entry + tp_move, entry - sl_move
        else:
            tp_price, sl_price = entry - tp_move, entry + sl_move

        outcome, exit_price, held = None, None, 0
        for j in range(i + 1, min(i + 1 + max_hold, n)):
            held = j - i
            hi, lo = float(df["high"].iloc[j]), float(df["low"].iloc[j])
            if side == "buy":
                hit_sl = lo <= sl_price
                hit_tp = hi >= tp_price
            else:
                hit_sl = hi >= sl_price
                hit_tp = lo <= tp_price
            if hit_sl and hit_tp:
                outcome, exit_price = "loss", sl_price   # conservative
                break
            if hit_sl:
                outcome, exit_price = "loss", sl_price
                break
            if hit_tp:
                outcome, exit_price = "win", tp_price
                break

        if outcome is None:
            # timed out — mark to market at last bar close
            exit_idx = min(i + max_hold, n - 1)
            exit_price = float(df["close"].iloc[exit_idx])
            held = exit_idx - i

        # P&L in $ minus spread
        direction = 1 if side == "buy" else -1
        gross = (exit_price - entry) * direction * per_dollar_move
        net = gross - spread_cost
        if outcome is None:
            outcome = "win" if net > 0 else "loss"

        trades.append({"side": side, "entry": round(entry, 2),
                       "exit": round(exit_price, 2), "net": round(net, 2),
                       "held_bars": held, "outcome": outcome})

        # don't re-enter while in the trade — skip past the exit
        i += max(held, 1)

    if not trades:
        return {"error": "No crossover signals in the sample."}

    wins   = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    net_total = sum(t["net"] for t in trades)
    gross_win = sum(t["net"] for t in wins)
    gross_loss = abs(sum(t["net"] for t in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss else None

    # max consecutive losses + simple equity drawdown
    eq, peak, max_dd, cur_streak, max_streak = 0.0, 0.0, 0.0, 0, 0
    for t in trades:
        eq += t["net"]
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
        if t["net"] <= 0:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 0

    span_bars = len(df)
    minutes_per_bar = {"1min": 1, "5min": 5, "15min": 15, "1h": 60}.get(interval, 5)
    span_days = round(span_bars * minutes_per_bar / 1440, 1)

    return {
        "params": {
            "interval": interval, "bars": span_bars, "span_days": span_days,
            "ema_fast": ema_fast, "ema_slow": ema_slow,
            "tp_usd": tp_usd, "sl_usd": sl_usd, "lot": lot,
            "spread_price": spread_price, "spread_cost_per_trade": round(spread_cost, 2),
            "max_hold_bars": max_hold,
        },
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1),
        "net_pnl_usd": round(net_total, 2),
        "avg_win_usd": round(gross_win / len(wins), 2) if wins else 0,
        "avg_loss_usd": round(-gross_loss / len(losses), 2) if losses else 0,
        "profit_factor": profit_factor,
        "max_consecutive_losses": max_streak,
        "max_drawdown_usd": round(max_dd, 2),
        "est_trades_per_day": round(len(trades) / span_days, 1) if span_days else None,
        "est_net_per_day_usd": round(net_total / span_days, 2) if span_days else None,
        "verdict": ("EDGE — net positive after spread" if net_total > 0
                    else "NO EDGE — net negative after spread"),
    }
