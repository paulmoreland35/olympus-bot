"""
TradeLocker REST API Client
Handles authentication, account info, instrument lookup, and order placement.

Key discoveries from live API inspection:
  - Auth endpoint returns accountBalance (not balance)
  - Trade URLs use account "id" (e.g. 682270), not accNum
  - accNum (e.g. "1") goes in the request HEADER
  - Instruments are dicts with id, name, routes: [{id, type}]
  - TRADE route is the one with type == "TRADE"
"""

import os
import requests
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class TradeLockerClient:
    def __init__(self, base_url: str, email: str, password: str, server: str):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.server = server

        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.account_id: Optional[str] = None   # used in URL paths (e.g. "682270")
        self.acc_num: Optional[str] = None       # used in accNum header (e.g. "1")
        self.balance: float = 0.0

        self.session = requests.Session()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self):
        """Obtain JWT access token from TradeLocker."""
        url = f"{self.base_url}/auth/jwt/token"
        payload = {
            "email": self.email,
            "password": self.password,
            "server": self.server,
        }
        resp = self.session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        self.access_token = data.get("accessToken") or data.get("access_token")
        self.refresh_token = data.get("refreshToken") or data.get("refresh_token")

        if not self.access_token:
            raise ValueError(f"No access token in auth response: {data}")

        self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})
        logger.info("TradeLocker authentication successful.")
        self._load_account()

    def refresh_access_token(self):
        """Refresh the JWT token using the refresh token."""
        url = f"{self.base_url}/auth/jwt/refresh"
        resp = self.session.post(url, json={"refreshToken": self.refresh_token}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data.get("accessToken") or data.get("access_token")
        self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})
        logger.info("Access token refreshed.")

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def _load_account(self):
        """Fetch accounts and cache account_id, acc_num, and balance."""
        url = f"{self.base_url}/auth/jwt/all-accounts"
        resp = self.session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        logger.info(f"Raw all-accounts response: {data}")
        accounts = data if isinstance(data, list) else data.get("accounts", [])
        if not accounts:
            raise ValueError("No accounts found on this TradeLocker profile.")

        target_acc_num = (os.getenv("TL_ACC_NUM") or os.getenv("TL_ACCOUNT_NUM", "")).strip()
        if target_acc_num:
            # Match against any field that might hold the broker account number
            def _matches(a):
                t = str(target_acc_num)
                return (
                    str(a.get("accNum", "")) == t
                    or str(a.get("accountNumber", "")) == t
                    or str(a.get("number", "")) == t
                    or str(a.get("login", "")) == t
                    or str(a.get("id", "")) == t
                )
            matched = [a for a in accounts if _matches(a)]
            if matched:
                account = matched[0]
                logger.info(f"Using targeted account TL_ACC_NUM={target_acc_num} -> accNum={account.get('accNum')}")
            else:
                # Log all account fields so we can diagnose the mismatch
                for i, a in enumerate(accounts):
                    logger.error(f"Account[{i}] fields: {a}")
                available = [str(a.get("accNum", "?")) for a in accounts]
                raise ValueError(f"Account {target_acc_num} not found. Available accNums: {available}")
        else:
            account = accounts[0]

        # "id" is used in URL paths; "accNum" goes in the request header
        self.account_id = str(account.get("id", ""))
        self.acc_num    = str(account.get("accNum", "1"))

        # Balance field is "accountBalance" in the live API
        self.balance = float(
            account.get("accountBalance")
            or account.get("balance")
            or account.get("equity")
            or 0.0
        )

        # Set accNum header — required for all trade endpoints
        self.session.headers.update({"accNum": self.acc_num})

        logger.info(
            f"Account loaded — id: {self.account_id} | accNum: {self.acc_num} | "
            f"balance: ${self.balance:,.2f}"
        )

    def get_balance(self) -> float:
        """Refresh and return current account balance."""
        try:
            url = f"{self.base_url}/auth/jwt/all-accounts"
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            accounts = data if isinstance(data, list) else data.get("accounts", [])
            if accounts:
                target_num = (os.getenv("TL_ACC_NUM") or os.getenv("TL_ACCOUNT_NUM", "")).strip()
                account = next((a for a in accounts if str(a.get("accNum")) == target_num), accounts[0]) if target_num else accounts[0]
                self.balance = float(
                    account.get("accountBalance")
                    or account.get("balance")
                    or account.get("equity")
                    or self.balance
                )
        except Exception as e:
            logger.warning(f"Could not refresh balance, using cached ${self.balance:,.2f}: {e}")
        return self.balance

    # ------------------------------------------------------------------
    # Instruments
    # ------------------------------------------------------------------

    def get_instrument_id(self, symbol: str) -> tuple[int, int, int]:
        """
        Look up instrument details by symbol name.
        Returns (tradableInstrumentId, instrumentId, routeId).
        """
        url = f"{self.base_url}/trade/accounts/{self.account_id}/instruments"
        resp = self.session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        instruments = data.get("d", {}).get("instruments", [])
        symbol_upper = symbol.upper()

        for inst in instruments:
            inst_name = str(inst.get("name", "")).upper()
            if inst_name == symbol_upper:
                tradable_id = int(inst.get("tradableInstrumentId", 0))
                inst_id     = int(inst.get("id", 0))

                # Find TRADE route from routes list
                routes = inst.get("routes", [])
                trade_route_id = None
                for route in routes:
                    if isinstance(route, dict) and route.get("type") == "TRADE":
                        trade_route_id = int(route["id"])
                        break

                if trade_route_id is None:
                    raise ValueError(f"No TRADE route found for instrument '{symbol}'")

                logger.info(
                    f"Instrument: {symbol} tradableId={tradable_id} "
                    f"id={inst_id} routeId={trade_route_id}"
                )
                return tradable_id, inst_id, trade_route_id

        raise ValueError(
            f"Instrument '{symbol}' not found. "
            f"Check the symbol name exactly as shown in TradeLocker."
        )

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_open_positions(self) -> list[dict]:
        """
        Fetch all open positions and return as a list of normalised dicts.

        TradeLocker returns positions in a columnar format:
          {"d": {"columns": ["id","side",...], "data": [[...],[...]]}}

        Each dict will always contain at minimum:
          id, side, qty, openPrice, stopLoss, takeProfit, unrealisedPnl,
          tradableInstrumentId, name (symbol)
        """
        url = f"{self.base_url}/trade/accounts/{self.account_id}/positions"
        resp = self.session.get(url, timeout=15)
        if resp.status_code == 401:
            self.refresh_access_token()
            resp = self.session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        d = data.get("d", data)
        columns = d.get("columns", [])
        rows    = d.get("data",    [])

        if columns and rows:
            # Columnar format — zip each row with the column headers
            positions = [dict(zip(columns, row)) for row in rows]
        elif isinstance(d, list):
            positions = d
        else:
            positions = []

        # Normalise key names so the rest of the code doesn't care about
        # minor API variations (e.g. "avgPrice" vs "openPrice")
        normalised = []
        for p in positions:
            pos = dict(p)
            # Entry price
            if "openPrice" not in pos:
                pos["openPrice"] = pos.get("avgPrice") or pos.get("price") or 0
            # SL / TP — may be absent (null) on positions with no SL/TP set
            pos["stopLoss"]   = float(pos.get("stopLoss")   or 0)
            pos["takeProfit"] = float(pos.get("takeProfit") or 0)
            pos["openPrice"]  = float(pos.get("openPrice")  or 0)
            pos["qty"]        = float(pos.get("qty")        or 0)
            pos["unrealisedPnl"] = float(pos.get("unrealisedPnl") or
                                         pos.get("unrealizedPnl") or 0)
            pos["side"] = str(pos.get("side", "")).lower()
            pos["id"]   = str(pos.get("id", ""))
            # Symbol name — may be in "name" or "symbol" field
            if "name" not in pos:
                pos["name"] = pos.get("symbol", "")
            normalised.append(pos)

        logger.debug(f"Fetched {len(normalised)} open positions.")
        return normalised

    def _get(self, url: str, timeout: int = 20):
        """GET with one automatic token refresh on 401."""
        resp = self.session.get(url, timeout=timeout)
        if resp.status_code == 401:
            self.refresh_access_token()
            resp = self.session.get(url, timeout=timeout)
        return resp

    def get_instrument_name_map(self) -> dict:
        """Returns {tradableInstrumentId(str): symbol_name} for this account."""
        try:
            url = f"{self.base_url}/trade/accounts/{self.account_id}/instruments"
            resp = self._get(url, timeout=15)
            resp.raise_for_status()
            instruments = resp.json().get("d", {}).get("instruments", [])
            return {str(i.get("tradableInstrumentId")): i.get("name", "?") for i in instruments}
        except Exception as e:
            logger.warning(f"Could not build instrument name map: {e}")
            return {}

    def get_closed_trades(self) -> list[dict]:
        """
        Fetch closed trades from TradeLocker's ordersHistory (broker-side truth,
        survives redeploys). Returns normalised dicts:
          {name, side, qty, openPrice, closePrice, pnl, outcome, exit_reason, closedAt}

        TradeLocker's real format (per the official SDK):
          - Column names come from GET /trade/config -> d.ordersHistoryConfig.columns
          - Rows come from GET /trade/accounts/{id}/ordersHistory -> d.ordersHistory
          - Each row is a raw array; zip with the columns to get a dict.
        Filled orders are paired by positionId (entry + exit) to derive P&L.
        Returns [] gracefully on any failure (report must never crash).
        """
        try:
            # 1. Column names from /trade/config
            cfg = self._get(f"{self.base_url}/trade/config", timeout=15)
            cfg.raise_for_status()
            cfg_d = cfg.json().get("d", {})
            cols  = [c["id"] for c in cfg_d.get("ordersHistoryConfig", {}).get("columns", [])]
            if not cols:
                logger.warning("[History] No ordersHistoryConfig columns in /trade/config.")
                return []

            # 2. History rows
            hist = self._get(f"{self.base_url}/trade/accounts/{self.account_id}/ordersHistory")
            hist.raise_for_status()
            rows = hist.json().get("d", {}).get("ordersHistory", [])
            orders = [dict(zip(cols, r)) for r in rows]

            # 3. Keep filled orders, group by positionId
            from collections import defaultdict
            from risk import _contract_size
            name_map = self.get_instrument_name_map()

            by_pos = defaultdict(list)
            for o in orders:
                if str(o.get("status", "")).lower() != "filled":
                    continue
                pid = str(o.get("positionId") or "")
                if pid and pid != "0":
                    by_pos[pid].append(o)

            closed = []
            for pid, olist in by_pos.items():
                if len(olist) < 2:
                    continue  # position still open (only the entry fill exists)
                olist.sort(key=lambda o: float(o.get("createdDate") or 0))
                entry_o, exit_o = olist[0], olist[-1]

                ticker     = name_map.get(str(entry_o.get("tradableInstrumentId")), "?")
                side       = str(entry_o.get("side", "")).lower()
                qty        = float(entry_o.get("filledQty") or entry_o.get("qty") or 0)
                open_px    = float(entry_o.get("avgPrice") or 0)
                close_px   = float(exit_o.get("avgPrice") or 0)
                contract   = _contract_size(ticker)
                direction  = 1 if side == "buy" else -1
                pnl        = (close_px - open_px) * direction * qty * contract

                # Label exit as TP/SL by which level the close price is nearest to
                tp = float(entry_o.get("takeProfit") or 0)
                sl = float(entry_o.get("stopLoss") or 0)
                reason = "manual"
                if tp and abs(close_px - tp) <= abs(close_px - sl):
                    reason = "tp"
                elif sl:
                    reason = "sl"

                closed.append({
                    "name":       ticker,
                    "side":       side,
                    "qty":        qty,
                    "openPrice":  round(open_px, 5),
                    "closePrice": round(close_px, 5),
                    "pnl":        round(pnl, 2),
                    "outcome":    "win" if pnl > 0 else "loss" if pnl < 0 else "be",
                    "exit_reason": reason,
                    "closedAt":   exit_o.get("createdDate") or "",
                })

            closed.sort(key=lambda c: float(c.get("closedAt") or 0))
            logger.info(f"[History] Built {len(closed)} closed trades from ordersHistory.")
            return closed
        except Exception as e:
            logger.warning(f"[History] Could not fetch closed trades: {e}")
            return []

    def modify_position_sl(self, position_id: str, new_sl: float) -> dict:
        """
        Move the stop loss on an open position to new_sl.
        Uses PUT /trade/accounts/{accountId}/positions/{positionId}.
        """
        url = f"{self.base_url}/trade/accounts/{self.account_id}/positions/{position_id}"
        payload = {
            "stopLoss":     round(new_sl, 5),
            "stopLossType": "absolute",
        }
        resp = self.session.put(url, json=payload, timeout=10)
        if resp.status_code == 401:
            self.refresh_access_token()
            resp = self.session.put(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("s") == "error":
            raise RuntimeError(f"Broker error modifying SL: {result.get('errmsg', result)}")
        logger.info(f"SL updated → position {position_id} new SL={new_sl}")
        return result

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> dict:
        """
        Place a market order with optional stop loss and take profit.

        Args:
            symbol:      e.g. "EURUSD"
            side:        "buy" or "sell"
            qty:         lot size, e.g. 0.10
            stop_loss:   price level for stop loss (optional)
            take_profit: price level for take profit (optional)

        Returns:
            API response dict
        """
        tradable_id, instrument_id, route_id = self.get_instrument_id(symbol)

        url = f"{self.base_url}/trade/accounts/{self.account_id}/orders"

        payload = {
            "tradableInstrumentId": tradable_id,
            "instrumentId":         instrument_id,
            "routeId":              route_id,
            "qty":                  round(qty, 2),
            "side":                 side.lower(),
            "type":                 "market",
            "validity":             "IOC",
        }

        if stop_loss and stop_loss > 0:
            payload["stopLoss"]     = round(stop_loss, 5)
            payload["stopLossType"] = "absolute"
        if take_profit and take_profit > 0:
            payload["takeProfit"]     = round(take_profit, 5)
            payload["takeProfitType"] = "absolute"

        logger.info(f"Placing order: {payload}")

        resp = self.session.post(url, json=payload, timeout=10)

        if resp.status_code == 401:
            logger.info("Token expired — refreshing and retrying...")
            self.refresh_access_token()
            resp = self.session.post(url, json=payload, timeout=10)

        resp.raise_for_status()
        result = resp.json()

        # Check for broker-side TP validation error — retry without TP
        if result.get("s") == "error" and "TP" in result.get("errmsg", ""):
            logger.warning(
                f"TP rejected by broker ({result.get('errmsg')}) — "
                f"retrying without take profit (SL still attached)."
            )
            payload_no_tp = {k: v for k, v in payload.items()
                             if k not in ("takeProfit", "takeProfitType")}
            resp2 = self.session.post(url, json=payload_no_tp, timeout=10)
            resp2.raise_for_status()
            result = resp2.json()
            result["_tp_dropped"] = True  # flag so caller knows TP wasn't set

        # Broker forbids SL in the order — place without SL, attach it after
        sl_to_attach = None
        if result.get("s") == "error" and "SL" in result.get("errmsg", "").upper():
            sl_to_attach = stop_loss
            logger.warning(
                f"SL rejected by broker ({result.get('errmsg')}) — "
                f"retrying without SL, will attach via modify after fill."
            )
            payload_no_sl = {k: v for k, v in payload.items()
                             if k not in ("stopLoss", "stopLossType")}
            resp3 = self.session.post(url, json=payload_no_sl, timeout=10)
            resp3.raise_for_status()
            result = resp3.json()
            result["_sl_pending"] = sl_to_attach  # caller must attach SL

        # Raise a proper exception if the broker still returns an error
        if result.get("s") == "error":
            raise RuntimeError(f"Broker error: {result.get('errmsg', result)}")

        # Attach SL via position modify if it was rejected in the order
        if sl_to_attach and result.get("s") != "error":
            try:
                import time; time.sleep(1)
                positions = self.get_open_positions()
                for pos in positions:
                    if str(pos.get("tradableInstrumentId")) == str(tradable_id):
                        self.modify_position_sl(str(pos["id"]), sl_to_attach)
                        logger.info(f"SL attached post-fill: {sl_to_attach}")
                        break
            except Exception as e:
                logger.warning(f"Could not attach SL after fill: {e}")

        logger.info(f"Order placed: {result}")
        return result
