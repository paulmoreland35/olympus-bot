"""
Trailing Stop Manager
---------------------
Polls every open TradeLocker position (bot-placed OR manual) and:

  1. Once price reaches the HALFWAY point between entry and TP1:
       → Move SL to breakeven (entry price)

  2. After breakeven is active, trail the SL behind current price
     at the same distance the original SL was from entry.
     (Only moves SL in the profitable direction — never backwards.)

Current price is derived from unrealised P&L when not supplied directly
by the broker:
  BUY:  current_price = entry + pnl / (qty × contract_size)
  SELL: current_price = entry − pnl / (qty × contract_size)
"""

import logging
import os
import threading
from typing import Optional

from risk import _contract_size   # reuse existing contract size table

logger = logging.getLogger(__name__)

# Fraction of the entry-to-TP distance price must reach before SL moves to
# breakeven. 0.5 = halfway to TP. Tunable per-deployment via env without a
# code change.
BREAKEVEN_TP_RATIO = float(os.getenv("BREAKEVEN_TP_RATIO", "0.5"))


class TrailingStopManager:
    """
    Thread-safe manager.  One instance lives for the lifetime of the app.
    Call process(positions) on every poll tick — it returns a list of
    (position_id, new_sl) tuples that the caller should apply.

    register_tp() lets the webhook pre-load TP1 for a position so trailing
    works even when the broker rejected the TP on the order (TP dropped case).
    """

    def __init__(self):
        self._lock  = threading.Lock()
        # position_id / order_id → state dict
        self._state: dict[str, dict] = {}
        # order_id → tp1 (pre-registered before position_id is known)
        self._pending_tp: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_tp(self, order_id: str, tp1: float):
        """
        Pre-register a TP1 value by order_id so trailing can work even when
        the broker didn't attach the TP to the position (TP dropped case).
        Called from the webhook right after the order is placed.
        """
        if tp1 and tp1 > 0:
            with self._lock:
                self._pending_tp[order_id] = tp1
            logger.info(f"[Trailing] Pre-registered TP1={tp1} for order {order_id}")

    def process(self, positions: list[dict]) -> list[tuple[str, float]]:
        """
        Evaluate every open position and return SL modifications needed.

        Args:
            positions: list of normalised position dicts from
                       TradeLockerClient.get_open_positions()

        Returns:
            List of (position_id, new_sl_price) to apply.
        """
        updates = []
        open_ids = set()

        for pos in positions:
            pos_id = pos.get("id", "")
            if not pos_id:
                continue
            open_ids.add(pos_id)

            result = self._evaluate(pos)
            if result is not None:
                updates.append((pos_id, result))

        # Clean up state for positions that have closed
        with self._lock:
            for pid in list(self._state.keys()):
                if pid not in open_ids:
                    ticker = self._state[pid].get("ticker", pos_id)
                    logger.info(f"[Trailing] Position closed — removing state: {ticker} ({pid})")
                    del self._state[pid]

        return updates

    def status(self) -> dict:
        """Return a human-readable summary of all tracked positions."""
        with self._lock:
            positions = []
            for pid, s in self._state.items():
                positions.append({
                    "id":              pid,
                    "ticker":          s.get("ticker", "?"),
                    "side":            s.get("side", "?"),
                    "entry":           s.get("entry", 0),
                    "activation_dist": round(s.get("activation_dist", 0), 5),
                    "trail_dist":      round(s.get("trail_dist", 0), 5),
                    "activated":       s.get("activated", False),
                    "current_sl":  s.get("last_sl", 0),
                })
            return {"tracked": len(positions), "positions": positions}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_current_price(self, pos: dict) -> Optional[float]:
        """
        Derive current market price from position data.
        Tries direct field first, then falls back to PnL calculation.
        """
        # Some brokers supply current price directly
        for field in ("currentPrice", "markPrice", "livePrice", "price"):
            val = pos.get(field)
            if val and float(val) > 0:
                return float(val)

        # Fall back: compute from unrealised PnL
        entry    = float(pos.get("openPrice", 0) or 0)
        qty      = float(pos.get("qty", 0)       or 0)
        pnl      = float(pos.get("unrealisedPnl", 0) or 0)
        ticker   = str(pos.get("name", ""))
        side     = str(pos.get("side", "")).lower()

        if entry <= 0 or qty <= 0:
            return None

        contract = _contract_size(ticker)
        if contract <= 0:
            return None

        dollar_per_unit = qty * contract
        if dollar_per_unit == 0:
            return None

        price_move = pnl / dollar_per_unit
        if side == "buy":
            return entry + price_move
        else:
            return entry - price_move

    def _evaluate(self, pos: dict) -> Optional[float]:
        """
        Core logic for one position.
        Returns new SL price if an update is needed, else None.

        Activation: price must reach BREAKEVEN_TP_RATIO of the way from
        entry to TP (default 50%). Falls back to a 1× SL-distance trigger
        when the position has no TP (e.g. broker dropped it on order entry).
        """
        pos_id      = pos["id"]
        side        = pos.get("side", "").lower()
        entry       = pos.get("openPrice", 0)
        current_sl  = pos.get("stopLoss", 0)
        tp          = pos.get("takeProfit", 0)
        ticker      = pos.get("name", pos_id)

        if not current_sl or current_sl <= 0:
            return None
        if not entry or entry <= 0:
            return None
        if side not in ("buy", "sell"):
            return None

        current_price = self._get_current_price(pos)
        if current_price is None or current_price <= 0:
            logger.debug(f"[Trailing] {ticker}: could not determine current price — skipping")
            return None

        sl_dist = abs(entry - current_sl)
        if sl_dist == 0:
            return None

        # Activation trigger: BREAKEVEN_TP_RATIO of the way from entry to TP.
        # Trailing distance (once active) always stays the original SL distance
        # regardless of where activation happens.
        if tp and tp > 0:
            activation_dist = abs(tp - entry) * BREAKEVEN_TP_RATIO
        else:
            activation_dist = sl_dist
        if side == "buy":
            activation_price = entry + activation_dist
        else:
            activation_price = entry - activation_dist

        with self._lock:
            state = self._state.get(pos_id)

            if state is None:
                activated = False

                # Already past activation on first poll — activate immediately
                if (side == "buy"  and current_price >= activation_price) or \
                   (side == "sell" and current_price <= activation_price):
                    activated = True
                    logger.info(
                        f"[Trailing] {ticker} ({pos_id}): past activation on first poll "
                        f"— activating immediately. entry={entry} activation={activation_price:.5f} "
                        f"price={current_price:.5f}"
                    )

                self._state[pos_id] = {
                    "ticker":          ticker,
                    "side":            side,
                    "entry":           entry,
                    "activation_dist": activation_dist,
                    "trail_dist":      sl_dist,
                    "activated":       activated,
                    "last_sl":         current_sl,
                }
                state = self._state[pos_id]

                if not activated:
                    return None

            # Recalculate activation_price using stored activation_dist (locked in at init)
            activation_dist  = state["activation_dist"]
            if side == "buy":
                activation_price = state["entry"] + activation_dist
            else:
                activation_price = state["entry"] - activation_dist

            # ----- Phase 1: waiting for breakeven trigger -----
            if not state["activated"]:
                reached = (side == "buy"  and current_price >= activation_price) or \
                          (side == "sell" and current_price <= activation_price)

                if not reached:
                    return None

                state["activated"] = True
                logger.info(
                    f"[Trailing] {ticker}: breakeven trigger reached! "
                    f"entry={entry} activation={activation_price:.5f} price={current_price:.5f} "
                    f"→ moving SL to breakeven ({entry}), trail_dist={state['trail_dist']:.5f}"
                )
                new_sl = entry

            # ----- Phase 2: trailing after breakeven -----
            else:
                trail_dist = state["trail_dist"]
                if side == "buy":
                    new_sl = current_price - trail_dist
                    # Only move SL upward (never backwards)
                    if new_sl <= current_sl:
                        return None
                else:
                    new_sl = current_price + trail_dist
                    # Only move SL downward (never backwards)
                    if new_sl >= current_sl:
                        return None

            new_sl = round(new_sl, 5)
            state["last_sl"] = new_sl

            # Sanity check — SL must be on the correct side of current price
            if side == "buy"  and new_sl >= current_price:
                logger.warning(f"[Trailing] {ticker}: computed SL {new_sl} >= current price {current_price:.5f} — skipping")
                return None
            if side == "sell" and new_sl <= current_price:
                logger.warning(f"[Trailing] {ticker}: computed SL {new_sl} <= current price {current_price:.5f} — skipping")
                return None

            return new_sl
