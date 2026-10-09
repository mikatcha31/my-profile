# Operating the trading bot (for Claude)

Work from `trading-bot/`. All commands are `python -m bot <cmd>`.

## Daily cycle
1. Refresh data: `python -m bot update`. If exchange hosts are blocked by the network
   policy, use the FMP MCP tool instead: call `mcp__FMP__crypto` with
   `endpoint=cryptocurrency-historical-price-eod-full`, `symbol=BTCUSD` (then ETHUSD, SOLUSD)
   and a `from_date` a few weeks back, then `python -m bot import-fmp <saved-json-file>`.
2. Trade: `python -m bot tick` (paper). It skips bars it has already handled.
3. Report: `python -m bot status`, plus `state/equity_paper.csv` for the history.
4. Commit `data/` and `state/` so the track record survives the session.

## Rules
- Never run `--broker live` unless the user explicitly asks in the current conversation.
- Don't change `config.json` parameters to chase backtest results; check any change
  out of sample (`backtest --end 2024-12-31` vs `--start 2025-01-01`).
- Run `python -m pytest -q tests` after any code change.
