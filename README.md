# Wakeup 2 Day Trade — helper scripts

Helper scripts used by the **Wakeup 2 Day Trade** Grok Bot, which writes fast
first-pass research on sharply moving small-cap / micro-cap ("runner") stocks.
The bot downloads these files from a pinned release tag into
`/workspace/runner-watch/`, checks their SHA-256 hashes, and runs them.

> **Disclaimer:** research tooling only. Nothing here or in the bot's output
> is financial or investment advice, a price target, or a buy/sell/hold
> recommendation. Data comes from free third-party sources that may be
> delayed, incomplete or wrong. Always check the primary sources yourself.

## Scripts

| Script | What it does | Dependencies |
|---|---|---|
| `prefetch.py` | Fetches everything for one ticker at the same time: SEC EDGAR (CIK, filings, XBRL shares/cash/burn/debt, texts of recent 8-K/424B/S-1/S-3/10-Q filings), the StockAnalysis.com quote and daily history, Finviz, Nasdaq/FINRA short interest (with split adjustment), Yahoo 5-minute bars and news. It then runs `chart.py` and writes `/workspace/runner-cache/<TICKER>-<YYYY-MM-DD>/summary.json`. | stdlib only (runs `chart.py`) |
| `snapshot.py` | Builds the ready-to-send Stage-1 "first look" message from `summary.json`: price, move, extended hours, volume, float, shares, market cap, short interest, latest filing and headline, each with its source. The last line is `CHART: <png>` or `CHART: none`. `--spike` prints the biggest 5-minute moves for timing checks. | stdlib only |
| `chart.py` | Draws a daily candlestick chart (~6 months, volume, MA9/MA20, log scale for big spikes) from StockAnalysis.com history, falling back to Yahoo. Default output: `/workspace/runner-charts/<TICKER>-<date>.png`. | matplotlib, mplfinance, pandas |
| `scorecard.py` | Renders a mobile-first summary card PNG (dilution risk, catalyst, cash runway tiles plus key stats) from a `card.json` written after the analysis. Default output: `/workspace/runner-charts/<TICKER>-card-<date>.png`. | matplotlib |
| `price_range.py` | Renders a mobile-first speculative price-range PNG (Bear/Base/Bull bar + scenario cards, optional momentum band) from a `price_range.json` written after the valuation. Default output: `/workspace/runner-charts/<TICKER>-range-<date>.png`. | matplotlib |

Usage:

```bash
python3 prefetch.py TICKER [--no-chart] [--no-docs]   # prints summary.json path
python3 snapshot.py <summary.json | TICKER> [--spike]
python3 chart.py TICKER [out.png] [--bars daily_bars.json] [--source sa|yahoo]
python3 scorecard.py --json card.json [out.png]
python3 price_range.py --json price_range.json [out.png]
```

## Requirements

- Python 3.9+ (uses `zoneinfo`).
- `pip install -r requirements.txt` (matplotlib, mplfinance, pandas). These are
  needed only for `chart.py`, `scorecard.py` and `price_range.py`. If `mplfinance` is missing,
  `chart.py` tries to pip-install it.
- Outbound HTTPS to sec.gov, stockanalysis.com, finviz.com, api.nasdaq.com,
  api.finra.org and Yahoo Finance.

## SEC User-Agent (please set this)

SEC EDGAR asks automated clients to send a User-Agent that includes contact
details. `prefetch.py` reads it from the `SEC_USER_AGENT` environment variable:

```bash
export SEC_USER_AGENT="YourName or YourOrg your.email@example.com"
```

If it isn't set, a generic placeholder (`RunnerAnalyst research contact@example.com`)
is used. The owner of each bot copy should set their own value. SEC may throttle
or block generic agents.

## How the bot uses them

1. On first run, or if a file is missing or its hash is wrong, the bot downloads
   the scripts from
   `https://raw.githubusercontent.com/derekshi/wakeup-2-day-trade-scripts/v1.1.0/<file>`
   into `/workspace/runner-watch/` and checks each SHA-256.
2. For `Analyze $TICKER`, it runs `prefetch.py`, then sends the `snapshot.py`
   output and the chart as a first look within seconds.
3. It writes a short Quick Read, researches the full report, writes `card.json` and
   `price_range.json`, renders `scorecard.py` and `price_range.py`, and sends the report.

Caches are stored under `/workspace/runner-cache/` and images under
`/workspace/runner-charts/`.

## License

MIT, see [LICENSE](LICENSE).
