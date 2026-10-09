"""Command line entry point: python -m bot <command>."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import data as md
from .backtest import CostModel, run_backtest
from .broker import BinanceBroker, PaperBroker
from .strategy import StrategyConfig, asset_signal, target_weights

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    cfg["strategy"] = StrategyConfig(**cfg.get("strategy", {}))
    cfg["costs"] = CostModel(**cfg.get("costs", {}))
    return cfg


def load_all(cfg: dict) -> dict[str, pd.DataFrame]:
    return {s: md.load_csv(ROOT / "data" / f"{s}.csv") for s in cfg["symbols"]}


def merge_into_csv(symbol: str, fresh: pd.DataFrame) -> int:
    path = ROOT / "data" / f"{symbol}.csv"
    old = md.load_csv(path) if path.exists() else fresh.iloc[:0]
    merged = pd.concat([old, fresh])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    md.save_csv(merged, path)
    return len(merged) - len(old)


def append_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        if new:
            w.writeheader()
        w.writerows(rows)


# --- commands ---------------------------------------------------------------

def cmd_update(args, cfg) -> None:
    for s in cfg["symbols"]:
        df, src = md.fetch(s, args.sources or cfg["data_sources"], days=args.days)
        print(f"{s}: {merge_into_csv(s, df):+d} new bars from {src} (last {df.index[-1].date()})")


def cmd_import_fmp(args, cfg) -> None:
    for s, df in md.import_fmp_json(args.file).items():
        print(f"{s}: {merge_into_csv(s, df):+d} new bars (last {df.index[-1].date()})")


def plot_equity(eq: pd.Series, bench: pd.Series, path: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return  # charts are optional
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})
    ax1.plot(eq.index, eq, label="Strategy", color="#2563eb", lw=1.6)
    ax1.plot(bench.index, bench, label="Buy & hold (equal weight)", color="#9ca3af", lw=1.2)
    ax1.set_yscale("log")
    ax1.set_ylabel("Equity (USD, log)")
    ax1.legend(loc="upper left", frameon=False)
    ax1.grid(alpha=0.3)
    for s, c in ((eq, "#2563eb"), (bench, "#9ca3af")):
        ax2.fill_between(s.index, (s / s.cummax() - 1) * 100, 0, color=c, alpha=0.4)
    ax2.set_ylabel("Drawdown %")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def cmd_backtest(args, cfg) -> None:
    data = {s: md.drop_incomplete_bar(df) for s, df in load_all(cfg).items()}
    if args.end:
        data = {s: df[df.index <= args.end] for s, df in data.items()}
    res = run_backtest(data, cfg["strategy"], cfg["costs"], args.capital, start=args.start)
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    pd.concat([res["equity"], res["benchmark"]], axis=1).to_csv(out / "backtest_equity.csv")
    res["trades"].to_csv(out / "backtest_trades.csv", index=False)
    report = {"strategy": res["stats"], "buy_and_hold": res["bench_stats"],
              "config": {"symbols": cfg["symbols"], "strategy": asdict(cfg["strategy"]),
                         "costs": asdict(cfg["costs"])}}
    (out / "backtest.json").write_text(json.dumps(report, indent=2))
    plot_equity(res["equity"], res["benchmark"], out / "backtest.png")
    print(f"{'':20}{'strategy':>14}{'buy & hold':>14}")
    for k in res["stats"]:
        print(f"{k:20}{res['stats'][k]!s:>14}{res['bench_stats'][k]!s:>14}")
    print(f"\nreport written to {out}/")


def make_broker(name: str, cfg: dict):
    if name == "paper":
        return PaperBroker(ROOT / "state" / "portfolio.json", cfg.get("initial_capital", 10_000), cfg["costs"])
    return BinanceBroker(live=(name == "live"), costs=cfg["costs"])


def cmd_tick(args, cfg) -> None:
    """One trading cycle. Safe to run as often as you like: it trades once per new daily bar."""
    if args.update:
        try:
            cmd_update(argparse.Namespace(sources=None, days=10), cfg)
        except Exception as e:  # noqa: BLE001 - trade on cached data, but say so
            print(f"warning: data update failed ({e}); using cached data")
    raw = load_all(cfg)
    data = {s: md.drop_incomplete_bar(df) for s, df in raw.items()}
    bar = min(df.index[-1] for df in data.values())
    age = (pd.Timestamp(datetime.now(timezone.utc).date()) - bar).days
    if age > 2 and not args.force:
        sys.exit(f"refusing to trade: last complete bar {bar.date()} is {age} days old (run update or --force)")

    weights = target_weights({s: df["close"] for s, df in data.items()}, cfg["strategy"])
    targets = weights.loc[bar].to_dict()
    # execution price: latest known price (today's partial bar if present, else last close)
    prices = {s: float(df["close"].iloc[-1]) for s, df in raw.items()}

    broker = make_broker(args.broker, cfg)
    state = broker.state
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if state.get("last_bar") == str(bar.date()) and not args.force:
        print(f"bar {bar.date()} already traded; nothing to do. equity={broker.equity(prices):,.2f}")
        return

    trades = broker.rebalance(targets, prices)
    state["last_bar"] = str(bar.date())
    broker.save()
    equity = broker.equity(prices)

    sdir = ROOT / "state"
    sdir.mkdir(exist_ok=True)
    append_csv(sdir / f"trades_{args.broker}.csv",
               [{"time": now, "bar": str(bar.date()), **t} for t in trades])
    append_csv(sdir / f"equity_{args.broker}.csv",
               [{"time": now, "bar": str(bar.date()), "equity": round(equity, 2),
                 **{f"w_{s}": round(w, 4) for s, w in targets.items()}}])

    print(f"[{now}] broker={args.broker} bar={bar.date()} equity={equity:,.2f}")
    for s, w in targets.items():
        print(f"  target {s}: {w:6.1%}  @ {prices[s]:,.2f}")
    for t in trades:
        print(f"  {t['side']:4} {t['qty']:.6f} {t['symbol']} @ {t['price']:,.2f} (= {t['notional']:,.2f} USD)")
    if not trades:
        print("  no trades (positions already within the rebalance band)")


def cmd_status(args, cfg) -> None:
    raw = load_all(cfg)
    prices = {s: float(df["close"].iloc[-1]) for s, df in raw.items()}
    broker = make_broker(args.broker, cfg)
    eq = broker.equity(prices)
    init = broker.state.get("initial_cash", cfg.get("initial_capital", 10_000))
    print(f"broker={args.broker}  last traded bar={broker.state.get('last_bar')}")
    print(f"equity {eq:,.2f} USD  ({(eq / init - 1):+.2%} since start)  cash {broker.cash():,.2f}")
    for s, u in broker.positions().items():
        print(f"  {s}: {u:.6f} units = {u * prices[s]:,.2f} USD ({u * prices[s] / eq:.1%})")
    print("\nsignals on last complete bar:")
    for s, df in raw.items():
        sig = asset_signal(md.drop_incomplete_bar(df)["close"], cfg["strategy"]).iloc[-1]
        print(f"  {s}: close={sig.close:,.2f} trend_score={sig.score:.2f} above_ma={bool(sig.above_ma)} "
              f"vol={sig.vol:.0%} exposure={sig.exposure:.2f}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="bot", description="Automated crypto trend-following bot")
    p.add_argument("--config", default=str(ROOT / "config.json"))
    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("update", help="download recent daily bars into data/")
    u.add_argument("--sources", type=lambda s: s.split(","), help="e.g. coinbase,binance,fmp")
    u.add_argument("--days", type=int, default=300)

    i = sub.add_parser("import-fmp", help="merge a Financial Modeling Prep JSON dump into data/")
    i.add_argument("file")

    b = sub.add_parser("backtest", help="simulate the strategy on data/")
    b.add_argument("--start")
    b.add_argument("--end")
    b.add_argument("--capital", type=float, default=10_000)

    for name, fn in (("tick", cmd_tick), ("status", cmd_status)):
        t = sub.add_parser(name)
        t.add_argument("--broker", choices=["paper", "testnet", "live"], default="paper")
        if name == "tick":
            t.add_argument("--update", action="store_true", help="fetch fresh data first")
            t.add_argument("--force", action="store_true", help="trade even if this bar was already handled")

    args = p.parse_args(argv)
    cfg = load_config(Path(args.config))
    {"update": cmd_update, "import-fmp": cmd_import_fmp, "backtest": cmd_backtest,
     "tick": cmd_tick, "status": cmd_status}[args.cmd](args, cfg)
