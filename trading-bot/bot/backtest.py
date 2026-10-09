"""Event-driven daily backtest: signals at close t, orders filled at open t+1."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .strategy import TRADING_DAYS, StrategyConfig, target_weights


@dataclass
class CostModel:
    fee: float = 0.001        # 0.10 % per side (typical spot taker fee)
    slippage: float = 0.0005  # 0.05 % adverse fill
    rebalance_band: float = 0.03  # skip trades smaller than 3 % of equity


def rebalance(equity: float, cash: float, units: dict[str, float], prices: dict[str, float],
              targets: dict[str, float], costs: CostModel) -> tuple[float, list[dict]]:
    """Move holdings toward target weights. Mutates `units`, returns (cash, trades).

    Sells run first so their proceeds can fund the buys.
    """
    trades = []
    deltas = {}
    for s, tw in targets.items():
        if tw is None or np.isnan(tw):
            continue
        cur_w = units.get(s, 0.0) * prices[s] / equity
        diff = tw - cur_w
        # always allow a full exit, otherwise ignore small drifts
        if abs(diff) >= costs.rebalance_band or (tw == 0 and units.get(s, 0) > 0):
            deltas[s] = diff
    for s, diff in sorted(deltas.items(), key=lambda kv: kv[1]):
        px = prices[s]
        if diff < 0:
            qty = min(units.get(s, 0.0), -diff * equity / px)
            fill = px * (1 - costs.slippage)
            cash += qty * fill * (1 - costs.fee)
            units[s] = units.get(s, 0.0) - qty
            side = "SELL"
        else:
            fill = px * (1 + costs.slippage)
            notional = min(diff * equity, cash)
            if notional <= 0:
                continue
            qty = notional * (1 - costs.fee) / fill
            cash -= notional
            units[s] = units.get(s, 0.0) + qty
            side = "BUY"
        # notional = USD spent on a buy (fee included), gross USD received on a sell
        trades.append({"symbol": s, "side": side, "qty": qty, "price": fill,
                       "notional": notional if side == "BUY" else qty * fill})
    return cash, trades


def run_backtest(data: dict[str, pd.DataFrame], cfg: StrategyConfig, costs: CostModel,
                 initial: float = 10_000.0, start: str | None = None) -> dict:
    closes = {s: df["close"] for s, df in data.items()}
    weights = target_weights(closes, cfg).shift(1)  # decided yesterday, executed today
    opens = pd.DataFrame({s: df["open"] for s, df in data.items()})
    close_px = pd.DataFrame(closes)
    dates = weights.dropna(how="all").index
    if start:
        dates = dates[dates >= pd.Timestamp(start)]

    cash, units, curve, log = initial, {}, [], []
    for d in dates:
        o = opens.loc[d].to_dict()
        equity = cash + sum(u * o[s] for s, u in units.items())
        cash, trades = rebalance(equity, cash, units, o, weights.loc[d].to_dict(), costs)
        for t in trades:
            log.append({"date": d.date().isoformat(), **t})
        c = close_px.loc[d]
        curve.append((d, cash + sum(u * c[s] for s, u in units.items())))

    eq = pd.Series(dict(curve), name="equity")
    bench = (close_px.loc[dates] / close_px.loc[dates].iloc[0]).mean(axis=1) * initial
    return {"equity": eq, "benchmark": bench.rename("buy_and_hold"),
            "trades": pd.DataFrame(log), "stats": stats(eq, len(log)),
            "bench_stats": stats(bench, 0)}


def stats(eq: pd.Series, n_trades: int) -> dict:
    rets = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    dd = eq / eq.cummax() - 1
    sharpe = rets.mean() / rets.std() * np.sqrt(TRADING_DAYS) if rets.std() > 0 else 0.0
    return {
        "start": eq.index[0].date().isoformat(),
        "end": eq.index[-1].date().isoformat(),
        "final_equity": round(float(eq.iloc[-1]), 2),
        "total_return_pct": round((eq.iloc[-1] / eq.iloc[0] - 1) * 100, 2),
        "cagr_pct": round(((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1) * 100, 2),
        "sharpe": round(float(sharpe), 2),
        "max_drawdown_pct": round(float(dd.min()) * 100, 2),
        "volatility_pct": round(float(rets.std() * np.sqrt(TRADING_DAYS)) * 100, 2),
        "trades": n_trades,
    }
