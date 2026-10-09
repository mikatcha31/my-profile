import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
import pytest

from bot.backtest import CostModel, rebalance, run_backtest
from bot.broker import BinanceBroker, PaperBroker
from bot.data import drop_incomplete_bar
from bot.strategy import StrategyConfig, asset_signal, target_weights


def synthetic(n=400, drift=0.002, seed=0):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.02, n)))
    idx = pd.date_range("2023-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": np.r_[close[0], close[:-1]], "high": close * 1.01,
                         "low": close * 0.99, "close": close, "volume": 1.0}, index=idx)


def test_no_lookahead():
    """Changing a future price must not change any earlier signal."""
    df = synthetic()
    cfg = StrategyConfig()
    a = asset_signal(df["close"], cfg)["exposure"]
    bumped = df["close"].copy()
    bumped.iloc[300] *= 2
    b = asset_signal(bumped, cfg)["exposure"]
    pd.testing.assert_series_equal(a.iloc[:300], b.iloc[:300])


def test_weights_bounded():
    data = {"A": synthetic(seed=1)["close"], "B": synthetic(seed=2, drift=-0.002)["close"]}
    w = target_weights(data, StrategyConfig()).dropna()
    assert (w >= 0).all().all() and (w.sum(axis=1) <= 1 + 1e-9).all()


def test_uptrend_is_invested_downtrend_is_flat():
    up = asset_signal(synthetic(drift=0.01, seed=3)["close"], StrategyConfig())["exposure"].iloc[-1]
    down = asset_signal(synthetic(drift=-0.01, seed=3)["close"], StrategyConfig())["exposure"].iloc[-1]
    assert up > 0.3 and down == 0


def test_rebalance_conserves_value_minus_costs():
    costs = CostModel(fee=0.001, slippage=0.0, rebalance_band=0.0)
    units = {}
    cash, trades = rebalance(1000, 1000, units, {"A": 10.0}, {"A": 0.5}, costs)
    assert trades[0]["side"] == "BUY"
    assert cash == pytest.approx(500)
    assert units["A"] * 10 == pytest.approx(500 * 0.999)
    cash, trades = rebalance(cash + units["A"] * 10, cash, units, {"A": 10.0}, {"A": 0.0}, costs)
    assert units["A"] == pytest.approx(0) and cash == pytest.approx(500 + 499.5 * 0.999)


def test_backtest_runs_and_reports():
    data = {"A": synthetic(seed=4), "B": synthetic(seed=5)}
    res = run_backtest(data, StrategyConfig(), CostModel())
    assert res["equity"].iloc[0] > 0 and res["stats"]["trades"] > 0
    assert set(res["stats"]) >= {"cagr_pct", "sharpe", "max_drawdown_pct"}


def test_paper_broker_persists(tmp_path):
    path = tmp_path / "p.json"
    b = PaperBroker(path, 1000)
    b.rebalance({"A": 0.5}, {"A": 10.0})
    b.state["last_bar"] = "2026-01-01"
    b.save()
    b2 = PaperBroker(path)
    assert b2.positions()["A"] > 0 and b2.state["last_bar"] == "2026-01-01"


def test_drop_incomplete_bar():
    from datetime import datetime, timezone
    df = synthetic(n=5)
    now = datetime(2023, 1, 5, 12, tzinfo=timezone.utc)
    assert drop_incomplete_bar(df, now).index[-1] == pd.Timestamp("2023-01-04")


def test_floor_step():
    assert BinanceBroker.floor_step(0.123456789, 0.00001) == 0.12345
    assert BinanceBroker.floor_step(12.9, 1.0) == 12


class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._d


class FakeSession:
    """Minimal stand-in for the Binance REST API."""

    def __init__(self, secret):
        self.headers, self.secret, self.orders = {}, secret, []
        self.balances = {"USDT": 1000.0, "BTC": 0.0}

    def request(self, method, url, params=None, timeout=None):
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if "signature" in q:  # verify HMAC exactly like Binance does
            payload = u.query.rsplit("&signature=", 1)[0]
            assert q["signature"] == hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        if u.path == "/api/v3/account":
            return FakeResp({"balances": [{"asset": a, "free": str(v)} for a, v in self.balances.items()]})
        if u.path == "/api/v3/exchangeInfo":
            return FakeResp({"symbols": [{"filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
                {"filterType": "NOTIONAL", "minNotional": "5"}]}]})
        if u.path == "/api/v3/order":
            self.orders.append(q)
            quote = float(q["quoteOrderQty"])
            return FakeResp({"orderId": 1, "executedQty": str(quote / 100), "cummulativeQuoteQty": str(quote)})
        raise AssertionError(url)


def test_binance_broker_signs_and_orders(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    sess = FakeSession(b"s")
    broker = BinanceBroker(session=sess)
    assert broker.base == BinanceBroker.TESTNET
    trades = broker.rebalance({"BTCUSD": 0.5}, {"BTCUSD": 100.0})
    assert sess.orders[0]["side"] == "BUY" and sess.orders[0]["quoteOrderQty"] == "500.00"
    assert trades[0]["qty"] == pytest.approx(5.0)


def test_live_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    monkeypatch.delenv("BINANCE_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="live trading refused"):
        BinanceBroker(live=True)
