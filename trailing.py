"""
Trailing Stop Manager
---------------------
Polls every open TradeLocker position (bot-placed OR manual) and moves its
SL through two staged lock-in points on the way to TP1:

  1. Once price reaches BREAKEVEN_TP_RATIO of the way to TP (default 30%):
       → Move SL to breakeven (entry price), then trail it behind price
         at the original SL distance.

  2. Once price reaches LOCK_IN_TP_RATIO of the way to TP (default 50%):
       → Move SL up to the price level stage 1 triggered at (locking in
         that much profit), then continue trailing behind price at the
         same distance until TP1 is hit.

Trailing only ever moves SL in the profitable direction — never backwards.
Without a TP on the position (e.g. broker dropped it), falls back to a
single-stage 1× SL-distance breakeven trigger, since there's no TP to
measure percentages against.

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
# breakeven (stage 1). 0.3 = 30% of the way to TP. Tunable per-deployment
# via env without a code change.
BREAKEVEN_TP_RATIO = float(os.getenv("BREAKEVEN_TP_RATIO", "0.3"))

# Fraction of the entry-to-TP distance price must reach before SL locks in
# to the stage-1 (breakeven-trigger) price level (stage 2). Must be greater
# than BREAKEVEN_TP_RATIO. 0.5 = 50% of the way to TP.
LOCK_IN_TP_RATIO = float(os.getenv("LOCK_IN_TP_RATIO", "0.5"))


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

    def register_tp(self, position_id: str, tp1: float):
        """
        Pre-register a TP1 value by broker position_id so trailing can work
        even when the broker didn't attach the TP to the position (TP
        dropped case). Called once the webhook has resolved the position_id
        for a freshly-placed order (see _link_trade_position in main.py) —
        _evaluate() consumes this the first time it sees that position with
        no takeProfit of its own.
        """
        if tp1 and tp1 > 0:
            with self._lock:
                self._pending_tp[position_id] = tp1
            logger.info(f"[Trailing] Pre-registered TP1={tp1} for position {position_id}")

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
                    "id":            pid,
                    "ticker":        s.get("ticker", "?"),
                    "side":          s.get("side", "?"),
                    "entry":         s.get("entry", 0),
                    "level_a_price": round(s.get("level_a_price", 0) or 0, 5),
                    "level_b_price": round(s["level_b_price"], 5) if s.get("level_b_price") else None,
                    "trail_dist":    round(s.get("trail_dist", 0), 5),
                    "stage":         s.get("stage", 0),
                    "current_sl":    s.get("last_sl", 0),
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
        Core logic for one position. Returns new SL price if an update is
        needed, else None.

        Two staged lock-in points on the way to TP1:
          Stage 0 -> 1 at BREAKEVEN_TP_RATIO (default 30%): SL -> breakeven.
          Stage 1 -> 2 at LOCK_IN_TP_RATIO   (default 50%): SL -> the price
              level stage 1 triggered at.
        Stages 1 and 2 both trail behind price at the original SL distance
        in between triggers. Falls back to a single-stage 1x SL-distance
        breakeven trigger when the position has no TP.
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

        def price_at(dist):
            return entry + dist if side == "buy" else entry - dist

        def reached(target):
            return (side == "buy" and current_price >= target) or \
                   (side == "sell" and current_price <= target)

        with self._lock:
            state = self._state.get(pos_id)

            if state is None:
                # First time seeing this position. sl_dist is only measured
                # here, from the ORIGINAL SL — it must never be recomputed
                # from current_sl on later polls, since current_sl equals
                # entry right after breakeven, which would make it 0 and
                # break trailing permanently from that point on.
                sl_dist = abs(entry - current_sl)
                if sl_dist == 0:
                    return None

                if not (tp and tp > 0):
                    # Broker dropped the TP on this position (e.g. rejected
                    # at order time because price had moved) — fall back to
                    # the TP1 the webhook pre-registered for it, if any.
                    tp = self._pending_tp.pop(pos_id, 0)
                has_tp = bool(tp and tp > 0)
                if has_tp:
                    tp_dist = abs(tp - entry)
                    level_a_dist = tp_dist * BREAKEVEN_TP_RATIO
                    level_b_dist = tp_dist * LOCK_IN_TP_RATIO
                else:
                    # No TP on the position — fall back to the simple 1:1
                    # SL-distance single-stage trigger. There's no TP to
                    # measure a % against, so there's no second stage either.
                    level_a_dist = sl_dist
                    level_b_dist = None

                level_a_price = price_at(level_a_dist)
                level_b_price = price_at(level_b_dist) if level_b_dist is not None else None

                # Figure out which stage it should already be in, in case
                # price gapped past one or both triggers before this poll.
                if level_b_price is not None and reached(level_b_price):
                    stage, new_sl = 2, level_a_price
                    logger.info(
                        f"[Trailing] {ticker} ({pos_id}): already past "
                        f"{LOCK_IN_TP_RATIO*100:.0f}%-to-TP on first poll — "
                        f"locking SL at the {BREAKEVEN_TP_RATIO*100:.0f}% level "
                        f"({new_sl:.5f})."
                    )
                elif reached(level_a_price):
                    stage, new_sl = 1, entry
                    logger.info(
                        f"[Trailing] {ticker} ({pos_id}): already past "
                        f"{BREAKEVEN_TP_RATIO*100:.0f}%-to-TP on first poll — "
                        f"moving SL to breakeven ({new_sl:.5f})."
                    )
                else:
                    stage, new_sl = 0, None

                self._state[pos_id] = {
                    "ticker":        ticker,
                    "side":          side,
                    "entry":         entry,
                    "level_a_price": level_a_price,
                    "level_b_price": level_b_price,
                    "trail_dist":    sl_dist,
                    "stage":         stage,
                    "last_sl":       new_sl if new_sl is not None else current_sl,
                }
                if new_sl is None:
                    return None
                new_sl = round(new_sl, 5)
                self._state[pos_id]["last_sl"] = new_sl
                return new_sl

            stage         = state["stage"]
            level_a_price = state["level_a_price"]
            level_b_price = state["level_b_price"]
            trail_dist    = state["trail_dist"]

            # ----- Stage 0: waiting for the breakeven trigger -----
            if stage == 0:
                if not reached(level_a_price):
                    return None
                state["stage"] = 1
                new_sl = entry
                logger.info(
                    f"[Trailing] {ticker}: {BREAKEVEN_TP_RATIO*100:.0f}%-to-TP reached! "
                    f"Moving SL to breakeven ({entry:.5f})."
                )

            # ----- Stage 1: breakeven active, trailing, watching for the lock-in trigger -----
            elif stage == 1:
                trail_candidate = current_price - trail_dist if side == "buy" \
                                   else current_price + trail_dist

                if level_b_price is not None and reached(level_b_price):
                    state["stage"] = 2
                    # Lock in at least the stage-1 level, but don't give back
                    # ground the ordinary trail has already earned.
                    new_sl = max(trail_candidate, level_a_price) if side == "buy" \
                             else min(trail_candidate, level_a_price)
                    logger.info(
                        f"[Trailing] {ticker}: {LOCK_IN_TP_RATIO*100:.0f}%-to-TP reached! "
                        f"Locking SL to {new_sl:.5f}."
                    )
                else:
                    new_sl = trail_candidate
                    if side == "buy" and new_sl <= current_sl:
                        return None
                    if side == "sell" and new_sl >= current_sl:
                        return None

            # ----- Stage 2: past the lock-in trigger, trailing until TP1 -----
            else:
                if side == "buy":
                    new_sl = current_price - trail_dist
                    if new_sl <= current_sl:
                        return None
                else:
                    new_sl = current_price + trail_dist
                    if new_sl >= current_sl:
                        return None

            new_sl = round(new_sl, 5)

            # Never actually move backward regardless of stage transitions above
            if side == "buy" and new_sl <= current_sl:
                return None
            if side == "sell" and new_sl >= current_sl:
                return None

            state["last_sl"] = new_sl

            # Sanity check — SL must be on the correct side of current price
            if side == "buy"  and new_sl >= current_price:
                logger.warning(f"[Trailing] {ticker}: computed SL {new_sl} >= current price {current_price:.5f} — skipping")
                return None
            if side == "sell" and new_sl <= current_price:
                logger.warning(f"[Trailing] {ticker}: computed SL {new_sl} <= current price {current_price:.5f} — skipping")
                return None

            return new_sl
