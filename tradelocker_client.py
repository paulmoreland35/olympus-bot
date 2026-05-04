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

        accounts = data if isinstance(data, list) else data.get("accounts", [])
        if not accounts:
            raise ValueError("No accounts found on this TradeLocker profile.")

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
                account = accounts[0]
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

    def get_instrument_id(self, symbol: str) -> tuple[int, int]:
        """
        Look up instrument ID and TRADE route ID by symbol name.
        Returns (instrumentId, routeId).
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
                inst_id = int(inst.get("id", 0))

                # Find TRADE route from routes list
                routes = inst.get("routes", [])
                trade_route_id = None
                for route in routes:
                    if isinstance(route, dict) and route.get("type") == "TRADE":
                        trade_route_id = int(route["id"])
                        break

                if trade_route_id is None:
                    raise ValueError(f"No TRADE route found for instrument '{symbol}'")

                logger.info(f"Instrument found: {symbol} id={inst_id} routeId={trade_route_id}")
                return inst_id, trade_route_id

        raise ValueError(
            f"Instrument '{symbol}' not found. "
            f"Check the symbol name exactly as shown in TradeLocker."
        )

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
        instrument_id, route_id = self.get_instrument_id(symbol)

        url = f"{self.base_url}/trade/accounts/{self.account_id}/orders"

        payload = {
            "instrumentId": instrument_id,
            "routeId":      route_id,
            "qty":          round(qty, 2),
            "side":         side.lower(),
            "type":         "market",
            "validity":     "GTC",
        }

        if stop_loss and stop_loss > 0:
            payload["stopLoss"] = round(stop_loss, 5)
        if take_profit and take_profit > 0:
            payload["takeProfit"] = round(take_profit, 5)

        logger.info(f"Placing order: {payload}")

        resp = self.session.post(url, json=payload, timeout=10)

        if resp.status_code == 401:
            logger.info("Token expired — refreshing and retrying...")
            self.refresh_access_token()
            resp = self.session.post(url, json=payload, timeout=10)

        resp.raise_for_status()
        result = resp.json()
        logger.info(f"Order placed: {result}")
        return result
