#!/usr/bin/env python3
"""Daily candlestick chart for a ticker (runner-quick-analysis).

Usage: python3 chart.py TICKER [out.png] [--bars daily_bars.json] [--source sa|yahoo]
Daily OHLCV, ~6 months. Primary source: StockAnalysis.com history page data
(stockanalysis.com/stocks/<t>/history/__data.json; S&P Global data, split-adjusted,
~128 sessions). Fallback: Yahoo's v8 chart API (query1, then query2).
--bars reuses bars already fetched by prefetch.py (JSON: {source, name, bars:[{date,
open,high,low,close,volume}]}) so nothing is downloaded twice.
Renders candles + volume + 9/20-day MAs with mplfinance; the caption names the
source actually used.
Default output: /workspace/runner-charts/TICKER-YYYY-MM-DD.png (last bar date).
Exits non-zero with a message if no data. Never fabricates bars.
"""
import json, os, subprocess, sys, time, random
import urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]


def die(msg, code=1):
    print(f"chart.py: {msg}", file=sys.stderr)
    sys.exit(code)


def ensure_mplfinance():
    try:
        import mplfinance  # noqa: F401
    except ImportError:
        print("chart.py: installing mplfinance...", file=sys.stderr)
        base = [sys.executable, "-m", "pip", "install", "-q"]
        for extra in ([], ["--user"], ["--user", "--break-system-packages"]):
            if subprocess.call(base + extra + ["mplfinance"]) == 0:
                break
        import site, importlib
        importlib.invalidate_caches()
        us = site.getusersitepackages()
        if us not in sys.path:
            sys.path.append(us)
        try:
            import mplfinance  # noqa: F401
        except ImportError:
            die("mplfinance is not installed and pip install failed")


def fetch(ticker, attempts=5):
    last_err = None
    for i in range(attempts):
        host = HOSTS[0] if i % 2 == 0 else HOSTS[1]
        url = (f"https://{host}/v8/finance/chart/{ticker}"
               "?range=6mo&interval=1d&includePrePost=false&events=div%2Csplit")
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            chart = data.get("chart") or {}
            if chart.get("error"):
                err = chart["error"]
                # "Not Found" / no data for symbol: retrying won't help
                die(f"Yahoo returned error for {ticker}: "
                    f"{err.get('code')}: {err.get('description')}", 2)
            res = (chart.get("result") or [None])[0]
            if not res:
                raise ValueError("empty result")
            return res
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} from {host}"
            if e.code == 404:
                try:
                    body = json.loads(e.read().decode("utf-8"))
                    d = body["chart"]["error"]["description"]
                    die(f"Yahoo has no chart data for {ticker}: {d}", 2)
                except (ValueError, KeyError, TypeError):
                    pass
        except Exception as e:  # network, JSON, empty
            last_err = f"{type(e).__name__}: {e} ({host})"
        wait = min(30, 2 ** i) + random.uniform(0, 1)
        print(f"chart.py: fetch failed ({last_err}); retry in {wait:.1f}s",
              file=sys.stderr)
        time.sleep(wait)
    die(f"could not fetch Yahoo data for {ticker} after {attempts} tries: {last_err}")


def fetch_sa(ticker):
    """Daily bars from StockAnalysis.com's SvelteKit data endpoint. Returns
    (DataFrame, meta) or raises. Follows redirects to /etf/ or /quote/otc/."""
    import pandas as pd
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    url = f"https://stockanalysis.com/stocks/{ticker.lower().replace('.', '-')}/history/__data.json"
    for _ in range(3):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("type") == "redirect":
            loc = d["location"].rstrip("/")
            url = "https://stockanalysis.com" + (loc if loc.endswith("/history") else loc + "/history") + "/__data.json"
            continue
        break
    merged = {}
    for n in d.get("nodes") or []:
        if n and n.get("type") == "data" and n.get("data"):
            o = _devalue(n["data"])
            if isinstance(o, dict): merged.update(o)
    h = merged.get("data") or {}
    rows = [b for b in (h.get("data") or []) if all(b.get(k) is not None for k in ("o", "h", "l", "c", "t"))]
    if len(rows) < 20:
        raise ValueError(f"only {len(rows)} usable daily bars")
    info = merged.get("info") or {}
    bars = [{"date": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"],
             "volume": b.get("v") or 0} for b in rows]
    return bars_frame(bars), {"shortName": info.get("name"),
                              "_source": "StockAnalysis.com" + (" (S&P Global data)" if h.get("source") == "spg"
                                                                else f" (data: {h.get('source') or 'n/a'})")}


def _devalue(arr):
    cache = {}
    def r(i):
        if i < 0: return None
        if i in cache: return cache[i]
        v = arr[i]
        if isinstance(v, dict):
            o = {}; cache[i] = o
            for k, j in v.items(): o[k] = r(j)
            return o
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in ("Date", "Set", "Map", "BigInt", "RegExp", "Object", "null"):
                return v[1] if len(v) > 1 else None
            o = []; cache[i] = o; o.extend(r(j) for j in v); return o
        cache[i] = v; return v
    return r(0)


def bars_frame(bars):
    import pandas as pd
    df = pd.DataFrame({"Open": [b["open"] for b in bars], "High": [b["high"] for b in bars],
                       "Low": [b["low"] for b in bars], "Close": [b["close"] for b in bars],
                       "Volume": [b.get("volume") or 0 for b in bars]},
                      index=pd.DatetimeIndex(pd.to_datetime([b["date"] for b in bars]), name="Date"))
    df = df.dropna(subset=["Open", "High", "Low", "Close"]).astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


def to_frame(res):
    import pandas as pd
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    if not ts or not q:
        return None, res.get("meta", {})
    meta = res.get("meta", {})
    off = int(meta.get("gmtoffset") or 0)  # exchange tz offset, seconds
    idx = [datetime.fromtimestamp(t + off, tz=timezone.utc).replace(tzinfo=None).date()
           for t in ts]
    df = pd.DataFrame({
        "Open": q.get("open"), "High": q.get("high"), "Low": q.get("low"),
        "Close": q.get("close"), "Volume": q.get("volume")},
        index=pd.DatetimeIndex(idx, name="Date"))
    # Drop incomplete bars (Yahoo sometimes emits nulls); never fill/fabricate.
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df["Volume"] = df["Volume"].fillna(0)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df, meta


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if len(sys.argv) >= 2 else 1)
    argv = sys.argv[1:]
    opts = {}
    for k in ("--bars", "--source"):
        if k in argv:
            i = argv.index(k); opts[k] = argv[i + 1]; del argv[i:i + 2]
    ticker = argv[0].strip().lstrip("$").upper()
    out = argv[1] if len(argv) > 1 else None

    df, meta, src = None, {}, None
    if opts.get("--bars"):
        try:
            b = json.load(open(opts["--bars"]))
            df = bars_frame(b["bars"]); meta = {"shortName": b.get("name")}
            src = b.get("source") or "prefetch bars"
        except Exception as e:
            print(f"chart.py: --bars unusable ({e}); fetching", file=sys.stderr)
            df = None
    if (df is None or df.empty) and opts.get("--source", "sa") == "sa":
        try:
            df, meta = fetch_sa(ticker); src = meta.pop("_source")
        except Exception as e:
            print(f"chart.py: StockAnalysis history failed ({type(e).__name__}: {e}); "
                  "falling back to Yahoo", file=sys.stderr)
            df = None
    if df is None or df.empty:
        res = fetch(ticker)
        df, meta = to_frame(res)
        src = "Yahoo Finance"
    if df is None or df.empty:
        die(f"no daily bars returned for {ticker}", 2)

    ensure_mplfinance()
    import matplotlib
    matplotlib.use("Agg")
    import logging
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    import mplfinance as mpf

    last = df.index[-1]
    last_s = last.strftime("%Y-%m-%d")
    last_nice = last.strftime("%b %-d, %Y")
    if not out:
        out = f"/workspace/runner-charts/{ticker}-{last_s}.png"
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    mavs = tuple(m for m in (9, 20) if len(df) >= m + 1)
    lo, hi = float(df["Low"].min()), float(df["High"].max())
    use_log = lo > 0 and hi / lo > 8

    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit",
                               wick="inherit", volume="in")
    style = mpf.make_mpf_style(base_mpf_style="nightclouds", marketcolors=mc,
                               gridstyle=":", gridcolor="#3a3f4b",
                               mavcolors=["#ffb74d", "#42a5f5"],
                               rc={"font.size": 18, "axes.labelsize": 19,
                                   "axes.titlesize": 24, "xtick.labelsize": 17,
                                   "ytick.labelsize": 17, "axes.linewidth": 1.2})
    name = meta.get("shortName") or meta.get("longName") or ""
    lc = float(df["Close"].iloc[-1])
    lc_s = f"${lc:,.2f}" if lc >= 1 else f"${lc:.4f}"
    # Two-line title sized for phones (figure is ~1:1.1, fonts ~1.6x the old 12x7 chart).
    title1 = f"{ticker}{' - ' + name if name else ''}"
    title2 = f"Daily, last ~6 months  |  Last close {last_nice}: {lc_s}"
    kw = dict(type="candle", style=style, volume=True, figsize=(10, 11),
              panel_ratios=(7, 2),
              update_width_config=dict(candle_linewidth=1.6, candle_width=0.75,
                                       volume_width=0.75, volume_linewidth=0.8,
                                       line_width=2.6), ylabel="Price ($)" + (" - log" if use_log else ""),
              ylabel_lower="Volume", datetime_format="%b %d",
              xrotation=0, tight_layout=False, returnfig=True,
              scale_padding={"left": 0.6, "right": 1.0, "top": 1.2, "bottom": 1.0},
              warn_too_much_data=10000)
    if mavs:
        kw["mav"] = mavs
    if use_log:
        kw["yscale"] = "log"
    fig, axes = mpf.plot(df, **kw)
    r = fig.canvas.get_renderer()
    figw = fig.get_figwidth() * fig.dpi

    def _fit(t, maxfrac, minsize):
        # shrink a text until it fits maxfrac of the figure width
        while t.get_window_extent(renderer=r).width > maxfrac * figw and t.get_fontsize() > minsize:
            t.set_fontsize(t.get_fontsize() - 0.5)
        return t
    _fit(fig.text(0.5, 0.978, title1, color="white", fontsize=26, weight="bold",
                  ha="center", va="top"), 0.95, 14)
    _fit(fig.text(0.5, 0.936, title2, color="#d0d4dc", fontsize=19,
                  ha="center", va="top"), 0.95, 12)
    ax = axes[0]
    # Use the full canvas: price panel ~70% of the plot height, volume below,
    # room for the two-line title and caption.
    for a in axes[:2]:
        a.set_position([0.135, 0.325, 0.835, 0.565])
    for a in axes[2:4]:
        a.set_position([0.135, 0.105, 0.835, 0.2])
    vax = axes[2]
    from matplotlib.ticker import FuncFormatter as _FF
    vmax = float(df["Volume"].max())
    if vmax > 0:
        vax.set_ylim(0, vmax * 1.08)

    def _vfmt(v, _):
        if v >= 1e9: return f"{v/1e9:.1f}B"
        if v >= 1e6: return f"{v/1e6:.0f}M" if v >= 1e7 else f"{v/1e6:.1f}M"
        if v >= 1e3: return f"{v/1e3:.0f}K"
        return f"{v:.0f}"
    vax.yaxis.set_major_formatter(_FF(_vfmt))
    vax.yaxis.get_offset_text().set_visible(False)
    vax.set_ylabel("Volume")
    from matplotlib.ticker import MaxNLocator as _MNL
    vax.yaxis.set_major_locator(_MNL(nbins=3, min_n_ticks=2))
    vax.xaxis.set_major_locator(_MNL(nbins=6, integer=True))
    for a in axes[:4]:
        a.tick_params(labelsize=17, width=1.2, length=6)
    if use_log:
        from matplotlib.ticker import LogLocator, FuncFormatter
        ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
        ax.yaxis.set_minor_locator(LogLocator(base=10, subs=(1, 2, 3, 4, 5, 6, 7, 8, 9)))
        fmt = FuncFormatter(lambda v, _: f"${v:,.2f}" if v < 10 else f"${v:,.0f}")
        ax.yaxis.set_major_formatter(fmt)
    if mavs:
        from matplotlib.lines import Line2D
        cols = ["#ffb74d", "#42a5f5"]
        handles = [Line2D([], [], color=cols[i], lw=3.2, label=f"MA{m}")
                   for i, m in enumerate(mavs)]
        ax.legend(handles=handles, loc="best", fontsize=17,
                  facecolor="#1e222d", edgecolor="#3a3f4b", labelcolor="white")
    adj = "split-adjusted by Yahoo" if src.startswith("Yahoo") else "split-adjusted"
    cap = (f"Source: {src}, daily bars through {last_nice} "
           f"(prices {adj})"
           + ("  |  log price scale" if use_log else ""))
    ct = fig.text(0.02, 0.012, cap, color="#b0b4bc", fontsize=14, ha="left", va="bottom")
    if ct.get_window_extent(renderer=r).width > 0.96 * figw:
        # too wide for one line: break before "(prices" / "|", keep the text unchanged
        i = cap.find(" (prices")
        ct.set_text(cap[:i] + "\n" + cap[i + 1:] if i > 0 else cap)
        ct.set_linespacing(1.3)
        _fit(ct, 0.96, 9)
    fig.savefig(out, dpi=100, facecolor=fig.get_facecolor())
    print(out)
    print(f"source={src} bars={len(df)} first={df.index[0]:%Y-%m-%d} last={last_s} "
          f"low={lo:.4g} high={hi:.4g} log={use_log} mav={list(mavs)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
