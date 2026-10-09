"""Brokers: a persistent paper account, and Binance spot (testnet or live)."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

from .backtest import CostModel, rebalance


class PaperBroker:
    """Simulated account persisted to a JSON file, filled with the same cost model as the backtest."""

    def __init__(self, path: str | Path, initial_cash: float = 10_000.0, costs: CostModel | None = None):
        self.path = Path(path)
        self.costs = costs or CostModel()
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
        else:
            self.state = {"cash": initial_cash, "units": {}, "initial_cash": initial_cash,
                          "created": datetime.now(timezone.utc).isoformat(), "last_bar": None}

    def positions(self) -> dict[str, float]:
        return dict(self.state["units"])

    def cash(self) -> float:
        return self.state["cash"]

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash() + sum(u * prices[s] for s, u in self.positions().items())

    def rebalance(self, targets: dict[str, float], prices: dict[str, float]) -> list[dict]:
        units = self.positions()
        cash, trades = rebalance(self.equity(prices), self.cash(), units, prices, targets, self.costs)
        self.state["cash"] = cash
        self.state["units"] = {s: u for s, u in units.items() if u > 1e-12}
        return trades

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2))


class BinanceBroker:
    """Binance spot via REST. Defaults to the testnet; live requires BINANCE_LIVE=yes.

    Keys come from BINANCE_API_KEY / BINANCE_API_SECRET. Symbols like BTCUSD are
    traded against USDT (BTCUSDT); "cash" is the free USDT balance.
    """

    TESTNET = "https://testnet.binance.vision"
    LIVE = "https://api.binance.com"

    def __init__(self, live: bool = False, costs: CostModel | None = None, session=None):
        if live and os.environ.get("BINANCE_LIVE") != "yes":
            raise RuntimeError("live trading refused: set BINANCE_LIVE=yes to confirm real-money orders")
        self.base = self.LIVE if live else self.TESTNET
        self.key = os.environ["BINANCE_API_KEY"]
        self.secret = os.environ["BINANCE_API_SECRET"].encode()
        self.costs = costs or CostModel()
        self.http = session or requests.Session()
        self.http.headers["X-MBX-APIKEY"] = self.key
        self._filters: dict[str, dict] = {}
        self.state = {}

    # -- low level ----------------------------------------------------------
    def _sign(self, params: dict) -> str:
        params = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        query = urlencode(params)
        sig = hmac.new(self.secret, query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={sig}"

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False):
        params = params or {}
        if signed:
            url = f"{self.base}{path}?{self._sign(params)}"
            r = self.http.request(method, url, timeout=20)
        else:
            r = self.http.request(method, f"{self.base}{path}", params=params, timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(f"Binance {method} {path} -> {r.status_code}: {r.text}")
        return r.json()

    @staticmethod
    def pair(symbol: str) -> str:
        return f"{symbol[:-3]}USDT"

    def _lot(self, pair: str) -> dict:
        if pair not in self._filters:
            info = self._request("GET", "/api/v3/exchangeInfo", {"symbol": pair})
            f = {x["filterType"]: x for x in info["symbols"][0]["filters"]}
            notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
            self._filters[pair] = {"step": float(f["LOT_SIZE"]["stepSize"]),
                                   "min_qty": float(f["LOT_SIZE"]["minQty"]),
                                   "min_notional": float(notional.get("minNotional", 0))}
        return self._filters[pair]

    @staticmethod
    def floor_step(qty: float, step: float) -> float:
        decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        return round(math.floor(qty / step + 1e-9) * step, decimals)

    # -- broker interface ---------------------------------------------------
    def _balances(self) -> dict[str, float]:
        acct = self._request("GET", "/api/v3/account", signed=True)
        return {b["asset"]: float(b["free"]) for b in acct["balances"]}

    def positions(self) -> dict[str, float]:
        bal = self._balances()
        return {s: bal.get(s[:-3], 0.0) for s in self.state.get("symbols", [])}

    def cash(self) -> float:
        return self._balances().get("USDT", 0.0)

    def equity(self, prices: dict[str, float]) -> float:
        bal = self._balances()
        return bal.get("USDT", 0.0) + sum(bal.get(s[:-3], 0.0) * p for s, p in prices.items())

    def rebalance(self, targets: dict[str, float], prices: dict[str, float]) -> list[dict]:
        self.state["symbols"] = list(prices)
        bal = self._balances()
        units = {s: bal.get(s[:-3], 0.0) for s in prices}
        cash = bal.get("USDT", 0.0)
        equity = cash + sum(units[s] * prices[s] for s in prices)
        # plan with the shared rebalance logic on a scratch copy, then send real orders
        _, planned = rebalance(equity, cash, dict(units), prices, targets, self.costs)
        done = []
        for t in planned:
            pair = self.pair(t["symbol"])
            lot = self._lot(pair)
            if t["side"] == "SELL":
                qty = self.floor_step(min(t["qty"], units[t["symbol"]]), lot["step"])
                if qty < lot["min_qty"] or qty * prices[t["symbol"]] < lot["min_notional"]:
                    continue
                params = {"symbol": pair, "side": "SELL", "type": "MARKET", "quantity": f"{qty:f}"}
            else:
                quote = round(t["notional"], 2)
                if quote < lot["min_notional"]:
                    continue
                params = {"symbol": pair, "side": "BUY", "type": "MARKET", "quoteOrderQty": f"{quote:.2f}"}
            res = self._request("POST", "/api/v3/order", params, signed=True)
            qty = float(res.get("executedQty", 0))
            quote_qty = float(res.get("cummulativeQuoteQty", 0))
            done.append({"symbol": t["symbol"], "side": t["side"], "qty": qty,
                         "price": quote_qty / qty if qty else 0.0, "notional": quote_qty,
                         "order_id": res.get("orderId")})
        return done

    def save(self) -> None:  # exchange is the source of truth
        pass
