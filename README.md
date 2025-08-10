# Binance Futures Testnet Trading Bot

Simple Python trading bot for Binance Futures Testnet. Supports MARKET and LIMIT orders, auto-adjusts quantity to meet `minNotional` and exchange filters, and logs trades.

## Features
- Market & Limit orders (BUY/SELL)
- Auto-check and adjust quantity to meet `minNotional` and `stepSize`
- Price rounded to `tickSize` for LIMIT orders
- Interactive mode and CLI mode
- Dry-run mode for previewing adjustments
- Logs requests/responses to `logs/trading.log`
- Records successful orders to `logs/trades.csv`

## Setup

1. Clone and enter the repo:
```bash
git clone <your-repo-url>
cd binance-bot
