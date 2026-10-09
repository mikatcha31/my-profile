"""Market data: daily OHLCV bars from local CSV files or public exchange APIs.

Every loader returns a DataFrame indexed by UTC date with columns
open, high, low, close, volume, sorted oldest -> newest.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

COLUMNS = ["open", "high", "low", "close", "volume"]
TIMEOUT = 20


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLUMNS].astype(float)
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "date"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def load_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    return _normalize(df)


def save_csv(df: pd.DataFrame, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index_label="date")


def import_fmp_json(path: str | Path) -> dict[str, pd.DataFrame]:
    """Parse a Financial Modeling Prep EOD dump (list of {symbol, date, open, ...})."""
    rows = json.loads(Path(path).read_text())
    out: dict[str, pd.DataFrame] = {}
    for sym, grp in pd.DataFrame(rows).groupby("symbol"):
        out[sym] = _normalize(grp.set_index("date"))
    return out


# --- Online providers -------------------------------------------------------
# Symbols are written as BASE + "USD" (e.g. BTCUSD); each provider maps them.

def fetch_coinbase(symbol: str, days: int = 300) -> pd.DataFrame:
    """Coinbase Exchange public candles (no API key, reachable from the US)."""
    product = f"{symbol[:-3]}-USD"
    url = f"https://api.exchange.coinbase.com/products/{product}/candles"
    r = requests.get(url, params={"granularity": 86400}, timeout=TIMEOUT,
                     headers={"User-Agent": "trading-bot"})
    r.raise_for_status()
    # [time, low, high, open, close, volume], newest first, max 300 rows
    df = pd.DataFrame(r.json(), columns=["time", "low", "high", "open", "close", "volume"])
    df["date"] = pd.to_datetime(df["time"], unit="s")
    return _normalize(df.set_index("date")).tail(days)


def fetch_binance(symbol: str, days: int = 300) -> pd.DataFrame:
    """Binance public klines via the market-data-only mirror."""
    pair = f"{symbol[:-3]}USDT"
    url = "https://data-api.binance.vision/api/v3/klines"
    r = requests.get(url, params={"symbol": pair, "interval": "1d", "limit": min(days, 1000)},
                     timeout=TIMEOUT)
    r.raise_for_status()
    df = pd.DataFrame([k[:6] for k in r.json()],
                      columns=["time", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["time"], unit="ms")
    return _normalize(df.set_index("date"))


def fetch_fmp(symbol: str, days: int = 300) -> pd.DataFrame:
    """Financial Modeling Prep (needs FMP_API_KEY)."""
    key = os.environ["FMP_API_KEY"]
    url = "https://financialmodelingprep.com/stable/historical-price-eod/full"
    start = (pd.Timestamp.utcnow() - pd.Timedelta(days=days + 5)).strftime("%Y-%m-%d")
    r = requests.get(url, params={"symbol": symbol, "from": start, "apikey": key}, timeout=TIMEOUT)
    r.raise_for_status()
    return _normalize(pd.DataFrame(r.json()).set_index("date"))


PROVIDERS = {"coinbase": fetch_coinbase, "binance": fetch_binance, "fmp": fetch_fmp}


def fetch(symbol: str, sources: list[str], days: int = 300) -> tuple[pd.DataFrame, str]:
    """Try each source in order; return the first that works and its name."""
    errors = []
    for name in sources:
        try:
            df = PROVIDERS[name](symbol, days)
            if len(df):
                return df, name
            errors.append(f"{name}: empty")
        except Exception as e:  # noqa: BLE001 - we report every failure below
            errors.append(f"{name}: {e}")
    raise RuntimeError(f"no data for {symbol}: " + "; ".join(errors))


def drop_incomplete_bar(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Remove today's bar: a daily candle is only final once the UTC day is over."""
    now = now or datetime.now(timezone.utc)
    today = pd.Timestamp(now.date())
    return df[df.index < today]
