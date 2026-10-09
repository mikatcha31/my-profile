"""Trend-following strategy: time-series momentum with volatility targeting.

For each asset, on each completed daily bar:
  * trend score = share of lookbacks (e.g. 20/60/120 days) with a positive return,
    so the position scales in and out instead of flipping all at once;
  * regime filter = close must be above its long moving average, otherwise flat;
  * size = score * min(1, target_vol / realized_vol) * per-asset budget.

All inputs are known at the bar's close; the backtest executes at the next open,
so there is no look-ahead.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

TRADING_DAYS = 365  # crypto trades every day


@dataclass
class StrategyConfig:
    lookbacks: list[int] = field(default_factory=lambda: [20, 60, 120])
    regime_ma: int = 150
    vol_window: int = 30
    target_vol: float = 0.40      # annualized, per asset
    max_weight: float = 1.0       # cap per asset as fraction of its budget

    @property
    def warmup(self) -> int:
        return max(max(self.lookbacks), self.regime_ma, self.vol_window) + 1


def asset_signal(close: pd.Series, cfg: StrategyConfig) -> pd.DataFrame:
    """Per-bar diagnostics and the target exposure (0..max_weight) for one asset."""
    score = sum((close.pct_change(lb) > 0).astype(float) for lb in cfg.lookbacks) / len(cfg.lookbacks)
    if cfg.regime_ma > 0:
        regime = (close > close.rolling(cfg.regime_ma).mean()).astype(float)
    else:  # filter disabled
        regime = pd.Series(1.0, index=close.index)
    vol = close.pct_change().rolling(cfg.vol_window).std() * np.sqrt(TRADING_DAYS)
    vol_scale = (cfg.target_vol / vol).clip(upper=1.0)
    exposure = (score * regime * vol_scale).clip(0, cfg.max_weight)
    exposure[: cfg.warmup] = np.nan
    return pd.DataFrame({"close": close, "score": score, "above_ma": regime,
                         "vol": vol, "exposure": exposure})


def target_weights(closes: dict[str, pd.Series], cfg: StrategyConfig) -> pd.DataFrame:
    """Portfolio weights per date: equal budget per asset, remainder in cash."""
    budget = 1.0 / len(closes)
    w = pd.DataFrame({s: asset_signal(c, cfg)["exposure"] for s, c in closes.items()})
    return w * budget
