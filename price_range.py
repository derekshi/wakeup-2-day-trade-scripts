#!/usr/bin/env python3
"""Speculative price-range image for a runner quick analysis (runner-quick-analysis).

Usage: python3 price_range.py --json price_range.json [out.png]
Renders a mobile-first dark-theme speculative price-range card (~800x1100
logical px, saved at 2x) AFTER the analyst has written the valuation block.
Same visual language as scorecard.py (BG/PANEL/LINE/TXT/SUB/DIM, LEVEL_COLORS).
Layout, top to bottom: title, ticker/company + reference price, three mini
summary columns (Bear/Base/Bull), horizontal range bar with price marker and
optional momentum band, four scenario cards (Bear/Base/Bull/Momentum), footer.
Never crashes on bad or missing fields; emoji are stripped; parse_math=False.

price_range.json keys (all optional except ticker; missing values show "n/a"):
  ticker, company, price, as_of, date (YYYY-MM-DD), ah_price,
  bear / base / bull / momentum: {"lo", "hi", "mkt_cap", "driver"}
    (flat keys like bear_lo / bear_hi also accepted),
  shares_note, source (default "Wakeup 2 Day Trade quick analysis").
If momentum is missing or marked n/a, the orange band is hidden and the
Momentum card reads "n/a · not a low-float runner".
Default output: /workspace/runner-charts/<TICKER>-range-<date>.png
"""
import json, os, re, sys, time
from datetime import date

T0 = time.time()
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle

W, H = 800, 1120          # logical px; saved at 2x
BG, PANEL, LINE = "#0f131a", "#161b24", "#2a3140"
TXT, SUB, DIM = "#f2f4f8", "#9aa3b2", "#5d6675"
BEAR_C, BASE_C, BULL_C, MOM_C = "#ef4444", "#eab308", "#22c55e", "#f97316"
BEAR_T, BASE_T, BULL_T, MOM_T = "#3a1515", "#332b0c", "#12301f", "#3a2210"
NA = "n/a"
M = 36

EMOJI_RE = re.compile("[\U0001F000-\U0001FFFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]")


def clean(v, default=NA):
    if v is None:
        return default
    if isinstance(v, float):
        v = f"{v:g}"
    s = EMOJI_RE.sub("", str(v)).replace("\\$", "$")
    s = re.sub(r"\s+", " ", s).strip(" ·|-")
    return s if s and s.lower() not in ("none", "null", "nan") else default


def num(v):
    try:
        if isinstance(v, (int, float)):
            return float(v)
        s = clean(v, "").replace("\u2212", "-").replace("$", "").replace(",", "").replace("%", "").strip()
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


def fmt_price(v):
    n = num(v)
    if n is None:
        return clean(v)
    if abs(n) >= 100:
        return f"${n:,.0f}" if n == int(n) else f"${n:,.2f}"
    if abs(n) >= 1:
        # whole dollars look cleaner without .00 when exact
        return f"${n:,.0f}" if float(n) == int(n) else f"${n:,.2f}"
    return f"${n:,.4f}"


def fmt_range(lo, hi):
    a, b = fmt_price(lo), fmt_price(hi)
    if a == NA and b == NA:
        return NA
    if a == NA:
        return b
    if b == NA:
        return a
    return f"{a} - {b}"


def scenario(d, key):
    """Normalize nested or flat scenario keys into {lo, hi, mkt_cap, driver, na}."""
    nested = d.get(key)
    out = {"lo": None, "hi": None, "mkt_cap": None, "driver": None, "na": False}
    if isinstance(nested, dict):
        out["lo"] = num(nested.get("lo") if nested.get("lo") is not None else nested.get("low"))
        out["hi"] = num(nested.get("hi") if nested.get("hi") is not None else nested.get("high"))
        out["mkt_cap"] = clean(nested.get("mkt_cap") or nested.get("market_cap"), "")
        out["driver"] = clean(nested.get("driver") or nested.get("what") or nested.get("text"), "")
        raw = clean(nested.get("status") or nested.get("label") or "", "")
        if raw.lower() in ("n/a", "na") or "not a low-float" in (out["driver"] or "").lower():
            out["na"] = True
    elif isinstance(nested, str):
        s = clean(nested, "")
        if s.lower() in ("n/a", "na") or "not a low-float" in s.lower():
            out["na"] = True
            out["driver"] = s or "n/a · not a low-float runner"
    # flat keys
    if out["lo"] is None:
        out["lo"] = num(d.get(f"{key}_lo") or d.get(f"{key}_low"))
    if out["hi"] is None:
        out["hi"] = num(d.get(f"{key}_hi") or d.get(f"{key}_high"))
    if not out["mkt_cap"]:
        out["mkt_cap"] = clean(d.get(f"{key}_mkt_cap") or d.get(f"{key}_market_cap"), "")
    if not out["driver"]:
        out["driver"] = clean(d.get(f"{key}_driver") or d.get(f"{key}_text"), "")
    # momentum absent entirely → n/a
    if key == "momentum" and out["lo"] is None and out["hi"] is None and not out["driver"]:
        out["na"] = True
        out["driver"] = "n/a · not a low-float runner"
    if key == "momentum" and out["driver"] and "not a low-float" in out["driver"].lower():
        out["na"] = True
    return out


def mid(lo, hi):
    if lo is None and hi is None:
        return None
    if lo is None:
        return hi
    if hi is None:
        return lo
    return (lo + hi) / 2.0


def pct_vs(ref, lo, hi):
    m = mid(lo, hi)
    if ref is None or m is None or ref == 0:
        return None
    return (m - ref) / abs(ref) * 100.0


def fmt_pct(p):
    if p is None:
        return NA
    sign = "+" if p > 0 else "−" if p < 0 else ""
    return f"{sign}{abs(p):.0f}% vs ref"


class Card:
    def __init__(self, height=H):
        self.H = height
        self.fig = plt.figure(figsize=(W / 100, height / 100), dpi=100, facecolor=BG)
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.set_xlim(0, W); self.ax.set_ylim(height, 0); self.ax.axis("off")
        self.r = self.fig.canvas.get_renderer()
        self.family = "DejaVu Sans"

    def width(self, t):
        return t.get_window_extent(renderer=self.r).width

    def text(self, x, y, s, size, color=TXT, weight="normal", ha="left", va="baseline",
             maxw=None, minsize=8, style="normal"):
        t = self.ax.text(x, y, s, fontsize=size, color=color, weight=weight, ha=ha, va=va,
                         family=self.family, style=style, parse_math=False)
        if maxw:
            while self.width(t) > maxw and size > minsize:
                size -= 0.5; t.set_fontsize(size)
            if self.width(t) > maxw:
                while len(s) > 4 and self.width(t) > maxw:
                    s = s[:-2]; t.set_text(s.rstrip() + "…")
        return t

    def wrap(self, s, size, maxw, maxlines=None, weight="bold", style="normal",
             prefer_breaks=True):
        probe = self.ax.text(0, 0, "", fontsize=size, family=self.family, weight=weight,
                             style=style, parse_math=False)
        def fits(x):
            probe.set_text(x); return self.width(probe) <= maxw
        if prefer_breaks:
            chunks = [p.strip() for p in re.split(r"\s+·\s+|;\s*", s) if p.strip()] or [s]
            joiner = " · "
        else:
            chunks = [s]
            joiner = " "
        lines, cur, ok = [], "", True
        for ch in chunks:
            cand = f"{cur}{joiner}{ch}" if cur else ch
            if fits(cand):
                cur = cand; continue
            if cur:
                lines.append(cur)
            cur = ""
            for w in ch.split():
                cand = f"{cur} {w}".strip()
                if fits(cand) or not cur:
                    cur = cand
                    if not fits(cand):
                        ok = False
                else:
                    lines.append(cur); cur = w
        if cur:
            lines.append(cur)
        probe.remove()
        if maxlines and len(lines) > maxlines:
            ok = False
        return lines, ok

    def fit_block(self, s, sizes, maxw, maxlines, weight="bold", style="normal",
                  prefer_breaks=True):
        for sz in sizes:
            lines, ok = self.wrap(s, sz, maxw, maxlines, weight, style, prefer_breaks)
            if ok:
                return sz, lines
        sz = sizes[-1]
        lines, _ = self.wrap(s, sz, maxw, None, weight, style, prefer_breaks)
        lines = lines[:maxlines]
        if len(lines) == maxlines:
            lines[-1] = lines[-1].rstrip(" ·,;") + " …"
        return sz, lines

    def rbox(self, x, y, w, h, color, r=8, alpha=1.0, ec="none", lw=0):
        p = FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
                           fc=color, ec=ec, lw=lw, alpha=alpha, mutation_aspect=1)
        self.ax.add_patch(p)
        return p


PT = 100 / 72


def render(d, out):
    bear = scenario(d, "bear")
    base = scenario(d, "base")
    bull = scenario(d, "bull")
    mom = scenario(d, "momentum")

    # Estimate height from driver lengths so nothing clips
    drivers = [bear.get("driver") or "", base.get("driver") or "",
               bull.get("driver") or "", mom.get("driver") or ""]
    long_drv = sum(1 for s in drivers if len(s) > 70)
    height = 1120 + long_drv * 18
    if clean(d.get("shares_note"), ""):
        height += 28
    c = Card(height)
    ax = c.ax
    Hloc = c.H
    c.rbox(10, 10, W - 20, Hloc - 20, PANEL, r=22, ec=LINE, lw=1.2)

    ticker = clean(d.get("ticker"), "?").upper().lstrip("$")
    company = clean(d.get("company") or d.get("company_name") or d.get("name"))
    ref = num(d.get("price") if d.get("price") is not None else d.get("ref_price") or d.get("reference"))
    price_s = fmt_price(ref) if ref is not None else clean(d.get("price"))
    as_of = clean(d.get("as_of"))
    shares_note = clean(d.get("shares_note"), "")
    src = clean(d.get("source"), "Wakeup 2 Day Trade quick analysis")
    dt = clean(d.get("date"), "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dt):
        dt = date.today().isoformat()

    # ---- title
    c.text(M, 48, "SPECULATIVE PRICE RANGE", 13, color=DIM, weight="bold")

    # ---- header: ticker/company left, price/as_of right
    t_pr = c.text(W - M, 100, price_s, 42, weight="bold", ha="right", maxw=280, minsize=24)
    c.text(M, 100, ticker, 48, weight="bold",
           maxw=W - 2 * M - c.width(t_pr) - 24, minsize=28)
    # company under ticker
    cw = W - 2 * M - c.width(t_pr) - 40
    sz, lines = c.fit_block(company, [18, 16, 14, 12], cw, 2, weight="normal")
    for j, ln in enumerate(lines):
        c.text(M, 130 + j * (sz * PT * 1.15), ln, sz, color=SUB)
    # as_of under price
    asof_label = f"reference price · {as_of}" if as_of != NA else "reference price"
    c.text(W - M, 138, asof_label, 12, color=SUB, ha="right",
           maxw=W / 2 - M, minsize=9)

    # ---- three mini summary columns
    y_sum = 178
    col_w = (W - 2 * M - 24) / 3
    summaries = [
        ("BEAR", bear, BEAR_C),
        ("BASE", base, BASE_C),
        ("BULL", bull, BULL_C),
    ]
    for i, (lab, sc, col) in enumerate(summaries):
        x = M + i * (col_w + 12)
        c.text(x + col_w / 2, y_sum, lab, 13, color=col, weight="bold", ha="center")
        rng = fmt_range(sc["lo"], sc["hi"])
        c.text(x + col_w / 2, y_sum + 28, rng, 20, color=TXT, weight="bold",
               ha="center", maxw=col_w - 8, minsize=12)
        p = pct_vs(ref, sc["lo"], sc["hi"])
        c.text(x + col_w / 2, y_sum + 54, fmt_pct(p), 13, color=col, weight="bold",
               ha="center", maxw=col_w - 4, minsize=10)

    # ---- horizontal range bar
    # Domain: min bear.lo .. max bull.hi (include momentum / ref if wider)
    vals = [v for v in (bear["lo"], bear["hi"], base["lo"], base["hi"],
                        bull["lo"], bull["hi"]) if v is not None]
    if not mom["na"]:
        vals += [v for v in (mom["lo"], mom["hi"]) if v is not None]
    if ref is not None:
        vals.append(ref)
    if not vals:
        vals = [0.0, 1.0]
    pmin, pmax = min(vals), max(vals)
    if pmax <= pmin:
        pmax = pmin + 1.0
    pad = (pmax - pmin) * 0.04
    pmin -= pad; pmax += pad
    span = pmax - pmin

    # Main price bar; leave room above for a separate momentum track + price pill
    bar_x, bar_y, bar_w, bar_h = M, 318, W - 2 * M, 28
    mom_h = 14
    mom_gap = 8  # air between momentum track and main bar
    mom_y = bar_y - mom_gap - mom_h

    def x_at(p):
        return bar_x + (p - pmin) / span * bar_w

    # Segment boundaries prefer bear.lo, bear.hi/base.lo, base.hi/bull.lo, bull.hi
    b0 = bear["lo"] if bear["lo"] is not None else pmin + pad
    b1 = bear["hi"] if bear["hi"] is not None else (base["lo"] if base["lo"] is not None else b0)
    b2 = base["hi"] if base["hi"] is not None else (bull["lo"] if bull["lo"] is not None else b1)
    b3 = bull["hi"] if bull["hi"] is not None else pmax - pad
    # Ensure monotonic
    segs = sorted([b0, b1, b2, b3])
    b0, b1, b2, b3 = segs[0], segs[1], segs[2], segs[3]
    colors = [BEAR_C, BASE_C, BULL_C]
    bounds = [b0, b1, b2, b3]
    # Draw track background
    c.rbox(bar_x, bar_y, bar_w, bar_h, "#1d2330", r=10)
    for i in range(3):
        x0, x1 = x_at(bounds[i]), x_at(bounds[i + 1])
        if x1 - x0 < 1:
            continue
        # clip rounded ends only on outer segments via simple rects inside track
        ax.add_patch(Rectangle((x0, bar_y), x1 - x0, bar_h, fc=colors[i], ec="none",
                               alpha=0.95, zorder=2))
    # Soft rounded overlay to clip corners visually
    c.rbox(bar_x, bar_y, bar_w, bar_h, PANEL, r=10, alpha=0.0, ec=LINE, lw=1.2)

    # Tick labels at boundaries
    tick_y = bar_y + bar_h + 22
    for p in bounds:
        xx = x_at(p)
        ax.plot([xx, xx], [bar_y + bar_h, bar_y + bar_h + 6], color=DIM, lw=1.0, zorder=3)
        c.text(xx, tick_y, fmt_price(p), 11, color=SUB, ha="center", weight="bold")

    # Momentum track sits ON TOP of the price bar (separate row, no overlap)
    if not mom["na"] and mom["lo"] is not None and mom["hi"] is not None:
        # faint full-width rail so the track reads as its own row
        c.rbox(bar_x, mom_y, bar_w, mom_h, "#1d2330", r=7, alpha=0.9, ec=LINE, lw=0.8)
        mx0, mx1 = x_at(min(mom["lo"], mom["hi"])), x_at(max(mom["lo"], mom["hi"]))
        mw = max(mx1 - mx0, 8)
        c.rbox(mx0, mom_y, mw, mom_h, MOM_C, r=7, alpha=0.85, ec=MOM_C, lw=1.0)
        mom_lab = f"MOMENTUM {fmt_range(mom['lo'], mom['hi'])}"
        probe = c.ax.text(0, 0, mom_lab, fontsize=11, family=c.family, weight="bold",
                          parse_math=False)
        lab_w = c.width(probe); probe.remove()
        lab_y = mom_y + mom_h / 2 + 4
        if mx1 + 12 + lab_w <= W - M:
            c.text(mx1 + 12, lab_y, mom_lab, 11, color=MOM_C, weight="bold", va="center")
        else:
            # label above the momentum track, right-aligned to the band
            c.text(min(mx1, W - M), mom_y - 10, mom_lab, 11, color=MOM_C,
                   weight="bold", ha="right", maxw=min(mw + 120, W - 2 * M), minsize=9)

    # Price marker pill + stem through momentum track down into the main bar
    if ref is not None:
        rx = x_at(ref)
        rx = max(bar_x + 4, min(bar_x + bar_w - 4, rx))
        stem_top = mom_y - 18 if (not mom["na"] and mom["lo"] is not None) else bar_y - 22
        # glow stem
        ax.plot([rx, rx], [stem_top, bar_y + bar_h + 8], color="#ffffff", lw=5.0,
                alpha=0.28, solid_capstyle="round", zorder=4)
        ax.plot([rx, rx], [stem_top + 2, bar_y + bar_h + 6], color="#ffffff", lw=2.4,
                solid_capstyle="round", zorder=5)
        # pill
        pill = fmt_price(ref)
        # measure pill width
        probe = c.ax.text(0, 0, pill, fontsize=13, family=c.family, weight="bold",
                          parse_math=False)
        pw = c.width(probe) + 22
        probe.remove()
        ph = 26
        px = rx - pw / 2
        px = max(bar_x - 4, min(bar_x + bar_w - pw + 4, px))
        py = stem_top - 28
        # soft glow behind pill
        c.rbox(px - 2, py - 2, pw + 4, ph + 4, "#ffffff", r=14, alpha=0.18)
        c.rbox(px, py, pw, ph, "#ffffff", r=12)
        c.text(px + pw / 2, py + ph / 2 + 5, pill, 13, color=BG, weight="bold",
               ha="center", va="center")

    # Optional shares_note under the bar
    y_after = tick_y + 18
    if shares_note:
        c.text(M, y_after + 8, shares_note, 12, color=DIM, style="italic",
               maxw=W - 2 * M, minsize=9)
        y_after += 28

    # ---- four scenario cards
    cards = [
        ("BEAR", bear, BEAR_C, BEAR_T),
        ("BASE", base, BASE_C, BASE_T),
        ("BULL", bull, BULL_C, BULL_T),
        ("MOMENTUM", mom, MOM_C, MOM_T),
    ]
    card_x, card_w = 22, W - 44
    y = y_after + 20
    # Leave room for footer (~70 px)
    footer_top = Hloc - 70
    avail = footer_top - y - 12
    n = 4
    gap = 10
    th = max(88, (avail - (n - 1) * gap) / n)

    for lab, sc, col, tint in cards:
        tile = c.rbox(card_x, y, card_w, th, tint, r=16, ec=LINE, lw=1.0)
        # accent bar clipped to tile
        bar = Rectangle((card_x, y), 12, th, fc=col, ec="none")
        ax.add_patch(bar)
        try:
            bar.set_clip_path(tile)
        except Exception:
            pass
        ix = card_x + 28
        c.text(ix, y + 26, lab, 14, color=col, weight="bold")
        if sc.get("na") and lab == "MOMENTUM":
            rng = "n/a"
            drv = sc.get("driver") or "n/a · not a low-float runner"
            mcap = ""
        else:
            rng = fmt_range(sc["lo"], sc["hi"])
            drv = sc.get("driver") or NA
            mcap = sc.get("mkt_cap") or ""
        # price range (and optional mkt cap) on the right of the label row
        right = f"{rng}" + (f"  ·  {mcap}" if mcap else "")
        c.text(W - M - 8, y + 26, right, 15, color=TXT, weight="bold", ha="right",
               maxw=card_w - 140, minsize=11)
        # driver text
        dw = card_w - 44
        sz, dlines = c.fit_block(drv, [14, 13, 12, 11], dw, 3 if th >= 100 else 2,
                                 weight="normal", prefer_breaks=False)
        lh = sz * PT * 1.2
        base_y = y + 48
        for j, ln in enumerate(dlines):
            c.text(ix, base_y + j * lh, ln, sz, color=SUB)
        y += th + gap

    # ---- footer
    fy = Hloc - 28
    ax.plot([M, W - M], [fy - 28, fy - 28], color=LINE, lw=1.2)
    c.text(M, fy - 10, "Speculative research ranges — not a recommendation, not a price target.",
           11.5, color=SUB, style="italic", maxw=W - 2 * M, minsize=9)
    c.text(M, fy + 10, f"Source: {src} · {dt}", 11.5, color=DIM,
           maxw=W - 2 * M, minsize=9)

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    c.fig.savefig(out, dpi=200, facecolor=BG)
    plt.close(c.fig)


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help") or "--json" not in argv:
        print(__doc__); sys.exit(0 if argv and argv[0] in ("-h", "--help") else 1)
    i = argv.index("--json")
    path = argv[i + 1] if i + 1 < len(argv) else None
    rest = argv[:i] + argv[i + 2:]
    try:
        d = json.load(open(path, encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("price_range.json must be an object")
    except Exception as e:
        print(f"price_range.py: cannot read {path}: {e}", file=sys.stderr); sys.exit(2)
    tk = re.sub(r"[^A-Z0-9.\-]", "", clean(d.get("ticker"), "RANGE").upper()) or "RANGE"
    dt = clean(d.get("date"), "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dt):
        dt = date.today().isoformat()
    out = rest[0] if rest else f"/workspace/runner-charts/{tk}-range-{dt}.png"
    try:
        render(d, out)
    except Exception as e:
        print(f"price_range.py: render failed: {type(e).__name__}: {e}", file=sys.stderr); sys.exit(3)
    print(out)
    print(f"price_range.py: rendered in {time.time() - T0:.2f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
