"""
Trade Log
---------
Records every trade entry and exit.  Persists to a JSON file so it
survives bot restarts (Railway ephemeral FS — resets on redeploy, but
stays intact across crashes/restarts within a deployment).

Each trade record:
  {
    "id":           unique string (timestamp + ticker)
    "position_id":  TradeLocker position ID (filled in when matched)
    "ticker":       "GBPUSD"
    "side":         "buy" | "sell"
    "lots":         0.02
    "entry":        1.3500
    "sl":           1.3460
    "tp1":          1.3540
    "balance_at_entry": 308.50
    "opened_at":    "2026-05-25T10:30:00Z"
    "closed_at":    "2026-05-25T11:15:00Z"   (null while open)
    "exit_price":   1.3555                    (null while open)
    "pnl":          14.50                     (null while open)
    "outcome":      "win" | "loss" | "be"     (null while open)
    "exit_reason":  "tp1" | "sl" | "trailing" | "manual" | "unknown"
  }
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_LOG_PATH = os.getenv("TRADE_LOG_PATH", "/tmp/trade_log.json")


class TradeLog:
    def __init__(self):
        self._lock   = threading.Lock()
        self._trades: list[dict] = []
        self._id_seq = 0
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self):
        try:
            if os.path.exists(_LOG_PATH):
                with open(_LOG_PATH, "r") as f:
                    self._trades = json.load(f)
                logger.info(f"[TradeLog] Loaded {len(self._trades)} trades from {_LOG_PATH}")
        except Exception as e:
            logger.warning(f"[TradeLog] Could not load trade log: {e}")
            self._trades = []

    def _save(self):
        try:
            with open(_LOG_PATH, "w") as f:
                json.dump(self._trades, f, indent=2)
        except Exception as e:
            logger.warning(f"[TradeLog] Could not save trade log: {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_entry(
        self,
        ticker:   str,
        side:     str,
        lots:     float,
        entry:    float,
        sl:       float,
        tp1:      float,
        balance:  float,
        order_id: str = "",
    ) -> str:
        """Record a new trade entry.  Returns the trade ID."""
        now = datetime.now(timezone.utc)
        with self._lock:
            self._id_seq += 1
            seq = self._id_seq
        # A trailing sequence number guarantees uniqueness even when two
        # trades on the same ticker land in the same second — a real
        # scenario during a burst of alerts — which second-resolution
        # timestamps alone would silently collide on.
        trade_id = f"{now.strftime('%Y%m%dT%H%M%S')}_{ticker}_{seq}"

        record = {
            "id":               trade_id,
            "position_id":      None,
            "order_id":         order_id,
            "ticker":           ticker.upper(),
            "side":             side.lower(),
            "lots":             lots,
            "entry":            entry,
            "sl":               sl,
            "tp1":              tp1 if tp1 and tp1 > 0 else None,
            "balance_at_entry": round(balance, 2),
            "opened_at":        now.isoformat(),
            "closed_at":        None,
            "exit_price":       None,
            "pnl":              None,
            "outcome":          None,
            "exit_reason":      None,
        }

        with self._lock:
            self._trades.append(record)
            self._save()

        logger.info(f"[TradeLog] Entry logged: {ticker} {side.upper()} {lots} @ {entry}")
        return trade_id

    def try_claim_position(self, trade_id: str, position_id: str) -> bool:
        """
        Atomically link this trade to position_id — unless some other trade
        has already claimed that same position_id, in which case this call
        fails so the caller can try its next candidate position instead.

        Needed because several trades on the same ticker+side can be open
        at once (e.g. a burst of alerts within the same second): without
        this check, every one of their background linking lookups would
        independently pick the same first broker position that matches
        ticker+side, leaving the others' position_id null forever and
        their eventual closes unrecorded.
        """
        with self._lock:
            target = None
            for t in self._trades:
                if t["id"] == trade_id:
                    target = t
                elif t["position_id"] == position_id:
                    return False
            if target is None or target["position_id"] is not None:
                return False
            target["position_id"] = position_id
            self._save()
            return True

    def reconcile_orphans(self, closed_trades: list[dict]) -> dict:
        """
        Close out log entries that never got a position_id (e.g. a burst of
        alerts raced past the linking window before that was fixed) by
        matching them against TradeLocker's own order history — broker
        truth, independent of this log — instead of leaving them stuck
        "open" forever.

        closed_trades: TradeLockerClient.get_closed_trades() output, each
        {name, side, qty, openPrice, closePrice, move, outcome,
        exit_reason, closedAt}.

        Matches by ticker + side + closest entry price among broker trades
        that closed at/after this orphan's own opened_at, each broker trade
        consumed at most once. Dollar P&L is reconstructed via contract
        size (same approach used elsewhere in this codebase for approximate
        P&L) — an approximation, not the broker's authoritative figure, but
        far better than an orphan sitting open indefinitely with no exit
        data at all.

        Returns {"matched": [...ids], "unmatched": [...ids]} — "unmatched"
        orphans had no broker trade to pair with (outside the fetched
        window, or genuinely still open) and are left untouched.
        """
        from risk import _contract_size

        with self._lock:
            orphans = [t for t in self._trades
                       if t["position_id"] is None and t["closed_at"] is None]
            used_idx: set = set()
            matched_ids = []

            for orphan in orphans:
                try:
                    opened_ms = datetime.fromisoformat(orphan["opened_at"]).timestamp() * 1000
                except Exception:
                    continue

                candidates = [
                    (i, c) for i, c in enumerate(closed_trades)
                    if i not in used_idx
                    and str(c.get("name", "")).upper() == orphan["ticker"]
                    and c.get("side") == orphan["side"]
                    and (c.get("closedAt") or 0) >= opened_ms - 2000  # small clock-skew tolerance
                ]
                if not candidates:
                    continue
                i, best = min(candidates, key=lambda ic: abs(ic[1]["openPrice"] - orphan["entry"]))
                used_idx.add(i)

                contract = _contract_size(orphan["ticker"])
                pnl = (
                    round(best["move"] * orphan["lots"] * contract *
                          (1 if orphan["side"] == "buy" else -1), 2)
                    if contract else None
                )

                orphan["closed_at"]   = datetime.fromtimestamp(
                    best["closedAt"] / 1000, tz=timezone.utc
                ).isoformat()
                orphan["exit_price"]  = best["closePrice"]
                orphan["pnl"]         = pnl
                orphan["outcome"]     = "be" if (pnl is not None and abs(pnl) < 0.01) else best["outcome"]
                orphan["exit_reason"] = best["exit_reason"]
                matched_ids.append(orphan["id"])

            self._save()
            unmatched_ids = [t["id"] for t in orphans if t["id"] not in matched_ids]
            return {"matched": matched_ids, "unmatched": unmatched_ids}

    def log_exit(
        self,
        position_id:  str,
        exit_price:   float,
        pnl:          float,
        exit_reason:  str = "unknown",
    ):
        """
        Record the close of a trade.
        Matches by position_id — if no match found (manual trade not logged
        at entry) a new record is created with partial data.
        """
        now = datetime.now(timezone.utc)
        outcome = "be" if abs(pnl) < 0.01 else ("win" if pnl > 0 else "loss")

        with self._lock:
            # Find most recent open trade with this position_id
            matched = None
            for t in reversed(self._trades):
                if t["position_id"] == position_id and t["closed_at"] is None:
                    matched = t
                    break

            if matched:
                matched["closed_at"]   = now.isoformat()
                matched["exit_price"]  = round(exit_price, 5)
                matched["pnl"]         = round(pnl, 2)
                matched["outcome"]     = outcome
                matched["exit_reason"] = exit_reason
            else:
                # Manual trade — create a partial record for it
                self._trades.append({
                    "id":               f"manual_{position_id}",
                    "position_id":      position_id,
                    "order_id":         None,
                    "ticker":           "UNKNOWN",
                    "side":             "unknown",
                    "lots":             None,
                    "entry":            None,
                    "sl":               None,
                    "tp1":              None,
                    "balance_at_entry": None,
                    "opened_at":        None,
                    "closed_at":        now.isoformat(),
                    "exit_price":       round(exit_price, 5),
                    "pnl":              round(pnl, 2),
                    "outcome":          outcome,
                    "exit_reason":      exit_reason,
                })

            self._save()

        logger.info(
            f"[TradeLog] Exit logged: position {position_id} | "
            f"P&L=${pnl:+.2f} | {outcome.upper()} | reason={exit_reason}"
        )

    def get_open_trades(self) -> list[dict]:
        """All trades not yet closed."""
        with self._lock:
            return [t for t in self._trades if t["closed_at"] is None]

    def get_recent(self, n: int = 50) -> list[dict]:
        """Most recent n closed trades, newest first."""
        with self._lock:
            closed = [t for t in self._trades if t["closed_at"] is not None]
        return sorted(closed, key=lambda t: t["closed_at"] or "", reverse=True)[:n]

    def summary(self) -> dict:
        """Aggregate stats across all closed trades."""
        with self._lock:
            closed = [t for t in self._trades if t["closed_at"] is not None]

        if not closed:
            return {
                "total_trades": 0,
                "message": "No closed trades yet.",
            }

        wins      = [t for t in closed if t["outcome"] == "win"]
        losses    = [t for t in closed if t["outcome"] == "loss"]
        breakeven = [t for t in closed if t["outcome"] == "be"]
        total_pnl = sum(t["pnl"] for t in closed if t["pnl"] is not None)
        win_rate  = len(wins) / len(closed) * 100 if closed else 0

        # Per-ticker breakdown
        tickers: dict[str, dict] = {}
        for t in closed:
            sym = t["ticker"]
            if sym not in tickers:
                tickers[sym] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
            tickers[sym]["trades"] += 1
            if t["outcome"] == "win":
                tickers[sym]["wins"] += 1
            elif t["outcome"] == "loss":
                tickers[sym]["losses"] += 1
            if t["pnl"] is not None:
                tickers[sym]["pnl"] += t["pnl"]

        # Best and worst performing pairs
        by_pnl  = sorted(tickers.items(), key=lambda x: x[1]["pnl"], reverse=True)
        best    = by_pnl[0]  if by_pnl else None
        worst   = by_pnl[-1] if by_pnl else None

        return {
            "total_trades":   len(closed),
            "wins":           len(wins),
            "losses":         len(losses),
            "breakeven":      len(breakeven),
            "win_rate_pct":   round(win_rate, 1),
            "total_pnl":      round(total_pnl, 2),
            "open_trades":    len(self.get_open_trades()),
            "best_pair":      {"ticker": best[0],  "pnl": round(best[1]["pnl"], 2)}  if best  else None,
            "worst_pair":     {"ticker": worst[0], "pnl": round(worst[1]["pnl"], 2)} if worst else None,
            "by_ticker":      {
                sym: {
                    "trades":   d["trades"],
                    "wins":     d["wins"],
                    "losses":   d["losses"],
                    "win_rate": round(d["wins"] / d["trades"] * 100, 1) if d["trades"] else 0,
                    "pnl":      round(d["pnl"], 2),
                }
                for sym, d in sorted(tickers.items(), key=lambda x: x[1]["pnl"], reverse=True)
            },
        }
