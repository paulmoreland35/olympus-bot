"""
TradeLocker REST API Client
Handles authentication, account info, instrument lookup, and order placement.
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
        self.account_id: Optional[str] = None
        self.acc_num: Optional[int] = None
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
        """Fetch accounts and set primary account ID, accNum, and balance."""
        url = f"{self.base_url}/auth/jwt/all-accounts"
        resp = self.session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # Handle both list and wrapped response
        accounts = data if isinstance(data, list) else data.get("accounts", [])

        if not accounts:
            raise ValueError("No accounts found on this TradeLocker profile.")

        # Pick first live account
        account = accounts[0]
        self.account_id = str(account.get("id") or account.get("accountId", ""))
        self.acc_num = int(account.get("accNum") or account.get("accountNumber", 0))
        self.balance = float(account.get("balance", 0.0))

        # Update session with accNum header (required for trade endpoints)
        self.session.headers.update({"accNum": str(self.acc_num)})

        logger.info(
            f"Account loaded — ID: {self.account_id} | accNum: {self.acc_num} | Balance: {self.balance}"
        )

    def get_balance(self) -> float:
        """Refresh and return current account balance."""
        url = f"{self.base_url}/trade/accounts/{self.acc_num}/accountDetails"
        try:
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            # TradeLocker wraps data in 'd' key
            details = data.get("d", data)
            self.balance = float(
                details.get("balance")
                or details.get("equity")
                or self.balance
            )
        except Exception as e:
            logger.warning(f"Could not refresh balance, using cached value: {e}")
        return self.balance

    # ------------------------------------------------------------------
    # Instruments
    # ------------------------------------------------------------------

    def get_instrument_id(self, symbol: str) -> tuple[int, int]:
        """
        Look up instrument ID and routeId by symbol name.
        Returns (instrumentId, routeId).
        """
        url = f"{self.base_url}/trade/accounts/{self.acc_num}/instruments"
        resp = self.session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        instruments = data.get("d", {}).get("instruments", data if isinstance(data, list) else [])

        symbol_upper = symbol.upper()
        for inst in instruments:
            # Instrument rows are often arrays: [id, name, ..., routeId]
            if isinstance(inst, list):
                inst_name = str(inst[2]).upper() if len(inst) > 2 else ""
                if inst_name == symbol_upper or symbol_upper in inst_name:
                    return int(inst[0]), int(inst[-1])
            elif isinstance(inst, dict):
                inst_name = str(inst.get("name", inst.get("symbol", ""))).upper()
                if inst_name == symbol_upper or symbol_upper in inst_name:
                    return int(inst.get("id", inst.get("instrumentId", 0))), int(
                        inst.get("routeId", inst.get("route", 0))
                    )

        raise ValueError(f"Instrument '{symbol}' not found on this account.")

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

        url = f"{self.base_url}/trade/accounts/{self.acc_num}/orders"

        payload = {
            "instrumentId": instrument_id,
            "routeId": route_id,
            "qty": round(qty, 2),
            "side": side.lower(),
            "type": "market",
            "validity": "GTC",
        }

        if stop_loss and stop_loss > 0:
            payload["stopLoss"] = round(stop_loss, 5)
        if take_profit and take_profit > 0:
            payload["takeProfit"] = round(take_profit, 5)

        logger.info(f"Placing order: {payload}")

        resp = self.session.post(url, json=payload, timeout=10)

        if resp.status_code == 401:
            # Token expired — refresh and retry once
            logger.info("Token expired, refreshing...")
            self.refresh_access_token()
            resp = self.session.post(url, json=payload, timeout=10)

        resp.raise_for_status()
        result = resp.json()
        logger.info(f"Order placed successfully: {result}")
        return result
